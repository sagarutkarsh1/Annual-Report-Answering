"""reportlens.indexer: real PageIndex SDK + fake OpenAI server for the happy path and the OpenAI failure modes, in-process fake
clients for scheduling, timeouts and the retry policy (those need exact control over what the 'SDK' does)."""
from __future__ import annotations

import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import pytest

from devtools.mock_openai import MockServer, start_mock_server
from reportlens import indexer, pageindex_compat as pc
from reportlens.config import Settings, load_settings
from reportlens.indexer import (CANCELLED_ERROR, NO_OUTLINE_NOTE, STAGES, IndexService, clone_index, count_nodes, load_page_texts,
                                load_tree, translate_error)
from reportlens.models import DocumentInfo
from reportlens.pdfutil import extract_page_texts
from reportlens.store import INTERRUPTED_INDEXING_ERROR, Store
from tests.pdf_factory import build_blank_pdf, build_encrypted_pdf
from tests.test_pageindex_compat import clean_sdk_state, outlined_pdf  # noqa: F401  (autouse fixture + small PDF builder)

FAKE_TREE = [{"title": "Strategic report", "node_id": "0000", "start_index": 1, "end_index": 12, "summary": "s",
              "nodes": [{"title": "Financial review", "node_id": "0001", "start_index": 3, "end_index": 6, "summary": "t"}]}]


# ------------------------------------------------------------------------------------------------ helpers
class Watcher:
    """Samples a session's document while it is indexing (what the browser's 1 s poll would see, only much faster)."""

    def __init__(self, store: Store, sid: str):
        self.store, self.sid = store, sid
        self.samples: list[tuple[str, str, float, Optional[str]]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _sample(self) -> None:
        doc = self.store.get_document(self.sid)
        if doc is not None:
            self.samples.append((doc.status, doc.stage, doc.progress, doc.description))

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample()
            time.sleep(0.01)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._sample()                      # the state the caller saw when it stopped waiting

    @property
    def stages(self) -> list[str]:
        out: list[str] = []
        for _, stage, _, _ in self.samples:
            if not out or out[-1] != stage:
                out.append(stage)
        return out

    @property
    def progress(self) -> list[float]:
        return [p for _, _, p, _ in self.samples]


def new_session(store: Store, cfg: Settings, pdf: Path, filename: str = "report.pdf") -> tuple[str, Path]:
    """A session with an uploaded document in status 'indexing', as the service leaves it before calling IndexService.start."""
    sid = store.create_session().id
    folder = cfg.session_dir(sid)
    folder.mkdir(parents=True)
    dest = folder / filename
    shutil.copyfile(pdf, dest)
    store.put_document(sid, DocumentInfo(id=uuid.uuid4().hex, filename=filename, doc_name=filename, size_bytes=dest.stat().st_size))
    return sid, dest


def wait_for(store: Store, sid: str, timeout: float = 90.0) -> DocumentInfo:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        doc = store.get_document(sid)
        if doc is not None and doc.status != "indexing":
            return doc
        time.sleep(0.02)
    raise AssertionError(f"indexing did not finish; last state {store.get_document(sid)}")


def run_job(svc: IndexService, store: Store, cfg: Settings, pdf: Path, timeout: float = 90.0) -> tuple[str, DocumentInfo, Watcher]:
    sid, dest = new_session(store, cfg, pdf)
    watcher = Watcher(store, sid)
    svc.start(sid, dest)
    try:
        doc = wait_for(store, sid, timeout)
    finally:
        watcher.stop()
    return sid, doc, watcher


class FakeClient:
    """Stands in for PageIndexClient: only what the indexer calls."""

    def __init__(self, submit: Callable[[str], dict]):
        self._submit = submit

    def submit_document(self, path: str, mode: Optional[str] = None) -> dict:
        return self._submit(path) if mode is None else self._submit(path + "::" + mode)

    def get_tree(self, doc_id: str, node_summary: bool = False, include_text: bool = True) -> dict:
        return {"result": FAKE_TREE}

    def get_document(self, doc_id: str) -> dict:
        return {"description": "A fake annual report."}


class FakeFactory:
    """client_factory double: records (index_summary_concurrency, mode) per client and delegates submit_document to `script`."""

    def __init__(self, script: Callable[[int, str], dict]):
        self.script = script
        self.calls: list[tuple[int, str]] = []
        self._n = 0
        self._lock = threading.Lock()

    def __call__(self, settings: Settings, session_id: str, *, for_indexing: bool = False, mode: str = "flash") -> FakeClient:
        assert for_indexing
        with self._lock:
            self.calls.append((settings.index_summary_concurrency, mode))

        def submit(path: str) -> dict:
            with self._lock:
                self._n += 1
                n = self._n
            return self.script(n, mode)

        return FakeClient(submit)


def ok(_n: int = 0, _mode: str = "flash") -> dict:
    return {"doc_id": "pi-" + "0" * 32, "name": "report.pdf"}


class RateLimited(Exception):
    """What the SDK raises when its 10 flat retries all hit 429 (LLMRetriesExhausted), wrapped like submit_document does."""

    def __init__(self) -> None:
        super().__init__("Failed to submit document: LLM completion failed after 10 retries: 429 Too Many Requests")
        self.__cause__ = type("LLMRetriesExhausted", (RuntimeError,), {})("rate limit reached")
        self.__cause__.status_code = 429  # type: ignore[attr-defined]


@pytest.fixture
def no_sdk_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK retries every failed model call 10 x with a fixed 1 s pause; make that instant so failure paths take ~1 s, not
    ~12 s per attempt (only `sleep` of the asyncio module the SDK's utils sees is replaced)."""
    import asyncio

    import pageindex.utils as sdk_utils

    class FastAsyncio:
        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

        @staticmethod
        async def sleep(_seconds: float) -> None:
            await asyncio.sleep(0)

    monkeypatch.setattr(sdk_utils, "asyncio", FastAsyncio())


@pytest.fixture
def mock() -> Iterator[MockServer]:
    server = start_mock_server()
    yield server
    server.stop()


@pytest.fixture
def cfg(settings: Settings, mock: MockServer) -> Settings:
    return settings.with_(openai_api_key="sk-test-indexer", openai_base_url=mock.base_url, index_model="idx-model-x")


@pytest.fixture
def store(cfg: Settings) -> Iterator[Store]:
    s = Store(cfg.db_path)
    yield s
    s.close()


@pytest.fixture
def make_service(cfg: Settings, store: Store) -> Iterator[Callable[..., IndexService]]:
    made: list[IndexService] = []

    def factory(client_factory: Any = pc.make_client, settings: Optional[Settings] = None, **kw: Any) -> IndexService:
        kw.setdefault("retry_pause_s", 0.0)
        svc = IndexService(settings or cfg, store, client_factory, **kw)
        made.append(svc)
        return svc

    yield factory
    for svc in made:
        svc.shutdown(wait=True, timeout=20)


@pytest.fixture(scope="session")
def small_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return outlined_pdf(tmp_path_factory.mktemp("small") / "report.pdf", 12)


@pytest.fixture(scope="session")
def flat_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """12 pages without any outline or headings: Flash can only offer one node per page and refuses that (> 10 pages)."""
    return outlined_pdf(tmp_path_factory.mktemp("flat") / "flat.pdf", 12, bookmarks=False)


# ------------------------------------------------------------------------------------------------ the full 60-page index
@pytest.fixture(scope="module")
def indexed60(tmp_path_factory: pytest.TempPathFactory, sample_pdf: Path) -> Iterator[dict[str, Any]]:
    """One real index of the 60-page sample (~8-12 s), shared by the tests that inspect its outcome."""
    mp = pytest.MonkeyPatch()
    mp.setenv("OPENAI_API_KEY", "sk-test-module")
    mp.delenv("OPENAI_BASE_URL", raising=False)
    server = start_mock_server()
    root = tmp_path_factory.mktemp("indexed60")
    cfg = load_settings(environ={}).with_(data_dir=root / "data", openai_api_key="sk-test-indexer", openai_base_url=server.base_url,
                                          index_model="idx-model-x")
    store = Store(cfg.db_path)
    svc = IndexService(cfg, store)
    try:
        sid, doc, watcher = run_job(svc, store, cfg, sample_pdf)
        yield {"cfg": cfg, "store": store, "sid": sid, "doc": doc, "watcher": watcher, "mock": server, "pdf": sample_pdf}
    finally:
        svc.shutdown(wait=True, timeout=20)
        store.close()
        server.stop()
        pc.remove_patches()
        pc.restore_openai_env()
        mp.undo()


@pytest.mark.slow
def test_full_index_of_the_sample_report(indexed60: dict[str, Any]) -> None:
    doc: DocumentInfo = indexed60["doc"]
    assert doc.status == "ready" and doc.stage == "ready" and doc.progress == 1.0
    assert doc.error is None
    assert doc.page_count == 60
    assert doc.node_count and doc.node_count > 5
    assert doc.pi_doc_id and doc.pi_doc_id.startswith("pi-")
    assert doc.title and doc.description
    assert doc.indexed_at and doc.index_seconds and doc.index_seconds > 0
    session = indexed60["store"].get_session(indexed60["sid"])
    assert session is not None and session.state == "ready"


@pytest.mark.slow
def test_progress_is_monotonic_and_capped_until_ready(indexed60: dict[str, Any]) -> None:
    watcher: Watcher = indexed60["watcher"]
    progress = watcher.progress
    assert progress == sorted(progress), "progress went backwards"
    assert len(set(progress)) >= 5, "progress should move in several steps"
    assert all(p <= 0.95 for (status, _, p, _) in watcher.samples if status == "indexing")
    assert progress[-1] == 1.0


@pytest.mark.slow
def test_stages_are_seen_in_order(indexed60: dict[str, Any]) -> None:
    stages = indexed60["watcher"].stages
    assert stages[-1] == "ready"
    seen = [s for s in stages if s in STAGES]
    assert seen == sorted(seen, key=STAGES.index), f"stages out of order: {stages}"
    assert {"building_tree", "summarizing", "finalizing", "ready"} <= set(stages)


@pytest.mark.slow
def test_the_index_used_the_models_and_the_key_we_configured(indexed60: dict[str, Any]) -> None:
    requests = indexed60["mock"].requests
    kinds = {r["kind"] for r in requests}
    assert {"index_leaf", "index_parent", "index_description"} <= kinds
    assert {r["json"]["model"] for r in requests} == {"idx-model-x"}
    assert {r["headers"]["authorization"] for r in requests} == {"Bearer sk-test-indexer"}


@pytest.mark.slow
def test_load_tree_matches_the_contract(indexed60: dict[str, Any]) -> None:
    tree = load_tree(indexed60["cfg"], indexed60["sid"])
    assert tree and count_nodes(tree) == indexed60["doc"].node_count

    def walk(nodes: list[dict]) -> None:
        for node in nodes:
            assert {"title", "node_id", "start_index", "end_index", "summary"} <= node.keys()
            assert "text" not in node
            assert 1 <= node["start_index"] <= node["end_index"] <= 60
            walk(node.get("nodes") or [])

    walk(tree)
    assert load_tree(indexed60["cfg"], indexed60["sid"], indexed60["doc"].pi_doc_id) == tree


@pytest.mark.slow
def test_load_page_texts_is_the_pdfium_text(indexed60: dict[str, Any]) -> None:
    texts = load_page_texts(indexed60["cfg"], indexed60["sid"])
    assert texts is not None and len(texts) == 60
    assert texts == extract_page_texts(indexed60["pdf"])


@pytest.mark.slow
def test_clone_index_round_trip(indexed60: dict[str, Any]) -> None:
    cfg: Settings = indexed60["cfg"]
    src_doc: DocumentInfo = indexed60["doc"]
    dst = indexed60["store"].create_session().id
    pi_doc_id = clone_index(cfg, indexed60["sid"], dst)
    assert pi_doc_id == src_doc.pi_doc_id
    client = pc.make_client(cfg, dst)
    assert client.get_document(pi_doc_id)["pageNum"] == 60
    assert client.get_page_content(pi_doc_id, "7")[0]["markdown"] == extract_page_texts(indexed60["pdf"])[6]
    assert load_tree(cfg, dst) == load_tree(cfg, indexed60["sid"])
    assert load_page_texts(cfg, dst) == load_page_texts(cfg, indexed60["sid"])
    # independent copies: deleting the clone leaves the source intact
    shutil.rmtree(cfg.session_dir(dst))
    assert pc.make_client(cfg, indexed60["sid"]).get_page_content(pi_doc_id, "1")


def test_clone_index_refuses_missing_source_and_occupied_destination(cfg: Settings, store: Store, small_pdf: Path,
                                                                     make_service: Callable[..., IndexService]) -> None:
    other = store.create_session().id
    with pytest.raises(FileNotFoundError):
        clone_index(cfg, store.create_session().id, other)
    svc = make_service()
    sid, doc, _ = run_job(svc, store, cfg, small_pdf)
    assert doc.status == "ready"
    clone_index(cfg, sid, other)
    with pytest.raises(FileExistsError):
        clone_index(cfg, sid, other)
    assert load_tree(cfg, other)           # the refused second clone did not damage the first
    assert not [p for p in cfg.session_dir(other).iterdir() if "copy" in p.name], "staging folder left behind"


def test_readers_on_a_session_without_an_index(cfg: Settings) -> None:
    assert load_tree(cfg, "f" * 32) == []
    assert load_page_texts(cfg, "f" * 32) is None


# ------------------------------------------------------------------------------------------------ small real index
def test_small_report_indexes_end_to_end(cfg: Settings, store: Store, small_pdf: Path, make_service: Callable[..., IndexService]) -> None:
    svc = make_service()
    sid, doc, _ = run_job(svc, store, cfg, small_pdf)
    assert (doc.status, doc.page_count) == ("ready", 12)
    assert not svc.is_running(sid)
    assert doc.title == "report"     # no PDF title metadata: the file name


def test_a_stale_store_from_a_failed_attempt_is_cleared_before_indexing(cfg: Settings, store: Store, small_pdf: Path,
                                                                        make_service: Callable[..., IndexService]) -> None:
    svc = make_service()
    sid, dest = new_session(store, cfg, small_pdf)
    stale = pc.storage_path(cfg, sid) / "docs" / ("pi-" + "a" * 32)
    stale.mkdir(parents=True)
    (stale / "doc.json").write_text("{}", encoding="utf-8")
    svc.start(sid, dest)
    doc = wait_for(store, sid)
    assert doc.status == "ready"
    assert not stale.exists()
    client = pc.make_client(cfg, sid)
    assert client.get_document(doc.pi_doc_id or "")["name"] == "report.pdf"     # not "report_1.pdf"


@pytest.mark.slow
def test_documents_over_64_pages_index_through_the_sdk_process_pool(cfg: Settings, store: Store, tmp_path: Path,
                                                                    make_service: Callable[..., IndexService]) -> None:
    from scripts.make_sample_pdf import build_sample_pdf

    pdf = build_sample_pdf(tmp_path / "big.pdf", pages=70)
    sid, doc, _ = run_job(make_service(), store, cfg, pdf, timeout=180)
    assert doc.status == "ready", doc.error
    assert doc.page_count == 70
    assert len(load_page_texts(cfg, sid) or []) == 70


# ------------------------------------------------------------------------------------------------ OpenAI failure modes (real SDK)
def test_auth_failure_names_the_key(cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                    make_service: Callable[..., IndexService]) -> None:
    mock.fail_next(kind="auth", count=1000)
    sid, doc, _ = run_job(make_service(), store, cfg, small_pdf)
    assert (doc.status, doc.stage) == ("failed", "failed")
    assert "API key" in (doc.error or "") and "OPENAI_API_KEY" in (doc.error or "")
    assert "sk-test" not in (doc.error or "")
    assert store.get_session(sid).state == "failed"  # type: ignore[union-attr]


def test_model_access_failure_names_model_and_setting(cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                      make_service: Callable[..., IndexService]) -> None:
    mock.fail_next(kind="model", count=1000)
    _, doc, _ = run_job(make_service(), store, cfg, small_pdf)
    assert doc.status == "failed"
    assert "idx-model-x" in (doc.error or "") and "PI_INDEX_MODEL" in (doc.error or "")


def test_rate_limit_is_retried_once_then_fails_with_advice(cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                           no_sdk_sleep: None, make_service: Callable[..., IndexService]) -> None:
    mock.fail_next(kind="rate_limit", count=100_000)
    seen: list[int] = []

    def factory(settings: Settings, sid: str, **kw: Any) -> Any:
        seen.append(settings.index_summary_concurrency)
        return pc.make_client(settings, sid, **kw)

    _, doc, _ = run_job(make_service(factory), store, cfg, small_pdf)
    assert doc.status == "failed"
    assert seen == [cfg.index_summary_concurrency, cfg.index_summary_concurrency // 2]
    assert "429" in (doc.error or "") and "PI_INDEX_SUMMARY_CONCURRENCY" in (doc.error or "")


def test_server_error_fails_with_a_plain_message_and_is_not_retried(cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                                    no_sdk_sleep: None, make_service: Callable[..., IndexService]) -> None:
    mock.fail_next(kind="server", count=100_000)
    attempts: list[int] = []

    def factory(settings: Settings, sid: str, **kw: Any) -> Any:
        attempts.append(1)
        return pc.make_client(settings, sid, **kw)

    _, doc, _ = run_job(make_service(factory), store, cfg, small_pdf)
    assert doc.status == "failed" and len(attempts) == 1
    assert "server error" in (doc.error or "") and "500" in (doc.error or "")
    assert "Traceback" not in (doc.error or "")


# ------------------------------------------------------------------------------------------------ bad PDFs
@pytest.mark.parametrize("kind, expected", [("scanned", "scanned"), ("encrypted", "password"), ("corrupt", "not a readable PDF")])
def test_unusable_pdfs_fail_with_the_friendly_message_and_never_call_the_model(
        kind: str, expected: str, cfg: Settings, store: Store, tmp_path: Path, mock: MockServer,
        make_service: Callable[..., IndexService]) -> None:
    pdf = tmp_path / f"{kind}.pdf"
    pdf.write_bytes({"scanned": build_blank_pdf(4), "encrypted": build_encrypted_pdf(), "corrupt": b"%PDF-1.7 definitely not a pdf"}[kind])
    sid, doc, _ = run_job(make_service(), store, cfg, pdf)
    assert (doc.status, doc.stage) == ("failed", "failed")
    assert expected in (doc.error or "")
    assert mock.requests == []


def test_too_many_pages_is_refused_before_indexing(cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                   make_service: Callable[..., IndexService]) -> None:
    _, doc, _ = run_job(make_service(settings=cfg.with_(max_pages=5)), store, cfg, small_pdf)
    assert doc.status == "failed" and "12 pages" in (doc.error or "") and "limit is 5" in (doc.error or "")
    assert mock.requests == []


# ------------------------------------------------------------------------------------------------ no outline
def test_no_outline_fails_clearly_when_the_fallback_is_off(cfg: Settings, store: Store, flat_pdf: Path, mock: MockServer,
                                                           make_service: Callable[..., IndexService]) -> None:
    settings = cfg.with_(index_fallback_standard=False)
    _, doc, _ = run_job(make_service(settings=settings), store, settings, flat_pdf)
    assert doc.status == "failed"
    assert "bookmarks" in (doc.error or "") and "PI_INDEX_FALLBACK_STANDARD" in (doc.error or "")
    assert mock.requests == []               # the real SDK refused the flat tree before spending a single model call


def test_real_sdk_refusal_triggers_the_standard_fallback(cfg: Settings, store: Store, flat_pdf: Path, mock: MockServer,
                                                         no_sdk_sleep: None, make_service: Callable[..., IndexService]) -> None:
    """Flash really refuses `flat_pdf`; the standard attempt then really starts and talks to the mock.  The mock is not a
    language model, so whether standard mode can finish against it is not what this checks: only that the attempt is made, the
    note is shown, and the document leaves 'indexing' either way."""
    modes: list[str] = []

    def factory(settings: Settings, sid: str, **kw: Any) -> Any:
        modes.append(kw.get("mode", "flash"))
        return pc.make_client(settings, sid, **kw)

    _, doc, watcher = run_job(make_service(factory), store, cfg, flat_pdf, timeout=120)
    assert modes == ["flash", "standard"]
    assert any(d == NO_OUTLINE_NOTE for _, _, _, d in watcher.samples)
    assert mock.requests, "standard mode should have asked the model for a structure"
    assert doc.status in ("ready", "failed")
    assert doc.description != NO_OUTLINE_NOTE


def test_fallback_shows_the_note_then_replaces_it_with_the_real_description(cfg: Settings, store: Store, small_pdf: Path,
                                                                            make_service: Callable[..., IndexService]) -> None:
    release = threading.Event()
    in_standard = threading.Event()

    def script(n: int, mode: str) -> dict:
        if mode == "flash":
            raise pc_error("Failed to submit document: PageIndex Flash found no layout structure in this document (12 pages); "
                           "try mode='standard', which builds the structure with the model.")
        in_standard.set()
        assert release.wait(20)
        return ok()

    factory = FakeFactory(script)
    svc = make_service(factory)
    sid, dest = new_session(store, cfg, small_pdf)
    svc.start(sid, dest)
    assert in_standard.wait(20)
    doc = store.get_document(sid)
    assert doc is not None
    assert (doc.status, doc.stage, doc.description) == ("indexing", "building_tree", NO_OUTLINE_NOTE)
    release.set()
    done = wait_for(store, sid)
    assert done.status == "ready" and done.description == "A fake annual report."
    assert [m for _, m in factory.calls] == ["flash", "standard"]
    assert factory.calls[1][0] == cfg.index_summary_concurrency        # the standard client is built from unmodified settings


def test_failed_fallback_clears_the_note(cfg: Settings, store: Store, small_pdf: Path, make_service: Callable[..., IndexService]) -> None:
    def script(n: int, mode: str) -> dict:
        if mode == "flash":
            raise pc_error("Failed to submit document: PageIndex Flash could not extract a structure from this PDF; try mode='standard'")
        raise pc_error("Failed to submit document: Processing failed")

    sid, doc, _ = run_job(make_service(FakeFactory(script)), store, cfg, small_pdf)
    assert doc.status == "failed"
    assert doc.description is None
    assert doc.error == "Indexing failed: Processing failed"


def pc_error(message: str) -> Exception:
    from pageindex import PageIndexAPIError
    return PageIndexAPIError(message)


# ------------------------------------------------------------------------------------------------ retry policy (fake SDK)
def test_rate_limit_retry_halves_concurrency_and_can_succeed(cfg: Settings, store: Store, small_pdf: Path,
                                                             make_service: Callable[..., IndexService]) -> None:
    def script(n: int, mode: str) -> dict:
        if n == 1:
            raise RateLimited()
        return ok()

    factory = FakeFactory(script)
    _, doc, _ = run_job(make_service(factory), store, cfg, small_pdf)
    assert doc.status == "ready"
    assert [c for c, _ in factory.calls] == [cfg.index_summary_concurrency, cfg.index_summary_concurrency // 2]


def test_rate_limit_retry_pauses_first(cfg: Settings, store: Store, small_pdf: Path, make_service: Callable[..., IndexService]) -> None:
    stamps: list[float] = []

    def script(n: int, mode: str) -> dict:
        stamps.append(time.monotonic())
        if n == 1:
            raise RateLimited()
        return ok()

    run_job(make_service(FakeFactory(script), retry_pause_s=0.5), store, cfg, small_pdf)
    assert stamps[1] - stamps[0] >= 0.45


def test_insufficient_quota_is_not_retried(cfg: Settings, store: Store, small_pdf: Path, make_service: Callable[..., IndexService]) -> None:
    def script(n: int, mode: str) -> dict:
        err = RuntimeError("Failed to submit document: You exceeded your current quota (insufficient_quota)")
        err.status_code = 429  # type: ignore[attr-defined]
        raise err

    factory = FakeFactory(script)
    _, doc, _ = run_job(make_service(factory), store, cfg, small_pdf)
    assert doc.status == "failed" and len(factory.calls) == 1
    assert "credit" in (doc.error or "")


def test_unexpected_errors_become_a_short_message(cfg: Settings, store: Store, small_pdf: Path, make_service: Callable[..., IndexService],
                                                  caplog: pytest.LogCaptureFixture) -> None:
    def script(n: int, mode: str) -> dict:
        raise ValueError("boom " + "x" * 400 + " sk-live-ABCDEF123456")

    _, doc, _ = run_job(make_service(FakeFactory(script)), store, cfg, small_pdf)
    assert doc.status == "failed"
    assert doc.error.startswith("Indexing failed: boom") and len(doc.error) < 260   # type: ignore[union-attr]
    assert "ABCDEF" not in doc.error                                                 # type: ignore[operator]
    assert any(r.exc_info for r in caplog.records), "the full traceback goes to the log"


# ------------------------------------------------------------------------------------------------ scheduling
def test_two_jobs_run_serially_and_the_second_shows_queued(cfg: Settings, store: Store, small_pdf: Path,
                                                           make_service: Callable[..., IndexService]) -> None:
    spans: list[tuple[float, float]] = []
    gate = threading.Event()

    def script(n: int, mode: str) -> dict:
        start = time.monotonic()
        if n == 1:
            assert gate.wait(20)
        time.sleep(0.1)
        spans.append((start, time.monotonic()))
        return ok()

    svc = make_service(FakeFactory(script))
    a, pa = new_session(store, cfg, small_pdf)
    b, pb = new_session(store, cfg, small_pdf)
    svc.start(a, pa)
    svc.start(b, pb)
    second = store.get_document(b)
    assert second is not None and (second.status, second.stage, second.progress) == ("indexing", "queued", 0.0)
    assert svc.is_running(a) and svc.is_running(b)
    gate.set()
    wait_for(store, a)
    wait_for(store, b)
    assert len(spans) == 2 and spans[0][1] <= spans[1][0], "jobs overlapped"
    assert not svc.is_running(a) and not svc.is_running(b)


def test_starting_a_running_session_again_is_ignored(cfg: Settings, store: Store, small_pdf: Path, make_service: Callable[..., IndexService]) -> None:
    gate = threading.Event()

    def script(n: int, mode: str) -> dict:
        assert gate.wait(20)
        return ok()

    factory = FakeFactory(script)
    svc = make_service(factory)
    sid, dest = new_session(store, cfg, small_pdf)
    svc.start(sid, dest)
    svc.start(sid, dest)
    gate.set()
    wait_for(store, sid)
    assert len(factory.calls) == 1


def test_job_timeout_fails_the_document_and_frees_the_worker(cfg: Settings, store: Store, small_pdf: Path,
                                                             make_service: Callable[..., IndexService]) -> None:
    hang = threading.Event()

    def script(n: int, mode: str) -> dict:
        if n == 1:
            hang.wait(30)
        return ok()

    svc = make_service(FakeFactory(script), job_timeout_s=0.5)
    a, pa = new_session(store, cfg, small_pdf)
    b, pb = new_session(store, cfg, small_pdf)
    svc.start(a, pa)
    svc.start(b, pb)
    first = wait_for(store, a)
    assert first.status == "failed" and "longer than" in (first.error or "")
    assert wait_for(store, b).status == "ready"       # the worker moved on although the first SDK call is still hanging
    hang.set()
    time.sleep(0.2)
    done = store.get_document(a)
    assert done is not None and done.status == "failed", "the abandoned job must not resurrect the document"


# ------------------------------------------------------------------------------------------------ cancel / shutdown
def test_cancel_stops_the_job_at_its_next_model_call_and_removes_the_store(cfg: Settings, store: Store, small_pdf: Path,
                                                                           mock: MockServer, make_service: Callable[..., IndexService]) -> None:
    mock.delay_ms = 400                                   # replies are held back, so the job is mid-flight when we cancel
    svc = make_service()
    sid, dest = new_session(store, cfg, small_pdf)
    svc.start(sid, dest)
    deadline = time.monotonic() + 30
    while not mock.requests and time.monotonic() < deadline:
        time.sleep(0.02)
    assert mock.requests, "the job never reached the model"
    svc.cancel(sid)
    doc = wait_for(store, sid, timeout=30)
    assert (doc.status, doc.error) == ("failed", CANCELLED_ERROR)
    assert not (cfg.session_dir(sid) / "pageindex").exists()
    assert not svc.is_running(sid)
    assert not mock.requests_of("index_description"), "a cancelled job must not carry on to the last model call"


def test_cancel_before_the_job_starts_skips_it(cfg: Settings, store: Store, small_pdf: Path, make_service: Callable[..., IndexService]) -> None:
    gate = threading.Event()
    factory = FakeFactory(lambda n, mode: (gate.wait(20), ok())[1])
    svc = make_service(factory)
    a, pa = new_session(store, cfg, small_pdf)
    b, pb = new_session(store, cfg, small_pdf)
    svc.start(a, pa)
    svc.start(b, pb)
    svc.cancel(b)
    gate.set()
    assert wait_for(store, a).status == "ready"
    cancelled = wait_for(store, b)
    assert (cancelled.status, cancelled.error) == ("failed", CANCELLED_ERROR)
    assert len(factory.calls) == 1


def test_cancel_of_an_unknown_session_is_a_no_op(make_service: Callable[..., IndexService]) -> None:
    make_service().cancel("e" * 32)


def test_shutdown_interrupts_queued_and_running_jobs_and_refuses_new_ones(cfg: Settings, store: Store, small_pdf: Path,
                                                                          make_service: Callable[..., IndexService]) -> None:
    gate = threading.Event()
    factory = FakeFactory(lambda n, mode: (gate.wait(5), ok())[1])
    svc = make_service(factory)
    a, pa = new_session(store, cfg, small_pdf)
    b, pb = new_session(store, cfg, small_pdf)
    svc.start(a, pa)
    svc.start(b, pb)
    gate.set()
    svc.shutdown(wait=True, timeout=20)
    for sid in (a, b):
        doc = store.get_document(sid)
        assert doc is not None and doc.status in ("ready", "failed")        # whatever happened, nothing is left 'indexing'
    c, pc_ = new_session(store, cfg, small_pdf)
    svc.start(c, pc_)
    late = store.get_document(c)
    assert late is not None and (late.status, late.error) == ("failed", INTERRUPTED_INDEXING_ERROR)


# ------------------------------------------------------------------------------------------------ robustness
def test_a_failing_store_never_kills_the_worker(cfg: Settings, store: Store, small_pdf: Path, make_service: Callable[..., IndexService],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    real_update = store.update_document
    broken = {"on": True}

    def flaky(sid: str, **fields: Any) -> Any:
        if broken["on"]:
            raise RuntimeError("database is locked")
        return real_update(sid, **fields)

    monkeypatch.setattr(store, "update_document", flaky)
    svc = make_service(FakeFactory(lambda n, mode: ok()))
    a, pa = new_session(store, cfg, small_pdf)
    svc.start(a, pa)
    deadline = time.monotonic() + 20
    while svc.is_running(a) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not svc.is_running(a)
    broken["on"] = False
    b, pb = new_session(store, cfg, small_pdf)
    svc.start(b, pb)
    assert wait_for(store, b).status == "ready"


def test_a_document_never_stays_indexing_when_the_final_write_fails(cfg: Settings, store: Store, small_pdf: Path,
                                                                    make_service: Callable[..., IndexService],
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    real_update = store.update_document

    def refuse_ready(sid: str, **fields: Any) -> Any:
        if fields.get("status") == "ready":
            raise ValueError("rejected by the model")
        return real_update(sid, **fields)

    monkeypatch.setattr(store, "update_document", refuse_ready)
    sid, doc, _ = run_job(make_service(FakeFactory(lambda n, mode: ok())), store, cfg, small_pdf)
    assert doc.status == "failed" and "unexpectedly" in (doc.error or "")


def test_a_session_deleted_while_indexing_is_not_resurrected(cfg: Settings, store: Store, small_pdf: Path,
                                                             make_service: Callable[..., IndexService]) -> None:
    gate = threading.Event()
    svc = make_service(FakeFactory(lambda n, mode: (gate.wait(20), ok())[1]))
    sid, dest = new_session(store, cfg, small_pdf)
    svc.start(sid, dest)
    store.delete_session(sid)
    gate.set()
    deadline = time.monotonic() + 20
    while svc.is_running(sid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert store.get_document(sid) is None and store.get_session(sid) is None


# ------------------------------------------------------------------------------------------------ the LLM shim
def test_install_llm_counters_is_idempotent_and_does_not_double_count() -> None:
    import pageindex.page_index_classic as classic
    import pageindex.utils as sdk_utils

    first = indexer.install_llm_counters()
    second = indexer.install_llm_counters()
    assert first == second == 5
    assert not hasattr(sdk_utils.llm_acompletion.__wrapped__, "__wrapped__")
    assert classic.llm_completion.__wrapped__ is sdk_utils.llm_completion.__wrapped__    # both wrap the SDK's one original


async def test_shim_counts_calls_for_the_current_job_only(cfg: Settings, store: Store) -> None:
    svc = IndexService(cfg, store, FakeFactory(lambda n, mode: ok()))
    try:
        job = indexer._Job(svc, "d" * 32, Path("x.pdf"))
        job.stage, job.estimate = "building_tree", 10
        calls: list[str] = []

        async def fake(model: str, prompt: str) -> str:
            calls.append(prompt)
            return "reply"

        counted = indexer._counted(fake, True, "call")
        assert await counted("m", "untracked") == "reply" and job.calls == 0       # no job in context: pass-through
        token = indexer._current_job.set(job)
        try:
            for i in range(5):
                assert await counted("m", f"p{i}") == "reply"
        finally:
            indexer._current_job.reset(token)
        assert job.calls == 5 and job.stage == "summarizing"
        assert 0.2 < job.progress < 0.95
    finally:
        svc.shutdown(wait=True, timeout=5)


def test_progress_creeps_while_the_layout_is_parsed_but_never_reaches_the_summary_milestone(cfg: Settings, store: Store,
                                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    svc = IndexService(cfg, store, FakeFactory(lambda n, mode: ok()))
    try:
        job = indexer._Job(svc, "d" * 32, Path("x.pdf"))
        updates: list[float] = []
        monkeypatch.setattr(svc, "_update", lambda sid, **f: updates.append(f["progress"]))
        job.tick()
        assert updates == []                                                    # not parsing a layout yet: nothing moves
        job.report("extracting_text", indexer._P_EXTRACTING)
        job.tick()
        assert job.progress == indexer._P_EXTRACTING
        job.report("building_tree", indexer._P_EXTRACTING)
        seen = [job.progress]
        for elapsed in (1.0, 5.0, 10.0, 25.0, 60.0, 3600.0, 2.0):               # the last one tries to go back in time
            job._stage_started = time.monotonic() - elapsed
            job.tick()
            seen.append(job.progress)
        assert seen == sorted(seen), "progress went backwards"
        assert seen[1] > indexer._P_EXTRACTING and seen[3] > seen[1]
        assert max(seen) < indexer._P_TREE
        job.report("summarizing", indexer._P_TREE)
        before = job.progress
        job._stage_started = time.monotonic() - 1000
        job.tick()                                                               # no creep once summaries have started
        assert job.progress == before == indexer._P_TREE
    finally:
        svc.shutdown(wait=True, timeout=5)


async def test_a_broken_progress_report_never_breaks_the_sdk_call(cfg: Settings, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = IndexService(cfg, store, FakeFactory(lambda n, mode: ok()))
    try:
        job = indexer._Job(svc, "d" * 32, Path("x.pdf"))
        monkeypatch.setattr(job, "report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store down")))

        def fake(model: str, prompt: str) -> str:
            return "fine"

        counted = indexer._counted(fake, False, "describe")
        token = indexer._current_job.set(job)
        try:
            assert counted("m", "p") == "fine"
        finally:
            indexer._current_job.reset(token)
    finally:
        svc.shutdown(wait=True, timeout=5)


async def test_a_cancelled_job_aborts_the_model_call() -> None:
    job = indexer._Job(None, "d" * 32, Path("x.pdf"))  # type: ignore[arg-type]
    job.cancelled.set()

    async def fake(model: str, prompt: str) -> str:
        raise AssertionError("must not be reached")

    counted = indexer._counted(fake, True, "call")
    token = indexer._current_job.set(job)
    try:
        with pytest.raises(indexer.JobCancelled) as info:
            await counted("m", "p")
    finally:
        indexer._current_job.reset(token)
    assert info.value.status_code == 401        # the SDK's "unrecoverable" class: it stops the whole run


# ------------------------------------------------------------------------------------------------ error translation
class _Err(Exception):
    def __init__(self, message: str, status: Optional[int] = None, cause: Optional[BaseException] = None):
        super().__init__(message)
        if status is not None:
            self.status_code = status
        self.__cause__ = cause


@pytest.mark.parametrize("exc, kind, needles", [
    (_Err("Failed to submit document: x", cause=_Err("Incorrect API key provided: sk-abc***", 401)), "auth", ["OPENAI_API_KEY", "rejected"]),
    (_Err("Failed to submit document: The api_key client option must be set"), "auth", ["OPENAI_API_KEY"]),
    (_Err("Failed to submit document: x", cause=_Err("The model `gpt-9` does not exist or you do not have access to it", 404)),
     "model", ["idx-model-x", "PI_INDEX_MODEL"]),
    (_Err("Failed to submit document: x", cause=_Err("x", 403)), "model", ["idx-model-x"]),
    (_Err("Failed to submit document: x", cause=_Err("LLM completion failed after 10 retries", 429)), "rate_limit", ["429", "PI_INDEX_SUMMARY_CONCURRENCY"]),
    (_Err("x", cause=_Err("You exceeded your current quota, please check your plan and billing details", 429)), "quota", ["credit"]),
    (_Err("Failed to submit document: x", cause=_Err("boom", 503)), "upstream", ["503", "server error"]),
    (_Err("Failed to submit document: Connection error."), "upstream", ["reached"]),
    (_Err("Failed to submit document: PDF has no content. All pages are blank."), "scanned", ["OCR"]),
    (_Err("Failed to submit document: PageIndex Flash found no text layer in this PDF"), "scanned", ["OCR"]),
    (_Err("Failed to submit document: PageIndex Flash found no layout structure in this document (12 pages)"), "no_outline", ["bookmarks"]),
    (_Err("PDF is encrypted or password-protected"), "encrypted", ["password"]),
    (_Err("Failed to submit document: weird   internal\nerror"), "other", ["Indexing failed: weird internal error"]),
])
def test_translate_error(exc: BaseException, kind: str, needles: list[str]) -> None:
    settings = load_settings(environ={}).with_(openai_api_key="sk-test-x", index_model="idx-model-x")
    fault = translate_error(exc, settings)
    assert fault.kind == kind
    for needle in needles:
        assert needle in fault.message
    assert "Traceback" not in fault.message and "sk-abc" not in fault.message


def test_translate_error_does_not_mistake_file_errors_for_a_missing_model() -> None:
    settings = load_settings(environ={}).with_(openai_api_key="sk-test-x")
    for exc in (FileNotFoundError(2, "No such file or directory", "C:/data/sessions/x/pageindex/docs/y/doc.json"),
                _Err("Failed to submit document: x", cause=PermissionError(13, "Permission denied"))):
        fault = translate_error(exc, settings)
        assert fault.kind == "storage" and "REPORTLENS_DATA_DIR" in fault.message and "model" not in fault.message.lower()
    assert translate_error(ConnectionError("reset"), settings).kind != "storage"


def test_translate_error_matches_the_openai_error_classes_exactly() -> None:
    class NotFoundError(Exception):
        status_code = None

    class PermissionDeniedError(Exception):
        pass

    class NotFoundInMyCode(Exception):          # a class that merely contains the word must not count
        pass

    settings = load_settings(environ={}).with_(openai_api_key="sk-test-x")
    assert translate_error(NotFoundError("x"), settings).kind == "model"
    assert translate_error(PermissionDeniedError("x"), settings).kind == "model"
    assert translate_error(NotFoundInMyCode("x"), settings).kind == "other"


def test_translate_error_without_a_configured_key_says_so() -> None:
    fault = translate_error(_Err("x", 401), load_settings(environ={}).with_(openai_api_key=None))
    assert fault.kind == "auth" and "No OpenAI API key" in fault.message


def test_translate_error_mentions_a_custom_base_url_for_model_errors() -> None:
    settings = load_settings(environ={}).with_(openai_base_url="http://proxy.local/v1")
    assert "OPENAI_BASE_URL" in translate_error(_Err("x", 404), settings).message


def test_translate_error_survives_cyclic_exception_chains() -> None:
    a, b = _Err("a"), _Err("b")
    a.__cause__, b.__cause__ = b, a
    assert translate_error(a, load_settings(environ={})).kind == "other"


def test_choose_title_prefers_pdf_metadata_then_file_name_then_tree() -> None:
    assert indexer._choose_title("Northbridge Annual Report", "x.pdf", FAKE_TREE) == "Northbridge Annual Report"
    assert indexer._choose_title(None, "National_Grid-Annual_Report.pdf", FAKE_TREE) == "National Grid Annual Report"
    assert indexer._choose_title("  ", None, [{"title": "Preface"}, {"title": "Strategic report"}]) == "Strategic report"
    assert indexer._choose_title(None, None, []) is None
