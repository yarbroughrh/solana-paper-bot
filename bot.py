#!/usr/bin/env python3
"""
PAPER-TRADING Solana memecoin bot.  SIMULATION ONLY.   (v3: suspension-aware supervisor, wake catch-up, shared Jupiter throttle)

Hard guarantees (by construction):
  * No wallet, no keypair, no private key, no signing library is imported or used.
  * Jupiter is called ONLY at /swap/v1/quote (read-only price quotes). The code
    refuses any URL that contains '/swap' other than '/swap/v1/quote'.
  * No RPC sendTransaction, no transaction building. All "fills" are numbers in a JSON ledger.

Process model (v2):
  `python3 -u bot.py`            -> SUPERVISOR. Writes bot.pid (its own pid), runs the worker as a child,
                                    restarts it if it exits or its heartbeat goes stale (>180 s).
  `python3 -u bot.py --worker`   -> WORKER (the trading loop). Started by the supervisor.
  `kill $(cat bot.pid)`          -> supervisor forwards SIGTERM to the worker, both stop cleanly.

Reliability notes: the bot folder lives on a FUSE mount (/cursor/stores) whose daemon is relaunched from time to
time. A file descriptor held open across a relaunch goes dead, so the v1 bot (stdout redirected into bot.log on the
mount) crashed on its next print() with nowhere to write the traceback. v2 never holds a store file open: every
log line / ledger save opens, writes and closes; stdout/stderr are redirected to local disk (/var/tmp).
"""
import json, os, sys, time, math, signal, traceback, random, threading, subprocess, faulthandler, atexit
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "ledger_solana.json")
TRADES_LOG = os.path.join(HERE, "trades.log")
BOT_LOG = os.path.join(HERE, "bot.log")
PID_FILE = os.path.join(HERE, "bot.pid")
PARAMS_FILE = os.path.join(HERE, "params.json")
LOCAL_DIR = os.environ.get("BOT_LOCAL_DIR", os.path.join(HERE, ".run"))   # scratch dir (gitignored) for stderr, faults, heartbeat
os.makedirs(LOCAL_DIR, exist_ok=True)
LOCAL_LOG = os.path.join(LOCAL_DIR, "bot_local.log")
WORKER_ERR = os.path.join(LOCAL_DIR, "worker_stderr.log")
HEARTBEAT = os.path.join(LOCAL_DIR, "heartbeat")
ET = ZoneInfo("America/New_York")
STATUS_EVERY_SEC = int(os.environ.get("STATUS_EVERY_SEC", "300"))

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111112"
JUP_QUOTE_URLS = ["https://lite-api.jup.ag/swap/v1/quote", "https://api.jup.ag/swap/v1/quote"]

# ---------------- RULES (defaults; params.json overrides) ----------------
P = {
    "START_USD": 1000.0,
    "DESKS": {"S1_launch": 1/3, "S2_dip": 1/3, "S3_breakout": 1/3},
    "ENABLED": {"S1_launch": True, "S2_dip": True, "S3_breakout": True},
    "POLL_SEC": 20, "FAST_SEC": 4, "MAX_POS_PCT": 0.10, "MAX_OPEN": 5, "MIN_POS_USD": 10.0, "MAX_IMPACT_PCT": 2.0,
    "LIQ_POS_FRAC": 0.01, "FEE_PER_SIDE": 0.0025, "HARD_STOP": -0.08, "TP1": 0.25, "TP1_FRAC": 0.5, "TRAIL": 0.15,
    "TIME_STOP_H": {"S1_launch": 2, "S2_dip": 12, "S3_breakout": 12},
    "DAILY_LOSS_HALT": 0.15, "BAN_AFTER_LOSSES": 2, "MIN_LIQ": 12_000, "MAX_TOP10": 60.0, "MAX_RECENT_PUMP": 30.0,
    "COOLDOWN_AFTER_EXIT_MIN": 60, "MAX_RUG_SCORE": 50,
    "S1": {"age_min_h": 10/60, "age_max_h": 1.0, "liq": 20_000, "v1": 40_000, "n1": 300, "br5": 1.2, "br1": 1.15,
           "m5_lo": 2, "m5_hi": 20, "h1_lo": 0, "mcap_max": 5_000_000, "rug_max": 20, "lp_min": 90, "top10_max": 35,
           "holder_max": 10, "holders_min": 300},
    "S2": {"liq": 40_000, "v24": 150_000, "h24_lo": 0, "h6_lo": -35, "h1_lo": -30, "h1_hi": -10, "m5_lo": 0.5, "m5_hi": 8, "br5": 1.0},
    "S3": {"liq": 30_000, "v1": 25_000, "vol_mult": 1.8, "h1_lo": 5, "h1_hi": 40, "m5_lo": 0, "m5_hi": 8, "br1": 1.15, "br5": 1.0},
}
def load_params():
    if os.path.exists(PARAMS_FILE):
        try:
            with open(PARAMS_FILE) as f: over = json.load(f)
            for k, v in over.items():
                if k.startswith("_") and k not in ("_version", "_period"): continue
                if isinstance(v, dict) and isinstance(P.get(k), dict): P[k].update(v)
                else: P[k] = v
        except Exception as e:
            log(f"params.json unreadable ({e}); using defaults")

BLUECHIP_SYMBOLS = {"BONK", "WIF", "POPCAT", "TRUMP", "MELANIA", "PENGU", "FARTCOIN", "MEW", "BOME",
                    "PNUT", "GOAT", "MOODENG", "GIGA", "SPX", "MICHI", "SLERF", "JUP", "SOL", "USDC",
                    "USDT", "PUMP", "RAY", "JTO", "W", "PYTH", "CHILLGUY", "AI16Z", "BRETT", "PEPE", "DOGE"}

STOP = False
def now_et(): return datetime.now(ET)
def ts(): return now_et().strftime("%Y-%m-%d %H:%M:%S ET")
def today_et(): return now_et().strftime("%Y-%m-%d")
ROLE = "worker" if "--worker" in sys.argv else "supervisor"

def _append(path, text):
    for i in range(3):
        try:
            with open(path, "a") as f: f.write(text)
            return True
        except Exception:
            time.sleep(0.5 * (i + 1))
    return False

def log(msg):
    """Never raises. Writes to bot.log (store, open/close per line) and a local mirror."""
    line = f"[{ts()}] [{ROLE} {os.getpid()}] {msg}\n"
    _append(BOT_LOG, line); _append(LOCAL_LOG, line)

# ---------------- HTTP with per-host rate limits & backoff ----------------
class Http:
    MIN_GAP = {"api.dexscreener.com": 1.1, "api.geckoterminal.com": 2.5, "api.rugcheck.xyz": 2.0,
               "lite-api.jup.ag": 1.0, "api.jup.ag": 1.5}
    # v3: Jupiter keyless quota is ~0.5 req/s per IP (developers.jup.ag/docs/portal/rate-limits), shared by
    # both hosts and both threads -> one process-wide limiter for every jup host.
    JUP_GAP = 2.2
    _jup_lock = threading.Lock(); _jup_last = [0.0]
    def __init__(self, name):
        self.name = name
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "paper-trading-research-bot/2.0 (simulation only)"
        self.last = {}; self.backoff_until = {}; self.fails = {}; self.stats = {}
        self.lock = threading.Lock()
    def get(self, url, params=None, timeout=12):
        if "/swap" in url and "/swap/v1/quote" not in url:
            raise RuntimeError("REFUSED: only Jupiter /swap/v1/quote is allowed")
        host = url.split("/")[2]
        st = self.stats.setdefault(host, {"ok": 0, "err": 0, "last_err": None})
        if time.time() < self.backoff_until.get(host, 0): return None
        if "jup.ag" in host:
            with Http._jup_lock:
                wait = Http._jup_last[0] + self.JUP_GAP - time.time()
                if wait > 0: time.sleep(min(wait, 10))
                Http._jup_last[0] = time.time()
        else:
          with self.lock:
            wait = self.last.get(host, 0) + self.MIN_GAP.get(host, 1.0) - time.time()
            if wait > 0: time.sleep(min(wait, 10))
            self.last[host] = time.time()
        try:
            r = self.s.get(url, params=params, timeout=(5, timeout))   # connect, read timeouts
            if r.status_code == 200:
                self.fails[host] = 0; st["ok"] += 1
                return r.json()
            st["err"] += 1; st["last_err"] = f"HTTP {r.status_code} {ts()}"
            if r.status_code == 429 and "jup.ag" in host:
                with Http._jup_lock: Http._jup_last[0] = time.time() + 3   # global pause; no per-host backoff spam
                st["r429"] = st.get("r429", 0) + 1
            elif r.status_code in (429, 500, 502, 503, 504, 403): self._backoff(host)
            return {"__error__": r.status_code, "__body__": r.text[:200]}
        except Exception as e:
            st["err"] += 1; st["last_err"] = f"{type(e).__name__} {ts()}"
            self._backoff(host); return None
    def _backoff(self, host):
        n = self.fails.get(host, 0) + 1; self.fails[host] = n
        d = min(120 if "jup" in host else 300, 5 * 2 ** (n - 1)) + random.random() * 2
        self.backoff_until[host] = time.time() + d
        log(f"[{self.name}] backoff {host} {d:.0f}s (fail #{n})")

http = Http("scan")        # discovery / entries thread
http_mon = Http("monitor") # position-monitor thread (own session + own rate limiter)
LOCK = threading.RLock()   # guards the ledger dict

# ---------------- Ledger ----------------
def new_ledger():
    S = P["START_USD"]
    return {"version": 2, "mode": "PAPER / SIMULATION ONLY", "created_at": ts(), "start_usd": S,
            "desks": {k: {"cash": S * w, "alloc": S * w} for k, w in P["DESKS"].items()},
            "open_positions": {}, "closed_trades": [], "bans": {}, "consec_losses": {}, "cooldowns": {},
            "day": {"date": today_et(), "start_equity": S, "halted": False},
            "equity_history": [], "heartbeat": None, "cycles": 0, "api_stats": {}, "last_candidates": [],
            "periods": [], "events": []}

def load_ledger():
    if os.path.exists(LEDGER):
        for i in range(5):
            try:
                with open(LEDGER) as f: L = json.load(f)
                break
            except Exception as e:
                log(f"ledger read failed ({e}), retry"); time.sleep(2)
        else:
            raise RuntimeError("ledger unreadable; refusing to start a new one over it")
        L.setdefault("periods", []); L.setdefault("events", [])
        log(f"resumed ledger: {len(L['open_positions'])} open, {len(L['closed_trades'])} closed")
        return L
    log("new ledger"); return new_ledger()

def save_ledger(L):
    with LOCK:
        data = json.dumps(L, indent=1)
    tmp = LEDGER + ".tmp"
    for i in range(3):
        try:
            with open(tmp, "w") as f: f.write(data)
            os.replace(tmp, LEDGER); return True
        except Exception as e:
            log(f"ledger save failed ({type(e).__name__}: {e}), retry"); time.sleep(1 + i)
    return False

def tlog(line):
    _append(TRADES_LOG, f"[{ts()}] {line}\n"); log("TRADE " + line)

def equity(L):
    cash = sum(d["cash"] for d in L["desks"].values())
    pos = sum(p["tokens"] * p.get("mark_price", p["entry_price"]) for p in L["open_positions"].values())
    return cash + pos

def event(L, kind, **kw):
    L["events"].append({"t": ts(), "kind": kind, **kw}); L["events"] = L["events"][-300:]

# ---------------- Data sources ----------------
def dex_pairs_for_tokens(addrs, h=None):
    h = h or http
    out = {}
    addrs = list(dict.fromkeys(addrs))
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i+30]
        d = h.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk))
        if not isinstance(d, list): continue
        for p in d:
            if p.get("chainId") != "solana": continue
            t = p["baseToken"]["address"]
            if t not in chunk: continue
            if p["quoteToken"]["address"] not in (SOL, USDC): continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if t not in out or liq > ((out[t].get("liquidity") or {}).get("usd") or 0): out[t] = p
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
                  "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=2",
                  "https://api.geckoterminal.com/api/v2/networks/solana/dexes/pumpswap/pools?page=1&sort=h24_tx_count_desc"]:
            d = http.get(u)
            if isinstance(d, dict) and "data" in d:
                for p in d["data"]:
                    try:
                        base = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
                        dex = p["relationships"]["dex"]["data"]["id"]
                        if dex == "pump-fun": continue
                        if float(p["attributes"].get("reserve_in_usd") or 0) < P["MIN_LIQ"]: continue
                        gt.append(base)
                    except Exception: pass
        _gecko_cache.update(t=time.time(), tokens=gt)
    toks += _gecko_cache["tokens"]
    with LOCK: toks += list(L["open_positions"].keys())
    skip = {SOL, USDC, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
    return [t for t in dict.fromkeys(toks) if t not in skip]

_rug_cache = {}
def rugcheck(mint, h=None):
    h = h or http
    c = _rug_cache.get(mint)
    if c and time.time() - c[0] < 900: return c[1]
    d = h.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", timeout=20)
    if isinstance(d, dict) and "__error__" not in d:
        _rug_cache[mint] = (time.time(), d); return d
    return None

_quote_cache = {}
def jup_quote(inp, out, amount_raw, h=None, slippage_bps=300, max_age=None):
    """Read-only Jupiter QUOTE (never /swap). Cached briefly; tries the fallback host only when the
    first one fails for a reason other than rate limiting."""
    h = h or http
    key = (inp, out, int(amount_raw))
    c = _quote_cache.get(key)
    if c and time.time() - c[0] < (P.get("QUOTE_CACHE_SEC", 3) if max_age is None else max_age): return c[1]
    params = {"inputMint": inp, "outputMint": out, "amount": str(int(amount_raw)), "slippageBps": slippage_bps, "swapMode": "ExactIn"}
    for u in JUP_QUOTE_URLS:
        d = h.get(u, params=params)
        if isinstance(d, dict) and d.get("outAmount"):
            _quote_cache[key] = (time.time(), d)
            if len(_quote_cache) > 500:
                for k in sorted(_quote_cache, key=lambda k: _quote_cache[k][0])[:250]: _quote_cache.pop(k, None)
            return d
        if isinstance(d, dict) and d.get("__error__") in (429, 400, 404): break
    return None

# ---------------- Price memory ----------------
PRICE_MEM = {}
def remember(tok, price):
    hh = PRICE_MEM.setdefault(tok, []); hh.append((time.time(), price))
    cut = time.time() - 3600
    while hh and hh[0][0] < cut: hh.pop(0)
def change_since(tok, minutes):
    hh = PRICE_MEM.get(tok) or []
    if len(hh) < 2: return None
    tgt = time.time() - minutes * 60
    if not [p for t, p in hh if t <= tgt + 30]: return None
    window = [p for t, p in hh if t >= tgt]
    lo = min(window) if window else hh[0][1]
    return (hh[-1][1] / lo - 1) * 100

# ---------------- Safety ----------------
def safety(mint, pair, strategy):
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
    if score >= P["MAX_RUG_SCORE"]: return False, f"rugcheck score_normalised {score} (high risk)", {}
    if (r.get("transferFee") or {}).get("pct"): return False, "transfer fee token", {}
    pool_accts = set()
    for k, v in (r.get("knownAccounts") or {}).items():
        if (v or {}).get("type") in ("AMM", "LOCKER"): pool_accts.add(k)
    for m in r.get("markets") or []:
        for f in ("pubkey", "liquidityA", "liquidityB"): pool_accts.add(m.get(f))
        for f in ("liquidityAAccount", "liquidityBAccount"): pool_accts.add((m.get(f) or {}).get("owner"))
    pool_accts.add(pair.get("pairAddress"))
    holders = [x for x in (r.get("topHolders") or []) if x.get("owner") not in pool_accts and x.get("address") not in pool_accts]
    top10 = sum(x.get("pct", 0) for x in holders[:10])
    if not r.get("topHolders"): return False, "no holder data", {}
    if top10 >= P["MAX_TOP10"]: return False, f"top10 {top10:.1f}% >= {P['MAX_TOP10']}%", {}
    lp_locked = 0.0
    try:
        mk = max(r.get("markets") or [], key=lambda m: (m.get("lp") or {}).get("lpLockedUSD") or 0)
        lp_locked = (mk.get("lp") or {}).get("lpLockedPct") or 0
    except ValueError: pass
    insiders = r.get("graphInsidersDetected") or 0
    info = {"rug_score_norm": score, "top10_ex_pool": round(top10, 1), "lp_locked_pct": round(lp_locked, 1),
            "insiders": insiders, "holders": r.get("totalHolders") or 0}
    if insiders > P.get("MAX_INSIDERS", 10**9): return False, f"insider network {insiders} > {P['MAX_INSIDERS']}", info
    if strategy == "S1_launch":
        s = P["S1"]
        if score > s["rug_max"]: return False, f"S1 needs rug score <={s['rug_max']} (got {score})", info
        if lp_locked < s["lp_min"]: return False, f"S1 needs LP burned/locked >={s['lp_min']}% (got {lp_locked:.0f}%)", info
        if top10 >= s["top10_max"]: return False, f"S1 needs top10 <{s['top10_max']}% (got {top10:.1f}%)", info
        if holders and holders[0].get("pct", 0) >= s["holder_max"]: return False, f"S1 single holder >={s['holder_max']}%", info
        if insiders > 0: return False, "S1 insider network detected", info
        if (r.get("totalHolders") or 0) < s["holders_min"]: return False, f"S1 needs >={s['holders_min']} holders", info
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
    if m["price"] <= 0 or m["liq"] < P["MIN_LIQ"]: return None, m
    if m["dex"] == "pumpfun": return None, m
    run15 = change_since(tok, 15)
    if m["m5"] > P["MAX_RECENT_PUMP"] or (run15 is not None and run15 > P["MAX_RECENT_PUMP"]): return None, m
    s1, s2, s3, en = P["S1"], P["S2"], P["S3"], P["ENABLED"]
    if en.get("S1_launch") and s1["age_min_h"] <= m["age_h"] < s1["age_max_h"] and m["liq"] >= s1["liq"] and m["v1"] >= s1["v1"] \
       and m["n1"] >= s1["n1"] and m["br5"] >= s1["br5"] and m["br1"] >= s1["br1"] and s1["m5_lo"] <= m["m5"] <= s1["m5_hi"] \
       and m["h1"] > s1["h1_lo"] and m["mcap"] <= s1["mcap_max"]:
        return "S1_launch", m
    if m["age_h"] < 1: return None, m
    if en.get("S2_dip") and m["liq"] >= s2["liq"] and m["v24"] >= s2["v24"] and m["h24"] > s2["h24_lo"] and m["h6"] > s2["h6_lo"] \
       and s2["h1_lo"] <= m["h1"] <= s2["h1_hi"] and s2["m5_lo"] <= m["m5"] <= s2["m5_hi"] and m["br5"] >= s2["br5"]:
        return "S2_dip", m
    avg6 = m["v6"] / 6 if m["v6"] else 0
    if en.get("S3_breakout") and m["liq"] >= s3["liq"] and m["v1"] >= s3["v1"] and avg6 > 0 and m["v1"] >= s3["vol_mult"] * avg6 \
       and s3["h1_lo"] <= m["h1"] <= s3["h1_hi"] and s3["m5_lo"] <= m["m5"] <= s3["m5_hi"] and m["br1"] >= s3["br1"] and m["br5"] >= s3["br5"]:
        return "S3_breakout", m
    return None, m

# ---------------- Paper fills via Jupiter quotes ----------------
def decimals_for(mint):
    c = _rug_cache.get(mint)
    if c: return (c[1].get("token") or {}).get("decimals")
    return None

def sim_buy(mint, usd):
    dec = decimals_for(mint)
    if dec is None: return None
    q = jup_quote(USDC, mint, usd * 1e6)
    if not q: return None
    impact = float(q.get("priceImpactPct") or 0) * 100
    tokens = int(q["outAmount"]) / 10 ** dec * (1 - P["FEE_PER_SIDE"])
    if tokens <= 0: return None
    return tokens, usd / tokens, impact

def sim_sell(mint, tokens, dec, h=None):
    if dec is None: return None
    q = jup_quote(mint, USDC, tokens * 10 ** dec, h=h)
    if not q: return None
    impact = float(q.get("priceImpactPct") or 0) * 100
    usd = int(q["outAmount"]) / 1e6 * (1 - P["FEE_PER_SIDE"])
    return usd, usd / tokens, impact

def copycat_ok(tok, pair, pairs):
    sym = (pair["baseToken"].get("symbol") or "").upper().lstrip("$")
    if sym in BLUECHIP_SYMBOLS: return False, f"copycat/bluechip ticker {sym}"
    rivals = [(t, p) for t, p in pairs.items() if (p["baseToken"].get("symbol") or "").upper().lstrip("$") == sym]
    if len(rivals) > 1:
        best = max(rivals, key=lambda tp: ((tp[1].get("liquidity") or {}).get("usd") or 0, -(tp[1].get("pairCreatedAt") or 0)))
        if best[0] != tok: return False, f"copycat of {sym} ({best[0][:6]}..)"
    return True, ""

# ---------------- Core ----------------
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
             "partial": frac < 0.999, "entry_safety": p.get("safety"), "params_version": p.get("params_version")}
    L["closed_trades"].append(trade)
    tlog(f"SELL {p['strategy']} {p['symbol']} {frac*100:.0f}% @ ${px:.10g} proceeds ${usd:.2f} pnl ${pnl:+.2f} ({trade['pnl_pct']:+.1f}%) reason={reason} impact={impact:.2f}%")
    p["tokens"] -= tokens; p["cost_usd"] -= cost; p["realized_usd"] = p.get("realized_usd", 0) + pnl
    if frac >= 0.999 or p["tokens"] <= 1e-12:
        total = p.get("realized_usd", 0)
        if total < 0:
            L["consec_losses"][tok] = L["consec_losses"].get(tok, 0) + 1
            if L["consec_losses"][tok] >= P["BAN_AFTER_LOSSES"]:
                L["bans"][tok] = {"symbol": p["symbol"], "since": ts()}; tlog(f"BAN {p['symbol']} {tok} after {P['BAN_AFTER_LOSSES']} losses in a row")
        else:
            L["consec_losses"][tok] = 0
        L["cooldowns"][tok] = time.time() + P["COOLDOWN_AFTER_EXIT_MIN"] * 60
        del L["open_positions"][tok]

def exit_decision(p, ret, px):
    age_h = (time.time() - p["entry_epoch"]) / 3600
    if ret <= P["HARD_STOP"]: return f"hard_stop {ret*100:.1f}%", 1.0
    if p.get("liq_now") and p["liq_now"] < 0.5 * p["entry_liq"]: return "liquidity_collapse", 1.0
    if not p.get("tp1_done") and ret >= P["TP1"]: return f"take_part +{ret*100:.1f}%", P["TP1_FRAC"]
    if p.get("tp1_done") and px <= p["peak_price"] * (1 - P["TRAIL"]): return f"trail -{P['TRAIL']*100:.0f}% from peak", 1.0
    if "BE_AFTER" in P and p.get("peak_price", 0) >= p["entry_price"] * (1 + P["BE_AFTER"]) and ret <= P.get("BE_LEVEL", 0.0):
        return f"breakeven_stop {ret*100:.1f}%", 1.0
    if age_h >= P["TIME_STOP_H"][p["strategy"]]: return f"time_stop {P['TIME_STOP_H'][p['strategy']]}h", 1.0
    return None, 1.0

def monitor_once(L, with_dex):
    """Fast loop: executable mark from a Jupiter SELL quote for the whole position; exits fill at that quote."""
    with LOCK: toks = list(L["open_positions"].keys())
    if not toks: return
    pairs = dex_pairs_for_tokens(toks, h=http_mon) if with_dex else {}
    for tok in toks:
        with LOCK:
            p = L["open_positions"].get(tok)
            if not p: continue
            tokens = p["tokens"]; dec = p.get("decimals")
        if dec is None:
            r = rugcheck(tok, h=http_mon); dec = ((r or {}).get("token") or {}).get("decimals")
        q = sim_sell(tok, tokens, dec, h=http_mon)
        with LOCK:
            p = L["open_positions"].get(tok)
            if not p: continue
            p["decimals"] = dec
            pr = pairs.get(tok)
            if pr:
                p["liq_now"] = (pr.get("liquidity") or {}).get("usd") or 0
                dpx = float(pr.get("priceUsd") or 0)
                if dpx > 0: p["dex_mark"] = dpx
            if q:
                px = q[1]; p["mark_price"] = px; p["mark_src"] = "jup_sell_quote"; p["mark_time"] = ts()
                p["sell_quote_fails"] = 0
            elif p.get("dex_mark"):
                px = p["dex_mark"]; p["mark_price"] = px; p["mark_src"] = "dexscreener"; p["mark_time"] = ts()
                p["sell_quote_fails"] = p.get("sell_quote_fails", 0) + 1
            else:
                p["sell_quote_fails"] = p.get("sell_quote_fails", 0) + 1; continue
            p["peak_price"] = max(p.get("peak_price", px), px)
            ret = px / p["entry_price"] - 1
            p["min_ret"] = min(p.get("min_ret", 0), ret); p["max_ret"] = max(p.get("max_ret", 0), ret)
            reason, frac = exit_decision(p, ret, px)
            if not reason: continue
            if LAST_WAKE["gap"] and time.time() - LAST_WAKE["t"] < 120:
                reason += f" [after {LAST_WAKE['gap']/60:.0f}min suspension gap]"
        # fill
        if frac >= 0.999 and q: fill = q
        else:
            fill = sim_sell(tok, tokens * frac, dec, h=http_mon)
        with LOCK:
            p = L["open_positions"].get(tok)
            if not p: continue
            if not fill:
                if p.get("sell_quote_fails", 0) < 3:
                    log(f"sell quote failed for {p['symbol']}, retry"); continue
                usd = p["tokens"] * frac * px * 0.95 * (1 - P["FEE_PER_SIDE"])
                fill = (usd, usd / (p["tokens"] * frac), 5.0); reason += " (no quote; 5% haircut fallback)"
            if frac < 0.999: p["tp1_done"] = True
            close_part(L, tok, frac, reason, fill)
        save_ledger(L)

LAST_WAKE = {"t": 0.0, "gap": 0.0}
def note_gap(L, src, gap):
    """Called when a loop notices wall-clock jumped (box/VM suspended). Positions were unprotected for `gap` s."""
    LAST_WAKE.update(t=time.time(), gap=gap)
    with LOCK:
        event(L, "wake_gap", src=src, seconds=round(gap), open=[p["symbol"] for p in L["open_positions"].values()])
        for p in L["open_positions"].values():
            p["unmonitored_s"] = round(p.get("unmonitored_s", 0) + gap)
    log(f"[{src}] wall-clock gap {gap:.0f}s (box suspended?) -> catch-up: re-marking {len(L['open_positions'])} open positions first")

def gap_thread(L):
    """Tiny 1-s ticker. A wall-clock jump means the box/VM was suspended (no process got CPU)."""
    last = time.time()
    while not STOP:
        time.sleep(1)
        now = time.time()
        if now - last > P.get("GAP_SEC", 45): note_gap(L, "ticker", now - last)
        last = now

def start_period_if_requested(L):
    """params.json may carry {"_period": {"name": "...", "note": "..."}}. If no period with that name exists,
    start a new measurement period: record start time and equity (history is never erased)."""
    req = P.get("_period")
    if not req or any(p.get("name") == req.get("name") for p in L.get("periods", [])): return
    with LOCK:
        per = {"name": req["name"], "start_time": ts(), "start_epoch": time.time(), "start_equity": round(equity(L), 2),
               "params_version": P.get("_version"), "closed_trades_before": len(L["closed_trades"]),
               "open_at_start": [p["symbol"] for p in L["open_positions"].values()], "note": req.get("note", "")}
        L["periods"].append(per)
    _append(os.path.join(HERE, "periods.jsonl"), json.dumps(per) + "\n")
    tlog(f"PERIOD START {per['name']} equity ${per['start_equity']:.2f} params={per['params_version']}")
    save_ledger(L)

def monitor_thread(L):
    n = 0
    while not STOP:
        t0 = time.time()
        try:
            monitor_once(L, with_dex=(n % 4 == 0)); n += 1
            touch_heartbeat("monitor")
        except Exception:
            log("monitor error:\n" + traceback.format_exc())
        dt = P["FAST_SEC"] - (time.time() - t0)
        if dt > 0: time.sleep(dt)

def try_entries(L, pairs):
    if time.time() - LAST_WAKE["t"] < P.get("WAKE_ENTRY_PAUSE_SEC", 30): return   # catch-up: exits first
    with LOCK:
        roll_day(L)
        eq = equity(L)
        if not L["day"]["halted"] and eq <= L["day"]["start_equity"] * (1 - P["DAILY_LOSS_HALT"]):
            L["day"]["halted"] = True; tlog(f"HALT new entries: equity ${eq:.2f} <= -{P['DAILY_LOSS_HALT']*100:.0f}% of day start ${L['day']['start_equity']:.2f}")
        if L["day"]["halted"]: return
        cands = []
        for tok, pr in pairs.items():
            if tok in L["open_positions"] or tok in L["bans"] or L["cooldowns"].get(tok, 0) > time.time(): continue
            strat, m = signal_for(tok, pr)
            if strat: cands.append((tok, pr, strat, m))
    cands.sort(key=lambda c: c[3]["liq"], reverse=True)
    lc = []
    for tok, pr, strat, m in cands[:6]:
        if STOP: break
        with LOCK:
            if len(L["open_positions"]) >= P["MAX_OPEN"]: break
        sym = pr["baseToken"].get("symbol")
        rec = {"token": tok, "symbol": sym, "strategy": strat, "liq": round(m["liq"]), "m5": m["m5"], "h1": m["h1"], "age_h": round(m["age_h"], 2), "t": ts()}
        ok, why = copycat_ok(tok, pr, pairs)
        if ok: ok, why, info = safety(tok, pr, strat)
        else: info = {}
        rec["decision"] = "pass" if ok else f"reject: {why}"; lc.append(rec)
        if not ok:
            log(f"candidate {strat} {sym} rejected: {why}"); continue
        with LOCK:
            desk = L["desks"][strat]
            size = min(P["MAX_POS_PCT"] * eq, desk["cash"], P["LIQ_POS_FRAC"] * m["liq"])
        if size < P["MIN_POS_USD"]:
            rec["decision"] = f"skip: size ${size:.2f} too small (desk cash ${desk['cash']:.2f})"; continue
        fill = None
        for _ in range(3):
            fill = sim_buy(tok, size)
            if not fill: break
            if fill[2] <= P["MAX_IMPACT_PCT"]: break
            size /= 2; fill = None
            if size < P["MIN_POS_USD"]: break
        if not fill:
            rec["decision"] = "skip: no quote / impact too high"; continue
        tokens, eff_px, impact = fill
        dev = eff_px / m["price"] - 1 if m["price"] else 0
        if dev > P.get("MAX_QUOTE_DEV", 0.04):
            # v3: quote fills far above the DexScreener mark = stale/odd market data (MICHIKO bought +33% over dex, stopped 3 s later)
            rec["decision"] = f"skip: quote {dev*100:+.1f}% vs dex price"; log(f"candidate {strat} {sym} skipped: Jupiter fill {dev*100:+.1f}% above dex price"); continue
        with LOCK:
            desk["cash"] -= size
            L["open_positions"][tok] = {"token": tok, "symbol": sym, "strategy": strat, "entry_time": ts(), "entry_epoch": time.time(),
                "entry_price": eff_px, "dex_price_at_entry": m["price"], "tokens": tokens, "cost_usd": size, "entry_impact_pct": round(impact, 3),
                "entry_liq": m["liq"], "mark_price": eff_px, "peak_price": eff_px, "tp1_done": False, "decimals": decimals_for(tok),
                "pair": pr.get("pairAddress"), "dex": m["dex"], "signal": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()},
                "safety": info, "params_version": P.get("_version", "v1")}
        rec["decision"] = "BOUGHT"
        tlog(f"BUY {strat} {sym} {tok} ${size:.2f} @ ${eff_px:.10g} (dex ${m['price']:.10g}) impact={impact:.2f}% liq=${m['liq']:,.0f} safety={info} params={P.get('_version','v1')}")
        save_ledger(L)
    with LOCK: L["last_candidates"] = lc

def touch_heartbeat(src):
    try:
        with open(HEARTBEAT + "." + src, "w") as f: f.write(str(time.time()))
    except Exception: pass

def cycle(L):
    toks = discover(L)
    pairs = dex_pairs_for_tokens(toks)
    for t, p in pairs.items():
        try: remember(t, float(p.get("priceUsd") or 0))
        except Exception: pass
    try_entries(L, pairs)
    with LOCK:
        eq = equity(L)
        L["cycles"] += 1
        L["heartbeat"] = {"time": ts(), "epoch": time.time(), "pid": os.getpid(), "supervisor_pid": os.getppid(),
                          "tokens_scanned": len(toks), "pairs_priced": len(pairs), "equity": round(eq, 2), "params_version": P.get("_version", "v1")}
        L["api_stats"] = {"scan": http.stats, "monitor": http_mon.stats}
        if L["cycles"] % 15 == 1:
            L["equity_history"].append({"t": ts(), "equity": round(eq, 2)}); L["equity_history"] = L["equity_history"][-2000:]
        nopen = len(L["open_positions"])
    save_ledger(L); touch_heartbeat("scan")
    if L["cycles"] % 15 == 1:
        log(f"cycle {L['cycles']}: scanned {len(toks)} tokens, priced {len(pairs)}, open {nopen}, equity ${eq:.2f}")

def write_status(L):
    """Write STATUS.md (repo root) via status.py. Never raises."""
    try:
        import status
        with LOCK: md = status.markdown(L)
        tmp = os.path.join(HERE, "STATUS.md.tmp")
        with open(tmp, "w") as f: f.write(md)
        os.replace(tmp, os.path.join(HERE, "STATUS.md"))
    except Exception:
        log("status write error:\n" + traceback.format_exc())

def worker():
    global STOP
    # detach stdout/stderr from whatever the launcher gave us (possibly a FUSE file) -> local disk
    try:
        fd = os.open(WORKER_ERR, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(fd, 1); os.dup2(fd, 2)
        _ff = open(WORKER_ERR, "a")
        faulthandler.enable(file=_ff, all_threads=True)
        faulthandler.register(signal.SIGUSR1, file=_ff, all_threads=True)
    except Exception: pass
    try: os.chdir(LOCAL_DIR)
    except Exception: pass
    def _sig(signum, _f):
        global STOP; STOP = True; log(f"worker got signal {signum}, stopping")
    signal.signal(signal.SIGTERM, _sig); signal.signal(signal.SIGINT, _sig); signal.signal(signal.SIGHUP, signal.SIG_IGN)
    sys.excepthook = lambda t, v, tb: log("UNCAUGHT:\n" + "".join(traceback.format_exception(t, v, tb)))
    threading.excepthook = lambda a: log("UNCAUGHT in thread:\n" + "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    atexit.register(lambda: log("worker exiting (atexit)"))
    load_params()
    log(f"PAPER worker starting (SIMULATION ONLY - no wallet, no transactions) params={P.get('_version','v1')}")
    L = load_ledger(); save_ledger(L)
    start_period_if_requested(L)
    with LOCK:
        hb = L.get("heartbeat") or {}
        if hb.get("epoch") and time.time() - hb["epoch"] > 120:
            event(L, "downtime", seconds=round(time.time() - hb["epoch"]), last_heartbeat=hb.get("time"))
            LAST_WAKE.update(t=time.time(), gap=time.time() - hb["epoch"])
            for p in L["open_positions"].values(): p["unmonitored_s"] = round(p.get("unmonitored_s", 0) + LAST_WAKE["gap"])
            log(f"previous heartbeat {hb.get('time')} ({(time.time()-hb['epoch'])/60:.1f} min ago) -> downtime recorded")
    threading.Thread(target=gap_thread, args=(L,), daemon=True).start()
    th = threading.Thread(target=monitor_thread, args=(L,), daemon=True); th.start()
    last_tick = time.time()
    sys.path.insert(0, HERE)
    last_status = 0.0
    while not STOP:
        t0 = time.time()
        last_tick = t0
        if t0 - last_status >= STATUS_EVERY_SEC:
            write_status(L); last_status = t0
        try: cycle(L)
        except Exception: log("cycle error:\n" + traceback.format_exc())
        if not th.is_alive():
            log("monitor thread died; restarting it"); th = threading.Thread(target=monitor_thread, args=(L,), daemon=True); th.start()
        sl = max(3, P["POLL_SEC"] - (time.time() - t0))
        end = time.time() + sl
        while not STOP and time.time() < end:
            a = time.time(); time.sleep(0.5)
            if time.time() - a > P.get("GAP_SEC", 45): break     # woke from suspension: cycle now
        last_tick = time.time()
    th.join(timeout=15)   # let an in-flight monitor fill finish before the final save
    save_ledger(L); write_status(L); log("worker stopped cleanly (ledger saved atomically)")

# ---------------- Supervisor ----------------
def _is_bot(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f: cmd = f.read().decode(errors="ignore")
        return "bot.py" in cmd and "solana" not in cmd.split("\0")[0]
    except Exception: return False

def supervisor():
    global STOP
    try:
        fd = os.open(os.path.join(LOCAL_DIR, "supervisor_stderr.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(fd, 1); os.dup2(fd, 2)
    except Exception: pass
    try:
        old = int(open(PID_FILE).read().strip())
        if old != os.getpid() and _is_bot(old):
            log(f"another supervisor (pid {old}) is alive; exiting to avoid duplicates"); return
    except Exception: pass
    try: os.chdir(LOCAL_DIR)
    except Exception: pass
    with open(PID_FILE, "w") as f: f.write(str(os.getpid()))
    child = None
    def _sig(signum, _f):
        global STOP; STOP = True
        log(f"supervisor got signal {signum}; stopping worker")
        if child and child.poll() is None:
            try: child.terminate()
            except Exception: pass
    signal.signal(signal.SIGTERM, _sig); signal.signal(signal.SIGINT, _sig); signal.signal(signal.SIGHUP, signal.SIG_IGN)
    log("supervisor starting (PAPER ONLY)")
    restarts = []
    while not STOP:
        for s in ("scan", "monitor"):
            try: os.remove(HEARTBEAT + "." + s)
            except Exception: pass
        try:
            child = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "bot.py"), "--worker"], cwd=LOCAL_DIR,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=False)
        except Exception as e:
            log(f"spawn failed: {e}"); time.sleep(10); continue
        log(f"worker started pid={child.pid}")
        started = time.time(); prev = time.time()
        while not STOP and child.poll() is None:
            time.sleep(1)
            now = time.time()
            if now - prev > 30:
                # The supervisor itself did not run for this long: the whole box was suspended, the worker
                # is not hung. Give it a grace period to catch up instead of killing it (v2 killed it every hour).
                log(f"supervisor loop gap {now-prev:.0f}s (box was suspended, not a worker hang); grace 90 s")
                _append(os.path.join(HERE, "diag", "suspensions.csv"),
                        f"{ts()},{now-prev:.0f},{open('/proc/sys/kernel/random/boot_id').read().strip()[:8]}\n")
                started = now - 120 + 90
            prev = now
            # hung-worker detection (real hangs only): heartbeats must be fresh after a 2 min grace
            if now - started > 120:
                stale = []
                for s, lim in (("scan", P.get("STALE_SCAN_SEC", 180)), ("monitor", P.get("STALE_MON_SEC", 120))):
                    try: age = now - float(open(HEARTBEAT + "." + s).read())
                    except Exception: age = 1e9
                    if age > lim: stale.append(f"{s} {age:.0f}s")
                if stale:
                    log(f"worker heartbeat stale ({', '.join(stale)}) while box awake -> real hang; dumping stacks, killing pid {child.pid}")
                    try: os.kill(child.pid, signal.SIGUSR1); time.sleep(1)
                    except Exception: pass
                    child.terminate()
                    try: child.wait(20)
                    except Exception: child.kill()
        if STOP:
            try: child.wait(90)
            except Exception: child.kill()
            break
        rc = child.returncode
        tail = ""
        try:
            with open(WORKER_ERR) as f: tail = "".join(f.readlines()[-25:])
        except Exception: pass
        log(f"worker pid {child.pid} exited rc={rc} ({'signal ' + str(-rc) if rc and rc < 0 else 'exit'}); stderr tail:\n{tail}")
        restarts = [t for t in restarts if time.time() - t < 600] + [time.time()]
        time.sleep(5 if len(restarts) < 4 else 60)
    log("supervisor stopped cleanly")

if __name__ == "__main__":
    if ROLE == "worker": worker()
    else: supervisor()
