#!/usr/bin/env python3
"""SUPERSEDED by run_mr2.py — kept as the record of the first pass.

Two defects, both fixed in run_mr2.py: bars are indexed by row rather than by
hour, so H=4 meant "four existing bars" on any pool with gaps; and returns are
compared against zero rather than against the same symbol's unconditional
return, which over this window conflates a real alt-vs-WETH drift with signal.

Mean-reversion test on the depth-confirmed universe. Spec frozen in GOAL.md.

Nothing here chooses a parameter: the grid, the split dates, the cost model and
the verdict thresholds all come from the pre-registration. The only thing this
script decides is which grid cell wins on IS, and it reports OOS for that cell
alone.
"""
from __future__ import annotations

import json
import subprocess
from collections import defaultdict

import numpy as np

CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=1800"]
VOL_WINDOW = 168
MIN_VOL_OBS = 120
KS = [2.0, 3.0]
HS = [1, 4, 24]
IS_END = "2026-07-16"
OOS_END = "2026-08-18"
BOOT = 10_000
RNG = np.random.default_rng(20260818)


def load() -> dict[str, dict]:
    """One pool per symbol, cheapest by measured cost, as frozen in GOAL.md §2."""
    sql = """
    WITH best AS (
      SELECT symbol AS sym, argMin(pool, cost_bps) AS best_pool,
             min(cost_bps) AS best_cost, any(is_major) AS maj
      FROM edge.bars_1h GROUP BY symbol)
    SELECT b.sym, b.best_pool, b.maj, b.best_cost,
           toString(a.bar), a.open, a.close
    FROM edge.bars_1h a INNER JOIN best b ON a.pool = b.best_pool
    ORDER BY b.sym, a.bar FORMAT TSV
    """
    p = subprocess.run(CH + ["--query", sql], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:600])
    out: dict[str, dict] = {}
    for ln in p.stdout.decode().strip().split("\n"):
        sym, pool, maj, cost, bar, op, cl = ln.split("\t")
        d = out.setdefault(sym, {"pool": pool, "is_major": int(maj), "cost_bps": float(cost),
                                 "bar": [], "open": [], "close": []})
        d["bar"].append(bar)
        d["open"].append(float(op))
        d["close"].append(float(cl))
    for d in out.values():
        d["open"] = np.array(d["open"])
        d["close"] = np.array(d["close"])
    return out


def signals(d: dict) -> tuple[np.ndarray, np.ndarray]:
    """z per bar, NaN where the trailing window is too sparse. No lookahead."""
    c = d["close"]
    lr = np.full(len(c), np.nan)
    lr[1:] = np.log(c[1:] / c[:-1])
    z = np.full(len(c), np.nan)
    for t in range(VOL_WINDOW + 1, len(c)):
        w = lr[t - VOL_WINDOW:t]
        w = w[np.isfinite(w)]
        if len(w) < MIN_VOL_OBS:
            continue
        s = w.std(ddof=1)
        if s > 0 and np.isfinite(lr[t]):
            z[t] = lr[t] / s
    return lr, z


def trades(d: dict, z: np.ndarray, k: float, h: int, side: str) -> list[dict]:
    """Entry at t+1 open, exit at t+1+h close, no overlapping positions."""
    out = []
    busy_until = -1
    n = len(z)
    for t in range(n):
        if t <= busy_until or not np.isfinite(z[t]):
            continue
        hit = z[t] < -k if side == "long" else z[t] > k
        if not hit:
            continue
        ei, xi = t + 1, t + 1 + h
        if xi >= n:
            break
        ep, xp = d["open"][ei], d["close"][xi]
        if not (ep > 0 and xp > 0):
            continue
        gross = np.log(xp / ep)
        if side == "short":
            gross = -gross
        out.append({"bar": d["bar"][ei], "day": d["bar"][ei][:10], "z": z[t],
                    "gross_bps": gross * 1e4,
                    "net_bps": gross * 1e4 - 2 * d["cost_bps"]})
        busy_until = xi
    return out


def block_bootstrap_p(tr: list[dict]) -> float:
    """Day-blocked: same-day returns across symbols share a market factor."""
    by_day = defaultdict(list)
    for t in tr:
        by_day[t["day"]].append(t["net_bps"])
    days = list(by_day.values())
    if len(days) < 5:
        return 1.0
    obs = float(np.mean([x for d in days for x in d]))
    means = np.empty(BOOT)
    nd = len(days)
    for i in range(BOOT):
        pick = RNG.integers(0, nd, nd)
        vals = [x for j in pick for x in days[j]]
        means[i] = np.mean(vals)
    centred = means - means.mean()
    return float(np.mean(np.abs(centred) >= abs(obs)))


def stats(tr: list[dict]) -> dict:
    if not tr:
        return {"n": 0}
    net = np.array([t["net_bps"] for t in tr])
    gross = np.array([t["gross_bps"] for t in tr])
    cut = np.percentile(np.abs(net), 99)
    trimmed = net[np.abs(net) <= cut]
    return {"n": len(tr), "gross_mean_bps": float(gross.mean()),
            "net_mean_bps": float(net.mean()), "net_median_bps": float(np.median(net)),
            "win_rate": float((net > 0).mean()),
            "net_mean_ex_top1pct_bps": float(trimmed.mean()),
            "days": len({t["day"] for t in tr})}


if __name__ == "__main__":
    data = load()
    non_major = {s: d for s, d in data.items() if d["is_major"] == 0}
    print(f"[mr] symbols total={len(data)} non_major={len(non_major)}", flush=True)

    sig = {s: signals(d) for s, d in non_major.items()}
    results = {}
    for side in ("long", "short"):
        for k in KS:
            for h in HS:
                allt = []
                for s, d in non_major.items():
                    allt += trades(d, sig[s][1], k, h, side)
                is_t = [t for t in allt if t["bar"] < IS_END]
                oos_t = [t for t in allt if IS_END <= t["bar"] < OOS_END]
                results[f"{side}_k{k:g}_h{h}"] = {
                    "side": side, "k": k, "h": h,
                    "is": stats(is_t), "oos": stats(oos_t),
                    "_oos_trades": oos_t,
                }

    print("\n=== IS grid (parameter selection happens here only) ===")
    print(f"{'cell':<18}{'n_is':>7}{'gross':>9}{'net':>9}{'median':>9}{'win':>7}")
    for key, r in results.items():
        i = r["is"]
        if not i.get("n"):
            print(f"{key:<18}{0:>7}")
            continue
        print(f"{key:<18}{i['n']:>7}{i['gross_mean_bps']:>9.1f}{i['net_mean_bps']:>9.1f}"
              f"{i['net_median_bps']:>9.1f}{i['win_rate']:>7.2f}")

    long_cells = {k: v for k, v in results.items()
                  if v["side"] == "long" and v["is"].get("n", 0) >= 30}
    best = max(long_cells, key=lambda k: long_cells[k]["is"]["net_mean_bps"])
    print(f"\n[mr] IS-selected long cell: {best}")

    r = results[best]
    r["oos"]["bootstrap_p"] = block_bootstrap_p(r["_oos_trades"])
    print("\n=== OOS for the selected cell only ===")
    for k2, v in r["oos"].items():
        print(f"  {k2:<28}{v}")

    print("\n=== short side (informational only; not implementable spot-only) ===")
    print(f"{'cell':<18}{'n_oos':>7}{'net_oos':>10}")
    for key, rr in results.items():
        if rr["side"] != "short":
            continue
        o = rr["oos"]
        print(f"{key:<18}{o.get('n',0):>7}"
              f"{(o.get('net_mean_bps') if o.get('n') else float('nan')):>10.1f}")

    dump = {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
            for k, v in results.items()}
    json.dump({"selected_is_cell": best, "grid": dump,
               "spec": {"vol_window": VOL_WINDOW, "ks": KS, "hs": HS,
                        "is_end": IS_END, "oos_end": OOS_END, "boot": BOOT}},
              open("out/mr_results.json", "w"), indent=1)
    print("\n[mr] wrote out/mr_results.json")
