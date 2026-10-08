"""reportlens.lite_llm: the openai-package replacement for PageIndex's litellm calls (indexing child on a small host).

The point of the module is memory (litellm is ~150 MB); the risk is behaving differently from litellm, so the key tests compare the two
paths request for request against the fake OpenAI server, and the failure semantics against the SDK's own retry loop."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from devtools.mock_openai import MockServer
from reportlens import lite_llm
from reportlens.config import Settings
from reportlens.indexer import IndexService
from reportlens.store import Store
from tests.test_indexer import cfg, make_service, mock, no_sdk_sleep, run_job, small_pdf, store  # noqa: F401
from tests.test_pageindex_compat import clean_sdk_state  # noqa: F401  (autouse)


@pytest.fixture(autouse=True)
def lite_off() -> Iterator[None]:
    lite_llm.uninstall()
    yield
    lite_llm.uninstall()


# ------------------------------------------------------------------------------------------------ model names
@pytest.mark.parametrize("given, wire", [("gpt-5.6-luna", "gpt-5.6-luna"), ("openai/gpt-4.1", "gpt-4.1"), ("litellm/openai/gpt-4.1", "gpt-4.1"),
                                         ("litellm/gpt-4.1", "gpt-4.1"), ("anthropic/claude-x", None), ("bedrock/amazon.nova", None),
                                         ("litellm/anthropic/claude-x", None), ("", None), (None, None)])
def test_plain_model_names(given, wire) -> None:
    assert lite_llm.plain_model(given) == wire


# ------------------------------------------------------------------------------------------------ installation
def test_install_rebinds_every_copy_and_uninstall_restores_them() -> None:
    import pageindex.page_index_classic as classic
    import pageindex.tree_optimize as optimize
    import pageindex.utils as utils

    before = (utils.llm_acompletion, utils.llm_completion, utils.count_tokens, classic.llm_acompletion, optimize.llm_acompletion)
    assert lite_llm.install() >= 5
    assert utils.llm_acompletion is lite_llm.llm_acompletion is classic.llm_acompletion is optimize.llm_acompletion
    assert utils.llm_completion is lite_llm.llm_completion and utils.count_tokens is lite_llm.count_tokens
    assert lite_llm.install() == 0, "idempotent"
    lite_llm.uninstall()
    assert (utils.llm_acompletion, utils.llm_completion, utils.count_tokens, classic.llm_acompletion, optimize.llm_acompletion) == before


def test_a_moved_sdk_seam_keeps_litellm_in_use(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import pageindex.utils as utils

    monkeypatch.delattr(utils, "_NO_RETRY_STATUS")
    with caplog.at_level(logging.WARNING, logger="reportlens.lite_llm"):
        assert lite_llm.install() == 0
    assert any("retry helpers moved" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------------------------------------ same requests as litellm
def _body_signature(mock: MockServer) -> list[tuple]:
    """What went over the wire, order-insensitive (the SDK sends concurrently): path + model + messages."""
    import json

    sig = []
    for r in mock.requests:
        body = r.get("json") or {}
        sig.append((r["path"], body.get("model"), json.dumps(body.get("messages"), sort_keys=True), tuple(sorted(k for k in body if k not in ("model", "messages")))))
    return sorted(sig)


def test_a_whole_indexing_run_sends_the_same_requests_and_builds_the_same_tree_as_litellm(cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                                                         make_service: Callable[..., IndexService]) -> None:
    from reportlens.indexer import load_tree

    sid_a, doc_a, _ = run_job(make_service(), store, cfg, small_pdf, timeout=120)
    via_litellm = _body_signature(mock)
    tree_a = load_tree(cfg, sid_a)
    assert doc_a.status == "ready" and via_litellm
    mock.clear_requests()

    assert lite_llm.install() > 0
    sid_b, doc_b, _ = run_job(make_service(), store, cfg, small_pdf, timeout=120)
    via_lite = _body_signature(mock)
    assert doc_b.status == "ready", doc_b.error
    assert via_lite == via_litellm, "the replacement must put exactly the same requests on the wire"
    assert load_tree(cfg, sid_b) == tree_a and doc_b.node_count == doc_a.node_count
    assert all(p == "/v1/chat/completions" for p, *_ in via_lite)


def test_the_key_and_base_url_of_the_job_are_used(cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                  make_service: Callable[..., IndexService]) -> None:
    lite_llm.install()
    _, doc, _ = run_job(make_service(), store, cfg, small_pdf, timeout=120)
    assert doc.status == "ready"
    assert {(r.get("headers") or {}).get("authorization") for r in mock.requests} == {"Bearer sk-test-indexer"}


def test_token_counts_match_litellms_within_one_percent() -> None:
    import litellm

    text = "The group reported underlying operating profit of 4.2 billion pounds for the year ended 31 March 2025. " * 40
    assert lite_llm.count_tokens("") == 0 and lite_llm.count_tokens(None) == 0
    theirs = litellm.token_counter(model="gpt-5.6-luna", text=text)
    assert abs(lite_llm.count_tokens(text) - theirs) <= max(3, theirs // 100)       # node sizing only; 0.3 % measured
    assert lite_llm.count_tokens("<|endoftext|> special tokens in a report are just text") > 5


def test_counting_survives_a_missing_tokenizer(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setitem(sys.modules, "tiktoken", None)                   # import tiktoken -> ImportError
    with caplog.at_level(logging.WARNING, logger="reportlens.lite_llm"):
        assert lite_llm.count_tokens("a" * 400) == 100
    assert any("tiktoken unavailable" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------------------------------------ failure semantics (the SDK's own policy)
def _failing_call(mock: MockServer, kind: str, **extra: Any) -> BaseException:
    import pageindex.utils as utils

    mock.fail_next(kind=kind, count=1000)
    lite_llm.install()
    with pytest.raises(Exception) as exc:
        asyncio.run(utils.llm_acompletion("gpt-x", "hello"))
    return exc.value


@pytest.mark.parametrize("kind, status", [("auth", 401), ("model", 404)])
def test_auth_and_model_errors_are_not_retried_and_keep_their_status(kind: str, status: int, mock: MockServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-lite")
    monkeypatch.setenv("OPENAI_BASE_URL", mock.base_url)
    err = _failing_call(mock, kind)
    assert getattr(err, "status_code", None) == status and len(mock.requests) == 1, err


def test_rate_limits_are_retried_ten_times_then_exhausted_with_the_status(mock: MockServer, monkeypatch: pytest.MonkeyPatch, no_sdk_sleep: None) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-lite")
    monkeypatch.setenv("OPENAI_BASE_URL", mock.base_url)

    async def instant(_s: float) -> None:
        return None

    monkeypatch.setattr(lite_llm.asyncio, "sleep", instant)
    from pageindex.utils import LLMRetriesExhausted

    err = _failing_call(mock, "rate_limit")
    assert isinstance(err, LLMRetriesExhausted) and err.status_code == 429 and len(mock.requests) == lite_llm.MAX_RETRIES


def test_the_sync_path_has_the_same_policy(mock: MockServer, monkeypatch: pytest.MonkeyPatch) -> None:
    import pageindex.utils as utils

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-lite")
    monkeypatch.setenv("OPENAI_BASE_URL", mock.base_url)
    monkeypatch.setattr(lite_llm.time, "sleep", lambda s: None)
    lite_llm.install()
    mock.fail_next(kind="auth", count=1)
    with pytest.raises(Exception) as auth:
        utils.llm_completion("gpt-x", "hi")
    assert getattr(auth.value, "status_code", None) == 401
    mock.fail_next(kind="server", count=1000)
    with pytest.raises(utils.LLMRetriesExhausted) as down:
        utils.llm_completion("gpt-x", "hi")
    assert down.value.status_code == 500


def test_another_provider_is_left_to_the_sdks_own_function(monkeypatch: pytest.MonkeyPatch) -> None:
    import pageindex.utils as utils

    calls: list = []

    async def fake_original(model, prompt):
        calls.append((model, prompt))
        return "from litellm"

    lite_llm.install()
    monkeypatch.setitem(lite_llm._originals, "llm_acompletion", fake_original)
    assert asyncio.run(utils.llm_acompletion("anthropic/claude-x", "q")) == "from litellm" and calls == [("anthropic/claude-x", "q")]


def test_a_model_that_chat_completions_refuses_switches_to_the_sdk_for_good(mock: MockServer, monkeypatch: pytest.MonkeyPatch,
                                                                            caplog: pytest.LogCaptureFixture) -> None:
    import pageindex.utils as utils

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-lite")
    monkeypatch.setenv("OPENAI_BASE_URL", mock.base_url)
    calls: list = []

    async def fake_original(model, prompt):
        calls.append(model)
        return "bridged"

    lite_llm.install()
    monkeypatch.setitem(lite_llm._originals, "llm_acompletion", fake_original)
    mock.fail_next(status=404, kind="model", count=1)
    monkeypatch.setattr(lite_llm, "_endpoint_refuses_chat", lambda exc: True)
    with caplog.at_level(logging.WARNING, logger="reportlens.lite_llm"):
        assert asyncio.run(utils.llm_acompletion("gpt-pro", "q")) == "bridged"
        assert asyncio.run(utils.llm_acompletion("gpt-pro", "q2")) == "bridged"       # no new attempt on /chat/completions
    assert calls == ["gpt-pro", "gpt-pro"] and len(mock.requests) == 1
    assert sum("not served by /chat/completions" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("status, text, refuses", [(404, "This is not a chat model and thus not supported in the v1/chat/completions endpoint", True),
                                                   (400, "use the Responses API", True), (404, "The model `x` does not exist", False),
                                                   (401, "v1/chat/completions", False), (500, "v1/chat/completions", False)])
def test_only_a_genuine_wrong_endpoint_answer_triggers_the_fallback(status: int, text: str, refuses: bool) -> None:
    err = type("E", (Exception,), {"status_code": status})(text)
    assert lite_llm._endpoint_refuses_chat(err) is refuses


# ------------------------------------------------------------------------------------------------ in the real child process
def test_the_indexing_child_never_imports_litellm_with_the_lite_path(cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                                     make_service: Callable[..., IndexService], caplog: pytest.LogCaptureFixture) -> None:
    child_cfg = cfg.with_(index_in_subprocess=True, lite_llm=True, low_memory=True)
    with caplog.at_level(logging.INFO, logger="reportlens.indexer"):
        _, doc, _ = run_job(make_service(settings=child_cfg), store, child_cfg, small_pdf, timeout=120)
    assert doc.status == "ready", doc.error
    finished = [r.getMessage() for r in caplog.records if "indexing child" in r.getMessage()]
    assert finished and "loaded=" in finished[0] and "litellm" not in finished[0].split("loaded=")[1], finished


def test_with_the_lite_path_off_the_child_does_load_litellm(cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                            make_service: Callable[..., IndexService], caplog: pytest.LogCaptureFixture) -> None:
    child_cfg = cfg.with_(index_in_subprocess=True, lite_llm=False)
    with caplog.at_level(logging.INFO, logger="reportlens.indexer"):
        _, doc, _ = run_job(make_service(settings=child_cfg), store, child_cfg, small_pdf, timeout=120)
    assert doc.status == "ready", doc.error
    finished = [r.getMessage() for r in caplog.records if "indexing child" in r.getMessage()]
    assert finished and "litellm" in finished[0].split("loaded=")[1]


# ------------------------------------------------------------------------------------------------ errors read like the openai package's
@pytest.mark.parametrize("kind, expected", [("auth", "auth"), ("model", "model"), ("rate_limit", "rate_limit"), ("server", "upstream")])
def test_translate_error_classifies_the_lite_paths_failures_like_litellms(kind: str, expected: str, mock: MockServer, monkeypatch: pytest.MonkeyPatch,
                                                                         no_sdk_sleep: None) -> None:
    from reportlens.config import load_settings
    from reportlens.indexer import translate_error

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-lite")
    monkeypatch.setenv("OPENAI_BASE_URL", mock.base_url)

    async def instant(_s: float) -> None:
        return None

    monkeypatch.setattr(lite_llm.asyncio, "sleep", instant)
    err = _failing_call(mock, kind)
    fault = translate_error(RuntimeError("Failed to submit document: x"), load_settings(environ={}).with_(openai_api_key="sk-x"))
    assert fault.kind == "other"                                              # (control: an unrelated error is not misread)
    assert translate_error(err, load_settings(environ={}).with_(openai_api_key="sk-x", index_model="gpt-x")).kind == expected


def test_a_dead_endpoint_is_a_connection_error_the_translator_calls_upstream(monkeypatch: pytest.MonkeyPatch, no_sdk_sleep: None) -> None:
    import pageindex.utils as utils
    from reportlens.config import load_settings
    from reportlens.indexer import translate_error

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-lite")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")              # nothing listens on the discard port

    async def instant(_s: float) -> None:
        return None

    monkeypatch.setattr(lite_llm.asyncio, "sleep", instant)
    lite_llm.install()
    with pytest.raises(utils.LLMRetriesExhausted) as err:
        asyncio.run(utils.llm_acompletion("gpt-x", "hello"))
    assert translate_error(err.value, load_settings(environ={}).with_(openai_api_key="sk-x")).kind == "upstream"
    assert isinstance(err.value.__cause__, lite_llm.APIConnectionError)


def test_the_child_does_not_need_the_openai_package(cfg: Settings, store: Store, small_pdf: Path, mock: MockServer,
                                                    make_service: Callable[..., IndexService], caplog: pytest.LogCaptureFixture) -> None:
    child_cfg = cfg.with_(index_in_subprocess=True, lite_llm=True, low_memory=True)
    with caplog.at_level(logging.INFO, logger="reportlens.indexer"):
        _, doc, _ = run_job(make_service(settings=child_cfg), store, child_cfg, small_pdf, timeout=120)
    assert doc.status == "ready", doc.error
    loaded = [r.getMessage() for r in caplog.records if "indexing child" in r.getMessage()][0].split("loaded=")[1]
    assert "openai" not in loaded and "litellm" not in loaded, loaded
