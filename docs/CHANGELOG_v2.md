# CHANGELOG v1 -> v2 (11 Nov 2026)

Teeno files alag rakhi hain, kyunki unka kaam alag hai: `PROMPT_02` naye chat mein akeli paste hoti hai, `data_manifest.yaml` machine-readable source of truth hai (prompt ke andar wahi file embed hai, script se match check kiya), `DATA_REQUIREMENTS.md` aapke padhne ka research reference hai.

Tags: COUNTED = script se gina. VERIFIED-WEB = official page / Upstox staff reply se. LOGIC = soch se pakdi galti.

## A. Badi galtiyan (result galat ya download fail kara sakti thi)

| # | Galti | Kahan thi | Fix |
|---|---|---|---|
| 1 | Rate limit conflict: official Upstox page par **25/s, 250/min, 1,000 per 30 min** dikha (VERIFIED-WEB), files mein 50/500/2,000 likha tha. Prompt ka 45 calls/min is limit ko todta (1,000/30 min = 33/min). | Prompt 3.6, Requirements ledger | Default **30 calls/min, max 900 per 30 min**, manifest mein badalne layak; probe headers log karta hai; owner khud badhata hai. |
| 2 | Strike band **delta-unaware** tha: "6 steps" ka claim ki delta 0.45-0.65 cover hoga, Bank Nifty monthly (3 hafte door) ke liye galat. Wahan ~9 steps ITM chahiye. | Prompt 6.2b | Margin = max(6 steps, 0.6 x spot x VIX/100 x sqrt(DTE/365)). Test T19. |
| 3 | Call-count estimate (38,360) **galat tareeke se** nikla: har contract ko har din ginta, jabki downloader din-ba-din band chunta hai. Simulation: ~22k-28k calls (1-day cap), ~5k-6.5k (28-day cap). Bank Nifty November monthly ko expired expiry ginta tha, jo TEST_END ke baad expire hota hai. | Requirements Part 2 | Naya arithmetic + exact `plan` command. |
| 4 | **Plan A criteria** sirf "20 hafte peeche tak data", par window 26 hafte ki hai. Plan A galat chunta. | Prompt 4, Requirements 4.1 | Naya **Plan A_PARTIAL** + depth ladder probe P19 (TEST_START tak ka test). |
| 5 | `is_stale = (v == 0)` har **index / VIX bar ko stale** bana deta (index ka volume 0 hota hai). | Prompt 6.5 | `has_volume` column; stale sirf jahan volume hota hai. Test T21. |
| 6 | **Look-ahead**: `ivp_vix_PROXY` din ka apna VIX close use karta; RV20 aur 14:00 ATM IV intraday par kab "pata" hote, yeh nahi likha tha. | Prompt 9.4, 9.5 | Har derived row par `available_from_ts`; IVP pichhle 252 closes par. Test T18 (data kaatkar dobara derive, values same aani chahiye). |
| 7 | **Volume baseline** ka rule (pichhle 5 din, wahi DTE bucket) weekly contract par DTE 0-2 ke liye kabhi match hi nahi karta. 7-din warm-up kaafi nahi. | Prompt 9.6, manifest | Do columns (same-DTE, any-DTE); Nifty/Sensex ka option warm-up 35 din. |
| 8 | `WARMUP_DAILY_FROM` fixed date tha: TEST_START peeche karte hi IVP chupchap kharab. | manifest | `WARMUP_DAILY_CALENDAR_DAYS: 400` (TEST_START se relative). |
| 9 | Chunking calendar days mein (T01: 62 chunks), weekend ke bekaar calls. | Prompt T01 | Sirf trading days. |
| 10 | Greps "place", "orders" `replace`, `recorder` jaise normal shabdon par fail karte. | Prompt rule 1, acceptance 3 | Allowlist test (primary) + word-boundary grep. |

## B. Ginti / numbers ki galtiyan (COUNTED)

- "126 = weekdays minus the **6** holidays" galat: window ke andar sirf **5** holiday hain (10 Nov TEST_END ke baad). 131 - 5 = 126 sahi, wajah galat thi. Holdout 25 din sahi nikla. Extension 1 Jan: +84, total 210, holdout 42: sahi.
- "**14** Shadow-Mode rows" lekin table mein sirf **11** SH hain (18, 26, 27, 34, 35, 36, 37, 38, 39, 41, 46). Baki 3 Idea file se match karne hain.
- Matrix mein 56 rows hain (48 checks + 8 rules); intro mein sirf 48 likha tha.
- D09 estimate 180 (25 x 7) galat, warm-up ke saath 25 x 9 = 225. D10: yaml mein 600, md mein 420: ab dono ek formula (N x 7).
- Runtime: md mein 66.7/min, prompt mein 45/min, README mein "66 minute se 10 ghante": ab sab 30/min par ek jaisa.
- BankNifty 7 expiries ki ginti: 6 expired + 1 live.

## C. Prompt ke andar contradictions / kami

- Prompt ka manifest (D01-D12) aur `data_manifest.yaml` (D01-D20) alag the: ab ek hi file embed, match verified.
- D05/D06 plan [A] tha par matrix Plan B "partial" bolta tha: ab `plan: [A,B]` + `b_mode: still_listed_only`.
- Sessions table `to: 2026-11-09` par khatam hoti thi: probe agli tarikh par fail hota. Ab `to: null`. `cash_non_fo_stock` class add.
- Expired 1-minute span: Upstox staff ka bayan (max 1 mahina) add, probe ab 1/5/28 din test karta hai.
- Contract key encoding: space (`Nifty 50` -> `%20`) pehle cover nahi tha. Test T04.
- Master `expiry` millisecond ko IST date mein badalna (UTC se din galat ho sakta hai). Test T20.
- Candle rows 6 elements (OI nahi) aur null volume handle nahi the; ab handle, kabhi 0 nahi banta.
- Resume state ab (instrument, din) coverage par: chunk size badalne par dobara download nahi.
- Raw file ka naam fetch_run_id ke saath (re-fetch ho sake); parquet har chunk ke bajay har (table, underlying, din) ek file (Windows par hazaaron chhoti files nahi).
- Windows support: `kill -9`, `sha256sum`, `chmod` par nirbharta hata di; `verify` command, `Popen.kill()` test.
- IV model: forward ab put-call parity se (fallback cost-of-carry); `forward_source` column.
- Time: "4 baje ke baad chalao" galat tha (F&O 15:40 tak, modification 16:15). Ab shaam 6 ke baad + agli subah last din dobara fetch.
- Naye safeguards: circuit breaker (3 x 429), timeouts, Ctrl+C, `--stop-at`, `--max-calls`, token-file permission warning.
- **Bhavcopy reconciliation** (naya 8.5): candle close/volume/OI ko NSE bhavcopy se match karta hai aur volume unit (units vs lots) ka sawal hal karta hai.
- Honest reporting rule: jo command AI chala nahi paya, "NOT RUN" likhe; pass ka jhootha claim nahi. Dependency versions invent nahi.
- Build order M1-M8 aur DECISIONS.md.
- Tests 17 -> 22 (T18 causality, T19 band, T20 expiry ms, T21 index stale, T22 reconciliation).

## D. Jo abhi bhi aapko decide / confirm karna hai (mere haath mein nahi)

1. Rate limit asli kaun sa hai (probe ke header log se dekhna, phir manifest mein `RATE_CALLS_PER_MIN`).
2. Expired data kitni peeche tak milta hai: sirf probe batayega (Plan A ya A_PARTIAL).
3. Baki 3 Shadow-Mode checks Idea file se match karna.
4. Daily OAuth token: Upstox ke login flow se aap khud banayenge; program token nahi banata (rule 2).
5. DATA_RIGHTS: Upstox ko likhit sawal bhejna (README mein verbatim).
6. Lot sizes, freeze limits, STT/charges abhi bhi FILE-CLAIM hain (Idea file se aaye, maine verify nahi kiye).
