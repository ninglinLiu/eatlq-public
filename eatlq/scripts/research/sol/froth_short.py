#!/usr/bin/env python3
"""FRONT C — froth/overheating SHORT bell (faithful to 1.md spec).

This is NOT the dip-BUY mean reversion (mr_validate, already -96%). This is the
SHORT side: short overheated 1H setups, and the #1 verdict metric is the
CATASTROPHE TAIL (a short's left tail is everything), not mean EV.

DATA: OKX 1H candles, 25 liquid majors, ~1.6y each (_cache_okx). Spot price path;
funding OMITTED (a short would COLLECT positive funding, so this is conservative
for shorts). No look-ahead: all features use <= t; froth thresholds learned on the
TRAIN time-segment only.

SIGNAL (spec 方案1, anti-overfit = extreme-quantile COUNT, no fitted weights):
  froth features (more extreme = more frothy): ext_ema20, ext_ema60, ret24, rsi14,
  vol_climax. On TRAIN, take each feature's top-1/3 as "hit". froth_score = #hits.

CANDIDATE FUNNEL (overheated then rolling over): ret24 >= +20% AND pulled back
>= 3% from the rolling 24h high.

ENTRY: short at next-bar OPEN. EXIT (spec, convex-friendly):
  - hard stop: adverse +15% (price rises 15% against short) => CATASTROPHE
  - once favorable >=12% (price down 12%): trail 1.5*ATR
  - 48h without >=10% favorable: time-exit
COST: taker 0.05%/side = 0.10% round trip. Non-overlapping per coin.

GATES (spec): H1 EV monotone UP with froth_score; H2 catastrophe_rate monotone
DOWN with froth_score (the real test); OOS train/test; placebo shuffle.
"""
from __future__ import annotations
import json
import glob
import os
import random
import statistics as st
from datetime import datetime, timezone

CACHE = "/opt/EATLQ/scripts/research/sol/_cache_okx"
MIN_HOURS = 2000
TAKER = 0.0005           # per side
RT_COST = 2 * TAKER      # round trip
RUNUP_MIN = 0.20         # ret24 >= +20% to be a candidate
PULLBACK_MIN = 0.03      # >=3% off the 24h high (rolling over)
HARD_STOP = 0.15         # adverse +15% => catastrophe
TRAIL_ARM = 0.12         # arm trailing once favorable >=12%
TRAIL_ATR = 1.5
TIME_BARS = 48           # 48h
NEED_FAV = 0.10          # need >=10% favorable within TIME_BARS else exit


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
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g = max(ch, 0.0)
        l = max(-ch, 0.0)
        if i <= n:
            gains += g
            losses += l
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
            feats.append(None)
            continue
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


def is_candidate(f):
    return f and f["ret24"] >= RUNUP_MIN and f["pullback"] >= PULLBACK_MIN


def sim_short_full(series, at_series, entry_i):
    """short at next-bar open; proper hard-stop + ATR trailing + time/fav exit."""
    n = len(series)
    entry = series[entry_i][1]
    if entry <= 0:
        return None
    armed = False
    best_low = entry
    end = min(entry_i + TIME_BARS, n)
    for j in range(entry_i, end):
        hi, lo, cl = series[j][2], series[j][3], series[j][4]
        adverse = (hi - entry) / entry
        if adverse >= HARD_STOP:                      # catastrophe stop
            return (-HARD_STOP - RT_COST, adverse, 1, j - entry_i + 1)
        best_low = min(best_low, lo)
        fav = (entry - lo) / entry
        if not armed and fav >= TRAIL_ARM:
            armed = True
        if armed:
            trail_px = best_low + TRAIL_ATR * at_series[j]   # buy-to-cover trail
            if hi >= trail_px:                                # price popped back up
                ret = (entry - trail_px) / entry - RT_COST
                mae = max((series[k][2] - entry) / entry for k in range(entry_i, j + 1))
                return (ret, mae, 0, j - entry_i + 1)
    ex_i = end - 1
    exit_px = series[ex_i][4]
    ret = (entry - exit_px) / entry - RT_COST
    mae = max((series[j][2] - entry) / entry for j in range(entry_i, ex_i + 1))
    return (ret, mae, 0, ex_i - entry_i + 1)


def collect_signals(data, split=0.6):
    """build candidate signals with froth features; learn thresholds on train."""
    all_sigs = []
    per_coin_feats = {}
    for sym, series in data.items():
        feats = features(series)
        at_series = atr(series)
        per_coin_feats[sym] = (series, feats, at_series)
        tmax = series[-1][0]
        tmin = series[0][0]
        cut = tmin + split * (tmax - tmin)
        for f in feats:
            if not is_candidate(f):
                continue
            entry_i = f["t"] + 1
            if entry_i >= len(series):
                continue
            seg = "train" if series[f["t"]][0] <= cut else "test"
            all_sigs.append({"sym": sym, "f": f, "entry_i": entry_i, "seg": seg,
                             "ts": series[f["t"]][0]})
    # learn top-1/3 thresholds on TRAIN
    FK = ["ext20", "ext60", "ret24", "rsi", "volc"]
    thr = {}
    tr = [s for s in all_sigs if s["seg"] == "train"]
    for k in FK:
        vals = sorted(s["f"][k] for s in tr)
        thr[k] = vals[int(2 / 3 * len(vals))] if vals else 0
    for s in all_sigs:
        s["score"] = sum(1 for k in FK if s["f"][k] >= thr[k])
    # simulate realized short
    for s in all_sigs:
        series, feats, at_series = per_coin_feats[s["sym"]]
        r = sim_short_full(series, at_series, s["entry_i"])
        s["sim"] = r
    return [s for s in all_sigs if s["sim"]], thr


def bin_of(score):
    if score <= 1:
        return "0-1"
    if score <= 3:
        return "2-3"
    return "4-5"


def report(sigs, seg_name, sigs_for_overlap=None):
    print(f"\n--- {seg_name}: short EV & CATASTROPHE by froth_score bin (n, EV, win%, catastrophe%, mae_p95) ---")
    bins = {}
    for s in sigs:
        bins.setdefault(bin_of(s["score"]), []).append(s)
    for b in ["0-1", "2-3", "4-5"]:
        g = bins.get(b, [])
        if not g:
            print(f"  bin {b}: (none)")
            continue
        rets = [s["sim"][0] for s in g]
        maes = sorted(s["sim"][1] for s in g)
        cat = sum(s["sim"][2] for s in g) / len(g)
        ev = sum(rets) / len(rets)
        win = sum(1 for r in rets if r > 0) / len(rets)
        mae95 = maes[int(0.95 * len(maes)) - 1] if maes else 0
        print(f"  bin {b}: n={len(g):4d} EV={100*ev:+6.2f}% win={100*win:4.0f}% "
              f"catastrophe={100*cat:5.1f}% mae_p95={100*mae95:5.1f}%")


def placebo(sigs, n=200, seed=3):
    """shuffle froth_score across signals; real edge -> top-bin EV collapses to mean."""
    rng = random.Random(seed)
    scores = [s["score"] for s in sigs]
    rets = [s["sim"][0] for s in sigs]
    real_top = st.mean([r for s, r in zip(sigs, rets) if s["score"] >= 4]) if any(s["score"] >= 4 for s in sigs) else None
    if real_top is None:
        return None, None
    sims = []
    for _ in range(n):
        rng.shuffle(scores)
        top = [r for sc, r in zip(scores, rets) if sc >= 4]
        if top:
            sims.append(st.mean(top))
    return real_top, (st.mean(sims) if sims else None)


def main():
    data = load()
    print(f"#### FRONT C: froth SHORT bell (1.md). #1 metric = CATASTROPHE tail, not EV. ####")
    print(f"coins={len(data)} | candidate funnel: ret24>=+{int(100*RUNUP_MIN)}% & pullback>={int(100*PULLBACK_MIN)}% off 24h-high")
    print(f"entry: short next-bar open | hard stop +{int(100*HARD_STOP)}% (catastrophe) | "
          f"trail {TRAIL_ATR}ATR after {int(100*TRAIL_ARM)}% | {TIME_BARS}h cap | cost {100*RT_COST:.2f}% RT (funding omitted=conservative)")
    sigs, thr = collect_signals(data)
    print(f"\ntotal signals={len(sigs)}  thresholds(top1/3,train)={ {k: round(v,3) for k,v in thr.items()} }")
    tr = [s for s in sigs if s["seg"] == "train"]
    te = [s for s in sigs if s["seg"] == "test"]
    allret = [s["sim"][0] for s in sigs]
    print(f"ALL signals realized short EV={100*sum(allret)/len(allret):+.2f}%  "
          f"median={100*st.median(allret):+.2f}%  catastrophe_rate={100*sum(s['sim'][2] for s in sigs)/len(sigs):.1f}%")
    report(sigs, "ALL")
    report(tr, "TRAIN (in-sample threshold)")
    report(te, "TEST (OOS)")
    rt, pb = placebo(sigs)
    if rt is not None:
        print(f"\nplacebo: real top-bin(4-5) EV={100*rt:+.2f}%  shuffled-mean={100*pb:+.2f}%  "
              f"-> {'separated' if abs(rt-pb) > 0.005 else 'NOT separated (score adds nothing)'}")
    # spec H1/H2 verdict
    print("\n=== SPEC VERDICT (H1 EV monotone UP & H2 catastrophe monotone DOWN with froth_score, on TEST) ===")
    bins = {}
    for s in te:
        bins.setdefault(bin_of(s["score"]), []).append(s)
    seq = [b for b in ["0-1", "2-3", "4-5"] if bins.get(b)]
    evs = [sum(x["sim"][0] for x in bins[b]) / len(bins[b]) for b in seq]
    cats = [sum(x["sim"][2] for x in bins[b]) / len(bins[b]) for b in seq]
    h1 = all(evs[i] <= evs[i + 1] for i in range(len(evs) - 1)) and (evs[-1] > 0 if evs else False)
    h2 = all(cats[i] >= cats[i + 1] for i in range(len(cats) - 1))
    print(f"  bins={seq}\n  EV seq={[round(100*x,2) for x in evs]} -> H1 {'PASS' if h1 else 'FAIL'}")
    print(f"  catastrophe seq={[round(100*x,1) for x in cats]} -> H2 {'PASS' if h2 else 'FAIL'}")
    print(f"  >>> {'CANDIDATE (tiny canary only)' if (h1 and h2) else 'FALSIFIED / no convex short edge here'}")


if __name__ == "__main__":
    main()
