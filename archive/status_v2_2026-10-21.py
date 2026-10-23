#!/usr/bin/env python3
"""Scorecard for the PAPER Solana memecoin bot. Reads ledger_solana.json (no network)."""
import json, os, sys
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__))
L = json.load(open(os.path.join(HERE, "ledger_solana.json")))

def alive(pid):
    try: os.kill(int(pid), 0); return True
    except Exception: return False

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
          f" tp1={'yes' if p.get('tp1_done') else 'no'} since {p['entry_time']}")
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
print("\n API:", {h: f"ok {v['ok']} / err {v['err']}" for h, v in (L.get('api_stats') or {}).items()})
