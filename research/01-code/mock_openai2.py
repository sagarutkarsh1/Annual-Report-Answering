"""Mock OpenAI server with /v1/responses (non-stream) in addition to chat/completions. Logs to mock_log2.jsonl."""
import json, sys, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
LOG = open("mock_log2.jsonl", "a", encoding="utf-8")


def resp_obj(j, output):
    return {"id": "resp_" + uuid.uuid4().hex, "object": "response", "created_at": int(time.time()), "model": j.get("model", "mock"),
            "status": "completed", "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 10,
                      "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 110}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        j = json.loads(body)
        LOG.write(json.dumps({"path": self.path, "body": j}, ensure_ascii=False) + "\n")
        LOG.flush()
        if self.path.endswith("/responses"):
            inp = j.get("input", [])
            n_out = sum(1 for m in inp if isinstance(m, dict) and m.get("type") == "function_call_output")
            if n_out == 0:
                out = [{"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "get_document_structure",
                        "arguments": json.dumps({"doc_name": "2023-annual-report.pdf"}), "status": "completed"}]
            elif n_out == 1:
                out = [{"type": "function_call", "id": "fc_2", "call_id": "call_2", "name": "get_page_content",
                        "arguments": json.dumps({"doc_name": "2023-annual-report.pdf", "pages": "10"}), "status": "completed"}]
            else:
                out = [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text",
                                     "text": 'Core PCE inflation was 2.8 percent. <cite doc="2023-annual-report.pdf" page="10"/>',
                                     "annotations": []}]}]
            b = json.dumps(resp_obj(j, out)).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'{"error":{"message":"mock: unsupported"}}')


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
