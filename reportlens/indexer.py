"""Background indexing of one uploaded PDF per session with PageIndex (Flash by default), plus readers for the stored index.

One worker thread runs one job at a time (the SDK's own parallelism is already 64 LLM calls wide, and a second concurrent job
would only trade rate-limit errors); a second upload waits with stage "queued".  `submit_document` is synchronous and has no
progress hook, so progress is observed from outside:

* the page-text seam in `pageindex_compat` says when text extraction starts and ends,
* a counting shim around the SDK's LLM entry points (`utils`, `tree_optimize`, `page_index_classic` each bind their own copy of
  `llm_acompletion` / `llm_completion`) says how many model calls have finished and when the last, document-description,
  call starts.  The job a call belongs to travels in a ContextVar, so a job abandoned after a timeout can never be mistaken
  for its successor.

Everything the worker does ends in `Store.update_document`; no exception leaves the worker, and a document never stays in
status "indexing" once its job is over.
"""
from __future__ import annotations

import contextvars
import functools
import importlib
import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from reportlens import pageindex_compat as compat
from reportlens.config import Settings
from reportlens.models import DocumentInfo
from reportlens.pdfutil import PdfError, PdfInfo, inspect_pdf
from reportlens.store import INTERRUPTED_INDEXING_ERROR, Store, now_iso

if TYPE_CHECKING:
    from pageindex import PageIndexClient

log = logging.getLogger("reportlens.indexer")

ClientFactory = Callable[..., "PageIndexClient"]

STAGES = ("queued", "validating", "extracting_text", "building_tree", "summarizing", "finalizing", "ready")
NO_OUTLINE_NOTE = "No outline found - using slower LLM-based structure detection"
CANCELLED_ERROR = "Indexing was cancelled."

# progress milestones (best effort: the SDK offers no real progress)
_P_VALIDATING, _P_EXTRACTING, _P_TREE, _P_FINALIZING = 0.05, 0.10, 0.20, 0.95
_P_CAP = 0.95                      # never above this until the document is really ready
_FLASH_ESTIMATE_PER_PAGE = 0.8     # model calls ~ number of tree nodes ~ 0.8 x pages (a 60-page report: 52 calls)
_MIN_ESTIMATE = 10
_TREE_CREEP_TAU_S = 15.0           # while the SDK parses the layout (no model calls, ~25 s on 300 pages) progress eases 0.10 -> 0.20
_TICK_S = 1.0                      # how often the worker looks at a running job to let it creep
_WRITE_STEP = 0.01                 # progress is persisted when it moved by at least this much (or the stage changed)

WORKER_MODULE = "reportlens.indexing_worker"       # the child process that indexes when Settings.index_in_subprocess is on (tests swap it)
DEFAULT_RETRY_PAUSE_S = 20.0
DEFAULT_JOB_TIMEOUT_S = 30 * 60.0
LOW_MEMORY_JOB_TIMEOUT_S = 90 * 60.0     # 0.1 CPU: a 300-page report needs 13+ minutes of CPU alone before the model calls (docs/DEPLOY.md)


# ============================================================================================ error translation
@dataclass(frozen=True)
class Fault:
    """What went wrong, classified.  `kind` drives the retry policy, `message` is what the user reads."""
    kind: str       # auth | model | quota | rate_limit | upstream | storage | scanned | encrypted | no_outline | other
    message: str


class RemoteFault(Exception):
    """The indexing child process classified the failure itself (it had the exception and its chain); carry its verdict through."""

    def __init__(self, fault: "Fault"):
        super().__init__(fault.message)
        self.fault = fault


_OPENAI_MODEL_ERRORS = frozenset({"notfounderror", "permissiondeniederror"})     # openai.NotFoundError / PermissionDeniedError
_KEY_RE = re.compile(r"(?:sk-|gsk_|xai-|tgp_|AIza)[A-Za-z0-9_\-*.]{4,}")
_SPACE_RE = re.compile(r"\s+")
_SDK_PREFIX_RE = re.compile(r"^(Failed to submit document:\s*)+")


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def _short_reason(exc: BaseException, limit: int = 200) -> str:
    text = _SPACE_RE.sub(" ", _SDK_PREFIX_RE.sub("", str(exc))).strip() or type(exc).__name__
    text = _KEY_RE.sub("sk-...", text)
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def translate_error(exc: BaseException, settings: Settings) -> Fault:
    """Map whatever the SDK / litellm / OpenAI client raised to a classified, human-readable fault.

    The SDK wraps most failures in PageIndexAPIError("Failed to submit document: ...") and a 429 that survives its ten flat
    retries in LLMRetriesExhausted, so classification looks at the whole `__cause__` chain: HTTP statuses, exception class
    names and message text."""
    chain = _chain(exc)
    for e in chain:
        if isinstance(e, RemoteFault):
            return e.fault
    statuses = {s for e in chain if isinstance(s := getattr(e, "status_code", None), int)}
    class_names = {type(e).__name__.lower() for e in chain}
    names = " ".join(sorted(class_names))
    text = " ".join(str(e) for e in chain).lower()
    model = settings.index_model

    if "no text layer" in text or "all pages are blank" in text or "image-only" in text:
        return Fault("scanned", "The PDF has no text layer (it looks scanned). Run OCR on it and upload it again.")
    if "no layout structure" in text or "could not extract a structure" in text:
        return Fault("no_outline", "This PDF has no bookmarks or recognisable headings, so no table of contents can be built "
                                   "from it. Upload a version with an outline, or set PI_INDEX_FALLBACK_STANDARD=true to let "
                                   "the model build the structure (slower and more expensive).")
    if "password" in text and ("pdf" in text or "encrypt" in text):
        return Fault("encrypted", "The PDF is password protected; remove the password and upload it again.")
    if any(isinstance(e, OSError) and not isinstance(e, (ConnectionError, TimeoutError)) for e in chain):
        return Fault("storage", "Could not read or write the index files; set REPORTLENS_DATA_DIR to a short path "
                                "(Windows path-length limit).")
    if settings.key_source == "visitor" or settings.llm_provider != "openai":
        fault = _provider_fault(settings, statuses, names, text, class_names)
        if fault is not None:
            return fault
    if 429 in statuses and ("insufficient_quota" in text or "exceeded your current quota" in text):
        return Fault("quota", "Your OpenAI account has run out of credit or quota (HTTP 429). Add billing credit at "
                              "platform.openai.com and upload the document again.")
    if 429 in statuses or "ratelimit" in names or "rate limit" in text or "rate_limit" in text:
        return Fault("rate_limit", "OpenAI is rate-limiting this API key (HTTP 429), even after retrying with fewer parallel "
                                   "requests. Wait a few minutes and upload again, lower PI_INDEX_SUMMARY_CONCURRENCY, or "
                                   "raise your OpenAI usage tier.")
    if (401 in statuses or "authentication" in names or "invalid_api_key" in text or "incorrect api key" in text
            or "api_key client option" in text or "no api key" in text):
        if not settings.openai_api_key:
            return Fault("auth", "No OpenAI API key is configured. Set OPENAI_API_KEY in the server's .env file, restart "
                                 "ReportLens and upload the document again.")
        return Fault("auth", "OpenAI rejected the API key. Check OPENAI_API_KEY in the server's .env file, restart "
                             "ReportLens and upload the document again.")
    if (statuses & {403, 404} or bool(class_names & _OPENAI_MODEL_ERRORS)
            or "model_not_found" in text
            or ("model" in text and ("does not exist" in text or "do not have access" in text))):
        hint = " Also check OPENAI_BASE_URL." if settings.openai_base_url else ""
        return Fault("model", f"The OpenAI model '{model}' was not found or your account has no access to it. Set "
                              f"PI_INDEX_MODEL in the server's .env file to a model you can use, restart ReportLens and "
                              f"upload the document again.{hint}")
    if (any(s >= 500 for s in statuses) or any(n in names for n in ("connection", "timeout", "internalserver", "unavailable"))
            or "connection error" in text):
        code = max((s for s in statuses if s >= 500), default=None)
        suffix = f" (HTTP {code})" if code else ""
        return Fault("upstream", f"OpenAI could not be reached or returned a server error{suffix}. Check the network "
                                 f"connection and try again in a minute.")
    return Fault("other", f"Indexing failed: {_short_reason(exc)}")


def _provider_fault(settings: Settings, statuses: set[int], names: str, text: str, class_names: set[str]) -> Optional[Fault]:
    """The model-side failures, worded for a visitor's own key or a non-OpenAI provider (no ".env" advice for a visitor)."""
    from .providers import PROVIDERS, bare_model

    provider = PROVIDERS.get(settings.llm_provider)
    who = provider.label if provider else "The model provider"
    visitor = settings.key_source == "visitor"
    where = "under 'Model & API key'" if visitor else "in the server's settings"
    if 429 in statuses and ("quota" in text or "credit" in text or "billing" in text or "balance" in text):
        return Fault("quota", f"{who} says the account behind {'your' if visitor else 'this'} API key has run out of credit or quota "
                              f"(HTTP 429). Add credit with {who}, then upload the document again.")
    if 429 in statuses or "ratelimit" in names or "rate limit" in text or "rate_limit" in text:
        return Fault("rate_limit", f"{who} is rate-limiting {'your' if visitor else 'this'} API key (HTTP 429), even after retrying "
                                   f"with fewer parallel requests. Wait a few minutes and upload again.")
    if 401 in statuses or "authentication" in names or "invalid_api_key" in text or "incorrect api key" in text or "no api key" in text:
        return Fault("auth", f"{who} rejected the API key. Check it {where} and upload the document again.")
    if (statuses & {403, 404} or bool(class_names & _OPENAI_MODEL_ERRORS) or "model_not_found" in text
            or ("model" in text and ("does not exist" in text or "do not have access" in text or "not found" in text))):
        return Fault("model", f"{who} could not find the model '{bare_model(settings.index_model)}', or the key has no access to "
                              f"it. Check the model names {where} and upload the document again.")
    if (any(s >= 500 for s in statuses) or any(n in names for n in ("connection", "timeout", "internalserver", "unavailable"))
            or "connection error" in text):
        return Fault("upstream", f"{who} could not be reached or returned a server error. Try again in a minute.")
    return None


# ============================================================================================ progress / LLM-call shim
class JobCancelled(Exception):
    """Raised inside the SDK's LLM entry point to abort a cancelled job at its next model call.

    `status_code = 401` is deliberate: the SDK's `_is_unrecoverable` treats 401/403/404 as a misconfiguration that no retry
    can fix and stops the whole run, instead of degrading node by node and carrying on to spend money."""
    status_code = 401


class _Job:
    """One queued / running index job and its progress, shared between the worker, the job thread and the LLM shim."""

    def __init__(self, service: "IndexService", session_id: str, pdf_path: Path, settings: Optional[Settings] = None):
        self.service = service
        self.session_id = session_id
        self.pdf_path = pdf_path
        self.settings: Settings = settings if settings is not None else getattr(service, "_settings", None)   # a visitor's own, or the server's
        self.cancelled = threading.Event()
        self.cancel_reason = "cancelled"          # cancelled | shutdown | timeout
        self.thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._closed = False
        self.stage = "queued"
        self.progress = 0.0
        self.mode = "flash"
        self.calls = 0
        self.estimate = _MIN_ESTIMATE
        self._written: tuple[str, float] = ("", -1.0)
        self._stage_started = time.monotonic()
        self.note_shown = False
        self.pdf_info: Optional[PdfInfo] = None

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise JobCancelled(self.cancel_reason)

    def close(self) -> None:
        """No further store writes from this job (it finished, was cancelled, or its thread was abandoned)."""
        with self._lock:
            self._closed = True

    def report(self, stage: Optional[str] = None, progress: Optional[float] = None, **fields: Any) -> None:
        """Move to `stage` and/or `progress` (never backwards, never above the cap) and persist when it matters."""
        with self._lock:
            if self._closed:
                return
            if stage is not None:
                if stage != self.stage:
                    self._stage_started = time.monotonic()
                self.stage = stage
            if progress is not None:
                self.progress = max(self.progress, min(progress, _P_CAP))
            significant = self.stage != self._written[0] or self.progress - self._written[1] >= _WRITE_STEP
            if not (fields or significant):
                return
            self._written = (self.stage, self.progress)
            stage_now, progress_now = self.stage, self.progress
        self.service._update(self.session_id, status="indexing", stage=stage_now, progress=round(progress_now, 3), **fields)

    def tick(self) -> None:
        """Called about once a second by the worker: while the SDK parses the layout it makes no model calls, so progress
        would sit still; ease it from the extraction milestone towards (never to) the tree milestone so the UI shows life."""
        with self._lock:
            creeping = not self._closed and self.mode == "flash" and self.stage == "building_tree"
            elapsed = time.monotonic() - self._stage_started
        if creeping:
            try:
                self.report(progress=_P_EXTRACTING + (_P_TREE - _P_EXTRACTING) * (1.0 - math.exp(-elapsed / _TREE_CREEP_TAU_S)) * 0.999)
            except Exception:  # noqa: BLE001 - progress is cosmetic
                log.debug("progress update failed", exc_info=True)

    # ---- called by the LLM shim (job thread or its event loop)
    def before_call(self, kind: str) -> None:
        self.check_cancelled()
        try:
            if kind == "describe":
                self.report("finalizing", _P_FINALIZING)
            elif self.mode == "flash" and self.stage in ("extracting_text", "building_tree"):
                self.report("summarizing", self._summary_progress())
        except Exception:  # noqa: BLE001 - progress is cosmetic; never break an SDK call over it
            log.debug("progress update failed", exc_info=True)

    def after_call(self) -> None:
        try:
            with self._lock:
                self.calls += 1
            if self.stage in ("summarizing", "building_tree"):
                self.report(progress=self._summary_progress())
        except Exception:  # noqa: BLE001
            log.debug("progress update failed", exc_info=True)

    def _summary_progress(self) -> float:
        done = min(1.0, self.calls / max(1, self.estimate))
        return _P_TREE + (_P_CAP - _P_TREE) * done


_current_job: contextvars.ContextVar[Optional[_Job]] = contextvars.ContextVar("reportlens_index_job", default=None)
_COUNTED = "_reportlens_counted"

# (module, function, is_async, kind).  utils.llm_completion is only ever called for the final document description in flash
# mode, which is how "finalizing" is detected; page_index_classic holds its own copies of both functions (star import).
_SHIM_TARGETS = (
    ("pageindex.page_index_classic", "llm_acompletion", True, "call"),
    ("pageindex.page_index_classic", "llm_completion", False, "call"),
    ("pageindex.tree_optimize", "llm_acompletion", True, "call"),
    ("pageindex.utils", "llm_acompletion", True, "call"),
    ("pageindex.utils", "llm_completion", False, "describe"),
)
_SHIM_LOCK = threading.Lock()
_shims_installed = False


def _counted(fn: Callable[..., Any], is_async: bool, kind: str) -> Callable[..., Any]:
    if is_async:
        @functools.wraps(fn)
        async def acounted(*args: Any, **kwargs: Any) -> Any:
            job = _current_job.get()
            if job is None:
                return await fn(*args, **kwargs)
            job.before_call(kind)
            try:
                return await fn(*args, **kwargs)
            finally:
                job.after_call()
        wrapper: Callable[..., Any] = acounted
    else:
        @functools.wraps(fn)
        def counted(*args: Any, **kwargs: Any) -> Any:
            job = _current_job.get()
            if job is None:
                return fn(*args, **kwargs)
            job.before_call(kind)
            try:
                return fn(*args, **kwargs)
            finally:
                job.after_call()
        wrapper = counted
    setattr(wrapper, _COUNTED, True)
    return wrapper


def install_llm_counters() -> int:
    """Wrap the SDK's model-call functions so jobs can see how far along they are.  Idempotent; returns how many entry points
    are instrumented.  A seam that has moved is logged and skipped: indexing still works, only fine-grained progress is lost."""
    global _shims_installed
    with _SHIM_LOCK:
        modules: dict[str, Any] = {}
        for module_name, _, _, _ in _SHIM_TARGETS:      # import everything first: page_index_classic must still hold the
            if module_name not in modules:               # SDK's originals when utils is patched below
                try:
                    modules[module_name] = importlib.import_module(module_name)
                except ImportError as exc:
                    log.warning("cannot import %s (%s); indexing progress will be coarse", module_name, exc)
        count = 0
        for module_name, attr, is_async, kind in _SHIM_TARGETS:
            module = modules.get(module_name)
            fn = getattr(module, attr, None)
            if module is None or not callable(fn):
                if module is not None:
                    log.warning("%s.%s not found; indexing progress will be coarse", module_name, attr)
                continue
            if not getattr(fn, _COUNTED, False):
                setattr(module, attr, _counted(fn, is_async, kind))
            count += 1
        if count and not _shims_installed:
            log.info("indexing progress shim installed on %d of %d SDK entry points", count, len(_SHIM_TARGETS))
        _shims_installed = bool(count)
        return count


def _on_page_text(event: str) -> None:
    _apply_page_text(_current_job.get(), event)


def _apply_page_text(job: Optional["_Job"], event: str) -> None:
    if job is None or job.mode != "flash":     # standard mode keeps the single "building_tree" stage
        return
    if event == "start":
        job.report("extracting_text", _P_EXTRACTING)
    else:
        job.report("building_tree", _P_EXTRACTING)       # progress then creeps towards _P_TREE (_Job.tick), summaries take over


# ============================================================================================ tree helpers
def count_nodes(tree: list[dict]) -> int:
    """Number of nodes in a PageIndex tree (every level)."""
    return sum(1 + count_nodes(node.get("nodes") or []) for node in tree)


def _strip_text(nodes: list[dict]) -> list[dict]:
    return [{k: (_strip_text(v) if k == "nodes" else v) for k, v in node.items() if k != "text"} for node in nodes]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("unreadable index file %s: %s", path, exc)
        return None


def _doc_dir(settings: Settings, session_id: str, pi_doc_id: Optional[str] = None) -> Optional[Path]:
    """The stored document of a session.  A store holds one document in practice; if a retry ever left two, the newest wins."""
    docs = compat.storage_path(settings, session_id) / "docs"
    if pi_doc_id:
        found = docs / pi_doc_id
        return found if (found / "doc.json").is_file() else None
    best: Optional[tuple[str, Path]] = None
    if docs.is_dir():
        for d in docs.iterdir():
            meta = _read_json(d / "doc.json") if d.is_dir() else None
            if isinstance(meta, dict):
                stamp = str(meta.get("createdAt") or "")
                if best is None or stamp > best[0]:
                    best = (stamp, d)
    return best[1] if best else None


def load_tree(settings: Settings, session_id: str, pi_doc_id: Optional[str] = None) -> list[dict]:
    """The session's PageIndex tree (title, node_id, start_index, end_index, summary, nodes), without page text.  Empty when
    the session has no readable index."""
    doc = _doc_dir(settings, session_id, pi_doc_id)
    tree = _read_json(doc / "tree.json") if doc else None
    return _strip_text(tree) if isinstance(tree, list) else []


def load_page_texts(settings: Settings, session_id: str, pi_doc_id: Optional[str] = None) -> Optional[list[str]]:
    """The page texts PageIndex stored (index 0 == page 1; the pdfium text, see pageindex_compat), or None without an index."""
    doc = _doc_dir(settings, session_id, pi_doc_id)
    pages = _read_json(doc / "pages.json") if doc else None
    if not isinstance(pages, list) or not pages:
        return None
    by_index = {int(p["page_index"]): str(p.get("markdown") or "") for p in pages if isinstance(p, dict) and "page_index" in p}
    return [by_index.get(i, "") for i in range(1, max(by_index, default=0) + 1)] or None


def clone_index(settings: Settings, src_session_id: str, dst_session_id: str) -> str:
    """Copy a session's PageIndex store into another session ("new chat with this document") and return the document id that
    is valid there.  The copy lands next to its destination and is renamed into place, so a crash leaves no half store, and
    it is verified by reading it back through the SDK.  Raises FileNotFoundError (nothing to copy), FileExistsError (the
    destination already has an index) or RuntimeError (the copy does not read back)."""
    src = compat.storage_path(settings, src_session_id)
    dst = compat.storage_path(settings, dst_session_id)
    doc = _doc_dir(settings, src_session_id)
    if doc is None:
        raise FileNotFoundError("The source chat has no indexed document to copy.")
    if dst.exists() and any(dst.iterdir()):
        raise FileExistsError("The destination chat already has an index.")
    pi_doc_id = doc.name
    staging = dst.with_name(f"pageindex.copy-{uuid.uuid4().hex[:8]}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(src, staging, ignore=shutil.ignore_patterns("*.tmp", ".lock"))
        if dst.exists():
            dst.rmdir()
        staging.rename(dst)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    try:
        client = compat.make_client(settings, dst_session_id)
        client.get_document(pi_doc_id)
        if not client.get_page_content(pi_doc_id, "1"):
            raise RuntimeError("the copied index has no page content")
    except Exception as exc:
        shutil.rmtree(dst, ignore_errors=True)
        raise RuntimeError(f"The copied index could not be read back: {_short_reason(exc)}") from exc
    return pi_doc_id


# ============================================================================================ the service
@dataclass
class _Outcome:
    doc_id: str
    name: str                       # the document name the SDK stored (what the agent must cite as doc="...")
    tree: list[dict]
    description: Optional[str]


class IndexFailure(Exception):
    """A job failure whose message is already fit for the user."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class IndexService:
    """Index uploaded PDFs one at a time on a background thread; see the module docstring.

    `retry_pause_s` is the wait before the single whole-job retry after an OpenAI 429; `job_timeout_s` bounds one job."""

    def __init__(self, settings: Settings, store: Store, client_factory: ClientFactory = compat.make_client, *,
                 retry_pause_s: float = DEFAULT_RETRY_PAUSE_S, job_timeout_s: Optional[float] = None):
        self._settings = settings
        self._store = store
        self._factory = client_factory
        self._on_heavy_job: Optional[Callable[[], None]] = None      # see set_heavy_job_hook
        self._retry_pause_s = retry_pause_s
        self._job_timeout_s = job_timeout_s if job_timeout_s is not None else (LOW_MEMORY_JOB_TIMEOUT_S if settings.low_memory else DEFAULT_JOB_TIMEOUT_S)
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}            # queued or running, by session id
        self._queue: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._closed = False
        installed = 0
        if self._uses_child(settings):           # the child process instruments its own copy of the SDK; this process never imports it
            installed = -1
        else:
            try:
                installed = install_llm_counters()
            except Exception:  # noqa: BLE001 - progress is optional; the SDK must keep working untouched
                log.exception("could not instrument the SDK")
                installed = 0
        if not installed:
            log.warning("indexing runs without fine-grained progress")
        compat.set_page_text_listener(_on_page_text)

    def _uses_child(self, settings: Settings) -> bool:
        """Index in `python -m reportlens.indexing_worker`?  A custom client factory (tests, embedding) always stays in-process."""
        return settings.index_in_subprocess and self._factory is compat.make_client

    # ---------------------------------------------------------------------------------------- public API
    def set_heavy_job_hook(self, hook: Optional[Callable[[], None]]) -> None:
        """`hook` runs just before an indexing child starts (the web service closes idle documents there); its errors are logged."""
        self._on_heavy_job = hook

    def start(self, session_id: str, pdf_path: Path, settings: Optional[Settings] = None) -> None:
        """Queue the indexing of `pdf_path` for this session and return at once.  Idempotent per session while a job is queued
        or running.  `settings`: this job's model provider and key (a visitor's own), default the server's."""
        with self._lock:
            if self._closed:
                self._update(session_id, status="failed", stage="failed", error=INTERRUPTED_INDEXING_ERROR)
                return
            if session_id in self._jobs:
                log.warning("session %s is already being indexed; ignoring the second start", session_id)
                return
            job = _Job(self, session_id, Path(pdf_path), settings)
            self._jobs[session_id] = job
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name="reportlens-indexer", daemon=True)
                self._worker.start()
        self._update(session_id, status="indexing", stage="queued", progress=0.0, error=None)
        self._queue.put(job)

    def is_running(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._jobs

    def cancel(self, session_id: str) -> None:
        """Best effort: the job stops at its next model call (or before it starts), its result is discarded and the session's
        PageIndex store is removed."""
        with self._lock:
            job = self._jobs.get(session_id)
        if job is not None:
            job.cancel_reason = "cancelled"
            job.cancelled.set()

    def shutdown(self, wait: bool = True, timeout: Optional[float] = 30.0) -> None:
        """Stop accepting work and abort what is queued or running (their documents are marked interrupted).  With `wait`,
        block until the worker thread has finished (at most `timeout` seconds)."""
        with self._lock:
            self._closed = True
            jobs = list(self._jobs.values())
            worker = self._worker
        for job in jobs:
            job.cancel_reason = "shutdown"
            job.cancelled.set()
        self._queue.put(None)
        if wait and worker is not None:
            worker.join(timeout)

    # ---------------------------------------------------------------------------------------- worker
    def _update(self, session_id: str, **fields: Any) -> Optional[DocumentInfo]:
        try:
            return self._store.update_document(session_id, **fields)
        except Exception:  # noqa: BLE001 - the worker must survive a full disk, a closed store, a deleted session...
            log.exception("could not update the document of session %s", session_id)
            return None

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                self._run(job)
            except BaseException:  # noqa: BLE001 - never let the one worker thread die
                log.exception("indexing worker error for session %s", job.session_id)
            finally:
                job.close()
                with self._lock:
                    if self._jobs.get(job.session_id) is job:
                        del self._jobs[job.session_id]
                self._ensure_not_indexing(job)

    def _run(self, job: _Job) -> None:
        started = time.monotonic()
        if job.cancelled.is_set():
            return self._finish_discarded(job)
        job.report("validating", _P_VALIDATING)
        box: dict[str, Any] = {}

        def pipeline() -> None:
            _current_job.set(job)
            try:
                box["outcome"] = self._pipeline(job)
            except BaseException as exc:  # noqa: BLE001 - reported to the worker thread below
                box["error"] = exc

        job.thread = threading.Thread(target=pipeline, name=f"reportlens-index-{job.session_id[:8]}", daemon=True)
        job.thread.start()
        deadline = time.monotonic() + self._job_timeout_s
        while job.thread.is_alive() and (left := deadline - time.monotonic()) > 0:
            job.thread.join(min(_TICK_S, left))
            job.tick()
        if job.thread.is_alive():
            # The SDK call cannot be interrupted; it is told to stop at its next model call and its result is dropped.
            job.cancel_reason = "timeout"
            job.cancelled.set()
            job.close()
            log.error("indexing of session %s exceeded %.0f s; abandoned", job.session_id, self._job_timeout_s)
            return self._fail(job, f"Indexing took longer than {int(self._job_timeout_s // 60)} minutes and was stopped. "
                                   f"Try again, or upload a smaller report.")
        error = box.get("error")
        if job.cancelled.is_set() or isinstance(error, JobCancelled):
            return self._finish_discarded(job)
        if isinstance(error, IndexFailure):
            return self._fail(job, error.message)
        if error is not None:
            log.error("indexing of session %s failed", job.session_id, exc_info=error)
            return self._fail(job, translate_error(error, job.settings).message)
        self._complete(job, box["outcome"], time.monotonic() - started)

    def _finish_discarded(self, job: _Job) -> None:
        job.close()
        shutil.rmtree(compat.storage_path(self._settings, job.session_id), ignore_errors=True)
        message = INTERRUPTED_INDEXING_ERROR if job.cancel_reason == "shutdown" else CANCELLED_ERROR
        log.info("indexing of session %s discarded (%s)", job.session_id, job.cancel_reason)
        self._fail(job, message, quiet=True)

    def _fail(self, job: _Job, message: str, *, quiet: bool = False) -> None:
        job.close()
        if not quiet:
            log.warning("indexing of session %s failed: %s", job.session_id, message)
        fields: dict[str, Any] = {"status": "failed", "stage": "failed", "error": message}
        if job.note_shown:
            fields["description"] = None
        self._update(job.session_id, **fields)

    def _complete(self, job: _Job, outcome: _Outcome, elapsed: float) -> None:
        job.close()
        doc = self._store.get_document(job.session_id)
        info = job.pdf_info
        if doc is not None and outcome.name and outcome.name != doc.doc_name:
            log.warning("PageIndex stored session %s as %r but the document is named %r; the agent will cite the former",
                        job.session_id, outcome.name, doc.doc_name)
        title = _choose_title(info.title if info else None, doc.filename if doc else None, outcome.tree)
        updated = self._update(
            job.session_id, status="ready", stage="ready", progress=1.0, error=None, title=title,
            description=outcome.description or None, page_count=info.page_count if info else None,
            node_count=count_nodes(outcome.tree), pi_doc_id=outcome.doc_id, indexed_at=now_iso(),
            index_seconds=round(elapsed, 1))
        if updated is None:
            log.info("session %s vanished while indexing; the result is dropped", job.session_id)
        else:
            log.info("indexed session %s: %s pages, %d nodes in %.1f s", job.session_id, updated.page_count,
                     updated.node_count or 0, elapsed)

    def _ensure_not_indexing(self, job: _Job) -> None:
        """Last line of defence: whatever happened, the document must not be left claiming to be indexing."""
        try:
            doc = self._store.get_document(job.session_id)
            if doc is not None and doc.status == "indexing" and not self.is_running(job.session_id):
                log.error("session %s was still 'indexing' after its job ended; marking it failed", job.session_id)
                self._update(job.session_id, status="failed", stage="failed",
                             error="Indexing failed unexpectedly. Please upload the document again.")
        except Exception:  # noqa: BLE001
            log.exception("could not verify the document state of session %s", job.session_id)

    # ---------------------------------------------------------------------------------------- the job itself
    def _pipeline(self, job: _Job) -> _Outcome:
        """validate -> index (flash, then standard if there is no outline and the fallback is on; one whole-job retry after a
        429 with half the concurrency) -> read the tree back.  Runs on the job thread."""
        settings = job.settings
        try:
            info = inspect_pdf(job.pdf_path)
        except PdfError as exc:
            raise IndexFailure(exc.message) from exc
        if info.page_count > settings.max_pages:
            raise IndexFailure(f"The PDF has {info.page_count} pages; the limit is {settings.max_pages}.")
        job.pdf_info = info
        job.estimate = max(_MIN_ESTIMATE, round(_FLASH_ESTIMATE_PER_PAGE * info.page_count))
        job.report(page_count=info.page_count)
        # A store left by an earlier failed attempt would make the SDK suffix the new document's name ("report_1.pdf"), and the
        # agent would then cite a name nobody expects.  Only empty or failed sessions are ever indexed, so it is stale.
        shutil.rmtree(compat.storage_path(settings, job.session_id), ignore_errors=True)
        mode, rate_retried = "flash", False
        while True:
            job.check_cancelled()
            try:
                return self._index_once(job, settings, mode)
            except JobCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                job.check_cancelled()
                fault = translate_error(exc, settings)
                if fault.kind == "no_outline" and mode == "flash" and settings.index_fallback_standard:
                    log.info("no outline in %s; falling back to standard indexing", job.pdf_path.name)
                    mode, job.mode, job.calls = "standard", "standard", 0
                    job.note_shown = True
                    job.report("building_tree", _P_TREE, description=NO_OUTLINE_NOTE)
                    continue
                if fault.kind == "rate_limit" and not rate_retried:
                    rate_retried = True
                    settings = settings.with_(index_summary_concurrency=max(1, settings.index_summary_concurrency // 2))
                    log.warning("OpenAI rate limit while indexing; retrying once with concurrency %d after %.0f s",
                                settings.index_summary_concurrency, self._retry_pause_s)
                    job.calls = 0
                    if job.cancelled.wait(self._retry_pause_s):
                        raise JobCancelled(job.cancel_reason) from exc
                    continue
                if fault.kind in ("other", "storage"):
                    log.error("indexing of %s failed", job.pdf_path.name, exc_info=exc)
                raise IndexFailure(fault.message) from exc

    def _index_once(self, job: _Job, settings: Settings, mode: str) -> _Outcome:
        if self._uses_child(settings):
            return self._index_in_child(job, settings, mode)
        if mode == "flash":
            client = self._factory(settings, job.session_id, for_indexing=True)
        else:
            client = self._factory(settings, job.session_id, for_indexing=True, mode="standard")
        job.report("extracting_text" if mode == "flash" else "building_tree",
                   _P_EXTRACTING if mode == "flash" else _P_TREE)
        result = (client.submit_document(str(job.pdf_path)) if mode == "flash"
                  else client.submit_document(str(job.pdf_path), mode="standard"))
        job.check_cancelled()
        job.report("finalizing", _P_FINALIZING)
        doc_id = result["doc_id"]
        tree = client.get_tree(doc_id, node_summary=True, include_text=False)["result"]
        meta = client.get_document(doc_id)
        return _Outcome(doc_id, str(result.get("name") or meta.get("name") or ""), tree, meta.get("description"))

    # ---------------------------------------------------------------------------------------- the child-process variant
    def _index_in_child(self, job: _Job, settings: Settings, mode: str) -> _Outcome:
        """Same job as `_index_once`, run by `python -m reportlens.indexing_worker` (see that module for why and for the protocol).
        The child's model-call and text-extraction events drive the same progress reporting; cancelling kills the child."""
        from reportlens.lowmem import HEAVY_JOB_LOCK, INDEXING_ACTIVE

        job.report("extracting_text" if mode == "flash" else "building_tree",
                   _P_EXTRACTING if mode == "flash" else _P_TREE)
        while not HEAVY_JOB_LOCK.acquire(timeout=0.5):         # a scoring child (short) is running: indexing and scoring never overlap on a small host
            job.check_cancelled()
        try:
            if self._on_heavy_job is not None:
                try:
                    self._on_heavy_job()
                except Exception:  # noqa: BLE001 - only a memory saver: the job runs either way
                    log.warning("could not free memory before the indexing child", exc_info=True)
            INDEXING_ACTIVE.set()
            return self._run_child(job, settings, mode)
        finally:
            INDEXING_ACTIVE.clear()
            HEAVY_JOB_LOCK.release()

    def _run_child(self, job: _Job, settings: Settings, mode: str) -> _Outcome:
        from reportlens.config import PROJECT_ROOT, settings_to_json

        env = dict(os.environ)
        if settings.key_source == "visitor":       # the child gets the visitor's key in its settings; the owner's stays out of reach
            for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "LLM_API_KEY", "LLM_BASE_URL"):
                env.pop(name, None)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(PROJECT_ROOT), env.get("PYTHONPATH")) if p)
        env.setdefault("MALLOC_ARENA_MAX", "2")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen([sys.executable, "-m", WORKER_MODULE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                text=True, encoding="utf-8", bufsize=1, env=env, cwd=str(PROJECT_ROOT))
        lines: "queue.Queue[Optional[str]]" = queue.Queue()

        def pump() -> None:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        threading.Thread(target=pump, name=f"index-child-{job.session_id[:8]}", daemon=True).start()
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({"settings": settings_to_json(settings), "session_id": job.session_id,
                                         "pdf_path": str(job.pdf_path), "mode": mode}) + "\n")
            proc.stdin.flush()
            outcome: Optional[_Outcome] = None
            fault: Optional[Fault] = None
            while True:
                if job.cancelled.is_set():
                    raise JobCancelled(job.cancel_reason)
                try:
                    line = lines.get(timeout=0.5)
                except queue.Empty:
                    continue
                if line is None:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    log.warning("indexing child printed a non-protocol line: %.120r", line)
                    continue
                kind = event.get("ev")
                if kind == "page_text":
                    _apply_page_text(job, str(event.get("event")))
                elif kind == "before":
                    job.before_call(str(event.get("kind")))
                elif kind == "after":
                    job.after_call()
                elif kind == "result":
                    log.info("indexing child of session %s finished (%s)", job.session_id[:8], event.get("mem") or "no memory figures")
                    outcome = _Outcome(event["doc_id"], str(event.get("name") or ""), event.get("tree") or [], event.get("description"))
                elif kind == "fault":
                    fault = Fault(str(event.get("kind") or "other"), str(event.get("message") or "Indexing failed."))
        finally:
            _stop_child(proc)
        if outcome is not None:
            return outcome
        if fault is not None:
            raise RemoteFault(fault)
        code = proc.returncode
        if code is not None and code < 0 or code == 137:          # killed by a signal (SIGKILL = the kernel's out-of-memory killer)
            raise RemoteFault(Fault("memory", "The server ran out of memory while indexing this report; it is too large for this host. "
                                              "Try a shorter report, or ask the owner to run it on a bigger instance."))
        raise RemoteFault(Fault("other", f"Indexing stopped unexpectedly (exit code {code}). Please try again."))


def _stop_child(proc: "subprocess.Popen[str]") -> None:
    """Let a finished child exit by itself, kill one that is still running (cancelled, timed out, shutting down)."""
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


_GENERIC_TITLES = frozenset({"preface", "contents", "cover", "untitled", "document"})


def _choose_title(pdf_title: Optional[str], filename: Optional[str], tree: list[dict]) -> Optional[str]:
    """PDF metadata title if it says something, else the file name without extension, else the first tree heading."""
    if pdf_title and len(pdf_title.strip()) >= 3 and pdf_title.strip().lower() not in _GENERIC_TITLES:
        return pdf_title.strip()[:200]
    if filename:
        stem = _SPACE_RE.sub(" ", Path(filename).stem.replace("_", " ").replace("-", " ")).strip()
        if stem:
            return stem[:200]
    for node in tree:
        title = str(node.get("title") or "").strip()
        if title and title.lower() not in _GENERIC_TITLES:
            return title[:200]
    return None
