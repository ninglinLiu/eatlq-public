"""Validate DELTA-NEUTRAL funding harvest (long spot + short perp) vs 15%/month.

Edge = funding paid by longs to shorts (when rate>0). Market-neutral, non-right-tail,
speed-insensitive. We pull ~1.6y of OKX 8h funding history for a broad liquid
universe and test a cross-sectional top-K harvest with realistic costs & capital
efficiency.

Realism:
  - 3 funding settlements/day (8h). Short earns +rate when rate>0, pays when <0.
  - capital efficiency kappa = notional / total_capital. Conservative kappa=0.5
    (e.g. $250 spot + $250 short w/ $250 margin). Aggressive kappa=0.8 shown too.
  - round-trip cost RT applied on TURNOVER (both legs, in+out ~4 taker fills).
    base RT=0.30% of notional per full enter+exit.
  - strategy: each 8h hold top-K coins by *current* funding (only if >threshold);
    rebalance pays RT*kappa on the fraction of notional turned over.
Outputs the 15%-gate panel: monthly distribution, worst month, %>=15%, etc.
"""
import json, os, glob, time, urllib.request, statistics as st
from datetime import datetime, timezone

CACHE = "/opt/EATLQ/scripts/research/sol/_cache_fund"
os.makedirs(CACHE, exist_ok=True)

UNIV = ["BTC", "ETH", "SOL", "DOGE", "XRP", "BNB", "ADA", "AVAX", "LINK", "LTC",
        "JUP", "JTO", "WIF", "BONK", "RAY", "RENDER", "PYTH", "W", "ORCA", "HNT",
        "POPCAT", "PNUT", "MEW", "BOME", "DRIFT", "IO", "TNSR", "MOODENG",
        "FARTCOIN", "GOAT", "ZEUS", "KMNO", "ME", "PENGU", "ONDO", "ENA", "UNI",
        "PEPE", "SUI", "APT", "SEI", "TIA", "ARB", "OP", "INJ", "TRUMP", "PUMP"]
MAXREC = 1800  # ~1.6y of 8h


def get(url, tries=4):
    for k in range(tries):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20).read())
        except Exception as e:
            if k == tries - 1:
                return {"__err__": str(e)[:120]}
            time.sleep(1.2 + k)


def fetch(sym):
    fn = f"{CACHE}/{sym}.json"
    if os.path.exists(fn):
        return json.load(open(fn))
    inst = f"{sym}-USDT-SWAP"
    rows = {}
    after = ""
    while len(rows) < MAXREC:
        url = f"https://www.okx.com/api/v5/public/funding-rate-history?instId={inst}&limit=100"
        if after:
            url += f"&after={after}"
        r = get(url)
        time.sleep(0.16)
        d = r.get("data") if isinstance(r, dict) else None
        if not d:
            break
        for x in d:
            t = int(x["fundingTime"]) // 1000
            rows[t] = float(x["realizedRate"])
        after = d[-1]["fundingTime"]
        if len(d) < 100:
            break
    if not rows:
        return None
    out = [[t, rows[t]] for t in sorted(rows)]
    json.dump(out, open(fn, "w"))
    return out


def fetch_all():
    data = {}
    for sym in UNIV:
        d = fetch(sym)
        if d and len(d) >= 200:
            data[sym] = d
        print(f"  {sym:10s} periods={0 if not d else len(d):4d}")
    return data


def mkey(ts):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt.year}-{dt.month:02d}"


def per_coin_stats(data):
    print(f"\n{'coin':10s}{'periods':>8s}{'avg/8h':>9s}{'ann%':>7s}{'%pos':>6s}{'p90/8h':>9s}")
    rowsout = []
    for sym, d in sorted(data.items(), key=lambda kv: -st.mean([r[1] for r in kv[1]])):
        rs = [r[1] for r in d]
        avg = st.mean(rs)
        ann = avg * 3 * 365 * 100
        pos = sum(1 for r in rs if r > 0) / len(rs)
        p90 = sorted(rs)[int(.9 * len(rs))]
        rowsout.append((sym, avg, ann, pos))
        print(f"{sym:10s}{len(d):>8d}{100*avg:>8.4f}%{ann:>6.0f}%{100*pos:>5.0f}%{100*p90:>8.4f}%")
    return rowsout


def backtest(data, K=5, kappa=0.5, rt=0.0030, thr=0.0, capital=500.0):
    """Cross-sectional top-K funding harvest, delta-neutral, equal weight."""
    # align on union of funding times
    times = sorted(set(t for d in data.values() for t, _ in d))
    rate = {sym: {t: r for t, r in d} for sym, d in data.items()}
    held = set()
    eq = capital
    monthly = {}
    prev_month_eq = {}
    eqlog = []
    for t in times:
        avail = [(rate[s][t], s) for s in data if t in rate[s]]
        avail = [x for x in avail if x[0] > thr]
        avail.sort(reverse=True)
        target = set(s for _, s in avail[:K])
        if not target and not held:
            eqlog.append((t, eq)); continue
        # turnover cost: positions entered or exited
        turn = len(target ^ held)  # symmetric diff = legs changed
        # each changed position costs rt on its notional share; notional/cap=kappa, /K per slot
        cost = eq * kappa * rt * (turn / max(K, 1))
        eq -= cost
        # funding earned this period on held target (short earns +rate)
        if target:
            per_slot = eq * kappa / len(target)
            earn = sum(per_slot * rate[s][t] for s in target)
            eq += earn
        held = target
        eqlog.append((t, eq))
    # monthly returns from eqlog
    bym = {}
    for t, e in eqlog:
        bym.setdefault(mkey(t), []).append(e)
    cal = []
    prev = capital
    for mk in sorted(bym):
        end = bym[mk][-1]
        cal.append((mk, end / prev - 1))
        prev = end
    final = eqlog[-1][1]
    months = len(cal)
    cagr = (final / capital) ** (1 / months) - 1 if months else 0
    return {"final": final, "cagr_m": cagr, "cal": cal, "months": months}


def summary(cal):
    xs = sorted(c[1] for c in cal)
    if not xs:
        return "none"
    return (f"mean={100*st.mean(xs):+.1f}% median={100*st.median(xs):+.1f}% worst={100*xs[0]:+.1f}% "
            f"best={100*xs[-1]:+.1f}% %neg={100*sum(1 for x in xs if x<0)/len(xs):.0f}% "
            f">=15%:{100*sum(1 for x in xs if x>=0.15)/len(xs):.0f}%")


def main():
    print("=== fetch funding history ===")
    data = fetch_all()
    print(f"\nuniverse with data: {len(data)}")
    per_coin_stats(data)
    print(f"\n=== DELTA-NEUTRAL TOP-K HARVEST (500U), gate=15%/mo ===")
    for K, kappa, rt, thr in [(5, 0.5, 0.0030, 0.0), (5, 0.8, 0.0030, 0.0),
                              (3, 0.5, 0.0030, 0.0002), (10, 0.5, 0.0030, 0.0),
                              (5, 0.5, 0.0015, 0.0002), (5, 0.5, 0.0030, 0.0005)]:
        b = backtest(data, K=K, kappa=kappa, rt=rt, thr=thr)
        print(f"K={K} kappa={kappa} rt={rt*100:.2f}% thr={thr*100:.3f}%/8h -> "
              f"final={b['final']:7.1f}U monthlyCAGR={100*b['cagr_m']:+.1f}% mo={b['months']} | {summary(b['cal'])}")
    b = backtest(data, K=5, kappa=0.5, rt=0.0030, thr=0.0002)
    print(f"\nbase(K5,kappa.5,rt.3%,thr.02%) monthly path:")
    print("  " + str([(c[0], round(100 * c[1], 1)) for c in b["cal"]]))


if __name__ == "__main__":
    main()
