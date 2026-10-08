"""scripts/render_limits_test.py: the table helpers, and one full run of the script against a real in-process server on the fake OpenAI
server (no Docker needed: with --base-url the script only drives HTTP).  The Docker side (memory sampling, OOM check) is exercised by
running the script by hand, see docs/DEPLOY.md."""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

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
