"""Model providers and the visitor's own key (reportlens/providers.py and everything it touches).

* parsing / validating the X-LLM-Config header and the owner's VISITOR_KEYS policy; custom URLs refused on public servers
* the mapping onto Settings (OpenAI: Responses API; everyone else: Chat Completions as ``openai/<id>``), env-level providers
* the REAL PageIndex SDK answering through an OpenAI-compatible provider (devtools.mock_openai) without touching litellm,
  with the visitor's key on the wire and never in os.environ
* scoring without an embeddings model; the budget ignores visitor-paid work; the web layer hands the settings through
"""
from __future__ import annotations

import base64
import json
import os
import sys
import types

import httpx
import pytest

from reportlens import pageindex_compat as compat
from reportlens.config import load_settings
from reportlens.lite_llm import plain_model
from reportlens.models import DocumentInfo, Message, ServiceError, Usage
from reportlens.providers import (KEYLESS_PLACEHOLDER, LLMChoice, apply_choice, bare_model, parse_llm_header, public_catalogue,
                                  settings_for_visitor, validate_choice)
from reportlens.qa import QAEngine
from reportlens.store import Store, new_id, now_iso
from reportlens.web.app import create_app
from tests.test_qa import live, world  # noqa: F401 - fixtures: the sample report indexed by the real SDK
from tests.test_service import build_env  # noqa: F401 - fixture
from tests.test_web import FakeService, parse_sse  # noqa: F401


def header(**raw) -> str:
    return base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")


# ============================================================================================ parsing and policy
def test_a_valid_header_round_trips(settings):
    choice = parse_llm_header(header(provider="anthropic", api_key="sk-ant-test-123", chat_model="claude-x"), settings)
    assert choice == LLMChoice(provider="anthropic", api_key="sk-ant-test-123", base_url="https://api.anthropic.com/v1/",
                               chat_model="claude-x")
    assert parse_llm_header(None, settings) is None and parse_llm_header("", settings) is None


@pytest.mark.parametrize("raw, words", [
    ({"provider": "nope", "api_key": "k"}, "Unknown provider"),
    ({"provider": "gemini", "chat_model": "m"}, "API key"),
    ({"provider": "gemini", "api_key": "has space", "chat_model": "m"}, "does not look valid"),
    ({"provider": "groq", "api_key": "gsk_x"}, "answer model"),
    ({"provider": "groq", "api_key": "gsk_x", "chat_model": "bad model!"}, "characters"),
    ({"provider": "custom", "api_key": "k", "chat_model": "m", "base_url": "ftp://x"}, "http(s)"),
    ({"provider": "custom", "api_key": "k", "chat_model": "m", "base_url": "http://user:pw@host/v1"}, "http(s)"),
])
def test_bad_choices_are_refused_with_a_reason(raw, words):
    with pytest.raises(ServiceError) as err:
        validate_choice(raw, allow_custom_url=True)
    assert err.value.code == "invalid_llm_config" and err.value.status == 400 and words in err.value.message


def test_garbage_headers_are_a_400_not_a_crash(settings):
    for value in ("%%%", base64.urlsafe_b64encode(b"not json").decode(), base64.urlsafe_b64encode(b"[1]").decode(), "x" * 5000):
        with pytest.raises(ServiceError) as err:
            parse_llm_header(value, settings)
        assert err.value.code == "invalid_llm_config"


def test_public_servers_refuse_urls_on_their_own_network(settings):
    public = load_settings(environ={"PUBLIC_MODE": "1"})
    assert not public.allow_custom_llm_url and settings.allow_custom_llm_url
    for provider in ("ollama", "custom"):
        with pytest.raises(ServiceError) as err:
            validate_choice({"provider": provider, "chat_model": "m", "base_url": "http://169.254.169.254/v1"}, allow_custom_url=False)
        assert "only available when you run the app yourself" in err.value.message
    assert {p["id"] for p in public_catalogue(public)["providers"]}.isdisjoint({"ollama", "custom"})
    assert load_settings(environ={"PUBLIC_MODE": "1", "ALLOW_CUSTOM_LLM_URL": "1"}).allow_custom_llm_url


def test_the_owners_policy_decides_whose_key_is_used(settings):
    choice = LLMChoice(provider="openai", api_key="sk-visitor-own-key")
    assert settings_for_visitor(settings, None) is settings
    mine = settings_for_visitor(settings, choice)
    assert mine.key_source == "visitor" and mine.openai_api_key == "sk-visitor-own-key" and settings.openai_api_key != mine.openai_api_key
    with pytest.raises(ServiceError) as err:
        settings_for_visitor(settings.with_(visitor_keys="required"), None)
    assert (err.value.code, err.value.status) == ("own_key_required", 403)
    with pytest.raises(ServiceError) as err:
        settings_for_visitor(settings.with_(visitor_keys="off"), choice)
    assert err.value.code == "own_key_disabled"
    assert load_settings(environ={"VISITOR_KEYS": "REQUIRED"}).visitor_keys == "required"
    assert load_settings(environ={"VISITOR_KEYS": "maybe"}).visitor_keys == "optional"


# ============================================================================================ mapping onto Settings
def test_openai_keeps_the_responses_api_and_the_stock_models(settings):
    s = apply_choice(settings.with_(chat_model="something-else"), LLMChoice(provider="openai", api_key="sk-v"), key_source="visitor")
    assert (s.chat_protocol, s.chat_model, s.index_model, s.judge_model) == ("responses", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-4.1")
    assert s.openai_base_url is None and s.embedding_model == "text-embedding-3-small" and s.llm_provider == "openai"


def test_other_providers_speak_chat_completions_with_exact_ids(settings):
    s = apply_choice(settings, LLMChoice(provider="openrouter", api_key="sk-or-v", base_url="https://openrouter.ai/api/v1",
                                         chat_model="anthropic/claude-x", index_model="google/gemini-y"), key_source="visitor")
    assert s.chat_protocol == "chat" and s.chat_model == "openai/anthropic/claude-x" and s.index_model == "openai/google/gemini-y"
    assert s.judge_model == "anthropic/claude-x" and s.question_rewrite_model == "google/gemini-y"     # direct API calls: bare ids
    assert s.embedding_model == "" and s.chat_reasoning_effort is None                                   # OpenRouter: no embeddings
    assert plain_model(s.index_model) == "google/gemini-y" and bare_model(s.chat_model) == "anthropic/claude-x"
    g = apply_choice(settings, LLMChoice(provider="gemini", api_key="AIzaTest", base_url="https://g/", chat_model="m",
                                         embedding_model="e"), key_source="visitor")
    assert g.embedding_model == "e"
    keyless = apply_choice(settings, LLMChoice(provider="ollama", base_url="http://localhost:11434/v1", chat_model="llama"),
                           key_source="visitor")
    assert keyless.openai_api_key == KEYLESS_PLACEHOLDER, "an explicit key, so nothing falls back to the server's"


def test_the_owner_can_run_the_whole_deployment_on_another_provider():
    s = load_settings(environ={"LLM_PROVIDER": "gemini", "LLM_API_KEY": "AIzaOwner", "PI_CHAT_MODEL": "gem-pro",
                               "PI_INDEX_MODEL": "gem-flash", "RAGAS_EMBEDDING_MODEL": "gem-embed"})
    assert s.llm_provider == "gemini" and s.key_source == "server" and s.openai_api_key == "AIzaOwner"
    assert s.openai_base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert (s.chat_model, s.index_model, s.embedding_model) == ("openai/gem-pro", "openai/gem-flash", "gem-embed")
    with pytest.raises(ValueError, match="LLM_PROVIDER=anthropic"):
        load_settings(environ={"LLM_PROVIDER": "anthropic", "LLM_API_KEY": "sk-ant-x"})                  # no model named
    assert load_settings(environ={"OPENAI_API_KEY": "sk-plain"}).chat_protocol == "responses"              # unchanged default


# ============================================================================================ the chat-model patch
def test_the_patch_sends_openai_prefixed_models_to_chat_completions_and_leaves_the_rest(monkeypatch):
    from pageindex import local_chat

    seen = []
    monkeypatch.setattr(local_chat, "_openai_model", lambda protocol, name, backend=None: seen.append((protocol, name)) or "sdk")
    monkeypatch.setattr(compat, "_patched_chat_model", False)
    assert compat._patch_compat_chat_model()
    try:
        model = local_chat._openai_model("chat", "openai/vendor/model-x", {"api_key": "sk-v", "base_url": "https://host.example/v1"})
        assert type(model).__name__ == "CompatChatModel" and model.model == "vendor/model-x"
        assert str(model._client.base_url).startswith("https://host.example/v1") and model._client.api_key == "sk-v"
        assert local_chat._openai_model("responses", "openai/gpt-x", {"base_url": "https://h/v1"}) == "sdk"
        assert local_chat._openai_model("chat", "gpt-x", {"base_url": "https://h/v1"}) == "sdk"          # bare: the SDK decides
        assert local_chat._openai_model("chat", "openai/gpt-x", None) == "sdk"                           # no base URL: the SDK's
        assert seen == [("responses", "openai/gpt-x"), ("chat", "gpt-x"), ("chat", "openai/gpt-x")]
    finally:
        compat.remove_patches()


def test_openai_only_request_fields_are_dropped_for_other_hosts():
    from agents import ModelSettings

    ms = ModelSettings(extra_body={"prompt_cache_key": "k", "keep": 1})
    assert compat._strip_openai_only(ms).extra_body == {"keep": 1}
    assert compat._strip_openai_only(ModelSettings(extra_body={"prompt_cache_key": "k"})).extra_body is None
    plain = ModelSettings(extra_body={"keep": 1})
    assert compat._strip_openai_only(plain) is plain


def test_a_visitors_key_never_reaches_the_process_environment(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-env")
    visitor = apply_choice(settings.with_(data_dir=tmp_path), LLMChoice(provider="groq", api_key="gsk_visitor_secret",
                                                                        base_url="https://api.groq.com/openai/v1", chat_model="m"),
                           key_source="visitor")
    try:
        compat.make_client(visitor, "c" * 32)
        assert os.environ["OPENAI_API_KEY"] == "sk-owner-env"
        assert compat._backend(visitor) == {"api_key": "gsk_visitor_secret", "api_base": "https://api.groq.com/openai/v1"}
    finally:
        compat.restore_openai_env()


# ============================================================================================ the real SDK, end to end
@pytest.mark.slow
@pytest.mark.parametrize("model_id", ["mock-answer-model", "vendor/mock-answer-model"])
def test_a_visitors_compatible_provider_answers_through_the_real_sdk_without_litellm(live, monkeypatch, model_id):
    trap = types.ModuleType("litellm")

    def boom(name):
        raise AssertionError(f"litellm.{name} was used on the OpenAI-compatible path")

    trap.__getattr__ = boom                                               # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", trap)
    monkeypatch.setitem(sys.modules, "agents.extensions.models.litellm_model", trap)
    env_before = os.environ.get("OPENAI_API_KEY")
    settings = apply_choice(live["settings"], LLMChoice(provider="custom", api_key="sk-visitor-own-123", base_url=live["server"].base_url,
                                                        chat_model=model_id), key_source="visitor")
    fact = next(f for f in live["facts"] if f["id"] == "customers_connected")
    try:
        events = list(QAEngine(settings).ask(session_id=live["sid"], doc=live["doc"], question=fact["question"], history=[],
                                             ctx=live["ctx"]))
    finally:
        compat.remove_patches()
    final = events[-1]
    assert final["type"] == "final" and final["status"] == "answered" and final["answer"].citations
    assert fact["page"] in {c.page for c in final["contexts"]}
    requests = live["server"].requests_of("agent")
    assert requests and all(r["path"].endswith("/chat/completions") for r in requests)
    assert {r["json"]["model"] for r in requests} == {model_id}, "the exact id, slashes included, no 'openai/' prefix"
    assert all(r["headers"].get("authorization") == "Bearer sk-visitor-own-123" for r in requests)
    assert all("prompt_cache_key" not in r["json"] for r in requests)
    assert os.environ.get("OPENAI_API_KEY") == env_before
    assert final["usage"].model == model_id


# ============================================================================================ scoring without embeddings
@pytest.mark.slow
async def test_scoring_without_an_embeddings_model_skips_only_relevancy(settings, tmp_path):
    from devtools.mock_openai import start_mock_server
    from reportlens.evaluation import NO_EMBEDDINGS, Evaluator
    from reportlens.models import ContextPage

    server = start_mock_server()
    try:
        s = apply_choice(settings, LLMChoice(provider="custom", api_key="sk-v", base_url=server.base_url, chat_model="judge-x"),
                         key_source="visitor")
        assert s.embedding_model == ""                            # none named: answer relevancy cannot be computed
        evaluator = Evaluator(s)
        try:
            scores = await evaluator.evaluate("How many customers?", "There are 4.2 million customers.",
                                              [ContextPage(page=3, text="We serve 4.2 million customers across the region.")])
        finally:
            await evaluator.aclose()
    finally:
        server.stop()
    assert scores.answer_relevancy is None and scores.errors.get("answer_relevancy") == NO_EMBEDDINGS
    assert scores.faithfulness is not None and scores.context_precision is not None
    assert scores.status == "done" and scores.judge_model == "judge-x"
    assert not server.requests_of("embeddings")


# ============================================================================================ who pays
def test_the_budget_ignores_work_paid_with_the_visitors_key(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    try:
        for source in ("server", "visitor", "none"):
            sid = store.create_session().id
            store.put_document(sid, DocumentInfo(id=new_id(), filename="a.pdf", doc_name="a.pdf", size_bytes=1, status="ready",
                                                 pi_doc_id=f"pi-{source}", key_source=source))
            store.add_message(Message(id=new_id(), session_id=sid, role="assistant", status="answered", created_at=now_iso(),
                                      usage=Usage(model="m", cost_usd=1.0), key_source=source))
        snap = store.usage_snapshot()
        assert snap.answer_cost_usd == 1.0 and snap.index_keys == ("pi-server",)
    finally:
        store.close()


# ============================================================================================ the web layer
class Recorder(FakeService):
    """FakeService that records the per-request settings it was handed."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.llm_seen: list = []

    def ask(self, sid, content, llm=None):
        self.llm_seen.append(llm)
        return super().ask(sid, content)


@pytest.fixture
def recorder(tmp_path, sample_pdf) -> Recorder:
    workdir = tmp_path / "svc"
    workdir.mkdir()
    return Recorder(workdir, sample_pdf)


async def test_the_header_reaches_the_service_as_visitor_settings(settings, recorder):
    sid = recorder.add_session("ready").id
    app = create_app(settings, recorder)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        own = header(provider="gemini", api_key="AIzaVisitorKey", chat_model="gem-pro")
        r = await c.post(f"/api/sessions/{sid}/messages", json={"content": "q?"}, headers={"X-LLM-Config": own})
        assert r.status_code == 200
        r = await c.post(f"/api/sessions/{sid}/messages", json={"content": "q?"})
        assert r.status_code == 200
        bad = await c.post(f"/api/sessions/{sid}/messages", json={"content": "q?"}, headers={"X-LLM-Config": "%%%"})
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_llm_config"
        cfg = (await c.get("/api/config")).json()
        assert cfg["llm"]["visitor_keys"] == "optional" and any(p["id"] == "anthropic" for p in cfg["llm"]["providers"])
        assert "AIza" not in json.dumps(cfg)
    first, second = recorder.llm_seen
    assert first.key_source == "visitor" and first.openai_api_key == "AIzaVisitorKey" and first.chat_model == "openai/gem-pro"
    assert second is None


async def test_required_own_keys_refuse_requests_without_one(settings, recorder):
    sid = recorder.add_session("ready").id
    app = create_app(settings.with_(visitor_keys="required"), recorder)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.post(f"/api/sessions/{sid}/messages", json={"content": "q?"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "own_key_required"


async def test_test_connection_checks_the_key_and_model_with_one_tiny_call(settings):
    from devtools.mock_openai import start_mock_server

    server = start_mock_server()
    try:
        app = create_app(settings, FakeService.__new__(FakeService))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            own = header(provider="custom", api_key="sk-visitor-test", base_url=server.base_url, chat_model="any-model",
                         embedding_model="emb-model")
            r = await c.post("/api/llm/check", headers={"X-LLM-Config": own})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] and set(body["checks"]) == {"answer_model", "embedding_model"}
            missing = await c.post("/api/llm/check")
            assert missing.status_code == 400
    finally:
        server.stop()
    chat = [r for r in server.requests if r["path"].endswith("/chat/completions")]
    assert len(chat) == 1 and chat[0]["json"]["model"] == "any-model" and chat[0]["json"]["max_tokens"] == 16
    assert chat[0]["headers"]["authorization"] == "Bearer sk-visitor-test"


# ============================================================================================ the public API surface
async def test_the_api_reference_is_served_without_a_cdn_and_without_a_code(settings):
    app = create_app(settings.with_(access_code="a-code-for-this-test"), FakeService.__new__(FakeService))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        page = await c.get("/docs")
        assert page.status_code == 200 and "/static/vendor/swagger-ui/swagger-ui-bundle.js" in page.text
        assert "<script>" not in page.text.replace(" ", ""), "no inline script: the CSP forbids it"
        assert "cdn" not in page.text.lower()
        spec = await c.get("/api/openapi.json")
        assert spec.status_code == 200
        body = spec.json()
        assert body["info"]["title"] == "Annual Report Lens" and body["info"]["license"]["name"] == "MIT"
        ask = body["paths"]["/api/sessions/{sid}/ask"]["post"]
        assert ask["tags"] == ["Questions"] and any(p["name"] == "X-LLM-Config" for p in ask["parameters"])
        assert (await c.get("/static/vendor/swagger-ui/swagger-ui-bundle.js")).status_code == 200
        assert (await c.get("/api/sessions")).status_code == 401                                 # the API itself still needs the code


async def test_asking_for_a_complete_json_answer(build_env):
    env = build_env()
    sid = env.ready_session()
    app = create_app(env.settings, env.service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60) as c:
        r = await c.post(f"/api/sessions/{sid}/ask", json={"content": "How many customers?"})
        assert r.status_code == 200, r.text
        message = r.json()["message"]
        assert message["role"] == "assistant" and message["status"] == "answered" and message["citations"]
        assert message["evaluation"]["status"] in ("done", "partial")
        quick = await c.post(f"/api/sessions/{sid}/ask", json={"content": "And employees?", "wait_for_scores": False})
        assert quick.status_code == 200 and quick.json()["message"]["status"] == "answered"
        empty = await c.post(f"/api/sessions/{sid}/ask", json={"content": "  "})
        assert empty.status_code == 400 and empty.json()["error"]["code"] == "empty_question"
        detail = (await c.get(f"/api/sessions/{sid}")).json()
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
