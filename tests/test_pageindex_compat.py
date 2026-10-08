"""reportlens.pageindex_compat against the real PageIndex SDK and the fake OpenAI server (devtools.mock_openai)."""
from __future__ import annotations

import io
import logging
import os
import threading
from pathlib import Path
from typing import Iterator

import pytest

from devtools.mock_openai import MockServer
from reportlens import pageindex_compat as pc
from reportlens.config import Settings
from reportlens.pdfutil import PDFIUM_LOCK, extract_page_texts
from tests.pdf_factory import H, _canvas, lines_at

SID = "a" * 32

_TOP = ["Strategic report", "Governance", "Financial statements", "Shareholder information"]
_SUB = ["Chairman's statement", "Chief executive's review", "Our business model", "Risk management", "Board of directors",
        "Audit committee report", "Remuneration report", "Income statement", "Balance sheet", "Cash flow statement",
        "Notes to the accounts", "Five year summary", "Glossary of terms", "Contact details"]


def outlined_pdf(path: Path, pages: int = 12, *, bookmarks: bool = True) -> Path:
    """A small text PDF.  With bookmarks Flash builds its tree from the outline in about a second (the 60-page sample takes
    ~7 s); without bookmarks and headings a document over 10 pages is a 'flat pages' tree that Flash refuses.  Titles are all distinct on
    purpose: a counter-style outline ('Chapter 1', 'Chapter 2'...) is classified as noise and ignored by the SDK."""
    buf = io.BytesIO()
    c = _canvas(buf)
    for i in range(pages):
        top = i % 3 == 0
        title = (_TOP[i // 3] if i // 3 < len(_TOP) else f"Appendix {i // 3}") if top else _SUB[i % len(_SUB)]
        if bookmarks:
            c.bookmarkPage(f"p{i}")
            c.addOutlineEntry(title, f"p{i}", level=0 if top else 1)
        if bookmarks:           # a heading would let Flash detect a structure from the layout alone
            c.setFont("VeraBd", 18)
            c.drawString(50, H - 60, title)
        lines_at(c, 50, 90, [f"Paragraph {k} of page {i + 1}: the group delivered revenue of {1000 + i * 10 + k} million "
                             f"in the year, up on last year." for k in range(30)])
        c.showPage()
    c.save()
    path.write_bytes(buf.getvalue())
    return path


@pytest.fixture(autouse=True)
def clean_sdk_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts and ends with an unpatched SDK, the original OpenAI environment and no real key in sight."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-original")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    pc.remove_patches()
    pc.restore_openai_env()
    yield
    pc.set_page_text_listener(None)
    pc.remove_patches()
    pc.restore_openai_env()


@pytest.fixture
def wired(settings: Settings, mock_openai: MockServer) -> Settings:
    return settings.with_(openai_api_key="sk-test-wiring", openai_base_url=mock_openai.base_url, index_model="idx-model-x",
                          chat_model="chat-model-x")


# ------------------------------------------------------------------------------------------------ patches
def test_apply_patches_is_idempotent_and_replaces_page_text() -> None:
    from pageindex.local_api import LocalAPI

    assert not pc.is_patched()
    pc.apply_patches()
    first = LocalAPI.__dict__["_extract_page_texts"]
    pc.apply_patches()
    assert pc.is_patched()
    assert LocalAPI.__dict__["_extract_page_texts"] is first
    assert os.environ["OPENAI_AGENTS_DISABLE_TRACING"] == "1"
    from pageindex.flash import api as flash_api
    for name in ("extract_toc", "_validate_pdf"):
        wrapper = getattr(flash_api, name)
        assert not hasattr(wrapper.__wrapped__, "__wrapped__"), "wrapped twice"


def test_apply_patches_is_thread_safe() -> None:
    from pageindex.flash import api as flash_api

    barrier = threading.Barrier(8)

    def go() -> None:
        barrier.wait()
        pc.apply_patches()

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert pc.is_patched()
    assert not hasattr(flash_api.extract_toc.__wrapped__, "__wrapped__")


def test_remove_patches_restores_the_sdk() -> None:
    from pageindex.flash import api as flash_api
    from pageindex.local_api import LocalAPI

    original_extract = LocalAPI.__dict__["_extract_page_texts"]
    original_toc = flash_api.extract_toc
    pc.apply_patches()
    pc.remove_patches()
    assert LocalAPI.__dict__["_extract_page_texts"] is original_extract
    assert flash_api.extract_toc is original_toc
    assert not pc.is_patched()


def test_missing_sdk_seam_degrades_loudly_without_crashing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from pageindex.local_api import LocalAPI

    monkeypatch.delattr(LocalAPI, "_extract_page_texts")
    with caplog.at_level(logging.WARNING, logger="reportlens.pageindex_compat"):
        pc.apply_patches()
    assert not pc.is_patched()
    assert any("PyPDF2" in r.getMessage() for r in caplog.records)


def test_unexpected_sdk_version_warns_but_still_patches(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(pc, "pageindex_version", lambda: "0.2.99")
    with caplog.at_level(logging.WARNING, logger="reportlens.pageindex_compat"):
        pc.apply_patches()
    assert any("0.2.99" in r.getMessage() and "tested" in r.getMessage() for r in caplog.records)
    assert pc.is_patched()


def test_missing_sdk_is_a_warning_not_a_crash(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(pc, "pageindex_version", lambda: None)
    with caplog.at_level(logging.WARNING, logger="reportlens.pageindex_compat"):
        pc.apply_patches()
    assert not pc.is_patched()
    assert any("not installed" in r.getMessage() for r in caplog.records)


def test_existing_trace_provider_is_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    import agents.tracing.setup as tracing_setup

    calls: list[bool] = []

    class Provider:
        def set_disabled(self, disabled: bool) -> None:
            calls.append(disabled)

    monkeypatch.setattr(tracing_setup, "GLOBAL_TRACE_PROVIDER", Provider())
    pc.apply_patches()
    assert calls == [True]


def test_pdfium_lock_blocks_the_sdk_while_another_thread_uses_pdfium(tmp_path: Path) -> None:
    from pageindex.flash import api as flash_api

    pdf = outlined_pdf(tmp_path / "r.pdf", 3)
    pc.apply_patches()
    done = threading.Event()
    threading.Thread(target=lambda: (flash_api._validate_pdf(pdf), done.set()), daemon=True).start()
    done.wait(5)                      # unlocked: validating a 3-page PDF is instant
    assert done.is_set()
    done.clear()
    with PDFIUM_LOCK:                 # the viewer / locator is inside PDFium now
        t = threading.Thread(target=lambda: (flash_api._validate_pdf(pdf), done.set()), daemon=True)
        t.start()
        assert not done.wait(0.4)
    assert done.wait(5)
    t.join(timeout=5)


# ------------------------------------------------------------------------------------------------ page text
def test_page_text_listener_sees_start_and_done(tmp_path: Path) -> None:
    pdf = outlined_pdf(tmp_path / "r.pdf", 4)
    pc.apply_patches()
    events: list[str] = []
    pc.set_page_text_listener(events.append)
    from pageindex.local_api import LocalAPI

    texts = LocalAPI._extract_page_texts(str(pdf))
    assert events == ["start", "done"]
    assert texts == extract_page_texts(pdf)


def test_a_failing_listener_never_breaks_extraction(tmp_path: Path) -> None:
    pdf = outlined_pdf(tmp_path / "r.pdf", 3)
    pc.apply_patches()

    def boom(_: str) -> None:
        raise RuntimeError("listener bug")

    pc.set_page_text_listener(boom)
    from pageindex.local_api import LocalAPI

    assert len(LocalAPI._extract_page_texts(str(pdf))) == 3


def test_get_page_content_returns_pdfium_text(wired: Settings, tmp_path: Path) -> None:
    pdf = outlined_pdf(tmp_path / "r.pdf", 12)
    client = pc.make_client(wired, SID, for_indexing=True)
    doc_id = client.submit_document(str(pdf))["doc_id"]
    expected = extract_page_texts(pdf)
    for page in (1, 5, 12):
        got = client.get_page_content(doc_id, str(page))
        assert [p["page_index"] for p in got] == [page]
        assert got[0]["markdown"] == expected[page - 1]
    assert "\r" not in got[0]["markdown"]


# ------------------------------------------------------------------------------------------------ environment
def test_configure_openai_env_applies_once_per_change(settings: Settings) -> None:
    s = settings.with_(openai_api_key="sk-test-one", openai_base_url="http://127.0.0.1:1/v1")
    pc.configure_openai_env(s)
    assert os.environ["OPENAI_API_KEY"] == "sk-test-one"
    assert os.environ["OPENAI_BASE_URL"] == "http://127.0.0.1:1/v1"
    os.environ["OPENAI_API_KEY"] = "changed-behind-our-back"
    pc.configure_openai_env(s)                                    # same settings: the environment is not touched again
    assert os.environ["OPENAI_API_KEY"] == "changed-behind-our-back"
    pc.configure_openai_env(s.with_(openai_api_key="sk-test-two"))
    assert os.environ["OPENAI_API_KEY"] == "sk-test-two"


def test_configure_openai_env_restores_what_it_replaced(settings: Settings) -> None:
    pc.configure_openai_env(settings.with_(openai_api_key="sk-test-one", openai_base_url="http://127.0.0.1:1/v1"))
    pc.configure_openai_env(settings.with_(openai_api_key="sk-test-one", openai_base_url=None))
    assert "OPENAI_BASE_URL" not in os.environ                    # no stale mock URL once settings stop carrying one
    pc.restore_openai_env()
    assert os.environ["OPENAI_API_KEY"] == "sk-test-original"


def test_configure_openai_env_is_thread_safe(settings: Settings) -> None:
    s = settings.with_(openai_api_key="sk-test-race", openai_base_url="http://127.0.0.1:2/v1")
    threads = [threading.Thread(target=pc.configure_openai_env, args=(s,)) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    pc.restore_openai_env()
    assert os.environ["OPENAI_API_KEY"] == "sk-test-original"     # the saved original was never overwritten by a racing thread


def test_make_client_wires_key_url_models_and_storage(wired: Settings, mock_openai: MockServer, tmp_path: Path) -> None:
    pdf = outlined_pdf(tmp_path / "r.pdf", 12)
    client = pc.make_client(wired, SID, for_indexing=True)
    store = wired.session_dir(SID) / "pageindex"
    assert store.is_dir()
    assert Path(client.storage_path) == store
    assert client.index_model == "idx-model-x"
    doc_id = client.submit_document(str(pdf))["doc_id"]
    assert (store / "docs" / doc_id / "tree.json").is_file()
    sent = mock_openai.requests_of("index_leaf") + mock_openai.requests_of("index_parent")
    assert sent
    for req in mock_openai.requests:
        assert req["headers"]["authorization"] == "Bearer sk-test-wiring"
        assert req["path"].startswith("/v1/")
        assert req["json"]["model"] == "idx-model-x"


def test_a_client_carries_its_own_credentials(wired: Settings, mock_openai: MockServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever another session leaves in os.environ, this client still talks to its own server with its own key."""
    pdf = outlined_pdf(tmp_path / "r.pdf", 12)
    client = pc.make_client(wired, SID, for_indexing=True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-someone-else")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")      # nothing listens here
    client.submit_document(str(pdf))
    assert mock_openai.requests
    assert {r["headers"]["authorization"] for r in mock_openai.requests} == {"Bearer sk-test-wiring"}


def test_the_chat_lane_uses_the_same_key_and_url(wired: Settings, mock_openai: MockServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = outlined_pdf(tmp_path / "r.pdf", 12)
    doc_id = pc.make_client(wired, SID, for_indexing=True).submit_document(str(pdf))["doc_id"]
    client = pc.make_client(wired, SID)
    assert client.chat_model == "chat-model-x"
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    mock_openai.clear_requests()
    answer = client.chat([{"role": "user", "content": "What did the group deliver?"}], doc_id=doc_id, protocol="responses",
                         citations=True)
    agent_calls = mock_openai.requests_of("agent")
    assert agent_calls and answer["output"]
    assert {r["headers"]["authorization"] for r in agent_calls} == {"Bearer sk-test-wiring"}
    assert all(r["path"] == "/v1/responses" and r["json"]["model"] == "chat-model-x" for r in agent_calls)


def test_standard_mode_client_has_no_flash_only_options(wired: Settings) -> None:
    flash = pc.make_client(wired, SID, for_indexing=True)
    standard = pc.make_client(wired, SID, for_indexing=True, mode="standard")
    assert flash._api._summary_concurrency == wired.index_summary_concurrency
    assert standard._api._summary_concurrency is None        # the SDK refuses submit_document(mode="standard") otherwise


# ------------------------------------------------------------------------------------------------ health
def test_check_environment_reports_versions_and_wiring(wired: Settings) -> None:
    pc.apply_patches()
    env = pc.check_environment(wired)
    assert env["pageindex_version"] == pc.PAGEINDEX_TESTED_VERSION and env["pageindex_supported"]
    assert env["litellm_version"] and env["openai_agents_version"] and env["ragas_version"]
    assert env["openai_key"] is True
    assert env["base_url"] == wired.openai_base_url
    assert env["patched"] is True
    assert "sk-test-wiring" not in repr(env)


def test_check_environment_without_key_and_with_credentials_in_the_url(settings: Settings) -> None:
    s = settings.with_(openai_api_key=None, openai_base_url="https://user:secret@proxy.example.com:8443/v1?token=abc")
    env = pc.check_environment(s)
    assert env["openai_key"] is False
    assert env["base_url"] == "https://proxy.example.com:8443/v1"
    assert "secret" not in repr(env) and "abc" not in repr(env)


def test_importing_the_module_sets_privacy_defaults() -> None:
    assert os.environ["RAGAS_DO_NOT_TRACK"] == "true"
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"].lower() == "true"


# ------------------------------------------------------------------------------------------------ privacy
_PRIVACY_PROBE = r"""
import json, socket, sys
hosts = []
real_getaddrinfo = socket.getaddrinfo
def spy(host, *args, **kwargs):
    hosts.append(str(host))
    return real_getaddrinfo(host, *args, **kwargs)
socket.getaddrinfo = spy            # every name lookup, from asyncio, httpx, requests and urllib alike

from devtools.mock_openai import start_mock_server
from reportlens import pageindex_compat as pc
from reportlens.config import load_settings
from reportlens.indexer import IndexService, load_tree
from reportlens.store import Store
from reportlens.models import DocumentInfo
import pathlib, shutil

root = pathlib.Path(sys.argv[2])
server = start_mock_server()
cfg = load_settings(environ={}).with_(data_dir=root / "data", openai_api_key="sk-test-probe", openai_base_url=server.base_url)
store = Store(cfg.db_path)
sid = store.create_session().id
pdf = cfg.session_dir(sid) / "report.pdf"
pdf.parent.mkdir(parents=True)
shutil.copyfile(sys.argv[1], pdf)
store.put_document(sid, DocumentInfo(id="d", filename="report.pdf", doc_name="report.pdf", size_bytes=1))
svc = IndexService(cfg, store)
svc.start(sid, pdf)
import time
while store.get_document(sid).status == "indexing":
    time.sleep(0.05)
doc = store.get_document(sid)
answer = pc.make_client(cfg, sid).chat([{"role": "user", "content": "What did the group deliver?"}], doc_id=doc.pi_doc_id,
                                       protocol="responses", citations=True)
svc.shutdown()
store.close()
server.stop()
print("PROBE " + json.dumps({"status": doc.status, "hosts": sorted(set(hosts)), "answered": bool(answer["output"])}))
"""


@pytest.mark.slow
def test_nothing_leaves_the_machine_except_to_the_configured_base_url(tmp_path: Path) -> None:
    """A fresh interpreter (so import-time behaviour of litellm / openai-agents / PageIndex is covered too) indexes a PDF and
    asks one question against the local fake server; every host name it looked up must be loopback."""
    import json
    import subprocess
    import sys as _sys

    pdf = outlined_pdf(tmp_path / "r.pdf", 12)
    project_root = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI", "PAGEINDEX", "CHATGPT"))}
    env.update(PYTHONPATH=str(project_root), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([_sys.executable, "-W", "ignore", "-c", _PRIVACY_PROBE, str(pdf), str(tmp_path)], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=240)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE ")), None)
    assert line, f"probe failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    result = json.loads(line[len("PROBE "):])
    assert result["status"] == "ready" and result["answered"]
    assert result["hosts"], "the spy saw no lookups at all"
    assert set(result["hosts"]) <= {"127.0.0.1", "localhost"}, result["hosts"]
