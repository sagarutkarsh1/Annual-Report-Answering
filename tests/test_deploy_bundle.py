"""The deployment files: requirements-deploy.txt, Dockerfile, .dockerignore and scripts/make_deploy_bundle.py (offline, no Docker needed)."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from scripts import make_deploy_bundle as bundle

ROOT = Path(__file__).resolve().parent.parent
FAKE_KEY = "sk-" + "proj-" + "A1b2C3d4" * 6              # assembled at run time so this file itself holds no key-like literal


def lines(name: str) -> list[str]:
    return (ROOT / name).read_text(encoding="utf-8").splitlines()


# ----------------------------------------------------------------------------------------------- requirements-deploy.txt
def requirement_lines() -> list[str]:
    out = []
    for raw in lines("requirements-deploy.txt"):
        text = raw.split("#", 1)[0].strip()
        if text:
            out.append(text)
    return out


def test_deploy_requirements_mirror_pyproject_with_the_same_pins():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    wanted = {re.split(r"[<>=!\[ ]", d, maxsplit=1)[0].lower(): d.split("#")[0].strip() for d in project}
    have = {re.split(r"[<>=!\[ ]", d, maxsplit=1)[0].lower(): d for d in requirement_lines()}
    for name, spec in wanted.items():
        assert have.get(name) == spec, f"{name}: pyproject says {spec!r}, requirements-deploy.txt says {have.get(name)!r}"
    assert {"pageindex==0.2.21", "ragas==0.4.3", "langchain-community==0.4.1"} <= set(have.values())


def test_deploy_requirements_are_linux_clean_and_keep_litellm_safe():
    text = "\n".join(requirement_lines()).lower()
    for bad in ("pywin32", "pypiwin32", "colorama", "win32", "winreg", "-r ", "requirements.lock"):
        assert bad not in text
    assert "litellm>=1.97,!=1.82.7,!=1.82.8" in requirement_lines()
    assert not any(line.startswith(("--index-url", "--extra-index-url", "--find-links", "http", "git+")) for line in requirement_lines())


# ----------------------------------------------------------------------------------------------- Dockerfile
def test_dockerfile_follows_the_hosting_requirements():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert instructions[0] == "FROM python:3.13-slim"
    assert "useradd --create-home --uid 1000 user" in text and "\nUSER user" in text
    assert "EXPOSE 7860" in text and "HEALTHCHECK" in text and "/api/health" in text
    for env in ("PYTHONUNBUFFERED=1", "PYTHONIOENCODING=utf-8", "RAGAS_DO_NOT_TRACK=true", "OPENAI_AGENTS_DISABLE_TRACING=1",
                "HOME=/home/user", "REPORTLENS_DATA_DIR=/tmp/reportlens", "PORT=7860", "PUBLIC_MODE=1", "MALLOC_ARENA_MAX=2"):
        assert env in text, env
    assert "pip install --no-cache-dir -r requirements-deploy.txt" in text
    copies = [ln for ln in instructions if ln.startswith("COPY")]
    assert sorted(c.split()[1] for c in copies) == ["demo", "devtools", "reportlens", "requirements-deploy.txt", "samples"]
    assert "requirements.lock" not in text
    assert "apt-get" not in text and "gcc" not in text and "build-essential" not in text      # wheels only, no compiler
    assert instructions[-1].startswith('CMD ["python", "-m", "reportlens", "--host", "0.0.0.0"')           # exec form: SIGTERM reaches python
    baked = " ".join(instructions)
    assert "OPENAI_API_KEY" not in baked and "ACCESS_CODE" not in baked                                        # no secret is baked in


def test_dockerignore_keeps_secrets_and_local_state_out_of_the_build_context():
    entries = {ln.strip() for ln in lines(".dockerignore") if ln.strip() and not ln.startswith("#")}
    for needed in (".venv/", "research/", "data/", "tests/", ".env", "__pycache__/", "node_modules/", "*.lock"):
        assert needed in entries, needed


# ----------------------------------------------------------------------------------------------- render.yaml
def render_yaml() -> str:
    return (ROOT / "render.yaml").read_text(encoding="utf-8")


def env_blocks(text: str) -> dict[str, dict[str, str]]:
    """{key: {field: value}} for every `- key:` entry of render.yaml (a tiny reader: the file is flat and has no anchors)."""
    out: dict[str, dict[str, str]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        m = re.match(r"\s*-\s*key:\s*(\S+)", line)
        if m:
            current = out.setdefault(m.group(1), {})
        elif current is not None and re.match(r"\s+\w+:", line):
            k, _, v = line.strip().partition(":")
            current[k] = v.strip().strip('"')
        elif line.strip().startswith("- ") or not line.strip():
            current = None
    return out


def test_render_blueprint_is_a_free_docker_web_service_with_a_health_check():
    text = render_yaml()
    for needle in ("type: web", "runtime: docker", "plan: free", "healthCheckPath: /api/health", "dockerfilePath: ./Dockerfile", "region: oregon"):
        assert needle in text, needle
    assert re.search(r'autoDeployTrigger:\s*"?off"?', text), "deploys are manual"
    assert "REPORTLENS_DEMO_MOCK" not in text


def test_render_blueprint_secrets_are_typed_in_the_dashboard_never_stored_in_the_file():
    env = env_blocks(render_yaml())
    for key in ("OPENAI_API_KEY", "ACCESS_CODE"):
        assert env[key] == {"sync": "false"}, f"{key} must be sync: false and carry no value"
    assert env["SESSION_SECRET"] == {"generateValue": "true"}
    assert bundle.audit_render_yaml(render_yaml()) == []
    assert not any(rx.search(render_yaml().encode()) for rx in bundle.SECRET_PATTERNS.values())


def test_render_blueprint_sets_the_safe_public_and_small_host_defaults():
    env = {k: v.get("value") for k, v in env_blocks(render_yaml()).items()}
    assert env["PUBLIC_MODE"] == "1" and env["TRUST_PROXY"] == "1" and env["ALLOWED_HOSTS"] == ".onrender.com"
    assert env["LOW_MEMORY"] == "1" and env["REPORTLENS_DATA_DIR"] == "/tmp/reportlens" and env["MALLOC_ARENA_MAX"] == "2"
    assert float(env["BUDGET_USD_TOTAL"]) > 0 and int(env["MAX_SESSIONS"]) > 0 and int(env["QUESTIONS_PER_HOUR_PER_IP"]) > 0
    assert "PORT" not in env, "Render injects PORT; the app follows it"


def test_the_render_audit_catches_a_secret_value_in_the_blueprint():
    bad = render_yaml().replace("- key: ACCESS_CODE", "- key: ACCESS_CODE\n        value: hunter2hunter2")
    assert any("ACCESS_CODE must be" in p for p in bundle.audit_render_yaml(bad))
    assert any("does not declare OPENAI_API_KEY" in p for p in bundle.audit_render_yaml(render_yaml().replace("OPENAI_API_KEY", "OTHER")))


# ----------------------------------------------------------------------------------------------- the bundle
@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("bundle") / "space"
    bundle.build(out, "hf")
    return out


@pytest.fixture(scope="module")
def built_render(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("bundle") / "render"
    bundle.build(out)                                     # render is the default target
    return out


def test_the_render_bundle_holds_exactly_the_managed_items(built_render):
    assert {p.name for p in built_render.iterdir()} == {"Dockerfile", ".dockerignore", ".gitignore", "requirements-deploy.txt", "render.yaml", "README.md",
                                                        "reportlens", "devtools", "samples", "demo", "LICENSE", "THIRD_PARTY_NOTICES.md",
                                                        "SECURITY.md"}
    assert bundle.audit(built_render) == []
    assert "Copyright (c) 2026 sagarutkarsh1" in (built_render / "LICENSE").read_text(encoding="utf-8")
    names = {p.name for p in built_render.rglob("*") if not p.relative_to(built_render).as_posix().startswith("demo/files/pageindex")}
    assert not names & {".env", "research", "data", "tests", ".venv", "__pycache__", "docs"} and not [n for n in names if n.endswith((".pyc", ".lock"))]
    assert (built_render / "reportlens" / "indexing_worker.py").is_file() and (built_render / "reportlens" / "web" / "static" / "index.html").is_file()
    assert b"\r\n" not in (built_render / "render.yaml").read_bytes() and b"\r\n" not in (built_render / "Dockerfile").read_bytes()


def test_the_render_readme_is_neutral_and_honest(built_render):
    text = (built_render / "README.md").read_text(encoding="utf-8")
    assert not text.startswith("---") and "hf.space" not in text and "Hugging Face" not in text
    low = text.lower()
    assert "sent to the model provider" in low and "temporary" in low and "do not upload confidential" in low and "how to use" in low and "several minutes" in low
    assert ".env" in (built_render / ".gitignore").read_text(encoding="utf-8")


def test_the_hf_bundle_keeps_its_old_shape(built):
    assert {p.name for p in built.iterdir()} == {"Dockerfile", ".dockerignore", "requirements-deploy.txt", "README.md", "reportlens", "devtools", "samples",
                                                 "demo", "LICENSE", "THIRD_PARTY_NOTICES.md", "SECURITY.md"}
    assert bundle.audit(built, "hf") == []
    assert (built / "samples" / "sample_annual_report.pdf").is_file() and (built / "devtools" / "mock_openai.py").is_file()


def test_the_space_readme_has_the_front_matter_and_the_honest_disclosure(built):
    text = (built / "README.md").read_text(encoding="utf-8")
    head, _, body = text.removeprefix("---\n").partition("\n---\n")
    meta = dict(line.split(": ", 1) for line in head.splitlines())
    assert meta["title"] == "Annual Report Lens" and meta["sdk"] == "docker" and meta["app_port"] == "7860" and meta["pinned"] == "false"
    assert meta["emoji"] and meta["colorFrom"] and meta["colorTo"] and 0 < len(meta["short_description"]) <= 60
    low = body.lower()
    assert "sent to the model provider" in low and "temporary" in low and "do not upload confidential" in low and "how to use" in low


def test_a_bundle_of_the_wrong_flavour_is_flagged(built, built_render):
    assert any("unexpected top-level" in p for p in bundle.audit(built_render, "hf"))
    assert any("unexpected top-level" in p or "front matter" in p for p in bundle.audit(built, "render"))


def test_the_audit_rejects_keys_tokens_and_forbidden_items(tmp_path):
    work = tmp_path / "copy"
    bundle.build(work)
    assert bundle.audit(work) == []
    (work / "reportlens" / "leak.txt").write_text(f"OPENAI_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    (work / ".env").write_text("X=1\n", encoding="utf-8")
    (work / "data").mkdir()
    (work / "token.txt").write_text("hf_" + "a" * 34 + " ghp_" + "b" * 36 + " rnd_" + "c" * 24, encoding="utf-8")
    joined = "\n".join(bundle.audit(work))
    assert "OpenAI-style key found in reportlens/leak.txt" in joined and "key assignment found in reportlens/leak.txt" in joined
    assert "forbidden item: .env" in joined and "forbidden item: data" in joined and "Hugging Face token found in token.txt" in joined
    assert "GitHub token found in token.txt" in joined and "Render API key found in token.txt" in joined
    assert "unexpected top-level items" in joined
    assert FAKE_KEY not in joined and "a" * 34 not in joined                       # the audit never echoes a secret


def test_a_secret_pasted_into_the_bundled_blueprint_rejects_the_bundle(tmp_path):
    work = tmp_path / "bp"
    bundle.build(work)
    blueprint = work / "render.yaml"
    blueprint.write_text(blueprint.read_text(encoding="utf-8").replace("- key: OPENAI_API_KEY", "- key: OPENAI_API_KEY\n        value: " + FAKE_KEY), encoding="utf-8")
    joined = "\n".join(bundle.audit(work))
    assert "OpenAI-style key found in render.yaml" in joined and "OPENAI_API_KEY must be" in joined and FAKE_KEY not in joined


def test_shipped_placeholders_and_lookalikes_do_not_trip_the_audit(tmp_path):
    work = tmp_path / "ok"
    bundle.build(work)
    (work / "reportlens" / "notes.txt").write_text('a task-list, "sk-demo-mock-not-a-real-key", sk-not-needed, sk-..., sk-1\n', encoding="utf-8")
    assert bundle.audit(work) == []


def test_rebuilding_refreshes_managed_items_keeps_git_and_refuses_strangers(tmp_path):
    work = tmp_path / "space"
    bundle.build(work)
    (work / ".git").mkdir()
    (work / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (work / "reportlens" / "stale.py").write_text("old", encoding="utf-8")
    bundle.build(work)
    assert (work / ".git" / "HEAD").is_file() and not (work / "reportlens" / "stale.py").exists() and bundle.audit(work) == []
    (work / "my-notes.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(SystemExit, match="already contains other items"):
        bundle.build(work)
    assert (work / "my-notes.txt").read_text(encoding="utf-8") == "keep me"                  # nothing of the user's is deleted


def test_switching_target_in_the_same_folder_removes_the_other_flavours_files(tmp_path):
    work = tmp_path / "x"
    bundle.build(work)
    bundle.build(work, "hf")
    assert not (work / "render.yaml").exists() and not (work / ".gitignore").exists() and bundle.audit(work, "hf") == []


def test_main_prints_the_render_steps_by_default(tmp_path, capsys):
    out = tmp_path / "render"
    assert bundle.main(["--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "Bundle ready (render)" in printed and "MB" in printed
    for needle in ("PRIVATE repository", "git push -u origin main", "New -> Blueprint", "OPENAI_API_KEY", "ACCESS_CODE", ".onrender.com", "separate messages"):
        assert needle in printed, needle
    assert "huggingface" not in printed


def test_main_reports_size_and_next_commands_for_hf(tmp_path, capsys):
    out = tmp_path / "space"
    assert bundle.main(["--target", "hf", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "Bundle ready (hf)" in printed and "MB" in printed and "git push space main" in printed and "WRITE token" in printed


def test_an_unknown_target_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="unknown target"):
        bundle.build(tmp_path / "x", "fly")


def test_main_refuses_an_output_folder_inside_the_project():
    with pytest.raises(SystemExit, match="outside the project"):
        bundle.main(["--out", str(ROOT / "bundle-inside-project")])
    assert not (ROOT / "bundle-inside-project").exists()


# ----------------------------------------------------------------------------------------------- the packaged static demo chat
@pytest.mark.parametrize("fixture_name", ["built_render", "built"])
def test_both_bundles_carry_the_packaged_demo_chat(request, fixture_name):
    out = request.getfixturevalue(fixture_name)
    chat = out / "reportlens" / "demo_data" / "chat.json"
    assert chat.is_file() and chat.stat().st_size > 1000
    assert chat.read_bytes() == (ROOT / "reportlens" / "demo_data" / "chat.json").read_bytes()
    assert not list((out / "reportlens" / "demo_data").glob("*.pdf")), "the static demo holds no third-party document"


def test_the_demo_chat_is_shipped_by_the_package_and_not_ignored_by_git():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "demo_data/*.json" in project["tool"]["setuptools"]["package-data"]["reportlens"]
    ignored = [ln.strip() for ln in lines(".gitignore") if ln.strip() and not ln.startswith("#")]
    assert not any("demo_data" in ln or ln in ("*.json", "reportlens/") for ln in ignored)
    dockerignore = {ln.strip() for ln in lines(".dockerignore") if ln.strip() and not ln.startswith("#")}
    assert not any("demo_data" in ln for ln in dockerignore)
    assert "COPY reportlens ./reportlens" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
