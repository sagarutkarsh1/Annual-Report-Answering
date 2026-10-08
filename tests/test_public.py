"""Offline tests for the public-deployment features: settings, access gate, spend budget, rate limits, host/origin guard and the
production entry point.  Everything here is off by default; the existing suites cover that default behaviour."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from reportlens import __main__ as entry
from reportlens import limits
from reportlens.config import Settings, load_settings
from reportlens.limits import (
    ANSWER_FALLBACK_USD,
    BUDGET_EXHAUSTED_MESSAGE,
    FAILED_ANSWER_FALLBACK_USD,
    QUESTION_RESERVE_USD,
    SlidingWindowLimiter,
    UsageBudget,
)
from reportlens.models import DocumentInfo, EvalScores, Message, ServiceError, Usage
from reportlens.store import Store, new_id
from reportlens.web import app as app_module
from reportlens.web.app import STATIC_DIR, allowed_hosts, create_app
from reportlens.web.auth import COOKIE_NAME, SESSION_TTL_S, AccessGate, client_ip
from tests.test_service import build_env, collect, env, error_of, payload  # noqa: F401 - fixtures
from tests.test_web import PDF_BYTES, FakeService, error_of as http_error, parse_sse, standard_script  # noqa: F401

CODE = "open sesame 42"


# ============================================================================================ settings
def test_defaults_without_public_mode_are_the_local_behaviour():
    s = load_settings(environ={})
    assert (s.public_mode, s.access_code, s.session_secret) == (False, None, None)
    assert (s.budget_usd_total, s.max_sessions, s.questions_per_hour_per_ip) == (0.0, 0, 0)
    assert (s.max_upload_mb, s.max_pages, s.index_fallback_standard, s.eval_max_contexts) == (100, 1200, True, 12)
    assert (s.index_cost_estimate_usd, s.eval_cost_estimate_usd) == (0.40, 0.08)
    assert s.allowed_hosts == () and s.trust_proxy is False and s.proxy_hops == 0 and s.port == 8000


def test_public_mode_fills_in_safe_defaults():
    s = load_settings(environ={"PUBLIC_MODE": "1"})
    assert s.public_mode and (s.budget_usd_total, s.max_sessions, s.questions_per_hour_per_ip) == (10.0, 30, 15)
    assert (s.max_upload_mb, s.max_pages, s.index_fallback_standard, s.eval_max_contexts) == (25, 400, False, 8)


def test_explicit_values_beat_the_public_defaults_even_zero():
    s = load_settings(environ={"PUBLIC_MODE": "true", "BUDGET_USD_TOTAL": "0", "MAX_SESSIONS": "5", "MAX_UPLOAD_MB": "60",
                               "PI_INDEX_FALLBACK_STANDARD": "true", "EVAL_MAX_CONTEXTS": "3", "QUESTIONS_PER_HOUR_PER_IP": "0"})
    assert (s.budget_usd_total, s.max_sessions, s.max_upload_mb, s.questions_per_hour_per_ip) == (0.0, 5, 60, 0)
    assert s.index_fallback_standard is True and s.eval_max_contexts == 3


def test_deployment_settings_are_parsed_and_forgiving():
    s = load_settings(environ={"ACCESS_CODE": "  abc  ", "SESSION_SECRET": " s ", "ALLOWED_HOSTS": " .HF.space , .onrender.com,,x.example ",
                               "TRUST_PROXY": "1", "PROXY_HOPS": "2", "BUDGET_USD_TOTAL": "oops", "MAX_SESSIONS": "-4",
                               "INDEX_COST_ESTIMATE_USD": "nan", "EVAL_COST_ESTIMATE_USD": "0.5"})
    assert s.access_code == "abc" and s.session_secret == "s"
    assert s.allowed_hosts == (".hf.space", ".onrender.com", "x.example") and s.trust_proxy and s.proxy_hops == 2
    assert s.budget_usd_total == 0.0 and s.max_sessions == 0 and s.index_cost_estimate_usd == 0.40 and s.eval_cost_estimate_usd == 0.5
    assert load_settings(environ={"ACCESS_CODE": "   "}).access_code is None


def test_port_comes_from_reportlens_port_then_port():
    assert load_settings(environ={"PORT": "7860"}).port == 7860
    assert load_settings(environ={"PORT": "10000", "REPORTLENS_PORT": "9000"}).port == 9000
    assert load_settings(environ={"PORT": "not-a-number"}).port == 8000


def test_public_config_never_contains_the_access_code_or_keys():
    s = load_settings(environ={"ACCESS_CODE": CODE, "SESSION_SECRET": "zzz-secret", "OPENAI_API_KEY": "sk-test-1234567"})
    blob = json.dumps(s.public())
    assert CODE not in blob and "zzz-secret" not in blob and "sk-test" not in blob and "access_code" not in blob


# ============================================================================================ rate limiter
class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_sliding_window_allows_n_per_window_and_reports_retry_after():
    clock = Clock()
    lim = SlidingWindowLimiter(3, 60.0, clock=clock)
    assert [lim.hit("a") for _ in range(3)] == [0, 0, 0]
    clock.now += 10
    assert lim.hit("a") == 50                                   # oldest event leaves the window in 50 s
    assert lim.hit("b") == 0                                    # other keys are independent
    clock.now += 50
    assert lim.hit("a") == 0                                    # one slot is free again


def test_sliding_window_refund_record_reset_and_unlimited():
    clock = Clock()
    lim = SlidingWindowLimiter(2, 60.0, clock=clock)
    lim.hit("a")
    lim.hit("a")
    lim.refund("a")
    assert lim.hit("a") == 0 and lim.hit("a") > 0
    lim.reset("a")
    assert lim.retry_after("a") == 0
    lim.record("a")
    lim.record("a")
    assert lim.retry_after("a") == 60
    off = SlidingWindowLimiter(0, 60.0, clock=clock)
    assert all(off.hit("a") == 0 for _ in range(100)) and off.retry_after("a") == 0
    off.refund("never-seen")                                     # no error


def test_sliding_window_stays_memory_bounded(monkeypatch):
    monkeypatch.setattr(SlidingWindowLimiter, "MAX_KEYS", 20)
    clock = Clock()
    lim = SlidingWindowLimiter(1, 60.0, clock=clock)
    for i in range(200):
        lim.hit(f"ip{i}")
        clock.now += 0.01
    assert len(lim._events) <= 21
    clock.now += 120
    for i in range(30):
        lim.hit(f"new{i}")
    assert len(lim._events) <= 31


# ============================================================================================ store snapshot + budget
def _doc(**kw) -> DocumentInfo:
    base = dict(id=new_id(), filename="a.pdf", doc_name="a.pdf", size_bytes=1, page_count=3, status="ready", stage="ready", progress=1.0)
    return DocumentInfo(**{**base, **kw})


def _answer(sid: str, *, cost=None, status="answered", usage=True, evaluation=None) -> Message:
    return Message(id=new_id(), session_id=sid, role="assistant", status=status,
                   usage=Usage(model="m", input_tokens=1, cost_usd=cost) if usage else None, evaluation=evaluation)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db" / "r.db")
    yield s
    s.close()


def test_usage_snapshot_counts_what_was_spent(store):
    a, b, c, d = (store.create_session().id for _ in range(4))
    store.put_document(a, _doc(pi_doc_id="pi-x"))
    store.put_document(b, _doc(pi_doc_id="pi-x"))                              # "new chat with this document": same index, paid once
    store.put_document(c, _doc(status="failed", progress=0.0))                  # failed before spending anything
    store.put_document(d, _doc(status="indexing", progress=0.0, pi_doc_id=None))
    store.add_message(Message(id=new_id(), session_id=a, role="user", content="q"))
    store.add_message(_answer(a, cost=0.2, evaluation=EvalScores(status="done")))
    store.add_message(_answer(a, cost=0.3, evaluation=EvalScores(status="skipped")))
    store.add_message(_answer(a, cost=None, evaluation=EvalScores(status="partial")))      # answered, price unknown
    store.add_message(_answer(a, cost=None, status="error", usage=False))                    # failed before any usage
    store.add_message(_answer(a, cost=None, status="streaming", usage=False))               # running: covered by the reserve
    snap = store.usage_snapshot()
    assert snap.answer_cost_usd == pytest.approx(0.5) and snap.unpriced_answers == 1 and snap.unpriced_failed == 1
    assert sorted(snap.index_keys) == sorted(["pi-x", f"doc:{store.get_document(d).id}"]) and snap.evaluations == 2
    only_a = store.usage_snapshot(a)
    assert only_a.index_keys == ("pi-x",) and only_a.answer_cost_usd == pytest.approx(0.5)
    assert store.usage_snapshot(exclude_index_keys={"pi-x"}).index_keys == (f"doc:{store.get_document(d).id}",)
    store.update_document(c, progress=0.4)
    assert len(store.usage_snapshot().index_keys) == 3                           # it got as far as spending: now it counts


def _budget(tmp_path, store, **changes) -> tuple[UsageBudget, Settings]:
    s = load_settings(environ={}).with_(data_dir=tmp_path / "data", budget_usd_total=10.0, **changes)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return UsageBudget(s, store), s


def test_budget_adds_answers_indexes_and_evaluations(tmp_path, store):
    budget, _ = _budget(tmp_path, store)
    sid = store.create_session().id
    store.put_document(sid, _doc(pi_doc_id="pi-1"))
    store.add_message(_answer(sid, cost=1.0, evaluation=EvalScores(status="done")))
    store.add_message(_answer(sid, cost=None))
    store.add_message(_answer(sid, status="error", usage=False))
    expected = 1.0 + ANSWER_FALLBACK_USD + FAILED_ANSWER_FALLBACK_USD + 0.40 + 0.08
    assert budget.spent_usd() == pytest.approx(expected)
    assert budget.status() == {"enabled": True, "used_fraction": round(expected / 10.0, 3)}
    budget.check()                                                                # well under: no error
    budget.check(reserve_usd=1.0)


def test_budget_refuses_at_the_limit_with_the_documented_error(tmp_path, store):
    budget, _ = _budget(tmp_path, store)
    sid = store.create_session().id
    store.add_message(_answer(sid, cost=9.9))
    budget.check()
    with pytest.raises(ServiceError) as exc:
        budget.check(reserve_usd=0.1)                                             # spent + reserve reaches the budget
    assert (exc.value.code, exc.value.status, exc.value.message) == ("budget_exhausted", 402, BUDGET_EXHAUSTED_MESSAGE)
    assert BUDGET_EXHAUSTED_MESSAGE == "The demo's usage budget has been used up. Please contact the owner."
    store.add_message(_answer(sid, cost=1.0))
    assert budget.status()["used_fraction"] == 1.0
    with pytest.raises(ServiceError):
        budget.check()


def test_budget_disabled_is_free_and_silent(tmp_path, store):
    s = load_settings(environ={}).with_(data_dir=tmp_path / "data")
    budget = UsageBudget(s, store)
    store.add_message(_answer(store.create_session().id, cost=1e6))
    budget.check(reserve_usd=1e9)
    budget.charge(5)
    assert budget.status() == {"enabled": False, "used_fraction": 0.0} and not (s.data_dir / limits.LEDGER_NAME).exists()


def test_deleting_a_chat_does_not_refund_the_budget(tmp_path, store):
    budget, s = _budget(tmp_path, store)
    sid = store.create_session().id
    store.put_document(sid, _doc(pi_doc_id="pi-1"))
    store.add_message(_answer(sid, cost=2.0, evaluation=EvalScores(status="done")))
    before = budget.spent_usd()
    budget.retire_session(sid)
    store.delete_session(sid)
    assert budget.spent_usd() == pytest.approx(before) == pytest.approx(2.48)
    again = UsageBudget(s, store)                                                  # a restart keeps the ledger
    assert again.spent_usd() == pytest.approx(2.48)
    ledger = json.loads((s.data_dir / limits.LEDGER_NAME).read_text(encoding="utf-8"))
    assert ledger["retired_keys"] == ["pi-1"] and ledger["retired_usd"] == pytest.approx(2.48)


def test_a_clone_of_a_deleted_chat_is_not_charged_for_the_index_twice(tmp_path, store):
    budget, _ = _budget(tmp_path, store)
    src, clone = store.create_session().id, store.create_session().id
    store.put_document(src, _doc(pi_doc_id="pi-1"))
    store.put_document(clone, _doc(pi_doc_id="pi-1"))
    assert budget.spent_usd() == pytest.approx(0.40)
    budget.retire_session(src)
    store.delete_session(src)
    assert budget.spent_usd() == pytest.approx(0.40)
    budget.retire_session(clone)
    store.delete_session(clone)
    assert budget.spent_usd() == pytest.approx(0.40)


def test_charge_and_retire_document_and_an_unreadable_ledger(tmp_path, store):
    budget, s = _budget(tmp_path, store)
    budget.charge(0.08)
    budget.retire_document("doc:1", charge_index=True)
    budget.retire_document("doc:1", charge_index=True)                             # idempotent
    budget.retire_document("doc:2", charge_index=False)
    assert budget.spent_usd() == pytest.approx(0.48)
    (s.data_dir / limits.LEDGER_NAME).write_text("{not json", encoding="utf-8")
    assert UsageBudget(s, store).spent_usd() == 0.0                                # starts from zero, does not crash


def test_ledger_write_failure_is_tolerated(tmp_path, store, monkeypatch):
    budget, _ = _budget(tmp_path, store)
    monkeypatch.setattr(limits.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    budget.charge(1.0)
    assert budget.spent_usd() == pytest.approx(1.0)


# ============================================================================================ the service
def ready(env) -> str:
    """A ready chat with an index of its own (the fake indexer gives every document the same PageIndex id)."""
    sid = env.ready_session()
    env.store.update_document(sid, pi_doc_id="pi-" + new_id())
    return sid


async def test_max_sessions_limits_chats_and_clones(build_env):
    env = build_env(max_sessions=2)
    svc = env.service
    a = svc.create_session()
    sid = env.ready_session()                                                      # second chat
    for call in (lambda: svc.create_session(), lambda: svc.create_session(sid)):
        with pytest.raises(ServiceError) as exc:
            call()
        assert error_of(exc) == ("session_limit", 429)
    svc.delete_session(a.id)
    assert svc.create_session().state == "empty"
    assert len(svc.list_sessions()) == 2


async def test_unlimited_sessions_by_default(env):
    for _ in range(40):
        env.service.create_session()
    assert len(env.service.list_sessions()) == 40


async def test_uploads_are_refused_with_402_once_the_budget_is_used_up(build_env):
    env = build_env(budget_usd_total=1.0)                                          # one index = 0.40
    for _ in range(3):
        ready(env)
    sid = env.service.create_session().id
    tmp = env.scratch / "late.pdf"
    shutil.copyfile(env.sample, tmp)
    started = len(env.indexer.started)
    with pytest.raises(ServiceError) as exc:
        env.service.attach_document(sid, "late.pdf", tmp)
    assert error_of(exc) == ("budget_exhausted", 402)
    assert tmp.exists() and len(env.indexer.started) == started and env.service.get_session(sid).document is None
    with pytest.raises(ServiceError):
        env.service.check_budget()
    assert env.service.usage_status() == {"enabled": True, "used_fraction": 1.0}
    assert len(env.service.list_sessions()) == 4 and env.service.get_session(sid).state == "empty"      # reads still work


async def test_questions_are_refused_before_the_engine_starts_when_the_budget_is_used_up(build_env):
    env = build_env(budget_usd_total=1.5)
    sid = env.ready_session()                                                      # 0.40
    await collect(env.service.ask(sid, "first"))                                   # + 0.50 (unpriced fake usage) + 0.08 scoring = 0.98
    await collect(env.service.ask(sid, "second"))                                  # allowed (0.98 < 1.5), then 1.56 >= 1.5
    calls = len(env.qa.calls)
    with pytest.raises(ServiceError) as exc:
        await collect(env.service.ask(sid, "third"))
    assert error_of(exc) == ("budget_exhausted", 402) and len(env.qa.calls) == calls
    assert [m.role for m in env.stored(sid)] == ["user", "assistant"] * 2          # nothing was written for the refused question
    assert env.service.get_session(sid).message_count == 4                         # the conversation is still readable


async def test_a_running_question_reserves_budget_for_the_next_one(build_env):
    import asyncio
    import threading

    env = build_env(budget_usd_total=1.3)
    sid = ready(env)                                                               # 0.40
    other = ready(env)                                                             # 0.80
    env.qa.gate = threading.Event()
    stream = env.service.ask(sid, "slow")
    first = await stream.__anext__()                                               # message_start: the run is registered
    assert first[0] == "message_start"
    # 0.80 spent + 0.60 held back for the running question >= 1.3
    with pytest.raises(ServiceError) as exc:
        await collect(env.service.ask(other, "parallel"))
    assert error_of(exc) == ("budget_exhausted", 402)
    env.qa.gate.set()
    async for _ in stream:
        pass
    await asyncio.sleep(0)


async def test_rerunning_a_score_is_billed_and_refused_when_the_budget_is_gone(build_env):
    env = build_env(budget_usd_total=1.2)
    sid = env.ready_session()                                                      # 0.40
    events = await collect(env.service.ask(sid, "q"))                              # + 0.50 + 0.08 = 0.98
    mid = payload(events, "message_start")["message_id"]
    await env.service.evaluate_message(sid, mid)                                   # a repeat: + 0.08 on the ledger = 1.06
    assert env.service._budget.spent_usd() == pytest.approx(1.06)
    for _ in range(4):
        try:
            await env.service.evaluate_message(sid, mid)
        except ServiceError as exc:
            assert (exc.code, exc.status) == ("budget_exhausted", 402)
            break
    else:
        pytest.fail("repeated scoring never hit the budget")


async def test_deleting_a_chat_keeps_its_spend_on_the_ledger(build_env):
    env = build_env(budget_usd_total=50.0)
    sid = env.ready_session()
    await collect(env.service.ask(sid, "q"))
    before = env.service._budget.spent_usd()
    assert before == pytest.approx(0.40 + ANSWER_FALLBACK_USD + 0.08)
    env.service.delete_session(sid)
    assert env.service.list_sessions() == []
    assert env.service._budget.spent_usd() == pytest.approx(before)
    assert (env.settings.data_dir / limits.LEDGER_NAME).is_file()


async def test_replacing_a_failed_upload_that_had_started_indexing_still_costs(build_env):
    env = build_env(budget_usd_total=50.0)
    sid = env.service.create_session().id
    env.upload(sid)
    env.indexer.fail(sid)
    env.store.update_document(sid, progress=0.6)
    assert env.service._budget.spent_usd() == pytest.approx(0.40)
    env.upload(sid)                                                                # the user tries again
    assert env.service._budget.spent_usd() == pytest.approx(0.80)                  # old attempt on the ledger + the new one
    env.indexer.fail(sid)                                                          # fails before spending anything (progress 0)
    env.upload(sid)
    assert env.service._budget.spent_usd() == pytest.approx(0.80)                  # ledger 0.40 (first failure) + the new attempt; the instant failure was free


async def test_cloning_does_not_charge_for_a_second_index(build_env, monkeypatch):
    env = build_env(budget_usd_total=50.0)
    sid = env.ready_session()
    pi_id = env.store.get_document(sid).pi_doc_id

    def same_id_cloner(settings, src, dst):
        shutil.copytree(settings.session_dir(src) / "pageindex", settings.session_dir(dst) / "pageindex")
        return pi_id

    monkeypatch.setattr(env.service, "_index_cloner", same_id_cloner)
    before = env.service._budget.spent_usd()
    env.service.create_session(sid)
    assert env.service._budget.spent_usd() == pytest.approx(before)


async def test_a_budgetless_service_never_touches_the_ledger(env):
    sid = env.ready_session()
    await collect(env.service.ask(sid, "q"))
    env.service.delete_session(sid)
    assert not (env.settings.data_dir / limits.LEDGER_NAME).exists()
    assert env.service.usage_status() == {"enabled": False, "used_fraction": 0.0}


# ============================================================================================ access gate (unit)
def test_gate_without_code_is_open():
    gate = AccessGate(load_settings(environ={}))
    assert not gate.required and gate.valid(None) and gate.valid("garbage")


def test_gate_code_comparison_and_cookie_roundtrip():
    now = [1_000_000.0]
    gate = AccessGate(load_settings(environ={"ACCESS_CODE": CODE, "SESSION_SECRET": "s1"}), clock=lambda: now[0])
    assert gate.required and gate.code_matches(CODE) and not gate.code_matches(CODE + "x") and not gate.code_matches("")
    assert not gate.code_matches("é" * 5000) and gate.code_matches(CODE)                       # long / non-ASCII input is safe
    token = gate.issue()
    assert gate.valid(token)
    now[0] += SESSION_TTL_S - 1
    assert gate.valid(token)
    now[0] += 2
    assert not gate.valid(token)                                                               # expired after 12 h
    assert SESSION_TTL_S == 12 * 3600


@pytest.mark.parametrize("mutate", [lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"), lambda t: "9" + t, lambda t: t.rsplit(".", 1)[0],
                                    lambda t: "", lambda t: "a.b.c", lambda t: t + ".extra", lambda t: "é.é.é"])
def test_gate_rejects_tampered_cookies(mutate):
    gate = AccessGate(load_settings(environ={"ACCESS_CODE": CODE}))
    assert not gate.valid(mutate(gate.issue())) and not gate.valid(None)


def test_cookies_do_not_survive_a_changed_code_or_secret_and_differ_per_process_without_a_secret():
    token = AccessGate(load_settings(environ={"ACCESS_CODE": CODE, "SESSION_SECRET": "s1"})).issue()
    assert AccessGate(load_settings(environ={"ACCESS_CODE": CODE, "SESSION_SECRET": "s1"})).valid(token)
    assert not AccessGate(load_settings(environ={"ACCESS_CODE": CODE + "!", "SESSION_SECRET": "s1"})).valid(token)
    assert not AccessGate(load_settings(environ={"ACCESS_CODE": CODE, "SESSION_SECRET": "s2"})).valid(token)
    random_a = AccessGate(load_settings(environ={"ACCESS_CODE": CODE}))
    assert not AccessGate(load_settings(environ={"ACCESS_CODE": CODE})).valid(random_a.issue())      # per-process secret
    assert random_a.valid(random_a.issue())


def test_cookie_attributes():
    gate = AccessGate(load_settings(environ={"ACCESS_CODE": CODE}))
    plain = gate.set_cookie_header("tok", secure=False)
    assert plain == f"{COOKIE_NAME}=tok; Path=/; Max-Age=43200; HttpOnly; SameSite=Lax"
    assert gate.set_cookie_header("tok", secure=True).endswith("; Secure")
    assert "Max-Age=0" in gate.set_cookie_header("", secure=False, max_age=0)


def _scope(headers: dict, client=("10.0.0.1", 5), scheme="http") -> dict:
    return {"type": "http", "scheme": scheme, "client": client, "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}


def test_client_ip_uses_the_socket_peer_unless_the_proxy_is_trusted():
    scope = _scope({"x-forwarded-for": "198.51.100.7, 10.1.1.1"})
    plain = load_settings(environ={})
    trusted = load_settings(environ={"TRUST_PROXY": "1"})
    assert client_ip(scope, plain) == "10.0.0.1"                                               # forged header ignored
    assert client_ip(scope, trusted) == "198.51.100.7"                                          # first hop
    assert client_ip(scope, load_settings(environ={"TRUST_PROXY": "1", "PROXY_HOPS": "1"})) == "10.1.1.1"
    assert client_ip(scope, load_settings(environ={"TRUST_PROXY": "1", "PROXY_HOPS": "9"})) == "198.51.100.7"
    assert client_ip(_scope({"x-forwarded-for": "garbage"}), trusted) == "10.0.0.1"
    assert client_ip(_scope({"x-forwarded-for": "[2001:db8::1]:443"}), trusted) == "2001:db8::1"
    assert client_ip(_scope({"x-forwarded-for": "203.0.113.5:4040"}), trusted) == "203.0.113.5"
    assert client_ip(_scope({}), trusted) == "10.0.0.1"
    assert client_ip({"type": "http", "headers": []}, plain) == "unknown"


def test_secure_cookie_follows_the_scheme_or_forwarded_proto():
    assert AccessGate.is_https(_scope({}, scheme="https"))
    assert AccessGate.is_https(_scope({"x-forwarded-proto": "https"}))
    assert AccessGate.is_https(_scope({"x-forwarded-proto": "https, http"}))
    assert not AccessGate.is_https(_scope({}))
    assert not AccessGate.is_https(_scope({"x-forwarded-proto": "http"}))


# ============================================================================================ the web layer
@pytest.fixture
def fake(tmp_path, sample_pdf) -> FakeService:
    workdir = tmp_path / "svc"
    workdir.mkdir()
    return FakeService(workdir, sample_pdf)


def make_client(app, *, ip: str = "203.0.113.9", base_url: str = "http://testserver", **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 4321)), base_url=base_url, **kw)


def gated(settings: Settings, **changes) -> Settings:
    return settings.with_(access_code=CODE, **changes)


async def test_no_access_code_means_no_gate(settings, fake):
    app = create_app(settings, fake)
    async with make_client(app) as c:
        assert (await c.get("/api/auth")).json() == {"required": False, "authenticated": True}
        assert (await c.get("/api/sessions")).status_code == 200
        login = await c.post("/api/login", json={"code": "anything"})
        assert login.status_code == 200 and login.json() == {"required": False, "authenticated": True} and "set-cookie" not in login.headers


async def test_gate_protects_every_api_route_but_not_the_open_ones_or_static_files(settings, fake):
    sid = fake.add_session("ready").id
    app = create_app(gated(settings), fake)
    async with make_client(app) as c:
        for method, path in [("GET", "/api/sessions"), ("POST", "/api/sessions"), ("GET", f"/api/sessions/{sid}"), ("GET", "/api/config"),
                             ("GET", f"/api/sessions/{sid}/document/file"), ("POST", f"/api/sessions/{sid}/messages"),
                             ("POST", f"/api/sessions/{sid}/document"), ("DELETE", f"/api/sessions/{sid}"), ("GET", "/api/openapi.json"),
                             ("GET", "/api/nope"), ("PUT", "/api/sessions")]:
            response = await c.request(method, path)
            assert response.status_code == 401, (method, path)
            assert http_error(response)["code"] == "auth_required"
            assert response.headers["x-content-type-options"] == "nosniff"
        assert not [call for call in fake.calls if call[0] != "health"]                         # the service was never reached
        for path in ("/", "/static/js/main.js", "/static/js/login.js", "/static/css/tokens.css", "/favicon.ico"):
            assert (await c.get(path)).status_code in (200, 204), path
        assert (await c.get("/api/auth")).json() == {"required": True, "authenticated": False}
        health = await c.get("/api/health")
        assert health.status_code == 200 and set(health.json()) == {"ok", "version"}             # nothing about the environment


async def test_login_sets_a_signed_httponly_cookie_and_unlocks_the_api(settings, fake):
    sid = fake.add_session("ready").id
    app = create_app(gated(settings), fake)
    async with make_client(app) as c:
        wrong = await c.post("/api/login", json={"code": "nope"})
        assert wrong.status_code == 401 and http_error(wrong)["code"] == "invalid_code" and "set-cookie" not in wrong.headers
        ok = await c.post("/api/login", json={"code": CODE})
        assert ok.status_code == 200 and ok.json() == {"required": True, "authenticated": True}
        cookie = ok.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE_NAME}=") and "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Max-Age=43200" in cookie
        assert "Secure" not in cookie and "Path=/" in cookie and CODE not in cookie
        assert (await c.get("/api/auth")).json() == {"required": True, "authenticated": True}
        assert (await c.get("/api/sessions")).status_code == 200
        pdf = await c.get(f"/api/sessions/{sid}/document/file", headers={"range": "bytes=0-9"})
        assert pdf.status_code == 206 and pdf.content.startswith(b"%PDF")                         # pdf.js sends only the cookie
        assert (await c.get("/api/config")).status_code == 200
        full = (await c.get("/api/health")).json()
        assert full["ok"] and "pageindex_version" in full


async def test_login_cookie_is_secure_behind_https(settings, fake):
    app = create_app(gated(settings), fake)
    async with make_client(app) as c:
        forwarded = await c.post("/api/login", json={"code": CODE}, headers={"x-forwarded-proto": "https"})
        assert "; Secure" in forwarded.headers["set-cookie"]
    async with make_client(app, base_url="https://testserver") as c:
        assert "; Secure" in (await c.post("/api/login", json={"code": CODE})).headers["set-cookie"]


async def test_logout_clears_the_cookie(settings, fake):
    app = create_app(gated(settings), fake)
    async with make_client(app) as c:
        await c.post("/api/login", json={"code": CODE})
        assert (await c.get("/api/sessions")).status_code == 200
        out = await c.post("/api/logout")
        assert out.status_code == 200 and "Max-Age=0" in out.headers["set-cookie"]
        assert (await c.get("/api/sessions")).status_code == 401
        assert (await c.post("/api/logout")).status_code == 200                                  # idempotent, works signed out


async def test_forged_expired_and_foreign_cookies_are_refused(settings, fake):
    app = create_app(gated(settings), fake)
    other = AccessGate(gated(settings, session_secret="another")).issue()
    async with make_client(app) as c:
        for value in ("garbage", "1.2.3", other, "9999999999.aa." + "0" * 64):
            r = await c.get("/api/sessions", headers={"cookie": f"{COOKIE_NAME}={value}"})
            assert r.status_code == 401, value
        token = (await c.post("/api/login", json={"code": CODE})).headers["set-cookie"].split(";")[0].split("=", 1)[1]
        assert (await c.get("/api/sessions", headers={"cookie": f"{COOKIE_NAME}={token}"})).status_code == 200
        gate = app.state.gate
        real = gate._clock
        gate._clock = lambda: real() + SESSION_TTL_S + 5
        assert (await c.get("/api/sessions", headers={"cookie": f"{COOKIE_NAME}={token}"})).status_code == 401
        gate._clock = real


async def test_a_header_is_not_a_credential(settings, fake):
    app = create_app(gated(settings), fake)
    async with make_client(app) as c:
        r = await c.get("/api/sessions", headers={"authorization": f"Bearer {CODE}", "x-access-code": CODE})
        assert r.status_code == 401


async def test_login_is_rate_limited_per_client_address(settings, fake):
    app = create_app(gated(settings), fake)
    async with make_client(app, ip="198.51.100.1") as c:
        for _ in range(10):
            assert (await c.post("/api/login", json={"code": "wrong"})).status_code == 401
        blocked = await c.post("/api/login", json={"code": "wrong"})
        assert blocked.status_code == 429 and http_error(blocked)["code"] == "too_many_attempts"
        assert 1 <= int(blocked.headers["retry-after"]) <= 600
        assert (await c.post("/api/login", json={"code": CODE})).status_code == 429               # even the right code waits
        assert (await c.get("/api/sessions")).status_code == 401
    async with make_client(app, ip="198.51.100.2") as other:
        assert (await other.post("/api/login", json={"code": CODE})).status_code == 200            # someone else is not locked out


async def test_a_successful_login_resets_the_failure_count(settings, fake):
    app = create_app(gated(settings), fake)
    async with make_client(app) as c:
        for _ in range(9):
            await c.post("/api/login", json={"code": "wrong"})
        assert (await c.post("/api/login", json={"code": CODE})).status_code == 200
        for _ in range(10):
            assert (await c.post("/api/login", json={"code": "wrong"})).status_code == 401


async def test_forwarded_for_is_ignored_unless_the_proxy_is_trusted(settings, fake):
    async def burn(app):
        async with make_client(app, ip="10.9.9.9") as c:
            for i in range(10):
                await c.post("/api/login", json={"code": "wrong"}, headers={"x-forwarded-for": f"203.0.113.{i}"})
            return (await c.post("/api/login", json={"code": "wrong"}, headers={"x-forwarded-for": "203.0.113.200"})).status_code

    assert await burn(create_app(gated(settings), fake)) == 429                                    # rotating a fake header does not help
    assert await burn(create_app(gated(settings, trust_proxy=True), fake)) == 401                  # a trusted proxy supplies the real address


async def test_login_validates_its_body_and_the_origin(settings, fake):
    app = create_app(gated(settings), fake)
    async with make_client(app) as c:
        assert (await c.post("/api/login", json={})).status_code == 400
        assert (await c.post("/api/login", content=b"not json", headers={"content-type": "application/json"})).status_code == 400
        assert (await c.post("/api/login", json={"code": "x" * 5000})).status_code == 400
        cross = await c.post("/api/login", json={"code": CODE}, headers={"origin": "https://evil.example"})
        assert cross.status_code == 403 and http_error(cross)["code"] == "forbidden_origin" and "set-cookie" not in cross.headers
        same = await c.post("/api/login", json={"code": CODE}, headers={"origin": "http://testserver"})
        assert same.status_code == 200


async def test_the_access_code_is_never_logged(settings, fake, caplog):
    app = create_app(gated(settings), fake)
    with caplog.at_level(logging.DEBUG):
        async with make_client(app) as c:
            await c.post("/api/login", json={"code": "my-wrong-guess-123"})
            await c.post("/api/login", json={"code": CODE})
    assert CODE not in caplog.text and "my-wrong-guess-123" not in caplog.text


# ----- per-IP question limiter
async def _ask(c, sid, ip=None):
    headers = {"x-forwarded-for": ip} if ip else {}
    return await c.post(f"/api/sessions/{sid}/messages", json={"content": "q"}, headers=headers)


async def test_questions_per_hour_per_ip(settings, fake):
    sid = fake.add_session("ready").id
    fake.ask_script = standard_script(sid)
    app = create_app(settings.with_(questions_per_hour_per_ip=2), fake)
    async with make_client(app) as c:
        assert (await _ask(c, sid)).status_code == 200
        assert (await _ask(c, sid)).status_code == 200
        limited = await _ask(c, sid)
        assert limited.status_code == 429 and http_error(limited)["code"] == "rate_limited" and int(limited.headers["retry-after"]) > 0
        assert "2 questions per hour" in http_error(limited)["message"]
        assert len([call for call in fake.calls if call[0] == "ask"]) == 2
    async with make_client(app, ip="198.51.100.77") as other:
        assert (await _ask(other, sid)).status_code == 200


async def test_refused_questions_do_not_use_up_the_allowance(settings, fake):
    empty = fake.add_session("empty").id
    app = create_app(settings.with_(questions_per_hour_per_ip=1), fake)
    async with make_client(app) as c:
        for _ in range(5):
            r = await _ask(c, empty)
            assert r.status_code != 429                                                             # 409/404-style refusals are free
        ready = fake.add_session("ready").id
        fake.ask_script = standard_script(ready)
        assert (await _ask(c, ready)).status_code == 200
        assert (await _ask(c, ready)).status_code == 429
        blank = await c.post(f"/api/sessions/{ready}/messages", json={"content": "   "})
        assert blank.status_code == 400


async def test_unlimited_by_default_and_the_forwarded_address_counts_behind_a_trusted_proxy(settings, fake):
    sid = fake.add_session("ready").id
    fake.ask_script = standard_script(sid)
    async with make_client(create_app(settings, fake)) as c:
        for _ in range(4):
            assert (await _ask(c, sid)).status_code == 200
    app = create_app(settings.with_(questions_per_hour_per_ip=1, trust_proxy=True), fake)
    async with make_client(app) as c:
        assert (await _ask(c, sid, "198.51.100.1")).status_code == 200
        assert (await _ask(c, sid, "198.51.100.2")).status_code == 200                              # a different visitor behind the proxy
        assert (await _ask(c, sid, "198.51.100.1")).status_code == 429


async def test_rescoring_shares_the_allowance(settings, fake):
    sid = fake.add_session("locked").id
    fake.ask_script = standard_script(sid)
    app = create_app(settings.with_(questions_per_hour_per_ip=2), fake)
    async with make_client(app) as c:
        mid = parse_sse((await _ask(c, sid)).text)[0][1]["message_id"]
        assert (await c.post(f"/api/sessions/{sid}/messages/{mid}/evaluate")).status_code == 200
        limited = await c.post(f"/api/sessions/{sid}/messages/{mid}/evaluate")
        assert limited.status_code == 429 and http_error(limited)["code"] == "rate_limited"


# ----- budget over HTTP, real service
async def test_budget_over_http_with_the_real_service(build_env):
    env = build_env(budget_usd_total=1.0, max_sessions=3)
    app = create_app(env.settings, env.service)
    async with make_client(app) as c:
        assert (await c.get("/api/config")).json()["usage_budget"] == {"enabled": True, "used_fraction": 0.0}
        sids = []
        for _ in range(3):
            created = await c.post("/api/sessions", json={})
            assert created.status_code == 201
            sids.append(created.json()["id"])
            uploaded = await c.post(f"/api/sessions/{sids[-1]}/document", files={"file": ("a.pdf", env.sample.read_bytes(), "application/pdf")})
            assert uploaded.status_code in (202, 402), uploaded.text
        limit = await c.post("/api/sessions", json={})
        assert limit.status_code == 429 and http_error(limit)["code"] == "session_limit"
        usage = (await c.get("/api/config")).json()["usage_budget"]
        assert usage["enabled"] and usage["used_fraction"] >= 0.8 and set(usage) == {"enabled", "used_fraction"}
        assert "$" not in json.dumps(usage)
        # one more upload is refused with the documented body, before the file is read
        extra = env.service._store.create_session().id
        env.service._settings = env.service._settings.with_(max_sessions=0)
        refused = await c.post(f"/api/sessions/{extra}/document", files={"file": ("b.pdf", env.sample.read_bytes(), "application/pdf")})
        assert refused.status_code == 402
        assert refused.json() == {"error": {"code": "budget_exhausted", "message": "The demo's usage budget has been used up. Please contact the owner."}}
        assert not list((env.settings.data_dir / "tmp").glob("*")) if (env.settings.data_dir / "tmp").exists() else True
        assert (await c.get("/api/sessions")).status_code == 200                                   # reads keep working
        assert (await c.get(f"/api/sessions/{sids[0]}")).status_code == 200


async def test_question_refused_over_http_with_402_json(build_env):
    env = build_env(budget_usd_total=0.3)
    sid = env.ready_session()                                                      # one index = 0.40 >= 0.30
    app = create_app(env.settings, env.service)
    async with make_client(app) as c:
        refused = await c.post(f"/api/sessions/{sid}/messages", json={"content": "what now?"})
        assert refused.status_code == 402 and refused.headers["content-type"].startswith("application/json")
        assert http_error(refused) == {"code": "budget_exhausted", "message": BUDGET_EXHAUSTED_MESSAGE}
        assert not env.qa.calls


async def test_the_fake_service_without_usage_support_reports_no_budget(settings, fake):
    async with make_client(create_app(settings, fake)) as c:
        assert (await c.get("/api/config")).json()["usage_budget"] == {"enabled": False, "used_fraction": 0.0}


async def test_upload_precheck_runs_before_the_body_is_read(settings, fake):
    class Broke(FakeService):
        def check_budget(self):
            raise ServiceError("budget_exhausted", BUDGET_EXHAUSTED_MESSAGE, 402)

    service = Broke(fake.workdir, fake.pdf)
    sid = service.add_session("empty").id
    async with make_client(create_app(settings, service)) as c:
        r = await c.post(f"/api/sessions/{sid}/document", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")})
        assert r.status_code == 402 and not [call for call in service.calls if call[0] == "attach_document"]
        assert not (settings.data_dir / "tmp").exists() or not list((settings.data_dir / "tmp").iterdir())


# ============================================================================================ host + origin guard
def test_allowed_hosts_policy():
    base = load_settings(environ={}).with_(host="0.0.0.0")
    assert allowed_hosts(base) == ["*"]                                                              # unchanged: local bind-all
    listed = base.with_(allowed_hosts=(".hf.space", "demo.example"))
    names = allowed_hosts(listed)
    assert "*" not in names and {"localhost", "127.0.0.1", ".hf.space", "demo.example"} <= set(names)  # bind-all no longer disables it
    assert allowed_hosts(base.with_(public_mode=True)) == ["*"]                                      # public, no list: any Host (warned)
    assert "*" not in allowed_hosts(load_settings(environ={}).with_(host="127.0.0.1", allowed_hosts=("x.example",)))
    assert "127.0.0.1" in allowed_hosts(load_settings(environ={}).with_(host="127.0.0.1"))


@pytest.mark.parametrize("host, ok", [
    ("demo.hf.space", True), ("a-b-c.hf.space", True), ("deep.sub.hf.space", True), ("hf.space", True), ("DEMO.HF.SPACE:443", True),
    ("localhost:7860", True), ("127.0.0.1:7860", True), ("my-app.onrender.com", True), ("exact.example", True),
    ("evilhf.space", False), ("hf.space.evil.com", False), ("demo.hf.space.evil.com", False), ("evil.example", False),
    ("onrender.com.evil.io", False), ("", False), ("exact.example.evil.io", False),
])
async def test_suffix_wildcards_and_exact_names(settings, fake, host, ok):
    app = create_app(settings.with_(host="0.0.0.0", allowed_hosts=(".hf.space", ".onrender.com", "exact.example")), fake)
    async with make_client(app) as c:
        r = await c.get("/api/sessions", headers={"host": host})
        assert (r.status_code == 200) == ok, (host, r.status_code)
        if not ok:
            assert r.status_code == 400 and http_error(r)["code"] == "invalid_host"


async def test_in_public_deployments_health_and_static_files_answer_any_host(settings, fake):
    app = create_app(settings.with_(host="0.0.0.0", allowed_hosts=(".hf.space",)), fake)
    async with make_client(app) as c:
        odd = {"host": "10.20.30.40:7860"}                                                           # what a platform probe may send
        assert (await c.get("/api/health", headers=odd)).status_code == 200
        assert (await c.get("/", headers=odd)).status_code == 200
        assert (await c.get("/static/js/main.js", headers=odd)).status_code == 200
        assert (await c.get("/api/sessions", headers=odd)).status_code == 400
        assert (await c.get("/api/config", headers=odd)).status_code == 400


async def test_the_local_guard_stays_strict_without_a_public_setup(settings, fake):
    app = create_app(settings, fake)                                                                  # 127.0.0.1, nothing configured
    async with make_client(app) as c:
        assert (await c.get("/api/health", headers={"host": "evil.example"})).status_code == 400
        assert (await c.get("/", headers={"host": "evil.example"})).status_code == 400


async def test_public_mode_without_allowed_hosts_accepts_any_host_but_warns_and_keeps_the_origin_check(settings, fake, caplog):
    with caplog.at_level(logging.WARNING, logger="reportlens.web"):
        app = create_app(settings.with_(host="0.0.0.0", public_mode=True), fake)
    assert "ALLOWED_HOSTS is empty" in caplog.text and "ACCESS_CODE is empty" in caplog.text
    async with make_client(app) as c:
        assert (await c.get("/api/sessions", headers={"host": "anything.example"})).status_code == 200
        forged = await c.post("/api/sessions", headers={"host": "anything.example", "origin": "https://evil.example"}, json={})
        assert forged.status_code == 403 and http_error(forged)["code"] == "forbidden_origin"
        own = await c.post("/api/sessions", headers={"host": "anything.example", "origin": "https://anything.example"}, json={})
        assert own.status_code == 201


async def test_a_fully_configured_public_app_logs_no_exposure_warning(settings, fake, caplog):
    with caplog.at_level(logging.WARNING, logger="reportlens.web"):
        create_app(settings.with_(host="0.0.0.0", public_mode=True, allowed_hosts=(".hf.space",), access_code=CODE, budget_usd_total=10.0), fake)
    assert "ALLOWED_HOSTS" not in caplog.text and "ACCESS_CODE" not in caplog.text and "BUDGET" not in caplog.text


@pytest.mark.parametrize("host, origin, ok", [
    ("demo.hf.space", "https://demo.hf.space", True),                    # default https port is dropped from the Origin
    ("demo.hf.space", "https://demo.hf.space:443", True),
    ("demo.hf.space:443", "https://demo.hf.space", False),               # a Host that spells the port does not match (browsers omit it)
    ("demo.hf.space", "http://demo.hf.space", True),
    ("demo.hf.space", "https://other.hf.space", False),
    ("demo.hf.space", "https://demo.hf.space.evil.com", False),
    ("localhost:7860", "http://localhost:7860", True),
    ("localhost:7860", "http://localhost:9999", False),
    ("demo.hf.space", "null", False),
])
async def test_origin_must_match_the_host(settings, fake, host, origin, ok):
    app = create_app(settings.with_(host="0.0.0.0", allowed_hosts=(".hf.space",)), fake)
    async with make_client(app) as c:
        r = await c.post("/api/sessions", headers={"host": host, "origin": origin}, json={})
        assert r.status_code == (201 if ok else 403), (host, origin, r.status_code)


async def test_forwarded_host_counts_only_when_the_proxy_is_trusted(settings, fake):
    base = settings.with_(host="0.0.0.0", allowed_hosts=(".hf.space", "internal"))
    headers = {"host": "internal:7860", "origin": "https://demo.hf.space", "x-forwarded-host": "demo.hf.space"}
    async with make_client(create_app(base, fake)) as c:
        assert (await c.post("/api/sessions", headers=headers, json={})).status_code == 403
    async with make_client(create_app(base.with_(trust_proxy=True), fake)) as c:
        assert (await c.post("/api/sessions", headers=headers, json={})).status_code == 201
        forged = {**headers, "origin": "https://evil.example"}
        assert (await c.post("/api/sessions", headers=forged, json={})).status_code == 403


# ============================================================================================ production entry point
class _FakeUvicorn:
    config_kwargs: dict = {}
    ran = False

    class Config:
        def __init__(self, app, **kw):
            _FakeUvicorn.config_kwargs = kw

    class Server:
        started = True
        should_exit = False

        def __init__(self, config):
            pass

        def run(self):
            _FakeUvicorn.ran = True


def _run_main(monkeypatch, tmp_path, argv, **settings_changes) -> int:
    import uvicorn

    s = load_settings(environ={}).with_(data_dir=tmp_path / "data", demo_mock=True, **settings_changes)
    monkeypatch.setattr("reportlens.config.load_settings", lambda *a, **k: s)
    monkeypatch.setattr(app_module, "create_app", lambda settings, *a, **k: object())
    monkeypatch.setattr(uvicorn, "Config", _FakeUvicorn.Config)
    monkeypatch.setattr(uvicorn, "Server", _FakeUvicorn.Server)
    monkeypatch.setattr(entry, "port_in_use", lambda host, port: False)
    _FakeUvicorn.ran = False
    return entry.main(argv)


def test_main_accepts_host_and_port_and_trusts_proxy_headers_only_when_told(monkeypatch, tmp_path, capsys):
    assert _run_main(monkeypatch, tmp_path, ["--host", "0.0.0.0", "--port", "7860"], trust_proxy=True) == 0
    kw = _FakeUvicorn.config_kwargs
    assert _FakeUvicorn.ran and kw["host"] == "0.0.0.0" and kw["port"] == 7860
    assert kw["proxy_headers"] is True and kw["forwarded_allow_ips"] == "*" and kw["timeout_graceful_shutdown"] == entry.GRACEFUL_SHUTDOWN_S
    assert _run_main(monkeypatch, tmp_path, ["--host", "0.0.0.0", "--port", "7860"]) == 0
    assert _FakeUvicorn.config_kwargs["proxy_headers"] is False and _FakeUvicorn.config_kwargs["forwarded_allow_ips"] is None


def test_main_uses_the_port_from_the_environment_settings(monkeypatch, tmp_path):
    assert _run_main(monkeypatch, tmp_path, ["--host", "0.0.0.0"], port=10000) == 0
    assert _FakeUvicorn.config_kwargs["port"] == 10000


def test_main_creates_an_empty_data_dir_and_survives_it_not_being_creatable(monkeypatch, tmp_path, caplog):
    assert _run_main(monkeypatch, tmp_path, []) == 0 and (tmp_path / "data").is_dir()
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="reportlens.main"):
        assert _run_main(monkeypatch, tmp_path, ["--data-dir", str(blocker / "sub")]) == 2
    assert "Cannot create the data folder" in caplog.text and not _FakeUvicorn.ran


def test_main_warns_about_an_open_bind_only_when_nothing_protects_it(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="reportlens.main"):
        _run_main(monkeypatch, tmp_path, ["--host", "0.0.0.0"])
    assert "without ACCESS_CODE or PUBLIC_MODE" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="reportlens.main"):
        _run_main(monkeypatch, tmp_path, ["--host", "0.0.0.0"], access_code=CODE)
        _run_main(monkeypatch, tmp_path, ["--host", "0.0.0.0"], public_mode=True)
    assert "without ACCESS_CODE" not in caplog.text


def test_the_startup_summary_never_contains_secrets(settings):
    s = settings.with_(access_code=CODE, session_secret="hush-hush", openai_api_key="sk-test-123456789", public_mode=True,
                       budget_usd_total=10.0, allowed_hosts=(".hf.space",))
    line = entry.describe_exposure(s)
    assert CODE not in line and "hush-hush" not in line and "sk-test" not in line
    assert "access_gate=on" in line and "budget=$10" in line and ".hf.space" in line


# ============================================================================================ startup cost + front end
def test_the_config_route_does_not_import_ragas():
    code = "import sys, reportlens.metric_info, reportlens.web.routes; assert 'ragas' not in sys.modules and 'litellm' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


def test_metric_info_is_still_exported_by_the_evaluation_module():
    from reportlens import evaluation, metric_info

    assert evaluation.METRIC_INFO is metric_info.METRIC_INFO


def test_front_end_knows_the_new_error_codes_and_screens():
    api_js = (STATIC_DIR / "js" / "api.js").read_text(encoding="utf-8")
    for code in ("auth_required", "invalid_code", "too_many_attempts", "budget_exhausted", "rate_limited", "session_limit"):
        assert f"{code}:" in api_js, code
    login_js = (STATIC_DIR / "js" / "login.js").read_text(encoding="utf-8")
    assert "Enter access code" in login_js and 'aria: { live: "polite" }' in login_js and '"for": "login-code"' in login_js.replace("for:", '"for":')
    assert (STATIC_DIR / "js" / "usage.js").is_file()
    main_js = (STATIC_DIR / "js" / "main.js").read_text(encoding="utf-8")
    assert "api.auth()" in main_js and "onSignOut" in main_js
    assert "Sign out" in (STATIC_DIR / "js" / "sidebar.js").read_text(encoding="utf-8")


def test_question_reserve_constant_is_sane():
    assert 0.3 <= QUESTION_RESERVE_USD <= 1.0 and 0 < FAILED_ANSWER_FALLBACK_USD < ANSWER_FALLBACK_USD
