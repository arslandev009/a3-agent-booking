"""Real-provider check of failover: a conversation that starts on one provider and finishes on the other.

    python scripts\\check_failover.py                    # groq -> gemini, then gemini -> groq
    python scripts\\check_failover.py --a groq --b gemini

Each scenario: turn 1 is answered by provider X; X is then put on cooldown (as if its quota ran out); turns 2 and 3 must
be answered by provider Y, using the tool-call history X produced, and the booking must exist at the end. This is the
part unit tests cannot prove: whether each real provider accepts the other's conversation history.
Uses a few thousand tokens per scenario. Nothing is saved.
"""
from __future__ import annotations

import argparse

from clinicagent import suite
from clinicagent.agent import ClinicAgent
from clinicagent.failover import FailoverLLM
from clinicagent.judge import Judge
from clinicagent.tools import ClinicTools

TURNS = ["Book a cleaning for tomorrow morning", "09:30", "Akbar Khan 03001234567"]


def scenario(first: str, second: str) -> bool:
    from llm_client.client import LLM
    llm = FailoverLLM([LLM(first), LLM(second)])
    tools = ClinicTools(now=lambda: suite.NOW)
    agent = ClinicAgent(llm, tools, judge=Judge(llm))
    print(f"\n=== starts on {first}, continues on {second}")
    served_by: list[set[str]] = []
    for i, text in enumerate(TURNS):
        if i == 1:
            llm.force_cooldown(first, 600)  # as if its quota had just run out
        before = len(llm.served)
        try:
            turn = agent.reply(text)
            reply = turn.reply
        except Exception as exc:  # noqa: BLE001
            reply = f"<ERROR {type(exc).__name__}: {str(exc)[:200]}>"
        served_by.append(set(llm.served[before:]))
        print(f"  patient: {text}\n  reply  : {reply[:220]}\n  served : {sorted(served_by[-1])}")
    booked = [r for r in tools.state() if r["status"] == "booked"]
    ok = served_by[0] == {first} and all(s == {second} for s in served_by[1:]) and len(booked) == 1
    print(f"  booked rows: {len(booked)}   ->  {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="groq")
    ap.add_argument("--b", default="gemini")
    args = ap.parse_args()
    results = [scenario(args.a, args.b), scenario(args.b, args.a)]
    print("\nfailover check:", "PASS" if all(results) else "FAIL (read the replies above)")


if __name__ == "__main__":
    main()
