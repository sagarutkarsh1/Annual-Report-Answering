"""
ragas_minimal_example.py  -  score ONE (question, answer, retrieved contexts) triple with RAGAS 0.4.3
using an OpenAI judge.  No ground-truth reference is needed.

Scores returned (all 0..1, higher = better), each is None if the metric failed / is undefined:
  faithfulness       ragas.metrics.collections.Faithfulness                       (2 LLM calls)
  answer_relevancy   ragas.metrics.collections.AnswerRelevancy  ("ResponseRelevancy" in the old docs)
                                                                                  (3 LLM calls + 2 embedding calls)
  context_precision  ragas.metrics.collections.ContextPrecisionWithoutReference   (1 LLM call per context)

VERIFIED on Windows 11 / Python 3.13.3 / 2026-10-07 with this exact pin set (see 04-ragas-evaluation.md):

    pip install "ragas==0.4.3" "langchain-community==0.4.1" "instructor>=1.15" "openai>=2"

  * langchain-community==0.4.1 is REQUIRED: with 0.4.2 (the pip default today) `import ragas` crashes
    (ModuleNotFoundError: langchain_community.chat_models.vertexai).
  * `pip install ragas` alone currently also resolves to an ancient instructor 1.3.2 + openai 1.109.1.

Run:
    set OPENAI_API_KEY=sk-...                      (real judge, costs a few cents)
    python ragas_minimal_example.py
    python ragas_minimal_example.py --sync         (same, through the sync wrapper)
    python ragas_minimal_example.py --offline      (NO key, NO network: talks to ragas_offline_stub_server.py on 127.0.0.1;
                                                    proves the call path only - the numbers are meaningless)

Design rules baked in (each one was verified, see the notes file):
  1. Collections API only (ragas.metrics.collections + llm_factory + embedding_factory). `evaluate()`,
     `ragas.metrics.Faithfulness`, LangchainLLMWrapper, LangchainEmbeddingsWrapper are deprecated in 0.4.3
     and `evaluate()` rejects collections metrics.
  2. Telemetry off: RAGAS_DO_NOT_TRACK must be exactly "true" (case-insensitive; "1" does NOT work) and
     must be set before the first ragas call.
  3. Always `await metric.ascore(...)`.  `metric.score()` raises inside a running loop, and when called
     repeatedly from sync code with ONE shared AsyncOpenAI client it fails on every 2nd call
     ("Connection error": the pooled connection belongs to the previous, closed event loop).
     -> async code: one AsyncOpenAI client per event loop;  sync code: SyncScorer (dedicated loop thread).
  4. Timeouts/retries/rate limits live on the OpenAI client (timeout=, max_retries=); RunConfig is legacy.
  5. Metrics are run concurrently with asyncio.gather(return_exceptions=True): one failing metric does
     not lose the other two.  NaN -> None so the result is valid JSON (json.dumps(nan) is not).
"""
from __future__ import annotations

import os

os.environ["RAGAS_DO_NOT_TRACK"] = "true"  # exactly "true"; set BEFORE importing ragas

import argparse
import asyncio
import json
import math
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI
from ragas import __version__ as RAGAS_VERSION
from ragas.embeddings.base import embedding_factory
from ragas.llms import llm_factory
from ragas.metrics.collections import (
    AnswerRelevancy,
    ContextPrecisionWithoutReference,
    Faithfulness,
)
from ragas.metrics.collections.context_precision.util import (
    ContextPrecisionInput,
    ContextPrecisionOutput,
)

# --------------------------------------------------------------------------------------
# Honest tooltips for the UI (what the numbers do and do not mean)
# --------------------------------------------------------------------------------------
TOOLTIPS = {
    "faithfulness": (
        "Share of the claims in the answer that an LLM judge could infer from the retrieved pages. "
        "1.0 = every claim is supported by the retrieved text. It does NOT check the claims against "
        "reality, and it does not know about text the retriever missed."
    ),
    "answer_relevancy": (
        "How closely the question can be re-created from the answer alone (embedding similarity of "
        "questions an LLM writes from the answer vs. your question). It ignores correctness. Evasive or "
        "'not found in the report' answers score 0; very long, multi-topic answers score lower."
    ),
    "context_precision": (
        "Rank-weighted share of the retrieved pages that an LLM judge found useful for producing this "
        "answer. 1.0 = all pages useful (and useful ones first). It measures the retrieved pages vs. the "
        "generated answer, not vs. a gold answer, so a wrong answer can still score high."
    ),
    "_general": (
        "LLM-judged estimates, not ground truth. Scores depend on the judge model and can differ by a few "
        "points between runs. Use them to compare answers and spot problems, not as accuracy percentages."
    ),
}


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------
@dataclass
class ScorerConfig:
    judge_model: str = os.getenv("RAGAS_JUDGE_MODEL", "gpt-4.1-mini")
    embedding_model: str = os.getenv("RAGAS_EMBEDDING_MODEL", "text-embedding-3-small")
    judge_max_tokens: int = int(os.getenv("RAGAS_JUDGE_MAX_TOKENS", "4096"))
    reasoning_effort: Optional[str] = os.getenv("RAGAS_JUDGE_REASONING_EFFORT") or None
    max_contexts: int = 8              # ContextPrecision = 1 LLM call per context
    max_chars_per_context: int = 8000  # ~2k tokens; ragas itself NEVER truncates
    concurrency: int = 8               # in-flight judge calls (process-wide semaphore)
    metric_timeout_s: float = 120.0
    client_timeout_s: float = 60.0
    client_max_retries: int = 3        # the OpenAI SDK retries 408/409/429/5xx with backoff
    relevancy_strictness: int = 3      # number of questions generated from the answer
    relevancy_temperature: float = 0.3 # non-reasoning judges only; ragas' default 0.01 makes the 3 questions near-identical
    base_url: Optional[str] = os.getenv("OPENAI_BASE_URL") or None
    api_key: Optional[str] = None      # default: OPENAI_API_KEY env var


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
_CITATION_RES = [
    re.compile(r"\s*\[(?:pp?\.?|pages?)\s*\d+(?:\s*[,\-–]\s*\d+)*\]", re.I),   # [p. 45] [pp 3-4] [page 7]
    re.compile(r"\s*\(\s*(?:pp?\.?|pages?)\s*\d+(?:\s*[,\-–]\s*\d+)*\s*\)", re.I),  # (p. 45) (page 7)
    re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]"),                                         # [1] [2, 3]
]


def strip_citations(answer: str) -> str:
    """Score the prose, not the citation markers ([p. 45], [1] ...). Keep the original for display."""
    for rx in _CITATION_RES:
        answer = rx.sub("", answer)
    return answer.strip()


def clean_contexts(contexts: List[str], max_contexts: int, max_chars: int) -> List[str]:
    """Drop blanks and duplicates (keep first = highest rank), cap length and count."""
    seen, out = set(), []
    for c in contexts:
        c = (c or "").strip()
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(c[:max_chars])
        if len(out) >= max_contexts:
            break
    return out


def _num(x: Any) -> Optional[float]:
    """MetricResult/float -> clamped, rounded float; NaN/None -> None (valid JSON)."""
    try:
        v = float(x)
    except Exception:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(min(1.0, max(0.0, v)), 4)


def _ragas_misses_reasoning_model(model: str) -> bool:
    """ragas 0.4.3 auto-adapts only 'o1'..'o9', 'gpt-5', 'gpt-5-*', 'gpt-6'..'gpt-19' (single integer version).
    Dotted ids such as gpt-5.4-mini, gpt-5.6-luna, gpt-6.1-sol are treated as classic chat models -> it
    would send max_tokens/temperature/top_p, which reasoning models reject (HTTP 400, expected, unverified live)."""
    m = re.match(r"^gpt-(\d+)\.\d+", model.lower())
    return bool(m and int(m.group(1)) >= 5)


def _is_reasoning_model(model: str) -> bool:
    """o1..o9, gpt-5*, gpt-6*, ... (dotted or not).  Reasoning models: temperature/top_p are not tunable."""
    return bool(re.match(r"^(?:o\d+|gpt-(?:[5-9]|1\d))(?:[.\-_]|$)", model.lower()))


def make_judge_llm(client: AsyncOpenAI, model: str, max_tokens: int, reasoning_effort: Optional[str] = None, **extra):
    kw: Dict[str, Any] = {"max_tokens": max_tokens, **extra}
    if reasoning_effort:
        kw["reasoning_effort"] = reasoning_effort
    llm = llm_factory(model, client=client, **kw)
    if _ragas_misses_reasoning_model(model):
        a = llm.model_args  # dict that is splatted into chat.completions.create(...)
        if "max_tokens" in a:
            a["max_completion_tokens"] = a.pop("max_tokens")
        a.pop("temperature", None)
        a.pop("top_p", None)
    return llm


class ParallelContextPrecision(ContextPrecisionWithoutReference):
    """Identical prompt and maths to ragas' ContextPrecisionWithoutReference, but
       (a) judges all contexts concurrently (stock class awaits them one by one: N x latency), and
       (b) also returns each context's verdict + reason so the UI can show which pages were judged useful.
       Relies on ragas 0.4.3 internals: self.prompt, self.llm, self._calculate_average_precision."""

    async def ascore_detailed(self, user_input: str, response: str, retrieved_contexts: List[str]):
        async def judge(ctx: str) -> ContextPrecisionOutput:
            data = ContextPrecisionInput(question=user_input, context=ctx, answer=response)
            return await self.llm.agenerate(self.prompt.to_string(data), ContextPrecisionOutput)

        outs = await asyncio.gather(*(judge(c) for c in retrieved_contexts))
        verdicts = [1 if o.verdict else 0 for o in outs]
        return self._calculate_average_precision(verdicts), [
            {"index": i, "verdict": v, "reason": o.reason} for i, (v, o) in enumerate(zip(verdicts, outs))
        ]


# --------------------------------------------------------------------------------------
# Async scorer  (create it INSIDE the event loop that will use it; never share across loops)
# --------------------------------------------------------------------------------------
class RagasScorer:
    def __init__(self, cfg: Optional[ScorerConfig] = None):
        self.cfg = cfg or ScorerConfig()
        c = self.cfg
        self.client = AsyncOpenAI(
            api_key=c.api_key,  # None -> OPENAI_API_KEY
            base_url=c.base_url,
            timeout=c.client_timeout_s,
            max_retries=c.client_max_retries,
        )
        llm = make_judge_llm(self.client, c.judge_model, c.judge_max_tokens, c.reasoning_effort)
        # separate LLM object for question generation so only it gets a non-zero temperature
        # (reasoning models: ragas forces temperature=1.0 anyway, so no extra args there)
        rel_extra = {} if _is_reasoning_model(c.judge_model) else {"temperature": c.relevancy_temperature, "top_p": 1.0}
        llm_rel = make_judge_llm(self.client, c.judge_model, c.judge_max_tokens, c.reasoning_effort, **rel_extra)
        embeddings = embedding_factory("openai", model=c.embedding_model, client=self.client)

        self.faithfulness = Faithfulness(llm=llm)
        self.answer_relevancy = AnswerRelevancy(llm=llm_rel, embeddings=embeddings, strictness=c.relevancy_strictness)
        self.context_precision = ParallelContextPrecision(llm=llm, name="context_precision")
        self._sem = asyncio.Semaphore(c.concurrency)

    async def aclose(self) -> None:
        await self.client.close()

    async def _guard(self, coro):
        async with self._sem:
            return await asyncio.wait_for(coro, timeout=self.cfg.metric_timeout_s)

    async def score(self, question: str, answer: str, contexts: List[str]) -> Dict[str, Any]:
        """contexts = the page texts the answering LLM actually read, best/first-ranked first."""
        t0 = time.perf_counter()
        c = self.cfg
        ctxs = clean_contexts(contexts, c.max_contexts, c.max_chars_per_context)
        resp = strip_citations(answer)
        out: Dict[str, Any] = {
            "faithfulness": None, "answer_relevancy": None, "context_precision": None,
            "context_verdicts": [], "errors": {},
            "n_contexts_scored": len(ctxs), "n_contexts_input": len(contexts),
            "judge_model": c.judge_model, "embedding_model": c.embedding_model, "ragas_version": RAGAS_VERSION,
        }
        if not question.strip() or not resp or not ctxs:
            out["errors"]["input"] = "need a non-empty question, answer and at least one non-empty context"
            return out

        results = await asyncio.gather(
            self._guard(self.faithfulness.ascore(user_input=question, response=resp, retrieved_contexts=ctxs)),
            self._guard(self.answer_relevancy.ascore(user_input=question, response=resp)),
            self._guard(self.context_precision.ascore_detailed(question, resp, ctxs)),
            return_exceptions=True,
        )
        names = ["faithfulness", "answer_relevancy", "context_precision"]
        for name, r in zip(names, results):
            if isinstance(r, BaseException):
                out["errors"][name] = f"{type(r).__name__}: {str(r)[:300]}"
                continue
            if name == "context_precision":
                value, verdicts = r
                out["context_verdicts"] = verdicts
                out[name] = _num(value)
            else:
                out[name] = _num(r.value)  # r is a ragas MetricResult
        out["latency_s"] = round(time.perf_counter() - t0, 2)
        return out


# --------------------------------------------------------------------------------------
# Sync wrapper for scripts / threadpool endpoints: ONE background loop owns the client
# --------------------------------------------------------------------------------------
class SyncScorer:
    def __init__(self, cfg: Optional[ScorerConfig] = None):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="ragas-loop", daemon=True)
        self._thread.start()
        self._scorer = asyncio.run_coroutine_threadsafe(self._build(cfg), self._loop).result()

    @staticmethod
    async def _build(cfg):
        return RagasScorer(cfg)

    def score(self, question: str, answer: str, contexts: List[str], timeout: Optional[float] = None) -> Dict[str, Any]:
        return asyncio.run_coroutine_threadsafe(self._scorer.score(question, answer, contexts), self._loop).result(timeout)

    def close(self) -> None:
        asyncio.run_coroutine_threadsafe(self._scorer.aclose(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)


# --------------------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------------------
DEMO_Q = "What was the group's underlying operating profit and how did the dividend change?"
DEMO_A = (
    "Underlying operating profit was 4,500 million pounds for the year [p. 12]. "
    "The full-year dividend increased by 9 percent [p. 14]."
)
DEMO_CTX = [  # in retrieval order; the 3rd page is deliberately irrelevant
    "Financial review. Group underlying operating profit was 4,500 million pounds for the year, up on last year.",
    "Dividend. The board proposed a full-year dividend increase of 9 percent in line with CPIH.",
    "Governance. The registered office is located in London and the company number is 4031152.",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="use the local stub server (no key, no network)")
    ap.add_argument("--sync", action="store_true", help="use SyncScorer instead of awaiting RagasScorer")
    args = ap.parse_args()

    cfg = ScorerConfig()
    if args.offline:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import ragas_offline_stub_server as stub  # noqa: WPS433

        _srv, base_url = stub.start()
        cfg.base_url, cfg.api_key = base_url, "offline-stub-not-a-real-key"
        print(f"[offline] stub OpenAI server at {base_url} (scores are meaningless, only the wiring is tested)")
    elif not os.getenv("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY, or run with --offline")

    if args.sync:
        s = SyncScorer(cfg)
        result = s.score(DEMO_Q, DEMO_A, DEMO_CTX)
        result2 = s.score(DEMO_Q, DEMO_A, DEMO_CTX)  # 2nd call on purpose: works (unlike metric.score() x2)
        s.close()
    else:
        async def go():
            scorer = RagasScorer(cfg)
            try:
                return await scorer.score(DEMO_Q, DEMO_A, DEMO_CTX)
            finally:
                await scorer.aclose()
        result = asyncio.run(go())
        result2 = None

    print(json.dumps(result, indent=2))
    if result2 is not None:
        print("second call identical keys:", sorted(result2) == sorted(result))


if __name__ == "__main__":
    main()
