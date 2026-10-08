"""A light replacement for the three PageIndex SDK functions that import litellm, for the indexing child on a small host.

Importing litellm costs about 130-180 MB of resident memory (1,400 modules: every provider, the proxy, the router, the loggers ...).
PageIndex's Flash indexing only needs three things from it:

    llm_completion / llm_acompletion   one chat completion with the SDK's own 10-attempt retry loop
    count_tokens                       a token count for node sizing

For an OpenAI model name (plain `gpt-...`, `openai/gpt-...` or `litellm/openai/...`) litellm sends exactly one request:
POST {base_url}/chat/completions with {"model", "messages"} (the SDK passes no other parameters; `max_retries=0` because the SDK
retries itself).  This module sends that same request with plain `httpx` (the `openai` package alone is ~50 MB of type modules, which
the indexing child has no other use for), keeps the SDK's retry policy, error classes (`status_code` attributes, `LLMRetriesExhausted`) and return values, and hands everything else
(another provider prefix, a model that is not served by /chat/completions, a failure while building the client) to the SDK's own
litellm functions, which are then loaded on demand.  `tests/test_lite_llm.py` compares the two paths request for request.

The SDK itself stays untouched on disk: we rebind the names in its modules, guarded like every patch in pageindex_compat.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("reportlens.lite_llm")

MAX_RETRIES = 10                      # the SDK's value (its loop is the retry policy)
REQUEST_TIMEOUT_S = 600.0             # litellm's default request timeout
_TOKEN_ENCODING = "cl100k_base"       # litellm's token counter gives the same count (+-1) for every OpenAI model name
_MODULES = ("pageindex.utils", "pageindex.tree_optimize", "pageindex.page_index_classic", "pageindex.local_api", "pageindex.page_index_md")
_NOT_A_CHAT_MODEL_HINTS = ("v1/chat/completions", "not a chat model", "use the responses", "/v1/responses", "only supported in v1/responses")

_lock = threading.Lock()
_installed = False
_fallback_all = False                 # set when the endpoint refuses /chat/completions for the model: litellm takes over for good
_originals: dict[str, Callable[..., Any]] = {}
_rebound: list[tuple[Any, str, Any]] = []     # (module, name, what it held before) for uninstall()
_async_clients: "weakref.WeakKeyDictionary[Any, dict]" = weakref.WeakKeyDictionary()
_sync_clients: dict[str, Any] = {}


def plain_model(model: Optional[str]) -> Optional[str]:
    """The bare OpenAI model name for `model`, or None when litellm must route it (another provider, no model).
    An explicit ``openai/`` prefix means "OpenAI protocol, the configured base URL, this exact id" (litellm's reading too), so
    what follows it is sent as is, slashes included: OpenRouter's ``vendor/model`` ids, for example."""
    if not model:
        return None
    name = model[len("litellm/"):] if model.startswith("litellm/") else model
    if name.startswith("openai/"):
        return name[len("openai/"):] or None
    return None if "/" in name or not name else name


DEFAULT_BASE_URL = "https://api.openai.com/v1"


def _credentials(backend: Optional[dict]) -> tuple[Optional[str], str]:
    backend = backend or {}
    key = backend.get("api_key") or os.environ.get("OPENAI_API_KEY")
    base = backend.get("api_base") or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL
    return key, base.rstrip("/")


class APIStatusError(Exception):
    """An HTTP error answer.  `status_code` is what the SDK's retry policy reads; the subclass names (RateLimitError ...) are what
    `indexer.translate_error` reads, exactly as for the `openai` package's exceptions."""

    status_code: Optional[int] = None


class BadRequestError(APIStatusError): ...
class AuthenticationError(APIStatusError): ...
class PermissionDeniedError(APIStatusError): ...
class NotFoundError(APIStatusError): ...
class RateLimitError(APIStatusError): ...
class InternalServerError(APIStatusError): ...
class APIConnectionError(Exception): ...
class APITimeoutError(APIConnectionError): ...


_STATUS_CLASSES = {400: BadRequestError, 401: AuthenticationError, 403: PermissionDeniedError, 404: NotFoundError, 429: RateLimitError}


def _status_error(status: int, body: str) -> APIStatusError:
    cls = _STATUS_CLASSES.get(status) or (InternalServerError if status >= 500 else APIStatusError)
    exc = cls(f"Error code: {status} - {body[:2000]}")
    exc.status_code = status
    return exc


def _request(backend: Optional[dict], model: str, messages: list[dict]) -> tuple[str, dict]:
    key, base = _credentials(backend)
    headers = {"Content-Type": "application/json", "User-Agent": "reportlens-lite-llm"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return f"{base}/chat/completions", {"headers": headers, "json": {"model": model, "messages": messages}}


def _content(response: Any) -> tuple[Any, str]:
    """(message content, finish reason) of a /chat/completions answer; an HTTP error becomes the exception the SDK expects."""
    if response.status_code >= 400:
        raise _status_error(response.status_code, response.text)
    choice = response.json()["choices"][0]
    return choice["message"]["content"], choice.get("finish_reason")


def _wrap_transport(exc: Exception) -> Exception:
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return APITimeoutError("Request timed out.")
    if isinstance(exc, httpx.TransportError):
        return APIConnectionError(f"Connection error. ({type(exc).__name__})")
    return exc


def _async_client():
    """One httpx.AsyncClient per event loop (the SDK runs `asyncio.run` repeatedly; a pooled connection must not outlive its loop)."""
    import httpx

    loop = asyncio.get_running_loop()
    per_loop = _async_clients.setdefault(loop, {})
    if "client" not in per_loop:
        per_loop["client"] = httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=30.0))
    return per_loop["client"]


def _sync_client():
    import httpx

    with _lock:
        if "client" not in _sync_clients:
            _sync_clients["client"] = httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=30.0))
        return _sync_clients["client"]


async def _acreate(backend: Optional[dict], model: str, messages: list[dict]) -> tuple[Any, Optional[str]]:
    url, kwargs = _request(backend, model, messages)
    failure: Optional[Exception] = None
    try:
        return _content(await _async_client().post(url, **kwargs))
    except APIStatusError:
        raise
    except Exception as exc:  # noqa: BLE001 - transport errors get the SDK's names; anything else is a bug and propagates as is
        failure = _wrap_transport(exc)
    raise failure          # outside the handler: the low-level OSError behind a refused connection must not become this error's context
                           # (translate_error would read it as a disk problem)


def _create(backend: Optional[dict], model: str, messages: list[dict]) -> tuple[Any, Optional[str]]:
    url, kwargs = _request(backend, model, messages)
    failure: Optional[Exception] = None
    try:
        return _content(_sync_client().post(url, **kwargs))
    except APIStatusError:
        raise
    except Exception as exc:  # noqa: BLE001
        failure = _wrap_transport(exc)
    raise failure


def _endpoint_refuses_chat(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status in (400, 404) and any(h in text for h in _NOT_A_CHAT_MODEL_HINTS)


def _utils():
    return importlib.import_module("pageindex.utils")


# ------------------------------------------------------------------------------------------------ the replacements
def llm_completion(model, prompt, chat_history=None, return_finish_reason=False):
    utils, original = _utils(), _originals.get("llm_completion")
    wire = plain_model(model)
    if wire is None or _fallback_all or original is None:
        return original(model, prompt, chat_history, return_finish_reason)
    messages = list(chat_history) + [{"role": "user", "content": prompt}] if chat_history else [{"role": "user", "content": prompt}]
    backend = utils._llm_backend.get()
    for i in range(MAX_RETRIES):
        try:
            content, finish_reason = _create(backend, wire, messages)
            if return_finish_reason:
                return content, ("max_output_reached" if finish_reason == "length" else "finished")
            return content
        except Exception as exc:  # noqa: BLE001 - the SDK's policy, verbatim
            if _endpoint_refuses_chat(exc) and _give_up_on_chat(exc):
                return original(model, prompt, chat_history, return_finish_reason)
            if getattr(exc, "status_code", None) in utils._NO_RETRY_STATUS:
                raise
            logging.error(f"Error: {exc}")
            if i < MAX_RETRIES - 1:
                logging.warning("Retrying LLM completion")
                time.sleep(1)
            else:
                raise utils.LLMRetriesExhausted(f"LLM completion failed after {MAX_RETRIES} retries: {exc}",
                                                status_code=getattr(exc, "status_code", None)) from exc


async def llm_acompletion(model, prompt):
    utils, original = _utils(), _originals.get("llm_acompletion")
    wire = plain_model(model)
    if wire is None or _fallback_all or original is None:
        return await original(model, prompt)
    messages = [{"role": "user", "content": prompt}]
    backend = utils._llm_backend.get()
    for i in range(MAX_RETRIES):
        try:
            return (await _acreate(backend, wire, messages))[0]
        except Exception as exc:  # noqa: BLE001
            if _endpoint_refuses_chat(exc) and _give_up_on_chat(exc):
                return await original(model, prompt)
            if getattr(exc, "status_code", None) in utils._NO_RETRY_STATUS:
                raise
            logging.error(f"Error: {exc}")
            if i < MAX_RETRIES - 1:
                logging.warning("Retrying LLM completion")
                await asyncio.sleep(1)
            else:
                raise utils.LLMRetriesExhausted(f"LLM completion failed after {MAX_RETRIES} retries: {exc}",
                                                status_code=getattr(exc, "status_code", None)) from exc


def _give_up_on_chat(exc: BaseException) -> bool:
    """The endpoint says this model is not served by /chat/completions: from now on the SDK's litellm (which bridges to the
    Responses API) answers every call.  True = retry this call through it."""
    global _fallback_all
    if not _fallback_all:
        log.warning("the model is not served by /chat/completions (%s); loading litellm to route it (about 150 MB more memory)", str(exc)[:160])
    _fallback_all = True
    return True


_encoder: Any = None
_encoder_failed = False


def _token_encoder():
    global _encoder, _encoder_failed
    if _encoder is not None or _encoder_failed:
        return _encoder
    try:
        spec = importlib.util.find_spec("litellm")
        if spec and spec.submodule_search_locations and not os.environ.get("TIKTOKEN_CACHE_DIR"):
            bundled = Path(next(iter(spec.submodule_search_locations))) / "litellm_core_utils" / "tokenizers"
            if bundled.is_dir():
                os.environ["TIKTOKEN_CACHE_DIR"] = str(bundled)      # litellm ships the BPE files: no download
        import tiktoken

        _encoder = tiktoken.get_encoding(_TOKEN_ENCODING)
    except Exception:  # noqa: BLE001 - counting is an estimate that sizes nodes; never fail indexing over it
        _encoder_failed = True
        log.warning("tiktoken unavailable; token counts are estimated as characters / 4", exc_info=True)
    return _encoder


def count_tokens(text, model=None):
    if not text:
        return 0
    encoder = _token_encoder()
    if encoder is None:
        return max(1, len(text) // 4)
    return len(encoder.encode(text, disallowed_special=()))


# ------------------------------------------------------------------------------------------------ installation
_REPLACEMENTS = {"llm_completion": llm_completion, "llm_acompletion": llm_acompletion, "count_tokens": count_tokens}


def install() -> int:
    """Rebind the three functions in every SDK module that holds its own copy.  Idempotent.  Returns how many bindings changed (0 = SDK
    shape unknown: the SDK keeps using litellm, which still works, only with more memory)."""
    global _installed
    with _lock:
        utils = importlib.import_module("pageindex.utils")
        for name in _REPLACEMENTS:
            if not callable(getattr(utils, name, None)):
                log.warning("pageindex.utils.%s not found; litellm stays in use", name)
                return 0
        if not _installed:
            for name in ("llm_completion", "llm_acompletion"):
                _originals[name] = getattr(utils, name)
            if not hasattr(utils, "_llm_backend") or not hasattr(utils, "_NO_RETRY_STATUS") or not hasattr(utils, "LLMRetriesExhausted"):
                log.warning("the SDK's retry helpers moved; litellm stays in use")
                return 0
        changed = 0
        for module_name in _MODULES:
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            for name, replacement in _REPLACEMENTS.items():
                current = getattr(module, name, None)
                if current is not None and current is not replacement:
                    _rebound.append((module, name, current))
                    setattr(module, name, replacement)
                    changed += 1
        _installed = True
        return changed


def uninstall() -> None:
    """Put the SDK's own functions back (tests, orderly shutdown)."""
    global _installed, _fallback_all, _encoder, _encoder_failed
    with _lock:
        for module, name, original in reversed(_rebound):
            setattr(module, name, original)
        _rebound.clear()
        _originals.clear()
        _sync_clients.clear()
        _installed = _fallback_all = _encoder_failed = False
        _encoder = None
