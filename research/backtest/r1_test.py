import os; BT = os.environ.get('BT_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))  # local backtest data dir (gitignored)
import sys, copy; sys.path.insert(0, BT)
from engine import *
toks = load(); mints = sorted(toks, key=lambda m: toks[m]['t'][0]); cut = int(len(mints)*0.6); train, test = mints[:cut], mints[cut:]
C = {'S1': [(-0.25, .3, 1, .15, 2), (-0.25, .2, 1, .15, 2), (-0.08, .25, .5, .15, 2)],
     'S2': [(-0.15, .5, 1, .15, 2), (-0.08, .3, 1, .15, 2), (-0.08, .25, .5, .15, 12)],
     'S3': [(-0.08, .1, .5, .15, 2), (-0.40, .1, .5, .15, 2), (-0.08, .25, .5, .15, 12)]}
for s, L in C.items():
    for hs, tp, tf, tr, tm in L:
        p = copy.deepcopy(BASE); p['ENABLED'] = {s: True}; p.update(HARD_STOP=hs, TP1=tp, TP1_FRAC=tf, TRAIL=tr); p['TIME_STOP_H'] = {s: tm}
        for mode in ['continuous', '5min', 'hourly']:
            for nm, ms in [('train', train), ('test', test)]:
                print(s, (hs, tp, tf, tr, tm), mode, nm, {k: round(float(v), 2) for k, v in summarize(run(p, toks, ms, mode)).items()}, flush=True)
