"""Tiny OpenAI-compatible mock on localhost. Logs every request body to mock_log.jsonl and answers
(1st call) with a get_document_structure tool call, (2nd) get_page_content, (3rd) a cited final answer.
Supports /v1/chat/completions (stream + non-stream). Anything else: logs + 404."""
import json, sys, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
LOG = open("mock_log.jsonl", "a", encoding="utf-8")
STATE = {"n": 0}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        try: j = json.loads(body)
        except Exception: j = {"raw": body[:200].decode("utf8","ignore")}
        LOG.write(json.dumps({"path": self.path, "body": j}, ensure_ascii=False) + "\n"); LOG.flush()
        if not self.path.endswith("/chat/completions"):
            self.send_response(404); self.end_headers(); self.wfile.write(b'{"error":{"message":"mock: unsupported path"}}'); return
        STATE["n"] += 1; n = STATE["n"]
        msgs = j.get("messages", [])
        n_tool_results = sum(1 for m in msgs if m.get("role") == "tool")
        if n_tool_results == 0:
            msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_document_structure", "arguments": json.dumps({"doc_name": "2023-annual-report.pdf"})}}]}; fin = "tool_calls"
        elif n_tool_results == 1:
            msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_2", "type": "function", "function": {"name": "get_page_content", "arguments": json.dumps({"doc_name": "2023-annual-report.pdf", "pages": "9-10"})}}]}; fin = "tool_calls"
        else:
            msg = {"role": "assistant", "content": 'Inflation slowed but stays above 2 percent. <cite doc="2023-annual-report.pdf" page="10"/>'}; fin = "stop"
        base = {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()), "model": j.get("model", "mock")}
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
        if j.get("stream"):
            self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
            def send(o): self.wfile.write(b"data: " + json.dumps(o).encode() + b"\n\n"); self.wfile.flush()
            c = dict(base, object="chat.completion.chunk")
            send(dict(c, choices=[{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]))
            if msg.get("tool_calls"):
                tc = msg["tool_calls"][0]
                send(dict(c, choices=[{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": ""}}]}, "finish_reason": None}]))
                send(dict(c, choices=[{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": tc["function"]["arguments"]}}]}, "finish_reason": None}]))
            else:
                send(dict(c, choices=[{"index": 0, "delta": {"content": msg["content"]}, "finish_reason": None}]))
            send(dict(c, choices=[{"index": 0, "delta": {}, "finish_reason": fin}]))
            send(dict(c, choices=[], usage=usage))
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
        else:
            out = dict(base, choices=[{"index": 0, "message": msg, "finish_reason": fin}], usage=usage)
            b = json.dumps(out).encode()
            self.send_response(200); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
