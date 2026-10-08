# Annual Report Lens REST API

Everything the web app does is available over HTTP. The interactive reference (Swagger UI) is at **`/docs`** on any running
instance and the machine-readable schema at `/api/openapi.json`. This page is the step-by-step version, with curl and Python.

Base URL: `http://127.0.0.1:8000` locally, `https://<your-service>.onrender.com` on Render. Bodies are JSON unless noted;
errors are always `{"error": {"code": "...", "message": "..."}}` with a matching HTTP status.

## 1. Sign in (only when the server has an access code)

`POST /api/login {"code": "..."}` sets two cookies: the login (12 hours) and your visitor id (one year). Keep them in a cookie
jar and send them with every request. **Each cookie jar is its own private visitor**: it sees only the chats it created.

```bash
curl -c jar -b jar -X POST "$BASE/api/login" -H "Content-Type: application/json" -d '{"code": "YOUR-ACCESS-CODE"}'
```

`GET /api/auth` tells you whether a code is needed (`required`) and whether you are signed in (`authenticated`). Ten wrong
codes from one address lock it out for ten minutes (HTTP 429 with `Retry-After`).

## 2. Create a chat and upload one PDF

```bash
SID=$(curl -s -c jar -b jar -X POST "$BASE/api/sessions" | python -c "import sys, json; print(json.load(sys.stdin)['id'])")
curl -c jar -b jar -F "file=@annual-report.pdf;type=application/pdf" "$BASE/api/sessions/$SID/document"
```

A chat holds exactly one document. The upload answers `202` at once and indexing runs in the background; poll
`GET /api/sessions/$SID` until `state` is `ready` (or `failed`, with the reason in `document.error`). `document.stage` and
`document.progress` (0 to 1) show where it is. A 50-page report takes a minute or two; 300 pages take minutes.

To ask about a document another chat of yours already indexed, without paying again: `POST /api/sessions
{"from_session": "<that chat's id>"}`. The demo chat works too: `GET /api/demo` gives its `session_id`.

## 3. Ask

**One JSON answer** (simplest):

```bash
curl -c jar -b jar -X POST "$BASE/api/sessions/$SID/ask" -H "Content-Type: application/json" \
     -d '{"content": "What are the primary sources of operating cash flow?", "wait_for_scores": true}'
```

Returns `{"message": {...}}`: the stored answer with

- `content`: Markdown with citation markers `[[c1]]`, `[[c2]]` ...
- `citations[]`: `index`, `page` (physical PDF page), `printed_page` (the number printed on it), `section_path`
  (for example `["Strategic report", "Financial review"]`), `quote` (the verified passage), `rects` (highlight rectangles in
  PDF points; empty = the passage was not located and the whole page applies), `claim` (the sentence it supports)
- `sources[]`, `steps[]` (what the agent read), `usage` (tokens and estimated cost), `elapsed_ms`
- `evaluation`: `status`, `faithfulness`, `answer_relevancy`, `context_precision` (0 to 1, `null` when not computed), `errors`
  (why a metric is missing), `judge_model`

With `"wait_for_scores": false` it returns as soon as the answer is complete; poll `GET /api/sessions/$SID/messages/<id>` until
`evaluation.status` is no longer `pending` or `running`. Model failures answer HTTP 502 with the error code.

**Streaming** (what the web app uses): `POST /api/sessions/$SID/messages {"content": "..."}` answers `text/event-stream`.
Events, in order:

| Event | Data |
|---|---|
| `message_start` | the stored question, the answer's `message_id` |
| `step`, `step_done` | the agent's steps ("Read pages 88-90") |
| `token` | `{"text": "..."}` pieces of the answer |
| `citation` | one resolved citation |
| `answer_done` | the complete answer message |
| `eval_started`, `eval_result`, `eval_done` | scoring started, one metric finished, all scores |
| `done` | the end |
| `error` | `{"code", "message"}`; the stream ends |

Comment lines `: ping` arrive every 15 seconds so proxies keep the connection open.

## 4. The document and the highlights

- `GET /api/sessions/$SID/document/file`: the PDF (supports `Range`).
- `GET /api/sessions/$SID/document/pages`: page sizes and printed page numbers.
- `GET /api/sessions/$SID/document/outline`: the section tree PageIndex built.
- `GET /api/sessions/$SID/locate?page=82&quote=...`: highlight rectangles for any quote on a page.
- `POST /api/sessions/$SID/messages/<id>/evaluate`: score an answer again.

## 5. Your own model provider (optional)

Send `X-LLM-Config` with a base64url-encoded JSON object on uploads, questions and re-scoring. Without it the server's model
answers. `GET /api/config` lists the providers this server accepts (`llm.providers`) and whether own keys are `off`,
`optional` or `required` (`llm.visitor_keys`).

```json
{"provider": "gemini", "api_key": "...", "chat_model": "<answer model id>",
 "index_model": "<optional>", "judge_model": "<optional>", "embedding_model": "<optional>"}
```

`provider` is one of `openai`, `anthropic`, `gemini`, `openrouter`, `groq`, `mistral`, `deepseek`, `xai`, `together` (and, on
servers you run yourself, `ollama` or `custom` with a `base_url`). The answer model must support tool calls. Without an
embeddings model, answer relevancy is skipped. `POST /api/llm/check` with the same header makes one tiny request to test the
key and model. The key is used for that request only: never stored, never logged, never counted against the server's budget.

## Python example

```python
import base64, json, time
import httpx

BASE = "https://your-service.onrender.com"
own_key = {"provider": "openai", "api_key": "sk-..."}          # optional: delete the header below to use the server's model
headers = {"X-LLM-Config": base64.urlsafe_b64encode(json.dumps(own_key).encode()).decode().rstrip("=")}

with httpx.Client(base_url=BASE, timeout=httpx.Timeout(30, read=600)) as c:      # the client keeps the cookies
    c.post("/api/login", json={"code": "YOUR-ACCESS-CODE"}).raise_for_status()
    sid = c.post("/api/sessions").json()["id"]
    with open("annual-report.pdf", "rb") as pdf:
        c.post(f"/api/sessions/{sid}/document", files={"file": ("annual-report.pdf", pdf, "application/pdf")},
               headers=headers).raise_for_status()
    while (state := c.get(f"/api/sessions/{sid}").json()["state"]) not in ("ready", "failed"):
        time.sleep(5)
    if state == "failed":
        raise SystemExit(c.get(f"/api/sessions/{sid}").json()["document"]["error"])

    answer = c.post(f"/api/sessions/{sid}/ask", json={"content": "What is management's outlook?"},
                    headers=headers).raise_for_status().json()["message"]
    print(answer["content"])
    for cite in answer["citations"]:
        print(f'[{cite["index"]}] page {cite["page"]} (printed {cite["printed_page"]}), {" > ".join(cite["section_path"])}: {cite["quote"]}')
    print({k: answer["evaluation"].get(k) for k in ("faithfulness", "answer_relevancy", "context_precision")})
```

## Limits and common errors

| HTTP / code | Meaning |
|---|---|
| 401 `auth_required` | Sign in first (step 1). |
| 403 `demo_read_only` | The demo chat cannot be changed; create your own (`from_session` works). |
| 403 `own_key_required` | This server only answers with your own key (`X-LLM-Config`). |
| 400 `invalid_llm_config` | The `X-LLM-Config` header is malformed or names an unknown provider. |
| 402 `budget_exhausted` | The server's spend budget is used up (your own key still works). |
| 404 `session_not_found` | No such chat, or it belongs to someone else, or the free server slept and restarted empty. |
| 409 `document_not_ready`, `session_busy` | Wait for indexing; one question at a time per chat. |
| 413 `file_too_large`, 422 `too_many_pages`, `scanned_pdf`, `encrypted_pdf` | The PDF breaks a limit (see `GET /api/config`). |
| 429 `rate_limited`, `session_limit` | Questions per hour per address, or chats at once; `Retry-After` says when. |
| 502 + a model code (`openai_auth`, `openai_model`, `openai_rate_limit`, `agent_failed`) | The model provider refused or failed. |
