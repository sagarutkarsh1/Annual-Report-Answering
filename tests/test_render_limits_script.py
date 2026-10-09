"""scripts/render_limits_test.py: the table helpers, and one full run of the script against a real in-process server on the fake OpenAI
server (no Docker needed: with --base-url the script only drives HTTP).  The Docker side (memory sampling, OOM check) is exercised by
running the script by hand, see docs/DEPLOY.md."""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from reportlens import pageindex_compat
from reportlens.config import load_settings
from reportlens.web.app import create_app
from scripts import render_limits_test as rl


def test_parse_size_understands_docker_stats_units() -> None:
    assert rl.parse_size("512MiB") == 512
    assert rl.parse_size("1.5GiB") == 1536
    assert rl.parse_size("2048KiB") == 2
    assert rl.parse_size("1048576B") == 1
    assert rl.parse_size("nonsense") != rl.parse_size("nonsense")        # NaN


def test_the_report_prints_a_table_and_flags_failures(capsys: pytest.CaptureFixture[str]) -> None:
    report = rl.Report()
    report.add(rl.Row("indexing", 12.3, 250.0, "60 pages"))
    report.add(rl.Row("question", 3.0, None, "boom", ok=False))
    report.facts["peak"] = "250 MiB"
    report.print()
    out = capsys.readouterr().out
    assert "indexing" in out and "12.3" in out and "250" in out and "FAILED: boom" in out and "peak: 250 MiB" in out
    assert out.strip().endswith("RESULT: FAIL - question: boom")
    clean = rl.Report()
    clean.add(rl.Row("x", 1.0))
    clean.print()
    assert "RESULT: PASS" in capsys.readouterr().out


def test_the_script_needs_exactly_one_target(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    for argv in (["--pdf", str(pdf)], ["--pdf", str(pdf), "--image", "x", "--base-url", "http://y"]):
        with pytest.raises(SystemExit):
            rl.main(argv)


@pytest.mark.slow
def test_a_full_run_against_a_live_demo_server_passes(tmp_path: Path, sample_pdf: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = load_settings(environ={"LOW_MEMORY": "1"}).with_(data_dir=tmp_path / "d", demo_mock=True, access_code="test")
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning", log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 90
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the server did not start"
        time.sleep(0.1)
    try:
        code = rl.main(["--base-url", f"http://127.0.0.1:{port}", "--pdf", str(sample_pdf), "--settle", "1", "--index-timeout", "240",
                        "--question", "What does the report say about customers?"])
    finally:
        server.should_exit = True
        thread.join(60)
        pageindex_compat.remove_patches()
        pageindex_compat.restore_openai_env()
    out = capsys.readouterr().out
    assert code == 0, out
    for step in ("login", "upload PDF", "indexing until ready", "question 1", "PDF range request"):
        assert step in out
    assert "RESULT: PASS" in out and "pings" in out


# ----------------------------------------------------------------------------------------------- the question-set measurements
def _sse(*events: tuple[str, dict]) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


BATCH_BODY = _sse(
    ("batch_start", {"items": [], "concurrency": 2}),
    ("token", {"index": 1, "message_id": "b", "text": "x"}), ("token", {"index": 0, "message_id": "a", "text": "y"}),
    ("answer_done", {"index": 1, "message": {}}), ("answer_done", {"index": 0, "message": {}}),
    ("error", {"index": 2, "code": "budget_exhausted", "message": "m", "message_id": "c"}),
    ("batch_done", {"answered": 2, "failed": 1}),
    ("eval_done", {"index": 0, "message_id": "a", "evaluation": {"status": "done"}}),
    ("eval_done", {"index": 1, "message_id": "b", "evaluation": {"status": "failed"}}),
    ("done", {}))


def _driver(handler) -> "rl.Driver":
    drv = rl.Driver("http://testserver", "code", rl.Report(), None)
    drv.client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")
    drv.sid = "s" * 32
    return drv


def test_timed_stream_notes_when_each_question_reached_each_stage() -> None:
    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.url.path)
        return httpx.Response(200, text=BATCH_BODY, headers={"content-type": "text/event-stream"})

    drv = _driver(handler)
    result = drv.run_batch("s" * 32, ["a?", "b?", "c?"])
    assert bodies == [f"/api/sessions/{'s' * 32}/batch"]
    assert sorted(result["per"]) == [0, 1, 2] and result["summary"] == {"answered": 2, "failed": 1}
    assert {"first_token", "answer_done", "eval_done"} <= set(result["per"][0]) and result["per"][2]["error"] == "budget_exhausted"
    assert result["answers_wall"] == result["marks"]["batch_done"] <= result["wall"]
    problems = rl.batch_failures(result, 3, "x")
    assert any("question 3 ended with budget_exhausted" in p for p in problems) and any("question 2 was scored 'failed'" in p for p in problems)
    assert rl.batch_failures({"per": {0: {"eval_status": "done"}}}, 1, "x") == []
    assert rl.batch_failures({"per": {}}, 2, "x") == ["x: 0 of 2 questions reported"]


def test_sequential_run_accumulates_the_wall_time_question_by_question() -> None:
    single = _sse(("message_start", {}), ("token", {"message_id": "a", "text": "x"}), ("answer_done", {"message": {}}),
                  ("eval_done", {"message_id": "a", "evaluation": {"status": "done"}}), ("done", {}))
    drv = _driver(lambda request: httpx.Response(200, text=single, headers={"content-type": "text/event-stream"}))
    result = drv.run_sequential("s" * 32, ["a?", "b?", "c?"])
    assert sorted(result["per"]) == [0, 1, 2] and result["wall"] > 0 and result["answers_wall"] <= result["wall"]
    assert result["per"][2]["answer_done"] >= result["per"][1]["answer_done"] >= result["per"][0]["answer_done"]
    assert result["per"][1]["eval_status"] == "done"


def test_the_batch_tables_print(capsys: pytest.CaptureFixture[str]) -> None:
    result = {"per": {0: {"first_token": 1.0, "answer_done": 5.0, "eval_done": 9.0}, 1: {"error": "budget_exhausted"}},
              "marks": {}, "summary": {}, "wall": 9.5, "answers_wall": 5.5}
    rl.print_batch("[x]", ["first question?", "second?"], result)
    variant = rl.VariantResult("batch x2", result, 301.0, 330.0, False, 8.5, ["boom"])
    rl.print_sweep([variant, rl.VariantResult("sequential")], ["first question?", "second?"])
    out = capsys.readouterr().out
    assert "ERROR budget_exhausted" in out and "all answers in: 5.5 s" in out and "SUMMARY" in out
    assert "batch x2" in out and "301.0" in out and "330.0" in out and "boom" in out and "sequential" in out


def test_the_sweep_needs_an_image_and_a_valid_mode_list(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with pytest.raises(SystemExit):
        rl.main(["--pdf", str(pdf), "--base-url", "http://y", "--batch-sweep", "seq,1"])
    assert rl.run_sweep(rl.argparse.Namespace(batch_sweep="seq,many", image="x")) == 2
    assert rl.run_sweep(rl.argparse.Namespace(batch_sweep="", image="x")) == 2


@pytest.mark.slow
def test_a_batch_run_against_a_live_demo_server_compares_both_ways(tmp_path: Path, sample_pdf: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = load_settings(environ={"LOW_MEMORY": "1"}).with_(data_dir=tmp_path / "d", demo_mock=True, access_code="test")
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning", log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 90
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the server did not start"
        time.sleep(0.1)
    try:
        code = rl.main(["--base-url", f"http://127.0.0.1:{port}", "--pdf", str(sample_pdf), "--settle", "1", "--index-timeout", "240",
                        "--batch", "--batch-compare", "--question", "What does the report say about customers?",
                        "--question", "What does the report say about dividends?"])
    finally:
        server.should_exit = True
        thread.join(60)
        pageindex_compat.remove_patches()
        pageindex_compat.restore_openai_env()
    out = capsys.readouterr().out
    assert code == 0, out
    assert "question set: sequential /messages" in out and "question set: batch /batch" in out and "all answers in:" in out and "RESULT: PASS" in out
