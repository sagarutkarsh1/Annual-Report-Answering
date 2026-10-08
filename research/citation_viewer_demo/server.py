"""
Minimal demo backend for the citation -> source side panel (research prototype, NOT the final app).

    pip install fastapi uvicorn pypdfium2 rapidfuzz
    set DEMO_PDF=path\to\report.pdf      (default: ../test_assets/test5.pdf)
    python server.py                      -> http://127.0.0.1:8765/

Endpoints
    GET /api/doc                         page_count + per-page visible size in PDF points (lets the browser lay out
                                         300 placeholders at the right height WITHOUT loading pdf.js pages)
    GET /api/pdf                         the PDF, served by Starlette FileResponse (HTTP Range / 206 built in)
    GET /api/locate?page=&quote=         quote locator -> {page, rects:[{x,y,w,h}], method, score, ...}
    GET /                                demo page (static/index.html)

Optional env:  DEMO_CSP=1  adds a strict Content-Security-Policy so the CDN/worker/blob rules can be tested.
               DEMO_PDF_CACHE="no-store"  to watch real Range traffic (default private,max-age=3600 lets Chrome answer
               pdf.js range requests from the cached full response so the server sees only one request)
The PDF bytes are held in memory and PDFium opens from bytes (no Windows file lock on the upload).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import quote_locator_prototype as ql  # noqa: E402

PDF_PATH = Path(os.environ.get("DEMO_PDF", HERE.parent / "test_assets" / "test5.pdf"))
CDN = "https://cdn.jsdelivr.net"

app = FastAPI(title="citation viewer demo")
_doc = ql.PdfiumDoc(PDF_PATH.read_bytes())
_sizes: list[dict] | None = None
_locate_cache: dict[tuple, dict] = {}


@app.middleware("http")
async def headers_and_log(request: Request, call_next):
    t = time.perf_counter()
    resp = await call_next(request)
    csp = os.environ.get("DEMO_CSP")
    if csp == "1":
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' 'unsafe-inline' {CDN}; style-src 'self' 'unsafe-inline'; "
            f"worker-src blob: {CDN}; connect-src 'self' {CDN}; img-src 'self' data: blob:; font-src 'self' data: {CDN}")
    elif csp == "2":      # minimal: worker-src blob: only (pdf.js wraps a cross-origin worker in a blob: URL)
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' 'unsafe-inline' {CDN}; style-src 'self' 'unsafe-inline'; "
            f"worker-src blob:; connect-src 'self' {CDN}; img-src 'self' data: blob:")
    elif csp == "3":      # broken on purpose: no blob: -> shows what fails
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' 'unsafe-inline' {CDN}; style-src 'self' 'unsafe-inline'; "
            f"worker-src {CDN}; connect-src 'self' {CDN}; img-src 'self' data: blob:")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    if "cache-control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-cache"          # dev: always revalidate static files (ETag/Last-Modified still apply)
    if request.url.path.startswith("/api/pdf"):
        print(f"[pdf] {request.method} range={request.headers.get('range')} -> {resp.status_code} "
              f"content-range={resp.headers.get('content-range')} len={resp.headers.get('content-length')} "
              f"{(time.perf_counter() - t) * 1000:.0f}ms", flush=True)
    return resp


@app.get("/api/doc")
def doc_info():
    global _sizes
    if _sizes is None:
        _sizes = [{"w": round(w, 2), "h": round(h, 2)} for w, h in _doc.page_sizes()]
    return {"name": PDF_PATH.name, "page_count": _doc.page_count, "pages": _sizes, "url": "/api/pdf"}


@app.api_route("/api/pdf", methods=["GET", "HEAD"])
def get_pdf():
    # FileResponse (starlette >= 0.39) honours Range / If-Range / HEAD and sets Accept-Ranges: bytes.
    return FileResponse(PDF_PATH, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{PDF_PATH.name}"',
                                 "Cache-Control": os.environ.get("DEMO_PDF_CACHE", "private, max-age=3600")})


@app.get("/api/locate")
def locate(page: int = Query(..., ge=1), quote: str = Query(..., min_length=1, max_length=4000)):
    key = (page, quote)
    if key not in _locate_cache:
        t = time.perf_counter()
        res = ql.locate(_doc.page_words, _doc.page_count, page, quote, radius=1,
                        doc_squash=_doc.squashed_pages)
        out = res.to_json()
        out["elapsed_ms"] = round((time.perf_counter() - t) * 1000, 1)
        _locate_cache[key] = out
    return JSONResponse(_locate_cache[key])


app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8765")), log_level="warning")
