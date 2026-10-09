import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from udp_multicast_proxy.streams import (
    StreamManager,
    StreamManagerClosedError,
    UnknownChannelError,
)

CHANNELS = {"239.123.1.1": "http://provider/1", "239.123.1.2": "http://provider/2"}


class FakeRestream:
    def __init__(self, group: str, url: str, events: list[str]) -> None:
        self.group, self.url, self.events = group, url, events

    def start(self) -> None:
        self.events.append(f"start {self.group} {self.url}")

    def stop(self) -> None:
        self.events.append(f"stop {self.group}")


@pytest.fixture
def events() -> list[str]:
    return []


@pytest.fixture
def manager(events: list[str]) -> StreamManager:
    return StreamManager(CHANNELS.get, lambda group, url: FakeRestream(group, url, events))


def test_first_viewer_starts_and_last_viewer_stops(
    manager: StreamManager, events: list[str]
) -> None:
    manager.acquire("239.123.1.1")
    manager.acquire("239.123.1.1")
    assert events == ["start 239.123.1.1 http://provider/1"]
    assert manager.active_channel_count == 1

    manager.release("239.123.1.1")
    assert events == ["start 239.123.1.1 http://provider/1"]

    manager.release("239.123.1.1")
    assert events == ["start 239.123.1.1 http://provider/1", "stop 239.123.1.1"]
    assert manager.active_channel_count == 0


def test_channels_are_independent(manager: StreamManager, events: list[str]) -> None:
    manager.acquire("239.123.1.1")
    manager.acquire("239.123.1.2")
    manager.release("239.123.1.1")

    assert events == [
        "start 239.123.1.1 http://provider/1",
        "start 239.123.1.2 http://provider/2",
        "stop 239.123.1.1",
    ]
    assert manager.active_channel_count == 1


def test_restarting_a_channel_after_it_stopped(manager: StreamManager, events: list[str]) -> None:
    for _ in range(2):
        with manager.viewing("239.123.1.1"):
            pass

    assert events == ["start 239.123.1.1 http://provider/1", "stop 239.123.1.1"] * 2


def test_unknown_channel(manager: StreamManager, events: list[str]) -> None:
    assert not manager.has_channel("239.9.9.9")
    with pytest.raises(UnknownChannelError):
        manager.acquire("239.9.9.9")

    assert events == []
    assert manager.active_channel_count == 0


def test_release_without_acquire_is_ignored(manager: StreamManager, events: list[str]) -> None:
    manager.release("239.123.1.1")

    assert events == []


def test_viewing_releases_on_error(manager: StreamManager, events: list[str]) -> None:
    with pytest.raises(RuntimeError), manager.viewing("239.123.1.1"):
        raise RuntimeError

    assert events[-1] == "stop 239.123.1.1"


def test_channel_url_is_resolved_when_the_stream_starts(events: list[str]) -> None:
    channels = dict(CHANNELS)
    manager = StreamManager(channels.get, lambda group, url: FakeRestream(group, url, events))
    channels["239.123.1.1"] = "http://provider/refreshed"

    manager.acquire("239.123.1.1")

    assert events == ["start 239.123.1.1 http://provider/refreshed"]


def test_shutdown_stops_everything_and_refuses_new_viewers(
    manager: StreamManager, events: list[str]
) -> None:
    manager.acquire("239.123.1.1")
    manager.acquire("239.123.1.2")

    manager.shutdown()

    assert sorted(events[2:]) == ["stop 239.123.1.1", "stop 239.123.1.2"]
    with pytest.raises(StreamManagerClosedError):
        manager.acquire("239.123.1.1")
    manager.release("239.123.1.1")  # a viewer leaving after shutdown is harmless
    assert manager.active_channel_count == 0


def test_concurrent_viewers_start_and_stop_each_channel_once() -> None:
    lock = threading.Lock()
    running: dict[str, int] = {}
    starts: list[str] = []

    class CountingRestream:
        def __init__(self, group: str) -> None:
            self.group = group

        def start(self) -> None:
            with lock:
                running[self.group] = running.get(self.group, 0) + 1
                starts.append(self.group)
                assert running[self.group] == 1, "two restreamers for one channel"

        def stop(self) -> None:
            with lock:
                running[self.group] -= 1

    manager = StreamManager(CHANNELS.get, lambda group, _url: CountingRestream(group))
    barrier = threading.Barrier(16)

    def viewer(index: int) -> None:
        group = list(CHANNELS)[index % 2]
        barrier.wait()
        for _ in range(200):
            with manager.viewing(group):
                pass

    with ThreadPoolExecutor(16) as pool:
        list(pool.map(viewer, range(16)))

    assert running == {"239.123.1.1": 0, "239.123.1.2": 0}
    assert manager.active_channel_count == 0
    assert len(starts) >= 2
