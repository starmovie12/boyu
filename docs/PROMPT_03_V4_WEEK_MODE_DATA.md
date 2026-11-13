# PROMPT 03 (v4.1): week mode, fast, live progress, phone folder, more data

Written 14 Nov 2026 (IST) by the assistant for itself, from the owner's "BOYU updl v4" prompt plus live facts.
The owner said: full freedom to rewrite, spend effort on code, test less, save chat budget. Work starts when the owner says "continue".

## Owner ke liye (Hinglish, short)
- Ek command: `bash termux_run.sh continue` = **ek hafta** (pehle mahina tha). Har baar ek hafta aur peeche.
- Chalte waqt ek line dikhegi jo har second badlegi (kitne % hua, kitna time bacha). Frozen nahi lagega.
- Output seedha phone ke `Download/BOYU_Data/week_NN_.../` folder mein, khuli files (CSV, Parquet, reports).
- Speed: 4 parallel workers. Ek hafta lagbhag 3-8 minute (ESTIMATE; phone par naapenge).
- Naya data: exchange-side PCR / max pain / OI (Upstox sirf ~2 hafte rakhta hai, isliye har hafte save hoga), FII/DII, holidays aur session time seedha Upstox se, zyada global index.
- Jo Upstox se nahi milta (index constituents, events, bhavcopy): aapko file deni hogi; `coverage` command batayega kya missing hai.

## LIVE FACTS (tag LIVE-14NOV: read-only GET calls with the owner's Plus analytics token, 14 Nov 2026 01:22-01:30 IST, about 450 calls, zero 429; token never stored in the repo)
| # | Fact | Consequence |
|---|---|---|
| L1 | Newest session not served yet: at 01:25 IST Sat, 1-minute and daily candles for Fri 13 Nov returned n=0 for Nifty, VIX, Sensex, a stock and (single-day request) a listed option; a 31-day range for the same option DID include 13 Nov. 12 Nov was complete (375 bars) | This is why probe P02/P03/P07/P08 said FAIL with HTTP 200. Never mark the newest day EMPTY_OK. Detect `data_through` (last day whose index 1-minute session is complete) and keep later days pending |
| L2 | Futures list for a weekly (non-monthly) expiry is empty; monthly expiry works (385 bars, last 15:39) | P14 FAIL is a probe bug: it must use a monthly expiry |
| L3 | V3 1-minute history starts 2022-01-03 (Dec 2021 empty) for Nifty, VIX, Sensex, Bank Nifty; a 31-day span works (7500 bars); daily from 2000-01-03 (2477 rows 2015-2024 in one call) | deep spot history is cheap: about 48 calls per instrument |
| L4 | Expired expiries: NIFTY 111 from 2024-10-03, SENSEX 110 from 2024-10-04, BANKNIFTY 31 from 2024-10-01. 1-minute option candles are complete (375 bars) on the very first expiry day (3 Oct 2024) and on every sampled date since | option history really starts Oct 2024 (staff said "consistent from 15 Jun 2025"; older data exists, quality to be judged by the validator) |
| L5 | One expired call can span 90 days (22,597 bars); 30 days = 8,085 bars | one call per contract per batch is safe |
| L6 | Contract list carries `lot_size` per contract: NIFTY 25 (Oct 2024, 30 Jan 2025 expiry), 75 (Apr-Aug 2025), 65 (2026); SENSEX 10 (Oct 2024), 20 later; BANKNIFTY 30 (Jun 2025, Jun 2026) | specs.yaml lot rows are partly wrong (NIFTY 75 "from 2025-01-02"); the contract's own lot is truth |
| L7 | Deep OTM contracts are dense too: median 1,802 of 1,925 minutes in a week; one contract-week is about 120 KB JSON | full chain = about 2.2 M rows per week; default stays the wide chain, `--chain full` optional |
| L8 | Real throughput (distinct expired contract-weeks, keep-alive session): 4 threads 640-790 calls/min, 8 threads 1,400 calls/min, median latency 300 ms, no 429, no rate-limit headers; a new connection per call costs 340 ms vs 47 ms | reuse one session, 4 workers; the limiter (not the network) sets the pace |
| L9 | `/v2/market/pcr`, `/max-pain`, `/oi`, `/change-oi`: work for a past date only about 2 weeks back (30 Oct OK, 23 Oct null, also null 3 weeks before a live expiry); `bucket_interval` 1 gives 386 points per day; `/oi` returns every strike (142 Nifty, 235 Sensex); `change-oi` needs `interval` in minutes (5 OK, "1D" is HTTP 400) | exchange-side chain metrics must be saved every week or they are lost |
| L10 | `/v2/market/fii` and `/dii`: 30 daily rows per call, `from=2026-04-01` works | check 48 without a manual file |
| L11 | `/v2/market/holidays` (2026: 22 rows with type TRADING_HOLIDAY / SETTLEMENT_HOLIDAY / SPECIAL_TIMING and per-exchange times) and `/holidays/{date}` (2024, 2025 work). The 16 TRADING_HOLIDAY dates equal reference/holidays.yaml. Muhurat 8 Nov 2026: NSE 14:20-15:30, NFO/BFO 14:20-15:40. Budget Sunday 1 Feb 2026 09:15-15:30 | tables can be tagged SOURCE; Muhurat times are now known |
| L12 | `/v2/market/timings/{date}` works back to 2022: NFO and BFO 09:15-15:40 from 3 Aug 2026, 15:30 before; NSE/BSE cash 15:30. For 8 Dec 2026 it still says 15:40 (SEBI circular says 15:45 from 7 Dec) | per-day session times from the API; the 7 Dec change stays UNVERIFIED (N09) |
| L13 | `/v2/option/chain` live: 142 strikes with bid/ask/qty/greeks; `/v3/market-quote/ltp` works for index and VIX | Recorder path verified live |
| L14 | global master has exactly 13 names: CAC 40, DAX, DOW JONES, FTSE 100, GIFT NIFTY, HANG SENG, NIKKEI 225, Oil (Brent), Oil (WTI), S&P, US 30, US Tech 100, USD INR | reference/globals.yaml "Oil Brent"/"Oil WTI" never resolve today (bug); no US VIX, dollar index, gold or bond yield exists there |
| L15 | `/v2/fundamentals/{isin}/corporate-actions` returns only the latest event (one dividend), not a history. HDFCBANK daily closes around its Aug 2025 bonus show no 50% gap (982.0 -> 973.4) | candles look back-adjusted for bonus/split (matches the staff statement); corporate-action history stays an owner file |
| L16 | D06 (band) and D08 (wide chain) plan the same contract-days twice; the D07 line in the plan is an inflated estimate (593) | remove the duplicate calls; print real counts |

## Hard rules (unchanged)
READ-ONLY GET only, allowlist + T09 word test stay green. No token in any file, log, commit or message. No invented data: unknown stays null or UNVERIFIED with an owner TODO. No fill without a flag. No compiled dependency. `data_manifest.yaml` and the copy embedded in PROMPT_02 stay identical (edit both together). CLI stays backward compatible. Owner messages Hinglish, code and docs English.

## Budget rules (owner asked for this)
1. No sub-agents, no long web research: the facts above are enough; anything not known is tagged UNVERIFIED.
2. Read a file only when it is about to be edited. Prefer new small modules over rewrites.
3. Tests: one small targeted test per risky behaviour (list below). Full pytest only at the 3 checkpoints. No separate hostile-review agent; one self-review of the final diff.
4. Docs short: `docs/AUDIT_v4.md` as a compact table of what was found while touching code; `docs/CHANGELOG_v4.md` one page; README only the changed sections.
5. Ship in order. Each work package ends green and pushed; if budget ends, the rest is listed as "left", never half done.

## Work packages (in this order)

### W1 Week batches + newest-day safety (new `updl/weekly.py`; `monthly.py` keeps old names as thin wrappers)
- `continue` = 7 calendar days by default; `--window-days N` (0 = old calendar month); `--weeks K` runs K batches in one go; `--reset-cursor`.
- `data_through`: one cheap call per run (index 1-minute of the candidate day). Days after it are `pending`, never covered, never EMPTY_OK; Hinglish line "13 Nov ka data Upstox par abhi nahi aaya".
- Run order: (a) catch-up = trading days newer than the newest finished day, up to `data_through` (max 7 per batch); (b) else the next older week. No gap, no overlap, correct across month and year ends.
- Old month cursor (window longer than the new one): explain in Hinglish, start the week cursor at today; days already covered in state are not downloaded again.
- First batch also fetches base history: daily spot/VIX from 2000 and 60 days of 1-minute spot/VIX (about 25 calls), so IVP and warm-up work. Derive keeps INSUFFICIENT_HISTORY where history is missing.
- Words Mahina -> Hafta, tags `week_NN_<from>_to_<to>`; refetch-last-day keyed on the newest covered day, not on batch number.

### W2 Speed (measure with `--profile`, then fix)
- One keep-alive `requests.Session`; `--workers` (default 4) for candle chunks; ONE lock-protected limiter (token bucket per minute + rolling 30-minute window); SQLite writes only from the main thread; a chunk is DONE only after its raw file is written atomically.
- Rate policy (replaces DECISIONS 15): default 240 calls/min and 1,800 per 30 min (official page: 500/min, 2,000 per 30 min; L8 saw no 429). On 429: honour Retry-After, halve, pause, ramp back slowly, and remember a lower cap in state. Flags `--rate`, `--max-per-30min`, `--workers`. Manifest + PROMPT_02 copy updated together.
- Remove waste: D06/D08 duplicate contract-days (L16), repeated master downloads, probe re-run inside `continue` unless missing or older than 7 days, expired future lists only on monthly expiries (already), real D07 count.
- Incremental pipeline: compact, validate, derive and export only the batch's days (today they walk the whole store every time).
- Vectorised Black-76 IV and greeks with numpy (no per-row solver, no `iterrows`); same maths, same columns; T14/T18 must still pass.
- Target: one week end to end in 8 minutes or less on the phone; report measured sandbox numbers and say honestly what the phone will add.

### W3 Live progress (new `updl/progress.py`, stdlib, ASCII)
- TTY: one line redrawn with `\r` at least once per second; not a TTY: one plain line every 25 calls or 15 s.
- `[2/5 Download] [#####-----] 57% 430/754 | D06 NIFTY | 231/min | 03:12 | ETA 02:20 | RUNNING`, status RUNNING / WAITING-RATE-CAP (mm:ss) / RETRY-429. Sleeps are sliced to 1 s.
- ETA models the 30-minute cap and uses the rolling real rate. Stages 2-5 show a spinner + counter. Final box: time, calls, retries, 429s, calls/min. Never prints a token.

### W4 Phone folder
- Root `/sdcard/Download/BOYU_Data/` (fallback `~/storage/downloads/BOYU_Data`; `--phone-dir` or env `UPDL_PHONE_DIR`). Each batch: new `week_NN_<from>_to_<to>/` written as `.tmp` then renamed: `chain_csv/`, `store/`, `xcheck_csv/`, `health.csv`, `health_summary.txt`, `README_EXPORT.txt`, `week_summary.txt`. One line appended to `INDEX.txt`. Zip stays in `updl_data/phone_outputs/`.
- Not writable: Hinglish `termux-setup-storage` hint, data stays in `updl_data`, no crash. End: absolute path, file count, MB, next command.

### W5 Fixes from live facts
- Probe: P02/P03/P07/P08 use `data_through`; P14 uses a monthly expiry; P19 reports the true oldest expiry with candles (list start), not only the ladder; report shows `data_through` and labels the manifest window as such.
- `reference/globals.yaml`: real names (L14), add the other global indices; names that do not resolve are reported, never a fake key.
- Calendar from Upstox: new allowlisted GET paths `/v2/market/holidays`, `/v2/market/holidays/{date}`, `/v2/market/timings/{date}`; a per-day timings cache (SOURCE, date read) drives expected bars and special sessions; YAML tables stay as the offline fallback and get SOURCE tags + Muhurat times. A day with neither API nor table entry is NOT OK with reason TABLE_GAP.
- `reference/specs.yaml`: lot rows corrected to what L6 proves, rest tagged UNVERIFIED; derived code keeps using the contract's own lot.

### W6 Exchange-side cross-check data (new `updl/marketinfo.py`)
- New allowlisted GET paths: `/v2/market/pcr`, `/v2/market/max-pain`, `/v2/market/oi`, `/v2/market/change-oi`, `/v2/market/fii`, `/v2/market/dii` (each with an allow test and a not-allowed neighbour test).
- Every batch whose days are inside the ~2-week retention saves, per index, expiry and day: PCR and max-pain series (1-minute buckets), per-strike OI, change in OI; plus FII/DII daily rows. Tables `xcheck_*` with `available_from_ts` (FII/DII usable from the next session). Null answers are recorded as null, not as zero.
- A short report compares our rebuilt PCR / max pain with the exchange-side values.

### W7 `python -m updl coverage`
One Hinglish line per item D01..D20 (+ xcheck, recorder): DELIVERED / PARTIAL / MISSING / OWNER-FILE-MISSING with rows, first and last day, reason; written to `reports/data_coverage.csv`; driven only by files on disk. Runs at the end of every batch (short form).

### W8 Recorder v2 (REST only)
Current + next weekly expiry (BANKNIFTY current monthly), `--every` down to 15 s inside the rate policy, spot / VIX / index-future LTP in every snapshot (new path `/v3/market-quote/ltp`), `local_ts` with milliseconds, a gaps file (missed snapshots, HTTP errors), one daily summary line, one 15:35 market-info snapshot. Shares the limiter with any download in the same process.

### W9 Only if budget remains (each one small and complete, else listed as left)
`--chain full`; `vega_DERIVED`, `iv_daily_DERIVED`, `skew_DERIVED`, `vix_term_DERIVED`, `gamma_flip_PROXY` (same causality rules); `spread-report` from recorder files; owner-file schemas + ingest validators (constituents, corporate actions, events, participant OI) with one good/one bad test each; index stock lists only from an official page actually opened (else owner TODO, D10 stays SKIPPED_NO_LIST); short docs `DATA_GAP_v4.md`, `RECORDER_V2_DESIGN.md` (WebSocket: design only), `EVENT_SOURCES.md`, `PAID_DATA_OPTIONS.md`.

## Targeted tests (all 48 old tests stay green)
window maths (3 weeks contiguous, month/year edge, holiday, Muhurat); `data_through` keeps the newest day pending; old month cursor migrates; second batch downloads nothing already covered; limiter with 4 threads on a fake clock never passes either cap; `--workers 4` store equals sequential store; kill in batch 2 then clean resume; progress on TTY and not-TTY without a token; phone folder tmp -> rename and unwritable case; vectorised IV equals the old solver on a sample; allow / not-allowed for every new path; market-info null handling; coverage on a fake-transport run; recorder v2 multi-expiry + gaps file.

## Branch and PRs
Only the session branch can be pushed, so: branch `claude/stoic-goldberg-prok6u` restarted from `main`. PR A = W1-W5 (opened when green; the owner merges from the phone). After the merge the branch restarts from `main`: PR B = W6-W8, PR C = W9. Small commits; nothing unpushed at the end of a turn.

## Done means
pytest green (count reported), `python -m compileall updl` clean, `bash -n` + shellcheck clean, offline CLI commands run, token grep of the diff clean, manifest == PROMPT_02 copy. `docs/AUDIT_v4.md`, `docs/CHANGELOG_v4.md`, DECISIONS (rate policy, new paths, calendar source, week cursor), README (one command, where files land, "Data jo abhi nahi mil raha"), `termux_run.sh` header. Everything not run on a real phone is listed as NOT RUN.

## Final reply to the owner (Hinglish, short)
What changed, measured numbers, exact Termux commands, where the files are, what is still the owner's work (exact file and folder), what is left and the risk.
