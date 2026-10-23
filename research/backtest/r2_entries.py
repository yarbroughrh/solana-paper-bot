import os; BT = os.environ.get('BT_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))  # local backtest data dir (gitignored)
import sys, copy, itertools; sys.path.insert(0, BT)
from engine import *
toks = load(); mints = sorted(toks, key=lambda m: toks[m]['t'][0]); cut = int(len(mints)*0.6); train, test = mints[:cut], mints[cut:]
def P(**kw):
    p = copy.deepcopy(BASE); p['ENABLED'] = {'S1': True}; p.update(HARD_STOP=-0.25, TP1=0.20, TP1_FRAC=1.0, TRAIL=0.15); p['TIME_STOP_H'] = {'S1': 2}
    p['S1'].update(kw); return p
rows = []
for amin, amax, liq, v1, br in itertools.product([10/60, 0.5], [1.0, 3.0], [20000, 40000], [40000, 100000], [1.15, 1.4]):
    if amin >= amax: continue
    p = P(age_min_h=amin, age_max_h=amax, liq=liq, v1=v1, br5=br, br1=br)
    for mode in ['continuous', '5min']:
        s = summarize(run(p, toks, train, mode))
        rows.append(dict(amin=round(amin, 2), amax=amax, liq=liq, v1=v1, br=br, mode=mode, **{k: float(v) for k, v in s.items()}))
df = pd.DataFrame(rows); df.to_csv(os.path.join(BT, 'r2_train.csv'), index=False)
w = df.pivot_table(index=['amin', 'amax', 'liq', 'v1', 'br'], columns='mode', values=['n', 'avg%'])
w['minavg'] = w[('avg%', 'continuous')].combine(w[('avg%', '5min')], min)
print(w.sort_values('minavg', ascending=False).head(10).to_string())
