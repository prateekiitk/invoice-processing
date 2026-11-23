"""Process one or more invoice PDFs from the command line and print the run stage by stage.

    python scripts/process_invoice.py samples/01_happy_path_apex_office.pdf
    python scripts/process_invoice.py samples/*.pdf --reset

Uses the same pipeline, database and ledger as the web app (runs appear on the dashboard).
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

os.environ.setdefault("STAGE_DELAY_MS", "0")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app import db  # noqa: E402
from app.pipeline import start_run  # noqa: E402

C = {"pass": "\033[32m", "fail": "\033[31m", "warn": "\033[33m", "info": "\033[90m", "skip": "\033[90m", "end": "\033[0m",
     "APPROVED": "\033[42;30m", "NEEDS_REVIEW": "\033[43;30m", "ON_HOLD": "\033[45;30m", "REJECTED": "\033[41;30m"}
SYM = {"pass": "✓", "fail": "✗", "warn": "!", "info": "·", "skip": "–"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--reset", action="store_true", help="reset runs + ledger to the seeded state first")
    args = ap.parse_args()
    if args.reset:
        db.reset_all()
    files = [f for p in args.pdfs for f in (glob.glob(p) or [p])]
    for f in files:
        data = Path(f).read_bytes()
        rid = start_run(Path(f).name, data, source="cli", background=False)
        r = db.get_run(rid)
        print(f"\n{'=' * 100}\n{Path(f).name}   (run {rid}, {r['duration_ms']} ms, extractor: {r['extractor'] or '-'})")
        for ev in r["events"]:
            if ev["type"] != "stage_completed":
                continue
            st = ev["status"]
            print(f"  {C[st]}{SYM[st]} {ev['stage']:<10}{C['end']} {ev['summary']}")
            for ch in ev["checks"]:
                if ch["status"] in ("fail", "warn"):
                    eff = f" [{ch['effect'].upper()}]" if ch["effect"] else ""
                    print(f"      {C[ch['status']]}{SYM[ch['status']]}{eff} {ch['title']}: {ch['detail']}{C['end']}")
        d = r["result"]["decision"]
        print(f"\n  {C[d['outcome']]} {d['outcome']} {C['end']}  {d['headline']}")
        for a in d["next_actions"]:
            print(f"    → {a}")


if __name__ == "__main__":
    main()
