import os; BT = os.environ.get('BT_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))  # local backtest data dir (gitignored)
import pandas as pd, numpy as np
s = pd.read_parquet(os.path.join(BT, 'hf', 'postgard_snapshots.parquet'))
s = s[(s.dex_id == 'pumpswap') & (s.price_usd > 0)].copy()
s['t'] = s.snapshot_time.astype('int64') // 10**6  # epoch s
s = s.sort_values(['mint', 't']).drop_duplicates(['mint', 't'])
cols = ['mint','t','seconds_since_graduation','price_usd','price_change_5m','price_change_1h','price_change_6h','price_change_24h',
        'market_cap_usd','liquidity_usd','volume_5m','volume_1h','volume_6h','volume_24h','buys_5m','sells_5m','buys_1h','sells_1h']
s = s[cols].rename(columns={'seconds_since_graduation':'age','price_usd':'px','price_change_5m':'m5','price_change_1h':'h1',
     'price_change_6h':'h6','price_change_24h':'h24','market_cap_usd':'mcap','liquidity_usd':'liq','volume_5m':'v5','volume_1h':'v1',
     'volume_6h':'v6','volume_24h':'v24','buys_5m':'b5','sells_5m':'s5','buys_1h':'b1','sells_1h':'s1'})
s.to_parquet(os.path.join(BT, 'snap.parquet'))
print(len(s), s.mint.nunique()); print(s.describe().T[['count','mean','50%']])
g = s.groupby('mint').agg(first_age=('age','min'), n=('t','size'), t0=('t','min'))
print(g.describe())
print('NaN m5 share', s.m5.isna().mean(), 'h1', s.h1.isna().mean())
