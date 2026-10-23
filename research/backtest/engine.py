import os; BT = os.environ.get('BT_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))  # local backtest data dir (gitignored)
"""Replay/backtest engine for the PAPER bot's rules on survivorship-free post-graduation snapshots.
Each snapshot carries DexScreener-style fields (price, liq, m5/h1/h6/h24 change, volumes, buy/sell counts),
the same inputs the live bot uses. Entry fills at the NEXT snapshot's price (latency), exits are only checked at
snapshots that fall inside 'awake' windows (duty-cycle model of the suspended box)."""
import numpy as np, pandas as pd, math, random

def load(path=os.path.join(BT, 'snap.parquet')):
    s = pd.read_parquet(path)
    for c in ['m5', 'h1', 'h6', 'h24']: s[c] = s[c].fillna(0.0)
    toks = {}
    for mint, g in s.groupby('mint', sort=False):
        toks[mint] = {c: g[c].to_numpy() for c in g.columns if c != 'mint'}
    return toks

# ---- duty cycle: which wall-clock seconds the box is awake ----
class Awake:
    def __init__(self, mode, seed=7):
        self.mode = mode; self.cache = {}; self.seed = seed
    def __call__(self, t):
        m = self.mode
        if m == 'continuous': return True
        period, length = {'hourly': (3600, 330), '15min': (900, 150), '5min': (300, 90)}[m]
        k = int(t // period)
        if k not in self.cache:
            r = random.Random(self.seed * 1000003 + k)
            self.cache[k] = k * period + r.uniform(0, period - length)
        st = self.cache[k]
        return st <= t < st + length

def signal(p, d, i):
    """p = params dict, d = token arrays, i = index. returns strategy name or None"""
    px = d['px'][i]; liq = d['liq'][i]; age_h = d['age'][i] / 3600
    if px <= 0 or liq < p['MIN_LIQ']: return None
    m5 = d['m5'][i]; h1 = d['h1'][i]
    if m5 > p['MAX_RECENT_PUMP']: return None
    b5, s5, b1, s1 = d['b5'][i], d['s5'][i], d['b1'][i], d['s1'][i]
    br5 = b5 / max(1, s5); br1 = b1 / max(1, s1); n1 = b1 + s1
    en = p['ENABLED']
    q = p['S1']
    if en.get('S1') and q['age_min_h'] <= age_h < q['age_max_h'] and liq >= q['liq'] and d['v1'][i] >= q['v1'] and n1 >= q['n1'] \
       and br5 >= q['br5'] and br1 >= q['br1'] and q['m5_lo'] <= m5 <= q['m5_hi'] and h1 > q['h1_lo'] and d['mcap'][i] <= q['mcap_max']:
        return 'S1'
    if age_h < 1: return None
    q = p['S2']
    if en.get('S2') and liq >= q['liq'] and d['v24'][i] >= q['v24'] and d['h24'][i] > q['h24_lo'] and d['h6'][i] > q['h6_lo'] \
       and q['h1_lo'] <= h1 <= q['h1_hi'] and q['m5_lo'] <= m5 <= q['m5_hi'] and br5 >= q['br5']:
        return 'S2'
    q = p['S3']; avg6 = d['v6'][i] / 6
    if en.get('S3') and liq >= q['liq'] and d['v1'][i] >= q['v1'] and avg6 > 0 and d['v1'][i] >= q['vol_mult'] * avg6 \
       and q['h1_lo'] <= h1 <= q['h1_hi'] and q['m5_lo'] <= m5 <= q['m5_hi'] and br1 >= q['br1'] and br5 >= q['br5']:
        return 'S3'
    return None

def cost_side(p, size, liq):
    # pool fee + bot allowance + constant-product impact (quote reserve ~ liq/2)
    return p['POOL_FEE'] + p['FEE_PER_SIDE'] + min(0.5, 2 * size / max(liq, 1))

def run_token(p, d, awake, size=75.0):
    trades = []; n = len(d['t']); i = 0; cooldown_until = 0; losses = 0; ntr = 0
    while i < n - 1:
        t = d['t'][i]
        if t < cooldown_until or not awake(t) or ntr >= p['MAX_TRADES_PER_TOKEN'] or losses >= p['BAN_AFTER_LOSSES']:
            i += 1; continue
        st = signal(p, d, i)
        if not st: i += 1; continue
        j = i + 1                                    # fill at next snapshot (latency)
        if d['t'][j] - t > 600: i += 1; continue    # data hole: skip
        liq0 = d['liq'][i]
        sz = min(size, p['LIQ_POS_FRAC'] * liq0)
        if sz < p['MIN_POS_USD']: i += 1; continue
        entry = d['px'][j] * (1 + cost_side(p, sz, d['liq'][j]))
        peak = entry; tp1 = False; remaining = 1.0; realized = 0.0; reason = None; k = j
        tstop = p['TIME_STOP_H'][st] * 3600
        last_awake_k = j
        for k in range(j + 1, n):
            tk = d['t'][k]
            if not awake(tk): continue
            pxk = d['px'][k]; ret = pxk / entry - 1
            peak = max(peak, pxk)
            gap = tk - d['t'][last_awake_k]; last_awake_k = k
            exitfrac = 0
            if ret <= p['HARD_STOP']: reason, exitfrac = 'stop', 1
            elif d['liq'][k] < 0.5 * liq0: reason, exitfrac = 'liq', 1
            elif not tp1 and ret >= p['TP1']: reason, exitfrac = 'tp1', p['TP1_FRAC']
            elif tp1 and pxk <= peak * (1 - p['TRAIL']): reason, exitfrac = 'trail', 1
            elif p.get('BE_AFTER') and peak >= entry * (1 + p['BE_AFTER']) and ret <= p.get('BE_LEVEL', 0): reason, exitfrac = 'be', 1
            elif tk - d['t'][j] >= tstop: reason, exitfrac = 'time', 1
            if exitfrac:
                frac = remaining * exitfrac if reason != 'tp1' else exitfrac
                fill = pxk * (1 - cost_side(p, sz * frac * (1 + max(ret, -0.99)), d['liq'][k]))
                realized += frac * (fill / entry - 1)
                remaining -= frac
                if reason == 'tp1': tp1 = True; reason = None; continue
                break
        if remaining > 1e-9:   # data ended: mark at last price
            fill = d['px'][n - 1] * (1 - cost_side(p, sz, d['liq'][n - 1])); realized += remaining * (fill / entry - 1)
            reason = reason or 'eod'; k = n - 1
        pnl_pct = realized
        trades.append({'strat': st, 't_entry': d['t'][j], 't_exit': d['t'][k], 'age_h': d['age'][i] / 3600, 'ret': pnl_pct,
                       'usd': pnl_pct * sz, 'size': sz, 'reason': reason, 'hold_min': (d['t'][k] - d['t'][j]) / 60})
        ntr += 1; losses = losses + 1 if pnl_pct < 0 else 0
        cooldown_until = d['t'][k] + p['COOLDOWN_MIN'] * 60
        i = k + 1
    return trades

def run(p, toks, mints, mode='continuous', seed=7):
    aw = Awake(mode, seed); out = []
    for m in mints:
        for tr in run_token(p, toks[m], aw): tr['mint'] = m; out.append(tr)
    return pd.DataFrame(out)

def summarize(df):
    if df is None or len(df) == 0: return {'n': 0}
    df = df.sort_values('t_exit'); cum = df.usd.cumsum(); dd = (cum.cummax().clip(lower=0) - cum).max()
    w = df[df.ret > 0]; l = df[df.ret <= 0]
    return {'n': len(df), 'win%': round(len(w) / len(df) * 100, 1), 'avg%': round(df.ret.mean() * 100, 2),
            'med%': round(df.ret.median() * 100, 2), 'tot$': round(df.usd.sum(), 1), 'maxDD$': round(dd, 1),
            'PF': round(w.usd.sum() / max(1e-9, -l.usd.sum()), 2), 'worst%': round(df.ret.min() * 100, 1)}

BASE = {  # live bot v1 params, mapped
    'MIN_LIQ': 12000, 'MAX_RECENT_PUMP': 30, 'ENABLED': {'S1': True, 'S2': True, 'S3': True},
    'S1': {"age_min_h": 10/60, "age_max_h": 1.0, "liq": 20000, "v1": 40000, "n1": 300, "br5": 1.2, "br1": 1.15, "m5_lo": 2, "m5_hi": 20, "h1_lo": 0, "mcap_max": 5e6},
    'S2': {"liq": 40000, "v24": 150000, "h24_lo": 0, "h6_lo": -35, "h1_lo": -30, "h1_hi": -10, "m5_lo": 0.5, "m5_hi": 8, "br5": 1.0},
    'S3': {"liq": 30000, "v1": 25000, "vol_mult": 1.8, "h1_lo": 5, "h1_hi": 40, "m5_lo": 0, "m5_hi": 8, "br1": 1.15, "br5": 1.0},
    'HARD_STOP': -0.08, 'TP1': 0.25, 'TP1_FRAC': 0.5, 'TRAIL': 0.15, 'TIME_STOP_H': {'S1': 2, 'S2': 12, 'S3': 12},
    'LIQ_POS_FRAC': 0.01, 'MIN_POS_USD': 10, 'FEE_PER_SIDE': 0.0025, 'POOL_FEE': 0.01,
    'COOLDOWN_MIN': 60, 'BAN_AFTER_LOSSES': 2, 'MAX_TRADES_PER_TOKEN': 99,
}
