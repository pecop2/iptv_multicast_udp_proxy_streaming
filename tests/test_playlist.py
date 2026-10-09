import io
import itertools
import stat
from pathlib import Path
from unittest import mock

import pytest
import requests

from udp_multicast_proxy.playlist import (
    PLAYLIST_USER_AGENT,
    ChannelPlaylist,
    PlaylistEntry,
    PlaylistError,
    build_proxy_playlist,
    download_m3u,
    multicast_groups,
    parse_m3u,
    write_text_atomically,
)

HOST, PROXY_PORT, MULTICAST_PORT = "192.168.1.2", 8011, 5004


def provider_playlist(channels: int) -> str:
    """A well-formed playlist as IPTV providers serve it."""
    lines = ['#EXTM3U url-tvg="http://provider.example/epg.xml"']
    for number in range(1, channels + 1):
        lines.append(
            f'#EXTINF:-1 tvg-id="ch{number}" tvg-logo="http://logo/{number}.png" '
            f'group-title="Group {number % 3}",Channel {number}'
        )
        lines.append(f"http://provider.example:8080/user/pass/{number}")
    return "\n".join(lines) + "\n"


def original_rewrite(text: str) -> tuple[str, dict[str, str]]:
    """The rewriting loop of the VLC-based version (create_multicast_m3u), verbatim
    apart from reading from a string instead of a file."""
    lines = io.StringIO(text, newline=None).readlines()
    output = "#EXTM3U\n"
    channel_url_map = {}
    first_octet, second_octet, third_octet, fourth_octet = 239, 123, 1, 1
    iter_range = range(1, len(lines), 2)
    for i in iter_range:
        curr_line = lines[i]
        if curr_line.startswith("#EXTINF"):
            address = f"{first_octet}.{second_octet}.{third_octet}.{fourth_octet}"
            source = f"http://{HOST}:{PROXY_PORT}/rtp/{address}:5004"
            if i != iter_range[-1]:
                source += "\n"
            channel_url_map[address] = lines[i + 1].strip()
            output += curr_line + source
            if fourth_octet < 254:
                fourth_octet += 1
            else:
                fourth_octet = 1
                third_octet += 1
            if third_octet == 255:
                second_octet += 1
                third_octet = 1
                fourth_octet = 1
    return output, channel_url_map


def rewrite(text: str) -> tuple[str, dict[str, str]]:
    playlist = build_proxy_playlist(
        parse_m3u(text), host_ip=HOST, proxy_port=PROXY_PORT, multicast_port=MULTICAST_PORT
    )
    return playlist.m3u, dict(playlist.channels)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_matches_the_vlc_based_version_on_a_well_formed_playlist(newline: str) -> None:
    text = provider_playlist(600).replace("\n", newline)  # crosses the .254 boundary

    expected_m3u, expected_channels = original_rewrite(text)
    m3u, channels = rewrite(text)

    # Identical apart from the newline that now terminates the last line.
    assert m3u == expected_m3u + "\n"
    assert channels == expected_channels
    assert list(channels) == list(expected_channels)


def test_rewritten_playlist_format() -> None:
    m3u, channels = rewrite(provider_playlist(2))

    assert m3u == (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-id="ch1" tvg-logo="http://logo/1.png" group-title="Group 1",Channel 1\n'
        "http://192.168.1.2:8011/rtp/239.123.1.1:5004\n"
        '#EXTINF:-1 tvg-id="ch2" tvg-logo="http://logo/2.png" group-title="Group 2",Channel 2\n'
        "http://192.168.1.2:8011/rtp/239.123.1.2:5004\n"
    )
    assert channels == {
        "239.123.1.1": "http://provider.example:8080/user/pass/1",
        "239.123.1.2": "http://provider.example:8080/user/pass/2",
    }


def test_multicast_group_sequence() -> None:
    per_second_octet = 254 * 254
    groups = list(itertools.islice(multicast_groups(), 2 * per_second_octet + 1))

    assert groups[:3] == ["239.123.1.1", "239.123.1.2", "239.123.1.3"]
    assert groups[253:255] == ["239.123.1.254", "239.123.2.1"]
    assert groups[per_second_octet - 1 : per_second_octet + 1] == ["239.123.254.254", "239.124.1.1"]
    assert groups[-1] == "239.125.1.1"
    assert len(set(groups)) == len(groups)


def test_too_many_channels() -> None:
    entries = [PlaylistEntry("#EXTINF:-1,x", "http://x")] * 3
    two_groups = iter(["239.123.1.1", "239.123.1.2"])

    with (
        mock.patch("udp_multicast_proxy.playlist.multicast_groups", return_value=two_groups),
        pytest.raises(PlaylistError, match="Too many channels"),
    ):
        build_proxy_playlist(entries, host_ip=HOST, proxy_port=PROXY_PORT, multicast_port=5004)


def test_parse_skips_directives_and_blank_lines_between_extinf_and_url() -> None:
    text = (
        "#EXTM3U\n"
        "\n"
        "#EXTINF:-1,One\n"
        "#EXTGRP:News\n"
        "#EXTVLCOPT:http-user-agent=Foo\n"
        "\n"
        "  http://provider/1  \n"
        "#EXTINF:-1,Two\n"
        "http://provider/2"  # no trailing newline
    )

    assert parse_m3u(text) == [
        PlaylistEntry("#EXTINF:-1,One", "http://provider/1"),
        PlaylistEntry("#EXTINF:-1,Two", "http://provider/2"),
    ]


def test_parse_ignores_urls_without_extinf_and_extinf_without_url() -> None:
    text = (
        "http://orphan/0\n"
        "#EXTINF:-1,Missing URL\n"
        "#EXTINF:-1,Three\n"
        "http://provider/3\n"
        "#EXTINF:-1,Trailing\n"
    )

    assert parse_m3u(text) == [PlaylistEntry("#EXTINF:-1,Three", "http://provider/3")]


def test_parse_handles_old_mac_line_endings_and_keeps_extinf_verbatim() -> None:
    text = '#EXTM3U\r#EXTINF:-1 tvg-name="Ä"  ,Ä \r http://provider/ä\r'

    assert parse_m3u(text) == [PlaylistEntry('#EXTINF:-1 tvg-name="Ä"  ,Ä ', "http://provider/ä")]


def _response(status: int, content: bytes) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = content
    return response


def test_download_sends_the_browser_user_agent_and_decodes_utf8_with_bom() -> None:
    content = "﻿#EXTM3U\n#EXTINF:-1,Čaj\nhttp://x\n".encode()
    with mock.patch("requests.get", return_value=_response(200, content)) as get:
        text = download_m3u("http://provider/list.m3u")

    assert text == "#EXTM3U\n#EXTINF:-1,Čaj\nhttp://x\n"
    get.assert_called_once()
    assert get.call_args.kwargs["headers"] == {"User-Agent": PLAYLIST_USER_AGENT}
    assert get.call_args.kwargs["timeout"]


def test_download_replaces_invalid_utf8() -> None:
    with mock.patch("requests.get", return_value=_response(200, b"#EXTINF:-1,\xff\nhttp://x\n")):
        assert download_m3u("http://provider/list.m3u") == "#EXTINF:-1,�\nhttp://x\n"


def test_download_rejects_non_200_status() -> None:
    with (
        mock.patch("requests.get", return_value=_response(403, b"Forbidden")),
        pytest.raises(PlaylistError, match="HTTP 403"),
    ):
        download_m3u("http://provider/list.m3u")


def test_download_wraps_network_errors() -> None:
    with (
        mock.patch("requests.get", side_effect=requests.ConnectionError("refused")),
        pytest.raises(PlaylistError, match="refused"),
    ):
        download_m3u("http://provider/list.m3u")


def test_write_text_atomically(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "channels.m3u"
    write_text_atomically(target, "first\n")
    write_text_atomically(target, "second\r\n")

    assert target.read_bytes() == b"second\r\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert [path.name for path in target.parent.iterdir()] == ["channels.m3u"]


class TestChannelPlaylist:
    def make(self, tmp_path: Path, responses: list[str | Exception]) -> ChannelPlaylist:
        def download(url: str) -> str:
            assert url == "http://provider/list.m3u"
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        return ChannelPlaylist(
            source_url="http://provider/list.m3u",
            output_path=tmp_path / "web" / "channels_multicast.m3u",
            host_ip=HOST,
            proxy_port=PROXY_PORT,
            multicast_port=MULTICAST_PORT,
            download=download,
        )

    def test_refresh_publishes_file_and_channels(self, tmp_path: Path) -> None:
        playlist = self.make(tmp_path, [provider_playlist(3)])
        assert playlist.updated_at is None
        assert playlist.upstream_url("239.123.1.1") is None

        assert playlist.refresh() == 3

        assert playlist.channel_count == 3
        assert playlist.updated_at is not None
        assert playlist.upstream_url("239.123.1.3") == "http://provider.example:8080/user/pass/3"
        assert playlist.upstream_url("239.123.1.4") is None
        written = (tmp_path / "web" / "channels_multicast.m3u").read_text()
        assert written == rewrite(provider_playlist(3))[0]

    @pytest.mark.parametrize(
        "failure",
        [PlaylistError("network down"), "<html>Account expired</html>"],
        ids=["download error", "no channels"],
    )
    def test_failed_refresh_keeps_the_previous_playlist(
        self, tmp_path: Path, failure: str | Exception
    ) -> None:
        playlist = self.make(tmp_path, [provider_playlist(2), failure])
        playlist.refresh()
        updated_at = playlist.updated_at
        written = (tmp_path / "web" / "channels_multicast.m3u").read_text()

        with pytest.raises(PlaylistError):
            playlist.refresh()

        assert playlist.channel_count == 2
        assert playlist.updated_at == updated_at
        assert (tmp_path / "web" / "channels_multicast.m3u").read_text() == written

    def test_refresh_replaces_channels(self, tmp_path: Path) -> None:
        playlist = self.make(tmp_path, [provider_playlist(3), provider_playlist(1)])
        playlist.refresh()

        playlist.refresh()

        assert playlist.channel_count == 1
        assert playlist.upstream_url("239.123.1.2") is None
