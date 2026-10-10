#!/usr/bin/env python3
"""
OOS-first univariate feature scan over the right-tail label substrate.

P0 question: do early (first-30min) features predict TOXICITY (not_sellable / dead_24h)?
P1 question: do early features predict the RIGHT TAIL (mfe_72h >= 5x)?

Discipline: discover quantile lift on TRAIN (oldest 50%) ONLY; then lock candidate
filters and validate ONCE on RECENT OOS (newest 25%). No tuning on the OOS set.
"""
from __future__ import annotations

import argparse
import csv
from statistics import median

FEATURES = [
    "early_swaps", "early_wbuy", "early_wsell", "early_buysell_ratio",
    "early_active_min", "early_tick_range", "entry_impact_10usd", "fee_bps",
]


def fnum(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load(path):
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in FEATURES + ["mfe_72h", "mfe_24h", "max_dd_before_5x"]:
            r[k] = fnum(r.get(k))
        for k in ["dead_24h", "dead_72h", "sellable_after_entry"]:
            r[k] = int(r[k]) if r.get(k) not in (None, "") else None
    rows.sort(key=lambda r: r["birth_utc"])
    return rows


def label_toxic(r):       # 1 = bad (cannot exit)
    return 1 if r["sellable_after_entry"] == 0 else 0


def label_5x(r):
    return 1 if (r["mfe_72h"] or 0) >= 5 else 0


def rate(rows, lab):
    rows = [r for r in rows if lab(r) is not None]
    return (sum(lab(r) for r in rows) / len(rows)) if rows else 0.0


def quantile_lift(train, feat, lab, q=5):
    vals = sorted(r[feat] for r in train if r[feat] is not None)
    if len(vals) < q * 5:
        return None
    cuts = [vals[int(len(vals) * i / q)] for i in range(1, q)]
    buckets = [[] for _ in range(q)]
    for r in train:
        v = r[feat]
        if v is None:
            continue
        b = 0
        while b < q - 1 and v >= cuts[b]:
            b += 1
        buckets[b].append(r)
    out = []
    for i, bk in enumerate(buckets):
        if not bk:
            out.append(None); continue
        lo = "-inf" if i == 0 else f"{cuts[i-1]:.4g}"
        hi = "+inf" if i == q - 1 else f"{cuts[i]:.4g}"
        out.append((f"[{lo},{hi})", len(bk), round(rate(bk, lab), 4)))
    return cuts, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="/opt/EATLQ/mvp/diagnostics/righttail_labels.csv")
    args = ap.parse_args()
    rows = load(args.csv)
    n = len(rows)
    train = rows[:n // 2]
    recent = rows[n * 3 // 4:]
    print(f"loaded={n} train={len(train)} recent_oos={len(recent)}\n")

    for name, lab in (("TOXIC(not_sellable)", label_toxic), ("RIGHT_TAIL(5x_72h)", label_5x)):
        base_tr = rate(train, lab); base_oos = rate(recent, lab)
        print(f"==== {name}  base train={base_tr:.4f}  recent_oos={base_oos:.4f} ====")
        for feat in FEATURES:
            res = quantile_lift(train, feat, lab, q=5)
            if not res:
                continue
            _, buckets = res
            cells = "  ".join(f"{b[0]}:n{b[1]}={b[2]:.3f}" for b in buckets if b)
            print(f"  {feat:22s} {cells}")
        print()

    # ---- locked candidate filters (chosen from TRAIN lift), validated on RECENT OOS ----
    def apply_rule(rows_set, rule):
        keep = [r for r in rows_set if rule(r)]
        return keep

    # P0 candidate: require pool depth + early activity (avoid thin/dead)
    def p0_rule(r):
        return (r["entry_impact_10usd"] is not None and abs(r["entry_impact_10usd"]) < 0.02
                and (r["early_swaps"] or 0) >= 15
                and (r["early_wsell"] or 0) > 0)

    lab10 = lambda r: 1 if (r["mfe_72h"] or 0) >= 10 else 0

    def report(tag, rs, rule):
        kept = apply_rule(rs, rule)
        if not kept:
            print(f"  {tag}: kept 0"); return
        win5_all = sum(label_5x(r) for r in rs); win5_keep = sum(label_5x(r) for r in kept)
        win10_all = sum(lab10(r) for r in rs); win10_keep = sum(lab10(r) for r in kept)
        rec5 = win5_keep / win5_all if win5_all else 0
        rec10 = win10_keep / win10_all if win10_all else 0
        print(f"  {tag}: keep {len(kept)}/{len(rs)} ({len(kept)/len(rs):.1%})  "
              f"notsell {rate(rs,label_toxic):.3f}->{rate(kept,label_toxic):.3f}  "
              f"p5x {rate(rs,label_5x):.3f}->{rate(kept,label_5x):.3f} (recall {rec5:.0%})  "
              f"p10x {rate(rs,lab10):.3f}->{rate(kept,lab10):.3f} (recall {rec10:.0%})")

    print("==== STRICT P0 RULE (capital preservation) ====")
    report("recent_oos", recent, p0_rule)
    report("train     ", train, p0_rule)

    # active_min threshold ladder (thresholds chosen from TRAIN buckets: 3/6/31)
    print("\n==== active_min ladder on RECENT OOS (recall vs toxicity tradeoff) ====")
    for thr in (3, 6, 15, 31):
        report(f"active_min>={thr:<3d}", recent, lambda r, t=thr: (r["early_active_min"] or 0) >= t)
    print("\n==== active_min ladder on TRAIN ====")
    for thr in (3, 6, 15, 31):
        report(f"active_min>={thr:<3d}", train, lambda r, t=thr: (r["early_active_min"] or 0) >= t)


if __name__ == "__main__":
    main()
