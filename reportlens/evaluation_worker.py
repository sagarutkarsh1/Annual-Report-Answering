"""Child process that scores answers with RAGAS and reports to its parent over a pipe:  python -m reportlens.evaluation_worker

Same idea as `indexing_worker`: importing RAGAS, its judge client and the metrics costs ~100 MB that the web process would keep for
ever after the first scoring.  On a 512 MB host that decides whether the next report can be indexed at all, so the scoring runs in a
short-lived child and the memory goes back to the operating system when it ends.

Usually it scores ONE answer and ends.  When the parent has more answers coming (a set of questions is being answered) it keeps the child
alive and sends the next request on the same pipe: importing RAGAS is most of a scoring's time on a 0.1 CPU host, and a child that is
already loaded skips it.  The parent closes stdin when it is done (or after a short idle time); that ends this process.

Protocol (one JSON object per line):
  parent -> child, a line on stdin:  {"settings": <config.settings_to_json>, "question", "answer", "contexts": [{"page", "text"}, ...]}
                 then stdin stays open: either the next request follows, or its closing (the parent is done, died or gave up) ends
                 this process; closing it while a scoring is running abandons that scoring at once.
  child -> parent, on stdout:
      {"ev": "metric", "metric", "value", "error"}      one metric finished (faithfulness | answer_relevancy | context_precision)
      {"ev": "result", "scores": <EvalScores json>, "mem": "<memory summary>"}
Everything else the libraries print goes to stderr (the host's log).  The API key arrives over the pipe, never on a command line.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
from typing import Optional

from reportlens.indexing_worker import _Emitter, prefer_to_be_killed_first


async def _score(request: dict, emit: _Emitter) -> None:
    from reportlens.config import settings_from_json
    from reportlens.lowmem import memory_summary, stub_datasets_for_ragas
    from reportlens.models import ContextPage

    settings = settings_from_json(request["settings"])
    if settings.low_memory:
        stub_datasets_for_ragas()
    from reportlens.evaluation import Evaluator          # the heavy import (RAGAS, ~100 MB)

    contexts = [ContextPage.model_validate(c) for c in request["contexts"]]
    evaluator = Evaluator(settings)
    try:
        scores = await evaluator.evaluate(request["question"], request["answer"], contexts,
                                          on_metric=lambda metric, value, error: emit(ev="metric", metric=metric, value=value, error=error))
    finally:
        await evaluator.aclose()
    emit(ev="result", scores=scores.model_dump(mode="json"), mem=memory_summary())


class _Requests:
    """The requests on stdin, one per line.  Off Windows a thread reads them, so a closed pipe (the parent died or gave up) ends the
    process even in the middle of a scoring; on Windows (development machines: a thread blocked on the pipe stalls other threads'
    imports) the main thread reads between scorings, as the one-shot worker always did."""

    def __init__(self) -> None:
        self.busy = False
        self._inbox: "queue.Queue[Optional[str]]" = queue.Queue()
        self._threaded = os.name != "nt"
        if self._threaded:
            threading.Thread(target=self._pump, name="requests", daemon=True).start()

    def _pump(self) -> None:
        try:
            for line in sys.stdin:
                if line.strip():
                    self._inbox.put(line)
        finally:
            if self.busy:
                os._exit(3)                       # nobody wants this scoring any more
            self._inbox.put(None)

    def next(self) -> Optional[str]:
        """The next request line, or None when the parent has closed the pipe."""
        if self._threaded:
            return self._inbox.get()
        while True:
            line = sys.stdin.readline()
            if not line:
                return None
            if line.strip():
                return line


def main() -> int:
    protocol = sys.stdout
    sys.stdout = sys.stderr
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stderr)
    requests = _Requests()
    line = requests.next()
    if line is None:
        return 2
    prefer_to_be_killed_first()
    emit = _Emitter(protocol)
    while line is not None:
        requests.busy = True
        try:
            asyncio.run(_score(json.loads(line), emit))
        except Exception:  # noqa: BLE001 - the parent turns a missing result into a failed score
            logging.getLogger("reportlens.evaluation_worker").exception("scoring crashed")
            return 1
        requests.busy = False
        try:
            from reportlens.lowmem import trim_memory

            trim_memory()                         # a child kept for the next answer hands this one's buffers back first
        except Exception:  # noqa: BLE001 - only a memory saver
            pass
        line = requests.next()
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
