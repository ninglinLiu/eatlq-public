"""Investigate the >1000% APR outliers by densely re-sampling in-range staked liquidity."""

import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C
import rpc as R

R.RATE_LIMIT_PER_SEC = 40.0
DENSE = 168  # one sample per hour of the epoch


def dense_profile(pool, b0, b1):
    step = (b1 - b0) // DENSE
    blocks = [b0 + k * step for k in range(DENSE)]
    vals = {}

    def work(bs):
        items = []
        for bn in bs:
            items.append(("eth_call", [{"to": pool, "data": C.SEL["stakedLiquidity()"]}, hex(bn)]))
            items.append(("eth_call", [{"to": pool, "data": C.SEL["liquidity()"]}, hex(bn)]))
        res = R.rpc_batch(items)
        for i, bn in enumerate(bs):
            sl, e1 = res[2 * i]
            tl, e2 = res[2 * i + 1]
            vals[bn] = None if (e1 or e2 or not sl or sl == "0x") else (
                R.dec_uint(sl), R.dec_uint(tl))

    chunks = [blocks[i:i + 10] for i in range(0, len(blocks), 10)]
    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(work, chunks))
    return [(b, vals[b]) for b in blocks if vals.get(b) is not None]


def main():
    panel = C.load_json("panel.json")
    ep_by_ts = {e["epoch_start"]: e for e in panel["epochs"]}

    top = C.ch_rows("""
        SELECT a.pool AS pool, a.gauge AS gauge, toUInt32(a.epoch_start) AS ep,
               a.apr_lb AS apr_lb, a.aero_amount AS aero, a.emission_usd AS emission_usd,
               a.staked_liquidity AS sl4, a.staked_liquidity_usd_ub AS usd_ub,
               a.staked_share AS share, u.symbol AS alt
        FROM census.aero_emission_apr a
        LEFT JOIN edge.bars_universe_50 u ON u.pool = a.pool
        ORDER BY a.apr_lb DESC LIMIT 3""")

    out = []
    for t in top:
        ep = int(t["ep"])
        g = ep_by_ts[ep]
        print(f"\n=== {t['alt']} {t['pool']} epoch {R.iso(ep)} ===")
        print(f"  4-sample APR lower bound : {float(t['apr_lb'])*100:,.0f}%")
        print(f"  AERO notified            : {float(t['aero']):,.1f}  (${float(t['emission_usd']):,.0f})")
        print(f"  4-sample mean stakedLiq  : {float(t['sl4']):,.0f}  "
              f"(USD upper bound ${float(t['usd_ub']):,.2f})")
        prof = dense_profile(t["pool"], g["block_start"], g["block_end"])
        sl = [v[0] for _, v in prof]
        tl = [v[1] for _, v in prof]
        nz = [x for x in sl if x > 0]
        frac_zero = 1 - len(nz) / len(sl) if sl else 0
        mean_all = sum(sl) / len(sl) if sl else 0
        mean_nz = sum(nz) / len(nz) if nz else 0
        print(f"  dense re-sample ({len(prof)} hourly points):")
        print(f"    hours with ZERO in-range staked liquidity : {frac_zero*100:.1f}%")
        print(f"    mean stakedLiquidity (all hours)          : {mean_all:,.0f}")
        print(f"    mean stakedLiquidity (non-zero hours)     : {mean_nz:,.0f}")
        print(f"    mean pool liquidity                       : {sum(tl)/len(tl):,.0f}")
        ratio = mean_all / float(t["sl4"]) if float(t["sl4"]) > 0 else None
        print(f"    dense-mean / 4-sample-mean                : "
              f"{ratio:.2f}x" if ratio else "    n/a")
        if mean_all > 0:
            print(f"    APR lower bound using dense mean          : "
                  f"{float(t['apr_lb']) * float(t['sl4']) / mean_all * 100:,.0f}%")
        out.append({
            "pool": t["pool"], "alt": t["alt"], "epoch": R.iso(ep),
            "apr_lb_4sample_pct": float(t["apr_lb"]) * 100,
            "dense_points": len(prof),
            "frac_hours_zero_staked": frac_zero,
            "mean_staked_dense": mean_all, "mean_staked_4sample": float(t["sl4"]),
            "apr_lb_dense_pct": (float(t["apr_lb"]) * float(t["sl4"]) / mean_all * 100)
            if mean_all > 0 else None,
        })
    C.save_json("apr_outlier_investigation.json", out)


if __name__ == "__main__":
    main()
