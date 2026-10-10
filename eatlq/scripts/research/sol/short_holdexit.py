"""Re-test the froth SHORT with a NON-self-sabotaging exit.

The mirror diagnostic showed buy&hold short on rollover candidates = +1.53%/48h
(real froth mean-reversion), but the 15% hard stop + ATR trail bled it to +0.20%.
So re-test the short with:
  - TIME exit at H bars (no tight trailing), i.e. let the mean-reversion play out
  - a WIDE catastrophe stop (default 40%) only to bound ruin, not to whipsaw
  - short COLLECTS clipped 8h funding during the hold (real Coinglass data)
Protocol: TRAIN-locked froth bins -> judge on TEST. Full PF/top1/bootstrap/cat.
"""
from __future__ import annotations
import json, glob, os, bisect, random, statistics as st
from froth_long import load, features, atr, is_rollover, is_breakout

FUND = "/opt/EATLQ/scripts/research/sol/_cache_fund_cg"
RT_COST = 0.0010
CLIP = float(os.environ.get("FUND_CLIP", "0.02"))
H = int(os.environ.get("H", "48"))
WIDE_STOP = float(os.environ.get("WIDE_STOP", "0.40"))
SPLIT = 0.6
USE_FUNDING = os.environ.get("NOFUND", "0") != "1"


def load_fund():
    out = {}
    for fn in glob.glob(f"{FUND}/*.json"):
        out[os.path.basename(fn)[:-5]] = sorted(json.load(open(fn))["rows"])
    return out


def fund_sum(rows, t0, t1):
    return sum(max(-CLIP, min(CLIP, r)) for ts, r in rows if t0 < ts <= t1)


def sim_short_hold(series, entry_i, frows):
    n = len(series); entry = series[entry_i][1]
    if entry <= 0:
        return None
    end = min(entry_i + H, n)
    cat = 0
    ex_i = end - 1
    for j in range(entry_i, end):
        adverse = (series[j][2] - entry) / entry         # high vs short
        if adverse >= WIDE_STOP:
            cat = 1; ex_i = j; break
    if cat:
        price_ret = -WIDE_STOP
    else:
        price_ret = (entry - series[ex_i][4]) / entry
    fund = fund_sum(frows, series[entry_i][0], series[ex_i][0]) if (USE_FUNDING and frows) else 0.0
    ret = price_ret - RT_COST + fund
    mae = max((series[k][2] - entry) / entry for k in range(entry_i, ex_i + 1))
    return (ret, mae, cat, ex_i - entry_i + 1)


def collect(data, fund, funnel, require_fund):
    sigs = []; per = {}
    cov = {s: fund[s][0][0] for s in fund} if fund else {}
    for sym, series in data.items():
        feats = features(series); per[sym] = (series, feats)
        tmax, tmin = series[-1][0], series[0][0]; cut = tmin + SPLIT * (tmax - tmin)
        for f in feats:
            if not funnel(f):
                continue
            ei = f["t"] + 1
            if ei >= len(series):
                continue
            t = series[f["t"]][0]
            if require_fund and (sym not in fund or t < cov.get(sym, 1e18)):
                continue
            seg = "train" if t <= cut else "test"
            # froth features for scoring
            c = [r[4] for r in series]
            sigs.append({"sym": sym, "ei": ei, "seg": seg, "t": t,
                         "ext20": f.get("ext20", 0), "f": f})
    # froth score: reuse ret24 & pullback driven 5-feature? keep simple: use ret24, rsi via features? 
    # We use the same FK as froth: need ext20/ext60/rsi/volc -> recompute richly
    return sigs, per


# richer features (ext20/ext60/rsi/volc) for froth scoring
def rich_features(series):
    from froth_long import ema, rsi, atr as _atr
    c = [r[4] for r in series]; v = [r[5] for r in series]; h = [r[2] for r in series]
    e20, e60 = ema(c, 20), ema(c, 60); rs = rsi(c)
    out = []
    for t in range(len(series)):
        if t < 60:
            out.append(None); continue
        out.append({"ext20": (c[t]-e20[t])/e20[t], "ext60": (c[t]-e60[t])/e60[t],
                    "ret24": c[t]/c[t-24]-1, "rsi": rs[t],
                    "volc": v[t]/(sum(v[t-24:t])/24+1e-9),
                    "pullback": (max(h[t-24:t+1])-c[t])/max(h[t-24:t+1]), "t": t})
    return out


def run(data, fund, funnel, label, require_fund):
    sigs_meta = []
    per = {}
    cov = {s: fund[s][0][0] for s in fund} if fund else {}
    for sym, series in data.items():
        rf = rich_features(series); per[sym] = (series, rf)
        tmax, tmin = series[-1][0], series[0][0]; cut = tmin + SPLIT*(tmax-tmin)
        for f in rf:
            if not funnel(f):
                continue
            ei = f["t"]+1
            if ei >= len(series):
                continue
            t = series[f["t"]][0]
            if require_fund and (sym not in fund or t < cov.get(sym, 1e18)):
                continue
            sigs_meta.append({"sym": sym, "ei": ei, "seg": "train" if t <= cut else "test",
                              "t": t, "f": f})
    FK = ["ext20", "ext60", "ret24", "rsi", "volc"]
    tr = [s for s in sigs_meta if s["seg"] == "train"]
    thr = {k: (sorted(s["f"][k] for s in tr)[int(2/3*len(tr))] if tr else 0) for k in FK}
    out = []; last_exit = {}
    for s in sorted(sigs_meta, key=lambda x: (x["sym"], x["ei"])):
        sym = s["sym"]
        if s["ei"] <= last_exit.get(sym, -1):
            continue
        series, rf = per[sym]
        r = sim_short_hold(series, s["ei"], fund.get(sym) if fund else None)
        if not r:
            continue
        last_exit[sym] = s["ei"] + r[3]
        s["score"] = sum(1 for k in FK if s["f"][k] >= thr[k]); s["sim"] = r
        out.append(s)
    print(f"\n===== {label} | H={H}h wide_stop={WIDE_STOP} funding={'ON' if (USE_FUNDING and require_fund) else 'OFF'} =====")
    if not out:
        print("  no signals"); return
    def ev(g): return 100*sum(x["sim"][0] for x in g)/len(g)
    def cat(g): return 100*sum(x["sim"][2] for x in g)/len(g)
    allr = [x["sim"][0] for x in out]
    print(f"  ALL n={len(out)} EV={100*sum(allr)/len(allr):+.2f}% catastrophe={cat(out):.1f}%")
    for seg in ["train", "test"]:
        g = [x for x in out if x["seg"] == seg]
        if not g: continue
        bins = {}
        for x in g: bins.setdefault("low(0-1)" if x["score"] <= 1 else "high(2-3)", []).append(x)
        parts = " | ".join(f"{k}:n={len(v)} EV={ev(v):+.2f}% cat={cat(v):.0f}%" for k, v in sorted(bins.items()))
        print(f"  {seg.upper():5s}: {parts}")
    # robustness on TEST high froth & all test
    te = [x for x in out if x["seg"] == "test"]
    for name, g in [("TEST all", te), ("TEST froth>=2", [x for x in te if x["score"] >= 2])]:
        rets = sorted((x["sim"][0] for x in g), reverse=True)
        if not rets:
            print(f"  [{name}] none"); continue
        pos = sum(r for r in rets if r > 0); neg = -sum(r for r in rets if r < 0)
        pf = pos/neg if neg else 999; pf1 = (pos-rets[0])/neg if neg else 999
        rng = random.Random(7); m = sorted(sum(rets[rng.randrange(len(rets))] for _ in range(len(rets)))/len(rets) for _ in range(1500))
        print(f"  [{name}] n={len(rets)} EV={100*sum(rets)/len(rets):+.2f}% PF={pf:.2f} "
              f"PF_top1rm={pf1:.2f} boot_p5={100*m[int(0.05*len(m))]:+.2f}%")


def main():
    data = load(); fund = load_fund()
    print(f"#### SHORT with TIME-exit (not self-sabotaging). clip={CLIP} ####")
    # full-history, no funding (max sample) to see if the raw short edge is robust
    run(data, {}, is_rollover, "ROLLOVER short, FULL history, no funding", require_fund=False)
    # funded window, funding ON
    run(data, fund, is_rollover, "ROLLOVER short, funded window, funding ON", require_fund=True)


if __name__ == "__main__":
    main()
