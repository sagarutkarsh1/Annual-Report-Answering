"""The ONLY place that patches or constructs the PageIndex SDK (D1 in docs/ARCHITECTURE.md).

The SDK releases roughly weekly, so everything that reaches into its internals lives here, is guarded, and degrades with a loud
warning instead of crashing:

* `apply_patches()`  - (1) page text from pdfium instead of PyPDF2 (D4), (2) PDFium's process-wide lock around the SDK's own
  PDFium use (the Flash layout parser runs in-process for documents under 64 pages and PDFium is not thread-safe), (3) the
  OpenAI Agents SDK's tracing is switched off.
* `configure_openai_env()` - key / base URL for the SDK, litellm and the OpenAI client, applied once per change.
* `make_client()`    - a `PageIndexClient` whose storage is the session's own folder (D3).
* `check_environment()` - versions and wiring for GET /api/health.
"""
from __future__ import annotations

import io
import logging
import os
import sys
import threading
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from reportlens.config import Settings

if TYPE_CHECKING:
    from pageindex import PageIndexClient

log = logging.getLogger("reportlens.pageindex_compat")

PAGEINDEX_TESTED_VERSION = "0.2.21"

# Privacy defaults, applied before the SDKs that read them are first imported (setdefault: an explicit choice wins).
#  - litellm would fetch its model-cost map over the network on import (PageIndex sets the same default, but only if its own
#    `utils` module is imported before litellm).
#  - RAGAS sends anonymous usage analytics unless told not to; it only honours the exact string "true".
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")

# page-text events for progress reporting: "start" / "done" around the SDK's text extraction (the first thing it does)
PageTextListener = Callable[[str], None]

_PATCH_LOCK = threading.RLock()
_patched_text = False          # LocalAPI._extract_page_texts replaced by the pdfium version
_patched_parser = False        # flash.main.parse_charlevel_meta_parallel wrapped (workers=1 -> page-at-a-time, see _lean_parse)
_patched_locks: list[str] = []  # SDK callables now wrapped in PDFIUM_LOCK
_original_extract: Any = None  # the SDK's own staticmethod object, kept so the patch can be undone
_original_locked: dict[tuple[Any, str], Any] = {}
_page_text_listener: Optional[PageTextListener] = None


# ------------------------------------------------------------------------------------------------ versions
def _version(dist: str) -> Optional[str]:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def pageindex_version() -> Optional[str]:
    return _version("pageindex")


# ------------------------------------------------------------------------------------------------ patches
def set_page_text_listener(listener: Optional[PageTextListener]) -> None:
    """Register (or clear) the callback told when the SDK starts and finishes reading page text.  Used by the indexer for
    the extracting_text stage; a listener that raises never breaks indexing."""
    global _page_text_listener
    _page_text_listener = listener


def _notify(event: str) -> None:
    listener = _page_text_listener
    if listener is None:
        return
    try:
        listener(event)
    except Exception:  # noqa: BLE001 - progress is cosmetic
        log.debug("page-text listener failed", exc_info=True)


def _pdfium_page_texts(file_path: str) -> list[str]:
    """Replacement for `LocalAPI._extract_page_texts`: the same clean pdfium text the viewer-side highlighter searches."""
    from reportlens.pdfutil import extract_page_texts

    _notify("start")
    try:
        return extract_page_texts(Path(file_path))
    finally:
        _notify("done")


def _patch_page_text() -> bool:
    """D4.  Verified against 0.2.21: `submit_document` calls `self._extract_page_texts(file_path)` and expects list[str]."""
    global _patched_text, _original_extract
    try:
        from pageindex.local_api import LocalAPI
    except ImportError as exc:
        log.warning("PageIndex SDK is not importable (%s); page text patch skipped", exc)
        return False
    current = LocalAPI.__dict__.get("_extract_page_texts")
    if current is None:
        log.warning("PageIndex %s has no LocalAPI._extract_page_texts; page text stays on PyPDF2 (digits may be corrupted)",
                    pageindex_version())
        return False
    if not _patched_text:
        _original_extract = current
        LocalAPI._extract_page_texts = staticmethod(_pdfium_page_texts)  # type: ignore[method-assign]
        _patched_text = True
    return True


_parse_workers: Optional[int] = None     # None = the SDK decides (one process per CPU core - 1 for PDFs of 64+ pages); 1 = parse in-process


def set_parse_workers(workers: Optional[int]) -> None:
    """How many processes the Flash layout parser may use.  The SDK spawns up to `cpu_count() - 1` worker processes for a PDF of
    64+ pages, and every worker re-imports the whole SDK stack (about 100 MB each).  On a 512 MB host `os.cpu_count()` still shows
    the machine's cores, so that is an instant out-of-memory kill: small hosts pin this to 1 (sequential, identical output)."""
    global _parse_workers
    _parse_workers = workers


def _locked(fn: Callable[..., Any], *, fill_workers: bool = False) -> Callable[..., Any]:
    """`fill_workers` (extract_toc(doc, workers=None, use_embedded_toc=True)): pass `workers=_parse_workers` unless the caller chose."""
    from reportlens.pdfutil import PDFIUM_LOCK

    def run_locked(*args: Any, **kwargs: Any) -> Any:
        if fill_workers and _parse_workers is not None and "workers" not in kwargs and len(args) < 2:
            kwargs["workers"] = _parse_workers
        with PDFIUM_LOCK:
            result = fn(*args, **kwargs)
        if fill_workers and _parse_workers == 1:       # small host: the layout parse just freed hundreds of MB; give them back before the model calls start
            from reportlens.lowmem import trim_memory

            trim_memory()
        return result

    run_locked.__name__ = getattr(fn, "__name__", "run_locked")
    run_locked.__wrapped__ = fn  # type: ignore[attr-defined]
    return run_locked


def _lean_parse(doc_handle: Any, original: Callable[..., Any]) -> Any:
    """The Flash layout parser for ONE process, one page at a time.

    The SDK's own sequential path (`parse_charlevel_meta`) extracts the raw characters of EVERY page into Python dicts before it
    merges any of them: for a 300-page annual report that is several hundred MB, which is what decides whether a 512 MB host
    survives.  Its process-pool path instead runs pass 1 + pass 2 per page and keeps only the merged spans; this runs those same
    per-page functions in this process.  Output is identical (the SDK's parity contract); the one document feature the per-page
    route cannot handle (identity-matrix Type-3 fonts need document-wide sizing) makes it fall back to the SDK's sequential path,
    exactly as the SDK's own pool does."""
    from pageindex.flash import parser_pdfium_parallel as par

    if isinstance(doc_handle, (str, Path)):
        kind, payload = "path", str(doc_handle)
    elif isinstance(doc_handle, io.BytesIO):
        kind, payload = "bytes", doc_handle.getvalue()
    else:
        return original(doc_handle, workers=1)
    results = None
    try:
        par._init_worker(kind, payload)
        results = [par._run_page(i) for i in range(len(par._worker_pdf))]
    except par._Type3Detected:
        log.info("lean parser: Type-3 fonts need document-wide sizing; using the SDK's sequential path for this PDF")
    finally:
        try:
            if par._worker_pdf is not None:
                par._worker_pdf.close()
        except Exception:  # noqa: BLE001
            pass
        par._worker_pdf = par._worker_pdf_doc = None
        par._worker_font_maps = {}
    if results is None:
        return original(doc_handle, workers=1)
    return [spans for spans, _ in results], [meta for _, meta in results]


def _patch_lean_parser() -> bool:
    """Route `workers=1` through `_lean_parse`.  Guarded: if the SDK's seams moved, the SDK's own parser stays in charge."""
    global _patched_parser
    try:
        from pageindex.flash import main as flash_main
        from pageindex.flash import parser_pdfium_parallel as par
    except ImportError as exc:
        log.warning("PageIndex Flash is not importable (%s); lean parser patch skipped", exc)
        return False
    original = getattr(flash_main, "parse_charlevel_meta_parallel", None)
    if original is None or not all(hasattr(par, n) for n in ("_init_worker", "_run_page", "_Type3Detected", "_worker_pdf")):
        log.warning("PageIndex %s has no per-page parser seams; workers=1 uses the SDK's sequential parser (needs much more memory)",
                    pageindex_version())
        return False
    if getattr(original, "_reportlens_lean", False):
        _patched_parser = True
        return True

    def parse(doc_handle: Any, workers: Optional[int] = None, *args: Any, **kwargs: Any) -> Any:
        if workers == 1 and not args and not kwargs:
            return _lean_parse(doc_handle, original)
        return original(doc_handle, workers, *args, **kwargs)

    parse._reportlens_lean = True  # type: ignore[attr-defined]
    parse.__wrapped__ = original  # type: ignore[attr-defined]
    _original_locked[(flash_main, "parse_charlevel_meta_parallel")] = original
    flash_main.parse_charlevel_meta_parallel = parse
    _patched_parser = True
    return True


def _patch_pdfium_lock() -> list[str]:
    """The Flash layout parser opens PDFium documents in this process (documents < 64 pages; larger ones use a process pool
    but still touch PDFium here).  PDFium crashes under concurrent use, so the web threads (viewer, locator) and the indexing
    thread must take turns: wrap the SDK's two in-process PDFium entry points in the shared lock."""
    try:
        from pageindex.flash import api as flash_api
    except ImportError as exc:
        log.warning("PageIndex Flash is not importable (%s); PDFium lock patch skipped", exc)
        return []
    applied = []
    for name in ("extract_toc", "_validate_pdf"):
        fn = getattr(flash_api, name, None)
        if fn is None:
            log.warning("pageindex.flash.api.%s not found; PDFium may be used concurrently while indexing", name)
        elif getattr(fn, "__wrapped__", None) is None:
            _original_locked[(flash_api, name)] = fn
            setattr(flash_api, name, _locked(fn, fill_workers=name == "extract_toc"))
            applied.append(name)
        else:
            applied.append(name)
    return applied


def _disable_agents_tracing() -> None:
    """No traces to anyone: the Agents SDK exports them to OpenAI's tracing endpoint by default (PageIndex's own agent run
    already passes tracing_disabled, but our own agent code might not).  The environment flag is read when the SDK first
    traces; a trace provider that already exists is switched off directly.  Never imports the SDK itself, and never creates
    its provider (that would start a background exporter thread)."""
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    setup = sys.modules.get("agents.tracing.setup")
    provider = getattr(setup, "GLOBAL_TRACE_PROVIDER", None)
    if provider is not None:
        provider.set_disabled(True)


def apply_patches(full: bool = True) -> None:
    """Idempotent and thread-safe.  Never raises: a patch that cannot be applied is logged loudly and skipped.

    `full=False` (the web process when indexing runs in a child process) only switches agent tracing off: the SDK's indexing
    half (Flash parser, page-text patch; ~30 MB) is never imported there, the child applies the full set."""
    global _patched_locks
    if not full:
        _disable_agents_tracing()
        return
    with _PATCH_LOCK:
        version = pageindex_version()
        first = not (_patched_text or _patched_locks)
        if version is None:
            log.warning("PageIndex SDK is not installed; indexing and chat will not work")
        elif version != PAGEINDEX_TESTED_VERSION and first:
            log.warning("PageIndex %s is installed but ReportLens was tested with %s: patches are applied only where the SDK "
                        "still has the expected seams; re-run the test-suite before trusting indexing or answers",
                        version, PAGEINDEX_TESTED_VERSION)
        text_ok = _patch_page_text() if version else False
        _patched_locks = _patch_pdfium_lock() if version else []
        if version:
            _patch_lean_parser()
        _disable_agents_tracing()
        if first:
            log.info("PageIndex %s: pdfium page text %s, PDFium lock on %s, agent tracing off", version,
                     "ON" if text_ok else "OFF (degraded)", ",".join(_patched_locks) or "nothing (degraded)")


def is_patched() -> bool:
    """True when the page-text override (the one that matters for answer quality) is active."""
    return _patched_text


def remove_patches() -> None:
    """Undo `apply_patches` (tests, and an orderly shutdown of an embedding application)."""
    global _patched_text, _patched_locks, _patched_parser
    with _PATCH_LOCK:
        if _patched_text and _original_extract is not None:
            from pageindex.local_api import LocalAPI
            LocalAPI._extract_page_texts = _original_extract  # type: ignore[method-assign]
        for (module, name), fn in _original_locked.items():
            setattr(module, name, fn)
        _original_locked.clear()
        _patched_text, _patched_locks, _patched_parser = False, [], False


# ------------------------------------------------------------------------------------------------ environment
_ENV_LOCK = threading.Lock()
_env_applied: Optional[tuple[Optional[str], Optional[str]]] = None   # (key, base_url) last pushed into os.environ
_env_original: dict[str, Optional[str]] = {}                         # values before we first touched each variable


def _set_env(name: str, value: Optional[str]) -> None:
    """Set (or, for None, restore the pre-ReportLens value of) one variable."""
    _env_original.setdefault(name, os.environ.get(name))
    if value is not None:
        os.environ[name] = value
        return
    original = _env_original[name]
    if original is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = original


def configure_openai_env(settings: Settings) -> None:
    """Point the SDK stack (litellm for indexing, the OpenAI client for the Responses chat lane) at the configured key and
    base URL.  Idempotent: the environment is only touched when (key, base_url) differ from what was last applied, under a
    lock, and a value that settings no longer carries goes back to what the process started with.  The key itself is also
    handed to each client explicitly (`make_client`), so a client never depends on whatever another session left behind."""
    global _env_applied
    wanted = (settings.openai_api_key, settings.openai_base_url)
    with _ENV_LOCK:
        if _env_applied == wanted:
            return
        _set_env("OPENAI_API_KEY", wanted[0] if wanted[0] else None)
        _set_env("OPENAI_BASE_URL", wanted[1] if wanted[1] else None)
        _env_applied = wanted
    log.info("OpenAI environment configured (key %s, base URL %s)", "set" if wanted[0] else "not set",
             _redact_url(wanted[1]) or "default")


def restore_openai_env() -> None:
    """Put OPENAI_API_KEY / OPENAI_BASE_URL back to their values from before `configure_openai_env` ran."""
    global _env_applied
    with _ENV_LOCK:
        for name in list(_env_original):
            _set_env(name, None)
        _env_original.clear()
        _env_applied = None


def _redact_url(url: Optional[str]) -> Optional[str]:
    """The URL without credentials / query (it may be a corporate proxy with a token in it)."""
    if not url:
        return None
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


# ------------------------------------------------------------------------------------------------ client
def _backend(settings: Settings) -> Optional[dict[str, str]]:
    """Per-client connection overrides (litellm kwargs; the OpenAI client takes `api_base` as `base_url`)."""
    backend: dict[str, str] = {}
    if settings.openai_api_key:
        backend["api_key"] = settings.openai_api_key
    if settings.openai_base_url:
        backend["api_base"] = settings.openai_base_url
    return backend or None


def storage_path(settings: Settings, session_id: str) -> Path:
    """The session's PageIndex store (D3): one store per session, so deleting a session is one rmtree."""
    return settings.session_dir(session_id) / "pageindex"


def make_client(settings: Settings, session_id: str, *, for_indexing: bool = False, mode: str = "flash") -> "PageIndexClient":
    """A `PageIndexClient` bound to this session's store.

    for_indexing: an indexing client carries no chat model (nothing will ask it questions) and honours
    `settings.index_summary_concurrency`.  mode="standard" builds the client for `submit_document(mode="standard")`, which
    the SDK refuses to combine with the flash-only summary-concurrency knob."""
    from pageindex import PageIndexClient

    apply_patches(full=for_indexing or not settings.index_in_subprocess)
    set_parse_workers(1 if settings.low_memory else None)
    if settings.low_memory:
        from reportlens.lowmem import disable_litellm_preload

        disable_litellm_preload()
    configure_openai_env(settings)
    store = storage_path(settings, session_id)
    store.mkdir(parents=True, exist_ok=True)
    backend = _backend(settings)
    # The dict form is mandatory: index="<model>" cannot be combined with storage_path.
    index: dict[str, Any] = {"model": settings.index_model, "storage_path": str(store), "backend": backend}
    if for_indexing and mode == "flash":
        index["summary_concurrency"] = max(1, settings.index_summary_concurrency)
    chat = None if for_indexing else {"model": settings.chat_model, "backend": backend}
    return PageIndexClient(index=index, chat=chat)


# ------------------------------------------------------------------------------------------------ health
def check_environment(settings: Settings) -> dict[str, Any]:
    """Versions and wiring, for GET /api/health.  Reads package metadata only (imports nothing heavy), never the key."""
    version = pageindex_version()
    return {
        "pageindex_version": version,
        "pageindex_tested_version": PAGEINDEX_TESTED_VERSION,
        "pageindex_supported": version == PAGEINDEX_TESTED_VERSION,
        "litellm_version": _version("litellm"),
        "openai_agents_version": _version("openai-agents"),
        "openai_version": _version("openai"),
        "ragas_version": _version("ragas"),
        "openai_key": bool(settings.openai_api_key),
        "base_url": _redact_url(settings.openai_base_url),
        "patched": _patched_text,
        "pdfium_lock": list(_patched_locks),
        "lean_parser": _patched_parser,
    }
