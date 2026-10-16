"""Print readable transcripts from a results JSON (default: only the FAILED scenarios).

    python scripts\\show.py results\\groq_openai-gpt-oss-120b_20261009-144700.json
    python scripts\\show.py results\\<file>.json O01 D04      # specific scenario ids
    python scripts\\show.py results\\<file>.json --all
    python scripts\\show.py results\\<file>.json --guards  # also show PASSED scenarios where a guard fired
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clinicagent.evals import SET_PATHS, load_scenarios

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--guards", action="store_true", help="show failed scenarios AND passed ones where a guard fired")
    a = ap.parse_args()

    data = json.loads(Path(a.file).read_text(encoding="utf-8"))
    turns = {s["id"]: s["turns"] for s in load_scenarios(SET_PATHS[data["meta"].get("set", "dev")])}
    print(f"{data['meta']['provider']}/{data['meta']['model']}  summary: {data['summary']['passed']}/{data['summary']['total']}\n")
    for r in data["scenarios"]:
        if a.ids and r["id"] not in a.ids:
            continue
        if not a.ids and not a.all and r["passed"] and not (a.guards and r["guards"]):
            continue
        print("=" * 78)
        print(f"{r['id']} ({r['category']}) {'PASS' if r['passed'] else 'FAIL'}")
        for f in r["failures"]:
            print(f"  FAILURE: {f}")
        for i, user in enumerate(turns[r["id"]]):
            print(f"\n  patient: {user}")
            for c in (c for c in r["tool_calls"] if c["turn"] == i):
                print(f"    [tool] {c['name']}({c['args']}) -> {c['result']}")
            if i < len(r["replies"]):
                print(f"  bot:     {r['replies'][i]}")
        if r["guards"]:
            print(f"\n  guards fired: {r['guards']}")
        for x in r.get("rejected_replies", []):
            print(f"  discarded by a guard: {x!r}")
        print()


if __name__ == "__main__":
    main()
