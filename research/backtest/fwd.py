import os; BT = os.environ.get('BT_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))  # local backtest data dir (gitignored)
import sys; sys.path.insert(0, BT)
from engine import *
import numpy as np, pandas as pd, copy
toks = load(); mints = sorted(toks, key=lambda m: toks[m]['t'][0]); cut = int(len(mints)*0.6)
def fwd(sel, horizons=(300, 900, 3600, 7200, 21600)):
    rows = []
    for m in mints[:cut]:
        d = toks[m]; t = d['t']; last = -1e18
        for i in range(len(t)-1):
            if t[i] - last < 3600: continue
            if not sel(d, i): continue
            last = t[i]; j = i+1; e = d['px'][j]*1.0125
            r = {'age_h': d['age'][i]/3600}
            for h in horizons:
                k = np.searchsorted(t, t[j]+h)
                r[h] = d['px'][min(k, len(t)-1)]*(1-0.0125)/e - 1 if k < len(t) else np.nan
            # path min within 1h
            k1 = np.searchsorted(t, t[j]+3600); r['min1h'] = d['px'][j:k1+1].min()/e-1; r['max1h'] = d['px'][j:k1+1].max()/e-1
            rows.append(r)
    return pd.DataFrame(rows)
def rep(name, df):
    if not len(df): print(name, 'none'); return
    s = {h: f"mean {df[h].mean()*100:+.1f} med {df[h].median()*100:+.1f} win {(df[h]>0).mean()*100:.0f}%" for h in (300, 900, 3600, 7200, 21600)}
    print(f"{name} n={len(df)}", s, f"max1h med {df.max1h.median()*100:+.0f}% min1h med {df.min1h.median()*100:+.0f}%")
P = BASE
for lo, hi in [(0, 10/60), (10/60, 1), (1, 3), (3, 6), (6, 24), (24, 48)]:
    rep(f'random age {lo:.2f}-{hi}h liq>=20k', fwd(lambda d, i: lo <= d['age'][i]/3600 < hi and d['liq'][i] >= 20000))
rep('S1 signal', fwd(lambda d, i: signal(P, d, i) == 'S1'))
rep('S2 signal', fwd(lambda d, i: signal(P, d, i) == 'S2'))
rep('S3 signal', fwd(lambda d, i: signal(P, d, i) == 'S3'))
rep('age1-6h liq>=30k h1<-30 (deep dip)', fwd(lambda d, i: 1 <= d['age'][i]/3600 < 6 and d['liq'][i] >= 30000 and d['h1'][i] < -30))
rep('age1-6h liq>=30k h1>+50 (strong mom)', fwd(lambda d, i: 1 <= d['age'][i]/3600 < 6 and d['liq'][i] >= 30000 and d['h1'][i] > 50))
rep('age1-6h liq>=30k v1>=100k', fwd(lambda d, i: 1 <= d['age'][i]/3600 < 6 and d['liq'][i] >= 30000 and d['v1'][i] >= 100000))
rep('age>=6h liq>=40k h6>0', fwd(lambda d, i: d['age'][i]/3600 >= 6 and d['liq'][i] >= 40000 and d['h6'][i] > 0))
rep('age>=6h liq>=40k', fwd(lambda d, i: d['age'][i]/3600 >= 6 and d['liq'][i] >= 40000))
