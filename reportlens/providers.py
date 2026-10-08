"""Model providers, and the visitor's own key ("bring your own key").

Every provider here is reached through its **OpenAI-compatible** endpoint, so the app needs no extra SDK and, more
importantly, no litellm in the web process (litellm alone is ~150 MB: it would not fit Render's free 512 MB next to an
indexing job).  How each part talks to the provider:

* answers   - OpenAI: the Responses API (bare model name, as before).  Everyone else: Chat Completions, through
              `pageindex_compat`'s chat-model patch (model name sent to the SDK as ``openai/<id>``: "OpenAI protocol, this
              base URL, this exact id"; litellm reads that prefix the same way, so a deployment that does load litellm agrees).
* indexing  - the summary calls go to ``<base_url>/chat/completions`` (``lite_llm`` in the indexing child; litellm otherwise).
* scoring   - RAGAS through an `AsyncOpenAI` client on the same base URL; the bare model id.  Answer relevancy also needs an
              embeddings model; providers without an embeddings API simply skip that one metric.

A visitor's choice arrives on every request in the ``X-LLM-Config`` header (base64url JSON, see `parse_llm_header`) and is
turned into a per-request copy of the settings (`settings_for_visitor`) whose ``key_source`` is "visitor": that copy is
used for the work the request starts and then dropped.  The key is never written to the process environment, a file, the
database or a log, and visitor-paid work is never charged to the owner's budget.  Custom base URLs (Ollama, a private
gateway) are refused on a public deployment unless the owner allows them (`ALLOW_CUSTOM_LLM_URL`): the server would
otherwise fetch any address a visitor names (SSRF).
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, fields
from typing import Any, Optional
from urllib.parse import urlsplit

from .config import Settings
from .models import ServiceError

LLM_HEADER = "x-llm-config"
OPENAI_PREFIX = "openai/"                 # "speak the OpenAI protocol to the configured base URL, this exact model id"
KEYLESS_PLACEHOLDER = "sk-not-needed"     # an explicit key, so nothing ever falls back to the server's OPENAI_API_KEY
MAX_HEADER_CHARS = 4096
MAX_KEY_CHARS = 512
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}")
_KEY_RE = re.compile(r"[\x21-\x7e]+")      # printable ASCII, no spaces


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    base_url: Optional[str]               # None: the visitor gives one (custom)
    embeddings: bool                      # has an OpenAI-style /embeddings endpoint (answer relevancy)
    key_url: str = ""                     # where to create a key
    models_url: str = ""                  # where the model ids are listed
    key_required: bool = True
    self_hosted_only: bool = False        # a URL on the server's own network: refused on public deployments

    @property
    def responses_api(self) -> bool:
        return self.id == "openai"

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "base_url": self.base_url, "embeddings": self.embeddings,
                "key_url": self.key_url, "models_url": self.models_url, "key_required": self.key_required,
                "self_hosted_only": self.self_hosted_only}


PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    Provider("openai", "OpenAI", "https://api.openai.com/v1", True,
             "https://platform.openai.com/api-keys", "https://platform.openai.com/docs/models"),
    Provider("anthropic", "Anthropic (Claude)", "https://api.anthropic.com/v1/", False,
             "https://console.anthropic.com/settings/keys", "https://docs.anthropic.com/en/docs/about-claude/models"),
    Provider("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", True,
             "https://aistudio.google.com/apikey", "https://ai.google.dev/gemini-api/docs/models"),
    Provider("openrouter", "OpenRouter (hundreds of models, one key)", "https://openrouter.ai/api/v1", False,
             "https://openrouter.ai/keys", "https://openrouter.ai/models"),
    Provider("groq", "Groq", "https://api.groq.com/openai/v1", False,
             "https://console.groq.com/keys", "https://console.groq.com/docs/models"),
    Provider("mistral", "Mistral", "https://api.mistral.ai/v1", True,
             "https://console.mistral.ai/api-keys", "https://docs.mistral.ai/getting-started/models/"),
    Provider("deepseek", "DeepSeek", "https://api.deepseek.com/v1", False,
             "https://platform.deepseek.com/", "https://api-docs.deepseek.com/"),
    Provider("xai", "xAI (Grok)", "https://api.x.ai/v1", False, "https://console.x.ai", "https://docs.x.ai/docs/models"),
    Provider("together", "Together AI", "https://api.together.xyz/v1", True,
             "https://api.together.ai/settings/api-keys", "https://docs.together.ai/docs/serverless-models"),
    Provider("ollama", "Ollama (on the server's machine)", "http://localhost:11434/v1", True,
             "", "https://ollama.com/library", key_required=False, self_hosted_only=True),
    Provider("custom", "Other OpenAI-compatible endpoint", None, True, key_required=False, self_hosted_only=True),
)}


@dataclass(frozen=True)
class LLMChoice:
    """What a visitor (or the owner, through environment variables) picked.  Empty model fields mean "the default"."""
    provider: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    chat_model: str = ""
    index_model: str = ""
    judge_model: str = ""
    embedding_model: str = ""


def _default(name: str) -> Any:
    """The stock OpenAI default of a Settings field (what the OpenAI preset falls back to, whatever the server runs)."""
    return next(f.default for f in fields(Settings) if f.name == name)


def _bad(message: str) -> ServiceError:
    return ServiceError("invalid_llm_config", message, 400)


def _model(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if text and not _MODEL_RE.fullmatch(text):
        raise _bad(f"The {what} name contains characters a model id cannot have.")
    return text


def _url(value: Any) -> str:
    text = str(value or "").strip()
    parts = urlsplit(text)
    if len(text) > 300 or parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise _bad("The base URL must be a plain http(s) address, for example http://localhost:11434/v1.")
    return text.rstrip("/") + ("/" if text.endswith("/") else "")


def validate_choice(raw: dict[str, Any], *, allow_custom_url: bool) -> LLMChoice:
    """Check a provider choice coming from outside (header or JSON body).  Raises 400 invalid_llm_config."""
    if not isinstance(raw, dict):
        raise _bad("The model settings must be a JSON object.")
    provider = PROVIDERS.get(str(raw.get("provider") or "").strip().lower())
    if provider is None:
        raise _bad(f"Unknown provider. Choose one of: {', '.join(PROVIDERS)}.")
    if provider.self_hosted_only and not allow_custom_url:
        raise _bad(f"{provider.label} is only available when you run the app yourself; this server accepts the listed cloud providers.")
    key = str(raw.get("api_key") or "").strip() or None
    if key is not None and (len(key) > MAX_KEY_CHARS or not _KEY_RE.fullmatch(key)):
        raise _bad("That API key does not look valid (no spaces, at most 512 characters).")
    if key is None and provider.key_required:
        raise _bad(f"Enter your {provider.label} API key.")
    base_url = provider.base_url
    if provider.base_url is None or (provider.self_hosted_only and raw.get("base_url")):
        base_url = _url(raw.get("base_url") or provider.base_url)
    choice = LLMChoice(provider=provider.id, api_key=key, base_url=base_url,
                       chat_model=_model(raw.get("chat_model"), "answer model"),
                       index_model=_model(raw.get("index_model"), "indexing model"),
                       judge_model=_model(raw.get("judge_model"), "judge model"),
                       embedding_model=_model(raw.get("embedding_model"), "embedding model"))
    if not provider.responses_api and not choice.chat_model:
        raise _bad(f"Enter the answer model: the model id exactly as {provider.label} names it.")
    return choice


def parse_llm_header(value: Optional[str], settings: Settings) -> Optional[LLMChoice]:
    """The visitor's choice from the X-LLM-Config header (base64url-encoded JSON), or None when there is none."""
    if not value:
        return None
    if len(value) > MAX_HEADER_CHARS:
        raise _bad("The model settings are too long.")
    try:
        padded = value.strip() + "=" * (-len(value.strip()) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError, binascii.Error):
        raise _bad("The model settings could not be read. Open 'Model & API key' and save them again.") from None
    return validate_choice(raw, allow_custom_url=settings.allow_custom_llm_url)


def apply_choice(settings: Settings, choice: LLMChoice, *, key_source: str) -> Settings:
    """A copy of `settings` that sends every model call of this request (or this deployment) to `choice`."""
    provider = PROVIDERS[choice.provider]
    key = choice.api_key or KEYLESS_PLACEHOLDER
    if provider.responses_api:
        chat = choice.chat_model or _default("chat_model")
        return settings.with_(
            llm_provider=provider.id, key_source=key_source, openai_api_key=key, openai_base_url=None,
            chat_protocol="responses", chat_model=chat, chat_reasoning_effort=_default("chat_reasoning_effort"),
            index_model=choice.index_model or _default("index_model"),
            judge_model=choice.judge_model or _default("judge_model"), judge_reasoning_effort=None,
            embedding_model=choice.embedding_model or _default("embedding_model"),
            question_rewrite_model=_default("question_rewrite_model"))
    index = choice.index_model or choice.chat_model
    return settings.with_(
        llm_provider=provider.id, key_source=key_source, openai_api_key=key, openai_base_url=choice.base_url,
        chat_protocol="chat", chat_model=OPENAI_PREFIX + choice.chat_model, chat_reasoning_effort=None,
        index_model=OPENAI_PREFIX + index,
        judge_model=choice.judge_model or choice.chat_model, judge_reasoning_effort=None,
        embedding_model=choice.embedding_model if provider.embeddings or choice.embedding_model else "",
        question_rewrite_model=index)


def settings_for_visitor(settings: Settings, choice: Optional[LLMChoice]) -> Settings:
    """Per-request settings: the visitor's own provider and key when they sent one (and the owner allows it), else the
    server's.  Raises 400/403 when the owner's policy says otherwise."""
    if choice is None:
        if settings.visitor_keys == "required" and not settings.demo_mock:
            raise ServiceError("own_key_required", "This server runs on your own API key. Open 'Model & API key' and add one.", 403)
        return settings
    if settings.visitor_keys == "off":
        raise ServiceError("own_key_disabled", "This server does not accept visitors' own API keys.", 403)
    return apply_choice(settings, choice, key_source="visitor")


def bare_model(model: str) -> str:
    """The id the provider knows (the OpenAI-protocol prefix stripped) - for display and for direct API calls."""
    return model[len(OPENAI_PREFIX):] if model.startswith(OPENAI_PREFIX) else model


def _error_text(response: Any) -> str:
    try:
        error = response.json().get("error")
        text = error.get("message") if isinstance(error, dict) else error
    except (ValueError, AttributeError):
        text = None
    return str(text or response.reason_phrase or f"HTTP {response.status_code}")[:240]


async def check_connection(s: Settings) -> dict[str, Any]:
    """'Test connection': one tiny chat completion with the answer model (and one embedding, when an embeddings model is set)
    on the chosen provider.  Costs a fraction of a cent; tells key, model and endpoint problems apart before any real work."""
    import httpx

    base = (s.openai_base_url or "https://api.openai.com/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {s.openai_api_key or KEYLESS_PLACEHOLDER}"}
    body: dict[str, Any] = {"model": bare_model(s.chat_model), "messages": [{"role": "user", "content": "Reply with OK."}]}
    body["max_completion_tokens" if s.llm_provider == "openai" else "max_tokens"] = 16
    probes = {"answer_model": ("chat/completions", body)}
    if s.embedding_model:
        probes["embedding_model"] = ("embeddings", {"model": bare_model(s.embedding_model), "input": "OK"})
    checks: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        for name, (path, payload) in probes.items():
            try:
                response = await client.post(f"{base}/{path}", json=payload, headers=headers)
            except httpx.HTTPError as exc:
                checks[name] = {"ok": False, "status": None, "message": f"Could not reach {urlsplit(base).hostname}: {type(exc).__name__}"}
                continue
            ok = response.status_code < 300
            checks[name] = {"ok": ok, "status": response.status_code, "message": "OK" if ok else _error_text(response)}
    return {"ok": all(c["ok"] for c in checks.values()), "provider": s.llm_provider,
            "answer_model": bare_model(s.chat_model), "checks": checks}


def public_catalogue(settings: Settings) -> dict[str, Any]:
    """For GET /api/config: which providers a visitor may pick and what this server does without their key."""
    return {
        "visitor_keys": settings.visitor_keys,
        "allow_custom_url": settings.allow_custom_llm_url,
        "server_provider": settings.llm_provider,
        "providers": [p.public() for p in PROVIDERS.values() if settings.allow_custom_llm_url or not p.self_hosted_only],
        "openai_defaults": {"chat_model": _default("chat_model"), "index_model": _default("index_model"),
                            "judge_model": _default("judge_model"), "embedding_model": _default("embedding_model")},
    }
