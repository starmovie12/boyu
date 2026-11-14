# PROMPT 04 (v4.2): repair, honest holes, exchange-side data, recorder v2, per-underlying export

Written 14 Nov 2026 (IST) by the assistant for itself, from the owner's "PROMPT_04 addendum" (problems seen on the
owner's phone, in the exported CSVs and in the first live runs). It sits on top of
`docs/PROMPT_03_V4_WEEK_MODE_DATA.md`; finished work (week mode, progress, workers, phone folder, routing fix) is
not redone. Budget rules unchanged: no sub-agents, targeted tests, code first, facts below are enough.

## Owner ke liye (Hinglish)
1. `repair`: purane month-run ke 1,797 fail hue chunks dobara line mein lagenge; 1,192 sahi chunks dobara download nahi honge.
2. Program ab jhooth nahi bolega: batch tabhi "POORA" jab sab chunks aaye; warna "ADHURA" + kaunsa din/kaunsa data nahi aaya (`holes.csv`).
3. Do `continue` ek saath nahi chalenge (lock). Phone band hone ke baad state check hoga.
4. Exchange ka PCR / max pain / OI / FII-DII abhi save hoga (sirf ~2 hafte peeche tak milta hai).
5. Recorder v2 (Monday 16 Nov 09:10 se chalana hai): kai expiry, spot/VIX/future LTP, gaps file.
6. Export har index ke alag folder mein, 1-minute long CSV, AI ke liye README.

## Facts (owner's phone and CSVs, not re-researched)
F1 1,192 DONE chunks + 1,797 FAILED_PERMANENT (all 400:UDAPI1021, plain listed keys sent to the expired endpoint).
F2 option holes 27 Oct - 13 Nov (BANKNIFTY from 28 Oct, NIFTY 11-13 Nov, SENSEX 13 Nov; wide chain mostly missing).
F3 13 Nov not served for spot/VIX/stocks at 12:30 IST on 14 Nov (a later live probe at 13:12 IST had it).
F4 two `continue` processes ran on one state; the phone switched off mid-run once.
F5 `weekday_mismatch` logged 1,383 times in one run. F6 exchange-side PCR / max pain / OI answer only ~2 weeks back.
F7 non-UDAPI100500 errors became permanent and were never retried. F8 data per underlying, phone-readable, AI-ready.
VERIFIED LIVE already (14 Nov): V3 serves a listed NIFTY option at 1 minute (31 days, 6,875 bars) and the second
real run fetched every listed contract through V3 with 0 failures, so PENDING_UNTIL_EXPIRY is not needed; one expired
call spans 90 days.

## PR A additions (same open PR)
A1 `repair` command: drop failure rows, queue their still-uncovered days (`repair_days` in the week cursor);
   `continue` runs queued repair windows first and suggests `repair` when UDAPI1021 rows exist. A contract that has
   expired since is found under its dated key; days already fetched under the listed key count as covered.
A2 failure policy: UDAPI1021 is never permanent and is not split-retried; 5 in a row stop the run (routing bug
   suspected); a batch with failed chunks says ADHURA / INCOMPLETE and lists them per item and index.
A3 `reports/day_status.csv` + `holes.csv`: COMPLETE / NOT_YET_SERVED / MISSING_ERROR / NOT_DOWNLOADED per
   (index, day, table).
A4 `weekday_mismatch`: one line per expiry, holiday-moved expiries are info (`expiry_moved_holiday`), real
   mismatches stay warnings.
A5 run lock (`state/run.lock`), SQLite `integrity_check` at start, stale `.tmp` cleanup.

## PR B (time-critical)
B1 `updl/marketinfo.py`: PCR, max pain, OI, change-in-OI per index, expiry and day inside retention + FII/DII;
   raw JSON + `xcheck_csv/`; runs first in every `continue`; data of day D usable from the next session.
B2 Recorder v2 (REST): current + next expiry, spot / VIX / future LTP per snapshot, ms timestamps, gaps file, daily
   summary, `--every` >= 15 s, shared limiter. The existing `record` command keeps working.

## PR C
C1 per-underlying export: `<INDEX>/spot_1m.csv`, `contracts.csv`, `options_1m/<expiry>.csv` (long), `futures_1m.csv`,
   `chain_15min/`, `raw_index.csv`, `ai_ready/<INDEX>_options_1m.parquet`; files split above 40 MB.
C2 `coverage` + `strategy_ready_days.csv` + `README_FOR_AI.md`.
C3 fewer calls (whole contract life per call where it helps), optional spot/VIX fast lane from Jan 2022.
C4 owner-file schemas and validators (events, constituents, corporate actions, participant OI), stock lists only from
   an official page actually opened; promised derived columns if budget remains.

## Done means
Named tests exist, full pytest green, numbers measured or marked NOT RUN LIVE, README table "kya milta hai / kya
nahi mil sakta", exact Termux commands, reminder to revoke the token and regenerate the API secret.
