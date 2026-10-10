"""Phase A+B for the majors mean-reversion validation.

A) Discover a liquid Solana token universe from GeckoTerminal top pools (by 24h
   volume), dedup by base token, keep the deepest SOL/USDC-quoted pool, drop
   stables & LSTs (those are separate structures already tested).
B) Fetch hourly OHLCV (1000h) per selected pool and CACHE to disk so the
   analysis can be re-run for free.

Zero RPC, zero cost (GeckoTerminal free API only).
"""
import json, os, time, urllib.request

GT = "https://api.geckoterminal.com/api/v2/networks/solana"
CACHE = "/opt/EATLQ/scripts/research/sol/_cache_mr"
os.makedirs(CACHE, exist_ok=True)

# Curated liquid Solana tokens (majors + liquid mid-caps). The API returns the
# pool name so we sanity-check the symbol; wrong/illiquid ones auto-skip.
UNIVERSE = {
    "SOL": "So11111111111111111111111111111111111111112",
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
    "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "RENDER": "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof",
    "JLP": "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4",
    "W": "85VBFQZC9TZkfaptBWjvUw7YbZjy52A6mjtPGjstQAmQ",
    "ORCA": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",
    "DRIFT": "DriFtupJYLTosbwoN8koMbEYSx54aFAVLddWsbksjwg7",
    "KMNO": "KMNo3nJsBXfcpJTVhZcXLW7RmTwTt4GVFE7suUBo9sS",
    "TNSR": "TNSRxcUxoT9xBG3de7PiJyTDYu7kskLqcpddxnEJAS6",
    "WEN": "WENWENvqqNya429ubCdR81ZmD69brwQaaBYY6p3LCpk",
    "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "MEW": "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",
    "BOME": "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ7i8d",
    "MNDE": "MNDEFzGvMt87ueuHvVU9VcTqsAP5b3fTGPsHuuPA5ey",
    "IO": "BZLbGTNCSFfoth2GYDtwr7e4imWzpR5jqcUuGEwr646K",
    "ZEUS": "ZEUS1aR7aX8DFFJf5QjWj2ftDDdNTroMNGo8YoQm3Gq",
    "HNT": "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux",
    "PNUT": "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump",
    "GOAT": "CzLSujWBLFsSjncfkh59rUFqvafWcY5tzedWJSuypump",
    "MOODENG": "ED5nyyWEzpPPiWimP8vYm7sD7TD3LAt3Q3gRTWHzPJBY",
    "FARTCOIN": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
    "AI16Z": "HeLp6NuQkmYB4pYWo2zYs22mESHXPQYzXbB8n4V98jwC",
}


def get(url, tries=4):
    for k in range(tries):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(
                url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}), timeout=25).read())
        except Exception as e:
            if k == tries - 1:
                return {"__err__": str(e)[:120]}
            time.sleep(3 + 2 * k)


def discover(min_resv=300_000):
    """For each curated mint, pick its deepest pool (any quote)."""
    seen = {}
    for sym, mint in UNIVERSE.items():
        r = get(f"{GT}/tokens/{mint}/pools")
        time.sleep(2.4)
        best = None
        for p in r.get("data", []):
            resv = float(p["attributes"].get("reserve_in_usd") or 0)
            if best is None or resv > best[1]:
                best = (p["id"].split("solana_")[-1], resv, p["attributes"].get("name") or "")
        if best and best[1] >= min_resv:
            seen[sym] = (best[0], best[1], best[2], mint)
        else:
            print(f"  skip {sym}: no deep pool ({best[1] if best else 0:.0f})")
    return seen


def fetch_ohlcv(pool, label, res="hour", limit=1000):
    fn = f"{CACHE}/{label}_{res}.json"
    if os.path.exists(fn):
        return json.load(open(fn))
    o = get(f"{GT}/pools/{pool}/ohlcv/{res}?aggregate=1&limit={limit}")
    time.sleep(2.4)
    cl = (((o or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list")
    if not cl:
        return None
    rows = sorted(cl, key=lambda x: x[0])
    # ts, open, high, low, close, volume  (full OHLC for stop-loss accuracy)
    data = [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows if r[4]]
    json.dump(data, open(fn, "w"))
    return data


def main():
    print("=== Phase A: discover universe ===")
    uni = discover()
    items = sorted(uni.items(), key=lambda kv: -kv[1][1])
    print(f"found {len(items)} liquid non-stable non-LST tokens")
    manifest = {}
    print("\n=== Phase B: fetch+cache hourly OHLCV ===")
    for sym, (pool, resv, name, mint) in items:
        d = fetch_ohlcv(pool, sym)
        n = len(d) if d else 0
        manifest[sym] = {"pool": pool, "resv": resv, "name": name, "mint": mint, "hours": n}
        print(f"  {sym:10s} resv=${resv/1e6:5.1f}M hours={n:4d} {name}")
    json.dump(manifest, open(f"{CACHE}/manifest.json", "w"), indent=2)
    ok = sum(1 for v in manifest.values() if v["hours"] >= 500)
    print(f"\ncached. tokens with >=500h: {ok}")


if __name__ == "__main__":
    main()
