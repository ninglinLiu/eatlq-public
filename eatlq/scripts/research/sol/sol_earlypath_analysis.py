#!/usr/bin/env python3
"""Stage 4a analysis — does EARLY-PATH activity (observable by T+180s) select the
capturable right tail, out-of-sample and past anti-self-deception gates?

Label: capturable winner = ath_mc >= --ath-thr AND time_to_ath_s >= --min-capture-s
       (peaks late enough for a 30-300s poller to have entered).

We split by creation DAY into TRAIN (older) / TEST (recent), pick rule thresholds
on TRAIN only, then report TEST hit-rate, lift, bootstrap p5, and the share of the
TEST winners contributed by the single best day (regime-fluke guard).
"""
from __future__ import annotations
import argparse
import json
import random
from collections import defaultdict
from datetime import datetime, timezone

EP = "/opt/EATLQ/mvp/diagnostics/sol/creates_earlypath.jsonl"


def day_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d")


def boot_p5_lift(sel, base, n_boot=3000, seed=1):
    rng = random.Random(seed)
    if not sel or base <= 0:
        return 0.0
    out = []
    for _ in range(n_boot):
        s = [sel[rng.randrange(len(sel))] for _ in range(len(sel))]
        out.append((sum(s) / len(s)) / base)
    out.sort()
    return out[int(0.05 * len(out))]


def evalrule(rows, y, fn):
    sel = [yy for r, yy in zip(rows, y) if fn(r)]
    n = len(sel)
    hits = sum(sel)
    return n, hits, (hits / n if n else 0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ath-thr", type=float, default=20000)
    ap.add_argument("--min-capture-s", type=float, default=120)
    args = ap.parse_args()
    rows = [json.loads(l) for l in open(EP)]
    N = len(rows)

    def is_win(r):
        return (r.get("ath_mc") or 0) >= args.ath_thr and (r.get("time_to_ath_s") or 0) >= args.min_capture_s
    y = [1 if is_win(r) else 0 for r in rows]
    base = sum(y) / N
    print(f"=== Stage 4a early-path selectability (N={N}, winners={sum(y)}, base={100*base:.2f}%) ===")
    print(f"    label: ATH>=${args.ath_thr:.0f} & time_to_ath>={args.min_capture_s:.0f}s\n")

    # describe early features by class
    def avg(key, sub):
        vals = [r.get(key) for r in sub if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else float("nan")
    win = [r for r, yy in zip(rows, y) if yy]
    los = [r for r, yy in zip(rows, y) if not yy]
    print("feature means          winners      losers")
    for k in ["n_trades_60s", "n_trades_180s", "n_trades_600s", "time_to_5th_s", "lifespan_s", "total_trades_seen"]:
        print("  %-18s %10.1f %10.1f" % (k, avg(k, win), avg(k, los)))
    print("  alive_at_180s%%      %9.0f%% %9.0f%%" % (
        100*sum(1 for r in win if r.get("alive_at_180s"))/max(len(win),1),
        100*sum(1 for r in los if r.get("alive_at_180s"))/max(len(los),1)))

    # candidate ex-ante rules (observable by T+180s)
    rules = {
        "n180>=20": lambda r: (r.get("n_trades_180s") or 0) >= 20,
        "n180>=40": lambda r: (r.get("n_trades_180s") or 0) >= 40,
        "n180>=60": lambda r: (r.get("n_trades_180s") or 0) >= 60,
        "n180>=100": lambda r: (r.get("n_trades_180s") or 0) >= 100,
        "alive180": lambda r: bool(r.get("alive_at_180s")),
        "n180>=40&alive": lambda r: (r.get("n_trades_180s") or 0) >= 40 and r.get("alive_at_180s"),
        "n60>=20&alive": lambda r: (r.get("n_trades_60s") or 0) >= 20 and r.get("alive_at_180s"),
        "t5<=20s": lambda r: (r.get("time_to_5th_s") is not None and r["time_to_5th_s"] <= 20),
        "n180_20_60": lambda r: 20 <= (r.get("n_trades_180s") or 0) <= 60,
    }
    print("\n  %-16s %6s %6s %7s %6s %8s" % ("rule(full-sample)", "n_sel", "hits", "hit%", "lift", "boot_p5"))
    for name, fn in rules.items():
        n, h, hr = evalrule(rows, y, fn)
        lift = hr / base if base else 0
        p5 = boot_p5_lift([yy for r, yy in zip(rows, y) if fn(r)], base)
        flag = "  PASS" if (p5 > 1.0 and lift >= 1.3 and n >= 30) else ""
        print("  %-16s %6d %6d %6.2f%% %6.2f %8.2f%s" % (name, n, h, 100*hr, lift, p5, flag))

    # OOS by day: TRAIN = older 60% of days, TEST = newest 40%
    days = sorted({day_of(r["created_ms"]) for r in rows})
    if len(days) >= 3:
        k = max(1, int(len(days) * 0.4))
        test_days = set(days[-k:])
        tr = [(r, yy) for r, yy in zip(rows, y) if day_of(r["created_ms"]) not in test_days]
        te = [(r, yy) for r, yy in zip(rows, y) if day_of(r["created_ms"]) in test_days]
        base_te = sum(yy for _, yy in te) / len(te)
        print(f"\n  OOS: TRAIN days={days[:-k]} ({len(tr)})  TEST days={sorted(test_days)} ({len(te)}) base_te={100*base_te:.2f}%")
        # pick best rule on TRAIN by lift (require n>=20 on train)
        best = None
        for name, fn in rules.items():
            n, h, hr = evalrule([r for r, _ in tr], [yy for _, yy in tr], fn)
            if n >= 20:
                lift = (hr / (sum(yy for _, yy in tr)/len(tr))) if tr else 0
                if best is None or lift > best[1]:
                    best = (name, lift, fn)
        if best:
            name, _, fn = best
            n, h, hr = evalrule([r for r, _ in te], [yy for _, yy in te], fn)
            lift = hr / base_te if base_te else 0
            p5 = boot_p5_lift([yy for r, yy in te if fn(r)], base_te)
            print(f"  best TRAIN rule = '{name}'  -> TEST: n_sel={n} hits={h} hit%={100*hr:.2f} lift={lift:.2f} boot_p5={p5:.2f}")
            # regime-fluke: best day's share of TEST hits within selected
            byd = defaultdict(int)
            for r, yy in te:
                if fn(r) and yy:
                    byd[day_of(r["created_ms"])] += 1
            tot = sum(byd.values())
            if tot:
                top = max(byd.values())
                print(f"  TEST winners-in-selected by day: {dict(byd)}  top-day share={100*top/tot:.0f}%")
            verdict = "PASS" if (lift >= 1.3 and p5 > 1.0 and n >= 30) else "FAIL"
            print(f"  >>> OOS gate verdict: {verdict}")


if __name__ == "__main__":
    main()
