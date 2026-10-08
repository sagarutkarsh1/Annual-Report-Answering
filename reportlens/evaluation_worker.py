"""Child process that scores ONE answer with RAGAS and reports to its parent over a pipe:  python -m reportlens.evaluation_worker

Same idea as `indexing_worker`: importing RAGAS, its judge client and the metrics costs ~100 MB that the web process would keep for
ever after the first scoring.  On a 512 MB host that decides whether the next report can be indexed at all, so the scoring runs in a
short-lived child and the memory goes back to the operating system when it ends.

Protocol (one JSON object per line):
  parent -> child, line 1 on stdin:  {"settings": <config.settings_to_json>, "question", "answer", "contexts": [{"page", "text"}, ...]}
                 then stdin stays open; its closing (the parent died or gave up) ends this process.
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
import sys
from typing import Any

from reportlens.indexing_worker import _Emitter, prefer_to_be_killed_first, start_parent_watch


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


def main() -> int:
    protocol = sys.stdout
    sys.stdout = sys.stderr
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stderr)
    first = sys.stdin.readline()
    if not first.strip():
        return 2
    request: Any = json.loads(first)
    prefer_to_be_killed_first()
    start_parent_watch()
    try:
        asyncio.run(_score(request, _Emitter(protocol)))
    except Exception:  # noqa: BLE001 - the parent turns a missing result into a failed score
        logging.getLogger("reportlens.evaluation_worker").exception("scoring crashed")
        return 1
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
