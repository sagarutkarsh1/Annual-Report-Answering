# The demo chat

A deployment can show one real chat to everyone, read-only and without an access code, so visitors see what the app does
before they ask for a code. This folder holds it; only this README is in git.

Make one from any chat of yours (nothing is re-computed, so it costs nothing):

```powershell
.venv\Scripts\python.exe scripts\export_demo.py --data-dir data --list
.venv\Scripts\python.exe scripts\export_demo.py --data-dir data --session <id> --out demo `
    --title "Demo: Example plc Annual Report 2025" --display-name "Example plc Annual Report 2025.pdf" `
    --attribution "Example plc Annual Report 2025 (c) Example plc. Shown for demonstration only." `
    --attribution-url https://www.example.com/investors
```

That writes `chat.json` (questions, cited answers, scores and the page texts behind them) and `files/` (the PDF and its
PageIndex store). The app installs it at start-up (`DEMO_DIR`, default this folder; set `DEMO_DIR=` to switch it off).

The PDF belongs to its publisher: that is why `chat.json` and `files/` are git-ignored and travel only in the private deploy
bundle (`scripts/make_deploy_bundle.ps1`), never in the public source repository.
