# ReportLens

Ask questions about **one annual-report PDF** and get answers with page-level citations. Click a citation and the PDF opens
beside the chat, scrolled to the page, with the supporting passage highlighted. Every answer is then scored with RAGAS
(faithfulness, answer relevancy, context precision).

It runs on your machine at `http://127.0.0.1:8000`. Retrieval is [PageIndex](https://github.com/VectifyAI/PageIndex)
(a table-of-contents tree that an LLM navigates, no vector database), the models are OpenAI's, and the web layer is FastAPI
serving a dependency-free browser UI.

## Quick start (Windows PowerShell)

```powershell
cd "C:\Users\ayush\Annual Report Answering"
copy .env.example .env ; notepad .env          # put your key after OPENAI_API_KEY=   (skip this for the demo)
.\run.ps1                                      # then open http://127.0.0.1:8000     (.\run.ps1 -Demo needs no key)
```

**The venv rule.** Always run Python through the project's own virtual environment, `.\.venv\Scripts\python.exe`
(`run.ps1` does this for you). Do not use whatever `python` is on your PATH, do not activate another environment, and keep the
project in a short path: the venv root must stay under about 105 characters or the `litellm` install fails on Windows, and the
PageIndex store inside `data\` builds very long paths (see Troubleshooting). Without the launcher:

```powershell
$env:PYTHONIOENCODING = "utf-8"
.\.venv\Scripts\python.exe -m reportlens --port 8000 --open
```

`python -m reportlens --help` lists the options: `--host`, `--port`, `--demo`, `--data-dir`, `--open`, `--log-level`.
`Ctrl+C` stops the server. Use a current Chrome or Edge (125+); the PDF viewer needs a modern browser.

## Settings (`.env`)

Precedence: real environment variables, then `.env`, then the defaults below. Every value is optional except
`OPENAI_API_KEY` (unless you run the demo). The file is git-ignored; `.env.example` is the template.

| Key | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | none | Your OpenAI key. The only secret. Never shown by the API or the UI. |
| `OPENAI_BASE_URL` | OpenAI | Only for a proxy or gateway (and the offline mock). |
| `PAGEINDEX_MODE` | `local` | Only `local` is implemented (PageIndex "Flash" mode, needs only the OpenAI key). `cloud` is reserved. Not in `.env.example`. |
| `PI_INDEX_MODEL` | `gpt-5.6-luna` | Writes the section summaries while indexing (cheap model is enough). |
| `PI_CHAT_MODEL` | `gpt-5.6-sol` | The agent that reads the outline, picks pages and writes the answer. |
| `PI_CHAT_REASONING_EFFORT` | `medium` | `none`, `low`, `medium`, `high`, `xhigh` or `max`. Empty = the model's default; leave empty for non-reasoning models. |
| `PI_CHAT_PROTOCOL` | `responses` | `responses` (needed for gpt-5.6/gpt-6 with tools) or `chat` (fallback lane). |
| `PI_CHAT_MAX_TURNS` | `12` | Cap on the agent's tool-call rounds per question. |
| `PI_INDEX_SUMMARY_CONCURRENCY` | `16` | Parallel summary calls while indexing. Lower it (8) if you hit 429 errors. |
| `PI_INDEX_FALLBACK_STANDARD` | `true` | A PDF without bookmarks and over 10 pages is refused by Flash; `true` retries with the slower, costlier LLM-built tree. |
| `QUESTION_REWRITE_MODEL` | `gpt-4.1-mini` | Turns a follow-up ("and for 2025?") into a standalone question. Used for scoring only. |
| `EVAL_ENABLED` | `true` | `false` skips RAGAS (no scores, no judge cost). |
| `RAGAS_JUDGE_MODEL` | `gpt-4.1` | The judge. Non-reasoning, different family from the answerer. `gpt-4.1-mini` was tried and rejected: on a long answer it degenerated into endless `0000` output during the faithfulness step. |
| `RAGAS_JUDGE_REASONING_EFFORT` | empty | Only if you choose a reasoning judge. |
| `RAGAS_JUDGE_MAX_TOKENS` | `4096` | Output cap per judge call. |
| `RAGAS_EMBEDDING_MODEL` | `text-embedding-3-small` | Used by answer relevancy. Keep it fixed so scores stay comparable. |
| `EVAL_MAX_CONTEXTS` | `12` | Max pages passed to RAGAS (context precision costs one judge call per page). |
| `EVAL_MAX_CHARS_PER_CONTEXT` | `8000` | Page text is cut to this length for scoring. |
| `EVAL_CONCURRENCY` | `8` | Parallel judge calls. Not in `.env.example`. |
| `REPORTLENS_HOST` | `127.0.0.1` | Interface to bind. Keep it local (no login, see Security). |
| `REPORTLENS_PORT` | `8000` | Port. |
| `REPORTLENS_DATA_DIR` | `./data` | Database, uploaded PDFs, indexes. Relative paths resolve against the project folder. |
| `MAX_UPLOAD_MB` | `100` | Larger uploads are refused (HTTP 413) while streaming, before they fill the disk. |
| `MAX_PAGES` | `1200` | Longer PDFs are refused. |
| `HISTORY_TURNS` | `6` | Previous question/answer pairs the agent sees on follow-ups. |
| `REPORTLENS_DEMO_MOCK` | `0` | `1` = the offline demo (below). Same as `--demo`. |

Model names follow the PageIndex documentation and OpenAI's current catalogue; check that your key can use them with
`scripts\live_smoke.py` (its first step is a free model-access check).

## Demo mode (no key, no cost)

```powershell
.\run.ps1 -Demo           # or: .\.venv\Scripts\python.exe -m reportlens --demo
```

Starts a fake OpenAI server inside the process and points everything at it with a dummy key, so nothing leaves your machine
and nothing is billed. You can click through the whole product: upload, indexing progress, chat, citations, highlights, the
RAGAS card. **Answers in demo mode are canned extracts copied from the pages the mock "agent" was shown, and the scores are
meaningless.** Use it to learn the UI and to check the plumbing, never to judge answer quality. Indexing the 60-page
sample (`samples\sample_annual_report.pdf`) takes about 15 seconds.

## How a question is answered

1. **Upload and index (once per chat).** The PDF is checked (magic bytes, password, scanned or not, page limit), its text is
   read with pdfium, and PageIndex builds a tree of sections from the bookmarks and layout. The index model writes a summary for
   each node. There are no embeddings and no vector store. Progress is shown as stages; time and cost grow with page count.
2. **Ask.** The PageIndex agent (`PI_CHAT_MODEL`) reads the tree (`get_document_structure`), decides which page ranges matter,
   reads them (`get_page_content("88-90")`) and writes the answer. The UI shows these steps live ("Read pages 88-90").
   The answer is streamed token by token.
3. **Cite.** The model marks each claim with a tag naming a page and a short verbatim quote. The tags never reach the
   browser: they become small chips (the file name and "p.89") and each one is resolved on the server to the physical page, the printed folio, the
   section path from the tree ("Strategic report > Financial review") and highlight rectangles.
4. **Score.** After the answer, RAGAS rates it (10 to 60 seconds). The pages the agent actually read are the "retrieved contexts".
   Scores arrive after the answer and never delay it.

One chat holds exactly one document and it is locked after the first question. To use another report, start a new chat; "new chat
with this document" reuses the existing index without re-indexing or paying again.

## Citations and highlighting, and where they stop working

Page numbers in chips and in the viewer are **physical PDF pages** ("Page 89 / 308"). The number printed on the page (the folio,
"87") is detected best-effort and shown in the hover card and the viewer footer, because many reports offset the two.

For each citation the server tries, in order: (1) find the model's quote on the cited page (exact, then fuzzy; also the pages
either side); (2) if there is no usable quote, align the answer sentence to the best-matching span on the page; (3) fall back to
highlighting the whole page, with a visible "passage not located" notice. A fuzzy match below 0.90 is accepted but flagged as
repaired, and a quote whose numbers differ from the matched text is rejected, so a highlight never silently lands on a different figure.

Limits to know about:

- Highlights come from word positions in the PDF's text layer. Tables, multi-column pages and text broken by hyphenation are the
  hard cases; expect some highlights that are a line or two off, and some that fall back to the whole page. The thresholds were set on
  synthetic reports and need calibrating on real ones in UAT.
- Scanned pages, text inside images and charts cannot be quoted or highlighted. Scanned PDFs are refused at upload.
- If the model cites a page it never read, or a page outside the document, the citation is flagged or dropped rather than shown as fact.
- Whether the real models obey the "quote verbatim" instruction has not been measured yet (see UAT).

## What the RAGAS scores mean (and don't)

All three are **estimates made by an AI judge** (`RAGAS_JUDGE_MODEL`), not measured accuracy. They move by a few points between
runs, and are not comparable across judge models.

| Score | What it measures | Caveats |
|---|---|---|
| Faithfulness | Share of the answer's claims that the judge can verify from the pages the agent read. | Does not check the pages against reality. Calculated numbers (growth rates, totals) are often marked unsupported even when right. |
| Answer relevancy | How well the question can be recovered from the answer alone. | Not correctness. "The report does not say" scores near 0 even though it is the right answer; rambling lowers it. |
| Context precision | How many of the pages read were judged useful, with more credit when useful pages were read first. | Compares pages with the generated answer, not with a verified one: a wrong answer from the wrong pages can score high. Page reading order is not a ranking. |

A metric that fails shows "n/a" with the reason, never 0. Use the scores to decide which answers to check against the cited
pages; do not set pass/fail thresholds before you have compared them with answers you have judged yourself.

## Costs (estimates, not measurements)

From `research\07-openai-models-and-costs.md`, using list prices on 2026-10-07 and a 308-page report. Nothing has been billed yet.

| Item | Typical | Range |
|---|---|---|
| Index one report (`gpt-5.6-luna`) | about $0.26 | $0.07 - $0.32 (about $0.001 per page) |
| One question, defaults (`gpt-5.6-sol`, medium) | about $0.35 | $0.16 - $0.88 |
| RAGAS for that answer (`gpt-4.1`) | about $0.06-0.09 | $0.04 - $0.15 |
| 100 questions plus one index | about $37 | $17 - $94 |

Question cost is dominated by re-reading the document outline, so it grows with page count. Every answer shows its own estimated
cost (from the token counts the API returns and `reportlens\pricing.py`); a model missing from that table shows no price rather
than a guess. `gpt-5.6-sol` is on a promotional price "at least through 2026-11-21", so re-check afterwards. A cheaper
setup for development: `PI_CHAT_MODEL=gpt-5.6-luna` with `PI_CHAT_REASONING_EFFORT=high`.

## Project layout

```
reportlens/            the product (Python package)
  service.py           ReportLensService: sessions, upload, ask, evaluate, locate (framework-free, no web imports)
  indexer.py           background indexing + progress          pageindex_compat.py  the only place that patches the SDK
  qa.py, pricing.py    agent run -> events, pages read, tokens and cost
  citations.py, locator.py, pdfutil.py   <cite> parsing, quote -> highlight rectangles, PDF text and folios
  evaluation.py        RAGAS scoring            store.py   SQLite persistence        models.py / config.py   shared types and settings
  web/                 app.py (factory, headers, errors, static) routes.py (REST) sse.py (event stream) static/ (the UI)
  __main__.py          python -m reportlens
devtools/mock_openai.py   the fake OpenAI server used by --demo and the tests
scripts/               make_sample_pdf.py, live_smoke.py (paid, optional)
samples/               a synthetic 60-page report with known facts (*.facts.json)
tests/                 offline tests          docs/ARCHITECTURE.md   the contracts          research/   background notes
data/                  created on first run (git-ignored): reportlens.db, sessions/<id>/ (PDF + index), tmp/ (uploads in flight)
```

## Running the tests

```powershell
.\run.ps1 -Test                                  # everything offline (about 5 minutes: the end-to-end tests index a 60-page PDF several times)
.\run.ps1 -Test -m "not slow"                    # skip the slow end-to-end tests
.\run.ps1 -Test tests\test_web.py -k upload      # anything after -Test goes to pytest
```

Tests never touch the network or your real data: they use temporary data folders and the fake OpenAI server. `tests\test_e2e_mock.py`
runs the real service, indexer, agent, citation resolver and RAGAS behind a real uvicorn server against that fake.

### Paid check against the real API (do this first)

```powershell
.\.venv\Scripts\python.exe scripts\live_smoke.py                       # prints the plan and estimated cost, refuses to spend
.\.venv\Scripts\python.exe scripts\live_smoke.py --yes                 # preflight, index the sample, ask one question, score it
.\.venv\Scripts\python.exe scripts\live_smoke.py --yes --pdf "C:\path\National Grid Annual Report.pdf" --question "What is Management Outlook?"
```

It first calls `models.retrieve` for every configured model (free), then indexes in a throw-away folder and prints the answer, steps,
each citation (page, folio, section, quote, whether highlight rectangles were found), pages read, tokens, cost and RAGAS scores.
Exit code 0 = all good, 1 = something failed, 2 = refused. It never prints your key.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `ModuleNotFoundError`, or `python` is a different version | Use `.\run.ps1` or `.\.venv\Scripts\python.exe`. Never the global Python. |
| Banner says no `OPENAI_API_KEY` | Add it to `.env` and restart, or run with `--demo`. The page loads but nothing can be indexed or answered. |
| "Port 8000 is already in use" | `--port 8001`, or stop the other program. |
| Indexing fails at the very end with an odd message (for example "model not found") although the model exists | Probably a Windows path-length problem: the PageIndex store nests deep folders under `data\`. Keep the project in a short path or set `REPORTLENS_DATA_DIR=C:\rl\data`, delete the chat and upload again. |
| `pip install` of `litellm` fails | The venv path is too long (keep the venv root under about 105 characters). Install only from `requirements.lock.txt`; litellm 1.82.7 and 1.82.8 were malicious releases and must never be installed. |
| "OpenAI rate limit" while indexing | Lower `PI_INDEX_SUMMARY_CONCURRENCY` (8, then 4), wait a few minutes, upload again. An "out of credit/quota" 429 is a billing problem, not a rate limit. |
| "model not found or no access" | Set `PI_INDEX_MODEL` / `PI_CHAT_MODEL` / `RAGAS_JUDGE_MODEL` to models your key can use; `live_smoke.py` shows which ones fail. |
| "looks scanned" / "no text layer" | The PDF has no selectable text. Run OCR first; ReportLens does not OCR. |
| No bookmarks: indexing is much slower and costs more | Flash needs an outline. With `PI_INDEX_FALLBACK_STANDARD=true` the model builds the structure instead (minutes; roughly $0.3 - $0.7 by estimate). Set it `false` to refuse such PDFs. |
| A chat stays "indexing" after a crash | Restart the server: interrupted indexing is marked failed ("interrupted") and you can upload again. |
| Highlight is on the wrong line, or the whole page flashes | See "Citations and highlighting". The page is still right; the notice in the viewer says whether the passage was found. |
| Scores show "n/a" or "partial" | A judge call failed (rate limit, model). The reason is in the card tooltip and the server log; "Re-run" tries again. |
| Browser shows a blank PDF panel | Use current Chrome or Edge; check the console for blocked requests (the page allows only its own origin). |

Logs go to the console (`--log-level debug` for more). Each request is logged with its duration, never its body.

## Security notes

- **Localhost only, no login.** The default bind is `127.0.0.1`. Anyone who can reach the port can read the uploaded reports and
  spend your OpenAI credit; `--host 0.0.0.0` prints a warning. Add authentication before exposing it.
- **Your data goes to OpenAI**: page text read by the agent, your questions and the answers (and the judge's inputs for scoring). With the
  Responses API OpenAI retains requests by default (`store`), per its own policy. Do not upload documents you may not send to OpenAI.
  Demo mode sends nothing.
- Uploads: size-capped while streaming, `%PDF-` magic checked, client file names sanitised and never used as paths; sessions are 32-hex ids
  validated before use. The UI renders model output through DOMPurify; the server sets a strict Content-Security-Policy
  (same-origin scripts, no CDN), `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` on every response.
- `GET /api/config` and `/api/health` never include the key. `.env` and `data\` are git-ignored; the data folder is not encrypted.
- Use a dedicated OpenAI project key with a spend limit and an expiry.

## Deploying publicly

The primary target is **Render's free web service** (512 MB RAM, 0.1 CPU, sleeps after 15 idle minutes, temporary disk), built from the
`Dockerfile` and the `render.yaml` Blueprint in this repository. Hugging Face now charges for Docker Spaces, so it is only an
alternative. Because you pay for the OpenAI calls, a public copy is protected: an access code, a spending budget, a cap on chats and a
per-visitor question limit (all off by default, all switched on together by `PUBLIC_MODE=1`). On a small host `LOW_MEMORY=1` (automatic
inside a container limited to 600 MB or less) lets a 300-page report index in a short-lived child process and scores answers in
another, so the web server itself stays small. The step-by-step guide for Windows, the measured memory and time on Render's limits, the cost and
threat-model tables and the take-down steps are in **[docs/DEPLOY.md](docs/DEPLOY.md)**.

```powershell
.\scripts\make_deploy_bundle.ps1          # builds C:\rl_deploy_bundle: only what Render needs, audited for keys and .env; prints the next steps
.venv\Scripts\python.exe scripts\render_limits_test.py --image reportlens:render --pdf "C:\path\report.pdf"   # Docker with Render's limits
```

The settings that matter (full list in `.env.example`): `ACCESS_CODE`, `BUDGET_USD_TOTAL`, `MAX_SESSIONS`, `QUESTIONS_PER_HOUR_PER_IP`,
`ALLOWED_HOSTS`, `TRUST_PROXY`, `LOW_MEMORY`. Put keys and codes in the host's dashboard (Render: environment variables with `sync: false`),
never in a file you push. Uploaded PDFs and questions go to OpenAI and the host's disk is temporary, so a public demo should only ever be
used with public documents.

## Known limitations and what to verify in UAT

Nothing here has run against the live OpenAI API yet; the build was tested offline against the fake server only.

Known limitations: one user, one process (running state is held in memory, so do not run several workers); one PDF per chat; no OCR, no
image or chart understanding; no authentication; highlight accuracy on tables and multi-column pages is heuristic; scores are AI-judged
estimates; English-language reports only have been considered; PageIndex and RAGAS release often, so versions are pinned
(`pageindex==0.2.21`, `ragas==0.4.3`).

UAT checklist (try these in order; stop at the first failure and send me the server log):

- [ ] `live_smoke.py --yes` passes (models available, citations carry rectangles, scores are numbers). Note its cost against the estimate.
- [ ] Upload the National Grid annual report. Note indexing time and whether it used the bookmarks (fast) or the fallback (slow).
- [ ] Ask the five sample questions below. For each: is the answer plausible, does every chip open the right page, is the highlight on the
      supporting passage, does the folio in the hover card match the printed page number (for example printed 87 = PDF page 89)?
- [ ] Ask a question the report cannot answer: it should say so without inventing a source.
- [ ] Ask a follow-up ("and the previous year?"): it uses the earlier turn; scoring uses the rewritten question.
- [ ] Press Stop mid-answer, then ask again (no "still being answered" error). Reload the page: the conversation and scores are still there.
- [ ] Try to upload a second document after the first question: refused. "New chat with this document" works without re-indexing.
- [ ] Delete a chat: it disappears and its folder under `data\sessions\` is gone.
- [ ] Compare the per-answer cost shown in the UI with the OpenAI usage dashboard. Check the scores against your own judgement of 5 to 10 answers.
- [ ] Open it in Edge and Chrome; resize to a narrow window.

Sample questions (National Grid):

1. What is the status of GHG reduction technology available to the company?
2. Has the company undertaken or announced / earmarked capex to meet its transition plans in the next 5 years?
3. To the best of your knowledge how prepared is the company for acute and chronic physical risk events through adaptation and resiliency measures on its business?
4. What are Primary Sources for operating cash flows?
5. What is Management Outlook?

## Wrapping the core in your own FastAPI service

`reportlens.service.ReportLensService` has no web imports: plain synchronous methods for sessions, documents, locate and messages (run them in a
thread pool), plus three coroutines (`ask`, which yields `(event_name, payload)` pairs, `evaluate_message` and `aclose`). It takes its
`Store`, `IndexService`, `QAEngine` and `Evaluator` as optional constructor arguments, so you can swap storage or models.

`reportlens.web` is a thin adapter you can reuse or replace: `create_app(settings, service)` accepts an injected service (and then does not
create or close one), `routes.py` is the REST surface below, and `sse.EventStreamResponse` turns the `ask` generator into a stream with
pings, per-event flushing and disconnect cancellation. Authentication, per-user storage and multi-worker state would be added around these.

| Method and path | Purpose |
|---|---|
| `GET /api/health`, `GET /api/config` | status; public settings plus the metric descriptions |
| `GET/POST /api/sessions`, `GET/PATCH/DELETE /api/sessions/{sid}` | chats (`POST` accepts `{"from_session": sid}` to reuse a document) |
| `POST /api/sessions/{sid}/document` | multipart `file` upload, then indexing runs in the background (poll the session) |
| `GET /api/sessions/{sid}/document/file` | the PDF (supports HTTP Range) |
| `GET /api/sessions/{sid}/document/pages`, `.../outline`, `.../locate?page=&quote=&claim=` | page sizes and folios, the section tree, highlight rectangles |
| `POST /api/sessions/{sid}/messages` | ask `{"content": "..."}`; answered as Server-Sent Events (see section 6 of `docs\ARCHITECTURE.md`) |
| `GET /api/sessions/{sid}/messages/{mid}`, `POST .../evaluate` | one message; re-run RAGAS |

Errors are always `{"error": {"code", "message"}}` with the matching HTTP status. The machine-readable schema is at `/api/openapi.json`
(the interactive docs page is off because its scripts come from a CDN that the Content-Security-Policy forbids).

## Licences and third-party components

ReportLens loads **no** assets from a CDN at runtime; everything below is vendored under `reportlens\web\static\vendor\`
(details and upgrade notes in `LICENSES.md` and `LICENSES-pdfjs.md` there).

| Component | Licence |
|---|---|
| pdf.js 6.4.299 | Apache-2.0 |
| marked 18.1.0 | MIT |
| DOMPurify 3.4.16 | Apache-2.0 or MPL-2.0 (your choice) |
| Geist Sans and Mono 1.7.2 | SIL Open Font License 1.1 |
| Lucide icons 1.52.0 (inlined SVG paths) | ISC |

The PageIndex name and logo are not used; the logo mark in the UI is an original drawing.

**PyMuPDF is deliberately not used, anywhere.** It is licensed AGPL-3.0, whose network clause (section 13) applies to a service that users
reach over HTTP, which is exactly what a later FastAPI deployment would be. All PDF text, page geometry and highlight rectangles come from
`pypdfium2` (BSD-3-Clause / Apache-2.0) with `rapidfuzz` (MIT) for fuzzy matching; PyPDF2 appears only because PageIndex imports it. The cost
of the decision is that there is no table detection; table text is built from pdfium word positions. Revisit it only with legal advice or a
commercial Artifex licence.
