"""REST + SSE routes (docs/ARCHITECTURE.md sections 5 and 6).

The routes are thin: validate input at the HTTP boundary, call `ReportLensService`, serialise. The service is
framework-free and mostly synchronous, so its blocking methods are exposed through plain `def` routes (FastAPI runs those
in a thread pool and the event loop never waits on SQLite, pdfium or file copies).
"""
from __future__ import annotations

import asyncio
import functools
import logging
import re
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Optional, Protocol
from urllib.parse import quote as url_quote
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect

from reportlens import __version__
from reportlens.config import Settings
from reportlens.models import (
    DocumentPages,
    EvalScores,
    LocateResponse,
    Message,
    ServiceError,
    Session,
    SessionDetail,
)
from reportlens.web.auth import client_ip
from reportlens.web.sse import EventStreamResponse

log = logging.getLogger("reportlens.web")

router = APIRouter()

CHUNK_BYTES = 1024 * 1024            # upload is written to disk in 1 MB pieces
PDF_MAGIC = b"%PDF-"
PDF_MAGIC_WINDOW = 1024              # the spec allows junk before the header, but only within the first KB
MULTIPART_OVERHEAD = 64 * 1024       # boundaries, part headers and small form fields around the file bytes
MAX_FIELD_BYTES = 64 * 1024          # cap for everything that is not the file
MAX_FILENAME_CHARS = 150

_ID = re.compile(r"[0-9a-f]{32}")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_FILENAME_FORBIDDEN = re.compile(r'[<>:"|?*]')


class RateLimited(ServiceError):
    """HTTP 429 with a Retry-After header (the app's ServiceError handler adds it)."""

    def __init__(self, code: str, message: str, retry_after: int):
        super().__init__(code, message, 429)
        self.retry_after = retry_after


def _wait_text(seconds: int) -> str:
    minutes = -(-seconds // 60)
    return "under a minute" if seconds < 60 else f"{minutes} minute{'s' if minutes != 1 else ''}"


class ServiceAPI(Protocol):
    """What the routes need from `reportlens.service.ReportLensService` (contract: docs/ARCHITECTURE.md 4.8)."""

    def create_session(self, from_session: Optional[str] = None) -> Session: ...
    def list_sessions(self) -> list[Session]: ...
    def get_session(self, sid: str) -> SessionDetail: ...
    def rename_session(self, sid: str, title: str) -> Session: ...
    def delete_session(self, sid: str) -> None: ...
    def attach_document(self, sid: str, filename: str, tmp_path: Path) -> Session: ...
    def document_path(self, sid: str) -> Path: ...
    def document_pages(self, sid: str) -> DocumentPages: ...
    def document_outline(self, sid: str) -> list[dict]: ...
    def locate(self, sid: str, page: int, quote: Optional[str], claim: Optional[str]) -> LocateResponse: ...
    def ask(self, sid: str, content: str) -> AsyncIterator[tuple[str, dict]]: ...
    def get_message(self, sid: str, mid: str) -> Message: ...
    async def evaluate_message(self, sid: str, mid: str) -> EvalScores: ...
    def health(self) -> dict: ...


# --------------------------------------------------------------------------------------------- dependencies
def get_service(request: Request) -> ServiceAPI:
    service = request.app.state.service
    if service is None:
        raise ServiceError("service_unavailable", "The server is still starting up. Try again in a moment.", 503)
    return service


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def valid_sid(sid: str) -> str:
    """Session ids are 32 hex chars. Anything else cannot exist, so it is a 404 and never reaches the service
    (no path is ever built from user input)."""
    if not _ID.fullmatch(sid):
        raise ServiceError("session_not_found", "That chat does not exist.", 404)
    return sid


def valid_mid(mid: str) -> str:
    if not _ID.fullmatch(mid):
        raise ServiceError("message_not_found", "That message does not exist.", 404)
    return mid


Service = Annotated[ServiceAPI, Depends(get_service)]
AppSettings = Annotated[Settings, Depends(get_settings)]
SessionId = Annotated[str, Depends(valid_sid)]
MessageId = Annotated[str, Depends(valid_mid)]


# --------------------------------------------------------------------------------------------- request bodies
class CreateSessionBody(BaseModel):
    from_session: Optional[str] = None


class RenameBody(BaseModel):
    title: str = Field(max_length=500)


class LoginBody(BaseModel):
    code: str = Field(max_length=1000)


class AskBody(BaseModel):
    content: str = Field(max_length=100_000)     # the service enforces the real limit (4000 chars) with a friendly message


# --------------------------------------------------------------------------------------------- meta
@router.get("/api/health")
def health(request: Request, service: Service) -> dict:
    """Cheap and open (no OpenAI call).  Behind the access gate, a caller without the cookie only learns that the server is up."""
    gate = request.app.state.gate
    if not gate.valid_for_scope(request.scope):
        return {"ok": True, "version": __version__}
    settings: Settings = request.app.state.settings
    defaults = {"ok": True, "version": __version__, "openai_configured": settings.openai_configured,
                "demo_mock": settings.demo_mock}
    return {**defaults, **service.health()}


# --------------------------------------------------------------------------------------------- access gate
@router.get("/api/auth")
def auth_status(request: Request) -> dict:
    gate = request.app.state.gate
    return {"required": gate.required, "authenticated": gate.valid_for_scope(request.scope)}


@router.post("/api/login")
def login(body: LoginBody, request: Request, settings: AppSettings) -> JSONResponse:
    gate = request.app.state.gate
    if not gate.required:
        return JSONResponse({"required": False, "authenticated": True})
    limiter = request.app.state.login_limiter
    ip = client_ip(request.scope, settings)
    wait = limiter.retry_after(ip)
    if wait:
        raise RateLimited("too_many_attempts", f"Too many wrong access codes. Try again in {_wait_text(wait)}.", wait)
    if not gate.code_matches(body.code):
        limiter.record(ip)
        log.warning("Wrong access code from %s", ip)
        raise ServiceError("invalid_code", "That access code is not correct.", 401)
    limiter.reset(ip)
    response = JSONResponse({"required": True, "authenticated": True})
    response.headers.append("set-cookie", gate.set_cookie_header(gate.issue(), secure=gate.is_https(request.scope)))
    return response


@router.post("/api/logout")
def logout(request: Request) -> JSONResponse:
    gate = request.app.state.gate
    response = JSONResponse({"required": gate.required, "authenticated": not gate.required})
    response.headers.append("set-cookie", gate.set_cookie_header("", secure=gate.is_https(request.scope), max_age=0))
    return response


@functools.cache
def _metric_info() -> dict:
    from reportlens.metric_info import METRIC_INFO  # not reportlens.evaluation: that imports ragas (seconds)

    return METRIC_INFO


@router.get("/api/config")
async def config(request: Request, settings: AppSettings) -> dict:
    status = getattr(request.app.state.service, "usage_status", None)
    usage = await asyncio.to_thread(status) if callable(status) else {"enabled": False, "used_fraction": 0.0}
    return {**settings.public(), "usage_budget": usage, "metrics": await asyncio.to_thread(_metric_info)}


# --------------------------------------------------------------------------------------------- sessions
@router.get("/api/sessions")
def list_sessions(service: Service) -> list[Session]:
    return service.list_sessions()


@router.post("/api/sessions", status_code=201)
def create_session(service: Service, body: Optional[CreateSessionBody] = None) -> Session:
    source = body.from_session if body else None
    if source is not None:
        valid_sid(source)
    return service.create_session(source)


@router.get("/api/sessions/{sid}")
def get_session(sid: SessionId, service: Service) -> SessionDetail:
    return service.get_session(sid)


@router.patch("/api/sessions/{sid}")
def rename_session(sid: SessionId, body: RenameBody, service: Service) -> Session:
    title = body.title.strip()
    if not title:
        raise ServiceError("empty_title", "The chat title cannot be empty.", 400)
    return service.rename_session(sid, title)


@router.delete("/api/sessions/{sid}", status_code=204)
def delete_session(sid: SessionId, service: Service) -> Response:
    service.delete_session(sid)
    return Response(status_code=204)


# --------------------------------------------------------------------------------------------- document
def safe_filename(raw: Optional[str]) -> str:
    """Display name for an upload. The client's name never becomes a path: the service stores the PDF under its own
    ASCII name, but we still strip directories, control characters and characters Windows forbids."""
    name = _FILENAME_FORBIDDEN.sub("_", _CONTROL_CHARS.sub("", (raw or "").replace("\\", "/")).rsplit("/", 1)[-1])
    stem = (name[:-4] if name.lower().endswith(".pdf") else name).strip(" .")
    return f"{stem[: MAX_FILENAME_CHARS - 4].rstrip(' .') or 'report'}.pdf"


def _too_large(settings: Settings) -> ServiceError:
    return ServiceError("file_too_large", f"That file is larger than the {settings.max_upload_mb} MB limit.", 413)


def _invalid_pdf() -> ServiceError:
    return ServiceError("invalid_pdf", "That file is not a valid PDF.", 400)


class _UploadReader:
    """Incremental multipart reader that never holds more than ~one chunk in memory and aborts as soon as a limit is
    crossed. Starlette's own form parser spools the whole body to disk before the route runs and has no cap for file
    parts, so it cannot enforce MAX_UPLOAD_MB; this one can."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.pending: list[bytes] = []            # file bytes not yet written to disk
        self.pending_bytes = 0
        self.file_bytes = 0
        self.other_bytes = 0
        self.filename: Optional[str] = None
        self.file_done = False
        self.head = bytearray()                   # first KB of the file, for the magic-number check
        self.head_checked = False
        self._headers: dict[bytes, bytes] = {}
        self._name = b""
        self._value = b""
        self._is_file = False

    # parser callbacks (python-multipart calls these synchronously from `write`)
    def on_part_begin(self) -> None:
        self._headers, self._name, self._value, self._is_file = {}, b"", b"", False

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def on_header_end(self) -> None:
        self._headers[self._name.lower()] = self._value
        self._name = self._value = b""

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        self._is_file = options.get(b"name") == b"file" and b"filename" in options and self.filename is None
        if self._is_file:
            self.filename = safe_filename(options[b"filename"].decode("utf-8", errors="replace"))

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if not self._is_file:
            self.other_bytes += len(chunk)
            if self.other_bytes > MAX_FIELD_BYTES:
                raise ServiceError("invalid_request", "Send exactly one PDF, in a form field named 'file'.", 400)
            return
        self.file_bytes += len(chunk)
        if self.file_bytes > self.settings.max_upload_bytes:
            raise _too_large(self.settings)
        if not self.head_checked:
            self.head += chunk[: PDF_MAGIC_WINDOW - len(self.head)]
            if len(self.head) >= PDF_MAGIC_WINDOW:
                self.check_head()
        self.pending.append(chunk)
        self.pending_bytes += len(chunk)

    def on_part_end(self) -> None:
        if self._is_file:
            self.file_done = True
            self.check_head()

    def check_head(self) -> None:
        self.head_checked = True
        if PDF_MAGIC not in self.head:
            raise _invalid_pdf()

    def take(self) -> bytes:
        data, self.pending, self.pending_bytes = b"".join(self.pending), [], 0
        return data


def _open_temp(tmp_dir: Path) -> tuple[Any, Path]:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"upload-{uuid4().hex}.pdf"
    return path.open("wb"), path


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("Could not delete temporary upload %s", path.name)


async def _receive_upload(request: Request, settings: Settings, out: Any) -> str:
    """Streams the multipart body into `out` (a binary file). Returns the sanitised filename; raises ServiceError."""
    media_type, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if media_type != b"multipart/form-data" or not boundary:
        raise ServiceError("invalid_request", "Upload the PDF as multipart/form-data with a 'file' field.", 400)
    declared = request.headers.get("content-length", "")
    limit = settings.max_upload_bytes + MULTIPART_OVERHEAD
    if declared.isdigit() and int(declared) > limit:
        raise _too_large(settings)            # reject before reading a single byte of the body

    reader = _UploadReader(settings)
    parser = MultipartParser(boundary, {
        "on_part_begin": reader.on_part_begin, "on_header_field": reader.on_header_field,
        "on_header_value": reader.on_header_value, "on_header_end": reader.on_header_end,
        "on_headers_finished": reader.on_headers_finished, "on_part_data": reader.on_part_data,
        "on_part_end": reader.on_part_end,
    })
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                raise _too_large(settings)
            parser.write(chunk)
            if reader.pending_bytes >= CHUNK_BYTES:
                await asyncio.to_thread(out.write, reader.take())
        parser.finalize()
    except MultipartParseError:
        raise ServiceError("invalid_request", "The upload could not be read. Try again.", 400) from None
    if reader.pending_bytes:
        await asyncio.to_thread(out.write, reader.take())
    if reader.filename is None or not reader.file_done:
        raise ServiceError("missing_file", "No file was received. Choose a PDF to upload.", 400)
    return reader.filename


@router.post("/api/sessions/{sid}/document", status_code=202)
async def upload_document(sid: SessionId, request: Request, service: Service, settings: AppSettings) -> Session:
    await asyncio.to_thread(service.get_session, sid)       # 404 early, before any bytes hit the disk
    check_budget = getattr(service, "check_budget", None)
    if callable(check_budget):
        await asyncio.to_thread(check_budget)               # 402 before reading a body that could not be indexed anyway
    out, tmp_path = await asyncio.to_thread(_open_temp, settings.data_dir / "tmp")
    try:
        try:
            filename = await _receive_upload(request, settings, out)
        finally:
            await asyncio.to_thread(out.close)
        return await asyncio.to_thread(service.attach_document, sid, filename, tmp_path)
    except ClientDisconnect:
        log.info("Client went away during an upload to session %s", sid[:8])
        raise ServiceError("upload_cancelled", "The upload was cancelled.", 400) from None
    finally:
        await asyncio.to_thread(_discard, tmp_path)           # a no-op once the service has moved the file


def _content_disposition(name: str) -> str:
    """inline + both the ASCII fallback and the RFC 5987 `filename*` form, so non-ASCII names survive."""
    fallback = re.sub(r'[^\x20-\x7e]|["\\]', "_", name)
    return f"inline; filename=\"{fallback}\"; filename*=UTF-8''{url_quote(name, safe='')}"


@router.head("/api/sessions/{sid}/document/file", include_in_schema=False)
@router.get("/api/sessions/{sid}/document/file", response_class=FileResponse)
def document_file(sid: SessionId, service: Service) -> FileResponse:
    path = service.document_path(sid)
    if not path.is_file():
        raise ServiceError("document_not_found", "The document file could not be found on the server.", 404)
    # FileResponse gives us Range/206, ETag, Last-Modified and HEAD. `no-cache` because a failed session can be
    # re-uploaded under the same URL, and a stale PDF would highlight the wrong text.
    return FileResponse(path, media_type="application/pdf", headers={
        "Content-Disposition": _content_disposition(path.name), "Cache-Control": "private, no-cache"})


@router.get("/api/sessions/{sid}/document/pages")
def document_pages(sid: SessionId, service: Service) -> DocumentPages:
    return service.document_pages(sid)


@router.get("/api/sessions/{sid}/document/outline")
def document_outline(sid: SessionId, service: Service) -> dict:
    return {"nodes": service.document_outline(sid)}


@router.get("/api/sessions/{sid}/locate")
def locate(
    sid: SessionId,
    service: Service,
    page: Annotated[int, Query(ge=1, le=100_000)],
    quote: Annotated[Optional[str], Query(max_length=4000)] = None,
    claim: Annotated[Optional[str], Query(max_length=4000)] = None,
) -> LocateResponse:
    return service.locate(sid, page, quote or None, claim or None)


# --------------------------------------------------------------------------------------------- chat
def _take_question_slot(request: Request, settings: Settings) -> str:
    """Count one paid action against the client's hourly allowance (QUESTIONS_PER_HOUR_PER_IP; 0 = unlimited) or raise 429."""
    ip = client_ip(request.scope, settings)
    wait = request.app.state.question_limiter.hit(ip)
    if wait:
        raise RateLimited("rate_limited", f"You have reached the limit of {settings.questions_per_hour_per_ip} questions per hour. "
                                          f"Please try again in {_wait_text(wait)}.", wait)
    return ip


@router.post("/api/sessions/{sid}/messages")
async def ask(sid: SessionId, body: AskBody, request: Request, service: Service, settings: AppSettings) -> EventStreamResponse:
    if not body.content.strip():
        raise ServiceError("empty_question", "Type a question first.", 400)
    ip = _take_question_slot(request, settings)
    events = service.ask(sid, body.content)
    # Peek-first: the service validates (not ready, busy, no key, budget, ...) before it yields anything, and those errors must be
    # proper JSON responses, which is impossible once a 200 stream has started.
    try:
        first: Optional[tuple[str, Any]] = await events.__anext__()
    except StopAsyncIteration:
        first = None
    except ServiceError:
        request.app.state.question_limiter.refund(ip)         # refused before it cost anything: it does not use up the hour's allowance
        raise
    return EventStreamResponse(events, first=first)


@router.get("/api/sessions/{sid}/messages/{mid}")
def get_message(sid: SessionId, mid: MessageId, service: Service) -> Message:
    return service.get_message(sid, mid)


@router.post("/api/sessions/{sid}/messages/{mid}/evaluate")
async def evaluate_message(sid: SessionId, mid: MessageId, request: Request, service: Service, settings: AppSettings) -> EvalScores:
    ip = _take_question_slot(request, settings)               # scoring costs money too: it shares the per-IP allowance
    try:
        return await service.evaluate_message(sid, mid)
    except ServiceError:
        request.app.state.question_limiter.refund(ip)
        raise
