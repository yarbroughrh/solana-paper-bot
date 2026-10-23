import os; BT = os.environ.get('BT_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))  # local backtest data dir (gitignored)
import sys, copy; sys.path.insert(0, BT)
from engine import *
toks = load(); mints = sorted(toks, key=lambda m: toks[m]['t'][0]); cut = int(len(mints)*0.6); train, test = mints[:cut], mints[cut:]
def P(**kw):
    p = copy.deepcopy(BASE); p['ENABLED'] = {'S1': True}; p.update(HARD_STOP=-0.25, TP1=0.20, TP1_FRAC=1.0, TRAIL=0.15); p['TIME_STOP_H'] = {'S1': 2}
    p['S1'].update(kw); return p
for nm, kw in [('R1 exits only', {}), ('br1.4 amax1', dict(br5=1.4, br1=1.4)), ('br1.4 amax3', dict(br5=1.4, br1=1.4, age_max_h=3.0)), ('br1.3 amax1', dict(br5=1.3, br1=1.3))]:
    for mode in ['continuous', '5min', '15min', 'hourly']:
        for sn, ms in [('train', train), ('test', test)]:
            print(nm, '|', mode, sn, {k: round(float(v), 2) for k, v in summarize(run(P(**kw), toks, ms, mode)).items()}, flush=True)
