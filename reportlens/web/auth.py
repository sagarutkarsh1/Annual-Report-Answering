"""Access gate for a publicly reachable deployment: one shared access code, a signed cookie, and a login rate limit.

* `ACCESS_CODE` empty  -> no gate (the local, single-user behaviour); every helper here is then a no-op.
* `ACCESS_CODE` set    -> `POST /api/login {code}` checks it in constant time and sets an HttpOnly, SameSite=Lax cookie holding
  `expiry.nonce.HMAC`.  The HMAC key is derived from the access code plus `SESSION_SECRET` (or random bytes per process), so
  changing either invalidates every cookie.  A cookie, not a header, because pdf.js fetches the PDF itself.
* `AuthMiddleware` answers 401 `auth_required` for every `/api/*` route except health / auth / login / logout.  Static files
  stay public (the login screen is part of them).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import time
from typing import Optional

from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Receive, Scope, Send

from reportlens.config import Settings

log = logging.getLogger("reportlens.web")

COOKIE_NAME = "rl_session"
SESSION_TTL_S = 12 * 3600
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_S = 10 * 60
OPEN_API_PATHS = frozenset({"/api/health", "/api/auth", "/api/login", "/api/logout"})
MAX_CODE_CHARS = 512


class AccessGate:
    """Signs and verifies the login cookie.  Immutable after construction; safe to share between threads."""

    def __init__(self, settings: Settings, *, clock=time.time):
        self.code = (settings.access_code or "").strip()
        self.required = bool(self.code)
        self._clock = clock
        secret = (settings.session_secret or "").encode("utf-8") or secrets.token_bytes(32)
        self._key = hashlib.sha256(b"reportlens-session-v1\0" + secret + b"\0" + self.code.encode("utf-8")).digest()
        self._code_digest = hashlib.sha256(self.code.encode("utf-8")).digest()

    # ----- the access code
    def code_matches(self, submitted: str) -> bool:
        """Constant-time comparison of fixed-length digests (so neither the content nor the length leaks, and non-ASCII is fine)."""
        digest = hashlib.sha256((submitted or "")[:MAX_CODE_CHARS].encode("utf-8", errors="replace")).digest()
        return hmac.compare_digest(digest, self._code_digest)

    # ----- the cookie
    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        payload = f"{int(self._clock()) + SESSION_TTL_S}.{secrets.token_hex(8)}"
        return f"{payload}.{self._sign(payload)}"

    def valid(self, token: Optional[str]) -> bool:
        if not self.required:
            return True
        try:
            expiry, nonce, signature = (token or "").split(".")
            payload = f"{expiry}.{nonce}"
            ok = hmac.compare_digest(signature.encode("ascii"), self._sign(payload).encode("ascii"))
            return ok and int(expiry) > self._clock()
        except (ValueError, UnicodeError):
            return False

    def valid_for_scope(self, scope: Scope) -> bool:
        if not self.required:
            return True
        raw = Headers(scope=scope).get("cookie")
        return self.valid(cookie_parser(raw).get(COOKIE_NAME) if raw else None)

    @staticmethod
    def is_https(scope: Scope) -> bool:
        """Secure cookies on https.  X-Forwarded-Proto is honoured whatever TRUST_PROXY says: a client that fakes it over plain
        http only makes the browser drop its own cookie."""
        forwarded = Headers(scope=scope).get("x-forwarded-proto", "").split(",")[0].strip().lower()
        return scope.get("scheme") == "https" or forwarded == "https"

    def set_cookie_header(self, token: str, *, secure: bool, max_age: int = SESSION_TTL_S) -> str:
        parts = [f"{COOKIE_NAME}={token}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Lax"]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)


class AuthMiddleware:
    """Pure ASGI: 401 `auth_required` for gated API routes when the request has no valid login cookie."""

    def __init__(self, app: ASGIApp, gate: AccessGate):
        self.app = app
        self.gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.gate.required:
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path.startswith("/api/") and path not in OPEN_API_PATHS and not self.gate.valid_for_scope(scope):
            from reportlens.web.app import error_response      # late: app.py imports this module

            await error_response("auth_required", "Enter the access code to continue.", 401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------------------------- client address
def _parse_ip(text: str) -> Optional[str]:
    """'1.2.3.4', '1.2.3.4:5678', '2001:db8::1' and '[2001:db8::1]:443' -> the bare address; anything else -> None."""
    text = text.strip()
    if text.startswith("[") and "]" in text:
        text = text[1: text.index("]")]
    elif text.count(":") == 1:                                   # IPv4 with a port (an IPv6 address has several colons)
        text = text.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def client_ip(scope: Scope, settings: Settings) -> str:
    """The address a limit is keyed on.  The socket peer, unless TRUST_PROXY=1: then the FIRST `X-Forwarded-For` entry (or, with
    PROXY_HOPS=N, the Nth from the right, which is what a proxy that appends the real client address can be trusted for).
    Malformed header values fall back to the socket peer."""
    peer = (scope.get("client") or ("unknown", 0))[0]
    if settings.trust_proxy:
        values = Headers(scope=scope).getlist("x-forwarded-for")
        entries = [e.strip() for v in values for e in v.split(",") if e.strip()]
        if entries:
            hops = settings.proxy_hops
            chosen = entries[-hops] if 0 < hops <= len(entries) else entries[0]
            parsed = _parse_ip(chosen)
            if parsed:
                return parsed
    return str(peer)
