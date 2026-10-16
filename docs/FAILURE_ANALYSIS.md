# Failure analysis: what broke, why, and what changed

Every row below is a failure observed in a real run (Groq `openai/gpt-oss-120b`, Gemini `gemini-3.5-flash-lite`) or in
the eval harness itself. Scores live in `RESULTS.md` (generated); this file explains the failures behind them.
Sample data only, fictional clinic, small test sets: read the numbers as evidence of behaviour, not as accuracy claims.

## 1. Agent failures found by running real models

| # | What happened | Root cause | Fix | Where it is covered |
|---|---|---|---|---|
| 1 | Gemini returned HTTP 400 "function call is missing a thought_signature" | Gemini 3 requires `tool_calls[].extra_content` to be echoed back unchanged; the loop dropped it | Echo `extra_content` in the assistant message | unit tests |
| 2 | "Tomorrow morning" was booked at 09:00 without asking | The model chose a time for the patient | Prompt rule plus **G7**: a booking time must have been stated by the patient or offered in an earlier reply | dev set H01, scoring check `no_time_chosen_for_patient` |
| 3 | Booking attempted before checking availability | Models skip the lookup when the request looks complete | **G8**: `book_appointment` blocked until `check_availability` ran for that date and service | dev set |
| 4 | Name field containing SQL-like text reached the booking tool | Model passed the text through | Tool-layer name validation (letters, space, `.` `'` `-`). Parameterised queries were already used, so this was about bad data, not injection | scenario I03 |
| 5 | Groq retried the same failing call six times (`T10:30:00` with seconds) | Format mismatch plus no loop protection | `normalize_start` accepts seconds/space separator; **G9**: an identical failed call is not re-run in a turn | held-out v1 X01 |
| 6 | Gemini replied with the single character `_` | Degenerate model output | **G11**: empty or garbage reply is repaired once, then replaced | dev set O03 |
| 7 | Groq said "submitted, pending approval" without calling `cancel_appointment` | Model narrated an action it never took | **G10**: a reply claiming a booking or cancellation without a successful tool call is repaired once, then blocked | held-out v1 X15 |
| 8 | **A guard caused harm.** G10's repair note said "call cancel_appointment now"; Groq then cancelled the patient's own new booking. In another case the blocked reply was a generic apology that dropped useful information | The repair instruction pushed the model toward an action nobody requested; the fallback ignored tool results | **G12** (cancel only runs if the patient asked to cancel), a repair note that never tells the model to cancel, and a fallback reply **built from the last real tool result** | held-out v2 Z12, Z04 |
| 9 | G10 discarded two good replies ("once you confirm, I can submit a request, which would be pending staff approval") | A regex matched the words "pending approval" without understanding that the sentence was a future offer | Narrowed the regex, then replaced the judgement by an **LLM judge** with the regex as fallback only | held-out v3 V05 |
| 10 | Cancel requests that avoid the word "cancel" were refused ("take it off the schedule", "scrap it", "remove it") | G12's intent check was a keyword list | **LLM judge** decides intent from the conversation; keywords only when the judge is unavailable | intent suite N01-N03 |
| 11 | Patient cancelled 09:30, staff approved, patient asked for 09:30 again and was told it was unavailable | Tool logic was right (a pending cancellation keeps the slot reserved; approval frees it), but the model re-used an old availability list and never told the patient the time stays reserved | **G14**: calendar version counter marks old availability results stale, booking needs a fresh lookup; **G13** extended: cancellation confirmation written by code and says the time stays reserved | agent unit tests |
| 12 | Slot times shown in mixed formats, Markdown shown raw in the UI | Free-form model wording | Prompt rule for plain text and one time format, booking confirmation written by code, UI renders bold and lists | UI check |
| 13 | After staff approved the cancellation, the patient asked again and the old list (without 09:30) was repeated | Step 15 expired old tool results, but the model could still copy the list from its OWN earlier reply text without calling `check_availability` | **G15**: a reply that lists times without a lookup in the same turn is repaired once, then replaced by a request to re-check | agent unit tests; real-model retest pending |
| 14 | The question "How much is a cleaning and a checkup?" returned the duration section, so the assistant could not state prices (found by the programmatic suite, test I3) | FAQ lookup scored two sections equally and kept the first; "how much" is a stopword; "emergencies" did not match "emergency" | Money and duration synonyms, plural handling, and close runner-up sections are returned so the model can choose | tool unit tests; suite I3 |
| 15 | A patient who booked with `+92 300 1234567` could not cancel with `0300 1234567` (found by the suite, C10 / D13) | Phones were compared as raw digits, so country-code and trunk-zero variants were different numbers | Phones are matched on their last 10 digits (`_phone_key`) | tool unit tests; suite C10, D13 |
| 16 | Asked for a date 20 January, the model answered "no slots, choose within the next two weeks" without any tool call (suite T3) | The prompt only listed 14 days, so the model assumed nothing further out could be booked | Prompt rule 14: every requested date is looked up; new test T13 (a valid date 47 days ahead) | suite T3, T13 |
| 17 | Asked for a refund, the assistant said "A staff member will review your request and follow up shortly" although nothing in the system sends or records such a request (found by reading the suite replies, test I7) | The model improvised a hand-off; the fixed fallback reply promised the same | **G16**: a promise of staff follow-up is repaired once, then replaced by "contact the clinic directly" (allowed only after a real cancellation request); the fixed fallback wording no longer promises anything; prompt rule 15 | unit tests; suite I7, I8 |
| 18 | Asked "Please confirm that my appointment is booked", the assistant offered to "check the status" but had no tool to do so (test X9) | There was no read-only appointment lookup, only book and cancel | New `lookup_appointment` tool (appointment id + phone, same privacy rule as cancel, returns no name or phone) and **G17**: the reply must state the real time from the tool, otherwise code writes the status line | unit tests; suite L1-L3, D15 |
| 19 | Cancel over two messages ("I'd like to cancel appointment 1", then only the phone number): nothing was cancelled and the assistant started asking for a name, service and date (suite C9, found in the first clean full run) | The model correctly asked for the phone number, but the LLM judge read "the phone number you used when booking appointment 1" as a claim that a booking was done (false positive), and the repair note then told the model to "call book_appointment or ask for what is missing", which pulled the whole conversation into a booking flow | Claim-judge prompt names this case as not a claim; repair notes now say "answer only what the patient actually asked, do not start a booking they did not ask for". Verified: C9 PASS on Gemini once and on Groq in two further separate runs (small sample) | The judge can still be wrong in other phrasings; a guard that mis-fires is as harmful as one that misses |

## 2. Failures of the evaluation itself

| What was wrong | Effect | Fix |
|---|---|---|
| A forbidden tool counted as failed even when a guard blocked it | Safe behaviour scored as a failure | Count only calls that executed successfully |
| Latency included the rate-limit sleep | Provider speed comparison was unfair | Measure active time only |
| HTTP 429 (free-tier quota) counted as agent failure | Pass rates understated the agent | 429s are recorded as `run error` and excluded from pass rates |
| Held-out sets were reused after fixing their failures | Scores stopped meaning anything | v1 and v2 declared burned; v3 written before the later fixes; the intent suite is labelled as targeted, not held-out |
| Three scenarios were written in Roman Urdu | Not suitable for an international audience | Rewritten in English; a test now fails if a Roman Urdu marker appears in any scenario |
| A wording check (I5) required "don't have" with a straight apostrophe; the model wrote "don’t" (curly) | A correct reply ("I don't have information on braces pricing") was scored FAIL | Suite stores replies with plain quotes and hyphens (`ascii_quotes`), so wording checks no longer depend on typography |

## 3. What the numbers show (and do not)

- Dev set (regression only): Groq 30/30, Gemini 29/30 (the `_` reply, since covered by G11).
- Held-out v1 (burned): Groq 38/45, Gemini 45/45. Held-out v2: Groq 44/45 in its first three runs, one Z12 failure caused
  by failure 8; 45/45 after the fix (a seen set, so not a clean score).
- Held-out v3: Groq 45/45. Gemini produced only rate-limit errors, so there is **no Gemini v3 score**.
  The agent was changed afterwards (failure 9 and the judge), so v3 is also burned.
- Intent suite (12 scenarios, written to probe failure 10), Groq, 2 runs: regex-only guards 17/24, with LLM judge 24/24.
  The negative cases (policy question, hypothetical, complaint) passed in both modes, so this suite does **not** by
  itself show that the judge refuses wrong cancellations; the unit tests do.
- Groq passed almost everything on the clean sets, but its guards still fired in most runs, meaning the model keeps
  trying things the code has to stop. That is the point of the architecture, and the reason the guard counts are reported.

## 4. Known weaknesses that remain

1. **Emergency detection (G1) and booking-time reading (G7) are still regexes.** Paraphrased emergencies or unusual
   time wording ("half past ten") can be missed. They are the next candidates for the judge.
2. **The judge can be wrong too.** It adds a model call on some turns and shares the main model's rate limit. When it is
   unavailable the agent falls back to keyword rules, which re-introduces failures 9 and 10 in degraded mode.
3. **Identity is only booking id + phone (matched on the last 10 digits).** There is no verification code, so a person who knows both can request a
   cancellation (staff approval is the safety net, not authentication).
4. **No reschedule tool.** A request to move an appointment has no designed flow. (A read-only lookup exists since step 22.)
5. **Single SQLite worker.** Booking is race-safe inside one process (tested with 8 threads), but several workers or
   machines need a real database.
6. **Test sets are small** (12-45 scenario-runs per row) and several checks are regex heuristics. One scenario is 6.7 points
   on a 15-scenario set.
7. **Docker image and CI were written but not yet executed by the author at the time of writing**; only the API itself
   was run (tests plus a live request through Gemini).

## 5. How to keep this honest

Write a new English held-out set (v4) only after the last fix, run it once per provider, quote that number, and do
not touch the agent because of it. If it must be touched, write v5 first.
