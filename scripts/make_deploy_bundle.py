"""Build the clean folder that becomes your deployment repository: Render (default) or a Hugging Face Space.

    python scripts/make_deploy_bundle.py [--target render|hf] [--out C:\\rl_deploy_bundle]
    (or:  .\\scripts\\make_deploy_bundle.ps1 [-Target render|hf] [-Out ...])

The bundle contains exactly what the container needs and nothing else:

    render:  Dockerfile  .dockerignore  .gitignore  requirements-deploy.txt  render.yaml  README.md (neutral)  reportlens/  devtools/  samples/
    hf:      Dockerfile  .dockerignore  requirements-deploy.txt  README.md (Space front matter)  reportlens/  devtools/  samples/

It never contains .env, research/, data/, tests/, docs/, virtual environments or caches, and the script FAILS (and removes
nothing you did not put there) if a key-like string, a token, a secret value in render.yaml or a forbidden file is found in the
result.  Re-running it refreshes the managed items in place and keeps a `.git` folder, so the bundle folder can be your git checkout.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = Path(r"C:\rl_deploy_bundle")
TARGETS = ("render", "hf")

FILES = ("Dockerfile", ".dockerignore", "requirements-deploy.txt", "LICENSE", "THIRD_PARTY_NOTICES.md", "SECURITY.md")
RENDER_FILES = ("render.yaml",)
TREES = ("reportlens", "devtools", "samples", "demo")        # demo/: the read-only demo chat (third-party PDF: private bundle only)
MANAGED_COMMON = (*FILES, "README.md", *TREES)
MANAGED = {"render": (*MANAGED_COMMON, *RENDER_FILES, ".gitignore"), "hf": MANAGED_COMMON}
KEEP = {".git"}                                             # allowed in the output folder, never touched

IGNORE_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".pageindex"}
IGNORE_SUFFIXES = {".pyc", ".pyo", ".map"}
FORBIDDEN_NAMES = {".env", "research", "data", "tests", "docs", ".venv", "venv", "scripts", "__pycache__", "node_modules", ".pytest_cache"}
FORBIDDEN_GLOBS = ("*.lock", "*.lock.txt", ".env.*")

# Anything that looks like a credential.  The two placeholders below are shipped on purpose (offline demo / client default).
SECRET_PATTERNS = {
    "OpenAI-style key": re.compile(rb"\bsk-(?:proj-|svcacct-|admin-|live-|test-)?[A-Za-z0-9_\-]{20,}"),
    "Hugging Face token": re.compile(rb"\bhf_[A-Za-z0-9]{30,}"),
    "GitHub token": re.compile(rb"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{30,}"),
    "Render API key": re.compile(rb"\brnd_[A-Za-z0-9]{20,}"),
    "Google API key": re.compile(rb"\bAIza[0-9A-Za-z_\-]{30,}"),
    "Groq / xAI key": re.compile(rb"\b(?:gsk|xai)[_-][A-Za-z0-9]{30,}"),
    "key assignment": re.compile(rb"\b(?:OPENAI_API_KEY|LLM_API_KEY|ACCESS_CODE|SESSION_SECRET|HF_TOKEN)\s*=\s*[\"']?(?!sk-demo-mock|sk-not-needed)[A-Za-z0-9_\-]{12,}"),
    "AWS key id": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "private key block": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
ALLOWED_PLACEHOLDERS = (b"sk-demo-mock-not-a-real-key", b"sk-not-needed")
RENDER_SECRET_KEYS = ("OPENAI_API_KEY", "ACCESS_CODE")      # must be `sync: false` in render.yaml (typed in the dashboard)

SHORT_DESCRIPTION = "Chat with annual reports: cited answers, live scores"
assert len(SHORT_DESCRIPTION) <= 60

README_BODY = """# Annual Report Lens

*Chat with your annual report, with live answer evaluation.*

Ask questions about **one annual-report PDF** and get answers with page-level citations. Click a citation and the PDF opens
beside the chat, scrolled to the page, with the supporting passage highlighted. Every answer is then scored live by an AI
judge with RAGAS (faithfulness, answer relevancy, context precision).

Retrieval is [PageIndex](https://github.com/VectifyAI/PageIndex) (a table-of-contents tree that a language model navigates,
no vector database). The server's model is OpenAI's; visitors may bring their own key for OpenAI, Anthropic, Google Gemini,
OpenRouter, Groq, Mistral, DeepSeek, xAI or Together AI.

By [sagarutkarsh1](https://github.com/sagarutkarsh1) · MIT licence (see LICENSE and THIRD_PARTY_NOTICES.md) · built with
[Claude Code](https://claude.com/claude-code). This repository is the deployment bundle; the source code, tests and docs live
in the main repository.

## How to use

0. No code yet? Open the link and choose **View a demo chat**: a real conversation with a published annual report, read-only.
1. Open the link you were given and enter the access code.
2. Start a chat and upload a text-based annual-report PDF (searchable text, not a scan; keep it within the size limit shown on the upload card).
3. Wait for indexing to finish ({INDEXING_TIME}), then ask a question.
4. Click a numbered citation chip to see the page and the highlighted passage. The scores appear under the answer after a few seconds.

The first request after a quiet period can take a minute while the service wakes up.

## Please read before you upload anything

- **Your PDF text and your questions are sent to the model provider** (the server's OpenAI account, or the provider of your own key) to build the index, write the answers and score them. Providers keep API requests for a limited time under their own policies.
- **Storage is temporary.** Uploaded files and chats live on the server's temporary disk and are deleted whenever the service restarts or goes to sleep.
- **Do not upload confidential, personal or otherwise sensitive documents.** Use public reports.
- This is a demo with a spending cap. When the cap is reached, uploads and questions pause until the owner resets it.
- Answers and scores are AI-generated estimates; check important figures against the cited page.
"""

SPACE_README = f"""---
title: Annual Report Lens
emoji: 🔎
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: {SHORT_DESCRIPTION}
---

""" + README_BODY.replace("Open the link you were given", "Open the **direct link** of this Space (`https://<owner>-<space>.hf.space`), not the embedded view on huggingface.co, ").replace(
    "{INDEXING_TIME}", "about a minute for a typical report")

RENDER_README = README_BODY.replace("{INDEXING_TIME}", "on the free server this can take several minutes for a long report")

RENDER_GITIGNORE = """# Local state and secrets never belong in this repository.
.env
.env.*
data/
.venv/
venv/
__pycache__/
*.pyc
.pytest_cache/
.DS_Store
"""


def copy_tree(src: Path, dst: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in IGNORE_DIRS or Path(n).suffix in IGNORE_SUFFIXES}

    shutil.copytree(src, dst, ignore=ignore)


def build(out: Path, target: str = "render") -> None:
    if target not in TARGETS:
        raise SystemExit(f"ERROR: unknown target {target!r} (use one of {', '.join(TARGETS)}).")
    managed = MANAGED[target]
    out.mkdir(parents=True, exist_ok=True)
    known = set(MANAGED["render"]) | set(MANAGED["hf"])         # refreshing a bundle of the other flavour is fine
    leftovers = sorted(p.name for p in out.iterdir() if p.name not in known and p.name not in KEEP)
    if leftovers:
        raise SystemExit(f"ERROR: {out} already contains other items ({', '.join(leftovers)}). Use an empty folder (or one made by this script).")
    for name in sorted(known):                              # refresh our own items only
        stale = out / name
        if stale.is_dir():
            shutil.rmtree(stale)
        elif stale.exists():
            stale.unlink()
    for name in (*FILES, *(RENDER_FILES if target == "render" else ())):
        source = REPO / name
        if not source.is_file():
            raise SystemExit(f"ERROR: {source} is missing.")
        shutil.copyfile(source, out / name)
    for name in TREES:
        if not (REPO / name).is_dir():
            raise SystemExit(f"ERROR: {REPO / name} is missing.")
        copy_tree(REPO / name, out / name)
    (out / "README.md").write_text(RENDER_README if target == "render" else SPACE_README, encoding="utf-8", newline="\n")
    if target == "render":
        (out / ".gitignore").write_text(RENDER_GITIGNORE, encoding="utf-8", newline="\n")
    for name in ("Dockerfile", "render.yaml"):
        if (out / name).exists():
            (out / name).write_bytes((out / name).read_bytes().replace(b"\r\n", b"\n"))      # Linux builds want LF endings


def audit_render_yaml(text: str) -> list[str]:
    """render.yaml must not carry a secret value: the two secrets are `sync: false`, and nothing else looks like a key."""
    problems: list[str] = []
    for name in RENDER_SECRET_KEYS:
        m = re.search(rf"-\s*key:\s*{name}\b[^\n]*\n((?:[ \t]+[^\n\- \t][^\n]*\n?)*)", text + "\n")
        block = m.group(1) if m else ""
        if not m:
            problems.append(f"render.yaml does not declare {name}")
        elif not re.search(r"sync:\s*false", block) or re.search(r"\bvalue:", block):
            problems.append(f"render.yaml: {name} must be `sync: false` with no value (type it in the Render dashboard)")
    return problems


def audit(out: Path, target: str = "render") -> list[str]:
    """Problems found in the finished bundle (empty list = clean)."""
    problems: list[str] = []
    top = {p.name for p in out.iterdir()}
    unexpected = top - set(MANAGED[target]) - KEEP
    if unexpected:
        problems.append(f"unexpected top-level items: {sorted(unexpected)}")
    for path in out.rglob("*"):
        if ".git" in path.relative_to(out).parts[:1]:
            continue
        rel = path.relative_to(out).as_posix()
        parts = set(path.relative_to(out).parts)
        in_demo_store = rel.startswith("demo/files/pageindex/")      # PageIndex's own store layout has a docs/ folder
        if parts & FORBIDDEN_NAMES and not (in_demo_store and parts & FORBIDDEN_NAMES == {"docs"}):
            problems.append(f"forbidden item: {rel}")
        if any(path.match(glob) for glob in FORBIDDEN_GLOBS):
            problems.append(f"forbidden file type: {rel}")
        if path.is_file():
            data = path.read_bytes()
            for label, rx in SECRET_PATTERNS.items():
                for match in rx.finditer(data):
                    if match.group(0) in ALLOWED_PLACEHOLDERS:
                        continue
                    problems.append(f"{label} found in {rel} (offset {match.start()})")      # never print the match itself
    readme = out / "README.md"
    if target == "hf" and readme.exists() and not readme.read_text(encoding="utf-8").startswith("---\ntitle: Annual Report Lens\n"):
        problems.append("README.md lost its Hugging Face front matter")
    if target == "render":
        if readme.exists() and readme.read_text(encoding="utf-8").startswith("---"):
            problems.append("README.md has Hugging Face front matter (this is a Render bundle)")
        blueprint = out / "render.yaml"
        if blueprint.exists():
            problems += audit_render_yaml(blueprint.read_text(encoding="utf-8"))
    return problems


def folder_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and ".git" not in p.relative_to(path).parts[:1])


def next_steps(out: Path, target: str) -> list[str]:
    if target == "hf":
        return [
            "Next steps (details: docs\\DEPLOY.md, 'Alternatives'). Hugging Face now charges for Docker Spaces, so this path is no longer free:",
            f'  1. cd "{out}"',
            "  2. git init -b main        (only the first time)",
            "  3. git remote add space https://huggingface.co/spaces/<your-user>/<your-space>",
            "  4. git add -A ; git commit -m \"Deploy Annual Report Lens\"",
            "  5. git push space main     (Git asks for your user name and, as the password, a Hugging Face WRITE token you create yourself)",
            "Set the secrets and variables on the Space's Settings page BEFORE or right after the first push; never put them in a file.",
        ]
    return [
        "Next steps (full walkthrough with explanations: docs\\DEPLOY.md):",
        "  1. On github.com create a PRIVATE repository (for example 'reportlens-deploy'), empty: no README, no .gitignore.",
        f'  2. cd "{out}"',
        "  3. git init -b main        (only the first time)",
        "  4. git add -A ; git commit -m \"Deploy Annual Report Lens\"",
        "  5. git remote add origin https://github.com/<your-user>/reportlens-deploy.git",
        "  6. git push -u origin main (Git opens a sign-in window or asks for your GitHub user name and a personal access token: you type them, nobody else)",
        "  7. dashboard.render.com -> New -> Blueprint -> connect that repository -> Render reads render.yaml.",
        "  8. Render asks for two secrets: OPENAI_API_KEY (a SEPARATE key from a project with its own monthly limit) and ACCESS_CODE",
        "     (12+ random characters). Type them into Render's form only. Click Deploy Blueprint.",
        "  9. The first build takes roughly 5-15 minutes (it downloads about a gigabyte of Python packages). When the service shows Live,",
        "     open https://reportlens-<random>.onrender.com (the exact address is at the top of the service page), enter the access code.",
        " 10. Send people the link and the access code in separate messages. The first visit after 15 idle minutes takes about a minute.",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the clean deployment bundle (the repository content for Render, or a Hugging Face Space).")
    parser.add_argument("--target", choices=TARGETS, default="render", help="render (default; free web service) or hf (Hugging Face Docker Space)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output folder (default {DEFAULT_OUT})")
    args = parser.parse_args(argv)
    out = args.out.expanduser().resolve()
    if REPO == out or REPO in out.parents:
        raise SystemExit("ERROR: the output folder must be outside the project folder.")
    build(out, args.target)
    problems = audit(out, args.target)
    if problems:
        print("BUNDLE REJECTED - nothing should be pushed from this folder:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    files = sum(1 for p in out.rglob("*") if p.is_file() and ".git" not in p.relative_to(out).parts[:1])
    size = folder_size(out)
    print(f"Bundle ready ({args.target}): {out}")
    print(f"  {files} files, {size / 1024 / 1024:.1f} MB  (audit passed: no .env, no keys or tokens, no research/data/tests)")
    print()
    for line in next_steps(out, args.target):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
