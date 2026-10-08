"""
(Research helper - NOT part of the product.)
Tiny OFFLINE stand-in for the OpenAI REST API (127.0.0.1 only, no network, no key).

It answers:
  POST /v1/chat/completions  -> canned JSON that matches each RAGAS prompt
  POST /v1/embeddings        -> deterministic hashed bag-of-words vectors

and records every request payload in REQUESTS so tests can assert on the
exact parameters ragas sent (model, max_tokens vs max_completion_tokens,
temperature, top_p, n, response_format ...).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUESTS: list[dict] = []          # every request body (parsed) + path
LOCK = threading.Lock()
EMB_DIM = 128
FAIL_NEXT: list[int] = []          # e.g. [429] -> next chat call returns HTTP 429 once
TRUNCATE_NEXT: list[bool] = []     # next chat call returns finish_reason=length + cut JSON
LATENCY = [0.0]                    # seconds to sleep per chat call (simulate a real LLM)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def embed(text: str) -> list[float]:
    vec = [0.0] * EMB_DIM
    for w in _words(text):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        vec[h % EMB_DIM] += 1.0
    return vec


def _extract_input_json(prompt: str) -> dict:
    """ragas prompts end with  'input: {json}\\nOutput: '"""
    m = re.search(r"input: (\{.*\})\s*Output:\s*$", prompt, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def _overlap(a: str, b: str) -> float:
    wa, wb = set(_words(a)), set(_words(b))
    if not wa:
        return 0.0
    return len(wa & wb) / len(wa)


def canned_answer(prompt: str) -> dict:
    data = _extract_input_json(prompt)
    # --- Faithfulness step 1: statement generator
    if "Break down each sentence" in prompt:
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", data.get("answer", "")) if s.strip()]
        return {"statements": sents}
    # --- Faithfulness step 2: NLI verdicts
    if "judge the faithfulness of a series of statements" in prompt:
        ctx = data.get("context", "")
        out = []
        for st in data.get("statements", []):
            v = 1 if _overlap(st, ctx) >= 0.7 else 0
            out.append({"statement": st, "reason": f"stub lexical overlap check -> {v}", "verdict": v})
        return {"statements": out}
    # --- AnswerRelevancy question generation (+ noncommittal flag)
    if "Generate a question for the given answer" in prompt:
        resp = data.get("response", "")
        nc = 1 if re.search(r"don't know|do not know|not sure|cannot find|not available", resp, re.I) else 0
        first = " ".join(resp.split()[:10])
        return {"question": f"What is stated about: {first}?", "noncommittal": nc}
    # --- ContextPrecision verdict per context
    if "verify if the context was useful" in prompt:
        ok = _overlap(data.get("answer", ""), data.get("context", "")) >= 0.25
        return {"reason": "stub lexical overlap check", "verdict": 1 if ok else 0}
    return {"unknown_prompt": True}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a, **k):  # silence
        pass

    def _send(self, code: int, obj: dict):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        with LOCK:
            REQUESTS.append({"path": self.path, "body": body})

        if self.path.endswith("/chat/completions"):
            with LOCK:
                fail = FAIL_NEXT.pop(0) if FAIL_NEXT else None
                trunc = TRUNCATE_NEXT.pop(0) if TRUNCATE_NEXT else False
            if fail:
                return self._send(fail, {"error": {"message": "stub rate limit", "type": "rate_limit_error", "code": "rate_limit_exceeded"}})
            prompt = "\n".join(
                m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])
                for m in body.get("messages", [])
            )
            if LATENCY[0]:
                time.sleep(LATENCY[0])
            payload = canned_answer(prompt)
            content = json.dumps(payload)
            finish = "stop"
            if trunc:
                content = content[: len(content) // 2]
                finish = "length"
            nchoices = int(body.get("n", 1) or 1)
            resp = {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", "stub"),
                "choices": [
                    {"index": i, "message": {"role": "assistant", "content": content}, "finish_reason": finish}
                    for i in range(nchoices)
                ],
                "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(content) // 4,
                          "total_tokens": (len(prompt) + len(content)) // 4},
            }
            return self._send(200, resp)

        if self.path.endswith("/embeddings"):
            inp = body.get("input", "")
            texts = inp if isinstance(inp, list) else [inp]
            b64 = body.get("encoding_format") == "base64"
            data = []
            for i, t in enumerate(texts):
                v = embed(t if isinstance(t, str) else str(t))
                emb = base64.b64encode(struct.pack(f"<{len(v)}f", *v)).decode() if b64 else v
                data.append({"object": "embedding", "index": i, "embedding": emb})
            return self._send(200, {"object": "list", "data": data, "model": body.get("model", "stub"),
                                    "usage": {"prompt_tokens": 1, "total_tokens": 1}})
        return self._send(404, {"error": {"message": "unknown path"}})


def start() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"
