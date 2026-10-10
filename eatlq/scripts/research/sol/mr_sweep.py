"""Hypothesis sweep on 1.6y OKX hourly data, judged by the 15%/month bar.

Tests, per coin and pooled:
  - signal = dip-buy (mean-reversion) vs breakout-buy (momentum)
  - horizons H, thresholds D
Metric that matters: drift-controlled EXCESS per trade, win rate, payoff, and
median coin-month return (single-coin full-bankroll). We want to know if ANY
config gives a positive, persistent, sizeable edge -> else liquid spot hourly
timing is efficient and 15%/mo is unreachable this way.
"""
import json, os, glob, statistics as st
from datetime import datetime, timezone

CACHE = "/opt/EATLQ/scripts/research/sol/_cache_okx"
HPM = 720


def load():
    data = {}
    for fn in glob.glob(f"{CACHE}/*_1h.json"):
        sym = os.path.basename(fn).split("_")[0]
        d = json.load(open(fn))
        if len(d) >= 1500:
            data[sym] = d
    return data


def mkey(ts):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt.year}-{dt.month:02d}"


def trades(series, D, H, cost, side):
    n = len(series); c = [r[4] for r in series]; ts = [r[0] for r in series]
    out = []; i = 1
    while i < n - 1:
        mv = c[i] / c[i - 1] - 1
        sig = (mv <= -D) if side == "dip" else (mv >= D)
        if sig:
            end = min(i + H, n - 1)
            out.append((ts[i], (c[end] / c[i]) * (1 - cost) - 1))
            i = end + 1
        else:
            i += 1
    return out


def coinmonths(trs):
    bym = {}
    for t, r in trs:
        bym.setdefault(mkey(t), []).append(r)
    res = []
    for mk, rs in bym.items():
        eq = 1.0
        for r in rs:
            eq *= (1 + r)
        res.append(eq - 1)
    return res


def evalcfg(data, D, H, cost, side):
    allr = []; cms = []; excesses = []
    for sym, s in data.items():
        trs = trades(s, D, H, cost, side)
        if len(trs) < 20:
            continue
        rets = [r for _, r in trs]
        allr += rets
        cms += coinmonths(trs)
        c = [r[4] for r in s]
        uncond = sum(c[k + H] / c[k] - 1 for k in range(len(c) - H)) / (len(c) - H)
        excesses.append(sum(rets) / len(rets) + cost - uncond)
    if not allr:
        return None
    win = sum(1 for r in allr if r > 0) / len(allr)
    cms.sort()
    return {"n": len(allr), "ev": sum(allr) / len(allr), "win": win,
            "excess": st.mean(excesses), "cm_med": st.median(cms),
            "cm_mean": st.mean(cms), "cm_pneg": sum(1 for x in cms if x < 0) / len(cms),
            "cm_p90": cms[int(.9 * len(cms))]}


def main():
    data = load()
    print(f"universe={len(data)} coins, ~1.6y hourly\n")
    print(f"{'side':9s}{'D':>5s}{'H':>4s}{'n':>6s}{'EV/tr':>8s}{'win%':>6s}{'excess':>8s}"
          f"{'cmMed':>8s}{'cmMean':>8s}{'cm%neg':>7s}{'cmP90':>7s}")
    for side in ["dip", "breakout"]:
        for D in [0.01, 0.02, 0.03, 0.05]:
            for H in [1, 3, 6, 12, 24]:
                r = evalcfg(data, D, H, 0.001, side)
                if not r:
                    continue
                print(f"{side:9s}{100*D:>4.0f}%{H:>4d}{r['n']:>6d}{100*r['ev']:>7.3f}%"
                      f"{100*r['win']:>6.0f}{100*r['excess']:>7.3f}%{100*r['cm_med']:>7.1f}%"
                      f"{100*r['cm_mean']:>7.1f}%{100*r['cm_pneg']:>6.0f}%{100*r['cm_p90']:>6.1f}%")
        print()


if __name__ == "__main__":
    main()
