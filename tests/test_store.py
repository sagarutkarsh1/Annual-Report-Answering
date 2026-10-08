"""Offline tests for reportlens.store (SQLite persistence)."""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reportlens import store as store_module
from reportlens.models import (
    Citation,
    ContextPage,
    DocumentInfo,
    EvalScores,
    Message,
    Rect,
    Source,
    Step,
    Usage,
)
from reportlens.store import (
    _MIGRATIONS,
    INTERRUPTED_ANSWER_ERROR,
    INTERRUPTED_INDEXING_ERROR,
    Store,
    SessionNotFound,
    new_id,
    now_iso,
)

UNICODE_TEXT = "Résumé 年度报告 — Ünïcödé “quotes” £1.2bn ✓ 😀 עברית"


# --------------------------------------------------------------------------------------------- helpers
@pytest.fixture
def store(tmp_path):
    # Parent directory deliberately does not exist yet.
    s = Store(tmp_path / "nested" / "data" / "reportlens.db")
    yield s
    s.close()


def make_doc(**overrides) -> DocumentInfo:
    base = dict(id=new_id(), filename="National Grid – Annual Report.pdf", doc_name="National_Grid_Annual_Report.pdf",
                size_bytes=12_345_678)
    base.update(overrides)
    return DocumentInfo(**base)


def make_msg(sid: str, role: str = "user", content: str = "hello", **overrides) -> Message:
    return Message(id=new_id(), session_id=sid, role=role, content=content, **overrides)


def full_assistant_message(sid: str) -> Message:
    return Message(
        id=new_id(), session_id=sid, role="assistant", status="answered",
        content=f"Revenue grew 5% [[c1]] driven by {UNICODE_TEXT} [[c2]].",
        citations=[
            Citation(id="c1", index=1, doc_name="r.pdf", page=88, cited_page=88, printed_page="86",
                     section_path=["Strategic Report", "Financial review"], node_id="0007", node_range=[85, 92],
                     quote="Revenue grew by 5%", quote_source="model", match_method="exact", match_score=1.0,
                     rects=[Rect(x=0.1, y=0.2, w=0.5, h=0.02), Rect(x=0.1, y=0.22, w=0.3, h=0.02)],
                     page_width=595.2, page_height=841.8, claim="Revenue grew 5%"),
            Citation(id="c2", index=2, doc_name="r.pdf", page=90, cited_page=89, match_method="page"),
        ],
        sources=[Source(page=88, printed_page="86", refs=1, citation_ids=["c1"], section_path=["Strategic Report"]),
                 Source(page=90, refs=1, citation_ids=["c2"])],
        steps=[Step(id="s1", kind="tool", tool="get_page_content", label="Read pages 88-90", pages=[88, 89, 90],
                    status="done", elapsed_ms=420)],
        usage=Usage(model="gpt-5.6-sol", input_tokens=1000, cached_tokens=200, output_tokens=300,
                    reasoning_tokens=100, cost_usd=0.0123),
        elapsed_ms=8123,
        evaluation=EvalScores(status="done", faithfulness=0.91, answer_relevancy=0.82, context_precision=0.7,
                              context_verdicts=[{"index": 0, "page": 88, "verdict": 1, "reason": UNICODE_TEXT}],
                              n_contexts_input=3, n_contexts_scored=3, judge_model="gpt-4.1-mini",
                              ragas_version="0.4.3", latency_s=12.5),
        created_at="2026-10-07T10:00:00Z",
    )


def raw_rows(store: Store, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(store._path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def raw_exec(store: Store, sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(store._path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------------------------- ids, time, setup
def test_new_id_is_32_hex_and_unique():
    ids = {new_id() for _ in range(1000)}
    assert len(ids) == 1000
    assert all(re.fullmatch(r"[0-9a-f]{32}", i) for i in ids)


def test_now_iso_format_and_utc():
    stamp = now_iso()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp)
    parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs(datetime.now(timezone.utc) - parsed) < timedelta(seconds=5)


def test_creates_missing_parent_dirs_from_settings(settings):
    assert not settings.data_dir.exists()
    s = Store(settings.db_path)
    try:
        assert settings.db_path.is_file()
    finally:
        s.close()


# --------------------------------------------------------------------------------------------- sessions
def test_create_and_get_session(store):
    created = store.create_session()
    assert re.fullmatch(r"[0-9a-f]{32}", created.id)
    assert (created.title, created.state, created.document, created.message_count) == ("New chat", "empty", None, 0)
    assert created.created_at == created.updated_at
    assert store.get_session(created.id) == created


def test_get_unknown_session_is_none(store):
    assert store.get_session("0" * 32) is None
    assert store.get_document("0" * 32) is None
    assert store.list_messages("0" * 32) == []


def test_create_session_title_is_cleaned(store):
    assert store.create_session("  What is\nthe\tdividend?  ").title == "What is the dividend?"
    assert store.create_session("   ").title == "New chat"
    assert len(store.create_session("x" * 500).title) == 120


def test_list_sessions_newest_updated_first_even_within_one_second(store, monkeypatch):
    monkeypatch.setattr(store_module, "now_iso", lambda: "2026-10-07T10:00:00Z")   # every write in the same second
    a, b, c = (store.create_session(t) for t in "abc")
    assert [s.title for s in store.list_sessions()] == ["c", "b", "a"]
    store.touch_session(a.id)
    assert [s.title for s in store.list_sessions()] == ["a", "c", "b"]
    store.add_message(make_msg(b.id))   # activity also moves a session up
    assert [s.title for s in store.list_sessions()] == ["b", "a", "c"]


def test_touch_session_updates_timestamp(store, monkeypatch):
    sess = store.create_session()
    monkeypatch.setattr(store_module, "now_iso", lambda: "2031-01-01T00:00:00Z")
    store.touch_session(sess.id)
    assert store.get_session(sess.id).updated_at == "2031-01-01T00:00:00Z"
    assert store.get_session(sess.id).created_at == sess.created_at
    store.touch_session("0" * 32)   # unknown id: silently nothing


def test_list_sessions_uses_a_single_select(store):
    for i in range(12):
        s = store.create_session(f"s{i}")
        store.put_document(s.id, make_doc(status="ready"))
        store.add_message(make_msg(s.id))
    statements: list[str] = []
    for conn in store._idle:    # white-box: list_sessions will use one of the pooled connections
        conn.set_trace_callback(statements.append)
    sessions = store.list_sessions()
    assert len(sessions) == 12
    assert all(s.state == "locked" and s.message_count == 1 and s.document is not None for s in sessions)
    assert len([s for s in statements if s.lstrip().upper().startswith(("SELECT", "WITH"))]) == 1


def test_rename_session(store):
    a = store.create_session("a")
    b = store.create_session("b")
    store.rename_session(a.id, "  Q3 \n results — 年度  ")
    assert store.get_session(a.id).title == "Q3 results — 年度"
    store.rename_session(a.id, "y" * 300)
    assert store.get_session(a.id).title == "y" * 120
    # renaming is not activity: order unchanged (b is still the most recent)
    assert [s.id for s in store.list_sessions()] == [b.id, a.id]


@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
def test_rename_rejects_empty_title(store, bad):
    sess = store.create_session("keep me")
    with pytest.raises(ValueError):
        store.rename_session(sess.id, bad)
    assert store.get_session(sess.id).title == "keep me"


def test_rename_unknown_session_is_noop(store):
    store.rename_session("0" * 32, "x")
    with pytest.raises(ValueError):   # validation happens before the lookup
        store.rename_session("0" * 32, "")


def test_delete_session_cascades_everything(store):
    keep, doomed = store.create_session("keep"), store.create_session("doomed")
    for sess in (keep, doomed):
        store.put_document(sess.id, make_doc(status="ready", pi_doc_id="pi-" + "a" * 32))
        u = make_msg(sess.id)
        a = make_msg(sess.id, "assistant")
        store.add_message(u)
        store.add_message(a, contexts=[ContextPage(page=1, text="t")])

    assert store.delete_session(doomed.id) is True
    assert store.delete_session(doomed.id) is False
    assert store.get_session(doomed.id) is None
    remaining = {t: raw_rows(store, f"SELECT COUNT(*) FROM {t}")[0][0]
                 for t in ("documents", "messages", "message_contexts")}
    assert remaining == {"documents": 1, "messages": 2, "message_contexts": 1}
    assert store.get_session(keep.id).message_count == 2
    assert len(store.list_messages(keep.id)) == 2


# --------------------------------------------------------------------------------------------- documents
def test_document_round_trip_includes_pi_doc_id(store):
    sess = store.create_session()
    pi = "pi-" + "0123456789abcdef" * 2
    doc = make_doc(page_count=212, status="ready", stage="ready", progress=1.0, title="Annual Report 2025/26",
                   description=UNICODE_TEXT, node_count=81, indexed_at="2026-10-07T10:05:00Z", index_seconds=301.25,
                   pi_doc_id=pi)
    store.put_document(sess.id, doc)

    got = store.get_document(sess.id)
    assert got == doc and got.pi_doc_id == pi
    assert "pi_doc_id" not in got.model_dump(mode="json")      # still never serialised to the browser
    assert store.get_session(sess.id).document.pi_doc_id == pi  # internal callers see it on the session too


def test_put_document_replaces_and_fills_created_at(store):
    sess = store.create_session()
    first = make_doc(pi_doc_id="pi-" + "1" * 32)
    store.put_document(sess.id, first)
    assert first.created_at and store.get_document(sess.id).created_at == first.created_at

    second = make_doc(filename="other.pdf", doc_name="other.pdf", status="failed", error="boom")
    store.put_document(sess.id, second)
    got = store.get_document(sess.id)
    assert (got.id, got.filename, got.status, got.pi_doc_id) == (second.id, "other.pdf", "failed", None)
    assert raw_rows(store, "SELECT COUNT(*) FROM documents")[0][0] == 1


def test_put_document_unknown_session_raises_and_writes_nothing(store):
    with pytest.raises(SessionNotFound):
        store.put_document("0" * 32, make_doc())
    assert raw_rows(store, "SELECT COUNT(*) FROM documents")[0][0] == 0


def test_update_document_partial_updates(store):
    sess = store.create_session()
    doc = make_doc(created_at="2026-10-07T09:00:00Z")
    store.put_document(sess.id, doc)

    out = store.update_document(sess.id, stage="building_tree", progress=0.4)
    assert (out.stage, out.progress, out.status) == ("building_tree", 0.4, "indexing")
    assert (out.filename, out.size_bytes, out.created_at) == (doc.filename, doc.size_bytes, doc.created_at)

    store.update_document(sess.id, pi_doc_id="pi-" + "b" * 32, page_count=88, title=UNICODE_TEXT)
    got = store.get_document(sess.id)
    assert (got.pi_doc_id, got.page_count, got.title) == ("pi-" + "b" * 32, 88, UNICODE_TEXT)
    assert (got.stage, got.progress) == ("building_tree", 0.4)   # earlier update survived

    store.update_document(sess.id, status="ready", stage="ready", progress=1.0, error=None, indexed_at="x")
    got = store.get_document(sess.id)
    assert got.status == "ready" and got.pi_doc_id == "pi-" + "b" * 32   # pi_doc_id carried through

    store.update_document(sess.id, pi_doc_id=None)
    assert store.get_document(sess.id).pi_doc_id is None
    assert store.update_document(sess.id) == store.get_document(sess.id)   # no fields: current document


def test_update_document_rejects_unknown_fields_atomically(store):
    sess = store.create_session()
    store.put_document(sess.id, make_doc())
    with pytest.raises(ValueError, match="bogus"):
        store.update_document(sess.id, progress=0.9, bogus=1)
    with pytest.raises(ValueError):
        store.update_document("0" * 32, nope=1)   # validated even when there is no document
    assert store.get_document(sess.id).progress == 0.0


def test_update_document_rejects_invalid_values(store):
    sess = store.create_session()
    store.put_document(sess.id, make_doc())
    with pytest.raises(ValueError):
        store.update_document(sess.id, status="exploded")
    with pytest.raises(ValueError):
        store.update_document(sess.id, size_bytes="lots")
    got = store.get_document(sess.id)
    assert got.status == "indexing" and isinstance(got.size_bytes, int)
    assert store.get_session(sess.id).state == "indexing"


def test_update_document_without_document_returns_none(store):
    sess = store.create_session()
    assert store.update_document(sess.id, status="ready") is None
    assert store.update_document("0" * 32, status="ready") is None


# --------------------------------------------------------------------------------------------- state and counts
def test_session_state_transitions(store):
    sess = store.create_session()
    state = lambda: store.get_session(sess.id).state   # noqa: E731
    assert state() == "empty"

    store.put_document(sess.id, make_doc())
    assert state() == "indexing"
    store.update_document(sess.id, stage="summarizing", progress=0.7)
    assert state() == "indexing"

    store.update_document(sess.id, status="ready", stage="ready", progress=1.0)
    assert state() == "ready"

    store.add_message(make_msg(sess.id, "user"))
    assert state() == "locked"
    store.add_message(make_msg(sess.id, "assistant"))
    assert state() == "locked"


def test_failed_state_and_reupload_flow(store):
    sess = store.create_session()
    store.put_document(sess.id, make_doc())
    store.update_document(sess.id, status="failed", stage="failed", error="Scanned PDF")
    s = store.get_session(sess.id)
    assert s.state == "failed" and s.document.error == "Scanned PDF"

    store.put_document(sess.id, make_doc())   # re-upload replaces the failed document
    assert store.get_session(sess.id).state == "indexing"


def test_only_user_messages_lock_the_session(store):
    sess = store.create_session()
    store.put_document(sess.id, make_doc(status="ready", stage="ready"))
    store.add_message(make_msg(sess.id, "assistant", "greeting"))
    s = store.get_session(sess.id)
    assert (s.state, s.message_count) == ("ready", 1)


def test_session_without_document(store):
    sess = store.create_session()
    assert store.get_document(sess.id) is None
    store.add_message(make_msg(sess.id))   # contrived, but must not crash or invent a document
    s = store.get_session(sess.id)
    assert s.document is None and s.state == "empty" and s.message_count == 1


def test_message_count_is_per_session_and_counts_both_roles(store):
    a, b = store.create_session("a"), store.create_session("b")
    for _ in range(3):
        store.add_message(make_msg(a.id, "user"))
        store.add_message(make_msg(a.id, "assistant"))
    store.add_message(make_msg(b.id, "user"))
    counts = {s.title: s.message_count for s in store.list_sessions()}
    assert counts == {"a": 6, "b": 1}
    assert store.get_session(a.id).message_count == 6


# --------------------------------------------------------------------------------------------- messages
def test_message_round_trip_of_every_model(store):
    sess = store.create_session()
    user = make_msg(sess.id, "user", f"Question: {UNICODE_TEXT}", created_at="2026-10-07T09:59:59Z")
    assistant = full_assistant_message(sess.id)
    store.add_message(user)
    store.add_message(assistant)

    assert store.get_message(sess.id, user.id) == user
    got = store.get_message(sess.id, assistant.id)
    assert got == assistant
    assert got.citations[0].rects[1].w == 0.3 and got.evaluation.context_verdicts[0]["reason"] == UNICODE_TEXT
    assert store.list_messages(sess.id) == [user, assistant]


def test_message_with_lone_surrogate_round_trips(store):
    sess = store.create_session()
    msg = make_msg(sess.id, "assistant", "bad \ud800 surrogate and NUL \x00 char")
    store.add_message(msg, contexts=[ContextPage(page=1, text="ctx \udfff \x00")])
    assert store.get_message(sess.id, msg.id).content == msg.content
    assert store.get_contexts(msg.id)[0].text == "ctx \udfff \x00"


def test_add_message_fills_created_at(store):
    sess = store.create_session()
    msg = make_msg(sess.id)
    assert msg.created_at == ""
    store.add_message(msg)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", msg.created_at)
    assert store.get_message(sess.id, msg.id).created_at == msg.created_at


def test_messages_keep_order_when_created_in_the_same_second(store):
    a, b = store.create_session("a"), store.create_session("b")
    stamp = "2026-10-07T10:00:00Z"
    expected: dict[str, list[str]] = {a.id: [], b.id: []}
    for i in range(40):
        sid = a.id if i % 3 else b.id    # interleave sessions
        msg = make_msg(sid, "user" if i % 2 == 0 else "assistant", f"m{i}", created_at=stamp)
        store.add_message(msg)
        expected[sid].append(msg.id)
    for sid, ids in expected.items():
        assert [m.id for m in store.list_messages(sid)] == ids


def test_get_message_is_scoped_to_its_session(store):
    a, b = store.create_session(), store.create_session()
    msg = make_msg(a.id)
    store.add_message(msg)
    assert store.get_message(a.id, msg.id) is not None
    assert store.get_message(b.id, msg.id) is None
    assert store.get_message(a.id, "0" * 32) is None
    assert store.get_message("0" * 32, msg.id) is None


def test_update_message_keeps_position_and_updates_every_part(store):
    sess = store.create_session()
    first = make_msg(sess.id, "user", "q")
    streaming = make_msg(sess.id, "assistant", "partial", status="streaming")
    last = make_msg(sess.id, "user", "q2")
    for m in (first, streaming, last):
        store.add_message(m)

    final = full_assistant_message(sess.id).model_copy(update={"id": streaming.id})
    store.update_message(final)
    assert [m.id for m in store.list_messages(sess.id)] == [first.id, streaming.id, last.id]
    assert store.get_message(sess.id, streaming.id) == final

    final.evaluation = EvalScores(status="failed", errors={"faithfulness": "timeout"})
    store.update_message(final)
    assert store.get_message(sess.id, streaming.id).evaluation.errors == {"faithfulness": "timeout"}
    assert store.get_session(sess.id).message_count == 3


def test_update_message_does_not_reorder_sessions(store):
    a = store.create_session("a")
    store.create_session("b")
    msg = make_msg(a.id, "assistant", status="streaming")
    store.add_message(msg)
    store.create_session("c")
    msg.status = "answered"
    store.update_message(msg)    # late RAGAS-style update on an older session
    assert [s.title for s in store.list_sessions()] == ["c", "a", "b"]


def test_update_message_ignores_missing_or_foreign_messages(store):
    a, b = store.create_session(), store.create_session()
    msg = make_msg(a.id, content="original")
    store.add_message(msg)

    store.update_message(make_msg(a.id, content="never added"))      # unknown id: no row, no error
    hijack = msg.model_copy(update={"session_id": b.id, "content": "hijacked"})
    store.update_message(hijack)                                      # wrong session: untouched
    assert store.get_message(a.id, msg.id).content == "original"
    assert store.list_messages(b.id) == []


def test_add_message_unknown_session_raises_and_writes_nothing(store):
    with pytest.raises(SessionNotFound):
        store.add_message(make_msg("0" * 32), contexts=[ContextPage(page=1, text="x")])
    assert raw_rows(store, "SELECT COUNT(*) FROM messages")[0][0] == 0
    assert raw_rows(store, "SELECT COUNT(*) FROM message_contexts")[0][0] == 0


def test_add_message_duplicate_id_raises(store):
    sess = store.create_session()
    msg = make_msg(sess.id)
    store.add_message(msg)
    with pytest.raises(sqlite3.IntegrityError):
        store.add_message(msg)
    assert len(store.list_messages(sess.id)) == 1


# --------------------------------------------------------------------------------------------- contexts
def test_contexts_round_trip_large_unicode_and_order(store):
    sess = store.create_session()
    msg = make_msg(sess.id, "assistant")
    big = (UNICODE_TEXT + " ") * 1200                                    # ~ 60 KB of non-ASCII text
    assert len(big.encode("utf-8")) > 50 * 1024
    contexts = [ContextPage(page=p, text=f"{p}: {big}") for p in (90, 12, 88)]   # read order, not page order
    store.add_message(msg, contexts=contexts)

    assert store.get_contexts(msg.id) == contexts
    assert store.get_message(sess.id, msg.id).content == msg.content


def test_set_contexts_replaces_and_isolates_messages(store):
    sess = store.create_session()
    m1, m2 = make_msg(sess.id, "assistant"), make_msg(sess.id, "assistant")
    store.add_message(m1)
    store.add_message(m2)
    assert store.get_contexts(m1.id) == []                  # never set

    store.set_contexts(m1.id, [ContextPage(page=1, text="a"), ContextPage(page=2, text="b")])
    store.set_contexts(m2.id, [ContextPage(page=9, text="z")])
    store.set_contexts(m1.id, [ContextPage(page=3, text="c")])
    assert store.get_contexts(m1.id) == [ContextPage(page=3, text="c")]
    assert store.get_contexts(m2.id) == [ContextPage(page=9, text="z")]

    store.set_contexts(m1.id, [])
    assert store.get_contexts(m1.id) == []
    assert raw_rows(store, "SELECT COUNT(*) FROM message_contexts")[0][0] == 2


def test_set_contexts_for_missing_message_is_ignored(store):
    store.set_contexts("0" * 32, [ContextPage(page=1, text="x")])
    assert store.get_contexts("0" * 32) == []
    assert raw_rows(store, "SELECT COUNT(*) FROM message_contexts")[0][0] == 0


def test_contexts_do_not_inflate_message_rows(store):
    sess = store.create_session()
    msg = make_msg(sess.id, "assistant")
    store.add_message(msg, contexts=[ContextPage(page=1, text="x" * 50_000)])
    (size,) = raw_rows(store, "SELECT length(data) FROM messages WHERE id = ?", (msg.id,))[0]
    assert size < 2000


# --------------------------------------------------------------------------------------------- recovery
def test_recover_interrupted(store):
    indexing, ready, failed = (store.create_session(t) for t in ("indexing", "ready", "failed"))
    store.put_document(indexing.id, make_doc(stage="summarizing", progress=0.5, pi_doc_id="pi-" + "c" * 32))
    store.put_document(ready.id, make_doc(status="ready", stage="ready"))
    store.put_document(failed.id, make_doc(status="failed", stage="failed", error="Scanned PDF"))

    done = make_msg(ready.id, "assistant", "complete", status="answered")
    streaming = make_msg(ready.id, "assistant", "half an answ", status="streaming")
    streaming_too = make_msg(indexing.id, "assistant", "", status="streaming")
    user = make_msg(ready.id, "user", "q")
    for m in (user, done, streaming, streaming_too):
        store.add_message(m)
    updated_before = store.get_session(ready.id).updated_at

    assert store.recover_interrupted() == 3   # 1 document + 2 messages

    doc = store.get_document(indexing.id)
    assert (doc.status, doc.stage, doc.error) == ("failed", "failed", INTERRUPTED_INDEXING_ERROR)
    assert doc.progress == 0.5 and doc.pi_doc_id == "pi-" + "c" * 32   # everything else preserved
    assert store.get_session(indexing.id).state == "failed"

    m = store.get_message(ready.id, streaming.id)
    assert (m.status, m.error, m.content) == ("error", INTERRUPTED_ANSWER_ERROR, "half an answ")
    assert store.get_message(indexing.id, streaming_too.id).status == "error"
    assert store.get_message(ready.id, done.id) == done
    assert store.get_document(ready.id).status == "ready"
    assert store.get_document(failed.id).error == "Scanned PDF"
    assert store.get_session(ready.id).updated_at == updated_before

    assert store.recover_interrupted() == 0   # idempotent


def test_skip_interrupted_evaluations(store):
    from reportlens.models import EvalScores

    sess = store.create_session("scored")
    store.put_document(sess.id, make_doc(status="ready", stage="ready"))
    scored = {st: make_msg(sess.id, "assistant", st, status="answered") for st in ("pending", "running", "done")}
    for st, m in scored.items():
        m.evaluation = EvalScores(status=st, faithfulness=0.5 if st == "done" else None, n_contexts_input=3, n_contexts_scored=2)
        store.add_message(m)
    plain = make_msg(sess.id, "assistant", "no scores", status="no_sources")
    store.add_message(plain)

    assert store.skip_interrupted_evaluations() == 2
    for st in ("pending", "running"):
        ev = store.get_message(sess.id, scored[st].id).evaluation
        assert (ev.status, ev.skipped_reason, ev.n_contexts_input, ev.n_contexts_scored) == ("skipped", "interrupted", 3, 2)
    assert store.get_message(sess.id, scored["done"].id).evaluation.status == "done"
    assert store.get_message(sess.id, plain.id) == plain
    assert store.skip_interrupted_evaluations() == 0   # idempotent


def test_recover_interrupted_on_empty_store(store):
    assert store.recover_interrupted() == 0


def test_recovery_survives_a_restart(tmp_path):
    path = tmp_path / "db" / "r.db"
    first = Store(path)
    sess = first.create_session()
    first.put_document(sess.id, make_doc())
    msg = make_msg(sess.id, "assistant", status="streaming")
    first.add_message(msg)
    first.close()                      # simulates the process dying with work in flight

    second = Store(path)
    try:
        assert second.recover_interrupted() == 2
        assert second.get_session(sess.id).state == "failed"
        assert second.get_message(sess.id, msg.id).status == "error"
    finally:
        second.close()


# --------------------------------------------------------------------------------------------- concurrency
def test_concurrent_writers_do_not_hit_database_is_locked(store):
    threads_n, per_thread = 8, 50
    sessions = [store.create_session(f"s{i}") for i in range(threads_n)]
    shared = store.create_session("shared")
    errors: list[BaseException] = []
    barrier = threading.Barrier(threads_n)

    def worker(i: int) -> None:
        try:
            barrier.wait()
            for j in range(per_thread):
                msg = make_msg(sessions[i].id, content=f"{i}:{j}")
                store.add_message(msg, contexts=[ContextPage(page=j, text="x")])
                store.add_message(make_msg(shared.id, content=f"{i}:{j}"))
                if j % 10 == 0:
                    store.touch_session(sessions[i].id)
                    store.list_sessions()    # readers run alongside the writers
        except BaseException as exc:    # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors

    for i, sess in enumerate(sessions):
        assert [m.content for m in store.list_messages(sess.id)] == [f"{i}:{j}" for j in range(per_thread)]
    shared_msgs = store.list_messages(shared.id)
    assert len(shared_msgs) == threads_n * per_thread
    for i in range(threads_n):    # per-thread order is preserved inside the shared interleaving
        mine = [m.content for m in shared_msgs if m.content.startswith(f"{i}:")]
        assert mine == [f"{i}:{j}" for j in range(per_thread)]
    assert store.get_session(shared.id).message_count == threads_n * per_thread


def test_two_store_instances_on_one_file_write_concurrently(tmp_path):
    path = tmp_path / "shared.db"
    stores = [Store(path), Store(path)]
    sess = stores[0].create_session()
    errors: list[BaseException] = []

    def worker(s: Store) -> None:
        try:
            for _ in range(60):
                s.add_message(make_msg(sess.id))
        except BaseException as exc:    # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(stores[i % 2],)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    try:
        assert not errors, errors
        assert stores[1].get_session(sess.id).message_count == 360
    finally:
        for s in stores:
            s.close()


def test_concurrent_partial_document_updates_do_not_lose_writes(store):
    sess = store.create_session()
    store.put_document(sess.id, make_doc())
    barrier = threading.Barrier(2)

    def bump_progress() -> None:
        barrier.wait()
        for i in range(1, 51):
            store.update_document(sess.id, progress=i / 50)

    def bump_node_count() -> None:
        barrier.wait()
        for i in range(1, 51):
            store.update_document(sess.id, node_count=i)

    threads = [threading.Thread(target=f) for f in (bump_progress, bump_node_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    doc = store.get_document(sess.id)
    assert (doc.progress, doc.node_count) == (1.0, 50)


# --------------------------------------------------------------------------------------------- schema, lifecycle
def test_schema_is_versioned_and_reopen_is_idempotent(tmp_path):
    path = tmp_path / "v.db"
    first = Store(path)
    sess = first.create_session("persisted")
    first.put_document(sess.id, make_doc(pi_doc_id="pi-" + "d" * 32))
    first.close()

    second = Store(path)      # migration runner must be a no-op now
    third = Store(path)       # ... even with two live instances
    try:
        assert raw_rows(second, "PRAGMA user_version") == [(len(_MIGRATIONS),)]
        tables = {r[0] for r in raw_rows(second, "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"sessions", "documents", "messages", "message_contexts"} <= tables
        assert third.get_session(sess.id).title == "persisted"
        assert second.get_document(sess.id).pi_doc_id == "pi-" + "d" * 32
    finally:
        second.close()
        third.close()


def test_concurrent_first_open_migrates_exactly_once(tmp_path):
    path = tmp_path / "race.db"
    opened: list[Store] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def opener() -> None:
        try:
            barrier.wait()
            opened.append(Store(path))
        except BaseException as exc:    # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    try:
        assert not errors, errors
        assert raw_rows(opened[0], "PRAGMA user_version") == [(len(_MIGRATIONS),)]
    finally:
        for s in opened:
            s.close()


def test_database_from_a_newer_version_is_refused(tmp_path):
    path = tmp_path / "future.db"
    Store(path).close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        Store(path)


def test_connection_pragmas(store):
    assert raw_rows(store, "PRAGMA journal_mode") == [("wal",)]
    conn = store._new_connection()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    finally:
        conn.close()


def test_foreign_keys_are_enforced(store):
    with pytest.raises(sqlite3.IntegrityError):
        conn = store._new_connection()
        try:
            conn.execute("INSERT INTO documents (session_id, status, data) VALUES ('nope', 'ready', '{}')")
        finally:
            conn.close()


def test_wal_file_survives_between_calls(store):
    # The idle pool stops SQLite from checkpointing and deleting the WAL after every single call.
    store.create_session()
    wal = Path(str(store._path) + "-wal")
    assert wal.exists()
    store.create_session()
    assert wal.exists()


def test_connections_are_reused_and_the_pool_is_bounded(store):
    for _ in range(20):
        store.create_session()
        store.list_sessions()
    assert len(store._idle) == 1                    # sequential callers share one connection

    sessions = [store.create_session() for _ in range(3)]
    barrier = threading.Barrier(20)

    def hammer() -> None:
        barrier.wait()
        for _ in range(10):
            store.get_session(sessions[0].id)

    threads = [threading.Thread(target=hammer) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert 1 <= len(store._idle) <= store_module.MAX_IDLE_CONNECTIONS
    assert all(not c.in_transaction for c in store._idle)


def test_failed_calls_leave_the_pool_usable(store):
    sess = store.create_session()
    msg = make_msg(sess.id)
    store.add_message(msg)
    with pytest.raises(sqlite3.IntegrityError):       # fails after BEGIN IMMEDIATE: must roll back cleanly
        store.add_message(msg)
    with pytest.raises(ValueError):
        store.rename_session(sess.id, " ")
    assert all(not c.in_transaction for c in store._idle)
    store.add_message(make_msg(sess.id))              # lock released, connection healthy
    assert store.get_session(sess.id).message_count == 2


def test_connection_checked_out_during_close_is_closed_on_return(tmp_path):
    s = Store(tmp_path / "late.db")
    conn = s._checkout()
    s.close()
    s._checkin(conn)
    with pytest.raises(sqlite3.ProgrammingError):     # closed, not returned to a dead pool
        conn.execute("SELECT 1")
    assert s._idle == []


def test_close_is_idempotent_and_releases_the_file(tmp_path):
    path = tmp_path / "c.db"
    s = Store(path)
    s.create_session()
    s.close()
    s.close()
    with pytest.raises(RuntimeError, match="closed"):
        s.list_sessions()
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if p.exists():
            p.unlink()          # would fail on Windows if a handle were still open
    assert not path.exists()


def test_close_works_from_another_thread(tmp_path):
    s = Store(tmp_path / "t.db")
    errors: list[BaseException] = []

    def closer() -> None:
        try:
            s.close()
        except BaseException as exc:    # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=closer)
    t.start()
    t.join(timeout=10)
    assert not errors


# --------------------------------------------------------------------------------------------- corrupt rows
def test_unreadable_rows_are_skipped_not_fatal(store, caplog):
    sess = store.create_session()
    store.put_document(sess.id, make_doc(status="ready"))
    good, bad, bad2 = (make_msg(sess.id, content=c) for c in ("good", "bad", "bad2"))
    for m in (good, bad, bad2):
        store.add_message(m)
    store.set_contexts(good.id, [ContextPage(page=1, text="x")])
    raw_exec(store, "UPDATE messages SET data = 'not json' WHERE id = ?", (bad.id,))
    raw_exec(store, "UPDATE messages SET data = '[1, 2]' WHERE id = ?", (bad2.id,))
    raw_exec(store, "UPDATE message_contexts SET data = '{oops' WHERE message_id = ?", (good.id,))

    with caplog.at_level(logging.ERROR, logger="reportlens.store"):
        assert [m.id for m in store.list_messages(sess.id)] == [good.id]
        assert store.get_message(sess.id, bad.id) is None
        assert store.get_contexts(good.id) == []
    assert "unreadable" in caplog.text.lower()

    raw_exec(store, "UPDATE documents SET data = '{\"id\": 5}' WHERE session_id = ?", (sess.id,))
    s = store.get_session(sess.id)         # sidebar must still load
    assert s.document is None and s.id == sess.id
    assert len(store.list_sessions()) == 1
