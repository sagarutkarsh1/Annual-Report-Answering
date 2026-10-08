"""Child process that indexes ONE PDF with PageIndex and reports to its parent over a pipe:  python -m reportlens.indexing_worker

Why a process of its own: the PageIndex SDK imports litellm (~150 MB), parses every character of every page (hundreds of MB for a
300-page annual report) and leaves what it allocated behind in the heap.  On a 512 MB host that decides between "works" and
"killed".  In a short-lived child all of it goes back to the operating system the moment indexing ends, and the web server (which
never imports the SDK's indexing half) stays small.  See docs/DEPLOY.md for the measurements.

Protocol (one JSON object per line):
  parent -> child, line 1 on stdin:  {"settings": <config.settings_to_json>, "session_id", "pdf_path", "mode": "flash"|"standard"}
                 then stdin stays open; its closing (the parent died or gave up) ends this process.
  child -> parent, on stdout:
      {"ev": "page_text", "event": "start"|"done"}      text extraction started / finished
      {"ev": "before", "kind": "call"|"describe"}       a model call is about to start
      {"ev": "after"}                                   a model call has finished
      {"ev": "result", "doc_id", "name", "tree", "description", "mem"}   mem: this process's resident/peak memory and loaded libraries (for the log)
      {"ev": "fault", "kind", "message"}                classified failure (indexer.translate_error), already fit for the user
Everything else the SDK prints goes to stderr (the host's log).  The API key arrives over the pipe, never on a command line.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from typing import Any

PROTOCOL_VERSION = 1


class _Emitter:
    def __init__(self, stream: Any):
        self._stream = stream
        self._lock = threading.Lock()

    def __call__(self, **event: Any) -> None:
        line = json.dumps(event, separators=(",", ":"))
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


class RemoteJob:
    """Stands in for indexer._Job inside the SDK's counting shims: forwards what they report."""

    def __init__(self, emit: _Emitter):
        self._emit = emit

    def before_call(self, kind: str) -> None:
        self._emit(ev="before", kind=kind)

    def after_call(self) -> None:
        self._emit(ev="after")


def start_parent_watch() -> None:
    """Exit when the parent goes away (Linux: the host kills our parent on restart/sleep; its pipe then reads EOF).  Not started on
    Windows (development machines): a thread blocked in a synchronous read of the stdin pipe stalls other threads' imports there
    (numpy's import hung behind it), and the parent kills its children explicitly anyway."""
    if os.name != "nt":
        threading.Thread(target=_exit_when_parent_goes, name="parent-watch", daemon=True).start()


def _exit_when_parent_goes() -> None:
    """Block on stdin; EOF means the parent closed the pipe or died: nobody wants this job any more."""
    try:
        sys.stdin.read()
    finally:
        os._exit(3)


def prefer_to_be_killed_first() -> None:
    """Out of memory on a small host: let the kernel's OOM killer pick this short-lived child, not the web server (Linux only; a
    process may always raise its own score)."""
    try:
        with open("/proc/self/oom_score_adj", "w", encoding="ascii") as handle:
            handle.write("900")
    except OSError:
        pass


def run(request: dict, emit: _Emitter) -> int:
    from reportlens import indexer, pageindex_compat as compat
    from reportlens.config import settings_from_json

    settings = settings_from_json(request["settings"])
    mode = request.get("mode", "flash")
    try:
        if settings.lite_llm:
            from reportlens import lite_llm

            lite_llm.install()                              # before the counters: they wrap whatever is bound at that moment
        indexer.install_llm_counters()
        compat.set_page_text_listener(lambda event: emit(ev="page_text", event=event))
        indexer._current_job.set(RemoteJob(emit))          # the shims look the job up in this ContextVar
        client = compat.make_client(settings, request["session_id"], for_indexing=True, **({} if mode == "flash" else {"mode": "standard"}))
        pdf = request["pdf_path"]
        result = client.submit_document(pdf) if mode == "flash" else client.submit_document(pdf, mode="standard")
        doc_id = result["doc_id"]
        tree = client.get_tree(doc_id, node_summary=True, include_text=False)["result"]
        meta = client.get_document(doc_id)
        from reportlens.lowmem import memory_summary

        emit(ev="result", doc_id=doc_id, name=str(result.get("name") or meta.get("name") or ""), tree=tree,
             description=meta.get("description"), mem=memory_summary())
        return 0
    except BaseException as exc:  # noqa: BLE001 - whatever happened is translated and handed to the parent
        fault = indexer.translate_error(exc, settings)
        if fault.kind in ("other", "storage"):
            logging.getLogger("reportlens.indexing_worker").error("indexing failed", exc_info=exc)
        emit(ev="fault", kind=fault.kind, message=fault.message)
        return 0


def main() -> int:
    protocol = sys.stdout
    sys.stdout = sys.stderr                                # stray prints (SDK, libraries) must never corrupt the protocol
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stderr)
    first = sys.stdin.readline()
    if not first.strip():
        return 2
    request = json.loads(first)
    prefer_to_be_killed_first()
    threading.Thread(target=_exit_when_parent_goes, name="parent-watch", daemon=True).start()
    return run(request, _Emitter(protocol))


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)           # do not wait for the SDK's executor / event-loop threads; the result has been sent
