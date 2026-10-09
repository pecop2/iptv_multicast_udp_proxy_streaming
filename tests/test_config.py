from pathlib import Path

import pytest

from udp_multicast_proxy.config import DEFAULT_UPSTREAM_USER_AGENT, ConfigError, Settings

REQUIRED = {"ORIGINAL_M3U_URL": "http://provider.example/list.m3u", "HOST_IP": "192.168.1.2"}


def test_defaults() -> None:
    settings = Settings.from_env(REQUIRED)

    assert settings.original_m3u_urls == ("http://provider.example/list.m3u",)
    assert settings.host_ip == "192.168.1.2"
    assert settings.number_of_clients == 3
    assert settings.ffmpeg_path == "ffmpeg"
    assert settings.upstream_user_agent == DEFAULT_UPSTREAM_USER_AGENT == "VLC/3.0.23 LibVLC/3.0.23"
    assert settings.log_level == "INFO"
    assert (settings.playlist_port, settings.stream_proxy_port) == (8010, 8011)
    assert settings.multicast_port == 5004
    assert settings.playlist_dir == Path("data/web_server_m3u")
    assert settings.playlist_refresh_interval == 12 * 3600
    assert settings.viewer_timeout == 10


def test_optional_variables() -> None:
    settings = Settings.from_env(
        REQUIRED
        | {
            "NUMBER_OF_CLIENTS": " 7 ",
            "FFMPEG_PATH": "/opt/ffmpeg/bin/ffmpeg",
            "UPSTREAM_USER_AGENT": "TiviMate/5.0",
            "LOG_LEVEL": "debug",
        }
    )

    assert settings.number_of_clients == 7
    assert settings.ffmpeg_path == "/opt/ffmpeg/bin/ffmpeg"
    assert settings.upstream_user_agent == "TiviMate/5.0"
    assert settings.log_level == "DEBUG"


def test_blank_optional_variables_fall_back_to_defaults() -> None:
    settings = Settings.from_env(
        REQUIRED | {"NUMBER_OF_CLIENTS": "", "FFMPEG_PATH": " ", "UPSTREAM_USER_AGENT": ""}
    )

    assert settings.number_of_clients == 3
    assert settings.ffmpeg_path == "ffmpeg"
    assert settings.upstream_user_agent == DEFAULT_UPSTREAM_USER_AGENT


@pytest.mark.parametrize("missing", ["ORIGINAL_M3U_URL", "HOST_IP"])
@pytest.mark.parametrize("value", [None, "", "  \n "], ids=["unset", "empty", "blank"])
def test_required_variables(missing: str, value: str | None) -> None:
    environ = {name: v for name, v in REQUIRED.items() if name != missing}
    if value is not None:
        environ[missing] = value

    with pytest.raises(ConfigError, match=f"{missing} is required"):
        Settings.from_env(environ)


@pytest.mark.parametrize(
    "url", ["your_m3u_url_here", "ftp://provider.example/list.m3u", "http://", "provider/list.m3u"]
)
def test_rejects_non_http_playlist_url(url: str) -> None:
    with pytest.raises(ConfigError, match="ORIGINAL_M3U_URL") as raised:
        Settings.from_env(REQUIRED | {"ORIGINAL_M3U_URL": url})

    # The value is not echoed: it may hold the account's credentials.
    assert str(raised.value) == (
        "ORIGINAL_M3U_URL must hold http:// or https:// URLs separated by spaces; it is not one."
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://a.example/list.m3u https://b.example:8443/get.php?u=1&p=2",
        "  http://a.example/list.m3u\n\thttps://b.example:8443/get.php?u=1&p=2\n",
    ],
    ids=["spaces", "newlines and tabs"],
)
def test_several_playlist_urls(value: str) -> None:
    settings = Settings.from_env(REQUIRED | {"ORIGINAL_M3U_URL": value})

    assert settings.original_m3u_urls == (
        "http://a.example/list.m3u",
        "https://b.example:8443/get.php?u=1&p=2",
    )


def test_names_the_invalid_url_by_position() -> None:
    with pytest.raises(ConfigError) as raised:
        Settings.from_env(REQUIRED | {"ORIGINAL_M3U_URL": "http://a.example/x your_m3u_url_here"})

    assert str(raised.value) == (
        "ORIGINAL_M3U_URL must hold http:// or https:// URLs separated by spaces; URL 2 is not one."
    )


def test_rejects_duplicate_playlist_urls() -> None:
    value = "http://a.example/x http://b.example/y http://a.example/x"

    with pytest.raises(ConfigError, match="lists URL 3 more than once"):
        Settings.from_env(REQUIRED | {"ORIGINAL_M3U_URL": value})


@pytest.mark.parametrize("value", ["three", "0", "-1", "2.5"])
def test_rejects_invalid_number_of_clients(value: str) -> None:
    with pytest.raises(ConfigError, match="NUMBER_OF_CLIENTS"):
        Settings.from_env(REQUIRED | {"NUMBER_OF_CLIENTS": value})


def test_rejects_unknown_log_level() -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Settings.from_env(REQUIRED | {"LOG_LEVEL": "verbose"})
