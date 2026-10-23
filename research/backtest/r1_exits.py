import os; BT = os.environ.get('BT_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))  # local backtest data dir (gitignored)
import sys, copy, itertools, json; sys.path.insert(0, BT)
from engine import *
toks = load(); mints = sorted(toks, key=lambda m: toks[m]['t'][0]); cut = int(len(mints)*0.6); train, test = mints[:cut], mints[cut:]
rows = []
for strat in ['S1', 'S2', 'S3']:
    for hs, tp, tf, tr, tm in itertools.product([-0.08, -0.15, -0.25, -0.40], [0.10, 0.20, 0.30, 0.50], [0.5, 1.0], [0.15, 0.30], [0.25, 0.5, 1, 2]):
        if tf == 1.0 and tr == 0.30: continue  # trail irrelevant when TP sells all
        p = copy.deepcopy(BASE); p['ENABLED'] = {strat: True}
        p.update(HARD_STOP=hs, TP1=tp, TP1_FRAC=tf, TRAIL=tr); p['TIME_STOP_H'] = {strat: tm}
        s = summarize(run(p, toks, train, 'continuous'))
        rows.append({'strat': strat, 'stop': hs, 'tp': tp, 'tpfrac': tf, 'trail': tr, 'time_h': tm, **{k: float(v) for k, v in s.items()}})
df = pd.DataFrame(rows); df.to_csv(os.path.join(BT, 'r1_exits_train.csv'), index=False)
for strat, g in df.groupby('strat'):
    print(strat, 'TRAIN top 8 by avg%'); print(g.sort_values('avg%', ascending=False).head(8).to_string(index=False))
    print(strat, 'marginal avg% by stop', g.groupby('stop')['avg%'].mean().round(2).to_dict(), 'by tp', g.groupby('tp')['avg%'].mean().round(2).to_dict(),
          'by time', g.groupby('time_h')['avg%'].mean().round(2).to_dict(), 'by tpfrac', g.groupby('tpfrac')['avg%'].mean().round(2).to_dict())
