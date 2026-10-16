import json

from clinicagent.evals import ScenarioResult, results_to_json
from clinicagent.report import build_report


def write(tmp_path, name, provider, sset, run, rows):
    res = [ScenarioResult(id=i, category=c, passed=p, failures=[] if p else ["check x: bad"], guards=g,
                          latency_s=2.0, error=err) for i, c, p, g, err in rows]
    meta = {"schema": 2, "provider": provider, "model": "m1", "set": sset, "run": run}
    f = tmp_path / name
    f.write_text(results_to_json(res, meta), encoding="utf-8")
    return f


def test_empty_report_is_valid():
    assert "No result files" in build_report([])


def test_report_counts_errors_separately_and_lists_flaky(tmp_path):
    f1 = write(tmp_path, "a.json", "groq", "heldout", 1,
               [("X01", "happy", True, [], None), ("X02", "happy", False, ["time_not_confirmed"], None),
                ("X03", "happy", False, [], "RateLimitError: 429")])
    f2 = write(tmp_path, "b.json", "groq", "heldout", 2,
               [("X01", "happy", True, [], None), ("X02", "happy", True, [], None), ("X03", "happy", True, [], None)])
    text = build_report([f1, f2])
    assert "| groq / m1 | heldout v1 (burned) | 2 | 5 | 4/5 (80%) | 1 |" in text      # 429 excluded from the denominator
    assert "| groq | heldout v1 (burned) | X02 | happy | 1/2 |" in text              # flaky scenario listed
    assert "X03" not in text.split("## Scenarios that did not always pass")[1].split("## Code-level")[0]
    assert "| groq | heldout v1 (burned) | time_not_confirmed | 1 |" in text
    assert "Honest limitations" in text


def test_old_schema_files_are_ignored(tmp_path):
    f = tmp_path / "old.json"
    f.write_text(json.dumps({"meta": {"provider": "groq", "model": "m"}, "summary": {}, "scenarios": []}))
    assert "No result files" in build_report([f])


def test_all_pass_report(tmp_path):
    f = write(tmp_path, "c.json", "gemini", "dev", 1, [("H01", "happy", True, [], None)])
    text = build_report([f])
    assert "every non-errored scenario passed" in text and "none fired" in text


def test_judge_runs_are_reported_separately_from_regex_only_runs(tmp_path):
    f1 = write(tmp_path, "a.json", "groq", "intent", 1, [("N01", "intent_cancel", False, [], None)])
    f2 = write(tmp_path, "b.json", "groq", "intent", 1, [("N01", "intent_cancel", True, [], None)])
    import json as _j
    d = _j.loads(f2.read_text()); d["meta"]["judge"] = True; f2.write_text(_j.dumps(d))
    text = build_report([f1, f2])
    assert "| groq / m1 | intent suite (targeted) | 1 | 1 | 0/1 (0%)" in text
    assert "| groq / m1 + LLM judge | intent suite (targeted) | 1 | 1 | 1/1 (100%)" in text
