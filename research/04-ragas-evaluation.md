# 04 - RAGAS evaluation: faithfulness, answer (response) relevancy, context precision - OpenAI judge, no ground truth

Researcher: "RAGAS". Date of research: 2026-10-07. Machine: Windows 11, Python 3.13.3 (venvs under the session scratchpad: `ragas-venv`, `ragas-venv2`, `ragas-venv3`).
Companion files in this folder:
- `ragas_minimal_example.py` - the wired, tested scorer (async class + sync wrapper + `--offline` mode).
- `ragas_example_offline_test.py` - 18 offline tests of that script (call counts, request parameters, failure modes).
- `ragas_offline_stub_server.py` - a 120-line fake OpenAI REST server (127.0.0.1 only) used for all offline proofs.
- Overlaps with `07-openai-models-and-costs.md` (section 7-8: judge model, prices, shim) and `03-pageindex-retrieval-patterns-and-prompts.md` (section 5.6: context blocks). Where we agree I say so; I re-verified the RAGAS parts independently.

Legend: **VERIFIED** = I read it in the installed source / official docs or ran it this session (offline, $0, no API key). **INFERRED** = my reasoning from verified facts. **UNVERIFIED** = needs a live OpenAI key to confirm.

---

## 0. Bottom line (read this first)

1. **Use RAGAS 0.4.3 (latest on PyPI, released 2026-01-13) and ONLY its new "collections" API**: `from ragas.metrics.collections import Faithfulness, AnswerRelevancy, ContextPrecisionWithoutReference`, with `llm_factory(model, client=AsyncOpenAI(...))` and `embedding_factory("openai", model=..., client=...)`, scored with `await metric.ascore(user_input=..., response=..., retrieved_contexts=[...])`. Everything else in the old docs (`evaluate()`, `SingleTurnSample` + `single_turn_ascore`, `LangchainLLMWrapper`, `from ragas.metrics import Faithfulness`) is deprecated in 0.4.3, noisier, and `evaluate()` rejects the new metrics. VERIFIED.
2. **A plain `pip install ragas` is broken today.** `import ragas` raises `ModuleNotFoundError: langchain_community.chat_models.vertexai` because langchain-community 0.4.2 (2026-05-22) removed that module and ragas 0.4.3 imports it unconditionally. Unmerged upstream fix PRs #2997/#3017, issue #2995 (open). **Pin `langchain-community==0.4.1`.** VERIFIED (reproduced twice, fixed twice). Verified working pin set:
   ```
   pip install "ragas==0.4.3" "langchain-community==0.4.1" "instructor>=1.15" "openai>=2"
   ```
   (Also works with openai 2.54.0 - what pip picks when PageIndex's `litellm==1.97.0` is added - and with the old instructor 1.3.2 / openai 1.109.1 that plain pip resolves to; see 1.3.)
3. **Context precision with no reference = `ContextPrecisionWithoutReference`** (alias `ContextUtilization`). It asks the judge, per retrieved context, "was this context useful in arriving at the given answer?" and averages precision@k over the useful ones. It compares contexts to **our generated answer**, not to the truth, and it is **rank-sensitive but does not penalise junk placed after the useful pages** (`[1,0,0]` scores 1.0). Show it next to a plain "x of N pages useful" number. See 5.
4. **Contexts to pass = the page texts the answering LLM actually read, one string per page, in the order they were retrieved** (not the quoted snippets, not whole multi-page nodes). Cap at ~8 pages x ~2k tokens. See 6.
5. **Per answer: 5+N judge LLM calls + 2 embedding calls** (N = number of contexts). Stock `ContextPrecision` awaits its N calls one-by-one; run them concurrently (my `ParallelContextPrecision` subclass: 2.48 s -> 0.35 s for 8 contexts at 0.3 s/call, identical score). Cost with `gpt-4.1-mini`: roughly $0.005-$0.036 per answer (3-10 pages; ~$0.011 for 5 pages); embeddings are ~free. See 4.5, 7.6.
6. **Judge = a non-reasoning model by default (`gpt-4.1-mini`, fallback `gpt-4o-mini`)**; set `max_tokens>=4096` (the default 1024 truncates the faithfulness NLI output once an answer has ~16+ statements -> `IncompleteOutputException`). RAGAS 0.4.3 only auto-adapts reasoning models whose id has an integer version (`gpt-5`, `gpt-5-mini`, `gpt-6-luna`, `o3`, `o4-mini`); **dotted ids (`gpt-5.4-mini`, `gpt-5.6-luna`, `gpt-6.1-sol`) are treated as classic chat models and would receive `max_tokens`/`temperature`/`top_p`** (expected HTTP 400, UNVERIFIED live). The example fixes that. See 7.
7. **Async rules (Windows too):** always `await metric.ascore(...)`; one `AsyncOpenAI` client per event loop; **never call `metric.score()` repeatedly with a shared AsyncOpenAI client - it fails on every 2nd call** (VERIFIED). For sync code use one dedicated loop thread (`SyncScorer` in the example). No `nest_asyncio` needed. See 8.
8. **Set `RAGAS_DO_NOT_TRACK=true`** (exactly "true"; "1" does not work) before importing ragas. See 9.
9. **Scores are LLM-judged estimates**, 0..1, judge-dependent, with specific blind spots (refusals score 0 on relevancy, derived numbers can score 0 on faithfulness). Ready-made honest tooltips are in 11 and in `TOOLTIPS` inside the example.

---

## 1. Version, packaging, environment

### 1.1 Version facts

| Fact | Value | Status |
|---|---|---|
| Latest PyPI release | **ragas 0.4.3**, uploaded 2026-01-13T17:47:59 (`ragas-0.4.3-py3-none-any.whl` + sdist). Nothing newer as of 2026-10-07 | VERIFIED (PyPI JSON + GitHub releases API) |
| Previous | 0.4.2 (2025-12-23), 0.4.1 (2025-12-10), 0.4.0 (2025-12-03), 0.3.9 (2025-11-11) | VERIFIED |
| `requires_python` | `>=3.9`; classifiers empty. **Runs on 3.13.3 / Windows 11** (all deps have cp313 win_amd64 wheels, no compiler needed) | VERIFIED (installed + ran) |
| Repo | moved from `explodinggradients/ragas` (301) to **https://github.com/vibrantlabsai/ragas**; default branch `main`; ~16k stars; **last commit 2026-02-24** (CI tweak). Effectively unmaintained for 7+ months; open PRs for the import bug are not merged | VERIFIED (GitHub API) |
| Docs | https://docs.ragas.io/en/stable/ ; I also read the v0.4.3 docs source (`docs/` of tag `v0.4.3`, cloned to scratchpad `ragas-src-0.4.3`) | VERIFIED |
| Install footprint | 98 packages, **593 MB** site-packages (pulls `datasets`, `pyarrow`, `pandas`, `scipy`, the whole langchain stack, `scikit-network`, ...). `import ragas` ~6 s warm | VERIFIED |
| Licence | Apache-2.0 (repo `LICENSE`, `pyproject.toml: license = {file = "LICENSE"}`) | VERIFIED |

Dependencies that matter (from the wheel metadata): `openai>=1.0.0`, `instructor` (unpinned), `langchain`, `langchain-core`, `langchain-community`, `langchain_openai` (all unpinned), `datasets>=4.0.0`, `pydantic>=2`, `tiktoken`, `diskcache>=5.6.3`, `nest-asyncio`, `numpy<3`, `pillow>=10.4`, `networkx`, `scikit-network`, `tqdm`, `typer`, `rich`, `appdirs`.

### 1.2 The `import ragas` crash (and the fix) - VERIFIED

- Symptom with a fresh `pip install ragas`: `ragas/llms/base.py` line 12 `from langchain_community.chat_models.vertexai import ChatVertexAI` -> `ModuleNotFoundError: No module named 'langchain_community.chat_models.vertexai'`. Whole package unusable (even `import ragas`).
- Cause: ragas pins nothing; pip resolves `langchain-community 0.4.2` (2026-05-22), which no longer ships `chat_models/vertexai.py`. I downloaded the wheels: **0.4.1 and 0.3.31 contain it, 0.4.2 does not.**
- Upstream: issue https://github.com/vibrantlabsai/ragas/issues/2995 and older #2741 (open); fix PRs #2997, #3017, #2979, #2991 all open/unmerged on 2026-10-07. `ChatVertexAI` moved to `langchain_google_vertexai`.
- Fix: **`pip install "langchain-community==0.4.1"`** (works with langchain-core 1.x). After that `import ragas` works (verified in three venvs).

### 1.3 Resolver outcomes (what pip actually picked) - VERIFIED

| Install command | instructor | openai | langchain-openai | Works? |
|---|---|---|---|---|
| `pip install ragas` | **1.3.2** (very old; pip backtracked) | 1.109.1 | 1.1.9 | import crashes (1.2); after the langchain-community pin it works (16/18 offline tests; the 2 "failures" are behavioural: instructor 1.3.2 silently re-asks once, so a single injected 429 / truncation is hidden instead of raised) |
| `pip install ragas "instructor>=1.15" "openai>=2"` | 1.17.0 | 3.3.0 | 1.6.7 | crash until the pin; with pin 18/18 |
| above + PageIndex requirements (`litellm==1.97.0`, `openai-agents>=0.18.1`, `mcp>=1.19`, ...) via `pip install --dry-run` | 1.17.0 | **2.54.0** | 1.6.7 | with openai 2.54.0 forced into the ragas env: `pip check` clean, 18/18 offline tests pass |

Latest `openai` SDK on PyPI is **3.26.0** (3.x line since 2026-08-12); instructor 1.17.0 needs `openai>=2.0,<4`; `07-openai-models-and-costs.md` notes `jiter` pins make pip prefer openai 2.54 next to litellm. Recommendation: **pin the stack once in the project requirements** (`ragas==0.4.3`, `langchain-community==0.4.1`, `instructor>=1.15`, `openai` whatever PageIndex's resolution allows - 2.54.0 and 3.3.0 both tested) and re-run `ragas_example_offline_test.py` after any bump.

---

## 2. Which API is current, which is deprecated (ragas 0.4.3) - VERIFIED in source

| Area | CURRENT (use) | DEPRECATED / legacy in 0.4.3 | Evidence |
|---|---|---|---|
| Metric classes | `ragas.metrics.collections.*` (`Faithfulness`, `AnswerRelevancy`, `ContextPrecision*`, ...). Subclass `BaseMetric` (-> `SimpleBaseMetric`); `async ascore(**kwargs) -> MetricResult`; sync `score()`, `batch_score()`, `abatch_score()` | `from ragas.metrics import Faithfulness, ResponseRelevancy, LLMContextPrecisionWithoutReference, ...` still works through `__getattr__` but emits `DeprecationWarning` ("will be removed in v1.0"). Legacy classes take `SingleTurnSample` via `single_turn_ascore` / `single_turn_score` | `metrics/__init__.py` (`_DEPRECATED_METRICS`), run output |
| Name of the "response relevancy" metric | **`AnswerRelevancy`** (`name="answer_relevancy"`). `ResponseRelevancy` does **not** exist in `collections` (ImportError verified) | Legacy module has `ResponseRelevancy` (primary dataclass) and `AnswerRelevancy` (subclass) and instance `answer_relevancy`; both report as `answer_relevancy`. The docs index page still titles it "Response Relevancy" | `metrics/_answer_relevance.py` |
| Context precision names | `ContextPrecisionWithReference`, **`ContextPrecisionWithoutReference`**; wrappers `ContextPrecision` (= WithReference, name `context_precision`) and `ContextUtilization` (= WithoutReference, name `context_utilization`) | legacy `LLMContextPrecisionWithReference/WithoutReference`, `NonLLMContextPrecisionWithReference` (needs `reference_contexts` + `rapidfuzz`), `IDBasedContextPrecision` (needs reference ids) | `collections/context_precision/metric.py` |
| LLM | `ragas.llms.llm_factory(model, provider="openai", client=..., adapter="auto", cache=None, **model_kwargs) -> InstructorBaseRagasLLM` (instructor, **Mode.JSON**) | `LangchainLLMWrapper`, `LlamaIndexLLMWrapper` (DeprecationWarning on construction); collections metrics **reject** them: `ValueError: Collections metrics only support modern InstructorLLM` | `llms/base.py`, `collections/base.py` |
| Embeddings | `ragas.embeddings.base.embedding_factory("openai", model=..., client=...)` -> `OpenAIEmbeddings` (native client; sync or async). Also `from ragas.embeddings import OpenAIEmbeddings` | `LangchainEmbeddingsWrapper`, `LlamaIndexEmbeddingsWrapper` (DeprecationWarning). **`from ragas.embeddings import embedding_factory` itself warns** - import from `ragas.embeddings.base` | `embeddings/__init__.py` |
| Batch evaluation | `@experiment(...)` decorator (`from ragas import experiment`) over a `Dataset`/`DataTable`; or just loop/gather `ascore` yourself | `evaluate()` and `aevaluate()` both emit `DeprecationWarning: ... Use the @experiment decorator instead`. **`evaluate()` only accepts legacy `Metric` objects**: passing a collections metric raises `TypeError: All metrics must be initialised metric objects` (verified) | `evaluation.py` |
| Timeouts/retries | Configure on the **OpenAI client**: `AsyncOpenAI(timeout=..., max_retries=...)` (docs `howtos/customizations/run_config.md`) | `RunConfig(timeout=180, max_retries=10, max_wait=60, max_workers=16, ...)` only affects legacy `evaluate()` / legacy wrappers; collections metrics ignore it | docs + source |
| Output | `MetricResult` (`.value`, `.reason`, `.traces`); behaves like a float (`float(r)`, arithmetic) | raw `float` (np.nan on failure) | `metrics/result.py` |

Migration guide in the repo: `docs/howtos/migrations/migrate_from_v03_to_v04.md` (its "before" example mixing `evaluate()` with collections metrics is misleading - it does not run).

`SingleTurnSample` / `EvaluationDataset` still exist and are exported from `ragas` (needed for legacy `evaluate()` and handy as a plain data record; the collections API itself just takes keyword args):

```python
from ragas import SingleTurnSample, EvaluationDataset
SingleTurnSample.model_fields  # user_input, retrieved_contexts, reference_contexts, retrieved_context_ids,
                               # reference_context_ids, response, multi_responses, reference, rubrics,
                               # persona_name, query_style, query_length         (all Optional)
s = SingleTurnSample(user_input="q", response="a", retrieved_contexts=["c1", "c2"])   # reference not needed
ds = EvaluationDataset(samples=[s]); ds.get_sample_type()  # SingleTurnSample ; ds.features() -> ['user_input','retrieved_contexts','response']
await metric.ascore(**s.model_dump(include={"user_input", "response", "retrieved_contexts"}))   # bridge to collections
```

---

## 3. Exact imports and signatures (0.4.3) - VERIFIED via `inspect.signature`

```python
from openai import AsyncOpenAI
from ragas.llms import llm_factory                       # also ragas.llms.base.llm_factory
from ragas.embeddings.base import embedding_factory      # NOT ragas.embeddings (warns)
from ragas.metrics.collections import (Faithfulness, AnswerRelevancy,
        ContextPrecisionWithoutReference, ContextPrecisionWithReference, ContextPrecision, ContextUtilization,
        ContextRelevance, ResponseGroundedness)
from ragas.metrics.result import MetricResult
from ragas import SingleTurnSample, EvaluationDataset, RunConfig, experiment      # evaluate/aevaluate exist but deprecated

llm_factory(model: str, provider: str = "openai", client: Any = None, adapter: str = "auto",
            cache: CacheInterface | None = None, **kwargs) -> InstructorBaseRagasLLM
   # client is REQUIRED (ValueError otherwise). kwargs -> InstructorModelArgs overrides: temperature=0.01, top_p=0.1,
   # max_tokens=1024, system_prompt=None (+ any extra e.g. reasoning_effort="low") are splatted into
   # client.chat.completions.create(model=..., messages=[system?, user], response_model=Pydantic, **kwargs).
   # Result has .model_args (dict you can edit), .generate(prompt, Model), async .agenerate(prompt, Model).
   # An async client => only agenerate works ("Cannot use agenerate() with a synchronous client" otherwise).
embedding_factory(provider="openai", model=None, run_config=None, client=None, interface="auto", base_url=None,
                  cache=None, **kwargs)
   # with client=... -> modern OpenAIEmbeddings(client, model="text-embedding-3-small" default, cache)
   # WITHOUT client and provider "openai"/model-name-like -> LEGACY path (LangchainEmbeddingsWrapper + DeprecationWarning)

Faithfulness(llm, name="faithfulness")                         .ascore(user_input, response, retrieved_contexts: list[str])
AnswerRelevancy(llm, embeddings, name="answer_relevancy", strictness=3)   .ascore(user_input, response)
ContextPrecisionWithoutReference(llm, name="context_precision_without_reference")  .ascore(user_input, response, retrieved_contexts)
ContextPrecisionWithReference(llm, name=...)                   .ascore(user_input, reference, retrieved_contexts)
ContextRelevance(llm, name, max_retries=5)                     .ascore(user_input, retrieved_contexts)      # no response needed, 2 calls
ResponseGroundedness(llm, name, max_retries=5)                 .ascore(response, retrieved_contexts)        # NVIDIA-style, 2 calls
RunConfig(timeout=180, max_retries=10, max_wait=60, max_workers=16, exception_types=(Exception,), log_tenacity=False, seed=42)
```

Metric objects validate their components at construction: wrong LLM type -> `ValueError("Collections metrics only support modern InstructorLLM ...")`; wrong embeddings -> same for embeddings. Input validation at call time: empty `response`/`retrieved_contexts` raise `ValueError` (Faithfulness: "retrieved_contexts is missing...").

---

## 4. How each metric works (prompts, verdicts, formula, calls, cost)

All three use the same prompt template (`ragas/prompt/metrics/base_prompt.py`): `instruction` + "Please return the output in a JSON format that complies with the following schema ..." + the Pydantic output JSON-schema + few-shot examples + `Now perform the same with the following input\ninput: {json}\nOutput: `. Instructor's `Mode.JSON` adds one system message with the schema and sets `response_format={"type":"json_object"}`; parsing is by Pydantic; **one attempt only** (instructor retry count 1 -> a malformed answer raises `InstructorRetryException`, truncation raises `IncompleteOutputException`). Request actually sent (captured): `{'model','max_tokens':1024,'response_format':{'type':'json_object'},'temperature':0.01,'top_p':0.1,'messages':[system,user]}`. VERIFIED.

### 4.1 Faithfulness - 2 sequential LLM calls

1. **Statement extraction.** Instruction: "Given a question and an answer, analyze the complexity of each sentence in the answer. Break down each sentence into one or more fully understandable statements. Ensure that no pronouns are used in any statement." Input `{"question","answer"}` -> output `{"statements": [str, ...]}`.
2. **NLI verification, one call for all statements.** Instruction: "Your task is to judge the faithfulness of a series of statements based on a given context. For each statement you must return verdict as 1 if the statement can be directly inferred based on the context or 0 if the statement can not be directly inferred based on the context." Input `{"context": "\n".join(retrieved_contexts), "statements": [...]}` -> output `{"statements":[{"statement","reason","verdict":0|1}, ...]}`.
3. `score = (#verdict==1) / (#statements)`. No statements -> `NaN` (`MetricResult(value=nan)`).
- All contexts are **concatenated into one string** (joined with `\n`, no separators, no ids). Order is irrelevant. Nothing is truncated.
- Output size ~61 tokens per statement entry (measured with tiktoken `o200k_base`): 16+ statements overflow the default `max_tokens=1024` -> `IncompleteOutputException`. Set `max_tokens>=4096`.
- Judge sees only the contexts, not the question's truth. "Directly inferred" is strict: computed values (growth %, sums), paraphrased table cells and cross-page inferences are often marked 0 (INFERRED from the prompt wording; not measured).

### 4.2 AnswerRelevancy (aka ResponseRelevancy) - `strictness` (default 3) sequential LLM calls + 2 embedding calls

1. Loop `strictness` times (**sequentially, one call each; no `n=3` in the new API**): instruction "Generate a question for the given answer and identify if the answer is noncommittal. Give noncommittal as 1 if the answer is noncommittal (evasive, vague, or ambiguous) and 0 if the answer is substantive. Examples of noncommittal answers: "I don't know", "I'm not sure", "It depends"." Input `{"response"}` -> output `{"question": str, "noncommittal": 0|1}`. The **context is not used**; only the answer.
2. Embed the original question (`embed_text`) and the generated questions (one batched `embed_texts` call) with the embeddings model.
3. `score = mean(cosine(question_vec, generated_q_vec_i)) * (0 if ALL generated were noncommittal else 1)`. No questions generated -> 0.0. Cosine can be negative or >1 only by float noise; ragas does not clamp (the docs say "usually between 0 and 1").
- Needs an **embeddings model** (default `text-embedding-3-small`; docs examples use it; `ada-002` still accepted). Embedding cost is negligible (~100 tokens per answer).
- **Gotcha (INFERRED from code, big effect on cost):** the 3 calls use `temperature=0.01, top_p=0.1`, so the 3 generated questions are near-identical; the legacy implementation deliberately used temperature 0.3 when `n>1`. Either set `strictness=1` (1/3 of the cost, ~same value) or give this metric its own `llm_factory(..., temperature=0.3, top_p=1.0)` (what the example does). Reasoning models are forced to temperature 1.0 anyway.
- Refusals/abstentions ("the report does not state ...") are noncommittal -> **score 0**. Show the answer's `status` (answered / partial / not_found) next to the score instead of a red 0.00.

### 4.3 Context precision - 1 LLM call per context

`ContextPrecisionWithoutReference.ascore(user_input, response, retrieved_contexts)` (stock: **sequential** loop):
1. For each context `c_k` in list order: prompt `{"question","context","answer"=response}` -> `{"reason": str, "verdict": 0|1}`. Instruction: 'Given question, answer and context verify if the context was useful in arriving at the given answer. Give verdict as "1" if useful and "0" if not with json output.' (3 few-shot examples ~600 tokens, Einstein / T20 world cup / tallest mountain.)
2. Average precision: `AP = ( sum_k  precision@k * v_k ) / ( sum_k v_k + 1e-10 )`, with `precision@k = (useful among first k) / k`. Code: `numerator = sum((sum(v[:i+1])/(i+1))*v[i])`, `denominator = sum(v)+1e-10`. So a perfect score prints as `0.99999999995`; all-zero verdicts -> exactly 0.0 (never NaN).
- Same math for the "with reference" variant (the judge then compares the context to `reference` instead of `response`).
- The legacy `LLMContextPrecisionWithoutReference` is the same logic plus a trivial ensembler; `ContextUtilization` (collections) is literally a subclass with another `name`.

AP behaviour on 0/1 verdict lists (computed with the exact formula):

| verdicts (rank 1..K) | AP | comment |
|---|---|---|
| `[1]`, `[1,1,1]` | 1.0 | |
| `[1,0,0]`, `[1,1,0,0]`, `[1,0,0,0,0,0,0,0]` | **1.0** | junk AFTER the useful pages is not penalised |
| `[0,1,0]` | 0.5 | |
| `[0,0,1]` | 0.333 | useful page read last |
| `[1,0,1,0]` | 0.833 | |
| `[0,0,1,1]` | 0.417 | |
| `[0,0,0,0,0,0,0,1]` | 0.125 | |
| `[0,0,0]` | 0.0 | |

So it is "are the useful pages ranked first", not "what fraction of retrieved pages was useful". For an agentic PageIndex pipeline whose reading order is not a relevance ranking, the rank part is partly noise -> also display `useful_pages / N` computed from the per-context verdicts (the example returns them).

Other reference-free alternatives in 0.4.3 (optional, not requested): `ContextRelevance` (2 judge calls on the joined contexts, 0-1 relevance of the retrieval to the question, ignores the answer), `ResponseGroundedness` (2 calls, 0/1/2 rubric averaged; a cheaper faithfulness-like check).

### 4.4 Calls, tokens and cost per answer (N contexts)

| Metric | LLM calls | Embedding calls | Static prompt (tokens, my count, excl. instructor system msg) |
|---|---|---|---|
| Faithfulness | 2 (sequential) | 0 | statement-gen 347; NLI 685 (+ all contexts + statements) |
| AnswerRelevancy | `strictness` = 3 (sequential) | 2 | 429 per call (+ answer) |
| ContextPrecisionWithoutReference | N (sequential in stock class) | 0 | 882 per call (+ one context + answer) |
| **Total** | **5 + N** | **2** | instructor adds ~150-340 tokens of schema per call (07 measured ~1,025 / 560 / 1,036 incl. that) |

Estimated cost per answer (my token model with the real prompts; reasoning judges add hidden output tokens; OpenAI list prices fetched 2026-10-07 from developers.openai.com/api/docs/pricing - see 07 for the authoritative table):

| Scenario | LLM calls | Input tokens | gpt-4o-mini | **gpt-4.1-mini** | gpt-5-mini* | gpt-4.1 |
|---|---|---|---|---|---|---|
| 3 ctx x 600 tok (quotes/snippets) | 8 | 10.6k | $0.002 | $0.005 | $0.004 | $0.027 |
| **5 ctx x 1,500 tok (pages)** | 10 | 24.3k | $0.004 | **$0.011** | $0.008 | $0.055 |
| 8 ctx x 2,500 tok (big pages) | 13 | 52.8k | $0.009 | $0.023 | $0.015 | $0.113 |
| 10 ctx x 3,500 tok (nodes) | 15 | 85.2k | $0.013 | $0.036 | $0.023 | $0.179 |

\* reasoning judge: hidden reasoning tokens not included - add roughly 250-700 output tokens per call (07's assumption). Embeddings: ~100 tokens x $0.02/M = $0.000002. 07's own scenarios give $0.008-$0.054 (likely $0.018) for `gpt-4.1-mini`; same order of magnitude.
Latency (INFERRED, non-reasoning judge ~1-3 s/call): faithfulness chain ~4-8 s; relevancy ~4-6 s; stock context precision N x 1-3 s; with the three metrics under `asyncio.gather` the wall time is the longest chain. With `ParallelContextPrecision` the critical path becomes faithfulness (~5-8 s). Measured on the stub with 0.3 s/call: stock context precision 8 ctx = 2.48 s vs parallel 0.35 s, identical value.

### 4.5 What the new API does NOT give you

No per-context verdicts, no reasons (`MetricResult.reason` is `None` for these metrics), no token/cost accounting, no built-in concurrency limiter. The example adds per-context verdicts (subclass), a semaphore, and error capture. For cost: wrap the OpenAI client before `llm_factory` (07's `meter_client`) or read `usage` yourself.

---

## 5. No ground truth: which context-precision variant?

| Variant | Needs | Verdict |
|---|---|---|
| `ContextPrecisionWithReference` / `ContextPrecision` / legacy `LLMContextPrecisionWithReference` | `reference` (gold answer) | not usable (no ground truth) |
| `NonLLMContextPrecisionWithReference` | `reference_contexts` + `rapidfuzz` | not usable |
| `IDBasedContextPrecision` | `retrieved_context_ids` + `reference_context_ids` | not usable |
| **`ContextPrecisionWithoutReference`** (= `ContextUtilization`; legacy `LLMContextPrecisionWithoutReference`) | `user_input`, `response`, `retrieved_contexts` | **use this** |

Caveats of using the answer as the pseudo-reference (INFERRED, important for honest UI text): (1) a wrong answer built from the wrong pages still gets "useful" verdicts; (2) a correct but terse answer that used only one of five pages marks the other four "not useful" even if they were legitimate background; (3) it measures the retriever **given the answer**, so it will correlate with faithfulness. Treat it as a retrieval-focus indicator, not accuracy. The docs' own wording: the no-reference variant "uses the LLM to compare each chunk in `retrieved_contexts` with the `response`".

---

## 6. What to pass as `retrieved_contexts` for a PageIndex pipeline

**Recommendation (concrete):** one string per **page** that was put in front of the answering LLM, in the order PageIndex/the agent retrieved them (tree-search rank, pages ascending inside a node), formatted exactly like the block the answer model saw (header + text):

```
[Page 89 | Financial statements > Consolidated income statement]
<plain page text, tables as text rows>
```

- De-duplicate, drop blank pages, cap at ~8 contexts and ~8,000 chars (~2k tokens) per context (ragas never truncates; `clean_contexts()` in the example does it). This agrees with 03 section 5.6 (`<source>` blocks, rank order, strip citation markers from `response`, do not reorder to game the metric).
- **Faithfulness** is judged against the *concatenation* - it needs the full evidence the answer used, so include every page the answer model could cite; pages it never saw must not be included (they would excuse nothing and cost tokens).
- **Context precision** is judged *per context* - so granularity decides the metric.

(The bias column below is my reasoning from the verified prompts/formulas, INFERRED - not yet measured on real annual-report pages.)

| Choice for `retrieved_contexts` | Faithfulness | Context precision | Calls/cost | Verdict |
|---|---|---|---|---|
| **Per-page texts the model read** | fair: evidence complete, judge input 5-20k tokens | informative: each page gets its own verdict | 5+N, N<=8: $0.01-0.03 | **recommended** |
| Per-node text (whole sections, can be 10-40 pages) | judge prompt explodes (30-85k tokens), "lost in the middle", cost x3-8 | nearly always 1 ("somewhere in this node is something useful") -> inflated, uninformative | N small but each call huge | avoid |
| One concatenated blob (1 context) | same as per-page | trivial 0 or 1 (AP with K=1) | cheapest | avoid (metric collapses) |
| Only the cited quotes/snippets | **circular/biased**: claims are checked against the very sentences the model picked; evidence it paraphrased from non-quoted text is penalised | near 1 by construction (quotes are useful by definition) | cheapest | avoid; keep quotes for the UI highlight only |
| Many small chunks (e.g. 20+ paragraphs) | fine | more verdicts, more calls, rank noise; 5+N grows | 25+ calls | only if the answerer really received chunks |

Other effects: ragas gives no context ids, so keep your own `index -> page` map to show "page 91 judged not useful" in the UI (the example returns `context_verdicts` with `index`). Scanned/OCR'd or table-heavy pages produce noisy text, which lowers faithfulness for numeric claims; numbers computed by the answer model (growth %, sums) will often be 0 on faithfulness even when right (the answer prompt in 03 asks it to show formula and cite operands - the operand statements pass, the derived one may not).
**Score the answer prose, not the markers**: strip `[p. 45]`, `[1]`, "Sources" footers from `response` before scoring (`strip_citations()` in the example; unit-tested: `(2024)`, `(2023: 3,900)` are preserved).

---

## 7. OpenAI integration gotchas (all verified offline against the real wire format)

### 7.1 How ragas talks to OpenAI
`llm_factory` -> `instructor.from_openai(client, mode=Mode.JSON)` -> **Chat Completions** with `response_format={"type":"json_object"}` (no function calling, no `json_schema` strict mode; ragas chose JSON mode because tool calling mangled `Dict` fields, issue #2490). Embeddings: `client.embeddings.create(input=..., model=...)`. Responses API is not used. No `n` parameter in the new API (the 3 relevancy questions are 3 requests).

### 7.2 Request parameters per judge model id (captured from the HTTP body sent by ragas 0.4.3)

| `llm_factory(model, client=c, ...)` | Request body params | Note |
|---|---|---|
| `gpt-4o-mini`, `gpt-4.1`, `gpt-4.1-mini` | `max_tokens=1024, temperature=0.01, top_p=0.1` | fine for classic models. **Raise `max_tokens`** |
| `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-6-luna` (any `gpt-<int 5..19>[-suffix]`), `o1/o3/o4-mini` | `max_completion_tokens=1024, temperature=1.0`, no `top_p` | auto-adapted. 1024 includes hidden reasoning tokens -> **pass `max_tokens=4096+`** (becomes `max_completion_tokens`). Forced `temperature=1.0` ignores your value. Whether OpenAI accepts it with effort != none: UNVERIFIED (07 §7) |
| `gpt-5.1`, `gpt-5.2`, `gpt-5.4-mini`, `gpt-5.6-luna`, `gpt-6.1-sol` (**dotted version**) | `max_tokens=1024, temperature=0.01, top_p=0.1` (treated as classic!) | **bug**: ragas' `is_reasoning_model` does `int("5.4")` -> ValueError -> False. Reasoning models reject `max_tokens` (HTTP 400, UNVERIFIED live). Fix used by the example (`make_judge_llm`): after `llm_factory`, edit `llm.model_args` - move `max_tokens` -> `max_completion_tokens`, drop `temperature`/`top_p`; pass `reasoning_effort="low"` etc. as a kwarg |
| any model + `reasoning_effort="low"` kwarg | body gets `reasoning_effort: "low"` | extra kwargs pass straight through |
| `openai/gpt-5-mini` (provider prefix) | treated as classic | do not prefix |

The OpenAI model landscape on 2026-10-07 (per 07, verified there against developers.openai.com): `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-luna`, `gpt-5.6-sol/terra/luna`, `gpt-5.5`, `gpt-5.4(-mini/-nano)`, `gpt-5.2`, `gpt-5.1`, `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-4.1(-mini/-nano)`, `gpt-4o(-mini)` all still priced. I saw the same in my own fetch of the pricing page (gpt-4.1-mini $0.40/$1.60 per M; gpt-4o-mini $0.15/$0.60; gpt-5-mini $0.25/$2.00; gpt-5-nano $0.05/$0.40; gpt-4.1 $2/$8; text-embedding-3-small $0.02).

### 7.3 Judge model choice (recommendation)
- **Default `gpt-4.1-mini`** (non-reasoning, deterministic-ish at temp 0.01/top_p 0.1, 1M context, ~$0.011/answer for 5 pages), fallback `gpt-4o-mini` (3x cheaper, weaker). Different model from the answerer (avoids self-preference; the answerer is a GPT-5.6/6 class model per 07).
- A reasoning judge (`gpt-5-mini`, `gpt-6-luna` effort none/low) is possible but: non-deterministic (temperature forced to 1), slower (4-10 s/call), hidden tokens, and the param-mapping pitfalls above. Not worth it for yes/no verdicts.
- Same judge + same embedding model for every run you want to compare; store the model ids with each score (the example returns them).
- Judge outputs are not bitwise reproducible even at temperature 0.01 (OpenAI has no guaranteed determinism); use `llm_factory(..., cache=DiskCacheBackend())` so re-scoring the same answer returns identical results at zero cost (VERIFIED: 2nd identical `Faithfulness.ascore` -> 0 HTTP calls, same value) and persist scores per message.

### 7.4 Truncation, NaN, errors (VERIFIED with injected faults)
| Situation | What happens |
|---|---|
| judge output hits `max_tokens` (finish_reason=length) | `instructor ... IncompleteOutputException: The output is incomplete due to a max_tokens length limit.` -> that metric raises (with instructor 1.3.2 it silently re-asks once instead) |
| HTTP 429/5xx | openai SDK retries first (`AsyncOpenAI(max_retries=N)`, default 2; stub test: 1x 429 + `max_retries=3` -> transparent success, 3 HTTP calls); then raises `InstructorRetryException` wrapping the API error |
| Faithfulness: answer yields no statements / NLI returns none | `MetricResult(value=nan)` |
| AnswerRelevancy: all generated questions flagged noncommittal, or no question generated at all | 0.0 (the legacy class returned NaN for the "no question" case) |
| ContextPrecision: all verdicts 0 / all verdicts 1 | 0.0 / 0.99999999995 (the `1e-10` epsilon) |
| empty `response` / empty `retrieved_contexts` list | `ValueError` before any LLM call; **a list with an empty string is NOT caught** (still calls the LLM) -> sanitise |
| legacy `evaluate()` | swallows every exception and reports `NaN` unless `raise_exceptions=True` (and I saw `OpenAIConnectionError` NaNs on a 2nd/3rd `evaluate()` in the same process, INFERRED cause: cached async httpx client bound to a closed loop) |
`json.dumps(float('nan'))` emits invalid JSON `NaN` (Starlette's `JSONResponse` raises) -> convert NaN to `None` (`_num()` in the example; asserted with `json.dumps(allow_nan=False)`).

### 7.5 Rate limits, timeouts, concurrency
- Put timeouts/retries on the client: `AsyncOpenAI(timeout=60, max_retries=3)` (SDK default timeout 600 s, retries 2 - per ragas docs). `RunConfig` is ignored by the new metrics.
- ragas has no concurrency limiter in the new API: wrap calls in a process-wide `asyncio.Semaphore(8)` (07 suggests the same) and `asyncio.wait_for` per metric. 10 concurrent scorings with semaphore=4 completed cleanly in the test.
- Tier limits (07): Build tier 5,000 RPM and 1-2M TPM for these models; one RAGAS pass is ~25-50k tokens, so the app is nowhere near them.
- `instructor`'s own retries: 1 attempt. Do not rely on it.

### 7.6 Cost control checklist
Cap contexts (<=8) and chars (<=8k each); `strictness=3` only with a diversified-temperature LLM (else 1); parallel context precision; disk cache; score asynchronously after streaming the answer; skip scoring when `status=="not_found"`.

---

## 8. Async, event loops, threads, Windows - VERIFIED patterns (stub server, Python 3.13.3 Windows, Proactor loop)

| Pattern | Result |
|---|---|
| `await metric.ascore(...)` inside the running loop (FastAPI/uvicorn endpoint, notebook) | works; 3 metrics under `asyncio.gather` and 10 concurrent scorings fine; metric objects hold no per-call state, safe to share across concurrent tasks on one loop |
| `metric.score(...)` (sync wrapper = `asyncio.run(self.ascore())`) called **from inside a running loop** | `RuntimeError: Cannot call sync score() from an async context. Use ascore() instead.` |
| `metric.score(...)` called repeatedly from sync code with ONE `AsyncOpenAI` client created outside | **fails on every 2nd call**: `[1.0, 'InstructorRetryException', 1.0, 'InstructorRetryException', ...]` (pooled connection belongs to the previous, closed loop; "Connection error"). Do not do this |
| fresh `AsyncOpenAI` + metrics created inside each `asyncio.run(main())` and `await client.close()` | works 5/5 |
| one persistent loop in the main thread, `loop.run_until_complete(...)` x5 with a shared client | works 5/5 |
| **dedicated background loop thread** + `asyncio.run_coroutine_threadsafe` (the example's `SyncScorer`), called 5x from main thread and from 8 worker threads at once | works; this is the pattern for scripts, Streamlit, or FastAPI `def` (threadpool) endpoints |
| `llm.generate()` / `embeddings.embed_text()` (sync API on an async client) inside a running loop | ragas spawns a helper thread with a new loop (`_run_async_in_current_loop`; code reading, not run) - the client then crosses loops, so avoid |
| `nest_asyncio` | **not needed** for the collections API (never imported by it; verified by the tests above). Only legacy `single_turn_score` / `evaluate()` call `apply_nest_asyncio()` when a loop is already running (it skips uvloop and then raises `RuntimeError`). uvicorn on Windows runs the standard asyncio Proactor loop (uvloop is not available on Windows) |
| FastAPI later | create `RagasScorer` in the app `lifespan` (same loop as requests), `await scorer.score(...)` in an `async def` endpoint or `BackgroundTasks`; shut down with `await scorer.aclose()`. Wrap the two ragas objects the example builds; no other global state |

**Legacy-path hazard (VERIFIED, one more reason to avoid it):** `LangchainLLMWrapper.agenerate_text` mutates `n` and `temperature` on the shared `ChatOpenAI` object. With `evaluate()` running metrics concurrently the relevancy request went out with `n=1` instead of 3 ("LLM returned 1 generations instead of requested 3. Proceeding with 1 generations."), silently computing relevancy on one question. The collections API has no such shared mutable state.

---

## 9. Telemetry opt-out - VERIFIED (`ragas/_analytics.py`)
- `RAGAS_DO_NOT_TRACK` is read via `os.environ.get(...).lower() == "true"` and cached with `lru_cache`. **Only the value `true` works** (`1`/`yes` do not). Set it before the first ragas call (in the example it is the first line, before imports; an `AnalyticsBatcher` daemon thread starts at import).
- Runtime proof (I replaced `requests.post` with a recorder so nothing left the machine; one `llm_factory` + one `Faithfulness.ascore` = 2 LLM calls): `RAGAS_DO_NOT_TRACK=true` -> **0** POST attempts; `RAGAS_DO_NOT_TRACK=1` -> **3** POSTs to `https://t.explodinggradients.com`; unset -> 3. `nest_asyncio` is not imported on that path. (`ragas-smoke/dnt_probe.py`)
- When not disabled, `track()` does a **synchronous** `requests.post("https://t.explodinggradients.com", timeout=1)` inside every `llm.agenerate` / embedding call and batched evaluation events -> in an async server this can block the event loop up to 1 s per call when the endpoint is slow/blocked. Payloads: anonymous user id, ragas version, provider/model names, request counts, metric names.
- Even with opt-out, creating an event object writes a local id file (`%LOCALAPPDATA%\ragas\ragas\uuid.json`; created by my offline runs). Nothing is sent. Harmless; delete if you care.

---

## 10. The minimal script and what I ran

File: `ragas_minimal_example.py` (349 lines). Public pieces: `ScorerConfig` (env-driven: `RAGAS_JUDGE_MODEL=gpt-4.1-mini`, `RAGAS_EMBEDDING_MODEL=text-embedding-3-small`, `RAGAS_JUDGE_MAX_TOKENS=4096`, `RAGAS_JUDGE_REASONING_EFFORT`, `OPENAI_BASE_URL`), `RagasScorer.score(question, answer, contexts) -> dict`, `SyncScorer`, `ParallelContextPrecision`, `strip_citations`, `clean_contexts`, `make_judge_llm`, `TOOLTIPS`. Returned dict:

```json
{"faithfulness": 0.8333, "answer_relevancy": 0.8712, "context_precision": 0.9167,
 "context_verdicts": [{"index": 0, "verdict": 1, "reason": "..."}, ...],
 "errors": {}, "n_contexts_scored": 3, "n_contexts_input": 3,
 "judge_model": "gpt-4.1-mini", "embedding_model": "text-embedding-3-small", "ragas_version": "0.4.3", "latency_s": 6.4}
```
(example numbers; a failed metric is `null` plus an entry in `errors`.)

Real run: `set OPENAI_API_KEY=... && python ragas_minimal_example.py` (not executed - no key, by design). Offline: `python ragas_minimal_example.py --offline [--sync]`.

### What I actually ran (all offline, $0)
1. Clean venv `ragas-venv3` (`pip install "ragas==0.4.3" "langchain-community==0.4.1" "instructor>=1.15" "openai>=2"`; `pip check`: no broken requirements; resolved ragas 0.4.3, instructor 1.17.0, openai 3.3.0, langchain-openai 1.6.7, langchain-community 0.4.1, langchain-core 1.6.7, pydantic 2.13.5, numpy 2.5.3, tiktoken 0.14.0).
2. `python ragas_minimal_example.py --offline` in `ragas-venv3`, exit 0:
```
[offline] stub OpenAI server at http://127.0.0.1:51093/v1 (scores are meaningless, only the wiring is tested)
{ "faithfulness": 1.0, "answer_relevancy": 0.4518, "context_precision": 1.0,
  "context_verdicts": [{"index":0,"verdict":1,...},{"index":1,"verdict":1,...},{"index":2,"verdict":0,...}],
  "errors": {}, "n_contexts_scored": 3, "n_contexts_input": 3, "judge_model": "gpt-4.1-mini",
  "embedding_model": "text-embedding-3-small", "ragas_version": "0.4.3", "latency_s": 8.9 }
```
   (latency inflated by a heavily loaded machine; the stub answers instantly.) `--offline --sync` (two consecutive sync calls through `SyncScorer`) also OK.
3. `python ragas_example_offline_test.py` -> **18 passed, 0 failed** on: `ragas-venv3` (openai 3.3.0, instructor 1.17.0), `ragas-venv2` (same, then openai 2.54.0). On the old resolver set (`ragas-venv`: instructor 1.3.2, openai 1.109.1, langchain-openai 1.1.9) 16/18 (the two fault-injection tests differ because old instructor retries silently - benign). The 18 checks: all three scores present; **8 chat + 2 embedding calls for N=3 (=5+N, 2)**; per-context verdicts; citation markers stripped before judging; request body (`gpt-4.1-mini`: `max_tokens=4096`, `response_format=json_object`); relevancy calls at temperature 0.3; one injected 429 fails exactly one metric and the other two still score; truncated JSON -> `IncompleteOutputException` captured; blank/duplicate contexts dropped and `max_contexts` enforced; empty contexts -> clear error, no LLM call; NaN/None/clamp helper and `json.dumps(allow_nan=False)`; request params for `gpt-4.1-mini`, `gpt-5-mini` (`max_completion_tokens`, temperature 1.0), `gpt-5.4-mini` and `gpt-6.1-sol` (shim: `max_completion_tokens`, no temperature/top_p); 10 concurrent scorings with semaphore 4; 5 consecutive `SyncScorer` calls.
4. Throw-away probes (code in session scratchpad `ragas-smoke/`): `smoke1` (collections call path + call counts 2 / 3+2 emb / N), `smoke2` (edge cases 3a-3f), `smoke3` (event-loop patterns A-D), `smoke4/5` (legacy path: DeprecationWarnings, `evaluate()` TypeError on collections metrics, `n` race), `smoke6` (reasoning-model request params), `smoke7` (DiskCacheBackend + parallel context precision timing), `params_map.py` (22 model ids), `cost_est.py` (token/cost model).

**Not verified (needs a live key):** actual judge verdict quality, real score ranges on annual-report text, whether OpenAI accepts the exact parameter combinations for `gpt-5.x/6.x` judges, real latencies and token counts per call.

---

## 11. Interpreting scores - text for the UI

Show 0-100% or 0.00-1.00 with a "?" popover; label the panel "Answer quality (estimated by an AI judge)". Per metric:

| Metric | Short label | Tooltip |
|---|---|---|
| faithfulness | Grounded in sources | Share of the claims in the answer that an LLM judge could infer from the retrieved pages. 1.0 = every claim is supported by the retrieved text. It does not check claims against reality, and it does not know about text the retriever missed. |
| answer_relevancy | On-topic | How closely the question can be re-created from the answer alone (embedding similarity between your question and questions an LLM writes from the answer). It ignores correctness. Evasive or "not found in the report" answers score 0; long multi-topic answers score lower. |
| context_precision | Retrieved pages were useful | Rank-weighted share of the retrieved pages that an LLM judge found useful for producing this answer; 1.0 = useful pages came first. It compares pages to the generated answer, not to a gold answer, so a wrong answer can still score high. Show `useful_pages/N` beside it. |
| general footnote | | LLM-judged estimates, not ground truth. They depend on the judge model (`gpt-4.1-mini`) and can shift by a few points between runs. Use them to compare answers and spot problems, not as accuracy percentages. |

Caveats to keep in mind (INFERRED unless stated):
- Scores are **relative**: typical well-supported answers land ~0.8-1.0 faithfulness, 0.6-0.95 relevancy (docs example 0.9165 for a one-line answer), 0.7-1.0 precision; do not threshold at fixed numbers until you have a UAT sample (e.g. 20-40 labelled questions) to calibrate - 07 suggests exactly this A/B.
- Judge dependence and noise: changing judge model or embedding model changes the scale (cosine values differ by embedding model); never compare numbers across judge/embedding settings. Run-to-run noise +-0.03-0.1 is plausible.
- Answer relevancy is **not** bounded away from penalising good answers: answers that add context beyond the question lower it; a correct refusal is 0. Faithfulness penalises correct arithmetic that is not literally in the text. Context precision is flattered by short answers drawn from the first page.
- Display `null`/error as "n/a (judge failed)" with the error, not as 0. Display abstentions as "n/a (no answer)".
- Because RAGAS is an LLM-as-judge on the same documents, it can share blind spots with the answerer (e.g. both misread a table). Keep citations clickable for human verification.

---

## 12. Open questions / risks

1. RAGAS is effectively unmaintained (last commit 2026-02-24) and its import depends on a removed langchain module: pin versions, keep `ragas_example_offline_test.py` in CI, and be ready to vendor the three metrics (prompts + ~60 lines of maths in `collections/*/metric.py` and `util.py`; Apache-2.0) to drop the 593 MB dependency tree if needed. `ParallelContextPrecision` already relies on 0.4.3 internals (`prompt`, `llm`, `_calculate_average_precision`).
2. Reasoning judges (gpt-5.x/6.x) and the exact parameter acceptance - needs a live key; the shim assumes "no temperature/top_p, `max_completion_tokens`".
3. The new `AnswerRelevancy` uses sequential, near-greedy generations - verify with a live key that question diversity at temperature 0.3 actually changes scores meaningfully vs `strictness=1`.
4. Faithfulness NLI verdicts on numeric/table content of annual reports are unmeasured; plan a small human-labelled UAT set.
5. `contexts` order for PageIndex agent runs is reading order, not a relevance ranking: AP's rank weighting is partly arbitrary there.
6. Telemetry file `uuid.json` is created under `%LOCALAPPDATA%` even with opt-out (no network).

---

## 13. Sources

- PyPI: https://pypi.org/project/ragas/ (JSON API `https://pypi.org/pypi/ragas/json`); langchain-community versions `https://pypi.org/pypi/langchain-community/json`; openai `https://pypi.org/pypi/openai/json`; instructor 1.17.0 wheel metadata.
- Source (read in the installed 0.4.3 wheel and tag `v0.4.3` clone): `ragas/metrics/collections/{base.py, faithfulness, answer_relevancy, context_precision, context_relevance, response_groundedness}`, `ragas/metrics/_faithfulness.py`, `_answer_relevance.py`, `_context_precision.py`, `metrics/__init__.py`, `llms/base.py` (`llm_factory`, `InstructorLLM._map_openai_params`, `LangchainLLMWrapper`), `llms/adapters/`, `embeddings/{__init__,base,openai_provider}.py`, `evaluation.py`, `run_config.py`, `executor.py`, `async_utils.py`, `_analytics.py`, `dataset_schema.py`, `experiment.py`.
- Docs (live + repo `docs/`): https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/ , `.../context_precision/` , `.../answer_relevance/` , `.../available_metrics/` ; repo `docs/howtos/customizations/run_config.md`, `docs/howtos/migrations/migrate_from_v03_to_v04.md`.
- GitHub: https://github.com/vibrantlabsai/ragas (releases API, issues #2995, #2741, #2490; PRs #2997, #3017, #2979, #2991; commits).
- OpenAI: https://developers.openai.com/api/docs/pricing , https://developers.openai.com/api/docs/models (+ `/models/gpt-4.1-mini`, `/models/gpt-5.4-mini`), guides `reasoning`, `structured-outputs` (fetched through a summarising tool - treat specifics as indicative; see 07 for the authoritative model/cost verification). The old `platform.openai.com/docs/*` URLs 301-redirect to developers.openai.com.
- PageIndex requirements (for dependency coexistence): https://raw.githubusercontent.com/VectifyAI/PageIndex/main/requirements.txt (`litellm==1.97.0`, `openai>=1.70.0`, `openai-agents>=0.18.1`, `mcp>=1.19.0,<3`, ...).
- Sibling notes: `07-openai-models-and-costs.md` (section 7-8), `03-pageindex-retrieval-patterns-and-prompts.md` (section 5.3, 5.6).
