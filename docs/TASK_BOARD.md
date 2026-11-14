# TASK BOARD (task-router mode, on top of docs/PROMPT_04_REPAIR_AND_DATA.md)

Rules: cheapest model that is safe for the task. Fable 5.2 = risky work (data loss, wasted calls, locks, rate limits).
Opus 5.5 = multi-file or logic-heavy but not risky. Sonnet 5.5 = docs, small tests, renames, board updates.
Same-model tasks are grouped. After every task: update this file, commit, push. On "continue": read this file first.
Order: open PR A -> time-critical B1, B2 (Recorder must be ready before Mon 16 Nov 09:10 IST) -> C.

| ID | Task | Size | Model | Effort | Status | Next step |
|---|---|---|---|---|---|---|
| W1-W5 | Week mode, speed, progress line, phone folder, live-fact fixes (PROMPT_03) | big | Fable | High | DONE | PR #3 merged |
| V01 | Listed contracts routed to V3 (was: expired endpoint, HTTP 400 UDAPI1021) | medium | Fable | Max | DONE | PR #3 merged |
| A1 | `repair` command, repair windows in the week cursor, listed-key coverage counts for the dated key | medium | Fable | Max | DONE | in PR #4 |
| A2 | Failure policy: UDAPI1021 never permanent, 5 in a row stop the run, batch says ADHURA with a per-item list | medium | Fable | Max | DONE | in PR #4 |
| A3 | reports/day_status.csv + holes.csv (COMPLETE / NOT_YET_SERVED / MISSING_ERROR / NOT_DOWNLOADED) | medium | Fable | Extra High | DONE | in PR #4 |
| A4 | weekday_mismatch: explain, one line per expiry, holiday moves are info | small | Fable | High | DONE | in PR #4 |
| A5 | Run lock, SQLite integrity_check, stale .tmp cleanup | medium | Fable | Max | DONE | in PR #4 |
| B1 | Exchange-side data now: PCR, max pain, OI, change-in-OI, FII/DII (retention ~2 weeks) | medium | Fable | Extra High | DONE | in PR #4 |
| B2 | Recorder v2 (REST): multi-expiry, spot/VIX/future LTP, gaps file, daily summary, shared limiter | medium | Fable | Extra High | DONE | in PR #4 |
| C3 | Fewer calls: whole contract life per call where it helps; optional spot/VIX fast lane from Jan 2022 | medium | Fable | Extra High | DONE | in PR #4 (`history`); whole-life chunks not built, DECISIONS 33 |
| C1 | Per-underlying export: spot_1m.csv, contracts.csv, options_1m/<expiry>.csv, futures_1m.csv, ai_ready parquet, 40 MB split | big | Opus | Extra High | DONE | updl/layout.py; tests/test_v42_layout.py |
| C2 | `coverage` + strategy_ready_days.csv + README_FOR_AI.md | medium | Opus | High | DONE | updl/coverage.py; README_FOR_AI in layout.py; tests/test_v42_layout.py |
| C4 | Owner-file schemas + validators (events, constituents, corporate actions, participant OI); stock lists only from an official page; derived extras | big | Opus | High | DONE (schemas, validators, ISINs) | left: derived extras + spread-report (needs Recorder days); stock lists = owner file |
| R1 | Code review / integration test of the Fable work (A, B) | medium | Opus | High | DONE (V26-V31 fixed, 80 tests) | -- |
| D1 | README table "kya milta hai / kya nahi", CHANGELOG, PR texts, this board | small | Sonnet | High | NEXT | switch to Sonnet: CHANGELOG_v4 (V26-V31), PR #4 text, README check |

Not possible at all (no task): bid/ask history, tick data, old exchange-side PCR/max pain/OI (lost after ~2 weeks).
