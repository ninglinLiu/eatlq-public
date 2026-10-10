"""Beta health-check: is the "spray fresh pools" regime a real, tradable, persistent
edge — or window luck / ideal-fill mirage?

Three lenses on the eligible opportunity population (per-trade net PnL):
  1. Right-tail concentration + win/payoff profile
  2. Monthly stability + leave-one-out (month / token)
  3. Realistic-friction stress (entry slippage, sellability haircut) + break-even

Unit = eligible successful scorable buys (loss-truth labels). This characterizes the
signal *population*; capacity scheduling is a separate (already-studied) constraint.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .common import OUT, save_json, utcnow
from .v3_common import V3_OUT, ensure_v3_out

# Preregistered friction grid (do not tune to a desired answer).
# NOTE: on-chain depth columns (depth_impact_*, depth_weth_to_move_1pct) are numerically
# BROKEN in the panel (saturated sentinel 5.0 / ~1e-15 units), so we model friction with
# defensible flat parametric costs instead of those columns.
GAIN_HAIRCUTS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
ROUNDTRIP_SLIPPAGE_BPS = [0, 50, 100, 200, 300, 600]  # applied to notional on both legs
EXTRA_GAS_USD = [0.0, 1.0, 3.0]  # extra MEV/gas per trade
WETH_USD = 3000.0


def _load_eligible() -> pd.DataFrame:
    lab = pd.read_parquet(OUT / "panel" / "labeled_wait_absorption.parquet")
    d = lab[(lab["scorable_primary"] == True) & (lab["buy_status"].eq("SUCCESS"))].copy()  # noqa: E712
    d["pnl"] = pd.to_numeric(d["wallet_pnl_weth"], errors="coerce").fillna(0.0)
    d["day"] = pd.to_datetime(d["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    d["month"] = pd.to_datetime(d["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m")
    d["token"] = d["token"].astype(str)
    return d


def right_tail_profile(d: pd.DataFrame) -> dict[str, Any]:
    pnl = d["pnl"].to_numpy()
    total = float(pnl.sum())
    pos = pnl[pnl > 0]
    neg = pnl[pnl < 0]
    gross_pos = float(pos.sum())
    n = len(pnl)
    srt = np.sort(pnl)[::-1]  # descending

    def top_share(frac: float) -> dict:
        k = max(1, int(n * frac))
        top_sum = float(srt[:k].sum())
        rest_sum = float(srt[k:].sum())
        return {
            "k": k,
            "top_pnl_sum": top_sum,
            "share_of_total": float(top_sum / total) if total != 0 else None,
            "total_without_top": rest_sum,
            "still_positive_without_top": bool(rest_sum > 0),
        }

    return {
        "n_trades": n,
        "total_pnl_weth": total,
        "win_rate": float((pnl > 0).mean()),
        "mean_trade_weth": float(pnl.mean()),
        "median_trade_weth": float(np.median(pnl)),
        "avg_win_weth": float(pos.mean()) if len(pos) else 0.0,
        "avg_loss_weth": float(neg.mean()) if len(neg) else 0.0,
        "payoff_ratio_win_over_loss": float(abs(pos.mean() / neg.mean())) if len(neg) and neg.mean() != 0 else None,
        "gross_positive_weth": gross_pos,
        "gross_negative_weth": float(neg.sum()),
        "top_0p1pct": top_share(0.001),
        "top_1pct": top_share(0.01),
        "top_5pct": top_share(0.05),
        "interpretation": (
            "If total flips negative after removing top 1% winners, the regime is a lottery "
            "(fat-tail dependent), not a robust harvestable beta."
        ),
    }


def _loo_sign_flip(pnl_by_unit: pd.Series) -> dict[str, Any]:
    total = float(pnl_by_unit.sum())
    if len(pnl_by_unit) == 0:
        return {"n_units": 0, "worst_drop_still_positive": None}
    ranked = pnl_by_unit.sort_values(ascending=False)
    top = float(ranked.iloc[0])
    without_top = total - top
    flips = 0
    for u in ranked.values:
        if total != 0 and (total > 0) != ((total - float(u)) > 0):
            flips += 1
    return {
        "n_units": int(len(pnl_by_unit)),
        "total": total,
        "top_unit_pnl": top,
        "total_without_top_unit": without_top,
        "still_positive_without_top_unit": bool(without_top > 0),
        "n_units_whose_removal_flips_sign": int(flips),
    }


def stability_profile(d: pd.DataFrame) -> dict[str, Any]:
    by_month = d.groupby("month")["pnl"].sum()
    by_token = d.groupby("token")["pnl"].sum()
    return {
        "n_months": int(len(by_month)),
        "n_months_positive": int((by_month > 0).sum()),
        "frac_months_positive": float((by_month > 0).mean()) if len(by_month) else 0.0,
        "monthly_pnl_weth": {m: float(v) for m, v in by_month.items()},
        "loo_month": _loo_sign_flip(by_month),
        "loo_token": _loo_sign_flip(by_token),
        "n_tokens": int(d["token"].nunique()),
        "interpretation": (
            "A real beta should stay positive across most months and survive removing its single "
            "best month/token. If one month or token carries the sign, it is window luck."
        ),
    }


def friction_stress(d: pd.DataFrame) -> dict[str, Any]:
    stake = float(d["stake_weth"].iloc[0]) if "stake_weth" in d.columns and len(d) else 0.0166667
    stake_usd = stake * WETH_USD
    pnl = d["pnl"].to_numpy()
    censored = d.get("censored", pd.Series(False, index=d.index)).astype(bool).to_numpy()
    n = len(pnl)

    def total_under(slip_bps: float, gas_usd: float, haircut: float) -> float:
        adj = pnl.copy()
        gain = adj > 0
        adj[gain] = adj[gain] * (1.0 - haircut)
        adj[gain & censored] = np.minimum(adj[gain & censored], 0.0)
        # round-trip slippage on notional (both legs) + extra gas, converted to WETH
        slip_weth = 2.0 * stake * (slip_bps / 10000.0)
        gas_weth = gas_usd / WETH_USD
        adj = adj - slip_weth - gas_weth
        return float(adj.sum())

    grid = []
    for slip in ROUNDTRIP_SLIPPAGE_BPS:
        for gas in EXTRA_GAS_USD:
            for h in GAIN_HAIRCUTS:
                tot = total_under(slip, gas, h)
                grid.append({
                    "slippage_bps": slip, "extra_gas_usd": gas, "gain_haircut": h,
                    "total_pnl_weth": tot, "total_pnl_usd": tot * WETH_USD, "positive": bool(tot > 0),
                })

    # break-even slippage (no haircut, no extra gas) — the purest friction axis
    slip_curve = [
        {"slippage_bps": s, "total_pnl_usd": total_under(s, 0.0, 0.0) * WETH_USD, "positive": total_under(s, 0.0, 0.0) > 0}
        for s in [0, 50, 100, 150, 178, 200, 300, 600]
    ]
    breakeven_slippage_bps = next((r["slippage_bps"] for r in slip_curve if not r["positive"]), None)

    # net edge per trade
    net_edge_usd = float(pnl.mean()) * WETH_USD

    # break-even gain haircut at a moderate real-world friction floor (100bps + $1 gas)
    hc_curve = [
        {"gain_haircut": h, "total_pnl_usd": total_under(100, 1.0, h) * WETH_USD, "positive": total_under(100, 1.0, h) > 0}
        for h in GAIN_HAIRCUTS
    ]
    breakeven_haircut = next((r["gain_haircut"] for r in hc_curve if not r["positive"]), None)

    return {
        "stake_usd": stake_usd,
        "net_edge_per_trade_usd": net_edge_usd,
        "breakeven_roundtrip_slippage_bps": breakeven_slippage_bps,
        "slippage_curve": slip_curve,
        "breakeven_gain_haircut_at_100bps_1usdgas": breakeven_haircut,
        "haircut_curve_at_moderate_friction": hc_curve,
        "grid": grid,
        "data_quality_note": (
            "On-chain depth columns are broken (depth_impact_*=5.0 sentinel; "
            "depth_weth_to_move_1pct~1e-15), so v1/v2/v3 'real V3 depth control' was effectively inert. "
            "Friction here is flat-parametric, not depth-derived."
        ),
        "interpretation": (
            f"Net edge is only ~${net_edge_usd:.2f} per ${stake_usd:.0f} trade. break-even round-trip "
            "slippage tells you how many bps of real fill cost wipes the edge; memecoin fresh-pool "
            "round-trips routinely exceed that."
        ),
    }


def run_beta_healthcheck() -> dict[str, Any]:
    ensure_v3_out()
    d = _load_eligible()
    print(f"[beta] eligible trades={len(d):,}", flush=True)

    tail = right_tail_profile(d)
    stab = stability_profile(d)
    fric = friction_stress(d)

    # Verdicts
    tail_ok = bool(tail["top_1pct"]["still_positive_without_top"])
    stab_ok = bool(
        stab["frac_months_positive"] >= 0.6
        and stab["loo_month"]["still_positive_without_top_unit"]
        and stab["loo_token"]["still_positive_without_top_unit"]
    )
    be_slip = fric["breakeven_roundtrip_slippage_bps"]
    friction_ok = bool(be_slip is None or be_slip >= 300)  # survives >=300bps round-trip friction

    if tail_ok and stab_ok and friction_ok:
        verdict = "BETA_REAL_BUT_HIGH_VARIANCE"
    elif not friction_ok:
        verdict = "BETA_DIES_UNDER_FRICTION"
    elif not tail_ok:
        verdict = "BETA_FRAGILE_FAT_TAIL_LOTTERY"
    elif not stab_ok:
        verdict = "BETA_FRAGILE_WINDOW_LUCK"
    else:
        verdict = "BETA_INCONCLUSIVE"

    report = {
        "task": "BETA_HEALTHCHECK",
        "generated_at": utcnow(),
        "unit": "eligible_successful_scorable_buys_per_trade_net_pnl",
        "right_tail": tail,
        "stability": stab,
        "friction": fric,
        "component_verdicts": {
            "SURVIVES_REMOVING_TOP_1PCT": "PASS" if tail_ok else "FAIL",
            "MONTHLY_AND_LOO_STABLE": "PASS" if stab_ok else "FAIL",
            "SURVIVES_REALISTIC_FRICTION": "PASS" if friction_ok else "FAIL",
        },
        "verdict": verdict,
        "tradable_conclusion": (
            "Regime is a positive but fat-tailed/fragile long-new-pools beta; if any component FAILs, "
            "the ideal-fill backtest PnL is not a dependable live edge."
        ),
    }
    save_json(V3_OUT / "beta_healthcheck.json", report)
    _write_md(report)
    print(f"[beta] verdict={verdict}", flush=True)
    return report


def _write_md(r: dict) -> None:
    t = r["right_tail"]
    s = r["stability"]
    f = r["friction"]
    lines = [
        "# BETA_HEALTHCHECK — 广撒网新池 beta 体检",
        "",
        f"Generated: `{r['generated_at']}`",
        "",
        f"## 总裁决: **{r['verdict']}**",
        "",
        "| 组件 | 结果 |",
        "| --- | --- |",
    ]
    for k, v in r["component_verdicts"].items():
        lines.append(f"| `{k}` | **{v}** |")
    lines += [
        "",
        "## 1. 右尾集中度 / 胜率赔率",
        "",
        f"- 交易数: `{t['n_trades']:,}`，总 PnL: `{t['total_pnl_weth']:.3f}` WETH",
        f"- 胜率: `{t['win_rate']:.1%}`，平均盈/亏: `{t['avg_win_weth']:.5f}` / `{t['avg_loss_weth']:.5f}`，赔率: `{t['payoff_ratio_win_over_loss']}`",
        f"- Top 1% 占总盈利: `{t['top_1pct']['share_of_total']}`；去掉 top1% 后总额: `{t['top_1pct']['total_without_top']:.3f}` → 仍正? **{t['top_1pct']['still_positive_without_top']}**",
        f"- Top 0.1% 占比: `{t['top_0p1pct']['share_of_total']}`；去掉后仍正? **{t['top_0p1pct']['still_positive_without_top']}**",
        "",
        "## 2. 稳定性 / LOO",
        "",
        f"- 有数据月: `{s['n_months']}`，其中正月: `{s['n_months_positive']}` (`{s['frac_months_positive']:.0%}`)",
        f"- 去掉最好一个月后仍正? **{s['loo_month']['still_positive_without_top_unit']}** (best month PnL `{s['loo_month']['top_unit_pnl']:.3f}`)",
        f"- 去掉最好一个 token 后仍正? **{s['loo_token']['still_positive_without_top_unit']}** (best token PnL `{s['loo_token']['top_unit_pnl']:.3f}`, tokens `{s['n_tokens']}`)",
        "",
        "## 3. 真实摩擦压力",
        "",
        f"> 数据质量: {f['data_quality_note']}",
        "",
        f"- 每笔净边际: **${f['net_edge_per_trade_usd']:.2f}** / 每 ${f['stake_usd']:.0f} 仓位",
        f"- 盈亏平衡往返滑点: **{f['breakeven_roundtrip_slippage_bps']} bps** (超过即转亏)",
    ]
    for row in f["slippage_curve"]:
        lines.append(
            f"  - 滑点 `{row['slippage_bps']} bps` → `{row['total_pnl_usd']:.0f}` USD ({'正' if row['positive'] else '负'})"
        )
    lines += [
        "",
        f"- 中等摩擦(100bps+$1 gas)下盈亏平衡赢家折价率: **{f['breakeven_gain_haircut_at_100bps_1usdgas']}**",
    ]
    lines += [
        "",
        "## 含义",
        "",
        r["tradable_conclusion"],
        "",
    ]
    (V3_OUT / "BETA_HEALTHCHECK.md").write_text("\n".join(lines))
    (V3_OUT.parent.parent / "BETA_HEALTHCHECK.md").write_text("\n".join(lines))


if __name__ == "__main__":
    run_beta_healthcheck()
