"""Run scripted scenarios against a real provider and score them.

    python scripts\\run_eval.py --provider groq                          # dev set (30), 1 run
    python scripts\\run_eval.py --provider groq --set heldout3 --runs 3  # fresh held-out set v3 (15), 3 runs
    python scripts\\run_eval.py --provider gemini --set heldout3 --runs 3 --sleep 6
    python scripts\\run_eval.py --provider groq --only H01,D01           # quick subset
    python scripts\\run_eval.py --provider groq --set intent --judge off   # regex-only guards, to compare with --judge on

One results file per run: results\\<provider>_<model>_<set>_r<N>_<time>.json (full transcripts).
Then:  python scripts\\make_report.py   ->  docs\\RESULTS.md
Free tiers rate-limit: HTTP 429 is recorded as 'run error' and excluded from pass rates in the report.
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

from llm_client.client import LLM

from clinicagent.agent import ClinicAgent
from clinicagent.evals import SET_PATHS, load_scenarios, results_to_json, run_scenario, summarize
from clinicagent.judge import Judge

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None)
    ap.add_argument("--set", choices=list(SET_PATHS), default="dev")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", default="", help="comma-separated scenario ids")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds to wait between turns")
    ap.add_argument("--judge", choices=["on", "off"], default="on",
                    help="on: LLM judge decides intent/claims (default); off: regex-only guards, for comparison")
    ap.add_argument("--retries", type=int, default=2, help="LLM retries on 429/5xx per call")
    a = ap.parse_args()

    only = {x.strip() for x in a.only.split(",") if x.strip()} or None
    scenarios = load_scenarios(SET_PATHS[a.set], only=only)
    llm = LLM(a.provider)
    out_dir = ROOT / "results"
    out_dir.mkdir(exist_ok=True)
    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "-", llm.model)
    pass_counts: dict[str, int] = {}

    def make_agent(llm_, tools_):
        return ClinicAgent(llm_, tools_, judge=Judge(llm_) if a.judge == "on" else None, retries=a.retries)

    for run in range(1, a.runs + 1):
        print(f"\n##### {llm.provider}/{llm.model}  set={a.set}  judge={a.judge}  run {run}/{a.runs}  ({len(scenarios)} scenarios)\n")
        results = []
        for sc in scenarios:
            r = run_scenario(sc, llm, sleep_s=a.sleep, make_agent=make_agent)
            results.append(r)
            pass_counts[r.id] = pass_counts.get(r.id, 0) + int(r.passed)
            print(f"{'PASS' if r.passed else 'FAIL'}  {r.id}  {r.category:<15} {r.latency_s:>5.1f}s")
            for f in r.failures:
                print(f"        - {f}")
        s = summarize(results)
        print(f"\n=== run {run}: {s['passed']}/{s['total']} passed | guards: {s['guards_fired'] or 'none'} "
              f"| avg {s['avg_latency_s']}s ===")
        path = out_dir / f"{llm.provider}_{safe_model}_{a.set}{"_judge" if a.judge == "on" else ""}_r{run}_{datetime.now():%Y%m%d-%H%M%S}.json"
        meta = {"schema": 2, "provider": llm.provider, "model": llm.model, "set": a.set, "run": run,
                "when": datetime.now().isoformat(), "judge": a.judge == "on"}
        path.write_text(results_to_json(results, meta), encoding="utf-8")
        print(f"saved: {path}")

    if a.runs > 1:
        print(f"\n===== passes out of {a.runs} runs, per scenario =====")
        for sid, n in pass_counts.items():
            print(f"  {sid}: {n}/{a.runs}" + ("" if n == a.runs else "   <-- not stable"))
    print("\nNext: python scripts\\make_report.py")


if __name__ == "__main__":
    main()
