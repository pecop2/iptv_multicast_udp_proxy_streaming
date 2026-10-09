"""The application wired together, with a stub ffmpeg and a local provider."""

import http.client
import logging
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from udp_multicast_proxy import app
from udp_multicast_proxy.config import Settings


class Provider:
    """Serves /list.m3u; every refresh can return a different playlist."""

    def __init__(self) -> None:
        self.playlists = ["#EXTM3U\n#EXTINF:-1,One\nhttp://provider/1\n"]
        self.status = HTTPStatus.OK
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = provider.playlists[0].encode()
                if len(provider.playlists) > 1:
                    provider.playlists.pop(0)
                self.send_response(provider.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/list.m3u"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def provider() -> Iterator[Provider]:
    provider = Provider()
    yield provider
    provider.close()


@pytest.fixture
def second_provider() -> Iterator[Provider]:
    provider = Provider()
    provider.playlists = ["#EXTM3U\n#EXTINF:-1,Two\nhttp://second-provider/2\n"]
    yield provider
    provider.close()


def make_fake_ffmpeg(directory: Path, version_line: str) -> str:
    """An executable that answers `-version` like ffmpeg."""
    path = directory / "ffmpeg"
    path.write_text(
        f"#!{sys.executable}\nimport sys\nif '-version' in sys.argv:\n    print({version_line!r})\n"
    )
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def fake_ffmpeg(tmp_path: Path) -> str:
    return make_fake_ffmpeg(tmp_path, "ffmpeg version 9.0.2-test Copyright (c) 2000-2026")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def make_settings(
    tmp_path: Path, providers: list[Provider], ffmpeg: str, **overrides: float
) -> Settings:
    return Settings(
        original_m3u_urls=tuple(provider.url for provider in providers),
        host_ip="127.0.0.1",
        ffmpeg_path=ffmpeg,
        playlist_port=free_port(),
        stream_proxy_port=free_port(),
        data_dir=tmp_path / "data",
        status_interval=overrides.get("status_interval", 0.05),
        playlist_refresh_interval=overrides.get("playlist_refresh_interval", 3600),
    )


def wait_until(condition: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


class RunningApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stop = threading.Event()
        self.exit_code: int | None = None
        self.thread = threading.Thread(target=self._run)
        self.thread.start()

    def _run(self) -> None:
        self.exit_code = app.run(self.settings, self.stop)

    def get(self, port: int, path: str, method: str = "GET") -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.read()
        except OSError:
            return 0, b""
        finally:
            connection.close()

    def playlist(self) -> bytes:
        status, body = self.get(self.settings.playlist_port, "/channels_multicast.m3u")
        return body if status == 200 else b""

    def shutdown(self) -> int | None:
        self.stop.set()
        self.thread.join(timeout=10)
        assert not self.thread.is_alive()
        return self.exit_code


def test_serves_playlist_and_streams_then_shuts_down_cleanly(
    tmp_path: Path, provider: Provider, fake_ffmpeg: str, caplog: pytest.LogCaptureFixture
) -> None:
    settings = make_settings(tmp_path, [provider], fake_ffmpeg)
    with caplog.at_level(logging.INFO):
        running = RunningApp(settings)
        wait_until(lambda: running.playlist() != b"")

        proxy = settings.stream_proxy_port
        assert running.playlist() == (
            f"#EXTM3U\n#EXTINF:-1,One\nhttp://127.0.0.1:{proxy}/rtp/239.123.1.1:5004\n".encode()
        )
        assert running.get(proxy, "/rtp/239.123.1.1:5004", method="HEAD")[0] == 200
        assert running.get(proxy, "/rtp/239.123.1.2:5004", method="HEAD")[0] == 404

        assert running.shutdown() == 0

    assert "Using ffmpeg version 9.0.2-test" in caplog.text
    assert (
        f"Playlist: http://127.0.0.1:{settings.playlist_port}/channels_multicast.m3u" in caplog.text
    )
    assert "Channels active: 0" in caplog.text
    assert running.get(settings.playlist_port, "/")[0] == 0  # ports are closed


def test_refreshes_the_playlist_when_due(
    tmp_path: Path, provider: Provider, fake_ffmpeg: str
) -> None:
    provider.playlists.append(
        "#EXTM3U\n#EXTINF:-1,One\nhttp://provider/1\n#EXTINF:-1,Two\nhttp://provider/2\n"
    )
    running = RunningApp(
        make_settings(tmp_path, [provider], fake_ffmpeg, playlist_refresh_interval=0.1)
    )
    try:
        wait_until(lambda: b"239.123.1.2" in running.playlist())
    finally:
        assert running.shutdown() == 0


def test_a_failed_refresh_keeps_serving_the_current_playlist(
    tmp_path: Path, provider: Provider, fake_ffmpeg: str, caplog: pytest.LogCaptureFixture
) -> None:
    running = RunningApp(
        make_settings(tmp_path, [provider], fake_ffmpeg, playlist_refresh_interval=0.1)
    )
    try:
        wait_until(lambda: running.playlist() != b"")
        before = running.playlist()
        provider.status = HTTPStatus.INTERNAL_SERVER_ERROR
        with caplog.at_level(logging.ERROR):
            wait_until(lambda: "Playlist refresh failed" in caplog.text)

        assert running.playlist() == before
    finally:
        assert running.shutdown() == 0


def test_merges_the_playlists_of_several_providers(
    tmp_path: Path, provider: Provider, second_provider: Provider, fake_ffmpeg: str
) -> None:
    settings = make_settings(tmp_path, [provider, second_provider], fake_ffmpeg)
    running = RunningApp(settings)
    try:
        wait_until(lambda: running.playlist() != b"")

        proxy = settings.stream_proxy_port
        assert (
            running.playlist()
            == (
                "#EXTM3U\n"
                f"#EXTINF:-1,One\nhttp://127.0.0.1:{proxy}/rtp/239.123.1.1:5004\n"
                f"#EXTINF:-1,Two\nhttp://127.0.0.1:{proxy}/rtp/239.123.1.2:5004\n"
            ).encode()
        )
        for group in ("239.123.1.1", "239.123.1.2"):
            assert running.get(proxy, f"/rtp/{group}:5004", method="HEAD")[0] == 200
    finally:
        assert running.shutdown() == 0


def test_exits_when_any_playlist_cannot_be_downloaded(
    tmp_path: Path,
    provider: Provider,
    second_provider: Provider,
    fake_ffmpeg: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    second_provider.status = HTTPStatus.FORBIDDEN
    settings = make_settings(tmp_path, [provider, second_provider], fake_ffmpeg)

    assert app.run(settings, threading.Event()) == 1
    origin = second_provider.url.removesuffix("/list.m3u")
    assert f"Original playlist 2 of 2: Downloading from {origin} failed: HTTP 403." in caplog.text


def test_exits_when_ffmpeg_is_too_old(
    tmp_path: Path, provider: Provider, caplog: pytest.LogCaptureFixture
) -> None:
    ffmpeg = make_fake_ffmpeg(tmp_path, "ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023")

    assert app.run(make_settings(tmp_path, [provider], ffmpeg), threading.Event()) == 1
    assert "ffmpeg 6.1 is too old: version 7.1 or newer is needed" in caplog.text


def test_runs_with_an_ffmpeg_build_without_a_version_number(
    tmp_path: Path, provider: Provider, caplog: pytest.LogCaptureFixture
) -> None:
    ffmpeg = make_fake_ffmpeg(tmp_path, "ffmpeg version N-118123-g0123abcd Copyright (c) 2000-2026")
    running = RunningApp(make_settings(tmp_path, [provider], ffmpeg))
    try:
        wait_until(lambda: running.playlist() != b"")
    finally:
        assert running.shutdown() == 0
    assert "Could not tell the ffmpeg version; version 7.1 or newer is needed." in caplog.text


def test_exits_when_ffmpeg_is_missing(
    tmp_path: Path, provider: Provider, caplog: pytest.LogCaptureFixture
) -> None:
    settings = make_settings(tmp_path, [provider], str(tmp_path / "no-such-ffmpeg"))

    assert app.run(settings, threading.Event()) == 1
    assert "is not usable" in caplog.text


def test_exits_when_the_playlist_cannot_be_downloaded(
    tmp_path: Path, provider: Provider, fake_ffmpeg: str, caplog: pytest.LogCaptureFixture
) -> None:
    provider.status = HTTPStatus.FORBIDDEN

    assert app.run(make_settings(tmp_path, [provider], fake_ffmpeg), threading.Event()) == 1
    assert "HTTP 403" in caplog.text


def test_exits_when_a_port_is_taken(
    tmp_path: Path, provider: Provider, fake_ffmpeg: str, caplog: pytest.LogCaptureFixture
) -> None:
    settings = make_settings(tmp_path, [provider], fake_ffmpeg)
    with socket.socket() as taken:
        taken.bind(("", settings.stream_proxy_port))
        taken.listen()

        assert app.run(settings, threading.Event()) == 1
    assert "Cannot open the HTTP ports" in caplog.text


def test_main_reports_configuration_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("ORIGINAL_M3U_URL", raising=False)
    monkeypatch.setenv("HOST_IP", "192.168.1.2")
    monkeypatch.setattr(app, "_configure_logging", lambda _level: None)  # keep caplog's handler

    assert app.main() == 2
    assert "ORIGINAL_M3U_URL is required" in caplog.text
