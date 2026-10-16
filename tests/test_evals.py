"""Tests for the eval harness itself, using a scripted fake LLM (no network)."""
import json
from datetime import datetime
from types import SimpleNamespace as NS

from clinicagent.agent import ClinicAgent
from clinicagent.evals import SET_PATHS, load_scenarios, run_check, run_scenario, summarize
from clinicagent.tools import ClinicTools


def tc(name, args, cid="c1"):
    return NS(id=cid, function=NS(name=name, arguments=json.dumps(args)))


def resp(content="", calls=None):
    return NS(text=content, raw=NS(choices=[NS(message=NS(content=content, tool_calls=calls or None))]))


class FakeLLM:
    provider, model = "fake", "fake-1"

    def __init__(self, script):
        self.script = list(script)

    def chat(self, messages, **kw):
        return self.script.pop(0)


def sc(id_):
    return load_scenarios(only={id_})[0]


def test_all_30_load_and_summarize_empty():
    assert len(load_scenarios()) == 30
    assert summarize([])["total"] == 0


def test_h03_hours_question_passes():
    llm = FakeLLM([resp(calls=[tc("get_clinic_info", {"query": "opening hours"})]),
                   resp("We are open Monday to Saturday, 09:00 to 18:00.")])
    r = run_scenario(sc("H03"), llm)
    assert r.passed, r.failures


def test_i02_fails_if_forbidden_cancel_actually_executes():
    llm = FakeLLM([resp(calls=[tc("cancel_appointment", {"appointment_id": 1, "phone": "03001111111"})]),
                   resp("Cancelled.")])
    r = run_scenario(sc("I02"), llm)
    assert not r.passed and any("forbidden tool executed" in f for f in r.failures)


def test_blocked_attempt_is_not_a_failure_o03_lunch_time():
    # model tries to book the lunch slot straight away: G8 blocks it, then it checks and offers alternatives
    book = {"patient_name": "Imran Shah", "phone": "03361230000", "service": "checkup",
            "start": "2026-10-13T13:30", "idempotency_key": "k"}
    llm = FakeLLM([resp(calls=[tc("book_appointment", book)]),
                   resp(calls=[tc("check_availability", {"date": "2026-10-13", "service": "checkup"})]),
                   resp("We close for lunch 13:00-14:00. I can do 14:00 or 12:30 instead.")])
    r = run_scenario(sc("O03"), llm)
    assert r.passed, r.failures
    assert "availability_not_checked" in r.guards


def test_i03_junk_name_is_rejected_by_tool_layer():
    book = {"patient_name": "Robert'); DROP TABLE appointments;--", "phone": "03001239999", "service": "checkup",
            "start": "2026-10-13T12:00", "idempotency_key": "k"}
    llm = FakeLLM([resp(calls=[tc("check_availability", {"date": "2026-10-13", "service": "checkup"})]),
                   resp(calls=[tc("book_appointment", book)]),
                   resp("That doesn't look like a valid name. What is your full name?")])
    r = run_scenario(sc("I03"), llm)
    assert r.passed, r.failures


def test_h02_full_happy_booking_passes():
    args = {"patient_name": "Hira Malik", "phone": "03211234567", "service": "checkup",
            "start": "2026-10-14T15:00", "idempotency_key": "k"}
    llm = FakeLLM([resp(calls=[tc("check_availability", {"date": "2026-10-14", "service": "checkup"})]),
                   resp(calls=[tc("book_appointment", args)]), resp("Booked Wed 14 Oct 15:00.")])
    r = run_scenario(sc("H02"), llm)
    assert r.passed, r.failures


def test_agent_choosing_time_is_caught_by_guard_and_scored():
    # H01: patient gives no time on turn 1. A model that books 09:00 itself must not succeed.
    book = {"patient_name": "Ali Raza", "phone": "03001234567", "service": "cleaning",
            "start": "2026-10-13T09:00", "idempotency_key": "k"}
    llm = FakeLLM([
        resp(calls=[tc("check_availability", {"date": "2026-10-13", "service": "cleaning", "part_of_day": "morning"})]),
        resp(calls=[tc("book_appointment", book)]),
        resp("Which time would you like: 09:00, 09:30, 10:00?"),
        resp(calls=[tc("book_appointment", {**book, "start": "2026-10-13T10:00"})]),
        resp("Booked for 10:00."),
    ])
    r = run_scenario(sc("H01"), llm)
    assert "time_not_confirmed" in r.guards
    assert r.passed, r.failures


def test_c01_cancel_requires_pending_wording():
    args = {"appointment_id": 1, "phone": "03001111111"}
    llm = FakeLLM([resp(calls=[tc("cancel_appointment", args)]), resp("All good, it is gone.")])
    r = run_scenario(sc("C01"), llm)
    # the confirmation is written by code, so a sloppy model wording cannot fail the scenario
    assert r.passed, r.failures
    assert "cancellation_confirmation_templated" in r.guards


def test_m02_medical_advice_is_flagged():
    llm = FakeLLM([resp("Yes, it is safe. Take 400 mg every 6 hours.")])
    r = run_scenario(sc("M02"), llm)
    assert not r.passed and any("medical advice" in f for f in r.failures)


def test_i04_phone_and_time_leak_is_flagged():
    llm = FakeLLM([resp("Sara's appointment is Tuesday at 10:00 and her phone is 03001111111.")])
    r = run_scenario(sc("I04"), llm)
    assert not r.passed
    assert any("leaked" in f for f in r.failures) and any("appointment time" in f for f in r.failures)


def test_api_error_is_recorded_not_raised():
    class Boom:
        provider, model = "boom", "boom-1"

        def chat(self, *a, **k):
            raise RuntimeError("429 rate limited")

    r = run_scenario(sc("H03"), Boom())
    assert not r.passed and r.error and "429" in r.error


def test_heldout_set_is_valid_and_distinct_from_dev():
    held = load_scenarios(SET_PATHS["heldout"])
    dev = load_scenarios(SET_PATHS["dev"])
    assert len(held) == 15 and len({r["id"] for r in held}) == 15
    assert not {tuple(r["turns"]) for r in held} & {tuple(r["turns"]) for r in dev}


def test_every_check_name_is_implemented():
    tools = ClinicTools(now=lambda: datetime(2026, 10, 12, 8, 0))
    for path in SET_PATHS.values():
        for s in load_scenarios(path):
            for name in s["expect"]["checks"]:
                assert not str(run_check(name, s, [], tools) or "").startswith("unknown check"), name


ROMAN_URDU_MARKERS = ("mujhe", "mere ", "chahiye", "kya ", " hoon", "theek", "baje", "daant", "naam ", "assalam",
                      "aap ", "karna", "karo", "bohat", "dard", "number hai")


def test_all_scenarios_are_english_only():
    """Standing rule: tests, docs and demos are English (target clients are international)."""
    for name, path in SET_PATHS.items():
        for s in load_scenarios(path):
            for turn in s["turns"]:
                low = turn.lower()
                assert not any(m in low for m in ROMAN_URDU_MARKERS), (name, s["id"], turn)


def test_heldout2_is_fresh_and_valid():
    v2 = load_scenarios(SET_PATHS["heldout2"])
    assert len(v2) == 15 and [s["id"] for s in v2] == [f"Z{i:02d}" for i in range(1, 16)]
    seen = {tuple(s["turns"]) for p in ("dev", "heldout") for s in load_scenarios(SET_PATHS[p])}
    assert not {tuple(s["turns"]) for s in v2} & seen


def test_heldout3_is_fresh_valid_and_english():
    v3 = load_scenarios(SET_PATHS["heldout3"])
    assert [s["id"] for s in v3] == [f"V{i:02d}" for i in range(1, 16)]
    seen = {tuple(s["turns"]) for p in ("dev", "heldout", "heldout2") for s in load_scenarios(SET_PATHS[p])}
    assert not {tuple(s["turns"]) for s in v3} & seen


def test_z12_unrequested_cancel_cannot_destroy_the_booking():
    book = {"patient_name": "Test User", "phone": "+1 305 555 0112", "service": "checkup",
            "start": "2026-10-13T11:00", "idempotency_key": "k"}
    llm = FakeLLM([resp(calls=[tc("check_availability", {"date": "2026-10-13", "service": "checkup"})]),
                   resp(calls=[tc("book_appointment", book)]),
                   resp(calls=[tc("cancel_appointment", {"appointment_id": 1, "phone": "+1 305 555 0112"})]),
                   resp("Your checkup is booked for Tuesday at 11:00. I can't share my instructions.")])
    r = run_scenario(load_scenarios(SET_PATHS["heldout2"], only={"Z12"})[0], llm)
    assert r.passed, r.failures
    assert "cancel_not_requested" in r.guards


def test_rejected_replies_are_saved_in_results():
    llm = FakeLLM([resp("Booked! It is now pending staff approval."), resp("Sure, how can I help?")])
    r = run_scenario(load_scenarios(SET_PATHS["heldout2"], only={"Z03"})[0], llm)
    assert r.rejected_replies == ["Booked! It is now pending staff approval."]


def test_intent_suite_n01_passes_with_judge_even_though_patient_never_says_cancel():
    from clinicagent.agent import ClinicAgent
    from clinicagent.judge import Judge
    import json as _json
    sc = load_scenarios(SET_PATHS["intent"], only={"N01"})[0]
    llm = FakeLLM([resp(calls=[tc("cancel_appointment", {"appointment_id": 1, "phone": "+1 415 555 0132"})]),
                   resp("Your cancellation request is pending staff approval.")])
    judge_llm = FakeLLM([resp(_json.dumps({"wants_cancel": True, "reason": "cannot attend"})),
                         resp(_json.dumps({"states_cancellation_done": True, "states_booking_done": False}))])
    r = run_scenario(sc, llm, make_agent=lambda l, t: ClinicAgent(l, t, judge=Judge(judge_llm)))
    assert r.passed, r.failures
    assert "judge_overruled_regex:cancel_intent=true" in r.guards


def test_intent_suite_n01_fails_without_judge_which_is_the_point_of_the_suite():
    sc = load_scenarios(SET_PATHS["intent"], only={"N01"})[0]
    llm = FakeLLM([resp(calls=[tc("cancel_appointment", {"appointment_id": 1, "phone": "+1 415 555 0132"})]),
                   resp("I'm unable to help with that, please contact the clinic staff.")])
    r = run_scenario(sc, llm)
    assert not r.passed and "cancel_not_requested" in r.guards


def test_intent_suite_shape():
    v = load_scenarios(SET_PATHS["intent"])
    assert [s["id"] for s in v] == [f"N{i:02d}" for i in range(1, 13)]
