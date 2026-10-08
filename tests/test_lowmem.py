"""Small-host support (Render free: 512 MB): LOW_MEMORY detection and defaults, settings hand-over to a child process, the
RAGAS import saver, in-process PDF parsing, one open document, no eager scoring import."""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from reportlens import config as config_module
from reportlens import lowmem
from reportlens import pageindex_compat as pc
from reportlens.config import (LOW_MEMORY_LIMIT_BYTES, Settings, detect_memory_limit_bytes, load_settings, settings_from_json,
                               settings_to_json)

MB = 1024 * 1024


# ------------------------------------------------------------------------------------------------ cgroup limit detection
def test_memory_limit_is_read_from_a_cgroup_file(tmp_path: Path) -> None:
    f = tmp_path / "memory.max"
    f.write_text("536870912\n")
    assert detect_memory_limit_bytes((str(f),)) == 512 * MB


@pytest.mark.parametrize("content", ["max", "", "garbage", "0", str(1 << 62), "-1"])
def test_no_limit_or_unreadable_means_none(tmp_path: Path, content: str) -> None:
    f = tmp_path / "memory.max"
    f.write_text(content)
    assert detect_memory_limit_bytes((str(f),)) is None


def test_missing_files_mean_none_and_the_second_file_is_tried(tmp_path: Path) -> None:
    assert detect_memory_limit_bytes((str(tmp_path / "nope"),)) is None
    v1 = tmp_path / "limit_in_bytes"
    v1.write_text(str(400 * MB))
    assert detect_memory_limit_bytes((str(tmp_path / "nope"), str(v1))) == 400 * MB


# ------------------------------------------------------------------------------------------------ the LOW_MEMORY switch
@pytest.fixture
def real_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """load_settings() as the server calls it (real environment, empty .env), with a fake container limit."""
    for name in ("LOW_MEMORY", "PUBLIC_MODE", "PI_INDEX_SUMMARY_CONCURRENCY", "EVAL_CONCURRENCY", "EVAL_MAX_CONTEXTS",
                 "MAX_OPEN_DOCS", "INDEX_IN_SUBPROCESS"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / "empty.env"
    env_file.write_text("")

    def load(limit: int | None, **env: str) -> Settings:
        monkeypatch.setattr(config_module, "detect_memory_limit_bytes", lambda *a, **k: limit)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return load_settings(env_file=env_file)

    return load


def test_public_mode_in_a_512_mb_container_turns_low_memory_on_by_itself(real_env) -> None:
    s = real_env(512 * MB, PUBLIC_MODE="1")
    assert s.low_memory and s.index_in_subprocess
    assert (s.index_summary_concurrency, s.eval_concurrency, s.eval_max_contexts, s.max_open_docs) == (6, 3, 6, 1)


def test_a_big_container_or_no_container_keeps_the_normal_public_defaults(real_env) -> None:
    for limit in (None, LOW_MEMORY_LIMIT_BYTES + 1, 16 * 1024 * MB):
        s = real_env(limit, PUBLIC_MODE="1")
        assert not s.low_memory and not s.index_in_subprocess
        assert (s.index_summary_concurrency, s.eval_concurrency, s.eval_max_contexts, s.max_open_docs) == (16, 8, 8, 4)


def test_a_small_container_without_public_mode_is_left_alone(real_env) -> None:
    assert not real_env(512 * MB).low_memory       # a developer's own Docker run is not a public deployment


def test_the_explicit_switch_wins_in_both_directions(real_env) -> None:
    assert real_env(None, LOW_MEMORY="1").low_memory
    assert not real_env(512 * MB, PUBLIC_MODE="1", LOW_MEMORY="0").low_memory
    assert real_env(None, LOW_MEMORY="1").index_in_subprocess


def test_explicit_values_beat_the_low_memory_defaults(real_env) -> None:
    s = real_env(512 * MB, PUBLIC_MODE="1", PI_INDEX_SUMMARY_CONCURRENCY="9", EVAL_CONCURRENCY="5", EVAL_MAX_CONTEXTS="4",
                 MAX_OPEN_DOCS="2", INDEX_IN_SUBPROCESS="0")
    assert (s.index_summary_concurrency, s.eval_concurrency, s.eval_max_contexts, s.max_open_docs) == (9, 5, 4, 2)
    assert not s.index_in_subprocess and s.low_memory


def test_tests_that_pass_an_environment_never_depend_on_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "detect_memory_limit_bytes", lambda *a, **k: 100 * MB)
    assert not load_settings(environ={"PUBLIC_MODE": "1"}).low_memory
    assert load_settings(environ={"PUBLIC_MODE": "1", "LOW_MEMORY": "true"}).low_memory


def test_the_flag_is_public_for_the_ui_and_the_default_is_off() -> None:
    assert load_settings(environ={}).public()["low_memory"] is False
    assert load_settings(environ={"LOW_MEMORY": "1"}).public()["low_memory"] is True


# ------------------------------------------------------------------------------------------------ settings over a pipe
def test_settings_survive_the_hand_over_to_a_child_process(tmp_path: Path) -> None:
    s = load_settings(environ={"PUBLIC_MODE": "1", "LOW_MEMORY": "1", "ALLOWED_HOSTS": ".onrender.com, a.example"}).with_(
        data_dir=tmp_path, openai_api_key="sk-test-roundtrip", chat_reasoning_effort=None)
    back = settings_from_json(settings_to_json(s))
    assert back == s
    assert isinstance(back.data_dir, Path) and isinstance(back.allowed_hosts, tuple)


def test_unknown_keys_from_a_newer_parent_are_ignored(tmp_path: Path) -> None:
    import json

    data = json.loads(settings_to_json(load_settings(environ={}).with_(data_dir=tmp_path)))
    data["future_setting"] = 1
    assert settings_from_json(json.dumps(data)).data_dir == tmp_path


# ------------------------------------------------------------------------------------------------ the RAGAS import saver
def test_the_datasets_stand_in_lets_ragas_import_without_pandas_and_pyarrow() -> None:
    """In a fresh interpreter (importing ragas for real would leak into this process)."""
    code = (
        "import os, sys; os.environ['RAGAS_DO_NOT_TRACK']='true'\n"
        "from reportlens import lowmem\n"
        "assert lowmem.stub_datasets_for_ragas()\n"
        "import ragas.metrics.collections, reportlens.evaluation\n"
        "heavy = [m for m in ('pandas', 'pyarrow', 'huggingface_hub') if m in sys.modules]\n"
        "print('HEAVY', heavy)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180, cwd=Path(__file__).resolve().parent.parent)
    assert out.returncode == 0, out.stderr[-800:]
    assert "HEAVY []" in out.stdout, out.stdout


def test_the_stand_in_never_replaces_a_real_datasets_module(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    real = types.ModuleType("datasets")
    monkeypatch.setitem(sys.modules, "datasets", real)
    assert lowmem.stub_datasets_for_ragas() is False
    assert sys.modules["datasets"] is real


def test_the_stand_in_is_installed_once_and_looks_like_a_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "datasets", raising=False)
    assert lowmem.stub_datasets_for_ragas() is True
    first = sys.modules["datasets"]
    assert lowmem.stub_datasets_for_ragas() is True and sys.modules["datasets"] is first
    import importlib.util

    assert importlib.util.find_spec("datasets") is not None         # a __spec__ of None would make find_spec raise
    assert isinstance(first.Dataset, type)


def test_trim_memory_never_raises() -> None:
    assert lowmem.trim_memory() in (True, False)


# ------------------------------------------------------------------------------------------------ in-process PDF parsing
def test_parse_workers_are_pinned_through_the_pdfium_lock_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK spawns cpu_count()-1 processes for PDFs of 64+ pages; small hosts pin it to 1 (the wrapper adds `workers=`)."""
    from pageindex.flash import api as flash_api

    calls: list[dict] = []
    pc.remove_patches()
    monkeypatch.setattr(flash_api, "extract_toc", lambda *a, **k: calls.append(k) or {"structure": []})
    try:
        pc.apply_patches()
        pc.set_parse_workers(None)
        flash_api.extract_toc("doc.pdf")
        pc.set_parse_workers(1)
        flash_api.extract_toc("doc.pdf")
        flash_api.extract_toc("doc.pdf", workers=3)               # an explicit choice is respected
        flash_api.extract_toc("doc.pdf", 2)                       # ... also when positional
    finally:
        pc.set_parse_workers(None)
        pc.remove_patches()
    assert calls == [{}, {"workers": 1}, {"workers": 3}, {}]


def test_make_client_pins_workers_only_in_low_memory_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    monkeypatch.setattr(pc, "set_parse_workers", lambda w: seen.append(w))
    monkeypatch.setattr(pc, "apply_patches", lambda full=True: None)
    monkeypatch.setattr(pc, "configure_openai_env", lambda s: None)
    base = load_settings(environ={}).with_(data_dir=tmp_path, openai_api_key="sk-test")
    pc.make_client(base, "a" * 32, for_indexing=True)
    pc.make_client(base.with_(low_memory=True), "b" * 32, for_indexing=True)
    assert seen == [None, 1]


# ------------------------------------------------------------------------------------------------ the start-up warm-up
def test_low_memory_skips_the_eager_scoring_import(caplog: pytest.LogCaptureFixture) -> None:
    from reportlens.web import app as app_module

    before = {t.name for t in threading.enumerate()}
    with caplog.at_level(logging.INFO):
        app_module._warm_up(load_settings(environ={}).with_(low_memory=True))
    assert "warm-up" not in {t.name for t in threading.enumerate()} - before
    assert any("LOW_MEMORY" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------------------------------------ the lean (page-at-a-time) parser
def test_lean_parser_gives_exactly_the_sdks_sequential_result(sample_pdf: Path) -> None:
    from pageindex.flash.parser_pdfium_charlevel import parse_charlevel_meta

    pc.remove_patches()
    calls: list = []
    lean = pc._lean_parse(sample_pdf, lambda *a, **k: calls.append((a, k)))
    spans, meta = parse_charlevel_meta(str(sample_pdf))
    assert not calls, "no fallback was needed"

    def sig(pages):      # Span has no __eq__: compare what the layout stage reads
        return [[(s.text, s.font_name, s.font_size, s.left_edge(), s.right_edge(), s.top_edge(), s.bottom_edge()) for s in page] for page in pages]

    assert sig(lean[0]) == sig(spans) and lean[1] == meta
    assert len(spans) == 60 and sum(len(p) for p in spans) > 300


def test_lean_parser_accepts_a_bytes_stream_and_falls_back_for_a_pdfium_document(sample_pdf: Path) -> None:
    import io

    calls: list = []
    spans, meta = pc._lean_parse(io.BytesIO(sample_pdf.read_bytes()), lambda *a, **k: calls.append((a, k)))
    assert len(spans) == len(meta) == 60 and not calls
    marker = object()
    assert pc._lean_parse(marker, lambda doc, workers: ("sdk", doc, workers)) == ("sdk", marker, 1)


def test_lean_parser_falls_back_to_the_sdk_when_the_pdf_has_type3_fonts(sample_pdf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pageindex.flash import parser_pdfium_parallel as par

    def type3(_page: int):
        raise par._Type3Detected(_page)

    monkeypatch.setattr(par, "_run_page", type3)
    assert pc._lean_parse(sample_pdf, lambda doc, workers: ("sdk", workers)) == ("sdk", 1)
    assert par._worker_pdf is None, "the per-page document is released"


def test_workers_1_goes_through_the_lean_parser_and_anything_else_to_the_sdk(sample_pdf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pageindex.flash import main as flash_main

    seen: list = []
    pc.remove_patches()
    monkeypatch.setattr(flash_main, "parse_charlevel_meta_parallel", lambda doc, workers=None, **k: seen.append(workers) or ([], []))
    monkeypatch.setattr(pc, "_lean_parse", lambda doc, original: seen.append("lean") or ([], []))
    try:
        assert pc._patch_lean_parser() and pc._patched_parser
        flash_main.parse_charlevel_meta_parallel(sample_pdf, workers=1)
        flash_main.parse_charlevel_meta_parallel(sample_pdf)
        flash_main.parse_charlevel_meta_parallel(sample_pdf, workers=4)
        assert seen == ["lean", None, 4]
        wrapped = flash_main.parse_charlevel_meta_parallel
        assert pc._patch_lean_parser() and flash_main.parse_charlevel_meta_parallel is wrapped, "idempotent"
    finally:
        pc.remove_patches()
    assert not pc._patched_parser


def test_a_moved_sdk_seam_leaves_the_sdk_parser_in_charge(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from pageindex.flash import parser_pdfium_parallel as par

    pc.remove_patches()
    monkeypatch.delattr(par, "_run_page")
    with caplog.at_level(logging.WARNING, logger="reportlens.pageindex_compat"):
        assert pc._patch_lean_parser() is False
    assert any("per-page parser seams" in r.getMessage() for r in caplog.records)


def test_health_reports_the_lean_parser() -> None:
    pc.remove_patches()
    pc.apply_patches()
    try:
        assert pc.check_environment(load_settings(environ={}))["lean_parser"] is True
    finally:
        pc.remove_patches()


def test_the_light_patch_set_does_not_touch_the_indexing_half_of_the_sdk() -> None:
    from pageindex.local_api import LocalAPI

    pc.remove_patches()
    original = LocalAPI.__dict__["_extract_page_texts"]
    pc.apply_patches(full=False)
    assert LocalAPI.__dict__["_extract_page_texts"] is original and not pc.is_patched()


# ------------------------------------------------------------------------------------------------ the browser side (no JS runner here: check the shipped files)
STATIC = Path(__file__).resolve().parent.parent / "reportlens" / "web" / "static" / "js"


def test_the_ui_explains_a_vanished_chat_as_a_sleeping_host_in_public_mode() -> None:
    api = (STATIC / "api.js").read_text(encoding="utf-8")
    assert "The demo restarted (free hosting sleeps). Please start a new chat and upload again." in api
    assert "publicMode" in api and "That chat no longer exists." in api, "the local wording stays for non-public runs"
    main = (STATIC / "main.js").read_text(encoding="utf-8")
    assert "publicMode: !!config.public_mode" in main
    assert "handleSessionGone" in main and "checkActiveStillExists" in main and "dropSessionCache(sid)" in main and "closePanel()" in main


def test_the_indexing_card_warns_about_the_slow_free_server_only_in_low_memory_mode() -> None:
    upload = (STATIC / "upload.js").read_text(encoding="utf-8")
    assert "state.config?.low_memory ?" in upload and "several minutes" in upload and "Keep this tab open" in upload


def test_summary_line_of_a_process_names_the_heavy_libraries_it_loaded() -> None:
    line = lowmem.memory_summary()
    assert line.startswith("rss=") and "loaded=" in line


def test_the_job_timeout_is_longer_on_a_small_host(tmp_path: Path) -> None:
    from reportlens import indexer
    from reportlens.store import Store

    store = Store(tmp_path / "t.db")
    try:
        normal = indexer.IndexService(load_settings(environ={}).with_(data_dir=tmp_path), store, client_factory=lambda *a, **k: None)
        small = indexer.IndexService(load_settings(environ={"LOW_MEMORY": "1"}).with_(data_dir=tmp_path), store, client_factory=lambda *a, **k: None)
        explicit = indexer.IndexService(load_settings(environ={}).with_(data_dir=tmp_path), store, client_factory=lambda *a, **k: None, job_timeout_s=7.0)
        assert (normal._job_timeout_s, small._job_timeout_s, explicit._job_timeout_s) == (indexer.DEFAULT_JOB_TIMEOUT_S, indexer.LOW_MEMORY_JOB_TIMEOUT_S, 7.0)
        assert indexer.LOW_MEMORY_JOB_TIMEOUT_S > indexer.DEFAULT_JOB_TIMEOUT_S
        for svc in (normal, small, explicit):
            svc.shutdown(wait=False)
    finally:
        store.close()
