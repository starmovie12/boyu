# TASK BOARD (task-router mode, on top of docs/PROMPT_04_REPAIR_AND_DATA.md)

Rules: cheapest model that is safe for the task. Fable 5.2 = risky work (data loss, wasted calls, locks, rate limits).
Opus 5.5 = multi-file or logic-heavy but not risky. Sonnet 5.5 = docs, small tests, renames, board updates.
Same-model tasks are grouped. After every task: update this file, commit, push. On "continue": read this file first.
Order: open PR A -> time-critical B1, B2 (Recorder must be ready before Mon 16 Nov 09:10 IST) -> C.

| ID | Task | Size | Model | Effort | Status | Next step |
|---|---|---|---|---|---|---|
| W1-W5 | Week mode, speed, progress line, phone folder, live-fact fixes (PROMPT_03) | big | Fable | High | DONE | in PR #3 (open, not merged) |
| V01 | Listed contracts routed to V3 (was: expired endpoint, HTTP 400 UDAPI1021) | medium | Fable | Max | DONE | in PR #3 |
| A1 | `repair` command, repair windows in the week cursor, listed-key coverage counts for the dated key | medium | Fable | Max | IN-PROGRESS | write updl/repair.py + test (1,000 failures, 100 DONE) |
| A2 | Failure policy: UDAPI1021 never permanent, 5 in a row stop the run, batch says ADHURA with a per-item list | medium | Fable | Max | TODO | after A1 |
| A3 | reports/day_status.csv + holes.csv (COMPLETE / NOT_YET_SERVED / MISSING_ERROR / NOT_DOWNLOADED) | medium | Fable | Extra High | TODO | after A2 |
| A4 | weekday_mismatch: explain, one line per expiry, holiday moves are info | small | Fable | High | TODO | after A3 |
| A5 | Run lock, SQLite integrity_check, stale .tmp cleanup | medium | Fable | Max | TODO | after A4; then update PR #3 text |
| B1 | Exchange-side data now: PCR, max pain, OI, change-in-OI, FII/DII (retention ~2 weeks) | medium | Fable | Extra High | TODO | new allowlisted GET paths + updl/marketinfo.py + tests |
| B2 | Recorder v2 (REST): multi-expiry, spot/VIX/future LTP, gaps file, daily summary, shared limiter | medium | Fable | Extra High | TODO | must not break `record`; ready before Mon 16 Nov 09:10 IST |
| C3 | Fewer calls: whole contract life per call where it helps; optional spot/VIX fast lane from Jan 2022 | medium | Fable | Extra High | TODO | measure calls per week before/after |
| C1 | Per-underlying export: spot_1m.csv, contracts.csv, options_1m/<expiry>.csv, futures_1m.csv, ai_ready parquet, 40 MB split | big | Opus | Extra High | TODO | switch model before starting |
| C2 | `coverage` + strategy_ready_days.csv + README_FOR_AI.md | medium | Opus | High | TODO | after C1 |
| C4 | Owner-file schemas + validators (events, constituents, corporate actions, participant OI); stock lists only from an official page; derived extras | big | Opus | High | TODO | after C2 |
| R1 | Code review / integration test of the Fable work (A, B) | medium | Opus | High | TODO | with the C group |
| D1 | README table "kya milta hai / kya nahi", CHANGELOG, PR texts, this board | small | Sonnet | High | TODO | at the end of each PR |

Not possible at all (no task): bid/ask history, tick data, old exchange-side PCR/max pain/OI (lost after ~2 weeks).
