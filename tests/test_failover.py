"""Provider failover: fake providers and a fake clock, no network."""
import pytest

from clinicagent.failover import FailoverLLM, ProvidersBusy, failoverable, suggested_wait_seconds


class RateLimitError(Exception):
    status_code = 429


class AuthenticationError(Exception):
    status_code = 401


class Fake:
    def __init__(self, provider, script):
        self.provider, self.model, self.script, self.calls = provider, provider + "-m", list(script), 0

    def chat(self, messages, **kw):
        self.calls += 1
        item = self.script.pop(0) if self.script else "ok"
        if isinstance(item, Exception):
            raise item
        return f"{self.provider}:{item}"


class Clock:
    t = 1000.0
    def __call__(self):
        return self.t


def pair(a_script=(), b_script=()):
    clock = Clock()
    a, b = Fake("groq", a_script), Fake("gemini", b_script)
    return FailoverLLM([a, b], clock=clock), a, b, clock


def test_uses_the_first_provider_while_it_works():
    f, a, b, _ = pair()
    assert f.chat([]) == "groq:ok" and a.calls == 1 and b.calls == 0 and f.last_provider == "groq"


def test_quota_error_moves_the_same_call_to_the_next_provider():
    f, a, b, _ = pair([RateLimitError("429 daily quota, try again in 3h")])
    assert f.chat([]) == "gemini:ok" and f.last_provider == "gemini"
    assert f.status()["cooling"] == {"groq": 3600}  # a 3 hour hint is capped at one hour, then groq is tried again
    f.chat([])
    assert a.calls == 1  # groq is not called again while cooling down


def test_preferred_provider_comes_back_after_its_cooldown():
    f, a, b, clock = pair([RateLimitError("429")])
    f.chat([])
    clock.t += 61
    assert f.chat([]) == "groq:ok" and f.last_provider == "groq"


def test_when_the_second_runs_out_the_first_takes_over_again():
    f, a, b, clock = pair([RateLimitError("429")], ["ok"])
    f.chat([])                    # groq fails, gemini answers
    clock.t += 61                 # groq is back
    f.force_cooldown("gemini", 300)
    assert f.chat([]) == "groq:ok"


def test_cooldown_grows_when_a_provider_keeps_failing():
    f, a, b, clock = pair([RateLimitError("429")] * 3)
    waits = []
    for _ in range(3):
        f.chat([])
        waits.append(f.status()["cooling"]["groq"])
        clock.t += 100000
    assert waits == [60, 120, 240]


def test_both_failing_raises_the_real_error_and_both_cool_down():
    f, a, b, _ = pair([RateLimitError("429 a")], [RateLimitError("429 b")])
    with pytest.raises(RateLimitError, match="429 b"):
        f.chat([])
    with pytest.raises(ProvidersBusy) as e:
        f.chat([])                # nobody is called now
    assert e.value.status_code == 503 and a.calls == 1 and b.calls == 1


def test_auth_failure_also_fails_over_but_a_bad_request_does_not():
    f, a, b, _ = pair([AuthenticationError("bad key")])
    assert f.chat([]) == "gemini:ok"
    g, c, d, _ = pair([ValueError("malformed request")])
    with pytest.raises(ValueError):
        g.chat([])
    assert d.calls == 0 and g.status()["cooling"] == {}


def test_wait_hints_from_both_providers_are_parsed():
    assert suggested_wait_seconds("Please try again in 4m32.16s") == pytest.approx(272.16)
    assert suggested_wait_seconds("Please retry in 28.5s.") == 28.5
    assert suggested_wait_seconds('"retryDelay": "28s"') == 28.0
    assert suggested_wait_seconds("nothing here") is None


def test_failoverable_classification():
    assert failoverable(RateLimitError("x")) and failoverable(AuthenticationError("x")) and failoverable(ProvidersBusy("x"))
    assert not failoverable(ValueError("x")) and not failoverable(KeyError("x"))


def test_provider_and_model_names_describe_the_chain():
    f, *_ = pair()
    assert f.provider == "failover(groq>gemini)" and f.model == "groq-m,gemini-m"


def test_api_keeps_answering_when_the_first_provider_is_out_of_quota():
    from fastapi.testclient import TestClient
    from clinicagent.agent import ClinicAgent
    from clinicagent.api import create_app
    from clinicagent.tools import ClinicTools
    from test_agent import NOW, resp

    class Quota(Fake):
        def chat(self, messages, **kw):
            raise RateLimitError("429 try again in 2h")

    class Ok(Fake):
        def chat(self, messages, **kw):
            self.calls += 1
            return resp("Hello from the second provider")

    llm = FailoverLLM([Quota("groq", []), Ok("gemini", [])])
    app = create_app(llm, ClinicTools(now=lambda: NOW), use_judge=False,
                     agent_factory=lambda l, t: ClinicAgent(l, t, retries=0))
    c = TestClient(app)
    r = c.post("/chat", json={"message": "hi"})
    assert r.status_code == 200 and r.json()["reply"] == "Hello from the second provider"
    h = c.get("/health").json()
    assert h["llm"]["ready"] == ["gemini"] and "groq" in h["llm"]["cooling"]


def test_all_providers_cooling_gives_the_friendly_503():
    from fastapi.testclient import TestClient
    from clinicagent.agent import ClinicAgent
    from clinicagent.api import BUSY_MESSAGE, create_app
    from clinicagent.tools import ClinicTools
    from test_agent import NOW

    f, a, b, _ = pair([RateLimitError("429")], [RateLimitError("429")])
    app = create_app(f, ClinicTools(now=lambda: NOW), use_judge=False,
                     agent_factory=lambda l, t: ClinicAgent(l, t, retries=0))
    c = TestClient(app)
    assert c.post("/chat", json={"message": "hi"}).json()["detail"] == BUSY_MESSAGE
    assert c.post("/chat", json={"message": "hi again"}).json()["detail"] == BUSY_MESSAGE
