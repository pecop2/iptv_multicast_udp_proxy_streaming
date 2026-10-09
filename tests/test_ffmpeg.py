import logging

import pytest
import requests

from udp_multicast_proxy.ffmpeg import (
    MINIMUM_VERSION,
    CommandFactory,
    SourceKind,
    build_command,
    classify_source,
    parse_version,
    stream_maps,
)

UA = "VLC/3.0.23 LibVLC/3.0.23"


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("http://provider:8080/user/pass/12345", SourceKind.LIVE_TS),
        ("http://provider:8080/live/user/pass/12345.ts", SourceKind.LIVE_TS),
        ("http://provider/stream.php?id=5", SourceKind.LIVE_TS),
        ("http://radio.example/stream.mp3", SourceKind.LIVE_TS),
        ("http://provider:8080/live/user/pass/12345.m3u8", SourceKind.HLS),
        ("https://cdn.example/Master.M3U8?token=a.mp4", SourceKind.HLS),
        ("http://provider/movie/user/pass/7.mkv", SourceKind.MEDIA_FILE),
        ("http://provider/series/user/pass/8.MP4", SourceKind.MEDIA_FILE),
        ("http://provider/movie/user/pass/9.avi?x=1", SourceKind.MEDIA_FILE),
        ("http://provider/folder.mp4/stream", SourceKind.LIVE_TS),
        # Xtream-style VOD paths, whatever the container
        ("http://provider:8080/movie/user/pass/10.ts", SourceKind.MEDIA_FILE),
        ("http://provider:8080/Series/user/pass/11", SourceKind.MEDIA_FILE),
        ("http://provider:8080/series/user/pass/12.m3u8", SourceKind.HLS),
        ("http://provider:8080/live/user/pass/movie.ts", SourceKind.LIVE_TS),
        ("http://provider:8080/movies/user/pass/13.ts", SourceKind.LIVE_TS),
    ],
)
def test_classify_source(url: str, kind: SourceKind) -> None:
    assert classify_source(url) is kind


@pytest.mark.parametrize(
    ("line", "version"),
    [
        ("ffmpeg version 9.0.2 Copyright (c) 2000-2026 the FFmpeg developers", (9, 0)),
        ("ffmpeg version 8.0.1 Copyright (c) 2000-2025 the FFmpeg developers", (8, 0)),
        ("ffmpeg version 7.1.1-1+b1 Copyright (c) 2000-2025 the FFmpeg developers", (7, 1)),
        ("ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 the FFmpeg developers", (6, 1)),
        ("ffmpeg version n7.1.1-20250101 Copyright (c) 2000-2025 the FFmpeg developers", (7, 1)),
        ("ffmpeg version 7.1 Copyright (c) 2000-2024 the FFmpeg developers", (7, 1)),
        ("ffmpeg version N-118123-g0123abcd Copyright (c) 2000-2026", None),
        ("something else entirely", None),
    ],
)
def test_parse_version(line: str, version: tuple[int, int] | None) -> None:
    assert parse_version(line) == version


def test_minimum_version_is_the_first_one_that_understands_the_maps() -> None:
    # Measured: 5.1, 6.1 and 7.0 reject "0:v:u:?" and drop tracks; 7.1+ accept it.
    assert MINIMUM_VERSION == (7, 1)
    assert (7, 0) < MINIMUM_VERSION <= (7, 1) < (8, 0) < (9, 0)


def test_live_ts_keeps_all_usable_tracks_and_subtitles() -> None:
    assert stream_maps(SourceKind.LIVE_TS) == [
        "-map", "0:v:u:?", "-map", "0:a:u:?", "-map", "0:s?",
    ]  # fmt: skip


@pytest.mark.parametrize("kind", [SourceKind.HLS, SourceKind.MEDIA_FILE])
def test_paced_sources_keep_audio_and_video_only(kind: SourceKind) -> None:
    assert stream_maps(kind) == ["-map", "0:v:u:?", "-map", "0:a:u:?"]


def test_hls_program_selects_one_variant() -> None:
    assert stream_maps(SourceKind.HLS, hls_program=2) == [
        "-map", "0:p:2:v:u:?", "-map", "0:p:2:a:u:?",
    ]  # fmt: skip


def test_live_ts_command() -> None:
    command = build_command(
        ffmpeg_path="/usr/local/bin/ffmpeg",
        source_url="http://provider/user/pass/1",
        group="239.123.1.1",
        port=5004,
        user_agent=UA,
        kind=SourceKind.LIVE_TS,
    )

    assert command == [
        "/usr/local/bin/ffmpeg", "-hide_banner", "-nostdin", "-nostats",
        "-loglevel", "level+warning",
        "-user_agent", UA, "-icy", "0",
        "-i", "http://provider/user/pass/1",
        "-map", "0:v:u:?", "-map", "0:a:u:?", "-map", "0:s?",
        "-c", "copy",
        "-max_interleave_delta", "500000",
        "-mpegts_flags", "+initial_discontinuity",
        "-f", "mpegts",
        "udp://239.123.1.1:5004?pkt_size=1316&ttl=1",
    ]  # fmt: skip


def test_hls_command_is_paced_and_maps_the_chosen_variant() -> None:
    command = build_command(
        ffmpeg_path="ffmpeg",
        source_url="http://provider/master.m3u8",
        group="239.123.1.2",
        port=5004,
        user_agent=UA,
        kind=SourceKind.HLS,
        hls_program=1,
    )

    assert command == [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats",
        "-loglevel", "level+warning",
        "-user_agent", UA, "-icy", "0",
        "-re",
        "-i", "http://provider/master.m3u8",
        "-map", "0:p:1:v:u:?", "-map", "0:p:1:a:u:?",
        "-c", "copy",
        "-mpegts_flags", "+initial_discontinuity",
        "-f", "mpegts",
        "udp://239.123.1.2:5004?pkt_size=1316&ttl=1",
    ]  # fmt: skip


def test_media_file_command_is_paced() -> None:
    command = build_command(
        ffmpeg_path="ffmpeg",
        source_url="http://provider/movie/1.mkv",
        group="239.123.1.3",
        port=5004,
        user_agent=UA,
        kind=SourceKind.MEDIA_FILE,
    )

    assert command[command.index("-i") - 1] == "-re"
    assert "-max_interleave_delta" not in command
    assert "0:s?" not in command


def test_udp_packets_are_whole_ts_packets() -> None:
    command = build_command(
        ffmpeg_path="ffmpeg",
        source_url="http://p/1",
        group="239.1.2.3",
        port=1234,
        user_agent=UA,
        kind=SourceKind.LIVE_TS,
    )

    assert command[-1] == "udp://239.1.2.3:1234?pkt_size=1316&ttl=1"
    assert 1316 % 188 == 0


class TestCommandFactory:
    def factory(self, playlists: dict[str, str | Exception]) -> CommandFactory:
        def fetch(url: str) -> str:
            result = playlists[url]
            if isinstance(result, Exception):
                raise result
            return result

        return CommandFactory(
            ffmpeg_path="ffmpeg", user_agent=UA, multicast_port=5004, fetch_playlist=fetch
        )

    def test_live_ts_does_not_fetch_anything(self) -> None:
        command = self.factory({})("239.123.1.1", "http://provider/user/pass/1")

        assert "0:s?" in command
        assert command[-1] == "udp://239.123.1.1:5004?pkt_size=1316&ttl=1"

    def test_master_playlist_maps_the_best_variant(self) -> None:
        master = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nlow.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=900\nhigh.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=500\nmid.m3u8\n"
        )
        command = self.factory({"http://p/master.m3u8": master})(
            "239.1.1.1", "http://p/master.m3u8"
        )

        assert "0:p:1:v:u:?" in command
        assert "0:p:1:a:u:?" in command

    @pytest.mark.parametrize(
        "playlist",
        [
            "#EXTM3U\n#EXTINF:4,\nseg.ts\n",
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nonly.m3u8\n",
        ],
        ids=["media playlist", "single variant"],
    )
    def test_nothing_to_choose_maps_every_stream(self, playlist: str) -> None:
        command = self.factory({"http://p/x.m3u8": playlist})("239.1.1.1", "http://p/x.m3u8")

        assert "0:v:u:?" in command
        assert "-re" in command

    def test_unreachable_playlist_falls_back_to_every_stream(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        url = "http://p/live/USER/PASS/1.m3u8"
        error = requests.ConnectionError(
            "Max retries exceeded with url: /live/USER/PASS/1.m3u8 (refused)"
        )
        factory = self.factory({url: error})

        with caplog.at_level(logging.WARNING):
            command = factory("239.1.1.1", url)

        assert "0:v:u:?" in command
        assert "239.1.1.1: could not inspect the HLS playlist" in caplog.text
        assert "refused" in caplog.text
        assert "USER" not in caplog.text
        assert "PASS" not in caplog.text
