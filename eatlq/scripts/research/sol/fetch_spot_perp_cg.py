#!/usr/bin/env python3
"""Fetch 8h SPOT price, PERP price, and PERP orderbook depth from Coinglass,
to turn the delta-neutral carry upper-bound into a real, friction-aware number."""
import json, glob, os, time, urllib.request, urllib.parse

KEY = os.environ.get("CG_KEY", "bd3e6ea0db034e1e9ccadbd821f98c3a")
B = "https://open-api-v4.coinglass.com/api"
FUND = "/opt/EATLQ/scripts/research/sol/_cache_fund_cg"
OUT = "/opt/EATLQ/scripts/research/sol/_cache_carry_cg"
os.makedirs(OUT, exist_ok=True)


def call(path, params):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{B}/{path}?{q}", headers={"CG-API-KEY": KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def spot_pair(exch, perp_pair):
    if exch == "OKX":
        return perp_pair.replace("-SWAP", "")      # SOL-USDT-SWAP -> SOL-USDT
    return perp_pair                                 # Binance SOLUSDT == spot


def ohlc(d):
    return [[int(x["time"]) // 1000, float(x["open"]), float(x["close"]),
             float(x.get("volume_usd", 0) or 0)] for x in d["data"]]


def main():
    for fn in sorted(glob.glob(f"{FUND}/*.json")):
        sym = os.path.basename(fn)[:-5]
        out = f"{OUT}/{sym}.json"
        if os.path.exists(out):
            print(f"{sym}: cached"); continue
        meta = json.load(open(fn)); exch, perp = meta["exchange"], meta["pair"]
        rec = {"exchange": exch, "perp": perp, "spot_pair": spot_pair(exch, perp)}
        try:
            ps = call("futures/price/history",
                      {"exchange": exch, "symbol": perp, "interval": "8h", "limit": 4500})
            time.sleep(2.2)
            sp = call("spot/price/history",
                      {"exchange": exch, "symbol": rec["spot_pair"], "interval": "8h", "limit": 4500})
            time.sleep(2.2)
            ob = call("futures/orderbook/ask-bids-history",
                      {"exchange": exch, "symbol": perp, "interval": "8h", "limit": 60})
            time.sleep(2.2)
        except Exception as e:
            print(f"{sym}: ERR {e}"); continue
        if ps.get("code") != "0" or not ps.get("data"):
            print(f"{sym}: perp price MISSING {ps.get('msg')}"); continue
        rec["perp_px"] = sorted(ohlc(ps))
        rec["spot_px"] = sorted(ohlc(sp)) if (sp.get("code") == "0" and sp.get("data")) else None
        if ob.get("code") == "0" and ob.get("data"):
            import statistics as stt
            bids = [float(x["bids_usd"]) for x in ob["data"]]
            asks = [float(x["asks_usd"]) for x in ob["data"]]
            rec["depth_bids_usd"] = stt.median(bids)
            rec["depth_asks_usd"] = stt.median(asks)
        json.dump(rec, open(out, "w"))
        sp_ok = "spot OK" if rec["spot_px"] else "NO SPOT"
        dep = rec.get("depth_bids_usd", 0)
        print(f"{sym}: perp n={len(rec['perp_px'])} {sp_ok} depth~${dep/1e6:.1f}M")


if __name__ == "__main__":
    main()
