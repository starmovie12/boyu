# README_OWNER_HINGLISH.md (Owner Guide + Termux 1-Mahina Continue Mode) — v3

Yeh guide simple Hinglish mein hai. Lines chhoti rakhi hain taaki aap apne Android phone (Termux) aur PC dono par aaraam se padh sako.

**Sabse pehle seedha jawab (v3 research, 12 Nov 2026):**
* Expired weekly options ka 1-minute data (OHLC, volume, OI) Upstox **sirf Plus plan** par deta hai.
* Upstox ke hisaab se expiries Oct 2024 se hain, candles 15 June 2025 se pakki hain.
* Purani option chain (bid/ask, IV, greeks) Upstox **kabhi nahi** deta. Isliye:
  * chain candles se dobara banti hai (`export`, `chain_csv/`),
  * aur aage ke liye `record` (Recorder) market time mein live chain save karta hai.
* Poori research: `docs/RESEARCH_EXPIRED_OPTIONS.md`. Galtiyan aur fixes: `docs/AUDIT_FINDINGS.md`.

## Table of Contents
1. Python 3.11 Install aur Requirements
2. Upstox Tokens aur Security Rules
3. Stage 0 Probe (`python -m updl probe`)
4. Plan aur Dry-Run (`plan` aur `download --dry-run`)
5. Asli Run aur Termux 1-Mahina `continue` Mode
6. Kitna Time aur Disk Space Lagega
7. OK / NOT OK / PROVISIONAL ka Matlab
8. Dates Badalna aur Sham 6 Baje ka Niyam
9. Upstox Plus Plan aur Brokerage Niyam
10. DATA_RIGHTS UNVERIFIED aur Upstox ko Likhne wala Sawal
11. AI ko Kya Paste Karna Hai (Aur Kya Kabhi Nahi)
12. NSE Bhavcopy Kaise Daalni Hai (`ingest`)
13. Termux Checklist (phone par step-by-step)
14. Phone par CSV Dekhna (`export`)
15. Recorder (`record`): live option chain save karna
16. Agar Kuch Galat Ho (Termux problems)

## 1. Python 3.11 Install aur Requirements
Apne PC ya Android Termux par Python 3.11+ install rakho aur virtual environment banao:

Linux / macOS:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows (PowerShell):
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Android Termux par `pip install -r requirements.txt` MAT chalao.** Termux par numpy/pandas pip se compile hote hain aur fail hote hain. Termux par bas yeh chalao:
```bash
bash termux_run.sh setup
```
Yeh khud `pkg update`, phir `pkg install python-numpy python-scipy python-pandas python-pyarrow`, phir `pip install -r requirements-termux.txt` karta hai.
Last line `[Termux Setup] OK -> pandas ... | numpy ...` dikhe to sab theek hai.
* **Termux F-Droid ya GitHub wala hona chahiye.** Google Play wale Termux mein `python-pandas` package nahi hai, setup wahan fail hoga.
* `pkg upgrade` ke baad Python version badle to `bash termux_run.sh setup` dobara chalao (yeh `.venv` khud naya bana deta hai).

## 2. Upstox Tokens aur Security Rules
* **Analytics Token**: Upstox Developer Apps -> Analytics tab. Free hai, 1 saal chalta hai, sirf padhne (read-only) ke liye. `UPSTOX_ANALYTICS_TOKEN` mein rakho.
* **Daily OAuth Access Token**: roz banta hai, raat **3:30 AM** par expire hota hai (Upstox docs). `UPSTOX_ACCESS_TOKEN` mein rakho.
* Expired contracts ke liye **Upstox Plus** chahiye. Upstox staff ke mutabik Plus user ka Analytics token bhi expired APIs par chalta hai (2026). Program yeh khud test karta hai (probe), maan ke nahi chalta.
* Token Upstox ke apne login flow se aap khud banate ho. Program kabhi token nahi banata. Guide: https://upstox.com/developer/api-documentation/open-api
* **Kabhi bhi apna token chat, GitHub ya kisi file (project ke andar) mein mat daalo.**

Sabse safe tarika (Termux / Linux / macOS): project ke BAHAR ek token file.
```bash
nano ~/.upstox_tokens
```
Usme do lines likho (apna asli token, chat mein kabhi nahi):
```text
analytics=aapka_analytics_token
access=aapka_daily_access_token
```
Phir:
```bash
chmod 600 ~/.upstox_tokens
```
```bash
echo 'export UPSTOX_TOKEN_FILE="$HOME/.upstox_tokens"' >> ~/.bashrc
```
```bash
source ~/.bashrc
```
Roz naya access token aaye to sirf `nano ~/.upstox_tokens` mein `access=` wali line badlo.

Windows (CMD / PowerShell):
```powershell
setx UPSTOX_ANALYTICS_TOKEN "aapka_analytics_token"
setx UPSTOX_ACCESS_TOKEN "aapka_daily_access_token"
```

## 3. Stage 0 Probe (`python -m updl probe`)
Sabse pehle probe chalao (max 40 calls):
```bash
python -m updl probe
```
Yeh `updl_data/probe_report.txt` aur `probe_report.json` banata hai aur ek Plan chunta hai:
* **Plan A**: Expired 1-minute candles (volume + OI) poori window ke liye mil rahe hain.
* **Plan A_PARTIAL**: Expired data mil raha hai par poore 6 mahine peeche tak nahi. Window chhoti ho jayegi. Aage ke liye Recorder chalao.
* **Plan B**: Expired calls fail (jaise Plus nahi hai). Sirf aaj ke listed contracts V3 se aayenge. Recorder abhi shuru karo.
* **Plan C**: Intraday option history bilkul nahi. Plan C se idea reject ho sakta hai, approve nahi.
* **BLOCKED_TOKEN**: token nahi mila ya Upstox ne reject kiya. Token theek karo, probe dobara chalao. (Download tab tak band.)

Note: Upstox ke historical-candle V3 endpoints token check nahi karte. Isliye asli token test P09 (expired list) par hota hai.

## 4. Plan aur Dry-Run (`plan` aur `download --dry-run`)
Asli download se pehle exact calls aur ghante dekho:
```bash
python -m updl plan
python -m updl download --dry-run --plan A
```
Yeh bina internet call ke exact calls aur samay batata hai.

## 5. Asli Run aur Termux 1-Mahina `continue` Mode
### A) Termux: ek-ek mahina peeche (`continue`)
Phone par 6 mahine ek saath mat utaro.
```bash
bash termux_run.sh continue
```
(ya `python -m updl continue`)
* Pehli baar: aaj ki tarikh se pichhla 1 mahina. Example: aaj 12 Nov 2026 -> `2026-10-12` se `2026-11-11` (last poora trading din).
* Dobara chalao: uske pehle ka mahina (`2026-09-12` se `2026-10-09`), phir uske pehle ka, aise hi.
* Har mahine ki zip: `updl_data/phone_outputs/` aur phone ke `Download/updl_months/` mein.
* Zip ke andar: `store/` (Parquet), `chain_csv/` (phone par khulne wali CSV, section 14), `health.csv`, `health_summary.txt`, `month_summary.txt`.
* Probe report 7 din se purani ho to `continue` khud probe dobara chala leta hai (max 40 calls).
* Beech mein ruk jaye (Android ne band kiya, token expire hua): wahi command dobara chalao. Kaam wahin se aage badhega.

### B) Full window pipeline (PC)
```bash
python -m updl download
python -m updl compact
python -m updl validate
python -m updl derive
python -m updl handoff
python -m updl verify
```

## 6. Kitna Time aur Disk Space Lagega
* Default speed: **30 calls per minute** (`RATE_CALLS_PER_MIN=30`, max 900 per 30 min).
* Formula: `calls ÷ 30 = minutes`.
* Upstox ka official rate page (12 Nov 2026): 50/second, 500/minute, 2000 per 30 minute. Program khud speed nahi badhata. Aap chaho to `--rate 60` de sakte ho (limit ke andar). 429 aaye to program khud aadha kar deta hai.
* Ek mahina (`continue`, 3 index): lagbhag 7,700 calls = ~4.3 ghante at 30/min, ~2.2 ghante at 60/min (ESTIMATE; `continue --dry-run` exact batata hai).
* Disk: ~0.2-0.3 GB per mahina, 6 mahine ~1.2-1.8 GB (ESTIMATE).
* Daily OAuth token raat 3:30 par expire hota hai (exit code 20). Naya token daalo, wahi command dobara chalao.

## 7. OK / NOT OK / PROVISIONAL ka Matlab
* **OK**: Us din ke expected 1-minute bars (>=99%), timestamps aur OHLC sab sahi hain.
* **PROVISIONAL**: Data sahi hai, par session timing UNVERIFIED hai (ginti mein aata hai). SENSEX hamesha PROVISIONAL.
* **NOT OK**: Data adhura ya galat. NOT OK din ginti mein nahi aate.

## 8. Dates Badalna aur Sham 6 Baje ka Niyam
* Dates: `--test-start` / `--test-end` ya `data_manifest.yaml` mein `TEST_START` / `TEST_END` (aur `HOLDOUT_START` dobara nikalo: aakhri 20% din).
* Naya din **shaam 6 baje (18:00 IST) ke baad** chalao (F&O 15:40 tak chalta hai; 7 Dec 2026 se 15:45 tak), aur agli subah ek baar aur (last din dobara fetch hota hai).

## 9. Upstox Plus Plan aur Brokerage Niyam
* Expired contracts ke liye Upstox Plus chahiye (Upstox docs: "available exclusively with an Upstox Plus plan subscription").
* Plus par options brokerage **Rs 30 prati order**, Basic par **Rs 20** (upstox.com/plus).
* Data utaar kar Basic par wapas jaana aapka faisla hai (plan switch par 24 ghante ka cooling-off, purani research note).

## 10. DATA_RIGHTS UNVERIFIED aur Upstox ko Likhne wala Sawal
`DATA_RIGHTS` abhi `UNVERIFIED` hai. Upstox staff ne forum par likha ki apne private use ke liye local storage theek hai (community.upstox.com/t/18261, Oct-Nov 2026), par yeh likhit ToS nahi hai. Upstox support ko yeh sawal verbatim bhejo aur likhit jawab aane tak data kisi ko mat do:

> "I am a retail API user. May I download historical and expired-contract candle data (index, futures, options, India VIX, cash stocks) through the API and store it on my own machine for personal strategy backtesting? Is there any retention limit, any limit on derived indicators or backtest results, and any restriction on keeping the data after I stop using the plan? Please answer in writing."

## 11. AI ko Kya Paste Karna Hai (Aur Kya Kabhi Nahi)
* AI ko sirf yeh 4 files paste karo:
  1. `updl_data/probe_report.txt`
  2. `updl_data/reports/health_summary.txt`
  3. `updl_data/reports/health.csv`
  4. `updl_data/reports/reconciliation.csv`
* **Kabhi bhi token, `.upstox_tokens` file, ya raw candle data chat mein paste mat karo.**

## 12. NSE Bhavcopy Kaise Daalni Hai (`ingest`)
NSE website se UDiFF F&O bhavcopy (`BhavCopy_NSE_FO_..._F_0000.csv.zip`) download karo, zip hi `external/bhavcopy/` mein rakho (unzip ki zaroorat nahi), phir:
```bash
python -m updl ingest external/bhavcopy/
python -m updl validate
```
Isse `reports/reconciliation.csv` banegi aur volume unit (units vs lots) saaf dikhega.

## 13. Termux Checklist (phone par step-by-step)
Har command alag chalao, ek ke baad ek.
1. Termux F-Droid/GitHub wala install karo. Phir:
```bash
pkg update -y && pkg install -y git python
```
2. Code lo (pehli baar):
```bash
git clone https://github.com/starmovie12/boyu
```
   Dobara update ke liye: `cd ~/boyu && git pull`
3. Folder mein jao:
```bash
cd ~/boyu
```
4. Setup:
```bash
bash termux_run.sh setup
```
5. Phone storage permission (ek baar, "Allow" dabana):
```bash
termux-setup-storage
```
6. Token file (section 2): `nano ~/.upstox_tokens`, `chmod 600 ~/.upstox_tokens`, `UPSTOX_TOKEN_FILE` set.
7. Phone ko sone se roko (lambe kaam ke liye):
```bash
termux-wake-lock
```
   Phone Settings -> Apps -> Termux -> Battery -> "Unrestricted / optimisation off".
8. Probe:
```bash
bash termux_run.sh probe
```
9. Ek mahina download:
```bash
bash termux_run.sh continue
```
10. Kaam khatam: `termux-wake-unlock`.

## 14. Phone par CSV Dekhna (`export`)
```bash
python -m updl export
```
* `updl_data/exports/chain_<start>_<end>.zip` banta hai (aur phone `Download/updl_months/` mein copy).
* Andar har index ke liye:
  * `chain_<INDEX>_<DATE>.csv`: har 15 minute, har expiry, har strike: spot, CE/PE close, volume, OI.
  * `summary_<INDEX>_<DATE>.csv`: har 15 minute: PCR, call OI wall, put OI wall, max pain.
  * `README_EXPORT.txt`: har column ka matlab.
* Har minute chahiye: `python -m updl export --every 1` (file badi hogi).
* Sirf woh values jo time T tak pata thi. Purani candle se aayi value par `is_filled=True` likha hota hai. Khali cell = data nahi tha (0 nahi bhara).
* Volume aur OI units (quantity) mein hain, lots mein nahi.
* Window badalni ho: `python -m updl export --test-start 2026-10-01 --test-end 2026-10-31`.

## 15. Recorder (`record`): live option chain save karna
Purani bid/ask, IV, greeks Upstox se kabhi nahi milenge. Isliye aaj se save karna shuru karo:
```bash
bash termux_run.sh record
```
* Market time (09:15 se close) mein har 60 second NIFTY, BANKNIFTY, SENSEX ki nearest expiry ki poori chain save hoti hai.
* File: `updl_data/recorder/<INDEX>/<YYYY-MM-DD>.csv` (bid, ask, qty, ltp, volume, OI, IV, delta, gamma, theta, vega).
* Free Analytics token kaafi hai. Lagbhag 3-6 calls per minute.
* Ruk jaye to wahi command dobara chalao: same file mein aage judta hai.
* Sirf kuch index: `--underlyings NIFTY,SENSEX`. Jaldi rokna: `--until 14:00`. Ek snapshot test: `--once`.
* Holiday / weekend par yeh khud mana kar deta hai.

## 16. Agar Kuch Galat Ho (Termux problems)
* `Unable to locate package python-pandas`: aap Google Play wala Termux use kar rahe ho. F-Droid/GitHub wala lo.
* `[Process completed (signal 9)]`: Android ne Termux band kiya. Battery optimisation band karo. Android 14+: Developer options -> "Disable child process restrictions". Phir wahi command dobara chalao.
* "Phone ke Download folder mein copy nahi ho paya": `termux-setup-storage` chalao. Zip `updl_data/phone_outputs/` mein safe hai.
* "Token expire ho gaya" (exit 20): naya access token file mein daalo, wahi command dobara.
* "Token nahi mila": `UPSTOX_TOKEN_FILE` set nahi hai (section 2).
* SSL error: `pkg update -y && pkg upgrade -y` (ca-certificates update hote hain).
