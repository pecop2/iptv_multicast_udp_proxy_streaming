"""The HTTP servers over real sockets. Multicast is replaced by plain UDP on
127.0.0.1 (see test_multicast.py for the multicast receiver itself)."""

import errno
import http.client
import queue
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from udp_multicast_proxy.servers import (
    StreamProxyServer,
    StreamRequestHandler,
    create_playlist_server,
    parse_stream_path,
)
from udp_multicast_proxy.streams import StreamManager

CHANNELS = {"239.123.1.1": "http://provider/1", "239.123.1.2": "http://provider/2"}
CHANNEL_PATH = "/rtp/239.123.1.1:5004"


@dataclass
class FakeRestream:
    group: str
    url: str
    started: threading.Event = field(default_factory=threading.Event)
    stopped: threading.Event = field(default_factory=threading.Event)

    def start(self) -> None:
        self.started.set()

    def stop(self) -> None:
        self.stopped.set()


class UnicastReceivers:
    """Stands in for `multicast.open_receiver`: every viewer gets its own UDP socket
    on 127.0.0.1, whose address the test uses to "multicast" datagrams to it."""

    def __init__(self) -> None:
        self.addresses: queue.Queue[tuple[str, int]] = queue.Queue()
        self.fail_with: OSError | None = None

    def __call__(self, group: str, port: int, timeout: float) -> socket.socket:
        assert port == 5004
        if self.fail_with is not None:
            raise self.fail_with
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(timeout)
        self.addresses.put(sock.getsockname())
        return sock

    def next_viewer(self) -> tuple[str, int]:
        return self.addresses.get(timeout=5)


class Harness:
    def __init__(self, viewer_timeout: float = 5) -> None:
        self.restreams: list[FakeRestream] = []
        self.manager = StreamManager(CHANNELS.get, self._create)
        self.receivers = UnicastReceivers()
        self.server = StreamProxyServer(
            ("127.0.0.1", 0),
            streams=self.manager,
            multicast_port=5004,
            viewer_timeout=viewer_timeout,
            backlog=3,
            open_receiver=self.receivers,
        )
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def _create(self, group: str, url: str) -> FakeRestream:
        restream = FakeRestream(group, url)
        self.restreams.append(restream)
        return restream

    def connect(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)

    def request(self, method: str, path: str) -> http.client.HTTPResponse:
        connection = self.connect()
        connection.request(method, path)
        return connection.getresponse()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.sender.close()


@pytest.fixture
def harness() -> Iterator[Harness]:
    harness = Harness()
    yield harness
    harness.close()


def read_exactly(response: http.client.HTTPResponse, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = response.read(size - len(data))
        assert chunk, f"stream ended after {len(data)} of {size} bytes"
        data += chunk
    return data


def wait_until(condition: Callable[[], bool], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def ts_datagram(index: int) -> bytes:
    return bytes([0x47, index % 256]) + bytes(1316 - 2)


def test_streams_the_channel_to_the_viewer(harness: Harness) -> None:
    response = harness.request("GET", CHANNEL_PATH)

    assert response.status == 200
    assert response.version == 10  # HTTP/1.0: the stream ends when the connection closes
    assert response.getheader("Content-Type") == "video/mp2t"
    assert response.getheader("Content-Length") is None
    assert [(r.group, r.url) for r in harness.restreams] == [("239.123.1.1", "http://provider/1")]

    viewer = harness.receivers.next_viewer()
    datagrams = [ts_datagram(i) for i in range(50)]
    for datagram in datagrams:
        harness.sender.sendto(datagram, viewer)

    assert read_exactly(response, 50 * 1316) == b"".join(datagrams)


def test_relays_datagrams_of_any_size(harness: Harness) -> None:
    response = harness.request("GET", CHANNEL_PATH)
    viewer = harness.receivers.next_viewer()
    # macOS refuses datagrams over 9216 bytes unless the send buffer is raised.
    harness.sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    sent = []
    for size in (188, 1316, 9024, 32712, 65507):
        datagram = bytes([0x47, size % 251]) * (size // 2) + b"\x47" * (size % 2)
        try:
            harness.sender.sendto(datagram, viewer)
        except OSError as error:
            if error.errno != errno.EMSGSIZE:  # larger than this OS allows: skip it
                raise
        else:
            sent.append(datagram)

    assert len(sent) >= 3
    assert read_exactly(response, sum(map(len, sent))) == b"".join(sent)


def test_viewer_leaving_stops_the_stream(harness: Harness) -> None:
    connection = harness.connect()
    connection.request("GET", CHANNEL_PATH)
    response = connection.getresponse()
    viewer = harness.receivers.next_viewer()
    harness.sender.sendto(ts_datagram(0), viewer)
    read_exactly(response, 1316)
    restream = harness.restreams[0]

    response.close()
    connection.close()
    # The proxy notices the viewer is gone when writing to it fails.
    wait_until(
        lambda: harness.sender.sendto(ts_datagram(1), viewer) > 0 and restream.stopped.is_set()
    )

    assert harness.manager.active_channel_count == 0


def test_a_viewer_that_stops_reading_is_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    # e.g. a TV switched off without closing the connection
    monkeypatch.setattr(StreamRequestHandler, "timeout", 0.5)
    harness = Harness()
    viewer_socket = socket.socket()
    try:
        viewer_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        viewer_socket.connect(("127.0.0.1", harness.server.server_address[1]))
        viewer_socket.sendall(f"GET {CHANNEL_PATH} HTTP/1.1\r\nHost: proxy\r\n\r\n".encode())
        viewer = harness.receivers.next_viewer()
        wait_until(lambda: len(harness.restreams) == 1)
        stopped = harness.restreams[0].stopped

        # Keep the channel busy until the proxy gives up on the viewer.
        wait_until(
            lambda: harness.sender.sendto(b"\x47" * 9024, viewer) > 0 and stopped.is_set(),
            timeout=20,
        )
    finally:
        viewer_socket.close()
        harness.close()


def test_a_stalled_stream_disconnects_the_viewer() -> None:
    harness = Harness(viewer_timeout=0.2)
    try:
        response = harness.request("GET", CHANNEL_PATH)

        assert response.read() == b""  # the proxy closed the connection
        assert harness.restreams[0].stopped.wait(5)
    finally:
        harness.close()


def test_viewers_of_one_channel_share_one_restream(harness: Harness) -> None:
    first = harness.request("GET", CHANNEL_PATH)
    first_viewer = harness.receivers.next_viewer()
    second = harness.request("GET", "/whatever/239.123.1.1:5004")
    second_viewer = harness.receivers.next_viewer()

    for viewer in (first_viewer, second_viewer):
        harness.sender.sendto(ts_datagram(7), viewer)

    assert read_exactly(first, 1316) == ts_datagram(7)
    assert read_exactly(second, 1316) == ts_datagram(7)
    assert len(harness.restreams) == 1
    assert harness.manager.active_channel_count == 1


@pytest.mark.parametrize(
    "path",
    [
        "/rtp/239.9.9.9:5004",  # not a channel
        "/rtp/239.123.1.1:5005",  # wrong port
        "/rtp/239.123.1.1",
        "/rtp/192.168.1.1:5004",  # not multicast
        "/favicon.ico",
        "/",
    ],
)
def test_unknown_streams_are_404(harness: Harness, path: str) -> None:
    for method in ("GET", "HEAD"):
        assert harness.request(method, path).status == 404
    assert harness.restreams == []


def test_head_does_not_start_the_stream(harness: Harness) -> None:
    response = harness.request("HEAD", CHANNEL_PATH)

    assert response.status == 200
    assert response.getheader("Content-Type") == "video/mp2t"
    assert response.read() == b""
    assert harness.restreams == []


def test_receiver_failure_is_503(harness: Harness) -> None:
    harness.receivers.fail_with = OSError("No such device")

    assert harness.request("GET", CHANNEL_PATH).status == 503
    assert harness.restreams == []


def test_requests_during_shutdown_are_503(harness: Harness) -> None:
    harness.manager.shutdown()

    assert harness.request("GET", CHANNEL_PATH).status == 503


def test_listen_backlog_comes_from_number_of_clients(harness: Harness) -> None:
    assert harness.server.request_queue_size == 3


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/rtp/239.123.1.1:5004", ("239.123.1.1", 5004)),
        ("/udp/239.123.254.254:5004?token=1", ("239.123.254.254", 5004)),
        ("239.123.1.1:1", ("239.123.1.1", 1)),
        ("/rtp/239.123.1.1:0", None),
        ("/rtp/239.123.1.1:65536", None),
        ("/rtp/239.123.1.1:\uff15\uff10\uff10\uff14", None),  # fullwidth digits
        ("/rtp/239.123.001.1:5004", None),
        ("/rtp/10.0.0.1:5004", None),
        ("/rtp/239.123.1.1:", None),
        ("/rtp/:5004", None),
        ("/rtp/239.123.1.1:5004/", None),
    ],
)
def test_parse_stream_path(path: str, expected: tuple[str, int] | None) -> None:
    assert parse_stream_path(path) == expected


@pytest.fixture
def playlist_server(tmp_path: Path) -> Iterator[ThreadingHTTPServer]:
    (tmp_path / "channels_multicast.m3u").write_text("#EXTM3U\n#EXTINF:-1,A\nhttp://x\n")
    server = create_playlist_server(("127.0.0.1", 0), tmp_path)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def playlist_request(
    server: ThreadingHTTPServer, method: str, path: str, **kwargs: object
) -> http.client.HTTPResponse:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    connection.request(method, path, **kwargs)  # type: ignore[arg-type]
    return connection.getresponse()


def test_playlist_is_served_as_audio_mpegurl(playlist_server: ThreadingHTTPServer) -> None:
    response = playlist_request(playlist_server, "GET", "/channels_multicast.m3u")

    assert response.status == 200
    assert response.getheader("Content-Type") == "audio/mpegurl"
    assert response.read() == b"#EXTM3U\n#EXTINF:-1,A\nhttp://x\n"


def test_playlist_server_lists_the_directory(playlist_server: ThreadingHTTPServer) -> None:
    response = playlist_request(playlist_server, "GET", "/")

    assert response.status == 200
    assert b"channels_multicast.m3u" in response.read()


@pytest.mark.parametrize(
    "kwargs",
    [{"body": b"x" * 200_000}, {"body": b""}, {"headers": {"Content-Length": "bogus"}}],
    ids=["body", "empty", "bad length"],
)
def test_playlist_server_accepts_post(
    playlist_server: ThreadingHTTPServer, kwargs: dict[str, object]
) -> None:
    assert playlist_request(playlist_server, "POST", "/anything", **kwargs).status == 200
