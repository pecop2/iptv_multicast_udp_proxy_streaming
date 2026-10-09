"""The ffmpeg command line that restreams one channel to its multicast group.

ffmpeg copies the provider's audio/video untouched (no transcoding) into MPEG-TS and
sends it to the channel's UDP multicast group in 1316-byte datagrams (7 TS packets,
the usual size for MPEG-TS over UDP, so no IP fragmentation).

How the source is read depends on its kind, decided from the URL:

* live MPEG-TS (`.ts`, no extension, or anything unrecognised -- the usual IPTV
  case): the provider already sends in real time, so the stream is not paced again
  (pacing would also drift against the provider's clock over long sessions). All
  usable video and audio tracks plus subtitle tracks (DVB subtitles, teletext) are
  kept, like VLC's `sout-all` did.
* HLS (`.m3u8`): segments arrive in bursts, so reading is paced to real time
  (`-re`). For a master playlist only the best variant is mapped. HLS subtitles are
  WebVTT, which MPEG-TS cannot carry, so they are left out.
* media files (`.mp4`, `.mkv`, ..., or anything under `/movie/` or `/series/` --
  VOD entries): downloaded as fast as the network allows unless paced, so they are
  paced too (`-re`). Their text subtitle formats cannot be carried in MPEG-TS
  either. A finite `.ts` file elsewhere is indistinguishable from a live stream by
  its URL and is not paced.

Two ffmpeg 9 behaviours shape these choices, both measured against real streams:

* `-re` paces by the slowest input stream, so a sparse subtitle track makes it
  alternate between bursts and multi-second stalls. Subtitles are therefore only
  mapped for live MPEG-TS, which is not paced.
* With a sparse subtitle track the muxer holds output until every track has a
  packet (up to 10 s by default); `-max_interleave_delta` caps that wait.

Tracks are selected with the `u` ("usable") stream specifier: a channel that
declares an audio track without currently sending it (e.g. an idle audio
description track) would otherwise make ffmpeg refuse to start. The trailing `:?`
lets a map match nothing (e.g. no video on radio channels).
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import requests

from . import hls
from .redaction import redact

log = logging.getLogger(__name__)

UDP_PACKET_SIZE = 7 * 188
# VLC sent with the operating system default multicast TTL, which is 1 on Linux:
# the streams never leave the local network segment.
MULTICAST_TTL = 1
MAX_INTERLEAVE_DELTA_US = 500_000
# The stream selection below ("0:v:u:?") needs the stream specifier parser of
# ffmpeg 7.1. Older versions reject it, fall back to one video and one audio track
# and silently drop the other audio languages and subtitles.
MINIMUM_VERSION = (7, 1)

MEDIA_FILE_EXTENSIONS = frozenset(
    {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".mpg", ".mpeg", ".vob"}
)
# Xtream Codes-style panels (what most single-connection IPTV providers run) serve
# VOD under /movie/ and /series/ -- sometimes as .ts files, which must be paced too.
VOD_PATH_SEGMENTS = frozenset({"movie", "series"})


class SourceKind(StrEnum):
    LIVE_TS = "live MPEG-TS"
    HLS = "HLS"
    MEDIA_FILE = "media file"


def parse_version(version_line: str) -> tuple[int, int] | None:
    """(major, minor) from the first line of `ffmpeg -version`.

    None when the line has no release number, as with builds from git master
    ("ffmpeg version N-118123-g...").
    """
    match = re.match(r"ffmpeg version n?(\d+)\.(\d+)", version_line)
    return (int(match[1]), int(match[2])) if match else None


def classify_source(url: str) -> SourceKind:
    """Decide how to read a channel from its URL path."""
    path = PurePosixPath(urlsplit(url).path)
    suffix = path.suffix.lower()
    if suffix == ".m3u8":
        return SourceKind.HLS
    directories = {part.lower() for part in path.parent.parts}
    if suffix in MEDIA_FILE_EXTENSIONS or directories & VOD_PATH_SEGMENTS:
        return SourceKind.MEDIA_FILE
    return SourceKind.LIVE_TS


def stream_maps(kind: SourceKind, hls_program: int | None = None) -> list[str]:
    """The `-map` options selecting the tracks to restream."""
    prefix = f"0:p:{hls_program}:" if hls_program is not None else "0:"
    maps = ["-map", f"{prefix}v:u:?", "-map", f"{prefix}a:u:?"]
    if kind is SourceKind.LIVE_TS:
        maps += ["-map", "0:s?"]
    return maps


def build_command(
    *,
    ffmpeg_path: str,
    source_url: str,
    group: str,
    port: int,
    user_agent: str,
    kind: SourceKind,
    hls_program: int | None = None,
) -> list[str]:
    """The full ffmpeg argument list for restreaming `source_url` to `group:port`."""
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel", "level+warning",
        # Input (HTTP) options: present the same request VLC did -- its
        # User-Agent and no "Icy-MetaData" header.
        "-user_agent", user_agent,
        "-icy", "0",
    ]  # fmt: skip
    if kind is not SourceKind.LIVE_TS:
        command.append("-re")
    command += ["-i", source_url]
    command += stream_maps(kind, hls_program)
    command += ["-c", "copy"]
    if kind is SourceKind.LIVE_TS:
        command += ["-max_interleave_delta", str(MAX_INTERLEAVE_DELTA_US)]
    command += [
        # Viewers stay connected while ffmpeg restarts. Marking the first packets
        # as discontinuous tells players that the reset of timestamps and
        # continuity counters is intended rather than corruption.
        "-mpegts_flags", "+initial_discontinuity",
        "-f", "mpegts",
        f"udp://{group}:{port}?pkt_size={UDP_PACKET_SIZE}&ttl={MULTICAST_TTL}",
    ]  # fmt: skip
    return command


@dataclass(frozen=True, slots=True)
class CommandFactory:
    """Builds the ffmpeg command for a channel each time ffmpeg is (re)started.

    For HLS the master playlist is inspected on every start, so a changed variant
    list is picked up after a restart.
    """

    ffmpeg_path: str
    user_agent: str
    multicast_port: int
    fetch_playlist: Callable[[str], str]

    def __call__(self, group: str, source_url: str) -> list[str]:
        kind = classify_source(source_url)
        hls_program = self._best_hls_program(group, source_url) if kind is SourceKind.HLS else None
        log.info(
            "%s: restreaming %s source%s.",
            group,
            kind,
            f" (variant {hls_program} of the master playlist)" if hls_program is not None else "",
        )
        return build_command(
            ffmpeg_path=self.ffmpeg_path,
            source_url=source_url,
            group=group,
            port=self.multicast_port,
            user_agent=self.user_agent,
            kind=kind,
            hls_program=hls_program,
        )

    def _best_hls_program(self, group: str, url: str) -> int | None:
        """The ffmpeg program of the best variant, or None to map every stream.

        None for media playlists and single-variant masters (nothing to choose), and
        when the playlist cannot be fetched: ffmpeg may still manage to open it, at
        worst downloading every variant instead of one.
        """
        try:
            playlist = self.fetch_playlist(url)
        except requests.RequestException as error:
            log.warning(
                "%s: could not inspect the HLS playlist, mapping all variants: %s",
                group,
                redact(str(error), url),
            )
            return None
        variants = hls.parse_variants(playlist)
        if len(variants) < 2:
            return None
        best = hls.best_variant(variants)
        return best.program if best is not None else None
