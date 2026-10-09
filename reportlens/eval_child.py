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
KEEP_WARM_S = 45.0                                  # a child kept for the next answer of a set waits this long for it (importing RAGAS is ~45 s of CPU at 0.1 CPU)
CHILD_TIMEOUT_S = 15 * 60.0                         # three metrics take under a minute even at 0.1 CPU once RAGAS is loaded; this only catches a hang

OnMetric = Callable[[str, Optional[float], Optional[str]], Optional[Awaitable[None]]]


def _failed(message: str, **extra: Any) -> EvalScores:
    return EvalScores(status="failed", errors={m: message for m in METRICS}, **extra)


class _Child:
    """A scoring child and the queue its output lines arrive on."""

    def __init__(self, proc: subprocess.Popen, lines: "asyncio.Queue[Optional[str]]"):
        self.proc = proc
        self.lines = lines


class ChildEvaluator:
    """`keep_warm` (true, or a function asked once the scoring is done: a set of questions is being answered and more answers are coming) leaves the child alive, with RAGAS imported, for
    up to `KEEP_WARM_S` after a scoring so the next one skips the import.  The heavy-job gate stays held meanwhile (an indexing child
    waits that long at most); a child that died, or any failure, ends the warm period."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self._closed = False
        self._warm: Optional[_Child] = None
        self._warm_timer: Optional[asyncio.TimerHandle] = None

    async def aclose(self) -> None:
        self._closed = True
        warm, self._warm = self._warm, None
        if self._warm_timer is not None:
            self._warm_timer.cancel()
            self._warm_timer = None
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            _kill(proc)
        if warm is not None:                                  # parked: nobody is waiting on it, so release the gate here
            await asyncio.shield(asyncio.to_thread(_stop, warm.proc))
            with self._lock:
                self._procs.discard(warm.proc)
            HEAVY_JOB_LOCK.release()

    @staticmethod
    def skipped(reason: str) -> EvalScores:
        return EvalScores(status="skipped", skipped_reason=reason)

    # ------------------------------------------------------------------------------------------- public API
    async def evaluate(self, question: str, answer: str, contexts: list[ContextPage], on_metric: Optional[OnMetric] = None,
                       *, keep_warm: "bool | Callable[[], bool]" = False) -> EvalScores:
        try:
            return await self._evaluate(question, answer, contexts, on_metric, keep_warm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
            log.exception("scoring in a child process crashed")
            return _failed(f"Scoring failed unexpectedly ({type(exc).__name__}).")

    # ------------------------------------------------------------------------------------------- the child
    def _spawn(self) -> _Child:
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
        loop = asyncio.get_running_loop()
        lines: "asyncio.Queue[Optional[str]]" = asyncio.Queue()

        def pump() -> None:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    loop.call_soon_threadsafe(lines.put_nowait, line)
            finally:
                loop.call_soon_threadsafe(lines.put_nowait, None)

        threading.Thread(target=pump, name="eval-child-reader", daemon=True).start()
        return _Child(proc, lines)

    async def _take_warm(self) -> Optional[_Child]:
        """The parked child, if there is one and it is still alive (the gate is then already ours)."""
        child, self._warm = self._warm, None
        if self._warm_timer is not None:
            self._warm_timer.cancel()
            self._warm_timer = None
        if child is not None and child.proc.poll() is not None:
            await self._release(child)                    # it died while parked
            return None
        return child

    def _park(self, child: _Child) -> None:
        self._warm = child
        loop = asyncio.get_running_loop()
        self._warm_timer = loop.call_later(KEEP_WARM_S, lambda: asyncio.ensure_future(self._expire(child)))

    async def _expire(self, child: _Child) -> None:
        if self._warm is child:
            self._warm, self._warm_timer = None, None
            await self._release(child)

    async def _release(self, child: _Child) -> None:
        """End a child and give the gate back (the caller holds the gate)."""
        try:
            await asyncio.shield(asyncio.to_thread(_stop, child.proc))
        finally:
            with self._lock:
                self._procs.discard(child.proc)
            HEAVY_JOB_LOCK.release()

    async def _evaluate(self, question: str, answer: str, contexts: list[ContextPage], on_metric: Optional[OnMetric],
                        keep_warm: "bool | Callable[[], bool]") -> EvalScores:
        child = await self._take_warm()
        if child is None:
            while not HEAVY_JOB_LOCK.acquire(blocking=False):          # polled, so a cancelled task never leaves the lock held
                await asyncio.sleep(GATE_POLL_S)
        reusable = False
        try:
            if self._closed:
                raise asyncio.CancelledError()
            if child is None:
                child = self._spawn()
            request = {"settings": settings_to_json(self._settings), "question": question, "answer": answer,
                       "contexts": [c.model_dump(mode="json") for c in contexts]}
            await asyncio.to_thread(self._send, child.proc, request)
            scores, got_result = await asyncio.wait_for(self._read(child.lines, child.proc, on_metric), timeout=CHILD_TIMEOUT_S)
            reusable = got_result and not self._closed and child.proc.poll() is None and bool(keep_warm() if callable(keep_warm) else keep_warm)
            return scores
        except asyncio.TimeoutError:
            log.error("the scoring child did not finish within %.0f s; killed", CHILD_TIMEOUT_S)
            return _failed("Scoring took too long and was stopped. Run it again.")
        finally:
            if child is not None and reusable:
                self._park(child)
            elif child is not None:
                await self._release(child)
            else:
                HEAVY_JOB_LOCK.release()

    @staticmethod
    def _send(proc: subprocess.Popen, request: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(request) + chr(10))
        proc.stdin.flush()

    async def _read(self, lines: "asyncio.Queue[Optional[str]]", proc: subprocess.Popen,
                    on_metric: Optional[OnMetric]) -> tuple[EvalScores, bool]:
        """(scores, whether the child itself delivered them).  Returns at the child's `result` line: a child kept for the next
        answer does not end, and one that is not simply has its pipe closed by the caller."""
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
                log.info("scoring child finished (%s)", event.get("mem") or "no memory figures")
                return EvalScores.model_validate(event["scores"]), True
        code = await asyncio.to_thread(proc.wait)
        if code < 0 or code == 137:
            return _failed("The server ran out of memory while scoring this answer. Run the scoring again in a moment."), False
        return _failed(f"Scoring stopped unexpectedly (exit code {code}). Run it again."), False


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
