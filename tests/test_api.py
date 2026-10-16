"""API tests: scripted fake LLM, in-memory calendar, no network."""
import threading

import pytest
from fastapi.testclient import TestClient

from clinicagent.agent import ClinicAgent
from clinicagent.api import BUSY_MESSAGE, create_app
from clinicagent.tools import ClinicTools

from test_agent import NOW, FakeLLM, resp, tc

SEED = {"name": "Emma Johnson", "phone": "+1 415 555 0132", "service": "checkup", "start": "2026-10-13T10:00"}
KEY = "staff-secret"


class RateLimitError(Exception):
    pass


class ScriptedLLM(FakeLLM):
    """Fake that can raise on demand (class name RateLimitError counts as transient)."""

    def __init__(self, script, raise_exc=None):
        super().__init__(script)
        self.raise_exc = raise_exc

    def chat(self, messages, **kw):
        if self.raise_exc:
            raise self.raise_exc
        return super().chat(messages, **kw)


def client(script=(), *, seed=False, staff_key=KEY, raise_exc=None, **kw):
    tools = ClinicTools(now=lambda: NOW)
    if seed:
        tools.seed([SEED])
    llm = ScriptedLLM(script, raise_exc)
    app = create_app(llm, tools, staff_key=staff_key, use_judge=False,
                     agent_factory=lambda l, t: ClinicAgent(l, t, retries=0), **kw)
    return TestClient(app), tools, llm


def test_health():
    c, _, _ = client()
    assert c.get("/health").json()["status"] == "ok"


def test_chat_creates_a_session_and_keeps_history():
    c, _, llm = client([resp("Hello! How can I help?"), resp("We open at 09:00.")])
    r1 = c.post("/chat", json={"message": "hi"}).json()
    assert r1["reply"] == "Hello! How can I help?" and len(r1["session_id"]) == 32 and "debug" not in r1
    r2 = c.post("/chat", json={"message": "when do you open?", "session_id": r1["session_id"]}).json()
    assert r2["session_id"] == r1["session_id"]
    roles = [m["role"] for m in llm.calls[1][0]]
    assert roles.count("user") == 2  # second call saw the first message too


def test_unknown_session_id_starts_a_new_session():
    c, _, _ = client([resp("Hello!")])
    r = c.post("/chat", json={"message": "hi", "session_id": "does-not-exist"}).json()
    assert r["session_id"] != "does-not-exist"


@pytest.mark.parametrize("body", [{"message": ""}, {"message": "x" * 1001}, {}])
def test_bad_messages_are_rejected(body):
    c, _, _ = client()
    assert c.post("/chat", json=body).status_code == 422


def test_debug_info_only_when_enabled():
    c, _, _ = client([resp("Hello!")], debug=True)
    assert c.post("/chat", json={"message": "hi"}).json()["debug"] == {"guards": [], "tool_calls": []}


def test_rate_limit_per_client():
    c, _, _ = client([resp("Hello there"), resp("Hello again"), resp("Hello once more")], rate_limit_per_min=2)
    assert [c.post("/chat", json={"message": "hi"}).status_code for _ in range(3)] == [200, 200, 429]


def test_transient_llm_failure_is_a_friendly_503():
    c, _, _ = client(raise_exc=RateLimitError("429 quota"))
    r = c.post("/chat", json={"message": "hi"})
    assert r.status_code == 503 and r.json()["detail"] == BUSY_MESSAGE and "quota" not in r.text


def test_unexpected_error_is_a_generic_500_without_internals():
    c, _, _ = client(raise_exc=ValueError("secret internal detail"))
    r = c.post("/chat", json={"message": "hi"})
    assert r.status_code == 500 and "secret" not in r.text


# ---------------------------------------------------------------- staff
def test_staff_endpoints_are_disabled_without_a_configured_key():
    c, _, _ = client(staff_key=None)
    assert c.get("/approvals", headers={"X-Staff-Key": "anything"}).status_code == 503


def test_staff_endpoints_need_the_right_key():
    c, _, _ = client()
    assert c.get("/approvals").status_code == 401
    assert c.get("/approvals", headers={"X-Staff-Key": "wrong"}).status_code == 401
    assert c.post("/approvals/1/approve", headers={"X-Staff-Key": "wrong"}).status_code == 401


def test_full_cancellation_flow_chat_then_staff_approves():
    cancel = {"appointment_id": 1, "phone": "+1 415 555 0132"}
    c, tools, _ = client([resp(calls=[tc("cancel_appointment", cancel)]),
                          resp("Your cancellation request is pending staff approval.")], seed=True)
    r = c.post("/chat", json={"message": "Please cancel booking 1, phone +1 415 555 0132"}).json()
    assert "pending staff approval" in r["reply"]
    h = {"X-Staff-Key": KEY}
    pending = c.get("/approvals", headers=h).json()["pending"]
    assert [p["appointment_id"] for p in pending] == [1] and "phone" not in pending[0]
    assert c.post("/approvals/1/approve", headers=h).json()["status"] == "cancelled"
    assert tools.state()[0]["status"] == "cancelled"
    assert c.post("/approvals/1/approve", headers=h).status_code == 409   # nothing pending any more


def test_staff_reject_restores_the_booking_and_unknown_id_is_404():
    cancel = {"appointment_id": 1, "phone": "+1 415 555 0132"}
    c, tools, _ = client([resp(calls=[tc("cancel_appointment", cancel)]), resp("It is pending staff approval.")], seed=True)
    c.post("/chat", json={"message": "cancel booking 1, +1 415 555 0132"})
    h = {"X-Staff-Key": KEY}
    assert c.post("/approvals/1/reject", headers=h).json()["status"] == "booked"
    assert c.post("/approvals/99/approve", headers=h).status_code == 404


def test_concurrent_bookings_of_the_same_slot_cannot_double_book():
    tools = ClinicTools(now=lambda: NOW)
    results = []

    def book(i):
        results.append(tools.dispatch("book_appointment", {
            "patient_name": f"Patient Number{chr(65 + i)}", "phone": f"0300111000{i}", "service": "checkup",
            "start": "2026-10-13T10:00", "idempotency_key": f"k{i}"}))

    ts = [threading.Thread(target=book, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(1 for r in results if r.get("ok")) == 1 and len(tools.state()) == 1


def test_home_serves_the_demo_ui():
    c, _, _ = client()
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "BrightSmile Dental" in r.text


def test_cors_only_for_configured_origins():
    c, _, _ = client(cors_origins=["https://demo.example.com"])
    ok = c.get("/health", headers={"Origin": "https://demo.example.com"})
    bad = c.get("/health", headers={"Origin": "https://evil.example.com"})
    assert ok.headers.get("access-control-allow-origin") == "https://demo.example.com"
    assert "access-control-allow-origin" not in bad.headers


# ---------------------------------------------------------------- demo safety nets (public hosting)
def test_daily_cap_pauses_the_demo_without_calling_the_model():
    from clinicagent.api import DEMO_LIMIT_MESSAGE
    c, _, llm = client([resp("Hello one"), resp("Hello two"), resp("never used")], daily_cap=2,
                       demo_video_url="https://example.com/demo")
    codes = [c.post("/chat", json={"message": "hi"}).status_code for _ in range(3)]
    assert codes == [200, 200, 503]
    r = c.post("/chat", json={"message": "hi"})
    assert r.json()["detail"].startswith(DEMO_LIMIT_MESSAGE) and "https://example.com/demo" in r.json()["detail"]
    assert len(llm.calls) == 2  # the paused demo spent no provider quota
    assert c.get("/health").json()["demo_paused"] is True


def test_daily_cap_resets_on_the_next_utc_day():
    day = ["2026-10-17"]
    c, _, _ = client([resp("Hello one"), resp("Hello two")], daily_cap=1, today=lambda: day[0])
    assert c.post("/chat", json={"message": "hi"}).status_code == 200
    assert c.post("/chat", json={"message": "hi"}).status_code == 503
    day[0] = "2026-10-18"
    assert c.post("/chat", json={"message": "hi"}).status_code == 200


def test_per_client_daily_cap_only_blocks_that_client():
    c, _, _ = client([resp("Hello one"), resp("Hello two"), resp("Hello three")], daily_cap_per_client=1, proxy_hops=1)
    post = lambda ip: c.post("/chat", json={"message": "hi"}, headers={"X-Forwarded-For": ip}).status_code
    assert [post("1.1.1.1"), post("1.1.1.1"), post("2.2.2.2")] == [200, 503, 200]


def test_forwarded_for_is_read_from_the_right_so_it_cannot_be_forged():
    c, _, _ = client([resp("Hello one"), resp("Hello two"), resp("Hello three")], daily_cap_per_client=1, proxy_hops=1)
    # the caller invents a different leftmost address each time; the proxy always appends the real one on the right
    post = lambda fake: c.post("/chat", json={"message": "hi"}, headers={"X-Forwarded-For": f"{fake}, 9.9.9.9"}).status_code
    assert [post("6.6.6.6"), post("7.7.7.7")] == [200, 503]


def test_without_trusted_proxy_the_forwarded_header_is_ignored():
    c, _, _ = client([resp("Hello one"), resp("Hello two")], daily_cap_per_client=1)
    post = lambda ip: c.post("/chat", json={"message": "hi"}, headers={"X-Forwarded-For": ip}).status_code
    assert [post("1.1.1.1"), post("2.2.2.2")] == [200, 503]
