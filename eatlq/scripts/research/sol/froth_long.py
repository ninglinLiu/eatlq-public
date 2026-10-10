#!/usr/bin/env python3
"""FROTH LONG — the mirror test the user demanded.

If shorting froth is INVERTED (more froth -> worse short EV, froth_short.py),
then the OTHER side of those trades was winning. This tests the LONG directly,
on the SAME OKX 1H data, SAME froth scoring, SAME train/test time split.

TWO funnels:
  (R) ROLLOVER long  = the literal mirror of the short candidates
        (ret24>=+20% AND pullback>=3% off 24h-high) -> go LONG instead of short.
  (B) BREAKOUT long  = pure strength (ret24>=+20% AND within 1% of 24h-high,
        i.e. NOT rolling over) -> momentum continuation.

LONG exit geometry (mirror of the short):
  - hard stop: adverse DROP >= 15% (price falls 15%) => CATASTROPHE
  - once favorable >=12% (price up 12%): trail 1.5*ATR off the running high
  - 48h cap.
COST: taker 0.05%/side = 0.10% round trip. Non-overlapping per coin.

IMPORTANT HONESTY: funding is OMITTED here. For a LONG this is OPTIMISTIC
(longs PAY positive funding, worst exactly in froth). So gross-positive here is
NECESSARY but NOT sufficient; real Coinglass hourly funding gets subtracted next.
"""
from __future__ import annotations
import json
import glob
import os
import random
import statistics as st

CACHE = "/opt/EATLQ/scripts/research/sol/_cache_okx"
MIN_HOURS = 2000
TAKER = 0.0005
RT_COST = 2 * TAKER
RUNUP_MIN = 0.20
PULLBACK_MIN = 0.03      # rollover funnel: >=3% off high
NEARHIGH_MAX = 0.01      # breakout funnel: <=1% off high
HARD_STOP = 0.15         # adverse -15% => catastrophe (long)
TRAIL_ARM = 0.12
TRAIL_ATR = 1.5
TIME_BARS = 48


def load():
    data = {}
    for fn in glob.glob(f"{CACHE}/*.json"):
        if fn.endswith("manifest.json"):
            continue
        sym = os.path.basename(fn).split("_")[0]
        d = json.load(open(fn))
        if len(d) >= MIN_HOURS:
            data[sym] = d
    return data


def ema(series, span):
    k = 2 / (span + 1)
    out = [series[0]]
    for x in series[1:]:
        out.append(out[-1] + k * (x - out[-1]))
    return out


def rsi(closes, n=14):
    out = [50.0] * len(closes)
    gains = losses = 0.0
    ag = al = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g = max(ch, 0.0); l = max(-ch, 0.0)
        if i <= n:
            gains += g; losses += l
            if i == n:
                ag, al = gains / n, losses / n
                out[i] = 100 - 100 / (1 + (ag / al if al else 999))
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + l) / n
            out[i] = 100 - 100 / (1 + (ag / al if al else 999))
    return out


def atr(series, n=14):
    tr = [0.0]
    for i in range(1, len(series)):
        h, l, pc = series[i][2], series[i][3], series[i - 1][4]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    out = [tr[0]] * len(series)
    a = sum(tr[1:n + 1]) / n if len(tr) > n else tr[0]
    for i in range(len(series)):
        if i >= n:
            a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def features(series):
    c = [r[4] for r in series]
    h = [r[2] for r in series]
    v = [r[5] for r in series]
    e20, e60 = ema(c, 20), ema(c, 60)
    rs = rsi(c)
    at = atr(series)
    feats = []
    for t in range(len(series)):
        if t < 60:
            feats.append(None); continue
        ext20 = (c[t] - e20[t]) / e20[t]
        ext60 = (c[t] - e60[t]) / e60[t]
        ret24 = c[t] / c[t - 24] - 1
        volc = v[t] / (sum(v[t - 24:t]) / 24 + 1e-9)
        hi24 = max(h[t - 24:t + 1])
        pullback = (hi24 - c[t]) / hi24
        feats.append({"t": t, "ext20": ext20, "ext60": ext60, "ret24": ret24,
                      "rsi": rs[t], "volc": volc, "pullback": pullback,
                      "hi24": hi24, "atr": at[t]})
    return feats


def is_rollover(f):
    return f and f["ret24"] >= RUNUP_MIN and f["pullback"] >= PULLBACK_MIN


def is_breakout(f):
    return f and f["ret24"] >= RUNUP_MIN and f["pullback"] <= NEARHIGH_MAX


def sim_long_full(series, at_series, entry_i):
    """long at next-bar open; hard-stop on drop + ATR trailing off the high + time."""
    n = len(series)
    entry = series[entry_i][1]
    if entry <= 0:
        return None
    armed = False
    best_high = entry
    end = min(entry_i + TIME_BARS, n)
    for j in range(entry_i, end):
        hi, lo, cl = series[j][2], series[j][3], series[j][4]
        adverse = (entry - lo) / entry                # price fell against the long
        if adverse >= HARD_STOP:
            return (-HARD_STOP - RT_COST, adverse, 1, j - entry_i + 1)
        best_high = max(best_high, hi)
        fav = (hi - entry) / entry
        if not armed and fav >= TRAIL_ARM:
            armed = True
        if armed:
            trail_px = best_high - TRAIL_ATR * at_series[j]
            if lo <= trail_px:
                ret = (trail_px - entry) / entry - RT_COST
                mae = max((entry - series[k][3]) / entry for k in range(entry_i, j + 1))
                return (ret, mae, 0, j - entry_i + 1)
    ex_i = end - 1
    exit_px = series[ex_i][4]
    ret = (exit_px - entry) / entry - RT_COST
    mae = max((entry - series[j][3]) / entry for j in range(entry_i, ex_i + 1))
    return (ret, mae, 0, ex_i - entry_i + 1)


def collect(data, funnel, split=0.6):
    sigs = []
    per = {}
    for sym, series in data.items():
        feats = features(series)
        at_series = atr(series)
        per[sym] = (series, feats, at_series)
        tmax, tmin = series[-1][0], series[0][0]
        cut = tmin + split * (tmax - tmin)
        last_exit = -1
        for f in feats:
            if not funnel(f):
                continue
            entry_i = f["t"] + 1
            if entry_i >= len(series) or entry_i <= last_exit:
                continue
            seg = "train" if series[f["t"]][0] <= cut else "test"
            sigs.append({"sym": sym, "f": f, "entry_i": entry_i, "seg": seg,
                         "ts": series[f["t"]][0]})
    # froth thresholds top1/3 on TRAIN
    FK = ["ext20", "ext60", "ret24", "rsi", "volc"]
    tr = [s for s in sigs if s["seg"] == "train"]
    thr = {}
    for k in FK:
        vals = sorted(s["f"][k] for s in tr)
        thr[k] = vals[int(2 / 3 * len(vals))] if vals else 0
    out = []
    last_exit_by = {}
    for s in sorted(sigs, key=lambda x: (x["sym"], x["entry_i"])):
        sym = s["sym"]
        if s["entry_i"] <= last_exit_by.get(sym, -1):
            continue
        s["score"] = sum(1 for k in FK if s["f"][k] >= thr[k])
        series, feats, at_series = per[sym]
        r = sim_long_full(series, at_series, s["entry_i"])
        if not r:
            continue
        s["sim"] = r
        last_exit_by[sym] = s["entry_i"] + r[3]
        out.append(s)
    return out, thr


def bin_of(score):
    return "0-1" if score <= 1 else ("2-3" if score <= 3 else "4-5")


def stats(g):
    rets = [s["sim"][0] for s in g]
    maes = sorted(s["sim"][1] for s in g)
    cat = sum(s["sim"][2] for s in g) / len(g)
    ev = sum(rets) / len(rets)
    win = sum(1 for r in rets if r > 0) / len(rets)
    mae95 = maes[int(0.95 * len(maes)) - 1] if maes else 0
    return len(g), ev, win, cat, mae95


def report(sigs, name):
    print(f"\n--- {name}: LONG EV by froth bin (n, EV, win%, catastrophe%, mae_p95) ---")
    bins = {}
    for s in sigs:
        bins.setdefault(bin_of(s["score"]), []).append(s)
    for b in ["0-1", "2-3", "4-5"]:
        g = bins.get(b, [])
        if not g:
            print(f"  bin {b}: (none)"); continue
        n, ev, win, cat, m95 = stats(g)
        print(f"  bin {b}: n={n:4d} EV={100*ev:+6.2f}% win={100*win:4.0f}% "
              f"catastrophe={100*cat:5.1f}% mae_p95={100*m95:5.1f}%")


def robust(sigs, name):
    rets = sorted((s["sim"][0] for s in sigs), reverse=True)
    if not rets:
        return
    pos = sum(r for r in rets if r > 0)
    neg = -sum(r for r in rets if r < 0)
    pf = pos / neg if neg else 999
    pf_t1 = (pos - rets[0]) / neg if neg else 999
    rng = random.Random(7)
    means = []
    for _ in range(2000):
        s = [rets[rng.randrange(len(rets))] for _ in range(len(rets))]
        means.append(sum(s) / len(s))
    means.sort()
    p5 = means[int(0.05 * len(means))]
    print(f"  [{name}] n={len(rets)} EV={100*sum(rets)/len(rets):+.2f}% "
          f"PF={pf:.2f} PF_top1removed={pf_t1:.2f} boot_p5={100*p5:+.2f}%")


def run(data, funnel, label):
    print(f"\n================= {label} =================")
    sigs, thr = collect(data, funnel)
    if not sigs:
        print("  no signals."); return
    print(f"total signals={len(sigs)}  froth thr(top1/3,train)={ {k: round(v,3) for k,v in thr.items()} }")
    allret = [s["sim"][0] for s in sigs]
    print(f"ALL realized LONG EV={100*sum(allret)/len(allret):+.2f}% "
          f"median={100*st.median(allret):+.2f}% "
          f"catastrophe={100*sum(s['sim'][2] for s in sigs)/len(sigs):.1f}%")
    report(sigs, "ALL")
    tr = [s for s in sigs if s["seg"] == "train"]
    te = [s for s in sigs if s["seg"] == "test"]
    report(tr, "TRAIN")
    report(te, "TEST (OOS)")
    print("\n  robustness on frothiest bin (score>=4):")
    robust([s for s in sigs if s["score"] >= 4], "ALL froth>=4")
    robust([s for s in te if s["score"] >= 4], "TEST froth>=4")
    robust(sigs, "ALL signals")


def main():
    data = load()
    print(f"#### FROTH LONG (mirror of the inverted short). coins={len(data)} ####")
    print(f"NOTE: funding OMITTED -> OPTIMISTIC for longs. Gross-positive is necessary not sufficient.")
    run(data, is_rollover, "ROLLOVER LONG (literal mirror of short candidates)")
    run(data, is_breakout, "BREAKOUT LONG (pure strength, within 1% of 24h high)")


if __name__ == "__main__":
    main()
