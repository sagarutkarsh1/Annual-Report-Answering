# 01 - The open-source PageIndex repo (VectifyAI/PageIndex): how it really works and how to call it from our own Python code

Research date: **2026-10-07**. Environment used for every experiment: **Windows 11 Home (10.0.26200), Python 3.13.3, 12 logical CPUs, no OpenAI/PageIndex key, no accounts, no money spent.**
Legend: **VERIFIED** = I saw it in source/docs/PyPI/GitHub this session or ran it; **INFERRED** = deduced, not directly observed.

Sibling notes (do not duplicate, read together): `02-pageindex-cloud-docs-and-api.md` (docs site, cloud API, MCP), `03-pageindex-retrieval-patterns-and-prompts.md` (prompts, answer pipeline, annual-report practice), `07-openai-models-and-costs.md`.

---

## 0. TL;DR - the things that will change our design

1. **The repo is no longer "run_pageindex.py + a tree-search prompt".** Since Aug 2026 (`pageindex` 0.2.10+) it is an **SDK with a local mode**: `PageIndexClient(index="gpt-5.6-luna", chat="gpt-5.6-sol")`, `client.submit_document(pdf)`, `client.chat(question, doc_id=...)`. Most blog posts / older cookbooks (CHATGPT_API_KEY, `gpt-4o-2024-11-20`, `client.index()`, `workspace=`, `pageindex.page_index` module, `tutorials/tree-search`) describe a **previous, different API**. (VERIFIED: current source; old files recovered from git history at commit `6fd2379`, 2026-04-28.)
2. **`pip install pageindex` gives the OSS lib *and* the cloud SDK in one package** (VERIFIED). Local mode needs **no PageIndex key** - only an OpenAI key (`OPENAI_API_KEY`). `PAGEINDEX_API_KEY` is cloud-only. I confirmed local code paths never call `api.pageindex.ai` (grep of `BASE_URL` use) and the agent run disables tracing (`RunConfig(tracing_disabled=True)`).
3. **Indexing = "PageIndex Flash"**: the tree skeleton comes from PDF **layout statistics + embedded bookmarks, no LLM** (pypdfium2); the LLM is only used for node **summaries**, a **tree-expand** pass for oversized nodes, and a one-sentence **doc description**. Measured on a real 222-page annual report: layout stage 18-22 s on 12 cores; 177-283 nodes; ~179 LLM calls. Cost ~ **$0.001/page with `gpt-5.6-luna`** (README) => **~$0.30 and ~1-2 min for a 308-page report** (INFERRED from the README benchmark table).
4. **Answering = an agent loop**, not "tree search then answer": OpenAI Agents SDK + LiteLLM, tools `browse_documents`, `get_document`, `get_document_structure`, `get_page_content`. The LLM reads the *tree with summaries* (paged in <=95,000-char parts), picks page ranges, reads **full page text**, answers; `citations=True` adds `<cite doc="..." page="N"/>` tags. (VERIFIED by running the real loop against a local mock OpenAI server; exact system prompt captured, Section 8.)
5. **Local citations are page-level only** (no block ids, no bounding boxes - those are cloud-only). "Cite page *and area*" and "highlight the passage" must be built by us (node breadcrumb from the tree + a verbatim quote we ask the model for + PDF.js text search). `client.get_citations()` **drops any extra attribute** such as `quote=` (VERIFIED), so we need our own cite parser (tested code in Section 9).
6. **Page numbers are physical, 1-based, inclusive** everywhere (tree `start_index`/`end_index`, `get_page_content`, `<cite page>`). Printed page numbers differ (VERIFIED: Fed report physical page 10 prints "4"). PDF.js `currentPageNumber` is also physical 1-based -> no off-by-one for the viewer.
7. **Stored page text comes from PyPDF2 (`extract_text()`), the tree from pdfium** - and the PyPDF2 text has broken spacing ("Monetar y Policy", "de velopments"; VERIFIED here) and, per note 03, corrupted digits on ~23% of pages of a real annual report. Because `get_page_content` feeds the answer model *and* RAGAS contexts, **override page-text extraction with pdfium** (one-line monkeypatch, VERIFIED working, 1.8 s for 222 pages, Section 9.3). pdfium is BSD/Apache - **avoid PyMuPDF (AGPL)**.
8. **Windows gotchas, all VERIFIED:** (a) `pip install pageindex` **fails if the venv path is long** (litellm ships very deep paths; MAX_PATH 260) - keep the venv root <= ~105 chars (Section 2.2); (b) AES-encrypted PDFs (very common for corporate reports) **crash `submit_document` unless `pycryptodome` is installed** (GitHub issue #426, reproduced and fixed here); (c) Flash uses a spawn `ProcessPoolExecutor` for docs >= 64 pages - works with or without an `if __name__ == "__main__"` guard (they hide `__main__` from workers) but guard anyway; (d) the cross-process store lock uses `fcntl`, which does not exist on Windows -> **no lock**, so use one store per session / serialise writes.
9. **Release cadence is ~every 3-5 days** (19 GitHub releases since 2026-08-13; 0.2.21 on 2026-10-01; HEAD is one commit ahead). Git/PyPI also carry `0.3.0.devN` = the older local client with a different API. **Pin `pageindex==0.2.21`** (what I tested) and test before bumping.
10. **What to vendor?** Nothing is *required*. pip-install the pinned package + `pycryptodome`, add two tiny adapters of ours (pdfium page text, context/cite capture). Optional later: vendor only `pageindex/flash/**` + `tree_optimize.py` + a small LLM shim if we want to drop litellm/openai-agents/mcp (Section 12).

---

## 1. Provenance, license, maintenance (VERIFIED unless noted)

| Item | Value |
|---|---|
| Repo | https://github.com/VectifyAI/PageIndex - MIT license (checked `LICENSE`, "Copyright (c) 2025 Vectify AI"; quoting code/prompts here is fine) |
| Popularity (GitHub API, 2026-10-07) | 38,887 stars, 3,367 forks, 120 open issues, default branch `main`, not archived |
| Activity | last push 2026-10-07 09:26 UTC; HEAD `d57adcd` "fix: unified-tree follow-ups in expand, intros and standard indexing (#554)"; 30 most recent commits span 2026-09-17 -> 2026-10-07; **19 GitHub releases** (0.2.10.dev1 on 2026-08-13 ... v0.2.21 on 2026-10-01) |
| PyPI | project `pageindex`, **latest stable 0.2.21 (2026-10-01)**, MIT, author Ray <ray@vectify.ai>, `Requires-Python >=3.10`, classifiers 3.10-3.15, wheel ~589 KB |
| PyPI history | 0.1.x-0.2.8 (2025-03 -> 2026-03-15): tiny **cloud-only SDK** (wheels 1-6 KB). 0.3.0.dev0-dev3 (2026-04-08 -> 07-10): the previous *local* client (`PageIndexClient(workspace=...)`, `.index()`, `retrieve_model`). **0.2.10 (2026-08-19) onward: the unified local+cloud SDK** (wheel 550 KB). `pip install pageindex` skips `.devN` pre-releases. |
| Version in repo | `pyproject.toml` says `0.2.10` but the **git tag is the version** ("nothing to bump in the repo", `.github/workflows/publish.yml`) - do not trust `pyproject.toml` for the version. |
| CI | `.github/workflows/tests.yml`: **ubuntu-latest only**, Python 3.10 and 3.13, with/without agent frameworks, pdfium 5 (+ pdfium 4 on 3.10). **No Windows CI** -> our Windows runs below are the only Windows evidence. |
| Python 3.13 | VERIFIED working: full local flow ran on 3.13.3/Windows (flash, mocked indexing, agent loop on both `chat` and `responses` lanes). |
| Telemetry | none found (grep for telemetry/analytics/posthog/sentry: nothing; local paths do not hit PageIndex servers). LiteLLM is told not to fetch its remote cost map (`LITELLM_LOCAL_MODEL_COST_MAP=True` set via `os.environ.setdefault`). |

Repo layout at HEAD (VERIFIED): `pageindex/` (`client.py` 2.7k lines, `agent_tools.py`, `local_chat.py`, `local_api.py`, `local_store.py`, `utils.py`, `tree_optimize.py`, `page_index_classic.py`, `page_index_md.py`, `cloud_api.py`, `mcp_bridge.py`, `chat_stream.py`, `imaging.py`, `naming.py`, `types.py`, `errors.py`, `config.yaml`, `flash/**`, `integrations/{openai_agents,anthropic_sdk,claude_agent_sdk}.py`), `cookbook/` (5 notebooks), `examples/documents/*.pdf` (+ stale `examples/results/*.json`), `tests/`, `run_pageindex.py`. **There is no `tutorials/` any more** (it was deleted; recoverable from history).

---

## 2. Install on Windows

### 2.1 Steps (tested)
```powershell
# SHORT venv path matters (see 2.2).  e.g. C:\Users\ayush\Annual Report Answering\.venv  (~44 chars) is fine.
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install "pageindex==0.2.21" pycryptodome
```
Resulting set I tested (84 dist-infos; `pip check`: "No broken requirements found"): `pageindex 0.2.21`, `litellm 1.104.0`, `openai 2.54.0`, `openai-agents 0.20.0`, `mcp 2.3.0`, `pypdfium2 5.14.0`, `PyPDF2 3.0.1`, `pillow 12.3.0`, `pydantic 2.13.5`, `tiktoken 0.14.0`, `httpx 0.28.1`, `PyYAML 6.0.3`, `regex 2026.9.29`, `sortedcontainers 2.4.0`, `python-dotenv 1.2.4`, `requests 2.34.2`, `pycryptodome` (added by me).
`import pageindex` + `PageIndexClient`: 0.1 s (lazy); client construction 0.9 s; a background thread pre-imports litellm (several seconds).
`pip install --dry-run ragas` on top of this venv resolves cleanly (ragas 0.4.3, numpy 2.5.3, pandas 3.0.6, langchain-core 1.6.7...) - **no resolver conflict at install level** (not a runtime test).

Install alternatives: `pip install -r requirements.txt` from the repo pins **`litellm==1.97.0`**, `pypdfium2==5.13.0`, `PyPDF2==3.0.1`, `python-dotenv==1.2.2` (and leaves `pymupdf` commented out as optional). Do not mix: issue #286 showed a `litellm`/`python-dotenv` pin conflict in the past. For our app, prefer the PyPI package + our own lock file.

### 2.2 Windows long-path failure (VERIFIED, reproduced)
`pip install pageindex` in a venv at `...\scratchpad\pi-venv` (135-char prefix) died with `OSError [Errno 2] ... litellm\proxy\guardrails\guardrail_hooks\litellm_content_filter\categories\prompt_injection_data_exfiltration.yaml` and pip's long-path hint. Measured on the installed tree: the longest `litellm` file path relative to `site-packages` is **133 chars** (a compiled `.pyc` under `litellm\proxy\pass_through_endpoints\llm_provider_handlers\__pycache__\`; the longest *source* file is ~110 chars, e.g. `...\litellm_content_filter\categories\prompt_injection_data_exfiltration.yaml`). With `\Lib\site-packages\` (19 chars) the **venv root must stay <= ~105 chars** (260 limit minus 19 minus 133, with a little margin) unless Windows `LongPathsEnabled` is on. I did not change any system setting; a directory junction does not help because Python resolves it to the real long path. Our project folder (`C:\Users\ayush\Annual Report Answering\.venv` = 44 chars) is far below the limit; the same applies to any CI/runner checkout path.

### 2.3 AES-encrypted PDFs need pycryptodome (VERIFIED, reproduced; GitHub #426 open)
`LocalAPI._extract_page_texts` opens the PDF with **PyPDF2**. For a PDF that opens fine in any viewer but uses AES permission-encryption with an empty user password: pdfium opens it; `page_index_flash` works (PyPDF2 use inside flash is guarded); but `client.submit_document()` fails with `PageIndexAPIError: Failed to submit document: could not read PDF: PyCryptodome is required for AES algorithm`. After `pip install pycryptodome` the same file indexes. (`cryptography` being installed does **not** help - PyPDF2 3.0.1 imports `Crypto`.) If we override page-text extraction with pdfium (Section 9.3) this stops mattering, but keep `pycryptodome` anyway for the PyPDF2 calls inside the flash parser and token counting.

### 2.4 Environment variables (VERIFIED in `utils.py`, `client.py`)
| Var | Meaning |
|---|---|
| `OPENAI_API_KEY` | **The** key for local mode (both indexing and chat; LiteLLM and the OpenAI SDK read it) |
| `OPENAI_BASE_URL` | honoured (OpenAI SDK + LiteLLM) - I used it to point the whole stack at a localhost mock server; also the route to Azure/proxies/OpenAI-compatible servers (use `openai/<slashed-id>` for ids containing `/`) |
| `CHATGPT_API_KEY` | **deprecated alias**: if `OPENAI_API_KEY` is unset it is copied across and a `FutureWarning` is raised |
| `PAGEINDEX_API_KEY` | cloud mode only |
| `.env` | `load_dotenv(find_dotenv(usecwd=True))` runs at `import pageindex.utils` - a `.env` in the **current working directory** (or parents) is loaded automatically |
| `LITELLM_LOG` | default `ERROR` (set by `_quiet_litellm`) |
| `LITELLM_LOCAL_MODEL_COST_MAP` | defaulted to `True` (offline cost map bundled with the installed litellm; contains gpt-5.6-luna/terra/sol: VERIFIED $0.2/$1.2, $2/$12, $4/$20 per 1M, 922k max input, 128k max output, endpoints chat+responses+batch) |

### 2.5 Default models (VERIFIED `utils.py` + `config.yaml`)
`DEFAULT_INDEX_MODEL = "gpt-5.6-luna"`, `DEFAULT_CHAT_MODEL = "gpt-5.6-sol"`. `PageIndexClient()` with no args -> index luna, chat sol. Resolution rules (`_resolve_models`): new names win over old, specific over general, `model=` sets every role; five names are accepted (`model, summary_model, retrieve_model, index_model, chat_model`). `summary_model` falls back to `index_model` -> `model` -> luna. README/docs advice: `index=` "a basic model is sufficient", `chat=` "use the best model you can afford". OpenAI model pages (fetched today): luna "cost-sensitive, high-volume" $0.2/$0.02(cached)/$1.2; terra "balance intelligence and cost" $2/$0.2/$12; sol flagship $4/$0.4/$20; all 1.05M context, 128k max output, reasoning effort none/low/medium(default)/high/xhigh/max. See note 07 for GPT-6 and deprecations.

---

## 3. Configuration reference

### 3.1 `pageindex/config.yaml` (VERIFIED; these are *standard-mode* knobs; flash ignores them and the CLI rejects them with `--mode flash`)
```yaml
# index_model: "gpt-5.6-luna"     # commented out: SDK default applies
# chat_model:  "gpt-5.6-sol"
toc_check_page_num: 20            # how many leading pages to scan for a table of contents (continues while pages keep being TOC)
max_page_num_each_node: 10        # a leaf is split further if it spans MORE than this many pages ...
max_token_num_each_node: 20000    # ... AND has >= this many tokens
if_add_node_id: "yes"
if_add_node_summary: "yes"
if_add_doc_description: "no"
if_add_node_text: "no"
```
`ConfigLoader().load(dict|SimpleNamespace|None)` merges user values over this file; unknown keys raise `ValueError("Unknown config keys")`.

### 3.2 `PageIndexClient.__init__` (exact, VERIFIED)
```python
PageIndexClient(api_key=None, *, index=None, chat=None, mode=None,
                index_model=None, chat_model=None, model=None, summary_model=None,
                summary_max_words=None, summary_concurrency=None, use_embedded_toc=None, optimize=None,
                retrieve_model=None, storage_path=None, index_backend=None, chat_backend=None,
                instructions=None)
PageIndexLocalClient(*, <same minus api_key/mode>)     # pins local; PageIndexCloudClient pins cloud
```
* **Two spellings per side, never mixed** (VERIFIED error): `index="gpt-5.6-luna"` (string slot) **cannot be combined with `storage_path=`** -> `PageIndexAPIError: index= and the flat index-side arguments (storage_path) are two spellings of the same thing`. Use either `index={"model": "gpt-5.6-luna", "storage_path": "D:/store"}` or flat `index_model="gpt-5.6-luna", storage_path=...`.
* `index` dict keys (local): `model, summary_model, summary_max_words, summary_concurrency, use_embedded_toc, optimize ("full"|"merge"|"off"), backend, storage_path`. `chat` dict keys: `model, backend`. `backend` = LiteLLM connection kwargs (`api_key`, `api_base`/`base_url`, `api_version`, `aws_*`...) passed verbatim.
* `storage_path` default `./.pageindex` (relative to cwd!). `instructions=` = standing system-prompt addendum for every chat call.
* `summary_max_words` default 150, `summary_concurrency` default 64 (flash only; refused with `mode="standard"`), `use_embedded_toc` default True, `optimize` default `"full"`.
* Setting `chat=` makes the answering agent run **in your process on your key** (always, in local mode). `client.chat_model` / `client.retrieve_model` are settable properties.
* `PageIndexAPIError(message, status_code=None)` is the single exception type (`pageindex.errors`).

### 3.3 Flash / tree-optimize constants (VERIFIED source)
| Constant | Value | Where |
|---|---|---|
| `SUMMARY_CONCURRENCY` | 64 simultaneous summary calls | `utils.py` |
| `EXPAND_CONCURRENCY` | 32 simultaneous expand calls (lanes overlap -> up to 96 in flight) | `tree_optimize.py` |
| `SUMMARY_RAW_TEXT_TOKENS` | 200 - a leaf with fewer tokens **reuses its raw text as its summary (no LLM call)** | `utils.py` |
| `SUMMARY_INTRO_MAX_PAGES` | 3 pages of a parent's "intro" text fed to the parent summary | `utils.py` |
| `SUMMARY_MAX_WORDS` | 150 | `utils.py` |
| `TRIGGER_PAGES` | 5 - expand looks only at collapsed nodes spanning > 5 pages | `tree_optimize.py` |
| `PAGE_CHARS` | 6000 chars/page handed to the expand model | `tree_optimize.py` |
| `max_rounds` / `empty_retries` | 3 merge+expand rounds / 1 retry when expand returns no children | `tree_optimize.optimize` |
| `FLAT_TREE_MAX_NODES` | 10 - a "one node per page" fallback tree larger than this is refused | `flash/api.py` |
| `_MIN_PARALLEL_PAGES` | 64 - below this pages are parsed sequentially; above it a spawn process pool of `cpu_count-1` | `flash/parser_pdfium_parallel.py` |
| `TOOL_RESPONSE_CHAR_LIMIT` / `_CHAR_BUDGET` | 100,000 / 95,000 chars per tool response | `agent_tools.py` |
| `STRUCTURE_FIRST_PAGE_THRESHOLD` | 20 pages: above it the agent is told to read the structure first | `agent_tools.py` |
| max agent turns | 10 (openai-agents `DEFAULT_MAX_TURNS`, VERIFIED 0.20.0); override `chat(max_turns=N)` | `local_chat.py` |
| LLM retry ladder | 10 attempts, fixed 1 s sleep, `max_retries=0` to litellm, `drop_params=True`; **no retry on 400/401/403/404**; all-10-failed raises `LLMRetriesExhausted` | `utils.llm_acompletion` |

### 3.4 CLI (`run_pageindex.py`, VERIFIED by running it)
```text
python run_pageindex.py --pdf_path X.pdf [--mode flash|standard (default flash)] [--no-summary] [--optimize [full|merge|off]]
        [--embedded-toc/--no-embedded-toc] [--index-model M] [--model M(legacy)] [--summary-model M]
        [--summary-max-words N] [--summary-concurrency N]
        # standard-mode only: --toc-check-pages N --max-pages-per-node N --max-tokens-per-node N
        #                     --if-add-node-id/-summary/-doc-description/-text yes|no
python run_pageindex.py --md_path X.md [--if-thinning yes] [--thinning-threshold 5000] [--summary-token-threshold 200]
```
Writes `./results/<pdf-stem>_structure.json` (`{"doc_name","doc_title","structure":[...],"has_abstract_or_references_section","toc_source", "optimize"?}`). `--no-summary --optimize off` needs **no LLM and no key** (ran it on `q1-fy25-earnings.pdf`: 13 nodes, `toc_source: detected`). The CLI does **not** build the SDK store, produce `doc.json`/`pages.json`, or chat - it is only a structure dumper. `--flash` is a hidden legacy alias.

---

## 4. Programmatic API (what our code calls)

All methods are **synchronous**. Internally they use `asyncio.run` through `utils.run_off_loop`, which moves the work to a worker thread if a loop is already running - so they are safe to call from async code but **block** the caller; wrap in `asyncio.to_thread(...)` / a threadpool in FastAPI. `submit_document` accepts **file paths only** (`os.path.isfile`); `page_index_flash` and `page_index_main` also accept `io.BytesIO`.

```python
from pageindex import PageIndexClient            # also: PageIndexLocalClient, PageIndexCloudClient, PageIndexAPIError
client = PageIndexClient(index={"model": "gpt-5.6-luna", "storage_path": r"D:\data\s1\.pageindex"},
                         chat="gpt-5.6-sol")      # needs OPENAI_API_KEY in env/.env
res = client.submit_document(r"D:\uploads\annual-report.pdf")       # -> {"doc_id": "pi-<32hex>", "name": "annual-report.pdf"}
doc_id = res["doc_id"]
answer = client.chat("What was group operating profit?", doc_id=doc_id, citations=True, protocol="responses")  # see 8.4
```

| Method | Signature (VERIFIED) | Returns |
|---|---|---|
| `submit_document` | `(file_path, mode=None, beta_headers=None, folder_id=None, metadata=None, wait=False)` | `{"doc_id","name"}`. Local: **indexes synchronously in this call** (mode `None`/`"flash"` default, `"standard"` for the old LLM-built tree). `beta_headers`/`folder_id` raise locally. `metadata` = JSON-able dict stored in `doc.json`. A taken name gets `_1.._99` appended + `UserWarning`. Raises `PageIndexAPIError` for: not a `.pdf`, unreadable PDF, **all pages blank** ("run OCR"), flash found no layout structure for >10 pages ("try mode='standard'"), tree page spans outside PyPDF2's page count. |
| `get_document` | `(doc_id)` | `{"id","name","description","status":"completed","createdAt" (naive UTC iso),"pageNum","folderId":None,"metadata"}` |
| `get_tree` | `(doc_id, node_summary=False, include_text=True)` | `{"doc_id","status":"completed","retrieval_ready":True,"result":[nodes],"metadata","features":{}}`; `include_text=True` re-slices page text into nodes (see 6.3) |
| `get_document_structure` | `(doc_id)` | `list[node]` with summaries, no text (= `get_tree(..., node_summary=True, include_text=False)["result"]`) |
| `get_page_content` | `(doc_id, pages: str)` pages like `"5-7"`, `"3,8"`, `"12"` | `list[{"page_index": int, "markdown": str}]` - **the key is called `markdown` but it is plain PyPDF2 text** (no markdown, no tables). Whitespace around `,`/`-` tolerated here (not in the tool layer). |
| `get_ocr` | `(doc_id, format="page"\|"node"\|"raw")` | local "OCR" = text captured at index time |
| `is_retrieval_ready` | `(doc_id)` | bool |
| `list_documents` | `(limit=50, offset=0, folder_id=None, recursive=False)` | `{"documents":[...], "total","limit","offset"}`, newest first |
| `get_document_id` / `delete_document` | `(name)` / `(doc_id)` | id / `{"message": "Document deleted successfully."}` |
| `chat` | `(messages, *, doc_id=None, stream=False, model=None, reasoning_effort=None, show_process=None, folder_id=None, protocol=None, instructions=None, citations=False, max_turns=None, backend=None, extra_headers=None, extra_body=None)` | answer lane: `str` (or `ChatStream` if `stream=True`); `protocol="chat_completions"` -> OpenAI-style dict (`choices`,`usage`); `protocol="responses"` -> dict with `output`, `items`, `usage`; `protocol="messages"` -> Anthropic |
| `chat_completions` | legacy twin of `chat(protocol="chat_completions")` | dict / iterators |
| `citation_prompt` | `(format="cite"\|"markdown")` | the frozen citation guidance text (Section 8.2) |
| `get_citations` / `resolve_citations` | `(answer, doc_id=None)` | list of `{"document","doc_id","page"[, "block_id", ...]}` / `{"answer": text with [[1]](#pageindex-citation-01) links, "citations":[{anchor,index,...}]}` |
| agent plumbing | `agent_tools(include_management=False)`, `as_openai_tools(include_management=False, hosted=False)`, `openai_agent_config(*, include_management=False, model=None, model_settings=None, name="PageIndex")`, `agent_instructions(*, include_management=False)`, `document_context(doc_id)`, `as_anthropic_tools`, `anthropic_runner_config`, `as_claude_mcp`, `claude_agent_config` | for building **our own agent** with the same tools |
| tree helpers | `from pageindex.utils import get_node, get_node_path, get_node_parent, get_node_map, create_node_mapping(tree, include_page_ranges=False, max_page=None), print_tree, remove_fields` | pass a node **list** (not the whole `get_tree` envelope) |
| low-level | `from pageindex.flash import page_index_flash`; `from pageindex import page_index, page_index_main, optimize_tree, md_to_tree, highlight_region` (lazy attrs) | see Sections 5, 7 |

`page_index_flash` exact signature: `page_index_flash(pdf, summary=True, summary_model=None, optimize=None, optimize_expand=None, optimize_model=None, summary_concurrency=None, use_embedded_toc=True, summary_max_words=None) -> dict` (`pdf` = `str | Path | io.BytesIO`; `optimize` = `"full"|"merge"|False`).
`page_index(doc, model=None, toc_check_page_num=None, max_page_num_each_node=None, max_token_num_each_node=None, if_add_node_id=None, if_add_node_summary=None, if_add_doc_description=None, if_add_node_text=None)` -> standard mode (`asyncio.run` inside: **cannot be called from a running loop**, writes a JSON log into `./logs/`).

**Multi-turn:** `chat` is stateless - pass the whole visible history as `messages=[{"role":"user",...},{"role":"assistant",...},...]` (answers appended as plain text; do **not** append a `show_process` text stream). Keep `doc_id` and `protocol` constant across a conversation (the doc-targeting block is part of the cached prefix). `system` rows join the managed prompt; `instructions=` appends after it.

**Doc scoping:** `chat(doc_id=...)` is enforced **in the tool layer** locally (`_allowed_ids`), not just in the prompt: the agent cannot read another document in the same store.

---

## 5. CLI vs SDK vs low-level functions - which entry point for what

| Need | Use | LLM? |
|---|---|---|
| Tree JSON only (debugging, offline tests) | `page_index_flash(pdf, summary=False, optimize=False)` or CLI `--no-summary --optimize off` | none |
| Tree with deterministic merge, no summaries | `page_index_flash(pdf, summary=False, optimize="merge")` | none |
| Production index of our PDF (tree + summaries + description + page store) | `PageIndexClient.submit_document(path)` | luna: ~180 calls / 222 pp |
| Old LLM-built tree (no layout structure, TOC-driven) | `submit_document(path, mode="standard")` / `page_index_main` | many sequential calls (Section 7.2) |
| Q&A | `PageIndexClient.chat(...)` (agent) or our own loop over `get_document_structure` + `get_page_content` | see Section 8 |

---

## 6. The tree JSON schema and page numbering (exact)

### 6.1 `page_index_flash` result (what the CLI writes)
```jsonc
{
  "doc_name": "2023-annual-report.pdf",          // file name, or "document.pdf" for a BytesIO input
  "doc_title": "Annual Report of the Board of Governors of the Federal Reserve System",   // may be null
  "structure": [ /* nodes, below */ ],
  "has_abstract_or_references_section": false,
  "toc_source": "bookmarks",                     // "detected" | "bookmarks" | "hybrid" | "pages" | "unreadable"
  "optimize": { "merges": 41, "expands": 0, "same_page_merges": 16, "same_page_dropped": 26,
                "kept_collapsed": 0, "before": {...search-cost metrics...}, "after": {...} }   // only when optimize is on
}
```
`toc_source`: `detected` (layout), `bookmarks` (embedded PDF outline), `hybrid` (bookmarks frame + detected sections grafted in), `pages` (no hierarchy: one node "Page N" per page; refused by the client when > 10 pages), `unreadable` (no text layer; `structure` empty; client says "run OCR").

### 6.2 Node schema (VERIFIED from a real run; `get_tree()["result"]` has the same fields in local and cloud since 0.2.21 / commit #541)
```jsonc
{
  "title": "3  Financial Stability",
  "node_id": "0014",            // 4-digit zero-padded STRING, pre-order (document order) numbering, re-written by write_node_id() at index time
  "start_index": 21,            // 1-based PHYSICAL page, inclusive
  "end_index": 31,              // inclusive; a parent's range COVERS ITS WHOLE SUBTREE ("union semantics")
  "summary": "...",             // present when summaries ran; a parent's summary describes its whole subtree
  "key_items": ["Financial Stability Oversight Council Activities", "Financial Stability Board Activities"],
                                // only on nodes whose subsections were merged away: titles kept as routing info
  "text": "...",                // only from get_tree(include_text=True); stripped from the stored tree
  "nodes": [ /* children, only when non-empty */ ]
}
```
There is **no `prefix_summary`/`page_index` any more** (2025-era cloud names); `unify_tree()` maps them to `summary`/`start_index` if a cloud server still sends them. (Docs page `/sdk/documents` still shows the old field names - the docs lag the code; VERIFIED by running.)

Structural rules (VERIFIED in `tree_optimize.py`, `flash/api.py`, run output):
* **Preface:** a hierarchy that starts after page 1 gets a first node `Preface` (`start_index` 1), running onto the first section's page unless that heading opens its page.
* **Intro node:** a parent whose first child starts on a *later* page gets a first child titled `"<parent title> (intro)"` holding the opening pages (no intro when the first child starts on the parent's own page).
* **Shared boundary pages:** a section "runs onto the page where the next entry starts" - siblings routinely share one page (e.g. `0004` 9-21 contains `0005` 9-15 and `0010` 15-21). When fetching "pages of node X" **de-duplicate pages across nodes**.
* Siblings covering identical pages are fused into one node titled `"A; B"` (`merge_same_page`), originals in `key_items`; a leaf summary call may rewrite that title.
* After optimise, ids are renumbered flat `0000, 0001, ...` in document order (expansion mints `0266.1` provenance ids first).
* `own_pages(node)` (utils) defines each node's *own* text (a parent's text = pages no child holds, possibly sharing its first child's start page); used by `get_tree(include_text=True)`.

### 6.3 Page numbering - exactly what to cite (VERIFIED)
* **Everything is the PDF's physical page order, 1-based**: tree `start_index`/`end_index`, `get_page_content` `page_index`, `<cite page="N">`, `pages` argument of the tools. The standard pipeline marks pages `<physical_index_N>` in LLM prompts (`<physical_index_N>\n{page text}\n<physical_index_N>`) and converts back with `convert_physical_index_to_int`; the flash pipeline has no tags (it is not LLM-driven).
* **Printed (folio) page numbers are never used.** Evidence on the Fed report: physical p10 starts "Recent Economic and Financial Developments" and ends with footer "4 110th Annual Report | 2023"; p11 ends "Monetary Policy and Economic Developments 5". Note 03 measured `physical = printed + 6` on 208/222 pages. The standard pipeline's offset logic (`calculate_page_offset` = most common `physical_index - page` over matched TOC entries) exists only to translate a printed TOC into physical pages.
* PDF.js (`pdfViewer.currentPageNumber`) is also physical 1-based => `<cite page="10">` opens page 10 with no conversion. Show the printed label separately if we want it (derive from footer, note 03).
* `LocalAPI._check_page_bounds` rejects trees referencing pages outside PyPDF2's page count (tree is pdfium, pages are PyPDF2).

### 6.4 What the local store writes (VERIFIED by running a mocked-LLM index of the 222-page Fed report)
`<storage_path>/` (default `./.pageindex`):
```
manifest.json                         # {"docs": {doc_id: <doc.json content>}}  - cache, rebuilt from docs/ if stale
docs/pi-<uuid4 hex>/doc.json          # {"id","name","description","status":"completed","createdAt","pageNum":222,"folderId":null,"metadata":null,"mode":"flash"}   (259 B)
docs/pi-<uuid4 hex>/tree.json         # list of nodes WITHOUT text (64 KB with ~100-char mock summaries; ~125-280 KB with real 60-150 word summaries, see sizing in 7.3)
docs/pi-<uuid4 hex>/pages.json        # [{"page_index": 1, "markdown": "<PyPDF2 text>"}, ...]   (565 KB for 222 pages)
```
**The original PDF is not copied into the store** - our app must keep its own copy for the viewer panel. Writes are atomic (`tmp` + `os.replace`). `toc_source`, `doc_title` and the `optimize` stats are **not persisted** (only `structure` + `description`). Reads tolerate corrupt JSON (treated as absent).
`DocStore.lock()` uses `fcntl` -> **no-op on Windows** (VERIFIED in source): concurrent `submit_document` calls into one store can race on name-uniquing and `manifest.json`. => **one `storage_path` per chat session** (also makes "delete session" = `rmtree`, and removes the `name_1` suffix logic).
Document **names are sanitised** (`naming.sanitize_filename`: `<>:"/\|?*` and control chars -> `_`, trailing dots/spaces stripped, reserved Windows names prefixed, <=180 UTF-8 bytes) and the agent must copy `doc_name` verbatim into every tool call -> store the upload under a simple ASCII name (e.g. `annual-report.pdf`) and show the original file name in our UI. `<cite doc="...">` carries this stored name.

---

## 7. Indexing pipelines: stages, LLM calls, concurrency, retries, cost, latency

### 7.1 PageIndex Flash (default; `flash/main.py::extract_toc` + `flash/api.py::page_index_flash`)
Order (VERIFIED from `extract_toc` docstring and code):
1. **Parse** with pypdfium2 at character level into spans (`parser_pdfium_charlevel/`), per-page viewport + `/Rotate`; docs >= 64 pages parsed in a **spawn `ProcessPoolExecutor`** (workers = `cpu_count-1`; falls back to sequential on any worker error; result identical).
2. Cluster spans into lines; page stats; **column detection** and re-cluster; remove line-number artefacts; document-level style stats; cluster lines into blocks + reading order.
3. **Classify** blocks: headers/footers, watermarks, TOC-like pages & boilerplate, captions, references, body paragraphs.
4. **Title** detection; **heading candidate** collection (`heading_detection/`); **outline assembly + validity gates** (a structured outline must cover enough chapters; an unstructured one is dropped if its max heading gap > 85% of the doc).
5. **Embedded bookmarks** (`use_embedded_toc=True`, `embedded_toc.py`): classify the outline as FULL (becomes the frame; detected sections grafted in), SKELETON (chapter frame; detected nodes re-hung; garbled titles repaired from bookmark strings), or IGNORE (garbage/enumeration -> detection stands). A bookmark target has no on-page position, so a section runs onto the next entry's page.
6. `page_index_flash` then: no hierarchy -> "Page N" fallback (or `unreadable`); hierarchy starting after p1 -> `Preface`; add **intro nodes**.
7. **Optimise** (`tree_optimize.optimize`, `optimize="full"`): rounds (<=3) of
   * `merge_same_page` + `merge` - **deterministic, free**: collapse any subtree whose structure does not beat a linear scan (cost model: routing cost R=1 page; `S(v)` pages to scan; `tree_cost`; merge iff `S(v) <= tree_cost(v)`), keeping removed titles in `key_items`;
   * `expand` - for any *collapsed* node spanning > 5 pages, **one LLM call** (prompt below, up to 2 attempts if the model returns nothing) proposes subsection headings printed in the pages; accepted only if each heading string is actually found on that page and the priced `expand_cost < collapse_cost`; children are attached with an intro node, then re-checked.
   Reports `before`/`after` worst-case and average search complexity in the `optimize` key.
8. **Summaries** run on the same event loop, overlapped with expand: a node is summarised as soon as expand can no longer change it (`SummaryScheduler`, deepest nodes first; leaves under 200 tokens reuse raw text; parents are composed from child summaries + <=3 intro pages). Prompts (verbatim, `utils.py`): leaf = "You are given a text chunk from a document... generate a concise description of everything that is covered... within {150} words ... Reply strictly in the following JSON format: {"summary": ...}"; parent = "...the text that opens the section (possibly empty) and the titles and summaries of its subsections...". Fallback if the model returns nothing: parent -> `"; ".join(child titles)`, leaf -> own text[:600].
9. `LocalAPI._index_flash` then calls `write_node_id`, builds the **doc description** (1 LLM call over the whole summarised tree; a 400/context error yields an empty description instead of failing) and stores tree + PyPDF2 pages + meta.

**Expand prompt (verbatim, `tree_optimize.py`):**
```text
You are splitting an over-long section of a PDF into its subsections.
Section title: {title}   Pages: {start}-{end}
{<page_N>...first 6000 chars of each page...</page_N>}
List the subsection headings that BEGIN within these pages, in document order, each with the page number it begins on. Rules:
- Use only headings printed in the document. Never invent or paraphrase one.
- A running header, a table column label, a table row label, or a cross-reference is not a subsection heading.
- If this section is continuous prose, or a single table spanning the pages, return an empty list. That is a valid and expected answer.
- Do not include the section's own title.
Reply with JSON only: {"subsections": [{"title": "<verbatim heading>", "page": <int>}]}
```

**LLM calls per page?** Not per page: per *node*. Measured (mock LLM, same 222-page Fed report, optimize="full"): **142 leaf summaries + 34 parent summaries + 2 expand + 1 description = 179 calls, ~349K prompt tokens** (my tiktoken count; includes ~72K for 2 big expand prompts). The repo's own benchmark for the same document (`pageindex/flash/README.md`): **281K input / 137K output tokens**. The large output count (137K for ~210 summary-sized replies) is most likely hidden reasoning tokens: gpt-5.6 defaults to reasoning effort "medium" (OpenAI model page) and `llm_acompletion` sends no reasoning setting (VERIFIED in source; the "reasoning tokens explain it" part is INFERRED).

**Retry / failure behaviour (VERIFIED code):** each call: 10 attempts x 1 s fixed sleep (no exponential backoff/jitter) with `max_retries=0` to litellm. 400/401/403/404 raise immediately. **A call that exhausts its 10 attempts on 429/5xx raises `LLMRetriesExhausted`, which `_is_unrecoverable` treats as fatal (status != 400) -> the whole `submit_document` aborts** with `PageIndexAPIError("Failed to submit document: ...")`. A per-node empty/failed summary otherwise degrades to the fallback text. If *no* summary call ever succeeded -> `RuntimeError("Summary generation failed for all nodes")`. Mitigation for small OpenAI tiers: lower `index={"summary_concurrency": 8..16}` and retry `submit_document` once at app level. (GitHub #283 reported the 429 storm before the 64-cap/priority gate existed.)

**Concurrency model:** asyncio throughout the LLM lane (`asyncio.gather`, `Semaphore(<=32)` for expand, a custom `_PriorityGate(64)` for summaries); the PDF parse is multi-process; everything is wrapped by `asyncio.run` in a thread -> blocking from the caller's view.

### 7.2 Standard mode (`mode="standard"`; `page_index_classic.py`) - the old LLM pipeline, kept for PDFs Flash refuses
VERIFIED stages and call shapes (not run - needs an LLM):
1. Page text + token counts via **PyPDF2** (`get_page_tokens`, `pdf_parser="PyMuPDF"` is an optional AGPL path - unused by us).
2. **TOC detection:** one *sequential* `toc_detector_single_page` LLM call per page from page 1 up to `toc_check_page_num` (20), continuing past it while pages keep being TOC pages.
3. **TOC extraction** + `detect_page_index` (does the TOC print page numbers?). Branches:
   * TOC **with** page numbers (`process_toc_with_page_numbers`): `toc_transformer` (TOC text -> JSON; continuation loop **capped at 5** + a completeness-check call each time - the old unbounded loop was GitHub #174), `toc_index_extractor` over the next 20 pages with `<physical_index_X>` tags, `extract_matching_page_pairs` -> `calculate_page_offset` (mode of `physical - printed`), `add_page_offset_to_toc_json`, `process_none_page_numbers` (one call per entry with no page).
   * TOC without page numbers: `process_toc_no_page_numbers` (chunks of <=20k tokens, `add_page_number_to_toc`).
   * **No TOC:** `process_no_toc`: `<physical_index_N>`-tagged pages grouped into <=20,000-token chunks (`page_list_to_group_text`, 1 page overlap) -> `generate_toc_init` then sequential `generate_toc_continue` (e.g. ~14 sequential calls for 300 text-dense pages).
4. **Verification** `verify_toc`: one concurrent **unthrottled** `asyncio.gather` call per TOC entry (`check_title_appearance`: "does the section start on that page?"); accuracy == 1.0 -> done; > 0.6 -> `fix_incorrect_toc_with_retries` (<=3 attempts; per bad entry a "find the page" call + a re-check); else fall back mode: with-page-numbers -> without -> no-TOC -> `raise Exception('Processing failed')`.
5. `check_title_appearance_in_start_concurrent` (1 call per node: does the section start at the top of the page? -> decides whether `end_index = next.start - 1` or `next.start`), `post_processing` -> `list_to_tree`.
6. **Large-node splitting** `process_large_node_recursively`: a leaf with `end-start > max_page_num_each_node (10)` **and** `>= max_token_num_each_node (20000)` tokens is re-run through `process_no_toc` on its own pages (and recursed).
7. `merge_tree`, ids, `summarize_tree` (same scheduler as flash), optional doc description.
Prompt hardening in this path: PDF text is wrapped in `<user_document>` tags and a regex redacts phrases like "ignore previous instructions" (`_sanitize_doc_text`). Known open bugs here: #467 (empty first chunk), #153 (`int + NoneType` on a TOC-with-pages doc), #340/#341 (no-TOC titles = whole sentences; same-page siblings get identical summaries). **Recommendation: Flash first; standard only as a manual fallback.** `page_index_main` prints progress to stdout and writes `./logs/<name>_<timestamp>.json` (opened without an encoding argument, but `json.dump` is ASCII-escaped so it is safe on cp1252).

### 7.3 Cost + latency estimate for a 308-page annual report (default models)
Basis: README benchmark table (`pageindex/flash/README.md`, "each run end to end with tree optimization") + README claim "$0.0011 per page with gpt-5.6-luna" + my 222-page measurements. luna = $0.2 in / $1.2 out per 1M (VERIFIED on OpenAI's model page today).

| Document (README) | Pages | Input tok | Output tok | Cost @ luna | $/page |
|---|---:|---:|---:|---:|---:|
| Federal Reserve 2023 report | 222 | 280,975 | 136,982 | **$0.221** | 0.00099 |
| 9/11 Commission Report | 585 | 720,624 | 200,202 | $0.384 | 0.00066 |
| PRML | 758 | 857,983 | 277,675 | $0.505 | 0.00067 |
| Machine Learning: A Probabilistic Perspective | 1,098 | 1,587,265 | 646,958 | $1.094 | 0.00100 |

* **308 pages (National Grid-sized), luna:** ~370K in + ~170K out => **~$0.28 (plausible range $0.20-$0.45)**; with `gpt-5.6-terra` as index model x10 (~$3), with `sol` x20 (~$6). INFERRED (interpolation, text density dominates).
* **Latency:** their chart fits "218 s / 1000 pages" (R^2 0.924); Fed 222 pp ~ 57 s end-to-end. => 308 pp ~ **65-95 s**, up to ~2-3 min if the account is rate-limited. My Windows run (12 cores): PDF layout parse 18-22 s for 222 pp; whole `submit_document` with **zero-latency mocked LLM** 28-43 s (parse + local tree work + disk writes), so real LLM latency adds the rest. INFERRED.
* **What makes it slow/expensive:** (1) the CPU-bound pdfium layout pass (process-pool start-up per worker imports the whole package; ~64+ pages to engage); (2) ~1 summary call per node with up to 64 in flight - input tokens dominated by long leaves (a leaf is summarised from *all* its pages, up to ~10 pages / 20k tokens); (3) reasoning-token output at "medium" effort; (4) expand prompts carry up to 6000 chars x pages of a big node (two prompts = 72K tokens here); (5) the description prompt contains the whole summarised tree (22.7K tokens with 100-char mock summaries; 40K+ with real ones); (6) fixed 1 s retry sleeps under 429.
* **Query-time structure payload** (matters for per-question cost): I rebuilt the 222-page tree's `get_document_structure` payload with synthetic summaries of 60/100/150 words (177 nodes): **126K / 194K / 277K chars = ~18.8K / 25.8K / 34.7K tokens, delivered in 2 / 3 / 4 tool-call parts** (95,000-char budget per part). A 308-page report will be ~1.4x that. INFERRED for real summaries.

---

## 8. Retrieval and answering in the repo

### 8.1 Current design (VERIFIED by reading and by running against a mock OpenAI server)
`client.chat()` -> `local_chat.py` builds an **OpenAI Agents SDK `Agent`** (`name="PageIndex"`, tools = the 4 read tools exposed as an in-process MCP server, model = LiteLLM `openai/<chat_model>` on the default lane, or `OpenAIResponsesModel` on `protocol="responses"`), runs `Runner.run`/`run_streamed(..., max_turns, RunConfig(tracing_disabled=True))`. The agent decides everything: it is *tree search by tool use*.

Request actually sent (captured; `model: "gpt-5.6-sol"`, `prompt_cache_key: "pageindex-<hash>"`, 4 function tools, `strict: false`):

**System message (verbatim):**
```text
You are PageIndex by Vectify AI, a document-focused assistant. Be concise, never use emojis, and do not expose tool names.

PageIndex by Vectify AI is a document platform for uploading and managing long PDFs (research papers, financial reports, legal docs, textbooks, etc.).

READING WORKFLOW:
- For documents over 20 pages: call get_document_structure() first to locate relevant sections, then get_page_content() with targeted page ranges.
- For small documents (20 pages or fewer): call get_page_content() directly.

TOOL USAGE RULES:
- Invoke a tool only when all required parameters are present or clearly inferable. Never invent placeholder values.
- If a tool returns an error, present the provided next_steps/options to the user instead of retrying blindly.

DOCUMENT DISCOVERY:
- browse_documents() — DEFAULT discovery tool, first choice for any document-related question. ...
DECISION: ... PERSISTENCE (before concluding the target document is not in the library): ... (several paragraphs about paging the whole library)

[only when citations=True, appended:]
GROUNDING
- Answer only from the user's PageIndex documents. Call get_page_content() and state only what was actually read there.
- Never fill a gap from general knowledge. When the documents do not answer the question, say so.

CITATIONS
- Cite only statements supported by tool outputs: <cite doc="{docName}" page="{pageNumber}"/> or <cite doc="{docName}" page="{pageNumber}" block="{blockId}"/>. Place immediately after the claim.
- When page content includes block_id values, citations MUST be block-level ... (dormant locally: no block ids)
- For a claim drawn from multiple blocks on one page, add one tag per supporting block (at most 3) ...
- Each tag must reference a SINGLE page integer. For multi-page citations, use separate tags.
```
(`CHAT_HEADER` + `AGENT_INSTRUCTIONS` + `LOCAL_CITATION_PROMPTS["cite"]`, then `instructions=`/client `instructions`. Full text of the discovery/persistence blocks is in note 03 section 2.5.)

**First user message (doc targeting, `doc_targeting_block`, verbatim shape):**
```text
The user has specified document: 2023-annual-report.pdf
Document metadata: {"id": "pi-659f...", "name": "2023-annual-report.pdf", "description": "...", "status": "completed", "createdAt": "...", "pageNum": 222, "folderId": null, "metadata": null}
Use this document's name to retrieve its content with get_document_structure() and get_page_content().
```
then the conversation history, then the new question.

**Tools (names + argument schemas, VERIFIED `TOOL_CONTRACT`, local variant hides folder args):**
* `browse_documents(offset=0, limit=10)` -> `{"success":true,"documents":[{name,description,status,created_at,metadata?}],"sort","next_offset","has_more","folders":[],"next_steps":{...}}`
* `get_document(doc_name, wait_for_completion=False)` -> `{"success":true,"name","description","status","created_at","page_count","folder_id","metadata?","next_steps":{...}}`
* `get_document_structure(doc_name, part=1, wait_for_completion=False)` -> `{"success":true,"doc_name","structure":[nodes with title,node_id,start_index,end_index,summary,nodes (no text)]}`; when > 95,000 chars: `{"total_parts":N,"structure":<chunk>,"pagination":{"part","total_parts","has_more"}}` and the model must iterate `part`.
* `get_page_content(doc_name, pages)` with `pages` matching `^(\d+(-\d+)?)(,\s*\d+(-\d+)?)*$` -> `{"success":true,"doc_name","total_pages","requested_pages","returned_pages","content":[{"page":10,"text":"..."}],"next_steps":{...}}`; pages beyond the 95,000-char budget are listed in `next_steps` ("For remaining pages, request: ..."); out-of-range pages reported; at most 10,000 pages per spec.
* `remove_document(doc_names)` only with `include_management=True` (never exposed by `chat`).
Every tool returns JSON text (a `{"type":"text","text":"<json>"}` MCP block); errors are `{"error", "errorCode", "next_steps"}`, never exceptions.

**Observed loop (mock run, 3 model turns):** `get_document_structure(doc_name)` -> `get_page_content(doc_name, pages="9-10")` -> final text with `<cite doc=".." page="10"/>`. The OSS benchmark / cookbook numbers (next subsection) say real runs use a handful of turns.

**What the LLM sees** = tree *without text* (titles, ids, ranges, summaries, `key_items`), then **whole-page text** for the pages it chooses (not node text, not chunks). **How "node text" is fetched:** the agent reads `start_index/end_index` of the chosen node(s) and calls `get_page_content` with those page ranges - there is no node-id-based fetch tool in the current SDK. **Final answer** = the same agent's last message (no separate answer prompt).

### 8.2 Citation guidance (VERIFIED `LOCAL_CITATION_PROMPTS`)
Two formats: `"cite"` (`<cite doc="..." page="N"/>`, default) and `"markdown"` (`[doc, p. N]`, not parseable by `get_citations`). Local page content has no `block_id`, so citations resolve to **pages**. `client.citation_prompt()` returns the text (to append to `agent_instructions()` in our own agent).

### 8.3 The older, still-documented retrieval pattern (for reference/ablation; quoted, MIT)
`examples/tutorials/tree-search/README.md` (deleted in Sep 2026; recovered from commit `6fd2379`):
```python
prompt = f"""
You are given a query and the tree structure of a document.
You need to find all nodes that are likely to contain the answer.

Query: {query}

Document tree structure: {PageIndex_Tree}

Reply in the following JSON format:
{{
  "thinking": <your reasoning about which nodes are relevant>,
  "node_list": [node_id1, node_id2, ...]
}}
"""
```
(+ an "Expert Knowledge of relevant sections: {Preference}" variant; example preference: "If the query mentions EBITDA adjustments, prioritize Item 7 (MD&A) and footnotes in Item 8 (Financial Statements) in 10-K reports.") The README of that era says the cloud dashboard combined LLM tree search with value-function MCTS ("more details will be released soon" - never open-sourced here).
Current notebook `cookbook/pageindex-vision-rag.ipynb` (still in repo; **cloud client** + GPT-4.1 VLM) - the "v2" search prompt:
```python
search_prompt = f"""
You are given a question and a tree structure of a document.
Each node contains a node id, node title, and a corresponding summary.
Your task is to find all tree nodes that are likely to contain the answer to the question.

Question: {query}

Document tree structure:
{json.dumps(tree_without_text, indent=2)}

Please reply in the following JSON format:
{{
    "thinking": "<Your thinking process on which nodes are relevant to the question>",
    "node_list": ["node_id_1", "node_id_2", ..., "node_id_n"]
}}
Directly return the final JSON structure. Do not output anything else.
"""
```
then `node_map = utils.create_node_mapping(tree, include_page_ranges=True, max_page=total_pages)` -> pages -> page images/text -> `answer_prompt = "Answer the question based on the images of the document pages as context. Question: {query} Provide a clear, concise answer based only on the context provided."`. The previous demo `examples/agentic_vectorless_rag_demo.py` used a 3-tool agent with this system prompt: "You are PageIndex, a document QA assistant. TOOL USE: Call get_document() first... Call get_document_structure() to identify relevant page ranges. Call get_page_content(pages="5-7") with tight ranges; never fetch the whole document. Before each tool call, output one short sentence explaining the reason. Answer based only on tool output. Be concise." Note 03 has our recommended 2-step pipeline built on these.

### 8.4 Calling it and capturing sources + RAGAS contexts (TESTED against mock servers on both protocols)
RAGAS needs `user_input`, `response`, `retrieved_contexts: list[str]`. The contexts are the `get_page_content` tool outputs. Two capture routes, both verified:

* **Answer lane streaming events** `st = client.chat(q, doc_id=..., citations=True, stream=True); for ev in st.events:` yields dicts `{"type":"answer","delta"}`, `{"type":"thinking","delta"}`, `{"type":"tool_call","call_id","name","arguments"(dict)}`, `{"type":"tool_result","call_id","name","output"}` where **`output` is `{"type":"text","text":"<json string>"}`** (not a str). One run serves one view: consume `.events` *or* text.
* **Responses protocol (recommended for OpenAI, what the benchmark/cookbooks use):** `r = client.chat(q, doc_id=..., citations=True, protocol="responses", reasoning_effort="low")` -> dict keys `id, object, created_at, model, status, output, items, usage, instructions, tools, tool_choice, parallel_tool_calls, temperature, top_p, reasoning, max_output_tokens, error, incomplete_details, metadata`. `r["output"]` = model-produced items (`function_call`..., final `message` with `content:[{"type":"output_text","text":...}]`); **`r["items"]`** = full transcript incl. `{"type":"function_call","name","arguments"(JSON str),"call_id"}` and `{"type":"function_call_output","call_id","output":[{"type":"input_text","text":"<json>"}]}`; `r["usage"] = {"input_tokens","input_tokens_details":{"cached_tokens","cache_write_tokens"},"output_tokens","output_tokens_details":{"reasoning_tokens"},"total_tokens"}` aggregated across turns. Streaming (`stream=True, protocol="responses"`): events `response.output_text.delta`, `response.output_item.done` (function_call items), terminal `response.completed|incomplete|failed` carrying the same envelope (VERIFIED from the cookbook code, not run).

Tested helper (`pi_capture.py`; both routes + a cite parser that **keeps extra attributes**, because `client.get_citations()` keeps only `document/doc_id/page` - VERIFIED it silently drops `quote=`):
```python
import json, re
CITE_RE = re.compile(r'<cite\s+([^<>]*?)\s*/?>')
ATTR_RE = re.compile(r'(\w+)=(["\'])(.*?)\2', re.S)

def _tool_text(output):
    if isinstance(output, str): return output
    if isinstance(output, dict): return output.get("text", "")
    if isinstance(output, list): return "\n".join(o.get("text", "") for o in output if isinstance(o, dict))
    return ""

def page_contexts_from_tool_json(text):
    try: j = json.loads(text)
    except Exception: return []
    if not isinstance(j, dict) or not j.get("success") or "content" not in j: return []
    return [{"page": c["page"], "text": c["text"]} for c in j["content"] if isinstance(c, dict) and "page" in c]

def from_events(events):                       # client.chat(..., stream=True).events
    answer, calls, ctx = [], [], []
    for ev in events:
        t = ev["type"]
        if t == "answer": answer.append(ev["delta"])
        elif t == "tool_call": calls.append({"name": ev["name"], "arguments": ev["arguments"]})
        elif t == "tool_result" and ev["name"] == "get_page_content":
            ctx += page_contexts_from_tool_json(_tool_text(ev["output"]))
    return {"answer": "".join(answer), "tool_calls": calls, "contexts": ctx}

def from_responses_envelope(r):                # client.chat(..., protocol="responses")
    calls, ctx, by_id = [], [], {}
    for it in r["items"]:
        if it.get("type") == "function_call":
            by_id[it["call_id"]] = it["name"]
            calls.append({"name": it["name"], "arguments": json.loads(it["arguments"])})
        elif it.get("type") == "function_call_output" and by_id.get(it["call_id"]) == "get_page_content":
            ctx += page_contexts_from_tool_json(_tool_text(it["output"]))
    answer = "".join(c["text"] for o in r["output"] if o.get("type") == "message"
                     for c in o["content"] if c.get("type") == "output_text")
    return {"answer": answer, "tool_calls": calls, "contexts": ctx, "usage": r.get("usage")}

def parse_cites(answer):
    out, seen = [], set()
    for m in CITE_RE.finditer(answer):
        a = {k: v for k, _, v in ATTR_RE.findall(m.group(1))}
        try: page = int(str(a.get("page", "")).split("-")[0])
        except ValueError: continue
        key = (a.get("doc"), page, a.get("quote"))
        if key not in seen:
            seen.add(key); out.append({"doc": a.get("doc"), "page": page, **{k: v for k, v in a.items() if k not in ("doc", "page")}})
    return out
```
Guard: if `contexts` is empty the model answered from tree summaries or from general knowledge -> RAGAS faithfulness is meaningless; re-ask or flag (INFERRED design rule).

**"Area" for a citation** (local mode has no bbox): from the stored tree take every node with `start_index <= page <= end_index` and show the **deepest** (smallest-span) node's breadcrumb (`get_node_path(tree, node_id)` titles) + `node_id` + page range + the quote. Tested on the Fed tree: page 10 -> `2 Monetary Policy and Economic Developments > March 2024 Summary > Recent Economic and Financial Developments (0007, pp.10-12)`.

**Why `protocol="responses"` (INFERRED but supported):** the code comments say OpenAI "sol-class" models reject function tools together with `reasoning_effort` on the chat-completions lane ("Function tools with reasoning_effort ... this model runs tools on the Responses lane: upgrade litellm"); the official benchmark and the cookbooks all use `protocol="responses"`; on this lane the OpenAI SDK is used directly (no LiteLLM). I could not run it against the real API.

### 8.5 Query-time cost/accuracy numbers published by the project (VERIFIED, raw `PageIndex-OSS-Benchmark/README.md`)
62 lookup questions over 34 PDFs (1,945 pages), flash trees built by luna, `responses` protocol, MMLongBench-Doc-V2 judge; **excludes tables/charts/arithmetic**. Cost = answering call only.

| chat model | effort | accuracy | avg $/question |
|---|---|---|---:|
| gpt-5.6-luna | none / low / medium / high | 85.5% / 85.5% / 91.9% / **96.8%** | 0.0031 / 0.0033 / 0.0038 / **0.0036** |
| gpt-5.6-terra | none / low / medium / high | 90.3% / 95.2% / 98.4% / **100%** | 0.0296 / 0.0324 / 0.0303 / **0.0325** |
| gpt-5.6-sol | none / low / medium / high | 96.8% / 96.8% / **100%** / 100% | 0.0759 / 0.0817 / **0.0810** / 0.0819 |

Cookbook example (NVIDIA 10-K, luna, cloud index): 40,202 input tokens (23,321 cached, 16,872 cache-write), 298 output, **$0.005**. Their cost formula: `(regular_in*in + cached*cached_rate + cache_write*in*1.25 + out*out)/1e6`. For a 308-page report expect ~2-3x the structure tokens of their 57-page average (INFERRED): luna/high ~$0.01, terra/high ~$0.06-0.10, sol/medium ~$0.15-0.25 per question.

---

## 9. Citations, quote-level highlighting, and page text quality

### 9.1 What local mode gives vs what the UI needs
| Need | Local PageIndex gives | We add |
|---|---|---|
| page | `<cite page="N">` (physical) | validate `1 <= N <= pageNum` and that N was actually fetched |
| section/area | nothing in the cite | breadcrumb from tree by page (Section 8.4) |
| passage to highlight | nothing (cloud has `block` + `bbox` in thousandths of the page, `highlight_region`) | ask for `quote="..."` via `instructions=` (our parser keeps it), verify it against the page text we stored, then PDF.js text-layer search + temporary highlight |
Cloud-only items (do not exist locally): `get_block`, `get_page_image`, `get_document_image`, folders, metadata search, MCP server. `pageindex.highlight_region(image, bbox, scale=1000)` is a Pillow helper that draws a yellow translucent rectangle with orange outline - it needs a page image + bbox, which local mode cannot produce.

### 9.2 Quote attribute is tolerated by the tag regex (VERIFIED)
`<cite doc="x.pdf" page="12" quote="held the target range at 5 1/4"/>` is matched by PageIndex's own regex; `resolve_citations` rewrites it to `[[3]](#pageindex-citation-03)` (and de-duplicates repeated page cites to one number) but returns no quote. So: ask for it, parse it ourselves, do not rely on `get_citations`.

### 9.3 Fix the page text source (VERIFIED, 1.8 s for 222 pages)
PyPDF2 output sample (p10): `'Recent Economic and F inancial De velopments\nInflation. ... has slow ed notably but remains abo ve 2 percent.'`; pdfium: `'Recent Economic and Financial Developments\r\nInflation. ... has slowed notably but remains above 2 percent.'` (CRLF). Override the static method before constructing the client; the tree already comes from pdfium so page counts agree and `_check_page_bounds` is satisfied:
```python
import pypdfium2 as pdfium
from pageindex.local_api import LocalAPI

def pdfium_page_texts(file_path: str) -> list[str]:
    doc = pdfium.PdfDocument(file_path)
    try:
        out = []
        for i in range(len(doc)):
            page = doc[i]; tp = page.get_textpage()
            out.append(tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n"))
            tp.close(); page.close()
        return out
    finally:
        doc.close()

LocalAPI._extract_page_texts = staticmethod(pdfium_page_texts)     # process-wide; or subclass LocalAPI and set client._api
```
Verified with a mocked-LLM `submit_document`: `get_page_content(doc_id, "10")` then returned clean text. Caveat: note 03 found table cells come out one per line with every extractor; if we later want table-aware text, this single seam (`_extract_page_texts`) is where to plug a better renderer - and the same text must be what the viewer-side highlighter searches. (Page text is also what summaries are written from? No: flash summaries use the pdfium-derived `page_texts` from `extract_toc` (block text in reading order), while `pages.json`/`get_page_content` use whatever `_extract_page_texts` returns.)

### 9.4 Prompt-injection note
Only the classic/standard indexing path sanitises PDF text. The answering agent sends raw page text to the model as tool output; our own system prompt should say that page text is data, not instructions (INFERRED good practice).

---

## 10. PDF libraries and licenses

| Library | Used for | Version (tested) | License | Note |
|---|---|---|---|---|
| **pypdfium2** (+ bundled PDFium) | flash layout parser, bookmarks, `_validate_pdf`, our page-text override | 5.14.0 (`>=5`; CI also tests 4.x) | BSD-3-Clause / Apache-2.0 (+ dependency licenses), VERIFIED from pip metadata | the license-safe workhorse; **keep** |
| **PyPDF2** | `LocalAPI._extract_page_texts` (stored page text), `utils.get_page_tokens`/`extract_text_from_pdf`, a guarded helper inside flash's `_PdfDoc` | 3.0.1 (pinned in `requirements.txt`) | BSD (classifier `License :: OSI Approved :: BSD License`) | **deprecated upstream** (superseded by `pypdf`); GitHub #478 "replace the deprecated PyPDF2" was closed 2026-09-18 as completed after a commenter suggested pypdfium2, but **HEAD still imports PyPDF2** (VERIFIED `utils.py`, `local_api.py`, `requirements.txt`); needs `pycryptodome` for AES; poor spacing/digit fidelity |
| **PyMuPDF (`pymupdf`/`fitz`)** | **NOT a dependency.** Only the optional `get_page_tokens(pdf_parser="PyMuPDF")` branch (`utils.py` ~L547, standard mode) and `cookbook/pageindex-vision-rag.ipynb` (`pip install ... PyMuPDF`) | n/a | **AGPL-3.0** (commercial license otherwise) | **FLAG: do not add it to a product we may distribute/host without legal review.** `requirements.txt` keeps `# pymupdf  # optional` commented out. Note 03 used it only for local measurement. |
| pdfplumber / pdfminer | not used | - | - | - |
| Pillow | `imaging.highlight_region` only | 12.3.0 | MIT-CMU (HPND-style; pip metadata) | - |
| pycryptodome | not a pageindex dependency; PyPDF2's AES backend (`import Crypto`) | latest from PyPI | BSD + Public Domain (pip metadata) | add to our requirements |
| litellm / openai-agents / mcp / openai | LLM routing, agent loop, tool server, OpenAI client | 1.104.0 / 0.20.0 / 2.3.0 / 2.54.0 | MIT / MIT / MIT / Apache-2.0 (pip metadata) | see supply-chain note in Section 11 |
Browser side (not in this repo): PDF.js (Apache-2.0) renders/searches the PDF for the side panel.

---

## 11. Known problems and project health (GitHub issues read, 2026-10-07)

Search used `api.github.com/search/issues` over `repo:VectifyAI/PageIndex is:issue` (windows, encoding, python 3.13, offset, 429, litellm, no toc, json). **No open issue is specific to Python 3.13.** The only Windows report is #426 (Python 3.12, AES PDFs). The repo has 120 open issues (mostly feature requests / downstream asks).

| # | State | Topic | Relevance / mitigation |
|---|---|---|---|
| #426 | open | `PyCryptodome is required for AES algorithm` on permission-encrypted PDFs (Windows/3.12) | reproduced; `pip install pycryptodome` or pdfium page-text override |
| #220 / #196 | closed | **LiteLLM supply-chain compromise**: PyPI litellm **1.82.7 and 1.82.8** (live 2026-03-24 ~10:39 UTC for ~40 min) shipped a credential stealer exfiltrating env vars/SSH/cloud creds to `models.litellm.cloud`; safe: <=1.82.6 or >=1.83.0 (docs.litellm.ai/blog/security-update-march-2026) | `pageindex` now requires `litellm>=1.97.0` (pip metadata) / pins `==1.97.0` in `requirements.txt`. **Pin and hash-lock all deps; keep OPENAI_API_KEY out of shared shells; rotate if 1.82.7/8 was ever installed.** |
| #283 | closed | 429 storms + cascading `KeyError` | fixed by caps/priority gate + JSON parsing hardening; **fixed 1 s retry remains** (Section 7.1) |
| #326, #257, #199, #97, #69 | closed | non-strict JSON replies, KeyErrors in classic path | classic-path only; flash uses tolerant `_reply_json` |
| #163 | closed | `toc_transformer` crashes on huge TOC (800-page PDF), context overflow | classic path; closed 2026-07-03 |
| #174 | closed | infinite loop in `toc_transformer` | bounded now (5 attempts, VERIFIED in source) |
| #467 / #153 / #340 / #341 / #30 | open | classic path: empty first chunk, `int+None`, duplicate summaries for same-page siblings (flash merges them), whole-sentence titles, dead code | avoid `mode="standard"` |
| #131 | closed | PDF parsing failed with Chinese characters (2026-03, classic path) | not re-tested; irrelevant for an English annual report |
| #7 | closed | file names with spaces failed (2025) | name sanitiser now exists; still use ASCII names for agent tool calls |
| #286 | closed | `litellm`/`python-dotenv` pin conflict in `requirements.txt` | prefer `pip install pageindex` + our lock |
| #478 | closed | replace deprecated PyPDF2 | suggested pypdfium2; not done at HEAD |
| #316 | open | incremental index updates | not available; re-index on change |
**Model switching:** supported via LiteLLM (`"provider/model"`, e.g. `anthropic/claude-sonnet-4-6`; Anthropic-native lanes `protocol="messages"`); OpenAI-compatible endpoints via `OPENAI_BASE_URL`/`backend`. Older issues (#27 Ollama, #90 custom models, #150) are answered by this.
**Maintenance verdict:** very active (weekly releases, 38.9k stars). That is also the risk: behaviour changed on 2026-10-01 (#541 unified tree) and 10-07 (#554) - pin and diff on upgrade. Stale artefacts: `examples/results/*.json` are old-schema (no summaries, parent `end_index` = first child's start); do not use them as ground truth.

---

## 12. Vendor vs pip-install (decision)

| Piece | Recommendation | Why |
|---|---|---|
| `pageindex` itself | **pip install, pinned `==0.2.21`** (+ `pycryptodome`) | working on Win/3.13; MIT; ~29.4k lines of Python (flash ~17.2k - the layout parser/classifiers - plus `client.py` 2.7k, `utils.py` 1.4k, `agent_tools.py` 1.8k, `local_chat.py` 1.5k) that we would otherwise own; weekly upstream fixes |
| Tree building (`pageindex/flash/**`, `tree_optimize.py`, parts of `utils.py`) | pip for now; **vendor only if** we must drop heavy deps or freeze behaviour | Flash needs only pypdfium2 + regex + sortedcontainers (+ PyPDF2 guarded); LLM calls funnel through `utils.llm_acompletion/llm_completion` (litellm). I verified that re-binding `pageindex.utils.llm_acompletion`, `pageindex.tree_optimize.llm_acompletion` and `pageindex.utils.llm_completion` redirects **all** indexing LLM calls (used for the mock tests) -> a ~20-line AsyncOpenAI shim could remove litellm from the indexing lane (shim itself INFERRED, technique VERIFIED). |
| Answer agent (`local_chat.py`, `agent_tools.py`) | use as is via `client.chat(protocol="responses")`; **or** write our own loop using `client.get_document_structure` + `client.get_page_content` (both stable, cheap, return plain Python) | own loop gives deterministic contexts, strict-JSON answers with quotes, bounded turns (see note 03 section 5) |
| Page text store | **adapt**: override `LocalAPI._extract_page_texts` with pdfium (9.3) | quality + license |
| Context/citation capture, tree-path "area", quote verification | **write ourselves** (8.4, 9) | not provided locally |
| Persistence | keep PageIndex's `DocStore` layout, **one `storage_path` per session**; keep our own PDF copy + our own sessions DB | no Windows lock; PDF not stored |
| `openai-agents`, `mcp`, `litellm` | unavoidable hard deps of the wheel (`Requires-Dist`), even if we never use them | install size / supply-chain surface; hash-pin |
Later FastAPI: all PageIndex calls are blocking -> `run_in_threadpool`/`asyncio.to_thread`; indexing in a background job (queue + status endpoint) because it takes 1-3 min; never call `submit_document` from module top level of a spawned worker (it raises a guided error if `multiprocessing.current_process()._inheriting`); do not run uvicorn multi-process workers writing the same store.

Suggested minimal adapter surface (keeps us portable to a FastAPI service):
```python
class PageIndexService:
    def __init__(self, session_dir, index_model="gpt-5.6-luna", chat_model="gpt-5.6-sol"): ...
    def index(self, pdf_path) -> {"doc_id","page_count","name"}          # submit_document, store per session
    def tree(self, doc_id) -> list[node]                                  # get_document_structure
    def ask(self, doc_id, messages) -> {"answer","citations":[{page,quote,node_path}],"contexts":[...],"usage":{...}}
```

---

## 13. Experiment log (everything below was actually run; scripts in `...\scratchpad\pi-smoke\`; the reusable ones are copied to `research\01-code\`: `pi_capture.py` (context/cite capture), `mock_openai.py` (chat/completions + tool-call mock), `mock_openai2.py` (`/v1/responses` mock), `t4_mock_client.py` (mocked-LLM indexing + call accounting), `t14_pdfium_pages.py` (pdfium page-text override), `t6_chat.py`, `t7_responses.py`; the mocks hard-code the sample doc name `2023-annual-report.pdf` and ports 8765/8766 - change the ports if busy)

| # | What | Result |
|---|---|---|
| E1 | `pip install pageindex==0.2.21` in a 135-char venv path | **fails** (MAX_PATH, litellm yaml); short path `C:\Users\ayush\pv` works |
| E2 | `page_index_flash(222-page Fed PDF, summary=False, optimize=False)` | 22.3 s guarded / 18.9 s unguarded script; `toc_source=bookmarks`; 17 top / 283 nodes; no LLM; unguarded script did **not** fork-bomb |
| E3 | same with `optimize="merge"` (bookmarks on/off) | 177 nodes (41 merges) / 178 nodes (`detected`), worst-case search cost 37 -> 37 pages |
| E4 | mocked-LLM `PageIndexClient.submit_document` (`utils.llm_acompletion`/`tree_optimize.llm_acompletion`/`utils.llm_completion` patched) | 28-43 s; calls: leaf 142, parent 34, expand 2, description 1; store files as in 6.4; `get_tree`, `get_document`, `get_page_content` shapes confirmed |
| E5 | PyPDF2 vs pdfium text of page 10; printed vs physical | spacing defects in PyPDF2; physical 10 = printed 4 |
| E6 | local OpenAI-compatible mock server on localhost + real `client.chat` | system prompt, doc-targeting message, 4 tool schemas, 3-turn loop, `.events` shapes captured; `/v1/chat/completions` used by the default lane |
| E7 | same, `protocol="responses"` (mock `/v1/responses`) | envelope keys, `items`, `usage` captured |
| E8 | `get_citations` / `resolve_citations` with extra `quote=` attr | attr tolerated, then dropped |
| E9 | AES-256 PDF (empty user pw) | pdfium+flash OK; `submit_document` fails w/o pycryptodome; OK after install |
| E10 | litellm 1.104.0 bundled cost map | luna/terra/sol prices, 922k/128k limits, endpoints chat+responses+batch |
| E11 | `LocalAPI._extract_page_texts` override with pdfium | works; 1.8 s extraction for 222 pp |
| E12 | structure payload sizing (synthetic summaries 60/100/150 words, 177 nodes) | 18.8K/25.8K/34.7K tokens, 2/3/4 parts |
| E13 | CLI `run_pageindex.py --no-summary --optimize off` | works; JSON as in 6.1 |
| E14 | `pip install --dry-run ragas` on the pageindex venv | resolves (ragas 0.4.3); `pip check` clean |
| E15 | repo HEAD vs 0.2.21 | HEAD is 1 commit ahead (#554, small tree follow-ups); not diffed line-by-line (CRLF noise) |
Leftovers (all throwaway, safe to delete): venv `C:\Users\ayush\pv` (short path on purpose; recreate with Section 2.1), repo clone `...\scratchpad\pi-oss`, scripts/stores in `...\scratchpad\pi-smoke`, GitHub/PyPI JSON in `...\scratchpad\pi-meta`. The mock OpenAI servers I started on ports 8766/8777 were stopped (port 8765 belongs to another process - not mine, untouched).

---

## 14. Unknowns, risks, UAT checklist

**Could not verify (no key):** real gpt-5.6 behaviour through LiteLLM's chat lane vs Responses (the "Function tools with reasoning_effort" refusal); real per-question latency (INFERRED 5-30 s) and turn counts; real summary quality/cost on our target PDF; OpenAI tier rate limits vs 64+32 concurrent calls; org verification requirements for gpt-5.6; whether the target National Grid PDF has bookmarks, AES encryption, scanned pages, 2-column layouts (affects `toc_source` and node quality).
**Risks:** (1) API churn (weekly releases, `0.3.0.devN` line) -> pin + wrapper; (2) 429 aborts whole index (fixed 1 s retries) -> lower `summary_concurrency`, app-level retry; (3) PyPDF2 text defects (digits!) -> pdfium override and a spot-check of financial figures; (4) agent may answer without reading pages -> guard on empty contexts; (5) tables lose structure in plain text; (6) no Windows CI upstream; (7) supply chain (litellm history) -> hash-pinned lockfile; (8) AES-encrypted PDFs -> pycryptodome; (9) node ranges overlap on boundary pages -> de-duplicate pages, mind context precision; (10) `submit_document` blocks 1-3 min -> background job + progress UI.
**UAT checklist:** index the real PDF: record `toc_source`, node count, `summary` coverage, time, tokens (`litellm` usage not exposed by `submit_document` - measure via our own proxy/log or OpenAI dashboard); ask 10 factual questions with known pages: check cited page == ground-truth page and `contexts` contain the figure; confirm the pdfium text contains the exact numbers; run the same question on `luna/high`, `terra/high`, `sol/medium`; kill the network mid-index and confirm the error is surfaced cleanly; open a second session and confirm doc isolation.

---

## 15. Sources (all fetched 2026-10-07)

* Repo (shallow clone, HEAD `d57adcd`): https://github.com/VectifyAI/PageIndex ; LICENSE (MIT); `README.md`, `pyproject.toml`, `requirements.txt`, `pageindex/config.yaml`, `run_pageindex.py`, `pageindex/{client,local_api,local_store,local_chat,agent_tools,utils,tree_optimize,page_index_classic,chat_stream,types,errors,naming,imaging}.py`, `pageindex/flash/{README.md,api.py,main.py,parser_pdfium_parallel.py,embedded_toc.py}`, `pageindex/integrations/openai_agents.py`, `cookbook/*.ipynb`, `.github/workflows/{tests,publish}.yml`
* Historical files (commit `6fd237986ece44915918d7f6fa50edb1a93fa922`, 2026-04-28): `examples/tutorials/tree-search/README.md`, `examples/agentic_vectorless_rag_demo.py`, `pageindex/{client,retrieve}.py` via raw.githubusercontent.com
* PyPI JSON: https://pypi.org/pypi/pageindex/json
* GitHub REST: https://api.github.com/repos/VectifyAI/PageIndex (+ `/releases`, `/commits`, `/tags`, `/issues`, `/search/issues`, `/issues/{196,220,426,467,153,340,341,283,163,174,286,478}`)
* Docs: https://docs.pageindex.ai/getting-started , /sdk/chat , /sdk/documents , /sdk/agents
* Benchmark: https://github.com/VectifyAI/PageIndex-OSS-Benchmark (raw README fetched)
* OpenAI model pages: https://developers.openai.com/api/docs/models/gpt-5.6-luna , /gpt-5.6-terra , /gpt-5.6-sol
* LiteLLM incident: https://docs.litellm.ai/blog/security-update-march-2026
