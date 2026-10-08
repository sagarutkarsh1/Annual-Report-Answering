"""SQLite persistence: sessions, their single document, chat messages and the page contexts behind each answer.

Design notes (the contract is docs/ARCHITECTURE.md 4.5):

* Pooled connections.  FastAPI runs us from many worker threads, so a connection is never used by two threads at once:
  each call checks one out of a small idle pool (or opens a new one) and returns it afterwards.  Reuse matters on
  Windows: a brand-new connection pays ~12 ms on its first write transaction, a reused one ~1 ms.  The idle pool also
  keeps SQLite from seeing "last connection closed" (which would checkpoint and delete the WAL file), and ``close()``
  releases every file handle, which Windows needs before the data directory can be deleted.
* Writers take a process-wide lock, then ``BEGIN IMMEDIATE``.  The lock avoids pointless busy-waiting between threads;
  IMMEDIATE makes read-modify-write methods safe (a deferred transaction that reads and then writes fails instantly with
  SQLITE_BUSY_SNAPSHOT in WAL mode, ignoring ``busy_timeout``).  ``busy_timeout`` still covers other processes.
* Pydantic models are stored as JSON text.  Only what we filter/sort on gets its own column: session recency, message
  ``seq`` (messages created within the same second must keep their order) and the document/message status.  Both are
  always written from the same model object, so they cannot drift.  ``DocumentInfo.pi_doc_id`` is excluded from
  serialisation by the model, hence its own column.
* JSON is written with ``ensure_ascii`` (the default) on purpose: a lone surrogate in model output or PDF text would
  make a UTF-8 text bind fail and lose the answer, whereas the escaped form always round-trips.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, TypeVar

from pydantic import BaseModel

from .models import ContextPage, DocumentInfo, EvalScores, Message, Session

log = logging.getLogger("reportlens.store")

DEFAULT_TITLE = "New chat"
TITLE_MAX_CHARS = 120
BUSY_TIMEOUT_S = 30.0
MAX_IDLE_CONNECTIONS = 8
INTERRUPTED_INDEXING_ERROR = "Indexing was interrupted - please upload the document again."
INTERRUPTED_ANSWER_ERROR = "The answer was interrupted."

@dataclass(frozen=True)
class UsageSnapshot:
    """What the stored rows say was spent (read-only view for the spend budget, see reportlens/limits.py)."""
    answer_cost_usd: float          # sum of the known `usage.cost_usd` of the assistant messages
    unpriced_answers: int           # finished answers whose cost is unknown (model missing from the price table)
    unpriced_failed: int            # assistant messages that ended in an error and recorded no usage at all
    index_keys: tuple[str, ...]     # one key per distinct paid-for index ("new chat with this document" shares its source's key)
    evaluations: int                # answers whose RAGAS scoring ran to a result (done / partial / failed)


# Shared by every Store in the process, whichever file it points at: write volume is tiny and one lock keeps it simple.
_WRITE_LOCK = threading.RLock()

# Titles are one-line sidebar labels: control characters and newlines (a pasted question) collapse to single spaces.
_TITLE_JUNK = re.compile(r"[\s\x00-\x1f\x7f]+")

# Append-only: schema version N == the first N entries applied (tracked in PRAGMA user_version).  Never edit a released
# entry - add a new one.
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (  # v1
        """CREATE TABLE sessions (
               id          TEXT PRIMARY KEY,
               title       TEXT NOT NULL,
               created_at  TEXT NOT NULL,
               updated_at  TEXT NOT NULL,
               updated_seq INTEGER NOT NULL   -- strict recency order; updated_at only has 1 s resolution
           )""",
        "CREATE INDEX idx_sessions_updated_seq ON sessions (updated_seq)",
        """CREATE TABLE documents (
               session_id TEXT PRIMARY KEY REFERENCES sessions (id) ON DELETE CASCADE,
               status     TEXT NOT NULL,
               pi_doc_id  TEXT,
               data       TEXT NOT NULL
           )""",
        """CREATE TABLE messages (
               seq        INTEGER PRIMARY KEY AUTOINCREMENT,
               id         TEXT NOT NULL UNIQUE,
               session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
               role       TEXT NOT NULL,
               status     TEXT NOT NULL,
               created_at TEXT NOT NULL,
               data       TEXT NOT NULL
           )""",
        "CREATE INDEX idx_messages_session ON messages (session_id, seq)",
        # Large page texts live apart from the message so listing a conversation stays cheap.
        """CREATE TABLE message_contexts (
               message_id TEXT PRIMARY KEY REFERENCES messages (id) ON DELETE CASCADE,
               data       TEXT NOT NULL
           )""",
    ),
)

# state per contract 4.5: no document -> empty | failed | indexing | ready with >=1 user message -> locked, else ready.
_SESSION_SELECT = """
SELECT s.id, s.title, s.created_at, s.updated_at,
       d.data AS doc_data, d.pi_doc_id AS pi_doc_id,
       CASE
           WHEN d.session_id IS NULL    THEN 'empty'
           WHEN d.status = 'failed'     THEN 'failed'
           WHEN d.status = 'indexing'   THEN 'indexing'
           WHEN EXISTS (SELECT 1 FROM messages u WHERE u.session_id = s.id AND u.role = 'user') THEN 'locked'
           ELSE 'ready'
       END AS state,
       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count
FROM sessions s
LEFT JOIN documents d ON d.session_id = s.id
"""

# Next value of the recency counter.  Only ever evaluated inside a write transaction, so MAX()+1 cannot collide.
_NEXT_SEQ = "(SELECT COALESCE(MAX(updated_seq), 0) + 1 FROM sessions)"

_M = TypeVar("_M", bound=BaseModel)


class SessionNotFound(LookupError):
    """A row that hangs off a session was written for a session that does not exist (e.g. deleted a moment ago)."""


def new_id() -> str:
    """32-hex id used for sessions, messages and documents."""
    return uuid.uuid4().hex


def now_iso() -> str:
    """UTC timestamp in the contract format, e.g. 2026-10-07T17:45:03Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_title(title: str) -> str:
    return _TITLE_JUNK.sub(" ", title).strip()[:TITLE_MAX_CHARS].rstrip()


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _load_model(cls: type[_M], text: str, extra: Optional[dict[str, Any]] = None) -> Optional[_M]:
    """Parse a stored JSON blob.  A row we cannot read (schema drift, manual edits) is logged and skipped so one bad
    row cannot take the whole sidebar or conversation down with it."""
    try:
        raw = json.loads(text)
        if extra:
            raw.update(extra)
        return cls.model_validate(raw)
    except (ValueError, TypeError, AttributeError) as exc:   # JSONDecodeError and ValidationError are ValueErrors
        log.error("Skipping unreadable %s row: %s", cls.__name__, exc)
        return None


def _load_document(text: Optional[str], pi_doc_id: Optional[str]) -> Optional[DocumentInfo]:
    if text is None:
        return None
    return _load_model(DocumentInfo, text, {"pi_doc_id": pi_doc_id})


def _session_from_row(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        title=row["title"],
        state=row["state"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        document=_load_document(row["doc_data"], row["pi_doc_id"]),
        message_count=row["message_count"],
    )


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT (autocommit-mode connection: we own transaction control)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _migrate(conn: sqlite3.Connection) -> None:
    version = _user_version(conn)
    if version > len(_MIGRATIONS):
        raise RuntimeError(
            f"Database schema v{version} is newer than this version of ReportLens understands (v{len(_MIGRATIONS)})."
        )
    for target in range(version + 1, len(_MIGRATIONS) + 1):
        with _transaction(conn):
            if _user_version(conn) >= target:   # another process/thread migrated while we waited for the lock
                continue
            for statement in _MIGRATIONS[target - 1]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target}")   # PRAGMA cannot be parameterised; target is our own int
        log.info("Migrated database to schema v%d", target)


class Store:
    """Thread-safe SQLite store.  All methods may be called from any thread; see the module docstring."""

    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._idle: list[sqlite3.Connection] = []
        self._idle_lock = threading.Lock()
        conn = self._new_connection()
        try:
            with _WRITE_LOCK:   # switching journal mode needs exclusive access, so never race another opener
                mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                if str(mode).lower() != "wal":
                    log.warning("SQLite journal mode is %r, not WAL; concurrent access may be slower", mode)
                _migrate(conn)
        except BaseException:
            conn.close()
            raise
        self._checkin(conn)

    # ----------------------------------------------------------------------------------------------- plumbing
    def _new_connection(self) -> sqlite3.Connection:
        # isolation_level=None: no implicit transactions; _transaction() issues BEGIN/COMMIT explicitly.
        # check_same_thread=False because pooled connections move between threads (never used by two at once).
        conn = sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_S, isolation_level=None, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")      # per connection, off by default
            conn.execute("PRAGMA synchronous = NORMAL")   # safe with WAL; fsync happens at checkpoints
        except BaseException:
            conn.close()
            raise
        return conn

    def _checkout(self) -> sqlite3.Connection:
        with self._idle_lock:
            if self._closed:
                raise RuntimeError("Store is closed")
            if self._idle:
                return self._idle.pop()
        return self._new_connection()

    def _checkin(self, conn: sqlite3.Connection) -> None:
        with self._idle_lock:
            # A connection still inside a transaction (failed ROLLBACK) is in an unknown state: never reuse it.
            if not self._closed and not conn.in_transaction and len(self._idle) < MAX_IDLE_CONNECTIONS:
                self._idle.append(conn)
                return
        conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._checkout()
        try:
            yield conn
        finally:
            self._checkin(conn)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._read() as conn, _WRITE_LOCK, _transaction(conn):
            yield conn

    def close(self) -> None:
        """Release the database file (needed on Windows before the data dir can be deleted).  Idempotent; connections
        that are checked out right now are closed as their calls finish."""
        with self._idle_lock:
            if self._closed:
                return
            self._closed = True
            idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()

    @staticmethod
    def _require_session(conn: sqlite3.Connection, sid: str) -> None:
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone() is None:
            raise SessionNotFound(sid)

    @staticmethod
    def _bump(conn: sqlite3.Connection, sid: str) -> None:
        conn.execute(
            f"UPDATE sessions SET updated_at = ?, updated_seq = {_NEXT_SEQ} WHERE id = ?", (now_iso(), sid)
        )

    # ----------------------------------------------------------------------------------------------- sessions
    def create_session(self, title: str = DEFAULT_TITLE) -> Session:
        title = _clean_title(title) or DEFAULT_TITLE
        sid, now = new_id(), now_iso()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at, updated_seq) "
                f"VALUES (?, ?, ?, ?, {_NEXT_SEQ})",
                (sid, title, now, now),
            )
        return Session(id=sid, title=title, state="empty", created_at=now, updated_at=now)

    def get_session(self, sid: str) -> Optional[Session]:
        with self._read() as conn:
            row = conn.execute(f"{_SESSION_SELECT} WHERE s.id = ?", (sid,)).fetchone()
        return _session_from_row(row) if row else None

    def list_sessions(self) -> list[Session]:
        """Most recently active first.  One query: state and message_count are computed in SQL."""
        with self._read() as conn:
            rows = conn.execute(f"{_SESSION_SELECT} ORDER BY s.updated_seq DESC").fetchall()
        return [_session_from_row(r) for r in rows]

    def rename_session(self, sid: str, title: str) -> None:
        """Trims, collapses whitespace and caps the title at 120 chars.  Does not count as activity (the session keeps
        its place in the sidebar).  Unknown ids are a no-op.  Raises ValueError for an empty title."""
        clean = _clean_title(title)
        if not clean:
            raise ValueError("Title must not be empty")
        with self._write() as conn:
            conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (clean, sid))

    def touch_session(self, sid: str) -> None:
        """Mark the session as just used (moves it to the top of list_sessions).  Unknown ids are a no-op."""
        with self._write() as conn:
            self._bump(conn, sid)

    def delete_session(self, sid: str) -> bool:
        """Delete the session and, by cascade, its document, messages and contexts.  Files are the service's job."""
        with self._write() as conn:
            return conn.execute("DELETE FROM sessions WHERE id = ?", (sid,)).rowcount > 0

    # ----------------------------------------------------------------------------------------------- document
    @staticmethod
    def _write_document(conn: sqlite3.Connection, sid: str, doc: DocumentInfo) -> None:
        conn.execute(
            """INSERT INTO documents (session_id, status, pi_doc_id, data) VALUES (?, ?, ?, ?)
               ON CONFLICT (session_id) DO UPDATE
               SET status = excluded.status, pi_doc_id = excluded.pi_doc_id, data = excluded.data""",
            (sid, doc.status, doc.pi_doc_id, _dumps(doc.model_dump(mode="json"))),
        )

    def put_document(self, sid: str, doc: DocumentInfo) -> None:
        """Insert or replace the session's document.  Fills an empty ``created_at`` on `doc` in place.
        Raises SessionNotFound if the session does not exist."""
        if not doc.created_at:
            doc.created_at = now_iso()
        with self._write() as conn:
            self._require_session(conn, sid)
            self._write_document(conn, sid, doc)
            self._bump(conn, sid)

    def get_document(self, sid: str) -> Optional[DocumentInfo]:
        with self._read() as conn:
            row = conn.execute("SELECT data, pi_doc_id FROM documents WHERE session_id = ?", (sid,)).fetchone()
        return _load_document(row["data"], row["pi_doc_id"]) if row else None

    def update_document(self, sid: str, **fields: Any) -> Optional[DocumentInfo]:
        """Partial update; returns the new document, or None if the session has none.  Raises ValueError for names that
        are not DocumentInfo fields and (pydantic's ValidationError is a ValueError) for values the model rejects."""
        unknown = fields.keys() - DocumentInfo.model_fields.keys()
        if unknown:
            raise ValueError(f"Unknown DocumentInfo field(s): {', '.join(sorted(unknown))}")
        with self._write() as conn:
            row = conn.execute("SELECT data, pi_doc_id FROM documents WHERE session_id = ?", (sid,)).fetchone()
            current = _load_document(row["data"], row["pi_doc_id"]) if row else None
            if current is None:
                return None
            # model_dump() omits pi_doc_id (excluded field), so carry it over explicitly.
            updated = DocumentInfo.model_validate({**current.model_dump(), "pi_doc_id": current.pi_doc_id, **fields})
            self._write_document(conn, sid, updated)
        return updated

    # ----------------------------------------------------------------------------------------------- messages
    @staticmethod
    def _put_contexts(conn: sqlite3.Connection, mid: str, contexts: list[ContextPage]) -> bool:
        """Upsert; False when the message does not exist (nothing is written)."""
        payload = _dumps([c.model_dump(mode="json") for c in contexts])
        return conn.execute(
            """INSERT INTO message_contexts (message_id, data) SELECT id, ? FROM messages WHERE id = ?
               ON CONFLICT (message_id) DO UPDATE SET data = excluded.data""",
            (payload, mid),
        ).rowcount > 0

    def add_message(self, msg: Message, contexts: Optional[list[ContextPage]] = None) -> None:
        """Append a message (and optionally its contexts, atomically) and mark the session as active.  Fills an empty
        ``created_at`` on `msg` in place.  Raises SessionNotFound if the session does not exist, sqlite3.IntegrityError
        if the message id is already taken."""
        if not msg.created_at:
            msg.created_at = now_iso()
        with self._write() as conn:
            self._require_session(conn, msg.session_id)
            conn.execute(
                "INSERT INTO messages (id, session_id, role, status, created_at, data) VALUES (?, ?, ?, ?, ?, ?)",
                (msg.id, msg.session_id, msg.role, msg.status, msg.created_at, _dumps(msg.model_dump(mode="json"))),
            )
            if contexts is not None:
                self._put_contexts(conn, msg.id, contexts)
            self._bump(conn, msg.session_id)

    @staticmethod
    def _write_message(conn: sqlite3.Connection, msg: Message) -> bool:
        return conn.execute(
            "UPDATE messages SET role = ?, status = ?, created_at = ?, data = ? WHERE id = ? AND session_id = ?",
            (msg.role, msg.status, msg.created_at, _dumps(msg.model_dump(mode="json")), msg.id, msg.session_id),
        ).rowcount > 0

    def update_message(self, msg: Message) -> None:
        """Overwrite a stored message (keeps its position).  Deliberately does not touch the session's recency: late
        updates such as RAGAS scores must not reorder the sidebar.  A message that is gone (its session was deleted
        while the answer streamed) is ignored, not an error."""
        with self._write() as conn:
            if not self._write_message(conn, msg):
                log.debug("update_message: message %s of session %s no longer exists", msg.id, msg.session_id)

    def set_contexts(self, mid: str, contexts: list[ContextPage]) -> None:
        """Replace the pages the agent read for message `mid`.  Ignored if the message no longer exists."""
        with self._write() as conn:
            if not self._put_contexts(conn, mid, contexts):
                log.debug("set_contexts: message %s no longer exists", mid)

    def get_contexts(self, mid: str) -> list[ContextPage]:
        with self._read() as conn:
            row = conn.execute("SELECT data FROM message_contexts WHERE message_id = ?", (mid,)).fetchone()
        if row is None:
            return []
        try:
            return [ContextPage.model_validate(item) for item in json.loads(row["data"])]
        except (ValueError, TypeError) as exc:
            log.error("Unreadable contexts for message %s: %s", mid, exc)
            return []

    def get_message(self, sid: str, mid: str) -> Optional[Message]:
        """None when the message does not exist *or belongs to another session*."""
        with self._read() as conn:
            row = conn.execute("SELECT data FROM messages WHERE id = ? AND session_id = ?", (mid, sid)).fetchone()
        return _load_model(Message, row["data"]) if row else None

    def list_messages(self, sid: str) -> list[Message]:
        """Chronological (insertion order: ``seq`` is monotonic even when created_at ties)."""
        with self._read() as conn:
            rows = conn.execute("SELECT data FROM messages WHERE session_id = ? ORDER BY seq", (sid,)).fetchall()
        return [m for m in (_load_model(Message, r["data"]) for r in rows) if m is not None]

    # ----------------------------------------------------------------------------------------------- recovery
    def recover_interrupted(self) -> int:
        """Call once at startup: nothing is running yet, so anything still 'indexing' / 'streaming' belongs to a process
        that died.  Marks those documents failed and those messages error; returns the number of rows changed."""
        changed = 0
        with self._write() as conn:
            stuck_docs = conn.execute(
                "SELECT session_id, data, pi_doc_id FROM documents WHERE status = 'indexing'"
            ).fetchall()
            for row in stuck_docs:
                doc = _load_document(row["data"], row["pi_doc_id"])
                if doc is None:
                    continue
                doc.status, doc.stage, doc.error = "failed", "failed", INTERRUPTED_INDEXING_ERROR
                self._write_document(conn, row["session_id"], doc)
                changed += 1
            for row in conn.execute("SELECT data FROM messages WHERE status = 'streaming'").fetchall():
                msg = _load_model(Message, row["data"])
                if msg is None:
                    continue
                msg.status, msg.error = "error", INTERRUPTED_ANSWER_ERROR   # partial content is kept for the UI
                self._write_message(conn, msg)
                changed += 1
        if changed:
            log.warning("Recovered %d interrupted document/message row(s) after an unclean shutdown", changed)
        return changed

    def skip_interrupted_evaluations(self) -> int:
        """Call once at startup, next to recover_interrupted(): a finished answer whose scores are still 'pending' / 'running'
        was being scored by a process that died.  Marks them skipped("interrupted") (the UI offers to run them again);
        returns the number of messages changed."""
        changed = 0
        with self._write() as conn:
            for row in conn.execute("SELECT data FROM messages WHERE role = 'assistant' AND status != 'streaming'").fetchall():
                msg = _load_model(Message, row["data"])
                if msg is None or msg.evaluation is None or msg.evaluation.status not in ("pending", "running"):
                    continue
                ev = msg.evaluation
                msg.evaluation = EvalScores(status="skipped", skipped_reason="interrupted",
                                            n_contexts_input=ev.n_contexts_input, n_contexts_scored=ev.n_contexts_scored)
                self._write_message(conn, msg)
                changed += 1
        if changed:
            log.warning("Marked the scores of %d answer(s) as interrupted after an unclean shutdown", changed)
        return changed

    # ----------------------------------------------------------------------------------------------- spend estimate
    def usage_snapshot(self, sid: Optional[str] = None, *, exclude_index_keys: Iterable[str] = ()) -> UsageSnapshot:
        """Totals for the spend budget over every session, or only `sid`.  A document counts once per index it paid for
        (key = PageIndex doc id, or the document row id while that is not known yet); a failed upload counts only when it got
        as far as spending something (progress > 0).  Keys in `exclude_index_keys` were already moved to the ledger."""
        where, params = ("WHERE session_id = ?", (sid,)) if sid else ("", ())
        with self._read() as conn:
            docs = conn.execute(f"SELECT status, pi_doc_id, data FROM documents {where}", params).fetchall()
            msgs = conn.execute(f"SELECT status, data FROM messages WHERE role = 'assistant' {'AND session_id = ?' if sid else ''}", params).fetchall()
        excluded = set(exclude_index_keys)
        keys: dict[str, None] = {}
        for row in docs:
            doc = _load_document(row["data"], row["pi_doc_id"])
            if doc is None or (row["status"] == "failed" and not doc.progress > 0):
                continue
            key = doc.pi_doc_id or f"doc:{doc.id}"
            if key not in excluded:
                keys[key] = None
        cost, unpriced, failed, evaluations = 0.0, 0, 0, 0
        for row in msgs:
            msg = _load_model(Message, row["data"])
            if msg is None:
                continue
            known = msg.usage.cost_usd if msg.usage is not None else None
            if known is not None:
                cost += max(0.0, known)
            elif msg.status in ("answered", "no_sources"):
                unpriced += 1
            elif msg.usage is None and msg.status == "error":      # a running question is covered by the budget's reserve
                failed += 1
            if msg.evaluation is not None and msg.evaluation.status in ("done", "partial", "failed"):
                evaluations += 1
        return UsageSnapshot(cost, unpriced, failed, tuple(keys), evaluations)
