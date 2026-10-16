"""Agent loop tests with a scripted fake LLM (no network, no API keys)."""
import json
from datetime import datetime
from types import SimpleNamespace as NS

from clinicagent.agent import EMERGENCY_REPLY, ClinicAgent, derive_key
from clinicagent.tools import ClinicTools

NOW = datetime(2026, 10, 12, 8, 0)


def tc(name, args, cid="c1"):
    return NS(id=cid, function=NS(name=name, arguments=args if isinstance(args, str) else json.dumps(args)))


def resp(content="", calls=None):
    msg = NS(content=content, tool_calls=calls or None)
    return NS(text=content, raw=NS(choices=[NS(message=msg)]))


class FakeLLM:
    provider, model = "fake", "fake-1"

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append((list(messages), kw))
        return self.script.pop(0)


def make(script):
    tools = ClinicTools(now=lambda: NOW)
    return ClinicAgent(FakeLLM(script), tools), tools


def test_simple_text_reply():
    agent, _ = make([resp("Hello! How can I help?")])
    turn = agent.reply("hi")
    assert turn.reply == "Hello! How can I help?" and turn.steps == 1 and not turn.tool_calls


def test_tools_and_system_prompt_are_sent():
    agent, _ = make([resp("Hello, how can I help you today?")])
    agent.reply("hi")
    msgs, kw = agent.llm.calls[0]
    assert "2026-10-13" in msgs[0]["content"] and "2026-10-18 (closed)" in msgs[0]["content"]
    assert {t["function"]["name"] for t in kw["tools"]} == {
        "check_availability", "book_appointment", "cancel_appointment", "lookup_appointment", "get_clinic_info"}
    assert kw["temperature"] == 0.0


def test_check_then_book_flow_changes_db():
    agent, tools = make([
        resp(calls=[tc("check_availability", {"date": "2026-10-13", "service": "cleaning"})]),
        resp(calls=[tc("book_appointment", {"patient_name": "Ali Raza", "phone": "03001234567",
                                            "service": "cleaning", "start": "2026-10-13T09:30",
                                            "idempotency_key": "model-made-up"}, "c2")]),
        resp("Booked: cleaning Tue 13 Oct 09:30."),
    ])
    turn = agent.reply("cleaning tomorrow at 9:30am, Ali Raza 03001234567")
    assert turn.steps == 3 and [c.name for c in turn.tool_calls] == ["check_availability", "book_appointment"]
    rows = tools.state()
    assert len(rows) == 1 and rows[0]["start"] == "2026-10-13T09:30"
    # tool result messages were fed back with the right ids
    last_msgs = agent.llm.calls[-1][0]
    assert [m["tool_call_id"] for m in last_msgs if m["role"] == "tool"] == ["c1", "c2"]


def test_idempotency_key_derived_in_code_so_retry_is_safe():
    args = {"patient_name": "Mina Javed", "phone": "0321 7778888", "service": "cleaning",
            "start": "2026-10-13T11:00", "idempotency_key": "random-1"}
    args2 = {**args, "idempotency_key": "random-2"}  # model invents a new key on retry
    agent, tools = make([
        resp(calls=[tc("check_availability", {"date": "2026-10-13", "service": "cleaning"})]),
        resp(calls=[tc("book_appointment", args)]), resp("done"),
        resp(calls=[tc("book_appointment", args2)]), resp("done again"),
    ])
    agent.reply("book it at 11:00 am")
    t2 = agent.reply("did it work? book again")
    assert len(tools.state()) == 1
    assert t2.tool_calls[0].result["idempotent_replay"] is True
    assert derive_key("0321 7778888", "cleaning", "2026-10-13T11:00") == derive_key(
        "03217778888", "cleaning", "2026-10-13T11:00")


def test_bad_json_arguments_never_execute():
    agent, tools = make([resp(calls=[tc("book_appointment", "{not json")]), resp("sorry, retrying")])
    turn = agent.reply("book")
    assert turn.tool_calls[0].result["error"] == "invalid_arguments"
    assert "bad_tool_arguments" in turn.guards and tools.state() == []


def test_unknown_tool_is_an_error_not_a_crash():
    agent, _ = make([resp(calls=[tc("approve_cancellation", {"appointment_id": 1})]), resp("Sorry, I cannot do that.")])
    turn = agent.reply("approve it")
    assert turn.tool_calls[0].result["error"] == "unknown_tool"


def test_max_steps_guard():
    loop = [resp(calls=[tc("get_clinic_info", {"query": "hours"}, f"c{i}")]) for i in range(10)]
    agent, _ = make(loop)
    turn = agent.reply("hours?")
    assert turn.steps == 6 and "max_steps" in turn.guards and "contact the clinic" in turn.reply


def test_emergency_bypasses_llm():
    agent, _ = make([])  # LLM must not be called
    turn = agent.reply("My child fell, a tooth is knocked out and he is struggling to breathe!")
    assert turn.reply == EMERGENCY_REPLY and "emergency_bypass" in turn.guards
    assert agent.llm.calls == []


def test_normal_toothache_is_not_bypassed():
    agent, _ = make([resp("I can't give medical advice, but I can book a visit.")])
    turn = agent.reply("my tooth hurts, can I get an appointment?")
    assert "emergency_bypass" not in turn.guards and len(agent.llm.calls) == 1


def test_cancel_reply_must_say_pending():
    agent, tools = make([])
    tools.seed([{"name": "Sara Khan", "phone": "03001111111", "service": "checkup", "start": "2026-10-13T10:00"}])
    agent.llm.script = [
        resp(calls=[tc("cancel_appointment", {"appointment_id": 1, "phone": "03001111111"})]),
        resp("Done, your appointment is gone."),
    ]
    turn = agent.reply("cancel booking 1, 03001111111")
    assert "cancellation_confirmation_templated" in turn.guards
    assert "pending staff approval" in turn.reply and "time stays reserved" in turn.reply and "gone" not in turn.reply
    assert tools.state()[0]["status"] == "cancel_pending"


def test_cancel_reply_already_ok_is_not_duplicated():
    agent, tools = make([])
    tools.seed([{"name": "Sara Khan", "phone": "03001111111", "service": "checkup", "start": "2026-10-13T10:00"}])
    agent.llm.script = [
        resp(calls=[tc("cancel_appointment", {"appointment_id": 1, "phone": "03001111111"})]),
        resp("Your cancellation request is pending staff approval."),
    ]
    turn = agent.reply("cancel 1")
    assert "pending_note_added" not in turn.guards


def test_gemini_thought_signature_is_echoed_back():
    sig = {"google": {"thought_signature": "CvcQAdHN2OekY10C"}}
    call = tc("check_availability", {"date": "2026-10-13", "service": "cleaning"})
    call.extra_content = sig
    agent, _ = make([resp(calls=[call]), resp("Here are the slots.")])
    agent.reply("cleaning tomorrow")
    second_request_msgs = agent.llm.calls[1][0]
    assistant = [m for m in second_request_msgs if m["role"] == "assistant"][0]
    assert assistant["tool_calls"][0]["extra_content"] == sig


def test_no_extra_content_for_other_providers():
    agent, _ = make([resp(calls=[tc("get_clinic_info", {"query": "hours"})]), resp("We are open nine to six.")])
    agent.reply("hours?")
    assistant = [m for m in agent.llm.calls[1][0] if m["role"] == "assistant"][0]
    assert "extra_content" not in assistant["tool_calls"][0]


def test_g7_model_cannot_pick_a_time_for_the_patient():
    book = {"patient_name": "Ali Raza", "phone": "03001234567", "service": "cleaning",
            "start": "2026-10-13T09:00", "idempotency_key": "x"}
    agent, tools = make([resp(calls=[tc("book_appointment", book)]), resp("Which time would you like?")])
    turn = agent.reply("Cleaning tomorrow morning please, Ali Raza 03001234567")
    assert turn.tool_calls[0].result["error"] == "time_not_confirmed"
    assert "time_not_confirmed" in turn.guards and tools.state() == []


def test_g7_allows_time_the_assistant_offered_and_patient_accepted():
    book = {"patient_name": "Ali Raza", "phone": "03001234567", "service": "cleaning",
            "start": "2026-10-13T09:00", "idempotency_key": "x"}
    agent, tools = make([resp("The first free time is 09:00. Shall I book it?"),
                         resp(calls=[tc("check_availability", {"date": "2026-10-13", "service": "cleaning"})]),
                         resp(calls=[tc("book_appointment", book)]), resp("Booked.")])
    agent.reply("Cleaning tomorrow morning please")
    agent.reply("yes, Ali Raza 03001234567")
    assert len(tools.state()) == 1


def test_g8_book_without_checking_availability_is_blocked():
    book = {"patient_name": "Ali Raza", "phone": "03001234567", "service": "cleaning",
            "start": "2026-10-13T10:00", "idempotency_key": "x"}
    agent, tools = make([resp(calls=[tc("book_appointment", book)]),
                         resp(calls=[tc("check_availability", {"date": "2026-10-13", "service": "cleaning"})]),
                         resp(calls=[tc("book_appointment", book, "c3")]), resp("Booked 10:00.")])
    turn = agent.reply("cleaning tomorrow at 10am, Ali Raza 03001234567")
    assert turn.tool_calls[0].result["error"] == "availability_not_checked"
    assert "availability_not_checked" in turn.guards
    assert turn.tool_calls[2].result["ok"] and len(tools.state()) == 1


def test_g8_check_for_other_date_or_service_does_not_unlock():
    book = {"patient_name": "Ali Raza", "phone": "03001234567", "service": "cleaning",
            "start": "2026-10-13T10:00", "idempotency_key": "x"}
    agent, tools = make([resp(calls=[tc("check_availability", {"date": "2026-10-14", "service": "cleaning"})]),
                         resp(calls=[tc("book_appointment", book)]), resp("sorry")])
    turn = agent.reply("cleaning on 13 Oct at 10am, Ali Raza 03001234567")
    assert turn.tool_calls[1].result["error"] == "availability_not_checked" and tools.state() == []


def test_g7_offer_in_same_turn_does_not_count():
    book = {"patient_name": "Ali Raza", "phone": "03001234567", "service": "cleaning",
            "start": "2026-10-13T09:00", "idempotency_key": "x"}
    # assistant text that comes AFTER the latest user message must not unlock booking
    agent, tools = make([resp("Let me book 09:00 for you.", calls=[tc("book_appointment", book)]), resp("Sorry, I cannot book that yet.")])
    turn = agent.reply("cleaning tomorrow morning, Ali Raza 03001234567")
    assert turn.tool_calls[0].result["error"] == "time_not_confirmed" and tools.state() == []


import pytest
from clinicagent.agent import mentions_time


@pytest.mark.parametrize("text,hh,mm,expected", [
    ("09:30 am", 9, 30, True), ("9:30am", 9, 30, True), ("at 9.30", 9, 30, True),
    ("3pm please", 15, 0, True), ("15:00", 15, 0, True),
    ("Wednesday at 3", 15, 0, True), ("10am", 10, 0, True), ("10", 10, 0, False),
    ("19:30", 9, 30, False), ("tomorrow morning", 9, 0, False), ("2026-10-13", 13, 0, False),
    ("11:00", 11, 0, True), ("5:45pm", 17, 45, True),
])
def test_mentions_time(text, hh, mm, expected):
    assert mentions_time(text, hh, mm) is expected


def test_history_accumulates_across_turns():
    agent, _ = make([resp("one"), resp("two")])
    agent.reply("a")
    agent.reply("b")
    roles = [m["role"] for m in agent.messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    assert len(agent.trace) == 2


def test_prompt_tells_model_a_bare_weekday_means_the_next_one():
    from clinicagent.agent import SYSTEM_PROMPT
    assert "bare weekday" in SYSTEM_PROMPT and "next" in SYSTEM_PROMPT.lower()


# ------------------------------------------------------------------ G9 / G10 / G11 + start normalisation
from clinicagent.agent import FAILSAFE_REPLY

SARA = {"name": "Sara Khan", "phone": "03001111111", "service": "checkup", "start": "2026-10-13T10:00"}
CHECK = ("check_availability", {"date": "2026-10-13", "service": "cleaning"})


def test_model_sending_seconds_in_start_still_books_and_key_is_stable():
    book = {"patient_name": "Hamza Yousaf", "phone": "0345-1112233", "service": "cleaning",
            "start": "2026-10-13T10:30:00", "idempotency_key": "x"}
    agent, tools = make([resp(calls=[tc(*CHECK)]), resp(calls=[tc("book_appointment", book)]), resp("Booked 10:30.")])
    turn = agent.reply("cleaning on 13 Oct at 10:30, Hamza Yousaf 0345-1112233")
    booked = turn.tool_calls[1]
    assert booked.result["ok"] and booked.args["start"] == "2026-10-13T10:30"
    assert booked.args["idempotency_key"] == derive_key("0345-1112233", "cleaning", "2026-10-13T10:30")
    assert len(tools.state()) == 1


def test_g9_identical_failed_call_is_not_run_twice():
    bad = {"patient_name": "A", "phone": "03001234567", "service": "cleaning", "start": "2026-10-13T10:30",
           "idempotency_key": "x"}
    agent, tools = make([resp(calls=[tc(*CHECK)]), resp(calls=[tc("book_appointment", bad, "c2")]),
                         resp(calls=[tc("book_appointment", bad, "c3")]),
                         resp("I need your full name to book this.")])
    turn = agent.reply("cleaning on 13 Oct at 10:30, A 03001234567")
    assert [c.result.get("error") for c in turn.tool_calls[1:]] == ["invalid_name", "repeated_failed_call"]
    assert "repeated_failed_call" in turn.guards and tools.state() == []


def test_g10_false_cancel_claim_is_repaired_by_forcing_the_tool_call():
    # real Groq failure (held-out X15): it said "submitted, pending approval" but never called cancel_appointment
    agent, tools = make([])
    tools.seed([SARA])
    agent.llm.script = [
        resp("I've submitted a cancellation request for appointment #1. It is pending staff approval."),
        resp(calls=[tc("cancel_appointment", {"appointment_id": 1, "phone": "03001111111"})]),
        resp("Your cancellation request is pending staff approval."),
    ]
    turn = agent.reply("Please cancel booking 1, phone 03001111111.")
    assert "repair:false_cancel_claim" in turn.guards
    assert tools.state()[0]["status"] == "cancel_pending"
    assert "pending" in turn.reply.lower()
    repair_msg = [m for m in agent.llm.calls[1][0] if m["role"] == "user"][-1]["content"]
    assert repair_msg.startswith("Automated check")


def test_g10_repeated_false_claim_is_blocked_with_safe_reply():
    agent, tools = make([])
    tools.seed([SARA])
    agent.llm.script = [resp("I've submitted your cancellation request, pending staff approval."),
                        resp("Your cancellation request has been submitted and is pending staff approval.")]
    turn = agent.reply("cancel it please")
    assert "blocked:false_cancel_claim" in turn.guards and turn.reply == FAILSAFE_REPLY
    assert tools.state()[0]["status"] == "booked"


def test_g10_false_booking_claim_is_repaired():
    agent, tools = make([resp("You're all set! Your cleaning is booked for 09:30."),
                         resp("Which time would you like?")])
    turn = agent.reply("I want a cleaning tomorrow")
    assert "repair:false_booking_claim" in turn.guards and turn.reply == "Which time would you like?"
    assert tools.state() == []


def test_g10_claim_is_fine_after_a_real_booking():
    book = {"patient_name": "Ali Raza", "phone": "03001234567", "service": "cleaning",
            "start": "2026-10-13T09:30", "idempotency_key": "x"}
    agent, _ = make([resp(calls=[tc(*CHECK)]), resp(calls=[tc("book_appointment", book)]),
                     resp("You're all set! Your cleaning is booked for Tue 13 Oct at 09:30.")])
    turn = agent.reply("cleaning on 13 Oct at 9:30am, Ali Raza 03001234567")
    assert not [g for g in turn.guards if g.startswith(("repair", "blocked"))]


def test_g11_garbage_reply_is_repaired_once_then_replaced():
    # real Gemini failure (dev O03): final reply was just "_"
    agent, _ = make([resp("_"), resp("Sorry about that. Which day suits you?")])
    turn = agent.reply("hi")
    assert "repair:empty_reply" in turn.guards and turn.reply == "Sorry about that. Which day suits you?"
    agent2, _ = make([resp("_"), resp("")])
    turn2 = agent2.reply("hi")
    assert "blocked:empty_reply" in turn2.guards and turn2.reply == FAILSAFE_REPLY


# ------------------------------------------------------------------ G12 + grounded fallback (real Groq run, held-out2 Z04 / Z12)
def test_g12_cancel_is_refused_when_patient_never_asked_to_cancel():
    # Z12: after booking, a guard note pushed the model into cancelling the patient's NEW booking
    book = {"patient_name": "Test User", "phone": "+1 305 555 0112", "service": "checkup",
            "start": "2026-10-13T11:00", "idempotency_key": "x"}
    cancel = {"appointment_id": 1, "phone": "+1 305 555 0112"}
    agent, tools = make([resp(calls=[tc("check_availability", {"date": "2026-10-13", "service": "checkup"})]),
                         resp(calls=[tc("book_appointment", book)]),
                         resp(calls=[tc("cancel_appointment", cancel, "c3")]),
                         resp("Your checkup is booked for Tuesday at 11:00.")])
    turn = agent.reply("Please book a checkup on Tuesday at 11:00 am for Test User, phone +1 305 555 0112.")
    assert turn.tool_calls[2].result["error"] == "cancel_not_requested"
    assert "cancel_not_requested" in turn.guards
    assert tools.state()[0]["status"] == "booked"


def test_unrequested_cancel_claim_repair_note_does_not_tell_the_model_to_cancel():
    agent, _ = make([resp("Booked! It is now pending staff approval."), resp("Which time would you like?")])
    turn = agent.reply("Checkup tomorrow at 9:00 am please")
    assert "repair:unrequested_cancel_claim" in turn.guards
    note = [m for m in agent.llm.calls[1][0] if m["role"] == "user"][-1]["content"]
    assert "has not asked to cancel" in note and "call cancel_appointment now" not in note
    assert turn.rejected == ["Booked! It is now pending staff approval."]


def test_blocked_reply_after_failed_cancel_explains_the_real_problem():
    # Z04: wrong phone -> not_found. The patient must hear why, not just "Sorry, I couldn't complete that"
    agent, tools = make([])
    tools.seed([SARA])
    agent.llm.script = [
        resp(calls=[tc("cancel_appointment", {"appointment_id": 1, "phone": "+1 415 555 0199"})]),
        resp("Your cancellation request is pending staff approval."),
        resp("It has been submitted and is pending staff approval."),
    ]
    turn = agent.reply("Please cancel booking 1, my phone is +1 415 555 0199.")
    assert "blocked:false_cancel_claim" in turn.guards
    assert "No appointment matches that id and phone number." in turn.reply
    assert tools.state()[0]["status"] == "booked"


def test_blocked_reply_after_real_booking_still_confirms_the_booking():
    book = {"patient_name": "Test User", "phone": "+1 305 555 0112", "service": "checkup",
            "start": "2026-10-13T11:00", "idempotency_key": "x"}
    agent, _ = make([resp(calls=[tc("check_availability", {"date": "2026-10-13", "service": "checkup"})]),
                     resp(calls=[tc("book_appointment", book)]),
                     resp("It is pending staff approval."), resp("It is now pending staff approval.")])
    turn = agent.reply("Book a checkup on Tuesday at 11:00 am for Test User, phone +1 305 555 0112")
    assert "blocked:unrequested_cancel_claim" in turn.guards
    assert turn.reply.startswith("Your checkup is booked for Tuesday 13 October, 11:00 to 11:30")


def test_fallback_never_shows_internal_guard_messages():
    book = {"patient_name": "Ali Raza", "phone": "03001234567", "service": "cleaning",
            "start": "2026-10-13T09:30", "idempotency_key": "x"}
    agent, _ = make([resp(calls=[tc("book_appointment", book)]),  # blocked by G8 (no availability check)
                     resp("_"), resp("_")])
    turn = agent.reply("cleaning on 13 Oct at 9:30am, Ali Raza 03001234567")
    assert turn.reply == FAILSAFE_REPLY and "check_availability" not in turn.reply


def test_conditional_future_cancel_wording_is_not_a_false_claim():
    from clinicagent.agent import CANCEL_CLAIM
    ok = ["I couldn't find booking 7. Once I have the right details, I can submit a cancellation request "
          "(which will be pending staff approval).",
          "Once you confirm, I'll submit the cancellation request, pending staff approval."]
    assert not any(CANCEL_CLAIM.search(t) for t in ok)


def test_asserted_pending_cancellation_is_still_a_claim():
    from clinicagent.agent import CANCEL_CLAIM
    assert CANCEL_CLAIM.search("Your cancellation is now pending staff approval.")
    assert CANCEL_CLAIM.search("I've submitted your cancellation request.")


def _book_script(final_text):
    book = {"patient_name": "Ali Raza", "phone": "03001234567", "service": "cleaning",
            "start": "2026-10-13T10:00", "idempotency_key": "x"}
    return [resp(calls=[tc("check_availability", {"date": "2026-10-13", "service": "cleaning"})]),
            resp(calls=[tc("book_appointment", book)]), resp(final_text)]


def test_booking_confirmation_is_written_by_code_with_id_and_exact_time():
    agent, _ = make(_book_script("**Booked!** Date: 2026\u201110\u201113, 10:00 (ends 10:30). Appointment ID: 99"))
    turn = agent.reply("Cleaning tomorrow at 10:00 please, Ali Raza 03001234567")
    assert turn.reply.startswith("Your cleaning is booked for Tuesday 13 October, 10:00 to 10:30. Your appointment ID is 1.")
    assert "**" not in turn.reply and "99" not in turn.reply
    assert "booking_confirmation_templated" in turn.guards


def test_no_templated_confirmation_when_nothing_was_booked():
    agent, _ = make([resp("We are open Monday to Saturday, 09:00 to 18:00.")])
    turn = agent.reply("When are you open?")
    assert turn.reply.startswith("We are open") and "booking_confirmation_templated" not in turn.guards


def test_system_prompt_asks_for_plain_text_and_one_line_time_lists():
    from clinicagent.agent import SYSTEM_PROMPT
    assert "Plain text only" in SYSTEM_PROMPT and "one line separated by commas" in SYSTEM_PROMPT


def test_stale_availability_is_expired_and_booking_needs_a_fresh_lookup():
    import json as _j
    book = {"patient_name": "Ali Raza", "phone": "03001234567", "service": "cleaning", "start": "2026-10-13T10:00",
            "idempotency_key": "x"}
    chk = {"date": "2026-10-13", "service": "cleaning"}
    agent, tools = make([
        resp(calls=[tc("check_availability", chk, cid="a1")]), resp("Free times: 09:00, 10:00, 11:00. Which one?"),
        resp(calls=[tc("book_appointment", book, cid="b1")]),          # tries to book on the old lookup
        resp(calls=[tc("check_availability", chk, cid="a2")]),
        resp(calls=[tc("book_appointment", book, cid="b2")]), resp("ok then"),
    ])
    agent.reply("Cleaning on Tuesday please")
    # somebody else changes the calendar between the two messages
    tools.dispatch("book_appointment", {"patient_name": "Other Person", "phone": "03009998888", "service": "checkup",
                                        "start": "2026-10-13T15:00", "idempotency_key": "other"})
    turn = agent.reply("10:00, Ali Raza 03001234567")
    old = [m for m in agent.messages if m.get("role") == "tool" and m["tool_call_id"] == "a1"][0]
    assert _j.loads(old["content"])["stale"] is True                       # earlier list can no longer be reused
    assert turn.tool_calls[0].result["error"] == "availability_not_checked"
    assert turn.tool_calls[-1].result["ok"] is True                         # booked after a fresh lookup


def test_retry_of_a_successful_booking_is_not_blocked_by_freshness_rules():
    book = {"patient_name": "Ali Raza", "phone": "03001234567", "service": "cleaning", "start": "2026-10-13T10:00",
            "idempotency_key": "x"}
    agent, tools = make([
        resp(calls=[tc("check_availability", {"date": "2026-10-13", "service": "cleaning"})]),
        resp(calls=[tc("book_appointment", book)]), resp("done"),
        resp(calls=[tc("book_appointment", book)]), resp("done"),
    ])
    agent.reply("cleaning at 10:00, Ali Raza 03001234567")
    t2 = agent.reply("did it work? book again at 10:00")
    assert t2.tool_calls[0].result["idempotent_replay"] is True and len(tools.state()) == 1


def test_calendar_version_changes_on_book_cancel_and_resolve():
    tools = ClinicTools(now=lambda: NOW)
    v0 = tools.version
    b = tools.dispatch("book_appointment", {"patient_name": "Ali Raza", "phone": "03001234567", "service": "cleaning",
                                            "start": "2026-10-13T10:00", "idempotency_key": "k"})
    v1 = tools.version
    tools.dispatch("cancel_appointment", {"appointment_id": b["appointment_id"], "phone": "03001234567"})
    v2 = tools.version
    tools.approve_cancellation(b["appointment_id"])
    assert v0 < v1 < v2 < tools.version


def test_cancellation_confirmation_says_the_slot_stays_reserved():
    from clinicagent.agent import cancellation_confirmation
    t = cancellation_confirmation({"appointment_id": 4})
    assert "appointment 4" in t and "pending staff approval" in t and "stays reserved" in t
    assert "already waiting" in cancellation_confirmation({"appointment_id": 4, "already_pending": True})


def test_slot_list_copied_from_earlier_conversation_is_repaired_with_a_fresh_lookup():
    import json as _j
    chk = {"date": "2026-10-14", "service": "cleaning"}
    agent, tools = make([
        resp("Free times on Wednesday 14 October: 09:00, 09:30, 11:00."),  # copied, no tool call in this turn
        resp(calls=[tc("check_availability", chk, cid="c1")]),
        resp("Available times on Wednesday 14 October: 09:00, 09:30, 11:00. Which would you like?"),
    ])
    turn = agent.reply("Book a cleaning for tomorrow morning")
    assert "repair:unverified_slot_list" in turn.guards
    assert [c.name for c in turn.tool_calls] == ["check_availability"]


def test_slot_list_without_lookup_is_replaced_if_the_model_does_not_comply():
    agent, _ = make([resp("Free times: 09:00, 09:30, 11:00."), resp("Still free: 09:00, 09:30, 11:00.")])
    turn = agent.reply("Book a cleaning for tomorrow morning")
    assert "blocked:unverified_slot_list" in turn.guards
    assert "09:30" not in turn.reply and "check the calendar" in turn.reply


def test_opening_hours_range_is_not_treated_as_a_slot_list():
    agent, _ = make([resp("We are open Monday to Saturday, 09:00 to 18:00.")])
    assert "repair:unverified_slot_list" not in agent.reply("When are you open?").guards


def test_system_prompt_lets_patients_ask_for_dates_beyond_the_calendar():
    from clinicagent.agent import SYSTEM_PROMPT
    assert "ANY date, up to 60 days" in SYSTEM_PROMPT and "without a lookup" in SYSTEM_PROMPT


SEEDED = [{"name": "Maria Lopez", "phone": "03007654321", "service": "cleaning", "start": "2026-10-13T10:00"}]


def test_lookup_lets_the_reply_state_an_existing_booking_and_the_time_comes_from_the_tool():
    agent, tools = make([
        resp(calls=[tc("lookup_appointment", {"appointment_id": 1, "phone": "0300 7654321"})]),
        resp("Your appointment is booked for tomorrow around noon."),   # wrong time, but not a false claim
    ])
    tools.seed(SEEDED)
    turn = agent.reply("What time is my appointment? ID 1, phone 0300 7654321")
    assert "false_booking_claim" not in " ".join(turn.guards)
    assert "lookup_summary_templated" in turn.guards
    assert "10:00" in turn.reply and "Tuesday 13 October" in turn.reply and "confirmed" in turn.reply


def test_lookup_reply_that_already_states_the_real_time_is_kept():
    agent, tools = make([
        resp(calls=[tc("lookup_appointment", {"appointment_id": 1, "phone": "03007654321"})]),
        resp("Your cleaning is on Tuesday at 10:00 and it is confirmed. Anything else?"),
    ])
    tools.seed(SEEDED)
    turn = agent.reply("When is my appointment? ID 1 phone 03007654321")
    assert turn.reply.startswith("Your cleaning is on Tuesday at 10:00") and "lookup_summary_templated" not in turn.guards


def test_lookup_with_wrong_phone_reveals_nothing():
    agent, tools = make([
        resp(calls=[tc("lookup_appointment", {"appointment_id": 1, "phone": "03000000000"})]),
        resp("I could not find an appointment with that id and phone number."),
    ])
    tools.seed(SEEDED)
    turn = agent.reply("When is my appointment? ID 1 phone 03000000000")
    assert "10:00" not in turn.reply and "Maria" not in turn.reply and turn.tool_calls[0].result["error"] == "not_found"


def test_pending_cancellation_is_described_correctly_by_lookup():
    agent, tools = make([resp(calls=[tc("lookup_appointment", {"appointment_id": 1, "phone": "03007654321"})]), resp("It is cancelled.")])
    tools.seed([dict(SEEDED[0], status="cancel_pending")])
    turn = agent.reply("Status of appointment 1, phone 03007654321?")
    assert "pending staff approval" in turn.reply and "reserved" in turn.reply


def test_promise_that_staff_will_follow_up_is_repaired_then_replaced():
    from clinicagent.agent import HANDOFF_REPLY
    agent, _ = make([resp("I'm sorry, I can't process refunds. A staff member will review your request and follow up shortly."),
                     resp("Our team will get back to you soon about the refund.")])
    turn = agent.reply("Can you give me a refund of my deposit?")
    assert "repair:false_staff_promise" in turn.guards and "blocked:false_staff_promise" in turn.guards
    assert turn.reply == HANDOFF_REPLY


def test_honest_handoff_wording_is_not_blocked():
    agent, _ = make([resp("I can't promise refunds. Please contact the clinic directly; the front-desk team can help with that.")])
    turn = agent.reply("Can you give me a refund of my deposit?")
    assert not any("false_staff_promise" in g for g in turn.guards)


def test_staff_review_of_a_real_cancellation_request_is_allowed():
    agent, tools = make([
        resp(calls=[tc("cancel_appointment", {"appointment_id": 1, "phone": "03007654321"})]),
        resp("Done. Our staff will review your request and confirm it."),
    ])
    tools.seed(SEEDED)
    turn = agent.reply("Please cancel appointment 1, phone 03007654321")
    assert not any("false_staff_promise" in g for g in turn.guards)


def test_failsafe_reply_does_not_promise_a_follow_up():
    from clinicagent.agent import FAILSAFE_REPLY, FOLLOWUP_PROMISE, HANDOFF_REPLY
    assert not FOLLOWUP_PROMISE.search(FAILSAFE_REPLY) and not FOLLOWUP_PROMISE.search(HANDOFF_REPLY)
