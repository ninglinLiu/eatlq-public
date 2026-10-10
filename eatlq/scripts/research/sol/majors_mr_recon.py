"""Zero-cost recon #2: short-horizon MEAN-REVERSION on liquid Solana majors.

Motivation: LST recon proved Solana spot round-trip cost ~ 0 (0.004% measured,
0.1% ultra-conservative). With ~0 cost, even small but GENUINE mean-reversion is
tradeable. Majors oscillate ~0.5-2%/hour (>> cost), symmetric (not right-tail),
and hour-scale reversion is poll-speed friendly.

Test (free GeckoTerminal hourly OHLCV, USD is fine for an asset's own MR):
  - lag-1 autocorrelation of hourly returns (negative => mean-reverting).
  - simple rule: after the price drops >= D over L hours, buy; exit after H hours;
    EV vs cost. Report across many tokens for breadth (not a single fluke).
NOTE: this is DIRECTIONAL (you hold the asset on the long leg, exposed to its
drift); we report MR signal strength, not a finished market-neutral strategy.
"""
import json, time, urllib.request, statistics as st

GT = "https://api.geckoterminal.com/api/v2/networks/solana"
TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
    "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "RENDER": "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof",
    "JLP": "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4",
    "W": "85VBFQZC9TZkfaptBWjvUw7YbZjy52A6mjtPGjstQAmQ",
}


def get(url):
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}), timeout=25).read())
    except Exception as e:
        return {"__err__": str(e)[:120]}


def top_pool(mint):
    r = get(f"{GT}/tokens/{mint}/pools")
    best = None
    for p in r.get("data", []):
        rs = float(p["attributes"].get("reserve_in_usd") or 0)
        if best is None or rs > best[1]:
            best = (p["id"].split("solana_")[-1], rs)
    return best[0] if best else None


def closes(pool):
    o = get(f"{GT}/pools/{pool}/ohlcv/hour?aggregate=1&limit=1000")
    cl = (((o or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list")
    if not cl:
        return None
    rows = sorted(cl, key=lambda x: x[0])
    return [r[4] for r in rows if r[4]]


def analyze(name, c, cost=0.0010):
    n = len(c)
    rets = [c[i] / c[i - 1] - 1 for i in range(1, n)]
    m = sum(rets) / len(rets)
    # lag-1 autocorr
    num = sum((rets[i] - m) * (rets[i - 1] - m) for i in range(1, len(rets)))
    den = sum((x - m) ** 2 for x in rets)
    ac1 = num / den if den else 0
    # MR rule: after a >=D drop over last 1h, buy; hold H; pnl - cost
    D, H = 0.02, 3
    trades = []
    i = 1
    while i < n - H:
        if c[i] / c[i - 1] - 1 <= -D:
            trades.append(c[i + H] / c[i] * (1 - cost) - 1)
            i += H
        else:
            i += 1
    nt = len(trades)
    ev = sum(trades) / nt if nt else 0
    win = sum(1 for x in trades if x > 0) / nt if nt else 0
    # DRIFT CONTROL: unconditional H-hour forward return (gross, no cost). The MR
    # edge must be the EXCESS of dip-buy over this baseline, else it's just drift.
    uncond = [c[i + H] / c[i] - 1 for i in range(0, n - H)]
    base = sum(uncond) / len(uncond)
    excess = (ev + cost) - base  # ev includes -cost; compare gross dip-buy vs gross baseline
    print(f"{name:7s} hrs={n:4d} sd={100*st.pstdev(rets):.2f}% ac1={ac1:+.3f}{'MR ' if ac1<0 else 'mom'} "
          f"dipBuy n={nt:3d} EV={100*ev:+.3f}% win={100*win:3.0f}% | uncond{H}h={100*base:+.3f}% "
          f"EXCESS={100*excess:+.3f}%")
    return ac1, nt, excess


def main():
    print("=== Majors short-horizon MR recon (hourly; cost=0.10% rt conservative) ===")
    acs, evs = [], []
    for name, mint in TOKENS.items():
        pool = top_pool(mint); time.sleep(2.2)
        if not pool:
            print(f"{name}: no pool"); continue
        c = closes(pool); time.sleep(2.2)
        if not c or len(c) < 200:
            print(f"{name}: insufficient ({0 if not c else len(c)})"); continue
        ac1, nt, excess = analyze(name, c)
        acs.append(ac1)
        if nt >= 10:
            evs.append(excess)
    if acs:
        print(f"\nmean lag1-autocorr={sum(acs)/len(acs):+.3f} (neg=MR)  "
              f"dipBuy mean EXCESS-over-drift={100*sum(evs)/len(evs):+.3f}% over {len(evs)} tokens (this is the real MR edge)")


if __name__ == "__main__":
    main()
