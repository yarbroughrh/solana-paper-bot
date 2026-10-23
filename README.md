# Solana memecoin PAPER-trading bot (simulation only)

> **PAPER TRADING ONLY — no real money, ever.** This repo has no wallet, keypair, private key, signing code, RPC
> or transaction code, and uses no secrets. Jupiter is called only at `/swap/v1/quote` (read-only quotes) and
> `Http.get` refuses any other `/swap` URL. All "fills" are numbers in `ledger_solana.json`. Not financial advice.

**Live scorecard: [STATUS.md](STATUS.md)** (refreshed about every 5 min while the bot runs, and at shutdown; times are ET).

## How it runs (GitHub Actions)
- `.github/workflows/paper-bot.yml` runs `python3 -u bot.py` (supervisor + worker) on `ubuntu-latest` for 5h45m
  (`timeout -s TERM 345m`). On SIGTERM the worker saves the ledger atomically, writes STATUS.md and exits.
- A background loop commits the state files (`ledger_solana.json`, `trades.log`, `periods.jsonl`, `STATUS.md`, `diag/`)
  every ~15 min as `github-actions[bot]` with message `state: <ET time>`; one final commit happens at shutdown.
- The run then starts the next run itself (`gh workflow run paper-bot.yml`). A `*/30` cron is a backup; a cron run exits
  right away if another run is in progress and the last state commit is under 20 min old. The `paper-bot`
  concurrency group keeps only one run active at a time.
- Each new run resumes from the committed ledger. Gaps between runs (a few minutes) are recorded as downtime.
- Bot logs appear in the Actions run log (`tail -F bot.log`); `bot.log` itself is not committed.

**Stop it:** Actions tab → *paper-bot* → "…" → **Disable workflow** (or `gh workflow disable paper-bot.yml`), then cancel
any active run (it gets a final state commit only on a clean SIGTERM; cancelling may lose up to 15 min of state).
Re-enable with `gh workflow enable paper-bot.yml && gh workflow run paper-bot.yml`.

**Run locally:** `pip install -r requirements.txt && python3 -u bot.py` (Ctrl-C stops cleanly). `python3 status.py` prints the scorecard,
`python3 status.py --md` prints STATUS.md.

## Files
- `bot.py` – supervisor + worker loop (~20 s/cycle). `status.py` – scorecard / STATUS.md. `params.json` – live strategy overrides.
- `ledger_solana.json` – full state (resumes on restart). `trades.log` – append-only fills. `periods.jsonl` – measurement periods.
- `diag/` – suspension diagnostics from the earlier box-hosted runs. `research/` – backtest harness (data not included). `archive/` – older versions.

## Data
- Discovery: DexScreener token-boosts latest/top + token-profiles latest (Solana); GeckoTerminal Solana trending, top-volume and new pools (every 2 min; pump.fun bonding-curve pools skipped — only graduated pools).
- Pricing/metrics: DexScreener `/tokens/v1/solana/{≤30 mints}` (best SOL/USDC pair by liquidity).
- Safety: RugCheck `/v1/tokens/{mint}/report` (cached 15 min).
- Fills: Jupiter quote USDC→token (buy) and token→USDC (sell), real route + price impact, plus 0.25% per side.
- Per-host rate limits and exponential backoff on 403/429/5xx/timeouts.

## Account
$1,000 paper USD in three desks of $333.33 (S1, S2, S3). Each desk spends only its own cash.
Position size = min(10% of total equity, desk cash, 1% of pool liquidity); halved (up to 3×) until Jupiter impact ≤ 2%; min $10.
Max 5 open positions, one per token.

## Safety filters (all strategies, before any buy)
- Mint and freeze authority revoked; no transfer fee; RugCheck not "rugged", no `danger`-level risk, score_normalised < 50.
- Top-10 holders < 60% excluding pool/AMM/locker accounts. Liquidity ≥ $12K (strategy minimums higher).
- Not up > 30% in the last 5 min (DexScreener m5) or 15 min (bot's own price samples).
- Copycats: blue-chip tickers (BONK, WIF, TRUMP …) skipped; if several scanned mints share a ticker, only the deepest-liquidity one is eligible.
- Banned after 2 consecutive losing trades on the token; 60-min cooldown after any exit.
- X/social posts are never a trigger.

## Strategies
- **S1_launch** – freshly graduated / new pool momentum (pool 10–60 min old, not pump.fun curve): liq ≥ $20K, 1h vol ≥ $40K, ≥300 txns/h,
  buy/sell ≥1.2 (5m) and ≥1.15 (1h), m5 +2…+20%, h1 > 0, mcap ≤ $5M. Extra: RugCheck score ≤ 20, LP burned/locked ≥ 90%, top-10 < 35%,
  largest holder < 10%, no insider network, ≥300 holders.
- **S2_dip** – bounce on established trending coin (pool > 1h): liq ≥ $40K, 24h vol ≥ $150K, h24 > 0, h6 > −35%, h1 −30…−10% (dip), m5 +0.5…+8% (bounce starting), 5m buys ≥ sells.
- **S3_breakout** – volume/momentum breakout (pool > 1h): liq ≥ $30K, 1h vol ≥ $25K and ≥ 1.8× the 6h hourly average, h1 +5…+40%, m5 0…+8%, buy/sell ≥1.15 (1h), ≥1.0 (5m).

## Exits (marked on DexScreener price, filled on Jupiter sell quote)
- Hard stop −8% from effective entry. Take half at +25%; remainder trails, exit on 15% drop from peak.
- Time stop: 2h (S1), 12h (S2/S3). Liquidity collapse (< 50% of entry liquidity) → exit.
- If the sell quote fails 3 cycles running, fill at mark −5% haircut (pessimistic).
- Daily loss: equity ≤ 85% of start-of-day (ET) equity → no new entries until next ET day (exits still run).

## Caveats
Paper fills assume the quote is executable instantly. Real trading would face latency, MEV/sandwiching, failed txs,
quote drift and honeypot behaviours that a quote cannot reveal. Results here are optimistic for that reason.

## v3 (2026-10-23): see CHANGELOG.md
- The live strategy is set by params.json and overrides the defaults in bot.py: S1 only, buy ratio >= 1.4, stop -25%, TP +20% sell all, 2h time stop.
- The supervisor is suspension-aware (diag/suspensions.csv). Real hangs are caught by stale checks (scan 180 s, monitor 120 s) plus a stack dump.
- Jupiter quotes are rate-limited to 1 per 2.2 s per process, with a 3 s cache.
- Measurement periods are recorded in ledger "periods" and periods.jsonl. status.py prints ALL-TIME and SINCE <period> lines.
- Backtest harness: research/backtest/ (data comes from HF Slinky21/Pumpfun_Memecoin_Corpus, re-download into research/backtest/data/hf, or set BT_DIR).
