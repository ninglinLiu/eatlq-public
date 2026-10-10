"""Execution-alpha probe: rebuild REAL tradable slippage per pool from full swap data,
then ask whether a subset that can be entered/exited below the break-even friction
threshold still has a positive net-of-slippage beta.

Key idea (model-free): in a single AMM pool, the entire price move between consecutive
swaps IS caused by that swap. So realized impact per WETH is directly observable:
    kappa_i = |p_i / p_{i-1} - 1| / quote_weth_i
We take a per-pool robust kappa (median over real buys), then a $50 clip's one-way
slippage ~= kappa_pool * stake_weth. This replaces the numerically-broken depth columns
(depth_impact_*=5.0 sentinel, depth_weth_to_move_1pct~1e-15).
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .common import OUT, save_json, utcnow
from .v3_common import V3_OUT, ensure_v3_out

WETH_USD = 3000.0
MIN_BUYS_PER_POOL = 5
DUST_WETH = 1e-5
BPS_THRESHOLDS = [50, 100, 178, 300, 600]  # round-trip bps subsets to test


def compute_pool_kappa() -> pd.DataFrame:
    """Per-pool realized price-impact coefficient kappa (fractional price move per WETH)."""
    rs = pd.read_parquet(OUT.parent / "data" / "resolved_swaps.parquet",
                         columns=["pool", "ts", "side", "quote_weth", "price_weth"])
    rs = rs.sort_values(["pool", "ts"]).reset_index(drop=True)
    rs["price_weth"] = pd.to_numeric(rs["price_weth"], errors="coerce")
    rs["quote_weth"] = pd.to_numeric(rs["quote_weth"], errors="coerce").abs()
    rows = []
    for pool, g in rs.groupby("pool", sort=False):
        p = g["price_weth"].to_numpy(dtype=float)
        q = g["quote_weth"].to_numpy(dtype=float)
        side = g["side"].to_numpy()
        if len(g) < 2:
            continue
        prev = p[:-1]
        cur = p[1:]
        with np.errstate(divide="ignore", invalid="ignore"):
            ret = np.abs(cur / prev - 1.0)
        qsz = q[1:]
        sd = side[1:]
        m = (
            np.isfinite(ret) & np.isfinite(qsz) & (qsz > DUST_WETH)
            & (prev > 0) & (cur > 0) & (sd == "buy") & (ret < 5.0)
        )
        if m.sum() < MIN_BUYS_PER_POOL:
            continue
        kappa = ret[m] / qsz[m]
        rows.append({
            "pool": pool,
            "kappa_med": float(np.median(kappa)),
            "kappa_p25": float(np.percentile(kappa, 25)),
            "n_buys_used": int(m.sum()),
        })
    out = pd.DataFrame(rows)
    print(f"[exec] kappa estimated for {len(out):,} pools", flush=True)
    return out


def _load_eligible() -> pd.DataFrame:
    lab = pd.read_parquet(OUT / "panel" / "labeled_wait_absorption.parquet")
    d = lab[(lab["scorable_primary"] == True) & (lab["buy_status"].eq("SUCCESS"))].copy()  # noqa: E712
    d["pnl"] = pd.to_numeric(d["wallet_pnl_weth"], errors="coerce").fillna(0.0)
    d["month"] = pd.to_datetime(d["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m")
    d["token"] = d["token"].astype(str)
    d["pool"] = d["pool"].astype(str)
    # LP fee is in pips (3000 = 0.30%, 10000 = 1.0%). Round-trip fee = 2 * fee/1e6.
    d["fee_frac_roundtrip"] = 2.0 * pd.to_numeric(d.get("fee_bps", 3000), errors="coerce").fillna(3000) / 1e6
    return d


def _subset_stats(d: pd.DataFrame) -> dict[str, Any]:
    pnl = d["pnl_net"].to_numpy()
    by_month = d.groupby("month")["pnl_net"].sum()
    total = float(pnl.sum())
    top_month = float(by_month.max()) if len(by_month) else 0.0
    return {
        "n_trades": int(len(d)),
        "n_pools": int(d["pool"].nunique()),
        "gross_pnl_weth": float(d["pnl"].sum()),
        "net_pnl_weth": total,
        "net_pnl_usd": total * WETH_USD,
        "net_edge_per_trade_usd": float(pnl.mean()) * WETH_USD if len(pnl) else 0.0,
        "win_rate_net": float((pnl > 0).mean()) if len(pnl) else 0.0,
        "n_months_positive": int((by_month > 0).sum()),
        "n_months": int(len(by_month)),
        "positive_without_best_month": bool((total - top_month) > 0),
    }


def run_execution_probe() -> dict[str, Any]:
    ensure_v3_out()
    kap = compute_pool_kappa()
    d = _load_eligible()
    stake = float(d["stake_weth"].iloc[0]) if "stake_weth" in d.columns else 0.0166667

    d = d.merge(kap[["pool", "kappa_med", "n_buys_used"]], on="pool", how="left")
    coverage = float(d["kappa_med"].notna().mean())
    # conservative: pools without a kappa estimate are treated as untradable (dropped)
    d = d[d["kappa_med"].notna()].copy()

    d["oneway_frac"] = d["kappa_med"] * stake
    # total round-trip cost = realized price impact (both legs) + LP fee (both legs)
    d["slip_frac_roundtrip"] = 2.0 * d["oneway_frac"]
    d["roundtrip_bps"] = (d["slip_frac_roundtrip"] + d["fee_frac_roundtrip"]) * 1e4
    d["cost_weth"] = (d["slip_frac_roundtrip"] + d["fee_frac_roundtrip"]) * stake
    d["pnl_net"] = d["pnl"] - d["cost_weth"]

    full = _subset_stats(d.assign())
    subsets = {}
    for bps in BPS_THRESHOLDS:
        sub = d[d["roundtrip_bps"] < bps]
        subsets[f"under_{bps}bps"] = _subset_stats(sub) if len(sub) else {"n_trades": 0}

    rt = d["roundtrip_bps"]
    dist = {
        "coverage_pools_with_kappa": coverage,
        "cost_includes": "realized_price_impact_both_legs + LP_fee_both_legs",
        "roundtrip_bps_p10": float(rt.quantile(0.10)),
        "roundtrip_bps_p50": float(rt.median()),
        "roundtrip_bps_p90": float(rt.quantile(0.90)),
        "fee_only_roundtrip_bps_p50": float((d["fee_frac_roundtrip"] * 1e4).median()),
        "slippage_only_roundtrip_bps_p50": float((d["slip_frac_roundtrip"] * 1e4).median()),
        "frac_under_178bps": float((rt < 178).mean()),
        "frac_under_100bps": float((rt < 100).mean()),
    }

    # Verdict: does a tradable subset exist that is (a) net-positive, (b) has positive
    # per-trade edge after its own slippage, (c) stays positive dropping its best month,
    # and (d) is non-trivial in size?
    best_key, best = None, None
    for bps in [100, 178, 300]:
        s = subsets[f"under_{bps}bps"]
        if s.get("n_trades", 0) >= 500 and s.get("net_pnl_weth", -1) > 0 and \
           s.get("net_edge_per_trade_usd", -1) > 0 and s.get("positive_without_best_month", False):
            best_key, best = f"under_{bps}bps", s
            break

    verdict = "TRADABLE_SUBSET_FOUND" if best else "NO_TRADABLE_SUBSET"

    report = {
        "task": "EXECUTION_ALPHA_PROBE",
        "generated_at": utcnow(),
        "method": "model_free_realized_impact_per_weth (fixes broken depth columns)",
        "stake_weth": stake, "stake_usd": stake * WETH_USD,
        "distribution": dist,
        "full_universe_net_of_slippage": full,
        "subsets_by_roundtrip_bps": subsets,
        "verdict": verdict,
        "tradable_subset_key": best_key,
        "interpretation": (
            "If NO_TRADABLE_SUBSET: even after cherry-picking only the deepest pools, once you "
            "charge each trade its own realized round-trip slippage the net edge is gone — the "
            "beta is real but not capturable at $50 clips. TRADABLE_SUBSET_FOUND would mean a "
            "depth-filtered slice survives and is worth a forward shadow."
        ),
    }
    save_json(V3_OUT / "execution_probe.json", report)
    kap.to_csv(V3_OUT / "pool_kappa.csv", index=False)
    _write_md(report)
    print(f"[exec] verdict={verdict} coverage={coverage:.1%}", flush=True)
    return report


def _write_md(r: dict) -> None:
    dist = r["distribution"]
    full = r["full_universe_net_of_slippage"]
    lines = [
        "# EXECUTION_ALPHA_PROBE — 可成交滑点重建 + 可交易子集",
        "",
        f"Generated: `{r['generated_at']}`  |  方法: `{r['method']}`",
        "",
        f"## 总裁决: **{r['verdict']}**",
        "",
        "## 真实往返成本分布 ($50 clip, 含 LP 手续费 + 实测冲击)",
        "",
        f"- 覆盖(有 kappa 估计的池): `{dist['coverage_pools_with_kappa']:.1%}`",
        f"- 往返总成本 p10/p50/p90: `{dist['roundtrip_bps_p10']:.0f}` / `{dist['roundtrip_bps_p50']:.0f}` / `{dist['roundtrip_bps_p90']:.0f}` bps",
        f"  - 其中 LP 手续费中位 `{dist['fee_only_roundtrip_bps_p50']:.0f}` bps + 实测滑点中位 `{dist['slippage_only_roundtrip_bps_p50']:.0f}` bps",
        f"- 低于 178bps 的比例: `{dist['frac_under_178bps']:.1%}`；低于 100bps: `{dist['frac_under_100bps']:.1%}`",
        "",
        "## net-of-slippage 结果（全体可估池）",
        "",
        f"- 交易数 `{full['n_trades']:,}`，毛 PnL `{full['gross_pnl_weth']:.1f}` WETH → **净 PnL `{full['net_pnl_weth']:.1f}` WETH (${full['net_pnl_usd']:.0f})**",
        f"- 每笔净边际: **${full['net_edge_per_trade_usd']:.2f}**，净胜率 `{full['win_rate_net']:.1%}`",
        "",
        "## 按往返滑点阈值筛子集 (net-of-slippage)",
        "",
        "| 阈值 | 笔数 | 净PnL(USD) | 每笔净边际(USD) | 正月/总月 | 去掉最好月仍正 |",
        "| --- | ---: | ---: | ---: | :--: | :--: |",
    ]
    for bps in BPS_THRESHOLDS:
        s = r["subsets_by_roundtrip_bps"][f"under_{bps}bps"]
        if s.get("n_trades", 0) == 0:
            lines.append(f"| <{bps}bps | 0 | - | - | - | - |")
        else:
            lines.append(
                f"| <{bps}bps | {s['n_trades']:,} | {s['net_pnl_usd']:.0f} | {s['net_edge_per_trade_usd']:.2f} | "
                f"{s['n_months_positive']}/{s['n_months']} | {s['positive_without_best_month']} |"
            )
    lines += ["", "## 含义", "", r["interpretation"], ""]
    (V3_OUT / "EXECUTION_ALPHA_PROBE.md").write_text("\n".join(lines))
    (V3_OUT.parent.parent / "EXECUTION_ALPHA_PROBE.md").write_text("\n".join(lines))


if __name__ == "__main__":
    run_execution_probe()
