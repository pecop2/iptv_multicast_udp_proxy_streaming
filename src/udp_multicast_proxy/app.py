"""Application entry point: wiring, the playlist refresh loop and shutdown."""

import functools
import logging
import signal
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from types import FrameType

from . import hls
from .config import ConfigError, Settings
from .ffmpeg import MINIMUM_VERSION, CommandFactory, parse_version
from .playlist import ChannelPlaylist, PlaylistError
from .restreamer import Restreamer
from .servers import StreamProxyServer, create_playlist_server
from .streams import StreamManager

log = logging.getLogger(__name__)


def main() -> int:
    """Console entry point: configure from the environment and run until stopped."""
    try:
        settings = Settings.from_env()
    except ConfigError as error:
        _configure_logging("INFO")
        log.error("Configuration error: %s", error)
        return 2
    _configure_logging(settings.log_level)
    return run(settings, _stop_on_signals())


def run(settings: Settings, stop: threading.Event) -> int:
    """Serve the playlist and the streams until `stop` is set; return an exit code."""
    version_line = _ffmpeg_version(settings.ffmpeg_path)
    if version_line is None:
        return 1
    log.info("Using %s", version_line)
    if not _ffmpeg_is_recent_enough(version_line):
        return 1

    playlist = ChannelPlaylist(
        source_urls=settings.original_m3u_urls,
        output_path=settings.playlist_dir / settings.playlist_file_name,
        host_ip=settings.host_ip,
        proxy_port=settings.stream_proxy_port,
        multicast_port=settings.multicast_port,
    )
    try:
        playlist.refresh()
    except PlaylistError as error:
        log.error("%s", error)
        return 1

    commands = CommandFactory(
        ffmpeg_path=settings.ffmpeg_path,
        user_agent=settings.upstream_user_agent,
        multicast_port=settings.multicast_port,
        fetch_playlist=functools.partial(
            hls.fetch_playlist, user_agent=settings.upstream_user_agent
        ),
    )
    streams = StreamManager(
        playlist.upstream_url,
        lambda group, url: Restreamer(group, functools.partial(commands, group, url)),
    )
    try:
        servers: list[ThreadingHTTPServer] = [
            create_playlist_server(("", settings.playlist_port), settings.playlist_dir),
            StreamProxyServer(
                ("", settings.stream_proxy_port),
                streams=streams,
                multicast_port=settings.multicast_port,
                viewer_timeout=settings.viewer_timeout,
                backlog=settings.number_of_clients,
            ),
        ]
    except OSError as error:
        log.error("Cannot open the HTTP ports: %s", error)
        return 1

    for server, name in zip(servers, ("playlist server", "stream proxy"), strict=True):
        threading.Thread(target=server.serve_forever, name=name, daemon=True).start()
    log.info(
        "Playlist: http://%s:%d/%s",
        settings.host_ip,
        servers[0].server_address[1],
        settings.playlist_file_name,
    )
    log.info("Stream proxy: http://%s:%d", settings.host_ip, servers[1].server_address[1])

    try:
        _refresh_periodically(settings, playlist, streams, stop)
    finally:
        log.info("Shutting down...")
        for server in servers:
            server.shutdown()
        streams.shutdown()
        for server in servers:
            server.server_close()
    return 0


def _refresh_periodically(
    settings: Settings, playlist: ChannelPlaylist, streams: StreamManager, stop: threading.Event
) -> None:
    """Log the status regularly and refresh the playlist when it is due."""
    while True:
        _log_status(playlist, streams)
        if stop.wait(settings.status_interval):
            return
        updated_at = playlist.updated_at
        if (
            updated_at is None
            or time.monotonic() - updated_at >= settings.playlist_refresh_interval
        ):
            try:
                playlist.refresh()
            except PlaylistError as error:
                log.error("Playlist refresh failed, keeping the current playlist: %s", error)


def _log_status(playlist: ChannelPlaylist, streams: StreamManager) -> None:
    log.info("Channels active: %d", streams.active_channel_count)
    if playlist.updated_at is not None:
        age = int(time.monotonic() - playlist.updated_at)
        hours, rest = divmod(age, 3600)
        log.info("Playlist updated %dh %dm %ds ago.", hours, rest // 60, rest % 60)


def _ffmpeg_version(ffmpeg_path: str) -> str | None:
    """ffmpeg's version line, or None (after logging why) if it cannot run."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            [ffmpeg_path, "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        log.error("ffmpeg (%s) is not usable: %s", ffmpeg_path, error)
        return None
    return result.stdout.partition("\n")[0]


def _ffmpeg_is_recent_enough(version_line: str) -> bool:
    required = ".".join(map(str, MINIMUM_VERSION))
    version = parse_version(version_line)
    if version is None:
        log.warning("Could not tell the ffmpeg version; version %s or newer is needed.", required)
    elif version < MINIMUM_VERSION:
        log.error(
            "ffmpeg %d.%d is too old: version %s or newer is needed to keep every audio "
            "and subtitle track.",
            *version,
            required,
        )
        return False
    return True


def _stop_on_signals() -> threading.Event:
    """An event set by SIGTERM (e.g. `docker stop`) or SIGINT (Ctrl+C)."""
    stop = threading.Event()

    def handle(signum: int, _frame: FrameType | None) -> None:
        log.info("Received %s.", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    return stop


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stderr,
        force=True,
    )
