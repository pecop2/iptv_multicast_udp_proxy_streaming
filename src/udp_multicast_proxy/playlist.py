"""The provider's M3U playlist and the rewritten playlist served to players."""

import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import requests

from .redaction import redact, url_origin

log = logging.getLogger(__name__)

# Some providers only hand the playlist to browser-like clients; this is the exact
# User-Agent the VLC-based version used for the download.
PLAYLIST_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36"
)
DOWNLOAD_TIMEOUT = (10.0, 60.0)  # (connect, read) seconds

# Path prefix of the per-channel proxy URLs. The transport is no longer RTP, but the
# prefix is kept so channel URLs already stored in IPTV apps (favourites, EPG
# mappings) stay valid.
STREAM_PATH_PREFIX = "/rtp/"


class PlaylistError(Exception):
    """Raised when the provider's playlist cannot be fetched or used."""


@dataclass(frozen=True, slots=True)
class PlaylistEntry:
    """One channel of the provider's playlist."""

    extinf: str  # the full "#EXTINF..." line, kept verbatim (name, logo, group, EPG id)
    url: str


@dataclass(frozen=True, slots=True)
class ProxyPlaylist:
    """The rewritten playlist and the multicast group assigned to each channel."""

    m3u: str
    channels: Mapping[str, str]  # multicast group -> upstream URL


def parse_m3u(text: str) -> list[PlaylistEntry]:
    """Return the channels of an M3U playlist, in playlist order.

    Each `#EXTINF` line is paired with the next line that is neither blank nor a
    `#` directive (such as `#EXTGRP` or `#EXTVLCOPT`). Everything else is ignored.
    """
    entries: list[PlaylistEntry] = []
    pending_extinf: str | None = None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if line.startswith("#EXTINF"):
            pending_extinf = line
        elif pending_extinf is not None and stripped and not stripped.startswith("#"):
            entries.append(PlaylistEntry(extinf=pending_extinf, url=stripped))
            pending_extinf = None
    return entries


def multicast_groups() -> Iterator[str]:
    """Yield the multicast group for each channel, in playlist order.

    239.123.1.1, 239.123.1.2, ..., 239.123.1.254, 239.123.2.1, ..., 239.123.254.254,
    239.124.1.1, ... -- the same assignment as the VLC-based version, so a channel
    keeps its URL as long as its position in the provider's playlist is unchanged.
    """
    for second in range(123, 256):
        for third in range(1, 255):
            for fourth in range(1, 255):
                yield f"239.{second}.{third}.{fourth}"


def build_proxy_playlist(
    entries: Sequence[PlaylistEntry], *, host_ip: str, proxy_port: int, multicast_port: int
) -> ProxyPlaylist:
    """Rewrite every channel URL to point at the HTTP stream proxy."""
    lines = ["#EXTM3U"]
    channels: dict[str, str] = {}
    groups = multicast_groups()
    for entry in entries:
        group = next(groups, None)
        if group is None:
            raise PlaylistError(f"Too many channels ({len(entries)}) to assign multicast groups.")
        channels[group] = entry.url
        lines.append(entry.extinf)
        lines.append(f"http://{host_ip}:{proxy_port}{STREAM_PATH_PREFIX}{group}:{multicast_port}")
    return ProxyPlaylist(m3u="\n".join(lines) + "\n", channels=MappingProxyType(channels))


def download_m3u(url: str) -> str:
    """Download the provider's playlist and decode it as UTF-8."""
    try:
        response = requests.get(
            url, headers={"User-Agent": PLAYLIST_USER_AGENT}, timeout=DOWNLOAD_TIMEOUT
        )
    except requests.RequestException as error:
        raise PlaylistError(
            f"Downloading from {url_origin(url)} failed: {redact(str(error), url)}"
        ) from error
    if response.status_code != requests.codes.ok:
        raise PlaylistError(
            f"Downloading from {url_origin(url)} failed: HTTP {response.status_code}."
        )
    return response.content.decode("utf-8-sig", errors="replace")


def write_text_atomically(path: Path, text: str) -> None:
    """Replace `path` with `text` so readers never observe a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as temp_file:
            temp_file.write(text)
        Path(temp_name).chmod(0o644)
        Path(temp_name).replace(path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


class ChannelPlaylist:
    """Keeps the served playlist file and the channel lookup table up to date.

    The provider playlists are merged in the order given; multicast groups are
    assigned across the merged list, so the channels of the first playlist keep
    the same URLs as when it is the only one.
    """

    def __init__(
        self,
        *,
        source_urls: Sequence[str],
        output_path: Path,
        host_ip: str,
        proxy_port: int,
        multicast_port: int,
        download: Callable[[str], str] = download_m3u,
    ) -> None:
        self._source_urls = tuple(source_urls)
        self._output_path = output_path
        self._host_ip = host_ip
        self._proxy_port = proxy_port
        self._multicast_port = multicast_port
        self._download = download
        # Replaced wholesale on refresh; reading a single attribute is atomic, so
        # request threads always see either the old or the new mapping.
        self._channels: Mapping[str, str] = MappingProxyType({})
        self._updated_at: float | None = None

    @property
    def channel_count(self) -> int:
        return len(self._channels)

    @property
    def updated_at(self) -> float | None:
        """`time.monotonic()` of the last successful refresh."""
        return self._updated_at

    def upstream_url(self, group: str) -> str | None:
        """The provider URL of the channel assigned to `group`, if any."""
        return self._channels.get(group)

    def refresh(self) -> int:
        """Download, merge, rewrite and publish the playlists; return the channel count.

        Nothing is published unless every playlist downloads and has channels: if
        one is missing, the previous playlist stays in place (so no channel moves to
        another URL) and PlaylistError is raised.
        """
        entries: list[PlaylistEntry] = []
        counts: list[int] = []
        for number, url in enumerate(self._source_urls, start=1):
            name = self._name(number)
            log.info("Downloading the %s from %s...", name, url_origin(url))
            try:
                found = parse_m3u(self._download(url))
            except PlaylistError as error:
                raise PlaylistError(f"{name.capitalize()}: {error}") from error
            if not found:
                raise PlaylistError(
                    f"{name.capitalize()} ({url_origin(url)}) does not contain any channels."
                )
            entries += found
            counts.append(len(found))
        playlist = build_proxy_playlist(
            entries,
            host_ip=self._host_ip,
            proxy_port=self._proxy_port,
            multicast_port=self._multicast_port,
        )
        try:
            write_text_atomically(self._output_path, playlist.m3u)
        except OSError as error:
            raise PlaylistError(f"Writing {self._output_path} failed: {error}") from error
        self._channels = playlist.channels
        self._updated_at = time.monotonic()
        breakdown = f" ({' + '.join(map(str, counts))})" if len(counts) > 1 else ""
        log.info("Playlist updated: %d channels%s.", len(entries), breakdown)
        return len(entries)

    def _name(self, number: int) -> str:
        total = len(self._source_urls)
        return "original playlist" if total == 1 else f"original playlist {number} of {total}"
