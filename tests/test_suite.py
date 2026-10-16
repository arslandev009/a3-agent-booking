"""The programmatic suite itself: its model-free tests must pass, the harness must not crash with a useless model,
and the main flow's checks must be satisfiable by a well-behaved model (so a FAIL on a real model means something)."""
import json
import re
from types import SimpleNamespace as NS

import pytest

from clinicagent import suite
from clinicagent.agent import ClinicAgent


class ToolBot:
    """Tiny rule-based stand-in for a well-behaved model (not a test of any real model)."""
    provider, model = "fake", "toolbot"

    def __init__(self):
        self.n = 0

    def _resp(self, content=None, calls=None):
        msg = NS(content=content, tool_calls=calls)
        return NS(text=content or "", raw=NS(choices=[NS(message=msg)]))

    def _call(self, name, args):
        self.n += 1
        return NS(id=f"c{self.n}", function=NS(name=name, arguments=json.dumps(args)))

    def chat(self, messages, **kw):
        last = messages[-1]
        users = [m for m in messages if m["role"] == "user" and not m["content"].startswith("Automated")]
        text = users[-1]["content"]
        if last["role"] == "tool":
            res = json.loads(last["content"])
            if "slots" in res:
                return self._resp("Available times on Thursday 15 October: " + ", ".join(res["slots"]) + ". Which time would you like?")
            return self._resp("Done, thank you.")
        if re.search(r"cleaning for tomorrow morning", text):
            return self._resp(calls=[self._call("check_availability", {"date": "2026-10-15", "service": "cleaning", "part_of_day": "morning"})])
        if text.strip() == "09:30":
            return self._resp("Please tell me your full name and phone number?")
        m = re.match(r"(\w+ \w+) (\d{11})$", text.strip())
        if m:
            return self._resp(calls=[self._call("book_appointment", {"patient_name": m[1], "phone": m[2], "service": "cleaning",
                                                                      "start": "2026-10-15T09:30", "idempotency_key": "x"})])
        m = re.search(r"appointment (\d+), phone (\d+)", text)
        if m:
            return self._resp(calls=[self._call("cancel_appointment", {"appointment_id": int(m[1]), "phone": m[2]})])
        return self._resp("How can I help you today?")


def _env(llm):
    return suite.Env(llm=llm, judge=False, make_agent=lambda tools: ClinicAgent(llm, tools, retries=0, sleeper=lambda s: None), sleep_s=0)


def test_model_free_sections_pass():
    env = _env(None)
    recs = [suite.run_test(t, env) for t in suite.TESTS if t.kind in ("tool", "api")]
    bad = [(r["id"], r["verdict"], [c["name"] for c in r["checks"] if not c["ok"]], r.get("error")) for r in recs
           if r["verdict"] not in ("PASS", "SKIPPED")]
    assert not bad, bad
    verdicts = {r["id"]: r["verdict"] for r in recs}
    assert verdicts["D13"] == "PASS"  # phone formats +92 / 0 now match


def test_main_flow_checks_are_satisfiable_by_a_well_behaved_model():
    t = next(x for x in suite.TESTS if x.id == "F-MAIN")
    rec = suite.run_test(t, _env(ToolBot()))
    failed = [(c["name"], c["detail"]) for c in rec["checks"] if not c["ok"]]
    assert rec["verdict"] == "PASS", (failed, rec.get("error"), [(x["user"], x["reply"], x["guards"]) for x in rec["turns"]])


def test_harness_survives_a_useless_model_and_reports_failures():
    env = _env(suite._Echo())
    recs = [suite.run_test(t, env) for t in suite.TESTS if t.kind == "agent"]
    assert all(r["verdict"] in ("PASS", "FAIL", "KNOWN_GAP_CONFIRMED", "KNOWN_GAP_FIXED") for r in recs), \
        [(r["id"], r.get("error")) for r in recs if r["verdict"] == "ERROR"]
    assert next(r for r in recs if r["id"] == "F-MAIN")["verdict"] == "FAIL"  # an echo model must not pass the main flow
    md = suite.summary_markdown({"provider": "fake", "model": "echo", "judge": False, "when": "now"}, recs)
    assert "F-MAIN" in md and "| Verdict | Count |" in md


def test_provider_quota_error_is_reported_as_rate_limit_not_failure():
    class Quota(Exception):
        status_code = 429
    boom = suite._Boom(Quota("quota"))
    t = next(x for x in suite.TESTS if x.id == "I1")
    assert suite.run_test(t, _env(boom))["verdict"] == "RATE_LIMIT"


def test_ids_are_unique_and_scenarios_are_english():
    ids = [t.id for t in suite.TESTS]
    assert len(ids) == len(set(ids))
    texts = " ".join(s for t in suite.TESTS for s in t.steps if isinstance(s, str))
    for marker in ("mujhe", "kal subah", "karni", "chahiye", "kya"):
        assert marker not in texts.lower()


def test_select_by_section_and_id():
    assert {t.id for t in suite.select(suite.TESTS, "I1,9")} >= {"I1", "D1", "D14"}


def test_quota_wait_is_parsed_from_the_provider_message():
    assert suite.quota_wait_seconds("Please try again in 4m32.16s. Need more tokens?") == pytest.approx(272.16)
    assert suite.quota_wait_seconds("Please try again in 7m6.816s") == pytest.approx(426.816)
    assert suite.quota_wait_seconds("try again in 1h2m3s") == 3723
    assert suite.quota_wait_seconds("try again in 48s") == 48
    assert suite.quota_wait_seconds("something else") is None


def test_tokens_are_counted_per_test_when_the_provider_reports_usage():
    class Counting(ToolBot):
        def chat(self, messages, **kw):
            res = super().chat(messages, **kw)
            res.raw.usage = NS(total_tokens=100)
            return res
    t = next(x for x in suite.TESTS if x.id == "F-MAIN")
    rec = suite.run_test(t, _env(Counting()))
    assert rec["llm_calls"] > 5 and rec["tokens"] == 100 * rec["llm_calls"]
    assert "Tokens used by this run" in suite.summary_markdown({"provider": "f", "model": "m", "judge": False, "when": "n"}, [rec])


def test_api_test_on_a_busy_provider_is_a_rate_limit_not_a_failure():
    t = next(x for x in suite.TESTS if x.id == "A12")
    env = suite.Env(llm=suite._Boom(type("Q", (Exception,), {"status_code": 429})("quota")), judge=False,
                    make_agent=lambda tools: ClinicAgent(suite._Boom(type("Q", (Exception,), {"status_code": 429})("quota")), tools, retries=0), sleep_s=0)
    assert suite.run_test(t, env)["verdict"] == "RATE_LIMIT"


def test_ascii_quotes_lets_regex_checks_see_plain_apostrophes():
    assert suite.ascii_quotes("I don’t have it for check‑up") == "I don't have it for check-up"
    # the I5 check that failed on a real reply written with a curly apostrophe
    reply = suite.ascii_quotes("I’m sorry, but I don’t have information on braces pricing.")
    assert re.search(r"don't have", reply)


def test_repair_note_keeps_the_agent_on_the_patients_real_request():
    from clinicagent.agent import REPAIR_NOTES
    assert "did not ask to book" in REPAIR_NOTES["false_booking_claim"]


def test_claim_judge_prompt_names_the_question_that_mentions_a_booking():
    from clinicagent.judge import CLAIM_PROMPT
    assert "phone number you used when booking" in CLAIM_PROMPT
