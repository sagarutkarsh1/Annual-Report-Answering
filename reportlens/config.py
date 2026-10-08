"""Runtime settings, read from the environment / a .env file.  One immutable object, passed explicitly (no globals)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _bool(v: Optional[str], default: bool) -> bool:
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(v: Optional[str], default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


def _float(v: Optional[str], default: float) -> float:
    try:
        value = float(v) if v not in (None, "") else default
    except ValueError:
        return default
    return value if value == value and value not in (float("inf"), float("-inf")) else default   # NaN / inf fall back


# Container memory limits: cgroup v2 first, then v1.  "max" (v2) or a huge number (v1) means "no limit".
_CGROUP_LIMIT_FILES = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")
LOW_MEMORY_LIMIT_BYTES = 600 * 1024 * 1024        # a container limit at or below this switches LOW_MEMORY on in a public deployment (Render free: 512 MB)
_NO_LIMIT = 1 << 60


def detect_memory_limit_bytes(files: tuple[str, ...] = _CGROUP_LIMIT_FILES) -> Optional[int]:
    """The memory limit of the container we run in (bytes), or None when there is none / it cannot be read (Windows, bare metal)."""
    for name in files:
        try:
            raw = Path(name).read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            continue
        if raw.isdigit() and 0 < int(raw) < _NO_LIMIT:
            return int(raw)
    return None


def _hosts(v: Optional[str]) -> tuple[str, ...]:
    """'a.example, .hf.space' -> ('a.example', '.hf.space'): lower-cased, trimmed, empty items dropped."""
    return tuple(h for h in (part.strip().lower() for part in (v or "").split(",")) if h)


@dataclass(frozen=True)
class Settings:
    # --- OpenAI ---
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    # --- PageIndex (local Flash mode) ---
    pageindex_mode: str = "local"                 # "local" only for now; "cloud" is reserved (needs PAGEINDEX_API_KEY)
    index_model: str = "gpt-5.6-luna"
    chat_model: str = "gpt-5.6-sol"
    chat_reasoning_effort: Optional[str] = "medium"
    chat_protocol: str = "responses"              # "responses" | "chat"
    chat_max_turns: int = 12
    index_summary_concurrency: int = 16           # 6 when low_memory
    index_fallback_standard: bool = True          # no outline + >10 pages: Flash refuses -> retry with the slower LLM-built "standard" tree
    question_rewrite_model: str = "gpt-4.1-mini"  # turns follow-ups into standalone questions (used as RAGAS user_input)
    # --- RAGAS ---
    eval_enabled: bool = True
    judge_model: str = "gpt-4.1"
    judge_reasoning_effort: Optional[str] = None
    judge_max_tokens: int = 4096
    embedding_model: str = "text-embedding-3-small"
    eval_max_contexts: int = 12
    eval_max_chars_per_context: int = 8000
    eval_concurrency: int = 8                     # 3 when low_memory
    # --- App ---
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    max_upload_mb: int = 100
    max_pages: int = 1200
    history_turns: int = 6
    demo_mock: bool = False                       # start devtools.mock_openai in-process and point OpenAI traffic at it
    # --- Public deployment (all off / unlimited by default = the local single-user behaviour) ---
    access_code: Optional[str] = None             # set = every /api route except health/auth/login needs the login cookie
    session_secret: Optional[str] = None          # extra HMAC key material for the login cookie (empty = random per process)
    public_mode: bool = False                     # safe defaults for budget, sessions, rate limit, upload size (see load_settings)
    budget_usd_total: float = 0.0                 # 0 = unlimited; estimated spend at which uploads and questions are refused (HTTP 402)
    max_sessions: int = 0                         # 0 = unlimited; chats that may exist at the same time (HTTP 429)
    questions_per_hour_per_ip: int = 0            # 0 = unlimited
    index_cost_estimate_usd: float = 0.40         # charged to the budget per indexed document
    eval_cost_estimate_usd: float = 0.08          # charged to the budget per completed evaluation
    allowed_hosts: tuple[str, ...] = ()           # Host header allow-list; ".hf.space" also matches every subdomain
    trust_proxy: bool = False                     # believe X-Forwarded-For / X-Forwarded-Host / X-Forwarded-Proto (only behind your own proxy)
    proxy_hops: int = 0                           # 0 = client is the FIRST X-Forwarded-For entry; N = the Nth from the right (N trusted proxies)
    # --- Small hosts (Render free: 512 MB RAM, 0.1 CPU).  LOW_MEMORY=1, or automatic with PUBLIC_MODE inside a container limited to <= 600 MB ---
    low_memory: bool = False                      # lazy scoring libraries, one open PDF, lighter RAGAS import, in-process parsing, smaller concurrency
    index_in_subprocess: bool = False             # index in a short-lived child process whose memory returns to the OS afterwards (default: = low_memory)
    eval_in_subprocess: bool = False              # RAGAS scoring in a short-lived child process (the web process never imports RAGAS); default = low_memory
    lite_llm: bool = False                        # the indexing child answers model calls with the openai package instead of importing litellm (-130 MB); default = low_memory
    max_open_docs: int = 4                        # sessions whose PDF / tree stay open in memory (LRU); 1 when low_memory

    # ----- derived helpers -----
    @property
    def db_path(self) -> Path:
        return self.data_dir / "reportlens.db"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    def session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / session_id

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key) or self.demo_mock

    def with_(self, **changes) -> "Settings":
        return replace(self, **changes)

    def public(self) -> dict:
        """Safe-to-expose subset for GET /api/config (never includes keys)."""
        return {
            "index_model": self.index_model,
            "chat_model": self.chat_model,
            "chat_reasoning_effort": self.chat_reasoning_effort,
            "chat_protocol": self.chat_protocol,
            "eval_enabled": self.eval_enabled,
            "judge_model": self.judge_model,
            "embedding_model": self.embedding_model,
            "max_upload_mb": self.max_upload_mb,
            "max_pages": self.max_pages,
            "openai_configured": self.openai_configured,
            "demo_mock": self.demo_mock,
            "public_mode": self.public_mode,
            "low_memory": self.low_memory,
        }


def settings_to_json(settings: Settings) -> str:
    """Serialise for a child process (indexing worker).  Carries the API key: hand it over a pipe, never on a command line."""
    data = asdict(settings)
    data["data_dir"] = str(settings.data_dir)
    return json.dumps(data)


def settings_from_json(text: str) -> Settings:
    data = json.loads(text)
    known = {f.name for f in fields(Settings)}
    data = {k: v for k, v in data.items() if k in known}
    data["data_dir"] = Path(data["data_dir"])
    if "allowed_hosts" in data:
        data["allowed_hosts"] = tuple(data["allowed_hosts"])
    return Settings(**data)


def load_settings(env_file: Optional[os.PathLike | str] = None, environ: Optional[dict] = None) -> Settings:
    """Precedence: real environment variables  >  .env file  >  defaults.
    `environ` (tests) replaces os.environ and disables .env loading unless env_file is given."""
    file_vals: dict = {}
    path = Path(env_file) if env_file else (PROJECT_ROOT / ".env" if environ is None else None)
    if path is not None and path.is_file():
        file_vals = {k: v for k, v in dotenv_values(path).items() if v is not None}
    env = {**file_vals, **(os.environ if environ is None else environ)}
    g = env.get

    data_dir = Path(g("REPORTLENS_DATA_DIR") or (PROJECT_ROOT / "data"))
    if not data_dir.is_absolute():
        data_dir = (PROJECT_ROOT / data_dir).resolve()

    effort = (g("PI_CHAT_REASONING_EFFORT", "medium") or "").strip().lower() or None
    judge_effort = (g("RAGAS_JUDGE_REASONING_EFFORT") or "").strip().lower() or None
    protocol = (g("PI_CHAT_PROTOCOL") or "responses").strip().lower()
    if protocol not in ("responses", "chat"):
        protocol = "responses"

    public = _bool(g("PUBLIC_MODE"), False)
    # PUBLIC_MODE only fills in what the owner did not set: an explicit value (even 0 = unlimited) always wins.
    def public_default(name: str, parse, public_value, local_value):
        return parse(g(name), public_value if public else local_value)

    # LOW_MEMORY: explicit 0/1 wins; unset = on when PUBLIC_MODE runs inside a container limited to <= 600 MB.  A real-environment
    # decision only: tests that pass `environ` never depend on the machine they run on.
    explicit_low = g("LOW_MEMORY")
    if explicit_low is not None and explicit_low.strip() != "":
        low = _bool(explicit_low, False)
    elif public and environ is None:
        limit = detect_memory_limit_bytes()
        low = limit is not None and limit <= LOW_MEMORY_LIMIT_BYTES
    else:
        low = False

    def tiered(name: str, parse, normal, public_value, low_value):
        """Defaults per deployment kind; an explicit value always wins."""
        return parse(g(name), low_value if low else public_value if public else normal)

    return Settings(
        openai_api_key=(g("OPENAI_API_KEY") or "").strip() or None,
        openai_base_url=(g("OPENAI_BASE_URL") or "").strip() or None,
        pageindex_mode=(g("PAGEINDEX_MODE") or "local").strip().lower(),
        index_model=(g("PI_INDEX_MODEL") or "gpt-5.6-luna").strip(),
        chat_model=(g("PI_CHAT_MODEL") or "gpt-5.6-sol").strip(),
        chat_reasoning_effort=effort,
        chat_protocol=protocol,
        chat_max_turns=_int(g("PI_CHAT_MAX_TURNS"), 12),
        index_summary_concurrency=max(1, tiered("PI_INDEX_SUMMARY_CONCURRENCY", _int, 16, 16, 6)),
        index_fallback_standard=public_default("PI_INDEX_FALLBACK_STANDARD", _bool, False, True),
        question_rewrite_model=(g("QUESTION_REWRITE_MODEL") or "gpt-4.1-mini").strip(),
        eval_enabled=_bool(g("EVAL_ENABLED"), True),
        judge_model=(g("RAGAS_JUDGE_MODEL") or "gpt-4.1").strip(),
        judge_reasoning_effort=judge_effort,
        judge_max_tokens=_int(g("RAGAS_JUDGE_MAX_TOKENS"), 4096),
        embedding_model=(g("RAGAS_EMBEDDING_MODEL") or "text-embedding-3-small").strip(),
        eval_max_contexts=tiered("EVAL_MAX_CONTEXTS", _int, 12, 8, 6),
        eval_max_chars_per_context=_int(g("EVAL_MAX_CHARS_PER_CONTEXT"), 8000),
        eval_concurrency=max(1, tiered("EVAL_CONCURRENCY", _int, 8, 8, 3)),
        host=(g("REPORTLENS_HOST") or "127.0.0.1").strip(),
        port=_int(g("REPORTLENS_PORT"), _int(g("PORT"), 8000)),     # PORT: what Render / Cloud Run / the Dockerfile inject
        data_dir=data_dir,
        max_upload_mb=public_default("MAX_UPLOAD_MB", _int, 25, 100),
        max_pages=public_default("MAX_PAGES", _int, 400, 1200),
        history_turns=_int(g("HISTORY_TURNS"), 6),
        demo_mock=_bool(g("REPORTLENS_DEMO_MOCK"), False),
        access_code=(g("ACCESS_CODE") or "").strip() or None,
        session_secret=(g("SESSION_SECRET") or "").strip() or None,
        public_mode=public,
        budget_usd_total=max(0.0, public_default("BUDGET_USD_TOTAL", _float, 10.0, 0.0)),
        max_sessions=max(0, public_default("MAX_SESSIONS", _int, 30, 0)),
        questions_per_hour_per_ip=max(0, public_default("QUESTIONS_PER_HOUR_PER_IP", _int, 15, 0)),
        index_cost_estimate_usd=max(0.0, _float(g("INDEX_COST_ESTIMATE_USD"), 0.40)),
        eval_cost_estimate_usd=max(0.0, _float(g("EVAL_COST_ESTIMATE_USD"), 0.08)),
        allowed_hosts=_hosts(g("ALLOWED_HOSTS")),
        trust_proxy=_bool(g("TRUST_PROXY"), False),
        proxy_hops=max(0, _int(g("PROXY_HOPS"), 0)),
        low_memory=low,
        index_in_subprocess=_bool(g("INDEX_IN_SUBPROCESS"), low),
        lite_llm=_bool(g("LITE_LLM"), low),
        eval_in_subprocess=_bool(g("EVAL_IN_SUBPROCESS"), low),
        max_open_docs=max(1, tiered("MAX_OPEN_DOCS", _int, 4, 4, 1)),
    )
