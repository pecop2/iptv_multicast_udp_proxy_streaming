"""End to end: provider -> ffmpeg -> multicast -> HTTP proxy -> viewer.

Each test plays a channel through the proxy the way an IPTV app does and checks
what the viewer receives and what the provider saw.
"""

import threading
from collections import Counter
from pathlib import Path

import pytest

from udp_multicast_proxy.config import DEFAULT_UPSTREAM_USER_AGENT

from .support import (
    DVB_SUBTITLE_SAMPLE,
    Provider,
    Proxy,
    Viewing,
    probe_streams,
    wait_until,
    watch,
)

pytestmark = pytest.mark.integration


def codecs(viewing: Viewing, tmp_path: Path) -> list[str]:
    capture = tmp_path / "capture.ts"
    capture.write_bytes(viewing.data)
    return sorted(stream["codec_name"] for stream in probe_streams(capture))


def assert_is_mpeg_ts(viewing: Viewing) -> None:
    assert viewing.status == 200
    assert viewing.content_type == "video/mp2t"
    assert len(viewing.data) > 100_000
    # Datagrams carry whole TS packets, so the stream is packet-aligned from byte 0.
    whole_packets = viewing.data[: len(viewing.data) // 188 * 188]
    assert whole_packets[::188] == b"\x47" * (len(whole_packets) // 188)


def test_live_channel_keeps_every_audio_track(proxy: Proxy, tmp_path: Path) -> None:
    viewing = watch(proxy.channel_urls()["multi audio"], seconds=8)

    assert_is_mpeg_ts(viewing)
    assert codecs(viewing, tmp_path) == ["aac", "h264", "mp2"]


def test_provider_sees_the_vlc_request(proxy: Proxy, provider: Provider) -> None:
    watch(proxy.channel_urls()["multi audio"], seconds=3)

    headers = provider.requests_for("/live/multi_audio.ts?multi")[-1].headers
    assert headers["User-Agent"] == DEFAULT_UPSTREAM_USER_AGENT
    assert "Icy-MetaData" not in headers


def test_viewers_of_a_channel_share_one_provider_connection(
    proxy: Proxy, provider: Provider
) -> None:
    url = proxy.channel_urls()["shared"]
    viewings: list[Viewing] = []
    viewers = [
        threading.Thread(target=lambda: viewings.append(watch(url, seconds=8))) for _ in range(3)
    ]
    for viewer in viewers:
        viewer.start()
    for viewer in viewers:
        viewer.join()

    assert len(viewings) == 3
    for viewing in viewings:
        assert_is_mpeg_ts(viewing)
    assert len(provider.requests_for("/live/multi_audio.ts?shared")) == 1


def test_provider_connection_closes_after_the_last_viewer_leaves(
    proxy: Proxy, provider: Provider
) -> None:
    path = "/live/multi_audio.ts?stop"
    watch(proxy.channel_urls()["stop"], seconds=4)

    wait_until(lambda: provider.open_stream_count(path) == 0)
    assert len(provider.requests_for(path)) == 1


@pytest.mark.skipif(not DVB_SUBTITLE_SAMPLE, reason="needs DVB_SUBTITLE_SAMPLE")
def test_sparse_dvb_subtitles_are_kept_and_delivery_stays_smooth(
    proxy: Proxy, tmp_path: Path
) -> None:
    viewing = watch(proxy.channel_urls()["dvb subtitles"], seconds=15)

    assert_is_mpeg_ts(viewing)
    assert codecs(viewing, tmp_path) == ["aac", "dvb_subtitle", "h264", "mp2"]
    # Without the interleaving limit ffmpeg holds everything back until the next
    # subtitle arrives: gaps of 4-10 s. With it, delivery is continuous.
    assert viewing.largest_gap(after=6) < 1.5


def test_channel_with_a_silent_declared_track_still_plays(proxy: Proxy, tmp_path: Path) -> None:
    viewing = watch(proxy.channel_urls()["silent track"], seconds=8)

    assert_is_mpeg_ts(viewing)
    assert codecs(viewing, tmp_path) == ["aac", "h264"]


def test_radio_channel_plays(proxy: Proxy, tmp_path: Path) -> None:
    viewing = watch(proxy.channel_urls()["radio"], seconds=6)

    assert viewing.status == 200
    assert len(viewing.data) > 50_000
    assert codecs(viewing, tmp_path) == ["mp2"]


def test_hls_downloads_only_the_best_variant(
    proxy: Proxy, provider: Provider, tmp_path: Path
) -> None:
    viewing = watch(proxy.channel_urls()["hls"], seconds=8)

    assert_is_mpeg_ts(viewing)
    streams = probe_streams(_save(viewing, tmp_path))
    assert sorted(stream["codec_name"] for stream in streams) == ["aac", "h264"]
    assert [s["height"] for s in streams if s["codec_type"] == "video"] == [360]
    segments = Counter(
        request.path.rsplit("/", 1)[1].split("_")[0]
        for request in provider.requests_for("/hls/")
        if request.path.endswith(".ts")
    )
    # ffmpeg probes the first segment of every variant once, then only fetches the best.
    assert segments["v2"] >= 3
    assert segments["v0"] <= 1
    assert segments["v1"] <= 1
    # Paced to real time: an 8 s viewing must not have pulled the whole 30 s VOD.
    assert segments["v2"] <= 8


def test_restarts_ffmpeg_when_the_provider_ends_the_stream(
    proxy: Proxy, provider: Provider
) -> None:
    # The provider sends this 30 s channel once (2 s faster at the start) and then
    # closes the connection; the viewer must keep receiving after ffmpeg restarts.
    viewing = watch(proxy.channel_urls()["restart"], seconds=36)

    assert viewing.status == 200
    assert len(provider.requests_for("/once/radio.ts?restart")) >= 2
    assert viewing.arrivals[-1][0] > 32


def test_vod_is_played_in_real_time(proxy: Proxy, provider: Provider, media: Path) -> None:
    # The provider serves the 30 s file as fast as the network allows.
    viewing = watch(proxy.channel_urls()["vod"], seconds=10)

    assert_is_mpeg_ts(viewing)
    file_size = (media / "multi_audio.ts").stat().st_size
    # Paced: about 10 of the 30 seconds, not the whole file (over and over).
    assert 0.15 * file_size < len(viewing.data) < 0.6 * file_size
    assert len(provider.requests_for("/movie/")) == 1


def _save(viewing: Viewing, tmp_path: Path) -> Path:
    path = tmp_path / "capture.ts"
    path.write_bytes(viewing.data)
    return path
