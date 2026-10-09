"""Runtime settings, read from environment variables."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# The VLC-based version of this proxy fetched every stream with VLC 3.0.23's default
# User-Agent. Providers commonly filter by User-Agent (and often reject ffmpeg's own
# "Lavf/..."), so keep presenting the same one unless the user overrides it.
DEFAULT_UPSTREAM_USER_AGENT = "VLC/3.0.23 LibVLC/3.0.23"


class ConfigError(Exception):
    """Raised when the environment does not describe a usable configuration."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    """Everything the application needs to run.

    Only the fields read in `from_env` are user-configurable; the rest are fixed
    defaults that tests override.
    """

    original_m3u_urls: tuple[str, ...]  # merged in this order
    host_ip: str
    number_of_clients: int = 3
    ffmpeg_path: str = "ffmpeg"
    upstream_user_agent: str = DEFAULT_UPSTREAM_USER_AGENT
    log_level: str = "INFO"

    playlist_port: int = 8010
    stream_proxy_port: int = 8011
    multicast_port: int = 5004
    data_dir: Path = Path("data")
    playlist_file_name: str = "channels_multicast.m3u"
    playlist_refresh_interval: float = 12 * 60 * 60
    status_interval: float = 10 * 60
    viewer_timeout: float = 10.0

    @property
    def playlist_dir(self) -> Path:
        """Directory served by the playlist HTTP server."""
        return self.data_dir / "web_server_m3u"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Settings:
        """Build settings from environment variables, validating each one."""
        return cls(
            original_m3u_urls=_playlist_urls(environ, "ORIGINAL_M3U_URL"),
            host_ip=_required(environ, "HOST_IP"),
            number_of_clients=_positive_int(environ, "NUMBER_OF_CLIENTS", default=3),
            ffmpeg_path=environ.get("FFMPEG_PATH", "").strip() or "ffmpeg",
            upstream_user_agent=(
                environ.get("UPSTREAM_USER_AGENT", "").strip() or DEFAULT_UPSTREAM_USER_AGENT
            ),
            log_level=_log_level(environ),
        )


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Environment variable {name} is required.")
    return value


def _playlist_urls(environ: Mapping[str, str], name: str) -> tuple[str, ...]:
    """One or more http(s) URLs separated by whitespace (URLs cannot contain spaces).

    The URLs are not echoed in errors: they usually hold the account's credentials.
    """
    urls = tuple(_required(environ, name).split())
    for number, url in enumerate(urls, start=1):
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            what = f"URL {number}" if len(urls) > 1 else "it"
            raise ConfigError(
                f"{name} must hold http:// or https:// URLs separated by spaces; {what} is not one."
            )
        if url in urls[: number - 1]:
            raise ConfigError(f"{name} lists URL {number} more than once.")
    return urls


def _positive_int(environ: Mapping[str, str], name: str, *, default: int) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}.") from None
    if value < 1:
        raise ConfigError(f"{name} must be at least 1, got {value}.")
    return value


def _log_level(environ: Mapping[str, str]) -> str:
    level = environ.get("LOG_LEVEL", "").strip().upper() or "INFO"
    if level not in _LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}, got {level!r}.")
    return level


_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
