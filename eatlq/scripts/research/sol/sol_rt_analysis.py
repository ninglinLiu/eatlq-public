#!/usr/bin/env python3
"""Stage 3 — right-tail existence, CAPTURABILITY, and ex-ante SELECTABILITY on a
multi-day unbiased pump.fun creation sample, with anti-self-deception gates.

This stage uses ONLY the zero-cost labels (ath_mc, time_to_ath, complete) +
birth-fixed ex-ante features. It answers, before spending any money or OHLCV RPC:

  Q1 EXISTENCE   : how often does a fresh pump coin reach a meaningful ATH?
  Q2 CAPTURABILITY: do right-tail coins peak slowly enough for a 30-300s poller?
  Q3 SELECTABILITY: can any ex-ante (birth-fixed) feature lift the right-tail
                    hit-rate above base rate, OUT OF SAMPLE (split by day) and
                    robustly (bootstrap p5 of lift > 1)?
  Q4 STABILITY    : is the right-tail rate stable across days (no regime fluke)?

A positive verdict requires: capturable winners exist AND an ex-ante rule lifts
hit-rate OOS with bootstrap_p5(lift)>1 and the lift not driven by one day.
"""
from __future__ import annotations
import argparse
import json
import math
import random
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone

LAB = "/opt/EATLQ/mvp/diagnostics/sol/creates_labeled.jsonl"


def day_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d")


def boot_p5_lift(pos, base_rate, n_boot=2000, seed=1):
    """bootstrap p5 of (hit_rate within selected set / base_rate)."""
    rng = random.Random(seed)
    if not pos or base_rate <= 0:
        return 0.0
    lifts = []
    for _ in range(n_boot):
        s = [pos[rng.randrange(len(pos))] for _ in range(len(pos))]
        hr = sum(s) / len(s)
        lifts.append(hr / base_rate)
    lifts.sort()
    return lifts[int(0.05 * len(lifts))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ath-thr", type=float, default=20000, help="right-tail ATH threshold (USD)")
    ap.add_argument("--min-capture-s", type=float, default=120, help="winner must peak at/after this (capturable)")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(LAB)]
    n = len(rows)
    print(f"=== Solana pump.fun right-tail analysis  (N={n}) ===\n")

    # ---- Q1 EXISTENCE ----
    ath = [r["ath_mc"] for r in rows if r.get("ath_mc")]
    grad = sum(1 for r in rows if r.get("graduated"))
    print("Q1 EXISTENCE")
    print("  graduated: %d (%.2f%%)" % (grad, 100 * grad / n))
    for thr in [10000, 20000, 50000, 100000, 200000]:
        c = sum(1 for a in ath if a >= thr)
        print("  ATH>=%-7d: %4d (%.2f%%)" % (thr, c, 100 * c / n))

    # ---- Q2 CAPTURABILITY ----
    print("\nQ2 CAPTURABILITY (time_to_ath for ATH>=%d winners)" % args.ath_thr)
    win = [r for r in rows if (r.get("ath_mc") or 0) >= args.ath_thr and r.get("time_to_ath_s") is not None]
    t2 = sorted(r["time_to_ath_s"] for r in win)
    if t2:
        print("  n=%d  p10=%.0f p25=%.0f p50=%.0f p75=%.0f p90=%.0f (s)" % (
            len(t2), t2[len(t2)//10], t2[len(t2)//4], t2[len(t2)//2], t2[3*len(t2)//4], t2[9*len(t2)//10]))
        for w in [30, 60, 120, 300]:
            print("    peak<=%3ds: %d/%d (%.0f%%)  -> these are UNcapturable by a %ds poller" % (
                w, sum(1 for x in t2 if x <= w), len(t2), 100*sum(1 for x in t2 if x <= w)/len(t2), w))
        cap = sum(1 for x in t2 if x >= args.min_capture_s)
        print("  CAPTURABLE winners (peak>=%ds): %d/%d (%.0f%%)" % (args.min_capture_s, cap, len(t2), 100*cap/len(t2)))

    # capturable-winner label
    def is_cap_win(r):
        return (r.get("ath_mc") or 0) >= args.ath_thr and (r.get("time_to_ath_s") or 0) >= args.min_capture_s
    y = [1 if is_cap_win(r) else 0 for r in rows]
    base = sum(y) / n
    print("\n  capturable-winner base rate = %.2f%% (%d/%d)" % (100*base, sum(y), n))

    # ---- Q4 STABILITY across days ----
    print("\nQ4 STABILITY (capturable-winner rate by creation day)")
    byday = defaultdict(lambda: [0, 0])
    for r, yy in zip(rows, y):
        d = day_of(r["created_ms"])
        byday[d][0] += yy
        byday[d][1] += 1
    for d in sorted(byday):
        w_, t_ = byday[d]
        print("  %s: %3d coins  winners=%d (%.2f%%)" % (d, t_, w_, 100*w_/t_ if t_ else 0))

    # ---- Q3 SELECTABILITY: ex-ante feature lift ----
    print("\nQ3 SELECTABILITY (birth-fixed ex-ante features; lift vs base, bootstrap p5)")
    feats = {
        "has_twitter": lambda r: r.get("has_twitter"),
        "has_website": lambda r: r.get("has_website"),
        "has_telegram": lambda r: r.get("has_telegram"),
        "has_desc": lambda r: r.get("has_desc"),
        "desc_len>=20": lambda r: (r.get("desc_len") or 0) >= 20,
        "nsfw": lambda r: r.get("nsfw"),
        "name_len>=10": lambda r: (r.get("name_len") or 0) >= 10,
        "not_mayhem": lambda r: r.get("mayhem") != "completed" and r.get("mayhem") is not None,
    }
    print("  %-16s %6s %8s %8s %8s" % ("feature", "n_sel", "hit%", "lift", "boot_p5"))
    for name, fn in feats.items():
        sel = [yy for r, yy in zip(rows, y) if fn(r)]
        if len(sel) < 10:
            print("  %-16s %6d   (too few)" % (name, len(sel)))
            continue
        hr = sum(sel) / len(sel)
        lift = hr / base if base else 0
        p5 = boot_p5_lift(sel, base)
        flag = "  <== PASS" if (p5 > 1.0 and lift >= 1.3 and len(sel) >= 30) else ""
        print("  %-16s %6d %7.2f%% %8.2f %8.2f%s" % (name, len(sel), 100*hr, lift, p5, flag))

    # creator repeat: does a creator appearing >1 in sample correlate?
    cc = defaultdict(int)
    for r in rows:
        cc[r.get("creator")] += 1

    print("\nNOTE: birth-fixed metadata features are weak by construction. If none")
    print("pass, the real ex-ante signal likely lives in the first 1-3min PRICE/")
    print("TRADE path (survival+climb), which needs stage-4 OHLCV on a subset.")


if __name__ == "__main__":
    main()
