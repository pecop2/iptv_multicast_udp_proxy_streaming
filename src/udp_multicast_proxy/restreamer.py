"""Keeping one ffmpeg process running for a channel while it has viewers."""

import itertools
import logging
import re
import shlex
import subprocess
import threading
from collections.abc import Callable, Iterable, Sequence
from typing import Protocol

from .redaction import redact

log = logging.getLogger(__name__)

RESTART_DELAY = 2.0  # seconds; keeps a broken channel from hammering the provider
STOP_TIMEOUT = 5.0  # seconds to wait for ffmpeg to exit before killing it

# ffmpeg 9.0.2 prints this note at error level whenever it parses the "u" stream
# specifier (a leftover debug message in fftools/cmdutils.c); it is not an error.
_BENIGN_LOG_LINES = frozenset({"[error] Parsed 'usable only'"})
_LOG_LEVEL_TAG = re.compile(r"\[(warning|error|fatal|panic)\]")


class Process(Protocol):
    """The part of `subprocess.Popen` the restreamer uses."""

    @property
    def stderr(self) -> Iterable[str] | None: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


def spawn_ffmpeg(command: Sequence[str]) -> Process:
    """Start ffmpeg detached from our stdin/stdout, with its log on a pipe."""
    return subprocess.Popen(  # noqa: S603 - argument list we built ourselves, no shell
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class Restreamer:
    """Runs ffmpeg for one channel and restarts it whenever it exits, until stopped.

    Restarting mirrors the VLC-based version, which played each channel in "repeat"
    mode: a dropped provider connection or the end of a VOD file starts it again.
    """

    def __init__(
        self,
        name: str,
        command: Callable[[], Sequence[str]],
        *,
        spawn: Callable[[Sequence[str]], Process] = spawn_ffmpeg,
        restart_delay: float = RESTART_DELAY,
        stop_timeout: float = STOP_TIMEOUT,
    ) -> None:
        self._name = name
        self._command = command
        self._spawn = spawn
        self._restart_delay = restart_delay
        self._stop_timeout = stop_timeout
        self._stopping = threading.Event()
        self._lock = threading.Lock()  # guards _process against a concurrent stop()
        self._process: Process | None = None
        self._thread = threading.Thread(target=self._supervise, name=f"ffmpeg {name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Terminate ffmpeg and stop restarting it.

        Returns once the current ffmpeg process (if any) has exited; no new one is
        started afterwards.
        """
        self._stopping.set()
        with self._lock:
            process = self._process
        if process is not None:
            self._terminate(process)

    def join(self, timeout: float | None = None) -> None:
        """Wait for the supervising thread to finish (after `stop`)."""
        self._thread.join(timeout)

    def _supervise(self) -> None:
        while not self._stopping.is_set():
            try:
                command = list(self._command())
            except Exception:
                log.exception("%s: could not prepare the ffmpeg command.", self._name)
            else:
                self._run(command)
            if self._stopping.wait(self._restart_delay):
                return

    def _run(self, command: list[str]) -> None:
        with self._lock:
            if self._stopping.is_set():
                return
            try:
                process = self._spawn(command)
            except OSError as error:
                log.error("%s: could not start ffmpeg: %s", self._name, error)
                return
            self._process = process
        log.debug("%s: started %s", self._name, _redacted(command))
        self._forward_log(process, _input_url(command))
        exit_code = process.wait()
        if not self._stopping.is_set():
            log.warning(
                "%s: ffmpeg exited with code %s; restarting in %g s.",
                self._name,
                exit_code,
                self._restart_delay,
            )

    def _forward_log(self, process: Process, source_url: str | None) -> None:
        """Relay ffmpeg's log lines until it closes stderr (when it exits)."""
        if process.stderr is None:
            return
        for raw_line in process.stderr:
            line = raw_line.rstrip()
            if not line or line in _BENIGN_LOG_LINES:
                continue
            if source_url:
                line = redact(line, source_url)  # e.g. "Error opening input file <URL>."
            tag = _LOG_LEVEL_TAG.search(line)
            level = logging.WARNING if tag is None or tag[1] == "warning" else logging.ERROR
            log.log(level, "%s: ffmpeg: %s", self._name, line)

    def _terminate(self, process: Process) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=self._stop_timeout)
        except subprocess.TimeoutExpired:
            log.warning("%s: ffmpeg did not exit on SIGTERM, killing it.", self._name)
            process.kill()
            process.wait()


def _input_url(command: Sequence[str]) -> str | None:
    """The URL ffmpeg reads from (the argument of `-i`)."""
    for option, value in itertools.pairwise(command):
        if option == "-i":
            return value
    return None


def _redacted(command: Sequence[str]) -> str:
    """The command for logging, without the source URL (it holds credentials)."""
    source_url = _input_url(command)
    return shlex.join("<channel URL>" if arg == source_url else arg for arg in command)
