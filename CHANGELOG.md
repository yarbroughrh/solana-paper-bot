# CHANGELOG — Solana memecoin PAPER bot (simulation only; no wallet, no keys, no transactions)

All times ET (America/New_York).

## v1 — 2026-10-21 15:30 ET (archived: archive/bot_v1_2026-10-21.py, README_v1.md)
Single-process loop, 20 s cycle. Three desks S1_launch / S2_dip / S3_breakout. Hard stop -8%, TP1 +25% (sell 50%), trail 15%.
Result before v2: 6 closed trades, all hard stops (incl. umi -99% while bot was down).

## v2 — 2026-10-21 18:16 ET (previous agent session; reconstructed from diff vs archive, no changelog was written then)
- Supervisor/worker split: `python3 bot.py` = supervisor (writes bot.pid, restarts worker if exited or heartbeat stale
  scan>300 s / monitor>180 s); `--worker` = trading loop.
- Separate position-monitor thread every 4 s (FAST_SEC) marking positions with a Jupiter SELL quote for the whole position
  (own Http session/rate limiter), DexScreener liquidity every 4th pass.
- Never holds a store (FUSE) file open: log/ledger writes open-write-close with retries; stdout/stderr to /var/tmp.
- params.json override mechanism (P dict); MAX_RUG_SCORE, MAX_INSIDERS, BE_AFTER/BE_LEVEL hooks; ledger v2 adds `periods`, `events`.
- Added GeckoTerminal pumpswap pools to discovery; holders count in safety info.
- api_stats became nested {"scan": {...}, "monitor": {...}} -> broke status.py last line (KeyError 'ok').
- Strategy parameters themselves unchanged from v1 (params_version "v1").

## 2026-10-23 session (this one) — progress notes
- 07:14 ET: found box freshly booted at 07:07:54 (uptime 6 min, /var/tmp wiped, old pid 206954 gone; bot last wrote 07:11:00).
  Restarted bot at 07:15 ET (supervisor pid in bot.pid). Started diag/ticklog.py suspension probe -> diag/ticks.csv.

## v3 infrastructure (deployed 2026-10-23 07:21 ET)
- Freeze diagnosis: the box gets suspended between agent runs. The supervisor's own 5 s loop went silent for 17-62 min each hour, so no process got any CPU and it was not a code hang. The box was also re-created at 07:07:54 ET (fresh boot, /var/tmp wiped), which killed the old supervisor. Awake windows run about 5-6 min per hour.
- Supervisor: if its own loop gap is over 30 s, it logs a suspension to diag/suspensions.csv and resets the 90 s grace period instead of killing the worker. A real hang means scan stale >180 s or monitor stale >120 s. On a hang it dumps the worker's stack with SIGUSR1, then terminates it.
- Worker: a gap thread (wall jump >45 s) logs a `wake_gap` event and adds unmonitored seconds to each open position. Exits within 120 s of a wake are tagged "[after N min suspension gap]", and entries pause for 30 s after a wake so exits come first.
- Jupiter: one process-wide limiter (2.2 s between calls, under the 0.5 RPS keyless limit), a 3 s quote cache, and a 3 s global pause on 429 (counted). No instant fallback-host retry on a 429.
- MAX_QUOTE_DEV 0.04: skip an entry if the Jupiter price is >4% above the DexScreener price (MICHIKO filled at +33%).
- load_params now keeps `_version` and `_period`. Measurement periods go to ledger["periods"] and periods.jsonl.
- status.py: fixed KeyError 'ok' (nested api_stats). It now shows ALL-TIME, SINCE <period>, gap/unmonitored minutes, and 429 counts.

## Backtest harness (research/backtest/)
- Data: HuggingFace Slinky21/Pumpfun_Memecoin_Corpus postgard_snapshots. Graduated pump.fun tokens on PumpSwap, Jun 5 - Jul 14 2026: 1,002,229 snapshots, 5,067 mints. It includes dead tokens, so it is survivorship-free for graduates. It does not cover the bonding-curve phase.
- Snapshot cadence is about 60 s for the first 2 h and sparser after that. That makes stop fills coarse, which matches the live slippage.
- Costs per side: 1% pool fee + 0.25% allowance + impact 2*size/liq. Size $75, capped at 1% of liquidity. Fill at the next snapshot.
- Split by token launch time, 60/40: train 3,040 tokens (before 2026-07-08 16:21 UTC), test 2,027 tokens.
- Duty-cycle modes check exits only while "awake": continuous, 5min (90 s per 5 min), 15min (150 s per 15 min), hourly (330 s per hour, like the live box).

### Round 0: v1 params (stop -8%, TP1 +25% sell 50%, trail 15%, time 2h/12h/12h, S1+S2+S3)
| mode | set | n | win% | avg P&L | maxDD$ | PF |
|---|---|---|---|---|---|---|
| continuous | train | 468 | 31.6 | -3.34% | 1270 | 0.74 |
| continuous | test | 192 | 29.2 | -6.43% | 946 | 0.57 |
| hourly | train | 170 | 27.1 | -14.27% | 2174 | 0.58 |
| hourly | test | 64 | 18.8 | -22.5% | n/a | n/a |
Forward returns (train): random entries at liq >= $20k are negative at every age and horizon (age <10 min: 1h median -24%). S1 signal: +0.1% mean at 15 min, -5.9% at 1 h. S2 1h mean -3.9%. S3 1h mean -14.3%.

### Round 1: exit grid per strategy (train, continuous; 256 combos per strategy)
Grid: stop {-8,-15,-25,-40%} x TP {10,20,30,50%} x TP fraction {0.5,1} x trail {15,30%} x time {0.25,0.5,1,2 h}. Full results are in r1_exits_train.csv. Even the best combos sit near zero on train, so I expect the grid winners to be data-mined (selection bias).
| strategy / exits | mode | train n / win% / avg / DD$ | test n / win% / avg / DD$ |
|---|---|---|---|
| S1 stop -25%, TP +20% sell all, 2h | continuous | 363 / 59.0 / -0.42% / 437 | 161 / 59.0 / -1.00% / 427 |
| same | 5min | 309 / 62.5 / +2.32% / 697 | 133 / 57.9 / -3.02% / 587 |
| same | hourly | 145 / 46.2 / -15.19% / 1773 | 61 / 42.6 / -20.26% / 1113 |
| S1 stop -25%, TP +30% sell all, 2h | continuous | 363 / 52.1 / -0.07% / 580 | 161 / 50.9 / -2.72% / 434 |
| S2 stop -15%, TP +50% sell all, 2h | continuous | 54 / 40.7 / +1.67% / 126 | 15 / 33.3 / +3.30% / 89 |
| same | 5min | 23 / 21.7 / -15.27% / 264 | 10 / 20.0 / -15.86% / 176 |
| S3 stop -8%, TP +10% sell 50%, 2h | continuous | 96 / 45.8 / +0.77% / 157 | 32 / 21.9 / -14.30% / 350 |
| S3 stop -40%, TP +10% sell 50%, 2h | continuous | 93 / 72.0 / +0.99% / 240 | 32 / 43.8 / -21.34% / 540 |
Decision: S1 gets stop -25%, TP +20% sell all, 2h time stop, the best on test and a smooth neighborhood. S3 is disabled (it collapses on test). S2 is disabled (n=15 on test, and it breaks at 5-min checking).

### Round 2: S1 entry filters (with the Round 1 exits)
Grid: age_min {10 min, 30 min} x age_max {1 h, 3 h} x liq {20k, 40k} x v1 {40k, 100k} x buy ratio br5=br1 {1.15, 1.4}, ranked on train by min(continuous, 5min). Results are in r2_train.csv. A later age_min (30 min) and liq 40k were worse. A stricter buy ratio helped.
| S1 variant | mode | train n / win% / avg / DD$ | test n / win% / avg / DD$ |
|---|---|---|---|
| br 1.4, age 10-60 min | continuous | 265 / 63.4 / +2.49% / 253 | 105 / 61.9 / -0.86% / 290 |
| br 1.4, age 10-60 min | 5min | 232 / 64.7 / +1.92% / 547 | 90 / 62.2 / +0.06% / 355 |
| br 1.4, age 10-60 min | 15min | 194 / 60.3 / -4.39% / 1053 | 74 / 50.0 / -18.33% / 1035 |
| br 1.4, age 10-60 min | hourly | 114 / 45.6 / -17.93% / 1685 | 42 / 40.5 / -28.28% / 957 |
| br 1.3, age 10-60 min | continuous | 309 / 60.2 / +0.29% / 325 | 129 / 60.5 / -0.20% / 276 |
| br 1.3, age 10-60 min | 5min | 261 / 63.6 / +2.98% / 506 | 107 / 60.7 / -1.27% / 438 |
| br 1.4, age 10 min-3 h | continuous | 362 / 63.5 / +2.51% / 298 | 151 / 62.3 / -0.88% / 572 |

### Round 3: duty-cycle robustness (from the rows above)
Every config, including the best, loses 16-28% per trade when exits are only checked hourly or every 15 min. The strategy only works if stops are checked within about 1-5 min. Near-continuous monitoring matters more than any parameter choice.

### Deployed 2026-10-23 07:30 ET: params.json v3-tuned-2026-10-23 (period started, equity $742.43)
S1 only, br5/br1 >= 1.4, stop -25%, TP1 +20% with TP1_FRAC 1.0, 2h time stop. S2/S3 entries are disabled; their open positions are still managed under the new global exits.
Honest verdict: on held-out data the best config is break-even at best (-0.86% continuous, +0.06% with 5-min checks). There is no demonstrated edge. The changes mainly cut losses compared with v1 (-6.4% per trade on test).
