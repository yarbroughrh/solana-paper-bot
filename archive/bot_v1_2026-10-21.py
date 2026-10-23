#!/usr/bin/env python3
"""
PAPER-TRADING Solana memecoin bot.  SIMULATION ONLY.

Hard guarantees (by construction):
  * No wallet, no keypair, no private key, no signing library is imported or used.
  * Jupiter is called ONLY at /swap/v1/quote (read-only price quotes). The code
    refuses any URL that contains '/swap' other than '/swap/v1/quote'.
  * No RPC sendTransaction, no transaction building. All "fills" are numbers in a JSON ledger.
"""
import json, os, sys, time, math, signal, traceback, random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "ledger_solana.json")
TRADES_LOG = os.path.join(HERE, "trades.log")
ET = ZoneInfo("America/New_York")

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111112"
JUP_QUOTE_URLS = ["https://lite-api.jup.ag/swap/v1/quote", "https://api.jup.ag/swap/v1/quote"]

# ---------------- RULES ----------------
START_USD = 1000.0
DESKS = {"S1_launch": 1/3, "S2_dip": 1/3, "S3_breakout": 1/3}
POLL_SEC = 20
MAX_POS_PCT = 0.10          # of total equity
MAX_OPEN = 5
MIN_POS_USD = 10.0
MAX_IMPACT_PCT = 2.0        # Jupiter priceImpactPct ceiling
FEE_PER_SIDE = 0.0025       # priority fee / slippage allowance per side
HARD_STOP = -0.08
TP1 = 0.25                  # take half
TRAIL = 0.15                # exit remainder on 15% drop from peak (after TP1)
TIME_STOP_H = {"S1_launch": 2, "S2_dip": 12, "S3_breakout": 12}
DAILY_LOSS_HALT = 0.15
BAN_AFTER_LOSSES = 2
MIN_LIQ = 12_000
MAX_TOP10 = 60.0
MAX_RECENT_PUMP = 30.0      # no buy if up >30% in last 5-15 min
COOLDOWN_AFTER_EXIT_MIN = 60

BLUECHIP_SYMBOLS = {"BONK", "WIF", "POPCAT", "TRUMP", "MELANIA", "PENGU", "FARTCOIN", "MEW", "BOME",
                    "PNUT", "GOAT", "MOODENG", "GIGA", "SPX", "MICHI", "SLERF", "JUP", "SOL", "USDC",
                    "USDT", "PUMP", "RAY", "JTO", "W", "PYTH", "CHILLGUY", "AI16Z", "BRETT", "PEPE", "DOGE"}

STOP = False
def _sig(*_):
    global STOP; STOP = True
signal.signal(signal.SIGTERM, _sig); signal.signal(signal.SIGINT, _sig)

def now_et(): return datetime.now(ET)
def ts(): return now_et().strftime("%Y-%m-%d %H:%M:%S ET")
def today_et(): return now_et().strftime("%Y-%m-%d")
def log(msg): print(f"[{ts()}] {msg}", flush=True)

# ---------------- HTTP with per-host rate limits & backoff ----------------
class Http:
    MIN_GAP = {"api.dexscreener.com": 1.1, "api.geckoterminal.com": 2.5, "api.rugcheck.xyz": 2.0,
               "lite-api.jup.ag": 1.2, "api.jup.ag": 1.5}
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "paper-trading-research-bot/1.0 (simulation only)"
        self.last = {}; self.backoff_until = {}; self.fails = {}
        self.stats = {}
    def get(self, url, params=None, timeout=15):
        if "/swap" in url and "/swap/v1/quote" not in url:
            raise RuntimeError("REFUSED: only Jupiter /swap/v1/quote is allowed")
        host = url.split("/")[2]
        st = self.stats.setdefault(host, {"ok": 0, "err": 0, "last_err": None})
        if time.time() < self.backoff_until.get(host, 0):
            return None
        gap = self.MIN_GAP.get(host, 1.0)
        wait = self.last.get(host, 0) + gap - time.time()
        if wait > 0: time.sleep(wait)
        self.last[host] = time.time()
        try:
            r = self.s.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                self.fails[host] = 0; st["ok"] += 1
                return r.json()
            st["err"] += 1; st["last_err"] = f"HTTP {r.status_code} {ts()}"
            if r.status_code in (429, 500, 502, 503, 504, 403):
                self._backoff(host)
            return {"__error__": r.status_code, "__body__": r.text[:200]}
        except Exception as e:
            st["err"] += 1; st["last_err"] = f"{type(e).__name__} {ts()}"
            self._backoff(host); return None
    def _backoff(self, host):
        n = self.fails.get(host, 0) + 1; self.fails[host] = n
        d = min(300, 5 * 2 ** (n - 1)) + random.random() * 2
        self.backoff_until[host] = time.time() + d
        log(f"backoff {host} {d:.0f}s (fail #{n})")

http = Http()

# ---------------- Ledger ----------------
def new_ledger():
    return {"version": 1, "mode": "PAPER / SIMULATION ONLY", "created_at": ts(), "start_usd": START_USD,
            "desks": {k: {"cash": START_USD * w, "alloc": START_USD * w} for k, w in DESKS.items()},
            "open_positions": {}, "closed_trades": [], "bans": {}, "consec_losses": {}, "cooldowns": {},
            "day": {"date": today_et(), "start_equity": START_USD, "halted": False},
            "equity_history": [], "heartbeat": None, "cycles": 0, "api_stats": {}, "last_candidates": []}

def load_ledger():
    if os.path.exists(LEDGER):
        with open(LEDGER) as f: L = json.load(f)
        log(f"resumed ledger: {len(L['open_positions'])} open, {len(L['closed_trades'])} closed")
        return L
    log("new ledger"); return new_ledger()

def save_ledger(L):
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as f: json.dump(L, f, indent=1)
    os.replace(tmp, LEDGER)

def tlog(line):
    with open(TRADES_LOG, "a") as f: f.write(f"[{ts()}] {line}\n")
    log("TRADE " + line)

def equity(L):
    cash = sum(d["cash"] for d in L["desks"].values())
    pos = sum(p["tokens"] * p.get("mark_price", p["entry_price"]) for p in L["open_positions"].values())
    return cash + pos

# ---------------- Data sources ----------------
def dex_pairs_for_tokens(addrs):
    """DexScreener /tokens/v1/solana/{up to 30 comma-separated} -> {token: best pair}"""
    out = {}
    addrs = list(dict.fromkeys(addrs))
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i+30]
        d = http.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk))
        if not isinstance(d, list): continue
        for p in d:
            if p.get("chainId") != "solana": continue
            t = p["baseToken"]["address"]
            if t not in chunk: continue
            if p["quoteToken"]["address"] not in (SOL, USDC): continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if t not in out or liq > ((out[t].get("liquidity") or {}).get("usd") or 0):
                out[t] = p
    return out

_gecko_cache = {"t": 0, "tokens": []}
def discover(L):
    toks = []
    for u in ["https://api.dexscreener.com/token-boosts/latest/v1", "https://api.dexscreener.com/token-boosts/top/v1",
              "https://api.dexscreener.com/token-profiles/latest/v1"]:
        d = http.get(u)
        if isinstance(d, list):
            toks += [x["tokenAddress"] for x in d if x.get("chainId") == "solana" and x.get("tokenAddress")]
    if time.time() - _gecko_cache["t"] > 120:
        gt = []
        for u in ["https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page=1",
                  "https://api.geckoterminal.com/api/v2/networks/solana/pools?page=1&sort=h24_volume_usd_desc",
                  "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=1",
                  "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=2"]:
            d = http.get(u)
            if isinstance(d, dict) and "data" in d:
                for p in d["data"]:
                    try:
                        base = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
                        dex = p["relationships"]["dex"]["data"]["id"]
                        if dex == "pump-fun": continue  # still on bonding curve, not graduated
                        if float(p["attributes"].get("reserve_in_usd") or 0) < MIN_LIQ: continue
                        gt.append(base)
                    except Exception: pass
        _gecko_cache.update(t=time.time(), tokens=gt)
    toks += _gecko_cache["tokens"]
    toks += list(L["open_positions"].keys())
    skip = {SOL, USDC, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
    return [t for t in dict.fromkeys(toks) if t not in skip]

_rug_cache = {}
def rugcheck(mint):
    c = _rug_cache.get(mint)
    if c and time.time() - c[0] < 900: return c[1]
    d = http.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", timeout=25)
    if isinstance(d, dict) and "__error__" not in d:
        _rug_cache[mint] = (time.time(), d); return d
    return None

def jup_quote(inp, out, amount_raw, slippage_bps=300):
    params = {"inputMint": inp, "outputMint": out, "amount": str(int(amount_raw)), "slippageBps": slippage_bps,
              "swapMode": "ExactIn"}
    for u in JUP_QUOTE_URLS:
        d = http.get(u, params=params)
        if isinstance(d, dict) and d.get("outAmount"):
            return d
    return None

# ---------------- Price memory (own samples for 15m change) ----------------
PRICE_MEM = {}
def remember(tok, price):
    h = PRICE_MEM.setdefault(tok, []); h.append((time.time(), price))
    cut = time.time() - 3600
    while h and h[0][0] < cut: h.pop(0)

def change_since(tok, minutes):
    h = PRICE_MEM.get(tok) or []
    if len(h) < 2: return None
    tgt = time.time() - minutes * 60
    old = [p for t, p in h if t <= tgt + 30]
    if not old: return None
    # max run-up over the window: current vs min within window
    window = [p for t, p in h if t >= tgt]
    lo = min(window) if window else old[-1]
    return (h[-1][1] / lo - 1) * 100

# ---------------- Safety ----------------
def safety(mint, pair, strategy):
    """Returns (ok, reason, info)."""
    r = rugcheck(mint)
    if not r: return False, "rugcheck unavailable", {}
    tok = r.get("token") or {}
    if tok.get("mintAuthority"): return False, "mint authority not revoked", {}
    if tok.get("freezeAuthority"): return False, "freeze authority not revoked", {}
    if r.get("rugged"): return False, "rugcheck: rugged", {}
    risks = r.get("risks") or []
    danger = [x.get("name") for x in risks if (x.get("level") or "").lower() == "danger"]
    if danger: return False, f"rugcheck danger: {danger[:3]}", {}
    score = r.get("score_normalised") or 0
    if score >= 50: return False, f"rugcheck score_normalised {score} (high risk)", {}
    if (r.get("transferFee") or {}).get("pct"): return False, "transfer fee token", {}
    # top-10 excluding pool / AMM / LP accounts
    pool_accts = set()
    for k, v in (r.get("knownAccounts") or {}).items():
        if (v or {}).get("type") in ("AMM", "LOCKER"): pool_accts.add(k)
    for m in r.get("markets") or []:
        for f in ("pubkey", "liquidityA", "liquidityB"): pool_accts.add(m.get(f))
        for f in ("liquidityAAccount", "liquidityBAccount"): pool_accts.add((m.get(f) or {}).get("owner"))
    pool_accts.add(pair.get("pairAddress"))
    holders = [h for h in (r.get("topHolders") or []) if h.get("owner") not in pool_accts and h.get("address") not in pool_accts]
    top10 = sum(h.get("pct", 0) for h in holders[:10])
    if not r.get("topHolders"): return False, "no holder data", {}
    if top10 >= MAX_TOP10: return False, f"top10 {top10:.1f}% >= {MAX_TOP10}%", {}
    lp_locked = 0.0
    try:
        mk = max(r.get("markets") or [], key=lambda m: (m.get("lp") or {}).get("lpLockedUSD") or 0)
        lp_locked = (mk.get("lp") or {}).get("lpLockedPct") or 0
    except ValueError: pass
    info = {"rug_score_norm": score, "top10_ex_pool": round(top10, 1), "lp_locked_pct": round(lp_locked, 1),
            "insiders": r.get("graphInsidersDetected") or 0}
    if strategy == "S1_launch":
        if score > 20: return False, f"S1 needs rug score <=20 (got {score})", info
        if lp_locked < 90: return False, f"S1 needs LP burned/locked >=90% (got {lp_locked:.0f}%)", info
        if top10 >= 35: return False, f"S1 needs top10 <35% (got {top10:.1f}%)", info
        if holders and holders[0].get("pct", 0) >= 10: return False, "S1 single holder >=10%", info
        if (r.get("graphInsidersDetected") or 0) > 0: return False, "S1 insider network detected", info
        if (r.get("totalHolders") or 0) < 300: return False, f"S1 needs >=300 holders", info
    return True, "ok", info

# ---------------- Signals ----------------
def metrics(p):
    now = time.time() * 1000
    age_h = (now - (p.get("pairCreatedAt") or now)) / 3.6e6
    pc = p.get("priceChange") or {}; vol = p.get("volume") or {}; tx = p.get("txns") or {}
    def br(k):
        b = (tx.get(k) or {}).get("buys", 0); s = (tx.get(k) or {}).get("sells", 0)
        return b / max(1, s), b + s
    return {"age_h": age_h, "liq": (p.get("liquidity") or {}).get("usd") or 0, "mcap": p.get("marketCap") or p.get("fdv") or 0,
            "m5": pc.get("m5") or 0, "h1": pc.get("h1") or 0, "h6": pc.get("h6") or 0, "h24": pc.get("h24") or 0,
            "v5": vol.get("m5") or 0, "v1": vol.get("h1") or 0, "v6": vol.get("h6") or 0, "v24": vol.get("h24") or 0,
            "br5": br("m5")[0], "br1": br("h1")[0], "n1": br("h1")[1], "dex": p.get("dexId"),
            "price": float(p.get("priceUsd") or 0)}

def signal_for(tok, p):
    m = metrics(p)
    if m["price"] <= 0 or m["liq"] < MIN_LIQ: return None, m
    if m["dex"] == "pumpfun": return None, m   # bonding curve, not graduated
    run15 = change_since(tok, 15)
    if m["m5"] > MAX_RECENT_PUMP or (run15 is not None and run15 > MAX_RECENT_PUMP): return None, m
    # S1: freshly graduated / new pool momentum (10 min - 1 h old)
    if 10/60 <= m["age_h"] < 1 and m["liq"] >= 20_000 and m["v1"] >= 40_000 and m["n1"] >= 300 \
       and m["br5"] >= 1.2 and m["br1"] >= 1.15 and 2 <= m["m5"] <= 20 and m["h1"] > 0 and m["mcap"] <= 5_000_000:
        return "S1_launch", m
    if m["age_h"] < 1: return None, m
    # S2: dip-buy bounce on established, trending token
    if m["liq"] >= 40_000 and m["v24"] >= 150_000 and m["h24"] > 0 and m["h6"] > -35 \
       and -30 <= m["h1"] <= -10 and 0.5 <= m["m5"] <= 8 and m["br5"] >= 1.0:
        return "S2_dip", m
    # S3: volume / momentum breakout on trending token
    avg_h_vol_6 = m["v6"] / 6 if m["v6"] else 0
    if m["liq"] >= 30_000 and m["v1"] >= 25_000 and avg_h_vol_6 > 0 and m["v1"] >= 1.8 * avg_h_vol_6 \
       and 5 <= m["h1"] <= 40 and 0 <= m["m5"] <= 8 and m["br1"] >= 1.15 and m["br5"] >= 1.0:
        return "S3_breakout", m
    return None, m

# ---------------- Paper fills via Jupiter quotes ----------------
def decimals_for(mint):
    c = _rug_cache.get(mint)
    if c: return ((c[1].get("token") or {}).get("decimals"))
    return None

def sim_buy(mint, usd):
    """Price a USDC->token buy with a real Jupiter quote. Returns (tokens_after_fee, eff_price, impact%) or None."""
    dec = decimals_for(mint)
    if dec is None: return None
    q = jup_quote(USDC, mint, usd * 1e6)
    if not q: return None
    impact = float(q.get("priceImpactPct") or 0) * 100
    tokens = int(q["outAmount"]) / 10 ** dec * (1 - FEE_PER_SIDE)
    if tokens <= 0: return None
    return tokens, usd / tokens, impact

def sim_sell(mint, tokens):
    dec = decimals_for(mint)
    if dec is None:
        rugcheck(mint); dec = decimals_for(mint)
        if dec is None: return None
    q = jup_quote(mint, USDC, tokens * 10 ** dec)
    if not q: return None
    impact = float(q.get("priceImpactPct") or 0) * 100
    usd = int(q["outAmount"]) / 1e6 * (1 - FEE_PER_SIDE)
    return usd, usd / tokens, impact

# ---------------- Copycat filter ----------------
def copycat_ok(tok, pair, pairs):
    sym = (pair["baseToken"].get("symbol") or "").upper().lstrip("$")
    if sym in BLUECHIP_SYMBOLS: return False, f"copycat/bluechip ticker {sym}"
    # if several tokens in the scan share this ticker, only the oldest+deepest one is allowed
    rivals = [(t, p) for t, p in pairs.items() if (p["baseToken"].get("symbol") or "").upper().lstrip("$") == sym]
    if len(rivals) > 1:
        best = max(rivals, key=lambda tp: ((tp[1].get("liquidity") or {}).get("usd") or 0, -(tp[1].get("pairCreatedAt") or 0)))
        if best[0] != tok: return False, f"copycat of {sym} ({best[0][:6]}..)"
    return True, ""

# ---------------- Core loop pieces ----------------
def roll_day(L):
    d = today_et()
    if L["day"]["date"] != d:
        L["day"] = {"date": d, "start_equity": round(equity(L), 2), "halted": False}
        log(f"new ET day {d}, start equity ${L['day']['start_equity']:.2f}")

def close_part(L, tok, frac, reason, fill):
    p = L["open_positions"][tok]
    tokens = p["tokens"] * frac
    usd, px, impact = fill
    cost = p["cost_usd"] * frac
    pnl = usd - cost
    L["desks"][p["strategy"]]["cash"] += usd
    trade = {"token": tok, "symbol": p["symbol"], "strategy": p["strategy"], "entry_time": p["entry_time"],
             "exit_time": ts(), "entry_price": p["entry_price"], "exit_price": px, "tokens": tokens,
             "cost_usd": round(cost, 4), "proceeds_usd": round(usd, 4), "pnl_usd": round(pnl, 4),
             "pnl_pct": round(pnl / cost * 100, 2) if cost else 0, "reason": reason, "exit_impact_pct": round(impact, 3),
             "partial": frac < 0.999, "entry_safety": p.get("safety")}
    L["closed_trades"].append(trade)
    tlog(f"SELL {p['strategy']} {p['symbol']} {frac*100:.0f}% @ ${px:.10g} proceeds ${usd:.2f} pnl ${pnl:+.2f} ({trade['pnl_pct']:+.1f}%) reason={reason} impact={impact:.2f}%")
    p["tokens"] -= tokens; p["cost_usd"] -= cost; p["realized_usd"] = p.get("realized_usd", 0) + pnl
    if frac >= 0.999 or p["tokens"] <= 1e-12:
        total = p.get("realized_usd", 0)
        if total < 0:
            L["consec_losses"][tok] = L["consec_losses"].get(tok, 0) + 1
            if L["consec_losses"][tok] >= BAN_AFTER_LOSSES:
                L["bans"][tok] = {"symbol": p["symbol"], "since": ts()}; tlog(f"BAN {p['symbol']} {tok} after {BAN_AFTER_LOSSES} losses in a row")
        else:
            L["consec_losses"][tok] = 0
        L["cooldowns"][tok] = time.time() + COOLDOWN_AFTER_EXIT_MIN * 60
        del L["open_positions"][tok]

def manage_positions(L, pairs):
    for tok in list(L["open_positions"].keys()):
        p = L["open_positions"][tok]
        pr = pairs.get(tok)
        if pr:
            px = float(pr.get("priceUsd") or 0)
            if px > 0:
                p["mark_price"] = px; p["peak_price"] = max(p.get("peak_price", px), px); p["mark_time"] = ts()
                p["liq_now"] = (pr.get("liquidity") or {}).get("usd") or 0
        px = p.get("mark_price", p["entry_price"])
        ret = px / p["entry_price"] - 1
        age_h = (time.time() - p["entry_epoch"]) / 3600
        reason, frac = None, 1.0
        if ret <= HARD_STOP: reason = f"hard_stop {ret*100:.1f}%"
        elif p.get("liq_now") and p["liq_now"] < 0.5 * p["entry_liq"]: reason = "liquidity_collapse"
        elif not p.get("tp1_done") and ret >= TP1: reason, frac = f"take_half +{ret*100:.1f}%", 0.5
        elif p.get("tp1_done") and px <= p["peak_price"] * (1 - TRAIL): reason = f"trail -{TRAIL*100:.0f}% from peak"
        elif age_h >= TIME_STOP_H[p["strategy"]]: reason = f"time_stop {TIME_STOP_H[p['strategy']]}h"
        if not reason: continue
        fill = sim_sell(tok, p["tokens"] * frac)
        if not fill:
            p["sell_quote_fails"] = p.get("sell_quote_fails", 0) + 1
            if p["sell_quote_fails"] < 3:
                log(f"sell quote failed for {p['symbol']}, retry next cycle"); continue
            # pessimistic fallback: mark price minus 5% haircut and fees
            usd = p["tokens"] * frac * px * 0.95 * (1 - FEE_PER_SIDE)
            fill = (usd, usd / (p["tokens"] * frac), 5.0); reason += " (no quote; 5% haircut fallback)"
        if frac < 1: p["tp1_done"] = True
        close_part(L, tok, frac, reason, fill)

def try_entries(L, pairs):
    roll_day(L)
    eq = equity(L)
    if not L["day"]["halted"] and eq <= L["day"]["start_equity"] * (1 - DAILY_LOSS_HALT):
        L["day"]["halted"] = True; tlog(f"HALT new entries: equity ${eq:.2f} <= -15% of day start ${L['day']['start_equity']:.2f}")
    if L["day"]["halted"]: return
    cands = []
    for tok, pr in pairs.items():
        if tok in L["open_positions"] or tok in L["bans"] or L["cooldowns"].get(tok, 0) > time.time(): continue
        strat, m = signal_for(tok, pr)
        if strat: cands.append((tok, pr, strat, m))
    cands.sort(key=lambda c: c[3]["liq"], reverse=True)
    L["last_candidates"] = []
    for tok, pr, strat, m in cands[:6]:
        if len(L["open_positions"]) >= MAX_OPEN: break
        sym = pr["baseToken"].get("symbol")
        rec = {"token": tok, "symbol": sym, "strategy": strat, "liq": round(m["liq"]), "m5": m["m5"], "h1": m["h1"], "age_h": round(m["age_h"], 2), "t": ts()}
        ok, why = copycat_ok(tok, pr, pairs)
        if ok: ok, why, info = safety(tok, pr, strat)
        else: info = {}
        rec["decision"] = "pass" if ok else f"reject: {why}"
        L["last_candidates"].append(rec)
        if not ok:
            log(f"candidate {strat} {sym} rejected: {why}"); continue
        desk = L["desks"][strat]
        size = min(MAX_POS_PCT * eq, desk["cash"], 0.01 * m["liq"])   # 1% of pool liq keeps impact small
        if size < MIN_POS_USD:
            rec["decision"] = f"skip: size ${size:.2f} too small (desk cash ${desk['cash']:.2f})"; continue
        fill = None
        for _ in range(3):
            fill = sim_buy(tok, size)
            if not fill: break
            if fill[2] <= MAX_IMPACT_PCT: break
            size /= 2; fill = None
            if size < MIN_POS_USD: break
        if not fill:
            rec["decision"] = "skip: no quote / impact too high"; continue
        tokens, eff_px, impact = fill
        desk["cash"] -= size
        L["open_positions"][tok] = {"token": tok, "symbol": sym, "strategy": strat, "entry_time": ts(), "entry_epoch": time.time(),
            "entry_price": eff_px, "dex_price_at_entry": m["price"], "tokens": tokens, "cost_usd": size, "entry_impact_pct": round(impact, 3),
            "entry_liq": m["liq"], "mark_price": m["price"], "peak_price": max(m["price"], eff_px), "tp1_done": False,
            "pair": pr.get("pairAddress"), "dex": m["dex"], "signal": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()},
            "safety": info}
        rec["decision"] = "BOUGHT"
        tlog(f"BUY {strat} {sym} {tok} ${size:.2f} @ ${eff_px:.10g} (dex ${m['price']:.10g}) impact={impact:.2f}% liq=${m['liq']:,.0f} safety={info}")

def cycle(L):
    toks = discover(L)
    pairs = dex_pairs_for_tokens(toks)
    for t, p in pairs.items():
        try: remember(t, float(p.get("priceUsd") or 0))
        except Exception: pass
    manage_positions(L, pairs)
    try_entries(L, pairs)
    eq = equity(L)
    L["cycles"] += 1
    L["heartbeat"] = {"time": ts(), "pid": os.getpid(), "tokens_scanned": len(toks), "pairs_priced": len(pairs), "equity": round(eq, 2)}
    L["api_stats"] = http.stats
    if L["cycles"] % 15 == 1:
        L["equity_history"].append({"t": ts(), "equity": round(eq, 2)}); L["equity_history"] = L["equity_history"][-2000:]
    save_ledger(L)
    log(f"cycle {L['cycles']}: scanned {len(toks)} tokens, priced {len(pairs)}, open {len(L['open_positions'])}, equity ${eq:.2f}")

def main():
    log(f"PAPER bot starting pid={os.getpid()} (SIMULATION ONLY - no wallet, no transactions)")
    with open(os.path.join(HERE, "bot.pid"), "w") as f: f.write(str(os.getpid()))
    L = load_ledger(); save_ledger(L)
    while not STOP:
        t0 = time.time()
        try: cycle(L)
        except Exception:
            log("cycle error:\n" + traceback.format_exc())
        sl = max(5, POLL_SEC - (time.time() - t0))
        for _ in range(int(sl)):
            if STOP: break
            time.sleep(1)
    save_ledger(L); log("stopped cleanly")

if __name__ == "__main__":
    main()
