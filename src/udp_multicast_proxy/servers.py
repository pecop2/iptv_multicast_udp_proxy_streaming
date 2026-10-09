"""The HTTP servers: the rewritten playlist and the per-channel stream proxy."""

import functools
import ipaddress
import logging
import os
import socket
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from . import multicast
from .streams import StreamManager, StreamManagerClosedError, UnknownChannelError

log = logging.getLogger(__name__)

STREAM_CONTENT_TYPE = "video/mp2t"
_DISCARD_CHUNK_SIZE = 64 * 1024


class PlaylistRequestHandler(SimpleHTTPRequestHandler):
    """Serves the rewritten playlist directory."""

    # The Content-Type the previous version served the playlist with (from the
    # Debian /etc/mime.types in its image); Python's built-in table differs.
    extensions_map = {  # noqa: RUF012 - overrides a plain stdlib class attribute
        **SimpleHTTPRequestHandler.extensions_map,
        ".m3u": "audio/mpegurl",
    }

    def do_POST(self) -> None:
        """Accept and ignore POST requests, as the previous version did."""
        try:
            remaining = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            remaining = 0
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, _DISCARD_CHUNK_SIZE))
            if not chunk:
                break
            remaining -= len(chunk)
        self.send_response(HTTPStatus.OK)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        log.info("Playlist server: %s - %s", self.address_string(), format % args)


def create_playlist_server(
    address: tuple[str, int], directory: os.PathLike[str]
) -> ThreadingHTTPServer:
    handler = functools.partial(PlaylistRequestHandler, directory=os.fspath(directory))
    return ThreadingHTTPServer(address, handler)


def parse_stream_path(path: str) -> tuple[str, int] | None:
    """Extract (multicast group, port) from a request path like `/rtp/239.1.2.3:5004`."""
    segment = urlsplit(path).path.rsplit("/", 1)[-1]
    address, separator, port_text = segment.rpartition(":")
    if not separator or not (port_text.isascii() and port_text.isdigit()):
        return None
    try:
        group = ipaddress.IPv4Address(address)
    except ValueError:
        return None
    port = int(port_text)
    if not group.is_multicast or not 0 < port < 65536:
        return None
    return str(group), port


class StreamProxyServer(ThreadingHTTPServer):
    """Relays a channel's multicast MPEG-TS to each HTTP viewer."""

    def __init__(
        self,
        address: tuple[str, int],
        *,
        streams: StreamManager,
        multicast_port: int,
        viewer_timeout: float,
        backlog: int,
        open_receiver: Callable[[str, int, float], socket.socket] = multicast.open_receiver,
    ) -> None:
        self.streams = streams
        self.multicast_port = multicast_port
        self.viewer_timeout = viewer_timeout
        self.open_receiver = open_receiver
        self.request_queue_size = backlog  # read by listen() during __init__
        super().__init__(address, StreamRequestHandler)


class StreamRequestHandler(BaseHTTPRequestHandler):
    """`GET /<anything>/<group>:<port>` streams that channel until the viewer leaves."""

    server: StreamProxyServer
    # Socket timeout for viewer connections. A viewer that vanishes without closing
    # the connection (Wi-Fi dropped, TV switched off) would otherwise block the
    # relay -- and keep the provider connection busy -- until TCP gives up, which
    # takes about 15 minutes.
    timeout = 30.0

    def do_HEAD(self) -> None:
        if self._requested_group() is not None:
            self._send_stream_headers()

    def do_GET(self) -> None:
        group = self._requested_group()
        if group is None:
            return
        server = self.server
        try:
            # Join the group before ffmpeg starts so no initial packets are missed.
            receiver = server.open_receiver(group, server.multicast_port, server.viewer_timeout)
        except OSError as error:
            log.error("%s: cannot receive the multicast group: %s", group, error)
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
            return
        with receiver:
            try:
                server.streams.acquire(group)
            except UnknownChannelError:
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown channel")
                return
            except StreamManagerClosedError:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Shutting down")
                return
            try:
                self._stream(receiver, group)
            finally:
                server.streams.release(group)

    def log_message(self, format: str, *args: Any) -> None:
        log.info("Stream proxy: %s - %s", self.address_string(), format % args)

    def _requested_group(self) -> str | None:
        """The requested channel's group, or None after replying 404."""
        target = parse_stream_path(self.path)
        if (
            target is None
            or target[1] != self.server.multicast_port
            or not self.server.streams.has_channel(target[0])
        ):
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown channel")
            return None
        return target[0]

    def _send_stream_headers(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", STREAM_CONTENT_TYPE)
        self.end_headers()

    def _stream(self, receiver: socket.socket, group: str) -> None:
        """Send the stream to the viewer until it leaves or the stream stalls."""
        viewer = self.address_string()
        try:
            self._send_stream_headers()
        except OSError:
            log.info("%s: viewer %s left.", group, viewer)
            return
        buffer = bytearray(multicast.MAX_DATAGRAM_SIZE)
        view = memoryview(buffer)
        while True:
            try:
                size = receiver.recv_into(buffer)
            except TimeoutError:
                log.warning(
                    "%s: no data for %g s, disconnecting viewer %s.",
                    group,
                    self.server.viewer_timeout,
                    viewer,
                )
                return
            except OSError as error:
                log.error("%s: receiving failed, disconnecting viewer %s: %s", group, viewer, error)
                return
            try:
                self.wfile.write(view[:size])
            except OSError:  # disconnected, or not reading for `timeout` seconds
                log.info("%s: viewer %s left.", group, viewer)
                return
