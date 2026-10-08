# 02 - PageIndex official docs, Cloud API, SDK and MCP

Researcher notes. Research date: 2026-10-07. No API keys were used, no accounts created, no paid API call made.
Legend: **VERIFIED** = seen in primary source (docs page, source code, OpenAPI file, official notebook) in this session. **INFERRED** = my deduction, not stated by a primary source. **NOT DOCUMENTED** = could not find it.

---

## 0. Headline findings (read this first)

1. **The PageIndex docs changed radically in Aug-Sep 2026.** Older material you may remember (`PageIndexClient(api_key)`, `submit_query` / `get_retrieval`, "Retrieval API", dashboard-side chat) is now legacy. The current model is: ONE Python client, `pageindex` (PyPI **0.2.21**, released 2026-10-01), with two independent sides: `index=` (where the tree is built and stored: `"cloud"` or a local model name) and `chat=` (which LLM does the reasoning and answers: **always your own model** per the docs). VERIFIED (docs.pageindex.ai/getting-started, /sdk/client).
2. **"Use OpenAI models as suggested by the docs" resolves to:** `index="gpt-5.6-luna"` (local indexing/summaries, cheap) and `chat="gpt-5.6-sol"` (the model that searches the tree and answers). Notebooks also use `gpt-5.6-terra` (mid tier). All three are real OpenAI models; I confirmed prices on OpenAI's own docs pages (section 9). VERIFIED.
3. **Keys needed:**
   - Local index + own chat model: only `OPENAI_API_KEY`. No PageIndex key.
   - Cloud index + own chat model: `PAGEINDEX_API_KEY` **and** `OPENAI_API_KEY`.
   - Cloud index + *managed* chat (legacy, no `chat=`): only `PAGEINDEX_API_KEY` (PageIndex picks and pays for the model). Source supports it; docs call it "legacy"/"managed cloud chat".
4. **Citations are now block-level on cloud, page-level on local.** Own-model chat emits `<cite doc="report.pdf" page="12" block="p12_text_3"/>` tags (cloud) or `<cite doc="report.pdf" page="12"/>` (local). Managed chat emits `<doc=report.pdf;page=12;block=p12_text_3>`. A cloud block resolves (`get_block`) to `{page, block_id, bbox, block_type, text}` - i.e. page + exact passage text + bounding box. This is exactly what the "click citation -> side panel -> highlight" feature needs. VERIFIED.
5. **Retrieved contexts for RAGAS:** with own-model chat the agent loop runs *in our process*, so we can capture every tool result (`get_page_content` outputs = the text the model actually read) via `ChatStream.events` or the Responses-protocol transcript (`response["items"]` -> `function_call_output`). The managed chat and the deprecated Retrieval API do NOT give full contexts in a clean way. VERIFIED (source + official notebooks).
6. **The Cloud does not run PageIndex's reasoning for us any more** (own-model path): retrieval = an OpenAI-Agents-SDK agent loop in our process, calling PageIndex tools (`get_document_structure`, `get_page_content`, ...) - locally implemented for local docs, or proxied to the cloud MCP server for cloud docs. VERIFIED (pageindex/local_chat.py, agent_tools.py).
7. **Pricing (cloud):** $0.01/page to index (one-time), $0.001/page/month active (first 1,000 pages free), retrieval unlimited/no fee, $10 free credits, no card. A 308-page report = **$3.08** one-time, $0/month. Local mode is free (you pay only OpenAI, about $0.001/page with luna = about $0.31 for 308 pages). VERIFIED (docs.pageindex.ai/pricing; flash blog).
8. **Not documented anywhere I could reach:** max pages per document, max file size, REST rate-limit numbers, data-retention period for paid accounts, which model the managed chat uses.
9. **Recommendation (detail in section 12):** Build against `PageIndexClient` with a config switch. Default = **local Flash index + own OpenAI chat model** (only an OpenAI key, page-level citations, full contexts). Optional upgrade = `index="cloud"` (block-level citations with bbox for pixel-exact highlighting). Same code path for chat, citations and RAGAS context capture in both.

---

## 1. Sources crawled

| Source | URL | Notes |
|---|---|---|
| Docs home | https://docs.pageindex.ai | Next.js site (not Mintlify). No `llms.txt`, `llms-full.txt`, `sitemap.xml` (all 404). Pages are plain HTML; I downloaded each with curl and converted to text. |
| **Agent skill file** | https://docs.pageindex.ai/SKILL.md | 200, `text/markdown`. Official, concise, current. Best single page to read. |
| Getting started | https://docs.pageindex.ai/getting-started (`/quickstart` redirects here) | |
| Python SDK | `/sdk`, `/sdk/client`, `/sdk/documents`, `/sdk/chat`, `/sdk/agents` | |
| JS SDK | `/js-sdk`, `/js-sdk/documents`, `/js-sdk/mcp-tools`, `/js-sdk/legacy/chat`, `/js-sdk/folders` | npm `@pageindex/sdk` |
| REST reference | https://docs.pageindex.ai/api-reference | Still describes `POST /doc/`, tree/OCR/metadata/block, `POST /chat/completions`. |
| MCP | https://docs.pageindex.ai/mcp | |
| Pricing | https://docs.pageindex.ai/pricing | |
| Cookbook | https://docs.pageindex.ai/cookbook (4 notebooks) | Plus `pageindex-vision-rag.ipynb` in the repo. |
| Changelog | https://docs.pageindex.ai/changelog | 3 entries (Aug 26, Sep 16, Sep 21 2026). |
| Tutorials | `/tutorials`, `/tutorials/tree-search/llm`, `/tutorials/doc-search` | Generic tree-search prompt. |
| OpenAPI | https://api.pageindex.ai/openapi.json | 200, 32 KB, FastAPI-generated, thin response schemas. |
| MCP server card | https://pageindex.ai/.well-known/mcp | |
| Site LLM index | https://pageindex.ai/llms.txt ; every pageindex.ai page also serves markdown via `Accept: text/markdown` or `.md` suffix | |
| Blogs | pageindex.ai/blog/{pageindex-intro, pageindex-chat, pageindex-flash, pageindex-filesystem, pageindex-vs-chatgpt, do-we-need-ocr, claude-code-agentic-rag, introducing-openkb} | |
| Legal | pageindex.ai/{privacy,terms,policies,enterprise}.md | |
| SDK source | PyPI wheel `pageindex-0.2.21-py3-none-any.whl` (unzipped and read); GitHub https://github.com/VectifyAI/PageIndex (MIT, ~38.9k stars, default branch `main`, last push 2026-10-07) | |
| MCP repo | https://github.com/VectifyAI/pageindex-mcp (MIT, default branch `master`) | Thin proxy/CLI; the tool logic is server-side. |
| Cookbook notebooks | raw.githubusercontent.com/VectifyAI/PageIndex/main/cookbook/*.ipynb | Contain real recorded outputs. |

### Docs sidebar (VERIFIED)
Introduction `/` -> Getting Started `/getting-started` -> **Python SDK** `/sdk` (Client `/sdk/client`, Document `/sdk/documents`, LLM `/sdk/chat`, Agent `/sdk/agents`) -> **JavaScript SDK** `/js-sdk` (Document, MCP Tools, Chat API - Legacy) -> MCP `/mcp` -> Pricing -> Cookbook -> Changelog -> Blog. There is no separate "Retrieval API", "OCR" or "FAQ" page any more (`/faq`, `/limits`, `/rate-limits`, `/sdk/retrieval`, `/sdk/ocr` all 404).

### Hostnames (VERIFIED unless noted)
| Host | Purpose |
|---|---|
| `https://api.pageindex.ai` | REST API + `/mcp` (SDK `BASE_URL`) |
| `https://docs.pageindex.ai` | docs |
| `https://pageindex.ai` | marketing, blog, `llms.txt` |
| `https://dash.pageindex.ai` -> redirects to `https://developer.pageindex.ai/login` | developer dashboard (API keys at `/api-keys`, billing at `/subscription`); login = "Continue with Google / GitHub" or email |
| `https://app.pageindex.ai` (and `https://chat.pageindex.ai`, which resolves to it) | the end-user chat web app (login wall). I did NOT sign in (account creation is out of scope). |
| `https://mcp.pageindex.ai/mcp` | OAuth-based MCP server (for interactive clients) |
| `https://app.pageindex.ai/mcp` / `https://chat.pageindex.ai/mcp` | MCP bound to the chat-app workspace (OAuth). The two pages disagree on which hostname; treat as INFERRED-equivalent |

Important: the **developer platform (API key) and the consumer chat app are separate workspaces**; "This PageIndex MCP ... does not share files or usage with PageIndex Chat" (docs.pageindex.ai/mcp).

---

## 2. The product surfaces and how they relate

```
PDF --upload--> [ Index ]                                   [ Retrieve + answer ]
                  |                                           |
        local: PageIndex Flash (open source, your machine)    own model (OpenAI etc.) running the PageIndex
        cloud: managed pipeline (OCR + layout blocks + tree)  agent loop IN YOUR PROCESS (client.chat)
                  |                                         or your own agent framework + PageIndex tools
                  v                                         or managed chat (legacy REST /chat/completions)
        tree JSON + page text (+ blocks/bbox on cloud)       or MCP (cloud only)
```
VERIFIED (getting-started, SKILL.md, client.py docstring).

- **Index** stage: tree = hierarchical table of contents with node titles, page ranges and LLM summaries. No vectors, no chunking.
- **Retrieve** stage: an LLM *agent* reads the tree (`get_document_structure`) and then reads chosen pages (`get_page_content`), iterating until it has enough. PageIndex's own description of the loop (pageindex-intro blog): read ToC -> select section -> extract info -> sufficient? yes: answer / no: loop. It also follows in-document references ("see Appendix G").
- Docs statement to keep: "An LLM searches that tree and answers the question. This model is always your own, in either index mode." (getting-started).

---

## 3. Current Python SDK (`pip install -U pageindex`, 0.2.21)

VERIFIED by installing `pageindex==0.2.21` in a throwaway venv and inspecting it (`inspect.signature`) plus reading source.

- Requires Python >=3.10 (we have 3.13.3: fine). Dependencies (core, not extras): `Pillow, PyPDF2, litellm>=1.97, mcp<3,>=1.19, openai>=1.70, openai-agents>=0.18.1, pypdfium2>=5, python-dotenv, pyyaml, regex, requests, sortedcontainers, urllib3`. Extras: `anthropic`, `claude`. Resolved in my test: `litellm 1.104.0`, `openai 2.54.0`, `openai-agents 0.20.0`, `mcp 2.3.0`.
- `.env` is auto-loaded at import: `load_dotenv(find_dotenv(usecwd=True))` in `pageindex/utils.py`. `CHATGPT_API_KEY` is a deprecated alias for `OPENAI_API_KEY`.
- **Windows gotcha (hit during my install):** `pip install pageindex` into a venv whose path is long fails with `OSError: [Errno 2] No such file or directory ... litellm\proxy\guardrails\guardrail_hooks\litellm_content_filter\categories\bias_sexual_orientation.yaml` (MAX_PATH 260). Either enable Windows long paths or put the venv at a short path (e.g. `C:\venvs\ragapp`). I succeeded with `C:\pi02`. Our project path `C:\Users\ayush\Annual Report Answering\...` plus `.venv\Lib\site-packages\...` may hit this: keep venv path short.

### 3.1 Constructor (VERIFIED, `client.py`)
```python
from pageindex import PageIndexClient

# A) cloud index + your own OpenAI chat model   (needs PAGEINDEX_API_KEY + OPENAI_API_KEY)
client = PageIndexClient(index="cloud", chat="gpt-5.6-sol")

# B) local Flash index + your own OpenAI models  (needs OPENAI_API_KEY only)
client = PageIndexClient(index="gpt-5.6-luna", chat="gpt-5.6-sol")

# C) cloud index + MANAGED chat (legacy; needs only PAGEINDEX_API_KEY)
client = PageIndexClient(index="cloud")        # chat_model is None -> client.chat() hits POST /chat/completions

# local storage dir (default ./.pageindex)
client = PageIndexClient(index={"model": "gpt-5.6-luna", "storage_path": "./my-documents"}, chat="gpt-5.6-sol")
```
Key parameters (`PageIndexClient.__init__`): `api_key`, `index` (str|dict), `chat` (str|dict), `mode`, `index_model` (default `gpt-5.6-luna`), `chat_model` (default `gpt-5.6-sol`), `model`, `summary_model`, `summary_max_words` (150), `summary_concurrency` (64), `use_embedded_toc` (True), `optimize` (`"full"`|`"merge"`|`"off"`, default `"full"`), `retrieve_model` (legacy alias of chat_model), `storage_path`, `index_backend`, `chat_backend`, `instructions`. One spelling per side (slot vs flat), mixing raises `PageIndexAPIError`.
- `index="cloud"` reads the key from env `PAGEINDEX_API_KEY` only when you say cloud explicitly; a bare `PageIndexClient()` stays local. Inline: `index={"api_key": "..."}`.
- Subclasses: `PageIndexCloudClient`, `PageIndexLocalClient` pin the side.
- Model names follow **LiteLLM**: bare = OpenAI, `anthropic/...`, `openrouter/...`, `openai/...` + `OPENAI_BASE_URL` for OpenAI-compatible servers; per-side `backend={"api_key","base_url",...}`.
- Env vars: `PAGEINDEX_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `LITELLM_LOG` (default ERROR).
- `instructions="..."` on the client (or per `chat(...)` call) is appended after PageIndex's managed system prompt (so we can customize persona/format but not replace the tool guidance).

### 3.2 Public method surface (VERIFIED via `dir(PageIndexClient)`)
`submit_document, get_document, get_document_id, list_documents, delete_document, get_tree, get_document_structure, get_ocr, get_page_content, get_block, get_page_image, get_document_image, is_retrieval_ready, submit_query, get_retrieval (both deprecated, cloud-only), chat, chat_completions (kept for old code), get_citations, resolve_citations, citation_prompt, agent_instructions, agent_tools, as_openai_tools, as_anthropic_tools, as_claude_mcp, openai_agent_config, anthropic_runner_config, claude_agent_config, document_context, folder_context, create_folder, list_folders, get_document_path, get_folder_path, get_folder_id, chat_model, retrieve_model`. Module also exports `PageIndexAPIError, ChatStream, highlight_region` and TypedDicts `IndexConfig, CloudIndexConfig, LocalIndexConfig, ChatConfig, ChatProcessOptions`.

Exact signatures:
```python
submit_document(file_path: str, mode: str|None=None, beta_headers: list[str]|None=None,
                folder_id: str|None=None, metadata: dict|None=None, wait: bool=False) -> {"doc_id", "name"}
get_document(doc_id) -> {"id","name","description","status","createdAt","pageNum","folderId","metadata"}
get_tree(doc_id, node_summary: bool=False, include_text: bool=True) -> {"doc_id","status","retrieval_ready","result":[nodes]}
get_ocr(doc_id, format: "page"|"node"|"raw"="page") -> {"doc_id","status","retrieval_ready","result": ...}
get_page_content(doc_id, pages: str) -> list[{"page_index", "markdown", ...}]     # pages: "5-7", "3,8", "12"
get_block(doc_id, block_id) -> {"doc_id","page","block_id","bbox","block_type","text"}      # cloud only
get_page_image(doc_id, page:int) -> str  # short-lived presigned URL, JPEG                  # cloud only
chat(messages: str|list[dict], *, doc_id=None, stream=False, model=None, reasoning_effort=None,
     show_process=None, folder_id=None, protocol=None, instructions=None, citations=False,
     max_turns=None, backend=None, extra_headers=None, extra_body=None)
get_citations(answer: str, doc_id=None) -> list[dict];  resolve_citations(answer, doc_id=None) -> {"answer","citations"}
```
Cloud failures raise `PageIndexAPIError` (has `.status_code`). Tool-level failures inside an agent come back as JSON, not exceptions (except 401/403, exhausted 429/5xx, RATE_LIMITED, USAGE_LIMIT_REACHED).

---

## 4. Indexing: submit -> poll -> ready

### 4.1 SDK flow (VERIFIED)
```python
res = client.submit_document("./national-grid-ar.pdf", wait=True)   # {"doc_id": "pi-abc123def456", "name": "national-grid-ar.pdf"}
doc_id = res["doc_id"]
```
- Cloud: async. `wait=True` polls `get_document()` with back-off (2 s, x1.5, cap 15 s), tolerates up to 2 transient poll failures (raises on the 3rd or on 401/403/404), raises `PageIndexAPIError` on status `"failed"` and after a **30 minute** timeout (`_wait_until_ready`). Without `wait`, poll `client.get_document(doc_id)["status"]`.
- Statuses (VERIFIED): `"queued"`, `"processing"`, `"completed"`, `"failed"` (the MCP tool description also lists `"pending"`).
- Local: synchronous inside `submit_document`; always `"completed"` on return. PDF only. Default `mode` "flash"; `mode="standard"` = classic slower LLM-built tree.
- A name already in use is stored as `name_1 ... name_99` and a `UserWarning` is emitted (names are unique per library). Filenames are sanitized (`naming.sanitize_filename`).
- Documents persist; **reuse `doc_id` across runs** (billing is per page indexed on cloud). `client.list_documents()` / `get_document_id("name.pdf")` find existing ones.
- Accepted types: cloud = PDF, PPTX, Word; local = PDF only.

### 4.2 Raw REST (VERIFIED: docs.pageindex.ai/api-reference + openapi.json + cloud_api.py)
Auth header: **`api_key: <PAGEINDEX_API_KEY>`** (not Bearer) for REST. (MCP uses `Authorization: Bearer`.)

| Op | Method + URL | Notes |
|---|---|---|
| Submit | `POST https://api.pageindex.ai/doc/` multipart `file` + form fields | SDK sends `if_retrieval=True`, optional `mode`, `beta_headers` (JSON string), `folder_id`, `metadata` (JSON string). OpenAPI body also lists `if_add_node_id`, `if_add_node_summary`, `if_add_doc_description`, `if_add_node_text`, `ultra_mode`, `golden_data`, `if_ocr` (default true). Returns `{"doc_id": "pi-..."}` (plus `name`). A ZIP upload returns 202 with per-entry results. |
| Status+tree | `GET /doc/{doc_id}/?type=tree&summary=true&include_text=false` | Processing: `{"doc_id","status":"processing"}`; completed: `{"doc_id","status":"completed","retrieval_ready":true,"result":[...]}`. |
| OCR | `GET /doc/{doc_id}/?type=ocr&format=page|node|raw` | `page`: list of `{"page_index" (1-based), "markdown", "images": [base64...]}`. |
| Metadata | `GET /doc/{doc_id}/metadata[/]` | `{"id","name","description","status","createdAt","pageNum","folderId","metadata"}` |
| Block | `GET /doc/{doc_id}/block/{block_id}/` | See 4.5. 404 if doc was indexed before 2026-09-13 (no blocks). |
| Page image | `GET /doc/s3/{doc_id}/images?start=N&end=N` | `{"images":[{"page":N,"url":"<presigned>"}]}` (SDK reads this; undocumented in docs, VERIFIED in `cloud_api.py`). |
| Doc image | `GET /doc/{doc_id}/image/{img_id}/` -> `{"url": ...}` | e.g. `img-7.jpeg` |
| List | `GET /docs/?limit=&offset=&folder_id=&name=&recursive=` | docs say limit 1-100 (REST page) vs 1-10,000 (SDK page); SDK enforces 1-10000. |
| Delete | `DELETE /doc/{doc_id}/` | `{"message": "Document deleted successfully."}` |
| Folders | `POST /folder/`, `GET /folders/?parent_folder_id=`, `DELETE /folder/{id}` | plus metadata schema routes |
Note trailing slashes: docs and SDK are inconsistent (`/docs` vs `/docs/`, `/doc/{id}/metadata` vs `/doc/{id}/metadata/`); the SDK uses trailing slashes. FastAPI redirects normally accept both (INFERRED).

### 4.3 Tree JSON (VERIFIED)
Raw cloud shape (`get_tree`, `summary=true`): leaf nodes carry `summary`, parents carry `prefix_summary`; first page is `page_index`.
```json
{"doc_id":"pi-abc123def456","status":"completed","result":[
 {"title":"Financial Stability","node_id":"0006","page_index":21,
  "prefix_summary":"The Federal Reserve maintains ...",
  "nodes":[{"title":"Monitoring Financial Vulnerabilities","node_id":"0007","page_index":22,"summary":"..."}]}]}
```
**The SDK normalizes both modes** (`utils.unify_tree`) to `{"title","node_id","start_index","end_index","summary","text"?,"nodes"}` (summary = `summary` or `prefix_summary`; `end_index` derived if the server omits it). `get_tree(doc_id)` default is `include_text=True, node_summary=False`; for a compact tree use `get_tree(doc_id, node_summary=True, include_text=False)` (or `get_document_structure(doc_id)` which does exactly that and returns just `result`). `include_text=True` adds a `text` field per node (text no child holds).

### 4.4 Page text: `get_page_content` / OCR
- Page-level markdown: `[{"page_index": 5, "markdown": "...", "images": [...]}]`.
- **Efficiency trap (VERIFIED in source):** SDK `client.get_page_content(doc_id, "5-7")` calls `get_ocr(doc_id, format="page")` for the WHOLE document and filters locally. On cloud, `images` are base64 blobs, so a 308-page report could be a very large download (size INFERRED). If we call it ourselves (e.g. to serve pages to the UI), fetch OCR **once**, cache to disk, slice locally. The agent's own `get_page_content` tool on cloud goes through the MCP server instead (server-side slicing).
- The agent tool caps each response at **100,000 chars** (`TOOL_RESPONSE_CHAR_LIMIT`, 95% budget); extra pages are reported as "remaining".
- Local mode: text comes from the PDF text layer; "no OCR model runs locally", scanned PDFs have no text.

### 4.5 Block JSON, bbox, page image (cloud only; VERIFIED docs + cookbook output)
```json
{"doc_id":"pi-abc123def456","page":3,"block_id":"p3_text_5","bbox":[78,24,293,44],"block_type":"text","text":"..."}
```
- Block id pattern: `p{page}_{type}_{n}` e.g. `p12_text_3`, `p37_table_6`, `p5_image_19`. `block_type` seen: `text`, `table`, `image`.
- **`bbox` = `[x0,y0,x1,y1]` in thousandths (0-1000) of page width/height, origin top-left.** To overlay on a PDF.js page: `left = x0/1000*pageWidthPx`, etc.
- Only documents indexed **since 2026-09-13** carry blocks (older ones: `get_block` -> 404, citations fall back to page-only). A new upload today is fine.
- `get_page_image(doc_id, page)` -> presigned JPEG URL; `pageindex.highlight_region(image_bytes_or_PIL, bbox, scale=1000)` draws a translucent yellow (255,210,0,alpha 65) box with orange outline and returns a PIL image (VERIFIED `imaging.py`, ran it offline OK).
- Recorded real example (cookbook `pageindex-citation.ipynb`, NVIDIA 10-K): `{'document': 'NVIDIA-FY2026-10-K.pdf','doc_id': 'pi-cmu3rrhyb...','page': 37,'block_id': 'p37_table_6','bbox': [26,378,975,512],'block_type': 'table','text': '|   | Year Ended  |...'}` - the `text` of a table block is a markdown table.

### 4.6 Metadata and folders (cloud only)
`submit_document(..., metadata={"source":"web","year":2024})` (flat dict of str/int/float/bool, keys `^[a-zA-Z][a-zA-Z0-9_]*$`, <=8 KB, must match workspace schema if one exists). Folders: `create_folder(name, description=None, parent_folder_id=None)` -> `{"folder": {"id","name","description","parent_folder_id","created_at","file_count","children_count"}}`; `list_folders(parent_folder_id="root"|id|None)`; `submit_document(folder_id=)`; `list_documents(folder_id=, recursive=)`. Not needed for our one-doc-per-session design (but a per-session folder is a possible isolation trick, INFERRED).

---

## 5. Answering: four ways to use the tree

### 5.1 Own-model chat: `client.chat(...)` (RECOMMENDED path)
VERIFIED (`client.py`, `local_chat.py`, `agent_tools.py`).

- Under the hood: an **OpenAI Agents SDK `Agent`** named "PageIndex" with `instructions = CHAT_HEADER + agent_instructions + extras`, tools = PageIndex tools, model = LiteLLM model (bare OpenAI name) on the default lane, or `OpenAIResponsesModel` when `protocol="responses"`; `Runner.run[_streamed]` with **tracing disabled** (`RunConfig(tracing_disabled=True)`), `max_turns` default **10**.
- System prompt header: *"You are PageIndex by Vectify AI, a document-focused assistant. Be concise, never use emojis, and do not expose tool names."* Then the agent instructions: reading workflow ("For documents over 20 pages: call get_document_structure() first to locate relevant sections, then get_page_content() with targeted page ranges. For small documents (20 pages or fewer): call get_page_content() directly."), tool usage rules, discovery/persistence rules (full text in `agent_tools.py` lines ~1581-1630; on cloud the live text is fetched from the MCP server's `instructions`).
- **Tools exposed to the model** (identical names on cloud MCP and local):
  | Tool | Params | Returns |
  |---|---|---|
  | `browse_documents` | `folder_id`, `recursive`, `sort` (`time`/`relevance`), `query`, `offset`, `limit` (1-50) | docs (+folders on cloud), `next_offset`, `has_more` |
  | `get_document` | `doc_name`, `folder_id`, `wait_for_completion` | status + metadata |
  | `get_document_structure` | `doc_name`, `folder_id`, `part`, `wait_for_completion` | outline (titles, page ranges, summaries), paginated by `part` |
  | `get_page_content` | `doc_name`, `pages` (`"5"`, `"3,7,10"`, `"5-10"`, `"1-3,7,9-12"`), `folder_id`, `wait_for_completion` | `{"success": true, "doc_name", "total_pages", "requested_pages", "returned_pages", "content":[{"page": N, "text": "..."}], "next_steps":{...}}` (cloud adds block ids and image annotations) |
  | cloud only: `get_folder_structure`, `search_documents` (keyword + LLM re-rank, score 6-10), `get_document_image` | | |
  | management (opt-in): `remove_document` (<=10 names) | | |
  Tool results are JSON strings: `{"success": true, ...,"next_steps":{...}}` or `{"error":..., "errorCode":..., "next_steps":{...}}`.
- Scoping: `chat(msg, doc_id=...)` puts a **targeting block** as the FIRST USER MESSAGE ("The user has specified document: X / Document metadata: {...} / Use this document's name to retrieve its content with get_document_structure() and get_page_content()."). On *local* docs the scope is also enforced at tool level; on *cloud* docs it only steers (prompt level). Keep `doc_id` constant across a conversation (prompt-cache prefix).
- Multi-turn: pass your own list `[{"role":"user"|"assistant","content":...}]`; history is **text only** (no tool turns) on the answer lane. For tool-aware continuation use `protocol="responses"` / `"messages"` transcripts.
- Return types: non-stream -> `str`; `stream=True` -> `ChatStream` (iterate for text chunks, with `show_process` weaving `[thinking]`, `[tool_call] name {args}`, `[tool_result] name: ...` lines; `show_process=False` for bare answer; `show_process={"thinking":False,"tool_call":True,"tool_result":True,"max_chars":200}`) or read `.events` for typed dicts:
  ```python
  {"type": "thinking"|"answer", "delta": str}
  {"type": "tool_call",   "call_id": str, "name": str, "arguments": dict|str}
  {"type": "tool_result", "call_id": str, "name": str, "output": str|list}   # full, never clipped
  ```
  One run serves ONE view (text OR events); consuming both raises. `ChatStream.close()` cancels the run.
- Other parameters: `model=`, `reasoning_effort="low"|"medium"|"high"` (LiteLLM spelling; for Responses `reasoning.effort`), `max_turns=`, `instructions=` (per call, appended), `citations=True`, `backend=`, `extra_headers=`, `extra_body=` (system/instructions/input/messages/tools/stream/doc_id refused here).
- `protocol="chat_completions"` -> returns `choices` + `usage` envelope (or chunk dicts when streaming); `protocol="responses"` -> OpenAI Responses envelope with `output`, an `items` transcript and cross-turn `usage` (requires bare/`openai/` model name); `protocol="messages"` -> Anthropic Messages (Claude model required). `show_process` is an error with a protocol.
- OpenAI "thinking": docs say "OpenAI models expose none on the chat protocol" (no `[thinking]` text on the default lane). Responses lane streams `ResponseReasoningSummaryTextDeltaEvent` if the model/effort provides summaries (INFERRED: needs `reasoning.summary` in `extra_body`).
- Streaming from a sync wrapper: `_stream_sync` runs the async agent on a background thread and yields synchronously via a queue(32). Safe to call from FastAPI threadpool endpoints; `run_off_loop` handles being called inside a running event loop.
- Usage from the Responses protocol (real recorded, cookbook): `{"input_tokens":40202,"input_tokens_details":{"cached_tokens":23321,"cache_write_tokens":16872},"output_tokens":298,"output_tokens_details":{"reasoning_tokens":71},"total_tokens":40500}` for a single-doc 10-K question with luna = about $0.005.

### 5.2 Your own agent framework + PageIndex tools (VERIFIED `/sdk/agents`)
```python
instructions = client.agent_instructions() + "\n\n" + client.citation_prompt()
tools = client.as_openai_tools()                 # OpenAI Agents SDK; also as_anthropic_tools(), as_claude_mcp(), agent_tools() (plain functions returning JSON strings)
messages = [{"role":"user","content": client.document_context(doc_id)},   # steering only, NOT a restriction
            {"role":"user","content": question}]
result = Runner.run_sync(Agent(name="PageIndex", instructions=instructions, tools=tools, model="gpt-5.6-sol"), messages)
```
One-call bundles: `Agent(**client.openai_agent_config(model="gpt-5.6-sol"))`, `client.anthropic_runner_config(model=...)`, `ClaudeAgentOptions(**client.claude_agent_config(model=...))`. On cloud, `as_openai_tools(hosted=True)` uses OpenAI's HostedMCPTool pointing at `https://api.pageindex.ai/mcp?tools=read` (OpenAI's servers then call PageIndex, INFERRED privacy implication: PageIndex key travels in the tool config). This gives us full control of the loop (e.g. custom prompts, forcing a quote per citation, capturing contexts) without losing PageIndex tools.

### 5.3 Managed chat: REST `POST /chat/completions` (LEGACY, "Chat API (beta)")
VERIFIED (api-reference, openapi.json, `cloud_api.py`, SDK tests).
- `POST https://api.pageindex.ai/chat/completions` (SDK uses trailing slash), header `api_key: ...`, JSON.
- OpenAPI description: "OpenAI-compatible chat completions endpoint with PageIndex MCP server ... Uses the user's API key to authenticate with PageIndex MCP server." Request schema: `messages[{role,content}]` (required, content must be string), `stream` (default false), `doc_id` (string|string[]|null), `folder_id`, `enable_citations` (default false). The docs additionally list `temperature` (0-1) and `stream_metadata` (not in the OpenAPI schema; DISCREPANCY - docs say supported, the schema does not list them; SDK sends them via payload anyway). The model is selected by PageIndex ("the managed cloud chat selects its own model and rejects" model/max_turns/backend params). Which model: NOT DOCUMENTED (the SSE `block_metadata` types `mcp_tool_use_start` etc. resemble Anthropic content-block naming; INFERRED, do not rely on it).
- Non-stream response: `{"id","object":"chat.completion","created","choices":[{"index":0,"message":{"role":"assistant","content":"..."},"finish_reason":"end_turn"}],"usage":{"prompt_tokens","completion_tokens","total_tokens"}}`.
- Streaming: SSE lines `data: {"choices":[{"delta":{"content":"The"}}]}` ... `data: [DONE]`. A server-side failure mid-stream arrives as a final `{"error": ...}` chunk. With `stream_metadata=true` chunks include `block_metadata`: `{"type": "text_block_start"|"text_stop"|"mcp_tool_use_start"|"mcp_tool_use_stop"|"mcp_tool_result_start"|"mcp_tool_result_stop", "tool_name", "server_name", "block_index"}`; tool-argument JSON is interleaved into `delta.content` between `mcp_tool_use_start` and the stop tag (the SDK's `_cloud_chunk_events` has to un-weave it). No tool results and no thinking are streamed.
- Citations: `enable_citations: true` -> inline `<doc=report.pdf;page=12;block=p12_text_3>` (page-only `<doc=file.pdf;page=1>` if the doc has no blocks) AND "the response also carries a `citations` array resolving each tag, with `bbox` for block-level ones". In SSE with `stream_metadata=True` a trailing chunk `{"object": "chat.completion.citations", "citations": [...]}` (VERIFIED in SDK test fixtures). Exact field list of each `citations` entry: NOT DOCUMENTED beyond bbox; the SDK's own `get_citations()` doc says it returns "the managed chat's `citations` entries, plus `doc_id` and the block's `text`" => the managed entries have *no* `doc_id` and *no* `text` (INFERRED). Cited passage text needs a `get_block` call per citation.
- **No retrieved context is returned** (no tool results). Not suitable for RAGAS context metrics except via post-hoc `get_block` of the cited blocks.
- The Python docs now say "The managed cloud chat serves `protocol="chat_completions"` too" but position it as legacy; the JS docs label it "Chat API (legacy)" and recommend MCP tools. Risk: may be deprecated.
- `thinking`/"Thought for N seconds": not streamed here.

### 5.4 Retrieval API (DEPRECATED)
- `POST /retrieval` body `{"doc_id": str, "query": str, "thinking": bool=false, "teamspace_id"?: str}` -> `{"retrieval_id": ...}`; `GET /retrieval/{retrieval_id}` -> status + results. OpenAPI tags POST as **"Internal (Chat Platform)"**; the SDK says "the cloud API marks this endpoint deprecated in favor of chat completions". The current docs have no Retrieval page at all.
- Response shape is not in any current primary source. A third-party project (JhonHander/obstetrics-rag-benchmark, `src/rag/pageindex.py`, written against the old API; INFERRED/secondary) parses it as: poll until `status` in {completed, done, success}; result has `retrieved_nodes: [{ "title", "node_id"?, "relevant_contents": [[{"page_index": N, "relevant_content": "..."}]], "metadata"? }]`. Poll interval 2 s, typical 2-5 s. It returned only snippets, no reasoning path in that parse.
- Verdict: do not build on it. It gave "retrieved text + page index" which was RAGAS-friendly, but is deprecated, internal-tagged and undocumented.

### 5.5 MCP (cloud only)
VERIFIED (docs/mcp, pageindex.ai/mcp.md, server card, SDK bridge).
- `https://api.pageindex.ai/mcp` (streamable HTTP, header `Authorization: Bearer <API key>`); unauthenticated POST returned `401 {"detail":"No authorization credentials provided"}` (I sent one probe; nothing else). `?tools=read` gives the read-only tool set. Supported protocol versions: 2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05. Handshake `initialize` result carries the server's agent `instructions`; MCP **prompt** `cited_answer` (argument `format`: `cite`|`markdown`) returns the citation-discipline prompt.
- `https://mcp.pageindex.ai/mcp` = OAuth 2.0 + dynamic client registration (for Claude/Cursor); `npx -y @pageindex/mcp` = local stdio server that can upload local PDFs (Node >=18); Claude Desktop `.mcpb` bundle on GitHub releases.
- MCP "is the chat and retrieval layer" - documents must be uploaded first (SDK/REST).
- JS SDK `client.tools.*` wraps these tools (`getFolderStructure, browseDocuments, searchDocuments, getDocumentStructure, getPageContent, getDocumentImage, getDocument, removeDocument`) - useful only if we wrote a Node backend.

---

## 6. Citations deep dive (what the user asked for: page AND area)

### 6.1 Formats (VERIFIED)
| Source | Format |
|---|---|
| Own-model chat, cloud doc | `<cite doc="report.pdf" page="12" block="p12_text_3"/>` |
| Own-model chat, local doc | `<cite doc="report.pdf" page="12"/>` |
| Managed chat | `<doc=report.pdf;page=12;block=p12_text_3>` (+ separate `citations` array) |
| markdown alt | `[report.pdf, p. 12, block p12_text_3]` via `citation_prompt(format="markdown")` (NOT parsed by `get_citations`) |

### 6.2 The citation prompt (VERIFIED, `LOCAL_CITATION_PROMPTS["cite"]`; cloud serves the MCP `cited_answer` prompt - same text minus the get_document_image bullet)
```
GROUNDING
- Answer only from the user's PageIndex documents. Call get_page_content() and state only what was actually read there.
- Never fill a gap from general knowledge. When the documents do not answer the question, say so.

CITATIONS
- Cite only statements supported by tool outputs: <cite doc="{docName}" page="{pageNumber}"/> or <cite doc="{docName}" page="{pageNumber}" block="{blockId}"/>. Place immediately after the claim.
- When page content includes block_id values, citations MUST be block-level: copy the exact block_id of the supporting block. Page-only cites are allowed ONLY when the tool output carries no block_id (legacy documents, structure outlines). NEVER invent or alter block_id values.
- For a claim drawn from multiple blocks on one page, add one tag per supporting block (at most 3); beyond that, cite the single strongest block.
- Each tag must reference a SINGLE page integer. For multi-page citations, use separate tags.
```
Enable with `client.chat(q, doc_id=..., citations=True)` (own model) or `instructions=client.citation_prompt()`. Hallucinated block ids are possible ("a block the document does not have (a model's slip) ... keeps its citation without a bbox").

### 6.3 Parsing / resolving (VERIFIED + ran offline)
- Regexes: `<doc=([^;<>]+);page=(\d+)(?:;block(?:_id)?=([^;<>]+))?>` and `<cite\s([^<>]*)>(?:(?P<inner>[^<>]*)</cite>)?` (attributes via `\b(\w+)=(["'])(.*?)\2`). The `<cite ...>quote</cite>` inner-text form is accepted by the parser (and re-emitted after the link) => we could instruct the model to put a short verbatim quote inside the tag (INFERRED to work, not documented).
- `page="12-14"` -> takes first page (`int(page_str.split("-")[0])`), but the prompt says one page per tag.
- Offline test result (my smoke test): input with duplicate and mixed formats returned de-duplicated list `[{"document":"report.pdf","page":15,"block_id":"p15_table_2"},{"document":"report.pdf","page":12,"block_id":"p12_text_3"},{"document":"report.pdf","page":12}]` (old-format tags are matched first, then `<cite>` tags).
- `get_citations(answer, doc_id=None)` -> per distinct citation `{'document','doc_id','page'}` plus, for block citations on cloud docs, `block_id` and everything `get_block` returns (`bbox`, `block_type`, `text`, ...). It resolves doc name -> id by `get_document(doc_id)["name"]` for the given ids, else by listing the whole library (two docs with the same name raise an error -> always pass `doc_id`). It makes **one `get_block` HTTP call per distinct block citation** (404/403 tolerated, others raise).
- `resolve_citations(answer, doc_id=None)` -> `{"answer": "... [[1]](#pageindex-citation-01) ...", "citations":[{"anchor":"pageindex-citation-01","index":1,"document","doc_id","page","block_id"?,"bbox"?,"block_type"?,"text"?}]}`; the same source cited twice reuses its number. Host page renders anchor targets.
- Local-mode citations: `{'document','doc_id','page'}` only. There is NO passage text, NO bbox, NO block id in local mode (VERIFIED in `tests/test_client.py::test_local_get_citations`).

### 6.4 So: do citations include the cited text?
- Cloud, block-level (own-model or managed): YES via the block: `text` (the exact block, e.g. a paragraph or a whole markdown table) + `bbox` + `block_type` + `page`. Obtained by `get_block`/`get_citations` (extra HTTP call).
- Local: page number only. For a passage-level highlight we must find the passage ourselves (options in section 11: ask the model for a verbatim quote; fuzzy-match the answer sentence against the page text; PDF.js `findController`).
- The Sources UI strings in the marketing page say "line-level citations" (pageindex.ai/chat) - that is the block/bbox feature.

### 6.5 Highlighting the cited region
- SDK way (images): `url = client.get_page_image(doc_id, page)`; `highlight_region(requests.get(url).content, bbox)` -> PIL image.
- Our UI way (recommended): render the user's own PDF with PDF.js; draw an absolutely positioned div over the page canvas using `bbox/1000`; fade it out after a few seconds. The bbox coordinate space (0-1000, top-left origin) is page-relative so it works for any render scale. **Caveat:** bbox was computed on the cloud's page rendering/orientation; if the PDF has a non-zero crop box or rotation, the overlay could be offset (INFERRED; test on the National Grid PDF).
- In local mode: highlight via PDF.js text-layer search of the quote/answer snippet on the cited page.

---

## 7. Getting retrieved contexts for RAGAS

RAGAS needs `retrieved_contexts: list[str]` per question (for faithfulness, context precision, and answer relevancy needs the question + answer + embeddings). What each route yields:

| Route | Contexts available? | How |
|---|---|---|
| `client.chat(stream=True).events` (own model; local or cloud docs) | **Yes, all of them** | Collect `{"type":"tool_result","name":"get_page_content","output": <JSON string>}`; `json.loads(output)["content"]` -> `[{"page": N, "text": "..."}]` (cloud adds blocks). Also note `get_document_structure` results (outline + summaries) are NOT evidence: exclude from contexts or keep as separate field. |
| `client.chat(..., protocol="responses", stream=True)` (own OpenAI model) | **Yes** | Final `response["items"]` contains `{"type":"function_call_output","output": ...}` (the multimodal notebook reads it exactly like this) and `function_call` items (name/arguments). `response["usage"]` gives tokens. Event types: `response.output_text.delta`, `response.output_item.done`, `response.completed|incomplete|failed`. |
| Custom agent via `as_openai_tools()` | Yes | Run hooks / `result.new_items` from the OpenAI Agents SDK. |
| Managed chat | No (only cited blocks via `get_block`) | Weak: context precision would be computed on cited text only. |
| Deprecated Retrieval API | Snippets + page_index (secondary source) | Not recommended. |

Two context definitions we should log per question (decide with RAGAS researcher):
1. `retrieved_contexts_read` = text of every page/block the agent *read* (what the generator saw). Matches RAGAS meaning of "retrieved". Could be long (pages of annual reports) - mind judge-LLM cost and context length.
2. `retrieved_contexts_cited` = text of the cited blocks (cloud) or the cited pages (local). Tighter; makes context precision look high by construction.

Skeleton (illustrative, not run; needs keys):
```python
def answer_with_trace(client, doc_id, question, history=()):
    msgs = [*history, {"role": "user", "content": question}]
    stream = client.chat(msgs, doc_id=doc_id, stream=True, citations=True)   # ChatStream
    answer, pages, steps = [], [], []
    for ev in stream.events:                       # consume ONE view only
        if ev["type"] == "answer":     answer.append(ev["delta"])
        elif ev["type"] == "tool_call": steps.append(ev)                    # UI: "Read pages 88-90"
        elif ev["type"] == "tool_result" and ev["name"] == "get_page_content":
            out = ev["output"] if isinstance(ev["output"], str) else "\n".join(i.get("text","") for i in ev["output"])
            data = json.loads(out)
            if data.get("success"): pages += data["content"]                # [{"page":..,"text":..}]
    text = "".join(answer)
    resolved = client.resolve_citations(text, doc_id=doc_id)                # {"answer","citations"}
    return text, resolved, pages, steps
```
Caveats: (a) `.events` gives no usage numbers; for token/cost accounting use the Responses protocol or LiteLLM callbacks. (b) With OpenAI models on the default lane there is no thinking text. (c) tool `output` may be a string or a list of structured items (text/image) - handle both (`_output_text` in source does).

---

## 8. How the PageIndex app UI is produced (what we should replicate)

I could not log in (account creation is out of scope) and your attached screenshots were not visible to me, so this section combines: PageIndex's public marketing screenshots (downloaded from pageindex.ai and viewed), SDK/MCP behavior, and your description of the target UI. **Everything about UI internals is INFERRED** unless noted.

### 8.1 What I could see (VERIFIED visually, public images on pageindex.ai)
- Older app screenshot (blog "pageindex-vs-chatgpt"): left icon rail; top bar with Share/Upgrade/user; answer in markdown with headings and **inline blue pill citations such as `enw_bp_2023-2028.pdf p.71`** after each claim; bottom composer "Ask a question..." with "+ Add documents" button and a settings icon; footer "PageIndex can make mistakes, please check the response."
- Newer app marketing image (`chat-feature-1.png`): **hovering a pill (`INTC_8-...000155.pdf p.4`) shows a card: PDF icon, file name, "Page 4 of 18", a "Text block" tag, a thumbnail + quoted snippet of the cited block, and a blue "Open page" button.** Clicking opens a **right-hand PDF viewer panel with tabs per document** (`TSLA_10...pdf | INTC_8-...pdf | 2026q2-...pdf`, close X) scrolled to the page, with the cited block **highlighted in blue/yellow tint**. This matches "block text + page N of M + block_type + bbox" from the cloud citation resolver 1:1.

### 8.2 Mapping UI elements to data (INFERRED)
| UI element (from your spec) | Likely source |
|---|---|
| Citation chip "Report.pdf p.89" | one `<cite doc= page= block=/>` tag -> label `{doc} p.{page}`; numbered by `resolve_citations` `index` |
| Hover card: page "N of M", "Text block", snippet | `get_block()` -> `page`, `block_type`, `text`; M = `get_document()["pageNum"]` |
| Click -> side PDF viewer scrolls to page, highlights text | PDF viewer jumps to `page`, overlays `bbox/1000` rectangle |
| "Sources panel: 19 references - 1 document" | aggregate over the resolved citations of the message: count distinct (doc, page, block), group by document |
| Agent steps "Read pages 88-90 from Report.pdf" | rendering of `tool_call` events: `get_page_content {"doc_name": ..., "pages": "88-90"}`; other steps from `get_document_structure` ("Read document outline"), `browse_documents` ("Looked up documents"), `get_document_image` ("Viewed image on p.5") |
| "Thought for N seconds" | wall-clock from run start to first answer delta (OpenAI models stream no reasoning text on chat protocol); with Claude + `reasoning_effort` real `[thinking]` text exists |
| Side viewer "Add Page to Chat" | no PageIndex API for it; UI action that appends that page's text (via `get_page_content`) as extra context/user message for the next turn (INFERRED, our own feature) |
| Multiple docs in tabs | multi-`doc_id` chat; our spec has one doc per session so a single tab |

### 8.3 The agent trace in real life (VERIFIED, official cookbook output, NVIDIA 10-K, one doc)
```
[Tool call] get_document_structure({"doc_name":"NVIDIA-FY2026-10-K.pdf","folder_id":"cmu3rre91000r0cp4qb90h0fa","part":1})
[Tool call] get_page_content({"doc_name":"NVIDIA-FY2026-10-K.pdf","folder_id":"...","pages":"37-40"})
NVIDIA's revenue in the prior fiscal year (fiscal 2025 ...) was **$130.497 billion**. <cite doc="NVIDIA-FY2026-10-K.pdf" page="37" block="p37_table_6"/>
```
Typical loop for one question: outline -> 1-3 targeted `get_page_content` calls -> answer with one `<cite/>` per claim. Multi-doc question (5 years of 10-Ks): `get_folder_structure`, N x `browse_documents`, N x `get_document_structure`, then page reads: 210k input tokens, about $0.031 with luna.

---

## 9. Models, keys, limits, pricing, privacy

### 9.1 OpenAI models named by the docs (VERIFIED; prices from OpenAI's own model pages today)
| Model | Role in docs | Input / cached / output per 1M tok | Context | Endpoints |
|---|---|---|---|---|
| `gpt-5.6-luna` | default `index_model` (also usable as chat) | $0.20 / $0.02 / $1.20 | 1.05M | Chat Completions + Responses |
| `gpt-5.6-terra` | mid tier (cookbook rate table) | $2.00 / $0.20 / $12.00 (cookbook table, "checked 2026-09-17"; not re-verified on OpenAI page) | | |
| `gpt-5.6-sol` | default `chat_model` ("best you can afford") | $4.00 / $0.40 / $20.00; >272K-token prompts cost 2x input / 1.5x output; `reasoning.effort`: none, low, medium (default), high, xhigh, max | 1.05M | Chat Completions + Responses |
Sources: https://developers.openai.com/api/docs/models/gpt-5.6-sol and `/gpt-5.6-luna`. (A third-party aggregator listed different prices, $5/$30 and $1/$6; OpenAI's own pages and the PageIndex notebooks agree with the table above, so I use them.)
Per-query cost estimate (INFERRED from recorded token counts + price ratio): luna about $0.005 (single doc, 40k tokens) to $0.03 (multi-doc, 210k tokens); sol is 20x luna on input and about 17x on output, so roughly $0.10-0.60 per question at the same token counts (prompt caching lowers it). For a 308-page report expect 30-100k input tokens/question.
Also: `chat=` can be any LiteLLM model (`anthropic/claude-opus-5`, `openrouter/...`, `openai/<name>` + `OPENAI_BASE_URL`).

### 9.2 Which keys (decision-relevant)
| Setup | PAGEINDEX_API_KEY | OPENAI_API_KEY |
|---|---|---|
| Local index + own chat | no | **yes** (indexing summaries + chat) |
| Cloud index + own chat | **yes** | **yes** (chat only) |
| Cloud index + managed chat | **yes** | no |
| RAGAS judge LLM + embeddings (separate from PageIndex) | n/a | yes (RAGAS default uses OpenAI) |

### 9.3 Pricing (VERIFIED, docs.pageindex.ai/pricing, changelog 2026-09-16)
- Indexing **$0.01/page**, one-time. Active pages **$0.001/page/month** (billed at month end, prorated, **first 1,000 pages free**). Retrieval: **unlimited**, no fee on active pages; "You pay your LLM provider directly". **$10 free credits, no credit card.** Example in docs: 3,000 pages = $30 one-time + $2/month.
- 308-page annual report: **$3.08** one-time (31% of free credits), monthly $0 (under 1,000 active pages).
- Legacy Standard/Pro/Max subscribers are grandfathered (credits = plan price, no active-page fee).
- PageIndex Chat (consumer app) plans, different product: Free (1,000 pages, 100 chat messages, basic MCP), Pro from $20/month, Team custom (pageindex.ai/chat).
- Local mode: free and open source; cost = your OpenAI tokens, benchmark "about $0.001 per page" with luna (9-1,098 pages; 13 s to 4.5 min). 308 pages: about $0.3 and about 1-2 minutes (INFERRED interpolation).
- Query-cost helper notebook: `cookbook/pageindex-query-pricing-demo.ipynb` (reads `usage` from the Responses protocol).

### 9.4 Limits (what exists, what doesn't)
| Item | Finding |
|---|---|
| Max pages per document | **NOT DOCUMENTED.** Evidence it is large: marketing shows 760- and 1,056-page examples; Fed report 222 pages in blog; OSS benchmark docs up to 1,098 pages. A 308-page report is well within demonstrated sizes. |
| Max file size | **NOT DOCUMENTED.** |
| REST rate limits | **NOT DOCUMENTED** (docs: "higher rate limits" available via sales). Error codes exist: JS SDK `UNAUTHORIZED 401`, `NOT_FOUND 404`, `RATE_LIMITED 429`, `USAGE_LIMIT_REACHED 403`, `INVALID_INPUT 400`, `SERVICE_UNAVAILABLE 503`, `INTERNAL_ERROR 500` (Python source maps `USAGE_LIMIT_REACHED` to 402 in tool errors: docs vs code disagree). MCP bridge retries 3x on 429/5xx. |
| Processing time (cloud) | NOT DOCUMENTED; SDK waits up to 30 min. |
| Free tier | $10 credits (= 1,000 pages indexing) per docs/pricing; Terms: free trial data "will become inaccessible upon termination or expiry of the Free Trial" and may be deleted (Terms cl. 2.3). |
| Agent tool response cap | 100,000 chars per tool call; `max_turns` default 10; MCP tool timeout (10 s connect, 240 s read). |
| Doc listing page size | 1-10,000 (SDK) |

### 9.5 Data handling / privacy (VERIFIED from pageindex.ai/terms.md, enterprise.md)
- Cloud: documents + derived tree/OCR stored on PageIndex servers until you delete (`delete_document`: "Permanently delete a document and all its associated data").
- Terms cl. 8.4: the Supplier "may use the Customer Data to improve the performance and functionality of the Software" (Vectify AI Limited, UK). Zero Data Retention, private cloud/VPC/on-prem only via sales/enterprise.
- Own-model chat on cloud docs: "page content then flows through your process to your model provider" (client.py docstring) - i.e. text goes PageIndex -> our server -> OpenAI.
- Local mode: "your documents never leave" the machine except what is sent to the LLM provider you configure.
- Annual reports are public, so low sensitivity; still show a notice in UI.

---

## 10. Decision table: Cloud vs self-hosted (local/OSS) for THIS project

Both are reached through the same `PageIndexClient`; "Cloud" below = `index="cloud"` + own OpenAI chat model (the managed-chat variant is shown separately).

| Need | Local (Flash, `index="gpt-5.6-luna"`) | Cloud index + own chat | Cloud managed chat (legacy) |
|---|---|---|---|
| (a) Retrieved contexts for RAGAS | **Yes**, full tool outputs via `.events` / Responses items | **Yes**, same (page text from cloud MCP/OCR) | **No** (only cited blocks via `get_block`) |
| (b) Page-level citations | Yes (`<cite doc page/>`) | Yes | Yes (`<doc=..;page=..>`) |
| (b) Passage/area-level citations | No bbox/text natively; need quote prompting + PDF.js text match | **Yes**: `block` id -> `text` + `bbox` + `block_type` (docs indexed since 2026-09-13) | Yes: `citations[]` with bbox; text via `get_block` |
| (c) Keys | OpenAI only | PageIndex + OpenAI | PageIndex only (+ OpenAI for RAGAS) |
| (d) Cost for 308 pp | about $0.3 index + tokens | $3.08 index (from $10 free credits) + tokens | $3.08 + (chat cost not stated) |
| (e) Control of prompts/models | Full: `instructions=`, custom agent via `as_openai_tools()`, any LiteLLM model, `max_turns`, `reasoning_effort` | Same | Minimal (`temperature`, `enable_citations`; model chosen by PageIndex) |
| (f) FastAPI-conversion friendliness | Good: pure Python, sync API, no external service, async-safe via threadpool; state = `./.pageindex` dir | Good, but network dependency + a second secret; OCR fetch is heavy | Easiest wire (plain REST/SSE) but legacy/undocumented fields |
| Text-layer vs scanned PDFs | Text-based only (no OCR) | OCR + image understanding | same as cloud |
| Charts / figures | no image understanding | yes (`get_document_image`; multimodal cookbook) | yes |
| Data leaves the machine | only to OpenAI | to PageIndex + OpenAI | to PageIndex (+ whatever model they use) |
| Persistence / multi-session | doc store on disk, `doc_id` stable | server-side, `doc_id` stable | server-side |
| Risk | Newest code path (Flash announced 2026-08-26; SDK 0.2.x churns weekly) | Account/billing dependency; undocumented size/rate limits | Marked legacy; may disappear; no contexts |

**Verdict:** Local Flash + own OpenAI chat = the best default for a dev/UAT phase (one key, free to re-index, full contexts, full control). Cloud index + own chat = the best upgrade if pixel-exact highlighting or scanned/figure-heavy PDFs matter: only the constructor line changes. Avoid managed chat for RAGAS.

---

## 11. Recommendations for the build

1. **Pin** `pageindex==0.2.21` (or whatever we test) in requirements; the SDK shipped 20+ releases in 2026 and the surface moved (e.g. `page_index` -> `start_index/end_index`, `retrieve_model` -> `chat_model`). Keep a thin adapter module `pi_adapter.py` around: `index_document(path) -> doc_id`, `get_tree`, `ask(doc_id, history, question) -> {answer, citations, contexts, steps, usage}`, `get_page_text(doc_id, page)`.
2. **Config switch** `PAGEINDEX_MODE=local|cloud` choosing `PageIndexClient(index="gpt-5.6-luna"|"cloud", chat=CHAT_MODEL)`; models from env (`INDEX_MODEL`, `CHAT_MODEL`), default luna/sol per docs; allow luna for chat during dev to save money.
3. **Session = one document:** store `doc_id` per session in our DB. Upload happens before first chat (UI gate). New chat = new session (+ new upload; or reuse an existing doc_id via `list_documents()`/`get_document_id` to avoid re-billing, optional). In cloud mode consider a per-session folder (optional).
4. **Chat call:** `client.chat(history, doc_id=doc_id, stream=True, citations=True)` and consume `.events` (typed) to drive the UI: `tool_call` -> "Read pages 88-90" step rows; `answer` deltas -> streaming markdown; collect `tool_result` of `get_page_content` as contexts. Never mix the text and events views on one stream. Do not append woven (`show_process`) text to history.
5. **Citation UX:** after the answer completes, `resolve_citations(answer, doc_id)`; render chips from `index/document/page`; hover card from `text`, `block_type`, `page`/`pageNum`; click -> viewer opens `/api/sessions/{id}/pdf` at page; draw bbox (cloud) or quote-search highlight (local). Cache `get_block` results per (doc_id, block_id). Serve the user's own PDF from our backend (we have the original file) to PDF.js; do not depend on `get_page_image` URLs (short-lived).
6. **Local-mode passage highlighting plan:** extend instructions: "After each `<cite doc=.. page=..>` put a 5-12 word verbatim quote from the page: `<cite doc=.. page=..>quote</cite>`" (parser tolerates inner text; INFERRED), then PDF.js `findController.executeCommand('find', {query: quote, highlightAll:false})` with fallback to the first matched sentence on that page. Validate quotes against `get_page_content(doc_id, page)` text (reject non-verbatim quotes).
7. **RAGAS inputs:** `user_input`=question, `response`=answer with `<cite>` tags stripped (so tags don't pollute faithfulness/relevancy judging), `retrieved_contexts`=texts of pages/blocks read (see section 7; maybe truncate/strip markdown tables to stay within judge context), `reference` optional (not needed for faithfulness/answer_relevancy; context_precision w/o reference uses response-based variant - check with the RAGAS researcher).
8. **Latency/cost guard rails:** `max_turns` (default 10), cap pages per call via prompt, stream first token quickly; show agent steps live (retrieval happens *during* generation: no separate retrieval phase per PageIndex docs).
9. **FastAPI later:** the adapter's methods are sync; expose `ask()` as an SSE generator running in a threadpool (`StreamingResponse(gen())` with a sync generator). Persist `doc_id`, sessions, messages, citation payloads, RAGAS scores in SQLite. `.pageindex/` dir must be a persistent volume in local mode.
10. **Fallback if Flash chokes on the PDF** (charts, multi-column): `mode="standard"` in local `submit_document`, or switch to cloud (OCR).

---

## 12. Gotchas and discrepancies noticed

- Docs vs SDK on "chat is always your own model": Python docs say so, but SDK source still supports managed chat when `chat_model` is unset (`PageIndexClient(index="cloud")`). Docs hide it; do not rely on it.
- `citations=True` on managed chat is `enable_citations`; on own-model it adds the prompt only; `chat_completions(enable_citations=True)` on own-model **raises**.
- `get_tree()` default includes node `text` (big for 308 pages). Always pass `include_text=False` for the UI outline.
- Cloud tree uses `page_index`; SDK-normalized uses `start_index/end_index`; raw REST (`api-reference`, JS SDK docs) still shows `page_index` + `text`.
- Raw-REST `Get Document Metadata` example in docs lacks `metadata` / `folderId` details that the SDK returns.
- OpenAPI says `GET /docs` list limit 1-100 (REST docs) while SDK allows 10,000; trust the SDK.
- API reference header says `api_key:`; MCP says `Authorization: Bearer`; the dashboard link in docs is `dash.pageindex.ai` but SDK docstrings and llms.txt say `developer.pageindex.ai` (same app).
- `show_process` stream text is a display format, not machine-readable; use `.events`.
- Same document name -> `_1` suffix; use unique names (e.g. `{session_id}.pdf`) to avoid cross-session ambiguity; `get_citations` errors when two docs share a cited name unless `doc_id` is passed. Since the model sees the *stored name* (`doc_name`) and writes it into `<cite doc=...>`, **name the uploaded file something human-friendly but unique**, because the chip label will be that string (e.g. `National-Grid-Annual-Report-2024.pdf`), and strip a session prefix in the UI.
- Windows: long-path pip failure (section 3); use short venv path.
- PageIndex `utils.py` calls `load_dotenv(find_dotenv(usecwd=True))` on import: a stray `.env` in CWD can inject keys.
- `get_page_content` via SDK downloads all OCR (section 4.4).
- Hosted MCP tool (`as_openai_tools(hosted=True)`) sends the PageIndex key to OpenAI's infrastructure - avoid.
- `PAGEINDEX_API_KEY` not set + `index="cloud"` => `PageIndexAPIError` (verified message).

---

## 13. Unknowns / not verified (need a key or a login)

1. Exact JSON of the cloud MCP `get_page_content` result with block ids (field names for block ids inside `content[]`). The prompt says "page content includes block_id values" but I saw no recorded sample. Needs a key (cookbook notebooks use a public read-only demo key string, `pageindex_demo_readonly_key`, but I did not use it: instructions forbid obtaining/using keys; the lead may decide).
2. Exact fields of the managed chat `citations[]` entries and of the trailing SSE citations chunk.
3. Max pages/file size/rate limits, cloud indexing time for 308 pages, free-tier hidden caps.
4. Which LLM the managed chat/app uses; app's UI internals ("Thought for N seconds", "Add Page to Chat", "19 references - 1 document") - not in any public doc.
5. Quality of Flash tree on the National Grid PDF (needs a run with an OpenAI key; Flash needs the PDF text layer and benefits from embedded bookmarks).
6. Whether `<cite>quote</cite>` inner text is honored by the model reliably.
7. Whether cloud bbox aligns with PDF.js rendering for rotated/cropped pages.

---

## 14. Smoke test log (offline, no network to PageIndex/OpenAI)
Environment: Windows 11, Python 3.13.3, throwaway venv `C:\pi02` (short path), `pip install pageindex==0.2.21`.
- Import OK; `PageIndexClient(index="cloud", chat="gpt-5.6-sol")` with a dummy `PAGEINDEX_API_KEY` constructs without network: `chat_model == "gpt-5.6-sol"`, `_local_chat is True`, `BASE_URL == "https://api.pageindex.ai"`.
- `PageIndexClient(index="cloud")` -> `chat_model is None`, `_local_chat is False` (= managed chat).
- Without `PAGEINDEX_API_KEY`: `PageIndexAPIError: index="cloud" reads the PageIndex API key from the PAGEINDEX_API_KEY environment variable, which is not set ...`.
- `highlight_region(PIL image 800x1000, [78,24,293,44])` returned an 800x1000 RGB image.
- `_parse_citations` on mixed `<cite .../>` and `<doc=..;page=..;block=..>` text: results as in section 6.3.
- Unauthenticated `POST https://api.pageindex.ai/mcp` initialize -> HTTP 401 `{"detail":"No authorization credentials provided"}`, `server: uvicorn`, `www-authenticate: Bearer`.
Throwaway artifacts live under the scratchpad `...\scratchpad\r02\` (docs HTML/text dumps, wheel, notebooks, openapi.json, marketing screenshots).

---

## 15. Source URLs (all fetched 2026-10-07)
- https://docs.pageindex.ai/ , /getting-started , /SKILL.md , /sdk , /sdk/client , /sdk/documents , /sdk/chat , /sdk/agents , /js-sdk , /js-sdk/documents , /js-sdk/mcp-tools , /js-sdk/legacy/chat , /mcp , /api-reference , /pricing , /cookbook , /changelog , /open-source , /tutorials , /tutorials/tree-search/llm , /tutorials/doc-search
- https://api.pageindex.ai/openapi.json
- https://pageindex.ai/llms.txt , /.well-known/mcp , /chat.md , /api.md , /mcp.md , /developer.md , /enterprise.md , /privacy.md , /terms.md , /policies.md
- https://pageindex.ai/blog/pageindex-intro , /pageindex-chat , /pageindex-flash , /pageindex-filesystem , /pageindex-vs-chatgpt
- https://pypi.org/project/pageindex/ (JSON API: version 0.2.21, 2026-10-01) ; wheel `pageindex-0.2.21-py3-none-any.whl`; `pageindex-0.2.8` wheel (for legacy retrieval signature)
- https://github.com/VectifyAI/PageIndex (README, releases v0.2.14-v0.2.21, `cookbook/*.ipynb`, `tests/test_client.py`, `tests/test_local_chat.py`)
- https://github.com/VectifyAI/pageindex-mcp (README, src/)
- https://developers.openai.com/api/docs/models/gpt-5.6-sol , /gpt-5.6-luna
- Secondary (flagged): https://mintlify.wiki/JhonHander/obstetrics-rag-benchmark/api/rag/pageindex and raw `src/rag/pageindex.py` (legacy retrieval response shape)
