# CHANGELOG v2 -> v3 (12 Nov 2026)

Seedha saar (Hinglish): Expired weekly options ka data Upstox sirf Plus par deta hai (1-minute candles, Oct 2024 se list, 15 June 2025 se pakka). Purani chain (bid/ask/IV/greeks) kabhi nahi milti, isliye ab chain candles se "time T tak jo pata tha" ke hisaab se banti hai, phone ke liye CSV export hai, aur aage ke liye live Recorder hai. `continue` mode ka ek bada bug (mahine ki boundary par contracts chhoot jaate the) theek hua.

## A. Badi galtiyan theek hui (details: docs/AUDIT_FINDINGS.md)
| ID | Kya tha | Ab |
|---|---|---|
| F01 | `continue` mein pichhle mahine ke boundary contracts skip ho jaate the (koi error nahi) | first-seen sirf saboot ke saath |
| F02/F03 | Plus na hone par (UDAPI1149 with 401) ya sirf Analytics token par probe "token expire" bolkar crash | error code pehle padha jata hai; probe report likhta hai; Plan B / BLOCKED_TOKEN |
| F04 | Agli subah ka re-fetch purane data se haar jata tha | fetch time se faisla |
| F05-F08 | Derive: missing spot minute par din ka aakhri close (look-ahead); missing volume/OI = 0; PCR sirf us minute ke trades se; GEX lot size se double | `updl/chain.py` as-of chain; null rehta hai; max pain add; GEX units sahi |
| F09 | SENSEX options 15:30 ke baad NOT OK (BSE bhi 15:40 tak chalta hai 3 Aug 2026 se) | sessions.yaml SEBI circular ke hisaab se |
| F10-F13 | Recorder ka sirf naam tha; Termux setup Play-store par fail; sdcard copy chupchap fail; phone CSV nahi | `record`, `export`, mazboot `termux_run.sh`, Hinglish hints |

## B. Naye commands
* `python -m updl export [--every 15] [--test-start .. --test-end ..] [--phone-dir ..]`: phone CSV zip (option chain at time T + summary PCR/OI walls/max pain + README_EXPORT.txt).
* `python -m updl record [--every 60] [--underlyings NIFTY,SENSEX] [--until HH:MM] [--once]`: live option-chain Recorder.
* `bash termux_run.sh record | export` pass-through; `setup` ab `pkg update` karta hai aur `.venv` repair karta hai.
* Purane commands same: probe, plan, download, compact, validate, derive, handoff, verify, report, ingest, continue, month.

## C. Reference tables
* sessions.yaml: BSE F&O 15:40 from 2026-08-03; 15:45 from 2026-12-07 (index F&O, both exchanges); cash F&O stocks 15:30 from 2026-12-07.
* iv_model.yaml: settlement 15:30 / 15:35 (CAS) / 15:30.
* specs.yaml: dated lot sizes (per contract expiry), expiry-rule dates.
* charges.yaml: SEBI fee, stamp duty, BSE charge, STT on exercise filled but UNVERIFIED.
* data_manifest.yaml: **unchanged** (must equal the PROMPT_02 embedded manifest).

## D. Tests
* Baseline v2: `python -m pytest -q` -> 22 passed, 1 failed (T15, root-only test bug); plain `pytest -q` -> import error.
* v3: T15 fixed (same check, correct for root and Windows); `pytest.ini` added; new file `tests/test_v3_fixes.py`. See the PR for the exact final counts.

## E. Abhi bhi open (aapke haath mein ya live token chahiye)
1. Plus active karna (brokerage Rs 30/order while on Plus) aur probe chalana: asli depth, span, BSE support sirf probe batayega.
2. Real Termux phone par setup/continue/record: NOT RUN by me (cloud sandbox).
3. DATA_RIGHTS: Upstox se likhit jawab.
4. N01-N05 in AUDIT_FINDINGS (bisection, band-contract zero-row check, gamma flip/skew/vix term, run-id collision, last day after 18:00).
