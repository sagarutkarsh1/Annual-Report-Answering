"""Fixtures for the synthetic annual report (see scripts/make_sample_pdf.py). Registered by tests/conftest.py."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.make_sample_pdf import build_sample_pdf, load_facts


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A freshly generated 60-page report ('Northbridge Energy plc Annual Report 2025/26'), printed folio = physical - 2.
    The `<name>.facts.json` sidecar sits next to it (see `sample_facts`)."""
    return build_sample_pdf(tmp_path_factory.mktemp("sample") / "sample_annual_report.pdf", pages=60, printed_offset=2, seed=0)


@pytest.fixture(scope="session")
def sample_facts(sample_pdf: Path) -> list[dict]:
    """Known facts of `sample_pdf`: id, kind, question, answer, key, page, printed_page, related_pages, quote, section_path."""
    return load_facts(sample_pdf)
