# README_OWNER_HINGLISH.md (Owner Guide + Termux 1-Mahina Continue Mode)

Yeh guide simple Hinglish mein hai. Lines chhoti rakhi hain taaki aap apne Android phone (Termux) aur PC dono par aaraam se padh sako.

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
10. DATA_RIGHTS UNVERIFIED aur Upstox ko Likhने wala Sawal
11. AI ko Kya Paste Karna Hai (Aur Kya Kabhi Nahi)
12. NSE Bhavcopy Kaise Daalni Hai (`ingest`)

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

**Android Termux par `pip install -r requirements.txt` MAT chalao.** Termux par numpy/pandas ke wheels nahi hote, pip unhe compile karne lagta hai aur `numpy` ka build `cpow ... complex.h` error ke saath fail ho jata hai. Termux par yeh karo:
```bash
pkg update -y
pkg install -y python git python-numpy python-scipy python-pandas python-pyarrow
pip install -r requirements-termux.txt
```
Ya ek hi command mein (yeh upar ke teeno kaam khud kar deta hai):
```bash
bash termux_run.sh setup
```
Last line `[Termux Setup] OK -> pandas ... | numpy ...` dikhe to sab theek hai.

## 2. Upstox Tokens aur Security Rules
* **Analytics Token**: Upstox Developer Apps -> Analytics tab mein jao. Yeh free hai, 1 saal tak chalta hai, aur sirf data padhne (read-only) ke kaam aata hai. Isko `UPSTOX_ANALYTICS_TOKEN` mein set karo.
* **OAuth Access Token**: Expired contracts (Plan A) ke liye Upstox Plus plan chalu karo (abhi free likha hai) aur roz ka OAuth token `UPSTOX_ACCESS_TOKEN` mein rakho.
* Token Upstox ke apne login flow se owner khud banata hai (program kabhi token nahi banata). Official guide: https://upstox.com/developer/api-documentation/open-api
* **Kabhi bhi apna token chat ya GitHub mein mat bhejo!**

Linux / macOS / Termux:
```bash
export UPSTOX_ANALYTICS_TOKEN="aapka_analytics_token"
export UPSTOX_ACCESS_TOKEN="aapka_daily_access_token"
```

Windows (CMD / PowerShell):
```powershell
setx UPSTOX_ANALYTICS_TOKEN "aapka_analytics_token"
setx UPSTOX_ACCESS_TOKEN "aapka_daily_access_token"
```

Ya project folder ke BAHAR ek chhoti file banao (`~/.upstox_tokens` jisme `chmod 600 ~/.upstox_tokens` ho):
```text
analytics=aapka_analytics_token
access=aapka_daily_access_token
```
Aur set karo: `export UPSTOX_TOKEN_FILE="$HOME/.upstox_tokens"`

## 3. Stage 0 Probe (`python -m updl probe`)
Sabse pehle probe chalao (budget max 40 calls):
```bash
python -m updl probe
```
Yeh `updl_data/probe_report.txt` aur `probe_report.json` banata hai aur char mein se ek Plan chunta hai:
* **Plan A**: Expired 1-minute candles (volume + OI ke saath) poore warm-up aur test window ke liye mil rahe hain.
* **Plan A_PARTIAL**: Expired data mil raha hai par poore 6 mahine peeche tak nahi hai. Window chhoti ho jayegi; Recorder bhi shuru karo.
* **Plan B**: Expired calls fail huye (jaise Plus plan nahi hai). Sirf aaj ke listed contracts V3 se utrenge. Recorder abhi shuru karo.
* **Plan C**: Intraday option history bilkul nahi mili. Plan C se idea reject ho sakta hai, approve nahi.

## 4. Plan aur Dry-Run (`plan` aur `download --dry-run`)
Asli download se pehle exact calls aur ghante dekho:
```bash
python -m updl plan
python -m updl download --dry-run --plan A
```
Yeh bina koi internet call kiye exact API calls aur lagne wala samay print karega.

## 5. Asli Run aur Termux 1-Mahina `continue` Mode
### A) Termux Special: Ek-Ek Mahina Piche Nikalne Wala Mode (`continue`)
Phone (Termux) par ek saath 6 mahine ka data nikalne ki zaroorat nahi hai!
* Jab aap pehli baar command doge, script khud check karegi ki aaj kitni tarikh hai (jaise 11 November 2026) aur pichhle **1 mahine** (`2026-10-11` se `2026-11-09`) ka poora data nikal kar, validate aur derive karke aapke phone mein (`updl_data/phone_outputs/` aur `/sdcard/Download/updl_months/` mein `.zip` file banakar) save kar degi:
```bash
python -m updl continue
```
* Ya Termux helper script se chalao:
```bash
bash termux_run.sh continue
```
* Jab pehla mahina save ho jaye, aur aap dobara wahi command (`python -m updl continue`) chalaoge, to yeh automatically us tarikh se **uske pichhle 1 mahine** (`2026-09-11` se `2026-10-10`) ka data nikal kar phone mein save kar degi!
* Jitni baar aap `python -m updl continue` bolte rahoge, yeh aaj ki tarikh se 1-1 mahina piche ka data nikalti rahegi (`Oct-Nov` -> `Sep-Oct` -> `Aug-Sep` -> `Jul-Aug` -> `Jun-Jul` -> `May-Jun` ...).

### B) Full Window Standard Pipeline (PC ya Full Run)
Agar poori window ek pipeline mein chalani ho:
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
* Lagbhag 5,000 calls (28-day cap) = ~3 ghante (ESTIMATE).
* Lagbhag 22,000 calls (1-day cap) = ~12 ghante (ESTIMATE). Termux 1-mahina mode mein har mahina iska sirf ~1/6th time lega!
* `plan` command exact calls batayega. Rate sirf `probe_report.txt` ke headers padhkar badhao (`--rate N`).
* Disk space: lagbhag 1 se 2 GB પૂरे 6 mahine ke liye (ESTIMATE).
* Daily OAuth token raat **03:30 IST** par expire ho sakta hai. Agar token expire ho jaye (exit code 20), to naya token banakar wahi command (`python -m updl continue` ya `download`) dobara chalao — kaam wahin se aage badhega!

## 7. OK / NOT OK / PROVISIONAL ka Matlab
* **OK**: Us din ke expected 1-minute bars (>=99%), timestamps aur OHLC sab sahi hain.
* **PROVISIONAL**: Data sahi hai, par session timing ya exchange rule `UNVERIFIED`/`ASSUMPTION` tag par hai (valid trading days mein gina jata hai).
* **NOT OK**: Data adhura hai ya galat hai. **NOT OK din ginti mein nahi aate** aur backtest se bahar rehte hain.

## 8. Dates Badalna aur Sham 6 Baje ka Niyam
* Dates badalne ke liye `--test-start` / `--test-end` flag ya `data_manifest.yaml` mein `TEST_START` / `TEST_END` badlo (aur `HOLDOUT_START` bhi dobara nikalo: aakhri 20% trading din = 25 din).
* Naya trading din hamesha **shaam 6 baje (18:00 IST) ke baad** chalao (kyunki F&O 15:40 tak chalta hai aur data final hone mein waqt lagta hai), aur agli subah ek baar aur chalao (last trading din automatically ek baar dobara fetch hota hai).

## 9. Upstox Plus Plan aur Brokerage Niyam
* Expired contracts ke liye Upstox Plus plan chahiye.
* Plus plan par options brokerage **Rs 30 prati order** hai, jabki Basic plan par **Rs 20 prati order** hai.
* Data utaarne ke baad Basic plan par wapas jaana owner ka apna faisla hai (plan switch par 24 ghante ka cooling-off hota hai).

## 10. DATA_RIGHTS UNVERIFIED aur Upstox ko Likhne wala Sawal
`DATA_RIGHTS` abhi `UNVERIFIED` hai. Upstox support ko yeh sawal verbatim bhejo aur jab tak likhit jawab na aaye, data kisi ko mat do:

> "I am a retail API user. May I download historical and expired-contract candle data (index, futures, options, India VIX, cash stocks) through the API and store it on my own machine for personal strategy backtesting? Is there any retention limit, any limit on derived indicators or backtest results, and any restriction on keeping the data after I stop using the plan? Please answer in writing."

## 11. AI ko Kya Paste Karna Hai (Aur Kya Kabhi Nahi)
* AI ko sirf yeh 4 files dikhao/paste karo:
  1. `probe_report.txt`
  2. `reports/health_summary.txt`
  3. `reports/health.csv`
  4. `reports/reconciliation.csv`
* **Kabhi bhi apna token, `.upstox_tokens` file, ya raw candle data chat mein paste mat karo.**

## 12. NSE Bhavcopy Kaise Daalni Hai (`ingest`)
Volume unit (lots vs units) confirm karne aur close/OI match karne ke liye NSE website se UDiFF F&O bhavcopy CSV download karke `external/bhavcopy/` mein rakho aur chalao:
```bash
python -m updl ingest external/bhavcopy/
python -m updl validate
```
Isse `reports/reconciliation.csv` ban jayegi aur volume ratio saaf dikh jayega.
