"""Helpers for the end-to-end tests: a mock provider, the app runner, a viewer."""

import http.client
import itertools
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from udp_multicast_proxy import app
from udp_multicast_proxy.config import Settings

FFMPEG = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg") or ""
FFPROBE = os.environ.get("FFPROBE_PATH") or shutil.which("ffprobe") or ""
# Real DVB subtitles cannot be generated with ffmpeg; the Docker test image downloads
# a sample from FFmpeg's test suite (FATE).
DVB_SUBTITLE_SAMPLE = os.environ.get("DVB_SUBTITLE_SAMPLE", "")
MEDIA_SECONDS = 30


def ffmpeg(*args: str) -> None:
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def probe_streams(path: Path) -> list[dict[str, Any]]:
    """The streams ffprobe finds in an MPEG-TS file."""
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name,height",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    streams: list[dict[str, Any]] = json.loads(result.stdout)["streams"]
    return streams


VIDEO = ["-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=25:duration={MEDIA_SECONDS}"]
TONE = ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={MEDIA_SECONDS}"]
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-b:v", "800k"]


def generate_media(root: Path) -> None:
    """Create the test channels' media in `root`."""
    # A typical IPTV channel: H.264 with two audio languages.
    ffmpeg(*VIDEO, *TONE,
           "-f", "lavfi", "-i", f"sine=frequency=880:sample_rate=48000:duration={MEDIA_SECONDS}",
           "-map", "0:v", "-map", "1:a", "-map", "2:a", *X264,
           "-c:a:0", "aac", "-c:a:1", "mp2",
           "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=deu",
           "-f", "mpegts", str(root / "multi_audio.ts"))  # fmt: skip
    # Declares an AC-3 track that only carries packets after 20 s.
    ffmpeg("-i", str(root / "multi_audio.ts"), "-itsoffset", "20", *TONE,
           "-map", "0:v", "-map", "0:a:0", "-map", "1:a", "-c:v", "copy", "-c:a:0", "copy",
           "-c:a:1", "ac3", "-t", str(MEDIA_SECONDS), "-f", "mpegts",
           str(root / "silent_track.ts"))  # fmt: skip
    # A radio channel: audio only.
    ffmpeg(*TONE, "-c:a", "mp2", "-f", "mpegts", str(root / "radio.ts"))
    # HLS with three quality levels.
    (root / "hls").mkdir()
    ffmpeg(*VIDEO, *TONE,
           "-filter_complex",
           "[0:v]split=3[a][b][c];[a]scale=256:144[v0];[b]scale=426:240[v1];[c]copy[v2]",
           "-map", "[v0]", "-map", "1:a", "-map", "[v1]", "-map", "1:a",
           "-map", "[v2]", "-map", "1:a",
           *X264, "-b:v:0", "150k", "-b:v:1", "400k", "-b:v:2", "800k", "-c:a", "aac",
           "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
           "-master_pl_name", "master.m3u8", "-var_stream_map", "v:0,a:0 v:1,a:1 v:2,a:2",
           "-hls_segment_filename", str(root / "hls" / "v%v_%03d.ts"),
           str(root / "hls" / "v%v.m3u8"))  # fmt: skip
    if DVB_SUBTITLE_SAMPLE:
        # Video, audio and the sample's sparse DVB subtitles (several seconds apart).
        ffmpeg("-i", str(root / "multi_audio.ts"), "-i", DVB_SUBTITLE_SAMPLE,
               "-map", "0:v", "-map", "0:a", "-map", "1:s", "-c", "copy",
               "-f", "mpegts", str(root / "dvb_subtitles.ts"))  # fmt: skip


@dataclass
class Request:
    path: str
    headers: dict[str, str]


class Provider:
    """A mock IPTV provider.

    `/live/<file>?<id>` streams the file in a loop at real-time speed after a 2 s
    burst, like live IPTV; `/once/<file>?<id>` stops after one pass;
    `/movie/.../<file>` is a plain download, like VOD; anything else is served as
    a static file. Every request is recorded.
    """

    def __init__(self, media: Path) -> None:
        self.media = media
        self.requests: list[Request] = []
        self.open_streams: dict[str, int] = {}
        self.lock = threading.Lock()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                provider._record(self)
                path = urlsplit(self.path).path
                kind, _, name = path.lstrip("/").partition("/")
                if kind in {"live", "once"}:
                    provider._stream(self, name, loop=kind == "live")
                elif path == "/list.m3u":
                    self._send(provider.playlist.encode())
                elif kind == "movie":  # VOD: a plain download, as fast as the network allows
                    self._send((provider.media / Path(path).name).read_bytes())
                else:
                    self._send((provider.media / path.lstrip("/")).read_bytes())

            def _send(self, body: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.playlist = "#EXTM3U\n"
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def add_channel(self, name: str, path: str) -> None:
        self.playlist += f'#EXTINF:-1 tvg-name="{name}",{name}\n{self.base_url}{path}\n'

    def requests_for(self, prefix: str) -> list[Request]:
        with self.lock:
            return [request for request in self.requests if request.path.startswith(prefix)]

    def open_stream_count(self, path: str) -> int:
        with self.lock:
            return self.open_streams.get(path, 0)

    def _record(self, handler: BaseHTTPRequestHandler) -> None:
        with self.lock:
            self.requests.append(Request(handler.path, dict(handler.headers.items())))

    def _stream(self, handler: BaseHTTPRequestHandler, name: str, *, loop: bool) -> None:
        data = (self.media / name).read_bytes()
        rate = len(data) / MEDIA_SECONDS
        chunk = 188 * 50
        handler.send_response(200)
        handler.send_header("Content-Type", "video/mp2t")
        handler.end_headers()
        with self.lock:
            self.open_streams[handler.path] = self.open_streams.get(handler.path, 0) + 1
        start, sent = time.monotonic(), 0
        try:
            while True:
                for offset in range(0, len(data), chunk):
                    piece = data[offset : offset + chunk]
                    handler.wfile.write(piece)
                    sent += len(piece)
                    ahead = (sent - 2 * rate) / rate - (time.monotonic() - start)
                    if ahead > 0:
                        time.sleep(ahead)
                if not loop:
                    return
        except OSError:
            pass
        finally:
            with self.lock:
                self.open_streams[handler.path] -= 1

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# Channel name -> provider path. Each test uses its own channel(s), so the
# provider's request log can be filtered per test.
CHANNELS = {
    "multi audio": "/live/multi_audio.ts?multi",
    "shared": "/live/multi_audio.ts?shared",
    "stop": "/live/multi_audio.ts?stop",
    "dvb subtitles": "/live/dvb_subtitles.ts?dvb",
    "silent track": "/live/silent_track.ts?silent",
    "radio": "/live/radio.ts?radio",
    "hls": "/hls/master.m3u8",
    "restart": "/once/radio.ts?restart",
    "vod": "/movie/user/pass/multi_audio.ts",
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class Proxy:
    """The application under test, running in a background thread."""

    def __init__(self, provider: Provider, data_dir: Path) -> None:
        self.settings = Settings(
            original_m3u_urls=(f"{provider.base_url}/list.m3u",),
            host_ip="127.0.0.1",
            ffmpeg_path=FFMPEG,
            playlist_port=free_port(),
            stream_proxy_port=free_port(),
            data_dir=data_dir,
        )
        self.stop = threading.Event()
        self.thread = threading.Thread(target=app.run, args=(self.settings, self.stop))
        self.thread.start()
        wait_until(lambda: self.channel_urls() != {}, timeout=30)

    def channel_urls(self) -> dict[str, str]:
        """Channel name -> proxy URL, read from the served playlist."""
        connection = http.client.HTTPConnection("127.0.0.1", self.settings.playlist_port, timeout=5)
        try:
            connection.request("GET", "/" + self.settings.playlist_file_name)
            lines = connection.getresponse().read().decode().splitlines()
        except OSError:
            return {}
        finally:
            connection.close()
        return {
            extinf.rpartition(",")[2]: url
            for extinf, url in zip(lines[1::2], lines[2::2], strict=True)
        }

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=30)


@dataclass
class Viewing:
    data: bytes
    status: int
    content_type: str | None
    arrivals: list[tuple[float, int]]  # (seconds since the request, bytes received)

    def largest_gap(self, after: float = 0.0) -> float:
        """The longest time without data, ignoring the first `after` seconds."""
        times = [moment for moment, _ in self.arrivals if moment >= after]
        return max((b - a for a, b in itertools.pairwise(times)), default=float("inf"))


def watch(url: str, seconds: float) -> Viewing:
    """Play a channel through the proxy for `seconds`, like an IPTV app would."""
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=15)
    start = time.monotonic()
    connection.request("GET", parts.path)
    response = connection.getresponse()
    data, arrivals = bytearray(), []
    while time.monotonic() - start < seconds:
        chunk = response.read1(65536)
        if not chunk:
            break
        data += chunk
        arrivals.append((time.monotonic() - start, len(chunk)))
    connection.close()
    return Viewing(bytes(data), response.status, response.getheader("Content-Type"), arrivals)


def wait_until(condition: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.1)
