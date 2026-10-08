"""Indexing in a short-lived child process (Settings.index_in_subprocess; on by default in LOW_MEMORY mode): the real SDK runs in
`python -m reportlens.indexing_worker` against the fake OpenAI server, progress and failures cross the pipe, cancelling kills
the child, and a child that dies (the kernel's out-of-memory killer) becomes a plain-language failure."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from devtools.mock_openai import MockServer
from reportlens import indexer
from reportlens.config import Settings
from reportlens.indexer import CANCELLED_ERROR, Fault, IndexService, RemoteFault, load_page_texts, load_tree, translate_error
from reportlens.store import Store
from tests.test_indexer import (cfg, flat_pdf, make_service, mock, new_session, run_job, small_pdf, store, wait_for)  # noqa: F401
from tests.test_pageindex_compat import clean_sdk_state  # noqa: F401  (autouse)


@pytest.fixture
def child_cfg(cfg: Settings) -> Settings:
    return cfg.with_(index_in_subprocess=True)


class SpawnRecorder:
    """Wraps subprocess.Popen as the indexer sees it, so a test can look at the children it started."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.procs: list[subprocess.Popen] = []
        real = subprocess.Popen

        def popen(*args: Any, **kwargs: Any) -> subprocess.Popen:
            proc = real(*args, **kwargs)
            self.procs.append(proc)
            return proc

        monkeypatch.setattr(indexer.subprocess, "Popen", popen)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> SpawnRecorder:
    return SpawnRecorder(monkeypatch)


def test_a_report_is_indexed_by_a_child_process_and_progress_crosses_the_pipe(child_cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                                             make_service: Callable[..., IndexService], spawned: SpawnRecorder) -> None:
    sid, doc, watcher = run_job(make_service(settings=child_cfg), store, child_cfg, small_pdf, timeout=120)
    assert doc.status == "ready", doc.error
    assert (doc.page_count, doc.progress) == (12, 1.0)
    assert len(spawned.procs) == 1 and spawned.procs[0].returncode is not None, "one child, and it has ended"
    for stage in ("extracting_text", "summarizing", "finalizing"):
        assert stage in watcher.stages, watcher.stages
    assert watcher.progress == sorted(watcher.progress)                    # never backwards
    assert max(p for s, _, p, _ in watcher.samples if s == "indexing") <= 0.95
    assert load_tree(child_cfg, sid) and len(load_page_texts(child_cfg, sid) or []) == 12
    assert {r["json"].get("model") for r in mock.requests if r.get("json")} >= {"idx-model-x"}, "the child used the configured model"


def test_the_api_key_never_appears_on_the_childs_command_line_or_environment(child_cfg: Settings, store: Store, small_pdf: Path,
                                                                            make_service: Callable[..., IndexService], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list, dict]] = []
    real = subprocess.Popen

    def popen(args: Any, **kwargs: Any) -> subprocess.Popen:
        seen.append((list(args), dict(kwargs.get("env") or {})))
        return real(args, **kwargs)

    monkeypatch.setattr(indexer.subprocess, "Popen", popen)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _, doc, _ = run_job(make_service(settings=child_cfg), store, child_cfg, small_pdf, timeout=120)
    assert doc.status == "ready", doc.error
    (args, env), = seen
    assert "sk-test-indexer" not in " ".join(args) and "sk-test-indexer" not in env.values()
    assert env.get("MALLOC_ARENA_MAX") == "2"


def test_a_failure_in_the_child_arrives_classified_and_unredacted_text_is_not_leaked(child_cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                                                    make_service: Callable[..., IndexService]) -> None:
    mock.fail_next(kind="auth", count=1000)
    sid, doc, _ = run_job(make_service(settings=child_cfg), store, child_cfg, small_pdf, timeout=120)
    assert (doc.status, doc.stage) == ("failed", "failed")
    assert "API key" in (doc.error or "") and "sk-test" not in (doc.error or "")
    assert store.get_session(sid).state == "failed"  # type: ignore[union-attr]


def test_a_model_access_failure_names_the_model_and_the_setting(child_cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                               make_service: Callable[..., IndexService]) -> None:
    mock.fail_next(kind="model", count=1000)
    _, doc, _ = run_job(make_service(settings=child_cfg), store, child_cfg, small_pdf, timeout=120)
    assert doc.status == "failed" and "idx-model-x" in (doc.error or "") and "PI_INDEX_MODEL" in (doc.error or "")


def test_unusable_pdfs_are_refused_before_a_child_is_started(child_cfg: Settings, store: Store, tmp_path: Path,
                                                            make_service: Callable[..., IndexService], spawned: SpawnRecorder) -> None:
    from tests.pdf_factory import build_blank_pdf

    pdf = tmp_path / "scanned.pdf"
    pdf.write_bytes(build_blank_pdf(4))
    _, doc, _ = run_job(make_service(settings=child_cfg), store, child_cfg, pdf)
    assert doc.status == "failed" and "scanned" in (doc.error or "") and not spawned.procs


def test_the_standard_fallback_also_runs_in_the_child(child_cfg: Settings, store: Store, flat_pdf: Path, mock: MockServer,
                                                      make_service: Callable[..., IndexService], spawned: SpawnRecorder) -> None:
    _, doc, watcher = run_job(make_service(settings=child_cfg.with_(index_fallback_standard=True)), store, child_cfg, flat_pdf, timeout=180)
    assert len(spawned.procs) == 2, "flash refused the flat PDF, then a second child ran standard mode"
    assert any(d == indexer.NO_OUTLINE_NOTE for _, _, _, d in watcher.samples)
    assert doc.status in ("ready", "failed")


def test_cancelling_kills_the_child_and_removes_the_store(child_cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                          make_service: Callable[..., IndexService], spawned: SpawnRecorder) -> None:
    mock.delay_ms = 400
    svc = make_service(settings=child_cfg)
    sid, dest = new_session(store, child_cfg, small_pdf)
    svc.start(sid, dest)
    deadline = time.monotonic() + 60
    while not mock.requests and time.monotonic() < deadline:
        time.sleep(0.05)
    assert mock.requests, "the child never reached the model"
    svc.cancel(sid)
    doc = wait_for(store, sid, timeout=30)
    assert (doc.status, doc.error) == ("failed", CANCELLED_ERROR)
    assert spawned.procs[0].poll() is not None, "the child must be gone"
    assert not (child_cfg.session_dir(sid) / "pageindex").exists()
    assert not mock.requests_of("index_description")


def _fake_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    (tmp_path / "rl_fake_worker.py").write_text(body, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in (str(tmp_path), os.environ.get("PYTHONPATH", "")) if p))
    monkeypatch.setattr(indexer, "WORKER_MODULE", "rl_fake_worker")


def test_a_child_killed_by_the_kernel_is_reported_as_out_of_memory(child_cfg: Settings, store: Store, small_pdf: Path, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch, make_service: Callable[..., IndexService]) -> None:
    _fake_worker(tmp_path, monkeypatch, "import os, sys\nsys.stdin.readline()\nos._exit(137)\n")
    _, doc, _ = run_job(make_service(settings=child_cfg), store, child_cfg, small_pdf, timeout=60)
    assert doc.status == "failed" and "ran out of memory" in (doc.error or "")


def test_a_child_that_just_stops_gives_a_retryable_message(child_cfg: Settings, store: Store, small_pdf: Path, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch, make_service: Callable[..., IndexService]) -> None:
    _fake_worker(tmp_path, monkeypatch, "import sys\nsys.stdin.readline()\nprint('not json')\nsys.exit(1)\n")
    _, doc, _ = run_job(make_service(settings=child_cfg), store, child_cfg, small_pdf, timeout=60)
    assert doc.status == "failed" and "stopped unexpectedly" in (doc.error or "") and "exit code 1" in (doc.error or "")


def test_a_hung_child_is_killed_when_the_job_times_out(child_cfg: Settings, store: Store, small_pdf: Path, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch, make_service: Callable[..., IndexService],
                                                      spawned: SpawnRecorder) -> None:
    _fake_worker(tmp_path, monkeypatch, "import sys, time\nsys.stdin.readline()\ntime.sleep(300)\n")
    _, doc, _ = run_job(make_service(settings=child_cfg, job_timeout_s=2.0), store, child_cfg, small_pdf, timeout=60)
    assert doc.status == "failed" and "longer than" in (doc.error or "")
    deadline = time.monotonic() + 15
    while spawned.procs[0].poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert spawned.procs[0].poll() is not None, "a timed-out job must not leave its child running"


def test_a_custom_client_factory_keeps_indexing_in_process(child_cfg: Settings, store: Store, small_pdf: Path, spawned: SpawnRecorder,
                                                          make_service: Callable[..., IndexService]) -> None:
    from tests.test_indexer import FakeFactory, ok

    shed: list[bool] = []
    svc = make_service(FakeFactory(ok), settings=child_cfg)
    svc.set_heavy_job_hook(lambda: shed.append(True))
    _, doc, _ = run_job(svc, store, child_cfg, small_pdf)
    assert doc.status == "ready" and not spawned.procs
    assert not shed, "no child, so nothing to make room for"


def test_the_web_process_sheds_memory_just_before_each_indexing_child(child_cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                                      make_service: Callable[..., IndexService], spawned: SpawnRecorder) -> None:
    calls: list[int] = []

    def shed() -> None:
        calls.append(len(spawned.procs))
        raise RuntimeError("a failing memory saver must not stop the job")

    svc = make_service(settings=child_cfg)
    svc.set_heavy_job_hook(shed)
    _, doc, _ = run_job(svc, store, child_cfg, small_pdf, timeout=120)
    assert doc.status == "ready", doc.error
    assert calls == [0] and len(spawned.procs) == 1, "called once, before the child was started"


def test_the_childs_verdict_survives_translate_error() -> None:
    fault = Fault("quota", "out of credit")
    wrapped = RuntimeError("Failed to submit document")
    wrapped.__cause__ = RemoteFault(fault)
    assert translate_error(RemoteFault(fault), Settings()) == fault
    assert translate_error(wrapped, Settings()) == fault
