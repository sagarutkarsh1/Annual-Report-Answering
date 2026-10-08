"""Offline tests for reportlens.evaluation: the real RAGAS 0.4.3 metrics run against tests/_ragas_stub.py."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import pytest
from ragas.metrics.result import MetricResult

import reportlens.evaluation as evaluation
from reportlens.config import Settings
from reportlens.evaluation import METRIC_INFO, METRICS, Evaluator, prepare_contexts, strip_citation_markers
from reportlens.models import ContextPage, EvalScores
from tests._ragas_stub import RagasStub

QUESTION = "What was operating profit and how did the dividend change?"
ANSWER = "Operating profit was 4,500 million pounds [[c1]]. The dividend rose 9 percent [[c2]]."
PAGES = [
    ContextPage(page=12, text="Operating profit was 4,500 million pounds for the year."),
    ContextPage(page=14, text="The dividend rose 9 percent in line with inflation."),
    ContextPage(page=90, text="The registered office is in London."),   # unrelated -> verdict 0
]


@pytest.fixture
def stub():
    s = RagasStub().start()
    yield s
    s.stop()


@pytest.fixture
def cfg(settings: Settings, stub: RagasStub) -> Settings:
    return settings.with_(openai_base_url=stub.base_url)


@pytest.fixture
async def make_evaluator(cfg: Settings):
    """Factory for Evaluators bound to the stub (no SDK retries: faults surface immediately); closed on teardown."""
    created: list[Evaluator] = []

    def make(settings: Optional[Settings] = None, **kwargs: Any) -> Evaluator:
        kwargs.setdefault("client_max_retries", 0)
        ev = Evaluator(settings or cfg, **kwargs)
        created.append(ev)
        return ev

    yield make
    for ev in created:
        await ev.aclose()


def _bodies(stub: RagasStub, metric: Optional[str] = None) -> list[dict]:
    return [r["body"] for r in stub.chat_requests(metric)]


def _prompts(stub: RagasStub) -> list[str]:
    return [json.dumps(b["messages"]) for b in _bodies(stub)]


# ------------------------------------------------------------------------------------------------ happy path
async def test_all_three_metrics_score_between_zero_and_one(make_evaluator, stub):
    scores = await make_evaluator().evaluate(QUESTION, ANSWER, PAGES)

    assert scores.status == "done"
    for metric in METRICS:
        value = getattr(scores, metric)
        assert isinstance(value, float) and 0.0 <= value <= 1.0, metric
    assert scores.faithfulness == 1.0               # both statements appear in the pages
    assert scores.context_precision == 1.0          # [1, 1, 0]: junk after the useful pages is not penalised
    assert scores.errors == {}
    assert scores.n_contexts_input == 3 and scores.n_contexts_scored == 3
    assert scores.judge_model == "gpt-4.1" and scores.embedding_model == "text-embedding-3-small"
    assert scores.ragas_version == "0.4.3"
    assert scores.latency_s is not None and scores.latency_s >= 0
    json.dumps(scores.model_dump(), allow_nan=False)   # NaN would make this invalid JSON


async def test_call_counts_and_context_verdicts_carry_page_numbers(make_evaluator, stub):
    scores = await make_evaluator().evaluate(QUESTION, ANSWER, PAGES)

    # 2 (faithfulness) + 3 (relevancy strictness) + N=3 (one per context); 2 embedding calls
    assert len(stub.chat_requests()) == 8
    assert len(stub.chat_requests("context_precision")) == 3
    assert len(stub.embedding_requests()) == 2
    assert [(v["index"], v["page"], v["verdict"]) for v in scores.context_verdicts] == [(0, 12, 1), (1, 14, 1), (2, 90, 0)]
    assert all(isinstance(v["reason"], str) for v in scores.context_verdicts)


async def test_relevancy_questions_use_their_own_temperature(make_evaluator, stub):
    await make_evaluator().evaluate(QUESTION, ANSWER, PAGES)

    assert {b["temperature"] for b in _bodies(stub, "answer_relevancy")} == {0.3}
    assert {b["temperature"] for b in _bodies(stub, "faithfulness")} == {0.01}
    assert all(b["max_tokens"] == 4096 and b["response_format"] == {"type": "json_object"} for b in _bodies(stub))


async def test_citation_markers_never_reach_the_judge(make_evaluator, stub):
    answer = "Operating profit was 4,500 million pounds [[c1]]. The dividend rose 9 percent (p. 14) [2]."
    await make_evaluator().evaluate(QUESTION, answer, PAGES)

    prompts = " ".join(_prompts(stub))
    assert "[[c" not in prompts and "p. 14" not in prompts and "[2]" not in prompts
    assert "4,500 million pounds" in prompts


# ------------------------------------------------------------------------------------------------ callbacks
async def test_on_metric_fires_as_each_metric_finishes(make_evaluator, stub):
    stub.delay.update({"faithfulness": 0.3, "answer_relevancy": 0.1})   # 2 x 0.3 s, 3 x 0.1 s, precision instant
    seen: list[tuple[str, Optional[float], Optional[str]]] = []

    scores = await make_evaluator().evaluate(QUESTION, ANSWER, PAGES, on_metric=lambda *a: seen.append(a))

    assert [m for m, _, _ in seen] == ["context_precision", "answer_relevancy", "faithfulness"]
    assert all(err is None for _, _, err in seen)
    assert {m: v for m, v, _ in seen} == {m: getattr(scores, m) for m in METRICS}


async def test_async_callback_is_awaited(make_evaluator):
    seen: list[str] = []

    async def on_metric(metric: str, value: Optional[float], error: Optional[str]) -> None:
        await asyncio.sleep(0)
        seen.append(metric)

    await make_evaluator().evaluate(QUESTION, ANSWER, PAGES, on_metric=on_metric)

    assert sorted(seen) == sorted(METRICS)


@pytest.mark.parametrize("is_async", [False, True])
async def test_callback_exceptions_never_break_scoring(make_evaluator, caplog, is_async):
    calls: list[str] = []

    def boom_sync(metric: str, value: Any, error: Any) -> None:
        calls.append(metric)
        raise RuntimeError("listener bug")

    async def boom_async(metric: str, value: Any, error: Any) -> None:
        boom_sync(metric, value, error)

    with caplog.at_level(logging.ERROR, logger="reportlens.evaluation"):
        scores = await make_evaluator().evaluate(QUESTION, ANSWER, PAGES, on_metric=boom_async if is_async else boom_sync)

    assert scores.status == "done"
    assert sorted(calls) == sorted(METRICS)          # one failure did not stop the other notifications
    assert "on_metric callback failed" in caplog.text


# ------------------------------------------------------------------------------------------------ failures
async def test_one_failing_metric_gives_partial(make_evaluator, stub):
    stub.fail["answer_relevancy"] = 500
    seen: dict[str, tuple[Optional[float], Optional[str]]] = {}

    scores = await make_evaluator().evaluate(QUESTION, ANSWER, PAGES, on_metric=lambda m, v, e: seen.update({m: (v, e)}))

    assert scores.status == "partial"
    assert scores.answer_relevancy is None
    assert scores.faithfulness == 1.0 and scores.context_precision == 1.0
    assert list(scores.errors) == ["answer_relevancy"] and "server error" in scores.errors["answer_relevancy"]
    assert seen["answer_relevancy"][0] is None and seen["answer_relevancy"][1] == scores.errors["answer_relevancy"]
    assert seen["faithfulness"] == (1.0, None)


async def test_failing_embeddings_only_hurt_answer_relevancy(make_evaluator, stub):
    stub.fail["embeddings"] = 500

    scores = await make_evaluator().evaluate(QUESTION, ANSWER, PAGES)

    assert scores.status == "partial" and set(scores.errors) == {"answer_relevancy"}


async def test_all_metrics_failing_gives_failed(make_evaluator, stub):
    stub.fail.update({"faithfulness": 500, "answer_relevancy": 500, "context_precision": 500})
    seen: list[str] = []

    scores = await make_evaluator().evaluate(QUESTION, ANSWER, PAGES, on_metric=lambda m, v, e: seen.append(m))

    assert scores.status == "failed"
    assert (scores.faithfulness, scores.answer_relevancy, scores.context_precision) == (None, None, None)
    assert set(scores.errors) == set(METRICS)
    assert scores.context_verdicts == []
    assert sorted(seen) == sorted(METRICS)


async def test_auth_failure_is_reported_without_leaking_the_key(make_evaluator, stub):
    stub.fail.update({m: 401 for m in ("faithfulness", "answer_relevancy", "context_precision")})

    scores = await make_evaluator().evaluate(QUESTION, ANSWER, PAGES)

    assert scores.status == "failed"
    assert all(msg == "OpenAI rejected the API key" for msg in scores.errors.values())
    assert "sk-test" not in json.dumps(scores.model_dump())


async def test_a_slow_metric_times_out_alone(make_evaluator, stub):
    stub.delay["faithfulness"] = 1.5

    scores = await make_evaluator(metric_timeout_s=0.4).evaluate(QUESTION, ANSWER, PAGES)

    assert scores.status == "partial"
    assert scores.errors == {"faithfulness": "Timed out waiting for the judge model"}
    assert scores.answer_relevancy is not None and scores.context_precision is not None


async def test_nan_score_becomes_none_with_an_explanation(make_evaluator, monkeypatch):
    async def nan_score(self: Any, **kwargs: Any) -> MetricResult:
        return MetricResult(value=float("nan"))

    monkeypatch.setattr(evaluation.Faithfulness, "ascore", nan_score)
    seen: dict[str, Any] = {}

    scores = await make_evaluator().evaluate(QUESTION, ANSWER, PAGES, on_metric=lambda m, v, e: seen.update({m: (v, e)}))

    assert scores.status == "partial" and scores.faithfulness is None
    assert "No usable score" in scores.errors["faithfulness"]
    assert seen["faithfulness"] == (None, scores.errors["faithfulness"])
    json.dumps(scores.model_dump(), allow_nan=False)


def test_num_handles_nan_inf_and_range():
    num = evaluation._num
    assert num(float("nan")) is None and num(float("inf")) is None and num(None) is None and num("x") is None
    assert num(0.99999999995) == 1.0 and num(-0.02) == 0.0 and num(1.3) == 1.0
    assert num(MetricResult(value=0.123456)) == 0.1235


async def test_unusable_configuration_fails_every_metric_without_raising(make_evaluator, cfg):
    seen: list[tuple[str, Optional[str]]] = []

    scores = await make_evaluator(cfg.with_(judge_model="")).evaluate(
        QUESTION, ANSWER, PAGES, on_metric=lambda m, v, e: seen.append((m, e)))

    assert scores.status == "failed" and set(scores.errors) == set(METRICS)
    assert sorted(m for m, _ in seen) == sorted(METRICS) and all(e for _, e in seen)


async def test_unexpected_crash_is_contained(make_evaluator, monkeypatch):
    async def explode(self: Any, *args: Any, **kwargs: Any) -> EvalScores:
        raise RuntimeError("bug")

    monkeypatch.setattr(Evaluator, "_evaluate", explode)

    scores = await make_evaluator().evaluate(QUESTION, ANSWER, PAGES)

    assert scores.status == "failed" and set(scores.errors) == set(METRICS)


# ------------------------------------------------------------------------------------------------ skipped paths
@pytest.mark.parametrize("answer, contexts, reason", [
    (ANSWER, [], "no_contexts"),
    (ANSWER, [ContextPage(page=3, text="   "), ContextPage(page=4, text="")], "no_contexts"),
    ("", PAGES, "empty_answer"),
    ("  [[c1]] [[c2]]  ", PAGES, "empty_answer"),
])
async def test_nothing_to_score_is_skipped_without_calling_openai(make_evaluator, stub, answer, contexts, reason):
    scores = await make_evaluator().evaluate(QUESTION, answer, contexts)

    assert scores.status == "skipped" and scores.skipped_reason == reason
    assert scores.n_contexts_input == len({c.page for c in contexts})
    assert stub.requests == []


async def test_disabled_missing_key_and_empty_question_are_skipped(make_evaluator, cfg, stub):
    off = await make_evaluator(cfg.with_(eval_enabled=False)).evaluate(QUESTION, ANSWER, PAGES)
    no_key = await make_evaluator(cfg.with_(openai_api_key=None, openai_base_url=None)).evaluate(QUESTION, ANSWER, PAGES)
    blank_q = await make_evaluator().evaluate("  ", ANSWER, PAGES)

    assert (off.skipped_reason, no_key.skipped_reason, blank_q.skipped_reason) == ("disabled", "no_api_key", "empty_question")
    assert stub.requests == []


def test_skipped_factory():
    s = Evaluator.skipped("disabled")
    assert (s.status, s.skipped_reason) == ("skipped", "disabled")


async def test_keyless_local_gateway_is_allowed(make_evaluator, cfg):
    scores = await make_evaluator(cfg.with_(openai_api_key=None)).evaluate(QUESTION, ANSWER, PAGES)

    assert scores.status == "done"


# ------------------------------------------------------------------------------------------------ contexts
def test_prepare_contexts_dedupes_by_page_keeps_first_and_drops_blanks():
    ctxs = [ContextPage(page=5, text="first"), ContextPage(page=5, text="second"), ContextPage(page=6, text="  "),
            ContextPage(page=7, text="  seventh  ")]

    kept, n_read = prepare_contexts(ctxs, max_contexts=10, max_chars=100)

    assert [(c.page, c.text) for c in kept] == [(5, "first"), (7, "seventh")]
    assert n_read == 3                                   # distinct pages read, blank page included


def test_prepare_contexts_caps_count_and_length_in_read_order():
    ctxs = [ContextPage(page=p, text="x" * 100) for p in (9, 3, 7, 1)]

    kept, n_read = prepare_contexts(ctxs, max_contexts=2, max_chars=10)

    assert [c.page for c in kept] == [9, 3] and all(len(c.text) == 10 for c in kept)
    assert n_read == 4


async def test_context_cap_limits_judge_calls(make_evaluator, cfg, stub):
    many = [ContextPage(page=p, text=f"Operating profit was 4,500 million pounds on page {p}.") for p in range(1, 9)]
    many += [ContextPage(page=2, text="duplicate of page 2"), ContextPage(page=50, text="")]

    scores = await make_evaluator(cfg.with_(eval_max_contexts=3)).evaluate(QUESTION, ANSWER, many)

    assert scores.n_contexts_input == 9 and scores.n_contexts_scored == 3
    assert len(stub.chat_requests("context_precision")) == 3
    assert [v["page"] for v in scores.context_verdicts] == [1, 2, 3]


async def test_long_contexts_are_truncated_before_judging(make_evaluator, cfg, stub):
    long_page = ContextPage(page=1, text="Operating profit was 4,500 million pounds. " + "filler " * 500 + "ZZTAILZZ")

    await make_evaluator(cfg.with_(eval_max_chars_per_context=200)).evaluate(QUESTION, ANSWER, [long_page])

    assert "ZZTAILZZ" not in " ".join(_prompts(stub))


# ------------------------------------------------------------------------------------------------ marker stripping
@pytest.mark.parametrize("raw, expected", [
    ("Profit rose [[c1]].", "Profit rose."),
    ("Profit [[c1]] rose [[c2]][[c3]].", "Profit rose."),
    ("- first [[c1]]\n- second [[c2]][[c3]]", "- first\n- second"),
    ("Revenue (2024) was 5 (2023: 3,900) [p. 12][1, 2].", "Revenue (2024) was 5 (2023: 3,900)."),
    ("See (page 7) and (pp. 3-4) and [pages 5, 6].", "See and and."),
    ("The [2023] plan stayed.", "The [2023] plan stayed."),
    ("", ""),
])
def test_strip_citation_markers(raw, expected):
    assert strip_citation_markers(raw) == expected


# ------------------------------------------------------------------------------------------------ reasoning judges
def _judge_bodies(stub: RagasStub) -> list[dict]:
    bodies = _bodies(stub)
    assert len(bodies) == 8
    return bodies


async def test_reasoning_judge_never_receives_classic_sampling_params(make_evaluator, cfg, stub):
    scores = await make_evaluator(cfg.with_(judge_model="gpt-5.6-sol")).evaluate(QUESTION, ANSWER, PAGES)

    assert scores.status == "done" and scores.judge_model == "gpt-5.6-sol"
    for body in _judge_bodies(stub):
        assert body["model"] == "gpt-5.6-sol"
        assert body["max_completion_tokens"] == 4096
        assert not {"max_tokens", "temperature", "top_p", "reasoning_effort"} & set(body)
        assert body["response_format"] == {"type": "json_object"}


async def test_classic_judge_keeps_classic_params(make_evaluator, cfg, stub):
    await make_evaluator(cfg.with_(judge_model="gpt-4.1-mini")).evaluate(QUESTION, ANSWER, PAGES)

    for body in _judge_bodies(stub):
        assert body["max_tokens"] == 4096 and "temperature" in body and "top_p" in body
        assert not {"max_completion_tokens", "reasoning_effort"} & set(body)


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-6.1-sol", "gpt-6-luna", "gpt-5-mini", "o4-mini"])
async def test_reasoning_effort_is_forwarded_and_budget_honoured(make_evaluator, cfg, stub, model):
    ev = make_evaluator(cfg.with_(judge_model=model, judge_reasoning_effort="low", judge_max_tokens=8192))
    await ev.evaluate(QUESTION, ANSWER, PAGES)

    for body in _judge_bodies(stub):
        assert body["reasoning_effort"] == "low" and body["max_completion_tokens"] == 8192
        assert not {"max_tokens", "temperature", "top_p"} & set(body)


async def test_effort_none_keeps_temperature_legal_for_reasoning_models(make_evaluator, cfg, stub):
    await make_evaluator(cfg.with_(judge_model="gpt-5.6-luna", judge_reasoning_effort="none")).evaluate(QUESTION, ANSWER, PAGES)

    assert all(b["reasoning_effort"] == "none" and "temperature" in b for b in _judge_bodies(stub))


async def test_effort_is_ignored_for_classic_judges(make_evaluator, cfg, stub):
    await make_evaluator(cfg.with_(judge_model="gpt-4.1-mini", judge_reasoning_effort="low")).evaluate(QUESTION, ANSWER, PAGES)

    assert all("reasoning_effort" not in b for b in _judge_bodies(stub))


@pytest.mark.parametrize("model, expected", [
    ("gpt-5.6-sol", True), ("gpt-6.1-sol", True), ("gpt-6-luna", True), ("gpt-5", True), ("gpt-5-mini", True),
    ("o3", True), ("o4-mini", True), ("gpt-4.1-mini", False), ("gpt-4o-mini", False), ("gpt-4.1", False),
    ("text-embedding-3-small", False), ("", False),
])
def test_reasoning_model_detection(model, expected):
    assert evaluation._is_reasoning_model(model) is expected


# ------------------------------------------------------------------------------------------------ lifecycle
async def test_two_sequential_evaluations_reuse_the_client(make_evaluator, stub):
    ev = make_evaluator()

    first = await ev.evaluate(QUESTION, ANSWER, PAGES)
    runtime = ev._runtime
    second = await ev.evaluate(QUESTION, ANSWER, PAGES)

    assert first.status == second.status == "done"
    assert ev._runtime is runtime
    assert len(stub.chat_requests()) == 16


async def test_concurrent_evaluations_share_one_runtime(make_evaluator):
    ev = make_evaluator()

    results = await asyncio.gather(*(ev.evaluate(QUESTION, ANSWER, PAGES) for _ in range(3)))

    assert [r.status for r in results] == ["done"] * 3


def test_evaluate_from_two_different_event_loops(cfg, stub):
    ev = Evaluator(cfg, client_max_retries=0)

    async def once() -> tuple[EvalScores, Any]:
        return await ev.evaluate(QUESTION, ANSWER, PAGES), ev._runtime

    first, rt1 = asyncio.run(once())       # that loop is closed afterwards, so its pooled connections are dead
    second, rt2 = asyncio.run(once())
    third, rt3 = asyncio.run(once())

    assert first.status == second.status == third.status == "done"
    assert rt1.client is not rt2.client and rt2.client is not rt3.client
    assert len(stub.chat_requests()) == 24


async def test_client_is_created_lazily_and_aclose_is_idempotent(cfg, stub):
    ev = Evaluator(cfg, client_max_retries=0)
    assert ev._runtime is None                      # no client (and no network) from __init__
    await ev.aclose()                               # before first use: no-op

    await ev.evaluate(QUESTION, ANSWER, PAGES)
    client = ev._runtime.client
    await ev.aclose()
    await ev.aclose()

    assert ev._runtime is None and client.is_closed()
    again = await ev.evaluate(QUESTION, ANSWER, PAGES)    # usable after close: rebuilds lazily
    assert again.status == "done"
    await ev.aclose()


async def test_eval_concurrency_bounds_in_flight_requests(make_evaluator, cfg, stub):
    stub.latency = 0.05
    many = [ContextPage(page=p, text=f"Operating profit was 4,500 million pounds on page {p}.") for p in range(1, 7)]

    scores = await make_evaluator(cfg.with_(eval_concurrency=2)).evaluate(QUESTION, ANSWER, many)

    assert scores.status == "done"
    assert 1 <= stub.max_in_flight <= 2


async def test_unbounded_concurrency_would_exceed_the_limit(make_evaluator, cfg, stub):
    """Guards the test above: with a high limit the stub really does see parallel calls."""
    stub.latency = 0.05
    many = [ContextPage(page=p, text=f"Operating profit was 4,500 million pounds on page {p}.") for p in range(1, 7)]

    await make_evaluator(cfg.with_(eval_concurrency=16)).evaluate(QUESTION, ANSWER, many)

    assert stub.max_in_flight > 2


# ------------------------------------------------------------------------------------------------ ragas internals guard
async def test_context_precision_falls_back_to_stock_metric_when_internals_change(make_evaluator, stub, monkeypatch, caplog):
    monkeypatch.setattr(evaluation, "ContextPrecisionInput", None)

    with caplog.at_level(logging.WARNING, logger="reportlens.evaluation"):
        ev = make_evaluator()
        scores = await ev.evaluate(QUESTION, ANSWER, PAGES)

    assert not isinstance(ev._runtime.context_precision, evaluation.ParallelContextPrecision)
    assert "falls back to the sequential stock" in caplog.text
    assert scores.status == "done" and scores.context_precision == 1.0
    assert scores.context_verdicts == []                # the stock class exposes no per-page verdicts


async def test_parallel_context_precision_is_used_when_internals_match(make_evaluator):
    ev = make_evaluator()
    await ev.evaluate(QUESTION, ANSWER, PAGES)

    assert isinstance(ev._runtime.context_precision, evaluation.ParallelContextPrecision)


async def test_parallel_context_precision_matches_the_stock_metric(make_evaluator, stub):
    ev = make_evaluator()
    await ev.evaluate(QUESTION, ANSWER, PAGES)            # builds the runtime
    parallel = ev._runtime.context_precision
    texts = [p.text for p in PAGES]
    stock = evaluation.ContextPrecisionWithoutReference(llm=parallel.llm)

    detailed, rows = await parallel.ascore_detailed(QUESTION, ANSWER, texts)
    reference = await stock.ascore(user_input=QUESTION, response=ANSWER, retrieved_contexts=texts)

    assert detailed == pytest.approx(reference.value) and [r["verdict"] for r in rows] == [1, 1, 0]


async def test_a_failing_context_judge_cancels_its_siblings(make_evaluator, stub):
    stub.fail["context_precision"] = 500
    stub.latency = 0.05
    many = [ContextPage(page=p, text=f"Operating profit was 4,500 million pounds on page {p}.") for p in range(1, 13)]

    scores = await make_evaluator().evaluate(QUESTION, ANSWER, many)

    assert scores.status == "partial" and "context_precision" in scores.errors


# ------------------------------------------------------------------------------------------------ UI metadata
def test_metric_info_is_complete_and_json_safe():
    assert set(METRIC_INFO) == {*METRICS, "_general"}
    for key, info in METRIC_INFO.items():
        assert {"label", "short", "tooltip"} <= set(info) and all(isinstance(v, str) and v for v in info.values()), key
    assert all("Higher is better" in METRIC_INFO[m]["direction"] for m in METRICS)
    json.dumps(METRIC_INFO)


def test_ragas_telemetry_is_disabled():
    import os
    assert os.environ["RAGAS_DO_NOT_TRACK"] == "true"
