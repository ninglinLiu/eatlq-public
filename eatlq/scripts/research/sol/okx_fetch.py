"""Fetch long (multi-month / multi-year) 1H history from OKX (free, no key).
Binance is 451 geo-blocked, Bybit 403; OKX works. This unblocks the monthly
distribution / worst-month / regime analysis that 41 days of GeckoTerminal can't.

Cache to _cache_okx/{SYM}_1h.json as [ts_s, o,h,l,c,v] (same shape mr_validate uses).
"""
import json, os, time, urllib.request

CACHE = "/opt/EATLQ/scripts/research/sol/_cache_okx"
os.makedirs(CACHE, exist_ok=True)

SYMS = ["SOL", "JUP", "JTO", "WIF", "BONK", "RAY", "RENDER", "PYTH", "W", "ORCA",
        "HNT", "POPCAT", "PNUT", "MEW", "BOME", "DRIFT", "IO", "TNSR", "MOODENG",
        "FARTCOIN", "GOAT", "ZEUS", "KMNO", "MNDE", "AI16Z", "JLP", "ME", "PENGU",
        "ONDO", "ENA", "AAVE", "LDO", "UNI", "LINK"]

MAX_CANDLES = 14000  # ~1.6y of 1H


def get(url, tries=4):
    for k in range(tries):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20).read())
        except Exception as e:
            if k == tries - 1:
                return {"__err__": str(e)[:120]}
            time.sleep(1.5 + k)


def fetch(sym):
    fn = f"{CACHE}/{sym}_1h.json"
    if os.path.exists(fn):
        return json.load(open(fn))
    inst = f"{sym}-USDT"
    rows = {}
    after = ""  # empty -> latest
    while len(rows) < MAX_CANDLES:
        url = f"https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar=1H&limit=100"
        if after:
            url += f"&after={after}"
        r = get(url)
        time.sleep(0.18)
        data = r.get("data") if isinstance(r, dict) else None
        if not data:
            break
        for c in data:
            ts = int(c[0]) // 1000
            rows[ts] = [ts, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])]
        after = data[-1][0]  # oldest ts in this page (ms), page older next
        if len(data) < 100:
            break
    if not rows:
        return None
    out = [rows[t] for t in sorted(rows)]
    json.dump(out, open(fn, "w"))
    return out


def main():
    man = {}
    for sym in SYMS:
        d = fetch(sym)
        n = len(d) if d else 0
        days = (d[-1][0] - d[0][0]) / 86400 if d else 0
        man[sym] = {"hours": n, "days": round(days, 1)}
        print(f"  {sym:10s} candles={n:5d} span={days:6.1f}d {'OK' if n>=2000 else 'thin/none'}")
    json.dump(man, open(f"{CACHE}/manifest.json", "w"), indent=2)
    ok = sum(1 for v in man.values() if v["hours"] >= 2000)
    print(f"\ncached. tokens with >=2000h(~83d): {ok}")


if __name__ == "__main__":
    main()
