"""RAGAS scoring of one answer: faithfulness, answer relevancy and context precision (no ground truth needed).

Productionised from research/ragas_minimal_example.py.  Rules carried over from that verified design:
  * collections API only (`ragas.metrics.collections`); `evaluate()` and the langchain wrappers are deprecated and
    `evaluate()` rejects collections metrics;
  * `RAGAS_DO_NOT_TRACK` must be exactly "true" before the first ragas import, otherwise every judge call does a
    blocking POST to the vendor's telemetry endpoint from inside the event loop;
  * always `await metric.ascore(...)` with ONE AsyncOpenAI client per event loop (a pooled connection from a closed
    loop makes every later call fail with "Connection error");
  * the three metrics run concurrently and fail independently; NaN/inf become None (json.dumps(nan) is invalid JSON).
"""
from __future__ import annotations

import os

os.environ["RAGAS_DO_NOT_TRACK"] = "true"  # exactly "true"; must precede the first ragas import

import asyncio
import inspect
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import openai
from openai import AsyncOpenAI
from ragas import __version__ as RAGAS_VERSION
from ragas.embeddings.base import embedding_factory
from ragas.llms import llm_factory
from ragas.metrics.collections import AnswerRelevancy, ContextPrecisionWithoutReference, Faithfulness

from reportlens.config import Settings
from reportlens.metric_info import METRIC_INFO  # noqa: F401 - re-exported; the wording lives in metric_info.py so /api/config needs no RAGAS import
from reportlens.models import ContextPage, EvalScores

try:  # ragas internals used by the parallel context-precision subclass; guarded, see _build_context_precision
    from ragas.metrics.collections.context_precision.util import ContextPrecisionInput, ContextPrecisionOutput
except ImportError:  # pragma: no cover - only if a future ragas moves them
    ContextPrecisionInput = ContextPrecisionOutput = None  # type: ignore[assignment,misc]

log = logging.getLogger("reportlens.evaluation")

METRICS = ("faithfulness", "answer_relevancy", "context_precision")

OnMetric = Callable[[str, Optional[float], Optional[str]], Optional[Awaitable[None]]]

# One judge call is a few seconds; this only bounds a hung request (client timeout/retries apply per HTTP call).
_METRIC_TIMEOUT_S = 120.0
_CLIENT_TIMEOUT_S = 60.0
_CLIENT_MAX_RETRIES = 2            # the SDK retries 408/409/429/5xx with backoff
# ragas' default temperature (0.01) makes the 3 questions of answer relevancy near-identical; 0.3 diversifies them.
_RELEVANCY_TEMPERATURE = 0.3
# Reasoning tokens count against max_completion_tokens, so a smaller budget truncates the JSON verdicts.
_MIN_REASONING_COMPLETION_TOKENS = 4096
_PLACEHOLDER_KEY = "sk-not-needed"  # AsyncOpenAI insists on a key even for a keyless local gateway (custom base_url)
NO_EMBEDDINGS = "Not available with this model provider (it has no embeddings API, which answer relevancy needs)"
_MAX_REASON_CHARS = 400

# ----------------------------------------------------------------------------------------------- text helpers
_CITATION_RES = (
    re.compile(r"\[\[c\d+\]\]"),                                                          # display markers [[c3]]
    re.compile(r"\[\s*(?:pp?\.?|pages?)\s*\d+(?:\s*[,\-–]\s*\d+)*\s*\]", re.I),          # [p. 45] [pp 3-4] [page 7]
    re.compile(r"\(\s*(?:pp?\.?|pages?)\s*\d+(?:\s*[,\-–]\s*\d+)*\s*\)", re.I),          # (p. 45) (page 7)
    re.compile(r"\[\s*\d{1,3}(?:\s*,\s*\d{1,3})*\s*\]"),                                 # [1] [2, 3]  (not years)
)


def strip_citation_markers(text: str) -> str:
    """Score the prose, not the markers.  `(2024)` and `(2023: 3,900)` are deliberately left alone."""
    for rx in _CITATION_RES:
        text = rx.sub("", text or "")
    text = re.sub(r"[ \t]+([.,;:!?)])", r"\1", text)   # gap a removed marker leaves before punctuation
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    return text.strip()


def prepare_contexts(contexts: list[ContextPage], max_contexts: int, max_chars: int) -> tuple[list[ContextPage], int]:
    """Dedupe by page (first read wins), drop blanks, cap count and length.  Returns (scored, n_distinct_pages_read).

    Ragas never truncates and the context-precision cost is one judge call per context, hence the caps."""
    seen: set[int] = set()
    kept: list[ContextPage] = []
    for ctx in contexts:
        if ctx.page in seen:
            continue
        seen.add(ctx.page)
        text = (ctx.text or "").strip()
        if text and len(kept) < max_contexts:
            kept.append(ContextPage(page=ctx.page, text=text[:max_chars]))
    return kept, len(seen)


def _num(value: Any) -> Optional[float]:
    """MetricResult/float -> rounded value clamped to 0..1; NaN/inf/garbage -> None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(min(1.0, max(0.0, v)), 4)


def _describe_error(exc: BaseException) -> str:
    """One short, key-free line for the UI.  instructor wraps API errors, so walk the cause chain."""
    cur: Optional[BaseException] = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        code = getattr(cur, "status_code", None)
        if isinstance(cur, (asyncio.TimeoutError, TimeoutError)):
            return "Timed out waiting for the judge model"
        if isinstance(cur, openai.AuthenticationError):
            return "OpenAI rejected the API key"
        if isinstance(cur, openai.PermissionDeniedError):
            return "OpenAI denied access to the judge model"
        if isinstance(cur, openai.NotFoundError):
            return "Judge or embedding model not found for this API key"
        if isinstance(cur, openai.RateLimitError):
            return "OpenAI rate limit or quota reached"
        if isinstance(cur, openai.APITimeoutError):
            return "OpenAI request timed out"
        if isinstance(cur, openai.APIConnectionError):
            return "Could not reach the OpenAI API"
        if isinstance(cur, openai.InternalServerError):
            return f"OpenAI server error (HTTP {code})"
        if isinstance(cur, openai.BadRequestError):
            return f"OpenAI rejected the judge request: {str(getattr(cur, 'message', cur))[:160]}"
        if type(cur).__name__ == "IncompleteOutputException":
            return "Judge output was cut off (raise RAGAS_JUDGE_MAX_TOKENS)"
        cur = cur.__cause__ or cur.__context__
    first_line = (str(exc).strip().splitlines() or [""])[0]
    return re.sub(r"(?:sk-|gsk_|xai-|tgp_|AIza)[A-Za-z0-9_\-]{8,}", "sk-***", f"{type(exc).__name__}: {first_line}")[:200]


# ----------------------------------------------------------------------------------------------- judge plumbing
def _is_reasoning_model(model: str) -> bool:
    """o1..o9, gpt-5*, gpt-6*, ... dotted ids included (gpt-5.6-sol, gpt-6.1-sol).  ragas 0.4.3 only recognises
    integer-only versions and would send max_tokens/temperature/top_p to the dotted ones (OpenAI rejects that)."""
    return bool(re.match(r"^(?:o\d+|gpt-(?:[5-9]|1\d))(?:[.\-_]|$)", model.lower()))


def _reasoning_kwargs(kwargs: dict[str, Any], effort: Optional[str]) -> dict[str, Any]:
    """Rewrite one chat.completions.create() call for a reasoning judge.  Temperature/top_p are only legal when
    the effort is "none"; max_tokens is not accepted at all."""
    kw = dict(kwargs)
    budget = [v for v in (kw.pop("max_tokens", None), kw.pop("max_completion_tokens", None)) if v is not None]
    kw["max_completion_tokens"] = max([_MIN_REASONING_COMPLETION_TOKENS, *budget])
    if effort:
        kw["reasoning_effort"] = effort
    if kw.get("reasoning_effort") != "none":
        kw.pop("temperature", None)
        kw.pop("top_p", None)
    return kw


def _install_call_hooks(client: AsyncOpenAI, sem: asyncio.Semaphore, *, reasoning: bool, effort: Optional[str]) -> None:
    """Wrap the client's create() calls BEFORE llm_factory/embedding_factory capture them.  Every judge and embedding
    request then passes the shared concurrency limit (ragas has none), and reasoning judges get legal parameters."""
    chat_create = client.chat.completions.create
    embed_create = client.embeddings.create

    async def chat(*args: Any, **kwargs: Any) -> Any:
        if reasoning:
            kwargs = _reasoning_kwargs(kwargs, effort)
        async with sem:
            return await chat_create(*args, **kwargs)

    async def embed(*args: Any, **kwargs: Any) -> Any:
        async with sem:
            return await embed_create(*args, **kwargs)

    client.chat.completions.create = chat  # type: ignore[method-assign]
    client.embeddings.create = embed  # type: ignore[method-assign]


class ParallelContextPrecision(ContextPrecisionWithoutReference):
    """Same prompt and average-precision maths as ragas' ContextPrecisionWithoutReference, but judges the contexts
    concurrently (the stock class awaits them one by one: N x latency) and also returns each verdict + reason so the
    UI can say which pages were useful.  Relies on ragas 0.4.3 internals: `prompt.to_string`, `llm.agenerate`,
    `_calculate_average_precision` - see `_build_context_precision` for the guard."""

    async def ascore_detailed(self, user_input: str, response: str, contexts: list[str]) -> tuple[float, list[dict[str, Any]]]:
        async def judge(context: str) -> Any:
            data = ContextPrecisionInput(question=user_input, context=context, answer=response)
            return await self.llm.agenerate(self.prompt.to_string(data), ContextPrecisionOutput)

        tasks = [asyncio.ensure_future(judge(c)) for c in contexts]
        try:
            outs = await asyncio.gather(*tasks)
        except BaseException:
            # gather() leaves the siblings running when one fails or the caller times out; stop the spend.
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        verdicts = [1 if o.verdict else 0 for o in outs]
        rows = [{"verdict": v, "reason": str(o.reason or "")[:_MAX_REASON_CHARS]} for v, o in zip(verdicts, outs)]
        return float(self._calculate_average_precision(verdicts)), rows


def _build_context_precision(llm: Any) -> ContextPrecisionWithoutReference:
    """The parallel subclass if ragas still looks like 0.4.3 inside, else the stock (sequential) class."""
    try:
        if ContextPrecisionInput is None or ContextPrecisionOutput is None:
            raise RuntimeError("context_precision.util models are missing")
        metric = ParallelContextPrecision(llm=llm, name="context_precision")
        probe = metric.prompt.to_string(ContextPrecisionInput(question="q", context="c", answer="a"))
        if not isinstance(probe, str) or not callable(metric._calculate_average_precision) or not hasattr(llm, "agenerate"):
            raise RuntimeError("unexpected ragas internals")
        return metric
    except Exception as exc:  # noqa: BLE001 - any mismatch means "internals changed": degrade, do not fail
        log.warning("ragas internals differ from 0.4.3 (%s); context precision falls back to the sequential stock "
                    "metric without per-page verdicts", exc)
        return ContextPrecisionWithoutReference(llm=llm, name="context_precision")


@dataclass
class _Runtime:
    """Everything bound to one event loop."""
    loop: asyncio.AbstractEventLoop
    client: AsyncOpenAI
    faithfulness: Faithfulness
    answer_relevancy: Optional[AnswerRelevancy]          # None: no embeddings model (a provider without an embeddings API)
    context_precision: ContextPrecisionWithoutReference


# ----------------------------------------------------------------------------------------------- the evaluator
class Evaluator:
    """Scores answers with RAGAS.  Safe to share between requests on one event loop; `evaluate` never raises (other
    than task cancellation) - problems land in `EvalScores.errors`.

    `client_timeout_s`, `client_max_retries` and `metric_timeout_s` are keyword-only knobs for tests; the service
    uses the defaults.  Concurrency (`settings.eval_concurrency`) limits in-flight OpenAI requests per event loop,
    which is process-wide in practice because the service runs one loop."""

    def __init__(self, settings: Settings, *, client_timeout_s: float = _CLIENT_TIMEOUT_S,
                 client_max_retries: int = _CLIENT_MAX_RETRIES, metric_timeout_s: float = _METRIC_TIMEOUT_S):
        self._settings = settings
        self._client_timeout_s = client_timeout_s
        self._client_max_retries = client_max_retries
        self._metric_timeout_s = metric_timeout_s
        self._runtime: Optional[_Runtime] = None   # built lazily inside the running loop; no network in __init__

    @staticmethod
    def skipped(reason: str) -> EvalScores:
        return EvalScores(status="skipped", skipped_reason=reason)

    async def aclose(self) -> None:
        """Close the HTTP client.  Idempotent; a client whose loop is gone (or is another running loop) is only dropped."""
        runtime, self._runtime = self._runtime, None
        if runtime is None:
            return
        if runtime.loop is asyncio.get_running_loop():
            await runtime.client.close()
        else:
            log.debug("dropping RAGAS client that belongs to another event loop")

    # ------------------------------------------------------------------------------------------- public API
    async def evaluate(self, question: str, answer: str, contexts: list[ContextPage],
                       on_metric: Optional[OnMetric] = None) -> EvalScores:
        """`answer` is the display text, `[[cN]]` markers included; they are stripped before judging.
        `on_metric(metric, value, error)` (sync or async) fires as EACH metric finishes; its failures are logged only."""
        try:
            return await self._evaluate(question, answer, contexts, on_metric)
        except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
            log.exception("RAGAS evaluation crashed")
            message = _describe_error(exc)
            return EvalScores(status="failed", errors={m: message for m in METRICS})

    # ------------------------------------------------------------------------------------------- internals
    async def _evaluate(self, question: str, answer: str, contexts: list[ContextPage],
                        on_metric: Optional[OnMetric]) -> EvalScores:
        s = self._settings
        scored, n_read = prepare_contexts(contexts, s.eval_max_contexts, s.eval_max_chars_per_context)
        response = strip_citation_markers(answer)

        def skip(reason: str) -> EvalScores:
            return self.skipped(reason).model_copy(update={"n_contexts_input": n_read})

        if not s.eval_enabled:
            return skip("disabled")
        if not s.openai_api_key and not s.openai_base_url:
            return skip("no_api_key")
        if not response:
            return skip("empty_answer")
        if not (question or "").strip():
            return skip("empty_question")
        if not scored:
            return skip("no_contexts")

        t0 = time.perf_counter()
        try:
            runtime = self._runtime_for(asyncio.get_running_loop())
        except Exception as exc:  # noqa: BLE001 - e.g. an unusable model/client configuration
            log.exception("could not build the RAGAS runtime")
            message = _describe_error(exc)
            for metric in METRICS:
                await self._notify(on_metric, metric, None, message)
            return EvalScores(status="failed", errors={m: message for m in METRICS}, n_contexts_input=n_read,
                              judge_model=s.judge_model, embedding_model=s.embedding_model,
                              ragas_version=RAGAS_VERSION, latency_s=round(time.perf_counter() - t0, 2))

        texts = [c.text for c in scored]
        verdict_rows: list[dict[str, Any]] = []

        async def faithfulness() -> Any:
            result = await runtime.faithfulness.ascore(user_input=question, response=response, retrieved_contexts=texts)
            return result.value

        async def answer_relevancy() -> Any:
            result = await runtime.answer_relevancy.ascore(user_input=question, response=response)
            return result.value

        async def context_precision() -> Any:
            metric = runtime.context_precision
            if isinstance(metric, ParallelContextPrecision):
                value, rows = await metric.ascore_detailed(question, response, texts)
                verdict_rows.extend({"index": i, "page": scored[i].page, **row} for i, row in enumerate(rows))
                return value
            return (await metric.ascore(user_input=question, response=response, retrieved_contexts=texts)).value

        relevancy_possible = runtime.answer_relevancy is not None
        outcomes = dict(zip(METRICS, await asyncio.gather(
            self._run_metric("faithfulness", faithfulness, on_metric),
            self._run_metric("answer_relevancy", answer_relevancy, on_metric) if relevancy_possible
            else self._unavailable("answer_relevancy", NO_EMBEDDINGS, on_metric),
            self._run_metric("context_precision", context_precision, on_metric),
        )))

        values = {m: v for m, (v, _) in outcomes.items()}
        errors = {m: e for m, (_, e) in outcomes.items() if e}
        n_ok = sum(v is not None for v in values.values())
        expected = len(METRICS) if relevancy_possible else len(METRICS) - 1
        scores = EvalScores(
            status="done" if n_ok == expected else "partial" if n_ok else "failed",
            **values,
            errors=errors,
            context_verdicts=sorted(verdict_rows, key=lambda r: r["index"]),
            n_contexts_input=n_read,
            n_contexts_scored=len(scored),
            judge_model=s.judge_model,
            embedding_model=s.embedding_model,
            ragas_version=RAGAS_VERSION,
            latency_s=round(time.perf_counter() - t0, 2),
        )
        log.info("ragas %s in %.1fs (judge=%s, contexts=%d/%d) %s", scores.status, scores.latency_s, s.judge_model,
                 len(scored), n_read, errors or "")
        return scores

    async def _run_metric(self, name: str, compute: Callable[[], Awaitable[Any]],
                          on_metric: Optional[OnMetric]) -> tuple[Optional[float], Optional[str]]:
        """Run one metric under the timeout, turn the outcome into (value, error) and tell the callback."""
        value: Optional[float] = None
        error: Optional[str] = None
        try:
            value = _num(await asyncio.wait_for(compute(), timeout=self._metric_timeout_s))
            if value is None:
                error = "No usable score for this answer (the judge found nothing to score)"
        except Exception as exc:  # noqa: BLE001 - one metric failing must not lose the others
            error = _describe_error(exc)
            log.warning("ragas metric %s failed: %s", name, error, exc_info=log.isEnabledFor(logging.DEBUG))
        await self._notify(on_metric, name, value, error)
        return value, error

    async def _unavailable(self, name: str, reason: str, on_metric: Optional[OnMetric]) -> tuple[Optional[float], Optional[str]]:
        """A metric this configuration cannot compute at all (not a failure: nothing to retry)."""
        await self._notify(on_metric, name, None, reason)
        return None, reason

    @staticmethod
    async def _notify(on_metric: Optional[OnMetric], name: str, value: Optional[float], error: Optional[str]) -> None:
        if on_metric is None:
            return
        try:
            pending = on_metric(name, value, error)
            if inspect.isawaitable(pending):
                await pending
        except Exception:  # noqa: BLE001 - a broken listener must never break scoring
            log.exception("on_metric callback failed for %s", name)

    def _runtime_for(self, loop: asyncio.AbstractEventLoop) -> _Runtime:
        """The client, its semaphore and the metrics must live and die with one event loop (see module docstring)."""
        runtime = self._runtime
        if runtime is not None and runtime.loop is loop:
            return runtime
        if runtime is not None:
            # An old loop's connections are unusable from here and closing them from this loop would fail; drop them.
            log.info("evaluate() called from a different event loop; rebuilding the RAGAS client")
        self._runtime = self._build_runtime(loop)
        return self._runtime

    def _build_runtime(self, loop: asyncio.AbstractEventLoop) -> _Runtime:
        s = self._settings
        client = AsyncOpenAI(
            api_key=s.openai_api_key or _PLACEHOLDER_KEY,
            base_url=s.openai_base_url,
            timeout=self._client_timeout_s,
            max_retries=self._client_max_retries,
        )
        reasoning = _is_reasoning_model(s.judge_model)
        _install_call_hooks(client, asyncio.Semaphore(max(1, s.eval_concurrency)), reasoning=reasoning,
                            effort=s.judge_reasoning_effort)
        llm = llm_factory(s.judge_model, client=client, max_tokens=s.judge_max_tokens)
        # Own LLM object so only question generation gets a non-greedy temperature (reasoning judges ignore it).
        llm_questions = llm if reasoning else llm_factory(
            s.judge_model, client=client, max_tokens=s.judge_max_tokens, temperature=_RELEVANCY_TEMPERATURE, top_p=1.0)
        relevancy = None
        if s.embedding_model:
            embeddings = embedding_factory("openai", model=s.embedding_model, client=client)
            relevancy = AnswerRelevancy(llm=llm_questions, embeddings=embeddings)
        return _Runtime(
            loop=loop,
            client=client,
            faithfulness=Faithfulness(llm=llm),
            answer_relevancy=relevancy,
            context_precision=_build_context_precision(llm),
        )
