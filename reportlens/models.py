"""Shared data contracts.  THESE ARE THE SOURCE OF TRUTH for every JSON shape that crosses a module, REST or SSE boundary.
Change a field here => update docs/ARCHITECTURE.md and the front end in the same commit.

Conventions
  * page numbers are PHYSICAL, 1-based PDF pages (what PDF.js shows as "Page N / total"); the folio printed on the page
    ("87") is carried separately as `printed_page`.
  * Rect coordinates are FRACTIONS (0..1) of the visible page (after /Rotate and CropBox), origin TOP-LEFT, so the browser can
    apply them as CSS percentages on top of any renderer.
  * timestamps are ISO-8601 UTC strings ("2026-10-07T17:45:03Z").
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ServiceError(Exception):
    """Raised by the service layer; the web layer maps it to HTTP {"error": {"code", "message"}} with `status`."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# --------------------------------------------------------------------------------------------- citations
class Rect(BaseModel):
    x: float
    y: float
    w: float
    h: float


MatchMethod = Literal["exact", "fuzzy", "fragments", "block", "page", "none"]
QuoteSource = Literal["model", "aligned", "none"]


class Citation(BaseModel):
    id: str                                   # "c1", "c2", ... one per <cite> tag occurrence, in answer order
    index: int                                # 1-based, == numeric part of id
    doc_name: str                             # display file name, e.g. "National Grid_Annual_Report.pdf"
    page: int                                # physical page the passage was LOCATED on (may differ from cited_page by +-1)
    cited_page: int                           # physical page the model wrote in its <cite page="..">
    printed_page: Optional[str] = None        # folio printed on `page`, e.g. "87", "xii"; None when unknown
    section_path: list[str] = Field(default_factory=list)  # PageIndex tree breadcrumb = the "area", e.g. ["Strategic Report","Financial review"]
    node_id: Optional[str] = None             # deepest tree node containing `page`
    node_range: Optional[list[int]] = None    # [start_index, end_index] of that node (physical pages)
    quote: Optional[str] = None               # verified passage text from the page ("what was highlighted"); None if not located
    quote_source: QuoteSource = "none"        # "model" = model-supplied quote=".." verified on the page; "aligned" = found by claim alignment
    match_method: MatchMethod = "none"
    match_score: float = 0.0                  # 0..1
    rects: list[Rect] = Field(default_factory=list)   # [] => highlight the whole page (passage not located)
    page_width: Optional[float] = None        # visible page size in PDF points
    page_height: Optional[float] = None
    claim: Optional[str] = None               # answer sentence/bullet this citation supports (hover card)


class Source(BaseModel):
    """One row of the 'Sources' list: unique pages, aggregated over citations."""
    page: int
    printed_page: Optional[str] = None
    refs: int                                 # number of citations pointing at this page
    citation_ids: list[str] = Field(default_factory=list)
    section_path: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------- agent trace
class Step(BaseModel):
    id: str                                   # "s1", "s2", ...
    kind: Literal["thinking", "tool"]
    tool: Optional[str] = None                # get_document_structure | get_page_content | get_document | browse_documents
    label: str                                # "Read pages 88-90", "Looked at the document outline", "Thinking"
    pages: list[int] = Field(default_factory=list)
    status: Literal["running", "done"] = "running"
    elapsed_ms: Optional[int] = None


class Usage(BaseModel):
    model: Optional[str] = None
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: Optional[float] = None          # estimate from reportlens.pricing; None when the model is not in the table


# --------------------------------------------------------------------------------------------- evaluation
class EvalScores(BaseModel):
    status: Literal["pending", "running", "done", "partial", "failed", "skipped"] = "pending"
    faithfulness: Optional[float] = None      # 0..1, None = not computed / failed
    answer_relevancy: Optional[float] = None  # a.k.a. response relevancy
    context_precision: Optional[float] = None
    errors: dict[str, str] = Field(default_factory=dict)             # metric -> short error text
    context_verdicts: list[dict[str, Any]] = Field(default_factory=list)  # [{"index":0,"page":89,"verdict":1,"reason":".."}]
    n_contexts_input: int = 0                 # pages the agent actually read
    n_contexts_scored: int = 0                # pages passed to RAGAS (capped)
    judge_model: Optional[str] = None
    embedding_model: Optional[str] = None
    ragas_version: Optional[str] = None
    latency_s: Optional[float] = None
    skipped_reason: Optional[str] = None      # "disabled" | "no_contexts" | "no_api_key" | "empty_answer"


# --------------------------------------------------------------------------------------------- messages / sessions
MessageStatus = Literal["streaming", "answered", "no_sources", "error"]


class Message(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "assistant"]
    content: str = ""                         # user: plain text. assistant: markdown containing [[c1]] [[c2]] .. citation markers
    status: MessageStatus = "answered"
    citations: list[Citation] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    usage: Optional[Usage] = None
    elapsed_ms: Optional[int] = None
    evaluation: Optional[EvalScores] = None
    error: Optional[str] = None
    created_at: str = ""


class DocumentInfo(BaseModel):
    id: str
    filename: str                             # original upload name (display)
    doc_name: str                             # ASCII storage name; what the agent cites as doc="..."
    size_bytes: int
    page_count: Optional[int] = None
    status: Literal["indexing", "ready", "failed"] = "indexing"
    stage: str = "queued"                     # queued|validating|extracting_text|building_tree|summarizing|finalizing|ready|failed
    progress: float = 0.0                     # 0..1, best effort
    error: Optional[str] = None
    title: Optional[str] = None               # from the PageIndex tree
    description: Optional[str] = None
    node_count: Optional[int] = None
    created_at: str = ""
    indexed_at: Optional[str] = None
    index_seconds: Optional[float] = None
    # internal: PageIndex doc id ("pi-<32hex>"). Stored in the DB, never serialised to the browser.
    pi_doc_id: Optional[str] = Field(default=None, exclude=True)


SessionState = Literal["empty", "indexing", "ready", "locked", "failed"]


class Session(BaseModel):
    """state: empty = no document yet (upload allowed) | indexing | ready (can chat; document still replaceable? NO - see below)
    | locked = first question asked | failed = indexing failed (re-upload allowed)

    Rule: exactly one document per session. Upload is accepted only in state `empty` or `failed`.
    """
    id: str
    title: str
    state: SessionState
    created_at: str
    updated_at: str
    document: Optional[DocumentInfo] = None
    message_count: int = 0


class SessionDetail(Session):
    messages: list[Message] = Field(default_factory=list)


class PageInfo(BaseModel):
    width: float                              # visible page width in PDF points
    height: float
    printed_page: Optional[str] = None


class DocumentPages(BaseModel):
    page_count: int
    pages: list[PageInfo]


class LocateResponse(BaseModel):
    """GET /api/sessions/{sid}/locate"""
    page: int
    hinted_page: int
    method: MatchMethod
    score: float
    rects: list[Rect]
    matched_text: str = ""
    page_width: Optional[float] = None
    page_height: Optional[float] = None


# --------------------------------------------------------------------------------------------- internal (not sent to the browser)
class ContextPage(BaseModel):
    """A page of text the agent actually read (tool result of get_page_content) - the RAGAS `retrieved_contexts`."""
    page: int
    text: str
