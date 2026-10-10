#!/usr/bin/env python3
"""Fetch 8h funding history from Coinglass for the OKX-cached majors. Cache to JSON.
8h is the NATIVE settlement granularity (funding_rate_interval=8), so this is the
real funding a long pays/receives -- not interpolation."""
import json, glob, os, time, urllib.request, urllib.parse

KEY = os.environ.get("CG_KEY", "bd3e6ea0db034e1e9ccadbd821f98c3a")
BASE = "https://open-api-v4.coinglass.com/api/futures/funding-rate/history"
OUT = "/opt/EATLQ/scripts/research/sol/_cache_fund_cg"
os.makedirs(OUT, exist_ok=True)

BINANCE_MAP = {"BONK": "1000BONK"}          # Binance uses 1000-prefixed
OKX_ONLY = {"ZEUS": "ZEUS-USDT-SWAP", "RAY": "RAY-USDT-SWAP"}


def syms():
    s = set()
    for fn in glob.glob("/opt/EATLQ/scripts/research/sol/_cache_okx/*.json"):
        if "manifest" in fn:
            continue
        s.add(os.path.basename(fn).split("_")[0])
    return sorted(s)


def get(exchange, pair):
    q = urllib.parse.urlencode({"exchange": exchange, "symbol": pair,
                                "interval": "8h", "limit": 4500})
    req = urllib.request.Request(f"{BASE}?{q}", headers={"CG-API-KEY": KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    if d.get("code") != "0" or not d.get("data"):
        return None, d.get("msg", "?")
    # normalize -> [[time_s, funding_close], ...]
    rows = [[int(x["time"]) // 1000, float(x["close"])] for x in d["data"]]
    rows.sort()
    return rows, None


def main():
    for s in syms():
        out = f"{OUT}/{s}.json"
        if os.path.exists(out):
            print(f"{s}: cached"); continue
        rows = err = None
        # try OKX swap first (matches the candle source), then Binance
        for exch, pair in [("OKX", f"{s}-USDT-SWAP"),
                           ("Binance", BINANCE_MAP.get(s, f"{s}USDT")),
                           ("OKX", OKX_ONLY.get(s, ""))]:
            if not pair:
                continue
            rows, err = get(exch, pair)
            time.sleep(2.2)            # stay under 30/min
            if rows:
                json.dump({"exchange": exch, "pair": pair, "rows": rows}, open(out, "w"))
                from datetime import datetime, timezone
                lo = datetime.fromtimestamp(rows[0][0], timezone.utc).date()
                hi = datetime.fromtimestamp(rows[-1][0], timezone.utc).date()
                print(f"{s}: {exch}:{pair} n={len(rows)} {lo}..{hi}")
                break
        if not rows:
            print(f"{s}: MISSING ({err})")


if __name__ == "__main__":
    main()
