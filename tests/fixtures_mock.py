"""Fixture for the fake OpenAI server (devtools/mock_openai.py). Registered by tests/conftest.py. Sets nothing global:
tests pass `mock_openai.base_url` to their client explicitly."""
from __future__ import annotations

from typing import Iterator

import pytest

from devtools.mock_openai import MockServer, start_mock_server


@pytest.fixture
def mock_openai() -> Iterator[MockServer]:
    server = start_mock_server()
    try:
        yield server
    finally:
        server.stop()
