#!/usr/bin/env python3
"""Fetch 8h OI (OHLC) and liquidation (long/short USD) history from Coinglass,
reusing the exchange/pair that already worked for funding."""
import json, glob, os, time, urllib.request, urllib.parse

KEY = os.environ.get("CG_KEY", "bd3e6ea0db034e1e9ccadbd821f98c3a")
B = "https://open-api-v4.coinglass.com/api"
FUND = "/opt/EATLQ/scripts/research/sol/_cache_fund_cg"
OI = "/opt/EATLQ/scripts/research/sol/_cache_oi_cg"
LIQ = "/opt/EATLQ/scripts/research/sol/_cache_liq_cg"
os.makedirs(OI, exist_ok=True); os.makedirs(LIQ, exist_ok=True)


def call(path, params):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{B}/{path}?{q}", headers={"CG-API-KEY": KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    for fn in sorted(glob.glob(f"{FUND}/*.json")):
        sym = os.path.basename(fn)[:-5]
        meta = json.load(open(fn))
        exch, pair = meta["exchange"], meta["pair"]
        # OI
        oif = f"{OI}/{sym}.json"
        if not os.path.exists(oif):
            d = call("futures/open-interest/history",
                     {"exchange": exch, "symbol": pair, "interval": "8h", "limit": 4500})
            time.sleep(2.2)
            if d.get("code") == "0" and d.get("data"):
                rows = [[int(x["time"]) // 1000, float(x["close"])] for x in d["data"]]
                json.dump({"exchange": exch, "pair": pair, "rows": sorted(rows)}, open(oif, "w"))
                print(f"{sym} OI: n={len(rows)}")
            else:
                print(f"{sym} OI MISSING: {d.get('msg')}")
        # Liquidation
        lqf = f"{LIQ}/{sym}.json"
        if not os.path.exists(lqf):
            d = call("futures/liquidation/history",
                     {"exchange": exch, "symbol": pair, "interval": "8h", "limit": 4500})
            time.sleep(2.2)
            if d.get("code") == "0" and d.get("data"):
                rows = [[int(x["time"]) // 1000,
                         float(x.get("long_liquidation_usd", 0) or 0),
                         float(x.get("short_liquidation_usd", 0) or 0)] for x in d["data"]]
                json.dump({"exchange": exch, "pair": pair, "rows": sorted(rows)}, open(lqf, "w"))
                print(f"{sym} LIQ: n={len(rows)}")
            else:
                print(f"{sym} LIQ MISSING: {d.get('msg')}")


if __name__ == "__main__":
    main()
