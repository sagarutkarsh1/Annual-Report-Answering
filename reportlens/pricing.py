"""OpenAI list prices and the per-answer cost estimate shown next to each answer.

Source: developers.openai.com model pages and /api/docs/pricing (standard processing), fetched 2026-10-07 and recorded in
research/07-openai-models-and-costs.md section 2.  The gpt-5.6-sol price is a promotion that runs "at least through
2026-11-21": re-check this table then.  Prices are an ESTIMATE of what the account is billed (no batch/flex/priority tier,
no regional uplift); a model that is not in the table has no price rather than a guessed one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .models import Usage

LONG_CONTEXT_TOKENS = 272_000     # a single prompt above this is billed at the surcharge rates below (gpt-5.6 / gpt-6 families)
LONG_CONTEXT_INPUT_FACTOR = 2.0   # input, cached input and cache writes
LONG_CONTEXT_OUTPUT_FACTOR = 1.5  # applies to the whole request, not only the tokens above the threshold


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens."""
    input: float
    cached: float                 # cache read
    output: float                 # includes reasoning tokens
    cache_write: Optional[float] = None   # None: the model bills cache writes like fresh input
    long_context_surcharge: bool = False


_SURCHARGED = {"long_context_surcharge": True}

PRICES: dict[str, ModelPrice] = {
    "gpt-6-astra": ModelPrice(10.00, 1.00, 50.00, 12.50, **_SURCHARGED),
    "gpt-6.1-sol": ModelPrice(2.00, 0.10, 10.00, 2.50, **_SURCHARGED),
    "gpt-6-sol": ModelPrice(2.00, 0.20, 10.00, 2.50, **_SURCHARGED),
    "gpt-6-luna": ModelPrice(0.10, 0.01, 0.50, 0.125, **_SURCHARGED),
    "gpt-5.6-sol": ModelPrice(4.00, 0.40, 20.00, 5.00, **_SURCHARGED),
    "gpt-5.6-terra": ModelPrice(2.00, 0.20, 12.00, 2.50, **_SURCHARGED),
    "gpt-5.6-luna": ModelPrice(0.20, 0.02, 1.20, 0.25, **_SURCHARGED),
    "gpt-4.1": ModelPrice(2.00, 0.50, 8.00),
    "gpt-4.1-mini": ModelPrice(0.40, 0.10, 1.60),
    "gpt-4o": ModelPrice(2.50, 1.25, 10.00),
    "gpt-4o-mini": ModelPrice(0.15, 0.075, 0.60),
}

# "gpt-4.1-2025-04-14", "gpt-4o-mini-2024-07-18" (and the compact "-20250414" spelling): a dated snapshot of an alias in the table.
# Only dates are stripped, so "gpt-4.1-nano" (a different, cheaper model) is never priced as "gpt-4.1".
_SNAPSHOT_RE = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")


def price_for(model: Optional[str]) -> Optional[ModelPrice]:
    """The price of `model` (exact id, or a dated snapshot of one); None when unknown."""
    if not model:
        return None
    name = model.strip().removeprefix("openai/")
    return PRICES.get(name) or PRICES.get(_SNAPSHOT_RE.sub("", name))


def estimate_cost(model: Optional[str], usage: Usage, *, prompt_tokens: Optional[int] = None,
                  cache_write_tokens: int = 0) -> Optional[float]:
    """Estimated USD for `usage`, or None when `model` has no price.

    usage.input_tokens counts every prompt token including the cached ones (and cache writes); the cached ones are billed at
    the cache-read rate.  Reasoning tokens are already inside output_tokens.

    prompt_tokens: size of the LARGEST single request, which decides the long-context surcharge (the whole request is billed
    at 2x input / 1.5x output above 272k tokens).  Defaults to usage.input_tokens, i.e. `usage` is one request; an agent run
    sums several requests, so its caller passes the per-request size (summing would trigger the surcharge wrongly).
    """
    price = price_for(model)
    if price is None:
        return None
    cached = min(max(usage.cached_tokens, 0), usage.input_tokens)
    writes = min(max(cache_write_tokens, 0), usage.input_tokens - cached)
    fresh = usage.input_tokens - cached - writes
    in_factor = out_factor = 1.0
    largest = usage.input_tokens if prompt_tokens is None else prompt_tokens
    if price.long_context_surcharge and largest > LONG_CONTEXT_TOKENS:
        in_factor, out_factor = LONG_CONTEXT_INPUT_FACTOR, LONG_CONTEXT_OUTPUT_FACTOR
    write_rate = price.input if price.cache_write is None else price.cache_write
    total = ((fresh * price.input + cached * price.cached + writes * write_rate) * in_factor
             + max(usage.output_tokens, 0) * price.output * out_factor)
    return total / 1_000_000
