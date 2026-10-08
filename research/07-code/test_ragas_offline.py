"""Offline: what does RAGAS 0.4.3 llm_factory actually put on the wire for each judge model? (mock transport, $0)"""
import asyncio
import json
import os
import warnings

warnings.filterwarnings("ignore")
os.environ["RAGAS_DO_NOT_TRACK"] = "true"

import openai as _oai

if int(_oai.__version__.split('.')[0]) >= 3:   # openai>=3 uses httpx2, 2.x uses httpx
    import httpx2 as httpx_lib
else:
    import httpx as httpx_lib
from openai import AsyncOpenAI
from ragas.llms import llm_factory
from ragas.metrics.collections.faithfulness.util import StatementGeneratorOutput, StatementGeneratorPrompt, StatementGeneratorInput

import openai_support as S

seen: list[dict] = []


def handler(request: httpx_lib.Request) -> httpx_lib.Response:
    body = json.loads(request.content)
    seen.append({"path": request.url.path, **{k: v for k, v in body.items() if k not in ("messages",)}})
    return httpx_lib.Response(200, json={
        "id": "c1", "object": "chat.completion", "created": 1, "model": body["model"],
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": json.dumps({"statements": ["Paris is the capital of France."]})}}],
        "usage": {"prompt_tokens": 500, "completion_tokens": 40, "total_tokens": 540,
                  "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 0}},
    })


async def probe(model: str, shim=None, **kw):
    meter = S.UsageMeter()
    client = AsyncOpenAI(api_key="sk-test", max_retries=0,
                         http_client=httpx_lib.AsyncClient(transport=httpx_lib.MockTransport(handler)))
    S.meter_client(client, meter, "ragas")  # wrap BEFORE llm_factory so instructor captures the metered create()
    if shim:
        S.adapt_for_reasoning(client, effort=shim)
    llm = llm_factory(model, client=client, **kw)
    out = await llm.agenerate(StatementGeneratorPrompt().to_string(StatementGeneratorInput(question="q", answer="a")),
                              StatementGeneratorOutput)
    return out, meter.snapshot()


async def main():
    for model, kw in [("gpt-4.1-mini", {}), ("gpt-6-luna", {}), ("gpt-6-luna", {"max_tokens": 4096, "reasoning_effort": "none"}),
                      ("gpt-6.1-sol", {}), ("gpt-5.6-luna", {}), ("gpt-6.1-sol", {"SHIM": "low"}), ("gpt-6-luna", {"SHIM": "none"}), ("gpt-5.6-luna", {"SHIM": "low"})]:
        seen.clear()
        shim = kw.pop("SHIM", None)
        out, snap = await probe(model, shim=shim, **kw)
        kw = {**kw, **({"SHIM": shim} if shim else {})}
        req = {k: v for k, v in seen[0].items() if k in ("model", "temperature", "top_p", "max_tokens", "max_completion_tokens",
                                                         "reasoning_effort", "response_format", "tools", "n", "path")}
        if "response_format" in req:
            req["response_format"] = req["response_format"].get("type") if isinstance(req["response_format"], dict) else req["response_format"]
        print(f"{model:13s} kw={kw!s:55s} WIRE={req}  metered_calls={snap['rows'][0]['calls']} usd={snap['total_usd']}")


asyncio.run(main())
