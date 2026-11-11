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
