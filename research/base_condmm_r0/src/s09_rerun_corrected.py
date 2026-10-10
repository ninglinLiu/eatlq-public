#!/usr/bin/env python3
"""Re-run the C1b preregistered test with the true per-day fee, and sweep for any
surviving positive cell anywhere in the panel.

s06/s07/s08 established that Aerodrome Slipstream fees are recomputed continuously and
that the panel priced every aero_cl pool with a single static snapshot for all 366 days.
The one positive cell in C1b (cbBTC on 0x70acdf..., +242.8 bps mean) was a pure artifact
of that: its fee only reached the stored 2500 in the last 60 days of the window.

This imports s02 unchanged so the methodology, thresholds, seed and preregistered cell
are bit-identical, and only swaps the outcome column for the fee-corrected one.
"""

import json
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/base_condmm_r0/src")
import s02_test as m

OUT = Path("/root/base_condmm_r0/out")


def fee_correction(pos):
    fc = json.loads((OUT / "fee_curve.json").read_text())["curve"]
    uni = [json.loads(l) for l in open(OUT / "universe50.jsonl") if l.strip()]
    stored = {p["pool"]: int(p["fee"]) for p in uni}

    import subprocess
    q = ("SELECT pool, toDate(bar), sum(toFloat64(vol_weth)) FROM edge.bars_1h_year_50 "
         "GROUP BY pool, toDate(bar) FORMAT TabSeparated")
    txt = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                          "--query", q], capture_output=True, text=True,
                         timeout=900).stdout
    vol = {}
    for line in txt.strip().split("\n"):
        if line:
            p, d, v = line.split("\t")
            vol.setdefault(p, {})[d] = float(v)

    ed = pd.to_datetime(pos["entry"]).dt.date
    keys = pd.DataFrame({"pool": pos["pool"].values, "ed": ed.values,
                         "T": pos["T"].values}).drop_duplicates()
    corr = {}
    for pl, d0, T in keys.itertuples(index=False):
        ser, f0 = fc.get(pl), stored.get(pl)
        if ser is None or not f0:
            corr[(pl, d0, T)] = 1.0
            continue
        pv = vol.get(pl, {})
        num = den = 0.0
        for k in range(int(T)):
            d = (d0 + timedelta(days=k)).isoformat()
            ft = ser.get(d)
            if ft is None:
                continue
            w = pv.get(d, 0.0) or 1.0
            num += w * ft
            den += w
        corr[(pl, d0, T)] = (num / den) / f0 if den > 0 else 1.0

    return np.array([corr[(p, e, t)] for p, e, t in zip(pos["pool"], ed, pos["T"])])


def main():
    import subprocess
    txt = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                          "--query", "SELECT pool, venue FROM edge.bars_universe_50 "
                                     "FORMAT TabSeparated"],
                         capture_output=True, text=True, timeout=300).stdout
    m.VENUE = dict(l.split("\t") for l in txt.rstrip("\n").split("\n") if l)

    pos, pan, meta = m.load()
    print(f"positions kept: {meta['positions_kept']}  panel: {meta['panel_records']}")

    pos["corr"] = fee_correction(pos)
    pos["net_strict_bps_true"] = pos["net_strict_bps"] + pos["fee_bps"] * (pos["corr"] - 1.0)
    pos["fee_bps_true"] = pos["fee_bps"] * pos["corr"]

    print(f"median corr = {pos['corr'].median():.4f}   "
          f"share |corr-1|>1% = {100.0 * (np.abs(pos['corr'] - 1) > .01).mean():.1f}%")

    results = {}
    for tag, col in (("stored_fee", "net_strict_bps"), ("true_fee", "net_strict_bps_true")):
        m.OUTCOME = col
        m.RNG = np.random.default_rng(20260819)  # identical seed both runs
        base = m.unit(pos, pan, "fv", 168, 1.0, True, 7, f"BASELINE all T7 [{tag}]")
        prim = m.unit(pos, pan, "fv", 168, 0.25, True, 7,
                      f"PRIMARY fv_168 q25 T7 [{tag}]", full_loo=True)
        prim["verdict"] = m.verdict(prim)
        results[tag] = {"baseline": base, "primary": prim}

        print(f"\n=== {tag} ===")
        for k, u in (("baseline", base), ("primary", prim)):
            print(f"  {k:9s} n={u['n_rows']:7d} blocks={u['blocks']:4d} "
                  f"mean={u['mean']:8.2f} [{u['lo']:8.2f}, {u['hi']:8.2f}]  "
                  f"fee={u['fee_bps']:7.2f} il={u['il_bps']:8.2f} "
                  f"quiet={u['quiet_frac']:.3f}")
        print(f"  top_symbol={prim['top_symbol']} "
              f"share={prim['top_symbol_net_share']:.3f} "
              f"loo_worst_lo={prim['loo_worst_lo']}")
        print(f"  VERDICT = {prim['verdict']}")

    print("\n" + "=" * 104)
    print("SWEEP: does ANY pool survive with a positive lower bound after correction?")
    print("=" * 104)
    m.OUTCOME = "net_strict_bps_true"
    m.RNG = np.random.default_rng(20260819)
    rows = []
    for T in sorted(pos["T"].unique()):
        sub = pos[pos["T"] == T]
        for pl, g in sub.groupby("pool"):
            if len(g) < 60 or g["entry"].nunique() < m.MIN_BLOCKS:
                continue
            s, n = m.day_blocks(g)
            r = m.boot_ci(s, n)
            rows.append({"pool": pl, "symbol": g["symbol"].iloc[0], "T": int(T),
                         "venue": m.VENUE.get(pl, "unknown"), "n": len(g),
                         "mean": r["mean"], "lo": r["lo"], "hi": r["hi"],
                         "corr": float(g["corr"].mean())})
    sw = pd.DataFrame(rows).sort_values("lo", ascending=False)
    print(f"pool x T cells tested: {len(sw)}")
    surv = sw[sw["lo"] > 0]
    print(f"cells with bootstrap lower bound > 0: {len(surv)}")
    print("\ntop 15 by lower bound:")
    print(sw.head(15).to_string(index=False,
          formatters={"mean": "{:8.1f}".format, "lo": "{:8.1f}".format,
                      "hi": "{:8.1f}".format, "corr": "{:5.3f}".format}))

    print("\nsame 15 cells priced with the STORED fee, for contrast:")
    m.OUTCOME = "net_strict_bps"
    m.RNG = np.random.default_rng(20260819)
    top = sw.head(15)
    out = []
    for r in top.itertuples(index=False):
        g = pos[(pos["pool"] == r.pool) & (pos["T"] == r.T)]
        s, n = m.day_blocks(g)
        b = m.boot_ci(s, n)
        out.append({"pool": r.pool[:12], "symbol": r.symbol, "T": r.T,
                    "stored_mean": b["mean"], "stored_lo": b["lo"],
                    "true_mean": r.mean, "true_lo": r.lo})
    print(pd.DataFrame(out).to_string(index=False, float_format=lambda x: f"{x:8.1f}"))

    json.dump({"results": results, "sweep": rows},
              open(OUT / "rerun_corrected.json", "w"), indent=1, default=str)
    print(f"\nwrote {OUT / 'rerun_corrected.json'}")


if __name__ == "__main__":
    main()
