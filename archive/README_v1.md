# Solana memecoin PAPER-TRADING bot (simulation only)

**No real money, ever.** No wallet, keypair, private key or signing code exists here. Jupiter is used only for
`/swap/v1/quote` (read-only quotes); `Http.get` refuses any other `/swap` URL. No RPC, no transactions.

## Files
- `bot.py` – long-running loop (~20 s/cycle). `status.py` – scorecard. `ledger_solana.json` – full state (resumes on restart).
- `trades.log` – append-only fills. `bot.log` – process log. `bot.pid` – current PID.

## Run
```
cd /cursor/stores/store-4804f032-7e96-4cbf-82f3-55114504c6f7/paper-trading/solana_bot
setsid nohup python3 -u bot.py >> bot.log 2>&1 < /dev/null &      # start / restart (resumes ledger)
kill $(cat bot.pid)                                                # stop cleanly
python3 status.py                                                  # scorecard
```
Reset from scratch: stop, then move `ledger_solana.json` aside.

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
