# 07 - OpenAI models, API surfaces, costs and ops (as of 2026-10-07)

Researcher: "OpenAI models and costs". Scope: which OpenAI models and API surface to use for (a) PageIndex tree building,
(b) retrieval reasoning (tree search), (c) final answer with citations, (d) RAGAS judge LLM, (e) RAGAS embeddings; plus
cost, latency, usage tracking, retries, concurrency, streaming, key handling, startup smoke test.

**Legend.** VERIFIED = I read it in a primary source this session (official docs `.md` pages, GitHub source, PyPI wheels) or ran it
(offline, mock transport, no key, $0). INFERRED = reasoned from verified facts; not measured. UNVERIFIED = needs a live key to confirm.

**Warning about prior knowledge.** The model landscape on 2026-10-07 is very different from 2025. Everything below was re-read from
`developers.openai.com` (the old `platform.openai.com/docs/*` URLs 301-redirect there; every docs page has a `.md` twin, e.g.
`https://developers.openai.com/api/docs/models/gpt-6-luna.md`). Raw copies I used are in
`C:\Users\ayush\AppData\Local\Temp\claude\C--Users-ayush-Annual-Report-Answering\536cb16d-2d66-4e30-9660-8cbaf6c92128\scratchpad\oai-raw\`.

---------------------------------------------------------------------------------------------------------------------------------

## 1. Bottom line

### 1.1 Recommended model IDs (all configurable by env var)

| Role | Env var | Default (recommended) | Fallback chain (first one that passes `models.retrieve`) | Why |
|---|---|---|---|---|
| (a) PageIndex tree building (index/summaries/expand) | `PI_INDEX_MODEL` | `gpt-5.6-luna` | `gpt-6-luna` -> `gpt-5.4-mini` -> `gpt-4.1-mini` -> `gpt-4o-mini` | It is PageIndex's own `DEFAULT_INDEX_MODEL` and the README/docs example (VERIFIED). README: "a basic model is sufficient" for indexing. ~$0.26 per 308-page report. `gpt-6-luna` is ~2x cheaper but newer than anything PageIndex has benchmarked and is not in PageIndex's pinned litellm 1.97.0 map (see 6.3). |
| (b)+(c) Retrieval reasoning + answer with citations (PageIndex agent loop does both) | `PI_CHAT_MODEL`, `PI_CHAT_REASONING_EFFORT`, `PI_CHAT_PROTOCOL` | `gpt-5.6-sol`, `medium`, `responses` | `gpt-6.1-sol` -> `gpt-5.6-terra` -> `gpt-5.5` -> `gpt-5.4` -> `gpt-4.1` (non-reasoning: leave effort empty) | PageIndex's `DEFAULT_CHAT_MODEL` and docs example (VERIFIED). Its own benchmark: terra/sol at medium = 98.4%/100% on 62 lookup questions. **Must use the Responses protocol** (tools + reasoning is unsupported / HTTP 400 on Chat Completions for these models, see 4.2). |
| (d) RAGAS judge LLM | `RAGAS_JUDGE_MODEL`, `RAGAS_JUDGE_REASONING_EFFORT`, `RAGAS_JUDGE_MAX_TOKENS` | `gpt-4.1-mini` (non-reasoning), effort unset, `4096` | `gpt-4o-mini` -> `gpt-4.1` -> `gpt-6-luna` with effort `none` -> any GPT-5.x/6.x reasoning model **through the `adapt_for_reasoning` shim** | RAGAS 0.4.3 `llm_factory` only works out of the box with non-reasoning models (VERIFIED by capturing its wire requests, section 7). Different model family from the answerer = less self-preference bias. $0.008-$0.054 per scored answer. 1M context. |
| (e) RAGAS embeddings (ResponseRelevancy/AnswerRelevancy) | `RAGAS_EMBEDDING_MODEL` | `text-embedding-3-small` | `text-embedding-3-large` -> `text-embedding-ada-002` | $0.02 per 1M tokens, about 100 tokens per answer, so cost is ~$0.000002. Pin one model so cosine scores are comparable across runs. |

Cost summary for one 308-page annual report (details in section 8):
- Index once: **$0.07 - $0.32** (likely ~$0.26 with `gpt-5.6-luna`; ~$0.12 with `gpt-6-luna`), 1.5-3 min.
- One question (retrieval+answer), docs-default `gpt-5.6-sol`: **$0.16 - $0.88**, likely ~$0.35. With `gpt-5.6-terra` ~$0.18; `gpt-6.1-sol` ~$0.18; `gpt-6-luna` ~$0.009. (Cost is dominated by re-reading the PageIndex tree, which grows with page count.)
- RAGAS scores for that answer with `gpt-4.1-mini`: **$0.008 - $0.054**, likely ~$0.018 (+ ~$0.000002 embeddings).
- 100-question UAT on the docs-default preset: roughly $37 (range $17-$94); Balanced/Newest presets ~$20; Budget (`gpt-6-luna`) ~$2.

### 1.2 Eleven things that will bite if ignored

1. **Lineup changed completely.** Current flagship family is GPT-6 (`gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-sol`, `gpt-6-luna`; Sep 2026) on top of GPT-5.6 (`gpt-5.6-sol/terra/luna`; dated snapshots in LiteLLM's Azure entries suggest ~Jul 2026, INFERRED). `gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4.1-mini` still exist and are not scheduled for retirement (only `gpt-4o-2024-05-13` and `gpt-4.1-nano` retire 2026-10-23). `gpt-4o-2024-11-20` is still listed as a `gpt-4o` snapshot.
2. **Chat Completions + tools + reasoning does not work** on `gpt-6-astra`, `gpt-6.1-sol` (no function calling at all on Chat Completions) and `gpt-6-sol`/`gpt-6-luna` (function calling only with `reasoning_effort="none"`). PageIndex's default `chat()` lane = LiteLLM chat.completions and can fail with HTTP 400 `Function tools with reasoning_effort are not supported for gpt-5.6-sol in /v1/chat/completions` (string is in PageIndex's own tests; PageIndex wraps it with advice, and newer LiteLLM may bridge to Responses automatically, but do not depend on that). Use `client.chat(..., protocol="responses")`, the lane PageIndex's own benchmark used.
3. **`pip install ragas` then `import ragas` FAILS** with fresh dependencies on Python 3.13: `ModuleNotFoundError: No module named 'langchain_community.chat_models.vertexai'` (ragas 0.4.3 + langchain-community 0.4.2). Fix: `pip install "langchain-community==0.4.1"` (VERIFIED: import then works).
4. **RAGAS 0.4.3 `llm_factory` mis-detects reasoning models** by name: only integer-only versions (`gpt-5`, `gpt-6`, `o3`...) are treated as reasoning. For `gpt-6.1-sol`, `gpt-5.6-*`, `gpt-5.5`, `gpt-5.4-*` it sends `max_tokens` + `temperature=0.01` + `top_p=0.1` (OpenAI says reasoning models reject these). For `gpt-6-luna/sol/astra` it sends `temperature=1.0` and `max_completion_tokens=1024` (1024 includes reasoning tokens, so structured output can be truncated). Use a non-reasoning judge, or the shim in `07-code/openai_support.py`.
5. **SDK/dependency pins collide:** `instructor 1.17.0` (needed by RAGAS) pins `jiter<0.15`; `openai>=3.x` needs `jiter>=0.16`. A plain `pip install pageindex ragas ...` therefore resolves to **openai 2.54.0 + openai-agents 0.20.0 + litellm 1.104.0** (VERIFIED by `pip install --dry-run` and then by a real install; `pip check` clean), not openai 3.26.0. Forcing `openai==3.26.0` next to instructor works at runtime (my offline end-to-end test ran that way) but `pip check` complains.
6. **PageIndex's repo pins `litellm==1.97.0` (2026-08-16)**, which predates GPT-6: no `gpt-6-*` entries in its cost/capability map. PyPI `pageindex 0.2.21` only requires `litellm>=1.97.0`, so pip picks 1.104.0 (has `gpt-6-astra/sol/luna`, still lacks `gpt-6.1-sol`).
7. **PageIndex retries every non-400/401/403/404 error 10 times with a flat 1 s sleep** (`llm_completion`/`llm_acompletion`, `max_retries=10`). A *billing* 429 (`credit_balance_exhausted`, `*_spend_limit_exceeded`, `organization_usage_limit_exceeded`) is NOT fixable by retrying but will be retried 10x per call. Pre-flight credits/limits and cap spend in the dashboard.
8. **Retiring soon - do not build on:** `gpt-5-2025-08-07`, `gpt-5-mini-2025-08-07`, `gpt-5-nano-2025-08-07`, `o3-2025-04-16`, `o3-pro` (Dec 11 2026); `o1`, `o1-pro`, `o3-mini`, `o4-mini`, `gpt-4-turbo`, `gpt-4.1-nano`, `gpt-3.5-turbo*` (Oct 23 2026); `gpt-5.1`, `gpt-5.4-nano`, `gpt-5.3-codex` (Apr 1 2027). The Assistants API shut down Aug 26 2026. Evals platform read-only Oct 31 2026.
9. **New usage tiers (Oct 6 2026):** Free / Build ($5 total credit purchases) / Launch ($100) / Grow ($500). The GPT-6/5.6 pages list rate limits only for Build and up; Free-tier access to these models is UNVERIFIED. Plan to buy >= $5 of credit.
10. **No `seed` on the Responses API** and no temperature control on reasoning models: results are not reproducible. Cache judge outputs (RAGAS `DiskCacheBackend`) and store per-answer scores.
11. **Windows MAX_PATH breaks `pip install litellm` (hence PageIndex) in a deeply nested venv.** `LongPathsEnabled` is `0` on this machine (registry, read-only check). The longest file in the litellm 1.104.0 wheel is 128 characters (`litellm/proxy/guardrails/guardrail_hooks/litellm_content_filter/guardrail_benchmarks/evals/block_disability_discrimination.jsonl`), so the **venv root must be shorter than ~110 characters**. I hit this for real: `pip install pageindex ragas ...` into a venv 138 characters deep failed with `OSError: [Errno 2] No such file or directory: '...\litellm\proxy\guardrails\guardrail_hooks\litellm_content_filter\categories\bias_sexual_orientation.yaml'`. A venv at `C:\Users\ayush\Annual Report Answering\.venv` (44 chars) is fine (191 chars for the longest litellm file). Never create the venv under a long path such as the Claude scratchpad.

---------------------------------------------------------------------------------------------------------------------------------

## 2. Current lineup (VERIFIED from `https://developers.openai.com/api/docs/models/<id>.md` and `/api/docs/pricing.md`, 2026-10-07)

Prices are USD per 1M tokens, standard processing, prompts <= 272K input tokens. Prompts > 272K are billed 2x input/cache and 1.5x output
for the whole request (GPT-6/5.6 pages); Batch and Flex = 50% of Standard; Fast mode = 2x. Cache writes (GPT-5.6 and later only) = 1.25x
input; cache reads = 0.10x input (0.05x for `gpt-6.1-sol`).

| Model ID | Notes | Context / max input / max output | Knowledge cutoff | Input | Cached in | Cache write | Output | `reasoning.effort` values (default) |
|---|---|---|---|---|---|---|---|---|
| `gpt-6-astra` | Flagship "most capable" | 1,050,000 / 922,000 / 128,000 | 2026-04-30 | 10.00 | 1.00 | 12.50 | 50.00 | low, medium, high, xhigh, max (no `none`; HTTP 400 if sent) |
| `gpt-6.1-sol` | "Near-Astra at lower cost"; released 2026-09-29 | same | 2026-04-30 | 2.00 | 0.10 | 2.50 | 10.00 | low, medium (default), high, xhigh, max (no `none`, no `minimal`) |
| `gpt-6-sol` | Released 2026-09-22 | same | 2026-04-20 | 2.00 | 0.20 | 2.50 | 10.00 | none, low, medium (default), high, xhigh, max |
| `gpt-6-luna` | "Most efficient"; released 2026-09-22 | same | 2026-05-18 | 0.10 | 0.01 | 0.125 | 0.50 | none, low, medium (default), high, xhigh, max |
| `gpt-5.6-sol` | PageIndex default chat model | same | 2026-02-16 | 4.00 | 0.40 | 5.00 | 20.00 | none, low, medium (default), ... max |
| `gpt-5.6-terra` | Balanced | same | 2026-02-16 | 2.00 | 0.20 | 2.50 | 12.00 | same |
| `gpt-5.6-luna` | PageIndex default index model | same | 2026-02-16 | 0.20 | 0.02 | 0.25 | 1.20 | same |
| `gpt-5.5` | | 1,050,000 / - / 128,000 | 2025-12-01 | 5.00 | 0.50 | - | 30.00 | none, low, medium (default), high, xhigh |
| `gpt-5.4-mini` | snapshot `gpt-5.4-mini-2026-03-17` | 400,000 / 272,000 / 128,000 | 2025-08-31 | 0.75 | 0.075 | - | 4.50 | none (default), low, medium, high, xhigh |
| `gpt-5.4-nano` | **deprecated, shutdown 2027-04-01** | 400,000 / 272,000 / 128,000 | 2025-08-31 | 0.20 | 0.02 | - | 1.25 | none (default) .. xhigh |
| `gpt-5-mini` | `gpt-5-mini-2025-08-07` **shutdown 2026-12-11** | 400,000 / 272,000 / 128,000 | 2024-05-31 | 0.25 | 0.025 | - | 2.00 | - |
| `gpt-4.1` | non-reasoning | 1,047,576 / - / 32,768 | 2024-06-01 | 2.00 | 0.50 | - | 8.00 | n/a |
| `gpt-4.1-mini` | non-reasoning; snapshot `gpt-4.1-mini-2025-04-14` | 1,047,576 / - / 32,768 | 2024-06-01 | 0.40 | 0.10 | - | 1.60 | n/a |
| `gpt-4o` | default snapshot `gpt-4o-2024-08-06`; also `-2024-11-20`, `-2024-05-13` (retires 2026-10-23) | 128,000 / - / 16,384 | 2023-10-01 | 2.50 | 1.25 | - | 10.00 | n/a |
| `gpt-4o-mini` | snapshot `gpt-4o-mini-2024-07-18` | 128,000 / - / 16,384 | 2023-10-01 | 0.15 | 0.075 | - | 0.60 | n/a |
| `text-embedding-3-small` | 1536-d | - | - | 0.02 | - | - | - | n/a |
| `text-embedding-3-large` | 3072-d | - | - | 0.13 | - | - | - | n/a |
| `text-embedding-ada-002` | older; no deprecation notice found | - | - | 0.10 | - | - | - | n/a |

Other facts:
- All GPT-6 / GPT-5.6 models: text+image input, text output, endpoints Chat Completions + Responses + Batch, features streaming / structured outputs / function calling / prompt caching (model pages).
- GPT-6 / GPT-5.6 model IDs have **no dated snapshots**: alias == snapshot (the "Snapshots" section lists only the alias). Log `response.model` and the request id for audit. `gpt-4.1-mini`, `gpt-4o-mini`, `gpt-5.4-mini` do have dated snapshots (pin them in production if you want stable judging).
- New in changelog: Decisions API (`POST /v1/decisions`, public beta, `gpt-6-luna` only, returns probabilities/choices/scores "about 10x faster than Responses") - interesting for judge-style verdicts but RAGAS cannot use it; ignore for now.
- Embeddings and `gpt-4.1*` have separate "Long Context" rate limits (e.g. `gpt-4.1-mini` Build: 500 RPM / 1M TPM); irrelevant for our prompt sizes.

### 2.1 Rate limits and usage tiers (VERIFIED, rate-limits guide + model pages)

| Tier | Qualification | Monthly usage limit | Astra / Sol (6, 6.1, 5.6) / Terra RPM / TPM | Luna RPM / TPM | `gpt-4.1-mini`, `gpt-4o-mini` RPM / TPM | `gpt-4.1`, `gpt-4o` RPM / TPM | embeddings RPM / TPM |
|---|---|---|---|---|---|---|---|
| Free | allowed geography | $100 | not listed (UNVERIFIED) | not listed | not listed | not listed | 100 / 40,000 (RPD 2,000) |
| Build | $5 total credit purchases | $500 | 5,000 / 1,000,000 | 5,000 / 2,000,000 | 5,000 / 2,000,000 | 5,000 / 450,000 | 5,000 / 1,000,000 |
| Launch | $100 | $5,000 | 10,000 / 4,000,000 | 10,000 / 10,000,000 | 10,000 / 10,000,000 | 10,000 / 2,000,000 | 10,000 / 5,000,000 |
| Grow | $500 | $200,000 | 15,000 / 40,000,000 | 30,000 / 180,000,000 | 30,000 / 150,000,000 | 10,000 / 30,000,000 | 10,000 / 10,000,000 |

Response headers worth reading for adaptive throttling: `x-ratelimit-remaining-requests`, `x-ratelimit-remaining-tokens`, `x-ratelimit-reset-*`,
`Retry-After` (on temporary 429/503). Ramp guidance: above ~1M TPM raise traffic by <= 50% per 15 minutes (`slow_down` 429). Not a concern at our scale.

---------------------------------------------------------------------------------------------------------------------------------

## 3. Deprecations and the specific questions asked

Source: `https://developers.openai.com/api/docs/deprecations.md` (VERIFIED). Notice period policy: GA models >= 6 months; specialised variants >= 3 months; `*preview*` models as little as 2 weeks.

| Shutdown | Models | Replacement named by OpenAI |
|---|---|---|
| 2026-08-26 (past) | Assistants API | Responses + Conversations API |
| 2026-09-24 (past) | Sora 2 / Videos API | none |
| 2026-10-23 (16 days away) | `gpt-3.5-turbo*`, `gpt-4-0613/gpt-4`, `gpt-4-1106-preview`, `gpt-4-turbo`, `gpt-4.1-nano`, **`gpt-4o-2024-05-13`**, `gpt-image-1`, `o1`, `o1-pro`, `o3-mini`, `o4-mini` (+ fine-tunes) | `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna` |
| 2026-10-31 / 2026-11-30 | Evals platform read-only / dashboard+API shut down; reusable prompts API; Agent Builder | Promptfoo / app code |
| 2026-12-11 | `gpt-5-2025-08-07`, `gpt-5-mini-2025-08-07`, `gpt-5-nano-2025-08-07`, `gpt-5-pro-2025-10-06`, `o3-2025-04-16`, `o3-pro-2025-06-10` | `gpt-5.6-sol` / `-terra` / `-luna` |
| 2027-01-06 / 01-20 / 02-26 | TTS-1 family; legacy realtime/audio; whisper-1 and 4o-transcribe family | realtime-2.1-mini / gpt-audio-1.5 / gpt-transcribe |
| 2027-04-01 | `gpt-5.1`, `gpt-5.4-nano`, `gpt-5.3-codex` | `gpt-6-sol` / `gpt-6-luna` |

Direct answers:
- **Is `gpt-4o-2024-11-20` still available?** Yes. It is listed as a snapshot on the `gpt-4o` model page and is not on the deprecations page. (`gpt-4o` alias default snapshot is `gpt-4o-2024-08-06`.) Only `gpt-4o-2024-05-13` retires (2026-10-23). `gpt-4o`/`gpt-4o-mini` have a 128K context, which is tight for a large PageIndex tree + pages: avoid for chat.
- **`gpt-4.1`?** Available (1,047,576 context, $2/$8); `gpt-4.1-mini` available; `gpt-4.1-nano` retires 2026-10-23. The `gpt-4.1` page itself says "we recommend starting with GPT-5 for complex tasks".
- **What replaced them?** For anything retiring, OpenAI names the GPT-5.6 family (`gpt-5.6-sol/terra/luna`); the newer GPT-6 family is the "start here" recommendation in the models guide ("use GPT-6 Astra ... GPT-6.1 Sol to balance ... GPT-6 Luna for cost-sensitive").
- **`models.retrieve` exposes `shutdown_date`** (null if none announced), so the app can warn at startup (section 9.5).

---------------------------------------------------------------------------------------------------------------------------------

## 4. API surfaces and parameter changes

### 4.1 Responses vs Chat Completions (VERIFIED, migrate-to-responses guide + SDK README)
- "While Chat Completions remains supported, Responses is recommended for all new projects." The openai-python README calls Chat Completions "supported indefinitely". Reasoning models "work better with the Responses API"; OpenAI cites 40-80% better cache utilization on Responses.
- Responses has no `n`, no `seed`, no `max_tokens`; it has `max_output_tokens`, `reasoning={"effort":..,"summary":..,"mode":..,"context":..}`, `text={"format":...}`, `store`, `previous_response_id`, `prompt_cache_key`, `prompt_cache_options`, `service_tier`, `safety_identifier`.
- Chat Completions keeps `max_completion_tokens` (and deprecated `max_tokens`), `reasoning_effort`, `response_format`, `n`, `seed` (Beta, best effort), `stream_options`.

### 4.2 Capability matrix that matters for us (VERIFIED: model pages + "Using GPT-6" guide + reasoning guide)

| Model | Responses + function tools + reasoning | Chat Completions + function tools | Chat Completions without tools | `none` effort |
|---|---|---|---|---|
| `gpt-6-astra` | yes | **no** | yes | **no** (400) |
| `gpt-6.1-sol` | yes | **no** | yes | **no** |
| `gpt-6-sol`, `gpt-6-luna` | yes | only with `reasoning_effort="none"` | yes | yes |
| `gpt-5.6-*` | yes | 400 `Function tools with reasoning_effort are not supported ... in /v1/chat/completions` (from PageIndex tests) unless effort none (INFERRED) | yes | yes |
| `gpt-4.1*`, `gpt-4o*` | yes | yes | yes | n/a (non-reasoning; do not send effort) |

=> PageIndex's agent (function tools: `get_document_structure`, `get_page_content`, ...) must run over Responses for every reasoning model.

### 4.3 Parameter changes
| Old habit | Now |
|---|---|
| `max_tokens` | Deprecated; "not compatible with o-series" (SDK docstring). Use `max_completion_tokens` (Chat) / `max_output_tokens` (Responses). Both include **reasoning tokens**. OpenAI recommends reserving >= 25,000 tokens for reasoning+output when experimenting; a too-small cap returns `status="incomplete"` / `finish_reason="length"` with possibly zero visible output but you still pay. |
| `temperature`, `top_p`, `top_logprobs`, `logprobs` | GPT-6 guide: "When reasoning effort is not `none`, remove `temperature`, `top_p`, and `top_logprobs`" (and `logprobs` on Chat, `message.output_text.logprobs` on Responses). Whether a value of exactly `1.0` is tolerated on GPT-6 is UNVERIFIED (RAGAS sends `temperature=1.0` for gpt-6-*). |
| `reasoning_effort` | Chat: `reasoning_effort`; Responses: `reasoning.effort`. Values `none/minimal/low/medium/high/xhigh/max`, model-dependent (table in 2). Defaults are model-dependent; GPT-6.1 Sol / Sol / Luna and GPT-5.6 default `medium`. Astra's default is not stated on the pages I read. `reasoning.mode` = `standard` (default) or `pro` (GPT-5.6/GPT-6) is independent. `configuration_update` input items change effort mid-conversation without breaking the cache. |
| `response_format` (JSON schema) | Chat: `response_format={"type":"json_schema","json_schema":{"name":..,"strict":True,"schema":..}}` or `client.chat.completions.parse(response_format=PydanticModel)`. Responses: `text={"format":{"type":"json_schema","name":..,"strict":true,"schema":..}}` or `client.responses.parse(text_format=PydanticModel)` -> `response.output_parsed`. Supported on `gpt-4o-mini`, `gpt-4o-2024-08-06` and all later models. JSON mode (`{"type":"json_object"}`) still works (RAGAS uses it). Root must be an object, all fields `required`, `additionalProperties:false`. I ran `responses.parse` offline against SDK 3.26.0: OK. |
| `seed` | Chat Completions only, "Beta", best effort. Not on Responses. |
| `n` | Chat Completions only. Responses returns one generation. |
| `prompt_cache_retention` | Deprecated for GPT-5.6+; use `prompt_cache_options={"ttl":"30m"}` (only value). Min cacheable prefix 1,024 tokens; cache lifetime >= 30 min after last write/use. |
| `service_tier` | `auto/default/flex/scale/priority/fast` (+ `ultrafast` on Astra). Flex = 50% price, slower, occasional unavailability: good for offline RAGAS batch re-scoring of GPT-6/5.6 judges (not for `gpt-4.1*`/`gpt-4o*`, which are not in the Flex table). |

### 4.4 Streaming (VERIFIED guide + SDK types)
- Responses: `client.responses.create(..., stream=True)` or the helper `with client.responses.stream(...) as s:` (exists in SDK 3.26.0). Typed events: `response.output_text.delta` (`.delta`), `response.reasoning_summary_text.delta` (needs `reasoning={"summary":"auto"}`; use this for a "thinking" UI), `response.function_call_arguments.delta`, `response.completed` (**carries `response.usage`**), `response.incomplete`, `response.failed`, `error`.
- Chat Completions: `stream=True, stream_options={"include_usage": True}`; usage only in the last chunk; no reasoning summaries.
- SDK does not retry a stream after bytes were delivered (README). Pre-stream HTTP errors (429/503) are retried normally.
- PageIndex streams for you: `client.chat(..., stream=True, show_process=...)` yields answer text (optionally woven with `[thinking]`, `[tool_call] name args`, `[tool_result]` sections; **on by default**, pass `show_process=False` for a bare answer); `protocol="responses"` + `stream=True` yields Responses events as dicts.

### 4.5 Retrying (VERIFIED, rate-limits + error-codes guides + SDK README)
- SDK defaults: `max_retries=2`, exponential backoff, retries connection errors, 408, 409, 429, >=500; default timeout 600 s (connect 5 s). `max_retries` must be a non-negative int.
- Do not retry (no amount of waiting fixes them): 429 with code `credit_balance_exhausted`, `organization_spend_limit_exceeded`, `project_spend_limit_exceeded`, `organization_usage_limit_exceeded`; 401; 403 (unsupported region); 400; 404.
- Retry with `Retry-After` honoured: 429 `rate_limit_exceeded`/`slow_down`, 503 `server_is_overloaded`, 500, timeouts, connection errors. OpenAI's own example: `tenacity` `@retry(wait=wait_random_exponential(min=1,max=60), stop=stop_after_attempt(6))`. If you add your own loop, set `max_retries=0` on the client or the loops multiply.
- `is_retryable(exc)` implementing exactly this is in `07-code/openai_support.py` (tested offline).

---------------------------------------------------------------------------------------------------------------------------------

## 5. OpenAI Python SDK on Python 3.13.3 / Windows 11 (VERIFIED by running it)

| Item | Result |
|---|---|
| Latest on PyPI | **`openai 3.26.0`**, released 2026-10-06; `requires-python >=3.10` (classifiers 3.10-3.14). 3.0.0 was 2026-08-12; last 2.x = 2.54.0 (2026-08-11). |
| Install into `...\scratchpad\openai-venv` | `pip install openai python-dotenv tenacity tiktoken` OK (cp313 win_amd64 wheels). `import openai; openai.__version__ == '3.26.0'`. |
| 3.0.0 breaking change | **HTTPX2 replaces httpx** (`httpx2`, `httpcore2`); `httpx` no longer installed. `http_client=` must be an `httpx2.Client/AsyncClient`. Timeouts: `httpx2.Timeout(...)`. |
| TLS on Windows | HTTPX2 uses the **OS trust store**, not `certifi`. Behind corporate TLS inspection this normally works (Windows store) but set `SSL_CERT_FILE` if not. My no-key call `models.retrieve("gpt-6-luna")` with a dummy key reached `api.openai.com` in 0.8 s and returned `401 invalid_api_key` -> `openai.AuthenticationError` (so TLS, DNS and proxy path are fine on this machine). |
| Client surface present | `responses.create/parse/stream`, `responses.input_tokens.count`, `chat.completions.create/parse/stream`, `embeddings.create`, `models.retrieve/list`, plus `decisions`, `live`, `skills`, `vector_stores`... |
| Offline mock testing | `httpx2.MockTransport` + `OpenAI(http_client=httpx2.Client(transport=...))` works; used for all tests in `07-code/`. |
| Error classes | `APIConnectionError`, `APITimeoutError`, `AuthenticationError` (401), `PermissionDeniedError` (403), `NotFoundError` (404), `BadRequestError` (400), `RateLimitError` (429; `.code` holds e.g. `credit_balance_exhausted`), `InternalServerError` (>=500). `exc.request_id` available on failures. |

**Combined environment actually built and tested** (short-path venv at `C:\Users\ayush\AppData\Local\Temp\ara-cv`, Python 3.13.3, Windows 11): `pip install pageindex ragas "langchain-community==0.4.1" langchain-openai python-dotenv` resolved to **openai 2.54.0, openai-agents 0.20.0, litellm 1.104.0, pageindex 0.2.21, ragas 0.4.3, instructor 1.17.0, langchain-community 0.4.1, langchain-openai 1.6.7, jiter 0.14.0, httpx 0.28.1 (httpx2 also present but unused by openai 2.x), pypdfium2 5.14.0, PyPDF2 3.0.1, tiktoken 0.14.0**. `pip check` reports no broken requirements. `from pageindex import PageIndexClient` works (`DEFAULT_INDEX_MODEL == "gpt-5.6-luna"`, `DEFAULT_CHAT_MODEL == "gpt-5.6-sol"` confirmed in the released 0.2.21 wheel). `07-code/test_offline.py` and the RAGAS wire/e2e tests pass on openai 2.54.0 as well as on 3.26.0 (they pick `httpx` or `httpx2` from the SDK major version). The install is slow (about 190 packages, pip backtracking, 24 MB litellm wheel; tens of minutes here), so freeze a lock file once chosen.

Practical env note: when RAGAS (instructor) and the latest openai are installed together pip backtracks (section 1.2 item 5). Decide in one place:
`pip install "openai>=2.54,<4"` is satisfied by either; pick a constraints file and keep the same pins in the Docker/FastAPI image later.

---------------------------------------------------------------------------------------------------------------------------------

## 6. Reconciling "use the models as suggested by the PageIndex docs" with reality

### 6.1 What PageIndex specifies (VERIFIED; repo `VectifyAI/PageIndex` @ `d57adcd` 2026-10-07, PyPI `pageindex 0.2.21` released 2026-10-01)
- `pageindex/config.yaml`: `# index_model: "gpt-5.6-luna"` / `# chat_model: "gpt-5.6-sol"` (commented, "unset keys use the SDK defaults shown"). `pageindex/utils.py`: `DEFAULT_INDEX_MODEL = "gpt-5.6-luna"`, `DEFAULT_CHAT_MODEL = "gpt-5.6-sol"`; legacy keys `model`, `summary_model`, `retrieve_model` still accepted.
- README quickstart: `PageIndexClient(index="gpt-5.6-luna", chat="gpt-5.6-sol")`. Model Recommendations: "`index=`: a basic model is sufficient ... `chat=`: use the best model you can afford." `docs.pageindex.ai/getting-started` shows the same two IDs and `export OPENAI_API_KEY=...`.
- Model names are LiteLLM's: bare names = OpenAI; other providers `provider/model`. Other docs claim OpenAI, Anthropic, OpenRouter or any OpenAI-compatible endpoint.
- README: "about **$0.001 per page** with `gpt-5.6-luna`" for local indexing; indexing time "roughly 13 seconds to 4.5 minutes" for 9 to 1,098 pages; PageIndex-OSS-Benchmark below.
- `chat()` options: `model`, `reasoning_effort` (`low/medium/high`; unset = model default), `max_turns`, `stream`, `show_process`, `citations=True` (local mode: page-level `<cite doc=".." page=".."/>`), `protocol` in `None|"chat_completions"|"responses"|"messages"`.

### 6.2 How PageIndex actually calls OpenAI (VERIFIED in source)
- **Indexing lane** (`pageindex/utils.py`): `litellm.completion/acompletion(model="openai/<name>", messages=[...], drop_params=True, max_retries=0, **index_backend)` with its own loop: 10 attempts, flat 1 s sleep, no retry on 400/401/403/404, otherwise raises `LLMRetriesExhausted`. It sends no temperature, no max tokens and no reasoning effort, so a reasoning index model runs at its **default effort (`medium`)**. `index_backend={...}` is merged verbatim into the litellm kwargs, so `index_backend={"reasoning_effort": "low"}` should work (INFERRED from the code; test it - could cut output tokens).
- Flash indexing concurrency: `summary_concurrency` default **64** calls per lane (expand capped at 32), so up to ~96 simultaneous calls. Total tokens for one 300-page doc (~0.5M) are far below Build-tier TPM (2M for Luna), so no 429 risk for one document; lower it (e.g. 16) only if several users index at once.
- **Chat lane** (`pageindex/local_chat.py`, `agent_tools.py`): OpenAI Agents SDK agent (`openai-agents`) with PageIndex tools; `RunConfig(tracing_disabled=True)` (no trace upload). `protocol="responses"` uses `OpenAIResponsesModel` + `openai.AsyncOpenAI` directly (no LiteLLM in the path). Default lane uses `LitellmModel` (chat.completions bridge). Returned envelope `usage` = cross-turn sums: Responses dialect `{input_tokens, input_tokens_details:{cached_tokens, cache_write_tokens}, output_tokens, output_tokens_details:{reasoning_tokens}, total_tokens}`; chat dialect `{prompt_tokens, completion_tokens, prompt_tokens_details:{cached_tokens}, ...}`. `include_usage=True` is set so streamed runs also report usage. `prompt_cache_key` is set automatically for OpenAI destinations; append returned `items` to the next call's `input` to keep cache-prefix continuity and the agent's memory.
- Agent workflow: for docs > 20 pages call `get_document_structure()` first (tree with node summaries, **paginated in parts of <= ~95,000 characters** (~24K tokens each)), then `get_page_content(pages="a-b")` for targeted ranges; responses capped at 100,000 chars.
- No telemetry in PageIndex code (only `RAGAS_DO_NOT_TRACK=true` is needed on the RAGAS side).

### 6.3 LiteLLM version facts (VERIFIED by reading the wheels' `model_prices_and_context_window_backup.json`)
| litellm | released | `gpt-5.6-luna` | `gpt-5.6-sol` | `gpt-6-luna/sol/astra` | `gpt-6.1-sol` |
|---|---|---|---|---|---|
| 1.97.0 (pinned in PageIndex `requirements.txt`; PyPI needs `>=1.97.0`) | 2026-08-16 | $0.20/$1.20 | **$5/$30** (stale; OpenAI page says $4/$20) | absent | absent |
| 1.104.0 (latest) | 2026-10-03 | $0.20/$1.20 | $4/$20 | present, correct prices | **absent** |

Consequences: (1) anything priced "from litellm's cost map" for GPT-6.1 Sol returns nothing - keep our own price table (`PRICES` in `07-code/openai_support.py`, source of truth = OpenAI pricing page). (2) The PageIndex benchmark's `gpt-5.6-sol` cost per question ($0.081) was priced from litellm's map (their README says so); if that map was 1.97.0 it is ~25-33% higher than at today's real $4/$20. (3) With unknown model IDs LiteLLM still passes the name through to OpenAI; `drop_params=True` may silently drop parameters it thinks are unsupported (INFERRED) - another reason to use `protocol="responses"` for chat, and to prefer `gpt-5.6-luna` for indexing until GPT-6 has been tried end-to-end.

### 6.4 PageIndex's own accuracy/cost benchmark (VERIFIED, `VectifyAI/PageIndex-OSS-Benchmark/results.json`)
62 lookup questions over 34 PDFs (1,945 pages, avg ~57 pages), `responses()` protocol, trees built with `gpt-5.6-luna`, MMLongBench-Doc-V2 judge; cost = answering call only.

| chat model | none | low | medium | high | avg $/question (n/l/m/h) |
|---|---|---|---|---|---|
| `gpt-5.6-luna` | 85.5% | 85.5% | 91.9% | 96.8% | 0.0031 / 0.0033 / 0.0038 / 0.0036 |
| `gpt-5.6-terra` | 90.3% | 95.2% | 98.4% | 100% | 0.0296 / 0.0324 / 0.0303 / 0.0325 |
| `gpt-5.6-sol` | 96.8% | 96.8% | 100% | 100% | 0.0759 / 0.0817 / 0.0810 / 0.0819 |

Takeaways: reasoning effort moves accuracy more than it moves cost; `terra@medium` ~ `sol@medium` at ~40% of the cost (on short docs; questions were text lookups, no tables/arithmetic, so annual-report table questions may need the stronger model). GPT-6 models are **not benchmarked by PageIndex**. README: native-PDF input costs 2.1x more than PageIndex at 52 pages and 16.6x at 420 pages (`gpt-5.6-sol`, no caching); at 805 pages it does not fit the context.

### 6.5 Recommended reconciliation
1. Ship the PageIndex-documented defaults (`gpt-5.6-luna` index, `gpt-5.6-sol` chat, effort `medium`, `protocol="responses"`) so "as per docs" is literally true and the behaviour matches what PageIndex tested.
2. Expose presets in the UI/config so UAT can A/B on 10-20 real annual-report questions using the app's own RAGAS + cost display: **Docs-default** (`gpt-5.6-sol`), **Balanced** (`gpt-5.6-terra`), **Newest** (`gpt-6.1-sol`, half the price of 5.6-sol, "near-Astra"; no `none/minimal` effort), **Budget** (`gpt-6-luna` @ high), **Premium** (`gpt-6-astra`).
3. Treat GPT-6 as "supported but unproven with PageIndex"; if used, prefer `litellm>=1.104.0` and keep `protocol="responses"`.

---------------------------------------------------------------------------------------------------------------------------------

## 7. RAGAS x OpenAI compatibility (what I could verify offline; scoring semantics belong to the RAGAS notes)

Environment: ragas **0.4.3** (PyPI latest, 2026-01-13; GitHub repo moved to `vibrantlabsai/ragas`, last commit 2026-02-24), instructor 1.17.0, langchain-core 1.6.7, langchain-openai 1.6.7 (needs `openai>=2.45,<4`), Python 3.13.3. `RAGAS_DO_NOT_TRACK=true` disables telemetry.

1. **Import failure** with `langchain-community 0.4.2`: `ragas/llms/base.py` line 12 imports `langchain_community.chat_models.vertexai`, which 0.4.2 removed. `langchain-community==0.4.1` (and 0.4.0, 0.3.31) still ship it. After pinning 0.4.1, `from ragas.metrics.collections import Faithfulness, AnswerRelevancy, ContextPrecisionWithoutReference` and the legacy `ragas.metrics` names import fine. (`ResponseRelevancy` is `AnswerRelevancy` in the new `collections` API.)
2. **How RAGAS calls OpenAI:** `llm_factory(model, client=AsyncOpenAI(...))` -> instructor `Mode.JSON` -> **Chat Completions** with `response_format={"type":"json_object"}`; `embedding_factory("openai", model=..., client=...)` -> `embeddings.create`. Extra kwargs to `llm_factory` (`max_tokens`, `reasoning_effort`, ...) are passed through to the request.
3. **What goes on the wire** (captured with a mock transport, `07-code/test_ragas_offline.py`):

| Judge | Request params RAGAS sends | Verdict |
|---|---|---|
| `gpt-4.1-mini`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4o` | `max_tokens=1024, temperature=0.01, top_p=0.1, response_format=json_object` | valid for non-reasoning models |
| `gpt-6-luna/sol/astra` (default kwargs) | `max_completion_tokens=1024, temperature=1.0` | 1024 includes reasoning tokens (truncation risk); `temperature=1.0` accepted? UNVERIFIED |
| `gpt-6-luna` + `max_tokens=4096, reasoning_effort="none"` | `max_completion_tokens=4096, reasoning_effort=none, temperature=1.0` | should be valid (temperature is only forbidden when effort != none); judge is non-deterministic |
| `gpt-6.1-sol`, `gpt-5.6-*`, `gpt-5.5`, `gpt-5.4-*` | `max_tokens=1024, temperature=0.01, top_p=0.1` (treated as non-reasoning) | **expected HTTP 400** |
| same + `adapt_for_reasoning(client, effort="low")` | `max_completion_tokens=4096, reasoning_effort=low`, no temperature/top_p | valid |

4. **Calls per scored answer (VERIFIED end-to-end offline with the real 0.4.3 classes, `07-code/test_ragas_e2e_offline.py`, N=4 contexts):** Faithfulness = 2 LLM calls (statement extraction, then NLI over ALL contexts joined); AnswerRelevancy = `strictness` (default 3) **sequential** single calls (not `n=3`) + 2 embedding calls (question; generated questions); ContextPrecisionWithoutReference = **1 sequential call per retrieved context**. Total LLM calls = **5 + N**, embeddings = 2; measured 9 LLM + 2 embedding calls for N=4. All three go through `chat.completions.create` / `embeddings.create`, so wrapping the client BEFORE `llm_factory` meters every call including instructor retries.
5. **Prompt sizes (measured with the real prompts + tiktoken `o200k_base`):** statement-gen ~500 tokens + answer; NLI ~1,025 + contexts + statements; AnswerRelevancy ~560 per call; ContextPrecision ~1,036 + context + answer (+ ~150-300 for instructor's JSON-mode schema text per call).
6. **Which contexts to pass:** the PageIndex pages actually read (`retrieved_contexts`). Faithfulness cost is dominated by total context tokens; ContextPrecision cost = N calls x (context + ~1.2K).
7. **Judge guidance:** (i) keep the judge a different family/version from the answerer; (ii) default `gpt-4.1-mini`; (iii) set `max_tokens>=4096` for any reasoning judge; (iv) use RAGAS `cache=DiskCacheBackend()` (supported by `llm_factory(..., cache=...)`) so re-scoring the same answer is free; (v) metrics are sequential internally: run the three metrics concurrently with `asyncio.gather` (as the e2e test does) and show scores asynchronously after the answer.
8. **Embeddings:** RAGAS docs/examples still use `text-embedding-ada-002` (RAGAS default for legacy calls). Use `text-embedding-3-small` instead (5x cheaper, newer; no deprecation of either announced).

---------------------------------------------------------------------------------------------------------------------------------

## 8. Cost and latency estimates (308-page annual report; all USD; assumptions explicit)

### 8.1 Indexing, PageIndex OSS defaults (local, **flash** mode: layout-extracted tree, deterministic merge + LLM "expand" + node summaries + doc description; `mode="standard"` is the opt-in slower LLM-built tree)

Evidence (VERIFIED): PageIndex flash README benchmark (input/output tokens with default index model):
165 pp 115,130/54,347; 222 pp 280,975/136,982; 585 pp 720,624/200,202; 758 pp 857,983/277,675; 1,098 pp 1,587,265/646,958. PageIndex-OSS-Benchmark `documents.json`: 19 priced docs, 1,475 pages, **mean 1,700 tokens/page (in+out) and $0.00089/page** at `gpt-5.6-luna` ($0.20/$1.20); range $0.00029-$0.00198/page (text density); 468-page doc = 973K tokens / $0.478. Calls: not published; INFERRED ~300-700 small calls (one per node for summaries + expand calls).

| Scenario (308 pages) | Input tokens | Output tokens (incl. reasoning) | `gpt-6-luna` | `gpt-5.6-luna` | `gpt-4.1-mini` | `gpt-4o-mini` |
|---|---|---|---|---|---|---|
| Low | 215K | 100K | $0.07 | $0.16 | ~$0.20 | $0.09 |
| Likely | 385K | 152K | $0.12 | **$0.26** (cross-check: 308 x $0.00089 = $0.27) | ~$0.27-0.40 | $0.15 |
| High | 445K | 190K | $0.14 | $0.32 | ~$0.48 | $0.18 |

Notes: `gpt-4.1-mini` has no reasoning tokens so its output would be lower than the table's assumption (upper bounds shown). If `gpt-6-luna`'s default `medium` reasoning inflates output 2x, double the output term (~$0.19 likely). Standard mode (INFERRED, not measured; TOC detection on first 20 pages, TOC extraction/transform, per-entry verification calls, recursive splitting of nodes > 10 pages/20K tokens, then summaries): 1.0-1.8M input + 0.1-0.25M output tokens, 300-700 calls -> $0.15-$0.30 (`gpt-6-luna`), $0.32-$0.66 (`gpt-5.6-luna`).
**Latency:** README says 13 s - 4.5 min for 9-1,098 pages -> **~1.5-3 min for 308 pages in flash** (INFERRED interpolation), standard mode 5-15 min (INFERRED). Index once per upload, show a progress bar, run in a background thread/worker (indexing is synchronous inside `submit_document`).

### 8.2 One question: PageIndex retrieval + answer (agent loop 3-6 model turns on the Responses API)
Evidence (VERIFIED) used to calibrate:
1. PageIndex-OSS-Benchmark: `gpt-5.6-luna` averages $0.0036/question on ~57-page documents; at $0.20/$1.20 that is only ~13K billed-equivalent input tokens + ~1K output. Cost is dominated by re-reading the document tree (`get_document_structure` returns node titles + summaries), which scales ~linearly with pages: 13K x (308/57) = **~70K**.
2. PageIndex flash-demo cookbook (47-page Kimi K3 report): the `get_document_structure` tool result was > 38,000 characters beyond the printed snippet, i.e. ~0.8K characters (~200 tokens) per page. For 308 pages: ~250K characters ~ 62K tokens, delivered in 3 parts of <= 95,000 characters (the tool's page budget). Annual reports are table-heavy with fewer headings per page, so real trees may be smaller; measure yours.
3. PageIndex query-pricing cookbook (multi-document NVIDIA 5-year revenue comparison, `gpt-5.6-luna`, `protocol="responses"`, streaming): cumulative usage `input_tokens=210,613` of which `cached_tokens=104,152` and `cache_write_tokens=106,446`, `output_tokens=1,883` (`reasoning_tokens=443`) -> $0.031 at luna rates using the formula `fresh*in + cached*cached_rate + written*1.25*in + out*out_rate`. Note how large the cache-write share is: every new tool result is billed at 1.25x once, then read at 0.1x on later turns.
4. **Measured offline (no LLM, no key)** with `pageindex.flash.page_index_flash(pdf, summary=False, optimize=...)` on the repo's sample `examples/documents/2023-annual-report.pdf` (222 pages, 540,247 text characters = **2,433 chars/page ~ 600 tokens/page**, `toc_source="bookmarks"`): raw tree = 283 nodes (**1.27 nodes/page**, 29.6K JSON characters before summaries); `optimize="merge"` = 177 nodes (**0.80 nodes/page**, 23.3K characters). The layout step alone took 16-19 s for 222 pages. Adding summaries of <= 150 words (assume ~600 characters average) gives ~200K characters ~ 50K tokens for 222 pages (~0.9K chars/page), matching the Kimi figure in point 2. For 308 pages: ~45-70K tokens of tree.
5. Sanity check against "stuff the whole report": 308 pages x ~600 tokens = ~185K tokens per question (~$0.74 at `gpt-5.6-sol` rates, uncached) vs ~$0.35 likely with PageIndex - consistent with the README's "2.1x at 52 pages, 16.6x at 420 pages" trend (the gap widens with length).
Assumed billed-equivalent input tokens for one 308-page question: **low 35K / likely 75K / high 180K**; output (incl. reasoning) **1K / 2.5K / 8K**.

| Chat model | Low | Likely | High | Notes |
|---|---|---|---|---|
| `gpt-6-luna` ($0.10/$0.50) | $0.004 | **$0.009** | $0.022 | unbenchmarked by PageIndex; `gpt-5.6-luna@high` scored 96.8% |
| `gpt-5.6-luna` ($0.20/$1.20) | $0.008 | $0.018 | $0.046 | |
| `gpt-5.6-terra` ($2/$12) | $0.082 | $0.18 | $0.46 | |
| `gpt-6.1-sol` ($2/$10) | $0.080 | $0.175 | $0.44 | cache reads are 0.05x, so follow-ups are cheaper still |
| `gpt-5.6-sol` ($4/$20) **docs default** | $0.16 | **$0.35** | $0.88 | PageIndex's benchmark number ($0.081/q on ~57-page docs) was priced from an older LiteLLM map ($5/$30) |
| `gpt-6-astra` ($10/$50) | $0.40 | $0.88 | $2.20 | |

How to cut it (VERIFIED levers in `PageIndexClient`): lower `summary_max_words` (default 150) and/or `optimize="merge"` at index time (smaller tree = cheaper every question); `max_turns`; `reasoning_effort="low"` (benchmark: little dollar change, some accuracy loss on small models); round-trip the previous `items` so follow-ups hit the prompt cache (TTL >= 30 min on GPT-5.6+) and skip re-reading the tree.
**Latency (INFERRED, not measured):** 3-6 sequential turns; `luna@medium` 6-20 s, `terra@medium` 10-40 s, `5.6-sol@medium` 15-60 s, `astra` 30-120 s end-to-end; the first visible answer token only appears on the last turn (stream it). Reading a 60K-token tree costs time-to-first-token on every cold question.

### 8.3 RAGAS per answer (calls = 5 + N LLM calls + 2 embedding calls; token model measured from real prompts)
Scenarios: **Low** N=3 contexts x 1.5K tokens, 4 statements -> ~17.8K input / 0.75K output, 8 calls. **Likely** N=6 x 2K, 8 statements -> ~38.6K / 1.35K, 11 calls. **High** N=15 x 3K, 15 statements -> ~124.6K / 2.65K, 20 calls. Reasoning judges add hidden output tokens per call (assumed +250/call at `low`, +700/call at `medium`).

| Judge | Low | Likely | High |
|---|---|---|---|
| `gpt-4o-mini` ($0.15/$0.60) | $0.003 | $0.007 | $0.020 |
| **`gpt-4.1-mini` ($0.40/$1.60)** | $0.008 | **$0.018** | $0.054 |
| `gpt-6-luna`, effort none ($0.10/$0.50) | $0.002 | $0.005 | $0.014 |
| `gpt-6-luna`, medium | $0.005 | $0.008 | $0.021 |
| `gpt-5.6-luna`, low (via shim) | $0.007 | $0.013 | $0.034 |
| `gpt-4.1` ($2/$8) | $0.042 | $0.088 | $0.27 |
| `gpt-4o` ($2.50/$10) | $0.052 | $0.11 | $0.34 |
| `gpt-6.1-sol`, low (via shim) | $0.063 | $0.118 | $0.33 |
| Embeddings (any scenario, ~100 tokens) | `-3-small` $0.000002 | | `-3-large` $0.000013 |

Context size note (measured above): annual-report pages average ~600 tokens (dense table pages maybe 1,000-1,500), so if each RAGAS context is one page the scenarios' 1.5K-3K tokens/context are conservative; real RAGAS spend is likely 30-60% below the table. Use page-level contexts (one per cited page) rather than concatenated ranges so ContextPrecision gets a meaningful per-chunk verdict.
**Latency (INFERRED):** ContextPrecision runs N calls sequentially, AnswerRelevancy 3 sequentially, Faithfulness 2; with the three metrics concurrent the critical path is ~N calls: non-reasoning judge ~1-3 s per call -> 6-20 s for N=6; reasoning judge at medium 4-10 s per call -> 25-60 s. If that is too slow, subclass/loop the ContextPrecision prompt yourself and `asyncio.gather` the N calls (metric math is trivial), or lower N by scoring per cited page.

### 8.4 Combined per question and UAT budget (likely; low-high in brackets; index cost included once)

| Preset (chat + judge) | Per question | 100-question UAT + 1 index |
|---|---|---|
| Docs-default: `gpt-5.6-sol` + `gpt-4.1-mini` | $0.37 ($0.17-$0.93) | ~$37 ($17-$94) |
| Balanced: `gpt-5.6-terra` + `gpt-4.1-mini` | $0.20 ($0.09-$0.51) | ~$20 ($9-$51) |
| Newest: `gpt-6.1-sol` + `gpt-4.1-mini` | $0.19 ($0.09-$0.49) | ~$20 ($9-$50) |
| Budget: `gpt-6-luna` + `gpt-4o-mini` | $0.015 ($0.007-$0.042) | ~$2 ($1-$5) |
| Premium: `gpt-6-astra` + `gpt-4.1` | $0.96 ($0.44-$2.47) | ~$97 ($44-$247) |

---------------------------------------------------------------------------------------------------------------------------------

## 9. Practical engineering

### 9.1 Keep the key in `.env` (python-dotenv), never in code
```
# .env  (git-ignored)                    # .env.example (committed, placeholders only)
OPENAI_API_KEY=sk-...                    OPENAI_API_KEY=
PI_INDEX_MODEL=gpt-5.6-luna              PI_INDEX_MODEL=gpt-5.6-luna
PI_CHAT_MODEL=gpt-5.6-sol                PI_CHAT_MODEL=gpt-5.6-sol
PI_CHAT_REASONING_EFFORT=medium          PI_CHAT_REASONING_EFFORT=medium
RAGAS_JUDGE_MODEL=gpt-4.1-mini           RAGAS_JUDGE_MODEL=gpt-4.1-mini
RAGAS_JUDGE_MAX_TOKENS=4096              RAGAS_EMBEDDING_MODEL=text-embedding-3-small
RAGAS_DO_NOT_TRACK=true
```
- `.gitignore` must list `.env`, `.pageindex/` (index store), `uploads/`, `*.log`.
- `load_dotenv(find_dotenv(usecwd=True))` - PageIndex's `utils.py` already does exactly this at import (VERIFIED), so one `.env` in the project root serves OpenAI SDK, LiteLLM and PageIndex. The SDK reads `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID` automatically.
- Use a **dedicated project key with an expiry date** (feature added 2026-09-10) and set a **hard monthly spend limit** on the project (hard limit -> 429 `project_spend_limit_exceeded`); keep the key server-side only (matters for the later FastAPI service); never log it (mask to `sk-...abcd`); never echo it in `/health`.
- Windows: avoid `setx OPENAI_API_KEY` on shared machines (persists in the registry); prefer `.env`.
- Public annual reports are low-sensitivity; if a confidential PDF is ever used, remember Responses `store` defaults to true (set `store=False`; ZDR orgs also need encrypted reasoning). PageIndex local mode sends only page text/summaries, not the PDF file.

### 9.2 Track tokens and cost per call (UI shows "cost of this answer")
- Usage fields (VERIFIED in SDK types): Responses `usage.input_tokens`, `usage.input_tokens_details.{cached_tokens, cache_write_tokens}`, `usage.output_tokens` (includes reasoning), `usage.output_tokens_details.reasoning_tokens`; Chat `usage.prompt_tokens`, `prompt_tokens_details.{cached_tokens, cache_write_tokens}`, `completion_tokens`, `completion_tokens_details.reasoning_tokens`; embeddings `usage.prompt_tokens`.
- Cost = `fresh_in*in + cached*cached_rate + cache_write*write_rate + out*out_rate`, `fresh_in = input - cached - cache_write`. Reasoning tokens are already inside `output_tokens`.
- Implemented and tested offline: `07-code/openai_support.py` (`PRICES`, `price_for` incl. dated-snapshot prefix match, `usage_from_response`, `cost_usd`, `UsageMeter.record/snapshot`, `meter_client(client, meter, stage)` wrapping `responses.create`, `chat.completions.create`, `embeddings.create` for sync **and async** clients). Gotcha found while testing: `inspect.iscoroutinefunction()` is False for the SDK's decorated async `create()`, so the wrapper keys off `isinstance(client, AsyncOpenAI)`.
- Sources of usage per stage: **PageIndex chat** -> envelope `usage` (cross-turn sum; pass it to `UsageMeter` via `Usage(...)`); **PageIndex indexing** (LiteLLM) -> subclass `litellm.integrations.custom_logger.CustomLogger` and implement `log_success_event` / `async_log_success_event(kwargs, response_obj, start_time, end_time)` (VERIFIED present in litellm 1.104.0), read `response_obj.usage`; register with `litellm.callbacks = [logger]` before `submit_document` (INFERRED to fire for PageIndex's calls) - or compute indexing cost from the estimate table when exact tracking is not worth it; **RAGAS** -> `meter_client(client, meter, "ragas")` BEFORE `llm_factory` (instructor captures the wrapped `create`, so retries are counted too).
- Compute prices from our table, not from LiteLLM's (missing `gpt-6.1-sol`). Keep `PRICES` in a JSON file overridable by env so a price change is a config change. Unknown model -> show tokens only (`usd=None`), never guess.
- Optional pre-flight sizing: `client.responses.input_tokens.count(model=..., input=...)` returns exact input tokens (docs do not state whether it is free: UNVERIFIED; do not use in loops).

### 9.3 Concurrency
- Build-tier ceilings (section 2.1) are generous: 5,000 RPM and 1-2M TPM. Realistic app load: indexing ~0.5M tokens/doc, one question ~50-120K tokens, one RAGAS pass ~40K tokens.
- Suggested limits: PageIndex `summary_concurrency=16` (default 64 is fine for a single user); a process-wide `asyncio.Semaphore(8)` around RAGAS judge calls; one in-flight indexing job per user; queue questions per session (one agent run at a time per session).
- `slow_down` 429s come from *ramp rate*, not volume; just honour `Retry-After`.
- Prefer SDK retries (`OpenAI(max_retries=4, timeout=120.0)`; a plain float works on both httpx and httpx2 SDK lines) over custom loops; if you add tenacity, set `max_retries=0` and use `is_retryable()`.

### 9.4 Retrieval + answer call, structured answer step (if you split it from PageIndex's own answer)
PageIndex's agent already answers with `<cite doc= page=/>` tags (page-level in local mode). If the UI needs passage-level highlights, add one cheap structured step over the pages the agent read:
```python
from pydantic import BaseModel
class Claim(BaseModel):
    text: str
    page: int
    quote: str            # verbatim span copied from that page's text; verify with `quote in page_text`
class Answer(BaseModel):
    answer: str
    claims: list[Claim]
r = client.responses.parse(model=cfg.chat_model, input=[...], text_format=Answer,
                           reasoning={"effort": "low"}, max_output_tokens=8000)
r.output_parsed ; r.usage            # tokens for the meter
```
Do not send `temperature` with reasoning effort != none. Reject/repair any quote that is not a substring of the stored page text (guards against hallucinated citations).

### 9.5 Startup smoke test that costs nothing
1. Key present and shaped like a key (never print it).
2. For each configured model: `client.models.retrieve(id)` (GET `/v1/models/{id}`, no tokens billed). Returns `id, owned_by, created, shutdown_date`; map `AuthenticationError` -> stop (bad key); `NotFoundError` -> model id wrong or not available to this project (use fallback chain); `PermissionDeniedError` -> region/project restriction; `APIConnectionError` -> network/proxy/TLS. A non-null `shutdown_date` within ~60 days -> warn in the UI. (That `retrieve` 404s for ids the *project* cannot use is INFERRED.)
3. Optional `--deep`: one real call per distinct model with a 256-token cap, e.g. `responses.create(model=m, input="ping", max_output_tokens=256, reasoning={"effort":"low"})` (<= ~$0.0002 on Luna; ~$0.003 on Sol; skip effort for `gpt-4.1*`). Use at least 256 output tokens: tiny caps on reasoning models return `incomplete` with no text.
4. Credit/limit awareness: there is no documented "get balance" API call in what I read (dashboard only); rely on spend limits + handling `credit_balance_exhausted` / `*_spend_limit_exceeded` as fatal in the UI.
`preflight()` (step 2) and the error classification are implemented and tested offline in `07-code/openai_support.py` (mock 200 / 404 / 429-with-code).

### 9.6 Minimal wiring sketch (all calls verified to construct/serialise offline; live behaviour UNVERIFIED without a key)
```python
import openai_support as S
cfg = S.ModelConfig(); meter = S.UsageMeter()

# (a)+(b)+(c) PageIndex, Responses protocol
from pageindex import PageIndexClient
pi = PageIndexClient(index_model=cfg.index_model, chat_model=cfg.chat_model, storage_path="./.pageindex")
doc = pi.submit_document("report.pdf")                      # flash mode by default; one doc per session
res = pi.chat("Underlying operating profit and why it changed?", doc_id=doc["doc_id"],
              protocol="responses", reasoning_effort=cfg.chat_effort or None, citations=True, max_turns=12)
# res["usage"] -> Usage(input_tokens=.., cached_tokens=.., cache_write_tokens=.., output_tokens=..)

# (d)+(e) RAGAS
from openai import AsyncOpenAI
from ragas.llms import llm_factory
from ragas.embeddings.base import embedding_factory
from ragas.metrics.collections import Faithfulness, AnswerRelevancy, ContextPrecisionWithoutReference
client = AsyncOpenAI(max_retries=4, timeout=120)
S.meter_client(client, meter, "ragas")                      # innermost
# S.adapt_for_reasoning(client, effort="low")               # ONLY for gpt-5.x / gpt-6.x judges
llm = llm_factory(cfg.judge_model, client=client, max_tokens=cfg.judge_max_tokens)
emb = embedding_factory("openai", model=cfg.embedding_model, client=client)
```
(`PageIndexClient(index_model=..., chat_model=...)` and `chat(protocol="responses", reasoning_effort=..., citations=True, doc_id=..., max_turns=...)` are signatures read from `pageindex/client.py`; the README's short form is `PageIndexClient(index=..., chat=...)`.)

Streaming to the UI with usage at the end (pattern copied from PageIndex's `cookbook/pageindex-query-pricing-demo.ipynb`, which ran it with a real key; there against Cloud + `folder_id`, local mode uses `doc_id`):
```python
final = None
for ev in pi.chat([{"role": "user", "content": question}], doc_id=doc_id, stream=True,
                  citations=True, protocol="responses", reasoning_effort="medium"):
    t = ev["type"]
    if t == "response.output_text.delta":            # answer tokens -> SSE/WebSocket to the browser
        push(ev["delta"])
    elif t == "response.output_item.done" and ev["item"].get("type") == "function_call":
        push_status(ev["item"]["name"], ev["item"].get("arguments", ""))   # "Reading pages 25-31..."
    elif t in {"response.completed", "response.incomplete", "response.failed"}:
        final = ev["response"]                        # final["usage"] = cross-turn token totals
usage = final["usage"]   # {"input_tokens", "input_tokens_details": {"cached_tokens", "cache_write_tokens"}, "output_tokens", "output_tokens_details": {"reasoning_tokens"}}
```
Always check `final["status"]`: `incomplete` (e.g. `max_output_tokens`/`max_turns`) and `failed` still bill tokens.

---------------------------------------------------------------------------------------------------------------------------------

## 10. Risks and unknowns (need a live key or later decision)

1. **GPT-6 + PageIndex end-to-end is untested by anyone I could find** (PageIndex benchmark stops at GPT-5.6). Run the UAT A/B (section 6.5) before changing defaults.
2. Whether OpenAI rejects `temperature=1.0` on GPT-6 with effort != none (affects RAGAS `llm_factory` on `gpt-6-*` unless `reasoning_effort="none"` or the shim is used). UNVERIFIED.
3. Whether a Free-tier or unverified organisation can call GPT-6/5.6 models; whether any organisation verification is needed. The docs I read do not say. UNVERIFIED.
4. Latency numbers in sections 8.2/8.3 are estimates; measure with `time.perf_counter()` around each stage and surface p50/p95 in the app.
5. Per-question token usage depends on tree size (node count x summary length), and is the dominant cost term. Measure the real tree right after indexing (`len(json.dumps(tree))/4` tokens) and re-estimate; if > ~60K tokens consider a smaller `summary_max_words` or `optimize="merge"`.
6. PageIndex's local mode is page-level citation only (block-level + bounding boxes are Cloud features per README table); passage highlighting needs our own quote matching (9.4).
7. `reasoning_effort` on PageIndex indexing lane via `index_backend` (INFERRED) and via `litellm` `drop_params=True` may be silently dropped for models LiteLLM does not know.
8. RAGAS is effectively unmaintained since Feb 2026 (last commit); expect to pin `ragas==0.4.3` + `langchain-community==0.4.1` and own any patches (the dotted-version reasoning detection bug is easy to monkeypatch: wrap the client as in `adapt_for_reasoning`).
9. GPT-5.6/GPT-6 aliases have no dated snapshots: judge/answer drift is possible mid-UAT; store `response.model`, request ids and timestamps with every score.
10. Dependency resolution (openai 2.54 vs 3.26, jiter pin, litellm 1.97 vs 1.104) must be frozen in a lock/constraints file once chosen.

---------------------------------------------------------------------------------------------------------------------------------

## 11. Sources (all fetched this session)

OpenAI docs (also `.md` twins):
- Models catalog: https://developers.openai.com/api/docs/models(.md); per-model pages e.g. https://developers.openai.com/api/docs/models/gpt-6-luna.md, `/gpt-6.1-sol.md`, `/gpt-6-astra.md`, `/gpt-6-sol.md`, `/gpt-5.6-sol.md`, `/gpt-5.6-terra.md`, `/gpt-5.6-luna.md`, `/gpt-5.4-mini.md`, `/gpt-4.1.md`, `/gpt-4.1-mini.md`, `/gpt-4o.md`, `/gpt-4o-mini.md`, `/text-embedding-3-small.md`, `/text-embedding-3-large.md`, `/text-embedding-ada-002.md`
- Pricing: https://developers.openai.com/api/docs/pricing.md
- Deprecations: https://developers.openai.com/api/docs/deprecations.md
- Changelog: https://developers.openai.com/api/docs/changelog.md
- Using GPT-6: https://developers.openai.com/api/docs/guides/latest-model.md ; Model selection: .../guides/model-selection.md
- Reasoning: https://developers.openai.com/api/docs/guides/reasoning.md ; Structured outputs: .../guides/structured-outputs.md ; Migrate to Responses: .../guides/migrate-to-responses.md
- Streaming: .../guides/streaming-responses.md ; Prompt caching: .../guides/prompt-caching.md ; Token counting: .../guides/token-counting.md
- Rate limits: .../guides/rate-limits.md ; Error codes: .../guides/error-codes.md ; Production best practices: .../guides/production-best-practices.md ; Decisions: .../guides/decisions.md
- API reference: https://developers.openai.com/api/reference/resources/responses/methods/create.md ; https://developers.openai.com/api/reference/resources/models/methods/retrieve.md ; .../models/methods/list.md
Code / packages:
- openai-python @ `4e152cd` (v3.26.0, 2026-10-06): https://github.com/openai/openai-python (README, CHANGELOG, httpx2.md, `src/openai/types/...`); PyPI https://pypi.org/project/openai/
- PageIndex @ `d57adcd` (2026-10-07): https://github.com/VectifyAI/PageIndex (README, `pageindex/utils.py`, `config.yaml`, `client.py`, `local_chat.py`, `agent_tools.py`, `flash/README.md`); PyPI https://pypi.org/project/pageindex/ (0.2.21); docs https://docs.pageindex.ai/getting-started , https://docs.pageindex.ai/sdk/chat
- PageIndex cookbooks `cookbook/pageindex-query-pricing-demo.ipynb` (real usage JSON + cost formula) and `cookbook/pageindex-flash-demo.ipynb` (tool-result sizes) in the same repo
- PageIndex-OSS-Benchmark: https://github.com/VectifyAI/PageIndex-OSS-Benchmark (`results.json`, `documents.json`, `pi_bench.py`)
- ragas 0.4.3 wheel + repo https://github.com/vibrantlabsai/ragas (`src/ragas/llms/base.py`, `metrics/collections/*`); PyPI https://pypi.org/project/ragas/
- litellm 1.97.0 and 1.104.0 wheels (model price maps, `CustomLogger`): https://pypi.org/project/litellm/ ; openai-agents https://pypi.org/project/openai-agents/ (0.23.1 needs `openai>=3,<4`)

## 12. Files produced (reusable)
- `C:\Users\ayush\Annual Report Answering\research\07-code\openai_support.py` - `ModelConfig` (env-driven), `PRICES`, usage/cost, `UsageMeter`, `meter_client`, `preflight`, `is_retryable`, `adapt_for_reasoning` (RAGAS shim).
- `...\07-code\test_offline.py` (mock transport: metering, snapshot price resolution, `responses.parse`, preflight 200/404, non-retryable 429) - passes on openai 3.26.0.
- `...\07-code\test_ragas_offline.py` (wire capture of RAGAS requests per judge model, with/without shim) and `test_ragas_e2e_offline.py` (3 metrics, call-count proof: 9 LLM + 2 embedding calls for N=4).
- Venvs used: `...\scratchpad\openai-venv` (openai 3.26.0 + ragas 0.4.3 + instructor 1.17.0 + langchain-community 0.4.1; no litellm because the path is too deep for it), `C:\Users\ayush\AppData\Local\Temp\ara-cv` (full resolver-chosen stack incl. pageindex 0.2.21 + litellm 1.104.0, short path; throwaway, safe to delete), `...\scratchpad\combo-venv` (failed MAX_PATH install; safe to delete).
