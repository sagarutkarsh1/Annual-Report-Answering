"""Offline end-to-end: 3 RAGAS 0.4.3 metrics through a mock OpenAI transport. Counts the REAL number of API calls/tokens-on-wire.
No network, no key, $0.  Run: python test_ragas_e2e_offline.py
"""
import asyncio
import json
import os
import time
import warnings

warnings.filterwarnings("ignore")
os.environ["RAGAS_DO_NOT_TRACK"] = "true"

import openai as _oai

if int(_oai.__version__.split('.')[0]) >= 3:   # openai>=3 uses httpx2, 2.x uses httpx
    import httpx2 as httpx_lib
else:
    import httpx as httpx_lib
from openai import AsyncOpenAI
from ragas.embeddings.base import embedding_factory
from ragas.llms import llm_factory
from ragas.metrics.collections import AnswerRelevancy, ContextPrecisionWithoutReference, Faithfulness

import openai_support as S

calls: list[dict] = []


def handler(request: httpx_lib.Request) -> httpx_lib.Response:
    body = json.loads(request.content)
    path = request.url.path
    if path.endswith("/embeddings"):
        inp = body["input"] if isinstance(body["input"], list) else [body["input"]]
        calls.append({"kind": "embeddings", "n_inputs": len(inp)})
        return httpx_lib.Response(200, json={"object": "list", "model": body["model"],
                                          "data": [{"object": "embedding", "index": i, "embedding": [1.0, 0.1 * (i + 1), 0.0]} for i in range(len(inp))],
                                          "usage": {"prompt_tokens": 20 * len(inp), "total_tokens": 20 * len(inp)}})
    text = "\n".join(m["content"] if isinstance(m["content"], str) else json.dumps(m["content"]) for m in body["messages"])
    if "Create one or more statements" in text or "break down" in text.lower() and "statements" in text.lower() and "verdict" not in text.lower():
        kind, out = "faith.statements", {"statements": ["Statement one.", "Statement two."]}
    elif "verdict" in text.lower() and "statements" in text.lower() and "context" in text.lower() and "useful in arriving" not in text:
        kind, out = "faith.nli", {"statements": [{"statement": "Statement one.", "reason": "ok", "verdict": 1},
                                                 {"statement": "Statement two.", "reason": "no", "verdict": 0}]}
    elif "useful in arriving" in text:
        kind, out = "ctxprec", {"reason": "useful", "verdict": 1}
    else:
        kind, out = "relevancy", {"question": "What was operating profit?", "noncommittal": 0}
    calls.append({"kind": kind, "keys": sorted(k for k in body if k != "messages")})
    return httpx_lib.Response(200, json={"id": "c", "object": "chat.completion", "created": 1, "model": body["model"],
                                      "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(out)}}],
                                      "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
                                                "prompt_tokens_details": {"cached_tokens": 0},
                                                "completion_tokens_details": {"reasoning_tokens": 0}}})


async def main():
    meter = S.UsageMeter()
    client = AsyncOpenAI(api_key="sk-test", max_retries=0, http_client=httpx_lib.AsyncClient(transport=httpx_lib.MockTransport(handler)))
    S.meter_client(client, meter, "ragas")
    llm = llm_factory("gpt-4.1-mini", client=client, max_tokens=4096)
    emb = embedding_factory("openai", model="text-embedding-3-small", client=client)
    faith = Faithfulness(llm=llm)
    rel = AnswerRelevancy(llm=llm, embeddings=emb, strictness=3)
    cp = ContextPrecisionWithoutReference(llm=llm)
    q = "What was underlying operating profit?"
    a = "Underlying operating profit was GBP 4.4bn [p. 42]. Capex was GBP 9bn [p. 44]."
    ctxs = ["page 42 text ...", "page 43 text ...", "page 44 text ...", "page 45 text ..."]
    t = time.time()
    f, r, p = await asyncio.gather(faith.ascore(q, a, ctxs), rel.ascore(q, a), cp.ascore(q, a, ctxs))
    print("scores:", round(f.value, 3), round(r.value, 3), round(p.value, 3), f"({time.time() - t:.2f}s mock)")
    kinds = {}
    for c in calls:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    print("calls by kind:", kinds, "TOTAL LLM calls:", sum(v for k, v in kinds.items() if k != "embeddings"))
    snap = meter.snapshot()
    print("metered:", [(x["model"], x["calls"]) for x in snap["rows"]], "usd(mock tokens)=", snap["total_usd"])


asyncio.run(main())
