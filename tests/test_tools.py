import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from clinicagent.tools import SERVICES, TOOL_SCHEMAS, ClinicTools

NOW = datetime(2026, 10, 12, 8, 0)  # Monday 08:00
TUE = "2026-10-13"
SUN = "2026-10-18"


@pytest.fixture
def t():
    return ClinicTools(now=lambda: NOW)


def book(t, start=f"{TUE}T10:00", key="k1", service="checkup", name="Ali Raza", phone="0300 1234567"):
    return t.book_appointment(name, phone, service, start, key)


# ---------------------------------------------------------------- availability
def test_slots_respect_hours_and_lunch(t):
    r = t.check_availability(TUE, "checkup")
    assert r["ok"]
    assert r["slots"][0] == "09:00"
    assert "13:00" not in r["slots"] and "13:30" not in r["slots"]
    assert "14:00" in r["slots"] and "17:30" in r["slots"]
    assert "18:00" not in r["slots"]


def test_long_service_cannot_overlap_lunch_or_close(t):
    slots = t.check_availability(TUE, "filling")["slots"]
    assert "12:30" not in slots  # would run into lunch
    assert "17:30" not in slots  # would run past 18:00
    assert "17:00" in slots


def test_sunday_closed(t):
    r = t.check_availability(SUN, "checkup")
    assert not r["ok"] and r["error"] == "clinic_closed"


def test_morning_afternoon_filter(t):
    m = t.check_availability(TUE, "checkup", "morning")["slots"]
    a = t.check_availability(TUE, "checkup", "afternoon")["slots"]
    assert all(s < "13:00" for s in m) and all(s >= "14:00" for s in a)


def test_past_far_invalid_dates(t):
    assert t.check_availability("2026-10-01", "checkup")["error"] == "date_in_past"
    assert t.check_availability("2027-03-01", "checkup")["error"] == "date_too_far"
    assert t.check_availability("tomorrow", "checkup")["error"] == "invalid_date"
    assert t.check_availability(TUE, "teleport")["error"] == "unknown_service"


def test_lead_time_today(t):
    # NOW is 08:00 Monday: 09:00 is inside the 60-minute lead window? 09:00 >= 09:00 -> allowed
    slots = t.check_availability("2026-10-12", "checkup")["slots"]
    assert slots[0] == "09:00"
    late = ClinicTools(now=lambda: datetime(2026, 10, 12, 8, 30))
    assert late.check_availability("2026-10-12", "checkup")["slots"][0] == "09:30"


# --------------------------------------------------------------------- booking
def test_book_and_slot_disappears(t):
    r = book(t)
    assert r["ok"] and r["status"] == "booked" and r["end"] == f"{TUE}T10:30"
    assert "10:00" not in t.check_availability(TUE, "checkup")["slots"]


def test_double_booking_rejected_with_alternatives(t):
    book(t)
    r = book(t, key="k2", name="Sara Khan")
    assert not r["ok"] and r["error"] == "slot_unavailable" and r["alternatives"]


def test_overlap_by_long_service_rejected(t):
    book(t, start=f"{TUE}T10:00")
    r = book(t, start=f"{TUE}T09:30", service="filling", key="k2")
    assert r["error"] == "slot_unavailable"


def test_idempotent_replay_creates_one_row(t):
    a = book(t)
    b = book(t)
    assert b["idempotent_replay"] is True and a["appointment_id"] == b["appointment_id"]
    assert len(t.state()) == 1


def test_validation_errors(t):
    assert book(t, name="A")["error"] == "invalid_name"
    assert book(t, phone="123")["error"] == "invalid_phone"
    assert book(t, start="Tuesday 10am")["error"] == "invalid_start"
    assert book(t, start=f"{TUE}T13:30")["error"] == "slot_unavailable"
    assert book(t, key="")["error"] == "missing_idempotency_key"


def test_sql_injection_name_is_rejected_and_table_survives(t):
    r = book(t, name="Robert'); DROP TABLE appointments;--")
    assert not r["ok"] and r["error"] == "invalid_name"
    assert book(t, key="k9")["ok"]  # table still exists and works
    assert len(t.state()) == 1


@pytest.mark.parametrize("name,ok", [
    ("Ali Raza", True), ("Sara O'Brien", True), ("Anne-Marie Smith", True), ("Dr. Hira Malik", True),
    ("علی رضا", True), ("A", False), ("Ali123", False), ("Ali; DROP", False), ("<script>x</script>", False),
    ("--", False), ("12345", False), ("", False),
])
def test_name_validation(t, name, ok):
    assert (book(t, name=name, key=f"k-{name}").get("error") != "invalid_name") is ok


# ---------------------------------------------------------------- cancellation
def test_cancel_requires_approval(t):
    a = book(t)
    r = t.cancel_appointment(a["appointment_id"], "03001234567")
    assert r["ok"] and r["status"] == "pending_approval"
    assert t.state()[0]["status"] == "cancel_pending"
    # slot stays blocked until a human decides
    assert "10:00" not in t.check_availability(TUE, "checkup")["slots"]
    assert t.approve_cancellation(a["appointment_id"])["status"] == "cancelled"
    assert "10:00" in t.check_availability(TUE, "checkup")["slots"]


def test_reject_cancellation_restores_booking(t):
    a = book(t)
    t.cancel_appointment(a["appointment_id"], "03001234567")
    assert t.reject_cancellation(a["appointment_id"])["status"] == "booked"


def test_cancel_wrong_phone_or_id_same_error(t):
    a = book(t)
    wrong_phone = t.cancel_appointment(a["appointment_id"], "03009999999")
    wrong_id = t.cancel_appointment(999, "03001234567")
    assert wrong_phone["error"] == wrong_id["error"] == "not_found"
    assert wrong_phone["message"] == wrong_id["message"]
    assert t.state()[0]["status"] == "booked"


def test_cancel_twice(t):
    a = book(t)
    t.cancel_appointment(a["appointment_id"], "03001234567")
    again = t.cancel_appointment(a["appointment_id"], "03001234567")
    assert again["ok"] and again["already_pending"]
    t.approve_cancellation(a["appointment_id"])
    assert t.cancel_appointment(a["appointment_id"], "03001234567")["error"] == "already_cancelled"


# ------------------------------------------------------------------------- FAQ
@pytest.mark.parametrize(
    "q,topic",
    [
        ("what are your opening hours", "Opening hours"),
        ("is there parking", "Location and parking"),
        ("do you accept cards", "Payment methods"),
        ("what is your refund policy", "Cancellation and refund policy"),
    ],
)
def test_faq_hits(t, q, topic):
    r = t.get_clinic_info(q)
    assert r["ok"] and r["topic"] == topic


def test_faq_miss_for_medical_question(t):
    assert t.get_clinic_info("which antibiotic should I take for toothache")["error"] == "not_found"


# -------------------------------------------------------------------- dispatch
def test_dispatch_unknown_and_bad_args(t):
    assert t.dispatch("drop_db", {})["error"] == "unknown_tool"
    assert t.dispatch("check_availability", {"nonsense": 1})["error"] == "invalid_arguments"


def test_dispatch_roundtrip(t):
    r = t.dispatch("check_availability", {"date": TUE, "service": "cleaning"})
    assert r["ok"]


def test_schemas_match_tools(t):
    names = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert names == {"check_availability", "book_appointment", "cancel_appointment", "lookup_appointment", "get_clinic_info"}
    # approval actions must never be exposed to the model
    assert "approve_cancellation" not in names


# ------------------------------------------------------------- eval set sanity
def test_scenarios_file_valid():
    p = Path(__file__).resolve().parents[1] / "data" / "eval" / "scenarios.jsonl"
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 30
    assert len({r["id"] for r in rows}) == 30
    cats = {r["category"] for r in rows}
    assert cats == {"happy", "double_booking", "ambiguous_date", "out_of_hours", "injection",
                    "medical_advice", "cancellation"}
    for r in rows:
        assert r["turns"] and "expect" in r
        for s in r["seed"]:
            assert s["service"] in SERVICES


def test_seeded_scenarios_load_into_db():
    p = Path(__file__).resolve().parents[1] / "data" / "eval" / "scenarios.jsonl"
    for line in p.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        tools = ClinicTools(now=lambda: datetime.fromisoformat(r["now"]))
        tools.seed(r["seed"])
        assert len(tools.state()) == len(r["seed"])


# ------------------------------------------------- start-time formats (models often add seconds)
@pytest.mark.parametrize("start", [f"{TUE}T10:30", f"{TUE}T10:30:00", f"{TUE} 10:30", f"{TUE} 10:30:00", f"  {TUE}T10:30:00  "])
def test_start_formats_are_normalized(t, start):
    r = book(t, start=start, key=f"k-{start}")
    assert r["ok"] and r["start"] == f"{TUE}T10:30"


@pytest.mark.parametrize("start", ["Tuesday 10am", "2026-10-13", "10:30", "2026-13-40T10:30", ""])
def test_bad_start_formats_still_rejected(t, start):
    assert book(t, start=start)["error"] == "invalid_start"


def test_price_questions_reach_the_fee_section_not_the_duration_section():
    t = ClinicTools()
    for q in ("How much is a cleaning and a checkup?", "price of cleaning and checkup", "how much does a checkup cost",
              "What do you charge for a cleaning?", "prices"):
        r = t.get_clinic_info(q)
        assert r["ok"] and r["topic"] == "Consultation fees", (q, r)
        assert re.search(r"cleaning costs", r["answer"]) and re.search(r"checkup costs", r["answer"])


def test_close_runner_up_sections_are_returned_for_the_model_to_choose():
    r = ClinicTools().get_clinic_info("How long does a cleaning take and how much is it?")
    assert r["ok"]
    topics = {r["topic"]} | {x["topic"] for x in r.get("also_possibly_relevant", [])}
    assert {"Consultation fees", "Services and duration"} <= topics


def test_plural_forms_match_singular_questions():
    t = ClinicTools()
    assert t.get_clinic_info("emergency")["topic"] == "Emergencies"
    assert t.get_clinic_info("emergencies")["topic"] == "Emergencies"


def test_same_phone_written_in_different_formats_is_the_same_patient(t):
    a = book(t, phone="+92 300 1234567")
    assert t.cancel_appointment(a["appointment_id"], "0300 1234567")["ok"]
    b = book(t, start=f"{TUE}T11:00", key="k2", phone="0300-1234567")
    assert t.cancel_appointment(b["appointment_id"], "0092 300 1234567")["ok"]


def test_different_numbers_are_still_different_patients(t):
    a = book(t, phone="0300 1234567")
    assert t.cancel_appointment(a["appointment_id"], "0300 1234568")["error"] == "not_found"
    assert t.cancel_appointment(a["appointment_id"], "4567")["error"] == "not_found"  # a short tail is not a match


def test_lookup_needs_id_and_matching_phone_and_returns_no_personal_data(t):
    a = book(t, phone="+92 300 1234567")
    ok = t.lookup_appointment(a["appointment_id"], "0300 1234567")
    assert ok["ok"] and ok["status"] == "booked" and ok["start"].endswith("10:00")
    assert "phone" not in ok and "patient_name" not in ok
    wrong = t.lookup_appointment(a["appointment_id"], "03009999999")
    unknown = t.lookup_appointment(999, "0300 1234567")
    assert wrong == unknown and wrong["error"] == "not_found"  # does not reveal whether the id exists


def test_lookup_shows_pending_and_cancelled_status(t):
    a = book(t)
    t.cancel_appointment(a["appointment_id"], "0300 1234567")
    assert t.lookup_appointment(a["appointment_id"], "0300 1234567")["status"] == "cancel_pending"
    t.approve_cancellation(a["appointment_id"])
    assert t.lookup_appointment(a["appointment_id"], "0300 1234567")["status"] == "cancelled"
