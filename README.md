# IPTV Multicast UDP Proxy streaming

## Idea
Most IPTV providers allow only one client at a time. This service lets several players share that one
connection: each channel is fetched from the provider once, restreamed by ffmpeg to a local UDP
multicast group, and served over HTTP to every player watching it. When the same channel is watched,
N players are served without problems. When the channels differ, it depends on the network connection
and stream quality.

- The provider's M3U playlist (or several playlists, merged) is rewritten so that every channel points
  at the local HTTP proxy, and served by a small file server. It is refreshed every 12 hours.
- The first viewer of a channel starts ffmpeg for it; further viewers share it; when the last viewer
  leaves, ffmpeg is stopped. If ffmpeg exits (provider dropped the connection, end of a VOD file), it is
  restarted while viewers remain.
- ffmpeg copies the audio and video untouched (no transcoding) into MPEG-TS.

```
provider ──HTTP──> ffmpeg ──UDP multicast 239.123.x.y:5004──> HTTP proxy :8011 ──> players
                                                                 playlist :8010 ──> players
```

## Configuration (environment variables)
- **ORIGINAL_M3U_URL**: the playlist URL provided by your IPTV provider. To combine several playlists
  (e.g. from different providers), list their URLs separated by spaces; their channels are merged in
  that order, and adding a playlist at the end doesn't change the URLs of the existing channels. If
  one of them can't be downloaded during a refresh, the current playlist is kept and the refresh is
  retried later; at startup the service exits (and Docker restarts it).
- **HOST_IP**: the local IP address of the machine running this (used in the rewritten playlist)
- **NUMBER_OF_CLIENTS** (default 3): the expected number of simultaneous viewers; sizes the proxy's
  connection queue
- **UPSTREAM_USER_AGENT** (optional): the User-Agent sent to the provider when fetching streams.
  Defaults to `VLC/3.0.23 LibVLC/3.0.23`, what the previous, VLC-based version sent, as many providers
  filter by User-Agent.
- **LOG_LEVEL** (optional, default `INFO`): `DEBUG`, `INFO`, `WARNING` or `ERROR`
- **FFMPEG_PATH** (optional): the ffmpeg binary to use when running without Docker, if `ffmpeg` on
  the PATH is not the one you want

## Docker build and run
- Change the environment variables in **docker-compose.yml** to your own values.
- Build and run: **make up** (or **docker compose up -d --build**). `make logs` follows the logs,
  `make down` stops it; `make` lists every target.
- The file server for the new playlist runs on port **8010**, the stream proxy on port **8011**.
  Don't change the host:container port mapping; the ports are part of the generated URLs.
- Download the new playlist from **http://your_host_ip:8010/channels_multicast.m3u** (your_host_ip
  is the value of **HOST_IP**) and add it to your IPTV app.
- The image is built for linux/amd64 and linux/arm64 (e.g. a 64-bit Raspberry Pi OS). It contains a
  static build of ffmpeg 9.0.2 and Python 3.14.

## Running without Docker (macOS, Linux)
Needs **ffmpeg 7.1 or newer** (older versions would silently drop audio and subtitle tracks, so
the service refuses to start with them) and [uv](https://docs.astral.sh/uv/), which also installs
Python 3.14 if needed.
- macOS: `brew install ffmpeg uv`
- Linux: ffmpeg 7.1+ from your distribution (Debian 13 has it; Ubuntu 24.04's 6.1 is too old) or a
  static build linked from https://ffmpeg.org/download.html, and uv from its installation page.

```sh
make install            # create .venv with the locked dependencies
cp .env.example .env    # then set ORIGINAL_M3U_URL and HOST_IP in .env
make run
```

On macOS, allow incoming connections for Python if the firewall asks: your IPTV apps connect to
ports 8010 and 8011.

## How channels are restreamed
Channel URLs are recognised by their extension:
- **Live MPEG-TS** (`.ts`, no extension, anything else; the usual IPTV channel): every video and
  audio track (all languages) and the DVB subtitle and teletext tracks are kept. Closed captions
  embedded in the video are kept too.
- **HLS** (`.m3u8`): of a playlist with several quality levels only the best one is downloaded.
  HLS subtitles (WebVTT) cannot be carried in MPEG-TS and are dropped.
- **Media files** (`.mp4`, `.mkv`, `.avi`, ..., and anything under `/movie/` or `/series/`, where
  Xtream-style providers serve VOD): played in real time; their text subtitles cannot be carried in
  MPEG-TS and are dropped. A finite `.ts` file at any other URL looks like a live stream and is not
  paced.

A track that a channel announces but does not currently send (e.g. an idle audio description track)
is left out instead of preventing the channel from starting.

## Multicast
- The multicast traffic is sent with TTL 1, so it is never routed beyond the local network segment.
- Not every network is multicast ready (enable IGMP snooping if your switch/router offers it), so
  beware, or specifically add routes to keep the multicast traffic on the loopback interface if your
  network gets flooded (when the router/switch treats it as broadcast). The packets are standard
  MPEG-TS over UDP, so players on a multicast-capable network can also open
  `udp://@239.123.x.y:5004` directly.

## Development
The project uses [uv](https://docs.astral.sh/uv/) with a lockfile (`uv.lock`). The Makefile wraps
the everyday commands (`make` lists them all):

```sh
make install     # uv sync: create .venv with the locked dependencies
make check       # lint, type check (mypy, strict) and the unit/component tests (~6 s)
make format      # format the code
make test-docker # every test, including the end-to-end tests, in the test image
make all         # check + test-docker + build the service image
make run         # run without Docker (see "Running without Docker")
```

### Tests
- `tests/`: unit tests, plus component tests with real sockets, real child processes and multicast
  that never leaves the machine (TTL 0).
- `tests/integration/`: end-to-end tests with real ffmpeg: a mock provider, the whole application
  and a viewer, covering multiple audio tracks, sparse DVB subtitles, HLS variant selection, silent
  tracks, radio, shared connections and restarts. They send multicast with TTL 1, so they are not
  run by default; run them in the test image, where the multicast stays on the container network:
  `make test-docker` (all tests) or `make test-integration` (end-to-end only). Building the test
  image also runs the linter and type checker. Without make:

```sh
docker build --target test -t udp-multicast-proxy:test .
docker run --rm udp-multicast-proxy:test
```

## Changes from the VLC-based version
- ffmpeg 9 replaces VLC. The multicast hop carries plain MPEG-TS over UDP (1316-byte datagrams)
  instead of RTP; players still receive the same MPEG-TS, now labelled `Content-Type: video/mp2t`.
  Channel URLs are unchanged, so playlists and favourites saved in IPTV apps keep working.
- Players are no longer pre-started: ffmpeg is started per channel on demand, which takes a fraction
  of a second. `NUMBER_OF_CLIENTS` now only sizes the connection queue.
- Several playlists can be combined (see **ORIGINAL_M3U_URL**), and provider credentials are kept
  out of the logs.
- A failed playlist refresh keeps the current playlist instead of stopping the service; requests for
  unknown channels get `404`; the container stops gracefully on `docker stop` and restarts
  automatically (`restart: unless-stopped`).
