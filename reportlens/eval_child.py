"""`ChildEvaluator`: the `Evaluator` interface, answered by `python -m reportlens.evaluation_worker` (one short-lived process per answer).

Used on small hosts (`Settings.eval_in_subprocess`, on with LOW_MEMORY) so the web process never imports RAGAS.  Only one heavy child
(indexing or scoring) runs at a time (`lowmem.HEAVY_JOB_LOCK`): a 300-page index and a scoring run together would not fit 512 MB.  A
scoring run that has to wait simply starts later; the answer itself was already delivered and stored.
Like `Evaluator.evaluate`, this never raises (other than cancellation): problems land in `EvalScores.errors`.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import subprocess
import sys
import threading
from typing import Any, Awaitable, Callable, Optional

from .config import PROJECT_ROOT, Settings, settings_to_json
from .lowmem import HEAVY_JOB_LOCK
from .models import ContextPage, EvalScores

log = logging.getLogger("reportlens.eval_child")

WORKER_MODULE = "reportlens.evaluation_worker"       # tests swap it
METRICS = ("faithfulness", "answer_relevancy", "context_precision")      # == evaluation.METRICS (asserted in the tests)
GATE_POLL_S = 0.5
CHILD_TIMEOUT_S = 15 * 60.0                         # three metrics take under a minute even at 0.1 CPU once RAGAS is loaded; this only catches a hang

OnMetric = Callable[[str, Optional[float], Optional[str]], Optional[Awaitable[None]]]


def _failed(message: str, **extra: Any) -> EvalScores:
    return EvalScores(status="failed", errors={m: message for m in METRICS}, **extra)


class ChildEvaluator:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            _kill(proc)

    @staticmethod
    def skipped(reason: str) -> EvalScores:
        return EvalScores(status="skipped", skipped_reason=reason)

    # ------------------------------------------------------------------------------------------- public API
    async def evaluate(self, question: str, answer: str, contexts: list[ContextPage], on_metric: Optional[OnMetric] = None) -> EvalScores:
        try:
            return await self._evaluate(question, answer, contexts, on_metric)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
            log.exception("scoring in a child process crashed")
            return _failed(f"Scoring failed unexpectedly ({type(exc).__name__}).")

    async def _evaluate(self, question: str, answer: str, contexts: list[ContextPage], on_metric: Optional[OnMetric]) -> EvalScores:
        while not HEAVY_JOB_LOCK.acquire(blocking=False):          # polled, so a cancelled task never leaves the lock held
            await asyncio.sleep(GATE_POLL_S)
        proc: Optional[subprocess.Popen] = None
        try:
            if self._closed:
                raise asyncio.CancelledError()
            env = dict(os.environ)
            if self._settings.key_source == "visitor":    # the visitor's key travels in the request; the owner's stays out of reach
                for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "LLM_API_KEY", "LLM_BASE_URL"):
                    env.pop(name, None)
            env["PYTHONPATH"] = os.pathsep.join(p for p in (str(PROJECT_ROOT), env.get("PYTHONPATH")) if p)
            env.setdefault("MALLOC_ARENA_MAX", "2")
            env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            proc = subprocess.Popen([sys.executable, "-m", WORKER_MODULE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                    encoding="utf-8", bufsize=1, env=env, cwd=str(PROJECT_ROOT))
            with self._lock:
                self._procs.add(proc)
            request = {"settings": settings_to_json(self._settings), "question": question, "answer": answer,
                       "contexts": [c.model_dump(mode="json") for c in contexts]}
            loop = asyncio.get_running_loop()
            lines: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
            child = proc

            def pump() -> None:
                assert child.stdout is not None
                try:
                    for line in child.stdout:
                        loop.call_soon_threadsafe(lines.put_nowait, line)
                finally:
                    loop.call_soon_threadsafe(lines.put_nowait, None)

            threading.Thread(target=pump, name="eval-child-reader", daemon=True).start()
            await asyncio.to_thread(self._send, child, request)
            return await asyncio.wait_for(self._read(lines, child, on_metric), timeout=CHILD_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.error("the scoring child did not finish within %.0f s; killed", CHILD_TIMEOUT_S)
            return _failed("Scoring took too long and was stopped. Run it again.")
        finally:
            if proc is not None:
                await asyncio.shield(asyncio.to_thread(_stop, proc))
                with self._lock:
                    self._procs.discard(proc)
            HEAVY_JOB_LOCK.release()

    @staticmethod
    def _send(proc: subprocess.Popen, request: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(request) + chr(10))
        proc.stdin.flush()

    async def _read(self, lines: "asyncio.Queue[Optional[str]]", proc: subprocess.Popen, on_metric: Optional[OnMetric]) -> EvalScores:
        result: Optional[EvalScores] = None
        while True:
            line = await lines.get()
            if line is None:
                break
            try:
                event = json.loads(line)
            except ValueError:
                log.warning("scoring child printed a non-protocol line: %.120r", line)
                continue
            kind = event.get("ev")
            if kind == "metric":
                await _notify(on_metric, str(event.get("metric")), event.get("value"), event.get("error"))
            elif kind == "result":
                result = EvalScores.model_validate(event["scores"])
                log.info("scoring child finished (%s)", event.get("mem") or "no memory figures")
        if result is not None:
            return result
        code = await asyncio.to_thread(proc.wait)
        if code < 0 or code == 137:
            return _failed("The server ran out of memory while scoring this answer. Run the scoring again in a moment.")
        return _failed(f"Scoring stopped unexpectedly (exit code {code}). Run it again.")


async def _notify(on_metric: Optional[OnMetric], name: str, value: Optional[float], error: Optional[str]) -> None:
    if on_metric is None:
        return
    try:
        pending = on_metric(name, value, error)
        if inspect.isawaitable(pending):
            await pending
    except Exception:  # noqa: BLE001 - a broken listener must never break scoring
        log.exception("on_metric callback failed for %s", name)


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass


def _stop(proc: subprocess.Popen) -> None:
    """A finished child exits by itself once its stdin closes; a running one (cancelled, timed out) is killed."""
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
