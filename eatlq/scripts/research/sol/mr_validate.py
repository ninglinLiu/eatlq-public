"""Validate the liquid-majors mean-reversion dip-buy against a MONTHLY 15% target.

The gate is NOT "is EV>0" but "can edge x frequency plausibly reach ~15%/month".

Outputs (per the brief):
  - per-coin avg triggers / month
  - net EV per trade (after cost)
  - win rate
  - avg loss / avg win (and payoff ratio)
  - max consecutive losses
  - monthly return distribution (coin-month samples + calendar months)
  - worst month
  - with stop-loss: still positive?
  - with daily trade cap: still positive?
  - portfolio sim on 500U: realistic monthly $ and %
  - drift control (excess over unconditional H-hold) so bull-beta isn't mistaken for alpha
  - OOS split (first 60% / last 40%)

Spot, long-only, hourly bars. Zero RPC / zero cost (cached GeckoTerminal data).
"""
import json, os, sys, glob, statistics as st
from datetime import datetime, timezone

CACHE = sys.argv[1] if len(sys.argv) > 1 else "/opt/EATLQ/scripts/research/sol/_cache_mr"
HOURS_PER_MONTH = 720
CAPITAL = 500.0
MIN_HOURS = 1500


def load():
    data = {}
    for fn in glob.glob(f"{CACHE}/*.json"):
        if fn.endswith("manifest.json"):
            continue
        sym = os.path.basename(fn).split("_")[0]
        d = json.load(open(fn))  # [ts,o,h,l,c,v]
        if len(d) >= MIN_HOURS:
            data[sym] = d
    return data


def month_key(ts):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt.year}-{dt.month:02d}"


def day_key(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def gen_trades(series, D, H, cost, stop=None):
    """Non-overlapping dip-buy trades. Entry at close of the dip bar; exit at
    +H bars or when intrabar low hits the stop. Returns list of dicts."""
    n = len(series)
    c = [r[4] for r in series]
    lo = [r[3] for r in series]
    ts = [r[0] for r in series]
    trades = []
    i = 1
    while i < n - 1:
        if c[i] / c[i - 1] - 1 <= -D:
            entry = c[i]
            end = min(i + H, n - 1)
            exit_px = c[end]
            exit_i = end
            if stop:
                stop_px = entry * (1 - stop)
                for j in range(i + 1, end + 1):
                    if lo[j] <= stop_px:
                        exit_px = stop_px
                        exit_i = j
                        break
            ret = (exit_px / entry) * (1 - cost) - 1
            trades.append({"ei": i, "ets": ts[i], "xts": ts[exit_i],
                           "ret": ret, "mult": (exit_px / entry) * (1 - cost)})
            i = exit_i + 1
        else:
            i += 1
    return trades


def max_consec_loss(rets):
    m = c = 0
    for r in rets:
        c = c + 1 if r <= 0 else 0
        m = max(m, c)
    return m


def coin_stats(sym, series, D, H, cost, stop=None):
    trs = gen_trades(series, D, H, cost, stop)
    rets = [t["ret"] for t in trs]
    months = max(1e-9, len(series) / HOURS_PER_MONTH)
    if not rets:
        return None
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    ev = sum(rets) / len(rets)
    # drift control: unconditional H-hold mean (gross)
    c = [r[4] for r in series]
    uncond = [c[k + H] / c[k] - 1 for k in range(0, len(c) - H)]
    base = sum(uncond) / len(uncond)
    excess = (ev + cost) - base
    # per-coin-month returns (single coin, full bankroll, compounded within month)
    bym = {}
    for t in trs:
        bym.setdefault(month_key(t["ets"]), []).append(t["ret"])
    cm_rets = []
    for mk, rs in bym.items():
        eq = 1.0
        for r in rs:
            eq *= (1 + r)
        cm_rets.append((mk, eq - 1, len(rs)))
    return {
        "sym": sym, "n": len(rets), "trig_pm": len(rets) / months, "ev": ev,
        "win": len(wins) / len(rets), "avg_win": (sum(wins) / len(wins)) if wins else 0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0,
        "payoff": (sum(wins) / len(wins)) / abs(sum(losses) / len(losses)) if wins and losses else 0,
        "mcl": max_consec_loss(rets), "sum": sum(rets), "excess": excess,
        "month_pm": len(rets) / months, "cm_rets": cm_rets,
    }


def portfolio_sim(data, D, H, cost, stop=None, daily_cap=None, M=4):
    """Single 500U bankroll, <=M concurrent positions, size=equity/M, compounding."""
    all_trades = []
    for sym, series in data.items():
        for t in gen_trades(series, D, H, cost, stop):
            all_trades.append((t["ets"], t["xts"], t["mult"], sym))
    all_trades.sort()
    cash = CAPITAL
    openp = []  # (xts, size, mult)
    day_count = {}
    eqlog = []
    taken = 0
    for ets, xts, mult, sym in all_trades:
        openp.sort()
        while openp and openp[0][0] <= ets:
            xt, size, mlt = openp.pop(0)
            cash += size * mlt
            eqlog.append((xt, cash + sum(s for _, s, _ in openp)))
        equity = cash + sum(s for _, s, _ in openp)
        if daily_cap is not None:
            dk = day_key(ets)
            if day_count.get(dk, 0) >= daily_cap:
                continue
        if len(openp) < M:
            size = min(equity / M, cash)
            if size > 5:
                cash -= size
                openp.append((xts, size, mult))
                taken += 1
                if daily_cap is not None:
                    day_count[dk] = day_count.get(dk, 0) + 1
    for xt, size, mlt in sorted(openp):
        cash += size * mlt
        eqlog.append((xt, cash))
    final = cash
    # span months
    tmin = min(t[0] for t in all_trades); tmax = max(t[1] for t in all_trades)
    months = (tmax - tmin) / (HOURS_PER_MONTH * 3600)
    total_ret = final / CAPITAL - 1
    monthly = (final / CAPITAL) ** (1 / months) - 1 if months > 0 else 0
    # calendar-month equity returns
    bym = {}
    for ts, eq in eqlog:
        bym.setdefault(month_key(ts), []).append(eq)
    cal = []
    prev = CAPITAL
    for mk in sorted(bym):
        end_eq = bym[mk][-1]
        cal.append((mk, end_eq / prev - 1))
        prev = end_eq
    return {"final": final, "total_ret": total_ret, "monthly": monthly,
            "months": months, "taken": taken, "cal": cal}


def report(data):
    D, H, cost = 0.02, 3, 0.001
    print(f"\n{'='*92}\nCONFIG: dip D={D*100:.0f}% entry, hold H={H}h, cost={cost*100:.2f}% round-trip, "
          f"capital={CAPITAL:.0f}U\nGATE: edge x frequency -> ~15%/month?\n{'='*92}")
    print(f"\n{'coin':9s}{'n':>4s}{'trig/mo':>8s}{'EV/tr':>8s}{'win%':>6s}"
          f"{'avgW':>7s}{'avgL':>7s}{'payoff':>7s}{'maxCL':>6s}{'excess':>8s}{'~mo%(EVxf)':>11s}")
    rows = []
    for sym, series in sorted(data.items()):
        s = coin_stats(sym, series, D, H, cost)
        if not s or s["n"] < 5:
            continue
        rows.append(s)
        approx_mo = s["trig_pm"] * s["ev"]
        print(f"{sym:9s}{s['n']:>4d}{s['trig_pm']:>8.1f}{100*s['ev']:>7.3f}%{100*s['win']:>6.0f}"
              f"{100*s['avg_win']:>6.2f}%{100*s['avg_loss']:>6.2f}%{s['payoff']:>7.2f}{s['mcl']:>6d}"
              f"{100*s['excess']:>7.3f}%{100*approx_mo:>10.1f}%")
    # coin-month distribution
    cm = [r for s in rows for r in [x[1] for x in s["cm_rets"]]]
    cm.sort()
    if cm:
        print(f"\nCOIN-MONTH RETURN DISTRIBUTION (n={len(cm)} coin-months, single-coin full-bankroll):")
        print(f"  mean={100*st.mean(cm):+.1f}%  median={100*st.median(cm):+.1f}%  "
              f"p10={100*cm[int(.1*len(cm))]:+.1f}%  p90={100*cm[int(.9*len(cm))]:+.1f}%  "
              f"worst={100*cm[0]:+.1f}%  %neg={100*sum(1 for x in cm if x<0)/len(cm):.0f}%  "
              f">=15%: {100*sum(1 for x in cm if x>=0.15)/len(cm):.0f}%")
    # portfolio sims
    def calsummary(cal):
        xs = [c[1] for c in cal]
        if not xs:
            return "no months"
        xs_s = sorted(xs)
        return (f"months={len(xs)} mean={100*st.mean(xs):+.1f}% median={100*st.median(xs):+.1f}% "
                f"worst={100*xs_s[0]:+.1f}% best={100*xs_s[-1]:+.1f}% %neg={100*sum(1 for x in xs if x<0)/len(xs):.0f}% "
                f">=15%:{100*sum(1 for x in xs if x>=0.15)/len(xs):.0f}%")
    print(f"\nPORTFOLIO SIM (500U, <=4 concurrent, size=eq/4, compounding):")
    for label, kw in [("base", {}), ("+stop 5%", {"stop": 0.05}),
                      ("+stop 8%", {"stop": 0.08}), ("+daily cap 3", {"daily_cap": 3}),
                      ("+stop8%+cap3", {"stop": 0.08, "daily_cap": 3})]:
        p = portfolio_sim(data, D, H, cost, **kw)
        print(f"  {label:14s} final={p['final']:8.1f}U total={100*p['total_ret']:+7.1f}% "
              f"monthlyCAGR={100*p['monthly']:+6.1f}% trades={p['taken']:4d} | {calsummary(p['cal'])}")
    # full monthly path of base
    pb = portfolio_sim(data, D, H, cost)
    print(f"  base monthly path: {[(c[0],round(100*c[1],1)) for c in pb['cal']]}")
    # market beta reference: SOL buy&hold monthly
    if "SOL" in data:
        sol = data["SOL"]; bym = {}
        for r in sol:
            bym.setdefault(month_key(r[0]), []).append(r[4])
        bh = []
        for mk in sorted(bym):
            v = bym[mk]; bh.append((mk, v[-1]/v[0]-1))
        print(f"  [ref] SOL buy&hold monthly: {[(c[0],round(100*c[1],1)) for c in bh]}")
    # cost sensitivity on portfolio monthly
    print(f"\nCOST SENSITIVITY (portfolio monthly%):")
    for cst in [0.0005, 0.001, 0.002]:
        p = portfolio_sim(data, D, H, cst)
        print(f"  cost={cst*100:.2f}%  monthly={100*p['monthly']:+.1f}%  final={p['final']:.1f}U")
    # OOS
    print(f"\nOOS (per-coin EV, first60% train / last40% test):")
    tr_ev, te_ev = [], []
    for sym, series in sorted(data.items()):
        k = int(len(series) * 0.6)
        a = coin_stats(sym, series[:k], D, H, cost)
        b = coin_stats(sym, series[k:], D, H, cost)
        if a and b and a["n"] >= 3 and b["n"] >= 3:
            tr_ev.append(a["ev"]); te_ev.append(b["ev"])
    if te_ev:
        print(f"  train mean EV={100*st.mean(tr_ev):+.3f}%  test mean EV={100*st.mean(te_ev):+.3f}%  "
              f"test %coins EV>0={100*sum(1 for x in te_ev if x>0)/len(te_ev):.0f}%  (n_coins={len(te_ev)})")


if __name__ == "__main__":
    report(load())
