"""Turn one of your chats into the app's read-only demo (the chat visitors can open without an access code).

    .venv\\Scripts\\python.exe scripts\\export_demo.py --data-dir data --list
    .venv\\Scripts\\python.exe scripts\\export_demo.py --data-dir data --session <id> --out demo ^
        --title "Demo: Example plc Annual Report 2025" --attribution "Example plc Annual Report 2025 (c) Example plc" ^
        --attribution-url https://www.example.com/investors

Only finished answers are copied, with their citations and scores.  By default the demo is static (chat.json only: no PDF;
a citation opens a card with the page, section and quote), small enough to ship with the code in reportlens/demo_data/.
--with-files makes the full demo: it also copies the PDF, its PageIndex store and the page texts behind each answer, so
citations open the highlighted page.  The app installs the demo from DEMO_DIR at start-up (unset: demo/ when it holds one,
else the packaged static demo).  Nothing is sent anywhere and nothing is re-computed, so exporting costs nothing.

A full demo holds the source document, so mind its copyright: demo/ is in .gitignore and travels only in the private
deploy bundle (scripts/make_deploy_bundle.py), never in the public source repository.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reportlens.config import load_settings  # noqa: E402
from reportlens.demo import export_session  # noqa: E402
from reportlens.store import Store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", type=Path, required=True, help="the data folder that holds reportlens.db and sessions/")
    p.add_argument("--list", action="store_true", help="list the chats in that folder and stop")
    p.add_argument("--session", help="id of the chat to export")
    p.add_argument("--out", type=Path, default=ROOT / "demo", help="demo folder to write (default: demo/)")
    p.add_argument("--title", help="title shown for the demo chat (default: the chat's own title)")
    p.add_argument("--attribution", help="one line naming the document's source and owner")
    p.add_argument("--attribution-url", help="where the document is published")
    p.add_argument("--display-name", help="file name shown for the document (default: the uploaded name)")
    p.add_argument("--with-files", action="store_true",
                   help="the full demo: also copy the PDF and its index, so citations open the highlighted page (keep it private)")
    args = p.parse_args(argv)

    data_dir = args.data_dir.resolve()
    if not (data_dir / "reportlens.db").is_file():
        p.error(f"{data_dir} has no reportlens.db")
    settings = load_settings(environ={}).with_(data_dir=data_dir)
    store = Store(settings.db_path)
    try:
        if args.list or not args.session:
            for s in store.list_sessions():
                doc = s.document
                print(f"{s.id}  {s.message_count:3d} messages  {doc.filename if doc else '-':40.40s}  {s.title}")
            return 0 if args.list else 2
        out = export_session(store, settings, args.session, args.out, attribution=args.attribution,
                             attribution_url=args.attribution_url, title=args.title, display_name=args.display_name,
                             with_files=args.with_files)
    finally:
        store.close()
    print(f"Demo written to {out}. Restart the app (or redeploy) to see it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
