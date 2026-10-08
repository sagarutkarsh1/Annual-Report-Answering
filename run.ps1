# ReportLens launcher for Windows PowerShell.
#
#   .\run.ps1                       start the server (needs OPENAI_API_KEY in .env)
#   .\run.ps1 -Demo                 offline demo: fake OpenAI server, no key, no cost
#   .\run.ps1 -Demo --port 9000 --open
#   .\run.ps1 -Test                 run the offline test-suite (pytest -q)
#   .\run.ps1 -Test tests\test_web.py -k upload      anything after -Test goes to pytest
#
# Every other argument is passed to `python -m reportlens` (see `.\run.ps1 --help`).
# It always uses the project's own virtual environment: do not activate another one first.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Error ("Virtual environment not found at $python. " +
        "Create it at the project root (a short path matters on Windows): python -m venv .venv ; " +
        ".\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt")
    exit 1
}

# UTF-8 everywhere: the report text contains pound signs, curly quotes and en dashes that the legacy console codepage cannot print.
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:RAGAS_DO_NOT_TRACK = "true"   # exactly "true"; stops RAGAS phoning home from inside the event loop

# `$args` (not param()) so that unknown GNU-style flags such as --port reach Python untouched.
$demo = $false
$test = $false
$rest = @()
foreach ($arg in $args) {
    switch -CaseSensitive -Regex ($arg) {
        '^-(?i:demo)$' { $demo = $true; continue }
        '^-(?i:test)$' { $test = $true; continue }
        default        { $rest += $arg }
    }
}

Set-Location -LiteralPath $root
if ($test) {
    & $python -m pytest -q @rest
} else {
    $serverArgs = @("-m", "reportlens")
    if ($demo) { $serverArgs += "--demo" }
    & $python @serverArgs @rest
}
exit $LASTEXITCODE
