"""End-to-end: the REAL stack (service, indexer, PageIndex SDK, QA engine, citations, RAGAS) behind a real uvicorn server,
talking to the fake OpenAI server (demo mode). Nothing leaves the machine and nothing costs money.

The mock gives canned extractive answers, so these tests prove wiring and contracts (event order, citation resolution,
persistence, locking, cleanup), not answer quality or score values.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, Optional

import httpx
import pytest
import uvicorn

from reportlens import pageindex_compat
from reportlens.config import load_settings
from reportlens.web.app import create_app

pytestmark = pytest.mark.slow

EVENT_ORDER = ["message_start", "answer_done", "eval_started", "eval_done", "done"]
METRICS = ["faithfulness", "answer_relevancy", "context_precision"]
INDEX_TIMEOUT_S = 180


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, base_url: str, data_dir: Path):
        self.base_url = base_url
        self.data_dir = data_dir


@pytest.fixture(scope="module")
def live(tmp_path_factory) -> Iterator[LiveServer]:
    """uvicorn in a thread, lifespan on: builds the real service on the demo mock exactly like `python -m reportlens --demo`."""
    data_dir = tmp_path_factory.mktemp("rl") / "d"          # short on purpose: PageIndex paths hit Windows MAX_PATH quickly
    settings = load_settings(environ={}).with_(data_dir=data_dir, demo_mock=True)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning",
                                           log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, name="e2e-uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + 90
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the e2e server did not start (see the captured log above)")
        time.sleep(0.1)
    try:
        yield LiveServer(f"http://127.0.0.1:{port}", data_dir)
    finally:
        server.should_exit = True
        thread.join(60)
        assert not thread.is_alive(), "server did not shut down"
        pageindex_compat.remove_patches()               # leave the SDK as other test modules expect to find it
        pageindex_compat.restore_openai_env()


@pytest.fixture(scope="module")
def http(live) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=live.base_url, timeout=120) as client:
        yield client


def parse_frames(lines: Iterator[str]) -> Iterator[tuple[str, Optional[dict]]]:
    """Incremental SSE parser: yields ("ping", None) for comments and (name, payload) for events, as they arrive."""
    name, data = None, []
    for line in lines:
        if line == "":
            if name is not None:
                yield name, json.loads("\n".join(data))
            name, data = None, []
        elif line.startswith(":"):
            yield "ping", None
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


def ask(http: httpx.Client, sid: str, question: str) -> list[tuple[str, dict, float]]:
    """POST a question and read the whole stream; returns (event, payload, seconds since request) in arrival order."""
    started = time.monotonic()
    events = []
    with http.stream("POST", f"/api/sessions/{sid}/messages", json={"content": question}) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        for name, payload in parse_frames(response.iter_lines()):
            if name != "ping":
                events.append((name, payload, time.monotonic() - started))
    return events


def wait_until_ready(http: httpx.Client, sid: str) -> dict:
    deadline = time.monotonic() + INDEX_TIMEOUT_S
    while time.monotonic() < deadline:
        session = http.get(f"/api/sessions/{sid}").json()
        if session["state"] in ("ready", "failed"):
            return session
        time.sleep(0.5)
    raise AssertionError("indexing did not finish in time")


@pytest.fixture(scope="module")
def indexed(http, sample_pdf) -> str:
    """A session whose document is uploaded and indexed (shared by the tests below; nothing asks a question in it)."""
    created = http.post("/api/sessions", json={})
    assert created.status_code == 201 and created.json()["state"] == "empty"
    sid = created.json()["id"]
    with sample_pdf.open("rb") as f:
        uploaded = http.post(f"/api/sessions/{sid}/document", files={"file": ("Northbridge Annual Report 2025-26.pdf", f, "application/pdf")})
    assert uploaded.status_code == 202, uploaded.text
    assert uploaded.json()["state"] == "indexing" and uploaded.json()["document"]["status"] == "indexing"
    session = wait_until_ready(http, sid)
    assert session["state"] == "ready", session["document"].get("error")
    return sid


def test_server_reports_demo_mode(http):
    health = http.get("/api/health").json()
    assert health["ok"] and health["demo_mock"] is True and health["openai_configured"] is True
    config = http.get("/api/config").json()
    assert config["demo_mock"] is True and set(config["metrics"]) >= set(METRICS)
    assert "api_key" not in json.dumps(config)
    assert http.get("/").status_code == 200


def test_indexed_document_is_served_and_explorable(http, indexed, sample_pdf):
    session = http.get(f"/api/sessions/{indexed}").json()
    document = session["document"]
    assert document["status"] == "ready" and document["page_count"] == 60 and document["node_count"]
    assert "pi_doc_id" not in document

    pdf = http.get(f"/api/sessions/{indexed}/document/file")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content == sample_pdf.read_bytes()
    part = http.get(f"/api/sessions/{indexed}/document/file", headers={"Range": "bytes=0-1023"})
    assert part.status_code == 206 and part.headers["content-range"].startswith("bytes 0-1023/") and len(part.content) == 1024

    pages = http.get(f"/api/sessions/{indexed}/document/pages").json()
    assert pages["page_count"] == 60 and len(pages["pages"]) == 60 and pages["pages"][14]["printed_page"] == "13"   # folio = physical - 2
    outline = http.get(f"/api/sessions/{indexed}/document/outline").json()["nodes"]
    assert outline and all({"title", "node_id", "start_index", "end_index", "nodes"} <= set(n) for n in outline)


def test_locate_finds_a_fact_on_its_page(http, indexed, sample_facts):
    fact = next(f for f in sample_facts if f["id"] == "customers_connected")
    found = http.get(f"/api/sessions/{indexed}/locate", params={"page": fact["page"], "quote": fact["quote"]}).json()
    assert found["page"] == fact["page"] and found["method"] in ("exact", "fuzzy", "fragments") and found["rects"]
    assert all(0 <= r["x"] <= 1 and 0 <= r["y"] <= 1 and 0 < r["w"] <= 1 and 0 < r["h"] <= 1 for r in found["rects"])
    nothing = http.get(f"/api/sessions/{indexed}/locate", params={"page": 1, "quote": "zebra giraffe quokka unrelated"})
    assert nothing.status_code == 200 and nothing.json()["method"] in ("page", "none", "block")
    assert http.get(f"/api/sessions/{indexed}/locate", params={"page": 999, "quote": "x"}).status_code == 400


def test_upload_validation_on_the_real_stack(http, live):
    sid = http.post("/api/sessions", json={}).json()["id"]
    not_pdf = http.post(f"/api/sessions/{sid}/document", files={"file": ("notes.pdf", b"hello, this is text", "application/pdf")})
    assert not_pdf.status_code == 400 and not_pdf.json()["error"]["code"] == "invalid_pdf"
    assert http.get(f"/api/sessions/{sid}").json()["state"] == "empty"
    assert not list((live.data_dir / "tmp").glob("*")), "temporary uploads must be cleaned up"
    assert http.delete(f"/api/sessions/{sid}").status_code == 204


def test_question_streams_events_citations_and_scores(http, indexed, sample_facts):
    """The heart of the product, through a real socket: stream order, no <cite> leakage, resolved citations, RAGAS scores."""
    sid = http.post("/api/sessions", json={"from_session": indexed}).json()["id"]
    assert http.get(f"/api/sessions/{sid}").json()["state"] == "ready"
    fact = next(f for f in sample_facts if f["id"] == "customers_connected")

    events = ask(http, sid, fact["question"])
    names = [n for n, _, _ in events]
    assert names[0] == "message_start" and names[-1] == "done" and names.count("done") == 1
    assert [n for n in names if n in EVENT_ORDER] == EVENT_ORDER, names
    assert names.index("answer_done") < names.index("eval_started") < names.index("eval_done")
    assert "error" not in names, [p for n, p, _ in events if n == "error"]
    assert names.count("eval_result") == 3 and {p["metric"] for n, p, _ in events if n == "eval_result"} == set(METRICS)
    step_ids = {p["step"]["id"] for n, p, _ in events if n == "step"}
    assert step_ids and {p["step_id"] for n, p, _ in events if n == "step_done"} <= step_ids
    assert any(p["step"]["tool"] == "get_page_content" for n, p, _ in events if n == "step")

    start = next(p for n, p, _ in events if n == "message_start")
    mid = start["message_id"]
    assert start["user_message"]["content"] == fact["question"] and start["user_message"]["role"] == "user"
    assert all(p.get("message_id") == mid for n, p, _ in events if n in ("step", "step_done", "token", "citation", "eval_started", "eval_result", "eval_done"))

    streamed = "".join(p["text"] for n, p, _ in events if n == "token")
    assert streamed and "<cite" not in streamed and "/>" not in streamed and "quote=" not in streamed
    assert "[[c1]]" in streamed

    message = next(p for n, p, _ in events if n == "answer_done")["message"]
    assert message["id"] == mid and message["status"] == "answered" and message["content"] == streamed
    assert message["evaluation"]["status"] in ("pending", "skipped") and message["usage"] is not None
    located = [c for c in message["citations"] if c["page"] == fact["page"] and c["rects"] and c["quote"]]
    assert located, message["citations"]
    best = located[0]
    assert fact["key"] in best["quote"] and best["quote_source"] in ("model", "aligned")
    assert best["match_method"] in ("exact", "fuzzy", "fragments") and best["printed_page"] == fact["printed_page"]
    assert best["section_path"] == fact["section_path"] and best["doc_name"].endswith(".pdf")
    assert all(0 <= r["x"] <= 1 and 0 <= r["y"] <= 1 for c in message["citations"] for r in c["rects"])
    assert message["sources"] and message["sources"][0]["page"] == fact["page"]

    scores = next(p for n, p, _ in events if n == "eval_done")["evaluation"]
    assert scores["status"] == "done"
    for metric in METRICS:
        assert isinstance(scores[metric], float) and 0.0 <= scores[metric] <= 1.0, (metric, scores)

    # flushed per event, not buffered to the end: the answer is on the wire well before RAGAS finishes
    at = {n: t for n, _, t in events}
    assert at["message_start"] < at["answer_done"] < at["eval_done"] and at["eval_done"] - at["message_start"] > 0.05

    # persisted: a reload shows the same conversation, and the session is now locked and titled after the question
    session = http.get(f"/api/sessions/{sid}").json()
    assert session["state"] == "locked" and session["message_count"] == 2 and session["title"].startswith(fact["question"][:20])
    user, assistant = session["messages"]
    assert (user["role"], assistant["role"]) == ("user", "assistant") and assistant["id"] == mid
    assert assistant["content"] == message["content"] and assistant["citations"] == message["citations"]
    assert assistant["evaluation"]["status"] == "done" and assistant["evaluation"]["faithfulness"] == scores["faithfulness"]
    assert http.get(f"/api/sessions/{sid}/messages/{mid}").json()["evaluation"]["status"] == "done"
    assert any(s["id"] == sid for s in http.get("/api/sessions").json())

    # the chip's highlight can be re-derived on demand
    again = http.get(f"/api/sessions/{sid}/locate", params={"page": best["page"], "quote": best["quote"]}).json()
    assert again["rects"] and again["page"] == best["page"]

    # a locked chat refuses a second document, and re-scoring works
    locked = http.post(f"/api/sessions/{sid}/document", files={"file": ("x.pdf", b"%PDF-1.4 " + b"x" * 100, "application/pdf")})
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "document_locked"
    rescored = http.post(f"/api/sessions/{sid}/messages/{mid}/evaluate")
    assert rescored.status_code == 200 and rescored.json()["status"] == "done"

    # a second question in the same chat works and is appended
    second = ask(http, sid, "What was the lost-time injury frequency rate in 2025/26?")
    assert [n for n, _, _ in second][-1] == "done" and "error" not in [n for n, _, _ in second]
    assert http.get(f"/api/sessions/{sid}").json()["message_count"] == 4
    assert http.delete(f"/api/sessions/{sid}").status_code == 204


def test_second_upload_to_an_indexed_chat_is_refused(http, indexed, sample_pdf):
    with sample_pdf.open("rb") as f:
        response = http.post(f"/api/sessions/{indexed}/document", files={"file": ("again.pdf", f, "application/pdf")})
    assert response.status_code == 409 and response.json()["error"]["code"] == "document_already_uploaded"


def test_new_chat_from_a_session_is_ready_without_reindexing_and_delete_cleans_up(http, live, indexed):
    clone = http.post("/api/sessions", json={"from_session": indexed})
    assert clone.status_code == 201 and clone.json()["state"] == "ready" and clone.json()["document"]["status"] == "ready"
    sid = clone.json()["id"]
    assert (live.data_dir / "sessions" / sid).is_dir()
    assert http.get(f"/api/sessions/{sid}/document/file").status_code == 200
    assert http.post("/api/sessions", json={"from_session": "f" * 32}).status_code == 404
    unready = http.post("/api/sessions", json={}).json()["id"]
    assert http.post("/api/sessions", json={"from_session": unready}).status_code == 409

    assert http.delete(f"/api/sessions/{sid}").status_code == 204
    assert http.get(f"/api/sessions/{sid}").status_code == 404
    assert not (live.data_dir / "sessions" / sid).exists(), "the chat's files must be removed with it"
    assert http.get(f"/api/sessions/{indexed}").json()["state"] == "ready", "deleting a copy must not touch the original"
    assert http.delete(f"/api/sessions/{unready}").status_code == 204


def test_errors_before_the_stream_are_json(http, indexed):
    assert http.post(f"/api/sessions/{indexed}/messages", json={"content": "  "}).status_code == 400
    empty = http.post("/api/sessions", json={}).json()["id"]
    not_ready = http.post(f"/api/sessions/{empty}/messages", json={"content": "hello?"})
    assert not_ready.status_code == 409 and not_ready.json()["error"]["code"] == "document_not_ready"
    assert http.post(f"/api/sessions/{'0' * 32}/messages", json={"content": "hello?"}).status_code == 404
    assert http.delete(f"/api/sessions/{empty}").status_code == 204


def test_closing_the_stream_cancels_the_run_and_frees_the_chat(http, indexed):
    sid = http.post("/api/sessions", json={"from_session": indexed}).json()["id"]
    with http.stream("POST", f"/api/sessions/{sid}/messages", json={"content": "How many customers does Northbridge connect?"}) as response:
        first = next(parse_frames(response.iter_lines()))
        assert first[0] == "message_start"
    # leaving the `with` closed the socket mid-answer: the server must stop the run and mark the answer cancelled
    deadline = time.monotonic() + 20
    assistant = None
    while time.monotonic() < deadline:
        messages = http.get(f"/api/sessions/{sid}").json()["messages"]
        assistant = next((m for m in messages if m["role"] == "assistant"), None)
        if assistant and assistant["status"] != "streaming":
            break
        time.sleep(0.2)
    assert assistant is not None and (assistant["status"], assistant["error"]) == ("error", "cancelled"), assistant
    events = ask(http, sid, "How many customers does Northbridge connect?")           # not stuck in session_busy
    assert [n for n, _, _ in events][-1] == "done"
    assert http.delete(f"/api/sessions/{sid}").status_code == 204


# --------------------------------------------------------------------------------------------- scripts/live_smoke.py
FAKE_KEY = "sk-test-not-real-0123456789"


def smoke_environ(mock_openai) -> dict:
    return {"OPENAI_API_KEY": FAKE_KEY, "OPENAI_BASE_URL": mock_openai.base_url}


def test_live_smoke_refuses_to_spend_without_yes(mock_openai, sample_pdf, capsys):
    from scripts import live_smoke

    code = live_smoke.main(["--pdf", str(sample_pdf)], environ=smoke_environ(mock_openai))
    out = capsys.readouterr().out
    assert code == live_smoke.EXIT_REFUSED and "Refusing to spend money without --yes" in out
    assert "gpt-5.6-sol" in out and "estimated cost" in out and FAKE_KEY not in out
    assert mock_openai.requests == [], "no request may be sent before --yes"


def test_live_smoke_needs_a_key_and_a_pdf(mock_openai, sample_pdf, tmp_path, capsys):
    from scripts import live_smoke

    assert live_smoke.main(["--yes"], environ={}) == live_smoke.EXIT_REFUSED
    assert "OPENAI_API_KEY" in capsys.readouterr().out
    missing = live_smoke.main(["--yes", "--pdf", str(tmp_path / "nope.pdf")], environ=smoke_environ(mock_openai))
    assert missing == live_smoke.EXIT_REFUSED and "PDF not found" in capsys.readouterr().out
    demo = live_smoke.main(["--yes"], environ={**smoke_environ(mock_openai), "REPORTLENS_DEMO_MOCK": "1"})
    assert demo == live_smoke.EXIT_REFUSED
    assert mock_openai.requests == []


def test_live_smoke_preflight_failure_stops_before_indexing(mock_openai, sample_pdf, capsys, monkeypatch):
    import openai

    from scripts import live_smoke

    class StubClient:
        """models.retrieve is a GET, which the mock cannot be told to fail, so the OpenAI client is replaced."""

        def __init__(self, **_: object):
            self.models = self

        def retrieve(self, model: str):
            if model == "gpt-5.6-sol":
                request = httpx.Request("GET", "http://x/v1/models/gpt-5.6-sol")
                raise openai.NotFoundError(f"The model `{model}` does not exist (key sk-abcdef0123456789)",
                                           response=httpx.Response(404, request=request), body=None)
            return SimpleNamespace(id=model, shutdown_date="2026-12-11" if model == "gpt-5.6-luna" else None)

        def close(self) -> None:
            pass

    monkeypatch.setattr(openai, "OpenAI", StubClient)
    code = live_smoke.main(["--yes", "--pdf", str(sample_pdf)], environ=smoke_environ(mock_openai))
    out = capsys.readouterr().out
    assert code == live_smoke.EXIT_FAILED and "Preflight failed" in out
    assert re.search(r"FAIL\s+chat\s+gpt-5.6-sol: HTTP 404", out) and "scheduled to shut down on 2026-12-11" in out
    assert "sk-abcdef0123456789" not in out and FAKE_KEY not in out, "keys quoted in error bodies must be scrubbed"
    assert mock_openai.requests == [], "nothing may be indexed after a failed preflight"


@pytest.mark.slow
def test_live_smoke_full_run_against_the_mock(mock_openai, sample_pdf, sample_facts, capsys):
    """The whole script against the fake server: preflight, index, ask, citations, scores; exit code 0, key never printed."""
    from scripts import live_smoke

    fact = next(f for f in sample_facts if f["id"] == "customers_connected")
    try:
        code = live_smoke.main(["--yes", "--pdf", str(sample_pdf), "--question", fact["question"]], environ=smoke_environ(mock_openai))
    finally:
        pageindex_compat.remove_patches()
        pageindex_compat.restore_openai_env()
    out = capsys.readouterr().out
    assert code == live_smoke.EXIT_OK, out
    for needle in ("PREFLIGHT", "ok    index", "INDEXING", "ANSWER (status: answered", "CITATIONS", "highlight rects:", "PAGES READ BY THE AGENT:",
                   "USAGE", "RAGAS", "faithfulness", "answer_relevancy", "context_precision", "OK: indexing"):
        assert needle in out, needle
    assert fact["key"] in out and FAKE_KEY not in out
    assert len(mock_openai.requests_of("index_leaf")) > 10
