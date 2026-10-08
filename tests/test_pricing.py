"""pricing: the price table, dated-snapshot resolution, cached tokens, cache writes and the long-context surcharge."""
from __future__ import annotations

import pytest

from reportlens.models import Usage
from reportlens.pricing import LONG_CONTEXT_TOKENS, PRICES, estimate_cost, price_for

MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna",
          "gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"]


def usage(inp=0, cached=0, out=0, reasoning=0) -> Usage:
    return Usage(input_tokens=inp, cached_tokens=cached, output_tokens=out, reasoning_tokens=reasoning)


def test_table_covers_every_documented_model():
    assert set(MODELS) <= set(PRICES)
    for name in MODELS:
        p = PRICES[name]
        assert 0 < p.cached < p.input < p.output, name      # a cache read is cheaper than fresh input; output costs most


@pytest.mark.parametrize("model,expected", [
    ("gpt-5.6-sol", 4.00 * 0.6 + 0.40 * 0.4 + 20.0 * 0.1),     # 600k fresh, 400k cached, 100k out -> per-1M table values
    ("gpt-4.1-mini", 0.40 * 0.6 + 0.10 * 0.4 + 1.60 * 0.1),
    ("gpt-4o", 2.50 * 0.6 + 1.25 * 0.4 + 10.0 * 0.1),
])
def test_cost_splits_fresh_cached_and_output(model, expected):
    cost = estimate_cost(model, usage(inp=1_000_000, cached=400_000, out=100_000), prompt_tokens=10_000)
    assert cost == pytest.approx(expected)


def test_known_example_from_the_research_notes():
    # NVIDIA 10-K trace, gpt-5.6-luna: 40,202 input (23,321 cached, 16,872 cache-write), 298 output ~ $0.005 (research/03 s2.9)
    u = usage(inp=40_202, cached=23_321, out=298)
    cost = estimate_cost("gpt-5.6-luna", u, cache_write_tokens=16_872)
    assert cost == pytest.approx((9 * 0.2 + 23_321 * 0.02 + 16_872 * 0.25 + 298 * 1.2) / 1e6)
    assert 0.004 < cost < 0.006


def test_unknown_or_missing_model_has_no_price():
    assert estimate_cost("gpt-9-imaginary", usage(inp=10, out=10)) is None
    assert estimate_cost("gpt-4.1-nano", usage(inp=10, out=10)) is None      # a different, cheaper model: never priced as gpt-4.1
    assert estimate_cost(None, usage(inp=10, out=10)) is None
    assert estimate_cost("", usage(inp=10, out=10)) is None


@pytest.mark.parametrize("snapshot,alias", [
    ("gpt-4.1-2025-04-14", "gpt-4.1"),
    ("gpt-4.1-mini-2025-04-14", "gpt-4.1-mini"),
    ("gpt-4o-2024-11-20", "gpt-4o"),
    ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
    ("gpt-4.1-20250414", "gpt-4.1"),
    ("openai/gpt-5.6-sol", "gpt-5.6-sol"),
])
def test_dated_snapshots_resolve_to_their_alias(snapshot, alias):
    assert price_for(snapshot) == PRICES[alias]
    u = usage(inp=5_000, cached=1_000, out=500)
    assert estimate_cost(snapshot, u) == estimate_cost(alias, u)


def test_zero_usage_costs_nothing_and_negative_values_are_clamped():
    assert estimate_cost("gpt-5.6-sol", usage()) == 0.0
    assert estimate_cost("gpt-5.6-sol", usage(inp=100, cached=500, out=0)) == pytest.approx(100 * 0.4 / 1e6)   # cached <= input


def test_cache_write_uses_the_write_rate_and_cannot_exceed_the_fresh_input():
    u = usage(inp=1_000_000, cached=0, out=0)
    small = {"prompt_tokens": 10_000}                    # keep the long-context surcharge out of this test
    assert estimate_cost("gpt-5.6-sol", u, cache_write_tokens=1_000_000, **small) == pytest.approx(5.00)   # 1.25x the 4.00 input price
    assert estimate_cost("gpt-5.6-sol", u, cache_write_tokens=9_000_000, **small) == pytest.approx(5.00)   # clamped to the input size
    assert estimate_cost("gpt-4.1", u, cache_write_tokens=1_000_000) == pytest.approx(2.00)               # no write premium on 4.1


def test_long_context_surcharge_doubles_input_and_inflates_output_for_the_whole_request():
    just_below = usage(inp=LONG_CONTEXT_TOKENS, cached=0, out=1_000_000)
    just_above = usage(inp=LONG_CONTEXT_TOKENS + 1, cached=0, out=1_000_000)
    below = estimate_cost("gpt-5.6-sol", just_below)
    above = estimate_cost("gpt-5.6-sol", just_above)
    assert below == pytest.approx(LONG_CONTEXT_TOKENS * 4.0 / 1e6 + 20.0)
    assert above == pytest.approx((LONG_CONTEXT_TOKENS + 1) * 4.0 * 2 / 1e6 + 20.0 * 1.5)


def test_surcharge_covers_cached_input_too():
    u = usage(inp=300_000, cached=200_000, out=0)
    assert estimate_cost("gpt-6-sol", u) == pytest.approx((100_000 * 2.0 + 200_000 * 0.20) * 2 / 1e6)


def test_gpt41_has_no_long_context_surcharge():
    u = usage(inp=900_000, out=100_000)
    assert estimate_cost("gpt-4.1", u) == pytest.approx((900_000 * 2.0 + 100_000 * 8.0) / 1e6)


def test_prompt_tokens_overrides_the_surcharge_decision_for_multi_request_usage():
    summed = usage(inp=600_000, out=10_000)             # e.g. six 100k-token agent turns: no single prompt is over the line
    assert estimate_cost("gpt-5.6-sol", summed) > estimate_cost("gpt-5.6-sol", summed, prompt_tokens=100_000)
    assert estimate_cost("gpt-5.6-sol", summed, prompt_tokens=100_000) == pytest.approx((600_000 * 4.0 + 10_000 * 20.0) / 1e6)
    assert estimate_cost("gpt-5.6-sol", usage(inp=100, out=1), prompt_tokens=LONG_CONTEXT_TOKENS + 1) == pytest.approx(
        (100 * 4.0 * 2 + 1 * 20.0 * 1.5) / 1e6)
