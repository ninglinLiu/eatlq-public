#!/usr/bin/env python3
"""Zero-cost recon for the LST peg-reversion structure (speed-insensitive, bounded,
value-anchored — the anti-thesis of the pump.fun right-tail).

Market ratio (LST priced in SOL) from GeckoTerminal OHLCV currency=token (verified
to equal the on-chain ratio, matches Jupiter live quote). Fair value grows at the
staking APY ~ a smooth near-linear upward drift; we remove it with a robust linear
fit so the residual = the tradeable premium/discount oscillation.

We report, per LST:
  - carry: annualized drift of the ratio (= staking yield earned just holding LST vs SOL)
  - premium oscillation: std, p5..p95 range, % of hours the premium is below its own
    rolling mean by > a threshold (capturable dips), mean-reversion half-life
  - a simple MR backtest: buy when premium z < -Z, exit when z >= 0, SOL-denominated
    PnL with a CONSERVATIVE round-trip cost (default 0.1%, ~25x the measured 0.004%).
"""
from __future__ import annotations
import json
import math
import time
import urllib.request

LST = {
    "mSOL": ("mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So", None),
    "JitoSOL": ("J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn", "Hp53XEtt4S8SvPCXarsLSdGfZBuUr5mMmZmX2DRNXQKp"),
    "bSOL": ("bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1", None),
    "INF": ("5oVNBeEEQvYi1cX3ir8Dx5n1P7pdxydbGF2X4TxVusJm", None),
    "jupSOL": ("jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v", None),
}
GT = "https://api.geckoterminal.com/api/v2/networks/solana"


def get(url):
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}), timeout=25).read())
    except Exception as e:
        return {"__err__": str(e)[:140]}


def best_sol_pool(mint):
    r = get(f"{GT}/tokens/{mint}/pools")
    best = None
    for p in r.get("data", []):
        nm = (p["attributes"].get("name") or "")
        if "/ SOL" in nm or nm.endswith("SOL"):
            rs = float(p["attributes"].get("reserve_in_usd") or 0)
            pid = p["id"].split("solana_")[-1]
            if best is None or rs > best[1]:
                best = (pid, rs, nm)
    return best


def ohlcv_ratio(pool):
    o = get(f"{GT}/pools/{pool}/ohlcv/hour?aggregate=1&limit=1000&currency=token")
    cl = (((o or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list")
    if not cl:
        return None
    rows = sorted(cl, key=lambda x: x[0])
    return [(r[0], r[4]) for r in rows if r[4]]  # (ts, close ratio)


def linfit(ts, ys):
    n = len(ts)
    t0 = ts[0]
    xs = [(t - t0) / 3600.0 for t in ts]  # hours
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx if sxx else 0
    a = my - b * mx
    return a, b, xs


def analyze(name, series, cost=0.001):
    ts = [t for t, _ in series]
    r = [v for _, v in series]
    n = len(r)
    a, b, xs = linfit(ts, r)  # r ~ a + b*hour ; b = fair drift per hour
    trend = [a + b * x for x in xs]
    prem = [r[i] / trend[i] - 1 for i in range(n)]  # relative premium/discount
    apy = (b / a) * 24 * 365 * 100 if a else 0  # annualized carry from holding LST vs SOL
    mp = sum(prem) / n
    sd = (sum((p - mp) ** 2 for p in prem) / n) ** 0.5
    sp = sorted(prem)
    p5, p95 = sp[int(0.05 * n)], sp[int(0.95 * n)]
    # mean-reversion half-life via AR(1) on premium: p_t - mp = rho*(p_{t-1}-mp)
    num = sum((prem[i] - mp) * (prem[i - 1] - mp) for i in range(1, n))
    den = sum((prem[i - 1] - mp) ** 2 for i in range(1, n))
    rho = num / den if den else 0
    hl = (math.log(0.5) / math.log(abs(rho))) if 0 < rho < 1 else float("inf")
    # simple MR backtest: enter when z<-Z, exit when z>=0 (z on rolling mean/std, window W)
    W, Z = 48, 1.0
    pos = None
    rets = []
    for i in range(W, n):
        wins = prem[i - W:i]
        m = sum(wins) / W
        s = (sum((x - m) ** 2 for x in wins) / W) ** 0.5 or 1e-9
        z = (prem[i] - m) / s
        if pos is None and z < -Z:
            pos = r[i]  # buy ratio (SOL per LST paid)
        elif pos is not None and z >= 0:
            rets.append(r[i] / pos * (1 - cost) - 1)  # SOL gained (incl carry over hold)
            pos = None
    nt = len(rets)
    ev = sum(rets) / nt if nt else 0
    win = sum(1 for x in rets if x > 0) / nt if nt else 0
    print(f"{name:8s} hrs={n:4d} carry_APY={apy:5.1f}%  prem mean={100*mp:+.2f}% sd={100*sd:.2f}% "
          f"[p5..p95]=[{100*p5:+.2f}..{100*p95:+.2f}]%  HL={hl:4.1f}h  "
          f"MR(z<-1):trades={nt:3d} EV/trade={100*ev:+.3f}% win={100*win:3.0f}% sumPnL={100*sum(rets):+.1f}%")


def main():
    print("=== LST peg-reversion recon (hourly, currency=token; cost=0.10% round-trip conservative) ===")
    for name, (mint, pool) in LST.items():
        if not pool:
            bp = best_sol_pool(mint)
            time.sleep(2.2)
            pool = bp[0] if bp else None
        if not pool:
            print(f"{name}: no SOL pool"); continue
        series = ohlcv_ratio(pool)
        time.sleep(2.2)
        if not series or len(series) < 100:
            print(f"{name}: insufficient OHLCV ({0 if not series else len(series)})"); continue
        analyze(name, series)


if __name__ == "__main__":
    main()
