"""Scoring in a short-lived child process (Settings.eval_in_subprocess; on with LOW_MEMORY) and the one-heavy-job-at-a-time rule.

Real RAGAS in the real child against the fake OpenAI server for the happy path; a stand-in worker module for the failure modes."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from devtools.mock_openai import MockServer, start_mock_server
from reportlens import eval_child, evaluation, lowmem
from reportlens.config import Settings, load_settings
from reportlens.eval_child import ChildEvaluator
from reportlens.models import ContextPage, EvalScores

ANSWER = "The group reported revenue of 4.2 billion pounds [[c1]]."
CONTEXTS = [ContextPage(page=3, text="Revenue for the year was 4.2 billion pounds, up from 3.9 billion in the prior year."),
            ContextPage(page=4, text="Operating profit rose 8 percent to 1.1 billion pounds.")]


@pytest.fixture
def mock() -> Iterator[MockServer]:
    server = start_mock_server()
    yield server
    server.stop()


@pytest.fixture
def cfg(tmp_path: Path, mock: MockServer) -> Settings:
    return load_settings(environ={}).with_(data_dir=tmp_path, openai_api_key="sk-test-evalchild", openai_base_url=mock.base_url,
                                           eval_in_subprocess=True)


def fake_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    (tmp_path / "rl_fake_eval_worker.py").write_text(body, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in (str(tmp_path), os.environ.get("PYTHONPATH", "")) if p))
    monkeypatch.setattr(eval_child, "WORKER_MODULE", "rl_fake_eval_worker")


def test_metric_names_match_the_evaluator() -> None:
    assert eval_child.METRICS == evaluation.METRICS


def test_the_real_child_scores_an_answer_and_reports_each_metric(cfg: Settings, mock: MockServer, caplog: pytest.LogCaptureFixture) -> None:
    seen: list[tuple[str, object, object]] = []

    async def go() -> EvalScores:
        return await ChildEvaluator(cfg).evaluate("What was revenue?", ANSWER, CONTEXTS, on_metric=lambda m, v, e: seen.append((m, v, e)))

    with caplog.at_level("INFO", logger="reportlens.eval_child"):
        scores = asyncio.run(go())
    assert scores.status in ("done", "partial"), scores
    assert sorted(m for m, _, _ in seen) == sorted(eval_child.METRICS)
    assert scores.n_contexts_scored == 2 and scores.ragas_version
    assert any("scoring child finished" in r.getMessage() and "ragas" in r.getMessage() for r in caplog.records), "the child's memory line is logged"
    assert mock.requests, "the child talked to the fake model"
    assert not lowmem.HEAVY_JOB_LOCK.locked()


def test_the_real_child_skips_without_contexts(cfg: Settings) -> None:
    scores = asyncio.run(ChildEvaluator(cfg).evaluate("q", ANSWER, []))
    assert (scores.status, scores.skipped_reason) == ("skipped", "no_contexts")


def test_the_childs_death_by_the_kernel_is_an_honest_failure(cfg: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_worker(tmp_path, monkeypatch, "import os, sys\nsys.stdin.readline()\nos._exit(137)\n")
    scores = asyncio.run(ChildEvaluator(cfg).evaluate("q", ANSWER, CONTEXTS))
    assert scores.status == "failed" and set(scores.errors) == set(eval_child.METRICS) and "ran out of memory" in scores.errors["faithfulness"]


def test_a_child_that_stops_without_a_result_says_so(cfg: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_worker(tmp_path, monkeypatch, "import sys\nsys.stdin.readline()\nprint('noise')\nsys.exit(3)\n")
    scores = asyncio.run(ChildEvaluator(cfg).evaluate("q", ANSWER, CONTEXTS))
    assert scores.status == "failed" and "exit code 3" in scores.errors["answer_relevancy"]


def test_a_hung_child_is_killed_after_the_timeout(cfg: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_worker(tmp_path, monkeypatch, "import sys, time\nsys.stdin.readline()\ntime.sleep(300)\n")
    monkeypatch.setattr(eval_child, "CHILD_TIMEOUT_S", 1.5)
    started = time.monotonic()
    scores = asyncio.run(ChildEvaluator(cfg).evaluate("q", ANSWER, CONTEXTS))
    assert scores.status == "failed" and "too long" in scores.errors["faithfulness"] and time.monotonic() - started < 30
    assert not lowmem.HEAVY_JOB_LOCK.locked()


def test_cancelling_kills_the_child_and_releases_the_gate(cfg: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_worker(tmp_path, monkeypatch, "import sys, time\nsys.stdin.readline()\nprint('{\"ev\": \"metric\", \"metric\": \"faithfulness\", \"value\": 1, \"error\": null}', flush=True)\ntime.sleep(300)\n")
    spawned: list[subprocess.Popen] = []
    real = subprocess.Popen
    monkeypatch.setattr(eval_child.subprocess, "Popen", lambda *a, **k: spawned.append(real(*a, **k)) or spawned[-1])

    async def go() -> None:
        got = asyncio.Event()

        async def on_metric(*_a):
            got.set()

        task = asyncio.create_task(ChildEvaluator(cfg).evaluate("q", ANSWER, CONTEXTS, on_metric=on_metric))
        await asyncio.wait_for(got.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert spawned[0].poll() is not None, "the child must be gone"
    assert not lowmem.HEAVY_JOB_LOCK.locked()


def test_a_broken_metric_listener_never_breaks_scoring(cfg: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = ('{"ev": "result", "scores": {"status": "done", "faithfulness": 1.0, "answer_relevancy": 0.5, "context_precision": 0.25}, "mem": "rss=1"}')
    fake_worker(tmp_path, monkeypatch, "import sys\nsys.stdin.readline()\nprint('{\"ev\": \"metric\", \"metric\": \"faithfulness\", \"value\": 1.0, \"error\": null}', flush=True)\n"
                                       f"print({result!r}, flush=True)\n")

    def boom(*_a):
        raise RuntimeError("listener bug")

    scores = asyncio.run(ChildEvaluator(cfg).evaluate("q", ANSWER, CONTEXTS, on_metric=boom))
    assert (scores.status, scores.faithfulness, scores.context_precision) == ("done", 1.0, 0.25)


def test_only_one_heavy_child_runs_at_a_time(cfg: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scoring waits while the gate is held (an indexing child is running) and starts the moment it is free."""
    result = '{"ev": "result", "scores": {"status": "done", "faithfulness": 1.0, "answer_relevancy": 1.0, "context_precision": 1.0}}'
    fake_worker(tmp_path, monkeypatch, f"import sys\nsys.stdin.readline()\nprint({result!r}, flush=True)\n")
    monkeypatch.setattr(eval_child, "GATE_POLL_S", 0.05)
    assert lowmem.HEAVY_JOB_LOCK.acquire(timeout=1)
    timeline: list[str] = []

    async def go() -> EvalScores:
        task = asyncio.create_task(ChildEvaluator(cfg).evaluate("q", ANSWER, CONTEXTS))
        await asyncio.sleep(0.6)
        timeline.append("done early" if task.done() else "still waiting")
        lowmem.HEAVY_JOB_LOCK.release()
        return await asyncio.wait_for(task, 30)

    scores = asyncio.run(go())
    assert timeline == ["still waiting"] and scores.status == "done"


def test_the_service_uses_the_child_evaluator_only_when_asked(settings: Settings, tmp_path: Path) -> None:
    from reportlens.service import ReportLensService
    from reportlens.store import Store
    from tests.test_service import FakeIndexer

    store = Store(settings.db_path)
    try:
        small = ReportLensService(settings.with_(eval_in_subprocess=True), store=store, indexer=FakeIndexer(store, settings), qa=object())
        assert isinstance(small._get_evaluator(), ChildEvaluator)
    finally:
        store.close()


def test_the_setting_follows_low_memory_unless_overridden() -> None:
    assert load_settings(environ={"LOW_MEMORY": "1"}).eval_in_subprocess
    assert not load_settings(environ={}).eval_in_subprocess
    assert not load_settings(environ={"LOW_MEMORY": "1", "EVAL_IN_SUBPROCESS": "0"}).eval_in_subprocess
    assert load_settings(environ={"EVAL_IN_SUBPROCESS": "1"}).eval_in_subprocess


def test_the_indexing_job_waits_for_a_running_scoring_child_and_cancels_while_waiting(tmp_path: Path) -> None:
    """The indexer takes the same gate: held by a scoring child, a cancelled job stops waiting instead of hanging."""
    from reportlens import indexer

    class Job:
        cancelled = threading.Event()

        def check_cancelled(self) -> None:
            if self.cancelled.is_set():
                raise indexer.JobCancelled("cancelled")

        def report(self, *a, **k) -> None:
            pass

    svc = indexer.IndexService.__new__(indexer.IndexService)
    job = Job()
    assert lowmem.HEAVY_JOB_LOCK.acquire(timeout=1)
    threading.Timer(0.6, job.cancelled.set).start()
    try:
        with pytest.raises(indexer.JobCancelled):
            svc._index_in_child(job, load_settings(environ={}), "flash")      # type: ignore[arg-type]
    finally:
        lowmem.HEAVY_JOB_LOCK.release()
    assert not lowmem.HEAVY_JOB_LOCK.locked()


def test_both_workers_ask_the_kernel_to_kill_them_before_the_web_server() -> None:
    from reportlens import indexing_worker

    assert indexing_worker.prefer_to_be_killed_first() is None                 # a no-op where /proc/self/oom_score_adj does not exist
    source = Path(indexing_worker.__file__).read_text(encoding="utf-8") + Path(eval_child.__file__).with_name("evaluation_worker.py").read_text(encoding="utf-8")
    assert source.count("prefer_to_be_killed_first()") >= 3                    # defined once, called by both mains


def test_the_parent_watch_starts_a_thread_on_linux_and_none_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from reportlens import indexing_worker

    started: list[str] = []

    class FakeThread:
        def __init__(self, target, name, daemon):
            self.target = target
            started.append(name)

        def start(self) -> None:
            started.append("started:" + self.target.__name__)

    monkeypatch.setattr(indexing_worker.threading, "Thread", FakeThread)
    monkeypatch.setattr(indexing_worker.os, "name", "posix")
    indexing_worker.start_parent_watch()
    monkeypatch.setattr(indexing_worker.os, "name", "nt")
    indexing_worker.start_parent_watch()
    assert started == ["parent-watch", "started:_exit_when_parent_goes"]
