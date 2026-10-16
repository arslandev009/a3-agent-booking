"""Try the agent in the terminal with a real provider.

    python scripts\\chat.py --provider groq
    python scripts\\chat.py --provider gemini --fixed-clock   # Mon 2026-10-12 08:00, same as the eval set

Uses clinic_demo.db (git-ignored). Type 'quit' to exit, 'pending' to list cancellations waiting for staff,
'approve <id>' / 'reject <id>' to act as clinic staff.
"""
from __future__ import annotations

import argparse
from datetime import datetime

from llm_client.client import LLM

from clinicagent.agent import ClinicAgent
from clinicagent.judge import Judge
from clinicagent.tools import ClinicTools


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None, help="groq | gemini | openai | local (default: LLM_PROVIDER)")
    ap.add_argument("--fixed-clock", action="store_true")
    ap.add_argument("--db", default="clinic_demo.db")
    ap.add_argument("--no-judge", action="store_true", help="regex-only guards (no LLM judge)")
    ap.add_argument("--verbose", "-v", action="store_true", help="show tool calls")
    a = ap.parse_args()

    now = (lambda: datetime(2026, 10, 12, 8, 0)) if a.fixed_clock else datetime.now
    tools = ClinicTools(a.db, now=now)
    llm = LLM(a.provider)
    agent = ClinicAgent(llm, tools, judge=None if a.no_judge else Judge(llm))
    print(f"Clinic agent ready ({agent.llm.provider}/{agent.llm.model}, judge {'off' if a.no_judge else 'on'}). Type 'quit' to exit.")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"quit", "exit"}:
            break
        if text.lower() == "pending":
            print([r for r in tools.state() if r["status"] == "cancel_pending"])
            continue
        if text.lower().startswith(("approve ", "reject ")):
            cmd, _, num = text.partition(" ")
            fn = tools.approve_cancellation if cmd.lower() == "approve" else tools.reject_cancellation
            print(fn(int(num)))
            continue
        turn = agent.reply(text)
        if a.verbose:
            for c in turn.tool_calls:
                print(f"  [tool] {c.name}({c.args}) -> {c.result}")
            if turn.guards:
                print(f"  [guards] {turn.guards}")
        print(f"bot> {turn.reply}")


if __name__ == "__main__":
    main()
