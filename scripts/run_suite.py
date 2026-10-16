"""Run the programmatic test suite (docs/MANUAL_TEST_PLAN.md) and save one result file per test.

    python scripts\\run_suite.py --provider groq
    python scripts\\run_suite.py --provider groq --only 2,C1,T10          # sections (1-6, 8, 9) or test ids
    python scripts\\run_suite.py --no-model                                # only the tests that need no model (API + tool layer)
    python scripts\\run_suite.py --provider groq --resume results\\suite_groq_...   # skip tests already saved, retry the rest
    python scripts\\run_suite.py --provider groq --wait-quota 10      # wait up to 10 min when the provider's quota resets soon
    python scripts\\run_suite.py --provider groq --fallback gemini   # when groq's quota is used up, run the rest on gemini
    python scripts\\run_suite.py --list

Output folder: results\\suite_<provider>_<model>_<time>\\  with  <TEST>.json (full transcript, tools, guards, checks),
summary.json and SUMMARY.md.  Paste SUMMARY.md back for analysis (or zip the folder).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from clinicagent import suite
from clinicagent.agent import ClinicAgent

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None)
    ap.add_argument("--only", default="", help="comma-separated test ids or section numbers")
    ap.add_argument("--judge", choices=["on", "off"], default="on")
    ap.add_argument("--sleep", type=float, default=1.5, help="seconds between turns (free tiers rate-limit)")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--wait-quota", type=int, default=0, metavar="MINUTES",
                    help="when the provider says 'try again in N minutes' and N <= MINUTES, wait and retry that test once")
    ap.add_argument("--fallback", default=None, metavar="PROVIDER",
                    help="when --provider hits its quota, rerun that test and the remaining ones on this provider (each result file records which provider ran it)")
    ap.add_argument("--no-model", action="store_true", help="run only tests that need no model")
    ap.add_argument("--resume", default="", help="existing results folder: keep finished tests, rerun ERROR/RATE_LIMIT/missing ones")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    tests = suite.select(suite.TESTS, a.only)
    if a.list:
        for t in tests:
            print(f"{t.id:<7} {t.kind:<6} section {t.section}  {t.title}")
        print(f"\n{len(tests)} tests")
        return

    judge = a.judge == "on"

    def build_env(provider_name):
        if a.no_model:
            return suite.Env(llm=None, judge=judge, make_agent=lambda tools: (_ for _ in ()).throw(RuntimeError("SKIP: no model configured")), sleep_s=a.sleep)
        from llm_client.client import LLM
        from clinicagent.judge import Judge
        llm = LLM(provider_name)

        def make_agent(tools):
            return ClinicAgent(llm, tools, judge=Judge(llm) if judge else None, retries=a.retries)

        return suite.Env(llm=llm, judge=judge, make_agent=make_agent, sleep_s=a.sleep)

    env = build_env(a.provider)
    fallback_env = build_env(a.fallback) if (a.fallback and not a.no_model) else None
    llm = env.llm
    provider, model = (llm.provider, llm.model) if llm else ("none", "none")
    if a.resume:
        out = Path(a.resume)
        if not out.is_absolute():
            out = ROOT / out
    else:
        out = ROOT / "results" / f"suite_{provider}_{re.sub(r'[^A-Za-z0-9._-]+', '-', model)}_{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)

    recs = []
    on_fallback = False
    for t in tests:
        f = out / f"{t.id}.json"
        if a.resume and f.exists():
            old = json.loads(f.read_text(encoding="utf-8"))
            if old.get("verdict") not in ("ERROR", "RATE_LIMIT", "SKIPPED"):
                recs.append(old)
                print(f"keep  {t.id:<7} {old['verdict']}")
                continue
        cur = fallback_env if on_fallback else env
        rec = suite.run_test(t, cur)
        if rec["verdict"] == "RATE_LIMIT" and fallback_env is not None and not on_fallback:
            on_fallback = True
            print(f"      {env.llm.provider} quota reached at {t.id}: switching to {fallback_env.llm.provider} for this test and the rest of the run")
            cur = fallback_env
            rec = suite.run_test(t, cur)
        elif rec["verdict"] == "RATE_LIMIT" and a.wait_quota:
            wait = suite.quota_wait_seconds(rec.get("error"))
            if wait is not None and wait <= a.wait_quota * 60:
                print(f"      quota reached at {t.id}: waiting {wait / 60:.1f} min, then retrying once")
                time.sleep(wait + 5)
                rec = suite.run_test(t, cur)
        if cur.llm is not None:
            rec["provider"], rec["model"] = cur.llm.provider, cur.llm.model
        f.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        recs.append(rec)
        bad = [c["name"] for c in rec["checks"] if not c["ok"]]
        tag = f" [{rec['provider']}]" if rec.get("provider") and rec["provider"] != provider else ""
        print(f"{rec['verdict']:<20} {t.id:<7} {t.title[:52]:<52} {rec['seconds']:>5}s{tag}" + (f"\n        - " + "\n        - ".join(bad) if bad else "")
              + (f"\n        ! {rec['error']}" if rec.get("error") else ""))
        sys.stdout.flush()

    meta = {"provider": provider, "model": model, "judge": judge, "fallback": (f"{fallback_env.llm.provider}/{fallback_env.llm.model}" if fallback_env else None), "when": datetime.now().isoformat(timespec="seconds"), "now_fixed": str(suite.NOW)}
    (out / "summary.json").write_text(json.dumps({"meta": meta, "results": [{k: r.get(k) for k in ("id", "section", "title", "verdict", "seconds", "provider", "model")} for r in recs]}, indent=2), encoding="utf-8")
    md = suite.summary_markdown(meta, recs)
    (out / "SUMMARY.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"saved in: {out}")
    if any(r["verdict"] == "RATE_LIMIT" for r in recs):
        print(f"\nSome tests hit the provider rate limit. Re-run only those:\n  python scripts\\run_suite.py --provider {provider} --resume \"{out}\"")


if __name__ == "__main__":
    main()
