#!/usr/bin/env python3
"""Forward collector (read-only, public GeckoTerminal API): records every new graduated pump.fun pool
(PumpSwap etc., mint ending 'pump') as it appears, so later OHLCV replays include the ones that die (no survivorship)."""
import json, os, time, requests
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "new_pools_seen.jsonl")
seen = set()
if os.path.exists(OUT):
    for l in open(OUT):
        try: seen.add(json.loads(l)["pool"])
        except Exception: pass
S = requests.Session(); S.headers["accept"] = "application/json"
while True:
    for page in (1, 2, 3):
        try:
            r = S.get(f"https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page={page}", timeout=(5, 15))
            if r.status_code != 200: time.sleep(10); continue
            for p in r.json().get("data", []):
                a = p["attributes"]; dex = p["relationships"]["dex"]["data"]["id"]
                base = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
                if dex == "pump-fun" or not base.endswith("pump") or a["address"] in seen: continue
                seen.add(a["address"])
                rec = {"pool": a["address"], "mint": base, "dex": dex, "name": a.get("name"), "created": a.get("pool_created_at"),
                       "first_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "liq": a.get("reserve_in_usd"), "fdv": a.get("fdv_usd")}
                with open(OUT, "a") as f: f.write(json.dumps(rec) + "\n")
        except Exception: pass
        time.sleep(3)
    time.sleep(50)
