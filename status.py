#!/usr/bin/env python3
"""Scorecard for the PAPER Solana memecoin bot. Reads ledger_solana.json (no network)."""
import json, os, sys
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__))

def alive(pid):
    try: os.kill(int(pid), 0); return True
    except Exception: return False

def _stats(trs):
    pos = defaultdict(float)
    for t in trs: pos[(t["token"], t["entry_time"])] += t["pnl_usd"]
    w = sum(1 for v in pos.values() if v >= 0); n = len(pos)
    return n, w, sum(pos.values())
def _fmt_api(v):
    if isinstance(v, dict) and "ok" in v: return f"ok {v['ok']} / err {v['err']}" + (f" / 429 {v['r429']}" if v.get("r429") else "")
    if isinstance(v, dict): return {h: _fmt_api(x) for h, x in v.items()}
    return str(v)

def _pos_stats(trs):
    """(n positions, wins, realized) grouping partial fills by token+entry_time."""
    return _stats(trs)

def markdown(L):
    """STATUS.md for the GitHub repo. Times are America/New_York (ET)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S ET")
    cash = sum(d["cash"] for d in L["desks"].values())
    opn = L["open_positions"].values()
    pos_val = sum(p["tokens"] * p.get("mark_price", p["entry_price"]) for p in opn)
    eq = cash + pos_val; start = L["start_usd"]; closed = L["closed_trades"]; hb = L.get("heartbeat") or {}
    o = ["# PAPER TRADING — no real money", "",
         "Simulated Solana memecoin bot. No wallet, no keys, no transactions; fills are Jupiter *quotes* only.", "",
         f"- **Updated:** {now}", f"- **Last tick:** {hb.get('time', 'n/a')} (cycle {L.get('cycles')})",
         f"- **Params:** {hb.get('params_version', 'n/a')}",
         f"- **Equity:** ${eq:,.2f} vs start ${start:,.2f} → ${eq-start:+,.2f} ({(eq/start-1)*100:+.2f}%)",
         f"- **Cash:** ${cash:,.2f} | open position value ${pos_val:,.2f}",
         f"- **Today ({L['day']['date']} ET):** ${eq-L['day']['start_equity']:+,.2f} vs day start; entries {'HALTED (daily loss limit)' if L['day']['halted'] else 'allowed'}", ""]
    n, w, r = _stats(closed)
    o.append(f"**All-time:** {n} closed positions, {w}W / {n-w}L ({w/n*100 if n else 0:.0f}% win), realized ${r:+,.2f}")
    o.append("")
    for per in L.get("periods") or []:
        n, w, r = _stats(closed[per.get("closed_trades_before", 0):]); se = per["start_equity"]
        o.append(f"**Since {per['name']}** (started {per['start_time']}): equity ${eq:,.2f} vs ${se:,.2f} → ${eq-se:+,.2f} ({(eq/se-1)*100:+.2f}%); "
                 f"{n} closed, {w}W / {n-w}L, realized ${r:+,.2f}")
        o.append("")
    o += [f"## Open positions ({len(L['open_positions'])})", ""]
    if L["open_positions"]:
        o += ["| Symbol | Strategy | Entered (ET) | Cost | Entry | Current | PnL |", "|---|---|---|---|---|---|---|"]
        for p in opn:
            m = p.get("mark_price", p["entry_price"]); val = p["tokens"] * m
            o.append(f"| {p['symbol']} | {p['strategy']} | {p['entry_time']} | ${p['cost_usd']:.2f} | ${p['entry_price']:.8g} | ${m:.8g} | "
                     f"${val-p['cost_usd']:+.2f} ({(m/p['entry_price']-1)*100:+.1f}%) |")
    else: o.append("None.")
    o += ["", "## Last 5 fills", ""]
    if closed:
        o += ["| Exit (ET) | Symbol | PnL | Reason |", "|---|---|---|---|"]
        for t in closed[-5:][::-1]:
            o.append(f"| {t['exit_time']} | {t['symbol']} | ${t['pnl_usd']:+.2f} ({t['pnl_pct']:+.1f}%) | {t['reason']} |")
    else: o.append("None yet.")
    o += ["", f"API: `{ {h: _fmt_api(v) for h, v in (L.get('api_stats') or {}).items()} }`", ""]
    return "\n".join(o)

def main():
    L = json.load(open(os.path.join(HERE, "ledger_solana.json")))


    cash = sum(d["cash"] for d in L["desks"].values())
    pos_val = sum(p["tokens"] * p.get("mark_price", p["entry_price"]) for p in L["open_positions"].values())
    eq = cash + pos_val
    start = L["start_usd"]
    hb = L.get("heartbeat") or {}
    today = L["day"]["date"]
    closed = L["closed_trades"]
    realized_today = sum(t["pnl_usd"] for t in closed if t["exit_time"].startswith(today))
    print("=" * 64)
    print(" SOLANA MEMECOIN BOT — PAPER TRADING (SIMULATION ONLY)")
    print("=" * 64)
    print(f" Process: pid {hb.get('pid')} {'RUNNING' if hb.get('pid') and alive(hb['pid']) else 'NOT RUNNING'} | last cycle {hb.get('time')} | cycles {L.get('cycles')}")
    print(f" Equity: ${eq:,.2f} vs start ${start:,.2f}  ->  total P&L ${eq-start:+,.2f} ({(eq/start-1)*100:+.2f}%)")
    print(f" Today ({today} ET): equity vs day start ${eq-L['day']['start_equity']:+,.2f} | realized today ${realized_today:+,.2f}"
          f" | entries {'HALTED (daily loss limit)' if L['day']['halted'] else 'allowed'}")
    print(f" Cash ${cash:,.2f} | open position value ${pos_val:,.2f}")
    print("\n Desks:")
    for k, d in L["desks"].items():
        print(f"   {k:12s} cash ${d['cash']:8.2f} (alloc ${d['alloc']:.2f})")
    print("\n By strategy (round trips; partial exits counted with final leg):")
    agg = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0, "n": 0})
    for t in closed:
        a = agg[t["strategy"]]; a["pnl"] += t["pnl_usd"]; a["n"] += 1
        if not t.get("partial"):
            pass
    # W/L per position: group by token+entry_time
    pos = defaultdict(float); posstrat = {}
    for t in closed:
        key = (t["token"], t["entry_time"]); pos[key] += t["pnl_usd"]; posstrat[key] = t["strategy"]
    for key, pnl in pos.items():
        a = agg[posstrat[key]]
        if pnl >= 0: a["w"] += 1
        else: a["l"] += 1
    for k in L["desks"]:
        a = agg[k]
        print(f"   {k:12s} W {a['w']:3d} / L {a['l']:3d} | fills {a['n']:3d} | realized ${a['pnl']:+.2f}")
    print(f"\n Open positions ({len(L['open_positions'])}/5):")
    for p in L["open_positions"].values():
        m = p.get("mark_price", p["entry_price"]); r = (m / p["entry_price"] - 1) * 100
        print(f"   {p['strategy']:12s} {p['symbol']:<10s} cost ${p['cost_usd']:.2f} entry ${p['entry_price']:.8g} mark ${m:.8g} ({r:+.1f}%)"
              f" tp1={'yes' if p.get('tp1_done') else 'no'} since {p['entry_time']}"
              + (f" | unmonitored {p['unmonitored_s']/60:.0f} min" if p.get('unmonitored_s') else ""))
    if closed:
        s = sorted(closed, key=lambda t: t["pnl_usd"])
        for lbl, t in (("Best", s[-1]), ("Worst", s[0])):
            print(f"\n {lbl} trade: {t['strategy']} {t['symbol']} ${t['pnl_usd']:+.2f} ({t['pnl_pct']:+.1f}%) reason {t['reason']} exit {t['exit_time']}")
        print("\n Last 5 fills:")
        for t in closed[-5:]:
            print(f"   {t['exit_time']} {t['strategy']} {t['symbol']} ${t['pnl_usd']:+.2f} ({t['pnl_pct']:+.1f}%) {t['reason']}")
    else:
        print("\n No closed trades yet.")
    if L["bans"]: print("\n Banned tokens:", ", ".join(v["symbol"] for v in L["bans"].values()))
    lc = L.get("last_candidates") or []
    if lc:
        print("\n Last cycle candidates:")
        for c in lc: print(f"   {c['strategy']:12s} {c['symbol']:<10s} liq ${c['liq']:,} m5 {c['m5']}% h1 {c['h1']}% -> {c['decision']}")

    # ---- measurement periods (v3): all-time above, plus each tuning period since its start ----
    n, w, r = _stats(closed)
    print(f"\n ALL-TIME: {n} closed positions, win rate {w/n*100 if n else 0:.0f}% ({w}W/{n-w}L), realized ${r:+.2f}, equity ${eq:,.2f} ({(eq/start-1)*100:+.2f}% vs ${start:,.0f})")
    for per in L.get("periods") or []:
        trs = closed[per.get("closed_trades_before", 0):]
        n, w, r = _stats(trs)
        se = per["start_equity"]
        print(f" SINCE {per['name']} (started {per['start_time']}, params {per.get('params_version')}): equity ${eq:,.2f} vs ${se:,.2f}"
              f" -> ${eq-se:+,.2f} ({(eq/se-1)*100:+.2f}%) | {n} closed positions, win rate {w/n*100 if n else 0:.0f}% ({w}W/{n-w}L), realized ${r:+.2f}")
    ev = L.get("events") or []
    gaps = [e for e in ev if e.get("kind") in ("wake_gap", "downtime")]
    if gaps:
        last24 = [e for e in gaps if e["t"] >= (hb.get("time") or "")[:10]]
        print(f" Suspension/downtime gaps logged (last 300 events): {len(gaps)}; today {len(last24)}, "
              f"{sum(e.get('seconds', 0) for e in last24)/60:.0f} min unmonitored today")

    print("\n API:", {h: _fmt_api(v) for h, v in (L.get('api_stats') or {}).items()})


if __name__ == "__main__":
    if "--md" in sys.argv:
        print(markdown(json.load(open(os.path.join(HERE, "ledger_solana.json")))))
    else: main()
