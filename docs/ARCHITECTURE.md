# ReportLens - architecture and module contracts

> Status: contract for the first build (2026-10-07). **This file + `reportlens/models.py` + `reportlens/config.py` are the
> source of truth.** If an implementation must deviate, change the contract first (and tell the other owners), never silently.
> Background research lives in `research/` (start with 01, 02, 03, 04, 05, 06, 07).

## 1. What the product does

1. A user opens the web UI and starts a **session** (chat). A session holds **exactly one PDF** (an annual report), which must be
   uploaded **before** the first question. After the first question the document is locked.
2. The upload is **indexed** by PageIndex (local "Flash" mode: a hierarchical table-of-contents *tree* with node summaries; no vectors).
3. Questions are answered by the **PageIndex agent** (reasoning-based retrieval): it reads the tree (`get_document_structure`),
   picks page ranges (`get_page_content("88-90")`), reads them and answers. The answer carries `<cite doc=".." page="N" quote=".."/>` tags.
4. Every citation is resolved to: physical page, printed folio, tree breadcrumb (the "area"), a **verified verbatim passage** and
   highlight rectangles. Clicking a chip opens the PDF in a right-hand panel, jumps to the page and flashes the highlight.
5. After the answer, **RAGAS** scores it: *faithfulness*, *answer (response) relevancy*, *context precision*, using the pages the agent
   actually read as `retrieved_contexts`. Scores stream in after the answer (they take 10-60 s) and are shown under the answer.

```
 browser (vanilla ES modules, pdf.js)                      FastAPI (reportlens.web)
 ┌───────────────┬──────────────────────┬──────────────┐        │  REST + SSE (POST /messages)
 │ sidebar       │ chat                 │ PDF panel    │ ◄──────┤
 │ sessions      │ steps · answer · chips│ highlight   │        ▼
 └───────────────┴──────────────────────┴──────────────┘   ReportLensService  (reportlens.service)
                                                             │        │         │          │
                                                    Store(sqlite)  IndexService  QAEngine   Evaluator
                                                                      │             │          │
                                                              PageIndex SDK   PageIndex SDK   RAGAS 0.4.3
                                                              (Flash, local)  (agent loop)    (OpenAI judge)
                                                                      └──── OpenAI (or devtools.mock_openai) ────┘
        pdf helpers: pdfutil (pdfium text, labels) · locator (quote -> rects) · citations (parse, resolve, markers)
```

## 2. Decisions (with the reason; details in research/)

| # | Decision | Why |
|---|---|---|
| D1 | `pageindex==0.2.21`, **local mode** (`PageIndexClient(index={"model":..,"storage_path":..}, chat=..)`), cloud reserved behind `PAGEINDEX_MODE` | Needs only `OPENAI_API_KEY`; gives us every retrieved page (RAGAS contexts); the SDK is the thing the PageIndex docs prescribe. The SDK changes weekly: pin, wrap, never import it outside `pageindex_compat.py`, `indexer.py`, `qa.py`. |
| D2 | Models per PageIndex docs: index `gpt-5.6-luna`, chat `gpt-5.6-sol`, effort `medium`, **Responses protocol** (`chat(..., protocol="responses")`); all env-configurable | Docs/README default. Chat-Completions lane + tools + reasoning is refused by gpt-5.6/gpt-6 (HTTP 400). A `chat` lane (`PI_CHAT_PROTOCOL=chat`) is kept as a fallback. |
| D3 | One PageIndex `storage_path` **per session** (`data/sessions/<sid>/pageindex`) | DocStore lock is a no-op on Windows; delete session = rmtree; "new chat with same document" = copy the folder. |
| D4 | Override the SDK's page text (PyPDF2, corrupts digits on ~23 % of a real report) with **pypdfium2** text (`LocalAPI._extract_page_texts`) | The agent reads, RAGAS judges and the highlighter searches the SAME clean text. |
| D5 | Citations: model emits `<cite doc page quote?/>`; we **verify the quote on the cited page** (exact -> fuzzy -> numeric guard), else **align the claim sentence** to the best span on the cited page, else highlight the page. Highlights computed **server-side** with pdfium word boxes (+rapidfuzz). | Local PageIndex citations are page-level only. PyMuPDF is AGPL - not used. |
| D6 | RAGAS 0.4.3 **collections API** (`ragas.metrics.collections`), judge `gpt-4.1` (mini loops on long NLI prompts), embeddings `text-embedding-3-small`; `retrieved_contexts` = one context per page read, in read order; async, one `AsyncOpenAI` client per event loop | Verified minimal example: `research/ragas_minimal_example.py`. Pin `langchain-community==0.4.1`. |
| D7 | Front end: vanilla ES modules + CSS variables (design tokens from research/06), **vendored** pdf.js / marked / DOMPurify / Lucide / Geist (no CDN at runtime), served by FastAPI static files | Pixel fidelity to the reference, no build step on Windows, trivial to hand to a FastAPI team. |
| D8 | Persistence: SQLite (`data/reportlens.db`) + files in `data/sessions/<sid>/` (`<doc_name>.pdf`, `pageindex/`) | Zero-ops, restart-safe. |
| D9 | Offline demo/mock: `devtools/mock_openai.py` (fake OpenAI server) + `REPORTLENS_DEMO_MOCK=1` | The whole UI + E2E tests run without a key or spend. Real-model behaviour still needs one live UAT pass. |

## 3. Repository layout and OWNERSHIP

Agents edit **only files they own**. Anything else: read-only (ask the orchestrator to change the contract).

```
pyproject.toml, .env.example, .gitignore, requirements.lock.txt      orchestrator
docs/ARCHITECTURE.md, reportlens/models.py, reportlens/config.py     orchestrator (contract)
tests/conftest.py                                                    orchestrator
reportlens/
  pdfutil.py        PDF inspection, clean page text, printed labels                      [pdf-citations]
  locator.py        quote/claim -> highlight rects (pdfium words + rapidfuzz)            [pdf-citations]
  citations.py      parse <cite>, stream filter, resolve -> Citation/Source              [pdf-citations]
  evaluation.py     RAGAS scorer                                                         [evaluation]
  store.py          SQLite persistence                                                   [store]
  pageindex_compat.py  the ONLY place that patches/constructs the SDK                    [pageindex-layer]
  indexer.py        background indexing + progress + tree loading                        [pageindex-layer]
  qa.py, pricing.py agent run -> events, contexts, usage                                 [qa-engine]
  service.py        orchestration (sessions, upload, ask, evaluate, locate)              [service-web]
  web/app.py, web/routes.py, web/sse.py, web/static/** (see 3.1)                         [service-web] + UI owners
  __main__.py       `python -m reportlens [--demo] [--port N]`                           [service-web]
devtools/mock_openai.py, devtools/__init__.py                                            [mock-and-samples]
scripts/make_sample_pdf.py, samples/                                                     [mock-and-samples]
tests/fixtures_sample.py, tests/fixtures_mock.py                                         [mock-and-samples]
tests/test_<module>.py                                                                   each module's owner
```

### 3.1 Front-end files
```
reportlens/web/static/index.html                          [ui-shell]
reportlens/web/static/css/tokens.css, base.css, app.css   [ui-shell]
reportlens/web/static/css/viewer.css                      [ui-viewer]
reportlens/web/static/js/main.js, api.js, state.js, sse.js, markdown.js, chat.js, sidebar.js, upload.js, scores.js, icons.js   [ui-shell]
reportlens/web/static/js/viewer.js                        [ui-viewer]
reportlens/web/static/vendor/**  (pdfjs, marked, dompurify, lucide sprite, geist fonts)   [ui-viewer owns pdfjs; ui-shell owns the rest]
```

## 4. Python module contracts

All code: Python >= 3.10 (target 3.13), type hints, `logging.getLogger("reportlens.<module>")` (no `print`), `pathlib`, UTF-8 everywhere.
Blocking SDK/PDF work is sync; the service moves it to threads (`asyncio.to_thread`).

### 4.1 `pdfutil.py`  [pdf-citations]
```python
class PdfError(Exception):   # .code in {"invalid_pdf","encrypted_pdf","scanned_pdf"}, .message
@dataclass(frozen=True) class PdfInfo: page_count:int; sizes:list[tuple[float,float]]  # visible pts, after /Rotate+CropBox
                                       text_pages:int; title:str|None
def inspect_pdf(path: Path) -> PdfInfo            # raises PdfError. scanned = text_pages < 5% of pages (and > 0 pages)
def extract_page_texts(path: Path) -> list[str]   # pdfium; index 0 == page 1; "\n" newlines; same text the agent will read
def detect_printed_labels(path: Path, page_texts: list[str] | None = None) -> list[str | None]
        # len == page_count. Order: PDF /PageLabels (pdfium get_page_label) when meaningful -> footer/header digit voting
        # (offset = printed - physical, mode of per-page candidates; Roman numerals for front matter) -> None
```
### 4.2 `locator.py`  [pdf-citations]  (productionise `research/quote_locator_prototype.py`; backend = pypdfium2 only)
```python
class PdfiumDoc:                         # opened from BYTES (no Windows file lock), thread-safe via an internal lock
    def __init__(self, data: bytes | Path): ...
    page_count: int
    def page_words(self, page: int) -> PageWords       # 1-based
    def close(self) -> None
@dataclass class LocateResult: page:int; hinted_page:int; method:str; score:float; rects:list[dict]; matched_text:str; ...
def locate_quote(doc: PdfiumDoc, page: int, quote: str, *, neighbours: int = 1) -> LocateResult
def align_claim(doc: PdfiumDoc, page: int, claim: str, *, neighbours: int = 0) -> LocateResult | None
        # no quote available: best-supporting span for an answer sentence on `page` (number tokens weighted); None if weak
```
Thresholds (calibrate in UAT): exact = 1.0; fuzzy >= 0.90 accepted; 0.78-0.90 "repaired" (accepted, flagged by lower `match_score`);
below -> not located. **Numeric guard**: if the quote contains numbers and the matched span's numbers differ, reject.

### 4.3 `citations.py`  [pdf-citations]
```python
CITE_RE  # <cite ... />  attributes parsed with a quote-tolerant parser (model may put " inside quote=)
@dataclass class RawCite: doc:str|None; page:int; quote:str|None; start:int; end:int
def parse_cites(raw: str) -> list[RawCite]

class CiteStreamFilter:                 # incremental, for token streaming
    def feed(self, delta: str) -> list[tuple]      # ("text", str) | ("cite", RawCite, n)   n = 1-based marker number
    def flush(self) -> list[tuple]                 # never emits half a tag; at end an unterminated "<cite" is emitted as text
MARKER = "[[c{n}]]"                    # display markers; one per <cite> occurrence, numbered in answer order

@dataclass class CitationContext:
    doc_display_name: str               # shown in chips ("National Grid_Annual_Report.pdf")
    pdf: PdfiumDoc
    tree: list[dict]                    # PageIndex nodes: title,node_id,start_index,end_index,summary,nodes
    printed_labels: list[str|None]
    read_pages: set[int] | None = None  # pages the agent read (a cite to an unread page is flagged in stats)

@dataclass class BuiltAnswer: text:str; citations:list[Citation]; sources:list[Source]; stats:dict
def build_answer(raw: str, ctx: CitationContext) -> BuiltAnswer
def resolve_cite(raw: RawCite, claim: str | None, ctx: CitationContext, n: int) -> Citation   # the per-cite pipeline
def tree_path_for_page(tree: list[dict], page: int) -> tuple[list[str], str | None, list[int] | None]  # breadcrumb, node_id, [start,end]
def strip_markers(text: str) -> str    # remove [[cN]] (history for the agent, text for RAGAS)
def claim_for(raw_answer: str, cite_start: int) -> str   # the sentence/bullet immediately before the tag, markdown/tags stripped
```
`stats` = `{"n_cites", "n_quote_verified", "n_aligned", "n_page_only", "n_unread_pages", "n_dropped"}`. A cite whose page is outside
`1..page_count` is dropped (marker removed) and counted in `n_dropped`. Never raise on malformed model output.

### 4.4 `evaluation.py`  [evaluation]  (productionise `research/ragas_minimal_example.py`)
```python
METRICS = ("faithfulness", "answer_relevancy", "context_precision")
METRIC_INFO: dict[str, dict]     # {"label","short","tooltip"} - honest wording, exported via GET /api/config
class Evaluator:
    def __init__(self, settings: Settings): ...            # no network in __init__; clients are created lazily inside the running loop
    async def evaluate(self, question: str, answer: str, contexts: list[ContextPage],
                       on_metric: Callable[[str, float | None, str | None], Awaitable[None] | None] | None = None) -> EvalScores
    async def aclose(self) -> None
    @staticmethod
    def skipped(reason: str) -> EvalScores
```
`answer` = display text WITH `[[cN]]` markers (the evaluator strips them). Never raises: failures land in `EvalScores.errors`;
`status` = done (3 values) | partial (1-2) | failed (0). `on_metric` is called as each metric finishes (drives `eval_result` SSE events).
`context_verdicts[i]` includes `page`. Respect `settings.eval_max_contexts`, `eval_max_chars_per_context`, `eval_concurrency`.

### 4.5 `store.py`  [store]
SQLite via stdlib `sqlite3`, one connection per call or per thread, WAL, a process-wide lock for writes; JSON columns for nested models.
```python
class Store:
    def __init__(self, db_path: Path)                        # creates schema
    def create_session(self, title: str = "New chat") -> Session
    def get_session(self, sid: str) -> Session | None        # computes .state and .message_count
    def list_sessions(self) -> list[Session]                 # newest updated first
    def rename_session(self, sid: str, title: str) -> None
    def touch_session(self, sid: str) -> None
    def delete_session(self, sid: str) -> bool               # DB rows only (files are the service's job)
    def put_document(self, sid: str, doc: DocumentInfo) -> None          # insert-or-replace (incl. pi_doc_id)
    def get_document(self, sid: str) -> DocumentInfo | None              # includes pi_doc_id (excluded only on serialisation)
    def update_document(self, sid: str, **fields) -> DocumentInfo | None # partial update (status, stage, progress, ...)
    def add_message(self, msg: Message, contexts: list[ContextPage] | None = None) -> None
    def update_message(self, msg: Message) -> None
    def set_contexts(self, mid: str, contexts: list[ContextPage]) -> None
    def get_contexts(self, mid: str) -> list[ContextPage]
    def get_message(self, sid: str, mid: str) -> Message | None
    def list_messages(self, sid: str) -> list[Message]       # chronological
    def recover_interrupted(self) -> int                     # documents stuck in 'indexing' -> failed("interrupted"); messages 'streaming' -> error
```
Session `state`: no document -> `empty`; document failed -> `failed`; indexing -> `indexing`; ready and 0 user messages -> `ready`; ready and >=1 -> `locked`.

### 4.6 `pageindex_compat.py`, `indexer.py`  [pageindex-layer]
```python
# pageindex_compat.py  - the only module that touches SDK internals
def apply_patches() -> None                       # idempotent; pdfium page text override (D4); logs the pageindex version, warns if != 0.2.21
def make_client(settings: Settings, session_id: str, *, for_indexing: bool = False) -> "PageIndexClient"
        # storage_path = settings.session_dir(sid)/"pageindex"; index={"model":..,"storage_path":..}, chat=settings.chat_model;
        # honours settings.openai_api_key/base_url (sets env for the SDK/litellm/agents SDK as needed, without leaking across sessions)
def check_environment(settings: Settings) -> dict   # {"pageindex_version","openai_key":bool,"litellm_version",...} for /api/health

# indexer.py
class IndexService:
    def __init__(self, settings: Settings, store: Store, client_factory=make_client)
    def start(self, session_id: str, pdf_path: Path) -> None   # non-blocking (daemon thread); updates store.update_document(stage, progress, status, page_count, title, node_count, pi_doc_id, indexed_at, index_seconds, error)
    def is_running(self, session_id: str) -> bool
    def cancel(self, session_id: str) -> None                  # best effort (delete session while indexing)
def load_tree(settings: Settings, session_id: str) -> list[dict]       # nodes WITHOUT text, from tree.json
def load_page_texts(settings, session_id) -> list[str] | None
```
Stages: `validating` -> `extracting_text` -> `building_tree` -> `summarizing` -> `finalizing` -> `ready` (or `failed` + `error` human-readable:
rate-limit / auth / scanned / no outline). `progress` is best effort (count wrapped LLM calls vs. an estimate of nodes ≈ 0.8 x pages).
Errors from the SDK are translated to short messages; never leave a document in `indexing` after an exception.

### 4.7 `qa.py`, `pricing.py`  [qa-engine]
```python
class QAError(Exception): code, message      # openai_auth | openai_rate_limit | openai_model | agent_max_turns | agent_failed | cancelled

class QAEngine:
    def __init__(self, settings: Settings, client_factory=make_client)
    def ask(self, *, session_id: str, doc: DocumentInfo, question: str,
            history: list[dict],              # [{"role":"user"|"assistant","content":str}] plain text, markers stripped, last N turns
            ctx: CitationContext,
            cancel: threading.Event | None = None) -> Iterator[dict]      # SYNC generator (service runs it in a thread)
```
Yielded events (plain dicts):
```
{"type":"step","step":Step}                                   # a tool call started (label like "Read pages 88-90")
{"type":"step_done","step_id","elapsed_ms","label","pages"}   # finished (label may be refined)
{"type":"token","text":str}                                   # display delta, [[cN]] markers already inserted, never a half marker
{"type":"citation","citation":Citation}                       # preliminary (page, doc_name, quote when model-supplied; no rects yet)
{"type":"final","answer":BuiltAnswer,"contexts":list[ContextPage],"usage":Usage,"steps":list[Step],
 "status":"answered"|"no_sources","elapsed_ms":int}
```
Rules: `contexts` = pages from `get_page_content` tool results in read order, deduped by page. Both protocols (`responses`
streaming raw events; `chat` lane `.events`) normalise to this stream. Instructions appended to the SDK's citation prompt tell the model to
add a short verbatim `quote="..."` to each `<cite/>`, cite PHYSICAL page numbers, and to say plainly when the report does not contain the answer.
Cancellation: closing the generator / setting `cancel` stops the agent run. Map SDK/OpenAI errors to `QAError`.
`pricing.py`: `PRICES` table (USD per 1M tokens: input, cached, output) for gpt-5.6-*, gpt-6*, gpt-4.1*, gpt-4o*; `estimate_cost(model, Usage) -> float|None`.

### 4.8 `service.py`  [service-web]
```python
class ReportLensService:
    def __init__(self, settings: Settings, *, store: Store | None = None, indexer: IndexService | None = None,
                 qa: QAEngine | None = None, evaluator: Evaluator | None = None)     # all injectable for tests
    # sessions
    def create_session(self, from_session: str | None = None) -> Session   # from_session: copy that session's document+index -> state ready
    def list_sessions(self) -> list[Session]
    def get_session(self, sid: str) -> SessionDetail                       # ServiceError(session_not_found,404)
    def rename_session(self, sid: str, title: str) -> Session
    def delete_session(self, sid: str) -> None                             # cancels indexing, removes DB rows + files
    # document
    def attach_document(self, sid: str, filename: str, tmp_path: Path) -> Session
        # validate (inspect_pdf; limits), move to data/sessions/<sid>/<doc_name>.pdf, create DocumentInfo(status=indexing), start indexer.
        # allowed only in state empty|failed (else 409 document_already_uploaded / document_locked)
    def document_path(self, sid: str) -> Path                              # 404 document_not_found
    def document_pages(self, sid: str) -> DocumentPages
    def document_outline(self, sid: str) -> list[dict]
    def locate(self, sid: str, page: int, quote: str | None, claim: str | None) -> LocateResponse
    # chat
    async def ask(self, sid: str, content: str) -> AsyncIterator[tuple[str, dict]]    # yields (sse_event_name, payload) - section 6
    async def ask_batch(self, sid: str, questions: list[str]) -> AsyncIterator[tuple[str, dict]]   # a set of independent questions in parallel - section 13
    def get_message(self, sid: str, mid: str) -> Message
    async def evaluate_message(self, sid: str, mid: str) -> EvalScores     # (re-)run RAGAS; persists
    def health(self) -> dict
    async def aclose(self) -> None
```
`ask` rules: 400 `empty_question` (strip, max 4000 chars); 409 `document_not_ready`; 409 `session_busy` (one in-flight question per
session); 503 `openai_not_configured`. It persists the user message and the assistant message (incrementally: status `streaming` -> final),
runs `QAEngine.ask` in a thread (bridged with `loop.call_soon_threadsafe` + `asyncio.Queue`), builds the final Message, then starts
evaluation as an independent `asyncio.Task` (survives client disconnect; result persisted on the message) and streams `eval_*` events
while the client is still connected. Client disconnect during the agent run cancels the run and marks the message `error: "cancelled"`.
First question auto-titles the session (first ~60 chars).

### 4.9 `web/` [service-web]
`create_app(settings: Settings | None = None, service: ReportLensService | None = None) -> FastAPI`. Static files at `/` (index.html), `/static/*`.
Errors: `ServiceError` -> `JSONResponse({"error":{"code","message"}}, status)`; unexpected -> 500 with a generic message + logged traceback.
Security: session ids are 32-hex and validated; uploads stream to a temp file with a size cap (413), PDF magic bytes checked, original
filename sanitised, no path from the client ever reaches the filesystem. Default bind 127.0.0.1. `GET /api/sessions/{sid}/document/file`
uses `FileResponse` (HTTP Range works) with `Content-Type: application/pdf`, `Content-Disposition: inline`.
Demo mode: `settings.demo_mock` -> start `devtools.mock_openai` in-process on a free port at startup, set `openai_base_url` + a dummy key.

## 5. REST API (all JSON unless noted)

| Method + path | Request | Success | Errors |
|---|---|---|---|
| `GET /api/health` | | `{ok, version, pageindex_version, openai_configured, demo_mock}` | |
| `GET /api/config` | | `Settings.public()` + `{"metrics": METRIC_INFO}` | |
| `GET /api/sessions` | | `Session[]` (newest first) | |
| `POST /api/sessions` | `{"from_session": "<sid>"?}` | 201 `Session` | 404 session_not_found, 409 document_not_ready |
| `GET /api/sessions/{sid}` | | `SessionDetail` (messages incl. citations/evaluation) | 404 |
| `PATCH /api/sessions/{sid}` | `{"title": str}` | `Session` | 404, 400 |
| `DELETE /api/sessions/{sid}` | | 204 | 404 |
| `POST /api/sessions/{sid}/document` | multipart `file` (.pdf) | 202 `Session` (state `indexing`) | 400 invalid_pdf, 409 document_already_uploaded / document_locked, 413 file_too_large, 422 scanned_pdf / encrypted_pdf / too_many_pages |
| `GET /api/sessions/{sid}/document/file` | | `application/pdf` (Range) | 404 document_not_found |
| `GET /api/sessions/{sid}/document/pages` | | `DocumentPages` | 404, 409 |
| `GET /api/sessions/{sid}/document/outline` | | `{"nodes":[{title,node_id,start_index,end_index,nodes}]}` | 404, 409 |
| `GET /api/sessions/{sid}/locate?page=&quote=&claim=` | | `LocateResponse` | 404, 400 |
| `POST /api/sessions/{sid}/messages` | `{"content": str}` | `text/event-stream` (section 6) | 400/404/409/503 as JSON **before** the stream starts |
| `POST /api/sessions/{sid}/batch` | `{"questions": [str, ...]}` | `text/event-stream`: section 6 with an `index` on every event, framed by `batch_start` / `batch_done` (section 13) | 400/402/403/404/409/429/503 as JSON **before** the stream starts |
| `GET /api/sessions/{sid}/messages/{mid}` | | `Message` | 404 |
| `POST /api/sessions/{sid}/messages/{mid}/evaluate` | | `EvalScores` (blocks until RAGAS finishes) | 404, 409 |

The UI polls `GET /api/sessions/{sid}` (every 1 s) while `document.status == "indexing"`.

## 6. SSE protocol - `POST /api/sessions/{sid}/messages`

Frames: `event: <name>\ndata: <json>\n\n`; `: ping` comment every 15 s. The stream stays open until evaluation finishes (or fails/skips).
Every payload that concerns the assistant message has `message_id`.

| event | payload |
|---|---|
| `message_start` | `{"user_message": Message, "message_id": "<assistant id>", "created_at": str}` |
| `step` | `{"message_id", "step": Step}` (status `running`) |
| `step_done` | `{"message_id", "step_id", "elapsed_ms", "label", "pages": [int]}` |
| `token` | `{"message_id", "text": "<markdown delta, may contain whole [[c3]] markers>"}` |
| `citation` | `{"message_id", "citation": Citation}` (preliminary: no rects) |
| `answer_done` | `{"message": Message}` (final content, final citations WITH rects/section_path/printed_page, sources, steps, usage, status, evaluation = `pending` or `skipped`) |
| `eval_started` | `{"message_id", "metrics": ["faithfulness","answer_relevancy","context_precision"], "n_contexts": int}` |
| `eval_result` | `{"message_id", "metric": str, "value": float \| null, "error": str \| null}` (one per metric as each finishes) |
| `eval_done` | `{"message_id", "evaluation": EvalScores}` |
| `error` | `{"code": str, "message": str, "message_id": str \| null}` (the assistant message is persisted with status `error`) |
| `done` | `{}` |

Order: `message_start`, (`step`, `step_done`)*, `token`*/`citation`* interleaved, `answer_done`, `eval_started`, `eval_result`*, `eval_done`, `done`.
Markers: the model's `<cite .../>` tags never reach the browser; they are replaced by `[[cN]]` and the browser renders chip N from
`message.citations[N-1]` (a chip whose citation has not arrived yet renders as a small placeholder).

## 7. Front-end contract (UI owners)

`js/viewer.js` exports (ui-viewer implements, ui-shell consumes):
```js
export class SourcePanel {
  constructor(rootEl, { onClose })              // rootEl = <aside id="source-panel">; panel hidden until open()
  async open({ sessionId, fileUrl, filename, pageCount, pages })   // pages = DocumentPages.pages; idempotent for the same sessionId
  async showCitation({ page, rects, quote, printedPage })           // scroll to page, flash highlight (150ms in / ~2.4s hold / 600ms out), update footer
  close(); destroy();  get isOpen()
}
```
Highlight rect source priority: `citation.rects` (server) -> `GET /locate?page&quote` -> whole-page flash with an honest
"Passage not located - showing page" notice. Chips use the PHYSICAL page number (matches the panel's "Page N / total"); the hover card also shows the
printed folio and the section breadcrumb ("area").
Design tokens, layout, behaviours: `research/06-ui-design-spec-from-screenshots.md` (sections 1-9). Use the product name **ReportLens** and an
original mark; never the PageIndex logo/name/copy. Fix the reference's contrast failures (see the spec's accessibility section).

## 8. Dev tooling contract  [mock-and-samples]

```python
# devtools/mock_openai.py
def start_mock_server(host="127.0.0.1", port=0, *, delay_ms: int = 0) -> MockServer
class MockServer: base_url:str        # "http://127.0.0.1:PORT/v1"  (use as OPENAI_BASE_URL)
                  requests: list[dict]   # every request body received (path, json)
                  def stop(self) -> None
# CLI: python -m devtools.mock_openai --port 8765
```
Implements (all with streaming where OpenAI streams): `POST /v1/chat/completions`, `POST /v1/responses`, `POST /v1/embeddings`, `GET /v1/models/{id}`.
Behaviour by request type (detect by prompt/tool content):
* PageIndex index prompts (leaf summary / parent summary / expand / doc description) -> valid JSON/text from the prompt's own text (extractive).
* PageIndex agent (tools present): turn 1 `get_document_structure`; turn 2 `get_page_content` for the best-matching node page range chosen from the tool output by
  keyword overlap with the question; turn 3 a **cited answer composed of real sentences copied from the page text it was given**, each followed by
  `<cite doc="<doc_name>" page="N" quote="<verbatim fragment>"/>`. Works on both `/chat/completions` and `/responses`, streaming and not.
* RAGAS judge prompts (statement generation, NLI verdicts, question generation, context-precision verdict) -> schema-valid JSON (see `research/ragas_offline_stub_server.py`).
* `/embeddings` -> deterministic hashed bag-of-words vectors (dim 64, L2-normalised), so similar texts have high cosine.
`scripts/make_sample_pdf.py`: `build_sample_pdf(path, pages=60, printed_offset=2, seed=0) -> Path` - a synthetic annual report with a cover, contents,
PDF **bookmarks/outline** (so PageIndex Flash finds a tree), 2-column narrative pages, financial tables, notes with cross-references, curly quotes,
hyphenation, folios offset from physical pages, and a known-facts JSON sidecar (`<name>.facts.json`: question, answer, page, quote) for tests.
Fixtures: `sample_pdf` (session-scoped), `mock_openai` (function-scoped, started/stopped).

## 9. Testing conventions
* `pytest` (config in pyproject; `-m "not live"` by default). Tests are offline and deterministic; anything that needs a real key is `@pytest.mark.live`.
* Run: `.venv\Scripts\python.exe -m pytest -q`. Windows: keep the venv at `<project>\.venv` (short path), set `PYTHONIOENCODING=utf-8`.
* Set `RAGAS_DO_NOT_TRACK=true` (exact string) before importing ragas.
* No test may touch the real network or `~/.pageindex`; use `tmp_path` data dirs and the mock server.

## 10. Known risks (carry into UAT)
1. No live OpenAI key was available during the build: real gpt-5.6 agent behaviour, `quote=` compliance, RAGAS numbers on real answers, per-question cost/latency are **unverified**.
2. National Grid PDF structure unknown (bookmarks? scanned? AES?). Flash refuses outline-less PDFs > 10 pages and scanned PDFs -> the UI explains and offers no workaround yet.
3. SDK churn (weekly releases, 0.3.0 dev line) - pinned to 0.2.21; supply-chain: litellm 1.82.7/1.82.8 were malicious (we resolve >= 1.104).
4. Highlight accuracy on tables / multi-column pages is heuristic; the UI always degrades to page-level highlight honestly.
5. RAGAS: 5 + N judge calls per answer; scores are judge-dependent estimates (UI tooltips say so).

## 11. Small hosts (Render free: 512 MB RAM, 0.1 CPU) - `LOW_MEMORY`
`Settings.low_memory` (env `LOW_MEMORY`; automatic with `PUBLIC_MODE` when the cgroup memory limit is <= 600 MB) switches on, together:
* **Indexing in a child process** (`index_in_subprocess`, `reportlens/indexing_worker.py`, JSON lines over a pipe; protocol in its docstring). The web process never imports the SDK's indexing half; the child's memory goes back to the OS when it ends. Cancelling or a job timeout kills the child; a child killed by the kernel becomes the "ran out of memory" failure. `indexer.RemoteFault` carries the child's classified error through `translate_error`.
* **Lean layout parser** (`pageindex_compat._lean_parse`): `workers=1` runs the SDK's per-page functions in-process, keeping spans only. The SDK's sequential path holds every character of every page as dicts (1.3 GB peak on a 308-page report) and its process pool re-imports the SDK in `cpu_count()-1` processes (instant OOM); output is identical to both (tested). Falls back to the SDK's sequential path for Type-3 fonts.
* **No litellm in the child** (`reportlens/lite_llm.py`, `lite_llm` setting): the three SDK functions that import litellm (~150 MB) are rebound to plain `httpx` requests (not even the `openai` package, ~50 MB of type modules the child has no other use for), same request on the wire (tested request for request), same retry policy and error classes; another provider prefix or a model that /chat/completions refuses falls back to litellm on demand.
* **Scoring in a child process** (`eval_in_subprocess`, `reportlens/eval_child.py` = `ChildEvaluator`, `reportlens/evaluation_worker.py`): the web process never imports RAGAS (it would otherwise keep ~100 MB for ever and the next indexing would not fit). The child runs the unchanged `Evaluator` (with `datasets` stubbed: no pandas/pyarrow, ~90 MB less) and streams each metric back. **One heavy child at a time**: `lowmem.HEAVY_JOB_LOCK` is taken by an indexing child and by a scoring child, so scoring waits while a report is indexed (the answer itself is never delayed) and the other way round. Both children set `oom_score_adj` so the kernel kills a child, never the web server.
* **The web process makes room before an indexing child starts** (`IndexService.set_heavy_job_hook`, wired by `ReportLensService` to `_shed_idle_documents`): every open document nobody is using right now (PDF bytes, outline, folios, squashed text) is closed and the heap trimmed; the next question reopens what it needs. Measured: about 40 MiB off the container's peak while a 308-page report is indexed.
* `PageIndexClient`'s background litellm preload is disabled, one open PDF (`max_open_docs=1`), lower concurrency (index 6, scoring 3, 6 contexts), `malloc_trim` after parsing and after each scoring.
Measurements and the reasoning are in docs/DEPLOY.md; `scripts/render_limits_test.py` reproduces them in Docker with Render's limits.

## 12. Public pieces: demo chat, private chats, visitors' own providers, the REST API
* **Read-only demo chat** (`reportlens/demo.py`, `scripts/export_demo.py`, `demo/`). A real chat exported once (questions, cited
  answers, scores, the page texts behind them, the PDF and its PageIndex store) is installed at every start-up under the fixed id
  `DEMO_SESSION_ID`, owner `store.DEMO_OWNER`. `AuthMiddleware` lets GET/HEAD of `/api/demo`, `/api/config`, `/api/openapi.json`
  and the demo chat's own routes through without the access code; `routes.check_access` refuses every write to it (403
  `demo_read_only`) and `ReportLensService._require_writable` refuses again. The budget (`Store.usage_snapshot`) and the chat cap
  (`Store.count_sessions`) ignore it; "ask your own question about this report" clones it (`key_source="none"`: not charged).
  The PDF is a third-party document: `demo/*` is git-ignored and travels only in the private deploy bundle.
* **Private chats** (`PRIVATE_CHATS`, on whenever `ACCESS_CODE` is set). Schema v2 adds `sessions.owner`. Every browser gets a
  signed, year-long `rl_visitor` cookie (key from `SESSION_SECRET`, else the access code: changing the code orphans nobody);
  `routes.check_access` is the single gate for every `/api/sessions/{sid}...` route (someone else's chat = 404), list/create are
  scoped to the visitor. API clients are visitors too (their cookie jar).
* **Model providers** (`reportlens/providers.py`). Every provider is reached through its OpenAI-compatible endpoint, so the web
  process never loads litellm (~150 MB, it would break the 512 MB fit). OpenAI keeps the Responses lane; others use the chat
  lane with model ids sent to the SDK as `openai/<id>`, which `pageindex_compat._patch_compat_chat_model` turns into the Agents
  SDK's `OpenAIChatCompletionsModel` on an explicit client (OpenAI-only body fields such as `prompt_cache_key` are dropped for
  other hosts), and which `lite_llm.plain_model` sends unchanged (slashes included) from the indexing child. RAGAS gets the bare
  id; without an embeddings model answer relevancy is reported as unavailable (`evaluation.NO_EMBEDDINGS`) and the run still
  counts as done. Owner level: `LLM_PROVIDER` + `LLM_API_KEY` + model ids (`config.load_settings` applies the same mapping).
* **Visitors' own keys** (`X-LLM-Config`, base64url JSON; `routes.request_llm` -> `providers.settings_for_visitor`). The result
  is a per-request `Settings` copy with `key_source="visitor"` handed to `ask` / `attach_document` / `evaluate_message`
  (`IndexService.start(settings=...)` -> `_Job.settings`; `QAEngine.with_settings`; `ReportLensService._new_evaluator`, closed
  after the run). Rules that keep the key contained: never written to `os.environ` (`make_client` skips
  `configure_openai_env`), child processes of a visitor's job do not inherit the owner's key, keyless endpoints get a placeholder
  key so nothing falls back to the owner's, key-shaped strings are redacted from logs, the budget skips `key_source != "server"`
  rows. `VISITOR_KEYS=off|optional|required`; custom base URLs only with `ALLOW_CUSTOM_LLM_URL` (default off in `PUBLIC_MODE`:
  SSRF). Front end: `llmstore.js` (sessionStorage, or localStorage when "remember"), `llm.js` (the dialog), every request in
  `api.js` / `sse.js` adds the header.
* **REST API.** `/docs` serves a vendored Swagger UI (no CDN; `static/docs.html` + `js/docs.js`, no inline script for the CSP),
  routes carry tags and summaries, `POST /api/sessions/{sid}/ask` returns one JSON answer (it consumes the same `service.ask`
  generator as the SSE route). Walkthrough: `docs/API.md`.

## 13. The question set ("Run all"): `POST /api/sessions/{sid}/batch`
After an upload the UI offers a preset, editable list of questions (`Settings.default_questions`, env `DEFAULT_QUESTIONS`; five built in) and
one click answers all of them in parallel.  Everything else about asking is unchanged: the same service, engine, citations and scoring.

* **Request / refusals.** `{"questions": [str, ...]}`.  `service.clean_questions` strips, drops blanks and exact duplicates (case-insensitive) and
  enforces `1..MAX_BATCH_QUESTIONS` questions of at most 4000 characters (400 `empty_question` / `too_many_questions` / `question_too_long`).
  Everything `ask` refuses is refused the same way and, as there, as JSON **before** the stream starts (the route peeks the first event):
  403 demo/read-only, 404, 409 `document_not_ready` / `session_busy` (one question *or set* in flight per chat), 402 budget, 503 no key, 429.
  The per-IP limiter takes **one token per question, all or nothing** (`SlidingWindowLimiter.hit_many`): too few left = the whole set is refused
  with 429 `rate_limited`; a refusal before the stream refunds them.
* **Events** (`ReportLensService.ask_batch`, an async generator like `ask`).  Names and payloads are the single-question ones (section 6) with an
  extra integer `index` (0-based position in the set) on every per-question event, and `message_start` is replaced by `batch_start`
  `{"items": [{"index","question","user_message","message_id"}], "concurrency"}`, sent first.  All user and assistant rows are created up front,
  in question order (stable `seq`, the chat shows every question at once).  `batch_done {"answered","failed"}` follows the last `answer_done` /
  `error`; `eval_*` of finished answers may still arrive after it; `done` closes the stream once every answer is scored or skipped.
* **Independence.** Each question gets no chat history and no rewrite (RAGAS `user_input` is the question).  A failure in one question is one
  `error` event for its `index` / `message_id` and a row stored as failed; the others continue.  Later single questions in the chat do see the
  batch's answers as history, like any earlier turn.
* **How it is run.**  `ask()` and `ask_batch()` share `_answer` (one question: lease the document, engine thread, events, stored answer, scoring)
  and differ only in what wraps it: `ask` yields it directly, `ask_batch` starts one asyncio task per question, all writing tagged events to one
  queue that the generator drains.  A process-wide `_Gates` object holds the limits that keep peak memory flat:
  `asyncio.Semaphore(BATCH_CONCURRENCY)` around each question's agent run (threads via the same `_spawn_engine`), a scoring semaphore (1 with
  `LOW_MEMORY`, 2 otherwise: each RAGAS run, in a child process on small hosts, is the big memory consumer; scoring of answer *k* starts as soon as
  it is stored while others are still answering), and, on a small host, a one-at-a-time lock taken by questions that start while an indexing child
  runs (`lowmem.INDEXING_ACTIVE`).  The agent slot is given back only after the answer is stored and its scoring queued, so the next question's
  **budget check** (made just before it starts, with `QUESTION_RESERVE_USD` held back for every question in flight, `_Run.active`) sees what the
  previous one cost: when the money runs out the remaining questions end as `error` / `budget_exhausted` and the batch closes cleanly.
* **Sharing.** One `_Resources` entry (open PDF, tree, folios) per session, leased by every question; its lazy values are built once under locks
  (`_Resources._memo`, `PdfiumDoc._derive_lock`, PDFium itself behind `PDFIUM_LOCK`).
* **Disconnect.**  Closing the stream sets the shared cancel event (children of the batch's master `_Run` share it), cancels the workers and marks
  every unfinished row `error: cancelled`; answers already stored keep being scored (independent tasks, as for a single question).
* **Settings** (`GET /api/config` also exposes `default_questions`, `max_batch_questions`, `batch_concurrency`): `DEFAULT_QUESTIONS`,
  `MAX_BATCH_QUESTIONS` (10), `BATCH_CONCURRENCY` (3; 2 with `LOW_MEMORY`; 1-6).  Measurements and the reasoning for the default: docs/DEPLOY.md.
* **Static demo without a PDF** (`demo.DemoInfo.has_document`, `GET /api/demo`): the packaged chat in `reportlens/demo_data/` is installed when
  `DEMO_DIR` is unset and `demo/` holds no `chat.json`; its document routes answer 404 `document_not_found` and cloning it 409
  `document_not_available`; citations open a card instead of the viewer.
* **A scoring child kept for the next answer of a set.** `ChildEvaluator.evaluate(..., keep_warm=<bool | callable>)`: when the caller says more answers are coming
  (a batch: `_Gates.unfinished > 1`) the child is parked instead of stopped, still holding the heavy-job gate, for `KEEP_WARM_S` (45 s); the next
  scoring reuses it (`evaluation_worker` loops over requests on its stdin, trimming the heap between them), so RAGAS is imported once per set instead of
  once per answer (about 45 s of CPU at 0.1 CPU).  A failure, a dead child, the idle timer or `aclose()` ends the warm period and frees the gate.

