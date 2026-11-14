# Audit v4 (14 Nov 2026): week mode, speed, progress, phone folder

Findings made while changing the code and on two real end-to-end runs with the owner's Plus analytics token
(read-only). Severity: Critical / High / Medium / Low. Tests: `tests/test_v4_week.py` unless noted.

| ID | Sev | Where (v3.1) | What was wrong | Evidence | Status |
|---|---|---|---|---|---|
| V01 | Critical | plan.py `build_selection_plan` | Every catalog contract was treated as "expired", also TODAY's listed ones. Keys like `NSE_FO\|61471` went to the expired endpoint: HTTP 400 UDAPI1021, so every still-listed option and future failed (FAILED_PERMANENT) and every day became NOT OK | first real run: 278 split retries, 978 failed chunks; mocked transports accepted any key | fixed; the v4 test transport rejects a key on the wrong endpoint |
| V02 | High | weekly/monthly window end | The newest session is not in the historical API for some hours after the close (13 Nov empty at 01:25 on 14 Nov). The old code would have stored the day as EMPTY_OK = covered for ever | LIVE-14NOV L1; probe P02/P03/P07/P08 FAIL with HTTP 200 on the owner's phone | fixed: `data_through` check, pending days are never covered; validator flag OPT_PARTIAL_DAY |
| V03 | High | monthly.py | One `continue` = one calendar month (about 3,500 calls) and no output for the whole run | owner's log | fixed: one week per run, live progress line |
| V04 | High | plan.py estimate | "Est. time" = calls / rate ignored the 30-minute cap (3,539 calls at --rate 60 printed 59 min, really about 103) | test `test_estimate_models_the_30_minute_cap...` | fixed: `estimate_minutes` |
| V05 | High | plan.py D06/D08 | Band and wide chain planned the same contract-days twice | plan output 1146 + 1780 | fixed: a day planned once per run; derive/export read both tables |
| V06 | High | derive.py | One root-finder call and several pandas row objects per option row: minutes per week on a phone | profile | fixed: numpy bisection + matrix as-of OI (`ivfast.py`, `chain.oi_matrices`); equality tests against the scalar code |
| V07 | High | store/validate/derive | Every run re-hashed all raw files and re-read the whole store | code | fixed: `since_utc` / `window` (only the batch) |
| V08 | Medium | reference/globals.yaml | "Oil Brent"/"Oil WTI" never matched the master names "Oil (Brent)"/"Oil (WTI)": crude was never downloaded, and a made-up key was requested | LIVE L14 | fixed: the 13 real names; unresolved names are reported, not requested |
| V09 | Medium | probe.py P14 | asked the futures list of a weekly expiry (always empty) and reported FAIL | LIVE L2 | fixed: monthly expiry |
| V10 | Medium | probe.py P19 | reported the ladder's deepest step (2025-10-07) as "oldest option date" | real probe now says 2024-10-03 | fixed: the first expiry of the list is tested too |
| V11 | Medium | probe report | "Usable Window" showed the fixed manifest window next to a newer "Recent Trading Day" | owner's report | fixed: relabelled; newest day with data + pending days shown |
| V12 | Medium | plan.py D07 | estimate counted every expiry ever seen (593 for one month); lists of expired expiries were asked again on every run | owner's log | fixed: cached in the catalog, real estimate |
| V13 | Medium | plan.py masters | instrument masters were never refreshed after the first download (new weekly contracts missing) | code | fixed: refreshed once per IST day |
| V14 | Medium | validate.py | a failed chunk that was fetched later under another range kept its failure row and marked the day NOT OK for ever | real run 2 | fixed: only still-uncovered days count |
| V15 | Medium | holidays/sessions | tables cover 2026 only; 2024-2025 weekday holidays were treated as trading days | LIVE L11/L12 | fixed: per-day facts from `/v2/market/timings/{date}` (cache), TABLE_GAP when neither API nor table knows the day |
| V16 | Medium | state.py | two fsync commits per chunk | code | fixed: one commit per second during parallel download |
| V17 | Low | monthly.py | `continue --dry-run` wrote the cursor | code | week mode writes nothing in dry-run (old month mode unchanged) |
| V18 | Low | client.py | a 429 paused only the thread that got it | code | fixed: `limiter.pause` holds every worker; slow ramp back after a halving |
| V19 | Low | specs.yaml | NIFTY lot 75 "from 2025-01-02" is wrong for the 30 Jan 2025 expiry (API says 25) | LIVE L6 | table note corrected; code uses each contract's own lot |
| V20 | High | plan.py `_fetch_chunk` | every 4xx except UDAPI100500 became FAILED_PERMANENT; a wrong-endpoint answer (UDAPI1021) was split into single days first (the owner's month run burned about 1,800 calls) and then kept for ever | owner's state: 1,797 FAILED_PERMANENT, all 400:UDAPI1021 | fixed: UDAPI1021 is retryable, never split; 5 in a row on one endpoint stop the run (`RoutingSuspected`) |
| V21 | High | (missing) | no way to re-queue failed chunks; the cursor moved on and holes stayed | owner's CSVs (F2) | fixed: `repair` command + `repair_days` in the week cursor; good chunks untouched (test with 1,000 failures / 100 DONE) |
| V22 | High | weekly.py final box | a batch with failed chunks still said POORA | code | fixed: ADHURA (INCOMPLETE) with a per-item list; `reports/day_status.csv`, `holes.csv` |
| V23 | Medium | (missing) | two `continue` processes could write one state; no check after a power loss | owner (F4) | fixed: `state/run.lock` (PID, start time, stale detection), SQLite `integrity_check`, `.tmp` cleanup |
| V24 | Low | master.py `listed_contracts` | `weekday_mismatch` was logged once per contract (1,383 lines). Meaning: the contract's expiry is not on the rule's weekday. Almost all were the monthly expiry moved from the 24 Nov holiday to Mon 23 Nov, which is expected | first live run | fixed: one line per expiry; a holiday move is `expiry_moved_holiday`, anything else stays `weekday_mismatch` |
| V25 | Medium | plan.py coverage | a contract fetched while listed (key `SEG\|token`) would be fetched again after expiry under its dated key | code | fixed: listed-key days count for the dated key |

## Measured (sandbox, real API, 4 workers, 240 calls/min)
See `docs/CHANGELOG_v4.md`.

## Second pass over the diff (self-review, no separate agent)
See the end of `docs/CHANGELOG_v4.md` for what it found.

## Not done here (left for PR B / PR C, see docs/PROMPT_03_V4_WEEK_MODE_DATA.md)
W6 market-info snapshots (PCR, max pain, OI, FII/DII), W7 `coverage`, W8 Recorder v2, W9 extras.
Weekend special sessions (Muhurat 8 Nov, Budget Sunday) are still not downloaded as trading days.
From 7 Dec 2026 the timings API still shows 15:40 for F&O (SEBI circular: 15:45): UNVERIFIED, N09 stays open.
