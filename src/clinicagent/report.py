"""Aggregate eval result files into a markdown report (pure functions, unit-tested)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

LIMITATIONS = """\
## Honest limitations
- **Only `heldout3` is a clean test, and only until it is tuned against.** The `dev` set (30), `heldout` (v1, 15) and
  `heldout2` (v2, 15) were used to find and fix problems, so treat them as regression suites, not as expected
  real-world accuracy. If the agent is changed because of a `heldout3` failure, write a v4 set before quoting a score again.
- **Held-out v1 and v2 are burned.** Their failures were studied and the agent was changed because of them
  (v2 led to guard G12 and the grounded fallback reply), so their numbers are regression numbers.
- **Held-out set is small (15 scenarios).** One scenario is 6.7 percentage points. Differences of 1-2 scenarios between
  providers are noise. Results are only meaningful as repeated runs (see the pass columns).
- **Meaning is judged by an LLM, exact facts by code.** An optional LLM judge decides "did the patient ask to cancel?"
  and "does this reply claim something was done?" (regexes are only the fallback). The judge can be wrong too, adds one
  small model call on some turns, and shares the main model's rate limit. Rows labelled "+ LLM judge" use it; older rows
  are regex-only. The `intent` suite was written to probe a known weakness, so it shows the judge's effect, not
  unbiased accuracy.
- **Behaviour checks are regex heuristics** (medical advice, refund promises, leaks, alternatives offered). They can
  miss a bad answer and can flag a fine one. Transcripts are in `results/` (read them with `scripts/show.py`).
- **LLM output varies between runs** even at temperature 0. A scenario that passes 3/3 is stronger evidence than 1/1.
- **Free-tier models and sample data only.** Rate-limit errors (HTTP 429) are excluded from pass rates and counted
  separately. No real patient data was used. The calendar is a SQLite mock with one chair and fixed hours.
- **Safety rules live in code** (guards G1-G12, tool-layer validation), so a weak model cannot execute a cancellation
  without staff approval or double-book; it can only give worse conversations. That is by design, and it is why
  the safety rows below are more trustworthy than the conversation-quality rows.
"""


SET_LABELS = {"heldout": "heldout v1 (burned)", "heldout2": "heldout v2 (burned)", "heldout3": "heldout v3 (fresh)", "dev": "dev (regression)",
              "intent": "intent suite (targeted)"}


def _label(sset: str) -> str:
    return SET_LABELS.get(sset, sset)


def _load(files: list[Path]) -> dict[tuple[str, str, str], list[dict]]:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for f in files:
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta = d.get("meta", {})
        if meta.get("schema") != 2:
            continue
        model = meta["model"] + (" + LLM judge" if meta.get("judge") else "")  # old files: regex-only
        groups[(meta["provider"], model, meta["set"])].append(d)
    return groups


def _is_error(sc: dict) -> bool:
    return bool(sc.get("error"))


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def build_report(files: list[Path]) -> str:
    groups = _load(files)
    out = [f"# A3 Clinic Appointment Agent: eval results\n",
           f"_Generated {date.today().isoformat()} by `scripts/make_report.py` from `results/*.json`. "
           "Do not edit by hand; rerun the script._\n"]
    if not groups:
        out.append("No result files with schema 2 found. Run `scripts/run_eval.py` first.\n")
        out.append(LIMITATIONS)
        return "\n".join(out)

    # ---- summary
    out.append("## Summary\n")
    out.append("| Provider / model | Set | Runs | Scenario-runs | Pass rate | Errored (429 etc.) | Avg latency / scenario |")
    out.append("|---|---|---|---|---|---|---|")
    for (prov, model, sset), runs in sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][0])):
        total = passed = errored = 0
        lat = []
        for d in runs:
            for sc in d["scenarios"]:
                if _is_error(sc):
                    errored += 1
                    continue
                total += 1
                passed += int(sc["passed"])
                lat.append(sc["latency_s"])
        avg = f"{sum(lat) / len(lat):.1f}s" if lat else "n/a"
        out.append(f"| {prov} / {model} | {_label(sset)} | {len(runs)} | {total} | {passed}/{total} ({_pct(passed, total)}) "
                   f"| {errored} | {avg} |")
    out.append("")

    # ---- per category
    for sset in sorted({k[2] for k in groups}):
        out.append(f"## Pass rate by category: {_label(sset)}\n")
        provs = [k for k in sorted(groups) if k[2] == sset]
        cats = sorted({sc["category"] for k in provs for d in groups[k] for sc in d["scenarios"]})
        out.append("| Category | " + " | ".join(f"{k[0]}" for k in provs) + " |")
        out.append("|---|" + "---|" * len(provs))
        for cat in cats:
            row = []
            for k in provs:
                p = t = 0
                for d in groups[k]:
                    for sc in d["scenarios"]:
                        if sc["category"] == cat and not _is_error(sc):
                            t += 1
                            p += int(sc["passed"])
                row.append(f"{p}/{t}" if t else "n/a")
            out.append(f"| {cat} | " + " | ".join(row) + " |")
        out.append("")

    # ---- unstable / failing scenarios
    out.append("## Scenarios that did not always pass\n")
    any_row = False
    out.append("| Provider | Set | Scenario | Category | Passed | Failure reasons (count) |")
    out.append("|---|---|---|---|---|---|")
    for (prov, model, sset), runs in sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][0])):
        per: dict[str, dict] = {}
        for d in runs:
            for sc in d["scenarios"]:
                if _is_error(sc):
                    continue
                e = per.setdefault(sc["id"], {"cat": sc["category"], "p": 0, "t": 0, "why": Counter()})
                e["t"] += 1
                e["p"] += int(sc["passed"])
                for f in sc["failures"]:
                    e["why"][f[:110]] += 1
        for sid, e in sorted(per.items()):
            if e["p"] < e["t"]:
                any_row = True
                why = "; ".join(f"{m} (x{n})" for m, n in e["why"].most_common(3))
                out.append(f"| {prov} | {_label(sset)} | {sid} | {e['cat']} | {e['p']}/{e['t']} | {why} |")
    if not any_row:
        out.append("| - | - | none | - | - | every non-errored scenario passed in every run |")
    out.append("")

    # ---- guards
    out.append("## Code-level guards that fired\n")
    out.append("A guard firing means the model tried something the code refused (a prevented mistake).\n")
    out.append("| Provider | Set | Guard | Times |")
    out.append("|---|---|---|---|")
    rows = 0
    for (prov, model, sset), runs in sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][0])):
        c: Counter = Counter()
        for d in runs:
            for sc in d["scenarios"]:
                c.update(sc.get("guards", []))
        for g, n in c.most_common():
            out.append(f"| {prov} | {_label(sset)} | {g} | {n} |")
            rows += 1
    if not rows:
        out.append("| - | - | none fired | 0 |")
    out.append("")
    out.append(LIMITATIONS)
    return "\n".join(out)
