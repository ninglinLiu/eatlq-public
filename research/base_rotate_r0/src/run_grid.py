"""Full preregistered grid for the rotation test, with random and fixed-hold controls."""
import json
import sys
import time
from collections import defaultdict

import numpy as np

sys.path.insert(0, "/root/base_rotate_r0/src")
import params as P
from rotate import load, Sim

OUT = "/root/base_rotate_r0/out"
N_RANDOM = 5
N_BOOT = 2000
CBBTC = "0x70acdf2ad0bf2402c957154f944c19ef4e1cbae1"


def day_of(recs, bucket):
    return np.array([bucket[r["t0"]] // 86400 for r in recs])


def boot(vals, days, n=N_BOOT, alpha=0.05, seed=7):
    """Block bootstrap over calendar days of position entry."""
    if len(vals) < 5:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    by = defaultdict(list)
    for v, d in zip(vals, days):
        by[d].append(v)
    keys = list(by)
    blocks = [np.array(by[k]) for k in keys]
    means = np.empty(n)
    for b in range(n):
        pick = rng.integers(0, len(blocks), len(blocks))
        means[b] = np.concatenate([blocks[j] for j in pick]).mean()
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(np.mean(vals)), float(lo), float(hi)


def summarize(recs, bucket, equity, tag):
    if not recs:
        return None
    n = np.array([r["net_vs_hodl_bps"] for r in recs])
    days = day_of(recs, bucket)
    m, lo, hi = boot(n, days, alpha=0.05 / 90)
    half = np.median(days)
    s1 = n[days <= half]
    s2 = n[days > half]
    eq = np.array([e[1] for e in equity]) if equity else np.array([np.nan])
    conc = defaultdict(float)
    for r in recs:
        conc[r["pool"]] += r["net_vs_hodl_bps"]
    tot = sum(v for v in conc.values())
    top_share = (max(conc.values()) / tot) if tot > 0 else float("nan")
    return dict(
        tag=tag, n_pos=len(recs),
        fee_bps=float(np.mean([r["fee_bps"] for r in recs])),
        il_bps=float(np.mean([r["il_bps"] for r in recs])),
        net_mean=m, net_lo=lo, net_hi=hi,
        net_med=float(np.median(n)), win=float((n > 0).mean()),
        h1=float(s1.mean()) if len(s1) else float("nan"),
        h2=float(s2.mean()) if len(s2) else float("nan"),
        eq_end=float(eq[-1]), eq_min=float(eq.min()), eq_max=float(eq.max()),
        top_pool_share=float(top_share),
    )


def main():
    d = load()
    bucket = d["bucket"]
    entry0 = int((np.datetime64(P.ENTRY_START + "T00:00:00").astype("int64") - bucket[0]) // 900)
    cb = int(np.flatnonzero(d["pool"] == CBBTC)[0]) if (d["pool"] == CBBTC).any() else None
    rows = []
    t_start = time.time()
    deltas = [int(x) for x in sys.argv[1].split(',')] if len(sys.argv) > 1 else P.DELTAS
    cells = [(dl, K, th, c) for dl in deltas for K in P.KS for th in P.THETAS for c in P.CS]
    for idx, (dl, K, th, c) in enumerate(cells):
        s = Sim(d, dl, K, th, c, entry0)
        recs, nsw, nrc = s.run("signal")
        r = summarize(recs, bucket, s.equity, "signal")
        if r is None:
            continue
        r.update(delta=P.DELTA_NAMES[dl], K=K, theta=th, c=c,
                 n_switch=nsw, n_recenter=nrc,
                 cap_hit_rate=s.cap_hits / max(s.cap_tries, 1),
                 switch_per_month=nsw / 11.0,
                 gas_usd_month=(nsw * P.GAS_SWITCH + nrc * P.GAS_RECENTER)
                 * P.GAS_USD_PER_UNIT / 11.0)
        # random control: same interval, same K, uniform draw from the eligible set
        rnd = []
        for seed in range(N_RANDOM):
            rr, _, _ = s.run("random", rng=np.random.default_rng(1000 + seed))
            if rr:
                rnd.append(np.mean([x["net_vs_hodl_bps"] for x in rr]))
        r["rand_mean"] = float(np.mean(rnd)) if rnd else float("nan")
        r["rand_sd"] = float(np.std(rnd)) if len(rnd) > 1 else float("nan")
        r["edge_vs_rand"] = r["net_mean"] - r["rand_mean"]
        if cb is not None:
            fr, _, _ = s.run("fixed", fixed=[cb] * K)
            r["cbbtc_mean"] = float(np.mean([x["net_vs_hodl_bps"] for x in fr])) if fr else float("nan")
            r["cbbtc_n"] = len(fr)
        rows.append(r)
        print(f"[{idx + 1}/{len(cells)}] {r['delta']:>6} K={K} th={th} c={c} "
              f"net={r['net_mean']:8.1f} [{r['net_lo']:8.1f},{r['net_hi']:8.1f}] "
              f"rand={r['rand_mean']:8.1f} eq_end=${r['eq_end']:>9,.0f} "
              f"sw/mo={r['switch_per_month']:.0f} ({time.time() - t_start:.0f}s)", flush=True)
        tag = sys.argv[2] if len(sys.argv) > 2 else "all"
        with open(f"{OUT}/grid_{tag}.json", "w") as f:
            json.dump(rows, f, indent=1)
    print(f"done, {len(rows)} cells, {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()
