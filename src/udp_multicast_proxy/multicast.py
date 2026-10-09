"""Receiving a channel's MPEG-TS from its UDP multicast group."""

import contextlib
import socket
import sys

# Large enough for any UDP datagram, so a datagram is never truncated.
MAX_DATAGRAM_SIZE = 65535
# Requested kernel receive buffer: absorbs bursts (e.g. the provider's initial
# burst) while a viewer's connection is briefly slow. The kernel caps it at
# net.core.rmem_max.
RECEIVE_BUFFER_SIZE = 4 * 1024 * 1024


def open_receiver(group: str, port: int, timeout: float) -> socket.socket:
    """A UDP socket joined to `group`; `recv` raises TimeoutError after `timeout` s."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        # Every viewer of a channel binds the same group and port.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECEIVE_BUFFER_SIZE)
        # All channels share one port. Binding to the group address makes Linux
        # deliver only this group's datagrams; Windows cannot bind to a multicast
        # address, so it binds to all interfaces instead.
        sock.bind(("" if sys.platform == "win32" else group, port))
        membership = socket.inet_aton(group) + socket.inet_aton("0.0.0.0")  # any interface
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.settimeout(timeout)
    except BaseException:
        sock.close()
        raise
    return sock
