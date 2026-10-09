"""Drive the whole ReportLens flow against a server and report time and memory - built to answer "does it fit Render's free tier?".

Two ways to run it (never needs a real OpenAI key: use the built-in mock, REPORTLENS_DEMO_MOCK=1):

  # 1. let the script start a Docker container with Render-free-like limits (512 MB RAM, swap off, 0.1 CPU) and tear it down
  python scripts/render_limits_test.py --image reportlens:render --pdf "C:\\path\\to\\report.pdf"

  # 2. test a server that is already running (no memory figures unless you also pass --container NAME)
  python scripts/render_limits_test.py --base-url http://127.0.0.1:7860 --code test --pdf report.pdf [--container NAME]

Flow: wait for /api/health (cold start) -> load the page -> login -> create a chat -> upload the PDF -> poll until it is indexed
-> ask two questions over SSE (counting pings and the longest silence) -> wait for the answer scoring (RAGAS) -> fetch the PDF
with a Range request -> settle and read the idle memory -> (--restart-check) replace the container with a fresh one from the same
image, as a Render sleep/wake does, and check the app comes back empty with the login still valid.  Prints a table; exit code 1
when something failed or the container was OOM-killed.  The report you give it stays on your machine (it is only uploaded to the local test server).

Question set ("Run all"): `--batch` asks the server's default questions (or your --question list) through POST /batch on a fresh copy of the
indexed chat and prints, per question, when the first token, the answer and the scores arrived, the wall time to all answers and to
all scores, and the memory peak.  `--batch-compare` does the same with the questions asked one after another through /messages first.
`--batch-sweep seq,1,2,3` (with --image) is the full experiment: it indexes the report ONCE into a Docker volume, then for every entry restarts
the container (same limits, BATCH_CONCURRENCY=n; `seq` = one question after another through /messages), runs the set on a fresh copy of the chat
and prints one comparison table.  `--mock-delay-ms` makes the fake model slow like a real one (OpenAI/network wait does not use the 0.1 CPU).

Memory comes from the container's cgroup (what Render's limit is enforced on): `docker stats` is sampled for the per-phase peak,
`memory.peak` / `memory.events` are read once, before the restart check.  Only the Python standard library and httpx are used.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

DEFAULT_QUESTIONS = (
    "What was the group's profit before tax and what were the main reasons for the change?",
    "What is the total dividend per share and on which page is it reported?",
)
RENDER_FREE_MEM_MB = 512
UNIT = {"b": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3}


# ------------------------------------------------------------------------------------------------ docker helpers
def docker(*args: str, check: bool = True, timeout: float = 60) -> str:
    res = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed: {res.stderr.strip()[:300]}")
    return res.stdout.strip()


def parse_size(text: str) -> float:
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", text)
    return float(m.group(1)) * UNIT.get(m.group(2).lower(), 1) / 1024 ** 2 if m else float("nan")   # MiB


class MemorySampler(threading.Thread):
    """Polls `docker stats` for the container: (monotonic time, MiB used).  `docker stats` reports usage without the page cache."""

    def __init__(self, container: str):
        super().__init__(daemon=True, name="mem-sampler")
        self.container = container
        self.samples: list[tuple[float, float]] = []
        self.cpu: list[tuple[float, float]] = []                  # (monotonic time, % of ONE cpu: 10 = the whole 0.1 cpu limit)
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                out = docker("stats", "--no-stream", "--format", "{{.MemUsage}}|{{.CPUPerc}}", self.container, check=False, timeout=30)
                if out:
                    mem, _, cpu = out.partition("|")
                    now = time.monotonic()
                    self.samples.append((now, parse_size(mem.split("/")[0])))
                    try:
                        self.cpu.append((now, float(cpu.strip().rstrip("%"))))
                    except ValueError:
                        pass
            except Exception:  # noqa: BLE001 - sampling must never break the test
                pass
            self._stop.wait(0.5)

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=35)

    def peak(self, start: float, end: float) -> Optional[float]:
        values = [v for t, v in self.samples if start <= t <= end and v == v]
        return max(values) if values else None

    def last(self) -> Optional[float]:
        return self.samples[-1][1] if self.samples else None

    def mean_cpu(self, start: float, end: float) -> Optional[float]:
        values = [v for t, v in self.cpu if start <= t <= end]
        return sum(values) / len(values) if values else None


def cgroup_read(container: str, path: str) -> Optional[str]:
    try:
        return docker("exec", container, "cat", path, check=False, timeout=30) or None
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------------------------------------ result table
@dataclass
class Row:
    name: str
    seconds: Optional[float] = None
    peak_mib: Optional[float] = None
    note: str = ""
    ok: bool = True


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def add(self, row: Row) -> Row:
        self.rows.append(row)
        if not row.ok:
            self.failures.append(f"{row.name}: {row.note}")
        return row

    def print(self) -> None:
        width = max(len(r.name) for r in self.rows) + 2
        print()
        print(f"{'step'.ljust(width)}{'seconds':>10}{'peak MiB':>11}  note")
        print("-" * (width + 21 + 40))
        for r in self.rows:
            secs = "-" if r.seconds is None else f"{r.seconds:.1f}"
            mem = "-" if r.peak_mib is None else f"{r.peak_mib:.0f}"
            print(f"{r.name.ljust(width)}{secs:>10}{mem:>11}  {'' if r.ok else 'FAILED: '}{r.note}")
        print()
        for key, value in self.facts.items():
            print(f"{key}: {value}")
        print("RESULT:", "FAIL - " + "; ".join(self.failures) if self.failures else "PASS")


# ------------------------------------------------------------------------------------------------ the flow
class Driver:
    def __init__(self, base: str, code: str, report: Report, sampler: Optional[MemorySampler]):
        self.client = httpx.Client(base_url=base, timeout=httpx.Timeout(60.0, read=1800.0), follow_redirects=False)
        self.code = code
        self.report = report
        self.sampler = sampler

    def step(self, name: str, fn, *, note_fn=None) -> Row:
        start = time.monotonic()
        row = Row(name)
        try:
            result = fn()
            row.note = (note_fn(result) if note_fn else (result or "")) or ""
        except Exception as exc:  # noqa: BLE001
            row.ok, row.note = False, f"{type(exc).__name__}: {str(exc)[:200]}"
        end = time.monotonic()
        row.seconds = end - start
        if self.sampler:
            row.peak_mib = self.sampler.peak(start - 0.5, end)
        return self.report.add(row)

    def login(self) -> str:
        r = self.client.post("/api/login", json={"code": self.code})
        if r.status_code != 200:
            raise RuntimeError(f"login HTTP {r.status_code}: {r.text[:120]}")
        return "cookie set"

    def create_session(self) -> str:
        r = self.client.post("/api/sessions")
        r.raise_for_status()
        self.sid = r.json()["id"]
        return self.sid[:8]

    def upload(self, pdf: Path) -> str:
        with pdf.open("rb") as fh:
            r = self.client.post(f"/api/sessions/{self.sid}/document", files={"file": (pdf.name, fh, "application/pdf")})
        if r.status_code not in (200, 202):
            raise RuntimeError(f"upload HTTP {r.status_code}: {r.text[:200]}")
        return f"{pdf.stat().st_size / 1024 / 1024:.1f} MB accepted"

    def wait_ready(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        stages: list[str] = []
        began = time.monotonic()
        failures = 0
        while time.monotonic() < deadline:
            try:
                r = self.client.get(f"/api/sessions/{self.sid}", timeout=60)
                failures = 0
            except httpx.HTTPError:
                failures += 1
                if failures > 20:
                    raise RuntimeError("the server stopped answering while indexing (killed? OOM?)")
                time.sleep(3)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"poll HTTP {r.status_code}: {r.text[:120]}")
            doc = r.json().get("document") or {}
            stage = f"{doc.get('stage')}"
            if not stages or not stages[-1].startswith(stage + "@"):
                stages.append(f"{stage}@{time.monotonic() - began:.0f}s")
            if doc.get("status") == "ready":
                self.doc = doc
                return f"{doc.get('page_count')} pages, {doc.get('node_count')} nodes; stages (first seen): {' > '.join(stages)}"
            if doc.get("status") == "failed":
                raise RuntimeError(f"indexing failed: {doc.get('error')}")
            time.sleep(2)
        raise TimeoutError(f"not ready after {timeout:.0f} s (stage {stages[-1] if stages else '?'})")

    def ask(self, question: str) -> str:
        """POST the question and read the SSE stream to the end.  Fails on an error event or a silence longer than 60 s."""
        start = time.monotonic()
        marks: dict[str, float] = {}
        last = start
        longest_gap = 0.0
        pings = 0
        tokens = 0
        error: Optional[str] = None
        eval_status = "-"
        event = ""
        with self.client.stream("POST", f"/api/sessions/{self.sid}/messages", json={"content": question},
                                headers={"Accept": "text/event-stream"}) as r:
            if r.status_code != 200:
                raise RuntimeError(f"ask HTTP {r.status_code}: {r.read().decode()[:200]}")
            for line in r.iter_lines():
                now = time.monotonic()
                longest_gap = max(longest_gap, now - last)
                last = now
                if line.startswith(": ping"):
                    pings += 1
                elif line.startswith("event:"):
                    event = line[6:].strip()
                    marks.setdefault(event, now - start)
                    if event == "token":
                        tokens += 1
                elif line.startswith("data:") and event == "error":
                    error = line[5:].strip()[:200]
                elif line.startswith("data:") and event == "eval_done":
                    try:
                        eval_status = str(json.loads(line[5:]).get("evaluation", {}).get("status"))
                    except ValueError:
                        eval_status = "unreadable"
        if error:
            raise RuntimeError(f"error event: {error}")
        if "done" not in marks:
            raise RuntimeError("stream ended without a done event")
        if "eval_done" in marks and eval_status not in ("done", "partial", "skipped"):
            raise RuntimeError(f"scoring ended with status {eval_status!r}")
        if longest_gap > 60:
            raise RuntimeError(f"stream silent for {longest_gap:.0f} s")
        return (f"answer_done {marks.get('answer_done', float('nan')):.1f} s, eval_done {marks.get('eval_done', float('nan')):.1f} s (scores: {eval_status}), "
                f"{tokens} token events, {pings} pings, longest silence {longest_gap:.1f} s")

    def ask_in(self, sid: str, question: str) -> str:
        """`ask` in another chat than the one this driver created."""
        keep, self.sid = self.sid, sid
        try:
            return self.ask(question)
        finally:
            self.sid = keep

    def clone_chat(self, source: str) -> str:
        """A fresh chat over the same already-indexed report (free: the index is copied, nothing is re-built)."""
        r = self.client.post("/api/sessions", json={"from_session": source})
        if r.status_code != 201:
            raise RuntimeError(f"clone HTTP {r.status_code}: {r.text[:160]}")
        return r.json()["id"]

    def default_questions(self) -> list[str]:
        return list(self.client.get("/api/config").json().get("default_questions") or DEFAULT_QUESTIONS)

    def timed_stream(self, sid: str, path: str, body: dict) -> dict:
        """POST and read an SSE stream, noting when each question's first token / answer / scores arrived (seconds since the POST)."""
        start = time.monotonic()
        per: dict[int, dict] = {}
        marks: dict[str, float] = {}
        summary: dict = {}
        event = ""
        with self.client.stream("POST", f"/api/sessions/{sid}/{path}", json=body, headers={"Accept": "text/event-stream"}) as r:
            if r.status_code != 200:
                raise RuntimeError(f"{path} HTTP {r.status_code}: {r.read().decode()[:200]}")
            for line in r.iter_lines():
                now = time.monotonic() - start
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        data = json.loads(line[5:])
                    except ValueError:
                        continue
                    index = data.get("index", 0) if isinstance(data, dict) else 0
                    if event in ("batch_start", "batch_done", "done"):
                        marks.setdefault(event, now)
                        if event == "batch_done":
                            summary = data
                        continue
                    row = per.setdefault(index, {})
                    if event == "token":
                        row.setdefault("first_token", now)
                    elif event in ("answer_done", "eval_done", "error"):
                        row[event] = now
                    if event == "error":
                        row["error"] = str(data.get("code"))
                    elif event == "eval_done":
                        row["eval_status"] = str((data.get("evaluation") or {}).get("status"))
        return {"per": per, "marks": marks, "summary": summary, "wall": time.monotonic() - start}

    def run_batch(self, sid: str, questions: list[str]) -> dict:
        out = self.timed_stream(sid, "batch", {"questions": questions})
        out["answers_wall"] = out["marks"].get("batch_done", out["wall"])
        return out

    def run_sequential(self, sid: str, questions: list[str]) -> dict:
        """The same questions one after another through /messages (same chat, so follow-ups carry history, like a person asking)."""
        per: dict[int, dict] = {}
        offset = 0.0
        answers_end = 0.0
        for i, question in enumerate(questions):
            one = self.timed_stream(sid, "messages", {"content": question})
            row = {k: v + offset for k, v in one["per"].get(0, {}).items() if isinstance(v, float)}
            row.update({k: v for k, v in one["per"].get(0, {}).items() if not isinstance(v, float)})
            per[i] = row
            answers_end = row.get("answer_done", answers_end)
            offset += one["wall"]
        return {"per": per, "marks": {"done": offset}, "summary": {}, "wall": offset, "answers_wall": answers_end}

    def pdf_range(self) -> str:
        r = self.client.get(f"/api/sessions/{self.sid}/document/file", headers={"Range": "bytes=0-1023"})
        if r.status_code != 206 or len(r.content) != 1024 or not r.content.startswith(b"%PDF"):
            raise RuntimeError(f"Range request answered HTTP {r.status_code}, {len(r.content)} bytes")
        total = r.headers.get("content-range", "")
        full = self.client.get(f"/api/sessions/{self.sid}/document/file")
        return f"206 ok ({total}); full download HTTP {full.status_code}, {len(full.content) / 1024 / 1024:.1f} MB"


def container_facts(container: str, sampler: Optional[MemorySampler], report: Report) -> None:
    """Peak memory, OOM kills and error lines of the container so far (into report.facts / report.failures)."""
    peak_stats = max((v for _, v in sampler.samples if v == v), default=None) if sampler else None
    cg_peak = cgroup_read(container, "/sys/fs/cgroup/memory.peak")
    cg_max = cgroup_read(container, "/sys/fs/cgroup/memory.max")
    events = cgroup_read(container, "/sys/fs/cgroup/memory.events") or ""
    oom_kills = re.search(r"oom_kill (\d+)", events)
    state = docker("inspect", "-f", "{{.State.OOMKilled}} {{.RestartCount}} {{.State.Status}}", container, check=False)
    oomed = state.startswith("true") or bool(oom_kills and int(oom_kills.group(1)) > 0)
    report.facts["peak memory (docker stats, no page cache)"] = f"{peak_stats:.0f} MiB of {RENDER_FREE_MEM_MB} MiB" if peak_stats else "n/a"
    if cg_peak and cg_peak.isdigit():
        report.facts["cgroup memory.peak (incl. page cache)"] = f"{int(cg_peak) / 1024 ** 2:.0f} MiB (limit {int(cg_max) / 1024 ** 2:.0f} MiB)" if cg_max and cg_max.isdigit() else f"{int(cg_peak) / 1024 ** 2:.0f} MiB"
    report.facts["OOM kills (cgroup) / killed / restarts / state"] = f"{oom_kills.group(1) if oom_kills else '?'} / {'YES' if oomed else 'no'} / {state}"
    if oomed:
        report.failures.append("container was OOM-killed")
    logs = docker("logs", "--tail", "400", container, check=False)
    bad = [ln for ln in logs.splitlines() if re.search(r"\b(ERROR|Traceback|CRITICAL)\b", ln)]
    report.facts["ERROR/Traceback lines in the last 400 log lines"] = str(len(bad))
    for ln in bad[:5]:
        print("  log:", ln[:200])
    mem_lines = [ln for ln in logs.splitlines() if re.search(r"memory (after|before)|indexing child of session", ln)]
    for ln in mem_lines[-8:]:
        print("  mem:", ln[:220])


def _fmt(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.1f}"


def batch_failures(result: dict, expected: int, label: str) -> list[str]:
    problems = [f"{label}: question {i + 1} ended with {row['error']}" for i, row in sorted(result["per"].items()) if "error" in row]
    problems += [f"{label}: question {i + 1} was scored {row['eval_status']!r}" for i, row in sorted(result["per"].items())
                 if row.get("eval_status") not in (None, "done", "partial", "skipped")]
    if len(result["per"]) != expected:
        problems.append(f"{label}: {len(result['per'])} of {expected} questions reported")
    return problems


def print_batch(label: str, questions: list[str], result: dict) -> None:
    print(f"\n{label}: seconds since the start; first token / answer complete / scores complete")
    print(f"  {'#':>2} {'first token':>12}{'answer':>9}{'scored':>9}  question")
    for i, question in enumerate(questions):
        row = result["per"].get(i, {})
        note = f"  ERROR {row['error']}" if "error" in row else ""
        print(f"  {i + 1:>2} {_fmt(row.get('first_token')):>12}{_fmt(row.get('answer_done')):>9}{_fmt(row.get('eval_done')):>9}  {question[:60]}{note}")
    print(f"  all answers in: {result['answers_wall']:.1f} s   all scores in (stream closed): {result['wall']:.1f} s")


@dataclass
class VariantResult:
    mode: str
    result: Optional[dict] = None
    peak_stats: Optional[float] = None
    peak_cgroup: Optional[float] = None
    oom: bool = False
    cpu: Optional[float] = None
    problems: list[str] = field(default_factory=list)


def read_peaks(container: str, sampler: Optional[MemorySampler], start: float, end: float) -> tuple[Optional[float], Optional[float], bool, Optional[float]]:
    """(docker-stats peak MiB, cgroup memory.peak MiB, OOM-killed?, mean CPU %) of a container for the window start..end."""
    cg_peak = cgroup_read(container, "/sys/fs/cgroup/memory.peak")
    events = cgroup_read(container, "/sys/fs/cgroup/memory.events") or ""
    oom_kills = re.search(r"oom_kill (\d+)", events)
    state = docker("inspect", "-f", "{{.State.OOMKilled}}", container, check=False)
    oomed = state.startswith("true") or bool(oom_kills and int(oom_kills.group(1)) > 0)
    return (sampler.peak(start - 0.5, end) if sampler else None,
            int(cg_peak) / 1024 ** 2 if cg_peak and cg_peak.isdigit() else None, oomed,
            sampler.mean_cpu(start, end) if sampler else None)


def print_sweep(variants: list[VariantResult], questions: list[str]) -> None:
    print()
    for v in variants:
        if v.result:
            print_batch(f"[{v.mode}]", questions, v.result)
    print("\nSUMMARY (seconds since the first request of the set)")
    print(f"{'mode':<10}{'answers in':>11}{'+ scores':>10}{'mean 1st token':>16}{'peak MiB':>10}{'cgroup MiB':>12}{'cpu %':>7}  notes")
    for v in variants:
        r = v.result
        firsts = [row["first_token"] for row in (r or {}).get("per", {}).values() if "first_token" in row]
        notes = "OOM-KILLED " if v.oom else ""
        notes += "; ".join(v.problems)
        print(f"{v.mode:<10}{_fmt(r['answers_wall'] if r else None):>11}{_fmt(r['wall'] if r else None):>10}{_fmt(sum(firsts) / len(firsts) if firsts else None):>16}"
              f"{_fmt(v.peak_stats):>10}{_fmt(v.peak_cgroup):>12}{_fmt(v.cpu):>7}  {notes}")


def run_sweep(args: argparse.Namespace) -> int:
    """Index the report once into a Docker volume, then measure the question set in a fresh container per mode (see the module docstring)."""
    modes = [m.strip().lower() for m in args.batch_sweep.split(",") if m.strip()]
    if not args.image or not modes or any(m != "seq" and not m.isdigit() for m in modes):
        print("--batch-sweep needs --image and a list like seq,1,2,3", file=sys.stderr)
        return 2
    stamp = int(time.time())
    volume, name = args.reuse_volume or f"rl-sweep-{stamp}", f"rl-sweep-{stamp}"
    if not args.reuse_volume:
        docker("volume", "create", volume)
        docker("run", "--rm", "--user", "root", "-v", f"{volume}:/data", "--entrypoint", "chown", args.image, "1000:1000", "/data", timeout=120)
    base_env = {"REPORTLENS_DATA_DIR": "/data", "BUDGET_USD_TOTAL": "0", "QUESTIONS_PER_HOUR_PER_IP": "0", "MAX_SESSIONS": "0",
                "REPORTLENS_MOCK_DELAY_MS": str(args.mock_delay_ms)}
    if args.reuse_volume:
        base_env["PRIVATE_CHATS"] = "0"              # the chat belongs to the visitor id of the earlier run's cookie jar: show every chat instead
    variants: list[VariantResult] = []
    questions: list[str] = list(args.question or [])
    failures: list[str] = []
    try:
        base, _ = start_container(args, name, base_env, volume)
        wait_health(base, 600)
        drv = Driver(base, args.code, Report(), None)
        drv.login()
        if args.reuse_volume:                        # a report indexed by an earlier sweep (--keep): its untouched chat is the source
            ready = [s for s in drv.client.get("/api/sessions").json() if s.get("state") == "ready"]
            if not ready:
                raise RuntimeError(f"volume {volume} holds no indexed, unasked chat")
            drv.sid = source = ready[0]["id"]
            print("reusing the report indexed in", volume, flush=True)
        else:
            drv.create_session()
            drv.upload(args.pdf)
            print("indexing once ...", flush=True)
            began = time.monotonic()
            print("  ", drv.wait_ready(args.index_timeout), f"({time.monotonic() - began:.0f} s)", flush=True)
            source = drv.sid
        for mode in modes:
            docker("rm", "-f", name, check=False, timeout=120)
            env = dict(base_env)
            if mode != "seq":
                env["BATCH_CONCURRENCY"] = mode
            base, _ = start_container(args, name, env, volume)
            wait_health(base, 600)
            drv.client.base_url = httpx.URL(base)
            variant = VariantResult("sequential" if mode == "seq" else f"batch x{mode}")
            variants.append(variant)
            sampler = MemorySampler(name)
            sampler.start()
            try:
                drv.login()
                questions = questions or drv.default_questions()
                if not args.no_warmup:                       # the first answer after a start loads the answer libraries (about 45 s of CPU): not part of the comparison
                    drv.ask_in(drv.clone_chat(source), questions[0])
                chat = drv.clone_chat(source)
                time.sleep(2)
                start = time.monotonic()
                variant.result = drv.run_sequential(chat, questions) if mode == "seq" else drv.run_batch(chat, questions)
                end = time.monotonic()
                time.sleep(1.5)
                variant.peak_stats, variant.peak_cgroup, variant.oom, variant.cpu = read_peaks(name, sampler, start, end)
                variant.problems = batch_failures(variant.result, len(questions), variant.mode)
            except Exception as exc:  # noqa: BLE001
                variant.problems.append(f"{type(exc).__name__}: {str(exc)[:160]}")
            finally:
                sampler.stop()
            failures += [f"{variant.mode}: {p}" for p in variant.problems] + ([f"{variant.mode}: OOM-killed"] if variant.oom else [])
        print_sweep(variants, questions)
        if args.json:
            args.json.write_text(json.dumps([{"mode": v.mode, "answers_wall": (v.result or {}).get("answers_wall"), "wall": (v.result or {}).get("wall"),
                                              "per": (v.result or {}).get("per"), "peak_stats_mib": v.peak_stats, "peak_cgroup_mib": v.peak_cgroup,
                                              "oom": v.oom, "cpu_percent": v.cpu, "problems": v.problems} for v in variants], indent=2))
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{type(exc).__name__}: {exc}")
        print("FAILED:", exc, file=sys.stderr)
    finally:
        if not args.keep:
            docker("rm", "-f", name, check=False, timeout=120)
            if not args.reuse_volume:
                docker("volume", "rm", "-f", volume, check=False, timeout=60)
        else:
            print(f"kept: docker volume {volume} (the indexed report; rerun with --reuse-volume {volume}) and container {name}")
    print("RESULT:", "FAIL - " + "; ".join(failures) if failures else "PASS")
    return 1 if failures else 0


def wait_health(base: str, timeout: float) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            if httpx.get(base + "/api/health", timeout=5).status_code == 200:
                return time.monotonic() - start
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"/api/health did not answer within {timeout:.0f} s")


def start_container(args: argparse.Namespace, name: str, extra_env: Optional[dict[str, str]] = None, volume: Optional[str] = None) -> tuple[str, float]:
    env = {"PUBLIC_MODE": "1", "REPORTLENS_DEMO_MOCK": "1", "ACCESS_CODE": args.code, "TRUST_PROXY": "1", "PORT": "10000",
           "SESSION_SECRET": "test-only-secret-not-a-real-one"}      # lets the login cookie survive the restart, as on Render
    for item in args.env:
        key, _, value = item.partition("=")
        env[key] = value
    env.update(extra_env or {})
    cmd = ["run", "-d", "--name", name, f"--memory={args.memory}", f"--memory-swap={args.memory}", f"--cpus={args.cpus}",
           "-p", "127.0.0.1::10000"]
    if volume:
        cmd += ["-v", f"{volume}:/data"]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    started = time.monotonic()
    docker(*cmd, args.image)
    mapping = docker("port", name, "10000/tcp")
    port = mapping.splitlines()[0].rsplit(":", 1)[1]
    return f"http://127.0.0.1:{port}", started


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pdf", required=True, type=Path, help="the report to upload (stays on this machine)")
    p.add_argument("--image", help="Docker image to start with Render-free-like limits (then --base-url is not needed)")
    p.add_argument("--base-url", help="test a server that is already running")
    p.add_argument("--container", help="with --base-url: container name, to read its memory")
    p.add_argument("--code", default="test", help="access code of the test server (a throw-away value, default 'test')")
    p.add_argument("--memory", default="512m", help="--memory and --memory-swap for --image (default 512m: swap off, OOM is real)")
    p.add_argument("--cpus", default="0.1", help="--cpus for --image (default 0.1, Render free)")
    p.add_argument("--env", action="append", default=[], metavar="K=V", help="extra environment variable for --image (repeatable)")
    p.add_argument("--question", action="append", help="question to ask (repeatable; default: two generic annual-report questions)")
    p.add_argument("--index-timeout", type=float, default=3600.0, help="seconds to wait for indexing (default 3600)")
    p.add_argument("--settle", type=float, default=20.0, help="seconds of silence before the idle reading (default 20)")
    p.add_argument("--restart-check", action="store_true", help="with --image: replace the container at the end with a fresh one from the same image (what a Render sleep/wake does: empty disk) and check the vanished chat answers 404 session_not_found")
    p.add_argument("--second-chat", action="store_true", help="after the first chat, index the same PDF in a second chat and ask once more (worst case for memory)")
    p.add_argument("--batch", action="store_true", help="run the question set through POST /batch on a fresh copy of the indexed chat (instead of the two single questions) and print the per-question timings")
    p.add_argument("--batch-compare", action="store_true", help="with --batch: first ask the same questions one after another through /messages and compare")
    p.add_argument("--batch-sweep", metavar="MODES", help="with --image: index once, then measure each mode in a fresh container, e.g. seq,1,2,3 (seq = one by one through /messages; n = BATCH_CONCURRENCY=n)")
    p.add_argument("--mock-delay-ms", type=int, default=0, help="REPORTLENS_MOCK_DELAY_MS for the fake model (--image / --batch-sweep): sleep per streamed chunk; agent turns wait 10x before the first byte, like a real model")
    p.add_argument("--reuse-volume", metavar="NAME", help="--batch-sweep: use the Docker volume of an earlier sweep run with --keep (its report is already indexed: skips the long indexing)")
    p.add_argument("--no-warmup", action="store_true", help="--batch-sweep: skip the throw-away first question that loads the answer libraries before each measured run")
    p.add_argument("--keep", action="store_true", help="leave the container running afterwards")
    p.add_argument("--json", type=Path, help="also write the rows as JSON here")
    args = p.parse_args(argv)
    if bool(args.image) == bool(args.base_url):
        p.error("give exactly one of --image or --base-url")
    if not args.pdf.is_file():
        p.error(f"{args.pdf} not found")
    if args.batch_sweep:
        if not args.image:
            p.error("--batch-sweep needs --image")
        return run_sweep(args)
    if args.mock_delay_ms:
        args.env.append(f"REPORTLENS_MOCK_DELAY_MS={args.mock_delay_ms}")

    report = Report()
    container = args.container
    name = f"rl-limits-{int(time.time())}"
    started = time.monotonic()
    if args.image:
        container = name
        base, started = start_container(args, name)
    else:
        base = args.base_url.rstrip("/")
    sampler: Optional[MemorySampler] = None
    exit_code = 1
    try:
        if container:
            sampler = MemorySampler(container)
            sampler.start()
        t0 = time.monotonic()
        health_s = wait_health(base, 600)
        cold = time.monotonic() - started
        report.add(Row("cold start: container run -> /api/health 200" if args.image else "wait for /api/health", cold if args.image else health_s,
                       sampler.peak(t0 - 1, time.monotonic()) if sampler else None, f"{args.cpus} cpu, {args.memory}" if args.image else ""))
        drv = Driver(base, args.code, report, sampler)
        drv.step("first page load (GET /)", lambda: (lambda r: f"HTTP {r.status_code}, {len(r.content)} bytes")(drv.client.get("/")))
        drv.step("login", drv.login)
        drv.step("create chat", drv.create_session)
        drv.step("upload PDF", lambda: drv.upload(args.pdf))
        indexed = drv.step("indexing until ready", lambda: drv.wait_ready(args.index_timeout)).ok
        if indexed and args.batch:
            questions = list(args.question or drv.default_questions())
            source = drv.sid
            for label, runner in (([("sequential /messages", drv.run_sequential)] if args.batch_compare else []) + [("batch /batch", drv.run_batch)]):
                def one(label=label, runner=runner) -> str:
                    result = runner(drv.clone_chat(source), questions)
                    print_batch(label, questions, result)
                    problems = batch_failures(result, len(questions), label)
                    if problems:
                        raise RuntimeError("; ".join(problems))
                    return f"{len(questions)} questions: all answers in {result['answers_wall']:.1f} s, scored by {result['wall']:.1f} s"
                drv.step(f"question set: {label}", one)
            drv.step("PDF range request", drv.pdf_range)
        elif indexed:                                     # nothing else can work on a document that is not ready
            for i, question in enumerate(args.question or DEFAULT_QUESTIONS, 1):
                drv.step(f"question {i} (stream until done, incl. scoring)", lambda q=question: drv.ask(q))
            drv.step("PDF range request", drv.pdf_range)
            if args.second_chat:                          # worst case for memory: the web process already holds the answer + scoring libraries
                drv.step("2nd chat: create", drv.create_session)
                drv.step("2nd chat: upload PDF", lambda: drv.upload(args.pdf))
                if drv.step("2nd chat: indexing until ready", lambda: drv.wait_ready(args.index_timeout)).ok:
                    drv.step("2nd chat: question (stream until done, incl. scoring)", lambda: drv.ask((args.question or DEFAULT_QUESTIONS)[0]))
        time.sleep(args.settle)
        if sampler:
            idle = sampler.peak(time.monotonic() - min(args.settle, 5), time.monotonic())
            report.add(Row(f"idle after {args.settle:.0f} s", None, idle, "memory in use once everything is finished"))
        if container:                                     # before the restart check: a fresh container starts with fresh counters
            container_facts(container, sampler, report)
        if args.restart_check and args.image:
            # What Render does after 15 idle minutes: the instance is stopped and the next visitor gets a NEW one from the same image,
            # with an empty disk (`docker restart` would keep the disk, so it is not the same thing).  The cookie survives (SESSION_SECRET).
            old_sid = getattr(drv, "sid", "")
            before = time.monotonic()
            docker("rm", "-f", container, timeout=120)
            new_base, _ = start_container(args, container)

            def wake() -> str:
                drv.client.base_url = httpx.URL(new_base)
                woke = wait_health(new_base, 600)
                r = drv.client.get(f"/api/sessions/{old_sid}")
                body = r.json().get("error", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
                if r.status_code != 404 or body.get("code") != "session_not_found":
                    raise RuntimeError(f"expected 404 session_not_found for the vanished chat, got HTTP {r.status_code} {body}")
                listing = drv.client.get("/api/sessions")
                if listing.status_code != 200 or listing.json() != []:
                    raise RuntimeError(f"expected an empty chat list after the restart, got HTTP {listing.status_code} {listing.text[:80]}")
                return f"/api/health after {woke:.1f} s; old chat -> 404 session_not_found; login cookie still valid; chat list empty"
            row = drv.step("restart = sleep/wake: health, vanished chat, cookie", wake)
            row.seconds = time.monotonic() - before
        report.print()
        if args.json:
            args.json.write_text(json.dumps({"rows": [r.__dict__ for r in report.rows], "facts": report.facts, "failures": report.failures}, indent=2))
        exit_code = 1 if report.failures else 0
    except Exception as exc:  # noqa: BLE001
        report.failures.append(f"{type(exc).__name__}: {exc}")
        if report.rows:
            report.print()
        else:
            print("FAILED:", exc, file=sys.stderr)
        if container:
            print(docker("logs", "--tail", "40", container, check=False))
    finally:
        if sampler:
            sampler.stop()
        if args.image and not args.keep:
            docker("rm", "-f", name, check=False)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
