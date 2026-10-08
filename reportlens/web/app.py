"""FastAPI application factory.

`create_app(settings, service)` returns a ready ASGI app. With no `service`, the lifespan builds the real stack
(Store -> recover interrupted work -> PageIndex patches -> `ReportLensService`) and closes it on shutdown; an injected
service (tests, a host application) is used as-is and never closed here.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import traceback
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from reportlens import __version__
from reportlens.config import Settings, load_settings
from reportlens.limits import SlidingWindowLimiter
from reportlens.models import ServiceError
from reportlens.web.auth import LOGIN_MAX_FAILURES, LOGIN_WINDOW_S, AccessGate, AuthMiddleware
from reportlens.web.routes import ServiceAPI, router

log = logging.getLogger("reportlens.web")

_KEY_FRAGMENT_RE = re.compile(r"(?:sk-|gsk_|xai-|tgp_|AIza)[A-Za-z0-9_\-*.]{6,}")   # OpenAI/Anthropic/OpenRouter/DeepSeek, Groq, xAI, Together, Google


class KeyRedactionFilter(logging.Filter):
    """Keeps API-key fragments out of the log: whatever an SDK error message echoes ("Incorrect API key provided: sk-abc***")
    is replaced with "sk-...", in the message and in the traceback text."""

    @staticmethod
    def redact(text: str) -> str:
        return _KEY_FRAGMENT_RE.sub("sk-...", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            if _KEY_FRAGMENT_RE.search(message):
                record.msg, record.args = self.redact(message), None
            if record.exc_info and not record.exc_text:
                record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip(chr(10))
            if record.exc_text:
                record.exc_text = self.redact(record.exc_text)
            if record.stack_info:
                record.stack_info = self.redact(record.stack_info)
        except Exception:  # noqa: BLE001 - never lose a log line over redaction
            pass
        return True


def install_log_redaction() -> None:
    """Attach the redaction filter to the 'reportlens' logger and to every handler of the root logger (child loggers do not
    run their parent's logger-level filters, their records reach the handlers).  Idempotent."""
    targets: list[Any] = [logging.getLogger("reportlens"), *logging.getLogger().handlers]
    for target in targets:
        if not any(isinstance(f, KeyRedactionFilter) for f in target.filters):
            target.addFilter(KeyRedactionFilter())


STATIC_DIR = Path(__file__).resolve().parent / "static"

API_DESCRIPTION = """
Everything the web app does is available over this API: create a chat, upload one PDF, wait for it to be indexed, ask
questions (streamed as Server-Sent Events, or as one JSON answer), open the cited pages and read the RAGAS scores.

**Access.** When the server has an access code, `POST /api/login` with it once; the response sets a cookie that every
later request sends (use a cookie jar: `curl -c jar -b jar`, `httpx.Client()`, `requests.Session()`). Each cookie jar is
its own private visitor: it sees only the chats it created. The read-only demo chat (`GET /api/demo`) needs no code.

**Your own model.** Send `X-LLM-Config: <base64url of a JSON object>` with `provider`, `api_key` and `chat_model` (plus
optional `index_model`, `judge_model`, `embedding_model`, and `base_url` for self-hosted endpoints) on uploads, questions
and re-scoring. `GET /api/config` lists the providers this server accepts. The key is used for that request only and is
never stored or logged.

Step-by-step examples (curl and Python): `docs/API.md` in the repository.
"""

API_TAGS = [
    {"name": "Access", "description": "Access code, the visitor cookie, server configuration and health."},
    {"name": "Demo", "description": "The read-only demo chat (no access code needed)."},
    {"name": "Chats", "description": "Your chats (each holds exactly one document)."},
    {"name": "Documents", "description": "Upload the PDF, poll its indexing, read pages, outline and highlight rectangles."},
    {"name": "Questions", "description": "Ask (streamed or as one JSON answer), read answers, re-run the RAGAS scoring."},
    {"name": "Model provider", "description": "Your own provider and key (optional)."},
]

# pdf.js needs its worker (same origin, plus blob: for its fallback wrapper) and 'wasm-unsafe-eval' for the JPX/JBIG2
# decoders; styles allow inline because the UI sets style attributes (progress bars, highlight boxes).
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

NO_CACHE = "no-cache"                                  # cacheable, but always revalidated (ETag -> cheap 304)
VENDOR_CACHE = "public, max-age=2592000"              # 30 days: vendored libraries change only when we re-vendor them

# Content types are set explicitly: Python's `mimetypes` reads the Windows registry, where .js / .mjs are often
# text/plain, and browsers refuse ES modules (and wasm streaming) served with a wrong type.
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".wasm": "application/wasm",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".bcmap": "application/octet-stream",
    ".pfb": "application/octet-stream",
    ".icc": "application/octet-stream",
}

_ERROR_CODES = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large", 503: "service_unavailable"}


def error_response(code: str, message: str, status: int, headers: Optional[dict[str, str]] = None) -> JSONResponse:
    """The one error shape of the API: {"error": {"code", "message"}}."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


# --------------------------------------------------------------------------------------------- static files
class StaticAssets(StaticFiles):
    """StaticFiles with explicit MIME types and cache policy: revalidate html/js/css, cache vendored libs for long."""

    def file_response(self, full_path: Any, stat_result: Any, scope: Scope, status_code: int = 200) -> Any:
        response = super().file_response(full_path, stat_result, scope, status_code)
        path = Path(full_path)
        mime = MIME_TYPES.get(path.suffix.lower())
        if mime:
            response.headers["content-type"] = mime
        try:
            top = path.resolve().relative_to(STATIC_DIR).parts[0]
        except (ValueError, IndexError):
            top = ""
        response.headers["cache-control"] = VENDOR_CACHE if top == "vendor" else NO_CACHE
        return response


# --------------------------------------------------------------------------------------------- middleware
class RequestMiddleware:
    """Pure ASGI (BaseHTTPMiddleware breaks streaming responses and disconnect handling). For every HTTP request:
    adds the security headers, logs `METHOD path status duration` (never bodies or query strings) and turns an
    unexpected exception into a generic 500 JSON response whose traceback goes to the log only."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 0

        async def send_with_headers(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            log.exception("Unhandled error on %s %s", scope["method"], scope["path"])
            if status:                      # headers already sent: the only honest option is to abort the connection
                raise
            response = error_response("internal_error", "Something went wrong on the server. See the server log.", 500)
            await response(scope, receive, send_with_headers)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            level = logging.DEBUG if scope["path"].startswith("/static/") else logging.INFO
            log.log(level, "%s %s %s %.0f ms", scope["method"], scope["path"], status or "-", elapsed_ms)


class LocalGuardMiddleware:
    """Pure ASGI protection against DNS rebinding and cross-site requests.

    * Host validation: a request whose Host header is not allowed gets 400 `invalid_host`.  `allowed_hosts` holds exact names
      and leading-dot suffixes (".hf.space" matches every subdomain, and "hf.space" itself); "*" accepts any Host (a server
      bound to every interface cannot know its own names, so there only the Origin check runs).
      In a configured public deployment (`public=True`) static files and /api/health are answered whatever the Host is
      (a platform's health probe does not use the public name); everything else under /api still needs an allowed Host.
    * CSRF: a state-changing request (POST/PUT/PATCH/DELETE) that carries an Origin header must come from the page's own
      origin (Origin host:port == Host, or X-Forwarded-Host when `trust_proxy`), otherwise 403 `forbidden_origin`.
      Requests without Origin (curl, tests) pass."""

    UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
    DEFAULT_PORTS = {"http": ":80", "https": ":443"}

    def __init__(self, app: ASGIApp, allowed_hosts: Iterable[str], *, trust_proxy: bool = False, public: bool = False):
        self.app = app
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.suffixes = tuple(h for h in self.allowed_hosts if h.startswith(".") and len(h) > 1)
        self.trust_proxy = trust_proxy
        self.public = public

    @staticmethod
    def _hostname(netloc: str) -> str:
        """'localhost:8000' -> 'localhost', '[::1]:8000' -> '[::1]' (Starlette's TrustedHostMiddleware cannot do the latter)."""
        netloc = netloc.strip().lower()
        if netloc.startswith("["):
            return netloc[: netloc.find("]") + 1] if "]" in netloc else netloc
        return netloc.split(":", 1)[0]

    def host_allowed(self, host: str) -> bool:
        name = self._hostname(host)
        if "*" in self.allowed_hosts or name in self.allowed_hosts:
            return True
        return any(name.endswith(suffix) or name == suffix[1:] for suffix in self.suffixes)

    @classmethod
    def _origin_netloc(cls, origin: str) -> str:
        """'https://x.hf.space' -> 'x.hf.space' (a default port is dropped so it compares equal to a Host without one)."""
        try:
            parts = urlsplit(origin)
            netloc = parts.netloc.lower()
        except ValueError:
            return ""
        default = cls.DEFAULT_PORTS.get(parts.scheme.lower())
        return netloc[: -len(default)] if default and netloc.endswith(default) else netloc

    def _own_hosts(self, headers: Headers, host: str) -> set[str]:
        hosts = {host.strip().lower()}
        if self.trust_proxy:
            forwarded = headers.get("x-forwarded-host", "").split(",")[0].strip().lower()
            if forwarded:
                hosts.add(forwarded)
        return hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        path = scope["path"]
        host_checked = not self.public or (path.startswith("/api/") and path != "/api/health")
        if host_checked and not self.host_allowed(host):
            log.warning("Rejected a request for unexpected Host %.80r", host)
            await error_response("invalid_host", "This server does not answer requests addressed to that host name.", 400)(scope, receive, send)
            return
        origin = headers.get("origin")
        if origin is not None and scope["method"] in self.UNSAFE_METHODS:
            origin_netloc = self._origin_netloc(origin)
            if not origin_netloc or origin_netloc not in self._own_hosts(headers, host):
                log.warning("Rejected a cross-origin %s %s from Origin %.80r", scope["method"], path, origin)
                await error_response("forbidden_origin", "Cross-origin requests are not allowed.", 403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------------------------- error handlers
def _validation_message(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "The request is not valid."
    first = errors[0]
    if first.get("type") == "json_invalid":
        return "The request body is not valid JSON."
    where = ".".join(str(part) for part in first.get("loc", ()) if part not in ("body", "query", "path"))
    problem = str(first.get("msg", "invalid value")).rstrip(".")
    return f"Invalid {where}: {problem}." if where else f"The request is not valid: {problem}."


async def _on_service_error(_: Request, exc: ServiceError) -> JSONResponse:
    retry_after = getattr(exc, "retry_after", None)
    return error_response(exc.code, exc.message, exc.status, headers={"Retry-After": str(retry_after)} if retry_after else None)


async def _on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response("invalid_request", _validation_message(exc), 400)


async def _on_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    try:
        phrase = HTTPStatus(exc.status_code).phrase
    except ValueError:
        phrase = "Request failed"
    message = exc.detail if isinstance(exc.detail, str) and exc.detail != phrase else f"{phrase}."
    code = _ERROR_CODES.get(exc.status_code, "http_error")
    return error_response(code, message, exc.status_code, headers=dict(exc.headers or {}))


# --------------------------------------------------------------------------------------------- lifespan
class _Stack:
    """What the lifespan created and must tear down (in reverse order)."""

    def __init__(self) -> None:
        self.service: Any = None
        self.indexer: Any = None
        self.store: Any = None
        self.mock: Any = None
        self.demo: Any = None

    async def close(self) -> None:
        if self.service is not None:
            await self.service.aclose()
        if self.indexer is not None:                  # the service leaves the indexer and the store to its creator
            await asyncio.to_thread(self.indexer.shutdown)
        if self.store is not None:
            await asyncio.to_thread(self.store.close)
        if self.mock is not None:
            await asyncio.to_thread(self.mock.stop)


def _start_mock(settings: Settings) -> tuple[Any, Settings]:
    """Demo mode: an in-process fake OpenAI server on a free port; all traffic is pointed at it with a dummy key so
    nothing can reach the real API, whatever is in .env."""
    from devtools.mock_openai import start_mock_server

    mock = start_mock_server()
    log.warning("DEMO MODE: OpenAI traffic goes to the built-in mock at %s. Answers are canned extracts and scores are meaningless.",
                mock.base_url)
    return mock, settings.with_(openai_base_url=mock.base_url, openai_api_key="sk-demo-mock-not-a-real-key")


def _build_stack(settings: Settings, stack: _Stack) -> Settings:
    """Blocking (SQLite, SDK imports): runs in a worker thread. Registers each resource on `stack` as soon as it exists
    so a failure half-way still closes what was opened."""
    from reportlens import pageindex_compat
    from reportlens.indexer import IndexService
    from reportlens.service import ReportLensService
    from reportlens.store import Store

    if settings.demo_mock:
        stack.mock, settings = _start_mock(settings)
    stack.store = Store(settings.db_path)
    stack.store.recover_interrupted()       # nothing is running yet: rows stuck in 'indexing'/'streaming' are from a dead process
    pageindex_compat.apply_patches(full=not settings.index_in_subprocess)
    stack.indexer = IndexService(settings, stack.store)
    stack.service = ReportLensService(settings, store=stack.store, indexer=stack.indexer)
    from reportlens.demo import install_demo

    stack.demo = install_demo(settings, stack.store)       # None without a demo folder; never raises
    _warm_up(settings)
    return settings


def _warm_up(settings: Settings) -> None:
    """Import the slow scoring libraries (RAGAS, ~5 s) on a background thread right after startup, so the first answer's scoring
    does not pay for it while the visitor waits.  Never blocks startup and never fails it."""
    if not settings.eval_enabled:
        return
    if settings.low_memory:
        log.info("LOW_MEMORY: the scoring libraries load on the first evaluation, not at start-up (saves ~100 MB while idle)")
        return

    def work() -> None:
        try:
            import reportlens.evaluation  # noqa: F401
        except Exception:  # noqa: BLE001 - scoring then reports its own import error
            log.warning("could not pre-load the scoring libraries", exc_info=True)

    threading.Thread(target=work, name="warm-up", daemon=True).start()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    stack = _Stack()
    if app.state.service is None:
        try:
            app.state.settings = await asyncio.to_thread(_build_stack, app.state.settings, stack)
        except BaseException:
            await stack.close()
            raise
        app.state.service = stack.service
        app.state.demo = getattr(stack, "demo", None)
    try:
        yield
    finally:
        if stack.service is not None:
            app.state.service = None            # requests that race the shutdown get a clean 503
        await stack.close()


# --------------------------------------------------------------------------------------------- factory
LOCAL_HOST_NAMES = ("localhost", "127.0.0.1", "[::1]", "testserver")     # "testserver" is Starlette's TestClient
BIND_ALL = ("0.0.0.0", "::", "")


def public_deployment(settings: Settings) -> bool:
    """True when the owner set up a public deployment (PUBLIC_MODE or ALLOWED_HOSTS): then bind-all no longer implies "any Host"."""
    return settings.public_mode or bool(settings.allowed_hosts)


def allowed_hosts(settings: Settings) -> list[str]:
    """Host header names the app answers to.
    * ALLOWED_HOSTS set: the loopback names (health checks, local use) + the configured names/suffixes, whatever the bind address.
    * PUBLIC_MODE without ALLOWED_HOSTS: any Host (the Origin check still runs; create_app logs a warning).
    * otherwise: the loopback names plus the configured host (anything when bound to all interfaces, as before)."""
    host = (settings.host or "").strip().lower()
    bound = [] if host in BIND_ALL else [f"[{host}]" if ":" in host and not host.startswith("[") else host]
    if settings.allowed_hosts:
        return [*LOCAL_HOST_NAMES, *settings.allowed_hosts, *bound]
    if settings.public_mode or host in BIND_ALL:
        return ["*"]
    return [*LOCAL_HOST_NAMES, *bound]


def _log_exposure(cfg: Settings) -> None:
    """One-time warnings about a public deployment that is not fully locked down."""
    if cfg.public_mode and not cfg.allowed_hosts:
        log.warning("PUBLIC_MODE is on but ALLOWED_HOSTS is empty: any Host header is accepted (the Origin check still applies). "
                    "Set ALLOWED_HOSTS, for example .hf.space or .onrender.com.")
    if cfg.public_mode and not cfg.access_code:
        log.warning("PUBLIC_MODE is on but ACCESS_CODE is empty: anyone who finds the URL can use the demo until the budget is used up.")
    if cfg.public_mode and cfg.budget_usd_total <= 0:
        log.warning("PUBLIC_MODE is on but BUDGET_USD_TOTAL is 0 (unlimited): nothing limits the spend except your OpenAI-side limit.")


def create_app(settings: Optional[Settings] = None, service: Optional[ServiceAPI] = None) -> FastAPI:
    app = FastAPI(
        title="Annual Report Lens",
        summary="Chat with an annual report: answers cited to the page, scored live with RAGAS.",
        description=API_DESCRIPTION,
        version=__version__,
        lifespan=_lifespan,
        docs_url=None,                      # FastAPI's own page loads Swagger UI from a CDN, which the CSP forbids: see /docs below
        redoc_url=None,
        openapi_url="/api/openapi.json",
        openapi_tags=API_TAGS,
        license_info={"name": "MIT", "url": "https://opensource.org/license/mit"},
        contact={"name": "sagarutkarsh1", "url": "https://github.com/sagarutkarsh1"},
    )
    install_log_redaction()
    app.state.settings = settings or load_settings()
    app.state.service = service
    app.state.demo = None
    cfg: Settings = app.state.settings
    app.state.gate = AccessGate(cfg)
    app.state.login_limiter = SlidingWindowLimiter(LOGIN_MAX_FAILURES, LOGIN_WINDOW_S)
    app.state.question_limiter = SlidingWindowLimiter(cfg.questions_per_hour_per_ip, 3600.0)
    app.state.check_limiter = SlidingWindowLimiter(20, 3600.0)          # "Test connection" of a visitor's own key
    _log_exposure(cfg)

    app.add_exception_handler(ServiceError, _on_service_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(StarletteHTTPException, _on_http_error)
    from reportlens.demo import DEMO_SESSION_ID

    app.add_middleware(AuthMiddleware, gate=app.state.gate, private_chats=cfg.private_chats,          # innermost: runs after the
                       demo_session_id=DEMO_SESSION_ID if cfg.demo_dir else None)                     # host / origin checks
    app.add_middleware(LocalGuardMiddleware, allowed_hosts=allowed_hosts(cfg), trust_proxy=cfg.trust_proxy, public=public_deployment(cfg))
    app.add_middleware(RequestMiddleware)               # added last = outermost: logs and adds headers to the guard's answers too
    app.include_router(router)

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type=MIME_TYPES[".html"], headers={"Cache-Control": NO_CACHE})

    @app.api_route("/docs", methods=["GET", "HEAD"], include_in_schema=False)
    def docs() -> FileResponse:
        """Swagger UI on /api/openapi.json, served from the vendored copy (no CDN)."""
        return FileResponse(STATIC_DIR / "docs.html", media_type=MIME_TYPES[".html"], headers={"Cache-Control": NO_CACHE})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)             # index.html carries an inline icon; stop the 404 noise

    app.mount("/static", StaticAssets(directory=STATIC_DIR), name="static")
    return app
