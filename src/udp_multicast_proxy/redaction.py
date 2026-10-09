"""Keeping provider credentials out of log messages.

IPTV providers put the account's username and password in their URLs, e.g.
`/get.php?username=...&password=...` for the playlist or `/live/<user>/<pass>/1.ts`
for a channel, and error messages from requests and ffmpeg quote those URLs.
"""

from urllib.parse import urlsplit

REDACTED = "<redacted>"


def url_origin(url: str) -> str:
    """The part of `url` that is safe to log: scheme, host and port."""
    parts = urlsplit(url)
    host_and_port = parts.netloc.rpartition("@")[2]  # drop any user:password@
    return f"{parts.scheme}://{host_and_port}"


def redact(text: str, url: str) -> str:
    """`text` without `url`, or the path and query of `url` that requests quotes."""
    parts = urlsplit(url)
    text = text.replace(url, f"{url_origin(url)}/{REDACTED}")
    path_and_query = parts.path + (f"?{parts.query}" if parts.query else "")
    if len(path_and_query) > 1:
        text = text.replace(path_and_query, f"/{REDACTED}")
    return text
