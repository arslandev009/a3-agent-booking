"""Build docs/RESULTS.md from results/*.json (only files written by run_eval.py schema 2)."""
from __future__ import annotations

import sys
from pathlib import Path

from clinicagent.report import build_report

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    files = sorted((ROOT / "results").glob("*.json"))
    text = build_report(files)
    out = ROOT / "docs" / "RESULTS.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}  ({len(files)} result files scanned)")


if __name__ == "__main__":
    sys.exit(main())
