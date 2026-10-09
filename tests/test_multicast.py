"""Real multicast on this machine. The sender uses TTL 0, so datagrams are only
looped back locally and never reach the network."""

import contextlib
import socket
from collections.abc import Iterator

import pytest

from udp_multicast_proxy.multicast import open_receiver

PORT = 45004


def receiver(group: str, timeout: float = 2) -> socket.socket:
    try:
        return open_receiver(group, PORT, timeout)
    except OSError as error:  # e.g. no multicast route on this machine
        pytest.skip(f"multicast is not available here: {error}")


@pytest.fixture
def sender() -> Iterator[socket.socket]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    with sock:
        yield sock


def send(sender: socket.socket, group: str, payload: bytes) -> None:
    try:
        sender.sendto(payload, (group, PORT))
    except OSError as error:
        pytest.skip(f"multicast is not available here: {error}")


def test_receives_datagrams_of_its_group_intact(sender: socket.socket) -> None:
    payload = bytes(range(188)) * 7
    with receiver("239.255.77.1") as sock:
        send(sender, "239.255.77.1", payload)

        assert sock.recv(65535) == payload


def test_only_receives_its_own_group(sender: socket.socket) -> None:
    with receiver("239.255.77.1") as first, receiver("239.255.77.2") as second:
        send(sender, "239.255.77.2", b"for-second")
        send(sender, "239.255.77.1", b"for-first")

        assert first.recv(65535) == b"for-first"
        assert second.recv(65535) == b"for-second"


def test_every_viewer_of_a_channel_gets_every_datagram(sender: socket.socket) -> None:
    with contextlib.ExitStack() as stack:
        viewers = [stack.enter_context(receiver("239.255.77.3")) for _ in range(3)]
        send(sender, "239.255.77.3", b"one")
        send(sender, "239.255.77.3", b"two")

        for viewer in viewers:
            assert [viewer.recv(65535), viewer.recv(65535)] == [b"one", b"two"]


def test_receive_times_out() -> None:
    with receiver("239.255.77.4", timeout=0.05) as sock, pytest.raises(TimeoutError):
        sock.recv(65535)


def test_requests_a_larger_receive_buffer_than_the_default() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as plain:
        default_size = plain.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    with receiver("239.255.77.5") as sock:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) >= default_size


def test_closes_the_socket_when_setup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[socket.socket] = []
    real_socket = socket.socket

    def tracking_socket(*args: int) -> socket.socket:
        sock = real_socket(*args)
        created.append(sock)
        return sock

    monkeypatch.setattr(socket, "socket", tracking_socket)

    with pytest.raises(OSError):  # noqa: PT011 - any socket error
        open_receiver("not-an-address", PORT, timeout=1)

    assert created
    assert created[0].fileno() == -1
