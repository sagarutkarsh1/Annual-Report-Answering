"""A local fake of the OpenAI REST API for ReportLens demos and offline tests.  Dev tooling only - never imported by the product
except in demo mode (`REPORTLENS_DEMO_MOCK=1`).

    python -m devtools.mock_openai --port 8765 [--delay-ms 40]     # then:  OPENAI_BASE_URL=http://127.0.0.1:8765/v1

    from devtools.mock_openai import start_mock_server
    srv = start_mock_server()                  # port 0 = pick a free port; srv.base_url -> "http://127.0.0.1:PORT/v1"
    ...; srv.requests; srv.fail_next(429); srv.stop()

Endpoints (stdlib `http.server`, no dependencies; streaming SSE wherever OpenAI streams):
    POST /v1/chat/completions    JSON and SSE (tool calls and text, `stream_options.include_usage` honoured)
    POST /v1/responses           JSON and SSE with the full event sequence the OpenAI Agents SDK needs:
                                 response.created, response.in_progress, response.output_item.added,
                                 response.function_call_arguments.delta/done, response.content_part.added,
                                 response.output_text.delta/done, response.content_part.done, response.output_item.done,
                                 response.completed (+ reasoning-summary events when the request asks for a summary)
    POST /v1/embeddings          deterministic hashed bag-of-words vectors, dim 64 (or `dimensions`), L2-normalised;
                                 `encoding_format` float or base64
    GET  /v1/models/{id}, /v1/models, /health

How a request is classified (first match wins; the same on chat.completions and responses):
    1. AGENT      `tools` contains get_document_structure | get_page_content | get_document | browse_documents
                  -> the PageIndex agent persona (devtools/_mock_agent.py): outline -> best node's pages (<= 4, <= 3 reads)
                  -> a markdown answer made of REAL sentences / table rows from the pages it was given, each followed by
                  <cite doc="NAME" page="N" quote="<verbatim fragment>"/>; or "The report does not appear to state this."
    2. PROMPT MARKERS in the system/user text (devtools/_mock_judge.py::PROMPT_MARKERS):
          "You are given a text chunk from a document"               PageIndex leaf summary      -> {"summary": ...}
          "Subsection Titles and Summaries:"                          PageIndex parent summary    -> {"summary": ...}
          "splitting an over-long section of a PDF"                   PageIndex tree expansion    -> {"subsections": []}
          "generate a one-sentence description for the document"      PageIndex doc description   -> one sentence
          "Break down each sentence into one or more fully ..."       RAGAS faithfulness step 1   -> {"statements": [...]}
          "judge the faithfulness of a series of statements"          RAGAS faithfulness step 2   -> {"statements": [{verdict}]}
          "Generate a question for the given answer and identify ..." RAGAS answer relevancy      -> {"question","noncommittal"}
          "verify if the context was useful in arriving at ..."       RAGAS context precision     -> {"reason","verdict"}
          "Rewrite the user's last question as a standalone question" follow-up rewrite           -> the text after "Last question: "
    3. JSON expected (response_format / forced tool / instructor-style system prompt carrying a json_schema)
                  -> a minimal valid instance of that schema.
    4. anything else -> a polite generic 200 reply.
The persona and judges decide purely from the text of the request, so the mock exercises the real data flow (tool results ->
page text -> answer -> citations -> RAGAS contexts) instead of replaying canned strings.

Test hooks on MockServer: `requests` (snapshot of every request: path, json, kind, stream, headers), `requests_of(kind)`,
`clear_requests()`, `fail_next(status=None, kind="rate_limit"|"auth"|"server"|"model"|"bad_request", count=1, path=None)`, `delay_ms`
(sleep per streamed chunk, once per non-streamed reply; agent turns also wait delay_ms*10 before the first byte, like a
model thinking), `aborted_streams` (streams the client hung up on), `stop()`.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit

from devtools import _mock_agent as agent
from devtools import _mock_judge as judge
from devtools._mock_text import EMBED_DIM, embed

log = logging.getLogger("reportlens.mock_openai")

GENERIC_REPLY = "This is the ReportLens mock OpenAI server; it has no scripted reply for this prompt."
_DEFAULT_MODEL = "gpt-5.6-sol"


# --------------------------------------------------------------------------------------------- failures
_FAILURES = {
    "rate_limit": (429, "rate_limit_exceeded", "requests", "Rate limit reached for requests (mock). Please try again later."),
    "auth": (401, "invalid_api_key", "invalid_request_error", "Incorrect API key provided: sk-test********. (mock)"),
    "server": (500, "server_error", "server_error", "The server had an error while processing your request. (mock)"),
    "model": (404, "model_not_found", "invalid_request_error", "The model does not exist or you do not have access to it. (mock)"),
    "bad_request": (400, "invalid_request_error", "invalid_request_error", "Unsupported parameter in the request. (mock)"),
}
_KIND_BY_STATUS = {429: "rate_limit", 401: "auth", 403: "auth", 404: "model", 400: "bad_request"}


@dataclass
class _Failure:
    status: int
    kind: str
    remaining: int
    path: Optional[str]


# --------------------------------------------------------------------------------------------- wire helpers
def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _chunks(text: str) -> list[str]:
    """Token-like pieces (<= 6 chars) so that <cite .../> tags and [[c1]] markers arrive split across deltas."""
    return re.findall(r"\s*\S{1,6}|\s+", text) or [""]


def _slices(text: str, n: int = 14) -> list[str]:
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


def _msg_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _tool_names(body: dict) -> set[str]:
    names = set()
    for t in body.get("tools") or []:
        if isinstance(t, dict):
            names.add((t.get("function") or {}).get("name") or t.get("name") or "")
    return names


def _forced_tool(body: dict) -> Optional[str]:
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        return (tc.get("function") or {}).get("name") or tc.get("name")
    return None


def _prompt_parts(body: dict, path: str) -> tuple[str, str]:
    """(system text, user text) of a chat.completions or responses request."""
    system, user = [], []
    if path.endswith("/responses"):
        if isinstance(body.get("instructions"), str):
            system.append(body["instructions"])
        items = body.get("input", [])
        for it in [{"role": "user", "content": items}] if isinstance(items, str) else items:
            if isinstance(it, dict) and it.get("role") in ("system", "developer"):
                system.append(_msg_text(it.get("content")))
            elif isinstance(it, dict) and it.get("role") == "user":
                user.append(_msg_text(it.get("content")))
    else:
        for m in body.get("messages", []):
            if isinstance(m, dict):
                (system if m.get("role") in ("system", "developer") else user).append(_msg_text(m.get("content")))
    return "\n".join(system), "\n".join(user)


def _json_schema_of(body: dict, system: str) -> Optional[dict]:
    rf = body.get("response_format")
    if isinstance(rf, dict) and isinstance(rf.get("json_schema"), dict) and isinstance(rf["json_schema"].get("schema"), dict):
        return rf["json_schema"]["schema"]
    fmt = (body.get("text") or {}).get("format") if isinstance(body.get("text"), dict) else None
    if isinstance(fmt, dict) and isinstance(fmt.get("schema"), dict):
        return fmt["schema"]
    name = _forced_tool(body)
    for t in body.get("tools") or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if name and isinstance(fn, dict) and fn.get("name") == name and isinstance(fn.get("parameters"), dict):
            return fn["parameters"]
    return judge.schema_from_system_prompt(system)


def _wants_json(body: dict, system: str) -> bool:
    rf = body.get("response_format")
    fmt = (body.get("text") or {}).get("format") if isinstance(body.get("text"), dict) else None
    return (isinstance(rf, dict) and rf.get("type", "text") != "text") or \
           (isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema")) or \
           bool(_forced_tool(body)) or "json_schema" in system


class _Plan:
    """What the mock will say for one request, independent of the wire format."""

    def __init__(self, kind: str, text: str = "", tool_calls: Optional[list[agent.ToolCall]] = None, reasoning: str = "",
                 forced_tool: Optional[str] = None, in_tokens: int = 0, cached: int = 0):
        self.kind, self.text, self.tool_calls, self.reasoning = kind, text, tool_calls or [], reasoning
        self.forced_tool = forced_tool
        self.in_tokens, self.cached = in_tokens, cached
        self.out_tokens = _tokens(text) + sum(_tokens(json.dumps(c.args)) + 6 for c in self.tool_calls) + 4
        self.reasoning_tokens = (24 + _tokens(reasoning)) if reasoning else 0


def plan_request(path: str, body: dict) -> _Plan:
    """Classify the request and decide the reply. Pure function of (path, body)."""
    system, user = _prompt_parts(body, path)
    full = f"{system}\n{user}"
    in_tokens = _tokens(full) + (650 if body.get("tools") else 0)
    names = _tool_names(body)
    if names & agent.PAGEINDEX_TOOLS:
        convo = agent.convo_from_responses(body) if path.endswith("/responses") else agent.convo_from_chat(body)
        reply = agent.decide(convo, frozenset(names))
        in_tokens = _tokens(convo.instructions) + _tokens(json.dumps(body.get("input", body.get("messages", [])))) + 650
        cached = int(in_tokens * 0.7) if convo.run else 0
        return _Plan("agent", reply.text, reply.tool_calls, reply.reasoning, in_tokens=in_tokens, cached=cached)

    kind = judge.classify_prompt(full)
    if kind == "rewrite_question":
        return _Plan(kind, judge.rewrite_reply(user), in_tokens=in_tokens)
    if kind and kind.startswith("index"):
        return _Plan(kind, judge.index_reply(kind, full), in_tokens=in_tokens)
    if kind and kind.startswith("ragas"):
        return _Plan(kind, judge.ragas_reply(kind, user), forced_tool=_forced_tool(body), in_tokens=in_tokens)
    if _wants_json(body, system):
        schema = _json_schema_of(body, system)
        text = json.dumps(judge.instance_from_schema(schema) if schema else {})
        return _Plan("json_schema", text, forced_tool=_forced_tool(body), in_tokens=in_tokens)
    return _Plan("generic", GENERIC_REPLY, in_tokens=in_tokens)


# ---- chat.completions payloads
def _usage_chat(p: _Plan) -> dict:
    return {"prompt_tokens": p.in_tokens, "completion_tokens": p.out_tokens, "total_tokens": p.in_tokens + p.out_tokens,
            "prompt_tokens_details": {"cached_tokens": p.cached, "audio_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": p.reasoning_tokens, "audio_tokens": 0,
                                          "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0}}


def _chat_tool_calls(p: _Plan) -> list[dict]:
    calls = [(_id("call"), c.name, json.dumps(c.args)) for c in p.tool_calls]
    if p.forced_tool and not calls:     # instructor-style "tool mode": the JSON goes into the forced function's arguments
        calls = [(_id("call"), p.forced_tool, p.text)]
    return [{"id": cid, "type": "function", "function": {"name": n, "arguments": a}} for cid, n, a in calls]


def chat_completion(model: str, p: _Plan, n: int = 1) -> dict:
    tcs = _chat_tool_calls(p)
    msg: dict[str, Any] = {"role": "assistant", "content": None if tcs else p.text, "refusal": None}
    if tcs:
        msg["tool_calls"] = tcs
    return {"id": _id("chatcmpl"), "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": i, "message": dict(msg), "finish_reason": "tool_calls" if tcs else "stop", "logprobs": None}
                        for i in range(max(1, n))],
            "usage": _usage_chat(p), "system_fingerprint": "fp_mock"}


def chat_stream_events(model: str, p: _Plan, include_usage: bool) -> Iterator[dict]:
    base = {"id": _id("chatcmpl"), "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
            "system_fingerprint": "fp_mock"}

    def chunk(delta: dict, finish: Optional[str] = None) -> dict:
        return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}]}

    tcs = _chat_tool_calls(p)
    yield chunk({"role": "assistant", "content": None if tcs else "", "refusal": None})
    for i, tc in enumerate(tcs):
        yield chunk({"tool_calls": [{"index": i, "id": tc["id"], "type": "function",
                                     "function": {"name": tc["function"]["name"], "arguments": ""}}]})
        for piece in _slices(tc["function"]["arguments"]):
            yield chunk({"tool_calls": [{"index": i, "function": {"arguments": piece}}]})
    if not tcs:
        for piece in _chunks(p.text):
            yield chunk({"content": piece})
    yield chunk({}, "tool_calls" if tcs else "stop")
    if include_usage:
        yield {**base, "choices": [], "usage": _usage_chat(p)}


# ---- responses payloads
def _usage_resp(p: _Plan) -> dict:
    return {"input_tokens": p.in_tokens, "input_tokens_details": {"cached_tokens": p.cached, "cache_write_tokens": 0},
            "output_tokens": p.out_tokens + p.reasoning_tokens, "output_tokens_details": {"reasoning_tokens": p.reasoning_tokens},
            "total_tokens": p.in_tokens + p.out_tokens + p.reasoning_tokens}


class _ResponseBuilder:
    """Builds the output items of a Responses reply once, so JSON and SSE variants agree."""

    def __init__(self, model: str, body: dict, p: _Plan):
        self.resp_id, self.model, self.p = _id("resp"), model, p
        self.created = int(time.time())
        self.body = body
        wants_summary = bool((body.get("reasoning") or {}).get("summary")) if isinstance(body.get("reasoning"), dict) else False
        self.items: list[dict] = []
        if p.reasoning and wants_summary:
            self.items.append({"type": "reasoning", "id": _id("rs"),
                               "summary": [{"type": "summary_text", "text": p.reasoning}]})
        calls = [(c.name, json.dumps(c.args)) for c in p.tool_calls]
        if p.forced_tool and not calls:
            calls = [(p.forced_tool, p.text)]
        for name, args in calls:
            self.items.append({"type": "function_call", "id": _id("fc"), "call_id": _id("call"), "name": name,
                               "arguments": args, "status": "completed"})
        if not calls:
            self.items.append({"type": "message", "id": _id("msg"), "role": "assistant", "status": "completed",
                               "content": [{"type": "output_text", "text": p.text, "annotations": [], "logprobs": []}]})

    def response(self, status: str = "completed", output: Optional[list[dict]] = None, usage: bool = True) -> dict:
        reasoning = self.body.get("reasoning") if isinstance(self.body.get("reasoning"), dict) else {}
        return {"id": self.resp_id, "object": "response", "created_at": self.created, "status": status, "model": self.model,
                "output": self.items if output is None else output, "parallel_tool_calls": True, "tool_choice": "auto",
                "tools": [], "error": None, "incomplete_details": None, "instructions": None, "metadata": {},
                "temperature": 1.0, "top_p": 1.0, "reasoning": {"effort": reasoning.get("effort"), "summary": None},
                "usage": _usage_resp(self.p) if usage else None}


def response_stream_events(rb: _ResponseBuilder) -> Iterator[dict]:
    seq = 0

    def ev(typ: str, **kw: Any) -> dict:
        nonlocal seq
        seq += 1
        return {"type": typ, "sequence_number": seq - 1, **kw}

    yield ev("response.created", response=rb.response("in_progress", [], usage=False))
    yield ev("response.in_progress", response=rb.response("in_progress", [], usage=False))
    for oi, item in enumerate(rb.items):
        t = item["type"]
        if t == "reasoning":
            yield ev("response.output_item.added", output_index=oi, item={**item, "summary": []})
            part = {"type": "summary_text", "text": ""}
            yield ev("response.reasoning_summary_part.added", item_id=item["id"], output_index=oi, summary_index=0, part=part)
            text = item["summary"][0]["text"]
            for piece in _chunks(text):
                yield ev("response.reasoning_summary_text.delta", item_id=item["id"], output_index=oi, summary_index=0, delta=piece)
            yield ev("response.reasoning_summary_text.done", item_id=item["id"], output_index=oi, summary_index=0, text=text)
            yield ev("response.reasoning_summary_part.done", item_id=item["id"], output_index=oi, summary_index=0,
                     part={"type": "summary_text", "text": text})
        elif t == "function_call":
            yield ev("response.output_item.added", output_index=oi, item={**item, "arguments": "", "status": "in_progress"})
            for piece in _slices(item["arguments"]):
                yield ev("response.function_call_arguments.delta", item_id=item["id"], output_index=oi, delta=piece)
            yield ev("response.function_call_arguments.done", item_id=item["id"], output_index=oi,
                     name=item["name"], arguments=item["arguments"])
        else:
            text = item["content"][0]["text"]
            yield ev("response.output_item.added", output_index=oi,
                     item={**item, "content": [], "status": "in_progress"})
            yield ev("response.content_part.added", item_id=item["id"], output_index=oi, content_index=0,
                     part={"type": "output_text", "text": "", "annotations": [], "logprobs": []})
            for piece in _chunks(text):
                yield ev("response.output_text.delta", item_id=item["id"], output_index=oi, content_index=0, delta=piece, logprobs=[])
            yield ev("response.output_text.done", item_id=item["id"], output_index=oi, content_index=0, text=text, logprobs=[])
            yield ev("response.content_part.done", item_id=item["id"], output_index=oi, content_index=0, part=item["content"][0])
        yield ev("response.output_item.done", output_index=oi, item=item)
    yield ev("response.completed", response=rb.response("completed"))


# --------------------------------------------------------------------------------------------- the server
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ReportLensMockOpenAI/1.0"

    @property
    def mock(self) -> "MockServer":
        return self.server.mock  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - silence the default stderr access log
        log.debug("%s " + fmt, self.address_string(), *args)

    # ---- plumbing
    def _send_json(self, status: int, obj: Any, extra_headers: Optional[dict[str, str]] = None) -> None:
        raw = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            self.close_connection = True

    def _error(self, status: int, code: Optional[str], etype: str, message: str, retryable: bool = False) -> None:
        self._send_json(status, {"error": {"message": message, "type": etype, "param": None, "code": code}},
                        {"x-should-retry": "true" if retryable else "false"})

    def _read_body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            data = b""
            while True:
                size = int(self.rfile.readline().strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _pause(self, factor: int = 1) -> None:
        delay = self.mock.delay_ms
        if delay > 0:
            time.sleep(delay * factor / 1000.0)

    def _sse(self, events: Iterator[dict], *, named: bool) -> None:
        """Write server-sent events; `named` adds `event: <type>` lines (Responses API). Stops quietly on hang-up."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            for ev in events:
                head = f"event: {ev['type']}\n" if named else ""
                self.wfile.write(f"{head}data: {json.dumps(ev)}\n\n".encode("utf-8"))
                self.wfile.flush()
                self._pause()
            if not named:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except OSError:  # BrokenPipe / ConnectionReset / ConnectionAborted: the client cancelled
            self.mock._note_abort()
            self.close_connection = True

    # ---- verbs
    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/")
        if path in ("", "/health"):
            return self._send_json(200, {"ok": True, "service": "reportlens-mock-openai"})
        if path == "/v1/models":
            return self._send_json(200, {"object": "list", "data": [self._model_obj(m) for m in ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-4.1-mini")]})
        m = re.fullmatch(r"/v1/models/(.+)", path)
        if m:
            return self._send_json(200, self._model_obj(m.group(1)))
        self._error(404, None, "invalid_request_error", f"Unknown request URL: GET {path}")

    @staticmethod
    def _model_obj(model_id: str) -> dict:
        return {"id": model_id, "object": "model", "created": 1_700_000_000, "owned_by": "mock-openai"}

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/")
        try:
            body = json.loads(self._read_body() or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except ValueError as exc:
            return self._error(400, None, "invalid_request_error", f"Could not parse the JSON body: {exc}")
        route = ("chat" if path.endswith("/chat/completions") else "responses" if path.endswith("/responses")
                 else "embeddings" if path.endswith("/embeddings") else None)
        if route is None:
            self.mock._record(path, body, "unknown", False, self.headers)
            return self._error(404, None, "invalid_request_error", f"Unknown request URL: POST {path}")
        if route == "embeddings":
            kind, stream = "embeddings", False
        else:
            plan = plan_request(path, body)
            kind, stream = plan.kind, bool(body.get("stream"))
        self.mock._record(path, body, kind, stream, self.headers)

        failure = self.mock._take_failure(path)
        if failure is not None:
            code, etype, message = _FAILURES[failure.kind][1:]
            return self._error(failure.status, code, etype, message)

        if route == "embeddings":
            return self._embeddings(body)
        if plan.kind == "agent":
            self._pause(10)  # a model "thinks" before its first token
        model = str(body.get("model") or _DEFAULT_MODEL)
        if route == "chat":
            if stream:
                include = bool((body.get("stream_options") or {}).get("include_usage"))
                return self._sse(chat_stream_events(model, plan, include), named=False)
            self._pause()
            return self._send_json(200, chat_completion(model, plan, int(body.get("n") or 1)))
        rb = _ResponseBuilder(model, body, plan)
        if stream:
            return self._sse(response_stream_events(rb), named=True)
        self._pause()
        return self._send_json(200, rb.response())

    def _embeddings(self, body: dict) -> None:
        raw = body.get("input", "")
        inputs = raw if isinstance(raw, list) and not (raw and all(isinstance(x, int) for x in raw)) else [raw]
        dim = body.get("dimensions") if isinstance(body.get("dimensions"), int) and body["dimensions"] > 0 else EMBED_DIM
        data, total = [], 0
        for i, item in enumerate(inputs):
            text = item if isinstance(item, str) else " ".join(str(x) for x in item) if isinstance(item, list) else str(item)
            vec = embed(text, dim)
            total += _tokens(text)
            emb: Any = base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii") \
                if body.get("encoding_format") == "base64" else vec
            data.append({"object": "embedding", "index": i, "embedding": emb})
        self._pause()
        self._send_json(200, {"object": "list", "data": data, "model": body.get("model", "text-embedding-3-small"),
                              "usage": {"prompt_tokens": total, "total_tokens": total}})


class _Server(ThreadingHTTPServer):
    daemon_threads = True            # a hung-up SSE client must never block shutdown
    request_queue_size = 128         # RAGAS and the agent open many connections at once; the default backlog of 5 refuses some on Windows


class MockServer:
    """A running fake OpenAI server. `base_url` is ready to be used as OPENAI_BASE_URL."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, delay_ms: int = 0):
        self.delay_ms = delay_ms
        self._lock = threading.Lock()
        self._requests: list[dict] = []
        self._failures: list[_Failure] = []
        self._aborted = 0
        self._stopped = False
        self._httpd = _Server((host, port), _Handler)
        self._httpd.mock = self  # type: ignore[attr-defined]
        self.host, self.port = self._httpd.server_address[0], self._httpd.server_address[1]
        self.base_url = f"http://{self.host}:{self.port}/v1"
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                        name=f"mock-openai-{self.port}", daemon=True)

    # ---- lifecycle
    def start(self) -> "MockServer":
        self._thread.start()
        log.info("mock OpenAI server listening on %s", self.base_url)
        return self

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3)

    def __enter__(self) -> "MockServer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---- inspection
    @property
    def requests(self) -> list[dict]:
        """Snapshot of every request so far: {"path","json","kind","stream","headers","ts"}."""
        with self._lock:
            return list(self._requests)

    def requests_of(self, kind: str) -> list[dict]:
        return [r for r in self.requests if r["kind"] == kind]

    def clear_requests(self) -> None:
        with self._lock:
            self._requests.clear()

    @property
    def aborted_streams(self) -> int:
        with self._lock:
            return self._aborted

    # ---- scripting
    def fail_next(self, status: Optional[int] = None, kind: Optional[str] = None, *, count: int = 1,
                  path: Optional[str] = None) -> None:
        """Make the next `count` requests (whose path contains `path`, if given) fail like OpenAI would.
        kind: rate_limit (429, the default) | auth (401) | server (500) | model (404) | bad_request (400); `status`
        overrides the HTTP code (and picks the kind when none is given).
        Failures are not retried by the openai SDK (x-should-retry: false), so error paths fail fast."""
        kind = kind or (_KIND_BY_STATUS.get(status) or ("server" if status >= 500 else "rate_limit") if status else "rate_limit")
        if kind not in _FAILURES:
            raise ValueError(f"unknown failure kind {kind!r}; use one of {sorted(_FAILURES)}")
        with self._lock:
            self._failures.append(_Failure(status or _FAILURES[kind][0], kind, count, path))

    # ---- called by the handler
    def _record(self, path: str, body: dict, kind: str, stream: bool, headers: Any) -> None:
        rec = {"path": path, "json": body, "kind": kind, "stream": stream,
               "headers": {k.lower(): v for k, v in headers.items()}, "ts": time.time()}
        with self._lock:
            self._requests.append(rec)

    def _take_failure(self, path: str) -> Optional[_Failure]:
        with self._lock:
            for f in self._failures:
                if f.path is None or f.path in path:
                    f.remaining -= 1
                    if f.remaining <= 0:
                        self._failures.remove(f)
                    return f
        return None

    def _note_abort(self) -> None:
        with self._lock:
            self._aborted += 1


def start_mock_server(host: str = "127.0.0.1", port: int = 0, *, delay_ms: int = 0) -> MockServer:
    """Start the mock on a background thread. port=0 picks a free port."""
    return MockServer(host, port, delay_ms=delay_ms).start()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ReportLens mock OpenAI server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--delay-ms", type=int, default=0, help="sleep per streamed chunk (UI demos)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    srv = start_mock_server(args.host, args.port, delay_ms=args.delay_ms)
    log.info("OPENAI_BASE_URL=%s OPENAI_API_KEY=sk-test   (Ctrl+C to stop)", srv.base_url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
