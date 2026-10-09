"""Offline tests for the question set ("Run all"): `ReportLensService.ask_batch`, `POST /api/sessions/{sid}/batch`, the settings
behind it, the limiter's N-token hit and the static demo that has no PDF.

Service level: a real service over fakes (tests/test_service.py) whose QA / evaluator record how many runs overlap.
Web level: the SSE framing with the real service, and the route's guards with an in-process fake service."""
from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest

from reportlens import config as config_module
from reportlens.config import DEFAULT_QUESTIONS, PACKAGED_DEMO_DIR, load_settings, settings_from_json, settings_to_json
from reportlens.demo import DEMO_SESSION_ID, export_session, install_demo
from reportlens.limits import QUESTION_RESERVE_USD, SlidingWindowLimiter
from reportlens.lowmem import INDEXING_ACTIVE
from reportlens.models import ContextPage, EvalScores, Message, ServiceError
from reportlens.service import MAX_QUESTION_CHARS, clean_questions
from reportlens.store import DEMO_OWNER
from reportlens.web.app import create_app
from tests.test_service import (  # noqa: F401 - fixtures
    METRICS,
    FakeEvaluator,
    FakeQA,
    FakeQAError,
    build_env,
    collect,
    env,
    error_of,
    names,
    wait_flag,
    wait_until,
)
from tests.test_web import FakeService, error_of as http_error, parse_sse, standard_script

QUESTIONS = ["What is the status of GHG reduction technology?", "Has the company earmarked capex?", "How prepared is the company?",
             "What are the primary sources of operating cash flows?", "What is the management outlook?"]
PER_QUESTION = ["step", "step_done", "token", "citation", "token", "answer_done", "eval_started", "eval_result", "eval_result", "eval_result", "eval_done"]


# ----------------------------------------------------------------------------------------------- doubles that measure overlap
class BatchQA(FakeQA):
    """FakeQA that records how many runs are alive at once, takes `work_s` per question, and can fail or hold chosen questions."""

    def __init__(self, fact: dict):
        super().__init__(fact)
        self.running = 0
        self.peak = 0
        self.work_s = 0.05
        self.started_order: list[str] = []
        self.fail_on: dict[str, Exception] = {}
        self.hold: dict[str, threading.Event] = {}
        self._mutex = threading.Lock()

    def ask(self, *, session_id, doc, question, history, ctx, cancel=None):
        with self._mutex:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.started_order.append(question)
        try:
            end = time.monotonic() + self.work_s
            while time.monotonic() < end:
                if cancel is not None and cancel.is_set():
                    return
                time.sleep(0.005)
            if question in self.fail_on:
                raise self.fail_on[question]
            gate = self.hold.get(question)
            while gate is not None and not gate.is_set():
                if cancel is not None and cancel.is_set():
                    return
                time.sleep(0.01)
            yield from super().ask(session_id=session_id, doc=doc, question=question, history=history, ctx=ctx, cancel=cancel)
        finally:
            with self._mutex:
                self.running -= 1


class TrackingEvaluator(FakeEvaluator):
    def __init__(self, delay: float = 0.05):
        super().__init__()
        self.running = 0
        self.peak = 0
        self.delay = delay
        self.log: list[tuple[str, float]] = []

    async def evaluate(self, question, answer, contexts, on_metric=None):
        self.running += 1
        self.peak = max(self.peak, self.running)
        self.log.append((question, time.monotonic()))
        try:
            await asyncio.sleep(self.delay)
            return await super().evaluate(question, answer, contexts, on_metric)
        finally:
            self.running -= 1


def instrumented(env, *, eval_delay: float = 0.05) -> tuple[BatchQA, TrackingEvaluator]:
    qa, evaluator = BatchQA(env.qa.fact), TrackingEvaluator(eval_delay)
    env.service._qa, env.service._evaluator = qa, evaluator
    env.qa, env.evaluator = qa, evaluator
    return qa, evaluator


def of_index(events: list[tuple[str, dict]], index: int) -> list[str]:
    return [n for n, p in events if p.get("index") == index]


def live_ask_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("ask-") and t.is_alive()]


# ----------------------------------------------------------------------------------------------- settings
def test_the_question_set_defaults_are_the_agreed_five():
    s = load_settings(environ={})
    assert s.default_questions == DEFAULT_QUESTIONS and len(DEFAULT_QUESTIONS) == 5
    assert DEFAULT_QUESTIONS[0] == "What is the status of GHG reduction technology available to the company?"
    assert DEFAULT_QUESTIONS[3] == "What are Primary Sources for operating cash flows ?"
    assert DEFAULT_QUESTIONS[4] == "What is Management Outlook ?"
    assert (s.max_batch_questions, s.batch_concurrency) == (10, 3)


def test_default_questions_come_from_lines_or_double_bars_without_blanks_or_duplicates():
    assert load_settings(environ={"DEFAULT_QUESTIONS": "One? || Two?\nThree?\r\n\n  Two?  ||"}).default_questions == ("One?", "Two?", "Three?")
    assert load_settings(environ={"DEFAULT_QUESTIONS": "A\\nB"}).default_questions == ("A", "B")        # a typed backslash-n is a line break too
    assert load_settings(environ={"DEFAULT_QUESTIONS": " \n || "}).default_questions == DEFAULT_QUESTIONS


def test_batch_concurrency_is_tiered_and_clamped():
    assert load_settings(environ={"LOW_MEMORY": "1"}).batch_concurrency == 2
    assert load_settings(environ={"LOW_MEMORY": "1", "BATCH_CONCURRENCY": "4"}).batch_concurrency == 4
    assert load_settings(environ={"BATCH_CONCURRENCY": "0"}).batch_concurrency == 1
    assert load_settings(environ={"BATCH_CONCURRENCY": "99"}).batch_concurrency == 6
    assert load_settings(environ={"BATCH_CONCURRENCY": "lots"}).batch_concurrency == 3
    assert load_settings(environ={"MAX_BATCH_QUESTIONS": "4"}).max_batch_questions == 4
    assert load_settings(environ={"MAX_BATCH_QUESTIONS": "0"}).max_batch_questions == 1
    assert load_settings(environ={"MAX_BATCH_QUESTIONS": "5000"}).max_batch_questions == config_module.MAX_BATCH_QUESTIONS_CAP


def test_the_question_set_is_public_and_survives_the_child_process_round_trip():
    s = load_settings(environ={"DEFAULT_QUESTIONS": "Q1?||Q2?", "MAX_BATCH_QUESTIONS": "7", "BATCH_CONCURRENCY": "2"})
    public = s.public()
    assert (public["default_questions"], public["max_batch_questions"], public["batch_concurrency"]) == (["Q1?", "Q2?"], 7, 2)
    back = settings_from_json(settings_to_json(s))
    assert back.default_questions == ("Q1?", "Q2?") and back.max_batch_questions == 7


# ----------------------------------------------------------------------------------------------- the limiter
class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_hit_many_is_all_or_nothing_and_reports_when_the_set_would_fit():
    clock = Clock()
    lim = SlidingWindowLimiter(5, 60.0, clock=clock)
    assert lim.hit_many("a", 3) == 0
    clock.now += 10
    assert lim.hit_many("a", 3) == 50                     # 2 left; the oldest 3 events (all at t=0) leave in 50 s
    assert lim.hit_many("a", 2) == 0                      # exactly what is left
    assert lim.hit("a") > 0                               # and now it is empty
    assert lim.hit_many("b", 5) == 0                      # other keys are independent
    assert lim.hit_many("c", 6) == 60                     # more than the whole allowance never fits
    assert lim.retry_after("c") == 0                      # ... and nothing was recorded for it


def test_refund_many_takes_back_the_latest_events():
    clock = Clock()
    lim = SlidingWindowLimiter(5, 60.0, clock=clock)
    lim.hit_many("a", 5)
    lim.refund_many("a", 3)
    assert lim.hit_many("a", 3) == 0 and lim.hit("a") > 0
    lim.refund_many("never-seen", 2)                      # no error
    assert SlidingWindowLimiter(0, 60.0).hit_many("a", 99) == 0


# ----------------------------------------------------------------------------------------------- cleaning the set
def test_clean_questions_trims_dedupes_and_validates():
    assert clean_questions(["  A?  ", "", "a?", "B?", "   ", "A?", "b? "], 10) == ["A?", "B?"]
    for bad, code in [([], "empty_question"), (["", "  "], "empty_question"), ([None, 3], "empty_question"),
                      (["x" * (MAX_QUESTION_CHARS + 1)], "question_too_long"), ([f"q{i}" for i in range(4)], "too_many_questions")]:
        with pytest.raises(ServiceError) as exc:
            clean_questions(bad, 3)
        assert exc.value.code == code and exc.value.status == 400
    assert clean_questions(["x" * MAX_QUESTION_CHARS], 1)
    assert len(clean_questions(["q1", "Q1", "q2", "Q2", "q3"], 3)) == 3          # the limit counts what is left after de-duplication


# ----------------------------------------------------------------------------------------------- the service
async def test_batch_rows_exist_up_front_in_order_and_events_are_indexed(build_env):
    env = build_env(batch_concurrency=2)
    qa, _ = instrumented(env)
    sid = env.ready_session()
    agen = env.service.ask_batch(sid, QUESTIONS[:4])
    first = await agen.__anext__()
    assert first[0] == "batch_start"
    items = first[1]["items"]
    assert first[1]["concurrency"] == 2 and [i["index"] for i in items] == [0, 1, 2, 3] and [i["question"] for i in items] == QUESTIONS[:4]
    stored = env.stored(sid)                                                # all eight rows are there before any answer
    assert [m.role for m in stored] == ["user", "assistant"] * 4 and [m.content for m in stored if m.role == "user"] == QUESTIONS[:4]
    assert [m.status for m in stored if m.role == "assistant"] == ["streaming"] * 4
    assert [i["message_id"] for i in items] == [m.id for m in stored if m.role == "assistant"]
    assert [Message.model_validate(i["user_message"]).id for i in items] == [m.id for m in stored if m.role == "user"]
    assert env.service.get_session(sid).title == QUESTIONS[0]

    events = [first] + [e async for e in agen]
    json.dumps(events)
    got = names(events)
    assert got[0] == "batch_start" and got[-1] == "done" and got.count("done") == 1 and got.count("batch_start") == 1
    assert dict(events)["batch_done"] == {"answered": 4, "failed": 0}
    assert got.index("batch_done") > max(i for i, (n, _) in enumerate(events) if n == "answer_done")     # after every answer
    for k in range(4):
        assert of_index(events, k) == PER_QUESTION, k                        # each question's own events keep their order
    for n, p in events:
        if n not in ("batch_start", "batch_done", "done"):
            assert isinstance(p["index"], int) and 0 <= p["index"] < 4
    by_mid = {i["message_id"]: i["index"] for i in items}
    for n, p in events:
        if n not in ("batch_start", "batch_done", "done", "answer_done"):
            assert by_mid[p["message_id"]] == p["index"]
        if n == "answer_done":
            assert by_mid[p["message"]["id"]] == p["index"]

    after = env.stored(sid)                                                  # persisted exactly like single questions
    assert [m.id for m in after] == [m.id for m in stored]
    for m, i in zip([m for m in after if m.role == "assistant"], range(4)):
        assert m.status == "answered" and m.content.startswith(QUESTIONS[i]) and m.citations and m.citations[0].rects
        assert m.usage.input_tokens == 10 and m.elapsed_ms == 34 and m.steps and m.evaluation.status == "done"
        assert [c.page for c in env.store.get_contexts(m.id)] == [2, env.qa.fact["page"]]
    assert all(c["history"] == [] for c in qa.calls)                         # independent: nothing from the chat is sent to the agent
    assert sorted(q for q, _ in env.evaluator.log) == sorted(QUESTIONS[:4])  # RAGAS user_input is the question itself
    assert not live_ask_threads()


async def test_a_later_single_question_sees_the_batch_as_history(env):
    sid = env.ready_session()
    await collect(env.service.ask_batch(sid, QUESTIONS[:2]))
    await collect(env.service.ask(sid, "and one more?"))
    history = env.qa.calls[-1]["history"]
    assert [h["content"] for h in history if h["role"] == "user"] == QUESTIONS[:2]


async def test_the_number_of_agent_runs_in_flight_never_exceeds_the_limit(build_env):
    for limit in (1, 2, 3):
        env = build_env(batch_concurrency=limit)
        qa, _ = instrumented(env)
        sid = env.ready_session()
        events = await collect(env.service.ask_batch(sid, [f"{q} #{limit}" for q in QUESTIONS]))
        assert qa.peak == limit, (limit, qa.peak)                            # reaches the limit, never beyond it
        assert dict(events)["batch_done"] == {"answered": 5, "failed": 0}
        asked = [f"{q} #{limit}" for q in QUESTIONS]
        assert set(qa.started_order[:limit]) == set(asked[:limit])           # the queue is first come, first served


async def test_questions_in_flight_hold_back_budget_for_other_work(build_env):
    env = build_env(batch_concurrency=2)
    qa, _ = instrumented(env)
    qa.hold = {q: threading.Event() for q in QUESTIONS[:3]}
    sid = env.ready_session()
    agen = env.service.ask_batch(sid, QUESTIONS[:3])
    await agen.__anext__()
    await wait_until(lambda: env.service._reserve() == 2 * QUESTION_RESERVE_USD)       # two run, the third waits for a slot
    for gate in qa.hold.values():
        gate.set()
    assert names([e async for e in agen])[-1] == "done"
    await wait_until(lambda: env.service._reserve() == 0.0)


async def test_scoring_goes_through_a_small_queue_and_starts_before_the_last_answer(build_env):
    env = build_env(batch_concurrency=3)
    qa, evaluator = instrumented(env, eval_delay=0.15)
    qa.hold = {QUESTIONS[4]: threading.Event()}                              # the last question keeps answering for a while
    sid = env.ready_session()
    agen = env.service.ask_batch(sid, QUESTIONS)
    seen: list[tuple[str, dict]] = []
    async for event in agen:
        seen.append(event)
        if event[0] == "eval_done":
            break
    assert "batch_done" not in names(seen)                                   # an early answer is already scored ...
    assert qa.running >= 1 and QUESTIONS[4] not in [q for q, _ in evaluator.log]   # ... while question 5 is still answering
    qa.hold[QUESTIONS[4]].set()
    seen += [e async for e in agen]
    assert evaluator.peak == 2                                               # two scorings at most (one in LOW_MEMORY)
    assert dict(seen)["batch_done"] == {"answered": 5, "failed": 0} and names(seen)[-1] == "done"


async def test_low_memory_scores_one_answer_at_a_time(build_env):
    env = build_env(batch_concurrency=3, low_memory=True)
    _, evaluator = instrumented(env, eval_delay=0.05)
    sid = env.ready_session()
    await collect(env.service.ask_batch(sid, QUESTIONS))
    assert evaluator.peak == 1 and len(evaluator.log) == 5


async def test_questions_run_one_at_a_time_while_a_report_is_being_indexed_on_a_small_host(build_env):
    env = build_env(batch_concurrency=3, low_memory=True)
    qa, _ = instrumented(env)
    sid = env.ready_session()
    INDEXING_ACTIVE.set()
    try:
        await collect(env.service.ask_batch(sid, QUESTIONS[:4]))
    finally:
        INDEXING_ACTIVE.clear()
    assert qa.peak == 1
    qa.peak = 0
    await collect(env.service.ask_batch(sid, [q + "?" for q in QUESTIONS[:4]]))      # indexing over: the full concurrency is back
    assert qa.peak == 3


async def test_one_failing_question_does_not_stop_the_others(env):
    qa, _ = instrumented(env)
    qa.fail_on = {QUESTIONS[1]: FakeQAError("openai_rate_limit", "Rate limit reached."), QUESTIONS[3]: RuntimeError("engine crashed")}
    sid = env.ready_session()
    events = await collect(env.service.ask_batch(sid, QUESTIONS))
    assert dict(events)["batch_done"] == {"answered": 3, "failed": 2} and names(events)[-1] == "done"
    mids = {i["index"]: i["message_id"] for i in dict(events)["batch_start"]["items"]}
    errors = {p["index"]: p for n, p in events if n == "error"}
    assert set(errors) == {1, 3} and errors[1]["code"] == "openai_rate_limit" and errors[1]["message_id"] == mids[1]
    assert errors[3]["code"] == "agent_failed" and "engine crashed" not in json.dumps(errors[3])
    for k in (1, 3):
        assert of_index(events, k) == ["error"]
    for k in (0, 2, 4):
        assert of_index(events, k) == PER_QUESTION
    rows = {m.id: m for m in env.stored(sid) if m.role == "assistant"}
    assert [rows[mids[k]].status for k in range(5)] == ["answered", "error", "answered", "error", "answered"]
    assert rows[mids[1]].error == "openai_rate_limit" and rows[mids[3]].error and "crashed" not in rows[mids[3]].error
    assert len(env.evaluator.calls) == 3


async def test_a_batch_where_nothing_could_be_answered_still_closes_cleanly(env):
    qa, _ = instrumented(env)
    qa.fail_on = {q: FakeQAError("openai_auth", "Key rejected.") for q in QUESTIONS[:2]}
    sid = env.ready_session()
    events = await collect(env.service.ask_batch(sid, QUESTIONS[:2]))
    assert dict(events)["batch_done"] == {"answered": 0, "failed": 2} and names(events)[-1] == "done" and env.evaluator.calls == []
    assert env.service.get_session(sid).state == "locked"                    # asked, even though nothing came back


async def test_closing_the_stream_cancels_every_run_and_marks_every_row(build_env):
    env = build_env(batch_concurrency=2)
    qa, _ = instrumented(env)
    qa.hold = {q: threading.Event() for q in QUESTIONS}                      # nothing ever finishes by itself
    sid = env.ready_session()
    agen = env.service.ask_batch(sid, QUESTIONS)
    await agen.__anext__()                                                   # batch_start
    await wait_until(lambda: qa.running == 2)                                # two run, three wait for a slot
    await agen.aclose()                                                      # what the web layer does when the client goes away
    assert not live_ask_threads()
    rows = [m for m in env.stored(sid) if m.role == "assistant"]
    assert len(rows) == 5 and all(m.status == "error" and m.error == "cancelled" for m in rows)
    assert set(qa.started_order) == set(QUESTIONS[:2]) and env.evaluator.calls == []   # the queued ones never started
    assert env.service._runs == {} and env.service._reserve() == 0.0
    qa.hold = {}
    assert names(await collect(env.service.ask_batch(sid, ["again?"])))[-1] == "done"     # the guard is free again


async def test_cancelling_the_consuming_task_cancels_the_batch(build_env):
    env = build_env(batch_concurrency=3)
    qa, _ = instrumented(env)
    qa.hold = {q: threading.Event() for q in QUESTIONS[:3]}
    sid = env.ready_session()

    async def consume() -> None:
        async for _ in env.service.ask_batch(sid, QUESTIONS[:3]):
            pass

    task = asyncio.create_task(consume())
    await wait_until(lambda: qa.running == 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_until(lambda: all(m.error == "cancelled" for m in env.stored(sid) if m.role == "assistant"))
    await wait_until(lambda: not live_ask_threads())


async def test_scoring_of_finished_answers_survives_a_disconnect(build_env):
    env = build_env(batch_concurrency=1)
    qa, evaluator = instrumented(env, eval_delay=0.2)
    qa.hold = {QUESTIONS[1]: threading.Event()}
    sid = env.ready_session()
    agen = env.service.ask_batch(sid, QUESTIONS[:2])
    async for event in agen:
        if event[0] == "answer_done":
            break
    await agen.aclose()                                                      # question 2 is mid-run, question 1 is being scored
    rows = [m for m in env.stored(sid) if m.role == "assistant"]
    assert rows[0].status == "answered" and rows[1].status == "error" and rows[1].error == "cancelled"
    await wait_until(lambda: env.stored(sid)[1].evaluation.status == "done")  # the scoring task is independent of the stream


async def test_the_budget_stops_a_batch_cleanly_when_it_runs_out(build_env):
    env = build_env(budget_usd_total=1.5, batch_concurrency=1)
    qa, _ = instrumented(env)
    sid = env.ready_session()                                                # 0.40 for the index; every answer counts 0.50 + 0.08 scoring
    events = await collect(env.service.ask_batch(sid, QUESTIONS))
    assert dict(events)["batch_done"] == {"answered": 2, "failed": 3} and names(events)[-1] == "done"
    errors = {p["index"]: p for n, p in events if n == "error"}
    assert set(errors) == {2, 3, 4} and all(e["code"] == "budget_exhausted" for e in errors.values())
    assert qa.started_order == QUESTIONS[:2]                                 # the engine never started for the refused ones
    rows = [m for m in env.stored(sid) if m.role == "assistant"]
    assert [m.status for m in rows] == ["answered", "answered", "error", "error", "error"]
    assert all(m.content == "" and m.error for m in rows[2:])


async def test_a_spent_budget_refuses_the_whole_set_before_any_row_is_written(build_env):
    env = build_env(budget_usd_total=0.3)
    sid = env.ready_session()
    with pytest.raises(ServiceError) as exc:
        await collect(env.service.ask_batch(sid, QUESTIONS))
    assert error_of(exc) == ("budget_exhausted", 402) and env.stored(sid) == []


async def test_validation_and_state_errors_come_before_any_event_or_row(build_env):
    env = build_env(max_batch_questions=3)
    sid = env.ready_session()
    for questions, expected in [([], ("empty_question", 400)), (["  ", ""], ("empty_question", 400)),
                                (QUESTIONS[:4], ("too_many_questions", 400)), (["x" * 4001], ("question_too_long", 400))]:
        with pytest.raises(ServiceError) as exc:
            await collect(env.service.ask_batch(sid, questions))
        assert error_of(exc) == expected
    with pytest.raises(ServiceError) as exc:
        await collect(env.service.ask_batch("0" * 32, ["q?"]))
    assert error_of(exc) == ("session_not_found", 404)
    indexing = env.service.create_session().id
    env.upload(indexing)
    with pytest.raises(ServiceError) as exc:
        await collect(env.service.ask_batch(indexing, ["q?"]))
    assert error_of(exc) == ("document_not_ready", 409)
    empty = env.service.create_session().id
    with pytest.raises(ServiceError) as exc:
        await collect(env.service.ask_batch(empty, ["q?"]))
    assert error_of(exc) == ("document_not_ready", 409)
    assert env.stored(sid) == [] and env.qa.calls == []
    dup = await collect(env.service.ask_batch(sid, ["Same?", "same?", " SAME? ", "Other?"]))                # de-duplicated before it is counted
    assert [i["question"] for i in dict(dup)["batch_start"]["items"]] == ["Same?", "Other?"]


async def test_without_a_key_the_set_is_refused_with_503(build_env):
    env = build_env(openai_api_key=None)
    sid = env.ready_session()
    with pytest.raises(ServiceError) as exc:
        await collect(env.service.ask_batch(sid, ["q?"]))
    assert error_of(exc) == ("openai_not_configured", 503)


async def test_only_one_question_or_set_runs_per_session(build_env):
    env = build_env(batch_concurrency=2)
    qa, _ = instrumented(env)
    qa.hold = {q: threading.Event() for q in QUESTIONS[:2]}
    sid = env.ready_session()
    batch = env.service.ask_batch(sid, QUESTIONS[:2])
    await batch.__anext__()
    for attempt in (env.service.ask(sid, "single?"), env.service.ask_batch(sid, ["other?"])):
        with pytest.raises(ServiceError) as exc:
            await attempt.__anext__()
        assert error_of(exc) == ("session_busy", 409)
    assert len(env.stored(sid)) == 4                                         # the refused ones left no rows
    for gate in qa.hold.values():
        gate.set()
    assert names([e async for e in batch])[-1] == "done"
    other = env.ready_session()                                              # another chat is unaffected by the guard
    assert names(await collect(env.service.ask_batch(other, ["fine?"])))[-1] == "done"
    single = env.service.ask(sid, "now?")
    await single.__anext__()                                                 # and a single question blocks a set the same way
    with pytest.raises(ServiceError) as exc:
        await env.service.ask_batch(sid, ["blocked?"]).__anext__()
    assert error_of(exc) == ("session_busy", 409)
    await single.aclose()


async def test_the_demo_chat_refuses_sets_like_questions(build_env):
    env = build_env()
    env.store.create_session("Demo", owner=DEMO_OWNER, sid=DEMO_SESSION_ID)
    with pytest.raises(ServiceError) as exc:
        await collect(env.service.ask_batch(DEMO_SESSION_ID, ["q?"]))
    assert error_of(exc) == ("demo_read_only", 403)


async def test_a_visitors_own_key_skips_the_budget_and_is_recorded_on_the_rows(build_env):
    env = build_env(budget_usd_total=0.01)
    sid = env.ready_session()
    visitor = env.settings.with_(key_source="visitor", openai_api_key="sk-visitor-test")
    events = await collect(env.service.ask_batch(sid, QUESTIONS[:2], visitor))
    assert dict(events)["batch_done"] == {"answered": 2, "failed": 0}
    assert [m.key_source for m in env.stored(sid) if m.role == "assistant"] == ["visitor", "visitor"]


async def test_the_engine_sees_one_shared_open_document_per_session(build_env):
    env = build_env(batch_concurrency=3)
    qa, _ = instrumented(env)
    sid = env.ready_session()
    await collect(env.service.ask_batch(sid, QUESTIONS))
    assert len({id(c["ctx"].pdf) for c in qa.calls}) == 1                    # one PdfiumDoc for all five questions
    assert env.service._cache[sid].users == 0                                # and every lease came back


# ----------------------------------------------------------------------------------------------- over HTTP (real service)
@pytest.fixture
def web(build_env):
    env = build_env(batch_concurrency=2)
    instrumented(env)
    return env, create_app(env.settings, env.service)


def http_client(app, *, ip: str = "203.0.113.9") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 4321)), base_url="http://testserver")


async def test_batch_route_streams_the_documented_frames(web):
    env, app = web
    sid = env.ready_session()
    async with http_client(app) as c:
        r = await c.post(f"/api/sessions/{sid}/batch", json={"questions": QUESTIONS[:3]})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        assert r.text.count("event: batch_start\n") == 1 and r.text.endswith("event: done\ndata: {}\n\n")
        events = parse_sse(r.text)
        assert events[0][0] == "batch_start" and events[-1] == ("done", {})
        assert dict(events)["batch_done"] == {"answered": 3, "failed": 0}
        for k in range(3):
            assert of_index(events, k) == PER_QUESTION
        assert [i["question"] for i in events[0][1]["items"]] == QUESTIONS[:3]
        detail = (await c.get(f"/api/sessions/{sid}")).json()
        assert [m["content"] for m in detail["messages"] if m["role"] == "user"] == QUESTIONS[:3]
        assert all(m["evaluation"]["status"] == "done" for m in detail["messages"] if m["role"] == "assistant")


async def test_batch_route_validation_is_json_before_the_stream(web):
    env, app = web
    sid = env.ready_session()
    async with http_client(app) as c:
        for body, code in [({"questions": []}, "empty_question"), ({"questions": ["", "  "]}, "empty_question"),
                           ({"questions": [f"q{i}" for i in range(11)]}, "too_many_questions"),
                           ({"questions": ["x" * 4001]}, "question_too_long")]:
            r = await c.post(f"/api/sessions/{sid}/batch", json=body)
            assert r.status_code == 400 and http_error(r)["code"] == code, (body, r.text)
            assert r.headers["content-type"].startswith("application/json")
        assert (await c.post(f"/api/sessions/{sid}/batch", json={})).status_code == 400            # missing field
        assert (await c.post(f"/api/sessions/{sid}/batch", json={"questions": "one?"})).status_code == 400
        assert (await c.post(f"/api/sessions/{'0' * 32}/batch", json={"questions": ["q?"]})).status_code == 404
        assert (await c.post(f"/api/sessions/nope/batch", json={"questions": ["q?"]})).status_code == 404
        empty = env.service.create_session().id
        r = await c.post(f"/api/sessions/{empty}/batch", json={"questions": ["q?"]})
        assert r.status_code == 409 and http_error(r)["code"] == "document_not_ready"
    assert env.stored(sid) == []


async def test_batch_route_budget_and_busy_errors_are_json(build_env):
    env = build_env(budget_usd_total=0.3)
    app = create_app(env.settings, env.service)
    sid = env.ready_session()
    async with http_client(app) as c:
        r = await c.post(f"/api/sessions/{sid}/batch", json={"questions": ["q?"]})
        assert r.status_code == 402 and http_error(r)["code"] == "budget_exhausted"
    free = build_env()
    qa, _ = instrumented(free)
    qa.hold = {"slow?": threading.Event()}
    app = create_app(free.settings, free.service)
    sid = free.ready_session()
    first = free.service.ask_batch(sid, ["slow?"])
    await first.__anext__()
    async with http_client(app) as c:
        r = await c.post(f"/api/sessions/{sid}/batch", json={"questions": ["other?"]})
        assert r.status_code == 409 and http_error(r)["code"] == "session_busy"
    qa.hold["slow?"].set()
    assert names([e async for e in first])[-1] == "done"


async def test_the_demo_is_refused_over_http_with_403(build_env):
    env = build_env()
    env.store.create_session("Demo", owner=DEMO_OWNER, sid=DEMO_SESSION_ID)
    app = create_app(env.settings, env.service)
    async with http_client(app) as c:
        r = await c.post(f"/api/sessions/{DEMO_SESSION_ID}/batch", json={"questions": ["q?"]})
        assert r.status_code == 403 and http_error(r)["code"] == "demo_read_only"


async def test_batch_route_takes_one_allowance_slot_per_question(build_env):
    env = build_env(questions_per_hour_per_ip=7)
    app = create_app(env.settings, env.service)
    sid = env.ready_session()
    async with http_client(app) as c:
        assert (await c.post(f"/api/sessions/{sid}/batch", json={"questions": QUESTIONS[:5]})).status_code == 200      # 5 of 7 used
        again = await c.post(f"/api/sessions/{sid}/batch", json={"questions": ["a?", "b?", "c?"]})                       # needs 3, 2 left
        assert again.status_code == 429 and http_error(again)["code"] == "rate_limited" and int(again.headers["retry-after"]) > 0
        assert "3 questions" in http_error(again)["message"] and len(env.stored(sid)) == 10                             # nothing of it ran
        ok = await c.post(f"/api/sessions/{sid}/batch", json={"questions": ["a?", "b?"]})                                # exactly what is left
        assert ok.status_code == 200
        assert (await c.post(f"/api/sessions/{sid}/messages", json={"content": "one more?"})).status_code == 429
    async with http_client(app, ip="198.51.100.7") as other:                                                             # another visitor
        sid2 = env.ready_session()
        assert (await other.post(f"/api/sessions/{sid2}/batch", json={"questions": QUESTIONS[:5]})).status_code == 200


async def test_a_refused_set_gives_its_allowance_back(build_env):
    env = build_env(questions_per_hour_per_ip=4)
    app = create_app(env.settings, env.service)
    empty = env.service.create_session().id
    sid = env.ready_session()
    async with http_client(app) as c:
        for _ in range(3):
            r = await c.post(f"/api/sessions/{empty}/batch", json={"questions": ["a?", "b?", "c?"]})
            assert r.status_code == 409                                      # refused before it cost anything: never a 429
        assert (await c.post(f"/api/sessions/{sid}/batch", json={"questions": ["a?", "b?", "c?", "d?"]})).status_code == 200


async def test_config_exposes_the_question_set(web):
    env, app = web
    async with http_client(app) as c:
        body = (await c.get("/api/config")).json()
    assert body["default_questions"] == list(DEFAULT_QUESTIONS) and body["max_batch_questions"] == 10 and body["batch_concurrency"] == 2


# ----------------------------------------------------------------------------------------------- the route with a fake service
class BatchFake(FakeService):
    """FakeService that can also stream a set."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.batch_calls: list[tuple] = []
        self.batch_script: list[tuple[str, object]] = []

    async def ask_batch(self, sid: str, questions: list[str]):
        self.batch_calls.append((sid, questions))
        session = self._get(sid)
        if session.state not in ("ready", "locked"):
            raise ServiceError("document_not_ready", "The document is still being indexed.", 409)
        if self.busy:
            raise ServiceError("session_busy", "Questions are still being answered.", 409)
        for name, payload in self.batch_script:
            yield name, payload


@pytest.fixture
def fake(tmp_path, sample_pdf) -> BatchFake:
    workdir = tmp_path / "svc"
    workdir.mkdir()
    return BatchFake(workdir, sample_pdf)


async def test_the_route_deduplicates_before_calling_the_service_and_frames_events(settings, fake):
    sid = fake.add_session("ready").id
    script = standard_script(sid)
    items = [{"index": 0, "question": "A?", "user_message": script[0][1]["user_message"], "message_id": script[0][1]["message_id"]}]
    fake.batch_script = [("batch_start", {"items": items, "concurrency": 2}),
                         *[(n, {**p, "index": 0}) for n, p in script[1:-1]],
                         ("batch_done", {"answered": 1, "failed": 0}), ("done", {})]
    async with http_client(create_app(settings, fake)) as c:
        r = await c.post(f"/api/sessions/{sid}/batch", json={"questions": [" A? ", "a?", ""]})
    assert r.status_code == 200 and fake.batch_calls == [(sid, ["A?"])]
    events = parse_sse(r.text)
    assert [n for n, _ in events] == ["batch_start", *[n for n, _ in script[1:-1]], "batch_done", "done"]
    assert all(p.get("index") == 0 for n, p in events if n not in ("batch_start", "batch_done", "done"))


# ----------------------------------------------------------------------------------------------- the static demo (no PDF)
@pytest.fixture
def static_demo(build_env):
    env = build_env(demo_dir=PACKAGED_DEMO_DIR)
    info = install_demo(env.settings, env.store)
    assert info is not None
    return env, info


def test_the_packaged_demo_is_a_valid_static_chat_with_the_five_answers(static_demo):
    env, info = static_demo
    assert info.has_document is False and info.questions == 5 and info.public()["has_document"] is False
    messages = env.store.list_messages(DEMO_SESSION_ID)
    questions = [m.content for m in messages if m.role == "user"]
    assert len(questions) == 5 and len({q.casefold() for q in questions}) == 5
    wanted = {q.strip().casefold().rstrip(" ?") for q in DEFAULT_QUESTIONS}
    assert {q.strip().casefold().rstrip(" ?") for q in questions} == wanted
    answers = [m for m in messages if m.role == "assistant"]
    assert len(answers) == 5 and all(m.status == "answered" and m.content.strip() and m.citations for m in answers)
    for m in answers:
        Message.model_validate(m.model_dump(mode="json"))
        assert m.evaluation is not None and m.evaluation.status == "done"
        assert all(c.page >= 1 for c in m.citations)
    assert not env.settings.session_dir(DEMO_SESSION_ID).exists() or not list(env.settings.session_dir(DEMO_SESSION_ID).glob("*.pdf"))


async def test_a_static_demo_answers_every_document_route_with_a_clean_404(static_demo):
    env, info = static_demo
    app = create_app(env.settings, env.service)
    app.state.demo = info
    base = f"/api/sessions/{DEMO_SESSION_ID}"
    async with http_client(app) as c:
        assert (await c.get("/api/demo")).json()["has_document"] is False
        assert (await c.get(base)).status_code == 200                                          # the transcript itself is readable
        for path in ("/document/file", "/document/pages", "/document/outline", "/locate?page=1&quote=Grid", "/locate?page=3&claim=x"):
            r = await c.get(base + path)
            assert r.status_code == 404 and http_error(r)["code"] == "document_not_found", (path, r.status_code, r.text)
        head = await c.head(base + "/document/file")
        assert head.status_code == 404
        clone = await c.post("/api/sessions", json={"from_session": DEMO_SESSION_ID})        # "ask your own question" has nothing to copy
        assert clone.status_code == 409 and http_error(clone)["code"] == "document_not_available"
        assert (await c.post(f"{base}/batch", json={"questions": ["q?"]})).status_code == 403


def test_the_packaged_demo_is_installed_when_the_project_has_no_demo_of_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)                              # an empty project: no .env, no demo/
    monkeypatch.delenv("DEMO_DIR", raising=False)
    (tmp_path / "demo").mkdir()
    assert load_settings().demo_dir == PACKAGED_DEMO_DIR                                      # demo/ holds no chat.json
    (tmp_path / "demo" / "chat.json").write_text("{}", encoding="utf-8")
    assert load_settings().demo_dir == tmp_path / "demo"                                      # a demo of the owner's wins
    monkeypatch.setenv("DEMO_DIR", "")
    assert load_settings().demo_dir is None                                                   # empty = no demo at all


async def test_an_exported_static_demo_round_trips(build_env, tmp_path):
    source = build_env(data_dir=tmp_path / "source-data")
    sid = source.ready_session()
    await collect(source.service.ask(sid, "How many customers?"))
    out = export_session(source.store, source.settings, sid, tmp_path / "static", title="Static", with_files=False)
    assert (out / "chat.json").is_file() and not (out / "files").exists()
    target = build_env(demo_dir=out)
    info = install_demo(target.settings, target.store)
    assert info is not None and info.has_document is False and info.questions == 1


# ----------------------------------------------------------------------------------------------- the scoring child kept for the next answer
class WarmAwareEvaluator(TrackingEvaluator):
    """Takes `keep_warm` like ChildEvaluator does; records what it would have answered when the scoring finished."""

    def __init__(self, delay: float = 0.02):
        super().__init__(delay)
        self.warm_after: list[bool] = []
        self.kwargs_seen: list[bool] = []

    async def evaluate(self, question, answer, contexts, on_metric=None, *, keep_warm=False):
        self.kwargs_seen.append(keep_warm is not False)
        scores = await super().evaluate(question, answer, contexts, on_metric)
        self.warm_after.append(bool(keep_warm() if callable(keep_warm) else keep_warm))
        return scores


async def test_a_set_asks_the_scoring_child_to_stay_for_the_next_answer_until_the_last(build_env):
    env = build_env(batch_concurrency=1)
    qa, _ = instrumented(env)
    evaluator = env.service._evaluator = env.evaluator = WarmAwareEvaluator()
    sid = env.ready_session()
    await collect(env.service.ask_batch(sid, QUESTIONS[:3]))
    assert evaluator.kwargs_seen == [True, True, True]
    assert evaluator.warm_after == [True, True, False]                        # the third answer is the last: nothing more is coming
    assert env.service._gates.unfinished == 0
    evaluator.warm_after.clear()
    await collect(env.service.ask(sid, "a single question"))                  # an ordinary question never asks for it
    assert evaluator.kwargs_seen[-1] is False and evaluator.warm_after == [False]


async def test_a_failed_question_stops_counting_as_more_coming(build_env):
    env = build_env(batch_concurrency=1)
    qa, _ = instrumented(env)
    qa.fail_on = {QUESTIONS[1]: FakeQAError("agent_failed", "boom")}
    evaluator = env.service._evaluator = env.evaluator = WarmAwareEvaluator(delay=0.5)      # still scoring when the second question has failed
    sid = env.ready_session()
    await collect(env.service.ask_batch(sid, QUESTIONS[:2]))
    assert evaluator.warm_after == [False]
    assert env.service._gates.unfinished == 0


async def test_a_cancelled_set_leaves_no_count_behind(build_env):
    env = build_env(batch_concurrency=1)
    qa, _ = instrumented(env)
    qa.hold = {q: threading.Event() for q in QUESTIONS[:3]}
    sid = env.ready_session()
    agen = env.service.ask_batch(sid, QUESTIONS[:3])
    await agen.__anext__()
    await agen.aclose()
    await wait_until(lambda: env.service._gates.unfinished == 0)
