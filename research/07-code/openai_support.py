"""Reference helpers: model config from .env, usage metering + USD cost, startup preflight.

Offline-tested (mock transport) against openai 3.26.0 / Python 3.13.3 / Windows.
No network and no key needed to import.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))  # .env is git-ignored; never hard-code keys


# --------------------------------------------------------------------------- config
def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else default


@dataclass(frozen=True)
class ModelConfig:
    index_model: str = field(default_factory=lambda: _env("PI_INDEX_MODEL", "gpt-5.6-luna"))
    chat_model: str = field(default_factory=lambda: _env("PI_CHAT_MODEL", "gpt-5.6-terra"))
    chat_effort: str = field(default_factory=lambda: _env("PI_CHAT_REASONING_EFFORT", "medium"))
    judge_model: str = field(default_factory=lambda: _env("RAGAS_JUDGE_MODEL", "gpt-4.1-mini"))
    judge_effort: str = field(default_factory=lambda: _env("RAGAS_JUDGE_REASONING_EFFORT", ""))  # "" = do not send
    judge_max_tokens: int = field(default_factory=lambda: int(_env("RAGAS_JUDGE_MAX_TOKENS", "4096")))
    embedding_model: str = field(default_factory=lambda: _env("RAGAS_EMBEDDING_MODEL", "text-embedding-3-small"))


# USD per 1M tokens. VERIFIED 2026-10-07 from developers.openai.com/api/docs/models/<id>.md
# (in, cached_in, cache_write, out). cache_write == in where the page lists no cache-write price.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "gpt-6-astra": (10.00, 1.00, 12.50, 50.00),
    "gpt-6.1-sol": (2.00, 0.10, 2.50, 10.00),
    "gpt-6-sol": (2.00, 0.20, 2.50, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.125, 0.50),
    "gpt-5.6-sol": (4.00, 0.40, 5.00, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 2.50, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 0.25, 1.20),
    "gpt-5.4-mini": (0.75, 0.075, 0.75, 4.50),
    "gpt-4.1": (2.00, 0.50, 2.00, 8.00),
    "gpt-4.1-mini": (0.40, 0.10, 0.40, 1.60),
    "gpt-4o": (2.50, 1.25, 2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.075, 0.15, 0.60),
    "text-embedding-3-small": (0.02, 0.0, 0.0, 0.0),
    "text-embedding-3-large": (0.13, 0.0, 0.0, 0.0),
}


def price_for(model: str) -> tuple[float, float, float, float] | None:
    """Exact match first, then longest known prefix (handles dated snapshots like gpt-4o-mini-2024-07-18)."""
    if model in PRICES:
        return PRICES[model]
    best = max((k for k in PRICES if model.startswith(k + "-")), key=len, default=None)
    return PRICES[best] if best else None


# --------------------------------------------------------------------------- usage / cost
@dataclass
class Usage:
    input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0  # includes reasoning tokens (billed as output)
    reasoning_tokens: int = 0

    def __iadd__(self, o: "Usage") -> "Usage":
        for f in ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens"):
            setattr(self, f, getattr(self, f) + getattr(o, f))
        return self


def usage_from_response(resp: Any) -> Usage:
    """Works for Responses (`input_tokens`...) and Chat Completions (`prompt_tokens`...) and Embeddings."""
    u = getattr(resp, "usage", None)
    if u is None:
        return Usage()
    if hasattr(u, "input_tokens"):  # Responses API (also ragas/instructor raw responses of that type)
        d_in = getattr(u, "input_tokens_details", None)
        d_out = getattr(u, "output_tokens_details", None)
        return Usage(u.input_tokens or 0, getattr(d_in, "cached_tokens", 0) or 0,
                     getattr(d_in, "cache_write_tokens", 0) or 0, u.output_tokens or 0,
                     getattr(d_out, "reasoning_tokens", 0) or 0)
    if hasattr(u, "prompt_tokens"):  # Chat Completions / Embeddings
        d_in = getattr(u, "prompt_tokens_details", None)
        d_out = getattr(u, "completion_tokens_details", None)
        return Usage(u.prompt_tokens or 0, getattr(d_in, "cached_tokens", 0) or 0,
                     getattr(d_in, "cache_write_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0,
                     getattr(d_out, "reasoning_tokens", 0) or 0)
    return Usage()


def cost_usd(model: str, u: Usage) -> float | None:
    p = price_for(model)
    if p is None:
        return None  # unknown model: show tokens only, never invent a price
    p_in, p_cached, p_write, p_out = p
    fresh = max(0, u.input_tokens - u.cached_tokens - u.cache_write_tokens)
    return (fresh * p_in + u.cached_tokens * p_cached + u.cache_write_tokens * p_write + u.output_tokens * p_out) / 1e6


class UsageMeter:
    """Thread-safe ledger. One per request/session; `snapshot()` goes straight to the UI/JSON."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.rows: list[dict] = []

    def record(self, stage: str, model: str, u: Usage) -> None:
        with self._lock:
            self.rows.append({"stage": stage, "model": model, "calls": 1, **u.__dict__, "usd": cost_usd(model, u)})

    def snapshot(self) -> dict:
        with self._lock:
            by: dict[tuple[str, str], dict] = {}
            for r in self.rows:
                k = (r["stage"], r["model"])
                a = by.setdefault(k, {"stage": k[0], "model": k[1], "calls": 0, "input_tokens": 0, "cached_tokens": 0,
                                      "cache_write_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "usd": 0.0,
                                      "usd_complete": True})
                for f in ("calls", "input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens"):
                    a[f] += r[f]
                if r["usd"] is None:
                    a["usd_complete"] = False
                else:
                    a["usd"] += r["usd"]
            rows = list(by.values())
            return {"rows": rows, "total_usd": round(sum(r["usd"] for r in rows), 6),
                    "complete": all(r["usd_complete"] for r in rows)}


def meter_client(client: Any, meter: UsageMeter, stage: str) -> Any:
    """Wrap create() on responses / chat.completions / embeddings (sync or async client) so EVERY call is recorded.

    Wrap BEFORE handing the client to instructor/ragas (`llm_factory`) so their internal retries are metered too.
    """
    import openai

    # NB: inspect.iscoroutinefunction() is False for the SDK's decorated async create(); test the client type instead.
    is_async = isinstance(client, openai.AsyncOpenAI)

    def wrap(fn: Callable, default_model: str | None = None):
        if is_async:
            async def awrapped(*a: Any, **kw: Any):
                r = await fn(*a, **kw)
                meter.record(stage, kw.get("model") or getattr(r, "model", "?"), usage_from_response(r))
                return r
            return awrapped

        def wrapped(*a: Any, **kw: Any):
            r = fn(*a, **kw)
            if hasattr(r, "usage"):  # streaming iterators have no .usage; meter those via the final event instead
                meter.record(stage, kw.get("model") or getattr(r, "model", "?"), usage_from_response(r))
            return r
        return wrapped

    client.responses.create = wrap(client.responses.create)
    client.chat.completions.create = wrap(client.chat.completions.create)
    client.embeddings.create = wrap(client.embeddings.create)
    return client


# --------------------------------------------------------------------------- startup preflight
def preflight(client: Any, wanted: dict[str, str]) -> dict[str, dict]:
    """Free check (no tokens): GET /v1/models/{id} per configured model. Returns {role: {ok, error, shutdown_date}}."""
    import openai

    out: dict[str, dict] = {}
    for role, model in wanted.items():
        try:
            m = client.models.retrieve(model)
            out[role] = {"model": model, "ok": True, "shutdown_date": getattr(m, "shutdown_date", None)}
        except openai.AuthenticationError as e:       # 401: bad/missing key -> stop everything
            out[role] = {"model": model, "ok": False, "fatal": True, "error": "invalid API key", "status": e.status_code}
        except openai.NotFoundError as e:              # 404: model id typo / no access for this project
            out[role] = {"model": model, "ok": False, "error": "model not found or not accessible", "status": e.status_code}
        except openai.PermissionDeniedError as e:      # 403: region / project restrictions
            out[role] = {"model": model, "ok": False, "error": "permission denied", "status": e.status_code}
        except openai.APIConnectionError as e:
            out[role] = {"model": model, "ok": False, "fatal": True, "error": f"cannot reach api.openai.com: {e.__cause__ or e}"}
    return out


# 429s that retrying cannot fix (VERIFIED list from developers.openai.com/api/docs/guides/error-codes.md)
NON_RETRYABLE_429_CODES = {
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
}


def is_retryable(exc: Exception) -> bool:
    import openai

    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError)):
        return True
    if isinstance(exc, openai.RateLimitError):
        return getattr(exc, "code", None) not in NON_RETRYABLE_429_CODES
    return False


# --------------------------------------------------------------------------- RAGAS / instructor shim for reasoning models
def adapt_for_reasoning(client: Any, effort: str | None = None, min_completion_tokens: int = 4096) -> Any:
    """Make an OpenAI client safe for RAGAS 0.4.3 `llm_factory` with GPT-5.6 / GPT-6.x reasoning models.

    RAGAS only recognises integer-only versions (gpt-5, gpt-6...) as reasoning models, so for `gpt-6.1-sol`,
    `gpt-5.6-*`, `gpt-5.5`, `gpt-5.4-*` it sends max_tokens + temperature=0.01 + top_p=0.1, and for `gpt-6-*` it
    sends temperature=1.0 + max_completion_tokens=1024.  OpenAI says to remove temperature/top_p whenever
    reasoning effort != none, and max_tokens is not accepted by reasoning models.  This shim, installed BEFORE
    `llm_factory(...)`, rewrites each Chat Completions call:
      max_tokens -> max_completion_tokens (raised to >= min_completion_tokens: reasoning tokens count against it),
      drops temperature/top_p, and injects reasoning_effort when `effort` is given.
    Call order: meter_client(client) first (innermost), then adapt_for_reasoning(client), then llm_factory.
    """
    import openai

    is_async = isinstance(client, openai.AsyncOpenAI)
    inner = client.chat.completions.create

    def fix(kw: dict) -> dict:
        kw = dict(kw)
        mt = kw.pop("max_tokens", None)
        mct = kw.pop("max_completion_tokens", None)
        want = max(x for x in (mt, mct, min_completion_tokens) if x is not None)
        kw["max_completion_tokens"] = want
        if effort:
            kw["reasoning_effort"] = effort
        if kw.get("reasoning_effort") != "none":
            kw.pop("temperature", None)
            kw.pop("top_p", None)
        return kw

    if is_async:
        async def acreate(*a: Any, **kw: Any):
            return await inner(*a, **fix(kw))
        client.chat.completions.create = acreate
    else:
        def create(*a: Any, **kw: Any):
            return inner(*a, **fix(kw))
        client.chat.completions.create = create
    return client
