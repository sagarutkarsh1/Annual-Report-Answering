"""ReportLensService: the orchestration layer between the web routes and the engines (docs/ARCHITECTURE.md 4.8, 5, 6).

Threading model
  * The sync methods (sessions, documents, locate) block on SQLite / PDFium / the filesystem and are thread-safe; the web
    layer calls them through a thread pool.  Only `ask`, `evaluate_message` and `aclose` are coroutines.
  * `ask` runs the sync QA engine on its own daemon thread (an agent run lasts minutes, so it must not occupy a slot of the
    shared executor that every other request needs) and bridges events to the loop with `call_soon_threadsafe`.
  * RAGAS runs as an independent asyncio.Task per answer.  The open SSE stream only *listens* to it, so a client that
    disconnects loses the live scores but never the persisted ones.

Resource ownership
  * Per-session open resources (PDF bytes in PDFium, tree, printed folios, page sizes) are built lazily once the document
    is ready and kept in a small LRU.  An evicted entry is closed only when nobody uses it any more (reference counting),
    so a long answer never has its PDF closed underneath it.
  * Files of a session live in `settings.session_dir(sid)`; they are removed with retries because Windows (antivirus,
    the indexer thread) holds handles for a moment.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import re
import shutil
import stat
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Iterable, Iterator, Optional, TypeVar

from pydantic import BaseModel

from . import __version__
from .citations import CitationContext, strip_markers
from .config import Settings
from .limits import QUESTION_RESERVE_USD, UsageBudget
from .locator import LocateResult, PdfiumDoc, align_claim, locate_quote
from .models import (
    Citation,
    ContextPage,
    DocumentInfo,
    DocumentPages,
    EvalScores,
    LocateResponse,
    Message,
    PageInfo,
    Rect,
    ServiceError,
    Session,
    SessionDetail,
    Source,
    Step,
    Usage,
)
from .pdfutil import PdfError, detect_printed_labels, inspect_pdf
from .store import DEFAULT_TITLE, SessionNotFound, Store, new_id, now_iso

if TYPE_CHECKING:  # the real classes are imported lazily so this module loads even when the engines are being rewritten
    from .evaluation import Evaluator
    from .indexer import IndexService
    from .qa import QAEngine

log = logging.getLogger("reportlens.service")

_T = TypeVar("_T")
_M = TypeVar("_M", bound=BaseModel)

MAX_QUESTION_CHARS = 4000
TITLE_MAX_CHARS = 60
MAX_OPEN_DOCS = 4                  # sessions whose PDF / tree / folios stay open (LRU)
MAX_DOC_NAME_CHARS = 80            # including ".pdf"
MAX_DISPLAY_NAME_CHARS = 255
SQUASH_WARM_MIN_PAGES = 150        # from this many pages up the whole-document quote search needs the squashed text warmed (locator)
THREAD_JOIN_TIMEOUT_S = 3.0        # how long a cancelled run may take to stop before we stop waiting (the web layer allows 5 s)
_RMTREE_ATTEMPTS = 9
_METRICS = ("faithfulness", "answer_relevancy", "context_precision")   # == evaluation.METRICS (asserted in the tests)
_VERIFIED = ("exact", "fuzzy", "fragments")                            # locator methods that found the text itself
_SESSION_ID_RE = re.compile(r"[0-9a-f]{32}")
_WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})
_PDF_ERROR_STATUS = {"invalid_pdf": 400, "encrypted_pdf": 422, "scanned_pdf": 422}
# Errors the UI words itself when a message is reloaded from the database (it reads Message.error as a code); any other
# failure is stored as its readable message.
_CODE_ERRORS = frozenset({"cancelled", "openai_auth", "openai_rate_limit", "openai_model", "agent_max_turns"})
_ANSWER_OK = ("answered", "no_sources")
_END = object()                    # sentinel posted by the engine thread when it is done


# ----------------------------------------------------------------------------------------------- small helpers
def _trim_memory() -> None:
    from .lowmem import memory_summary, trim_memory

    trim_memory()
    log.info("memory after scoring: %s", memory_summary())      # one line per scored answer: the host's limit is the thing to watch


def _dump(obj: Any) -> Any:
    """JSON-ready form of a pydantic model; anything else is passed through (engines may yield plain dicts)."""
    return obj.model_dump(mode="json") if isinstance(obj, BaseModel) else obj


def _coerce(cls: type[_M], value: Any) -> _M:
    return value if isinstance(value, cls) else cls.model_validate(value)


def make_title(question: str) -> str:
    """One-line sidebar title from the first question: its first ~60 characters, cut at a word boundary."""
    one_line = " ".join(question.split())
    if len(one_line) <= TITLE_MAX_CHARS:
        return one_line
    cut = one_line[:TITLE_MAX_CHARS]
    space = cut.rfind(" ")
    if space >= TITLE_MAX_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip(" .,;:-") + "…"


def slugify_doc_name(filename: str, taken: Iterable[str] = ()) -> str:
    """ASCII storage name ending in .pdf: no path characters, at most 80 chars, never a Windows device name, and unique
    among `taken` (case-insensitive: NTFS).  This is also what the agent cites as doc="..."."""
    base = re.split(r"[\\/]", filename or "")[-1]
    stem = re.sub(r"\.pdf$", "", base, flags=re.I)
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_-")[: MAX_DOC_NAME_CHARS - 4].strip("_-") or "report"
    if stem.lower() in _WINDOWS_RESERVED:
        stem = f"doc_{stem}"
    used = {t.lower() for t in taken}
    candidate, n = f"{stem}.pdf", 1
    while candidate.lower() in used:
        n += 1
        suffix = f"_{n}"
        candidate = f"{stem[: MAX_DOC_NAME_CHARS - 4 - len(suffix)]}{suffix}.pdf"
    return candidate


def display_filename(filename: str, fallback: str) -> str:
    """The original upload name for display: last path component, control characters gone, bounded."""
    name = re.split(r"[\\/]", filename or "")[-1]
    name = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", name).split())
    return name[:MAX_DISPLAY_NAME_CHARS] or fallback


def clean_questions(questions: Iterable[str], limit: int) -> list[str]:
    """The questions of a batch as they will be asked: trimmed, blanks and exact duplicates (any capitalisation) dropped, order
    kept.  400 empty_question when nothing is left, too_many_questions above `limit`, question_too_long above 4000 characters."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in questions or ():
        q = (raw or "").strip() if isinstance(raw, str) else ""
        if q and q.casefold() not in seen:
            seen.add(q.casefold())
            out.append(q)
    if not out:
        raise ServiceError("empty_question", "Add at least one question first.", 400)
    if len(out) > limit:
        raise ServiceError("too_many_questions", f"A set can hold at most {limit} questions; this one has {len(out)}.", 400)
    if any(len(q) > MAX_QUESTION_CHARS for q in out):
        raise ServiceError("question_too_long", f"Questions are limited to {MAX_QUESTION_CHARS} characters.", 400)
    return out


def select_eval_contexts(contexts: list[ContextPage], citations: list[Citation], cap: int) -> list[ContextPage]:
    """Pages handed to RAGAS (research/00 D4).  Distinct non-blank pages in read order; when more than `cap` were read, every
    CITED page is kept first (first come within the cap) and the remaining slots are filled in read order.  The result is
    always in read order: reordering to favour cited pages would flatter the rank-weighted context precision."""
    seen: set[int] = set()
    read: list[ContextPage] = []
    for ctx in contexts:
        if ctx.page not in seen and ctx.text.strip():
            seen.add(ctx.page)
            read.append(ctx)
    cap = max(1, cap)
    if len(read) <= cap:
        return read
    cited = {c.page for c in citations}
    keep = [c.page for c in read if c.page in cited][:cap]
    keep += [c.page for c in read if c.page not in cited][: cap - len(keep)]
    chosen = set(keep)
    return [c for c in read if c.page in chosen]


def build_history(messages: list[Message], turns: int) -> list[dict]:
    """Plain-text history for the agent: the last `turns` question/answer pairs whose answer succeeded, `[[cN]]` markers
    stripped (the model never saw them and they would only confuse it)."""
    pairs: list[tuple[Message, Message]] = []
    i = 0
    while i < len(messages) - 1:
        user, reply = messages[i], messages[i + 1]
        if user.role == "user" and reply.role == "assistant":
            if reply.status in _ANSWER_OK and reply.content.strip():
                pairs.append((user, reply))
            i += 2
        else:
            i += 1
    history: list[dict] = []
    for user, reply in pairs[-turns:] if turns > 0 else []:
        history.append({"role": "user", "content": user.content})
        history.append({"role": "assistant", "content": strip_markers(reply.content)})
    return history


def _clear_readonly(func: Callable, path: str, _exc: Any) -> None:
    """rmtree error hook: a read-only file (common after a PDF copy on Windows) is made writable and removed again."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree_once(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_readonly)
    else:  # pragma: no cover - the project targets 3.13; this keeps 3.10/3.11 working
        shutil.rmtree(path, onerror=_clear_readonly)


def remove_tree(path: Path) -> None:
    """Delete a folder, retrying with back-off on Windows sharing violations (antivirus, a still-winding-down indexer)."""
    delay = 0.05
    for attempt in range(_RMTREE_ATTEMPTS):
        try:
            _rmtree_once(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if attempt == _RMTREE_ATTEMPTS - 1:
                raise
            log.debug("could not remove %s yet (%s); retrying", path, exc)
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def _move_into_place(src: Path, dst: Path) -> None:
    """Atomic move of the upload into the session folder.  Across drives os.replace fails: copy beside the target first so
    the final step is still a rename and a crash never leaves a truncated PDF under the real name."""
    try:
        os.replace(src, dst)
        return
    except OSError as exc:
        log.debug("rename %s -> %s failed (%s); copying instead", src, dst, exc)
    part = dst.with_name(dst.name + ".part")
    try:
        shutil.copyfile(src, part)
        os.replace(part, dst)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    src.unlink(missing_ok=True)


def _prune_outline(nodes: list) -> list[dict]:
    return [
        {
            "title": n.get("title"),
            "node_id": n.get("node_id"),
            "start_index": n.get("start_index"),
            "end_index": n.get("end_index"),
            "nodes": _prune_outline(n.get("nodes") or []),
        }
        for n in nodes
        if isinstance(n, dict)
    ]


TreeLoader = Callable[[Settings, str, Optional[str]], list[dict]]      # (settings, session id, PageIndex doc id) -> nodes
IndexCloner = Callable[[Settings, str, str], str]                      # (settings, source sid, new sid) -> PageIndex doc id there


def _default_tree_loader(settings: Settings, sid: str, pi_doc_id: Optional[str]) -> list[dict]:
    from .indexer import load_tree

    return load_tree(settings, sid, pi_doc_id)


def _default_page_texts_loader(settings: Settings, sid: str, pi_doc_id: Optional[str]) -> Optional[list[str]]:
    from .indexer import load_page_texts

    return load_page_texts(settings, sid, pi_doc_id)


def _default_index_cloner(settings: Settings, source_sid: str, dest_sid: str) -> str:
    from .indexer import clone_index

    return clone_index(settings, source_sid, dest_sid)


# ----------------------------------------------------------------------------------------------- open resources
class _Resources:
    """What is derived from one ready document, built on first use and shared by every request on that session."""

    def __init__(self, sid: str, doc: DocumentInfo, path: Path, settings: Settings, tree_loader: TreeLoader):
        self.sid = sid
        self.doc = doc
        self.path = path
        self.pdf = PdfiumDoc(path)           # reads the bytes: no Windows file lock on the PDF; raises PdfError
        self._settings = settings
        self._tree_loader = tree_loader
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {}
        self.users = 0                       # guarded by the service's cache lock, as is `retired`
        self.retired = False

    def _memo(self, key: str, build: Callable[[], _T]) -> _T:
        with self._lock:
            if key not in self._values:
                self._values[key] = build()
            return self._values[key]

    @property
    def tree(self) -> list[dict]:
        def load() -> list[dict]:
            try:
                return self._tree_loader(self._settings, self.sid, self.doc.pi_doc_id)
            except Exception:  # noqa: BLE001 - the PDF stays usable without its outline (no breadcrumbs, empty outline)
                log.exception("could not load the page tree of session %s", self.sid)
                return []

        return self._memo("tree", load)

    @property
    def labels(self) -> list[Optional[str]]:
        def detect() -> list[Optional[str]]:
            try:
                try:        # the text PageIndex stored lets the folio vote skip blank pages without extracting again
                    texts = _default_page_texts_loader(self._settings, self.sid, self.doc.pi_doc_id)
                except Exception:  # noqa: BLE001 - optional speed-up only
                    texts = None
                return detect_printed_labels(self.path, texts)
            except Exception:  # noqa: BLE001 - folios are display-only; never fail an answer over them
                log.warning("could not detect printed page numbers of session %s", self.sid, exc_info=True)
                return [None] * self.pdf.page_count

        return self._memo("labels", detect)

    @property
    def sizes(self) -> list[tuple[float, float]]:
        return self._memo("sizes", self.pdf.page_sizes)


@dataclass
class _Run:
    """One in-flight question; a session has at most one registered run.  A batch registers one master run (its children are
    the questions' own runs: they share the master's `cancel`, so stopping the batch stops them all)."""
    cancel: threading.Event = field(default_factory=threading.Event)     # tells the engine thread to stop
    lock: threading.Lock = field(default_factory=threading.Lock)         # serialises the terminal write vs the abandon path
    terminal: bool = False                                                # the assistant message holds its final state
    abandoned: bool = False                                               # the consumer is gone
    thread: Optional[threading.Thread] = None
    active: int = 1                                                       # questions being worked on now: the budget holds money back for each
    children: "list[_Run]" = field(default_factory=list)


@dataclass
class _Turn:
    """One question on its way to an answer: its two stored rows and what the stream needs to know about it."""
    run: _Run
    user: Message
    assistant: Message
    history: list[dict] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)                     # tokens streamed so far (kept on the row if the run fails)
    index: Optional[int] = None                                           # position in a batch (None: a single question)
    silent: bool = False                                                  # nobody is listening any more: no closing events
    outcome: str = "pending"                                              # pending | answered | failed


class _Gates:
    """What a batch shares with every other batch of the process, so peak memory stays flat however many are running: a few agent
    slots, fewer scoring slots, and (small hosts) a lock that makes questions run one at a time while a report is being indexed."""

    def __init__(self, agents: int, evaluations: int):
        self.agent = asyncio.Semaphore(max(1, agents))
        self.evaluation = asyncio.Semaphore(max(1, evaluations))
        self.serial = asyncio.Lock()
        self.unfinished = 0                      # questions of all running batches that are not completely done (answered and scored, or failed)


# ----------------------------------------------------------------------------------------------- the service
class ReportLensService:
    def __init__(self, settings: Settings, *, store: Optional[Store] = None, indexer: "Optional[IndexService]" = None,
                 qa: "Optional[QAEngine]" = None, evaluator: "Optional[Evaluator]" = None,
                 tree_loader: Optional[TreeLoader] = None, index_cloner: Optional[IndexCloner] = None):
        self._settings = settings
        self._store = store if store is not None else Store(settings.db_path)
        if indexer is None:
            from .indexer import IndexService

            indexer = IndexService(settings, self._store)
        set_hook = getattr(indexer, "set_heavy_job_hook", None)          # the web app builds the indexer itself; test fakes may lack it
        if set_hook is not None:
            set_hook(self._shed_idle_documents)
        if qa is None:
            from .qa import QAEngine

            qa = QAEngine(settings)
        self._indexer = indexer
        self._qa = qa
        self._evaluator = evaluator
        self._evaluator_injected = evaluator is not None       # a test double scores every request, whoever's key
        self._tree_loader = tree_loader or _default_tree_loader
        self._index_cloner = index_cloner or _default_index_cloner
        self._cache: "OrderedDict[str, _Resources]" = OrderedDict()
        self._cache_lock = threading.Lock()
        self._lock_table_guard = threading.Lock()
        self._state_locks: dict[str, threading.Lock] = {}     # attach / delete / clone of one session
        self._load_locks: dict[str, threading.Lock] = {}      # building one session's resources
        self._runs: dict[str, _Run] = {}                      # touched on the event loop (and read by delete_session)
        self._eval_tasks: dict[str, asyncio.Task] = {}        # message id -> scoring task (strong refs keep them alive)
        self._gates: Optional[_Gates] = None                  # shared by every batch, built on first use (on the event loop)
        self._env_info: Optional[dict] = None
        self._budget = UsageBudget(settings, self._store)     # no-ops unless BUDGET_USD_TOTAL > 0
        self._create_lock = threading.Lock()                  # MAX_SESSIONS: count and insert as one step
        # Nothing is running yet: rows still 'indexing' / 'streaming' belong to a process that died.
        self._store.recover_interrupted()
        self._store.skip_interrupted_evaluations()

    # ------------------------------------------------------------------------------------------- plumbing
    def _lock_for(self, table: dict[str, threading.Lock], sid: str) -> threading.Lock:
        with self._lock_table_guard:
            return table.setdefault(sid, threading.Lock())

    def _session_dir(self, sid: str) -> Path:
        if not _SESSION_ID_RE.fullmatch(sid or ""):      # defence in depth: the id becomes a folder name
            raise ServiceError("session_not_found", "That chat no longer exists.", 404)
        return self._settings.session_dir(sid)

    def _require_session(self, sid: str) -> Session:
        self._session_dir(sid)
        session = self._store.get_session(sid)
        if session is None:
            raise ServiceError("session_not_found", "That chat no longer exists.", 404)
        return session

    def _require_ready(self, sid: str) -> DocumentInfo:
        doc = self._require_session(sid).document
        if doc is None:
            raise ServiceError("document_not_found", "This chat has no document yet.", 404)
        if doc.status != "ready":
            raise ServiceError("document_not_ready", "The document is still being indexed. You can ask once it is ready.", 409)
        return doc

    def _pdf_path(self, sid: str, doc: DocumentInfo) -> Path:
        return self._session_dir(sid) / doc.doc_name

    def _acquire(self, sid: str) -> _Resources:
        """A leased entry of the resource cache (built when missing or when the document was replaced).  Pair with _release."""
        doc = self._require_ready(sid)
        with self._lock_for(self._load_locks, sid):
            with self._cache_lock:
                res = self._cache.get(sid)
                if res is not None and res.doc.id == doc.id and not res.retired:
                    res.users += 1
                    self._cache.move_to_end(sid)
                    return res
            self._drop(sid)
            path = self._pdf_path(sid, doc)
            if not path.is_file():
                raise ServiceError("document_not_found", "The document file could not be found on the server.", 404)
            try:
                res = _Resources(sid, doc, path, self._settings, self._tree_loader)
            except PdfError as exc:
                log.error("stored PDF of session %s cannot be opened: %s", sid, exc.message)
                raise ServiceError("document_unreadable", "The stored document could not be opened.", 500) from exc
            res.users = 1
            if res.pdf.page_count > SQUASH_WARM_MIN_PAGES:      # we are in a worker thread: ~10 ms/page, once per open document
                try:
                    res.pdf.squashed_pages()
                except Exception:  # noqa: BLE001 - only the whole-document quote search depends on it
                    log.warning("could not prepare the whole-document search of session %s", sid, exc_info=True)
            with self._cache_lock:
                self._cache[sid] = res
                evicted = []
                while len(self._cache) > min(MAX_OPEN_DOCS, self._settings.max_open_docs):
                    evicted.append(self._cache.popitem(last=False)[1])
            for old in evicted:
                self._retire(old)
            return res

    def _release(self, res: _Resources) -> None:
        with self._cache_lock:
            res.users -= 1
            close_now = res.retired and res.users <= 0
        if close_now:
            res.pdf.close()

    def _retire(self, res: _Resources) -> None:
        with self._cache_lock:
            res.retired = True
            close_now = res.users <= 0
        if close_now:
            res.pdf.close()

    def _drop(self, sid: str) -> None:
        with self._cache_lock:
            res = self._cache.pop(sid, None)
        if res is not None:
            self._retire(res)

    def _shed_idle_documents(self) -> None:
        """Close every open document nobody is using and hand the memory back to the OS.  Called by the indexer just before an
        indexing child starts: on a 512 MB host the open PDF, its outline, folios and squashed text of the last answered report
        are tens of MB the child needs more.  A leased document stays open; the next question simply reopens what it needs."""
        with self._cache_lock:
            idle = [sid for sid, res in self._cache.items() if res.users <= 0]
            dropped = [self._cache.pop(sid) for sid in idle]
        for res in dropped:
            self._retire(res)
        from .lowmem import memory_summary, trim_memory

        trim_memory()
        log.info("memory before an indexing child (%d idle document(s) closed): %s", len(dropped), memory_summary())

    def _acquire_for_answers(self, sid: str) -> _Resources:
        """A lease whose tree and folios are already computed, so the event loop never pays for them."""
        res = self._acquire(sid)
        try:
            _ = res.tree, res.labels
        except BaseException:
            self._release(res)
            raise
        return res

    async def _lease_for_answers(self, sid: str) -> _Resources:
        """Async lease.  If the caller is cancelled while the thread is still building, the lease is returned when it lands
        (otherwise the document would stay open forever)."""
        task = asyncio.ensure_future(asyncio.to_thread(self._acquire_for_answers, sid))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(self._release_late)
            raise

    def _release_late(self, task: "asyncio.Future[_Resources]") -> None:
        if not task.cancelled() and task.exception() is None:
            self._release(task.result())

    @contextmanager
    def _resources(self, sid: str) -> Iterator[_Resources]:
        res = self._acquire(sid)
        try:
            yield res
        finally:
            self._release(res)

    def _reserve(self) -> float:
        """Budget held back for work that is running right now and whose cost is not stored yet (questions, scoring tasks)."""
        running = sum(run.active + sum(child.active for child in run.children) for run in list(self._runs.values()))
        return running * QUESTION_RESERVE_USD + len(self._eval_tasks) * self._settings.eval_cost_estimate_usd

    def _present(self, msg: Message) -> Message:
        """A score that is 'pending/running' with no task behind it (the process restarted, or the task was cancelled before
        it could write) would shimmer forever: show it as failed so the user can re-run it."""
        ev = msg.evaluation
        if msg.role == "assistant" and ev is not None and ev.status in ("pending", "running") and msg.id not in self._eval_tasks:
            stale = ev.model_copy(update={"status": "failed", "errors": {"evaluation": "Scoring was interrupted. Run it again."}})
            return msg.model_copy(update={"evaluation": stale})
        return msg

    # ------------------------------------------------------------------------------------------- sessions
    def create_session(self, from_session: Optional[str] = None, *, owner: str = "") -> Session:
        """`owner`: the visitor the chat belongs to ('' in the local single-user app).  `from_session` may be the demo chat:
        "ask your own question about this report" reuses its index, so it costs nothing to set up."""
        limit = self._settings.max_sessions
        with self._create_lock if limit > 0 else contextlib.nullcontext():
            if limit > 0 and self._store.count_sessions() >= limit:
                raise ServiceError("session_limit", f"This demo allows at most {limit} chats at once. Delete one to start another.", 429)
            if from_session is None:
                return self._store.create_session(owner=owner)
            return self._clone_session(from_session, owner=owner)

    def session_owner(self, sid: str) -> Optional[str]:
        """Who `sid` belongs to (None: no such chat).  The web layer's per-request access check."""
        return self._store.session_owner(sid)

    def _require_writable(self, sid: str) -> Session:
        session = self._require_session(sid)
        if session.read_only:
            raise ServiceError("demo_read_only", "The demo chat is read-only. Start your own chat to ask questions.", 403)
        return session

    def check_budget(self) -> None:
        """Raise the 402 budget_exhausted error when no more work may be started (the upload route asks before reading the body)."""
        self._budget.check(reserve_usd=self._reserve())

    def usage_status(self) -> dict:
        """{"enabled", "used_fraction"} for GET /api/config (a fraction only: the owner's dollar amounts stay private)."""
        return self._budget.status()

    def _clone_session(self, source_sid: str, *, owner: str = "") -> Session:
        """'New chat with the same document': copy the PDF and the PageIndex store (D3), no re-indexing, no cost."""
        source = self._require_session(source_sid)
        doc = source.document
        if doc is None or doc.status != "ready":
            raise ServiceError("document_not_ready", "That chat has no indexed document to reuse yet.", 409)
        if not self._pdf_path(source_sid, doc).is_file():          # the static demo: a transcript without its PDF
            raise ServiceError("document_not_available", "This report is not stored on the server. Start a chat and upload it.", 409)
        new = self._store.create_session(owner=owner)
        dest = self._session_dir(new.id)
        try:
            with self._lock_for(self._state_locks, source_sid):
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self._pdf_path(source_sid, doc), dest / doc.doc_name)
                pi_doc_id = self._index_cloner(self._settings, source_sid, new.id)
            copy = doc.model_copy(update={"id": new_id(), "pi_doc_id": pi_doc_id, "created_at": now_iso(),
                                          **({"key_source": "none"} if source.read_only else {})})
            self._store.put_document(new.id, copy)
        except Exception as exc:  # noqa: BLE001 - leave no half-made session behind
            log.exception("could not clone session %s into %s", source_sid, new.id)
            self._store.delete_session(new.id)
            try:
                remove_tree(dest)
            except OSError:
                log.warning("leftover files of the failed clone remain in %s", dest)
            raise ServiceError("clone_failed", "The document could not be copied into a new chat.", 500) from exc
        return self._store.get_session(new.id) or new

    def list_sessions(self, owner: Optional[str] = None) -> list[Session]:
        """`owner` given: that visitor's chats only.  None: every chat but the demo (the local single-user app)."""
        return self._store.list_sessions(owner)

    def get_session(self, sid: str) -> SessionDetail:
        session = self._require_session(sid)
        messages = [self._present(m) for m in self._store.list_messages(sid)]
        return SessionDetail(**{name: getattr(session, name) for name in Session.model_fields}, messages=messages)

    def rename_session(self, sid: str, title: str) -> Session:
        self._require_writable(sid)
        try:
            self._store.rename_session(sid, title)
        except ValueError as exc:
            raise ServiceError("invalid_title", "The title must not be empty.", 400) from exc
        return self._require_session(sid)

    def delete_session(self, sid: str) -> None:
        self._require_writable(sid)
        with self._lock_for(self._state_locks, sid):
            run = self._runs.get(sid)
            if run is not None:
                run.cancel.set()                       # an open answer stream ends with a 'cancelled' error
            self._indexer.cancel(sid)
            self._drop(sid)                            # PDFium handles first: an open document blocks deletion on Windows
            try:
                remove_tree(self._session_dir(sid))
            except OSError as exc:
                log.error("could not remove the files of session %s: %s", sid, exc)
                raise ServiceError("delete_failed", "The chat's files are still in use. Please try deleting it again in a moment.", 500) from exc
            self._budget.retire_session(sid)           # its rows are about to vanish: keep what they cost on the ledger
            self._store.delete_session(sid)
        with self._lock_table_guard:
            self._state_locks.pop(sid, None)
            self._load_locks.pop(sid, None)

    # ------------------------------------------------------------------------------------------- document
    def attach_document(self, sid: str, filename: str, tmp_path: Path, llm: Optional[Settings] = None) -> Session:
        """Validate and adopt an uploaded PDF.  On success `tmp_path` is moved away; on any error it is left for the caller to
        delete.  Allowed only while the session is empty or its previous upload failed (one document per session).
        `llm`: the request's own provider and key (a visitor's), default the server's."""
        tmp_path = Path(tmp_path)
        s = llm or self._settings
        if s.key_source == "server":
            self._budget.check(reserve_usd=self._reserve())        # 402 before any PDF work, so a refused upload costs nothing
        with self._lock_for(self._state_locks, sid):
            session = self._require_writable(sid)
            if session.state == "locked":
                raise ServiceError("document_locked", "The document is locked after the first question. Start a new chat to use a different one.", 409)
            if session.state in ("indexing", "ready"):
                raise ServiceError("document_already_uploaded", "This chat already has a document. Start a new chat to use a different one.", 409)
            try:
                info = inspect_pdf(tmp_path)
            except PdfError as exc:
                raise ServiceError(exc.code, exc.message, _PDF_ERROR_STATUS.get(exc.code, 400)) from exc
            if info.page_count > self._settings.max_pages:
                raise ServiceError("too_many_pages", f"This PDF has {info.page_count} pages; the limit is {self._settings.max_pages}.", 422)
            size = tmp_path.stat().st_size

            folder = self._session_dir(sid)
            if session.document is not None:           # a failed upload is being replaced: its files go first
                old = session.document
                self._budget.retire_document(old.pi_doc_id or f"doc:{old.id}", charge_index=old.progress > 0)
                self._drop(sid)
                try:
                    remove_tree(folder / "pageindex")
                    (folder / session.document.doc_name).unlink(missing_ok=True)
                except OSError as exc:
                    log.error("could not remove the previous upload of session %s: %s", sid, exc)
                    raise ServiceError("delete_failed", "The previous upload is still in use. Please try again in a moment.", 500) from exc
            folder.mkdir(parents=True, exist_ok=True)
            doc_name = slugify_doc_name(filename, os.listdir(folder))
            _move_into_place(tmp_path, folder / doc_name)

            doc = DocumentInfo(id=new_id(), filename=display_filename(filename, doc_name), doc_name=doc_name, size_bytes=size,
                               page_count=info.page_count, status="indexing", stage="queued", progress=0.0, created_at=now_iso(),
                               key_source=s.key_source)
            self._store.put_document(sid, doc)
            self._budget.invalidate()
            try:
                if llm is not None:
                    self._indexer.start(sid, folder / doc_name, settings=llm)
                else:
                    self._indexer.start(sid, folder / doc_name)
            except Exception:  # noqa: BLE001 - never leave a document in 'indexing' that nothing is indexing
                log.exception("could not start indexing for session %s", sid)
                self._store.update_document(sid, status="failed", stage="failed", error="Indexing could not be started. Please upload the document again.")
            return self._require_session(sid)

    def document_path(self, sid: str) -> Path:
        doc = self._require_session(sid).document
        path = self._pdf_path(sid, doc) if doc is not None else None
        if path is None or not path.is_file():
            raise ServiceError("document_not_found", "The document file could not be found on the server.", 404)
        return path

    def document_pages(self, sid: str) -> DocumentPages:
        with self._resources(sid) as res:
            labels, sizes = res.labels, res.sizes
            pages = [PageInfo(width=round(w, 2), height=round(h, 2), printed_page=labels[i] if i < len(labels) else None)
                     for i, (w, h) in enumerate(sizes)]
            return DocumentPages(page_count=res.pdf.page_count, pages=pages)

    def document_outline(self, sid: str) -> list[dict]:
        with self._resources(sid) as res:
            return _prune_outline(res.tree)

    def locate(self, sid: str, page: int, quote: Optional[str], claim: Optional[str]) -> LocateResponse:
        """Highlight rectangles for a passage: the quote when it verifies on the page (or a neighbour), else the sentence that
        best supports `claim`, else the quote's area, else just the page.  The same order the citation resolver uses."""
        quote, claim = (quote or "").strip(), (claim or "").strip()
        if not quote and not claim:
            raise ServiceError("missing_quote", "Provide a quote or a claim to locate.", 400)
        with self._resources(sid) as res:
            if not 1 <= page <= res.pdf.page_count:
                raise ServiceError("invalid_page", f"Page must be between 1 and {res.pdf.page_count}.", 400)
            found: Optional[LocateResult] = None
            area: Optional[LocateResult] = None
            if quote:
                hit = locate_quote(res.pdf, page, quote, neighbours=1)
                if hit.method in _VERIFIED:
                    found = hit
                elif hit.method == "block":
                    area = hit
            if found is None and claim:
                found = align_claim(res.pdf, page, claim)
            found = found or area
            if found is None:
                width, height = res.sizes[page - 1]
                return LocateResponse(page=page, hinted_page=page, method="page", score=0.0, rects=[], page_width=width, page_height=height)
            return LocateResponse(page=found.page, hinted_page=found.hinted_page, method=found.method, score=round(found.score, 4),
                                  rects=[Rect(**r) for r in found.rects], matched_text=found.matched_text,
                                  page_width=found.page_width or None, page_height=found.page_height or None)

    # ------------------------------------------------------------------------------------------- messages
    def get_message(self, sid: str, mid: str) -> Message:
        self._require_session(sid)
        msg = self._store.get_message(sid, mid)
        if msg is None:
            raise ServiceError("message_not_found", "That message no longer exists.", 404)
        return self._present(msg)

    def health(self) -> dict:
        if self._env_info is None:
            try:
                from .pageindex_compat import check_environment

                self._env_info = check_environment(self._settings)
            except Exception:  # noqa: BLE001 - health must answer even when the SDK layer is broken
                log.warning("environment check failed", exc_info=True)
                return self._health({})
        return self._health(self._env_info)

    def _health(self, env: dict) -> dict:
        return {"ok": True, "version": __version__, "pageindex_version": env.get("pageindex_version"),
                "openai_configured": self._settings.openai_configured, "demo_mock": self._settings.demo_mock, "environment": env}

    # ------------------------------------------------------------------------------------------- ask
    async def ask(self, sid: str, content: str, llm: Optional[Settings] = None) -> AsyncIterator[tuple[str, dict]]:
        """Yield (sse_event_name, payload) per docs/ARCHITECTURE.md section 6.  Validation problems raise ServiceError before
        the first event.  Closing the generator (client disconnect) cancels the agent run; scoring carries on regardless.
        `llm`: the request's own provider and key (a visitor's), default the server's."""
        s = llm or self._settings
        engine = self._engine_for(llm)
        question = (content or "").strip()
        if not question:
            raise ServiceError("empty_question", "Type a question first.", 400)
        if len(question) > MAX_QUESTION_CHARS:
            raise ServiceError("question_too_long", f"Questions are limited to {MAX_QUESTION_CHARS} characters.", 400)
        doc = await self._check_can_ask(sid, s)
        if sid in self._runs:                          # no await between this check and the registration below
            raise ServiceError("session_busy", "The previous question is still being answered. Wait for it to finish or press Stop.", 409)
        run = self._runs[sid] = _Run()
        turn: Optional[_Turn] = None
        try:
            began = asyncio.ensure_future(asyncio.to_thread(self._begin_turn, sid, question) if s.key_source == "server"
                                          else asyncio.to_thread(self._begin_turn, sid, question, s.key_source))
            try:
                user_msg, assistant, history = await asyncio.shield(began)
            except SessionNotFound as exc:
                raise ServiceError("session_not_found", "That chat no longer exists.", 404) from exc
            except asyncio.CancelledError:
                began.add_done_callback(self._cancel_late_turn)    # the thread still writes both rows; do not leave them 'streaming'
                raise
            turn = _Turn(run, user_msg, assistant, history)
            yield "message_start", {"user_message": _dump(user_msg), "message_id": assistant.id, "created_at": assistant.created_at}
            answer = self._answer(sid, doc, turn, engine, s)
            try:
                async for event in answer:
                    yield event
            finally:
                await answer.aclose()                  # releases the document lease and the rewrite task before the cleanup below
            if not turn.silent:
                yield "done", {}
        finally:
            self._runs.pop(sid, None)
            self._budget.invalidate()
            if turn is not None:
                await self._abandon(run, turn.assistant, turn.partial)

    def _engine_for(self, llm: Optional[Settings]) -> Any:
        return self._qa.with_settings(llm) if llm is not None and hasattr(self._qa, "with_settings") else self._qa

    async def _check_can_ask(self, sid: str, s: Settings) -> DocumentInfo:
        """What `ask` and `ask_batch` both require before anything is stored: a writable chat with a ready document, a configured
        provider and, on the server's own key, budget left.  Raises ServiceError (404 / 403 / 409 / 503 / 402)."""
        doc = (await asyncio.to_thread(self._require_writable, sid)).document
        if doc is None or doc.status != "ready":
            raise ServiceError("document_not_ready", "The document is not ready yet. You can ask once it has been indexed.", 409)
        if not s.openai_configured:
            raise ServiceError("openai_not_configured", "OpenAI API key is not configured - add OPENAI_API_KEY to .env and restart.", 503)
        if s.key_source == "server":
            await asyncio.to_thread(self._budget.check, reserve_usd=self._reserve())
        return doc

    async def _answer(self, sid: str, doc: DocumentInfo, turn: _Turn, engine: Any, s: Settings,
                      gates: Optional[_Gates] = None) -> AsyncIterator[tuple[str, dict]]:
        """Everything between `message_start` and `done` for ONE question whose rows already exist: the agent run (steps, tokens,
        citations), the stored answer (`answer_done`), the scoring (`eval_*`).  A failure ends as an `error` event with the
        row persisted as failed; the caller adds `done` unless `turn.silent`.  `gates` (a batch) makes the run wait for a free
        agent slot, checks the budget just before it starts and gives scoring its own small queue."""
        run, assistant, question, history, partial = turn.run, turn.assistant, turn.user.content, turn.history, turn.partial
        mid, sid_short = assistant.id, sid[:8]
        rewrite: Optional[asyncio.Task] = None
        res: Optional[_Resources] = None
        slot: list[Callable[[], None]] = []             # the agent slot of a batch question, until it is given back

        def give_back() -> None:
            """Free the agent slot and the budget reserve.  Only once the answer (or its failure) is stored and its scoring is
            queued: the next question's budget check must see what this one cost."""
            while slot:
                slot.pop()()
            if gates is not None:
                run.active = 0

        try:
            try:
                if history:                            # runs beside the agent; only the scoring needs its result
                    rewrite = asyncio.create_task(self._rewrite(question, history, engine), name=f"rewrite-{mid[:8]}")
                if gates is not None:
                    slot.append(await self._enter_agent_slot(gates))
                    if s.key_source == "server":       # checked when this question really starts: a batch stops cleanly when the money runs out
                        await asyncio.to_thread(self._budget.check, reserve_usd=self._reserve())
                    run.active = 1
                res = await self._lease_for_answers(sid)
            except ServiceError as exc:
                for event in await self._fail(turn, exc.code, exc.message):
                    yield event
                return
            ctx = CitationContext(doc_display_name=doc.filename, pdf=res.pdf, tree=res.tree, printed_labels=res.labels)

            inbox: asyncio.Queue = asyncio.Queue()
            run.thread = self._spawn_engine(asyncio.get_running_loop(), inbox, run, dict(
                session_id=sid, doc=doc, question=question, history=history, ctx=ctx, cancel=run.cancel), engine, turn.index)
            final: Optional[dict] = None
            failure: Optional[BaseException] = None
            while True:
                item = await inbox.get()
                if item is _END:
                    break
                if isinstance(item, BaseException):
                    failure = item
                    break
                kind = item.get("type")
                if kind == "step":
                    yield "step", {"message_id": mid, "step": _dump(item["step"])}
                elif kind == "step_done":
                    yield "step_done", {"message_id": mid, "step_id": item.get("step_id"), "elapsed_ms": item.get("elapsed_ms"),
                                        "label": item.get("label"), "pages": list(item.get("pages") or [])}
                elif kind == "token":
                    if item.get("text"):
                        partial.append(item["text"])
                        yield "token", {"message_id": mid, "text": item["text"]}
                elif kind == "citation":
                    yield "citation", {"message_id": mid, "citation": _dump(item["citation"])}
                elif kind == "final":
                    final = item
                else:
                    log.debug("ignoring engine event %r", kind)
            if final is None:
                code, message = self._failure_details(failure, run)
                events = await self._fail(turn, code, message)
                give_back()
                for event in events:
                    yield event
                return

            answer, contexts = self._build_answer(assistant, final)
            n_read = len({c.page for c in contexts})
            reason = self._skip_reason(answer.content, contexts)
            selected = [] if reason else select_eval_contexts(contexts, answer.citations, self._settings.eval_max_contexts)
            answer.evaluation = (EvalScores(status="skipped", skipped_reason=reason, n_contexts_input=n_read) if reason
                                 else EvalScores(status="pending", n_contexts_input=n_read, n_contexts_scored=len(selected)))
            if not await asyncio.to_thread(self._write_terminal, run, answer, contexts):
                turn.silent = True                     # abandoned while saving; the caller's cleanup has taken over
                return
            turn.outcome = "answered"
            sink: Optional[asyncio.Queue] = None
            if not reason:                             # no await since the terminal write: the task exists before anyone can leave
                sink = asyncio.Queue()
                pending_rewrite, rewrite = rewrite, None     # the scoring task owns the rewrite from here on
                standalone = (lambda: pending_rewrite) if pending_rewrite is not None else (lambda: self._constant(question))
                self._start_eval(sid, mid, standalone, answer.content, selected, n_read, sink, settings=s, gates=gates)
            give_back()                                # the next question may start (and check the budget) while this one is scored
            yield "answer_done", {"message": _dump(answer)}
            if sink is not None:
                yield "eval_started", {"message_id": mid, "metrics": list(_METRICS), "n_contexts": len(selected)}
                while True:
                    event = await sink.get()
                    if event is None:                  # the task ended without a result (cancelled)
                        break
                    yield event
                    if event[0] == "eval_done":
                        break
        except Exception:  # noqa: BLE001 - after message_start every failure must end as an error event, not a dead stream
            log.exception("question on session %s failed unexpectedly", sid_short)
            for event in await self._fail(turn, "agent_failed", "Something went wrong while answering. Please try again."):
                yield event
        finally:
            give_back()
            if res is not None:
                self._release(res)
            if rewrite is not None:
                rewrite.cancel()

    def _begin_turn(self, sid: str, question: str, key_source: str = "server") -> tuple[Message, Message, list[dict]]:
        """Persist the question and the (still empty) answer; build the history; title the session on its first question."""
        session = self._require_session(sid)
        previous = self._store.list_messages(sid)
        history = build_history(previous, self._settings.history_turns)
        user = Message(id=new_id(), session_id=sid, role="user", content=question, status="answered", created_at=now_iso())
        reply = Message(id=new_id(), session_id=sid, role="assistant", status="streaming", created_at=now_iso(), key_source=key_source)
        self._store.add_message(user)
        self._store.add_message(reply)
        if session.title == DEFAULT_TITLE and not any(m.role == "user" for m in previous):
            self._store.rename_session(sid, make_title(question))
        return user, reply, history

    def _cancel_late_turn(self, turn: "asyncio.Future[tuple[Message, Message, list[dict]]]") -> None:
        if not turn.cancelled() and turn.exception() is None:
            _, reply, _ = turn.result()
            self._store.update_message(reply.model_copy(update={"status": "error", "error": "cancelled"}))

    # ------------------------------------------------------------------------------------------- ask_batch
    def _batch_gates(self) -> _Gates:
        if self._gates is None:                        # on the event loop: no thread can race this
            s = self._settings
            self._gates = _Gates(s.batch_concurrency, 1 if s.low_memory else 2)
        return self._gates

    async def _enter_agent_slot(self, gates: _Gates) -> Callable[[], None]:
        """Wait for one of `batch_concurrency` agent slots.  While this process indexes a report (small hosts: that child needs
        ~280 MB) a question that starts also takes the one-at-a-time lock.  Returns the function that gives the slot back."""
        from .lowmem import INDEXING_ACTIVE

        await gates.agent.acquire()
        serial = False
        try:
            if self._settings.low_memory and INDEXING_ACTIVE.is_set():
                await gates.serial.acquire()
                serial = True
        except BaseException:
            gates.agent.release()
            raise

        def leave() -> None:
            if serial:
                gates.serial.release()
            gates.agent.release()

        return leave

    def _begin_batch(self, sid: str, questions: list[str], key_source: str = "server") -> list[tuple[Message, Message]]:
        """Persist every question and its (still empty) answer up front, in question order; title the session on its first question."""
        session = self._require_session(sid)
        first_ever = not any(m.role == "user" for m in self._store.list_messages(sid))
        rows: list[tuple[Message, Message]] = []
        for question in questions:
            user = Message(id=new_id(), session_id=sid, role="user", content=question, status="answered", created_at=now_iso())
            reply = Message(id=new_id(), session_id=sid, role="assistant", status="streaming", created_at=now_iso(), key_source=key_source)
            self._store.add_message(user)
            self._store.add_message(reply)
            rows.append((user, reply))
        if session.title == DEFAULT_TITLE and first_ever:
            self._store.rename_session(sid, make_title(questions[0]))
        return rows

    def _cancel_late_batch(self, began: "asyncio.Future[list[tuple[Message, Message]]]") -> None:
        if not began.cancelled() and began.exception() is None:
            for _, reply in began.result():
                self._store.update_message(reply.model_copy(update={"status": "error", "error": "cancelled"}))

    async def ask_batch(self, sid: str, questions: list[str], llm: Optional[Settings] = None) -> AsyncIterator[tuple[str, dict]]:
        """Answer a set of independent questions, `batch_concurrency` at a time, and yield (sse_event_name, payload) as
        `ask` does (docs/ARCHITECTURE.md section 13).  Every event of a question carries its 0-based `index` beside `message_id`.
        `batch_start` comes first (all rows already exist, in question order), `batch_done` when every answer has finished (scoring
        may still be running), `done` last.  Validation problems raise ServiceError before the first event.  One failing question
        never stops the others; closing the generator cancels all that are still running."""
        s = llm or self._settings
        engine = self._engine_for(llm)
        asked = clean_questions(questions, self._settings.max_batch_questions)
        doc = await self._check_can_ask(sid, s)
        if sid in self._runs:                          # no await between this check and the registration below
            raise ServiceError("session_busy", "Questions are still being answered in this chat. Wait for them to finish or press Stop.", 409)
        master = self._runs[sid] = _Run(active=0)
        gates = self._batch_gates()
        turns: list[_Turn] = []
        workers: list[asyncio.Task] = []
        try:
            began = asyncio.ensure_future(asyncio.to_thread(self._begin_batch, sid, asked, s.key_source))
            try:
                rows = await asyncio.shield(began)
            except SessionNotFound as exc:
                raise ServiceError("session_not_found", "That chat no longer exists.", 404) from exc
            except asyncio.CancelledError:
                began.add_done_callback(self._cancel_late_batch)
                raise
            for index, (user, reply) in enumerate(rows):
                child = _Run(cancel=master.cancel, active=0)
                master.children.append(child)
                turns.append(_Turn(child, user, reply, index=index))
            inbox: asyncio.Queue = asyncio.Queue()       # the questions start now; their events wait here behind `batch_start`
            loop = asyncio.get_running_loop()
            gates.unfinished += len(turns)
            for turn in turns:
                worker = loop.create_task(self._batch_worker(sid, doc, turn, engine, s, gates, inbox), name=f"batch-{turn.index}")
                worker.add_done_callback(lambda _w, g=gates: setattr(g, "unfinished", g.unfinished - 1))       # also for one cancelled before it started
                workers.append(worker)
            yield "batch_start", {"items": [{"index": t.index, "question": t.user.content, "user_message": _dump(t.user),
                                             "message_id": t.assistant.id} for t in turns],
                                  "concurrency": self._settings.batch_concurrency}
            running, answered, failed, decided, sent_done = len(turns), 0, 0, set(), False
            while running:
                index, name, data = await inbox.get()
                if name is None:                       # that question has finished (answer and scoring)
                    running -= 1
                    if index not in decided:           # it ended without a verdict (only when its consumer had left): count it as failed
                        decided.add(index)
                        failed += 1
                elif name in ("answer_done", "error"):
                    if index not in decided:
                        decided.add(index)
                        answered, failed = answered + (name == "answer_done"), failed + (name == "error")
                    yield name, {**data, "index": index}
                else:
                    yield name, {**data, "index": index}
                if not sent_done and len(decided) == len(turns):       # every answer is in: scoring may still be running
                    sent_done = True
                    yield "batch_done", {"answered": answered, "failed": failed}
            yield "done", {}
        finally:
            self._runs.pop(sid, None)
            if any(not w.done() for w in workers):
                master.cancel.set()                    # the consumer left (or the task was cancelled): stop every run
                for worker in workers:
                    worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            self._budget.invalidate()
            if turns:
                await asyncio.gather(*(self._abandon(t.run, t.assistant, t.partial) for t in turns), return_exceptions=True)

    async def _batch_worker(self, sid: str, doc: DocumentInfo, turn: _Turn, engine: Any, s: Settings, gates: _Gates,
                            inbox: asyncio.Queue) -> None:
        """One question of a batch: its events go to `inbox` tagged with the question's index; `(index, None, None)` ends it."""
        index = turn.index
        answer = self._answer(sid, doc, turn, engine, s, gates)
        try:
            async for name, data in answer:
                inbox.put_nowait((index, name, data))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - _answer reports its own failures; this only keeps one question from taking the batch down
            log.exception("question %s of a batch on session %s failed unexpectedly", index, sid[:8])
            for name, data in await self._fail(turn, "agent_failed", "Something went wrong while answering. Please try again."):
                inbox.put_nowait((index, name, data))
        finally:
            with contextlib.suppress(Exception):
                await answer.aclose()
            inbox.put_nowait((index, None, None))

    def _spawn_engine(self, loop: asyncio.AbstractEventLoop, inbox: asyncio.Queue, run: _Run, kwargs: dict,
                      engine: Any = None, index: Optional[int] = None) -> threading.Thread:
        """Run the sync QA generator on a dedicated daemon thread, forwarding every event (and its failure) to `inbox`."""
        def post(item: Any) -> None:
            try:
                loop.call_soon_threadsafe(inbox.put_nowait, item)
            except RuntimeError:                       # the loop is gone (shutdown): nobody is listening
                pass

        def work() -> None:
            events = None
            try:
                events = (engine or self._qa).ask(**kwargs)
                for event in events:
                    post(event)
                    if run.cancel.is_set():
                        break
            except Exception as exc:  # noqa: BLE001 - reported to the consumer, which maps it to an error event
                post(exc)
            finally:
                close = getattr(events, "close", None)
                if close is not None:
                    try:
                        close()                        # lets the engine cancel its agent run
                    except Exception:  # noqa: BLE001
                        log.debug("closing the engine generator failed", exc_info=True)
                post(_END)

        thread = threading.Thread(target=work, name=f"ask-{kwargs['session_id'][:8]}" + ("" if index is None else f"-{index}"), daemon=True)
        thread.start()
        return thread

    @staticmethod
    def _failure_details(failure: Optional[BaseException], run: _Run) -> tuple[str, str]:
        if run.cancel.is_set():
            return "cancelled", "Stopped before the answer was finished."
        code, message = getattr(failure, "code", None), getattr(failure, "message", None)
        if isinstance(code, str) and isinstance(message, str):          # a QAError: already worded for the user
            return code, message
        if failure is not None:
            log.error("the QA engine crashed", exc_info=failure)
        return "agent_failed", "The assistant stopped before producing an answer. Please try again."

    @staticmethod
    def _build_answer(assistant: Message, final: dict) -> tuple[Message, list[ContextPage]]:
        built = final["answer"]
        contexts = [_coerce(ContextPage, c) for c in final.get("contexts") or []]
        status = final.get("status")
        message = assistant.model_copy(update={
            "content": built.text,
            "status": status if status in _ANSWER_OK else "answered",
            "citations": [_coerce(Citation, c) for c in built.citations],
            "sources": [_coerce(Source, s) for s in built.sources],
            "steps": [_coerce(Step, s) for s in final.get("steps") or []],
            "usage": _coerce(Usage, final["usage"]) if final.get("usage") is not None else None,
            "elapsed_ms": final.get("elapsed_ms"),
            "error": None,
        })
        return message, contexts

    def _write_terminal(self, run: _Run, message: Message, contexts: Optional[list[ContextPage]] = None) -> bool:
        """Persist the assistant message's final state, once.  False when the consumer already left (then _abandon owns the
        message) or a final state was already written."""
        with run.lock:
            if run.abandoned or run.terminal:
                return False
            if contexts is not None:
                self._store.set_contexts(message.id, contexts)
            self._store.update_message(message)
            self._store.touch_session(message.session_id)
            run.terminal = True
            return True

    async def _fail(self, turn: _Turn, code: str, message: str) -> list[tuple[str, dict]]:
        """Persist the assistant message as failed and return the `error` event that tells the client (none if it is gone)."""
        failed = turn.assistant.model_copy(update={
            "status": "error", "error": code if code in _CODE_ERRORS else message,
            "content": strip_markers("".join(turn.partial)).strip()})
        if not await asyncio.to_thread(self._write_terminal, turn.run, failed):
            turn.silent = True
            return []
        turn.outcome = "failed"
        return [("error", {"code": code, "message": message, "message_id": turn.assistant.id})]

    async def _abandon(self, run: _Run, assistant: Message, partial: list[str]) -> None:
        """Cleanup when the stream ends.  If the message never reached a final state (client disconnect, task cancelled, server
        shutdown) stop the engine and mark it cancelled.  Runs on the cleanup path, so the marking is synchronous: it must not
        depend on one more successful await."""
        with run.lock:
            run.abandoned = True
            terminal = run.terminal
            if not terminal:
                run.cancel.set()
                self._store.update_message(assistant.model_copy(update={
                    "status": "error", "error": "cancelled", "content": strip_markers("".join(partial)).strip()}))
        thread = run.thread
        if not terminal and thread is not None and thread.is_alive():
            try:
                await asyncio.to_thread(thread.join, THREAD_JOIN_TIMEOUT_S)
            except BaseException:  # noqa: BLE001 - cancelled again while cleaning up: the thread is a daemon and its event is set
                log.debug("gave up waiting for the engine thread of session %s", assistant.session_id)

    @staticmethod
    async def _constant(value: _T) -> _T:
        return value

    async def _rewrite(self, question: str, history: list[dict], engine: Any = None) -> str:
        """Standalone form of a follow-up question (RAGAS user_input).  Any failure falls back to the question as typed."""
        try:
            text = await asyncio.to_thread((engine or self._qa).rewrite_question, history, question)
        except Exception:  # noqa: BLE001
            log.warning("question rewrite failed; scoring with the question as typed", exc_info=True)
            return question
        return (text or "").strip() or question

    # ------------------------------------------------------------------------------------------- evaluation
    def _skip_reason(self, answer: str, contexts: list[ContextPage]) -> Optional[str]:
        s = self._settings
        if not s.eval_enabled:
            return "disabled"
        if not (s.openai_configured or s.openai_base_url):
            return "no_api_key"
        if not strip_markers(answer).strip():
            return "empty_answer"
        if not any(c.text.strip() for c in contexts):
            return "no_contexts"
        return None

    def _new_evaluator(self, settings: Settings) -> Any:
        """A one-off evaluator for another provider / key (a visitor's own): the caller closes it after the run."""
        if settings.eval_in_subprocess:
            from .eval_child import ChildEvaluator

            return ChildEvaluator(settings)
        if settings.low_memory:
            from .lowmem import stub_datasets_for_ragas

            stub_datasets_for_ragas()
        from .evaluation import Evaluator

        return Evaluator(settings)

    def _get_evaluator(self) -> "Evaluator":
        if self._evaluator is None:
            if self._settings.eval_in_subprocess:       # the web process never imports RAGAS: a short-lived child scores each answer
                from .eval_child import ChildEvaluator

                self._evaluator = ChildEvaluator(self._settings)
                return self._evaluator
            if self._settings.low_memory:
                from .lowmem import stub_datasets_for_ragas

                stub_datasets_for_ragas()
            from .evaluation import Evaluator

            self._evaluator = Evaluator(self._settings)
        return self._evaluator

    def _start_eval(self, sid: str, mid: str, question: Callable[[], Awaitable[str]], answer: str, contexts: list[ContextPage],
                    n_read: int, sink: Optional[asyncio.Queue], *, settings: Optional[Settings] = None,
                    gates: Optional[_Gates] = None) -> asyncio.Task:
        """Start scoring as a task of its own (strongly referenced here, so it outlives the request that started it).
        `question` is a factory so nothing is created when the run is refused.  `gates` (a batch): the scoring waits for a free
        slot first, so only a few scorings (each a RAGAS run) are in memory at once."""
        if mid in self._eval_tasks:
            raise ServiceError("evaluation_in_progress", "This answer is already being evaluated.", 409)
        task = asyncio.get_running_loop().create_task(self._eval_job(sid, mid, question(), answer, contexts, n_read, sink, settings, gates),
                                                      name=f"eval-{mid[:8]}")
        self._eval_tasks[mid] = task

        def finished(t: asyncio.Task) -> None:
            if self._eval_tasks.get(mid) is t:
                del self._eval_tasks[mid]
            if not t.cancelled() and t.exception() is not None:
                log.error("evaluation task of %s crashed", mid, exc_info=t.exception())

        task.add_done_callback(finished)
        return task

    async def _eval_job(self, sid: str, mid: str, question: Awaitable[str], answer: str, contexts: list[ContextPage], n_read: int,
                        sink: Optional[asyncio.Queue], settings: Optional[Settings] = None,
                        gates: Optional[_Gates] = None) -> EvalScores:
        if gates is None:
            return await self._score(sid, mid, question, answer, contexts, n_read, sink, settings)
        try:
            await gates.evaluation.acquire()           # waits in the queue as 'pending': the row says 'running' only once it is scored
        except asyncio.CancelledError:
            self._store_eval(sid, mid, EvalScores(status="failed", errors={"evaluation": "Scoring was interrupted. Run it again."}, n_contexts_input=n_read))
            if sink is not None:
                sink.put_nowait(None)
            raise
        try:
            return await self._score(sid, mid, question, answer, contexts, n_read, sink, settings,
                                     more_coming=lambda: gates.unfinished > 1)       # others (besides this one) are still on their way
        finally:
            gates.evaluation.release()

    async def _score(self, sid: str, mid: str, question: Awaitable[str], answer: str, contexts: list[ContextPage], n_read: int,
                     sink: Optional[asyncio.Queue], settings: Optional[Settings] = None, *,
                     more_coming: Optional[Callable[[], bool]] = None) -> EvalScores:
        """Score one answer.  `more_coming` (it belongs to a set of questions; says whether other answers are still on their way): an
        evaluator that can keep its scoring child alive for the next answer (`ChildEvaluator`) is asked to, so the library import is paid
        once per set instead of once per answer."""
        own = settings is not None and settings is not self._settings and not self._evaluator_injected
        evaluator: Any = None
        def on_metric(metric: str, value: Optional[float], error: Optional[str]) -> None:
            if sink is not None:
                sink.put_nowait(("eval_result", {"message_id": mid, "metric": metric, "value": value, "error": error}))

        try:
            await asyncio.to_thread(self._store_eval, sid, mid, EvalScores(status="running", n_contexts_input=n_read, n_contexts_scored=len(contexts)))
            # The first call imports RAGAS (seconds, or tens of seconds on a 0.1 CPU host): never on the event loop, or every
            # stream would stop pinging and every request would stall meanwhile.
            evaluator = await asyncio.to_thread(self._new_evaluator, settings) if own else await asyncio.to_thread(self._get_evaluator)
            warm = {"keep_warm": more_coming} if more_coming and not own and "keep_warm" in inspect.signature(evaluator.evaluate).parameters else {}
            scores = await evaluator.evaluate(await question, answer, contexts, on_metric=on_metric, **warm)
            scores = scores.model_copy(update={"n_contexts_input": n_read})
        except asyncio.CancelledError:
            # Shutdown: leave an honest record (synchronously: awaiting again could be cancelled again) and release the stream.
            self._store_eval(sid, mid, EvalScores(status="failed", errors={"evaluation": "Scoring was interrupted. Run it again."}, n_contexts_input=n_read))
            if sink is not None:
                sink.put_nowait(None)
            raise
        except Exception:  # noqa: BLE001 - the evaluator promises not to raise; a fake or a bug must still end the stream
            log.exception("evaluation of message %s failed unexpectedly", mid)
            scores = EvalScores(status="failed", errors={m: "Scoring failed unexpectedly." for m in _METRICS}, n_contexts_input=n_read)
        finally:
            if own and evaluator is not None:              # a visitor's evaluator holds their key: never kept for the next request
                try:
                    await evaluator.aclose()
                except Exception:  # noqa: BLE001
                    log.debug("closing a per-request evaluator failed", exc_info=True)
        await asyncio.to_thread(self._store_eval, sid, mid, scores)
        if self._settings.low_memory:
            await asyncio.to_thread(_trim_memory)         # give the scoring buffers back to the OS: the host counts every MB
        if sink is not None:
            sink.put_nowait(("eval_done", {"message_id": mid, "evaluation": _dump(scores)}))
        return scores

    def _store_eval(self, sid: str, mid: str, scores: EvalScores) -> None:
        """Write scores into the stored message (re-read first: nothing else is overwritten)."""
        try:
            msg = self._store.get_message(sid, mid)
            if msg is not None:
                self._store.update_message(msg.model_copy(update={"evaluation": scores}))
        except Exception:  # noqa: BLE001 - e.g. the store was closed during shutdown
            log.warning("could not save the scores of message %s", mid, exc_info=True)

    async def evaluate_message(self, sid: str, mid: str, llm: Optional[Settings] = None) -> EvalScores:
        """(Re-)run RAGAS for a stored answer and persist the result.  Scoring is a task of its own: if this request is
        dropped the scores are still saved."""
        await asyncio.to_thread(self._require_writable, sid)
        msg = await asyncio.to_thread(self.get_message, sid, mid)
        if msg.role != "assistant" or msg.status not in _ANSWER_OK:
            raise ServiceError("not_evaluable", "Only a finished answer can be evaluated.", 409)
        if mid in self._eval_tasks:
            raise ServiceError("evaluation_in_progress", "This answer is already being evaluated.", 409)
        s = llm or self._settings
        if s.key_source == "server":
            await asyncio.to_thread(self._budget.check, reserve_usd=self._reserve())
        contexts = await asyncio.to_thread(self._store.get_contexts, mid)
        n_read = len({c.page for c in contexts})
        reason = self._skip_reason(msg.content, contexts)
        if reason:
            scores = EvalScores(status="skipped", skipped_reason=reason, n_contexts_input=n_read)
            await asyncio.to_thread(self._store_eval, sid, mid, scores)
            return scores
        selected = select_eval_contexts(contexts, msg.citations, self._settings.eval_max_contexts)
        rerun = msg.evaluation is not None and msg.evaluation.status in ("done", "partial", "failed")
        engine = self._qa.with_settings(llm) if llm is not None and hasattr(self._qa, "with_settings") else None
        task = self._start_eval(sid, mid, lambda: self._standalone_for(sid, mid, engine), msg.content, selected, n_read, None, settings=s)
        if rerun and s.key_source == "server":         # the stored row only ever shows one scoring: bill the repeats here
            self._budget.charge(self._settings.eval_cost_estimate_usd)
        return await asyncio.shield(task)

    async def _standalone_for(self, sid: str, mid: str, engine: Any = None) -> str:
        """The standalone question behind a stored answer (rewritten again when the chat had history before it)."""
        messages = await asyncio.to_thread(self._store.list_messages, sid)
        at = next((i for i, m in enumerate(messages) if m.id == mid), None)
        if at is None or at == 0 or messages[at - 1].role != "user":
            return ""
        question = messages[at - 1].content
        history = build_history(messages[: at - 1], self._settings.history_turns)
        return await self._rewrite(question, history, engine) if history else question

    # ------------------------------------------------------------------------------------------- shutdown
    async def aclose(self) -> None:
        """Stop what this service started: running questions, scoring tasks, the evaluator's HTTP client and open PDFs.  The
        store and the indexer belong to the caller."""
        runs = [r for top in list(self._runs.values()) for r in (top, *top.children)]
        for run in runs:
            run.cancel.set()
        tasks = list(self._eval_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for run in runs:
            if run.thread is not None and run.thread.is_alive():
                await asyncio.to_thread(run.thread.join, THREAD_JOIN_TIMEOUT_S)
        evaluator, self._evaluator = self._evaluator, None
        if evaluator is not None:
            await evaluator.aclose()
        with self._cache_lock:
            entries = list(self._cache.values())
            self._cache.clear()
        for res in entries:
            self._retire(res)
