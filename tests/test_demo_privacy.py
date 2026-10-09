"""The public-facing pieces added for "Annual Report Lens": the read-only demo chat and private chats per browser.

* demo: export a real chat (`reportlens.demo.export_session`), install it at start-up (`install_demo`), anyone may read it
  without the access code, nobody may change it, "ask your own question about this report" clones it, and the spend budget
  and the chat cap ignore it.
* private chats: with an access code, each browser (signed `rl_visitor` cookie) sees and opens only its own chats; someone
  else's chat is a 404, exactly like one that does not exist.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from reportlens.config import load_settings
from reportlens.demo import CHAT_FILE, DEMO_SESSION_ID, export_session, install_demo
from reportlens.store import _MIGRATIONS, DEMO_OWNER, Store
from reportlens.web.app import create_app
from reportlens.web.auth import VISITOR_COOKIE, AccessGate
from tests.test_service import build_env, collect, payload  # noqa: F401 - fixtures

CODE = "correct horse battery staple 42"


def client(app, *, ip: str = "203.0.113.9") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 4321)), base_url="http://testserver")


@pytest.fixture
async def demo_dir(build_env, tmp_path) -> Path:
    """A demo folder exported from a real (fake-model) chat with one answered question."""
    source = build_env(data_dir=tmp_path / "source-data")                  # its own database: the demo is exported from elsewhere
    sid = source.ready_session()
    events = await collect(source.service.ask(sid, "How many customers?"))
    assert payload(events, "answer_done")
    out = tmp_path / "demo"
    export_session(source.store, source.settings, sid, out, title="Demo: Northbridge", attribution="Northbridge plc (c)",
                   attribution_url="https://example.com/ir", with_files=True)          # the full demo: PDF, index and page texts
    return out


@pytest.fixture
def public(build_env, demo_dir):
    """(env, app) of a gated deployment with private chats and the demo installed."""
    env = build_env(access_code=CODE, private_chats=True, demo_dir=demo_dir, access_request_email="owner@example.com")
    app = create_app(env.settings, env.service)
    app.state.demo = install_demo(env.settings, env.store)
    assert app.state.demo is not None
    return env, app


async def login(c: httpx.AsyncClient) -> None:
    r = await c.post("/api/login", json={"code": CODE})
    assert r.status_code == 200, r.text


# ============================================================================================ export / install
def test_export_and_install_round_trip_and_reinstall_is_idempotent(build_env, demo_dir):
    chat = json.loads((demo_dir / CHAT_FILE).read_text(encoding="utf-8"))
    assert chat["title"] == "Demo: Northbridge" and chat["document"]["pi_doc_id"].startswith("pi-")
    assert (demo_dir / "files" / chat["document"]["doc_name"]).is_file() and (demo_dir / "files" / "pageindex").is_dir()
    env = build_env(demo_dir=demo_dir)
    for _ in range(2):                                                     # every start-up replaces the previous copy
        info = install_demo(env.settings, env.store)
        assert info is not None and info.session_id == DEMO_SESSION_ID and info.questions == 1
    session = env.store.get_session(DEMO_SESSION_ID)
    assert session.owner == DEMO_OWNER and session.read_only and session.document.status == "ready"
    messages = env.store.list_messages(DEMO_SESSION_ID)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert all(m.session_id == DEMO_SESSION_ID for m in messages)
    source_ids = {m["message"]["id"] for m in chat["messages"]}
    assert not source_ids & {m.id for m in messages}, "the copy gets its own message ids"
    assert env.store.get_contexts(messages[1].id), "the page texts behind the answer came along"
    assert (env.settings.session_dir(DEMO_SESSION_ID) / session.document.doc_name).is_file()
    assert env.service.list_sessions() == [], "the demo is not one of 'your' chats"


def test_a_missing_or_broken_demo_never_stops_the_app(build_env, demo_dir, tmp_path):
    env = build_env(demo_dir=tmp_path / "nothing-here")
    assert install_demo(env.settings, env.store) is None
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / CHAT_FILE).write_text('{"format": 1, "document": {}}', encoding="utf-8")
    env2 = build_env(demo_dir=broken)
    assert install_demo(env2.settings, env2.store) is None
    assert env2.store.get_session(DEMO_SESSION_ID) is None


def test_the_demo_costs_nothing_against_the_budget_or_the_chat_cap(build_env, demo_dir):
    env = build_env(demo_dir=demo_dir, budget_usd_total=1.0, max_sessions=1)
    install_demo(env.settings, env.store)
    demo_answer = env.store.list_messages(DEMO_SESSION_ID)[1]
    assert demo_answer.usage is not None                                   # it did record a cost when it was made
    snap = env.store.usage_snapshot()
    assert snap.answer_cost_usd == 0 and snap.index_keys == () and snap.evaluations == 0
    assert env.store.count_sessions() == 0
    env.service.create_session()                                           # the one allowed chat is still available


def test_settings_for_the_public_pieces(tmp_path):
    gated = load_settings(environ={"ACCESS_CODE": "x"})
    assert gated.private_chats and gated.demo_dir is None                   # tests (environ=...) never pick up the project's demo
    assert not load_settings(environ={}).private_chats
    assert not load_settings(environ={"ACCESS_CODE": "x", "PRIVATE_CHATS": "0"}).private_chats
    s = load_settings(environ={"DEMO_DIR": str(tmp_path), "ACCESS_REQUEST_EMAIL": " me@example.com "})
    assert s.demo_dir == tmp_path and s.access_request_email == "me@example.com"
    assert load_settings(environ={"DEMO_DIR": "  "}).demo_dir is None


def test_an_existing_database_gains_the_owner_column_and_keeps_its_chats(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    for statement in _MIGRATIONS[0]:
        conn.execute(statement)
    conn.execute("INSERT INTO sessions (id, title, created_at, updated_at, updated_seq) VALUES (?, 'Old chat', 't', 't', 1)", ("a" * 32,))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    store = Store(db)
    try:
        [old] = store.list_sessions()
        assert old.title == "Old chat" and old.owner == "" and not old.read_only
        assert store.session_owner("a" * 32) == "" and store.session_owner("b" * 32) is None
    finally:
        store.close()


# ============================================================================================ the demo over HTTP
async def test_the_demo_is_readable_without_a_code(public):
    env, app = public
    async with client(app) as c:
        auth = (await c.get("/api/auth")).json()
        assert auth == {"required": True, "authenticated": False, "request_email": "owner@example.com"}
        demo = (await c.get("/api/demo")).json()
        assert demo["available"] and demo["session_id"] == DEMO_SESSION_ID and demo["attribution_url"] == "https://example.com/ir"
        assert (await c.get("/api/config")).status_code == 200
        detail = (await c.get(f"/api/sessions/{DEMO_SESSION_ID}")).json()
        assert detail["read_only"] and len(detail["messages"]) == 2 and "owner" not in detail
        mid = detail["messages"][1]["id"]
        assert (await c.get(f"/api/sessions/{DEMO_SESSION_ID}/messages/{mid}")).status_code == 200
        pdf = await c.get(f"/api/sessions/{DEMO_SESSION_ID}/document/file", headers={"range": "bytes=0-9"})
        assert pdf.status_code == 206 and pdf.content.startswith(b"%PDF")
        assert (await c.get(f"/api/sessions/{DEMO_SESSION_ID}/document/pages")).status_code == 200
        # everything else still needs the code
        for method, path in [("GET", "/api/sessions"), ("POST", "/api/sessions"), ("POST", f"/api/sessions/{DEMO_SESSION_ID}/messages"),
                             ("DELETE", f"/api/sessions/{DEMO_SESSION_ID}")]:
            assert (await c.request(method, path)).status_code == 401, (method, path)


async def test_nobody_can_change_the_demo_even_with_the_code(public):
    env, app = public
    mid = env.store.list_messages(DEMO_SESSION_ID)[1].id
    async with client(app) as c:
        await login(c)
        for method, path, kw in [("POST", f"/api/sessions/{DEMO_SESSION_ID}/messages", {"json": {"content": "q?"}}),
                                 ("DELETE", f"/api/sessions/{DEMO_SESSION_ID}", {}),
                                 ("PATCH", f"/api/sessions/{DEMO_SESSION_ID}", {"json": {"title": "mine now"}}),
                                 ("POST", f"/api/sessions/{DEMO_SESSION_ID}/document", {"files": {"file": ("a.pdf", b"%PDF-1.4", "application/pdf")}}),
                                 ("POST", f"/api/sessions/{DEMO_SESSION_ID}/messages/{mid}/evaluate", {})]:
            r = await c.request(method, path, **kw)
            assert r.status_code == 403 and r.json()["error"]["code"] == "demo_read_only", (method, path, r.text)
    assert env.store.get_session(DEMO_SESSION_ID).title == "Demo: Northbridge"


async def test_the_service_refuses_demo_writes_on_its_own_too(public):
    env, _ = public
    with pytest.raises(Exception) as err:
        env.service.delete_session(DEMO_SESSION_ID)
    assert getattr(err.value, "code", "") == "demo_read_only"
    with pytest.raises(Exception) as err:
        await collect(env.service.ask(DEMO_SESSION_ID, "anything?"))
    assert getattr(err.value, "code", "") == "demo_read_only"


async def test_asking_your_own_question_about_the_demo_report_clones_it(public):
    env, app = public
    async with client(app) as c:
        await login(c)
        r = await c.post("/api/sessions", json={"from_session": DEMO_SESSION_ID})
        assert r.status_code == 201, r.text
        new = r.json()
        assert new["state"] == "ready" and not new["read_only"] and new["document"]["filename"]
        assert [s["id"] for s in (await c.get("/api/sessions")).json()] == [new["id"]]


# ============================================================================================ private chats
async def test_each_browser_sees_and_opens_only_its_own_chats(public):
    env, app = public
    async with client(app, ip="198.51.100.1") as a, client(app, ip="198.51.100.2") as b:
        await login(a)
        await login(b)
        sid_a = (await a.post("/api/sessions")).json()["id"]
        sid_b = (await b.post("/api/sessions")).json()["id"]
        assert [s["id"] for s in (await a.get("/api/sessions")).json()] == [sid_a]
        assert [s["id"] for s in (await b.get("/api/sessions")).json()] == [sid_b]
        for method, path, kw in [("GET", f"/api/sessions/{sid_a}", {}), ("DELETE", f"/api/sessions/{sid_a}", {}),
                                 ("PATCH", f"/api/sessions/{sid_a}", {"json": {"title": "x"}}),
                                 ("POST", f"/api/sessions/{sid_a}/messages", {"json": {"content": "q?"}}),
                                 ("GET", f"/api/sessions/{sid_a}/document/file", {}),
                                 ("POST", "/api/sessions", {"json": {"from_session": sid_a}})]:
            r = await b.request(method, path, **kw)
            assert r.status_code == 404 and r.json()["error"]["code"] == "session_not_found", (method, path, r.text)
        assert env.store.get_session(sid_a) is not None, "b could not delete a's chat"


async def test_the_visitor_cookie_is_signed_httponly_and_long_lived(public):
    env, app = public
    async with client(app) as c:
        first = await c.get("/api/auth")
        cookie = first.headers["set-cookie"]
        assert cookie.startswith(f"{VISITOR_COOKIE}=") and "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Max-Age=31536000" in cookie
        assert "set-cookie" not in (await c.get("/api/auth")).headers, "issued once, then reused"
        await login(c)
        sid = (await c.post("/api/sessions")).json()["id"]
        token = c.cookies[VISITOR_COOKIE]
        vid, signature = token.split(".")
        c.cookies.set(VISITOR_COOKIE, f"{'f' * 32}.{signature}")                # someone else's id with this signature
        r = await c.get(f"/api/sessions/{sid}")
        assert r.status_code == 404 and VISITOR_COOKIE in r.headers.get("set-cookie", ""), "a forged id is replaced, not trusted"


def test_visitor_ids_survive_a_restart_with_the_same_secret_or_code(settings):
    one = AccessGate(settings.with_(access_code=CODE))
    _, token = one.issue_visitor()
    scope = {"type": "http", "headers": [(b"cookie", f"{VISITOR_COOKIE}={token}".encode())]}
    assert AccessGate(settings.with_(access_code=CODE)).visitor_from_scope(scope)                    # same code, new process
    assert AccessGate(settings.with_(access_code=CODE, session_secret="s1")).visitor_from_scope(scope) is None
    two = AccessGate(settings.with_(access_code=CODE, session_secret="s1"))
    _, token2 = two.issue_visitor()
    scope2 = {"type": "http", "headers": [(b"cookie", f"{VISITOR_COOKIE}={token2}".encode())]}
    assert AccessGate(settings.with_(access_code="a new code", session_secret="s1")).visitor_from_scope(scope2), \
        "changing the access code does not orphan anyone's chats"


async def test_without_private_chats_the_local_app_shows_every_chat(build_env, demo_dir):
    env = build_env(demo_dir=demo_dir)                                      # no access code: the single-user local app
    app = create_app(env.settings, env.service)
    app.state.demo = install_demo(env.settings, env.store)
    async with client(app, ip="127.0.0.1") as a, client(app, ip="127.0.0.2") as b:
        sid = (await a.post("/api/sessions")).json()["id"]
        assert [s["id"] for s in (await b.get("/api/sessions")).json()] == [sid]
        assert "set-cookie" not in (await b.get("/api/sessions")).headers
        assert (await b.get(f"/api/sessions/{DEMO_SESSION_ID}")).json()["read_only"]
