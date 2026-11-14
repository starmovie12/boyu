# CHANGELOG v3.1 -> v4 (14 Nov 2026) — PR A: week mode, speed, live progress, phone folder

Seedha saar (Hinglish): `bash termux_run.sh continue` ab ek HAFTA nikalta hai, chalte waqt ek line har second
badalti hai, 4 workers se download hota hai, aur output seedha phone ke `Download/BOYU_Data/week_NN_.../` folder
mein khuli files ke roop mein aata hai. Asli API par chala kar ek bada purana bug mila aur theek hua: abhi-listed
options/futures galat endpoint par ja rahe the aur sab fail ho rahe the.

## Naya
* `continue` = 7 din ka batch (`--window-days N`, `--weeks K`, `--reset-cursor`, `--back-to DATE`; `--window-days 0` = purana mahina mode).
* Naye din ka safe handling: Upstox par jo din abhi nahi aaya woh "pending" rehta hai, agli baar apne aap aata hai.
* Live progress line (`updl/progress.py`): %, calls, calls/min, ETA, `WAITING-RATE-CAP`, `RETRY-429`.
* `--workers` (default 4), `--max-per-30min`, `--profile`; ek hi thread-safe rate limiter; pichhle run ke calls bhi 30-minute limit mein gine jate hain.
* Phone folder `Download/BOYU_Data/` (`--phone-dir`, env `UPDL_PHONE_DIR`), `.tmp` -> rename, `INDEX.txt`.
* Calendar seedha Upstox se (`/v2/market/timings/{date}`), har din cache; `TABLE_GAP` jab din ka pata na ho.
* `bash termux_run.sh update` = git pull + setup.
* Global instruments: 13 asli naam (Brent/WTI ab sach mein download hote hain, plus S&P, US Tech 100, Dow, Nikkei, Hang Seng, DAX, FTSE, CAC).

## Bug fixes (poori list: docs/AUDIT_v4.md, V01-V19)
V01 listed contracts expired endpoint par (Critical) · V02 naya din EMPTY_OK ban jata · V04 galat ETA · V05 D06/D08 double
download · V06/V07 dheema derive aur har baar poora store · V08 crude ke galat naam · V09/V10/V11 probe (P14, oldest date,
window label) · V12 D07 estimate · V13 stale master · V14 purani failure rows.

## Measured (cloud sandbox, real Upstox API, owner's Plus analytics token, read-only; phone: NOT RUN)
| What | Number |
|---|---|
| Probe | 35 calls, 17 s, every test PASS except P21 (UNVERIFIED by design); oldest option date 2024-10-03 |
| Week 2026-11-07..11-13 (4 trading days, 3 indices), run 2 after the V01 fix | 434 chunks: 420 DONE, 14 EMPTY_OK, 0 failed; 496,441 option rows; all 12 index-days OK / PROVISIONAL |
| Processing after download | compact 5 s, validate 2 s, derive 11 s, export + copy 4 s = 22 s |
| Download speed | 235 calls/min measured at the 240/min setting (run 1: 1,570 calls in 6.6 min, 0 retries, 0 x 429) |
| A clean week | about 780 calls = about 3.3 min download + 0.4 min processing (ESTIMATE from the two runs: run 1 wasted calls on V01, run 2 re-used run 1's index data) |
| Rate-cap wait seen live | run 2 started 2 minutes after run 1's 1,570 calls: it waited 17 min in WAITING-RATE-CAP, then finished (the 1,800 per 30 min cap works across runs) |
| Size of one week | phone folder 42 MB (321 files), zip 42 MB, store about 40 MB for the week + base history |
| Cross-check | our rebuilt NIFTY chain on 12 Nov at the close: PCR 0.835, max pain 25,250 over 46 strikes; Upstox market-info API for the full chain: PCR 0.852, max pain 25,200 |
| Before (v3.1, from the owner's phone log) | one month = 3,539 planned calls at 30-60/min, about 100 min, no output while running, live contracts failing |

Phone par CPU aur network dheema hoga: download wahi rate limit se bandha hai (network theek ho to ~3-4 min), processing
cloud ke 22 s se kai guna (ESTIMATE 1-2 min). Target "8 minute ke andar" phone par NOT RUN / UNVERIFIED.

## Tests
`python -m pytest -q`: 60 passed (48 purane + 12 naye in `tests/test_v4_week.py`). compileall, pyflakes, bash -n, shellcheck clean.

## Self-review of the diff (second pass)
* Found and fixed: prints from other modules were glued to the live line on a terminal (stdout proxy + pty check); ETA ignored the cap wait; forbidden word `order` as a variable name (T09 caught it); bisection returned the largest root on flat prices while the scalar solver returns the smallest; a third copy of every week on disk.
* Known and left: week mode does not download weekend special sessions; FAILED_PERMANENT chunks are retried on every run; legacy month mode keeps its old (slow, whole-store) pipeline; `weekday_mismatch` log lines are noisy.

## v4.2 (PROMPT_04, same PR): repair aur sach-sach status
* `bash termux_run.sh repair`: purane fail hue chunks (aapke phone par 1,797, sab UDAPI1021) wapas line mein; sahi 1,192 chunks ko haath nahi lagta. Phir `continue` pehle wahi din laata hai.
* UDAPI1021 ab kabhi "permanent fail" nahi banta; 5 baar lagataar aaye to run ruk jata hai (calls barbaad nahi hote).
* Batch tabhi "POORA (COMPLETE)" jab sab chunks aaye; warna "ADHURA (INCOMPLETE)" + list. `day_status.csv` aur `holes.csv` har batch folder mein.
* Do run ek saath nahi (`run.lock`); phone band hone ke baad state database check hota hai.
* `weekday_mismatch` ka shor band: expiry holiday ki wajah se ek din pehle ho to woh normal hai (`expiry_moved_holiday`).
* Tests: 66 passed (6 naye in `tests/test_v42_repair.py`). Holes before/after `repair` on the owner's phone state: NOT RUN LIVE (the phone state is not in the sandbox; the test rebuilds 1,000 failures + 100 DONE).
* **Exchange ka data ab save hota hai** (`updl/marketinfo.py`): har `continue` sabse pehle PCR, max pain, OI, change-in-OI (har index, har din, ~2 hafte peeche tak) aur FII/DII save karta hai: `updl_data/xcheck_csv/` aur har batch folder mein `xcheck_csv/`. NOT RUN LIVE (token revoke ho chuka hona chahiye); live facts 14 Nov ke probe calls se hain.
* **Recorder v2**: current + next expiry, spot / future / VIX ka LTP har snapshot mein, `gaps_<date>.csv`, `summary.csv`, `--every` kam se kam 15 second. Download ke saath chal sakta hai (apna lock; download apni limit kam kar leta hai). NOT RUN LIVE (market band tha).
* Tests ab 71 passed.

## Baaki (PR C)
Exchange-side PCR / max pain / OI / FII-DII snapshots (sirf ~2 hafte milte hain), `coverage` command, Recorder v2, extras.
