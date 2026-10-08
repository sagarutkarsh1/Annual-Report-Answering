"""Live smoke test: one real index + one real question + one real RAGAS run, end to end, through the real service.

    .venv\\Scripts\\python.exe scripts\\live_smoke.py [--pdf PATH] [--question "..."] [--yes]

This spends real OpenAI money, so it only prints its plan (models, estimated cost range) and exits with code 2 unless you
pass --yes. It never prints the API key. Steps:

  1. free preflight: `models.retrieve` for every configured model (catches typos, missing access, retired models);
  2. index the PDF in a throw-away data directory (the project's data/ is never touched);
  3. ask the question through `ReportLensService.ask` and print the answer, the agent's steps, each citation
     (page, printed folio, section, quote, whether highlight rectangles were found), pages read, tokens, cost and RAGAS scores.

Exit code: 0 = everything worked, 1 = a step failed, 2 = refused / not configured.
Without --question the first question from `<pdf>.facts.json` is used when that file exists (the bundled sample), else a generic one.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PDF = PROJECT_ROOT / "samples" / "sample_annual_report.pdf"
GENERIC_QUESTION = "What were the main highlights of the financial year?"
INDEX_TIMEOUT_S = 25 * 60
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2

# USD for ONE operation on a 308-page report as (low, likely, high), from research/07-openai-models-and-costs.md section 8.
# They are estimates from token counts and list prices, not measurements; the script scales them by page count.
REFERENCE_PAGES = 308
INDEX_COST = {"gpt-5.6-luna": (0.07, 0.26, 0.32), "gpt-6-luna": (0.07, 0.12, 0.32)}
QUESTION_COST = {
    "gpt-6-luna": (0.004, 0.009, 0.022), "gpt-5.6-luna": (0.008, 0.018, 0.046), "gpt-5.6-terra": (0.082, 0.18, 0.46),
    "gpt-6.1-sol": (0.080, 0.175, 0.44), "gpt-5.6-sol": (0.16, 0.35, 0.88), "gpt-6-astra": (0.40, 0.88, 2.20),
}
JUDGE_COST = {
    "gpt-4o-mini": (0.003, 0.007, 0.020), "gpt-4.1-mini": (0.008, 0.018, 0.054), "gpt-4.1": (0.042, 0.088, 0.27),
    "gpt-4o": (0.052, 0.11, 0.34),
}
MIN_SCALE = 0.25                       # a 10-page excerpt still pays for the outline, the answer and the judge

_KEY = re.compile(r"sk-[A-Za-z0-9_\-*.]{3,}")


def say(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def scrub(text: object) -> str:
    """Anything that may echo a credential (OpenAI error bodies quote the start of the key) goes through here first."""
    return _KEY.sub("sk-...", str(text))


def estimate_range(settings: Any, pages: int) -> tuple[Optional[tuple[float, float]], list[str]]:
    """(low, high) USD for index + one question + RAGAS, or None when a model is not in the research tables."""
    scale = max(pages / REFERENCE_PAGES, MIN_SCALE)
    parts = [("indexing", settings.index_model, INDEX_COST, pages / REFERENCE_PAGES),
             ("question", settings.chat_model, QUESTION_COST, scale),
             ("RAGAS scoring", settings.judge_model, JUDGE_COST, 1.0)]
    low = high = 0.0
    notes: list[str] = []
    for label, model, table, factor in parts:
        row = table.get(model)
        if row is None:
            notes.append(f"{label}: no estimate for model '{model}' (not in research/07); check the OpenAI usage dashboard")
            return None, notes
        low, high = low + row[0] * factor, high + row[2] * factor
        notes.append(f"{label}: ${row[0] * factor:.3f} - ${row[2] * factor:.3f} ({model})")
    return (low, high), notes


def default_question(pdf: Path) -> str:
    facts = pdf.with_suffix(".facts.json")
    if facts.is_file():
        try:
            return json.loads(facts.read_text(encoding="utf-8"))[0]["question"]
        except (OSError, ValueError, LookupError, TypeError):
            pass
    return GENERIC_QUESTION


def models_in_use(settings: Any) -> list[tuple[str, str]]:
    """(role, model id) with duplicates removed, in the order they are used."""
    roles = [("index", settings.index_model), ("chat", settings.chat_model), ("question rewrite", settings.question_rewrite_model),
             ("RAGAS judge", settings.judge_model), ("RAGAS embeddings", settings.embedding_model)]
    seen: set[str] = set()
    return [(role, model) for role, model in roles if not (model in seen or seen.add(model))]


def preflight(settings: Any, models: list[tuple[str, str]]) -> bool:
    """Free check that the key can see every model. Prints one line per model; True when all are usable."""
    import openai

    client = openai.OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url, max_retries=1, timeout=30)
    ok = True
    try:
        for role, model in models:
            try:
                info = client.models.retrieve(model)
            except openai.APIStatusError as exc:
                ok = False
                say(f"  FAIL  {role:<17} {model}: HTTP {exc.status_code} {scrub(exc.message)[:150]}")
            except openai.OpenAIError as exc:
                ok = False
                say(f"  FAIL  {role:<17} {model}: {type(exc).__name__}: {scrub(exc)[:150]}")
            else:
                shutdown = getattr(info, "shutdown_date", None)
                say(f"  ok    {role:<17} {model}" + (f"  WARNING: scheduled to shut down on {shutdown}" if shutdown else ""))
    finally:
        client.close()
    return ok


async def ask_question(service: Any, sid: str, question: str) -> tuple[list[tuple[str, dict]], list[str]]:
    events: list[tuple[str, dict]] = []
    problems: list[str] = []
    streamed = 0
    async for name, payload in service.ask(sid, question):
        events.append((name, payload))
        if name == "token":
            streamed += len(payload.get("text", ""))
        elif name in ("step_done", "answer_done", "eval_started", "eval_result", "eval_done"):
            say(f"  [{name}]" + (f" {payload.get('label')}" if name == "step_done" else "") +
                (f" {payload['metric']}={payload['value']}" if name == "eval_result" else ""))
        elif name == "error":
            problems.append(f"{payload.get('code')}: {payload.get('message')}")
    say(f"  streamed {streamed} characters of answer text")
    return events, problems


def report_answer(events: list[tuple[str, dict]], contexts: int) -> list[str]:
    """Prints the final answer, steps, citations, usage and scores; returns the problems found in them."""
    problems: list[str] = []
    done = next((p["message"] for n, p in events if n == "answer_done"), None)
    if done is None:
        return ["no answer_done event: the run never produced an answer"]
    say("\nANSWER (status: %s, %s ms)" % (done["status"], done.get("elapsed_ms")))
    say(done["content"] or "(empty)")
    if done["status"] == "error":
        problems.append(f"answer status is error: {done.get('error')}")
    say("\nSTEPS")
    for step in done.get("steps", []):
        say(f"  {step['id']:<4} {step['label']}  {step.get('elapsed_ms')} ms")
    say("\nCITATIONS")
    for c in done.get("citations", []):
        located = "highlight rects: %d" % len(c["rects"]) if c["rects"] else "highlight rects: none (whole page)"
        say(f"  [{c['id']}] page {c['page']} (printed {c.get('printed_page') or '?'}) cited {c['cited_page']}  "
            f"{' > '.join(c.get('section_path') or []) or '(no section)'}")
        say(f"        {c['match_method']}/{c['quote_source']} score {c['match_score']:.2f}  {located}")
        say(f"        quote: {(c.get('quote') or '(not located)')[:160]}")
    if not done.get("citations") and done["status"] == "answered":
        problems.append("the answer has no citations")
    say(f"\nPAGES READ BY THE AGENT: {contexts}")
    usage = done.get("usage") or {}
    cost = usage.get("cost_usd")
    say("USAGE  model=%s input=%s (cached %s) output=%s (reasoning %s) cost=%s" % (
        usage.get("model"), usage.get("input_tokens"), usage.get("cached_tokens"), usage.get("output_tokens"),
        usage.get("reasoning_tokens"), f"${cost:.4f}" if cost is not None else "unknown (model not in the price table)"))
    scores = next((p["evaluation"] for n, p in events if n == "eval_done"), None)
    say("\nRAGAS")
    if scores is None:
        problems.append("no eval_done event: scoring never finished")
        say("  (no scores)")
        return problems
    for metric in ("faithfulness", "answer_relevancy", "context_precision"):
        value = scores.get(metric)
        say(f"  {metric:<18} {'n/a' if value is None else f'{value:.3f}'}  {scores['errors'].get(metric, '')}")
    say(f"  status {scores['status']}, judge {scores.get('judge_model')}, {scores.get('n_contexts_scored')} of "
        f"{scores.get('n_contexts_input')} pages scored, {scores.get('latency_s')} s")
    if scores["status"] != "done":
        problems.append(f"RAGAS status is {scores['status']}: {scores.get('errors') or scores.get('skipped_reason')}")
    return problems


async def run_pipeline(settings: Any, pdf: Path, question: str) -> int:
    from reportlens import pageindex_compat
    from reportlens.indexer import IndexService
    from reportlens.service import ReportLensService
    from reportlens.store import Store

    workdir = Path(tempfile.mkdtemp(prefix="rl_smoke_"))      # short path: PageIndex paths are long on Windows
    settings = settings.with_(data_dir=workdir / "data")
    store = indexer = service = None
    try:
        pageindex_compat.apply_patches()
        store = Store(settings.db_path)
        indexer = IndexService(settings, store)
        service = ReportLensService(settings, store=store, indexer=indexer)
        sid = service.create_session().id
        upload = workdir / "upload.pdf"
        shutil.copyfile(pdf, upload)
        session = await asyncio.to_thread(service.attach_document, sid, pdf.name, upload)
        say(f"\nINDEXING {pdf.name} ({session.document.page_count} pages)")
        started, last = time.monotonic(), None
        while time.monotonic() - started < INDEX_TIMEOUT_S:
            session = await asyncio.to_thread(service.get_session, sid)
            doc = session.document
            if doc.stage != last:
                last = doc.stage
                say(f"  {time.monotonic() - started:6.1f}s  {doc.stage:<15} {doc.progress:.0%}")
            if doc.status != "indexing":
                break
            await asyncio.sleep(1.0)
        if session.document.status != "ready":
            say(f"  INDEXING FAILED: {scrub(session.document.error or 'timed out')}")
            return EXIT_FAILED
        say(f"  ready: {session.document.node_count} outline nodes in {session.document.index_seconds} s")

        say(f"\nQUESTION: {question}")
        events, problems = await ask_question(service, sid, question)
        contexts = 0
        answer = next((p["message"] for n, p in events if n == "answer_done"), None)
        if answer is not None:
            contexts = len(await asyncio.to_thread(store.get_contexts, answer["id"]))
        problems += report_answer(events, contexts)
        if problems:
            say("\nFAILED:\n  " + "\n  ".join(scrub(p) for p in problems))
            return EXIT_FAILED
        say("\nOK: indexing, answering, citations and scoring all worked.")
        return EXIT_OK
    finally:
        if service is not None:
            await service.aclose()
        if indexer is not None:
            await asyncio.to_thread(indexer.shutdown)
        if store is not None:
            store.close()
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv: Optional[Sequence[str]] = None, environ: Optional[dict] = None) -> int:
    parser = argparse.ArgumentParser(description="Live (paid) end-to-end smoke test of ReportLens against the real OpenAI API.")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF, help="PDF to index (default: the bundled sample report)")
    parser.add_argument("--question", help="question to ask (default: the first one in <pdf>.facts.json, else a generic one)")
    parser.add_argument("--yes", action="store_true", help="really spend money; without it the script only prints its plan")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    from reportlens.config import load_settings
    from reportlens.pdfutil import PdfError, inspect_pdf

    settings = load_settings(environ=environ) if environ is not None else load_settings()
    if settings.demo_mock:
        say("REPORTLENS_DEMO_MOCK is on: this script tests the real API. Unset it and try again.")
        return EXIT_REFUSED
    if not settings.openai_api_key:
        say("No OPENAI_API_KEY found. Add it to .env (see .env.example) and run again.")
        return EXIT_REFUSED
    pdf = args.pdf.expanduser().resolve()
    if not pdf.is_file():
        say(f"PDF not found: {pdf}")
        return EXIT_REFUSED
    try:
        pages = inspect_pdf(pdf).page_count
    except PdfError as exc:
        say(f"The PDF cannot be used: {exc.message}")
        return EXIT_FAILED
    question = (args.question or default_question(pdf)).strip()

    models = models_in_use(settings)
    say("ReportLens live smoke test")
    say(f"  PDF       {pdf} ({pages} pages)")
    say(f"  question  {question}")
    say(f"  endpoint  {'custom base URL (OPENAI_BASE_URL)' if settings.openai_base_url else 'api.openai.com'}")
    say("  models    " + ", ".join(f"{role}={model}" for role, model in models))
    say(f"  chat      protocol={settings.chat_protocol} effort={settings.chat_reasoning_effort or 'model default'}")
    estimate, notes = estimate_range(settings, pages)
    for note in notes:
        say(f"            {note}")
    say("  estimated cost: " + (f"${estimate[0]:.2f} - ${estimate[1]:.2f} for the whole run (a rough estimate from token counts; "
                                "see research/07-openai-models-and-costs.md)" if estimate else "unknown (see above)"))
    if not args.yes:
        say("\nRefusing to spend money without --yes. Re-run with --yes to start (the preflight below is free).")
        return EXIT_REFUSED

    say("\nPREFLIGHT (free: models.retrieve)")
    if not preflight(settings, models):
        say("Preflight failed: fix the model names or key access above (PI_INDEX_MODEL, PI_CHAT_MODEL, RAGAS_JUDGE_MODEL ...).")
        return EXIT_FAILED
    return asyncio.run(run_pipeline(settings, pdf, question))


if __name__ == "__main__":
    sys.path.insert(0, str(PROJECT_ROOT))      # lets `python scripts/live_smoke.py` import reportlens without installing it
    raise SystemExit(main())
