"""Shared pytest fixtures.

Extra fixture modules (tests/fixtures_*.py) are picked up automatically when they exist, so module owners can ship
fixtures without editing this file:
    tests/fixtures_sample.py   -> `sample_pdf` (session-scoped Path to a generated annual-report PDF)
    tests/fixtures_mock.py     -> `mock_openai` (running fake OpenAI server; see docs/ARCHITECTURE.md section 8)
"""
from __future__ import annotations

import importlib.util
import os

os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")      # exactly "true"; must precede the first ragas import
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import pytest

from reportlens.config import Settings, load_settings

pytest_plugins = [
    name for name in ("tests.fixtures_sample", "tests.fixtures_mock")
    if importlib.util.find_spec(name) is not None
]


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Isolated Settings: fresh data dir, no real key, eval on, no .env loading."""
    s = load_settings(environ={"OPENAI_API_KEY": "sk-test-not-real"})
    return s.with_(data_dir=tmp_path / "data")
