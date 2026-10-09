# syntax=docker/dockerfile:1

# Pinned versions: bump them deliberately (and re-run the tests).
ARG PYTHON_VERSION=3.14.8
ARG FFMPEG_VERSION=9.0.2
ARG UV_VERSION=0.12.24

# Static build of the latest ffmpeg release (linux/amd64 and linux/arm64).
FROM mwader/static-ffmpeg:${FFMPEG_VERSION} AS ffmpeg
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-trixie AS base
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install the locked dependencies, then the application, into /app/.venv.
FROM base AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Test image, see "Tests" in the README:
#   docker build --target test -t udp-multicast-proxy:test .
#   docker run --rm udp-multicast-proxy:test
# Building it runs the linters and type checker; running it runs every test,
# including the end-to-end tests with real ffmpeg and multicast.
FROM build AS test
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe
# Real DVB subtitles (from FFmpeg's own test suite) for the subtitle tests.
ADD --checksum=sha256:93ad6d0be649bb29697275ff522a983d475a1e58ab070271f912b86799e04a86 \
    https://fate-suite.ffmpeg.org/sub/dvbsubtest_filter.ts /opt/samples/dvbsubtest_filter.ts
ENV DVB_SUBTITLE_SAMPLE=/opt/samples/dvbsubtest_filter.ts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked
COPY tests ./tests
RUN .venv/bin/ruff format --check && .venv/bin/ruff check && .venv/bin/mypy
CMD [".venv/bin/pytest", "-m", "integration or not integration"]

FROM base AS runtime
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir /app/data \
    && chown app:app /app/data
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"
USER app
EXPOSE 8010 8011
HEALTHCHECK --interval=1m --timeout=10s --start-period=1m \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/channels_multicast.m3u', timeout=5)"]
CMD ["udp-multicast-proxy"]
