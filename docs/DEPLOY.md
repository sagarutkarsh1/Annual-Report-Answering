# Deploying Annual Report Lens publicly on Render's free tier

This guide puts Annual Report Lens on **Render's free web service** so you can send people a link. You pay for OpenAI, so a public
copy is protected by an access code, private chats per visitor, a spending budget, a chat cap and a per-visitor question limit
(part C explains what each one stops). Visitors without a code can still open the **read-only demo chat**, and signed-in visitors
can use **their own provider key** instead of yours. Everything is written for Windows and a non-expert; commands go into
**PowerShell**.

> **Already deployed?** Jump to **"Updating an existing deployment"** at the end of part A.

> **Hugging Face is no longer free for this.** As of October 2026 Hugging Face charges for Docker Spaces, so it is only mentioned under
> "Alternatives" at the end. Render's free web service is the supported path.

> **What was tested, and what was not.** The image was built and run in Docker on a Windows PC with exactly Render's free limits
> (`--memory=512m --memory-swap=512m --cpus=0.1`; swap off, so an out-of-memory kill is real) against the built-in fake OpenAI
> server, uploading the owner's 308-page National Grid report. The numbers are in part B. **Nothing was run on the real Render and
> no real OpenAI call was made**; part F lists everything that is still unverified. Button and menu names on Render and GitHub are
> written from their documentation and may differ slightly; if a name does not match, look for the closest one.

> **The one rule.** Never paste an API key or an access code into a chat, a file in the project, an e-mail or a screenshot. Type them
> only into the password boxes of the websites named below (OpenAI, Render) or into Git's own sign-in window. If one ever leaks,
> revoke it at once (part A, step 12).

## What you need

| Item | Why | Cost |
|---|---|---|
| A GitHub account | Render builds from a Git repository; use a **private** one | Free |
| A Render account | Hosts the app. Render says a card is **not required** to run a free instance, but signing up (or verifying your identity) may still ask for one; read what the page says before you continue | Free |
| Your OpenAI account | Pays for the answers. You make a **separate** key for the demo | Pay as you go |
| [Git for Windows](https://git-scm.com/download/win) | Pushes the bundle to GitHub | Free |
| This project, with its `.venv` | Builds the bundle | - |

You do **not** need Docker on your own PC: Render builds the image. (If you have Docker Desktop, part E shows how to rehearse the
free limits first.)

---

## A. Step by step

### 1. Make a spending-limited OpenAI key for the demo

1. Open the OpenAI platform, **Settings -> Organization -> Limits** (menu names change now and then) and set a **monthly budget** you can live with, for example 20 USD. This is your real ceiling: it holds even if everything else here fails.
2. Create a **new Project** called `reportlens-demo` (Settings -> Projects), give it its own monthly budget if the page offers one, and create **an API key inside that project**. Copy it once; you will paste it into Render in step 7 and nowhere else.
3. Do not reuse the key in your local `.env`. A separate key can be revoked without touching your own work.

### 2. Build the bundle

```powershell
cd "C:\Users\ayush\Annual Report Answering"
.\scripts\make_deploy_bundle.ps1
```

It creates `C:\rl_deploy_bundle` (use `-Out D:\somewhere` to change it) with exactly: `Dockerfile`, `.dockerignore`, `.gitignore`,
`requirements-deploy.txt`, `render.yaml`, a neutral `README.md`, `LICENSE`, `THIRD_PARTY_NOTICES.md`, `SECURITY.md`, `reportlens/`,
`devtools/`, `samples/` and `demo/` (the read-only demo chat, if you exported one: see `demo/README.md`; it contains the published
report it quotes, which is why this repository must stay **private**). It then audits the folder and **refuses** (exit code 1) if it
finds a `.env`, anything shaped like an API key (OpenAI, Anthropic, Google, Groq, xAI) or a GitHub/Hugging Face/Render token, a
secret *value* in `render.yaml`, or a `research/`, `data/` or `tests/` folder. Run it again whenever you change the code; it refreshes its own files and
leaves a `.git` folder alone. (`-Target hf` builds the old Hugging Face flavour.)

### 3. Create a private GitHub repository

1. Sign in at github.com, click **+ -> New repository**.
2. Name it, for example, `reportlens-deploy`. Choose **Private**. Leave "Add a README", ".gitignore" and "license" **unticked** (the bundle has its own). Click **Create repository**.
3. Keep the page open: it shows the repository address, `https://github.com/<your-user>/reportlens-deploy.git`.

### 4. Push the bundle (you type the credentials, nobody else)

```powershell
cd C:\rl_deploy_bundle
git init -b main                                   # first time only
git add -A
git commit -m "Deploy ReportLens"
git remote add origin https://github.com/<your-user>/reportlens-deploy.git
git push -u origin main
```

Git for Windows opens a **sign-in window** (Git Credential Manager) for GitHub: sign in there. If it asks for a password in the console
instead, GitHub no longer accepts your account password: create a **personal access token** yourself (GitHub -> your avatar ->
Settings -> Developer settings -> Personal access tokens -> a fine-grained token with *Contents: read and write* on this one repository)
and paste it into that prompt only. `<your-user>` is a placeholder: copy the real address from the GitHub page. If `git commit`
says it does not know who you are, run `git config --global user.name "Your Name"` and `git config --global user.email "you@example.com"` once.

### 5. Create the service on Render

1. Sign in at dashboard.render.com (sign up first if you have no account; Render may ask you to connect GitHub: allow access to **only** the `reportlens-deploy` repository).
2. **New + -> Blueprint** -> choose the `reportlens-deploy` repository -> Render reads `render.yaml` and shows one **Web Service** named `reportlens`, plan **Free**, runtime **Docker**.
   (No Blueprint button? **New + -> Web Service** -> same repository -> Language **Docker** -> Instance type **Free** -> Health check path `/api/health` -> and add the variables from part E by hand.)

### 6. Type the secrets

Render now asks for the values marked `sync: false` in the file, **once**:

| Name | Value |
|---|---|
| `OPENAI_API_KEY` | the demo project's key from step 1 |
| `ACCESS_CODE` | a code you invent: 12 or more random characters, for example three unrelated words and two digits. This is what you send to people. |

`SESSION_SECRET` is generated by Render (it keeps visitors signed in, and their chats theirs, when the service wakes up). Everything
else in `render.yaml` (public mode, host list, budget 10 USD, 10 chats, 15 questions per hour per visitor, 25 MB / 400 pages,
low-memory mode, the e-mail shown for code requests `ACCESS_REQUEST_EMAIL`, visitors' own keys `VISITOR_KEYS=optional`) is already
set; change any of it later under the service's **Environment** tab.

### 7. Deploy and wait

Click **Deploy Blueprint**. The first build downloads and installs about a gigabyte of Python packages (PageIndex, RAGAS, litellm ...):
expect **5-15 minutes** (the package install alone took under 3 minutes on a fast PC). The log shows *Build*, then *Deploy*; Render
marks the service **Live** when `/api/health` answers. If it says *Build failed* or *Deploy failed*, open **Logs** and read the last
lines (part E, Troubleshooting). Free instances are small, so **a failed build is more likely than on a paid plan**: use *Manual Deploy ->
Clear build cache & deploy* once before looking for another cause.

### 8. Try it yourself first

The address is at the top of the service page: `https://reportlens-<something>.onrender.com`.

1. You see **Enter access code**. Type your code. A wrong code shows an error; ten wrong ones from one address lock that address out for ten minutes.
2. Upload a **public**, text-based PDF. Start with a short one (a 50-page report indexes in a few minutes on the free server). The upload card says that indexing "can take several minutes" and to keep the tab open: that is true here (part B).
3. Ask a question; click a citation chip. Check the OpenAI usage page: a real question costs 0.3-0.65 USD (part D).
4. Open the sidebar: there is a **Sign out** item (shown only because an access code is set).

### 9. Share it

Anyone can open the link and choose **View a demo chat** without a code; a link ending in `#/s/de300000000000000000000000000001`
opens the demo directly (handy for a CV or a post). People who want to try their own report e-mail you for a code (the login screen
says how). Send the link and the access code in **separate** messages if you can. Tell people: it is a demo, use public documents only,
the first page load after a quiet spell can take about a minute, **indexing a long report takes minutes (10 or more for 300 pages)**
and chats disappear when the service sleeps (step 10).

### 10. Sleeping, cold starts and what is lost

* Render **spins a free service down after 15 minutes without incoming traffic** and starts it again on the next request (Render says that takes about a minute). The visitor sees Render's own "waking up" page first. After that ReportLens needs a further **15-35 s** to start listening (measured under the free limits; the scoring libraries load later, on the first scoring).
* The disk is **temporary**. Every sleep, restart, redeploy and Render-initiated move **wipes** chats, uploaded PDFs, indexes and the spend counter. A visitor who comes back after a sleep finds an empty chat list; the page then says *"The demo restarted (free hosting sleeps). Please start a new chat and upload again."* and starts a fresh chat. Nothing confidential is left on the server after it sleeps.
* Because the spend counter lives on that disk too, it restarts from zero with each wake-up. That is why the OpenAI-side monthly limit from step 1 is the real ceiling and `BUDGET_USD_TOTAL` is the per-run guard (part C).
* A free account has **750 free instance hours per month** across all its free services (a service that is asleep does not use any). One always-awake service would need 744 hours, so a single demo fits; two do not.
* Do not buy persistent storage unless you want chats to survive; it would also keep uploaded documents (and free services cannot have a disk anyway).

### 11. Check what you have spent

* **OpenAI dashboard -> Usage** (filter by the `reportlens-demo` project). This is the truth; ReportLens's own figure is an estimate.
* Inside the app, visitors only see a fraction: a quiet banner appears above 80 % used, and a clear "usage budget used up" message with the composer disabled at 100 %. The dollar amount is never sent to the browser.
* Render **Logs** show lines like `usage budget exhausted (estimated 10.04 of 10.00 USD)` when the guard trips, every wrong access-code attempt with the visitor address, and, per indexed report and per scored answer, one line `memory ...: rss=... peak=... loaded=...` that tells you how close the instance is to its 512 MB.

### 12. Update, rotate and take down

* **Update the app:** change the code, run `.\scripts\make_deploy_bundle.ps1`, then in `C:\rl_deploy_bundle`: `git add -A`, `git commit -m "Update"`, `git push`. Deploys are manual (`autoDeployTrigger: "off"` in `render.yaml`): in Render open the service -> **Manual Deploy -> Deploy latest commit**.
* **Change the access code:** service -> **Environment** -> edit `ACCESS_CODE` -> Save. The service restarts and **every existing login stops working** (the cookie is signed with the code).
* **Rotate or revoke the OpenAI key:** create a new key in the demo project, replace the `OPENAI_API_KEY` variable (the service restarts), then **delete the old key** on the OpenAI page. If you ever suspect a leak, delete first, replace after.
* **Own keys only (you pay nothing):** set `VISITOR_KEYS=required`. Your `OPENAI_API_KEY` is then never used for visitors; everyone
  adds their own key under **Model & API key**. `off` does the opposite (only your key).
* **Take the demo down:** service -> **Settings -> Suspend Service** (reversible; stops all traffic and instance hours) or **Delete Web Service** (permanent). Then delete the demo OpenAI key, and the GitHub repository if you no longer need it.

---

### Updating an existing deployment

You deployed before the demo chat, private chats and own keys existed? Three steps:

1. Build the new bundle (it now also carries `demo/`, `LICENSE` and the notices): `.\scripts\make_deploy_bundle.ps1`
2. Push it: in `C:\rl_deploy_bundle` run `git add -A`, `git commit -m "Demo chat, private chats, own keys, API docs"`, `git push`.
3. In Render, open the service -> **Environment** and add `ACCESS_REQUEST_EMAIL` (your e-mail) and, if you like, `VISITOR_KEYS`
   (`optional` is the default). Render adds new `render.yaml` variables only when the Blueprint is synced (**Blueprints** -> your
   blueprint -> **Manual Sync**), so typing them here is the quickest. Then **Manual Deploy -> Deploy latest commit**.

Nothing to migrate: the database lives on the temporary disk and the app upgrades its schema at start-up.

## B. Measured on Render's free limits (Docker, 512 MB, swap off, 0.1 CPU)

The same image, run the way Render runs it, with the fake OpenAI server (`REPORTLENS_DEMO_MOCK=1`), the 308-page, 7.4 MB National
Grid annual report, `PUBLIC_MODE=1`, `TRUST_PROXY=1`, an access code and a login. Reproduce with
`scripts\render_limits_test.py` (part E). "Peak" is the container's memory use from `docker stats` (page cache excluded) sampled every
~1.5 s plus the kernel's `memory.peak`.

Measured on 8 October 2026 with the image built from this repository (`scripts\render_limits_test.py --second-chat --restart-check`):

| Step | Time | Peak memory | Notes |
|---|---|---|---|
| Cold start: container started -> `/api/health` answers | 17 s | 66 MiB | Render's own wake-up minute comes on top |
| Upload the 7.4 MB PDF | 12 s | 90 MiB | |
| Index 308 pages | **9.0 min** | **348 MiB** | 397 sections. Almost all of it is the layout parse on 0.1 CPU; the fake model answers instantly |
| First question, then its scores | answer 55 s, scores 80 s | 334 MiB | The first answer after a start loads the answer libraries (about 45 s of the 55) |
| Second question, then its scores | answer 8 s, scores 30 s | 325 MiB | |
| PDF page request (`Range`) | 0.5 s | 220 MiB | |
| **Worst case:** a second 308-page report indexed after answers were scored | 9.0 min | **467 MiB** | The web process now holds the answer libraries; the indexing child needs about 280 MiB on top |
| Question in that second chat | answer 32 s, scores 56 s | 358 MiB | |
| Idle once everything is finished | - | 228 MiB | |
| Sleep/wake: a fresh container from the same image | `/api/health` after 16 s | 68 MiB | The old chat answers `404 session_not_found` (the page shows "the demo restarted"), the login cookie still works, the chat list is empty |

* **Re-measured after the demo chat, private chats and visitors' own keys were added** (the demo installed in the container):
  every step passed again with no out-of-memory kill; first indexing 352 MiB, worst case 484 MiB, idle 257 MiB. That run's
  timings are not comparable (the machine was busy with the test-suite at the same time), so the table keeps the clean ones.
* **No out-of-memory kill in any step** (the cgroup's `oom_kill` counter stayed 0; `memory.peak`, page cache included, was 473 MiB).
* **Headroom:** about 160 MiB in the common case (the service wakes up, one report, questions) and about 45 MiB in the worst case above. If the limit is ever reached, the kernel is told to kill an indexing or scoring child, never the web server: the upload then says "ran out of memory" and the service stays up.
* Answers stream without a gap longer than 14 s (the server sends a keep-alive every 15 s), so Render's proxy never sees an idle connection.

### The question set ("Run all"): answering several questions at once under the same limits

After an upload the page offers five editable preset questions and one button, "Run all", that answers them in parallel
(`POST /api/sessions/{id}/batch`, `docs/ARCHITECTURE.md` section 13). Measured on 9 October 2026 with the same image and limits
(`--memory=512m --memory-swap=512m --cpus=0.1`, `PUBLIC_MODE=1`, `LOW_MEMORY` automatic), the 308-page report indexed once and copied
into a fresh chat for every run, the five default questions, and the fake model made slow like a real one
(`REPORTLENS_MOCK_DELAY_MS=100`: 1 s before each agent turn, 0.1 s per streamed chunk). Every row is a fresh container whose first question
was a throw-away warm-up (the answer libraries are loaded by then, as they are on a Render instance that has already served a question).
Reproduce with `scripts\render_limits_test.py --image reportlens:render --pdf <report> --batch-sweep seq,1,2,3 --mock-delay-ms 100`
(indexes once into a Docker volume, 25-30 minutes at 0.1 CPU, then about 15 minutes per row).

| Mode | First answer | All 5 answers | All 5 scored | Peak memory (`docker stats`) | `memory.peak` (cache incl.) | CPU used |
|---|---|---|---|---|---|---|
| One after another, `/messages` (what you did before) | 208 s | 850 s | 923 s | 356 MiB | 365 MiB | 9.1 % |
| **Run all, `BATCH_CONCURRENCY=1`** | 180 s | 662 s | 672 s | 361 MiB | 371 MiB | 9.5 % |
| **Run all, `BATCH_CONCURRENCY=2` (the `LOW_MEMORY` default)** | 224 s | **586 s** | **659 s** | 375 MiB | 384 MiB | 9.9 % |
| **Run all, `BATCH_CONCURRENCY=3`** | 375 s | 625 s | 683 s | 378 MiB | 388 MiB | 10.0 % |

No row was killed for memory (`oom_kill` 0). **What this says, honestly:**

* **Memory is not what limits parallelism.** Each extra question in flight costs about 7-10 MiB; three at once peaked at 378 MiB,
  well under the 450 MiB line. The scoring children are the large consumers and they are queued one at a time (below).
* **At 0.1 CPU the work is CPU-bound, so parallelism helps less than the number of questions suggests.** The container used 9-10 % of a
  CPU in every row: the agent's own work (reading the outline and pages, resolving citations) and each scoring import RAGAS on the same
  tenth of a core. What does overlap is the *waiting*: for the model's answer (on Render: the network and OpenAI's latency, free of
  charge to the CPU) and, across questions, answering while an earlier answer is being scored. That is where the gain comes from:
  five questions take 586 s instead of 850 s (-31 %), and everything including the scores 659 s instead of 923 s (-29 %).
* **A third question in flight does not help**: the answers finish no sooner (625 s vs 586 s) and the first one arrives much later
  (375 s vs 224 s) because three runs now share the same tenth of a core. **2 is the default for `LOW_MEMORY`, 3 elsewhere.** On a host with
  more CPU than Render's free tier, raise `BATCH_CONCURRENCY` (up to 6): the CPU limit, not the memory, is what holds it back here.
* **Scoring is the long pole and is queued separately.** One RAGAS scoring child runs at a time (`LOW_MEMORY`; two otherwise) and
  starts the moment its answer is stored. A child used to be started per answer, importing RAGAS each time (about 45 s of CPU at 0.1 CPU).
  A set now keeps one child alive, warm, for up to 45 s between answers: the last answer was scored 10-75 s after it arrived instead of
  150-290 s (the earlier run of the same experiment: 746 / 760 / 794 s to everything scored for concurrency 1 / 2 / 3, against 672 / 659 / 683 s
  above, at +5-10 MiB of peak memory). While it waits for the next answer the child holds the heavy-job gate, so indexing another report
  waits that long at most.
* While a report is being indexed in the same process (a 280 MiB child), a set answers one question at a time regardless of the setting.
* The numbers move by about 10 % between identical runs (the Docker VM shares the machine), so differences smaller than that are noise.

Other speed-ups looked at and **not** applied: PageIndex re-reads and re-parses the whole `pages.json` of the report for every
page-read tool call (about 1-2 MB per call; a cache would save some CPU at the price of holding the parsed pages, ~10 MB, resident, and
is a patch of the SDK's storage layer); the document outline is re-read from disk once per question by the agent but only once per
document by the service (it is cached with the open PDF). The question rewrite already runs beside the agent and a set has none.

### What was changed to get there (each step re-measured)

Every row is a full run of `scripts\render_limits_test.py` on the 308-page report under the same limits; "peak" is `docker stats`.

| Run | Change | Result |
|---|---|---|
| 0 | The image as it was (`PUBLIC_MODE=1`, nothing else) | **Killed for running out of memory** 4.5 minutes into indexing: PageIndex's layout parser starts one worker process per CPU core of the *machine* (not of the 0.1 CPU share), and each one re-imports the SDK. The whole service died. |
| 1 | `LOW_MEMORY`: indexing in a short-lived child process, parser forced to one process, one open PDF, lower concurrency, RAGAS without the `datasets` package, `MALLOC_ARENA_MAX=2` | The indexing **child** was killed (the SDK's own one-process parser keeps every character of every page: 1.3 GB). The web server survived and the upload said "ran out of memory", which is the intended failure mode. |
| 2 | Lean layout parser: the SDK's per-page functions one page at a time (identical output, tested) | Passed, but at 511 of 512 MiB: litellm (~150 MB) was loaded in the child for the summary calls. Idle afterwards: 372 MiB. |
| 3 | No litellm: the child sends the same `/chat/completions` request itself; PageIndex's background litellm preload switched off | Peak **371 MiB**, idle 245 MiB. |
| 4 | The test gained the worst case: a second report indexed after questions were answered and scored | The second indexing stalled at 511 MiB: the web process still held the scoring libraries (~100 MB). |
| 5 | Scoring (RAGAS) in its own child process; only one heavy child (indexing or scoring) at a time, the other waits; the kernel is told to kill a child before the web server. The indexing child uses plain `httpx` instead of the `openai` package (~50 MB) | Everything passed; the second indexing peaked at **494 MiB**. |
| 6 | Before an indexing child starts, the web process closes the documents nobody is using (PDF, outline, folios and search text of the last answered report) and trims its heap | About 40 MiB lower everywhere: first indexing **348 MiB**, second **467 MiB** (the table above). |

### Honest limits of these numbers

* **CPU time is the cost of a 0.1 vCPU**, not of the mock: parsing and building a 300-page document is CPU work, so it takes minutes whatever OpenAI does. With the real OpenAI, model-call latency comes on top of the figures above (summarising about 400 sections at 6 in parallel, roughly 5-10 s per call: plan for **15-30 minutes in total for a 300-page report**), and the job limit on this host is 90 minutes.
* Memory is the limit that matters; time only costs patience. Short reports (50-100 pages) use much less of both.
* The instance is shared: if Render's own monitoring shows memory near 512 MB at any time, the instance restarts and everything on it is lost.

---

## C. Threat model and cost

### What a stranger can do

| Situation | What they can do | What stops them |
|---|---|---|
| **Without the access code** | Load the page, the login screen, the API reference (`/docs`, `openapi.json`) and **read** the demo chat (its messages, its PDF). Nothing else: every other `/api` route (chats, uploads, questions) answers `401 auth_required`, and nobody can change the demo (`403 demo_read_only`). `/api/health` answers `{ok, version}` only. | The gate, checked on every request; the demo is read-only in the web layer and again in the service |
| **Guessing the code** | Ten wrong tries per address per ten minutes, then `429`. | Per-address lockout. A 12+ character random code makes guessing hopeless; a 4-digit code does not, so do not use one. Rotating addresses defeats the lockout only if they can forge `X-Forwarded-For`, see the note below. |
| **With the code** | Upload documents up to 25 MB and 400 pages, ask questions, up to 10 chats at once, 15 questions per hour each address (re-scoring counts too). See **only their own chats**: every browser has a signed visitor cookie and another visitor's chat answers 404 exactly like a missing one. | Budget, chat cap, per-address limit, private chats |
| **With the code and their own key** | Use their own provider (OpenAI, Anthropic, Gemini, OpenRouter, Groq, Mistral, DeepSeek, xAI, Together) for their own requests; not counted against your budget. They cannot point the server at an address of their choosing (no custom URLs on a public server: no SSRF), and their key is never stored, logged or put where another request could use it. | `VISITOR_KEYS`, provider allow-list, per-request settings |
| **With the code, abusing it** | Spend the budget quickly (about 25 uploads or 15-30 questions) and then block everyone until the service restarts. | The budget is the brake: when used up, uploads and questions answer `402 budget_exhausted` and reads still work. Cost to you is capped at the budget. |
| **Sending a request from another website to a signed-in visitor's browser** | Nothing: state-changing requests with a foreign `Origin` get `403`, the cookie is `SameSite=Lax` and `HttpOnly`. | Origin check + cookie flags |
| **Pointing a different host name at the service** | Rejected with `400 invalid_host` for everything except the static page and `/api/health`. | `ALLOWED_HOSTS` (`.onrender.com`) |

**Note on forged addresses.** `TRUST_PROXY=1` makes the per-address limits use the *first* `X-Forwarded-For` entry. Some proxies append the real address to whatever the client sent, in which case a visitor can invent a new address per request and never hit the per-address limits. The budget, the chat cap and the OpenAI limit are not affected. If you observe that on Render, set `PROXY_HOPS=1` (use the last entry, the one the proxy added). This could not be checked against Render's real proxy from here.

### Worst-case spend

| Layer | Limits | Resets? |
|---|---|---|
| `BUDGET_USD_TOTAL` (default 10) | Estimated spend per run of the service, with a small reserve for questions still running; overshoot is at most a few in-flight questions (about 0.65 USD each) | **Yes**, on every sleep / restart / redeploy (the counter lives on the temporary disk) |
| OpenAI monthly limit (step 1) | Hard stop on your OpenAI account | Monthly |

**Worst case in a month = the OpenAI-side limit.** Per run it is `BUDGET_USD_TOTAL` plus a little overshoot. A stranger cannot restart your service, but every wake-up after sleep gets a fresh counter, so set the OpenAI limit to what you can afford to lose and treat `BUDGET_USD_TOTAL` as the day-to-day guard.

How the estimate is built (no database change): stored answer costs (from the token counts OpenAI returns and `pricing.py`) + 0.40 USD per indexed document (`INDEX_COST_ESTIMATE_USD`) + 0.08 USD per completed scoring (`EVAL_COST_ESTIMATE_USD`). An answer whose model is missing from the price table is charged 0.50 USD, a failed one 0.10 USD. A deleted chat's spend is moved to a small ledger file first, so "upload, ask, delete, repeat" is not free.

### Data handling, honestly

* The **text of the PDF pages the assistant reads, your questions and the answers** are sent to OpenAI (index summaries, answers, scoring). OpenAI retains API requests for a limited time under its own policy (the Responses API stores them by default). The bundled README says so to everyone who finds the repository.
* Uploaded files, indexes and chats are kept only on the instance's **temporary disk** and vanish on sleep/restart. They are not backed up and not encrypted at rest.
* Anyone with the access code can read any chat on the running service; the owner can read the Render logs (they contain request lines and visitor addresses, never the code, the key or document text).
* Render and GitHub see your code and traffic metadata as providers. If a document must stay confidential, do not upload it to a public demo; run ReportLens on your own PC (`run.ps1`).

---

## D. Model-cost presets for a public demo

The defaults stay as they are in `.env.example` (and the README). These are the Render *environment variables* to change when cost matters more than the last bit of answer quality.

Measured so far (your first live runs, `gpt-5.6-sol` at `medium`): a question costs **0.27-0.64 USD**; indexing a report costs about **0.30 USD**; scoring a few cents on top (the budget charges 0.08). The README's cost table lists the earlier estimates.

| Preset | Variables | Effect |
|---|---|---|
| **Default (quality)** | nothing to set | `gpt-5.6-sol`, `medium`, scoring on. The numbers above. |
| **Cheaper answers** | `PI_CHAT_MODEL=gpt-5.6-terra`, `PI_CHAT_REASONING_EFFORT=low` | List price per million tokens drops from 4 / 20 USD (input / output) to 2 / 12 USD, and less reasoning is generated. Real saving on your documents not measured yet; check a few questions in the OpenAI usage page before relying on it. Answer quality can drop on multi-step questions. |
| **No scoring** | `EVAL_ENABLED=false` | No RAGAS judge calls at all: saves a few cents per answer, removes 10-60 s (minutes on the free CPU) of background work **and about 100 MB of memory** (the scoring libraries are never loaded). The score card is simply absent. |
| **Cheapest sensible demo** | `PI_CHAT_MODEL=gpt-5.6-terra`, `PI_CHAT_REASONING_EFFORT=low`, `EVAL_ENABLED=false`, `EVAL_COST_ESTIMATE_USD=0` | Combine the three. |
| **Tighter fences** | `MAX_UPLOAD_MB=10`, `MAX_PAGES=250`, `BUDGET_USD_TOTAL=5`, `QUESTIONS_PER_HOUR_PER_IP=8` | Smaller documents mean cheaper indexing, faster indexing on the free CPU and cheaper questions (the outline is re-read every question, so cost grows with page count). |

Model names follow the PageIndex documentation and OpenAI's catalogue; confirm your key can use them (`scripts\live_smoke.py` has a free model-access check). When you change the answer model, the budget keeps working as long as the model is in `reportlens\pricing.py`; otherwise every answer is charged the 0.50 USD fallback.

---

## E. Reference

### Environment variables for a public deployment

`render.yaml` sets the ones in **bold**.

| Name | Default | Meaning |
|---|---|---|
| **`OPENAI_API_KEY`** | - | Secret (typed in Render). The only key. |
| **`ACCESS_CODE`** | empty (no gate) | Secret (typed in Render). Shared code; cookie lasts 12 h. |
| **`SESSION_SECRET`** | random per start | Generated by Render. Keeps logins valid across sleeps. |
| **`PUBLIC_MODE`** | `0` (image: `1`) | Fills the defaults below that you did not set |
| **`LOW_MEMORY`** | automatic | `1` = small-host mode (child-process indexing, light libraries, one open PDF, lower concurrency); automatic with `PUBLIC_MODE` in a container limited to <= 600 MB; `0` turns it off |
| `INDEX_IN_SUBPROCESS`, `LITE_LLM`, `MAX_OPEN_DOCS` | follow `LOW_MEMORY` | Individual parts of the small-host mode |
| **`BUDGET_USD_TOTAL`** | `0` (public: `10`) | Estimated USD at which uploads/questions stop; `0` = unlimited |
| **`MAX_SESSIONS`** | `0` (public: `30`; Blueprint: `10`) | Chats at once |
| **`QUESTIONS_PER_HOUR_PER_IP`** | `0` (public: `15`) | Per visitor address |
| **`MAX_UPLOAD_MB`** | `100` (public: `25`) | Upload size cap |
| **`MAX_PAGES`** | `1200` (public: `400`) | PDF page cap |
| `PI_INDEX_FALLBACK_STANDARD` | `true` (public: `false`) | PDFs without bookmarks are refused instead of indexed the slow, costly way |
| `PI_INDEX_SUMMARY_CONCURRENCY` | `16` (`LOW_MEMORY`: `6`) | Parallel summary calls while indexing |
| `DEFAULT_QUESTIONS` | five built-in questions | The preset set offered after an upload (visitors edit it): one per line or separated by `||` |
| `MAX_BATCH_QUESTIONS` | `10` | Questions one "Run all" may carry (a set also costs one `QUESTIONS_PER_HOUR_PER_IP` slot per question) |
| `BATCH_CONCURRENCY` | `3` (`LOW_MEMORY`: `2`) | Questions of a set the agent works on at once (1-6); see the measurements in part B |
| `EVAL_MAX_CONTEXTS`, `EVAL_CONCURRENCY` | `12`/`8` (public: `8`; `LOW_MEMORY`: `6`/`3`) | Pages scored per answer, parallel judge calls |
| `INDEX_COST_ESTIMATE_USD` / `EVAL_COST_ESTIMATE_USD` | `0.40` / `0.08` | What the budget charges |
| **`ALLOWED_HOSTS`** | empty | Comma list; `.onrender.com` also matches every subdomain |
| **`TRUST_PROXY`** | `0` | Believe `X-Forwarded-For/-Host/-Proto` (hosts only) |
| `PROXY_HOPS` | `0` | `0` = first forwarded address; `N` = Nth from the right |
| `PORT` | `7860` in the image | Render injects its own (10000 on the free tier); `REPORTLENS_PORT` overrides `PORT`. Do not set it. |
| **`REPORTLENS_DATA_DIR`** | `/tmp/reportlens` in the image | Database, uploads, indexes (temporary) |
| **`MALLOC_ARENA_MAX`** | `2` in the image | glibc keeps two heaps instead of one per CPU: less memory |
| **`ACCESS_REQUEST_EMAIL`** | empty | Shown on the code screen: "No code yet? Email ... to request one." |
| **`VISITOR_KEYS`** | `optional` | Visitors' own provider keys: `off`, `optional`, `required` (your key is never used for visitors) |
| `PRIVATE_CHATS` | on with `ACCESS_CODE` | Each browser sees only its own chats |
| `ALLOW_CUSTOM_LLM_URL` | off with `PUBLIC_MODE` | Lets visitors name any model URL (Ollama, a gateway). Keep it off on a public server (SSRF) |
| `DEMO_DIR` | `/app/demo` in the image | The read-only demo chat; empty = none |
| `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_BASE_URL` | `openai` | Run the whole deployment on another provider (then set `PI_CHAT_MODEL` too) |

With `PUBLIC_MODE=1` and no `ALLOWED_HOSTS` the app accepts any Host header (the Origin check still runs) and logs a warning.

### Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Build "stuck" for over 20 minutes or *Build failed* | Open the service's **Logs**. A network blip during `pip install` is the usual cause: **Manual Deploy -> Clear build cache & deploy**. If the log says the build was *killed* or ran out of memory, Render's free builder is too small for this image: try again, or build the image yourself (part F) and deploy it as an "existing image". |
| *Deploy failed* / "No open ports detected" | Look at the last log lines. `Cannot create the data folder` -> set `REPORTLENS_DATA_DIR=/tmp/reportlens`. A Python traceback -> send it to the developer. Do not set `PORT`. |
| Page shows `invalid_host` | `ALLOWED_HOSTS` does not match the address in the browser. Use `.onrender.com`, or your custom domain (add it to the list). |
| Login works but the next click says "Enter the access code" again | The host is not https-aware (`TRUST_PROXY=1` missing) or the service restarted without `SESSION_SECRET`. Check both variables. |
| The page says "The demo restarted (free hosting sleeps)" | The service slept (15 idle minutes) and woke up empty. Start a new chat and upload again. This is expected on the free plan. |
| Indexing ends with "ran out of memory" | The report is too large for 512 MB at this moment (another chat was being scored, or the report is unusually dense). Try again when the demo is quiet, use a shorter report, lower `MAX_PAGES`, or upgrade the instance (a paid 2 GB instance has no such limit). |
| The service restarts by itself and chats vanish | Render restarted it (free instances may be restarted at any time, or the memory limit was hit). Check **Events** and the memory graph; see part F. |
| Everyone sees "usage budget has been used up" | The estimate reached `BUDGET_USD_TOTAL`. restarting the service (Render's service menu) clears the counter, or raise the variable; check the OpenAI usage page first. |
| "Too many wrong access codes" | The address was locked for up to ten minutes; wait, or restart the service. |
| Indexing fails with "no bookmarks" | Public mode refuses PDFs without an outline (they would cost 0.3-0.7 USD and, on this CPU, an hour). Set `PI_INDEX_FALLBACK_STANDARD=true` only if you accept that. |
| Questions fail with an OpenAI auth/model error | The key or the model name is wrong for that project: fix the variable or `PI_CHAT_MODEL`. |

### Rehearse Render's limits on your own PC (optional)

With Docker Desktop running:

```powershell
cd "C:\Users\ayush\Annual Report Answering"
docker build -t reportlens:render .
.venv\Scripts\python.exe scripts\render_limits_test.py --image reportlens:render --pdf "C:\path\to\a\report.pdf" --restart-check
```

The script starts the image with `--memory=512m --memory-swap=512m --cpus=0.1`, `PUBLIC_MODE=1`, the fake OpenAI server and a throw-away
access code, logs in, uploads the PDF, waits for indexing, asks two questions over the event stream, waits for the scoring, fetches the PDF
with a Range request, optionally restarts the container (a Render sleep/wake) and prints the table of part B. It exits with 1 if anything
failed or the kernel killed a process. Nothing is sent anywhere and nothing is billed. Your PDF is only uploaded to that local container.
The question set: `--batch` asks the server's default questions through `POST /batch` on a fresh copy of the indexed chat (instead of the two
single questions) and prints, per question, when the first token, the answer and the scores arrived; `--batch-compare` asks them one after
another through `/messages` first; `--batch-sweep seq,1,2,3 --mock-delay-ms 100` is the experiment of part B (it indexes once into a Docker
volume, restarts the container per mode with `BATCH_CONCURRENCY=n`, and prints one comparison table with the peak memory, the cgroup peak, the OOM
counter and the CPU use of every run; `--keep` leaves the indexed volume for `--reuse-volume NAME`).
Never put a real key on a `docker run` command line you keep in your shell history.

---

## F. What may still fail on the real Render (unverified)

* **The question-set timings come from the fake model.** `REPORTLENS_MOCK_DELAY_MS` imitates the model's latency, but real OpenAI latencies, rate limits (several agent runs share one key's tokens-per-minute) and streaming vary. With real calls the waiting share is larger, so parallelism should help a little more than measured; the 429 behaviour of `BATCH_CONCURRENCY=3` on a small OpenAI tier was not tried.
* **Real OpenAI instead of the fake server.** Memory use differs a little: real responses are longer, HTTPS adds buffers, and the real tree has different node counts. The headroom in part B is there for this, but it was not measured with real calls. Check the `memory ...` log lines on your first real upload and answer.
* **The `/chat/completions` shortcut for indexing.** On a small host the indexing child sends the model calls itself instead of through litellm (same request, tested against the fake server request for request). If OpenAI answers that your index model is not served by `/chat/completions`, it automatically loads litellm for the rest of that job (about 150 MB more memory; the log says so).
* **Render may restart or move a free service at any time**, and may be slower or faster than 0.1 CPU in practice (it is a shared, burstable limit). Everything on the instance is lost when that happens; visitors see the "demo restarted" message.
* **Build memory on Render's builders.** The image installs about 1 GB of wheels with no compiler. If a free build is killed, build the image on your PC and push it to a registry, then deploy "existing image" (not covered here).
* **Proxy details.** Per-visitor limits assume Render's proxy puts the visitor's address first in `X-Forwarded-For` (see the note in part C); Render's own documentation says the 15-minute timer counts inbound traffic, but whether its health checks count as traffic was not checked here (if they did, the service would never sleep and would use all 744 monthly hours: watch the hours on the Billing page in the first days).
* **Free-tier terms change.** The 750 hours, the 15-minute sleep, the memory and CPU figures come from Render's documentation and your brief; check the current free-tier page before you rely on them.
* **Long reports under real latency.** A 300-page report was indexed in 9 minutes against the instant fake server; real model latency adds minutes. The 90-minute job limit leaves a wide margin, but visitors must keep the tab open (the page polls; closing it does not stop the job, but the chat is only reachable while the service stays awake).

---

## Alternatives

* **Hugging Face Spaces (Docker).** Hugging Face now charges for Docker Spaces (October 2026), so it is no longer a free path. The same image works there: `.\scripts\make_deploy_bundle.ps1 -Target hf` builds a Space-flavoured folder (README front matter, port 7860); set `ALLOWED_HOSTS=.hf.space`, `TRUST_PROXY=1` and the secrets in the Space's *Variables and secrets*, push it with a write token you create yourself, and open the direct `*.hf.space` link (not the embedded page on huggingface.co). A paid CPU Space has 16 GB of memory, so `LOW_MEMORY` is not needed there (set `LOW_MEMORY=0`).
* **Any other Docker host** (Fly.io, Google Cloud Run, a small VPS): same `Dockerfile`, same variables; `PORT` is honoured. With 1 GB or more of memory set `LOW_MEMORY=0` for faster indexing and an unchanged feature set.
