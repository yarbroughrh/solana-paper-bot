import os; BT = os.environ.get('BT_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))  # local backtest data dir (gitignored)
import sys, copy, time; sys.path.insert(0, BT)
from engine import *
toks = load(); mints = sorted(toks, key=lambda m: toks[m]['t'][0])
cut = int(len(mints) * 0.6); train, test = mints[:cut], mints[cut:]
import datetime as dt
print('tokens', len(mints), 'train', len(train), 'test', len(test), 'split at', dt.datetime.utcfromtimestamp(toks[test[0]]['t'][0]))
for mode in ['continuous', 'hourly']:
    for name, ms in [('train', train), ('test', test)]:
        t0 = time.time(); df = run(BASE, toks, ms, mode)
        print(mode, name, summarize(df), f'{time.time()-t0:.0f}s')
        if len(df):
            for st, g in df.groupby('strat'): print('   ', st, summarize(g), g.reason.value_counts().to_dict())
