# A3: Clinic Appointment Agent

A receptionist agent for a fictional dental clinic ("BrightSmile Dental"). It answers clinic questions, books,
looks up and cancels appointments through tools, and cancellations need a human staff decision. Everything is
sample data.

**Design rule in one line:** the model talks, plain code protects the calendar. Exact facts (ids, phones, times,
opening hours, double booking, idempotency) are enforced in code; questions of meaning (did the patient ask to
cancel? does this reply claim something was done?) are judged by a small LLM judge, with keyword rules only as a
fallback.

> Not production ready and not for real patients. See [Limitations](#limitations).

## What it does

```
patient message -> agent loop (LLM) -> 5 tools -> SQLite mock calendar
                         |                 check_availability, book_appointment, cancel_appointment,
                         |                 lookup_appointment (read only), get_clinic_info (FAQ file)
                         +-> guards G1-G17 + LLM judge (code decides, the model only proposes)
cancel_appointment -> status 'cancel_pending' -> staff approve / reject (NOT a model tool, API key protected)
```

- Hours Mon-Sat 09:00-18:00, lunch 13:00-14:00, Sunday closed, 30-minute grid, one chair, bookings up to 60 days ahead.
- A pending cancellation keeps the slot reserved; approval frees it, rejection keeps it.
- Patient identity is booking id + phone (compared on the last 10 digits, so `0300 1234567` matches `+92 300 1234567`).
  Wrong id and wrong phone give the same error.
- No medical advice (FAQ returns `not_found` for clinical questions); emergencies are escalated.
- Booking and cancellation confirmations are written by code from the database result, not by the model.

## Quick start (PowerShell)

```powershell
cd "E:\Mission 5 AI\ai-studio-scaffold\ai-studio\projects\a3-agent-booking"
pip install -e ".[api,dev]"
pytest -q                                   # no API key needed, scripted fake LLM
python scripts\chat.py --provider groq -v   # needs GROQ_API_KEY or GEMINI_API_KEY in the ai-studio root .env
```

In the terminal chat type `pending` to see cancellation requests and `approve 1` / `reject 1` to act as staff.

### HTTP API, UI and Docker

```powershell
$env:STAFF_API_KEY = "change-me"
uvicorn clinicagent.api:build_app --factory --port 8000     # UI at http://localhost:8000/
```
```powershell
python scripts\vendor_llm_client.py
docker compose up --build                                    # one worker, SQLite volume, trace on; keys from a local .env (copy .env.example)
```

- `POST /chat`: one agent per server-side session, message <= 1000 chars, 20 messages/min per client, text never logged.
- `GET /approvals`, `POST /approvals/{id}/approve|reject`: staff only (`X-Staff-Key`), disabled without `STAFF_API_KEY`.
- Transient LLM failures (429/5xx after retries) return `503` with a friendly message.
- Env: `LLM_PROVIDER`, `CLINIC_DB`, `STAFF_API_KEY`, `CLINIC_JUDGE=on|off`, `CLINIC_RATE_LIMIT`, `CLINIC_DEBUG=1`, `CLINIC_CORS_ORIGINS`, and the public-demo settings below.
- UI: patient chat, "Behind the scenes" (tool and guard names only, no message text), "Front desk" approvals.
  To host the page elsewhere open it with `?api=https://your-api` and set `CLINIC_CORS_ORIGINS`.
- Keys are read at run time and are not baked into the image.

### Public demo safety (before hosting this anywhere)

A public URL spends your own provider quota, so the API has safety nets, all off by default:

| Env var | Effect |
|---|---|
| `CLINIC_DAILY_CAP=300` | after 300 chat messages per UTC day (whole server) `/chat` answers `503` with a "demo paused, watch the video" message and makes **no model call** |
| `CLINIC_DAILY_CAP_PER_CLIENT=40` | the same limit per client address, so one visitor cannot use the whole budget |
| `CLINIC_PROXY_HOPS=1` | number of trusted reverse proxies in front (hosts such as Render use 1). Limits then use the real client address, read from the right of `X-Forwarded-For` (the left side can be forged). Leave at 0 when there is no proxy |
| `CLINIC_DEMO_VIDEO_URL` | link added to the "paused" message |

Limits of this design: the counters live in memory, so they reset when a free host restarts or wakes the process, and a
small cap is only a safety net, not accounting. A real kill-switch belongs in front of the provider keys (a gateway) or in
the provider's own spending limits. One chat message costs several model calls (agent loop plus judge), so set the cap
from your provider's daily token limit, not from the message count alone.

## Code-level guards (`agent.py`)

| Guard | Rule |
|---|---|
| G1 | emergency keywords bypass the LLM |
| G2 | idempotency key derived in code |
| G3 | malformed tool arguments never run |
| G4 | max steps per turn |
| G5 | cancel replies must say "pending staff approval" |
| G6 | approve/reject are not model tools |
| G7 | the model can never pick a booking time (patient stated it, or it was offered earlier) |
| G8 | booking blocked until `check_availability` ran for that date and service |
| G9 | an identical tool call that already failed this turn is not run again |
| G10 | a reply claiming a booking/cancellation without a successful tool call is repaired once, then blocked |
| G11 | empty or garbage reply is repaired once, then replaced by a safe fallback |
| G12 | `cancel_appointment` only runs if the patient expressed cancel intent (LLM judge, keywords as fallback) |
| G13 | confirmation text after booking / cancellation request is written by code |
| G14 | old availability lists become stale when the calendar changes (calendar `version`) |
| G15 | a reply listing times must follow a `check_availability` call in the same turn |
| G16 | no promise that staff will call or follow up (only cancellation requests reach staff) |
| G17 | a reply about an existing appointment states the real time from `lookup_appointment`, else code writes it |

The judge (`judge.py`) returns only a JSON verdict that code enforces; it never acts and sees the conversation as quoted
data. If it is unavailable the agent falls back to keyword rules and records `judge_unavailable`.

## How it was tested, and what the numbers mean

| Layer | Result | Honest reading |
|---|---|---|
| `pytest` (fake LLM, no key, also run in CI) | **194 passed** | proves code paths and guards, not model quality |
| Programmatic suite (`scripts/run_suite.py`, 76 tests: info, main flow, identity, slots, validation, safety, API, tool layer) | First clean full run on Groq `gpt-oss-120b` (step 22 code): **74/76 PASS**, 240,865 tokens. The 2 failures: I5 (a wording check that rejected a curly apostrophe: test bug) and C9 (real bug: judge false positive plus a misleading repair note). After the step 23 fixes: I5, I7, F-MAIN, C1, C6-C9 PASS on Groq (C9 passed in 2 further separate Groq runs) and C6-C9 also on Gemini `gemini-3.5-flash-lite`. | The two failed tests were re-run on the fixed code, not the whole suite: no single full run exists on the final code. Earlier "71/71" runs were assembled from pieces on older builds |
| Dev scenarios (30) | Groq: 30/30 before step 22; 26/30 after it, the 4 misses (C01-C04) were HTTP 429 quota errors, not agent failures. Gemini `gemini-3.5-flash-lite`: C01-C04 4/4 on the final code | dev set was used for tuning: a regression check, not an accuracy score |
| Held-out sets v1-v3 | burned (fixes were made from their failures) | do not quote them as a score; a fresh set is needed for a blind number |

The suite was written after seeing the failures it covers, so it is a regression suite, not a blind benchmark.
Token use is saved per test (the main cancel-and-rebook flow is about 52k tokens on Groq), which is why a full run plus
development work exceeds the Groq free-tier daily limit of 200k tokens.

Failures found in real runs, their causes and fixes are in [`docs/FAILURE_ANALYSIS.md`](docs/FAILURE_ANALYSIS.md)
(18 rows, for example: a cancelled-and-approved slot not offered again, the model copying stale time lists,
false "staff will call you" promises, a missing lookup tool). Guards observed firing on the real model include
booking/cancellation templating, `unverified_slot_list` repair, `availability_not_checked`, `judge_overruled_regex`,
`judge_caught:false_booking_claim` and `emergency_bypass`.

### Reproduce

```powershell
python scripts\run_suite.py --no-model                              # API + calendar rules, no quota
python scripts\run_suite.py --provider groq --wait-quota 10         # everything, waits when the daily quota is hit
python scripts\run_suite.py --provider groq --only 2,C1,T10         # sections or ids
python scripts\run_suite.py --provider groq --resume results\suite_groq_openai_gpt-oss-120b_20261014-1355   # re-run only ERROR / RATE_LIMIT / SKIPPED
python scripts\run_eval.py  --provider groq --set dev --only C01,C02,C03,C04
python scripts\run_eval.py  --provider gemini --set dev --sleep 2
python scripts\make_report.py                                        # -> docs\RESULTS.md
```

Each suite test saves `results/suite_<provider>_<model>_<time>/<ID>.json` (every message, tools, guards fired, checks,
tokens, final database rows) and a `SUMMARY.md`. Verdicts: PASS, FAIL, ERROR, RATE_LIMIT (not scored),
KNOWN_GAP_CONFIRMED / KNOWN_GAP_FIXED, SKIPPED. Some checks are wording heuristics, so the summary lists replies a
person should read. Fixed clock: suite Wed 2026-10-14 08:00, dev eval Mon 2026-10-12 08:00.
Rule for held-out scoring: if you fix something because of a held-out failure, write a fresh set before quoting a score.

## Docs

- [`docs/FAILURE_ANALYSIS.md`](docs/FAILURE_ANALYSIS.md): every failure seen, root cause, fix, what is still weak.
- [`docs/MANUAL_TEST_PLAN.md`](docs/MANUAL_TEST_PLAN.md): the manual checks the suite automates.
- [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md): 4-minute demo video script and prep list.
- `docs/RESULTS.md`: generated eval table, do not edit by hand.
- CI: `.github/workflows/a3-ci.yml` runs the tests (no keys) and checks the committed scenario files match `scripts/make_scenarios.py`.

## Limitations

- Sample data only; no real patient data, no real verification. Identity is booking id + phone (last 10 digits), no one-time code.
- Emergency detection (G1) is a keyword list; paraphrases rely on the model. G7 and G16 are regex rules; the judge can be wrong.
- No reschedule tool (cancel, then book again). Staff approval is an API/UI action, not a notification queue.
- One worker process (SQLite plus an in-process lock). Several workers need Postgres with a unique constraint on
  slot, a shared session store and a shared rate limiter.
- Test sets are small and written by the author. Groq's free tier cannot hold repeated full runs.
- UI-only checks are manual: layout at phone width, "Front desk" wording, staff key forgotten after reload.

## Roadmap

- [x] A3.1 mock calendar, tools, FAQ, scenario sets
- [x] A3.2 agent loop on `llm-client` with code-level guards
- [x] A3.3 eval runner (tool-call correctness, final state, behaviour checks)
- [x] A3.4 provider comparison, LLM judge, held-out discipline, `docs/RESULTS.md`
- [x] A3.5 FastAPI, Dockerfile, compose, CI (Docker image built and run by the author)
- [x] A3.6a demo UI served at `/`
- [x] A3.6b README, failure analysis, manual test plan, programmatic suite, demo script
- [ ] A3.6b remaining: one last full-suite run on the final code (optional, uses ~240k Groq tokens; `--fallback gemini` covers the quota), public hosting (single-worker host with a persistent
  volume), recorded video, short write-up, a fresh blind held-out set run once
