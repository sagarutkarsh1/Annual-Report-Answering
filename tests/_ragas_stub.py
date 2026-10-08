"""Offline stand-in for the OpenAI REST API, shaped for the RAGAS 0.4.3 prompts used by reportlens.evaluation.

Runs on 127.0.0.1 in a daemon thread and speaks only what the evaluator needs:
  POST /v1/chat/completions  JSON-mode answers that satisfy each metric's output schema
  POST /v1/embeddings        deterministic hashed bag-of-words vectors

Tests steer it with plain attributes: `fail` (status per metric/"embeddings"), `delay` (seconds per metric) and
read `requests` to assert on what was sent on the wire.  The "judge" is a lexical-overlap heuristic, so scores are
deterministic and meaningful only in direction (supported text -> 1, unrelated text -> 0).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

EMB_DIM = 128

# The marker text of each RAGAS prompt (ragas/metrics/collections/*/util.py) -> logical request kind.
_PROMPT_KINDS = (
    ("Break down each sentence", "faithfulness_statements"),
    ("judge the faithfulness of a series of statements", "faithfulness_verdicts"),
    ("Generate a question for the given answer", "relevancy"),
    ("verify if the context was useful", "context_precision"),
)
_KIND_TO_METRIC = {
    "faithfulness_statements": "faithfulness",
    "faithfulness_verdicts": "faithfulness",
    "relevancy": "answer_relevancy",
    "context_precision": "context_precision",
}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def embed(text: str) -> list[float]:
    vec = [0.0] * EMB_DIM
    for w in _words(text):
        vec[int(hashlib.md5(w.encode()).hexdigest(), 16) % EMB_DIM] += 1.0
    return vec


def _overlap(a: str, b: str) -> float:
    wa, wb = set(_words(a)), set(_words(b))
    return len(wa & wb) / len(wa) if wa else 0.0


def _input_json(prompt: str) -> dict:
    """ragas prompts end with 'input: {json}\\nOutput: '."""
    m = re.search(r"input: (\{.*\})\s*Output:\s*$", prompt, re.S)
    try:
        return json.loads(m.group(1)) if m else {}
    except ValueError:
        return {}


def kind_of(prompt: str) -> str:
    return next((kind for marker, kind in _PROMPT_KINDS if marker in prompt), "unknown")


def judge_answer(kind: str, prompt: str) -> dict[str, Any]:
    data = _input_json(prompt)
    if kind == "faithfulness_statements":
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", data.get("answer", "")) if s.strip()]
        return {"statements": sentences}
    if kind == "faithfulness_verdicts":
        context = data.get("context", "")
        rows = []
        for st in data.get("statements", []):
            verdict = 1 if _overlap(st, context) >= 0.7 else 0
            rows.append({"statement": st, "reason": "stub lexical overlap", "verdict": verdict})
        return {"statements": rows}
    if kind == "relevancy":
        response = data.get("response", "")
        noncommittal = 1 if re.search(r"not (?:stated|available)|cannot find|don't know", response, re.I) else 0
        return {"question": "What is stated about: " + " ".join(response.split()[:10]) + "?", "noncommittal": noncommittal}
    if kind == "context_precision":
        useful = _overlap(data.get("answer", ""), data.get("context", "")) >= 0.25
        return {"reason": "stub lexical overlap", "verdict": 1 if useful else 0}
    return {"unknown_prompt": True}


class _QuietServer(ThreadingHTTPServer):
    """Clients that time out or are cancelled hang up mid-response; that is expected here, not worth a traceback."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        if not issubclass(sys.exc_info()[0] or Exception, ConnectionError):
            super().handle_error(request, client_address)


@dataclass
class RagasStub:
    """Start with `RagasStub().start()`; `base_url` ends in /v1 (use as OPENAI_BASE_URL)."""

    fail: dict[str, int] = field(default_factory=dict)        # metric name or "embeddings" -> HTTP status to return
    delay: dict[str, float] = field(default_factory=dict)     # metric name -> seconds to sleep before answering
    latency: float = 0.0                                      # extra seconds for every chat call (simulates a real judge)
    max_in_flight: int = 0                                    # highest number of chat calls served simultaneously
    requests: list[dict[str, Any]] = field(default_factory=list)   # {"path","kind","body"} for every request
    base_url: str = ""
    _server: Optional[_QuietServer] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _in_flight: int = 0

    def start(self) -> "RagasStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # keep pytest output clean
                pass

            def _send(self, status: int, obj: dict) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:  # noqa: N802 (http.server API)
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path.endswith("/chat/completions"):
                    stub._chat(self, body)
                elif self.path.endswith("/embeddings"):
                    stub._embeddings(self, body)
                else:
                    self._send(404, {"error": {"message": "unknown path"}})

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True, name="ragas-stub").start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ------------------------------------------------------------------ inspection helpers
    def chat_requests(self, metric: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [r for r in self.requests if r["path"].endswith("/chat/completions")]
        return [r for r in rows if metric is None or _KIND_TO_METRIC.get(r["kind"]) == metric]

    def embedding_requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self.requests if r["path"].endswith("/embeddings")]

    def clear(self) -> None:
        with self._lock:
            self.requests.clear()
            self.max_in_flight = 0

    # ------------------------------------------------------------------ endpoints
    def _record(self, path: str, kind: str, body: dict) -> None:
        with self._lock:
            self.requests.append({"path": path, "kind": kind, "body": body})

    def _error(self, handler: BaseHTTPRequestHandler, status: int) -> None:
        handler._send(status, {"error": {"message": "stub failure", "type": "server_error", "code": None}})  # type: ignore[attr-defined]

    def _chat(self, handler: BaseHTTPRequestHandler, body: dict) -> None:
        prompt = "\n".join(
            m["content"] if isinstance(m["content"], str) else json.dumps(m["content"]) for m in body.get("messages", [])
        )
        kind = kind_of(prompt)
        self._record(handler.path, kind, body)
        metric = _KIND_TO_METRIC.get(kind)
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            time.sleep(self.latency + self.delay.get(metric, 0.0))
        finally:
            with self._lock:
                self._in_flight -= 1
        if metric in self.fail:
            return self._error(handler, self.fail[metric])
        content = json.dumps(judge_answer(kind, prompt))
        handler._send(200, {  # type: ignore[attr-defined]
            "id": "chatcmpl-stub", "object": "chat.completion", "created": 0, "model": body.get("model", "stub"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(content) // 4,
                      "total_tokens": (len(prompt) + len(content)) // 4},
        })

    def _embeddings(self, handler: BaseHTTPRequestHandler, body: dict) -> None:
        self._record(handler.path, "embeddings", body)
        if "embeddings" in self.fail:
            return self._error(handler, self.fail["embeddings"])
        raw = body.get("input", "")
        texts = raw if isinstance(raw, list) else [raw]
        as_base64 = body.get("encoding_format") == "base64"
        data = []
        for i, text in enumerate(texts):
            vec = embed(text if isinstance(text, str) else str(text))
            emb: Any = base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode() if as_base64 else vec
            data.append({"object": "embedding", "index": i, "embedding": emb})
        handler._send(200, {  # type: ignore[attr-defined]
            "object": "list", "data": data, "model": body.get("model", "stub"),
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })
