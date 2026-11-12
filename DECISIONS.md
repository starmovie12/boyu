# DECISIONS.md — Architectural & Engineering Decisions

1. **Read-Only Enforcement (Rule 1)**:
   - `updl/client.py` enforces `ALLOWED_PATH_TEMPLATES` via compiled regexes and `MASTER_URLS` before any HTTP request is made.
   - Zero occurrences of forbidden words (`order`, `funds`, `portfolio`, `position`, `gtt`, `payout`, `login`, `authorization/token`) or hardcoded bar counts (`375`) exist anywhere in `updl/`.
2. **Secret Redaction & Filename Length (Rules 2 & 3)**:
   - `updl/timeutil.py` uses a compact 6-character UTC stamp (`HHMMSS`) for default `run_id` values (`rHHMMSS`) so raw filenames (`YYYY-MM-DD_YYYY-MM-DD.rHHMMSS.meta.json`) stay under 40 characters and never false-positive `secretscan.py`'s 40+ character token detector (`[A-Za-z0-9._-]{40,}`).
3. **Point-in-Time Contract Selection & Delta-Aware Band (Section 6.2)**:
   - `filter_contracts_point_in_time` logs and excludes `MASTER_ONLY` contracts (contracts present in today's master for already-expired dates that are missing from the expired API list).
   - Strike margin grows with `sqrt(calendar_DTE / 365)` via `max(STRIKE_MARGIN_STEPS * step, K * spot * (VIX_high/100) * sqrt(cal_dte/365))`.
4. **Causality & Zero Look-Ahead in Derived Tables (Rule 8 & Section 9)**:
   - Every derived row carries `available_from_ts` (IST).
   - `ivp_vix_PROXY` uses strictly the previous 252 daily closes (`trading_date < D`), never day `D`'s own close.
   - `rv20_DERIVED` emits both `INTRADAY_PREV_CLOSE` (`available_from_ts = D 09:15`, using closes up to `D-1`) and `EOD_INCLUSIVE` (`available_from_ts = D session close`).
   - Volume baselines match by `(minute_of_day, option_type, moneyness_bucket)` and update the historical accumulator only after day `D` finishes processing.
5. **Termux Month-by-Month Rolling Backwards Continue Mode (`updl/monthly.py`)**:
   - Added `python -m updl continue` (and `termux_run.sh continue`) so mobile Termux runs never have to pull all 6 months at once.
   - Automatically checks today's IST date (e.g., `2026-11-11`), pulls the previous 1-month window (`2026-10-11` to `2026-11-09`), compacts, validates, derives, and saves a ready-to-use `.zip` package and summary in `<workdir>/phone_outputs/` (and `/sdcard/Download/updl_months/` on Android Termux).
   - Persists state in `<workdir>/state/monthly_cursor.json` so each subsequent `continue` command automatically pulls the next earlier 1-month window (`2026-09-11..2026-10-10`, `2026-08-11..2026-09-10`, etc.).

## v3 decisions (12 Nov 2026, audit; details in docs/AUDIT_FINDINGS.md and docs/RESEARCH_EXPIRED_OPTIONS.md)

6. **New allowlisted endpoint `/v2/option/chain` (Recorder only)**:
   - Read-only GET, listed in `ALLOWED_PATH_TEMPLATES` on purpose, with tests (`TestRecorder`). Upstox has no historical option-chain endpoint ("We do not currently offer historical option chain data", staff 2025-04-21), so bid/ask/IV/greeks can only be saved live. The Analytics Token supports the Option Chain category.
   - `/v2/option/contract` and every account, trading or token endpoint stay outside the allowlist.
7. **Recorder (`python -m updl record`)**: CSV per underlying and day (`recorder/<U>/<date>.csv`), append + fsync per snapshot, resumable by re-running. Columns follow PROMPT_02 6.5 (bid, ask, bid_qty, ask_qty, iv_source, exchange_ts, local_ts) plus the greeks Upstox returns; `exchange_ts` is empty because the response has no exchange timestamp. CSV instead of Parquet so a phone can open it and an append never rewrites a file.
8. **Chain "as of T" (`updl/chain.py`)**: used by derive (`chain_metrics_1m`) and export. A value is visible at T only if its candle closed at or before T. Values carried from an older candle are flagged (`is_filled`, `age_min`); this is the only form of carrying and it is always visible (Rule 4). Missing stays null.
9. **GEX units**: Upstox reports OI in units (staff 2026-10-19), so `gex_proxy_PROXY = sign x gamma x OI_units x spot^2 x 0.01`. This equals the spec's `gamma x OI x lot_size x ...` when OI is in contracts; the lot-size factor was a double count.
10. **Settlement time for IV** comes only from `reference/iv_model.yaml` (15:30 / 15:35 CAS / 15:30 from 7 Dec 2026 per SEBI circulars). This deliberately replaces the spec's 15:40 ASSUMPTION; the derive code holds no date.
11. **Tokens (PROMPT_02 rule 2 kept)**: expired calls use the access token; the analytics token only after the probe proved it (P09 when it is the only token, P16 when both exist). A 401 is mapped by error code first (UDAPI1149 -> Plus required, UDAPI100067 -> token type not permitted) because UDAPI1149 has been seen with HTTP 401.
12. **Probe with a token problem** writes `probe_report.txt` with plan `BLOCKED_TOKEN` and exits 20; `download` refuses a report whose plan is not A/A_PARTIAL/B/C.
13. **first_seen with evidence only** (`first_seen_v3`): a contract's first-seen date is stored only when the first candle came after the requested start, and only lowered later. Needed for the backwards `continue` mode.
14. **Compaction order** uses the state's `fetched_utc` (sortable) instead of `rHHMMSS` run ids; run ids stay short for the 40-character rule.
15. **Manifest unchanged**: `data_manifest.yaml` still equals the manifest embedded in PROMPT_02 (rate 30/min, 900 per 30 min). The program never raises the rate; the README tells the owner that `--rate 60` is inside the documented 500/min and 2000/30 min.
16. **Phone copies** use `shutil.copyfile` (not `copy2`) and print a `termux-setup-storage` hint on failure. `continue` refreshes a missing or 7-day-old probe report itself.
17. **v3.1 first-seen window**: the catalog keeps `(first_date, evidence_from)`; a contract is skipped only on days inside `[evidence_from, first_date)`, the range a request covered without any candle. Earlier days stay selectable (the backwards `continue` mode needs this for illiquid strikes of monthly contracts).
18. **v3.1 spot flags**: every derived/exported row that uses the spot carries `spot_age_min` and `spot_is_filled` (the index stops at 15:30 while F&O trades on; Rule 4).
