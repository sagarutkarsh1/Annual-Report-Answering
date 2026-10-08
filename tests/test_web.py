"""Offline tests for reportlens.web (routes, SSE, static files, security headers) against an in-process fake service.

The fake follows docs/ARCHITECTURE.md 4.8; the real stack is exercised in tests/test_e2e_mock.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
import pytest

from reportlens import __version__
from reportlens.config import Settings
from reportlens.models import (
    Citation,
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
)
from reportlens.web import app as app_module
from reportlens.web import sse
from reportlens.web.app import CONTENT_SECURITY_POLICY, STATIC_DIR, create_app
from reportlens.web.routes import safe_filename

PDF_BYTES = b"%PDF-1.4\n" + b"x" * 5000 + b"\n%%EOF\n"
SSE_FRAME = re.compile(r"event: (?P<name>[\w.-]+)\ndata: (?P<data>.*)\n\n")


def new_id() -> str:
    return uuid.uuid4().hex


def parse_sse(text: str) -> list[tuple[str, dict]]:
    return [(m["name"], json.loads(m["data"])) for m in SSE_FRAME.finditer(text)]


# --------------------------------------------------------------------------------------------- fake service
class FakeService:
    """Just enough ReportLensService behaviour to exercise every route and error path."""

    def __init__(self, workdir: Path, pdf: Path):
        self.workdir = workdir
        self.pdf = pdf
        self.sessions: dict[str, SessionDetail] = {}
        self.calls: list[tuple] = []
        self.ask_script: list[tuple[str, object]] = []
        self.ask_delay = 0.0
        self.ask_raises: Optional[BaseException] = None        # raised mid-stream, after the first event
        self.openai_configured = True
        self.busy = False
        self.generators_closed = 0

    # ---- helpers
    def _get(self, sid: str) -> SessionDetail:
        if sid not in self.sessions:
            raise ServiceError("session_not_found", "That chat does not exist.", 404)
        return self.sessions[sid]

    def add_session(self, state: str = "ready") -> SessionDetail:
        sid = new_id()
        doc = None
        if state != "empty":
            dest = self.workdir / f"{sid}.pdf"
            shutil.copyfile(self.pdf, dest)
            doc = DocumentInfo(id=new_id(), filename="Annual Report – 2026.pdf", doc_name=dest.name, size_bytes=dest.stat().st_size,
                               page_count=60, status="ready", stage="ready", progress=1.0)
        s = SessionDetail(id=sid, title="New chat", state=state, created_at="2026-10-07T10:00:00Z",
                          updated_at="2026-10-07T10:00:00Z", document=doc)  # type: ignore[arg-type]
        self.sessions[sid] = s
        return s

    # ---- contract
    def create_session(self, from_session: Optional[str] = None) -> Session:
        self.calls.append(("create_session", from_session))
        if from_session:
            if self._get(from_session).state not in ("ready", "locked"):
                raise ServiceError("document_not_ready", "The document is not ready.", 409)
            return self.add_session("ready")
        return self.add_session("empty")

    def list_sessions(self) -> list[Session]:
        return [Session(**s.model_dump()) for s in self.sessions.values()]

    def get_session(self, sid: str) -> SessionDetail:
        return self._get(sid)

    def rename_session(self, sid: str, title: str) -> Session:
        self.calls.append(("rename_session", sid, title))
        self._get(sid).title = title
        return Session(**self._get(sid).model_dump(exclude={"messages"}))

    def delete_session(self, sid: str) -> None:
        self._get(sid)
        del self.sessions[sid]

    def attach_document(self, sid: str, filename: str, tmp_path: Path) -> Session:
        self.calls.append(("attach_document", sid, filename, tmp_path))
        session = self._get(sid)
        assert tmp_path.is_file()
        if filename.startswith("scanned"):
            raise ServiceError("scanned_pdf", "This PDF looks scanned.", 422)
        if session.state not in ("empty", "failed"):
            raise ServiceError("document_already_uploaded", "This chat already has a document.", 409)
        dest = self.workdir / f"{sid}.pdf"
        shutil.move(str(tmp_path), dest)                       # like the real service: the temp file is consumed
        session.document = DocumentInfo(id=new_id(), filename=filename, doc_name=dest.name, size_bytes=dest.stat().st_size)
        session.state = "indexing"
        return Session(**session.model_dump(exclude={"messages"}))

    def document_path(self, sid: str) -> Path:
        document = self._get(sid).document
        if document is None:
            raise ServiceError("document_not_found", "This chat has no document yet.", 404)
        return self.workdir / document.doc_name

    def document_pages(self, sid: str) -> DocumentPages:
        if self._get(sid).state not in ("ready", "locked"):
            raise ServiceError("document_not_ready", "The document is still being indexed.", 409)
        return DocumentPages(page_count=2, pages=[PageInfo(width=595.0, height=842.0, printed_page="i"), PageInfo(width=595.0, height=842.0)])

    def document_outline(self, sid: str) -> list[dict]:
        self.document_pages(sid)
        return [{"title": "Strategic report", "node_id": "0001", "start_index": 1, "end_index": 2, "nodes": []}]

    def locate(self, sid: str, page: int, quote: Optional[str], claim: Optional[str]) -> LocateResponse:
        self.calls.append(("locate", sid, page, quote, claim))
        self._get(sid)
        return LocateResponse(page=page, hinted_page=page, method="exact", score=1.0, rects=[Rect(x=0.1, y=0.2, w=0.3, h=0.04)],
                              matched_text=quote or "", page_width=595.0, page_height=842.0)

    async def ask(self, sid: str, content: str) -> AsyncIterator[tuple[str, dict]]:
        self.calls.append(("ask", sid, content))
        try:
            session = self._get(sid)
            if not content.strip():
                raise ServiceError("empty_question", "Type a question first.", 400)
            if session.state not in ("ready", "locked"):
                raise ServiceError("document_not_ready", "The document is still being indexed.", 409)
            if self.busy:
                raise ServiceError("session_busy", "The previous question is still being answered.", 409)
            if not self.openai_configured:
                raise ServiceError("openai_not_configured", "OpenAI API key is not configured.", 503)
            for name, payload in self.ask_script:
                if self.ask_delay:
                    await asyncio.sleep(self.ask_delay)
                yield name, payload
            if self.ask_raises is not None:
                raise self.ask_raises
        finally:
            self.generators_closed += 1

    def get_message(self, sid: str, mid: str) -> Message:
        self._get(sid)
        return Message(id=mid, session_id=sid, role="assistant", content="Revenue grew [[c1]].", status="answered")

    async def evaluate_message(self, sid: str, mid: str) -> EvalScores:
        self.calls.append(("evaluate_message", sid, mid))
        if self._get(sid).state != "locked":
            raise ServiceError("message_not_found", "That message does not exist.", 404)
        return EvalScores(status="done", faithfulness=1.0, answer_relevancy=0.8, context_precision=0.5)

    def health(self) -> dict:
        return {"pageindex_version": "0.2.21", "openai_configured": self.openai_configured}


def standard_script(sid: str) -> list[tuple[str, object]]:
    """A realistic event sequence (docs/ARCHITECTURE.md section 6), with pydantic models inside the payloads."""
    mid = new_id()
    user = Message(id=new_id(), session_id=sid, role="user", content="What was revenue?", created_at="2026-10-07T10:01:00Z")
    cite = Citation(id="c1", index=1, doc_name="report.pdf", page=15, cited_page=15, printed_page="13", quote="revenue “rose” £14,812m",
                    quote_source="model", match_method="exact", match_score=1.0, rects=[Rect(x=0.1, y=0.2, w=0.5, h=0.02)])
    final = Message(id=mid, session_id=sid, role="assistant", content="Revenue was £14,812m [[c1]].", status="answered", citations=[cite],
                    evaluation=EvalScores(status="pending"), created_at="2026-10-07T10:01:01Z")
    metrics = ["faithfulness", "answer_relevancy", "context_precision"]
    scores = EvalScores(status="done", faithfulness=1.0, answer_relevancy=0.9, context_precision=0.7)
    return [
        ("message_start", {"user_message": user, "message_id": mid, "created_at": "2026-10-07T10:01:01Z"}),
        ("step", {"message_id": mid, "step": {"id": "s1", "kind": "tool", "label": "Read pages 15", "status": "running"}}),
        ("step_done", {"message_id": mid, "step_id": "s1", "elapsed_ms": 12, "label": "Read pages 15", "pages": [15]}),
        ("token", {"message_id": mid, "text": "Revenue was £14,812m "}),
        ("token", {"message_id": mid, "text": "[[c1]]."}),
        ("citation", {"message_id": mid, "citation": cite}),
        ("answer_done", {"message": final}),
        ("eval_started", {"message_id": mid, "metrics": metrics, "n_contexts": 1}),
        ("eval_result", {"message_id": mid, "metric": "faithfulness", "value": 1.0, "error": None}),
        ("eval_done", {"message_id": mid, "evaluation": scores}),
        ("done", {}),
    ]


# --------------------------------------------------------------------------------------------- fixtures
@pytest.fixture
def service(tmp_path, sample_pdf) -> FakeService:
    workdir = tmp_path / "svc"
    workdir.mkdir()
    return FakeService(workdir, sample_pdf)


@pytest.fixture
def small_settings(settings: Settings) -> Settings:
    return settings.with_(max_upload_mb=1)


@pytest.fixture
def app(small_settings, service):
    return create_app(small_settings, service)


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def error_of(response: httpx.Response) -> dict:
    body = response.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}, body
    return body["error"]


def tmp_leftovers(settings: Settings) -> list[Path]:
    tmp = settings.data_dir / "tmp"
    return list(tmp.iterdir()) if tmp.exists() else []


# --------------------------------------------------------------------------------------------- meta
async def test_health_merges_service_and_settings(client):
    body = (await client.get("/api/health")).json()
    assert body == {"ok": True, "version": __version__, "pageindex_version": "0.2.21", "openai_configured": True, "demo_mock": False}


async def test_config_is_public_settings_plus_metrics(client, small_settings):
    body = (await client.get("/api/config")).json()
    assert {**small_settings.public()}.items() <= body.items()
    assert set(body["metrics"]) >= {"faithfulness", "answer_relevancy", "context_precision"}
    assert body["metrics"]["faithfulness"]["label"]
    assert "sk-" not in json.dumps(body) and "api_key" not in body


# --------------------------------------------------------------------------------------------- sessions
async def test_session_lifecycle(client, service):
    created = await client.post("/api/sessions", json={})
    assert created.status_code == 201
    sid = created.json()["id"]
    assert created.json()["state"] == "empty"

    assert [s["id"] for s in (await client.get("/api/sessions")).json()] == [sid]
    detail = await client.get(f"/api/sessions/{sid}")
    assert detail.status_code == 200 and detail.json()["messages"] == []

    renamed = await client.patch(f"/api/sessions/{sid}", json={"title": "  FY26 results  "})
    assert renamed.status_code == 200 and renamed.json()["title"] == "FY26 results"
    assert ("rename_session", sid, "FY26 results") in service.calls

    deleted = await client.delete(f"/api/sessions/{sid}")
    assert deleted.status_code == 204 and deleted.content == b""
    assert (await client.get(f"/api/sessions/{sid}")).status_code == 404


async def test_create_session_accepts_empty_body_and_from_session(client, service):
    assert (await client.post("/api/sessions")).status_code == 201                    # no body at all
    ready = service.add_session("ready")
    cloned = await client.post("/api/sessions", json={"from_session": ready.id})
    assert cloned.status_code == 201 and cloned.json()["state"] == "ready"
    assert ("create_session", ready.id) in service.calls


async def test_create_session_from_unknown_or_unready_session(client, service):
    assert error_of(await client.post("/api/sessions", json={"from_session": new_id()}))["code"] == "session_not_found"
    assert error_of(await client.post("/api/sessions", json={"from_session": "../../etc"}))["code"] == "session_not_found"
    indexing = service.add_session("indexing")
    response = await client.post("/api/sessions", json={"from_session": indexing.id})
    assert response.status_code == 409 and error_of(response)["code"] == "document_not_ready"


async def test_session_detail_never_leaks_internal_document_id(client, service):
    ready = service.add_session("ready")
    ready.document.pi_doc_id = "pi-secret"                        # type: ignore[union-attr]
    assert "pi-secret" not in (await client.get(f"/api/sessions/{ready.id}")).text
    assert "pi_doc_id" not in (await client.get("/api/sessions")).text


@pytest.mark.parametrize("bad", ["abc", "A" * 32, "0" * 31, "0" * 33, "..%2F..%2Fetc", "%2e%2e", "g" * 32, "0" * 32 + "%20"])
async def test_malformed_session_ids_are_404_and_never_reach_the_service(client, service, bad):
    for method, suffix in [("GET", ""), ("DELETE", ""), ("GET", "/document/file"), ("GET", "/document/pages"), ("GET", "/locate?page=1")]:
        response = await client.request(method, f"/api/sessions/{bad}{suffix}")
        assert response.status_code == 404, (method, suffix)
        assert error_of(response)["code"] in ("session_not_found", "not_found")
    assert (await client.post(f"/api/sessions/{bad}/messages", json={"content": "hi"})).status_code == 404
    assert (await client.post(f"/api/sessions/{bad}/document", files={"file": ("a.pdf", PDF_BYTES)})).status_code == 404
    assert service.calls == []


async def test_malformed_message_id_is_404(client, service):
    sid = service.add_session("locked").id
    for path in ("messages/xyz", "messages/xyz/evaluate"):
        response = await client.request("POST" if path.endswith("evaluate") else "GET", f"/api/sessions/{sid}/{path}")
        assert response.status_code == 404 and error_of(response)["code"] == "message_not_found"


async def test_rename_validation(client, service):
    sid = service.add_session("ready").id
    assert error_of(await client.patch(f"/api/sessions/{sid}", json={"title": "   "}))["code"] == "empty_title"
    missing = await client.patch(f"/api/sessions/{sid}", json={})
    assert missing.status_code == 400 and error_of(missing)["code"] == "invalid_request"
    broken = await client.patch(f"/api/sessions/{sid}", content=b"{nope", headers={"content-type": "application/json"})
    assert broken.status_code == 400 and error_of(broken)["message"] == "The request body is not valid JSON."
    assert (await client.patch(f"/api/sessions/{new_id()}", json={"title": "x"})).status_code == 404


# --------------------------------------------------------------------------------------------- upload
async def upload(client, sid, data=PDF_BYTES, name="Annual Report.pdf", **kwargs):
    return await client.post(f"/api/sessions/{sid}/document", files={"file": (name, data, "application/pdf")}, **kwargs)


async def test_upload_happy_path_streams_to_service_and_cleans_up(client, service, small_settings):
    sid = service.add_session("empty").id
    response = await upload(client, sid, name="National Grid – Annual Report 2025.pdf")
    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "indexing" and body["document"]["filename"] == "National Grid – Annual Report 2025.pdf"
    _, _, filename, tmp_path = next(c for c in service.calls if c[0] == "attach_document")
    assert tmp_path.parent == small_settings.data_dir / "tmp"
    assert (service.workdir / f"{sid}.pdf").read_bytes() == PDF_BYTES
    assert tmp_leftovers(small_settings) == []


async def test_upload_accepts_pdf_header_within_first_kilobyte(client, service):
    sid = service.add_session("empty").id
    assert (await upload(client, sid, data=b"\xef\xbb\xbfjunk\n" + PDF_BYTES)).status_code == 202


async def test_upload_larger_than_one_chunk_arrives_intact(client, service, small_settings):
    sid = service.add_session("empty").id
    big = b"%PDF-1.7\n" + bytes(range(256)) * 3000                         # ~750 KB, still under the 1 MB cap
    assert (await upload(client, sid, data=big)).status_code == 202
    assert (service.workdir / f"{sid}.pdf").read_bytes() == big


@pytest.mark.parametrize("raw,expected", [
    ("..\\..\\evil/../a b.pdf", "a b.pdf"), ("C:\\Users\\x\\r.pdf", "r.pdf"), ("noext", "noext.pdf"), ("", "report.pdf"),
    ("bad<>:\"|?*.PDF", "bad_______.pdf"), ("tab\t\x00name.pdf", "tabname.pdf"), ("   .pdf", "report.pdf"),
    ("é" * 400 + ".pdf", ("é" * 146) + ".pdf"),
])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected
    assert "/" not in safe_filename(raw) and "\\" not in safe_filename(raw)


async def test_upload_filename_is_sanitised_before_the_service_sees_it(client, service):
    sid = service.add_session("empty").id
    assert (await upload(client, sid, name="../../../Windows/evil.pdf")).status_code == 202
    assert [c[2] for c in service.calls if c[0] == "attach_document"] == ["evil.pdf"]


async def test_upload_too_large_is_rejected_before_reading_the_body(client, service, small_settings):
    sid = service.add_session("empty").id
    response = await upload(client, sid, data=b"%PDF-" + b"0" * (small_settings.max_upload_bytes + 10))
    assert response.status_code == 413 and error_of(response)["code"] == "file_too_large"
    assert "1 MB" in error_of(response)["message"]
    assert not [c for c in service.calls if c[0] == "attach_document"]
    assert tmp_leftovers(small_settings) == []


async def test_upload_too_large_without_content_length_is_stopped_while_streaming(client, service, small_settings):
    sid = service.add_session("empty").id
    boundary = "xBOUNDARYx"
    sent = 0

    async def body():
        nonlocal sent
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="big.pdf"\r\n'
                f"Content-Type: application/pdf\r\n\r\n%PDF-1.7\n").encode()
        yield head
        for _ in range(8):                                              # 8 x 512 KB = 4 MB offered, cap is 1 MB
            sent += 1
            yield b"0" * (512 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()

    response = await client.post(f"/api/sessions/{sid}/document", content=body(),
                                 headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    assert response.status_code == 413 and error_of(response)["code"] == "file_too_large"
    assert sent < 8, "the server must stop reading once the cap is crossed"
    assert tmp_leftovers(small_settings) == []


async def test_upload_exactly_at_the_limit_is_accepted(client, service, small_settings):
    sid = service.add_session("empty").id
    exact = b"%PDF-" + b"0" * (small_settings.max_upload_bytes - 5)
    assert (await upload(client, sid, data=exact)).status_code == 202


@pytest.mark.parametrize("data", [b"This is not a PDF at all", b"", b"PK\x03\x04" + b"0" * 4000, b"x" * 2000 + b"%PDF-1.4"])
async def test_upload_rejects_wrong_magic(client, service, small_settings, data):
    sid = service.add_session("empty").id
    response = await upload(client, sid, data=data, name="fake.pdf")
    assert response.status_code == 400 and error_of(response)["code"] == "invalid_pdf"
    assert not [c for c in service.calls if c[0] == "attach_document"]
    assert tmp_leftovers(small_settings) == []


async def test_upload_without_a_file_field(client, service, small_settings):
    sid = service.add_session("empty").id
    no_file = await client.post(f"/api/sessions/{sid}/document", data={"note": "hi"}, files={"other": ("x.pdf", PDF_BYTES)})
    assert no_file.status_code == 400 and error_of(no_file)["code"] in ("missing_file", "invalid_request")
    not_multipart = await client.post(f"/api/sessions/{sid}/document", json={"file": "x"})
    assert not_multipart.status_code == 400 and error_of(not_multipart)["code"] == "invalid_request"
    only_fields = await client.post(f"/api/sessions/{sid}/document", data={"a": "b"})
    assert only_fields.status_code == 400
    assert tmp_leftovers(small_settings) == []


async def test_upload_truncated_multipart_is_a_clean_400(client, service, small_settings):
    sid = service.add_session("empty").id
    head = b'--B\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n\r\n%PDF-1.4 and then the body just ends'
    response = await client.post(f"/api/sessions/{sid}/document", content=head, headers={"content-type": "multipart/form-data; boundary=B"})
    assert response.status_code == 400
    assert tmp_leftovers(small_settings) == []


async def test_upload_service_errors_keep_status_and_remove_the_temp_file(client, service, small_settings):
    sid = service.add_session("empty").id
    scanned = await upload(client, sid, name="scanned.pdf")
    assert scanned.status_code == 422 and error_of(scanned)["code"] == "scanned_pdf"
    assert tmp_leftovers(small_settings) == []

    ok = await upload(client, sid)
    assert ok.status_code == 202
    again = await upload(client, sid)
    assert again.status_code == 409 and error_of(again)["code"] == "document_already_uploaded"
    assert tmp_leftovers(small_settings) == []


async def test_upload_unknown_session_is_404_and_writes_nothing(client, small_settings):
    response = await upload(client, new_id())
    assert response.status_code == 404 and error_of(response)["code"] == "session_not_found"
    assert tmp_leftovers(small_settings) == []


# --------------------------------------------------------------------------------------------- PDF file route
async def test_pdf_file_route_serves_inline_pdf(client, service):
    sid = service.add_session("ready").id
    response = await client.get(f"/api/sessions/{sid}/document/file")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-disposition"].startswith("inline;") and "filename*=UTF-8''" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["etag"] and response.headers["last-modified"]
    assert response.content.startswith(b"%PDF-") and len(response.content) == (service.workdir / f"{sid}.pdf").stat().st_size


async def test_pdf_file_route_supports_range_requests(client, service):
    sid = service.add_session("ready").id
    size = (service.workdir / f"{sid}.pdf").stat().st_size
    part = await client.get(f"/api/sessions/{sid}/document/file", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 0-99/{size}" and len(part.content) == 100 and part.content.startswith(b"%PDF-")
    tail = await client.get(f"/api/sessions/{sid}/document/file", headers={"Range": f"bytes={size - 10}-"})
    assert tail.status_code == 206 and len(tail.content) == 10
    assert (await client.get(f"/api/sessions/{sid}/document/file", headers={"Range": f"bytes={size + 5}-"})).status_code == 416


async def test_pdf_file_route_head(client, service):
    sid = service.add_session("ready").id
    head = await client.head(f"/api/sessions/{sid}/document/file")
    assert head.status_code == 200 and head.content == b"" and int(head.headers["content-length"]) > 0
    assert head.headers["etag"] and head.headers["accept-ranges"] == "bytes"


async def test_pdf_file_route_errors(client, service):
    empty = service.add_session("empty").id
    response = await client.get(f"/api/sessions/{empty}/document/file")
    assert response.status_code == 404 and error_of(response)["code"] == "document_not_found"
    ready = service.add_session("ready").id
    (service.workdir / f"{ready}.pdf").unlink()
    assert error_of(await client.get(f"/api/sessions/{ready}/document/file"))["code"] == "document_not_found"


# --------------------------------------------------------------------------------------------- pages / outline / locate
async def test_pages_and_outline(client, service):
    ready = service.add_session("ready").id
    pages = (await client.get(f"/api/sessions/{ready}/document/pages")).json()
    assert pages["page_count"] == 2 and pages["pages"][0]["printed_page"] == "i"
    assert (await client.get(f"/api/sessions/{ready}/document/outline")).json()["nodes"][0]["title"] == "Strategic report"
    indexing = service.add_session("indexing").id
    for suffix in ("pages", "outline"):
        response = await client.get(f"/api/sessions/{indexing}/document/{suffix}")
        assert response.status_code == 409 and error_of(response)["code"] == "document_not_ready"


async def test_locate_passes_arguments_through(client, service):
    sid = service.add_session("ready").id
    response = await client.get(f"/api/sessions/{sid}/locate", params={"page": 15, "quote": "revenue rose", "claim": "Revenue grew"})
    assert response.status_code == 200 and response.json()["rects"] and response.json()["method"] == "exact"
    assert service.calls[-1] == ("locate", sid, 15, "revenue rose", "Revenue grew")
    await client.get(f"/api/sessions/{sid}/locate", params={"page": 3, "quote": ""})
    assert service.calls[-1] == ("locate", sid, 3, None, None)


@pytest.mark.parametrize("query", ["", "page=0", "page=-3", "page=abc", "page=1.5", "page=999999999", "page=1&quote=" + "x" * 4001])
async def test_locate_validates_input(client, service, query):
    sid = service.add_session("ready").id
    response = await client.get(f"/api/sessions/{sid}/locate?{query}")
    assert response.status_code == 400 and error_of(response)["code"] == "invalid_request"
    assert "x" * 50 not in error_of(response)["message"]                   # never echo user input back


# --------------------------------------------------------------------------------------------- messages (SSE)
async def test_message_stream_framing_and_order(client, service):
    sid = service.add_session("ready").id
    service.ask_script = standard_script(sid)
    response = await client.post(f"/api/sessions/{sid}/messages", json={"content": "What was revenue?"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["connection"] == "keep-alive"
    assert response.text.isascii() and response.text.endswith("\n\n")

    events = parse_sse(response.text)
    assert [n for n, _ in events] == ["message_start", "step", "step_done", "token", "token", "citation", "answer_done",
                                      "eval_started", "eval_result", "eval_done", "done"]
    data = dict(events)
    assert data["message_start"]["user_message"]["content"] == "What was revenue?"          # pydantic -> JSON
    assert data["answer_done"]["message"]["citations"][0]["rects"][0]["w"] == 0.5
    assert data["answer_done"]["message"]["content"] == "Revenue was £14,812m [[c1]]."        # non-ASCII round trip
    assert data["eval_done"]["evaluation"]["status"] == "done"
    assert service.calls[-1] == ("ask", sid, "What was revenue?")


async def test_message_stream_frames_are_exactly_event_data_blank_line(client, service):
    sid = service.add_session("ready").id
    service.ask_script = [("token", {"message_id": "m", "text": "line1\nline2 \u2028 ünï"}), ("done", {})]
    text = (await client.post(f"/api/sessions/{sid}/messages", json={"content": "q"})).text
    assert text == ('event: token\ndata: {"message_id":"m","text":"line1\\nline2 \\u2028 \\u00fcn\\u00ef"}\n\n'
                    "event: done\ndata: {}\n\n")


async def test_message_stream_appends_done_when_the_service_forgets(client, service):
    sid = service.add_session("ready").id
    service.ask_script = [("token", {"message_id": "m", "text": "hi"})]
    names = [n for n, _ in parse_sse((await client.post(f"/api/sessions/{sid}/messages", json={"content": "q"})).text)]
    assert names == ["token", "done"]


@pytest.mark.parametrize("body,status,code", [
    ({"content": ""}, 400, "empty_question"),
    ({"content": "   \n\t"}, 400, "empty_question"),
    ({}, 400, "invalid_request"),
    ({"content": 42}, 400, "invalid_request"),
    ({"text": "wrong field"}, 400, "invalid_request"),
])
async def test_message_input_errors_are_json_before_the_stream(client, service, body, status, code):
    sid = service.add_session("ready").id
    response = await client.post(f"/api/sessions/{sid}/messages", json=body)
    assert response.status_code == status and error_of(response)["code"] == code
    assert response.headers["content-type"].startswith("application/json")


async def test_message_bad_json_body(client, service):
    sid = service.add_session("ready").id
    response = await client.post(f"/api/sessions/{sid}/messages", content=b"not json", headers={"content-type": "application/json"})
    assert response.status_code == 400 and error_of(response)["code"] == "invalid_request"


async def test_message_service_errors_become_proper_http_errors(client, service):
    ready = service.add_session("ready").id
    indexing = service.add_session("indexing").id
    assert (await client.post(f"/api/sessions/{new_id()}/messages", json={"content": "q"})).status_code == 404
    not_ready = await client.post(f"/api/sessions/{indexing}/messages", json={"content": "q"})
    assert not_ready.status_code == 409 and error_of(not_ready)["code"] == "document_not_ready"
    service.busy = True
    busy = await client.post(f"/api/sessions/{ready}/messages", json={"content": "q"})
    assert busy.status_code == 409 and error_of(busy)["code"] == "session_busy"
    service.busy, service.openai_configured = False, False
    no_key = await client.post(f"/api/sessions/{ready}/messages", json={"content": "q"})
    assert no_key.status_code == 503 and error_of(no_key)["code"] == "openai_not_configured"
    assert service.generators_closed == 4                                   # every generator was finalised


async def test_message_stream_unexpected_exception_becomes_error_then_done(client, service, caplog):
    sid = service.add_session("ready").id
    service.ask_script = [("message_start", {"message_id": "m"})]
    service.ask_raises = RuntimeError("secret internal detail /tmp/x.py")
    with caplog.at_level(logging.ERROR, logger="reportlens.sse"):
        response = await client.post(f"/api/sessions/{sid}/messages", json={"content": "q"})
    events = parse_sse(response.text)
    assert [n for n, _ in events] == ["message_start", "error", "done"]
    assert events[1][1]["code"] == "internal_error" and events[1][1]["message_id"] is None
    assert "secret" not in response.text and "Traceback" not in response.text
    assert "secret internal detail" in caplog.text                          # ...but it is in the log


async def test_message_stream_service_error_midstream_keeps_its_code(client, service):
    sid = service.add_session("ready").id
    service.ask_script = [("message_start", {"message_id": "m"})]
    service.ask_raises = ServiceError("openai_rate_limit", "OpenAI rate limit reached.", 429)
    events = parse_sse((await client.post(f"/api/sessions/{sid}/messages", json={"content": "q"})).text)
    assert events[-2] == ("error", {"code": "openai_rate_limit", "message": "OpenAI rate limit reached.", "message_id": None})
    assert events[-1][0] == "done"


async def test_message_stream_unserialisable_payload_is_contained(client, service):
    sid = service.add_session("ready").id
    service.ask_script = [("message_start", {"message_id": "m"}), ("token", {"message_id": "m", "text": float("nan")}), ("token", {"never": 1})]
    events = parse_sse((await client.post(f"/api/sessions/{sid}/messages", json={"content": "q"})).text)
    assert [n for n, _ in events] == ["message_start", "error", "done"]            # NaN is not valid JSON: contained, not leaked
    assert events[1][1]["code"] == "internal_error"


async def test_message_stream_empty_service_stream_still_ends_with_done(client, service):
    sid = service.add_session("ready").id
    service.ask_script = []
    response = await client.post(f"/api/sessions/{sid}/messages", json={"content": "q"})
    assert response.status_code == 200 and parse_sse(response.text) == [("done", {})]


def test_format_event_rejects_bad_names_and_handles_models():
    with pytest.raises(ValueError):
        sse.format_event("bad\nname", {})
    with pytest.raises(ValueError):
        sse.format_event("", {})
    frame = sse.format_event("citation", Citation(id="c1", index=1, doc_name="d.pdf", page=1, cited_page=1))
    assert frame.startswith("event: citation\ndata: {") and frame.endswith("}\n\n") and frame.count("\n") == 3


async def test_message_stream_sends_ping_comments_while_the_service_is_busy(client, service, monkeypatch):
    monkeypatch.setattr(sse, "PING_INTERVAL_S", 0.05)
    sid = service.add_session("ready").id
    service.ask_script = [("message_start", {"message_id": "m"}), ("token", {"message_id": "m", "text": "late"}), ("done", {})]
    service.ask_delay = 0.3
    text = (await client.post(f"/api/sessions/{sid}/messages", json={"content": "q"})).text
    assert text.count(": ping\n\n") >= 3
    assert [n for n, _ in parse_sse(text)] == ["message_start", "token", "done"]       # pings never break event framing
    assert ": ping" not in "".join(m["data"] for m in SSE_FRAME.finditer(text))


async def asgi_post_and_disconnect(app, path: str, body: dict, *, after_chunks: int = 1, signal_disconnect: bool = True) -> list[dict]:
    """Drives the ASGI app directly (httpx buffers whole responses) and hangs up after `after_chunks` body chunks, either by
    sending `http.disconnect` or (ASGI >= 2.4 style) by making the next `send` fail with OSError."""
    payload = json.dumps(body).encode()
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "method": "POST", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "", "scheme": "http", "client": ("127.0.0.1", 5000),
             "server": ("testserver", 80), "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                                                 (b"content-length", str(len(payload)).encode())]}
    sent_body = False
    hang_up = asyncio.Event()
    sent: list[dict] = []

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await hang_up.wait()
        if not signal_disconnect:
            await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message):
        if hang_up.is_set() and not signal_disconnect:
            raise OSError("client gone")
        sent.append(message)
        if len([m for m in sent if m["type"] == "http.response.body"]) >= after_chunks:
            hang_up.set()

    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    return sent


async def test_client_disconnect_closes_the_service_generator(app, service):
    sid = service.add_session("ready").id
    closed = asyncio.Event()
    started = asyncio.Event()

    async def endless(_sid, _content):
        try:
            yield "message_start", {"message_id": "m"}
            started.set()
            await asyncio.sleep(3600)              # an agent run that would take minutes
            yield "token", {"message_id": "m", "text": "never"}
        finally:
            closed.set()                           # what the real service uses to cancel the run

    service.ask = endless                          # type: ignore[method-assign]
    sent = await asgi_post_and_disconnect(app, f"/api/sessions/{sid}/messages", {"content": "q"})
    assert started.is_set()
    assert closed.is_set(), "the service generator must be closed when the client goes away"
    bodies = [m["body"] for m in sent if m["type"] == "http.response.body"]
    assert b"event: message_start" in bodies[0]


async def test_failed_send_also_closes_the_service_generator(app, service, monkeypatch):
    """ASGI 2.4 servers signal a gone client by failing `send`; the next ping then triggers the cleanup."""
    monkeypatch.setattr(sse, "PING_INTERVAL_S", 0.02)
    sid = service.add_session("ready").id
    closed = asyncio.Event()

    async def endless(_sid, _content):
        try:
            yield "message_start", {"message_id": "m"}
            await asyncio.sleep(3600)
        finally:
            closed.set()

    service.ask = endless                          # type: ignore[method-assign]
    await asgi_post_and_disconnect(app, f"/api/sessions/{sid}/messages", {"content": "q"}, after_chunks=2, signal_disconnect=False)
    assert closed.is_set()


async def test_disconnect_before_the_first_byte_still_closes_the_generator():
    """The response object is dropped before `stream_response` ever iterates the body."""
    closed = asyncio.Event()

    async def source():
        try:
            yield "message_start", {}
            await asyncio.sleep(3600)
        finally:
            closed.set()

    events = source()
    first = await events.__anext__()
    response = sse.EventStreamResponse(events, first=first)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(_):
        raise OSError("client gone")

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert closed.is_set()


async def test_normal_completion_closes_the_generator_too(client, service):
    sid = service.add_session("ready").id
    service.ask_script = standard_script(sid)
    await client.post(f"/api/sessions/{sid}/messages", json={"content": "q"})
    assert service.generators_closed == 1


async def test_get_message_and_evaluate(client, service):
    locked = service.add_session("locked").id
    mid = new_id()
    message = (await client.get(f"/api/sessions/{locked}/messages/{mid}")).json()
    assert message["id"] == mid and message["status"] == "answered"
    scores = await client.post(f"/api/sessions/{locked}/messages/{mid}/evaluate")
    assert scores.status_code == 200 and scores.json()["faithfulness"] == 1.0
    ready = service.add_session("ready").id
    missing = await client.post(f"/api/sessions/{ready}/messages/{mid}/evaluate")
    assert missing.status_code == 404 and error_of(missing)["code"] == "message_not_found"


# --------------------------------------------------------------------------------------------- headers, errors, logging
def assert_security_headers(response: httpx.Response) -> None:
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_csp_matches_the_contract():
    assert CONTENT_SECURITY_POLICY == (
        "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'")


async def test_security_headers_on_every_kind_of_response(client, service):
    sid = service.add_session("ready").id
    service.ask_script = standard_script(sid)
    responses = [
        await client.get("/"), await client.get("/static/js/main.js"), await client.get("/api/health"), await client.get("/api/nope"),
        await client.get("/api/sessions/zzz"), await client.get(f"/api/sessions/{sid}/locate"), await client.get(f"/api/sessions/{sid}/document/file"),
        await client.post("/api/sessions", content=b"{", headers={"content-type": "application/json"}),
        await client.post(f"/api/sessions/{sid}/messages", json={"content": "q"}), await client.delete(f"/api/sessions/{sid}"),
        await client.get("/static/does-not-exist.js"), await client.put("/api/sessions"),
    ]
    assert len(responses) == 12
    for response in responses:
        assert_security_headers(response)


def test_api_key_fragments_never_reach_a_log_handler(service, settings):
    import io

    create_app(settings, service)                                  # installs the redaction (idempotent)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    app_module.install_log_redaction()                             # handlers added later are covered by the next install
    logger = logging.getLogger("reportlens.qa")
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        logger.warning("Incorrect API key provided: sk-proj-AbC123***xyz9 for %s", "sk-live_ABCDEF123456")
        try:
            raise RuntimeError("401 Incorrect API key provided: sk-abcdef**********wxyz")
        except RuntimeError:
            logger.exception("call failed")
        logger.info("short sk-1 and a plain message stay")
    finally:
        logger.setLevel(old_level)
        root.removeHandler(handler)
    out = stream.getvalue()
    assert "sk-proj" not in out and "AbC123" not in out and "sk-live" not in out and "sk-abcdef" not in out and "wxyz" not in out
    assert out.count("sk-...") >= 3 and "call failed" in out and "Traceback" in out and "short sk-1 and a plain message stay" in out
    filters = [f for f in logging.getLogger("reportlens").filters if isinstance(f, app_module.KeyRedactionFilter)]
    assert len(filters) == 1                                       # idempotent


@pytest.mark.parametrize("host", ["localhost", "localhost:8000", "127.0.0.1:8123", "[::1]:8000", "[::1]", "testserver", "LOCALHOST"])
async def test_local_host_names_are_accepted(app, host):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        assert (await c.get("/api/health", headers={"host": host})).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "localhost.evil.example", "10.0.0.5:8000", "", "[::2]:80"])
async def test_foreign_host_headers_are_rejected_with_the_standard_error(app, host):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        response = await c.get("/api/health", headers={"host": host})
    assert response.status_code == 400 and error_of(response)["code"] == "invalid_host"
    assert_security_headers(response)


async def test_the_configured_host_is_accepted_and_bind_all_skips_host_validation(small_settings, service):
    for configured, host, ok in [("myreports.lan", "myreports.lan:8000", True), ("myreports.lan", "evil.example", False),
                                 ("0.0.0.0", "192.168.1.20:8000", True), ("::", "pc.local:8000", True)]:
        app = create_app(small_settings.with_(host=configured), service)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            assert (await c.get("/api/health", headers={"host": host})).status_code == (200 if ok else 400), (configured, host)


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
@pytest.mark.parametrize("origin", ["http://evil.example", "http://localhost:9999", "null", "http://testserver.evil.example", "garbage"])
async def test_cross_origin_state_changes_are_forbidden(client, service, method, origin):
    sid = service.add_session("ready").id
    path = "/api/sessions" if method == "POST" else f"/api/sessions/{sid}"
    response = await client.request(method, path, headers={"origin": origin}, json={"title": "x"} if method == "PATCH" else None)
    assert response.status_code == 403 and error_of(response)["code"] == "forbidden_origin"
    assert_security_headers(response)
    assert not [c for c in service.calls if c[0] in ("create_session", "delete_session", "rename_session")]


async def test_same_origin_and_origin_less_state_changes_and_cross_origin_reads_pass(client, service):
    sid = service.add_session("ready").id
    same = await client.post("/api/sessions", headers={"origin": "http://testserver"}, json={})
    assert same.status_code == 201
    renamed = await client.patch(f"/api/sessions/{sid}", headers={"origin": "http://testserver"}, json={"title": "ok"})
    assert renamed.status_code == 200
    assert (await client.post("/api/sessions", json={})).status_code == 201                      # no Origin: curl, scripts
    assert (await client.get("/api/sessions", headers={"origin": "http://evil.example"})).status_code == 200   # reads are not state changes
    assert (await client.delete(f"/api/sessions/{sid}", headers={"origin": "http://testserver"})).status_code == 204


async def test_unknown_api_paths_and_wrong_methods_are_json(client):
    missing = await client.get("/api/does/not/exist")
    assert missing.status_code == 404 and error_of(missing) == {"code": "not_found", "message": "Not Found."}
    wrong = await client.put("/api/sessions")
    assert wrong.status_code == 405 and error_of(wrong)["code"] == "method_not_allowed" and "GET" in wrong.headers["allow"]
    assert (await client.get("/definitely-not-a-page")).status_code == 404


async def test_unexpected_errors_are_generic_500_with_logged_traceback(client, service, caplog):
    def boom():
        raise RuntimeError("database exploded at C:\\secret\\path")

    service.list_sessions = boom                   # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger="reportlens.web"):
        response = await client.get("/api/sessions")
    assert response.status_code == 500
    assert error_of(response)["code"] == "internal_error"
    assert "secret" not in response.text and "Traceback" not in response.text and "exploded" not in response.text
    assert "database exploded" in caplog.text and "Traceback" in caplog.text
    assert_security_headers(response)


async def test_requests_are_logged_at_info_with_duration_and_no_query_or_body(client, service, caplog):
    sid = service.add_session("ready").id
    with caplog.at_level(logging.INFO, logger="reportlens.web"):
        await client.get(f"/api/sessions/{sid}/locate", params={"page": 2, "quote": "TOP SECRET QUOTE"})
        await client.post("/api/sessions", json={"from_session": "a" * 32})
    lines = [r.getMessage() for r in caplog.records if r.name == "reportlens.web"]
    assert any(re.fullmatch(rf"GET /api/sessions/{sid}/locate 200 \d+ ms", line) for line in lines), lines
    assert any(re.fullmatch(r"POST /api/sessions 404 \d+ ms", line) for line in lines), lines
    assert "TOP SECRET" not in caplog.text and "a" * 32 not in "".join(line for line in lines if "POST" in line)


async def test_service_not_yet_available_is_a_503(small_settings):
    app = create_app(small_settings)               # lifespan never ran, so there is no service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        response = await c.get("/api/sessions")
        assert response.status_code == 503 and error_of(response)["code"] == "service_unavailable"
        assert (await c.get("/")).status_code == 200            # the shell still loads


# --------------------------------------------------------------------------------------------- static files
async def test_index_page(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert "<title>" in response.text and "/static/js/main.js" in response.text
    assert (await client.head("/")).status_code == 200


@pytest.mark.parametrize("path,mime", [
    ("/static/js/main.js", "text/javascript; charset=utf-8"),
    ("/static/css/app.css", "text/css; charset=utf-8"),
    ("/static/vendor/marked/marked.esm.js", "text/javascript; charset=utf-8"),
    ("/static/vendor/pdfjs/pdf.min.mjs", "text/javascript; charset=utf-8"),
    ("/static/vendor/pdfjs/pdf.worker.min.mjs", "text/javascript; charset=utf-8"),
    ("/static/vendor/pdfjs/wasm/qcms_bg.wasm", "application/wasm"),
    ("/static/vendor/fonts/Geist-Variable.woff2", "font/woff2"),
    ("/static/vendor/pdfjs/cmaps/H.bcmap", "application/octet-stream"),
])
async def test_static_mime_types(client, path, mime):
    response = await client.get(path)
    assert response.status_code == 200, path
    assert response.headers["content-type"] == mime


async def test_static_cache_policy(client):
    for path in ("/static/js/main.js", "/static/css/app.css", "/static/index.html"):
        assert (await client.get(path)).headers["cache-control"] == "no-cache", path
    for path in ("/static/vendor/pdfjs/pdf.min.mjs", "/static/vendor/fonts/Geist-Variable.woff2", "/static/vendor/pdfjs/wasm/qcms_bg.wasm"):
        cache = (await client.get(path)).headers["cache-control"]
        assert re.fullmatch(r"public, max-age=\d+", cache) and int(cache.rsplit("=", 1)[1]) >= 86400 * 7, path


async def test_static_revalidation_returns_304(client):
    first = await client.get("/static/js/main.js")
    second = await client.get("/static/js/main.js", headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304


@pytest.mark.parametrize("path", ["/static/../../pyproject.toml", "/static/%2e%2e/%2e%2e/pyproject.toml", "/static/..%5c..%5c.env",
                                  "/static/js/", "/static/missing.js"])
async def test_static_does_not_escape_its_directory(client, path):
    response = await client.get(path)
    assert response.status_code == 404
    assert b"[project]" not in response.content and b"OPENAI_API_KEY" not in response.content


def test_static_front_end_is_present():
    for name in ("index.html", "js/main.js", "js/api.js", "js/sse.js", "js/viewer.js", "vendor/pdfjs/pdf.min.mjs"):
        assert (STATIC_DIR / name).is_file(), name


def test_front_end_calls_only_routes_the_server_has(app):
    """Contract check between the UI and the routes: every /api URL literal in js/ matches a registered route."""
    patterns = [re.compile("^" + re.sub(r"\{[^/]+\}", r"[^/]+", p) + "$") for p in app.openapi()["paths"] if p.startswith("/api")]
    used: set[str] = set()
    for js in (STATIC_DIR / "js").glob("*.js"):
        for literal in re.findall(r"""[`"'](/api/[^`"'?]*)""", js.read_text(encoding="utf-8")):
            used.add(re.sub(r"\$\{[^}]+\}", "x", literal).rstrip("/"))
    assert used, "no /api literals found: the regex is stale"
    for literal in used:
        if literal == "/api/sessions":
            continue
        assert any(p.match(literal) for p in patterns), f"front end calls {literal} but no route matches"


# --------------------------------------------------------------------------------------------- lifespan
async def test_injected_service_is_used_and_not_closed_by_the_app(small_settings):
    closed: list[bool] = []

    class Closable(FakeService):
        async def aclose(self) -> None:
            closed.append(True)

    service = Closable(Path("."), Path("."))
    app = create_app(small_settings, service)
    async with app.router.lifespan_context(app):
        assert app.state.service is service
    assert app.state.service is service and closed == []


class _StackRecorder:
    def __init__(self, order: list[str]):
        self.order, self.service = order, None

    async def close(self) -> None:
        self.order.append("closed")


async def test_lifespan_builds_and_tears_down_the_stack(small_settings, monkeypatch):
    order: list[str] = []
    built = object()

    def fake_build(settings, stack):
        stack.service = built
        order.append("built")
        return settings.with_(openai_base_url="http://mock/v1")

    monkeypatch.setattr(app_module, "_build_stack", fake_build)
    monkeypatch.setattr(app_module, "_Stack", lambda: _StackRecorder(order))
    app = create_app(small_settings)
    async with app.router.lifespan_context(app):
        assert app.state.service is built and app.state.settings.openai_base_url == "http://mock/v1"
        assert order == ["built"]
    assert order == ["built", "closed"] and app.state.service is None


async def test_lifespan_failure_still_closes_what_was_opened(small_settings, monkeypatch):
    order: list[str] = []

    def fake_build(settings, stack):
        raise RuntimeError("store is locked")

    monkeypatch.setattr(app_module, "_build_stack", fake_build)
    monkeypatch.setattr(app_module, "_Stack", lambda: _StackRecorder(order))
    app = create_app(small_settings)
    with pytest.raises(RuntimeError, match="store is locked"):
        async with app.router.lifespan_context(app):
            pytest.fail("must not start")
    assert order == ["closed"]
