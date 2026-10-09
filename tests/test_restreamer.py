"""The supervisor is tested against real child processes (small Python scripts
standing in for ffmpeg), so signals, pipes and exit codes behave as in production."""

import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence

import pytest

from udp_multicast_proxy.restreamer import Process, Restreamer, spawn_ffmpeg

PYTHON = sys.executable


def script(source: str) -> list[str]:
    return [PYTHON, "-c", source]


SLEEPER = script(
    "import sys, time\nprint('[warning] Starting', file=sys.stderr, flush=True)\ntime.sleep(60)\n"
)
IGNORES_SIGTERM = script(
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('[warning] ready', file=sys.stderr, flush=True)\n"
    "time.sleep(60)\n"
)


class RecordingSpawner:
    """Spawns real processes and remembers them."""

    def __init__(self) -> None:
        self.processes: list[Process] = []
        self.started = threading.Condition()

    def __call__(self, command: Sequence[str]) -> Process:
        process = spawn_ffmpeg(command)
        with self.started:
            self.processes.append(process)
            self.started.notify_all()
        return process

    def wait_for(self, count: int, timeout: float = 10) -> None:
        with self.started:
            assert self.started.wait_for(lambda: len(self.processes) >= count, timeout)


@pytest.fixture
def spawner() -> Iterator[RecordingSpawner]:
    spawner = RecordingSpawner()
    yield spawner
    for process in spawner.processes:
        if process.poll() is None:
            process.kill()
            process.wait()


def wait_until(condition: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def test_stop_terminates_ffmpeg(spawner: RecordingSpawner) -> None:
    restreamer = Restreamer("239.1.1.1", lambda: SLEEPER, spawn=spawner)
    restreamer.start()
    spawner.wait_for(1)

    restreamer.stop()

    assert spawner.processes[0].poll() is not None
    restreamer.join(timeout=5)
    assert len(spawner.processes) == 1


def test_restarts_ffmpeg_whenever_it_exits(
    spawner: RecordingSpawner, caplog: pytest.LogCaptureFixture
) -> None:
    exits = script("import sys; sys.exit(3)")
    restreamer = Restreamer("239.1.1.1", lambda: exits, spawn=spawner, restart_delay=0.05)

    with caplog.at_level(logging.WARNING):
        restreamer.start()
        spawner.wait_for(3)
        restreamer.stop()
    restreamer.join(timeout=5)

    assert "239.1.1.1: ffmpeg exited with code 3; restarting in 0.05 s." in caplog.messages


def test_the_command_is_rebuilt_for_every_start(spawner: RecordingSpawner) -> None:
    calls: list[int] = []

    def command() -> list[str]:
        calls.append(1)
        return script("pass")

    restreamer = Restreamer("239.1.1.1", command, spawn=spawner, restart_delay=0.01)
    restreamer.start()
    spawner.wait_for(2)
    restreamer.stop()
    restreamer.join(timeout=5)

    assert len(calls) >= 2


def test_kills_ffmpeg_that_ignores_sigterm(
    spawner: RecordingSpawner, caplog: pytest.LogCaptureFixture
) -> None:
    restreamer = Restreamer("239.1.1.1", lambda: IGNORES_SIGTERM, spawn=spawner, stop_timeout=0.3)
    restreamer.start()
    wait_until(lambda: "ready" in caplog.text)

    with caplog.at_level(logging.WARNING):
        restreamer.stop()

    assert spawner.processes[0].poll() == -9
    assert "did not exit on SIGTERM" in caplog.text


def test_does_not_start_ffmpeg_after_stop(spawner: RecordingSpawner) -> None:
    released = threading.Event()

    def slow_command() -> list[str]:  # e.g. inspecting an HLS playlist
        released.wait(5)
        return SLEEPER

    restreamer = Restreamer("239.1.1.1", slow_command, spawn=spawner)
    restreamer.start()
    restreamer.stop()
    released.set()
    restreamer.join(timeout=5)

    assert spawner.processes == []


def test_forwards_ffmpeg_log_with_levels(
    spawner: RecordingSpawner, caplog: pytest.LogCaptureFixture
) -> None:
    noisy = script(
        "import sys\n"
        "print(\"[error] Parsed 'usable only'\", file=sys.stderr)\n"
        "print('[in#0/mpegts @ 0x1] [warning] Packet corrupt', file=sys.stderr)\n"
        "print('', file=sys.stderr)\n"
        "print('[tcp @ 0x2] [error] Connection refused', file=sys.stderr)\n"
        "print('[fatal] Error opening input files', file=sys.stderr)\n"
        "print('untagged line', file=sys.stderr)\n"
    )
    restreamer = Restreamer("239.1.1.1", lambda: noisy, spawn=spawner, restart_delay=60)

    with caplog.at_level(logging.WARNING, logger="udp_multicast_proxy.restreamer"):
        restreamer.start()
        wait_until(lambda: "exited with code 0" in caplog.text)
    restreamer.stop()

    forwarded = [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if "ffmpeg: " in record.getMessage()
    ]
    assert forwarded == [
        ("WARNING", "239.1.1.1: ffmpeg: [in#0/mpegts @ 0x1] [warning] Packet corrupt"),
        ("ERROR", "239.1.1.1: ffmpeg: [tcp @ 0x2] [error] Connection refused"),
        ("ERROR", "239.1.1.1: ffmpeg: [fatal] Error opening input files"),
        ("WARNING", "239.1.1.1: ffmpeg: untagged line"),
    ]


def test_missing_binary_is_logged_and_retried(caplog: pytest.LogCaptureFixture) -> None:
    attempts: list[int] = []

    def command() -> list[str]:
        attempts.append(1)
        return [os.devnull + "-no-such-ffmpeg"]

    restreamer = Restreamer("239.1.1.1", command, restart_delay=0.01)
    with caplog.at_level(logging.ERROR):
        restreamer.start()
        wait_until(lambda: len(attempts) >= 3)
        restreamer.stop()
    restreamer.join(timeout=5)

    assert "239.1.1.1: could not start ffmpeg" in caplog.text


def test_command_errors_are_logged_and_retried(caplog: pytest.LogCaptureFixture) -> None:
    attempts: list[int] = []

    def command() -> list[str]:
        attempts.append(1)
        raise ValueError("boom")

    restreamer = Restreamer("239.1.1.1", command, restart_delay=0.01)
    with caplog.at_level(logging.ERROR):
        restreamer.start()
        wait_until(lambda: len(attempts) >= 2)
        restreamer.stop()
    restreamer.join(timeout=5)

    assert "could not prepare the ffmpeg command" in caplog.text


def test_debug_log_hides_the_channel_url(
    spawner: RecordingSpawner, caplog: pytest.LogCaptureFixture
) -> None:
    command = [*script("pass"), "-i", "http://user:secret@provider/user/secret/1"]
    restreamer = Restreamer("239.1.1.1", lambda: command, spawn=spawner, restart_delay=60)

    with caplog.at_level(logging.DEBUG, logger="udp_multicast_proxy.restreamer"):
        restreamer.start()
        wait_until(lambda: "started" in caplog.text)
    restreamer.stop()

    assert "<channel URL>" in caplog.text
    assert "secret" not in caplog.text
