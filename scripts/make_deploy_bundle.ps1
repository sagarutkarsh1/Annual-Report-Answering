<#
.SYNOPSIS
  Builds the clean folder that becomes your deployment repository (Render by default, or a Hugging Face Space).

.DESCRIPTION
  Copies exactly what the container needs (Dockerfile, .dockerignore, requirements-deploy.txt, render.yaml, a neutral README.md,
  reportlens/, devtools/, samples/ and a .gitignore; with -Target hf: a Space README.md with the Hugging Face front matter and no
  render.yaml) and nothing else, then audits the result: it fails if it finds .env, research/, data/, tests/, anything that looks
  like an API key or token, or a secret value in render.yaml. Prints the folder size and the next steps.
  Re-running it refreshes the managed items and keeps a .git folder, so the bundle folder can be your git checkout.

.EXAMPLE
  .\scripts\make_deploy_bundle.ps1
  .\scripts\make_deploy_bundle.ps1 -Out D:\my-bundle
  .\scripts\make_deploy_bundle.ps1 -Target hf -Out D:\my-space
#>
param(
    [string]$Out = "C:\rl_deploy_bundle",
    [ValidateSet("render", "hf")]
    [string]$Target = "render"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Virtual environment not found at $python (see README: Quick start)."
    exit 1
}
$env:PYTHONIOENCODING = "utf-8"
& $python (Join-Path $PSScriptRoot "make_deploy_bundle.py") --target $Target --out $Out
exit $LASTEXITCODE
