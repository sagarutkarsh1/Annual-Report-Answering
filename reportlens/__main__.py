"""Command line entry point:  python -m reportlens [--host H] [--port N] [--demo] [--data-dir D] [--open] [--log-level L]

Importing this module has no side effects (only definitions); the server starts under `if __name__ == "__main__"` at the
bottom. That matters on Windows, where the PageIndex SDK may start `spawn` worker processes that re-import `__main__`:
anything that ran at import time would run again in every worker.
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional, Sequence

log = logging.getLogger("reportlens.main")

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
NOISY_LOGGERS = ("httpx", "httpcore", "openai", "LiteLLM", "litellm", "asyncio")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m reportlens",
        description="ReportLens: ask questions about an annual report and get answers with page-level citations and RAGAS scores.",
    )
    parser.add_argument("--host", help="interface to bind (default: REPORTLENS_HOST or 127.0.0.1; use 0.0.0.0 in a container, with ACCESS_CODE / PUBLIC_MODE set)")
    parser.add_argument("--port", type=int, help="port (default: REPORTLENS_PORT, then PORT, then 8000)")
    parser.add_argument("--demo", action="store_true",
                        help="offline demo: use the built-in fake OpenAI server, no API key and no cost (answers are canned extracts)")
    parser.add_argument("--data-dir", help="where the database, uploaded PDFs and indexes live (default: REPORTLENS_DATA_DIR or ./data)")
    parser.add_argument("--open", action="store_true", help="open the browser once the server is up")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"], help="log verbosity (default: info)")
    return parser


def port_in_use(host: str, port: int) -> bool:
    """True if something is already listening. Windows lets two sockets share a port unless the bind is exclusive."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper()), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)
    if level != "debug":                      # per-request client chatter drowns out our own log
        for name in NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)


def _announce(text: str) -> None:
    """User-facing banner on stdout, independent of the log level."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _open_browser_when_ready(server: object, url: str) -> None:
    for _ in range(300):                      # up to 30 s
        if getattr(server, "started", False):
            webbrowser.open(url)
            return
        if getattr(server, "should_exit", False):
            return
        time.sleep(0.1)


GRACEFUL_SHUTDOWN_S = 10          # hosts send SIGTERM when a Space sleeps or is redeployed; open answer streams must not hold the exit up


def describe_exposure(settings) -> str:
    """One log line about how the server is protected (never contains the access code, the secret or any key)."""
    budget = f"${settings.budget_usd_total:g}" if settings.budget_usd_total > 0 else "unlimited"
    return (f"public_mode={settings.public_mode} access_gate={'on' if settings.access_code else 'off'} budget={budget} "
            f"max_sessions={settings.max_sessions or 'unlimited'} questions_per_hour_per_ip={settings.questions_per_hour_per_ip or 'unlimited'} "
            f"allowed_hosts={','.join(settings.allowed_hosts) or '-'} trust_proxy={settings.trust_proxy} "
            f"max_upload_mb={settings.max_upload_mb} max_pages={settings.max_pages} low_memory={settings.low_memory} "
            f"index_in_subprocess={settings.index_in_subprocess} data_dir={settings.data_dir}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    started = time.perf_counter()
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)

    import uvicorn

    from reportlens.config import load_settings
    from reportlens.web.app import create_app

    settings = load_settings()
    overrides: dict = {}
    if args.host:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.data_dir:
        overrides["data_dir"] = Path(args.data_dir).expanduser().resolve()
    if args.demo:
        overrides["demo_mock"] = True
    settings = settings.with_(**overrides)

    if not settings.demo_mock and not settings.openai_api_key:
        log.warning("No OPENAI_API_KEY found. Add OPENAI_API_KEY to .env or run with --demo. "
                    "The page will load, but uploads cannot be indexed and questions cannot be answered.")
    if settings.host not in LOCAL_HOSTS and not (settings.access_code or settings.public_mode):
        log.warning("Binding to %s without ACCESS_CODE or PUBLIC_MODE: anyone who can reach this port can read uploaded reports "
                    "and spend your OpenAI credit. Keep it on 127.0.0.1 unless you know the network is trusted.", settings.host)
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)       # an empty, ephemeral or fresh data dir is normal; an unwritable one is not
    except OSError as exc:
        log.error("Cannot create the data folder %s (%s). Set REPORTLENS_DATA_DIR to a writable folder, for example /tmp/reportlens.", settings.data_dir, exc)
        return 2
    if settings.port and port_in_use(settings.host, settings.port):
        log.error("Port %d is already in use. Stop the other program or choose another port with --port N.", settings.port)
        return 2

    url = f"http://{'localhost' if settings.host in ('0.0.0.0', '::') else settings.host}:{settings.port}/"
    # proxy_headers only behind your own proxy (TRUST_PROXY=1): otherwise a client could forge X-Forwarded-For and dodge the per-IP limits.
    config = uvicorn.Config(create_app(settings), host=settings.host, port=settings.port, log_level=args.log_level,
                            log_config=None, access_log=False,    # our middleware logs every request, with its duration
                            proxy_headers=settings.trust_proxy, forwarded_allow_ips="*" if settings.trust_proxy else None,
                            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S)
    server = uvicorn.Server(config)
    log.info("Settings: %s", describe_exposure(settings))
    log.info("Imports and configuration took %.1f s; the server now builds its stack (SDK imports take a few seconds) and starts listening.",
             time.perf_counter() - started)
    _announce(f"ReportLens {'DEMO ' if settings.demo_mock else ''}running at {url}  (Ctrl+C to stop)")
    if args.open:
        threading.Thread(target=_open_browser_when_ready, args=(server, url), name="open-browser", daemon=True).start()
    try:
        server.run()          # uvicorn turns SIGINT and SIGTERM into a graceful shutdown (lifespan teardown closes the store and the indexer)
    except KeyboardInterrupt:
        pass
    log.info("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
