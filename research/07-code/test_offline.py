"""Offline test of openai_support.py using httpx_lib.MockTransport (zero network, zero cost)."""
import asyncio
import json

import openai as _oai

if int(_oai.__version__.split('.')[0]) >= 3:   # openai>=3 uses httpx2, 2.x uses httpx
    import httpx2 as httpx_lib
else:
    import httpx as httpx_lib
import openai
from openai import AsyncOpenAI, OpenAI
from pydantic import BaseModel

import openai_support as S

RESP = {
    "id": "resp_1", "object": "response", "created_at": 1, "status": "completed", "model": "gpt-6-luna",
    "output": [{"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": "{\"answer\": \"42\"}", "annotations": []}]}],
    "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 400, "cache_write_tokens": 100},
              "output_tokens": 300, "output_tokens_details": {"reasoning_tokens": 200}, "total_tokens": 1300},
    "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
}
CHAT = {
    "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-4.1-mini-2025-04-14",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
    "usage": {"prompt_tokens": 2000, "completion_tokens": 100, "total_tokens": 2100,
              "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 0}},
}
EMB = {"object": "list", "model": "text-embedding-3-small",
       "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
       "usage": {"prompt_tokens": 100, "total_tokens": 100}}


def handler(request: httpx_lib.Request) -> httpx_lib.Response:
    p = request.url.path
    if p.endswith("/responses"):
        return httpx_lib.Response(200, json=RESP)
    if p.endswith("/chat/completions"):
        return httpx_lib.Response(200, json=CHAT)
    if p.endswith("/embeddings"):
        return httpx_lib.Response(200, json=EMB)
    if p.endswith("/models/gpt-6-luna"):
        return httpx_lib.Response(200, json={"id": "gpt-6-luna", "object": "model", "created": 1, "owned_by": "openai", "shutdown_date": None})
    if p.endswith("/models/gpt-9-nope"):
        return httpx_lib.Response(404, json={"error": {"message": "The model `gpt-9-nope` does not exist", "type": "invalid_request_error", "param": None, "code": "model_not_found"}})
    if p.endswith("/models/quota"):
        return httpx_lib.Response(429, json={"error": {"message": "You exceeded your current quota", "type": "insufficient_quota", "param": None, "code": "credit_balance_exhausted"}})
    return httpx_lib.Response(500, json={"error": {"message": "mock", "type": "server_error"}})


def mk(cls, **kw):
    http = (httpx_lib.AsyncClient if cls is AsyncOpenAI else httpx_lib.Client)(transport=httpx_lib.MockTransport(handler))
    return cls(api_key="sk-test", http_client=http, max_retries=0, **kw)


def main() -> None:
    meter = S.UsageMeter()
    c = S.meter_client(mk(OpenAI), meter, "retrieval")
    r = c.responses.create(model="gpt-6-luna", input="hi", reasoning={"effort": "low"}, max_output_tokens=300)
    assert r.output_text == '{"answer": "42"}'
    c.chat.completions.create(model="gpt-4.1-mini", messages=[{"role": "user", "content": "x"}], max_completion_tokens=100)
    c.embeddings.create(model="text-embedding-3-small", input="q")

    # async + instructor-style "wrap before use"
    async def run_async():
        ac = S.meter_client(mk(AsyncOpenAI), meter, "ragas")
        await ac.chat.completions.create(model="gpt-4.1-mini", messages=[{"role": "user", "content": "x"}])
    asyncio.run(run_async())

    snap = meter.snapshot()
    print(json.dumps([(r["stage"], r["model"], r["calls"], r["usd"]) for r in snap["rows"]]))
    # hand check: gpt-6-luna: fresh=1000-400-100=500*0.10 + 400*0.01 + 100*0.125 + 300*0.5 = 50+4+12.5+150 = 216.5 -> 0.0002165
    lun = [x for x in snap["rows"] if x["model"] == "gpt-6-luna"][0]
    assert abs(lun["usd"] - 0.0002165) < 1e-9, lun
    mini = [x for x in snap["rows"] if x["model"].startswith("gpt-4.1-mini")]
    print("gpt-4.1-mini rows:", mini)

    # snapshot-name price resolution
    assert S.price_for("gpt-4o-mini-2024-07-18") == S.PRICES["gpt-4o-mini"]
    assert S.price_for("gpt-4.1-mini-2025-04-14") == S.PRICES["gpt-4.1-mini"]
    assert S.price_for("totally-new-model") is None

    # structured outputs helper compiles + parses (Responses API, pydantic text_format)
    class Ans(BaseModel):
        answer: str

    parsed = mk(OpenAI).responses.parse(model="gpt-6-luna", input="q", text_format=Ans)
    print("responses.parse ->", parsed.output_parsed)
    assert parsed.output_parsed.answer == "42"

    # preflight classifications
    pf = S.preflight(mk(OpenAI), {"index": "gpt-6-luna", "chat": "gpt-9-nope"})
    print(pf)
    assert pf["index"]["ok"] and not pf["chat"]["ok"]

    # non-retryable 429 detection
    try:
        mk(OpenAI).models.retrieve("quota")
    except openai.RateLimitError as e:
        print("429 code:", e.code, "retryable:", S.is_retryable(e))
        assert e.code == "credit_balance_exhausted" and not S.is_retryable(e)
    print("ALL OFFLINE CHECKS PASSED on openai", openai.__version__)


if __name__ == "__main__":
    main()
