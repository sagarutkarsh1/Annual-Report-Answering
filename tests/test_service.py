"""Offline tests for reportlens.service: sessions, the one-document rule, resources, locate, and the ask/evaluate flow.

The engines are in-process fakes (FakeIndexer, FakeQA, FakeEvaluator) that follow the contract of docs/ARCHITECTURE.md
4.4 / 4.6 / 4.7; the answer they yield is a REAL BuiltAnswer made by citations.build_answer on the sample PDF.  The last
section drives the real IndexService / QAEngine / Evaluator against devtools.mock_openai when those modules exist."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from reportlens import service as service_module
from reportlens.citations import CitationContext, build_answer, strip_markers
from reportlens.config import Settings
from reportlens.models import (
    Citation,
    ContextPage,
    DocumentInfo,
    DocumentPages,
    EvalScores,
    LocateResponse,
    Message,
    ServiceError,
    Step,
)
from reportlens.service import (
    ReportLensService,
    build_history,
    make_title,
    remove_tree,
    select_eval_contexts,
    slugify_doc_name,
)
from reportlens.store import INTERRUPTED_ANSWER_ERROR, INTERRUPTED_INDEXING_ERROR, Store, new_id, now_iso
from tests import pdf_factory as pf

TREE = [
    {"title": "Strategic report", "node_id": "0001", "start_index": 3, "end_index": 20, "summary": "s", "nodes": [
        {"title": "Highlights", "node_id": "0002", "start_index": 4, "end_index": 4, "summary": "h"}]},
    {"title": "Governance", "node_id": "0003", "start_index": 21, "end_index": 40, "summary": "g"},
]
METRICS = ("faithfulness", "answer_relevancy", "context_precision")
NAMES_HAPPY = ["message_start", "step", "step_done", "token", "citation", "token", "answer_done", "eval_started",
               "eval_result", "eval_result", "eval_result", "eval_done", "done"]


# ----------------------------------------------------------------------------------------------- fakes
class FakeIndexer:
    """IndexService stand-in: records calls; `finish` / `fail` play the part of the background job."""

    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self.started: list[tuple[str, Path]] = []
        self.cancelled: list[str] = []
        self.fail_start = False

    def start(self, session_id: str, pdf_path: Path) -> None:
        if self.fail_start:
            raise RuntimeError("boom")
        self.started.append((session_id, Path(pdf_path)))

    def is_running(self, session_id: str) -> bool:
        return False

    def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)

    def finish(self, sid: str) -> None:
        folder = self.settings.session_dir(sid) / "pageindex"
        (folder / "docs").mkdir(parents=True, exist_ok=True)
        (folder / "manifest.json").write_text(json.dumps({"doc": "pi-fake"}), encoding="utf-8")
        self.store.update_document(sid, status="ready", stage="ready", progress=1.0, pi_doc_id="pi-" + "a" * 32, node_count=3,
                                   title="Northbridge", indexed_at=now_iso(), index_seconds=1.5)

    def fail(self, sid: str, error: str = "Rate limited") -> None:
        folder = self.settings.session_dir(sid) / "pageindex"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "partial.json").write_text("{}", encoding="utf-8")
        self.store.update_document(sid, status="failed", stage="failed", error=error)


CLONED_DOC_ID = "pi-" + "b" * 32


def fake_clone_index(settings: Settings, src: str, dst: str) -> str:
    """indexer.clone_index stand-in: copies the store folder and returns the document id valid in the copy."""
    shutil.copytree(settings.session_dir(src) / "pageindex", settings.session_dir(dst) / "pageindex")
    return CLONED_DOC_ID


class FakeQAError(Exception):
    """Same duck type as reportlens.qa.QAError: .code and .message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class FakeQA:
    """QAEngine stand-in.  Yields the contract's event dicts; the final answer is built from real cite tags on the real PDF."""

    def __init__(self, fact: dict):
        self.fact = fact
        self.calls: list[dict] = []
        self.rewrite_calls: list[tuple[str, list[dict]]] = []
        self.rewrite_result: Callable[[str, list[dict]], str] = lambda q, h: f"standalone: {q}"
        self.rewrite_raises = False
        self.contexts: Optional[list[ContextPage]] = None      # default: contents page + the cited page
        self.status = "answered"
        self.cite = True
        self.with_final = True
        self.raise_after_token: Optional[Exception] = None
        self.gate: Optional[threading.Event] = None            # the engine waits here after its first token
        self.barrier: Optional[threading.Barrier] = None
        self.started = threading.Event()                       # first token was produced
        self.saw_cancel = threading.Event()
        self.ended = threading.Event()                         # the generator body finished (also on close())

    def rewrite_question(self, history: list[dict], question: str) -> str:
        self.rewrite_calls.append((question, history))
        if self.rewrite_raises:
            raise RuntimeError("rewrite down")
        return self.rewrite_result(question, history)

    def ask(self, *, session_id: str, doc: DocumentInfo, question: str, history: list[dict], ctx: CitationContext,
            cancel: Optional[threading.Event] = None):
        self.calls.append(dict(session_id=session_id, doc=doc, question=question, history=history, ctx=ctx))
        page, quote = self.fact["page"], self.fact["quote"]
        try:
            yield {"type": "step", "step": Step(id="s1", kind="tool", tool="get_page_content", label=f"Read page {page}", pages=[page])}
            yield {"type": "step_done", "step_id": "s1", "elapsed_ms": 12, "label": f"Read page {page}", "pages": [page]}
            yield {"type": "token", "text": f"{question}: "}
            self.started.set()
            if self.barrier is not None:
                self.barrier.wait(10)
            if self.gate is not None:
                while not self.gate.is_set():
                    if cancel is not None and cancel.is_set():
                        return
                    time.sleep(0.01)
            if self.raise_after_token is not None:
                raise self.raise_after_token
            raw = f"{question}: Northbridge connects 8.4 million customers."
            if self.cite:
                raw += f' <cite doc="{doc.doc_name}" page="{page}" quote="{quote}"/>'
            built = build_answer(raw, ctx)
            if built.citations:
                yield {"type": "citation", "citation": built.citations[0].model_copy(update={"rects": [], "quote": None})}
            yield {"type": "token", "text": built.text}
            if self.with_final:
                contexts = self.contexts if self.contexts is not None else [
                    ContextPage(page=2, text="Contents of the report"), ContextPage(page=page, text=quote)]
                yield {"type": "final", "answer": built, "contexts": contexts, "usage": {"model": "fake", "input_tokens": 10, "output_tokens": 5},
                       "steps": [Step(id="s1", kind="tool", tool="get_page_content", label=f"Read page {page}", pages=[page], status="done", elapsed_ms=12)],
                       "status": self.status, "elapsed_ms": 34}
        finally:
            if cancel is not None and cancel.is_set():
                self.saw_cancel.set()                          # whichever way it stopped, the cancel Event had reached it
            self.ended.set()


class FakeEvaluator:
    """Evaluator stand-in: records calls, reports each metric through on_metric, can be held open with `gate`."""

    def __init__(self):
        self.calls: list[dict] = []
        self.gate: Optional[asyncio.Event] = None
        self.entered = asyncio.Event()
        self.closed = False
        self.explode = False

    async def evaluate(self, question: str, answer: str, contexts: list[ContextPage], on_metric=None) -> EvalScores:
        self.calls.append(dict(question=question, answer=answer, contexts=contexts))
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.explode:
            raise RuntimeError("judge exploded")
        values = dict(zip(METRICS, (0.9, 0.8, 0.7)))
        for metric, value in values.items():
            if on_metric is not None:
                pending = on_metric(metric, value, None)
                if asyncio.iscoroutine(pending):
                    await pending
        return EvalScores(status="done", **values, n_contexts_input=len(contexts), n_contexts_scored=len(contexts), judge_model="fake-judge")

    async def aclose(self) -> None:
        self.closed = True


# ----------------------------------------------------------------------------------------------- fixtures
@dataclass
class Env:
    settings: Settings
    store: Store
    indexer: FakeIndexer
    qa: FakeQA
    evaluator: FakeEvaluator
    service: ReportLensService
    scratch: Path
    sample: Path
    facts: list[dict]

    def upload(self, sid: str, name: str = "Northbridge Annual Report 2025.pdf") -> None:
        tmp = self.scratch / f"upload-{new_id()}.pdf"
        shutil.copyfile(self.sample, tmp)
        self.service.attach_document(sid, name, tmp)

    def ready_session(self, name: str = "Northbridge Annual Report 2025.pdf") -> str:
        sid = self.service.create_session().id
        self.upload(sid, name)
        self.indexer.finish(sid)
        return sid

    def stored(self, sid: str) -> list[Message]:
        return self.store.list_messages(sid)


@pytest.fixture
async def build_env(settings, sample_pdf, sample_facts, tmp_path):
    """Factory: build_env(**setting_changes) -> Env.  Every environment is torn down (service closed, store closed)."""
    made: list[Env] = []

    def make(**changes: Any) -> Env:
        cfg = settings.with_(**changes) if changes else settings
        store = Store(cfg.db_path)
        fact = next(f for f in sample_facts if '"' not in f["quote"])
        indexer, qa, evaluator = FakeIndexer(store, cfg), FakeQA(fact), FakeEvaluator()
        svc = ReportLensService(cfg, store=store, indexer=indexer, qa=qa, evaluator=evaluator, tree_loader=lambda s, sid, pid: TREE, index_cloner=fake_clone_index)
        scratch = tmp_path / f"scratch{len(made)}"
        scratch.mkdir()
        made.append(Env(cfg, store, indexer, qa, evaluator, svc, scratch, sample_pdf, sample_facts))
        return made[-1]

    yield make
    for e in made:
        await e.service.aclose()
        e.store.close()


@pytest.fixture
async def env(build_env) -> Env:
    return build_env()


async def collect(agen) -> list[tuple[str, dict]]:
    return [event async for event in agen]


async def wait_until(predicate: Callable[[], Any], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.02)


async def wait_flag(flag: threading.Event, timeout: float = 10.0) -> None:
    assert await asyncio.to_thread(flag.wait, timeout), "thread flag not set in time"


def names(events: list[tuple[str, dict]]) -> list[str]:
    return [n for n, _ in events]


def payload(events: list[tuple[str, dict]], name: str) -> dict:
    return next(p for n, p in events if n == name)


def error_of(exc: pytest.ExceptionInfo) -> tuple[str, int]:
    assert isinstance(exc.value, ServiceError)
    return exc.value.code, exc.value.status


# ----------------------------------------------------------------------------------------------- pure helpers
@pytest.mark.parametrize("filename, expected", [
    ("National Grid – Annual Report 2024.pdf", "National_Grid_Annual_Report_2024.pdf"),
    ("Réport  (final)!!.PDF", "Report_final.pdf"),
    ("..\\..\\evil/../../etc/passwd.pdf", "passwd.pdf"),
    ("C:\\Users\\me\\Documents\\q3 results.pdf", "q3_results.pdf"),
    ("年度报告.pdf", "report.pdf"),
    ("", "report.pdf"),
    ("   .pdf", "report.pdf"),
    ("CON.pdf", "doc_CON.pdf"),
    ("nul", "doc_nul.pdf"),
    ("annual-report_v2.pdf", "annual-report_v2.pdf"),
    ("report.2024.final.pdf", "report_2024_final.pdf"),
])
def test_slugify_doc_name(filename, expected):
    assert slugify_doc_name(filename) == expected


def test_slugify_doc_name_is_bounded_and_unique():
    long = slugify_doc_name("a" * 300 + ".pdf")
    assert len(long) == 80 and long.endswith(".pdf")
    first = slugify_doc_name("Report.pdf")
    second = slugify_doc_name("Report.pdf", [first])
    third = slugify_doc_name("report.PDF", [first, second])           # NTFS is case-insensitive
    assert len({first, second, third}) == 3
    assert slugify_doc_name("a" * 300 + ".pdf", [long]) != long and len(slugify_doc_name("a" * 300 + ".pdf", [long])) <= 80


def test_make_title():
    assert make_title("  What was\n the revenue?  ") == "What was the revenue?"
    q = "What is the status of GHG reduction technology available to the company and what did management say?"
    title = make_title(q)
    assert len(title) <= 61 and title.endswith("\u2026") and "\n" not in title and q.startswith(title[:-1].rstrip())
    assert make_title("x" * 200) == "x" * 60 + "\u2026"


def msg(role: str, content: str, status: str = "answered", sid: str = "s") -> Message:
    return Message(id=new_id(), session_id=sid, role=role, content=content, status=status)


def test_build_history_pairs_markers_and_window():
    turns = [(msg("user", f"q{i}"), msg("assistant", f"a{i} [[c1]] text [[c2]]")) for i in range(5)]
    messages = [m for pair in turns for m in pair]
    history = build_history(messages, 2)
    assert history == [{"role": "user", "content": "q3"}, {"role": "assistant", "content": "a3 text"},
                       {"role": "user", "content": "q4"}, {"role": "assistant", "content": "a4 text"}]
    assert build_history(messages, 0) == [] and len(build_history(messages, 99)) == 10


def test_build_history_skips_failed_and_dangling_turns():
    messages = [
        msg("user", "q1"), msg("assistant", "ok", "no_sources"),
        msg("user", "q2"), msg("assistant", "partial", "error"),         # failed: dropped with its question
        msg("user", "q3"),                                               # question without any answer row
        msg("user", "q4"), msg("assistant", "fine"),
        msg("user", "q5"), msg("assistant", "still running", "streaming"),
    ]
    assert [h["content"] for h in build_history(messages, 6)] == ["q1", "ok", "q4", "fine"]


def ctx_page(page: int) -> ContextPage:
    return ContextPage(page=page, text=f"text of page {page}")


def cite(page: int) -> Citation:
    return Citation(id="c1", index=1, doc_name="d.pdf", page=page, cited_page=page)


def test_select_contexts_keeps_everything_under_the_cap_in_read_order():
    contexts = [ctx_page(9), ctx_page(3), ctx_page(9), ContextPage(page=5, text="  "), ctx_page(7)]
    assert [c.page for c in select_eval_contexts(contexts, [], 12)] == [9, 3, 7]


def test_select_contexts_cap_policy_keeps_all_cited_pages():
    contexts = [ctx_page(p) for p in (10, 11, 12, 13, 14, 15, 16)]
    # cited 15 and 13 must survive; one free slot is filled by the first read page; order stays the read order
    assert [c.page for c in select_eval_contexts(contexts, [cite(15), cite(13)], 3)] == [10, 13, 15]
    assert [c.page for c in select_eval_contexts(contexts, [], 3)] == [10, 11, 12]
    # more cited pages than slots: first come within the cap, read order
    assert [c.page for c in select_eval_contexts(contexts, [cite(16), cite(14), cite(12), cite(10)], 2)] == [10, 12]
    assert [c.page for c in select_eval_contexts(contexts, [cite(99)], 1)] == [10]            # citation of an unread page


def test_service_metrics_match_the_evaluator():
    from reportlens.evaluation import METRICS as EVAL_METRICS

    assert tuple(service_module._METRICS) == tuple(EVAL_METRICS)


def test_remove_tree_clears_read_only_files(tmp_path):
    folder = tmp_path / "x" / "y"
    folder.mkdir(parents=True)
    f = folder / "a.txt"
    f.write_text("x")
    os.chmod(f, 0o444)
    remove_tree(tmp_path / "x")
    assert not (tmp_path / "x").exists()
    remove_tree(tmp_path / "x")                                       # already gone: no error


def test_remove_tree_retries_transient_sharing_violations(tmp_path, monkeypatch):
    target = tmp_path / "t"
    target.mkdir()
    attempts = []
    real = service_module._rmtree_once

    def flaky(path):
        attempts.append(path)
        if len(attempts) < 3:
            raise PermissionError("in use")
        real(path)

    monkeypatch.setattr(service_module, "_rmtree_once", flaky)
    monkeypatch.setattr(service_module.time, "sleep", lambda s: None)
    remove_tree(target)
    assert len(attempts) == 3 and not target.exists()


def test_remove_tree_gives_up_after_the_last_attempt(tmp_path, monkeypatch):
    target = tmp_path / "t"
    target.mkdir()
    monkeypatch.setattr(service_module, "_rmtree_once", lambda p: (_ for _ in ()).throw(PermissionError("in use")))
    monkeypatch.setattr(service_module.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        remove_tree(target)


# ----------------------------------------------------------------------------------------------- sessions
async def test_session_lifecycle_and_not_found(env):
    svc = env.service
    a, b = svc.create_session(), svc.create_session()
    assert a.state == "empty" and a.document is None and a.title == "New chat"
    assert [s.id for s in svc.list_sessions()] == [b.id, a.id]
    renamed = svc.rename_session(a.id, "  My   chat ")
    assert renamed.title == "My chat" and svc.get_session(a.id).messages == []
    with pytest.raises(ServiceError) as exc:
        svc.rename_session(a.id, "   ")
    assert error_of(exc) == ("invalid_title", 400)
    for bad in ("0" * 32, "../../etc", "", "ABC", a.id + "x"):
        for call in (lambda s=bad: svc.get_session(s), lambda s=bad: svc.delete_session(s), lambda s=bad: svc.rename_session(s, "t"),
                     lambda s=bad: svc.document_path(s), lambda s=bad: svc.create_session(s)):
            with pytest.raises(ServiceError) as exc:
                call()
            assert error_of(exc) == ("session_not_found", 404)
    with pytest.raises(ServiceError) as exc:
        svc.get_message(a.id, "nope")
    assert error_of(exc) == ("message_not_found", 404)


async def test_attach_document_adopts_the_upload(env):
    sid = env.service.create_session().id
    tmp = env.scratch / "upload.pdf"
    shutil.copyfile(env.sample, tmp)
    session = env.service.attach_document(sid, "National Grid – Annual Report 2024.pdf", tmp)
    doc = session.document
    assert session.state == "indexing" and doc.status == "indexing" and doc.stage == "queued"
    assert doc.filename == "National Grid – Annual Report 2024.pdf" and doc.doc_name == "National_Grid_Annual_Report_2024.pdf"
    assert doc.page_count == 60 and doc.size_bytes == env.sample.stat().st_size and doc.created_at
    target = env.settings.session_dir(sid) / doc.doc_name
    assert target.read_bytes() == env.sample.read_bytes() and not tmp.exists()          # moved, not copied
    assert env.indexer.started == [(sid, target)]
    assert env.service.document_path(sid) == target
    assert env.store.get_document(sid).id == doc.id


async def test_attach_rejects_a_second_upload_while_indexing_and_when_ready(env):
    sid = env.service.create_session().id
    env.upload(sid)
    tmp = env.scratch / "again.pdf"
    shutil.copyfile(env.sample, tmp)
    for stage in ("indexing", "ready"):
        if stage == "ready":
            env.indexer.finish(sid)
        with pytest.raises(ServiceError) as exc:
            env.service.attach_document(sid, "second.pdf", tmp)
        assert error_of(exc) == ("document_already_uploaded", 409)
    assert tmp.exists() and len(env.indexer.started) == 1                                # untouched, nothing re-indexed


async def test_attach_rejects_upload_after_the_first_question(env):
    sid = env.ready_session()
    await collect(env.service.ask(sid, "first question"))
    assert env.service.get_session(sid).state == "locked"
    tmp = env.scratch / "late.pdf"
    shutil.copyfile(env.sample, tmp)
    with pytest.raises(ServiceError) as exc:
        env.service.attach_document(sid, "late.pdf", tmp)
    assert error_of(exc) == ("document_locked", 409)


async def test_attach_after_a_failed_index_replaces_the_old_files(env):
    sid = env.service.create_session().id
    env.upload(sid, "First try.pdf")
    first = env.store.get_document(sid)
    env.indexer.fail(sid)
    folder = env.settings.session_dir(sid)
    assert env.service.get_session(sid).state == "failed" and (folder / "pageindex" / "partial.json").exists()

    env.upload(sid, "Second try.pdf")
    session = env.service.get_session(sid)
    assert session.state == "indexing" and session.document.id != first.id and session.document.error is None
    assert session.document.doc_name == "Second_try.pdf"
    assert sorted(os.listdir(folder)) == ["Second_try.pdf"]                              # old pdf and pageindex folder are gone
    assert [Path(p).name for _, p in env.indexer.started] == ["First_try.pdf", "Second_try.pdf"]
    # the replacement has its own (clean) resources, i.e. no stale cache entry survives
    env.indexer.finish(sid)
    assert env.service.document_pages(sid).page_count == 60


async def test_attach_reuses_the_name_when_the_same_file_is_uploaded_again_after_failure(env):
    sid = env.service.create_session().id
    env.upload(sid, "Report.pdf")
    env.indexer.fail(sid)
    env.upload(sid, "Report.pdf")
    assert env.store.get_document(sid).doc_name == "Report.pdf"


@pytest.mark.parametrize("kind, code, status", [("garbage", "invalid_pdf", 400), ("empty", "invalid_pdf", 400),
                                                 ("encrypted", "encrypted_pdf", 422), ("scanned", "scanned_pdf", 422)])
async def test_attach_maps_pdf_errors(env, kind, code, status):
    data = {"garbage": b"definitely not a pdf", "empty": b"", "encrypted": pf.build_encrypted_pdf(), "scanned": pf.build_blank_pdf(4)}[kind]
    sid = env.service.create_session().id
    tmp = env.scratch / "bad.pdf"
    tmp.write_bytes(data)
    with pytest.raises(ServiceError) as exc:
        env.service.attach_document(sid, "bad.pdf", tmp)
    assert error_of(exc) == (code, status) and exc.value.message
    assert tmp.exists() and env.service.get_session(sid).state == "empty" and env.indexer.started == []
    assert not env.settings.session_dir(sid).exists() or not list(env.settings.session_dir(sid).iterdir())


async def test_attach_enforces_the_page_limit(build_env):
    env = build_env(max_pages=10)
    sid = env.service.create_session().id
    tmp = env.scratch / "big.pdf"
    shutil.copyfile(env.sample, tmp)
    with pytest.raises(ServiceError) as exc:
        env.service.attach_document(sid, "big.pdf", tmp)
    assert error_of(exc) == ("too_many_pages", 422) and "60" in exc.value.message and "10" in exc.value.message
    assert env.service.get_session(sid).state == "empty" and tmp.exists()


async def test_a_failed_failed_upload_keeps_the_previous_failure_visible(env):
    """A rejected replacement must not destroy the failed document (and its message) the user is looking at."""
    sid = env.service.create_session().id
    env.upload(sid)
    env.indexer.fail(sid, "Rate limited")
    bad = env.scratch / "bad.pdf"
    bad.write_bytes(b"nope")
    with pytest.raises(ServiceError):
        env.service.attach_document(sid, "bad.pdf", bad)
    session = env.service.get_session(sid)
    assert session.state == "failed" and session.document.error == "Rate limited"


async def test_attach_marks_the_document_failed_when_indexing_cannot_start(env):
    env.indexer.fail_start = True
    sid = env.service.create_session().id
    env.upload(sid)
    session = env.service.get_session(sid)
    assert session.state == "failed" and "upload" in session.document.error.lower()


async def test_concurrent_uploads_to_one_session_admit_exactly_one(env):
    sid = env.service.create_session().id
    tmps = []
    for i in range(3):
        t = env.scratch / f"u{i}.pdf"
        shutil.copyfile(env.sample, t)
        tmps.append(t)

    def attempt(t: Path) -> str:
        try:
            env.service.attach_document(sid, "r.pdf", t)
            return "ok"
        except ServiceError as exc:
            return exc.code

    results = await asyncio.gather(*(asyncio.to_thread(attempt, t) for t in tmps))
    assert sorted(results) == ["document_already_uploaded", "document_already_uploaded", "ok"]
    assert len(env.indexer.started) == 1


async def test_document_path_before_upload_is_404(env):
    sid = env.service.create_session().id
    with pytest.raises(ServiceError) as exc:
        env.service.document_path(sid)
    assert error_of(exc) == ("document_not_found", 404)


# ----------------------------------------------------------------------------------------------- new chat with the same document
async def test_new_chat_from_a_ready_session_copies_document_and_index(env):
    src = env.ready_session()
    await collect(env.service.ask(src, "ask something first"))                           # a locked source works too
    new = env.service.create_session(from_session=src)
    old_doc, doc = env.service.get_session(src).document, new.document
    assert new.id != src and new.state == "ready" and new.title == "New chat" and new.message_count == 0
    assert doc.id != old_doc.id and doc.status == "ready" and doc.doc_name == old_doc.doc_name and doc.filename == old_doc.filename
    assert doc.page_count == old_doc.page_count and env.store.get_document(new.id).pi_doc_id == CLONED_DOC_ID
    new_folder = env.settings.session_dir(new.id)
    assert (new_folder / doc.doc_name).read_bytes() == env.sample.read_bytes()
    assert (new_folder / "pageindex" / "manifest.json").read_text(encoding="utf-8") == json.dumps({"doc": "pi-fake"})
    assert env.indexer.started == [(src, env.settings.session_dir(src) / old_doc.doc_name)]   # nothing was re-indexed
    # independent copies: deleting the source leaves the clone intact and chat-able
    env.service.delete_session(src)
    events = await collect(env.service.ask(new.id, "does the clone still work?"))
    assert events[-1][0] == "done" and "answer_done" in names(events)


async def test_new_chat_from_unusable_sources(env):
    with pytest.raises(ServiceError) as exc:
        env.service.create_session(from_session="0" * 32)
    assert error_of(exc) == ("session_not_found", 404)
    empty = env.service.create_session().id
    indexing = env.service.create_session().id
    env.upload(indexing)
    failed = env.service.create_session().id
    env.upload(failed)
    env.indexer.fail(failed)
    for src in (empty, indexing, failed):
        with pytest.raises(ServiceError) as exc:
            env.service.create_session(from_session=src)
        assert error_of(exc) == ("document_not_ready", 409)
    assert len(env.service.list_sessions()) == 3                                         # no stray sessions


async def test_clone_failure_leaves_nothing_behind(env):
    src = env.ready_session()
    shutil.rmtree(env.settings.session_dir(src) / "pageindex")                           # the index is gone: copy must fail
    before = {s.id for s in env.service.list_sessions()}
    with pytest.raises(ServiceError) as exc:
        env.service.create_session(from_session=src)
    assert error_of(exc) == ("clone_failed", 500)
    assert {s.id for s in env.service.list_sessions()} == before
    assert sorted(p.name for p in env.settings.sessions_dir.iterdir()) == [src]


async def test_clone_uses_the_document_id_returned_by_the_cloner(env):
    src = env.ready_session()
    new = env.service.create_session(from_session=src)
    assert env.store.get_document(new.id).pi_doc_id == CLONED_DOC_ID != env.store.get_document(src).pi_doc_id


# ----------------------------------------------------------------------------------------------- delete
async def test_delete_session_removes_rows_files_and_cancels_indexing(env):
    sid = env.ready_session()
    env.service.document_pages(sid)                                                      # opens the PDF in the cache
    await collect(env.service.ask(sid, "one question"))
    folder = env.settings.session_dir(sid)
    assert folder.exists()
    env.service.delete_session(sid)
    assert not folder.exists() and env.indexer.cancelled == [sid]
    assert env.store.get_session(sid) is None and env.store.list_messages(sid) == []
    with pytest.raises(ServiceError) as exc:
        env.service.get_session(sid)
    assert error_of(exc) == ("session_not_found", 404)


async def test_delete_while_indexing_and_while_empty(env):
    empty = env.service.create_session().id
    env.service.delete_session(empty)                                                   # no folder was ever made
    sid = env.service.create_session().id
    env.upload(sid)
    env.service.delete_session(sid)
    assert env.indexer.cancelled == [empty, sid] and not env.settings.session_dir(sid).exists()


async def test_delete_that_cannot_remove_files_keeps_the_session(env, monkeypatch):
    sid = env.ready_session()
    monkeypatch.setattr(service_module, "remove_tree", lambda p: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(ServiceError) as exc:
        env.service.delete_session(sid)
    assert error_of(exc) == ("delete_failed", 500)
    assert env.store.get_session(sid) is not None                                        # can be retried
    monkeypatch.undo()
    env.service.delete_session(sid)
    assert env.store.get_session(sid) is None


# ----------------------------------------------------------------------------------------------- document views
async def test_document_pages_and_outline(env, sample_facts):
    sid = env.ready_session()
    pages = env.service.document_pages(sid)
    assert isinstance(pages, DocumentPages) and pages.page_count == 60 == len(pages.pages)
    assert all(p.width > 0 and p.height > 0 for p in pages.pages)
    fact = sample_facts[0]
    assert pages.pages[fact["page"] - 1].printed_page == fact["printed_page"]            # folio = physical - 2
    assert pages.pages[0].printed_page is None                                           # the cover has no folio
    outline = env.service.document_outline(sid)
    assert outline == [
        {"title": "Strategic report", "node_id": "0001", "start_index": 3, "end_index": 20, "nodes": [
            {"title": "Highlights", "node_id": "0002", "start_index": 4, "end_index": 4, "nodes": []}]},
        {"title": "Governance", "node_id": "0003", "start_index": 21, "end_index": 40, "nodes": []}]
    assert all("summary" not in n for n in outline)


async def test_document_views_need_a_ready_document(env):
    sid = env.service.create_session().id
    with pytest.raises(ServiceError) as exc:
        env.service.document_pages(sid)
    assert error_of(exc) == ("document_not_found", 404)
    env.upload(sid)
    for call in (env.service.document_pages, env.service.document_outline, lambda s: env.service.locate(s, 1, "x", None)):
        with pytest.raises(ServiceError) as exc:
            call(sid)
        assert error_of(exc) == ("document_not_ready", 409)
    assert env.service.document_path(sid).is_file()                                      # the file itself is served while indexing


async def test_outline_survives_an_unreadable_tree(build_env):
    def broken(settings, sid, pi_doc_id):
        raise FileNotFoundError("tree.json")

    env = build_env()
    env.service._tree_loader = broken
    sid = env.ready_session()
    assert env.service.document_outline(sid) == []
    assert env.service.document_pages(sid).page_count == 60


async def test_locate_by_quote_claim_and_page(env, sample_facts):
    sid = env.ready_session()
    fact = sample_facts[0]
    hit = env.service.locate(sid, fact["page"], fact["quote"], None)
    assert isinstance(hit, LocateResponse) and hit.page == fact["page"] == hit.hinted_page
    assert hit.method in ("exact", "fuzzy") and hit.score > 0.9 and hit.rects and hit.page_width and hit.matched_text
    assert all(0 <= r.x <= 1 and 0 <= r.y <= 1 and r.w > 0 and r.h > 0 for r in hit.rects)

    off = env.service.locate(sid, fact["page"] + 1, fact["quote"], None)                  # off-by-one citation is corrected
    assert off.page == fact["page"] and off.hinted_page == fact["page"] + 1 and off.rects

    by_claim = env.service.locate(sid, fact["page"], None, "We now connect 8.4 million customers, an increase of 96,000.")
    assert by_claim.rects and by_claim.page == fact["page"]

    miss = env.service.locate(sid, 7, "zebra quantum harmonica dinosaur", None)
    assert miss.method in ("page", "block") and (miss.method != "page" or (miss.rects == [] and miss.page_width))
    only_page = env.service.locate(sid, 7, None, "zebra quantum harmonica dinosaur")
    assert only_page.method == "page" and only_page.rects == [] and only_page.hinted_page == 7


async def test_locate_validation(env):
    sid = env.ready_session()
    for quote, claim in ((None, None), ("", "  "), ("   ", None)):
        with pytest.raises(ServiceError) as exc:
            env.service.locate(sid, 1, quote, claim)
        assert error_of(exc) == ("missing_quote", 400)
    for page in (0, -3, 61, 10_000):
        with pytest.raises(ServiceError) as exc:
            env.service.locate(sid, page, "customers", None)
        assert error_of(exc) == ("invalid_page", 400)


# ----------------------------------------------------------------------------------------------- resource cache
async def test_cache_is_an_lru_that_never_closes_a_leased_document(env, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_OPEN_DOCS", 2)
    s1, s2, s3 = (env.ready_session() for _ in range(3))
    svc = env.service

    r1 = svc._acquire(s1)
    svc._release(r1)
    r2 = svc._acquire(s2)                                                                 # stays leased (an answer is running)
    svc.document_pages(s3)                                                                # evicts s1 (oldest)
    with pytest.raises(RuntimeError):
        r1.pdf.page_words(1)                                                              # closed on eviction
    svc.document_pages(s1)                                                                # evicts s2, which is still in use ...
    assert r2.pdf.page_words(1).words                                                     # ... and must stay usable
    svc._release(r2)
    with pytest.raises(RuntimeError):
        r2.pdf.page_words(1)                                                              # closed once the last user let go


async def test_resources_are_shared_between_requests_and_closed_by_aclose(env):
    sid = env.ready_session()
    first, second = env.service._acquire(sid), env.service._acquire(sid)
    assert first is second and first.users == 2
    env.service._release(first)
    env.service._release(second)
    await env.service.aclose()
    with pytest.raises(RuntimeError):
        first.pdf.page_words(1)


async def test_concurrent_requests_on_different_sessions(env, sample_facts):
    a, b = env.ready_session(), env.ready_session()
    fact = sample_facts[0]

    def work(sid: str) -> tuple[int, int]:
        pages = env.service.document_pages(sid).page_count
        return pages, len(env.service.locate(sid, fact["page"], fact["quote"], None).rects)

    results = await asyncio.gather(*(asyncio.to_thread(work, s) for s in (a, b, a, b, a, b)))
    assert {r[0] for r in results} == {60} and all(r[1] > 0 for r in results)


# ----------------------------------------------------------------------------------------------- ask: validation
async def test_ask_validation_errors_are_raised_before_any_event(env):
    svc = env.service
    empty = svc.create_session().id
    indexing = svc.create_session().id
    env.upload(indexing)
    failed = svc.create_session().id
    env.upload(failed)
    env.indexer.fail(failed)
    ready = env.ready_session()

    cases = [(ready, "", "empty_question", 400), (ready, "   \n ", "empty_question", 400),
             (ready, "x" * 4001, "question_too_long", 400), ("0" * 32, "hi", "session_not_found", 404),
             ("../x", "hi", "session_not_found", 404), (empty, "hi", "document_not_ready", 409),
             (indexing, "hi", "document_not_ready", 409), (failed, "hi", "document_not_ready", 409)]
    for sid, content, code, status in cases:
        with pytest.raises(ServiceError) as exc:
            await svc.ask(sid, content).__anext__()
        assert error_of(exc) == (code, status), (sid, content[:10])
    assert env.store.list_messages(ready) == [] and env.qa.calls == []                    # nothing was persisted or started
    assert len((await collect(svc.ask(ready, "x" * 4000)))) > 3                           # the limit itself is inclusive


async def test_ask_without_an_openai_key_is_503(build_env):
    env = build_env(openai_api_key=None)
    sid = env.ready_session()
    with pytest.raises(ServiceError) as exc:
        await env.service.ask(sid, "hello").__anext__()
    assert error_of(exc) == ("openai_not_configured", 503)
    assert env.store.list_messages(sid) == []


async def test_demo_mock_counts_as_configured(build_env):
    env = build_env(openai_api_key=None, demo_mock=True)
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "hello"))
    assert events[-1][0] == "done"


# ----------------------------------------------------------------------------------------------- ask: the stream
async def test_ask_event_order_and_payload_shapes(env):
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "How many customers does Northbridge connect?"))
    assert names(events) == NAMES_HAPPY
    json.dumps(events)                                                                    # every payload is plain JSON

    start = payload(events, "message_start")
    user = Message.model_validate(start["user_message"])
    mid = start["message_id"]
    assert user.role == "user" and user.content == "How many customers does Northbridge connect?" and start["created_at"]
    assert all(p["message_id"] == mid for n, p in events if n not in ("message_start", "answer_done", "done"))

    step = Step.model_validate(payload(events, "step")["step"])
    assert step.status == "running" and step.pages
    done = payload(events, "step_done")
    assert (done["step_id"], done["elapsed_ms"], done["pages"]) == ("s1", 12, step.pages) and done["label"]
    assert Citation.model_validate(payload(events, "citation")["citation"]).rects == []   # preliminary: no rects yet

    message = Message.model_validate(payload(events, "answer_done")["message"])
    assert message.id == mid and message.role == "assistant" and message.status == "answered"
    assert "[[c1]]" in message.content and message.content.startswith("How many customers")
    [citation] = message.citations
    assert citation.id == "c1" and citation.rects and citation.printed_page and citation.section_path == ["Strategic report", "Highlights"]
    assert [s.page for s in message.sources] == [citation.page] and message.steps[0].status == "done"
    assert message.usage.input_tokens == 10 and message.elapsed_ms == 34 and message.evaluation.status == "pending"
    assert message.evaluation.n_contexts_input == 2 == message.evaluation.n_contexts_scored

    assert payload(events, "eval_started") == {"message_id": mid, "metrics": list(METRICS), "n_contexts": 2}
    results = [p for n, p in events if n == "eval_result"]
    assert [(r["metric"], r["value"], r["error"]) for r in results] == [("faithfulness", 0.9, None), ("answer_relevancy", 0.8, None), ("context_precision", 0.7, None)]
    scores = EvalScores.model_validate(payload(events, "eval_done")["evaluation"])
    assert scores.status == "done" and scores.faithfulness == 0.9 and scores.n_contexts_input == 2
    assert events[-1] == ("done", {})


async def test_ask_persists_both_messages_contexts_and_scores(env):
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "How many customers?"))
    mid = payload(events, "message_start")["message_id"]
    user, assistant = env.stored(sid)
    assert (user.role, user.status, user.content) == ("user", "answered", "How many customers?")
    streamed = Message.model_validate(payload(events, "answer_done")["message"])
    assert assistant.id == mid and assistant.status == "answered" and assistant.content == streamed.content
    assert assistant.citations == streamed.citations and assistant.sources == streamed.sources and assistant.steps == streamed.steps
    assert assistant.evaluation.status == "done" and assistant.evaluation.faithfulness == 0.9    # scores were saved before eval_done
    assert [c.page for c in env.store.get_contexts(mid)] == [2, env.qa.fact["page"]]
    assert env.service.get_message(sid, mid) == assistant
    detail = env.service.get_session(sid)
    assert detail.state == "locked" and detail.message_count == 2 and [m.id for m in detail.messages] == [user.id, mid]


async def test_status_streaming_is_visible_while_the_answer_is_running(env):
    sid = env.ready_session()
    env.qa.gate = threading.Event()
    agen = env.service.ask(sid, "slow one")
    seen = [await agen.__anext__() for _ in range(4)]                                     # message_start, step, step_done, token
    assert names(seen)[0] == "message_start"
    streaming = env.stored(sid)[1]
    assert streaming.role == "assistant" and streaming.status == "streaming" and streaming.content == ""
    env.qa.gate.set()
    rest = [e async for e in agen]
    assert rest[-1][0] == "done" and env.stored(sid)[1].status == "answered"


async def test_first_question_titles_the_session(env):
    sid = env.ready_session()
    question = "What is the status of GHG reduction technology available to the company? Please be thorough and cite pages."
    await collect(env.service.ask(sid, question))
    title = env.service.get_session(sid).title
    assert title == make_title(question) and len(title) <= 61 and title.startswith("What is the status of GHG reduction technology")
    await collect(env.service.ask(sid, "A completely different follow-up"))
    assert env.service.get_session(sid).title == title                                    # only the first question titles


async def test_a_renamed_session_keeps_its_title(env):
    sid = env.ready_session()
    env.service.rename_session(sid, "Board pack")
    await collect(env.service.ask(sid, "first question"))
    assert env.service.get_session(sid).title == "Board pack"


async def test_asking_moves_the_session_to_the_top(env):
    old = env.ready_session()
    newer = env.ready_session()
    assert [s.id for s in env.service.list_sessions()][0] == newer
    await collect(env.service.ask(old, "bump me"))
    assert [s.id for s in env.service.list_sessions()][0] == old


async def test_no_sources_answer(env):
    env.qa.cite, env.qa.status = False, "no_sources"
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "Is there a moon base?"))
    message = Message.model_validate(payload(events, "answer_done")["message"])
    assert message.status == "no_sources" and message.citations == [] and "citation" not in names(events)
    assert env.stored(sid)[1].status == "no_sources"


# ----------------------------------------------------------------------------------------------- ask: history and rewrite
async def test_history_is_plain_text_and_the_rewrite_runs_only_with_history(env):
    sid = env.ready_session()
    first = await collect(env.service.ask(sid, "q one"))
    assert env.qa.calls[0]["history"] == [] and env.qa.rewrite_calls == []                # no history: no rewrite
    assert env.evaluator.calls[0]["question"] == "q one"

    answer_one = Message.model_validate(payload(first, "answer_done")["message"]).content
    assert "[[c1]]" in answer_one
    second = await collect(env.service.ask(sid, "and for last year?"))
    history = env.qa.calls[1]["history"]
    assert history == [{"role": "user", "content": "q one"}, {"role": "assistant", "content": strip_markers(answer_one)}]
    assert all("[[" not in h["content"] for h in history)
    assert env.qa.rewrite_calls == [("and for last year?", history)]
    assert env.evaluator.calls[1]["question"] == "standalone: and for last year?"         # RAGAS sees the standalone question
    assert env.qa.calls[1]["question"] == "and for last year?"                            # the agent gets the question as typed
    assert names(second) == NAMES_HAPPY


async def test_history_window_and_failed_turns_are_excluded(build_env):
    env = build_env(history_turns=2)
    sid = env.ready_session()
    for i in range(3):
        await collect(env.service.ask(sid, f"question {i}"))
    env.qa.raise_after_token = FakeQAError("openai_rate_limit", "Slow down")
    await collect(env.service.ask(sid, "this one fails"))
    env.qa.raise_after_token = None
    await collect(env.service.ask(sid, "question 4"))
    history = env.qa.calls[-1]["history"]
    assert [h["content"] for h in history if h["role"] == "user"] == ["question 1", "question 2"]  # window of 2, failure skipped


async def test_a_failing_rewrite_falls_back_to_the_question_as_typed(env):
    env.qa.rewrite_raises = True
    sid = env.ready_session()
    await collect(env.service.ask(sid, "first"))
    events = await collect(env.service.ask(sid, "second"))
    assert env.evaluator.calls[1]["question"] == "second" and events[-1][0] == "done"


# ----------------------------------------------------------------------------------------------- ask: evaluation
async def test_context_cap_policy_is_applied_to_the_evaluator_input(build_env):
    env = build_env(eval_max_contexts=3)
    page = env.qa.fact["page"]
    env.qa.contexts = [ContextPage(page=p, text=f"page {p} text") for p in (10, 11, 12, 13)] + [ContextPage(page=page, text="cited page text"),
                                                                                                  ContextPage(page=10, text="duplicate read")]
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "q"))
    sent = env.evaluator.calls[0]["contexts"]
    assert [c.page for c in sent] == [10, 11, page]                                      # the cited page survives, read order kept
    assert payload(events, "eval_started")["n_contexts"] == 3
    final = payload(events, "eval_done")["evaluation"]
    assert final["n_contexts_input"] == 5 and final["n_contexts_scored"] == 3             # 5 distinct pages read, 3 scored
    assert [c.page for c in env.store.get_contexts(payload(events, "message_start")["message_id"])] == [10, 11, 12, 13, page, 10]  # all kept


async def test_eval_answer_keeps_markers_for_the_evaluator_to_strip(env):
    sid = env.ready_session()
    await collect(env.service.ask(sid, "q"))
    assert "[[c1]]" in env.evaluator.calls[0]["answer"]


@pytest.mark.parametrize("change, qa_change, reason", [
    ({"eval_enabled": False}, {}, "disabled"),
    ({}, {"contexts": []}, "no_contexts"),
    ({}, {"contexts": [ContextPage(page=3, text="   ")]}, "no_contexts"),
    ({}, {"cite": False, "status": "no_sources", "empty": True}, "empty_answer"),
])
async def test_evaluation_is_skipped_without_eval_events(build_env, change, qa_change, reason):
    env = build_env(**change)
    for key, value in qa_change.items():
        if key == "empty":
            env.qa.fact = dict(env.qa.fact)
        else:
            setattr(env.qa, key, value)
    if qa_change.get("empty"):
        env.qa.ask = _empty_answer_ask(env.qa)
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "q"))
    assert names(events)[-2:] == ["answer_done", "done"] and not any(n.startswith("eval_") for n in names(events))
    message = Message.model_validate(payload(events, "answer_done")["message"])
    assert message.evaluation.status == "skipped" and message.evaluation.skipped_reason == reason
    assert env.evaluator.calls == []
    assert env.stored(sid)[1].evaluation.skipped_reason == reason


def _empty_answer_ask(qa: FakeQA):
    from reportlens.citations import BuiltAnswer

    def ask(*, session_id, doc, question, history, ctx, cancel=None):
        qa.calls.append(dict(question=question, history=history))
        yield {"type": "final", "answer": BuiltAnswer(text="", citations=[], sources=[], stats={}), "contexts": [ContextPage(page=2, text="x")],
               "usage": None, "steps": [], "status": "no_sources", "elapsed_ms": 1}

    return ask


async def test_evaluation_task_survives_an_abandoned_stream_and_saves_its_scores(env):
    env.evaluator.gate = asyncio.Event()
    sid = env.ready_session()
    agen = env.service.ask(sid, "q")
    seen = []
    async for event in agen:
        seen.append(event)
        if event[0] == "eval_started":
            break
    await agen.aclose()                                                                   # the browser tab closed during scoring
    mid = payload(seen, "message_start")["message_id"]
    assert env.stored(sid)[1].status == "answered"                                        # a finished answer is not "cancelled"
    assert mid in env.service._eval_tasks and not env.service._eval_tasks[mid].done()
    await asyncio.wait_for(env.evaluator.entered.wait(), 5)
    assert env.stored(sid)[1].evaluation.status == "running"
    env.evaluator.gate.set()
    await wait_until(lambda: env.stored(sid)[1].evaluation.status == "done")
    assert env.stored(sid)[1].evaluation.faithfulness == 0.9 and env.stored(sid)[1].evaluation.judge_model == "fake-judge"
    await wait_until(lambda: not env.service._eval_tasks)


async def test_a_crashing_evaluator_ends_the_stream_with_failed_scores(env):
    env.evaluator.explode = True
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "q"))
    assert names(events)[-2:] == ["eval_done", "done"]
    scores = EvalScores.model_validate(payload(events, "eval_done")["evaluation"])
    assert scores.status == "failed" and set(scores.errors) == set(METRICS) and "judge exploded" not in json.dumps(events)
    assert env.stored(sid)[1].evaluation.status == "failed"


async def test_aclose_cancels_scoring_closes_the_evaluator_and_leaves_an_honest_record(env):
    env.evaluator.gate = asyncio.Event()
    sid = env.ready_session()
    agen = env.service.ask(sid, "q")
    async for event in agen:
        if event[0] == "eval_started":
            break
    await asyncio.wait_for(env.evaluator.entered.wait(), 5)
    rest = asyncio.create_task(collect(agen))                                             # a client is still listening
    await env.service.aclose()
    tail = await asyncio.wait_for(rest, 5)                                                # ... and is released, not hung
    assert tail[-1][0] == "done" and env.evaluator.closed
    stored = env.stored(sid)[1]
    assert stored.evaluation.status == "failed" and "interrupted" in stored.evaluation.errors["evaluation"].lower()
    assert env.service._eval_tasks == {} and env.service._cache == {}


# ----------------------------------------------------------------------------------------------- evaluate_message
async def test_evaluate_message_reruns_and_persists(env):
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "first q"))
    events2 = await collect(env.service.ask(sid, "second q"))
    mid = payload(events2, "message_start")["message_id"]
    env.evaluator.calls.clear()
    scores = await env.service.evaluate_message(sid, mid)
    assert scores.status == "done" and scores.faithfulness == 0.9 and scores.n_contexts_input == 2
    [call] = env.evaluator.calls
    assert call["question"] == "standalone: second q" and "[[c1]]" in call["answer"] and [c.page for c in call["contexts"]] == [2, env.qa.fact["page"]]
    assert env.service.get_message(sid, mid).evaluation == scores
    # the first answer has no history: its question is used as typed
    env.evaluator.calls.clear()
    await env.service.evaluate_message(sid, payload(events, "message_start")["message_id"])
    assert env.evaluator.calls[0]["question"] == "first q"


async def test_evaluate_message_errors(env):
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "q"))
    start = payload(events, "message_start")
    for call, code, status in (
        (lambda: env.service.evaluate_message(sid, "nope"), "message_not_found", 404),
        (lambda: env.service.evaluate_message("0" * 32, start["message_id"]), "session_not_found", 404),
        (lambda: env.service.evaluate_message(sid, start["user_message"]["id"]), "not_evaluable", 409),
    ):
        with pytest.raises(ServiceError) as exc:
            await call()
        assert error_of(exc) == (code, status)
    env.qa.raise_after_token = FakeQAError("agent_failed", "x")
    failed = payload(await collect(env.service.ask(sid, "will fail")), "message_start")["message_id"]
    with pytest.raises(ServiceError) as exc:
        await env.service.evaluate_message(sid, failed)
    assert error_of(exc) == ("not_evaluable", 409)


async def test_evaluate_message_rejects_a_concurrent_run(env):
    sid = env.ready_session()
    mid = payload(await collect(env.service.ask(sid, "q")), "message_start")["message_id"]
    env.evaluator.calls.clear()
    env.evaluator.entered.clear()
    env.evaluator.gate = asyncio.Event()
    first = asyncio.create_task(env.service.evaluate_message(sid, mid))
    await asyncio.wait_for(env.evaluator.entered.wait(), 5)
    with pytest.raises(ServiceError) as exc:
        await env.service.evaluate_message(sid, mid)
    assert error_of(exc) == ("evaluation_in_progress", 409)
    assert env.service.get_message(sid, mid).evaluation.status == "running"
    env.evaluator.gate.set()
    assert (await first).status == "done"
    assert len(env.evaluator.calls) == 1


async def test_evaluate_message_survives_a_dropped_request(env):
    sid = env.ready_session()
    mid = payload(await collect(env.service.ask(sid, "q")), "message_start")["message_id"]
    env.evaluator.entered.clear()
    env.evaluator.gate = asyncio.Event()
    request = asyncio.create_task(env.service.evaluate_message(sid, mid))
    await asyncio.wait_for(env.evaluator.entered.wait(), 5)
    request.cancel()                                                                      # the HTTP client went away
    with pytest.raises(asyncio.CancelledError):
        await request
    env.evaluator.gate.set()
    await wait_until(lambda: env.service.get_message(sid, mid).evaluation.status == "done")


async def test_evaluate_message_skips_when_there_is_nothing_to_score(build_env):
    env = build_env(openai_api_key=None)                                                  # asking is refused, scoring is just skipped
    sid = env.ready_session()
    user = Message(id=new_id(), session_id=sid, role="user", content="q")
    reply = Message(id=new_id(), session_id=sid, role="assistant", content="An answer", status="answered", evaluation=EvalScores(status="pending"))
    env.store.add_message(user)
    env.store.add_message(reply, contexts=[ContextPage(page=2, text="page two")])
    scores = await env.service.evaluate_message(sid, reply.id)
    assert scores.status == "skipped" and scores.skipped_reason == "no_api_key" and scores.n_contexts_input == 1
    assert env.store.get_message(sid, reply.id).evaluation.skipped_reason == "no_api_key" and env.evaluator.calls == []

    env2 = build_env()
    sid2 = env2.ready_session()
    bare = Message(id=new_id(), session_id=sid2, role="assistant", content="An answer", status="answered")
    env2.store.add_message(bare)                                                          # no contexts were stored
    assert (await env2.service.evaluate_message(sid2, bare.id)).skipped_reason == "no_contexts"


async def test_stale_pending_scores_are_shown_as_failed_and_can_be_rerun(env):
    sid = env.ready_session()
    reply = Message(id=new_id(), session_id=sid, role="assistant", content="An answer [[c1]]", status="answered",
                    evaluation=EvalScores(status="pending", n_contexts_input=1))
    env.store.add_message(Message(id=new_id(), session_id=sid, role="user", content="q"))
    env.store.add_message(reply, contexts=[ContextPage(page=2, text="page two")])
    shown = env.service.get_session(sid).messages[1].evaluation
    assert shown.status == "failed" and "interrupted" in shown.errors["evaluation"].lower()
    assert env.service.get_message(sid, reply.id).evaluation.status == "failed"
    assert (await env.service.evaluate_message(sid, reply.id)).status == "done"
    assert env.service.get_session(sid).messages[1].evaluation.status == "done"


# ----------------------------------------------------------------------------------------------- ask: failures
async def test_qa_error_becomes_an_error_event_and_a_failed_message(env):
    env.qa.raise_after_token = FakeQAError("openai_auth", "OpenAI rejected the API key.")
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "q"))
    assert names(events) == ["message_start", "step", "step_done", "token", "error", "done"]
    mid = payload(events, "message_start")["message_id"]
    assert payload(events, "error") == {"code": "openai_auth", "message": "OpenAI rejected the API key.", "message_id": mid}
    stored = env.stored(sid)[1]
    assert stored.status == "error" and stored.error == "openai_auth" and stored.content.startswith("q:")   # partial text kept
    assert env.evaluator.calls == []
    env.qa.raise_after_token = None                                                       # the session is usable again
    assert names(await collect(env.service.ask(sid, "retry")))[-1] == "done"


async def test_other_errors_store_the_readable_message(env):
    env.qa.raise_after_token = FakeQAError("agent_failed", "The agent could not read page 7.")
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "q"))
    assert payload(events, "error")["code"] == "agent_failed"
    assert env.stored(sid)[1].error == "The agent could not read page 7."


async def test_engine_crash_is_reported_without_a_stack_trace(env, caplog):
    env.qa.raise_after_token = RuntimeError("secret internals /home/x/y.py line 3")
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "q"))
    error = payload(events, "error")
    assert error["code"] == "agent_failed" and "secret" not in json.dumps(events) and "Traceback" not in error["message"]
    assert env.stored(sid)[1].status == "error" and "secret" not in (env.stored(sid)[1].error or "")
    assert any("QA engine crashed" in r.message for r in caplog.records)                  # ... but it is logged


async def test_engine_that_ends_without_an_answer_is_an_error(env):
    env.qa.with_final = False
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "q"))
    assert payload(events, "error")["code"] == "agent_failed" and env.stored(sid)[1].status == "error"


async def test_unreadable_answer_payload_ends_as_an_error_event(env):
    sid = env.ready_session()

    def broken(**kw):
        yield {"type": "token", "text": "hi"}
        yield {"type": "final"}                                                           # contract violation: no "answer"

    env.qa.ask = broken
    events = await collect(env.service.ask(sid, "q"))
    assert names(events)[-2:] == ["error", "done"] and env.stored(sid)[1].status == "error"


async def test_a_document_that_cannot_be_opened_is_an_error_event(env):
    sid = env.ready_session()
    (env.settings.session_dir(sid) / env.store.get_document(sid).doc_name).unlink()
    events = await collect(env.service.ask(sid, "q"))
    assert names(events) == ["message_start", "error", "done"] and payload(events, "error")["code"] == "document_not_found"
    assert env.stored(sid)[1].status == "error" and env.qa.calls == []


# ----------------------------------------------------------------------------------------------- ask: concurrency and cancellation
async def test_second_question_on_the_same_session_is_rejected_while_one_is_running(env):
    sid = env.ready_session()
    env.qa.gate = threading.Event()
    first = env.service.ask(sid, "first")
    await first.__anext__()                                                               # message_start: the run is registered
    with pytest.raises(ServiceError) as exc:
        await env.service.ask(sid, "second").__anext__()
    assert error_of(exc) == ("session_busy", 409)
    assert [m.role for m in env.stored(sid)] == ["user", "assistant"]                     # the rejected question left no trace
    env.qa.gate.set()
    rest = [e async for e in first]
    assert rest[-1][0] == "done"
    assert names(await collect(env.service.ask(sid, "third")))[-1] == "done"              # the guard was released


async def test_two_questions_on_different_sessions_run_at_the_same_time(env):
    a, b = env.ready_session(), env.ready_session()
    env.qa.barrier = threading.Barrier(2)                                                 # each engine waits for the other: only truly concurrent runs pass

    ra, rb = await asyncio.wait_for(asyncio.gather(collect(env.service.ask(a, "alpha")), collect(env.service.ask(b, "bravo"))), 20)
    for sid, result, text in ((a, ra, "alpha"), (b, rb, "bravo")):
        assert names(result) == NAMES_HAPPY
        assert Message.model_validate(payload(result, "answer_done")["message"]).content.startswith(text)
        assert [m.content for m in env.stored(sid) if m.role == "user"] == [text]
    assert payload(ra, "message_start")["message_id"] != payload(rb, "message_start")["message_id"]
    assert len(env.evaluator.calls) == 2


async def test_client_disconnect_cancels_the_run_and_marks_the_message(env):
    env.qa.gate = threading.Event()                                                       # the engine would wait forever
    sid = env.ready_session()
    agen = env.service.ask(sid, "never finishes")
    seen = []
    async for event in agen:
        seen.append(event)
        if event[0] == "token":
            break
    await agen.aclose()                                                                   # what the web layer does on disconnect
    assert env.qa.saw_cancel.is_set() and env.qa.ended.is_set()                           # the cancel Event reached the engine, which stopped
    assert not any(t.name.startswith("ask-") and t.is_alive() for t in threading.enumerate())
    mid = payload(seen, "message_start")["message_id"]
    stored = env.stored(sid)[1]
    assert stored.id == mid and stored.status == "error" and stored.error == "cancelled" and stored.content == "never finishes:"
    assert env.evaluator.calls == []
    env.qa.gate = None                                                                    # guard released: the user can ask again
    again = await collect(env.service.ask(sid, "second try"))
    assert names(again)[-1] == "done"
    assert [h["content"] for h in env.qa.calls[-1]["history"]] == []                      # the cancelled turn is not history


async def test_cancelling_the_consuming_task_cancels_the_run(env):
    env.qa.gate = threading.Event()
    sid = env.ready_session()

    async def consume() -> None:
        async for _ in env.service.ask(sid, "q"):
            pass

    task = asyncio.create_task(consume())
    await wait_flag(env.qa.started)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_flag(env.qa.saw_cancel)
    await wait_until(lambda: env.stored(sid)[1].error == "cancelled")
    await wait_flag(env.qa.ended)


async def test_disconnect_before_the_first_event_cancels_nothing_and_releases_the_guard(env):
    sid = env.ready_session()
    agen = env.service.ask(sid, "q")
    await agen.__anext__()                                                                # message_start only
    await agen.aclose()
    assert env.stored(sid)[1].error == "cancelled"
    assert names(await collect(env.service.ask(sid, "again")))[-1] == "done"


async def test_cancelling_while_the_document_is_still_loading_leaks_no_lease(env):
    entered, release = threading.Event(), threading.Event()

    def slow_tree(settings, sid, pi_doc_id):
        entered.set()
        release.wait(10)
        return TREE

    env.service._tree_loader = slow_tree
    sid = env.ready_session()

    async def consume() -> None:
        async for _ in env.service.ask(sid, "q"):
            pass

    task = asyncio.create_task(consume())
    await wait_flag(entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await wait_until(lambda: sid in env.service._cache and env.service._cache[sid].users == 0)   # the late lease came back
    assert env.stored(sid)[1].error == "cancelled" and env.qa.calls == []


async def test_cancelling_while_the_question_is_being_saved_does_not_leave_a_streaming_row(env, monkeypatch):
    saving, release = threading.Event(), threading.Event()
    real = env.service._begin_turn

    def slow_begin(sid, question):
        saving.set()
        release.wait(10)
        return real(sid, question)

    monkeypatch.setattr(env.service, "_begin_turn", slow_begin)
    sid = env.ready_session()

    async def consume() -> None:
        async for _ in env.service.ask(sid, "q"):
            pass

    task = asyncio.create_task(consume())
    await wait_flag(saving)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await wait_until(lambda: len(env.stored(sid)) == 2 and env.stored(sid)[1].status == "error")
    assert env.stored(sid)[1].error == "cancelled" and env.qa.calls == []
    assert names(await collect(env.service.ask(sid, "next")))[-1] == "done"                  # and the session is not stuck busy


async def test_deleting_a_session_cancels_its_running_question(env):
    sid = env.ready_session()
    env.qa.gate = threading.Event()
    agen = env.service.ask(sid, "q")
    async for event in agen:
        if event[0] == "token":
            break
    await wait_flag(env.qa.started)
    await asyncio.to_thread(env.service.delete_session, sid)
    rest = await asyncio.wait_for(collect(agen), 10)
    assert payload(rest, "error")["code"] == "cancelled" and rest[-1][0] == "done"
    assert env.qa.saw_cancel.is_set() and env.store.get_session(sid) is None


# ----------------------------------------------------------------------------------------------- restart
async def test_messages_survive_a_restart_and_interrupted_work_is_recovered(build_env, settings):
    env = build_env()
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "How many customers?"))
    mid = payload(events, "message_start")["message_id"]
    before = env.service.get_session(sid)

    other = env.service.create_session().id                                              # work that a crash would interrupt
    env.upload(other)
    env.store.add_message(Message(id=new_id(), session_id=sid, role="assistant", status="streaming", content="half an ans"))
    # (the second user message is missing on purpose: a crash can leave any prefix)

    store2 = Store(settings.db_path)                                                      # "restart": new Store + service, same data dir
    try:
        svc2 = ReportLensService(settings, store=store2, indexer=FakeIndexer(store2, settings), qa=FakeQA(env.qa.fact),
                                 evaluator=FakeEvaluator(), tree_loader=lambda s, i, p: TREE, index_cloner=fake_clone_index)
        after = svc2.get_session(sid)
        assert after.messages[:2] == before.messages and after.document == before.document and after.title == before.title
        assert after.messages[1].id == mid and after.messages[1].evaluation.status == "done" and after.messages[1].citations[0].rects
        interrupted = after.messages[2]
        assert interrupted.status == "error" and interrupted.error == INTERRUPTED_ANSWER_ERROR and interrupted.content == "half an ans"
        recovered = svc2.get_session(other)
        assert recovered.state == "failed" and recovered.document.error == INTERRUPTED_INDEXING_ERROR
        assert svc2.document_pages(sid).page_count == 60                                  # files and resources work after the restart
        assert [c.page for c in store2.get_contexts(mid)] == [2, env.qa.fact["page"]]
        assert names(await collect(svc2.ask(sid, "and then?")))[-1] == "done"
        await svc2.aclose()
    finally:
        store2.close()


async def test_long_documents_are_squashed_once_when_opened(build_env, monkeypatch):
    monkeypatch.setattr(service_module, "SQUASH_WARM_MIN_PAGES", 10)                     # the sample has 60 pages
    env = build_env()
    sid = env.ready_session()
    assert env.service.document_pages(sid).page_count == 60
    assert env.service._cache[sid].pdf.squashed_ready


async def test_stored_page_texts_are_handed_to_the_folio_detection(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(service_module, "_default_page_texts_loader", lambda settings, sid, pid: ["one", "two"])
    monkeypatch.setattr(service_module, "detect_printed_labels", lambda path, texts=None: seen.setdefault("texts", texts) and [None] * 60)
    sid = env.ready_session()
    env.service.document_pages(sid)
    assert seen["texts"] == ["one", "two"]


async def test_short_documents_are_not_squashed_when_opened(env):
    sid = env.ready_session()
    env.service.document_pages(sid)
    assert not env.service._cache[sid].pdf.squashed_ready


async def test_restart_marks_unfinished_scores_as_interrupted(build_env, settings):
    env = build_env()
    sid = env.ready_session()
    events = await collect(env.service.ask(sid, "How many customers?"))
    mid = payload(events, "message_start")["message_id"]
    msg = env.store.get_message(sid, mid)
    env.store.update_message(msg.model_copy(update={"evaluation": EvalScores(status="running", n_contexts_input=2, n_contexts_scored=2)}))

    store2 = Store(settings.db_path)                                                      # "restart"
    try:
        svc2 = ReportLensService(settings, store=store2, indexer=FakeIndexer(store2, settings), qa=FakeQA(env.qa.fact),
                                 evaluator=FakeEvaluator(), tree_loader=lambda s, i, p: TREE, index_cloner=fake_clone_index)
        ev = svc2.get_message(sid, mid).evaluation
        assert (ev.status, ev.skipped_reason, ev.n_contexts_input) == ("skipped", "interrupted", 2)
        await svc2.aclose()
    finally:
        store2.close()


# ----------------------------------------------------------------------------------------------- health
async def test_health(env):
    info = env.service.health()
    assert info["ok"] is True and info["version"] == "0.1.0" and info["openai_configured"] is True and info["demo_mock"] is False
    assert "pageindex_version" in info and isinstance(info["environment"], dict)
    assert "sk-test-not-real" not in json.dumps(info)


async def test_health_survives_a_broken_environment_check(env, monkeypatch):
    import reportlens.pageindex_compat as compat

    monkeypatch.setattr(compat, "check_environment", lambda s: (_ for _ in ()).throw(RuntimeError("sdk exploded")))
    info = env.service.health()
    assert info["ok"] is True and info["pageindex_version"] is None and info["environment"] == {}


# ----------------------------------------------------------------------------------------------- real engines
_REAL = all(importlib.util.find_spec(f"reportlens.{m}") is not None for m in ("indexer", "qa"))


@pytest.mark.slow
@pytest.mark.skipif(not _REAL, reason="reportlens.indexer / reportlens.qa are not available yet")
async def test_real_engines_against_the_mock_openai_server(settings, sample_pdf, sample_facts, mock_openai, tmp_path):
    """Real Store + IndexService + QAEngine + Evaluator talking to devtools.mock_openai: index once, then ask, follow up,
    disconnect, fail and clone on top of that one index."""
    cfg = settings.with_(openai_base_url=mock_openai.base_url, openai_api_key="sk-mock", data_dir=tmp_path / "real")
    svc = ReportLensService(cfg)
    fact = next(f for f in sample_facts if f["kind"] == "text")
    try:
        sid = svc.create_session().id
        upload = tmp_path / "up.pdf"
        shutil.copyfile(sample_pdf, upload)
        svc.attach_document(sid, "Northbridge Annual Report.pdf", upload)
        deadline = time.monotonic() + 120
        while svc.get_session(sid).document.status == "indexing":
            assert time.monotonic() < deadline, "indexing did not finish"
            await asyncio.sleep(0.5)
        document = svc.get_session(sid).document
        assert document.status == "ready", document.error
        assert svc.document_outline(sid) and svc.document_pages(sid).page_count == 60

        # a cited answer, scored by the real Evaluator
        events = await collect(svc.ask(sid, fact["question"]))
        got = names(events)
        assert got[0] == "message_start" and got[-1] == "done" and "answer_done" in got and "eval_done" in got, got
        message = Message.model_validate(payload(events, "answer_done")["message"])
        assert message.status == "answered" and fact["key"] in message.content, message.content
        assert any(c.page == fact["page"] and c.rects for c in message.citations)
        stored = svc.get_message(sid, message.id)
        assert stored.evaluation.status == "done" and stored.evaluation.n_contexts_input >= 1
        assert svc.get_session(sid).title.startswith(fact["question"][:20])

        # a follow-up goes through the real question rewrite
        mock_openai.clear_requests()
        followup = await collect(svc.ask(sid, "and what about the previous year?"))
        assert names(followup)[-1] == "done"
        assert any(r["json"].get("model") == cfg.question_rewrite_model for r in mock_openai.requests)

        # a dropped connection while the model "thinks" cancels the run
        mock_openai.delay_ms = 150
        agen = svc.ask(sid, fact["question"])
        async for event in agen:
            if event[0] == "step":
                break
        await agen.aclose()
        mock_openai.delay_ms = 0
        cancelled = svc.get_session(sid).messages[-1]
        assert cancelled.status == "error" and cancelled.error == "cancelled"
        assert not any(t.name.startswith("ask-") and t.is_alive() for t in threading.enumerate())

        # an OpenAI failure surfaces as a worded error event
        mock_openai.fail_next(kind="auth", path="/responses")
        failed = await collect(svc.ask(sid, fact["question"]))
        assert payload(failed, "error")["code"] == "openai_auth" and names(failed)[-1] == "done"
        assert svc.get_session(sid).messages[-1].error == "openai_auth"

        # new chat with the same document: real index copy, immediately answerable
        clone = svc.create_session(from_session=sid)
        assert clone.state == "ready" and clone.document.id != document.id and clone.message_count == 0
        again = await collect(svc.ask(clone.id, fact["question"]))
        assert "answer_done" in names(again) and fact["key"] in Message.model_validate(payload(again, "answer_done")["message"]).content
    finally:
        await svc.aclose()
        svc._store.close()


# ----------------------------------------------------------------------------------------------- small hosts (LOW_MEMORY)
async def test_scoring_libraries_are_loaded_off_the_event_loop(env):
    """The first evaluation imports RAGAS (seconds; tens of seconds on a 0.1 CPU host): it must not stall the loop's other work."""
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []
    real = env.service._get_evaluator
    env.service._get_evaluator = lambda: (seen.append(threading.current_thread()), real())[1]
    await collect(env.service.ask(env.ready_session(), "q"))
    assert seen and all(t is not loop_thread for t in seen)


async def test_low_memory_keeps_one_document_open_and_trims_after_scoring(build_env, monkeypatch):
    trimmed: list[bool] = []
    monkeypatch.setattr(service_module, "_trim_memory", lambda: trimmed.append(True))
    env = build_env(low_memory=True, max_open_docs=1)
    s1, s2 = env.ready_session(), env.ready_session()
    r1 = env.service._acquire(s1)
    env.service._release(r1)
    env.service._release(env.service._acquire(s2))
    with pytest.raises(RuntimeError):
        r1.pdf.page_words(1)                                                              # the first one was closed: only one is open
    await collect(env.service.ask(s2, "q"))
    assert trimmed, "memory is handed back to the OS after a scoring run"


async def test_idle_documents_are_closed_before_an_indexing_child_and_leased_ones_stay_open(build_env):
    env = build_env()
    idle_sid, busy_sid = env.ready_session(), env.ready_session()
    idle = env.service._acquire(idle_sid)
    env.service._release(idle)
    busy = env.service._acquire(busy_sid)
    try:
        env.service._shed_idle_documents()
        assert list(env.service._cache) == [busy_sid]
        with pytest.raises(RuntimeError):
            idle.pdf.page_words(1)                                                        # closed
        assert busy.pdf.page_words(1) is not None                                         # an answer in flight keeps its document
    finally:
        env.service._release(busy)
    assert env.service.document_pages(idle_sid).page_count == 60                          # the next request simply reopens it


def test_the_service_hands_its_memory_saver_to_an_injected_indexer(settings):
    seen: list = []

    class HookedIndexer(FakeIndexer):                                                     # the web app builds the indexer and injects it
        def set_heavy_job_hook(self, hook):
            seen.append(hook)

    store = Store(settings.db_path)
    try:
        svc = ReportLensService(settings, store=store, indexer=HookedIndexer(store, settings), qa=object())
        assert seen == [svc._shed_idle_documents]
        ReportLensService(settings, store=store, indexer=FakeIndexer(store, settings), qa=object())   # an indexer without the hook is fine
    finally:
        store.close()


async def test_the_normal_configuration_never_trims(env, monkeypatch):
    monkeypatch.setattr(service_module, "_trim_memory", lambda: pytest.fail("trim in normal mode"))
    await collect(env.service.ask(env.ready_session(), "q"))


def test_the_real_evaluator_is_built_with_the_datasets_saver_only_in_low_memory_mode(settings, monkeypatch):
    calls: list[bool] = []
    import reportlens.lowmem as lowmem_module

    monkeypatch.setattr(lowmem_module, "stub_datasets_for_ragas", lambda: calls.append(True) or True)
    monkeypatch.setitem(sys.modules, "reportlens.evaluation", types.SimpleNamespace(Evaluator=lambda s: ("evaluator", s.low_memory)))
    store = Store(settings.db_path)
    try:
        for low in (False, True):
            svc = ReportLensService(settings.with_(low_memory=low), store=store, indexer=FakeIndexer(store, settings), qa=object())
            assert svc._get_evaluator() == ("evaluator", low)
        assert calls == [True]
    finally:
        store.close()
