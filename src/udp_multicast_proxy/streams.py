"""Running a channel's restreamer only while somebody is watching it."""

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


class UnknownChannelError(LookupError):
    """The requested multicast group is not assigned to any channel."""


class StreamManagerClosedError(RuntimeError):
    """The application is shutting down and no longer starts streams."""


class Restream(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


@dataclass(slots=True)
class _ActiveChannel:
    restream: Restream
    viewers: int = 0


class StreamManager:
    """Reference-counts viewers per channel.

    The first viewer of a channel starts its restreamer; every further viewer shares
    it; the last viewer to leave stops it.
    """

    def __init__(
        self,
        resolve_url: Callable[[str], str | None],
        create_restream: Callable[[str, str], Restream],
    ) -> None:
        self._resolve_url = resolve_url
        self._create_restream = create_restream
        self._lock = threading.Lock()
        self._active: dict[str, _ActiveChannel] = {}
        self._closed = False

    def has_channel(self, group: str) -> bool:
        return self._resolve_url(group) is not None

    @property
    def active_channel_count(self) -> int:
        with self._lock:
            return len(self._active)

    def acquire(self, group: str) -> None:
        """Register a viewer of `group`, starting its restreamer if needed."""
        with self._lock:
            if self._closed:
                raise StreamManagerClosedError
            channel = self._active.get(group)
            if channel is None:
                url = self._resolve_url(group)
                if url is None:
                    raise UnknownChannelError(group)
                log.info("%s: first viewer, starting the stream.", group)
                channel = _ActiveChannel(self._create_restream(group, url))
                channel.restream.start()
                self._active[group] = channel
            channel.viewers += 1
            log.info("%s: %d viewer(s).", group, channel.viewers)

    def release(self, group: str) -> None:
        """Unregister a viewer of `group`, stopping its restreamer after the last one."""
        with self._lock:
            channel = self._active.get(group)
            if channel is None:
                return
            channel.viewers -= 1
            if channel.viewers > 0:
                log.info("%s: %d viewer(s).", group, channel.viewers)
                return
            del self._active[group]
            log.info("%s: last viewer left, stopping the stream.", group)
            # Stopping while holding the lock means the next stream only starts
            # once this ffmpeg has disconnected, which matters for providers that
            # allow a single connection when a viewer switches channels.
            channel.restream.stop()

    @contextmanager
    def viewing(self, group: str) -> Iterator[None]:
        """`acquire` for the duration of the block, `release` afterwards."""
        self.acquire(group)
        try:
            yield
        finally:
            self.release(group)

    def shutdown(self) -> None:
        """Stop every running restreamer and refuse new viewers."""
        with self._lock:
            self._closed = True
            channels, self._active = list(self._active.values()), {}
            for channel in channels:
                channel.restream.stop()
