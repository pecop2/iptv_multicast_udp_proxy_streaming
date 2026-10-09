import pytest

from udp_multicast_proxy.redaction import redact, url_origin

PLAYLIST = "http://provider.example:8080/get.php?username=USER&password=PASS&type=m3u_plus"


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        (PLAYLIST, "http://provider.example:8080"),
        ("https://user:secret@cdn.example/list.m3u", "https://cdn.example"),
        ("http://10.0.0.1/live/USER/PASS/1.ts", "http://10.0.0.1"),
    ],
)
def test_url_origin(url: str, origin: str) -> None:
    assert url_origin(url) == origin


def test_redacts_the_path_and_query_quoted_by_requests() -> None:
    message = (
        "HTTPConnectionPool(host='provider.example', port=8080): Max retries exceeded with url: "
        "/get.php?username=USER&password=PASS&type=m3u_plus (Caused by NewConnectionError("
        "'Failed to establish a new connection: [Errno 111] Connection refused'))"
    )

    redacted = redact(message, PLAYLIST)

    assert "USER" not in redacted
    assert "PASS" not in redacted
    assert "with url: /<redacted> (Caused by" in redacted
    assert "Connection refused" in redacted


def test_redacts_the_full_url_quoted_by_ffmpeg() -> None:
    url = "http://user:secret@provider.example/live/USER/PASS/1.ts"
    line = f"[error] Error opening input file {url}."

    assert (
        redact(line, url) == "[error] Error opening input file http://provider.example/<redacted>."
    )


def test_leaves_unrelated_text_alone() -> None:
    assert (
        redact("Read timed out. (read timeout=60)", PLAYLIST) == "Read timed out. (read timeout=60)"
    )
    assert redact("path / stays", "http://provider.example/") == "path / stays"
