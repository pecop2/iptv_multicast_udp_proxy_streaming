"""Picking one variant of an HLS master playlist.

Given a master playlist, ffmpeg's HLS demuxer exposes every variant (quality level)
as a separate program, and mapping all streams would make it download every variant
at once. Mapping the program of the best variant downloads only that one.
"""

import re
from dataclasses import dataclass

import requests

FETCH_TIMEOUT = (10.0, 10.0)  # (connect, read) seconds

_STREAM_INF = "#EXT-X-STREAM-INF:"
_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


@dataclass(frozen=True, slots=True)
class Variant:
    """One `#EXT-X-STREAM-INF` entry of a master playlist."""

    program: int  # ffmpeg numbers the variant programs 0, 1, 2, ... in playlist order
    bandwidth: int
    uri: str


def parse_variants(text: str) -> list[Variant]:
    """Return the variants of a master playlist (empty for a media playlist).

    Mirrors libavformat/hls.c: every `#EXT-X-STREAM-INF` tag followed by a URI line
    is the next variant; other `#` lines, such as I-frame playlists, are skipped.
    """
    variants: list[Variant] = []
    pending_bandwidth: int | None = None
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        if line.startswith(_STREAM_INF):
            pending_bandwidth = _bandwidth(line.removeprefix(_STREAM_INF))
        elif line and not line.startswith("#") and pending_bandwidth is not None:
            variants.append(Variant(len(variants), pending_bandwidth, line))
            pending_bandwidth = None
    return variants


def best_variant(variants: list[Variant]) -> Variant | None:
    """The highest-bandwidth variant (the first one on ties), if any."""
    return max(variants, key=lambda variant: variant.bandwidth, default=None)


def fetch_playlist(url: str, *, user_agent: str) -> str:
    """Download a playlist; raises requests.RequestException on failure."""
    response = requests.get(url, headers={"User-Agent": user_agent}, timeout=FETCH_TIMEOUT)
    response.raise_for_status()
    return response.content.decode("utf-8-sig", errors="replace")


def _bandwidth(attributes: str) -> int:
    for key, value in _ATTRIBUTE.findall(attributes):
        if key == "BANDWIDTH":
            digits = re.match(r"\d+", value.strip('"'))
            return int(digits.group()) if digits else 0
    return 0
