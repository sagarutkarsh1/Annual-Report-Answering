"""devtools/mock_openai.py: wire formats (checked with the real openai SDK), the agent persona, the judge responders, failure
scripting - and the point of the mock: the real PageIndex SDK and RAGAS run against it end to end, offline."""
from __future__ import annotations

import asyncio
import base64
import http.client
import json
import math
import re
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import openai
import pytest

from devtools import _mock_agent as agent
from devtools import _mock_judge as judge
from devtools import _mock_text as text
from devtools.mock_openai import plan_request, start_mock_server

TOOLS = [{"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object", "properties": {}}}}
         for n in sorted(agent.PAGEINDEX_TOOLS)]
RESP_TOOLS = [{"type": "function", "name": t["function"]["name"], "parameters": t["function"]["parameters"]} for t in TOOLS]
CITE_RE = re.compile(r'<cite doc="([^"]*)" page="(\d+)"(?: quote="([^"]*)")?/>')


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("￾", "")).strip()


def sdk(mock) -> openai.OpenAI:
    return openai.OpenAI(base_url=mock.base_url, api_key="sk-test-not-real", max_retries=0)


# ------------------------------------------------------------------------------------------------ a tiny document
DOC = "Acme Group Annual Report.pdf"
OUTLINE = {"success": True, "doc_name": DOC, "structure": [
    {"title": "Strategic report", "node_id": "0001", "start_index": 1, "end_index": 9,
     "summary": "Overview of the year and the financial results.", "nodes": [
         {"title": "Financial review", "node_id": "0002", "start_index": 3, "end_index": 8,
          "summary": "Group revenue, operating profit, capital expenditure, the dividend and net debt for the year."},
         {"title": "Sustainability", "node_id": "0003", "start_index": 9, "end_index": 9,
          "summary": "Carbon emissions and climate targets."}]},
    {"title": "Governance", "node_id": "0004", "start_index": 10, "end_index": 12, "summary": "The board and its committees."},
]}
PAGES = {
    3: "Financial review\nGroup revenue rose by 8% to 5,120 million (2024/25: 4,741 million), helped by new connections.\n"
       "Operating profit was 812 million.",
    4: "The Board proposed a final dividend of 14.5 pence per share.",
    5: "Our infra-\nstructure programme continued across the North. Net debt was 3,340 million at the year end.",
    6: "Employee engagement remained high.",
    7: "Costs\n£m 2025/26 2024/25\nCapital expenditure (1,204) (1,101)\nDisposals 55 12",
    8: "Closing remarks.",
    9: 'We aim to cut emissions by 40% by 2030, our "Net Zero Plan" target, supported by new battery storage sites.',
    10: "Dame Ann Fox chairs the Board.",
}


def run_tool(name: str, args: dict) -> dict:
    if name == "get_document_structure":
        return OUTLINE
    pages = sorted({p for part in args["pages"].split(",") for p in range(int(part.split("-")[0]), int(part.split("-")[-1]) + 1)})
    return {"success": True, "doc_name": DOC, "total_pages": 12, "requested_pages": args["pages"],
            "content": [{"page": p, "text": PAGES.get(p, f"Page {p} has no content.")} for p in pages if p <= 12]}


def target_block(name: str = DOC, pages: int = 12) -> str:
    return (f"The user has specified document: {name}\nDocument metadata: " +
            json.dumps({"id": "pi-1", "name": name, "pageNum": pages}) + "\nUse this document's name to retrieve its content.")


def drive(client: openai.OpenAI, question: str, *, lane: str, stream: bool, history: list[dict] | None = None,
          tools: list[dict] | None = None) -> tuple[str, list[tuple[str, dict]], list[dict]]:
    """Run the agent loop like the PageIndex SDK would, executing tools locally. Returns (answer, calls, raw outputs)."""
    calls: list[tuple[str, dict]] = []
    raw: list[dict] = []
    system = "You are PageIndex, a document-focused assistant."
    if lane == "chat":
        msgs: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": target_block()},
                            *(history or []), {"role": "user", "content": question}]
    else:
        items: list[dict] = [{"role": "user", "content": target_block()}, *(history or []), {"role": "user", "content": question}]
    for _ in range(8):
        if lane == "chat":
            kw = dict(model="gpt-5.6-sol", messages=msgs, tools=tools or TOOLS)
            if stream:
                content, tcs = "", {}
                for ch in client.chat.completions.create(**kw, stream=True, stream_options={"include_usage": True}):
                    if ch.choices:
                        d = ch.choices[0].delta
                        content += d.content or ""
                        for tc in d.tool_calls or []:
                            slot = tcs.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                            slot["id"] = tc.id or slot["id"]
                            slot["name"] = (tc.function.name or "") if tc.function and tc.function.name else slot["name"]
                            slot["arguments"] += (tc.function.arguments or "") if tc.function else ""
                    else:
                        raw.append({"usage": ch.usage.model_dump()})
                calls_now = [(v["id"], v["name"], v["arguments"]) for _, v in sorted(tcs.items())]
            else:
                resp = client.chat.completions.create(**kw)
                raw.append(resp.model_dump())
                m = resp.choices[0].message
                content = m.content or ""
                calls_now = [(tc.id, tc.function.name, tc.function.arguments) for tc in m.tool_calls or []]
            if not calls_now:
                return content, calls, raw
            msgs.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": i, "type": "function", "function": {"name": n, "arguments": a}} for i, n, a in calls_now]})
            for i, n, a in calls_now:
                calls.append((n, json.loads(a)))
                msgs.append({"role": "tool", "tool_call_id": i, "content": json.dumps(run_tool(n, json.loads(a)))})
        else:
            kw = dict(model="gpt-5.6-sol", input=items, tools=RESP_TOOLS)
            if stream:
                out_items, text_parts = [], []
                for ev in client.responses.create(**kw, stream=True):
                    if ev.type == "response.output_item.done":
                        out_items.append(ev.item.model_dump(exclude_none=True))
                    elif ev.type == "response.output_text.delta":
                        text_parts.append(ev.delta)
                    elif ev.type == "response.completed":
                        raw.append(ev.response.model_dump())
                answer = "".join(text_parts)
            else:
                resp = client.responses.create(**kw)
                raw.append(resp.model_dump())
                out_items = [o.model_dump(exclude_none=True) for o in resp.output]
                answer = resp.output_text
            fcs = [o for o in out_items if o["type"] == "function_call"]
            if not fcs:
                return answer, calls, raw
            for o in fcs:
                items.append({k: o[k] for k in ("type", "call_id", "name", "arguments")})
                calls.append((o["name"], json.loads(o["arguments"])))
                out = json.dumps(run_tool(o["name"], json.loads(o["arguments"])))
                items.append({"type": "function_call_output", "call_id": o["call_id"], "output": [{"type": "input_text", "text": out}]})
    raise AssertionError("the persona never produced a final answer")


# ------------------------------------------------------------------------------------------------ wire formats
def test_models_and_health_endpoints(mock_openai):
    c = sdk(mock_openai)
    assert c.models.retrieve("gpt-5.6-sol").id == "gpt-5.6-sol"
    assert {m.id for m in c.models.list().data} >= {"gpt-5.6-sol"}
    conn = http.client.HTTPConnection(mock_openai.host, mock_openai.port, timeout=5)
    conn.request("GET", "/health")
    assert json.loads(conn.getresponse().read())["ok"] is True
    conn.close()


@pytest.mark.parametrize("path,payload", [("/v1/nope", "{}"), ("/v1/chat/completions", "{not json")])
def test_unknown_path_and_bad_body_are_json_errors(mock_openai, path, payload):
    conn = http.client.HTTPConnection(mock_openai.host, mock_openai.port, timeout=5)
    conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    body = json.loads(resp.read())
    assert resp.status in (400, 404) and "message" in body["error"]
    conn.close()


def test_chat_completions_json_and_stream_agree(mock_openai):
    c = sdk(mock_openai)
    msgs = [{"role": "user", "content": "Say something."}]
    plain = c.chat.completions.create(model="gpt-4.1-mini", messages=msgs)
    assert plain.choices[0].finish_reason == "stop" and plain.usage.total_tokens > 0
    chunks = list(c.chat.completions.create(model="gpt-4.1-mini", messages=msgs, stream=True, stream_options={"include_usage": True}))
    streamed = "".join(ch.choices[0].delta.content or "" for ch in chunks if ch.choices)
    assert streamed == plain.choices[0].message.content
    assert chunks[-1].usage is not None and chunks[-1].choices == []
    assert [ch.choices[0].finish_reason for ch in chunks if ch.choices][-1] == "stop"
    no_usage = list(c.chat.completions.create(model="gpt-4.1-mini", messages=msgs, stream=True))
    assert all(ch.usage is None for ch in no_usage)


def test_responses_json_and_stream_event_sequence(mock_openai):
    c = sdk(mock_openai)
    plain = c.responses.create(model="gpt-5.6-sol", input="Say something.")
    assert plain.status == "completed" and plain.usage.input_tokens_details.cache_write_tokens == 0
    events = list(c.responses.create(model="gpt-5.6-sol", input="Say something.", stream=True))
    kinds = [e.type for e in events]
    assert kinds[:4] == ["response.created", "response.in_progress", "response.output_item.added", "response.content_part.added"]
    assert kinds[-4:] == ["response.output_text.done", "response.content_part.done", "response.output_item.done", "response.completed"]
    assert [e.sequence_number for e in events] == list(range(len(events)))
    assert "".join(e.delta for e in events if e.type == "response.output_text.delta") == plain.output_text
    assert events[-1].response.output_text == plain.output_text and events[-1].response.usage.total_tokens > 0


def test_responses_stream_function_call_events(mock_openai):
    c = sdk(mock_openai)
    body = dict(model="gpt-5.6-sol", tools=RESP_TOOLS,
                input=[{"role": "user", "content": target_block()}, {"role": "user", "content": "What was group revenue?"}])
    events = list(c.responses.create(**body, stream=True))
    kinds = [e.type for e in events]
    assert kinds[2:3] == ["response.output_item.added"] and "response.function_call_arguments.delta" in kinds
    done = next(e for e in events if e.type == "response.function_call_arguments.done")
    assert done.name == "get_document_structure" and json.loads(done.arguments) == {"doc_name": DOC}
    assert "".join(e.delta for e in events if e.type == "response.function_call_arguments.delta") == done.arguments
    final = events[-1].response
    assert final.output[0].type == "function_call" and final.output[0].call_id


def test_reasoning_summary_events_only_when_requested(mock_openai):
    c = sdk(mock_openai)
    base = dict(model="gpt-5.6-sol", tools=RESP_TOOLS, stream=True,
                input=[{"role": "user", "content": target_block()}, {"role": "user", "content": "What was group revenue?"}])
    assert not any("reasoning" in e.type for e in c.responses.create(**base, reasoning={"effort": "low"}))
    with_summary = list(c.responses.create(**base, reasoning={"effort": "low", "summary": "auto"}))
    summary = "".join(e.delta for e in with_summary if e.type == "response.reasoning_summary_text.delta")
    assert "outline" in summary
    assert [o.type for o in with_summary[-1].response.output] == ["reasoning", "function_call"]


def test_embeddings_are_deterministic_normalised_and_semantic(mock_openai):
    c = sdk(mock_openai)
    texts = ["Net debt fell to 21,466 million", "Net debt decreased to 21,466 million", "Our board met six times"]
    first = c.embeddings.create(model="text-embedding-3-small", input=texts)       # the SDK asks for base64 by default
    again = c.embeddings.create(model="text-embedding-3-small", input=texts)
    vecs = [d.embedding for d in first.data]
    assert vecs == [d.embedding for d in again.data]
    assert all(len(v) == 64 and abs(math.sqrt(sum(x * x for x in v)) - 1) < 1e-5 for v in vecs)
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
    assert dot(vecs[0], vecs[1]) > 0.7 > 0.3 > dot(vecs[0], vecs[2])
    floats = c.embeddings.create(model="m", input="Net debt fell", encoding_format="float", dimensions=16).data[0].embedding
    assert len(floats) == 16
    assert len(c.embeddings.create(model="m", input="").data[0].embedding) == 64     # empty text still yields a unit vector


def test_embeddings_raw_base64_roundtrip(mock_openai):
    conn = http.client.HTTPConnection(mock_openai.host, mock_openai.port, timeout=5)
    conn.request("POST", "/v1/embeddings", body=json.dumps({"model": "m", "input": ["a b c"], "encoding_format": "base64"}),
                 headers={"Content-Type": "application/json"})
    emb = json.loads(conn.getresponse().read())["data"][0]["embedding"]
    assert list(struct.unpack("<64f", base64.b64decode(emb))) == pytest.approx(text.embed("a b c"), abs=1e-6)
    conn.close()


# ------------------------------------------------------------------------------------------------ request log, failures, timing
def test_requests_are_logged_with_kind_and_a_snapshot_is_returned(mock_openai):
    c = sdk(mock_openai)
    c.chat.completions.create(model="gpt-4.1-mini", messages=[{"role": "user", "content": "hi"}])
    c.embeddings.create(model="m", input="x")
    log = mock_openai.requests
    assert [(r["path"], r["kind"], r["stream"]) for r in log] == [("/v1/chat/completions", "generic", False), ("/v1/embeddings", "embeddings", False)]
    assert log[0]["json"]["messages"][0]["content"] == "hi" and log[0]["headers"]["authorization"].startswith("Bearer ")
    log.clear()
    assert len(mock_openai.requests) == 2
    assert len(mock_openai.requests_of("embeddings")) == 1
    mock_openai.clear_requests()
    assert mock_openai.requests == []


@pytest.mark.parametrize("kwargs,exc,status", [
    ({"status": 429}, openai.RateLimitError, 429),
    ({"kind": "rate_limit"}, openai.RateLimitError, 429),
    ({"kind": "auth"}, openai.AuthenticationError, 401),
    ({"kind": "server"}, openai.InternalServerError, 500),
    ({"status": 503}, openai.InternalServerError, 503),
    ({"kind": "model"}, openai.NotFoundError, 404),
    ({"status": 400}, openai.BadRequestError, 400),
])
def test_fail_next_raises_the_matching_openai_error_once(mock_openai, kwargs, exc, status):
    c = sdk(mock_openai)
    mock_openai.fail_next(**kwargs)
    with pytest.raises(exc) as info:
        c.responses.create(model="gpt-5.6-sol", input="hi")
    assert info.value.status_code == status
    assert c.responses.create(model="gpt-5.6-sol", input="hi").status == "completed"      # only the next request fails
    assert len(mock_openai.requests) == 2                                                  # and the SDK did not retry


def test_fail_next_count_and_path_filter(mock_openai):
    c = sdk(mock_openai)
    mock_openai.fail_next(kind="rate_limit", count=2, path="/responses")
    c.embeddings.create(model="m", input="unaffected")
    for _ in range(2):
        with pytest.raises(openai.RateLimitError):
            c.responses.create(model="m", input="x")
    assert c.responses.create(model="m", input="x").status == "completed"
    with pytest.raises(ValueError):
        mock_openai.fail_next(kind="bogus")


def test_delay_ms_slows_streaming_per_chunk(mock_openai):
    c = sdk(mock_openai)
    t0 = time.perf_counter()
    list(c.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}], stream=True))
    fast = time.perf_counter() - t0
    mock_openai.delay_ms = 15
    t0 = time.perf_counter()
    n = len(list(c.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}], stream=True)))
    slow = time.perf_counter() - t0
    assert n > 10 and slow >= 0.8 * n * 0.015 and slow > fast + 0.1


def test_client_hangup_mid_stream_is_counted(mock_openai):
    mock_openai.delay_ms = 25
    conn = http.client.HTTPConnection(mock_openai.host, mock_openai.port, timeout=5)
    conn.request("POST", "/v1/responses", body=json.dumps({"model": "m", "input": "hi", "stream": True}),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.headers["Content-Type"].startswith("text/event-stream")
    resp.read(200)
    resp.close()          # the response object keeps the socket alive until it is closed too
    conn.close()
    deadline = time.time() + 5
    while mock_openai.aborted_streams == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert mock_openai.aborted_streams >= 1


def test_request_log_is_thread_safe(mock_openai):
    def hammer() -> None:
        c = sdk(mock_openai)
        for i in range(6):
            c.chat.completions.create(model="m", messages=[{"role": "user", "content": f"q{i}"}])

    threads = [threading.Thread(target=hammer) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    log = mock_openai.requests
    assert len(log) == 60 and {r["kind"] for r in log} == {"generic"}
    assert sorted({r["json"]["messages"][0]["content"] for r in log}) == [f"q{i}" for i in range(6)]


def test_stop_is_idempotent_and_releases_the_port():
    server = start_mock_server()
    port = server.port
    server.stop()
    server.stop()
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", port, timeout=1).request("GET", "/health")


# ------------------------------------------------------------------------------------------------ the agent persona
@pytest.mark.parametrize("lane", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_agent_loop_reads_outline_then_pages_then_cites_real_text(mock_openai, lane, stream):
    answer, calls, raw = drive(sdk(mock_openai), "What was the group revenue for the year?", lane=lane, stream=stream)
    assert [n for n, _ in calls] == ["get_document_structure", "get_page_content"]
    assert calls[0][1] == {"doc_name": DOC}
    assert calls[1][1] == {"doc_name": DOC, "pages": "3-6"}                  # best node 3-8, capped at 4 pages
    cites = CITE_RE.findall(answer)
    assert cites and all(d == DOC for d, _, _ in cites)
    assert "5,120 million" in CITE_RE.sub("", answer)
    for _, page, quote in cites:
        assert quote and len(quote.split()) <= 25 and '"' not in quote
        assert norm(quote) in norm(PAGES[int(page)])                            # a verbatim fragment of the cited page
    for sentence in (s.strip(" -") for s in CITE_RE.sub("", answer).splitlines() if s.strip()):
        assert norm(sentence) in norm(" ".join(PAGES.values())).replace("infra- structure", "infrastructure")
    usage = raw[-1]["usage"]
    assert usage and (usage.get("input_tokens") or usage.get("prompt_tokens"))


def test_usage_reports_plausible_token_counts_and_cache_hits(mock_openai):
    _, _, raw = drive(sdk(mock_openai), "What was the group revenue for the year?", lane="responses", stream=False)
    first, last = raw[0]["usage"], raw[-1]["usage"]
    assert first["input_tokens_details"]["cached_tokens"] == 0 and last["input_tokens_details"]["cached_tokens"] > 0
    assert last["input_tokens"] > first["input_tokens"] > 100 and last["output_tokens"] > 0
    assert last["output_tokens_details"]["reasoning_tokens"] > 0


def _convo(question: str, run: list[agent.Event], doc: str | None = DOC, pages: int = 12) -> agent.Convo:
    users = ([target_block(doc, pages)] if doc else []) + [question]
    return agent._finish(agent.Convo(), users, run)


def _outline_run(page_reads: tuple[str, ...] = (), outline: dict = OUTLINE) -> list[agent.Event]:
    run = [agent.Event("call", "c0", "get_document_structure", {"doc_name": DOC}),
           agent.Event("output", "c0", "get_document_structure", {}, json.dumps(outline))]
    for i, spec in enumerate(page_reads, 1):
        args = {"doc_name": DOC, "pages": spec}
        run += [agent.Event("call", f"c{i}", "get_page_content", args),
                agent.Event("output", f"c{i}", "get_page_content", {}, json.dumps(run_tool("get_page_content", args)))]
    return run


def test_first_turn_calls_the_outline_and_gives_a_reason():
    reply = agent.decide(_convo("What was group revenue?", []))
    assert [(c.name, c.args) for c in reply.tool_calls] == [("get_document_structure", {"doc_name": DOC})]
    assert "group revenue" in reply.reasoning


def test_document_name_with_spaces_and_parenthesis_is_copied_verbatim():
    name = "National Grid_Annual Report (2025).pdf"
    assert agent.decide(_convo("net debt?", [], doc=name)).tool_calls[0].args == {"doc_name": name}


def test_without_a_document_block_the_persona_browses_first():
    reply = agent.decide(_convo("What was group revenue?", [], doc=None))
    assert reply.tool_calls[0].name == "browse_documents"
    run = [agent.Event("call", "b", "browse_documents", {}),
           agent.Event("output", "b", "browse_documents", {}, json.dumps({"documents": [{"name": "found.pdf"}]}))]
    assert agent.decide(_convo("What was group revenue?", run, doc=None)).tool_calls[0].args == {"doc_name": "found.pdf"}


def test_paged_outline_is_read_to_the_end_before_choosing_pages():
    page1 = {**OUTLINE, "structure": OUTLINE["structure"][:1], "total_parts": 2, "pagination": {"part": 1, "total_parts": 2, "has_more": True}}
    page2 = {**OUTLINE, "structure": OUTLINE["structure"][1:], "total_parts": 2, "pagination": {"part": 2, "total_parts": 2, "has_more": False}}
    run = [agent.Event("call", "a", "get_document_structure", {}), agent.Event("output", "a", "get_document_structure", {}, json.dumps(page1))]
    assert agent.decide(_convo("net debt?", run)).tool_calls[0].args == {"doc_name": DOC, "part": 2}
    run += [agent.Event("call", "b", "get_document_structure", {"part": 2}), agent.Event("output", "b", "get_document_structure", {}, json.dumps(page2))]
    assert agent.decide(_convo("Who chairs the Board?", run)).tool_calls[0].args["pages"] == "10-12"


def test_tool_outputs_wrapped_or_split_into_parts_are_understood():
    wrapped = json.dumps({"type": "text", "text": json.dumps(OUTLINE)})
    run = [agent.Event("call", "a", "get_document_structure", {}), agent.Event("output", "a", "get_document_structure", {}, wrapped)]
    assert agent.decide(_convo("What was the net debt?", run)).tool_calls[0].name == "get_page_content"


def test_responses_input_with_part_lists_and_reasoning_items_is_parsed():
    body = {"instructions": "be brief", "tools": RESP_TOOLS, "input": [
        {"role": "user", "content": [{"type": "input_text", "text": target_block()}]},
        {"role": "user", "content": [{"type": "input_text", "text": "What was the net debt?"}]},
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {"type": "function_call", "call_id": "c1", "name": "get_document_structure", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": json.dumps(OUTLINE)}]},
    ]}
    convo = agent.convo_from_responses(body)
    assert convo.question == "What was the net debt?" and convo.doc_name == DOC and convo.page_count == 12
    assert agent.decide(convo).tool_calls[0].args["pages"] == "3-6"


def test_follow_up_answers_the_latest_question_not_the_history(mock_openai):
    history = [{"role": "user", "content": "Who chairs the Board?"}, {"role": "assistant", "content": "Dame Ann Fox."}]
    answer, calls, _ = drive(sdk(mock_openai), "What dividend was proposed?", lane="responses", stream=False, history=history)
    assert "14.5 pence" in answer and calls[-1][1]["pages"] == "3-6"


def test_when_the_pages_hold_nothing_relevant_it_says_so_without_a_cite(mock_openai):
    answer, calls, _ = drive(sdk(mock_openai), "What is the chief executive's favourite colour?", lane="chat", stream=False)
    assert answer == agent.NOT_FOUND and "<cite" not in answer
    assert [n for n, _ in calls] == ["get_document_structure"]                # nothing in the outline matched: no pages read


def test_unmatched_pages_lead_to_more_reads_then_a_not_found():
    q = "What did the Governance committees decide?"
    reply = agent.decide(_convo(q, _outline_run(("10-12",))))
    assert reply.text == agent.NOT_FOUND or reply.tool_calls                     # never loops: bounded by MAX_READS
    run = _outline_run(("3-6", "7-8", "9", "10-12"))
    assert agent.decide(_convo("Which committee audited the colour scheme?", run)).text == agent.NOT_FOUND


def test_a_second_window_is_read_when_the_first_has_no_evidence():
    reply = agent.decide(_convo("What was the capital expenditure?", _outline_run(("3-6",))))
    assert reply.tool_calls and reply.tool_calls[0].args["pages"] == "7-8"      # rest of the same node
    final = agent.decide(_convo("What was the capital expenditure?", _outline_run(("3-6", "7-8"))))
    assert final.text.startswith("Capital expenditure:") and 'page="7"' in final.text


def test_table_rows_become_readable_lines_with_verbatim_quotes():
    final = agent.decide(_convo("What was the capital expenditure?", _outline_run(("3-6", "7-8"))))
    assert "Capital expenditure: (1,204) (2025/26), (1,101) (2024/25)" in final.text
    assert 'quote="Capital expenditure (1,204) (1,101)"' in final.text


def test_quotes_avoid_hyphen_breaks_and_double_quotes():
    hyphen = agent.decide(_convo("Tell me about the infrastructure programme", _outline_run(("3-6",))))
    assert "infrastructure programme" in hyphen.text                            # display text repairs the line-end hyphen
    for _, _, quote in CITE_RE.findall(hyphen.text):
        assert "infra- structure" not in quote and "infra-" not in quote
    quoted = agent.decide(_convo("What is the emissions target for 2030?", _outline_run(("9",))))
    (_, page, quote), = CITE_RE.findall(quoted.text)
    assert page == "9" and '"' not in quote and norm(quote) in norm(PAGES[9])


def test_quotes_are_at_most_25_words_even_for_long_sentences():
    long_sentence = " ".join(f"word{i}" for i in range(10)) + " revenue " + " ".join(f"term{i}" for i in range(40)) + "."
    pages = {**PAGES, 3: long_sentence}
    run = _outline_run(("3-6", "7-8"))
    run[3] = agent.Event("output", "c1", "get_page_content", {}, json.dumps(
        {"success": True, "content": [{"page": p, "text": t} for p, t in pages.items() if 3 <= p <= 6]}))
    reply = agent.decide(_convo("What was the revenue word3?", run))
    (_, _, quote), = CITE_RE.findall(reply.text)
    assert 1 <= len(quote.split()) <= 25 and "revenue" in quote


def test_without_an_outline_tool_the_persona_reads_from_the_front():
    only_pages = frozenset({"get_page_content"})
    reply = agent.decide(_convo("What was the net debt?", []), only_pages)
    assert reply.tool_calls[0].name == "get_page_content" and reply.tool_calls[0].args["pages"] == "1-4"


def test_empty_question_gets_a_polite_reply():
    assert "Please ask" in agent.decide(_convo("", [])).text


def test_unknown_tool_results_and_errors_do_not_crash_the_persona():
    run = [agent.Event("call", "a", "get_document_structure", {}), agent.Event("output", "a", "get_document_structure", {}, "not json"),
           agent.Event("call", "b", "get_page_content", {"pages": "x"}), agent.Event("output", "b", "get_page_content", {}, '{"error":"bad"}')]
    assert agent.decide(_convo("What was the net debt?", run)).text == agent.NOT_FOUND


# ------------------------------------------------------------------------------------------------ prompt classification
def _chat(*messages: tuple[str, str], **extra: Any) -> dict:
    return {"model": "gpt-4.1-mini", "messages": [{"role": r, "content": c} for r, c in messages], **extra}


def test_index_prompts_get_extractive_json_from_their_own_text():
    leaf = ("You are given a text chunk from a document.\n    Your task is to ... within 40 words.\n\n    Given Text: "
            "Strategic report\nGroup revenue rose 8% to 5,120 million. The Board proposed a dividend.\n\n    Reply strictly in the following JSON format:\n    {...}")
    plan = plan_request("/v1/chat/completions", _chat(("user", leaf)))
    assert plan.kind == "index_leaf" and "5,120 million" in json.loads(plan.text)["summary"]
    titled = plan_request("/v1/chat/completions", _chat(("user", leaf.replace("within 40 words.", "within 40 words. Also return a short title, at most 12 words, x."))))
    assert set(json.loads(titled.text)) == {"title", "summary"}

    parent = ('You are given a section of a document ...\n    Section Title: Financial review\n\n    Opening Text: Group results.\n\n'
              '    Subsection Titles and Summaries: [{"title": "Cash flow", "summary": "Cash generated was 6,991. More."}, {"title": "Dividend", "summary": "x"}]\n\n'
              '    Reply strictly in the following JSON format:')
    plan = plan_request("/v1/chat/completions", _chat(("user", parent)))
    summary = json.loads(plan.text)["summary"]
    assert plan.kind == "index_parent" and "Cash flow; Dividend" in summary and "Cash generated was 6,991" in summary

    expand = "You are splitting an over-long section of a PDF into its subsections.\nSection title: X"
    assert json.loads(plan_request("/v1/chat/completions", _chat(("user", expand))).text) == {"subsections": []}

    desc = ("Your task is to generate a one-sentence description for the document.\n    Document Structure: [{'title': 'Preface', 'node_id': '0000'}, {'title': 'Strategic report'}, "
            "{'title': 'Governance'}]\n    \n    Directly return the description, do not include any other text.")
    plan = plan_request("/v1/chat/completions", _chat(("user", desc)))
    assert plan.kind == "index_description" and "strategic report and governance" in plan.text and "Preface" not in plan.text


def test_chrome_and_key_figures_survive_into_summaries():
    page = ("Financial statements\nAnnual Report 2025/26\nConsolidated cash flow statement\nFor the year ended 31 March 2026.\n"
            "£m\n2025/26\n6,991\nCash generated from operations Interest paid\n(1,142)\n42\nNorthbridge Energy plc Annual Report 2025/26")
    summary = judge.extractive_summary(page, 150)
    assert "Cash generated from operations Interest paid" in summary and "Annual Report 2025/26" not in summary


def _ragas_prompt(prompt_cls, input_cls, **fields) -> str:
    return prompt_cls().to_string(input_cls(**fields))


def _ragas_request(prompt: str, schema_name: str) -> dict:
    system = f'As a genius expert ... match the following json_schema:\n\n{{"title": "{schema_name}", "type": "object", "properties": {{}}}}\n\nMake sure'
    return _chat(("system", system), ("user", prompt), response_format={"type": "json_object"})


def test_ragas_judge_prompts_get_schema_valid_json():
    from ragas.metrics.collections.answer_relevancy.util import AnswerRelevanceInput, AnswerRelevancePrompt
    from ragas.metrics.collections.context_precision.util import ContextPrecisionInput, ContextPrecisionPrompt
    from ragas.metrics.collections.faithfulness.util import (
        NLIStatementInput, NLIStatementPrompt, StatementGeneratorInput, StatementGeneratorPrompt)

    answer = "- Cash generated from operations: 6,991 (2025/26) [[c1]]\n- Net debt was 3,340 million. <cite doc=\"x\" page=\"3\"/>"
    p = plan_request("/v1/chat/completions", _ragas_request(
        _ragas_prompt(StatementGeneratorPrompt, StatementGeneratorInput, question="q?", answer=answer), "StatementGeneratorOutput"))
    statements = json.loads(p.text)["statements"]
    assert p.kind == "ragas_statements" and statements == ["Cash generated from operations: 6,991 (2025/26)", "Net debt was 3,340 million."]

    ctx = "Cash generated from operations 6,991 6,534. Net debt was 3,340 million at the year end."
    p = plan_request("/v1/chat/completions", _ragas_request(
        _ragas_prompt(NLIStatementPrompt, NLIStatementInput, context=ctx, statements=[*statements, "Net debt was 9,999 million.", "The moon is made of cheese."]),
        "NLIStatementOutput"))
    verdicts = [s["verdict"] for s in json.loads(p.text)["statements"]]
    assert p.kind == "ragas_nli" and verdicts == [1, 1, 0, 0]

    p = plan_request("/v1/chat/completions", _ragas_request(
        _ragas_prompt(AnswerRelevancePrompt, AnswerRelevanceInput, response="Net debt was 3,340 million at the year end."), "AnswerRelevanceOutput"))
    out = json.loads(p.text)
    assert p.kind == "ragas_question" and out["noncommittal"] == 0 and "Net debt" in out["question"]
    p = plan_request("/v1/chat/completions", _ragas_request(
        _ragas_prompt(AnswerRelevancePrompt, AnswerRelevanceInput, response=agent.NOT_FOUND), "AnswerRelevanceOutput"))
    assert json.loads(p.text)["noncommittal"] == 1

    useful = plan_request("/v1/chat/completions", _ragas_request(
        _ragas_prompt(ContextPrecisionPrompt, ContextPrecisionInput, question="q?", context=ctx, answer="Net debt was 3,340 million."), "ContextPrecisionOutput"))
    useless = plan_request("/v1/chat/completions", _ragas_request(
        _ragas_prompt(ContextPrecisionPrompt, ContextPrecisionInput, question="q?", context="The Board visited the control centre.", answer="Net debt was 3,340 million."), "ContextPrecisionOutput"))
    assert useful.kind == "ragas_precision" and json.loads(useful.text)["verdict"] == 1 and json.loads(useless.text)["verdict"] == 0


def test_the_question_rewrite_prompt_is_answered_with_the_last_question():
    from reportlens.qa import _REWRITE_SYSTEM

    prompt = ("Conversation:\nUser: What was net debt?\nAssistant: Net debt was 3,340 million.\n\n"
              "Last question: And the year before?\n\nStandalone question:")
    for path, body in [("/v1/chat/completions", _chat(("system", _REWRITE_SYSTEM), ("user", prompt))),
                       ("/v1/responses", {"model": "m", "instructions": _REWRITE_SYSTEM, "input": prompt})]:
        p = plan_request(path, body)
        assert p.kind == "rewrite_question" and p.text == "And the year before?"
    assert plan_request("/v1/chat/completions", _chat(("system", _REWRITE_SYSTEM), ("user", "no marker"))).text == "no marker"


def test_mock_ragas_questions_keep_the_topic_and_the_years_so_relevancy_is_not_zero():
    from devtools._mock_judge import _question_about
    from devtools._mock_text import embed

    def cosine(a: str, b: str) -> float:
        return sum(x * y for x, y in zip(embed(a), embed(b)))

    question = _question_about("Cash generated from operations 6,991 6,534 (2025/26 2024/25) [[c1]]")
    assert question == "What was Cash generated from operations 2025/26 2024/25?"
    assert cosine("What was cash generated from operations in 2025/26?", question) > 0.5
    assert "3,340" in _question_about("£3,340 million was the net debt at the year end.")      # opens with a figure: keeps the digits
    assert _question_about("- The effective tax rate was 20.0%.") == "What was The effective tax rate?"
    assert _question_about("") == "What was this?"


def test_unknown_prompts_get_a_generic_reply_or_a_schema_instance():
    generic = plan_request("/v1/chat/completions", _chat(("user", "Translate this to French.")))
    assert generic.kind == "generic" and generic.text
    schema = {"title": "Out", "type": "object", "required": ["n", "tags", "ok"],
              "properties": {"n": {"type": "integer"}, "tags": {"type": "array", "items": {"type": "string"}}, "ok": {"type": "boolean"}}}
    for body in (_chat(("user", "x"), response_format={"type": "json_schema", "json_schema": {"name": "Out", "schema": schema}}),
                 _chat(("system", "provide the parsed objects in json that match the following json_schema:\n" + json.dumps(schema)), ("user", "x"))):
        planned = plan_request("/v1/chat/completions", body)
        assert planned.kind == "json_schema" and set(json.loads(planned.text)) == {"n", "tags", "ok"}


def test_forced_tool_calls_carry_the_json_in_their_arguments(mock_openai):
    c = sdk(mock_openai)
    tool = {"type": "function", "function": {"name": "Out", "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}}}
    resp = c.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}], tools=[tool],
                                     tool_choice={"type": "function", "function": {"name": "Out"}})
    call = resp.choices[0].message.tool_calls[0]
    assert call.function.name == "Out" and json.loads(call.function.arguments) == {"n": 1}


# ------------------------------------------------------------------------------------------------ real clients (the point of the mock)
@pytest.fixture(scope="module")
def pageindex_env(tmp_path_factory):
    """Index the sample PDF with the real PageIndex SDK (Flash, local mode) against a mock server; shared by the tests below."""
    from scripts.make_sample_pdf import build_sample_pdf, load_facts

    mp = pytest.MonkeyPatch()
    server = start_mock_server()
    try:
        pdf = build_sample_pdf(tmp_path_factory.mktemp("pi_sample") / "sample_annual_report.pdf")
        mp.setenv("OPENAI_API_KEY", "sk-test-not-real")
        mp.setenv("OPENAI_BASE_URL", server.base_url)
        from pageindex import PageIndexClient
        from pageindex.local_api import LocalAPI

        pages = _pdfium_pages(pdf)
        # same seam the product patches (pdfium text instead of PyPDF2's) so the agent reads clean text
        mp.setattr(LocalAPI, "_extract_page_texts", staticmethod(lambda _path: list(pages)))
        client = PageIndexClient(index={"model": "gpt-5.6-luna", "storage_path": str(tmp_path_factory.mktemp("pi_store"))}, chat="gpt-5.6-sol")
        started = time.perf_counter()
        result = client.submit_document(str(pdf))
        yield SimpleNamespace(server=server, client=client, doc_id=result["doc_id"], name=result["name"], pdf=pdf, pages=pages,
                              facts=load_facts(pdf), index_seconds=time.perf_counter() - started,
                              index_requests=server.requests)
    finally:
        server.stop()
        mp.undo()


def _pdfium_pages(pdf: Path) -> list[str]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    try:
        out = []
        for i in range(len(doc)):
            page = doc[i]
            tp = page.get_textpage()
            out.append(tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n"))
            tp.close()
            page.close()
        return out
    finally:
        doc.close()


def _assert_cited_quotes_are_on_their_pages(answer: str, pages: list[str]) -> list[tuple[int, str]]:
    cites = [(int(p), q) for _, p, q in CITE_RE.findall(answer)]
    assert cites, f"no cite in {answer!r}"
    for page, quote in cites:
        assert quote and norm(quote) in norm(pages[page - 1]), f"quote {quote!r} is not on page {page}"
    return cites


@pytest.mark.slow
def test_pageindex_indexes_the_sample_with_summaries(pageindex_env):
    env = pageindex_env
    kinds = [r["kind"] for r in env.index_requests]
    assert kinds.count("index_leaf") >= 20 and kinds.count("index_parent") >= 5 and kinds.count("index_description") == 1
    assert set(kinds) <= {"index_leaf", "index_parent", "index_description", "index_expand"}
    doc = env.client.get_document(env.doc_id)
    assert doc["pageNum"] == 60 and doc["status"] == "completed" and "annual report" in doc["description"]
    tree = env.client.get_tree(env.doc_id, node_summary=True, include_text=False)["result"]
    nodes: list[dict] = []
    stack = list(tree)
    while stack:
        n = stack.pop()
        nodes.append(n)
        stack.extend(n.get("nodes") or [])
    assert len(nodes) > 30 and all(n.get("summary") for n in nodes)
    assert [n["title"] for n in tree if n["title"] != "Preface"] == ["Strategic report", "Governance", "Financial statements", "Other information"]
    assert all(1 <= n["start_index"] <= n["end_index"] <= 60 for n in nodes)


@pytest.mark.slow
def test_pageindex_answers_every_known_fact_with_verified_citations(pageindex_env):
    env = pageindex_env
    env.server.clear_requests()
    for fact in env.facts:
        result = env.client.chat(fact["question"], doc_id=env.doc_id, citations=True, protocol="responses")
        answer = "".join(c["text"] for o in result["output"] if o["type"] == "message" for c in o["content"])
        assert norm(fact["key"]).lower() in norm(CITE_RE.sub("", answer)).lower(), f"{fact['id']}: {answer!r}"
        cited = {page for page, _ in _assert_cited_quotes_are_on_their_pages(answer, env.pages)}
        # right page = the fact's page (or a page that answers it too), or any cited page that states the key figure itself
        stated_on_cited = norm(fact["key"]).lower() in " ".join(norm(env.pages[p - 1]) for p in cited).lower()
        assert cited & {fact["page"], *fact["related_pages"]} or stated_on_cited, f"{fact['id']}: cited {cited}, wanted {fact['page']}"
        names = [i["name"] for i in result["items"] if i.get("type") == "function_call"]
        assert names[0] == "get_document_structure" and set(names) == {"get_document_structure", "get_page_content"} and len(names) <= 4
        assert result["usage"]["input_tokens"] > 0


@pytest.mark.slow
def test_pageindex_responses_protocol_streaming_matches_non_streaming(pageindex_env):
    env = pageindex_env
    question = "What was cash generated from operations in 2025/26?"
    whole = env.client.chat(question, doc_id=env.doc_id, citations=True, protocol="responses")
    final_text = "".join(c["text"] for o in whole["output"] if o["type"] == "message" for c in o["content"])

    env.server.clear_requests()
    deltas, done_items, completed = [], [], None
    for ev in env.client.chat(question, doc_id=env.doc_id, citations=True, protocol="responses", stream=True):
        kind = ev["type"] if isinstance(ev, dict) else ev.type
        if kind == "response.output_text.delta":
            deltas.append(ev["delta"] if isinstance(ev, dict) else ev.delta)
        elif kind == "response.output_item.done":
            done_items.append(ev["item"] if isinstance(ev, dict) else ev.item)
        elif kind == "response.completed":
            completed = ev
    assert all(r["stream"] for r in env.server.requests) and len(env.server.requests) == 3
    assert "".join(deltas) == final_text
    assert completed is not None and len(done_items) == 3                       # 2 function calls + the final message
    _assert_cited_quotes_are_on_their_pages(final_text, env.pages)
    assert "6,991" in final_text


@pytest.mark.slow
def test_pageindex_default_lane_streams_events_and_returns_text(pageindex_env):
    env = pageindex_env
    question = "What was the total dividend per share for 2025/26?"
    env.server.clear_requests()
    stream = env.client.chat(question, doc_id=env.doc_id, citations=True, stream=True)
    events = list(stream.events)
    types = [e["type"] for e in events]
    assert types.count("tool_call") == 2 and types.count("tool_result") == 2 and types.count("answer") > 5
    assert [e["name"] for e in events if e["type"] == "tool_call"] == ["get_document_structure", "get_page_content"]
    answer = "".join(e["delta"] for e in events if e["type"] == "answer")
    assert "36.15 pence" in answer
    _assert_cited_quotes_are_on_their_pages(answer, env.pages)
    read = next(e for e in events if e["type"] == "tool_result" and e["name"] == "get_page_content")
    payload = json.loads(read["output"]["text"] if isinstance(read["output"], dict) else read["output"])
    assert payload["success"] and 1 <= len(payload["content"]) <= 4            # contexts the evaluator will score
    assert all(r["path"] == "/v1/chat/completions" for r in env.server.requests)

    plain = env.client.chat(question, doc_id=env.doc_id, citations=True)
    assert isinstance(plain, str) and "36.15 pence" in plain
    completions = env.client.chat(question, doc_id=env.doc_id, citations=True, protocol="chat_completions")
    assert completions["choices"][0]["message"]["content"] == plain


@pytest.mark.slow
def test_pageindex_unanswerable_question_gets_no_cite(pageindex_env):
    env = pageindex_env
    result = env.client.chat("What is the chief executive's favourite colour?", doc_id=env.doc_id, citations=True, protocol="responses")
    answer = "".join(c["text"] for o in result["output"] if o["type"] == "message" for c in o["content"])
    assert answer == agent.NOT_FOUND
    names = [i["name"] for i in result["items"] if i.get("type") == "function_call"]
    assert names[0] == "get_document_structure" and names.count("get_page_content") <= 3     # it looked, found nothing, said so


@pytest.mark.slow
def test_pageindex_multi_turn_history_is_answered_for_the_last_question(pageindex_env):
    env = pageindex_env
    messages = [{"role": "user", "content": "Who is the chief executive?"}, {"role": "assistant", "content": "Raj Patel."},
                {"role": "user", "content": "What was the total fees paid to the external auditor?"}]
    answer = env.client.chat(messages, doc_id=env.doc_id, citations=True)
    assert "£4.3 million" in answer and "Raj Patel" not in answer


@pytest.mark.slow
def test_pageindex_surfaces_openai_errors_from_the_mock(pageindex_env):
    from pageindex import PageIndexAPIError

    env = pageindex_env
    env.server.fail_next(kind="auth", path="/responses")
    with pytest.raises(PageIndexAPIError) as info:
        env.client.chat("What was group revenue?", doc_id=env.doc_id, citations=True, protocol="responses")
    assert info.value.status_code == 401
    env.server.fail_next(kind="rate_limit", path="/responses")
    with pytest.raises(PageIndexAPIError) as info:
        env.client.chat("What was group revenue?", doc_id=env.doc_id, citations=True, protocol="responses")
    assert info.value.status_code == 429


@pytest.mark.slow
async def test_ragas_collections_metrics_score_a_mock_answer(pageindex_env):
    """Evaluator-style scoring (ragas.metrics.collections via llm_factory / embedding_factory) against the mock."""
    from ragas.embeddings.base import embedding_factory
    from ragas.llms import llm_factory
    from ragas.metrics.collections import AnswerRelevancy, ContextPrecisionWithoutReference, Faithfulness

    env = pageindex_env
    question = "What was cash generated from operations in 2025/26?"
    stream = env.client.chat(question, doc_id=env.doc_id, citations=True, stream=True)
    events = list(stream.events)
    answer = CITE_RE.sub("", "".join(e["delta"] for e in events if e["type"] == "answer")).strip()
    contexts: list[str] = []
    for e in events:
        if e["type"] == "tool_result" and e["name"] == "get_page_content":
            raw = e["output"]["text"] if isinstance(e["output"], dict) else e["output"]
            contexts += [c["text"] for c in json.loads(raw)["content"]]
    assert answer and contexts

    client = openai.AsyncOpenAI(base_url=env.server.base_url, api_key="sk-test-not-real", max_retries=0)
    try:
        llm = llm_factory("gpt-4.1-mini", client=client, max_tokens=1024)
        emb = embedding_factory("openai", model="text-embedding-3-small", client=client)
        faith, relevancy, precision = await asyncio.gather(
            Faithfulness(llm=llm).ascore(user_input=question, response=answer, retrieved_contexts=contexts),
            AnswerRelevancy(llm=llm, embeddings=emb).ascore(user_input=question, response=answer),
            ContextPrecisionWithoutReference(llm=llm).ascore(user_input=question, response=answer, retrieved_contexts=contexts))
    finally:
        await client.close()
    for score in (faith.value, relevancy.value, precision.value):
        assert isinstance(score, float) and 0.0 <= score <= 1.0
    assert faith.value == pytest.approx(1.0)                                    # the answer is copied from the pages it read
    assert relevancy.value > 0.3
    kinds = {r["kind"] for r in env.server.requests}
    assert {"ragas_statements", "ragas_nli", "ragas_question", "ragas_precision", "embeddings"} <= kinds


async def test_ragas_metrics_work_on_the_function_scoped_fixture(mock_openai):
    """Same wiring on a fresh server: a non-committal answer scores 0 on relevancy, a supported one scores 1 on faithfulness."""
    from ragas.embeddings.base import embedding_factory
    from ragas.llms import llm_factory
    from ragas.metrics.collections import AnswerRelevancy, Faithfulness

    client = openai.AsyncOpenAI(base_url=mock_openai.base_url, api_key="sk-test-not-real", max_retries=0)
    try:
        llm = llm_factory("gpt-4.1-mini", client=client, max_tokens=1024)
        emb = embedding_factory("openai", model="text-embedding-3-small", client=client)
        ctx = ["Net debt was 3,340 million at the year end."]
        good = await Faithfulness(llm=llm).ascore(user_input="What was net debt?", response="Net debt was 3,340 million.", retrieved_contexts=ctx)
        bad = await Faithfulness(llm=llm).ascore(user_input="What was net debt?", response="Net debt was 9,999 million.", retrieved_contexts=ctx)
        evasive = await AnswerRelevancy(llm=llm, embeddings=emb).ascore(user_input="What was net debt?", response=agent.NOT_FOUND)
    finally:
        await client.close()
    assert good.value == 1.0 and bad.value == 0.0 and evasive.value == 0.0


def test_cli_serves_until_terminated():
    import socket
    import subprocess
    import sys

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen([sys.executable, "-m", "devtools.mock_openai", "--port", str(port)], cwd=root,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        client = openai.OpenAI(base_url=f"http://127.0.0.1:{port}/v1", api_key="sk-test", max_retries=0, timeout=2)
        deadline = time.time() + 15
        while True:
            try:
                assert client.models.retrieve("gpt-5.6-sol").id == "gpt-5.6-sol"
                break
            except openai.APIConnectionError:
                assert time.time() < deadline and proc.poll() is None, "mock server did not start"
                time.sleep(0.2)
        assert client.responses.create(model="m", input="hi").status == "completed"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
