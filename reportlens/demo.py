"""The built-in, read-only demo chat: a real conversation (questions, cited answers, live scores) that anyone may open
without an access code, so visitors see what the app does before they ask the owner for a code.

A demo is a folder made by ``scripts/export_demo.py`` from any chat, in one of two shapes:

    static (default)     chat.json only: title, document row, every question and cited answer with its scores.  No PDF: a
                         citation opens a card with the page, section and verified quote instead of the viewer.  Small enough
                         to ship inside the package (``reportlens/demo_data/``), so it reaches every deployment with the code.
    full (--with-files)  chat.json (plus the page texts behind each answer) and files/<doc>.pdf + files/pageindex/: citations
                         open the PDF at the highlighted passage.  The PDF is a third-party document: keep it out of public
                         repositories (``demo/`` in the project is git-ignored for that reason).

``DEMO_DIR`` picks the folder; unset = ``demo/`` in the project when it holds a chat.json, else the packaged static demo.

At start-up `install_demo` copies the files into the data folder under the fixed id `DEMO_SESSION_ID`, owned by
`store.DEMO_OWNER`.  The web layer lets anyone *read* that chat and nobody change it; the spend budget ignores it.  The
install is idempotent (an existing demo is replaced) and never stops the app from starting: a broken demo is logged and
skipped.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .config import Settings
from .models import ContextPage, DocumentInfo, Message
from .store import DEMO_OWNER, Store

log = logging.getLogger("reportlens.demo")

DEMO_SESSION_ID = "de300000000000000000000000000001"     # well-known, 32-hex like every session id (the UI routes on it)
CHAT_FILE = "chat.json"
FILES_DIR = "files"
FORMAT = 1


@dataclass(frozen=True)
class DemoInfo:
    """What GET /api/demo tells the browser."""
    session_id: str
    title: str
    filename: str
    page_count: Optional[int]
    questions: int
    attribution: Optional[str] = None
    attribution_url: Optional[str] = None
    has_document: bool = True             # False: a static demo (no PDF on the server; citations open a source card)

    def public(self) -> dict[str, Any]:
        return {"available": True, **asdict(self)}


def _demo_message_id(old: str) -> str:
    """Stable, collision-free ids for the demo copy (the source chat may live in the same database during development)."""
    return hashlib.sha256(f"{DEMO_SESSION_ID}:{old}".encode()).hexdigest()[:32]


# ------------------------------------------------------------------------------------------------ export
def export_session(store: Store, settings: Settings, sid: str, out_dir: Path, *, attribution: Optional[str] = None,
                   attribution_url: Optional[str] = None, title: Optional[str] = None,
                   display_name: Optional[str] = None, with_files: bool = False) -> Path:
    """Write chat `sid` of `store` / `settings.data_dir` as a demo folder.  Only finished answers are taken.  `with_files`: the
    full demo (PDF, PageIndex store, page texts); otherwise the static one (chat.json only)."""
    session = store.get_session(sid)
    if session is None:
        raise LookupError(f"no chat {sid} in {settings.data_dir}")
    doc = session.document
    if doc is None or doc.status != "ready" or not doc.pi_doc_id:
        raise ValueError("that chat has no indexed document")
    source_dir = settings.session_dir(sid)
    pdf = source_dir / doc.doc_name
    store_dir = source_dir / "pageindex"
    if with_files and (not pdf.is_file() or not store_dir.is_dir()):
        raise FileNotFoundError(f"the chat's files are missing under {source_dir}")
    messages = []
    for msg in store.list_messages(sid):
        if msg.role == "assistant" and msg.status not in ("answered", "no_sources"):
            continue
        contexts = store.get_contexts(msg.id) if msg.role == "assistant" and with_files else []
        messages.append({"message": msg.model_dump(mode="json"), "contexts": [c.model_dump(mode="json") for c in contexts]})
    if not any(m["message"]["role"] == "assistant" for m in messages):
        raise ValueError("that chat has no finished answer to show")
    chat = {
        "format": FORMAT,
        "title": title or session.title,
        "document": {**doc.model_dump(mode="json"), "pi_doc_id": doc.pi_doc_id,
                     **({"filename": display_name} if display_name else {})},           # display only: doc_name is what citations use
        "messages": messages,
        "attribution": attribution,
        "attribution_url": attribution_url,
    }
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = out_dir / FILES_DIR
    if files.exists():
        shutil.rmtree(files)
    if with_files:
        files.mkdir(parents=True)
        shutil.copyfile(pdf, files / doc.doc_name)
        shutil.copytree(store_dir, files / "pageindex")
    (out_dir / CHAT_FILE).write_text(json.dumps(chat, indent=1, ensure_ascii=True), encoding="utf-8")
    return out_dir


# ------------------------------------------------------------------------------------------------ install
def install_demo(settings: Settings, store: Store) -> Optional[DemoInfo]:
    """Install (or replace) the demo chat from `settings.demo_dir`.  None when there is no demo or it cannot be used."""
    folder = settings.demo_dir
    if folder is None or not (Path(folder) / CHAT_FILE).is_file():
        return None
    try:
        return _install(Path(folder), settings, store)
    except Exception:  # noqa: BLE001 - a broken demo must never keep the app from starting
        log.exception("the demo chat in %s could not be installed; continuing without it", folder)
        try:
            store.delete_session(DEMO_SESSION_ID)
        except Exception:  # noqa: BLE001
            pass
        return None


def _install(folder: Path, settings: Settings, store: Store) -> DemoInfo:
    chat = json.loads((folder / CHAT_FILE).read_text(encoding="utf-8"))
    if chat.get("format") != FORMAT:
        raise ValueError(f"unsupported demo format {chat.get('format')!r}")
    raw_doc = dict(chat["document"])
    pi_doc_id = raw_doc.pop("pi_doc_id")
    doc = DocumentInfo.model_validate(raw_doc).model_copy(update={"pi_doc_id": pi_doc_id})
    source = folder / FILES_DIR
    has_document = (source / doc.doc_name).is_file() and (source / "pageindex").is_dir()   # else: a static demo

    store.delete_session(DEMO_SESSION_ID)                  # a previous run's copy (local data folders persist)
    dest = settings.session_dir(DEMO_SESSION_ID)
    if dest.exists():
        shutil.rmtree(dest)
    if has_document:
        shutil.copytree(source, dest)

    store.create_session(chat.get("title") or doc.filename, owner=DEMO_OWNER, sid=DEMO_SESSION_ID)
    store.put_document(DEMO_SESSION_ID, doc)
    questions = 0
    for item in chat["messages"]:
        msg = Message.model_validate(item["message"])
        msg = msg.model_copy(update={"id": _demo_message_id(msg.id), "session_id": DEMO_SESSION_ID})
        contexts = [ContextPage.model_validate(c) for c in item.get("contexts") or []]
        store.add_message(msg, contexts if msg.role == "assistant" else None)
        questions += msg.role == "user"
    info = DemoInfo(session_id=DEMO_SESSION_ID, title=chat.get("title") or doc.filename, filename=doc.filename,
                    page_count=doc.page_count, questions=questions, attribution=chat.get("attribution"),
                    attribution_url=chat.get("attribution_url"), has_document=has_document)
    log.info("demo chat installed: %r (%s, %d question(s), %s)", info.title, doc.filename, questions,
             "with its PDF" if has_document else "static: no PDF")
    return info
