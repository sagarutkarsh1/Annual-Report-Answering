"""
Offline tests for ragas_minimal_example.py (no key, no network; uses ragas_offline_stub_server.py on 127.0.0.1).
Run:   python ragas_example_offline_test.py
Proves: the call path on this machine, call counts per metric, request parameters per judge model,
        partial-failure behaviour, NaN->None, input sanitising, sync wrapper.  Scores are from a fake judge.
"""
import asyncio
import collections
import json
import os
import sys

os.environ["RAGAS_DO_NOT_TRACK"] = "true"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ragas_offline_stub_server as stub  # noqa: E402
import ragas_minimal_example as ex  # noqa: E402

srv, BASE = stub.start()
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"   [{detail}]" if detail else ""))


def cfg(**kw):
    return ex.ScorerConfig(base_url=BASE, api_key="offline-stub", client_max_retries=0, **kw)


Q, A = ex.DEMO_Q, ex.DEMO_A
CTX = ex.DEMO_CTX


def kinds():
    return collections.Counter("chat" if r["path"].endswith("completions") else "emb" for r in stub.REQUESTS)


async def main():
    # 1. happy path + call counts (N=3 contexts -> 2 + 3 + 3 = 8 chat calls, 2 embedding calls)
    s = ex.RagasScorer(cfg())
    stub.REQUESTS.clear()
    r = await s.score(Q, A, CTX)
    k = kinds()
    check("happy path: all three scores present", all(r[m] is not None for m in ("faithfulness", "answer_relevancy", "context_precision")), json.dumps({m: r[m] for m in ("faithfulness", "answer_relevancy", "context_precision")}))
    check("call counts = 5+N chat calls and 2 embedding calls (N=3)", k["chat"] == 8 and k["emb"] == 2, str(dict(k)))
    check("per-context verdicts returned", len(r["context_verdicts"]) == 3 and r["context_verdicts"][2]["verdict"] == 0)
    check("citation markers stripped before judging", not any("[p." in json.dumps(q["body"]["messages"]) for q in stub.REQUESTS if q["path"].endswith("completions")))
    chat0 = next(q["body"] for q in stub.REQUESTS if q["path"].endswith("completions"))
    check("non-reasoning judge request params", chat0["model"] == "gpt-4.1-mini" and chat0["max_tokens"] == 4096 and chat0["response_format"] == {"type": "json_object"}, str({k_: v for k_, v in chat0.items() if k_ != "messages"}))
    rel_calls = [q["body"] for q in stub.REQUESTS if q["path"].endswith("completions") and "Generate a question for the given answer" in json.dumps(q["body"]["messages"])]
    check("question-generation calls use temperature 0.3 (not ragas' 0.01) so the 3 questions can differ", len(rel_calls) == 3 and all(b["temperature"] == 0.3 for b in rel_calls))
    await s.aclose()

    # 2. partial failure: HTTP 429 on exactly one chat call, SDK retries disabled -> one metric errors, others survive
    s = ex.RagasScorer(cfg())
    stub.FAIL_NEXT.append(429)
    r = await s.score(Q, A, CTX)
    n_err = len(r["errors"])
    check("one HTTP 429 fails exactly one metric, the other two still scored", n_err == 1 and sum(r[m] is not None for m in ("faithfulness", "answer_relevancy", "context_precision")) == 2, str(r["errors"]))
    await s.aclose()

    # 3. truncated JSON (finish_reason=length) -> metric error, not a crash
    s = ex.RagasScorer(cfg())
    stub.TRUNCATE_NEXT.append(True)
    r = await s.score(Q, A, CTX)
    check("truncated judge output -> IncompleteOutputException captured in errors", any("IncompleteOutputException" in v for v in r["errors"].values()), str(r["errors"]))
    await s.aclose()

    # 4. input sanitising
    s = ex.RagasScorer(cfg(max_contexts=2))
    stub.REQUESTS.clear()
    r = await s.score(Q, A, ["", "  ", CTX[0], CTX[0], CTX[1], CTX[2]])
    check("blank + duplicate contexts dropped, max_contexts enforced", r["n_contexts_scored"] == 2 and r["n_contexts_input"] == 6, f"scored={r['n_contexts_scored']}")
    r = await s.score(Q, A, [])
    check("no contexts -> clear error, no LLM call", "input" in r["errors"])
    await s.aclose()

    # 5. no statements -> Faithfulness NaN -> None (valid JSON)
    import math
    check("NaN -> None helper", ex._num(float("nan")) is None and ex._num(1.2) == 1.0 and ex._num(-0.1) == 0.0 and ex._num(0.99999999995) == 1.0)
    json.dumps(r, allow_nan=False)  # must not raise
    check("result is strict-JSON serialisable (allow_nan=False)", True)

    # 6. reasoning-model parameter handling (what is actually sent)
    for model, expect in [
        ("gpt-4.1-mini", {"max_tokens": 4096}),
        ("gpt-5-mini", {"max_completion_tokens": 4096, "temperature": 1.0}),
        ("gpt-5.4-mini", {"max_completion_tokens": 4096}),   # ragas misses it; our shim fixes it
        ("gpt-6.1-sol", {"max_completion_tokens": 4096}),
    ]:
        s = ex.RagasScorer(cfg(judge_model=model))
        stub.REQUESTS.clear()
        await s.score(Q, A, CTX[:1])
        body = next(q["body"] for q in stub.REQUESTS if q["path"].endswith("completions"))
        ok = all(body.get(k_) == v for k_, v in expect.items())
        if "max_completion_tokens" in expect:
            ok = ok and "max_tokens" not in body
        if model in ("gpt-5.4-mini", "gpt-6.1-sol"):
            ok = ok and "temperature" not in body and "top_p" not in body
        check(f"request params for {model}", ok, str({k_: v for k_, v in body.items() if k_ not in ("messages", "response_format")}))
        await s.aclose()

    # 7. concurrency: 10 answers scored at once through one scorer
    s = ex.RagasScorer(cfg(concurrency=4))
    stub.LATENCY[0] = 0.05
    rs = await asyncio.gather(*(s.score(Q, A, CTX) for _ in range(10)))
    stub.LATENCY[0] = 0.0
    check("10 concurrent scorings, semaphore=4, all complete without errors", all(not x["errors"] for x in rs))
    await s.aclose()


asyncio.run(main())

# 8. sync wrapper reused several times (the case where metric.score() with a shared client breaks every 2nd call)
ss = ex.SyncScorer(cfg())
outs = [ss.score(Q, A, CTX) for _ in range(5)]
ss.close()
check("SyncScorer: 5 consecutive sync calls all succeed", all(not o["errors"] for o in outs))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
