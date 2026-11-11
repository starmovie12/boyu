# DATA_REQUIREMENTS v2 (Parts 2 to 4) — corrected 11 Nov 2026 IST (see CHANGELOG_v2.md)

Tags: SOURCE = read on a page this session · STAFF = Upstox staff reply on the developer community (not a documentation clause) · UNVERIFIED = could not confirm · ESTIMATE = my arithmetic · ASSUMPTION = my choice · FILE-CLAIM = written in the owner's files, not re-verified by me.
"Secondary" next to SOURCE = news/broker page quoting an NSE circular; the circular itself was not opened.
Only Upstox pages and NSE/SEBI-quoting pages were used. Upstox page URLs are in the FACT LEDGER (Part 4).

Bottom line: Test window 11 May to 9 Nov 2026 = **126 trading days** (COUNTED by script: 131 weekdays minus 5 holidays inside the window; the 10 Nov holiday is after TEST_END). Upstox serves expired-contract 1-minute candles **only on the Upstox Plus plan** (SOURCE). Plan A is possible but **UNPROVEN until the Stage-0 probe runs**. Two new cautions: (1) the official Upstox rate-limit page showed **25/s, 250/min, 1,000 per 30 min** while broker pages quote 50/500/2,000, so v2 defaults to a safe 30 calls/min until the probe says otherwise; (2) a Plan A that only reaches ~20 weeks back cannot cover a 26-week window, so v2 adds **Plan A_PARTIAL**.

---

# PART 2. DATA REQUIREMENTS LIST

Window shorthand: TEST = 11 May to 9 Nov 2026 (parameter). WARM = pre-window history. All dates are parameters (see `data_manifest.yaml`).

| ID | Data item | Instruments | Granularity | Test window | Warm-up | Fields | Needed by | Source | Avail. | Pri | Est. API calls | Est. size | Tag |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| D01 | Spot candles | Nifty 50, Bank Nifty, Sensex | 1 min (derive 3, 15) | TEST | 45 calendar days (>=20 trading days: RV20, ADX carry-over, EMA/ATR/RSI/BB/Keltner) | ts_open, OHLC, vol, OI(0) | Din_Ka_Mausam, Range_Breakout, Timeframe_Match, EMA_Slope, RSI, Lambi_Wick, Squeeze, BOS/CHoCH/FVG, Shock_Mode, Teeno_Index, Chase_Guard | Upstox V3 historical candle, unit minutes | YES (Jan 2022 onward) | P0 | 27 (3 idx x ceil((45 warm + 183 test calendar days)/28) = 3 x 9; 28-day chunks) | 3 idx x ~48,000 rows ~ 2 MB parquet | SOURCE upstox.com/developer/api-documentation/v3/get-historical-candle-data, 11 Nov 2026 |
| D02 | Spot daily | same 3 | 1 day | TEST | from 1 Apr 2025 (>=252 trading days) | OHLC | gap vs prev close, prev-day range, RV20, regimes | V3 historical, unit days (1 decade per call) | YES | P0 | 3 | <0.1 MB | SOURCE same page |
| D03 | India VIX | `NSE_INDEX|India VIX` | 1 min | TEST | 45 cal days | OHLC | Shock_Mode, VIX_Gati, IV fallback | V3 historical | YES | P0 | 9 | <1 MB | SOURCE same page |
| D04 | India VIX daily | same | 1 day | TEST | from 1 Apr 2025 | OHLC | IVP fallback (VIX percentile), 250-day median regime | V3 historical | YES | P0 | 1 | <0.1 MB | SOURCE |
| D05 | Index futures (current + next month) | Nifty, BankNifty, Sensex | 1 min | TEST | 45 cal days | OHLC, vol, OI | Volume_Uchhal, VWAP (spot index has no volume), futures premium | expired futures via Plus endpoints; live contract via V3 | PARTIAL (expired needs Plus; BSE probably served, probe P15) | P0 (Plan B: still-listed futures only) | about 24 to 250 (cap dependent) | ~25 MB | SOURCE expired-instruments pages |
| D06 | Option contracts, P0 band | Nifty weekly (current + next on expiry days), BankNifty monthly, Sensex weekly; CE and PE | 1 min | TEST | 35 trading days (Nifty, Sensex) / 7 (BankNifty): needed for the same-DTE volume baseline | OHLC, vol, OI | Delta_Filter (band is delta-aware, see 6.2b), Premium_Range, Strike_Volume, Fake_Spike, Chase_Guard, OI_Deewar, Premium_Tezi, Pre_Breakout, Stop-Loss, Time-Stop, cost model | Expired: `/v2/expired-instruments/historical-candle`; live: V3 historical | PARTIAL (Plus needed; depth UNVERIFIED) | P0 | **about 5,000-6,500 (28-day cap) to about 22,000-28,000 (1-day cap)** (old 38,400 was inflated, see arithmetic below) | up to ~15M rows; parquet 0.2-0.4 GB, raw gz 0.5-1.0 GB | ESTIMATE (simulation below) |
| D07 | Expiry list + contract list per expiry | 3 indices | n/a | TEST+WARM | — | expiry_date, expired_instrument_key, strike, option_type | contracts "listed on day D" | `/v2/expired-instruments/expiries`, `/option/contract`, `/future/contract` | PARTIAL (Plus) | P0 | about 130 (Nifty ~33 + Sensex ~32 + BankNifty ~8 expiries with warm-up, ~24 futures lists, 3 expiry lists) | <5 MB | SOURCE backtesting page |
| D08 | Wide chain (ATM +/- 20 strikes) | 3 indices | 1 min | TEST | — | close, vol, OI | PCR_Trend, OI_Deewar walls, Max_Pain, GEX, Gamma_Flip | same as D06 | PARTIAL | P1 | about 2.4x D06 | 2.4x D06 | ESTIMATE |
| D09 | Cash stocks: heavyweights + 12 BankNifty banks | HDFCBANK, ICICIBANK, RELIANCE, INFY + banks | 1 min | TEST | 45 cal days | OHLC, vol | Heavyweight_Stocks, Manipulation_Cash_Volume, Breadth (BankNifty) | V3 historical, `NSE_EQ|<ISIN>` | YES | P0 | ~225 (~25 unique stocks x 9 chunks: warm-up + test) | ~15 MB | SOURCE |
| D10 | Cash stocks: Nifty50 + Sensex constituents | up to ~45 more unique (after removing D09 names) | 1 min | TEST | — | OHLC, vol | Index_Breadth, Manipulation_Cash_Volume (Nifty/Sensex expiry) | V3 historical | YES | P1 (list undecided, Sawal 16) | ~315 (N x 7) | ~35 MB | ESTIMATE |
| D11 | USD/INR, Brent, WTI, GIFT Nifty | Upstox global instruments | 1 min | TEST | — | OHLC | Macro_Divergence (shadow), GIFT info | V3 historical with global keys | PARTIAL (GIFT latency "120 Seconds", indicators "20 Seconds") | P2 | ~30 | <2 MB | SOURCE instruments page |
| D12 | Instrument master snapshot | NSE, BSE, global | file | today only | — | key, lot, tick, freeze, expiry, strike | key discovery | `assets.upstox.com/market-quote/instruments/exchange/{NSE,BSE,global}.json.gz` | YES (today; **no past masters**; excludes expired) | P0 | 0 (3 file fetches) | ~30 MB | SOURCE instruments page |
| D13 | NSE F&O UDiFF common bhavcopy (all strikes daily OHLC, vol, OI) | NSE F&O | 1 day | TEST+WARM | — | OHLC, OI, volume | daily cross-check AND volume-unit reconciliation (v2 8.5); Plan C core | nseindia.com/all-reports-derivatives | YES (UDiFF format since 8 Jul 2024) | P1 (P0 under Plan C) | manual/owner | ~5 MB | SOURCE secondary |
| D14 | NSE participant-wise F&O OI | FII, DII, Pro, Client | 1 day | TEST | — | long/short | FII_DII_Macro_Context | nseindia.com all-reports (file name UNVERIFIED) | UNVERIFIED | P1 | manual | <1 MB | UNVERIFIED |
| D15 | Session + holiday calendar | all classes | dated table | TEST+WARM | — | open, close, special flag | expected-bar count, Trading_Zone, Expiry_Filter | static (Part 5) + NSE circulars | YES | P0 | 0 | <0.1 MB | SOURCE secondary |
| D16 | Contract specs by date | 3 indices | dated table | TEST | — | lot, tick, freeze, step, expiry weekday | Balance_Ek_Lot, cost model | static + master snapshot + circulars | PARTIAL (past values from circulars only) | P0 | 0 | tiny | FILE-CLAIM |
| D17 | Dated charge table | NSE, BSE options | dated table | TEST | — | STT, txn, brokerage, GST, SEBI, stamp | Kharcha_Filter, cost model | static + official pages | PARTIAL | P0 | 0 | tiny | FILE-CLAIM |
| D18 | Event calendar, minute stamps | RBI, central banks, stats, exchange notices | events | TEST+WARM | — | event_ts_ist, class | Bade_Event_Lock, Event exit | owner builds from official pages | NO (no historical file exists) | P0 (gate) | manual | tiny | UNVERIFIED |
| D19 | Constituents + weights by date | Bank Nifty, Nifty50, Sensex | monthly/semiannual | TEST | — | symbol, ISIN, weight, from, to | Breadth, survivorship | NSE Indices / BSE factsheets | UNVERIFIED | P1 | manual | tiny | UNVERIFIED |
| D20 | India 10-year yield | India | daily | TEST | — | yield | Macro_Divergence | not seen in Upstox global list | UNVERIFIED | P2 | — | — | UNVERIFIED |

**Call-count arithmetic (ESTIMATE, rewritten in v2).** Trading days in TEST: 126 (counted). Nifty weekly expiries in window: 27 (26 Tuesdays plus the 10 Nov Tuesday that moves to Mon 9 Nov; 20 Oct holiday moves to 19 Oct). Sensex weekly: 26 Thursdays. BankNifty monthly expiries inside the window: **6** (26 May, 30 Jun, 28 Jul, 25 Aug, 29 Sep, 27 Oct). The November monthly (24 Nov is a holiday so it moves to 23 Nov) expires AFTER TEST_END and is a live contract fetched through V3, not an expired one (v1 counted it as a 7th expired expiry).
**What was wrong in v1:** it multiplied "union of strikes over the whole expiry" by "all days", i.e. it assumed every contract is downloaded on every day. The downloader selects a band per day (6.2b), so calls = sum over days of that day's band, and with a larger span cap each contract is one call per run of days.
Simulation (ESTIMATE; Nifty 25,000 step 50, Sensex 82,000 step 100, Bank Nifty 57,000 step 100, day range about 1%, VIX 14): contract-days at a 1-day cap = **about 18,700 with the old flat 6-step band, about 21,900 with the v2 delta-aware band** (the old band misses delta 0.45-0.65 strikes of a monthly contract three weeks out: roughly 9 steps ITM needed, band gave 6). Add the 35-day warm-up for Nifty/Sensex (about +25%): **about 22,000-28,000 calls at a 1-day cap; about 5,000-6,500 at a 28-day cap** (one call per contract run). The `plan` command prints the exact figure before any call.
- Rate: v1 mixed 66.7/min (runtime) with 45/min (prompt). v2 default is **30 calls/min** and at most 900 per rolling 30 minutes, because the official page lists 1,000 per 30 minutes (SOURCE) and other pages list 2,000 (conflict, UNVERIFIED which applies to expired endpoints). Runtime at 30/min: 5,000 calls = about 2.8 h; 22,000 = about 12.2 h; 28,000 = about 15.6 h. If the probe shows the higher limit, the owner may raise the rate.
- OAuth token (expired endpoints) dies at about 03:30 IST next day (FILE-CLAIM), so long jobs need two sessions: the downloader is resumable.

**Warm-up per item.** IVP 252 trading days of daily VIX BEFORE the decision day (the current day's close is never in the window; the bot's own IV record cannot be rebuilt that far back). Realized Volatility 20 days (daily). ADX joins previous day (45 calendar-day 1-minute warm-up). Strike-volume baseline: the literal rule (previous 5 trading days, same minute, same moneyness bucket, same days-to-expiry bucket) has NO matches for DTE 0-2 on weekly contracts, because every earlier day is a different DTE. v2 writes two columns: same-DTE (previous 5 occurrences of that DTE bucket = about 7 weekly cycles = 35 trading days of warm-up for Nifty/Sensex; Bank Nifty monthly DTE 0 has no history) and any-DTE (7 days is enough). Daily warm-up is now RELATIVE: TEST_START minus 400 calendar days (v1 fixed 1 Apr 2025, which silently starves IVP if TEST_START is moved back). MISSION regime uses the trailing-250-day VIX median.

## Machine-readable manifest
See `data_manifest.yaml` (same IDs D01 to D20).

---

# PART 3. CHECK-BY-CHECK BACKTESTABILITY MATRIX

The Check Suchi table in the Idea file has **48 rows** (v1 counted them; the brief said "about 52"). Rows 1-48 below are the 48 checks; rows 49-56 are 8 extra RULES (entry, stops, expiry shift, cost model, Monte Carlo), so the table has 56 rows in all. **11 rows are marked SH** (18, 26, 27, 34, 35, 36, 37, 38, 39, 41, 46); v1 said 14, which does not match its own table: re-check the three missing ones against the Idea file.
Plan A = Upstox Plus, expired 1-minute candles work. Plan B = only still-listed contracts. Plan C = spot/futures/VIX/stocks 1-minute + NSE daily bhavcopy.
Candles carry no bid, ask, depth or ticks. Everything needing those is Recorder-only.

| # | Check / rule | Data needed | Backtestable on Upstox history? | Plan A / B / C | Proxy or workaround | Risk of a false result |
|---|---|---|---|---|---|---|
| 1 | Din_Ka_Mausam_Gate | spot 1m->3m ADX14 with previous day, gap, range; GIFT info only | YES | OK / OK / OK | none | ADX warm-up; GIFT latency 120 s so keep info-only |
| 2 | Trading_Zone_Gate | clock, session table | YES | OK / OK / OK | zone times need re-fit for 15:40 close (3 Aug) | wrong close time before/after 3 Aug |
| 3 | Bade_Event_Lock | rebuilt event calendar, post-event IV | WITH PROXY | OK (IV from candles) / calendar only / calendar only | owner-built calendar; IV from last price | calendar minute errors; no historical calendar exists |
| 4 | Shock_Mode | 3 spot 1m + VIX 1m | YES | OK / OK / OK | none | VIX 1m stamping |
| 5 | Delta_Filter | option close, spot, T, r, IV model | WITH PROXY | OK / partial / NO | IV and delta COMPUTED (model fixed, Sawal 115) | model mismatch with broker Greeks; last price not mid |
| 6 | Premium_Range_Gate | premium + ask + buffer | WITH PROXY | OK / partial / NO | close + modelled half-spread (PROXY) | ask unknown |
| 7 | Balance_Ek_Lot_Gate | premium x lot, capital parameter | YES | OK / OK / partial | dated lot table | simulated balance only |
| 8 | Expiry_Filter | expiry list, clock | YES | OK / partial (no expired list) / partial | Get Expiries API; bhavcopy expiries | shifts on holidays |
| 9 | IV_Overpriced_Filter | IV, RV20, event dates | WITH PROXY | OK / partial / NO | IV from last price; spread rule becomes NA | stale last price |
| 10 | IVP_Filter | 252-day IV record | WITH PROXY | VIX percentile / VIX percentile / VIX percentile | India VIX percentile over the PREVIOUS 252 closes, tagged PROXY | VIX is index-wide, not strike IV; using today's close would be look-ahead |
| 11 | Liquidity_Filter | depth (Market_Depth_Levels) | NO: Recorder only | proxy only | volume floor + conservative spread (2 x worst observed) tagged PROXY | can only reject ideas, never approve |
| 12 | Kharcha_Filter | delta, theta est., dated charges, slippage | WITH PROXY | OK / partial / NO | cost table static | charge constants are FILE-CLAIMs |
| 13 | Strike_Volume_Filter | option vol, 5-day same-time baseline by moneyness + DTE bucket | YES (two baseline columns) | OK / NO / NO | any-DTE baseline if same-DTE history is missing | literal 5-day same-DTE rule has no matches for DTE 0-2 on weekly contracts; strike-name baseline would be wrong |
| 14 | Fake_Spike_Filter | neighbour strikes (+/-2) same minute | YES | OK / partial / NO | none | thin neighbours -> missing bars |
| 15 | Chase_Guard | premium rise since breakout candle close, ATR, wall | WITH PROXY | OK / partial / NO | candle close for bid/ask mid | pullback rule needs definition |
| 16 | OI_Deewar_Doori_Gate | strike OI (wide chain), Delta | WITH PROXY | OK / partial / NO | OI from option candles | intraday OI timing (is OI at close of that minute?) UNVERIFIED |
| 17 | Manipulation_Cash_Volume_Check | cash stocks 1m traded value, options OI | WITH PROXY | OK / OK (stocks) / partial | "ajeeb OI" threshold undefined | stock list undecided -> NA-Data |
| 18 | Manipulation_GEX_Mausam_Check (SH) | chain OI x gamma | WITH PROXY | OK / NO / NO | GEX proxy, dealer assumption | assumption-based |
| 19 | Manipulation_Settlement_Check | last-hour cash stocks; settlement averaging | WITH PROXY | partial / partial / NO | cash F&O stocks stop continuous trading at 15:15 from 3 Aug (CAS 15:15-15:35) so candles miss the auction | method changes 3 Aug and 7 Dec |
| 20 | Lambi_Wick_Check | breakout candle OHLC | YES | OK / OK / OK | none | none |
| 21 | Tick_Heartbeat_Check | tick timestamps | NO: Recorder only | not testable | assume PASS, flagged | hides feed-gap days |
| 22 | Broker_Exchange_Gadbad_Check | connection, halts, circuits, rejects | NO: Recorder only | partial | halt detection from zero-volume runs | real glitches invisible |
| 23 | Call_Put_Takraav_Gate | internal trade state | YES | OK / OK / OK | none | none |
| 24 | Premium_Tezi_Check | premium speed vs 5-minute average | WITH PROXY | OK / partial / NO | 1-minute resolution only | sub-minute speed lost |
| 25 | Volume_Uchhal_Check | futures volume, VWAP from day start, futures premium | YES | OK / partial (live near-month future only) / partial | VWAP on futures (index has no volume) | expired futures need Plus |
| 26 | Rate_Update_Gati_Check (SH) | ticks per second | NO: Recorder only | not testable | none | none |
| 27 | Iceberg_Order_Check (SH) | depth | NO: Recorder only | not testable | none | none |
| 28 | Trap_Detection_Check | breakout persistence from first tick | WITH PROXY | OK / OK / OK | 1-minute closes | first-tick timing lost |
| 29 | Range_Breakout_Check | closed-candle ranges, opening range, previous-day range | YES | OK / OK / OK | none | unfinished candle (banned) |
| 30 | Timeframe_Match_Check | 3 min vs 15 min | YES | OK / OK / OK | derive from 1m | alignment of 15m buckets |
| 31 | EMA_Slope_Check | EMA 9/21 | YES | OK / OK / OK | none | warm-up |
| 32 | RSI_Momentum_Check | RSI | YES | OK / OK / OK | none | warm-up |
| 33 | Ulta_Palat_Reversal_Chart_Check | trap, RSI divergence, VWAP | YES | OK / OK / OK | pivots only after closed candles | pivot look-ahead |
| 34 | Volatility_Squeeze_Check (SH) | BB 20/2, Keltner 20/1.5 ATR | YES | OK / OK / OK | none | none |
| 35 | Order_Block_BOS_CHoCH_FVG_Check (SH) | closed candles | YES | OK / OK / OK | none | swing confirm delay |
| 36 | Max_Pain_Shift_Check (SH) | full-chain OI, expiry day | WITH PROXY | OK (wide chain) / NO / EOD only | P1 wide chain | cost: ~2.4x calls |
| 37 | GEX_Positioning_Check (SH) | chain OI, gamma | WITH PROXY | OK / NO / NO | GEX proxy | dealer assumption |
| 38 | Gamma_Flip_Check (SH) | same | WITH PROXY | OK / NO / NO | proxy | same |
| 39 | IV_Skew_Check (SH) | call vs put IV | WITH PROXY | OK / NO / NO | IV from last price | stale last price |
| 40 | OI_Deewar_Short_Covering_Check | minute OI change on major strikes | WITH PROXY | OK / NO / NO | OI per candle | OI timing |
| 41 | Pre_Breakout_Alert_Check (SH) | premium + OI of 2 neighbours | WITH PROXY | OK / partial / NO | none | thin strikes |
| 42 | PCR_Trend_Check | chain OI | WITH PROXY | OK (wide chain) / NO / EOD only | P1 | band truncation biases PCR |
| 43 | Teeno_Index_Disha_Check | 3 spot 1m | YES | OK / OK / OK | none | Sensex session time UNVERIFIED |
| 44 | Index_Breadth_Check | constituents, weights by date, stock 1m | WITH PROXY | OK / OK / OK | weights from external file | survivorship; list undecided |
| 45 | Heavyweight_Stocks_Check | stock 1m | YES | OK / OK / OK | none | halted stock handling |
| 46 | Macro_Divergence_Check (SH) | USD/INR, crude, 10Y yield | WITH PROXY | partial / partial / partial | Upstox global candles (delayed); yield UNVERIFIED | latency, missing yield |
| 47 | VIX_Gati_Term_Structure_Check | VIX 1m, near vs next IV | WITH PROXY | OK / partial / VIX only | term structure computed | needs next expiry IV |
| 48 | FII_DII_Macro_Context_Check | previous-evening participant data | YES (non-Upstox) | OK / OK / OK | usable from NEXT session only | publication lag |
| 49 | Entry (limit + slippage buffer) | OHLC, vol, spread model | WITH PROXY | OK / partial / NO | fill rule below | optimistic fills |
| 50 | Stop-Loss 25-30% of premium | 1m OHLC | WITH PROXY | OK / partial / NO | same-minute stop and target: stop first | gap-through |
| 51 | Trailing stop | 1m OHLC | WITH PROXY | OK / partial / NO | closed-bar only | same-minute ambiguity |
| 52 | Time-Stop 8-15 min | 1m | YES | OK / partial / NO | none | none |
| 53 | Expiry shift 13:30 (next expiry) | next-expiry candles from 13:30 | YES | OK / NO / NO | none | next expiry missing under Plan B |
| 54 | Balance_Ek_Lot (as a rule) | see #7 | YES | see #7 | none | none |
| 55 | Cost model | dated charges, lot table | YES | OK / OK / OK | constants, not downloads | constants are FILE-CLAIMs |
| 56 | Monte Carlo inputs | trade list from backtest | needs trades | OK only if >=150 trades | block bootstrap | too few trades |

**Fill rule for zero-volume or stale minutes (PROXY):** a minute with volume 0 or no candle is NOT fillable. Fill = next bar with volume > 0, price = max(close, modelled ask). Never fill at a last-traded price carried forward.

---

# PART 4. PLANS, RISKS, LAW, FACTS

## 4.1 Plan decision
**Plan A / A_PARTIAL (conditional).** Upstox documents expired-contract candles for 1, 3, 5, 15, 30 minute and day, with volume and OI (SOURCE). Condition: Upstox Plus plan, which is free to activate initially (SOURCE, may become chargeable). Open items: how far back expired intraday data goes (UNVERIFIED; the expiry lists for Sensex were seen starting around Oct 2024 in a community post, intraday depth unclear), the max days per call for expired 1-minute (**STAFF: keep minute-level requests to at most one month; the official page states no cap, so the probe tests 1, 5 and 28 days**), whether BSE (Sensex) expired contracts return candles (probably yes: the expiries endpoint answered for BSE_INDEX|SENSEX in a community post; the probe confirms), and whether the Analytics Token is accepted for expired endpoints (UNVERIFIED; it is not on the supported list). Known flakiness: community posts report UDAPI100500 "Something went wrong" on very large ranges.
**Stage 0 decides.** Probe passes AND the oldest returned date reaches back to TEST_START minus the option warm-up -> Plan A. Probe passes but depth is shorter (the window is 26 weeks, v1's 20-week test would have wrongly said Plan A) -> **Plan A_PARTIAL**: download from the oldest date, shrink the window, report the new trading-day count. Expired calls fail -> **Plan B** (only still-listed contracts): option history inside the window is incomplete; every option-dependent check dies for the days before the oldest available contract; start the Recorder NOW. Spot/futures/VIX/stocks checks (about 20 of 48; live near-month futures partly) survive. Sensex option history is the first thing to go. Plan C: no intraday option history; use bhavcopy daily; can reject an idea, never approve it.

## 4.2 Is 6 months enough? (MISSION 8.2 / 8.3)
- TEST window 11 May to 9 Nov 2026 = **126 trading days** (COUNTED: 131 weekdays minus 5 in-window holidays; v1 wrote "minus 6", which would give 125; 11 Nov is a trading day, so 127 once it is added after it closes). G1 needs >=125 and drops NOT OK days. **BORDERLINE:** two NOT OK days break it.
- Holdout = last 20% = 25 days (5 Oct to 9 Nov). The floor is 25 days AND 30 trades. 30 trades in 25 days depends on the bot's trade rate (unknown). **Likely UNDERPOWERED** for 150 trades.
- Extension: F-DATAAGE says no older than the latest expiry-day or lot-size regime change. Nifty/NSE index expiries moved to Tuesday from Sep 2025 (SOURCE secondary); BankNifty lot became 30 from Jan 2026 (FILE-CLAIM). Cleanest extension: start **1 Jan 2026** (COUNTED: +84 trading days, total 210; holdout 20% = 42 days). Note the daily warm-up is now relative (TEST_START minus 400 days), so moving TEST_START needs no other edit. 12 or 18 months would cross regime changes; the idea file's 18-month proposal conflicts with F-DATAAGE unless dated tables handle every change. Also check Plus expired-data depth first.

## 4.3 Holdout partition
Stored data goes into two physical trees: `store/dev/` and `store/holdout/` (dates >= HOLDOUT_START). The loader library refuses `holdout/` unless env `OPEN_HOLDOUT_ONCE=1`, and appends a hash-chained line to `holdout_log.jsonl`; a second open is refused. The holdout partition marker `HOLDOUT_SEALED.json` carries the sha256 of every file.

## 4.4 Backtest-lie register (with mitigation, all built into Part 5)
| Lie | Mitigation |
|---|---|
| Look-ahead via candle timestamps | every candle has ts_open and ts_close; indicators use bars with ts_close <= decision time |
| Unfinished candles | derived 3m/15m bars only emitted after their last 1m bar closed |
| Survivorship in contracts | contract set for day D = contracts whose first candle <= D and expiry >= D from the expired-contract list; never today's master |
| Survivorship in constituents | dated constituent file; no file -> check = NA-Data |
| Stale last-traded price on zero-volume minutes | `is_stale` flag; not fillable |
| Mid-price fills with no spread | modelled half-spread tagged PROXY |
| Same-minute stop and target | stop first |
| Session-time change 3 Aug 2026 | dated session table; expected bars 375 before, 385 after for F&O index contracts, 360 for cash F&O stocks after 3 Aug (ESTIMATE, to be confirmed by probe) |
| Expiry shifts on holidays | take expiry dates from the Get Expiries API, never from weekday arithmetic |
| Muhurat and special sessions mixed in | `session_type` column; excluded from normal-day counts |
| End-of-day OI used as intraday | OI column carries `oi_timing = per_candle_UNVERIFIED` until the probe proves it |
| Lot-size and freeze-limit changes | dated specs table |
| Charge changes | dated charge table |
| Timezone/daylight errors | all stamps parsed with +05:30; IST has no daylight saving |
| Duplicate bars | dedupe on (instrument, ts_open), keep and log |
| Partial downloads accepted silently | per-chunk expected-bar check; chunk marked INCOMPLETE; day NOT OK |
| Synthetic or interpolated values hidden in real data | forbidden; any fill needs a visible `is_filled` column |
| Fitting parameters on the holdout | physical separation + one-time open log (4.3) |
| Daily values that include today's close used intraday (VIX percentile, RV20, ATM IV at 14:00) | `available_from_ts` on every derived row; IVP uses the previous 252 closes; test T18 truncation-invariance |
| Index bars flagged "stale" because index volume is 0 | `has_volume` column; is_stale only where volume exists |
| Strike band too narrow for long-dated contracts | delta-aware band margin (grows with sqrt(time)); test T19 |
| Volume baseline with no matching history on weekly contracts | two baseline columns + 35-day warm-up; `baseline_status` |
| Unknown volume unit (units vs lots) | NSE bhavcopy reconciliation, flag VOLUME_UNIT_SUSPECT |
| Rate limit mis-set (documentation conflict) | conservative default 30/min, 900 per 30 min; probe logs headers |

## 4.5 DATA_RIGHTS verdict: **UNVERIFIED**
I found no clause on official Upstox pages that says storing API market data locally for personal backtesting is allowed or forbidden. What I did find: a Team Upstox reply on the developer community saying it is allowed to distribute market data to Upstox users and that "no additional approval" is required (a forum post, not a ToS clause). NSE has a separate "Data Sharing Policy for Research and Analysis" document (SOURCE); its commercial-use clause was not read (Sawal 140 stays open).
Safe default: store locally, personal use, derived outputs only if ever shared, never share raw candles (this is the MISSION "UNVERIFIED" row).
**Exact question to send to Upstox (owner sends it, not the AI):** "I am a retail API user. May I download historical and expired-contract candle data (index, futures, options, India VIX, cash stocks) through the API and store it on my own machine for personal strategy backtesting? Is there any retention limit, any limit on derived indicators or backtest results, and any restriction on keeping the data after I stop using the plan? Please answer in writing."

## 4.6 Paid options (OWNER DECISIONS, never assumed)
1. Upstox Plus (needed for expired contracts). Free to activate initially; may become chargeable; **options brokerage is Rs 30 per order on Plus versus Rs 20 on Basic** (SOURCE upstox.com/plus), plan switches have a 24-hour cooling-off (SOURCE T&C). Cheapest route: activate Plus, download, switch back to Basic before real trading. ESTIMATE of cost for data: Rs 0 now. Rs 30 per order matters only if the owner places orders while on Plus.
2. Purchased licensed historical option data (tick or 1-minute with bid/ask): price UNVERIFIED; not researched; only if Plan B.
3. A VPS or static IP: not needed for market-data GET calls with the Analytics Token (SOURCE).

## 4.7 FACT LEDGER (reusable by MISSION T03)

| Name | Value | Unit | Tag | Source URL | Date read | Effective |
|---|---|---|---|---|---|---|
| V3 historical candle endpoint | `GET /v3/historical-candle/:instrument_key/:unit/:interval/:to_date/:from_date` | path | SOURCE | https://upstox.com/developer/api-documentation/v3/get-historical-candle-data | 2026-11-11 | current |
| V3 minutes availability | from Jan 2022; interval 1..300 | — | SOURCE | same | 2026-11-11 | current |
| V3 max range, minutes 1-15 | 1 month per call | span | SOURCE | same | 2026-11-11 | current |
| V3 max range, minutes >15 and hours | 1 quarter per call | span | SOURCE | same | 2026-11-11 | current |
| V3 days / weeks / months | from Jan 2000; days = 1 decade per call | — | SOURCE | same | 2026-11-11 | current |
| Over-limit behaviour | API throws an invalid-request error | — | SOURCE | same | 2026-11-11 | current |
| Candle array | [timestamp(start of candle, ISO +05:30), O, H, L, C, volume, OI] | — | SOURCE | same | 2026-11-11 | current |
| India VIX key | `NSE_INDEX|India VIX` | key | SOURCE | same | 2026-11-11 | current |
| Global instruments | GIFT NIFTY, USD INR, Oil Brent/WTI in global.json.gz; GIFT latency 120 s | — | SOURCE | https://upstox.com/developer/api-documentation/instruments | 2026-11-11 | current |
| Intraday V3 | `GET /v3/historical-candle/intraday/:instrument_key/:unit/:interval` (current day) | path | SOURCE | https://upstox.com/developer/api-documentation/v3/get-intra-day-candle-data | 2026-11-11 | current |
| Expired APIs need Plus | error UDAPI1149 otherwise | — | SOURCE | https://upstox.com/developer/api-documentation/get-expired-historical-candle-data | 2026-11-11 | current |
| Expired candle endpoint | `GET /v2/expired-instruments/historical-candle/:expired_instrument_key/:interval/:to_date/:from_date` | path | SOURCE | same | 2026-11-11 | current |
| Expired intervals | 1minute, 3minute, 5minute, 15minute, 30minute, day | — | SOURCE | same | 2026-11-11 | current |
| Expired max range, 1minute | official page states none; Upstox staff told a user to limit minute-level ranges to at most one month; both dates required | span | STAFF (community.upstox.com/t/error-udapi100500-something-went-wrong-please-contact-us/14384) | same | 2026-11-11 | probe decides 1/5/28 |
| Expired depth | not stated (docs sample uses a 2020-02-24 start for 30minute); Sensex expiry list seen starting 2024-10-04 in a community post | — | UNVERIFIED | same | 2026-11-11 | probe P19 |
| BSE expired contracts | expiries endpoint answers for BSE_INDEX\|SENSEX (community post); candles not yet confirmed | — | STAFF context, probe P15 | https://community.upstox.com/t/getting-historical-f-o-data-before-first-expiry-2024-10-04/9491 | 2026-11-11 | probe decides |
| Expired key format | `NSE_FO|<token>|dd-mm-yyyy` | key | SOURCE | https://upstox.com/developer/api-documentation/backtesting | 2026-11-11 | current |
| Get Expiries | `GET /v2/expired-instruments/expiries?instrument_key=` | path | SOURCE | same | 2026-11-11 | current |
| Expired option contracts | `GET /v2/expired-instruments/option/contract?instrument_key=&expiry_date=` | path | SOURCE | same | 2026-11-11 | current |
| Expired future contracts | `GET /v2/expired-instruments/future/contract` (same params) | path | SOURCE | https://upstox.github.io/upstox-python/docs/ExpiredInstrumentApi.html | 2026-11-11 | current |
| Plus: free to activate initially | "if chargeable, notified in advance" | — | SOURCE | https://upstox.com/files/terms-and-condition/plus-pack.pdf | 2026-11-11 | current |
| Plus: options brokerage | Rs 30 per order (Basic Rs 20) | Rs | SOURCE | https://upstox.com/plus | 2026-11-11 | current |
| Plus switch cooling-off | 24 hours | hours | SOURCE | plus-pack.pdf | 2026-11-11 | current |
| Rate limit, official page text | **25/s, 250/min, 1,000 per 30 min per API per user** | requests | SOURCE (page text seen via search 11 Nov) | https://upstox.com/developer/api-documentation/rate-limiting | 2026-11-11 | CONFLICT |
| Rate limit, other pages | 50/s, 500/min, 2,000 per 30 min for standard APIs incl. historical candles (broker review page; community posts also mention 2,000/30 min) | requests | SOURCE secondary | https://www.chittorgarh.com/broker/upstox/api-for-algo-trading-review/33/ | 2026-11-11 | CONFLICT, probe + owner decide |
| Rate-limit headers, 429 behaviour | not documented; "temporary suspension" | — | UNVERIFIED | same | 2026-11-11 | — |
| Analytics Token | free, 1-year, read-only, GET only; one per account; new one revokes the previous Analytics Token | — | SOURCE | https://upstox.com/developer/api-documentation/analytics-token | 2026-11-11 | current |
| Analytics Token supported | Historical candle V3, quotes, option chain, option contracts, option Greeks, market status (no expired-instruments APIs listed) | — | SOURCE | same | 2026-11-11 | current |
| Market data without Static IP | yes (public GET APIs) | — | SOURCE | https://upstox.com/developer/api-documentation/announcements/analytics-token-static-ip | 2026-11-11 | current |
| Extended token (all apps) | 1 year, read-only account-trade APIs | — | SOURCE | https://upstox.com/developer/api-documentation/announcements/extended-tokens-all-apps | 2026-11-11 | 2026-03-11 |
| Option chain API | active contracts only; live greeks; no historical chain endpoint | — | SOURCE (nav list) | https://upstox.com/developer/api-documentation/option-chain | 2026-11-11 | current |
| Instrument master | daily at ~6 AM; BOD excludes expired contracts; has lot_size, freeze_quantity, tick_size, expiry; no past snapshots | — | SOURCE | https://upstox.com/developer/api-documentation/instruments | 2026-11-11 | current |
| NSE F&O close | 15:40 from 3 Aug 2026 (was 15:30); trade modification end 16:15 | time | SOURCE secondary (circular NSE/FAOP/74467) | https://bigul.co/blog/market-update/nse-revises-fo-trading-hours-extends-market-close-by-10-minutes-from-august-3-2026 | 2026-11-11 | 2026-08-03 |
| Cash CAS | F&O stocks: continuous trading ends 15:15, auction to 15:35 from 3 Aug 2026; morning pre-open realignment from 7 Sep 2026 (details UNVERIFIED) | time | SOURCE secondary | https://www.multibagg.ai/market-pulse/articles/nse-fo-closing-auction-hours-cmscwd1a610h00zqox7g9fa3o | 2026-11-11 | 2026-08-03 |
| Derivative VWAP window | 15:10 to 15:40 from 3 Aug 2026 | time | SOURCE secondary | https://5paisa.com/blog/nse-extends-fo-trading-by-10-minutes | 2026-11-11 | 2026-08-03 |
| Holidays in window | 28 May, 26 Jun, 14 Sep, 2 Oct, 20 Oct 2026 (NSE/CMTR/71775 and /72260); 10 Nov and 24 Nov are holidays too but fall after TEST_END | dates | SOURCE secondary | https://www.multibagg.ai/market-pulse/articles/nse-bse-holiday-calendar-cmum7opyi00010agmbmglas5z | 2026-11-11 | 2026 |
| Muhurat | Sun 8 Nov 2026 (inside the window; excluded from normal-day counts), timings not yet notified when quoted | date | SOURCE secondary | same | 2026-11-11 | 2026-11-08 |
| Expiry on holiday | previous trading day (example: 31 Mar 2026 moved to 30 Mar) | rule | SOURCE secondary | same | 2026-11-11 | current |
| NSE index/stock derivatives expiry weekday | Tuesday since Sep 2025 | rule | SOURCE secondary | same | 2026-11-11 | 2025-09 |
| Special Sunday session | 1 Feb 2026 (Budget), 09:15-15:30, no T+0 | date | SOURCE secondary | same | 2026-11-11 | 2026-02-01 |
| UDiFF common bhavcopy | replaced old F&O bhavcopy from 8 Jul 2024 | format | SOURCE | https://nseindia.com/all-reports-derivatives | 2026-11-11 | 2024-07-08 |
| Lot sizes Nifty 65 / BankNifty 30 (Jan 2026) / Sensex 20 | — | units | FILE-CLAIM | owner's Idea file | 2026-11-11 | not re-verified |
| Freeze limit Nifty 1,800 / BankNifty 900 from 2 Nov 2026 | — | units | FILE-CLAIM | NSE/FAOP/77189 as quoted in the file | 2026-11-11 | 2026-11-02 |
| STT option sell 0.15% from 1 Apr 2026; NSE option txn 0.03553% from 1 Mar 2026 | — | % | FILE-CLAIM | NSE/FATAX/73524, NSE/FA/73061 as quoted in the file | 2026-11-11 | not re-verified |
| BSE (Sensex) option charges | differ from NSE; value not found | — | UNVERIFIED | — | 2026-11-11 | — |
| TEST trading days | 126 (11 May to 9 Nov 2026) | days | COUNTED (script) | 131 weekdays minus 5 holidays | 2026-11-11 | — |
| Volume baseline warm-up | same-DTE needs 5 prior occurrences = about 35 trading days for weekly contracts | days | ASSUMPTION (logic) | this file, Part 2 | 2026-11-11 | — |
