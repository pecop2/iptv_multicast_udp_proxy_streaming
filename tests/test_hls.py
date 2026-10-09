from unittest import mock

import pytest
import requests

from udp_multicast_proxy.hls import Variant, best_variant, fetch_playlist, parse_variants

MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",LANGUAGE="en",NAME="English",URI="audio_en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=559864,AVERAGE-BANDWIDTH=539534,RESOLUTION=426x240,CODECS="avc1.640015,mp4a.40.2",AUDIO="aud"
v0.m3u8

#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=90000,URI="iframes.m3u8"
#EXT-X-STREAM-INF:CODECS="avc1.64001f,mp4a.40.2",BANDWIDTH=3532520,RESOLUTION=1280x720
https://cdn.example/v2.m3u8?token=abc
#EXT-X-STREAM-INF:BANDWIDTH=1180264,RESOLUTION=640x360
v1.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:10
#EXTINF:4.0,
seg10.ts
#EXTINF:4.0,
seg11.ts
"""


def test_parse_variants_in_ffmpeg_program_order() -> None:
    assert parse_variants(MASTER) == [
        Variant(program=0, bandwidth=559864, uri="v0.m3u8"),
        Variant(program=1, bandwidth=3532520, uri="https://cdn.example/v2.m3u8?token=abc"),
        Variant(program=2, bandwidth=1180264, uri="v1.m3u8"),
    ]


def test_media_playlist_has_no_variants() -> None:
    assert parse_variants(MEDIA) == []


def test_parse_variants_handles_crlf_quoted_values_and_missing_bandwidth() -> None:
    text = (
        "#EXTM3U\r\n"
        '#EXT-X-STREAM-INF:CODECS="a,b",BANDWIDTH=200\r\n'
        "a.m3u8\r\n"
        "#EXT-X-STREAM-INF:RESOLUTION=1x1\r\n"
        "\r\n"
        "b.m3u8   \r\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=bogus\r\n"
        "c.m3u8\r\n"
    )

    assert parse_variants(text) == [
        Variant(0, 200, "a.m3u8"),
        Variant(1, 0, "b.m3u8"),
        Variant(2, 0, "c.m3u8"),
    ]


def test_a_stream_inf_without_uri_does_not_create_a_variant() -> None:
    text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n#EXT-X-STREAM-INF:BANDWIDTH=2\nb.m3u8\n"

    assert parse_variants(text) == [Variant(0, 2, "b.m3u8")]


def test_best_variant_is_the_highest_bandwidth() -> None:
    assert best_variant(parse_variants(MASTER)) == Variant(
        1, 3532520, "https://cdn.example/v2.m3u8?token=abc"
    )


def test_best_variant_prefers_the_first_on_ties_and_handles_empty() -> None:
    assert best_variant([Variant(0, 5, "a"), Variant(1, 5, "b")]) == Variant(0, 5, "a")
    assert best_variant([]) is None


def test_fetch_playlist_uses_the_user_agent() -> None:
    response = requests.Response()
    response.status_code = 200
    response._content = b"\xef\xbb\xbf#EXTM3U\n"
    with mock.patch("requests.get", return_value=response) as get:
        assert fetch_playlist("http://p/x.m3u8", user_agent="UA/1") == "#EXTM3U\n"

    assert get.call_args.kwargs["headers"] == {"User-Agent": "UA/1"}


def test_fetch_playlist_raises_on_http_errors() -> None:
    response = requests.Response()
    response.status_code = 404
    with mock.patch("requests.get", return_value=response), pytest.raises(requests.HTTPError):
        fetch_playlist("http://p/x.m3u8", user_agent="UA/1")
