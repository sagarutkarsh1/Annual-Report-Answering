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
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                out = docker("stats", "--no-stream", "--format", "{{.MemUsage}}", self.container, check=False, timeout=30)
                if out:
                    self.samples.append((time.monotonic(), parse_size(out.split("/")[0])))
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


def start_container(args: argparse.Namespace, name: str) -> tuple[str, float]:
    env = {"PUBLIC_MODE": "1", "REPORTLENS_DEMO_MOCK": "1", "ACCESS_CODE": args.code, "TRUST_PROXY": "1", "PORT": "10000",
           "SESSION_SECRET": "test-only-secret-not-a-real-one"}      # lets the login cookie survive the restart, as on Render
    for item in args.env:
        key, _, value = item.partition("=")
        env[key] = value
    cmd = ["run", "-d", "--name", name, f"--memory={args.memory}", f"--memory-swap={args.memory}", f"--cpus={args.cpus}",
           "-p", "127.0.0.1::10000"]
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
    p.add_argument("--keep", action="store_true", help="leave the container running afterwards")
    p.add_argument("--json", type=Path, help="also write the rows as JSON here")
    args = p.parse_args(argv)
    if bool(args.image) == bool(args.base_url):
        p.error("give exactly one of --image or --base-url")
    if not args.pdf.is_file():
        p.error(f"{args.pdf} not found")

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
        if indexed:                                       # nothing else can work on a document that is not ready
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
