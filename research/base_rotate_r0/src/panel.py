"""Export the 15-minute rotation panel from ClickHouse into dense numpy matrices.

Produces out/panel.npz with pool x bucket matrices. Buckets with no swaps carry
forward the last price and zero volume, so a position held through a quiet bucket
earns nothing and suffers no price move, which is what actually happens on chain.
"""
import io
import subprocess
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/base_rotate_r0/src")
import params as P

OUT = "/root/base_rotate_r0/out"


def ch(query, fmt="Parquet"):
    r = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
         "--max_memory_usage=40000000000", "--query", f"{query} FORMAT {fmt}"],
        capture_output=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[-3000:])
    return pd.read_parquet(io.BytesIO(r.stdout))


def main():
    print("[1/5] pool metadata", flush=True)
    pools = ch("""
        SELECT pool, alt, venue, fee, weth_is_token1, symbol, cost_p50
        FROM edge.bars_universe_50 ORDER BY pool""")
    print(f"      {len(pools)} pools", flush=True)

    print("[2/5] daily point-in-time cost / feasibility", flush=True)
    cost = ch(f"""
        SELECT pool, d, cost_bps_med, feasible, max_weth_out_30d, need_weth, eth_usd
        FROM census.pit_cost_50
        WHERE d >= '{P.PANEL_START}' AND pool IN (SELECT pool FROM edge.bars_universe_50)
        ORDER BY pool, d""")
    print(f"      {len(cost)} pool-days", flush=True)

    print("[3/5] 15-minute bars", flush=True)
    bars = ch(f"""
        SELECT pool, b, n_swaps, vol_over_L, weth_vol, L_q10, sp_first, sp_last, sp_min, sp_max
        FROM edge.rot_15m2 WHERE b >= '{P.PANEL_START}' ORDER BY pool, b""")
    print(f"      {len(bars)} pool-buckets", flush=True)

    print("[4/5] densify onto a common 15-minute grid", flush=True)
    # ClickHouse Parquet hands back DateTime as uint32 unix seconds and Date as
    # days since epoch, so the whole panel is indexed in integer seconds.
    bsec = bars["b"].to_numpy().astype(np.int64)
    grid = np.arange(bsec.min(), bsec.max() + 900, 900, dtype=np.int64)
    pidx = {p: i for i, p in enumerate(pools["pool"])}
    NP, NB = len(pidx), len(grid)
    print(f"      {NP} pools x {NB} buckets = {NP * NB / 1e6:.1f}M cells", flush=True)

    keep = bars["pool"].isin(pidx).to_numpy()
    bars = bars[keep]
    pi = bars["pool"].map(pidx).to_numpy().astype(np.int64)
    bi = (bsec[keep] - grid[0]) // 900

    def mat(col, fill=0.0, dtype=np.float64):
        m = np.full((NP, NB), fill, dtype=dtype)
        m[pi, bi] = bars[col].to_numpy()
        return m

    vol_over_L = mat("vol_over_L")
    weth_vol = mat("weth_vol")
    n_swaps = mat("n_swaps", dtype=np.float32)
    L_q10 = mat("L_q10", np.nan)
    sp_first = mat("sp_first", np.nan)
    sp_last = mat("sp_last", np.nan)
    sp_min = mat("sp_min", np.nan)
    sp_max = mat("sp_max", np.nan)

    # carry the last observed price and liquidity across quiet buckets
    print("      forward-filling price and liquidity", flush=True)
    for m in (sp_last, L_q10):
        idx = np.where(np.isfinite(m), np.arange(NB)[None, :], -1)
        np.maximum.accumulate(idx, axis=1, out=idx)
        rows = np.arange(NP)[:, None]
        filled = m[rows, np.maximum(idx, 0)]
        m[:] = np.where(idx >= 0, filled, np.nan)
    for m in (sp_first, sp_min, sp_max):
        np.copyto(m, sp_last, where=~np.isfinite(m))

    print("[5/5] daily cost matrices aligned to buckets", flush=True)
    cost = cost[cost["pool"].isin(pidx)]
    cday = cost["d"].to_numpy().astype(np.int64)   # days since epoch
    d0, d1 = cday.min(), cday.max()
    ND = int(d1 - d0 + 1)
    cpi = cost["pool"].map(pidx).to_numpy().astype(np.int64)
    cdi = cday - d0

    def dmat(col, fill):
        m = np.full((NP, ND), fill, dtype=np.float64)
        m[cpi, cdi] = cost[col].to_numpy()
        return m

    cost_bps_d = dmat("cost_bps_med", np.inf)
    feasible_d = dmat("feasible", 0.0)
    maxout_d = dmat("max_weth_out_30d", 0.0)
    need_d = dmat("need_weth", np.inf)
    day_of_bucket = np.clip(grid // 86400 - d0, 0, ND - 1).astype(np.int64)

    eth = ch(f"""SELECT bar, eth_usd FROM edge.eth_usd_1h
                 WHERE bar >= '{P.PANEL_START}' ORDER BY bar""")
    esec = eth["bar"].to_numpy().astype(np.int64)
    order = np.argsort(esec)
    esec, eusd = esec[order], eth["eth_usd"].to_numpy()[order]
    pos = np.clip(np.searchsorted(esec, grid, side="right") - 1, 0, len(esec) - 1)
    eth_b = eusd[pos]

    np.savez_compressed(
        f"{OUT}/panel.npz",
        pool=pools["pool"].to_numpy().astype("U42"),
        symbol=pools["symbol"].to_numpy().astype("U32"),
        venue=pools["venue"].to_numpy().astype("U12"),
        alt=pools["alt"].to_numpy().astype("U42"),
        fee=pools["fee"].to_numpy().astype(np.float64),
        weth_is_token1=pools["weth_is_token1"].to_numpy().astype(np.int8),
        bucket=grid,
        vol_over_L=vol_over_L, weth_vol=weth_vol, n_swaps=n_swaps,
        L_q10=L_q10, sp_first=sp_first, sp_last=sp_last, sp_min=sp_min, sp_max=sp_max,
        cost_bps_d=cost_bps_d, feasible_d=feasible_d, maxout_d=maxout_d, need_d=need_d,
        day_of_bucket=day_of_bucket, eth_usd=eth_b,
    )
    print(f"wrote {OUT}/panel.npz", flush=True)


if __name__ == "__main__":
    main()
