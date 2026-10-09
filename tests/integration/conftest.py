"""Fixtures for the end-to-end tests: real ffmpeg, real multicast, a mock provider.

These tests send UDP multicast (TTL 1), so they only run when selected with
`-m integration` -- normally inside the Docker test image (see the README), where
the multicast stays on the container's network.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from .support import CHANNELS, FFMPEG, FFPROBE, Provider, Proxy, generate_media


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if FFMPEG and FFPROBE:
        return
    skip = pytest.mark.skip(reason="needs ffmpeg and ffprobe (or FFMPEG_PATH and FFPROBE_PATH)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("media")
    generate_media(root)
    return root


@pytest.fixture(scope="session")
def provider(media: Path) -> Iterator[Provider]:
    provider = Provider(media)
    for name, path in CHANNELS.items():
        provider.add_channel(name, path)
    yield provider
    provider.close()


@pytest.fixture(scope="session")
def proxy(provider: Provider, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Proxy]:
    proxy = Proxy(provider, tmp_path_factory.mktemp("data"))
    yield proxy
    proxy.close()
