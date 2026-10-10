#!/usr/bin/env python3
"""Deployer (delayed-rug) vs aggressive trader EV — side-by-side from ClickHouse + backtest."""
from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime, timezone

OUT = "/opt/EATLQ/mvp/audits/deployer_vs_trader_ev_20260629"
ETH_USD = 2500.0  # conservative spot for $ display
LOOKBACKS = [30, 60, 90, 120, 200]

# ── deployer cost model (Base mainnet, Jun 2026) ──
GAS_DEPLOY_TOKEN = 0.004
GAS_CREATE_POOL = 0.006
GAS_ADD_LIQ = 0.003
GAS_WASH_SWAPS = 0.008  # ~4 self swaps
GAS_REMOVE_LIQ = 0.004
GAS_PER_ATTEMPT = GAS_DEPLOY_TOKEN + GAS_CREATE_POOL + GAS_ADD_LIQ + GAS_WASH_SWAPS + GAS_REMOVE_LIQ

INITIAL_LP_ETH = 5.0  # PARADAENGI-class depth target
DEPLOYER_WASH_ETH = 0.3  # self volume to paint chart (sunk-ish)

# Blacklist decay: after skeleton/symbol flagged, success rate multiplier by attempt #
BLACKLIST_DECAY = [
    (1, 1.00),   # first pool
    (2, 0.85),   # same symbol cluster heat
    (3, 0.55),   # deployer rotation still works
    (4, 0.35),   # symbol+skel known
    (5, 0.15),   # heavily flagged
]

CH_QUERY = """
WITH fs AS (
  SELECT pool_address,
    min(block_time) t0,
    max(block_time) t1,
    toUnixTimestamp(min(block_time)) u0,
    toUnixTimestamp(max(block_time)) u1
  FROM eatlq.uniswap_v3_swaps_decoded
  WHERE chain_id=8453 AND block_time >= now() - INTERVAL {days} DAY
  GROUP BY pool_address
),
sw AS (
  SELECT s.pool_address,
    s.tx_hash,
    toUnixTimestamp(s.block_time) - fs.u0 dt,
    if(s.amount0 > 0, s.amount0 / 1e18, 0) a0in,
    if(s.amount1 > 0, s.amount1 / 1e18, 0) a1in
  FROM eatlq.uniswap_v3_swaps_decoded s
  JOIN fs USING (pool_address)
  WHERE s.chain_id=8453
),
agg AS (
  SELECT pool_address,
    sumIf(a0in, dt <= 600) s0_10,
    sumIf(a1in, dt <= 600) s1_10,
    sumIf(a0in, dt <= 900) s0_15,
    sumIf(a1in, dt <= 900) s1_15,
    sumIf(a0in, dt <= 1800) s0_30,
    sumIf(a1in, dt <= 1800) s1_30,
    countIf(dt <= 600) swaps10,
    countIf(dt <= 1800) swaps30,
    uniqExactIf(tx_hash, dt <= 1800 AND (a0in > 0 OR a1in > 0)) tx30
  FROM sw
  GROUP BY pool_address
),
life AS (
  SELECT pool_address, (u1 - u0) / 60.0 life_min, t0 FROM fs
),
pools AS (
  SELECT
    l.pool_address,
    l.t0,
    l.life_min,
    least(a.s0_10, a.s1_10) weth10,
    least(a.s0_15, a.s1_15) weth15,
    least(a.s0_30, a.s1_30) weth30,
    a.swaps10,
    a.swaps30,
    a.tx30
  FROM life l
  JOIN agg a USING (pool_address)
)
SELECT
  count() AS attempts,
  countIf(weth10 >= 0.1) AS any_activity,
  countIf(weth10 >= 3) AS ge3_10m,
  countIf(weth10 >= 3 AND life_min > 15) AS ge3_surv15,
  countIf(weth10 >= 3 AND life_min > 15 AND swaps10 >= 5) AS ge3_minpre5_surv15,
  countIf(weth10 < 0.1) AS empty_pools,
  round(avgIf(weth30, weth10 >= 3 AND life_min > 15), 4) AS avg_weth30_success,
  round(quantileIf(0.5)(weth30, weth10 >= 3 AND life_min > 15), 4) AS med_weth30,
  round(quantileIf(0.25)(weth30, weth10 >= 3 AND life_min > 15), 4) AS p25_weth30,
  round(quantileIf(0.75)(weth30, weth10 >= 3 AND life_min > 15), 4) AS p75_weth30,
  round(avgIf(tx30, weth10 >= 3 AND life_min > 15), 2) AS avg_buyers_30m,
  countIf(weth10 >= 3 AND life_min > 15 AND life_min <= 120 AND weth15 <= 12) AS rug_like,
  round(quantileIf(0.5)(weth15, weth10 >= 3 AND life_min > 15 AND life_min <= 120 AND weth15 <= 12), 4) AS med_w15_rug_like,
  round(quantileIf(0.5)(weth15, weth10 >= 3 AND life_min > 15 AND weth15 <= 12), 4) AS med_w15_shallow
FROM pools
FORMAT JSON
"""


def ch_json(days: int) -> dict:
    q = CH_QUERY.format(days=days)
    raw = subprocess.check_output(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "-q", q],
        text=True,
    )
    return json.loads(raw)["data"][0]


def deployer_ev_month(
    stats: dict,
    days: int,
    launches_per_day: float,
    extractable_frac: float = 0.55,
) -> dict:
    """Model one deployer launching `launches_per_day` pools.

    extractable_frac: share of external WETH30 you keep after pulling (rest was your LP).
    Conservative: external ≈ weth30 - INITIAL_LP; we use on-chain weth30 distribution.
    """
    attempts_market = int(stats["attempts"])
    ge3_surv15 = int(stats["ge3_surv15"])
    empty = int(stats["empty_pools"])
    med_weth = float(stats["med_weth30"] or 0)
    avg_weth = float(stats["avg_weth30_success"] or 0)

    p_success = ge3_surv15 / max(attempts_market, 1)
    p_empty = empty / max(attempts_market, 1)
    p_fail_gas_only = max(0, 1 - p_success - p_empty)  # some activity but rug failed

    n_launches = launches_per_day * days
    decay_avg = sum(m for _, m in BLACKLIST_DECAY) / len(BLACKLIST_DECAY)

    # Expected successful pulls (with blacklist decay for repeat deployer)
    raw_successes = n_launches * p_success
    adj_successes = raw_successes * decay_avg

    # Profit per success: total WETH at pull ~ weth30; net ≈ weth30 - INITIAL_LP - wash
    # Use median for base, also compute with p25/p75
    def profit_per_success(weth30: float) -> float:
        external = max(0, weth30 - INITIAL_LP_ETH)
        return external - GAS_PER_ATTEMPT - DEPLOYER_WASH_ETH * 0.5

    med_profit = profit_per_success(med_weth)
    p25_profit = profit_per_success(float(stats["p25_weth30"] or 0))
    p75_profit = profit_per_success(float(stats["p75_weth30"] or 0))

    # Failed attempts cost
    fail_gas = n_launches * (1 - p_success) * GAS_PER_ATTEMPT
    # Partial fails: lost wash + small LP bleed
    partial_bleed = n_launches * p_fail_gas_only * (DEPLOYER_WASH_ETH + 0.05)

    gross = adj_successes * med_profit
    net = gross - fail_gas - partial_bleed

    return {
        "days": days,
        "launches_per_day": launches_per_day,
        "market_attempts": attempts_market,
        "market_ge3_surv15": ge3_surv15,
        "p_success_ge3_surv15": round(p_success, 4),
        "p_empty": round(p_empty, 4),
        "med_weth30_success": med_weth,
        "avg_weth30_success": avg_weth,
        "profit_per_success_med_eth": round(med_profit, 4),
        "profit_per_success_p25_eth": round(p25_profit, 4),
        "profit_per_success_p75_eth": round(p75_profit, 4),
        "your_launches": round(n_launches, 1),
        "expected_successes_raw": round(raw_successes, 2),
        "expected_successes_decay_adj": round(adj_successes, 2),
        "monthly_net_eth": round(net / days * 30, 4),
        "monthly_net_usd": round(net / days * 30 * ETH_USD, 2),
        "total_net_eth": round(net, 4),
        "capital_at_risk_eth": INITIAL_LP_ETH,
        "roi_on_capital_monthly_pct": round((net / days * 30) / INITIAL_LP_ETH * 100, 1),
    }


def load_trader_ev() -> dict:
    path = "/opt/EATLQ/mvp/audits/blacklist_framework_recompare_20260629/blacklist_framework_recompare.json"
    with open(path) as f:
        d = json.load(f)
    ag = next(s for s in d["scenarios"] if s["tag"] == "B_aggressive_F_off_TRAIL025")
    pf = next(s for s in d["scenarios"] if s["tag"] == "C_post_fix_20260629_TRAIL025")
    return {
        "aggressive": {
            "monthly_usd": ag["portfolio"]["monthly_usd"],
            "monthly_eth": ag["portfolio"]["monthly_usd"] / ETH_USD,
            "span_days": ag["portfolio"]["span_days"],
            "trades_per_month": ag["portfolio"]["trades_per_month"],
            "fatal_rate": ag["portfolio"]["fatal_rate"],
            "win_rate": ag["portfolio"]["win_rate"],
            "capital_usd": 500,
            "stake_usd": 50,
        },
        "post_fix": {
            "monthly_usd": pf["portfolio"]["monthly_usd"],
            "monthly_eth": pf["portfolio"]["monthly_usd"] / ETH_USD,
        },
        "paradaengi_live": {
            "pools_hit_user": 3,
            "user_loss_eth": 0.144,
            "external_victim_eth": 3.94,
            "profit_per_pool_med_eth": 3.94 / 3,
        },
    }


def main():
    import os

    os.makedirs(OUT, exist_ok=True)
    windows = {}
    for d in LOOKBACKS:
        windows[str(d)] = ch_json(d)

    # Single deployer scenarios: 1, 2, 5 launches/day
    deployer_scenarios = []
    for d in LOOKBACKS:
        st = windows[str(d)]
        for lpd in [1.0, 2.0, 5.0]:
            deployer_scenarios.append(deployer_ev_month(st, d, lpd))

    trader = load_trader_ev()

    # PARADAENGI-calibrated scenario (observed real rug economics)
    para = {
        "attempts_observed": 3,
        "success_rate_if_1_per_week": 3 / max(1, 21),  # 3 hits in ~3 weeks live
        "external_per_success_eth": 3.94 / 3,
        "gas_and_wash_eth": GAS_PER_ATTEMPT + DEPLOYER_WASH_ETH * 0.5,
        "monthly_if_7_launches": None,
    }
    profit = para["external_per_success_eth"] - para["gas_and_wash_eth"]
    para["monthly_if_7_launches"] = round(
        7 * 4 * (3 / 21) * profit * 0.58, 4
    )  # 7 launches/wk, decay 0.58 avg

    report = {
        "meta": {
            "ts": datetime.now(timezone.utc).isoformat(),
            "eth_usd": ETH_USD,
            "gas_per_attempt_eth": GAS_PER_ATTEMPT,
            "initial_lp_eth": INITIAL_LP_ETH,
            "weth_heuristic": "least(sum token0_in, sum token1_in) — WETH side proxy",
            "note": "Trader EV from 644-pool labeled universe backtest; deployer from full Base CH",
        },
        "market_funnel_by_window": windows,
        "deployer_scenarios": deployer_scenarios,
        "paradaengi_calibrated": para,
        "trader_backtest": trader,
        "headline_30d_1launch_per_day": next(
            s for s in deployer_scenarios if s["days"] == 30 and s["launches_per_day"] == 1.0
        ),
    }

    out_json = f"{OUT}/deployer_vs_trader_ev.json"
    with open(out_json, "w") as f:
        json.dump(report, f, indent=2)

    # Print summary table
    print("=" * 72)
    print("MARKET FUNNEL (Base Uniswap V3, WETH-side proxy)")
    print("=" * 72)
    print(f"{'days':>5} {'attempts':>9} {'ge3_10m':>8} {'ge3_s15':>8} {'empty%':>7} {'med_w30':>8}")
    for d in LOOKBACKS:
        s = windows[str(d)]
        att = int(s["attempts"])
        emp = int(s["empty_pools"]) / max(att, 1) * 100
        print(
            f"{d:>5} {att:>9} {int(s['ge3_10m']):>8} {int(s['ge3_surv15']):>8} "
            f"{emp:>6.1f}% {float(s['med_weth30'] or 0):>8.2f}"
        )

    print("\n" + "=" * 72)
    print("DEPLOYER EV (you launch N/day, 5 ETH LP, blacklist decay avg 0.58)")
    print("=" * 72)
    print(f"{'days':>5} {'lpd':>4} {'p_succ':>7} {'$/mo':>10} {'ETH/mo':>8} vs trader")
    ag_mo = trader["aggressive"]["monthly_usd"]
    for s in deployer_scenarios:
        if s["days"] not in (30, 90, 200):
            continue
        ratio = s["monthly_net_usd"] / ag_mo
        print(
            f"{s['days']:>5} {s['launches_per_day']:>4.0f} {s['p_success_ge3_surv15']:>6.1%} "
            f"{s['monthly_net_usd']:>10.0f} {s['monthly_net_eth']:>8.3f}  {ratio:>5.2f}x"
        )

    print("\n" + "=" * 72)
    print("TRADER (aggressive backtest, $500 wallet, $50 stake)")
    print("=" * 72)
    t = trader["aggressive"]
    print(f"  monthly ${t['monthly_usd']:.0f} ({t['monthly_eth']:.3f} ETH @ ${ETH_USD})")
    print(f"  span {t['span_days']}d, {t['trades_per_month']:.1f} trades/mo, fatal {t['fatal_rate']:.1%}")
    print(f"  post-fix PARADAENGI blacklist: ${trader['post_fix']['monthly_usd']:.0f}/mo")

    print(f"\nPARADAENGI observed: {para['external_per_success_eth']:.2f} ETH ext/pool, "
          f"decayed monthly ~{para['monthly_if_7_launches']:.3f} ETH if 7 launches/wk")

    print("\n" + "=" * 72)
    print("RUG-LIKE MARKET (ge3 + surv15 + life<=2h + w15<=12 ETH, PARADAENGI-scale)")
    print("=" * 72)
    for d in LOOKBACKS:
        s = windows[str(d)]
        att = int(s["attempts"])
        rl = int(s.get("rug_like") or 0)
        med = float(s.get("med_w15_rug_like") or 0)
        print(f"  {d:>3}d: {rl:>3} rug-like / {att} attempts ({rl/max(att,1)*100:.2f}%)  med_w15={med:.2f} ETH")

    # Skilled vs average deployer (1 launch/day, 30d/month)
    s200 = windows["200"]
    p_rug = int(s200.get("rug_like") or 0) / max(int(s200["attempts"]), 1)
    med_rug = float(s200.get("med_w15_rug_like") or 6.57)
    ext_med = max(0, med_rug - INITIAL_LP_ETH)
    gas_mo = 30 * GAS_PER_ATTEMPT
    avg_mo = 30 * p_rug * 0.58 * ext_med - gas_mo
    skilled_mo = 30 * 0.10 * 0.58 * para["external_per_success_eth"] - gas_mo
    print(f"\n  Average deployer (market rug-like rate {p_rug:.2%}): ~{avg_mo:.3f} ETH/mo (${avg_mo*ETH_USD:.0f})")
    print(f"  Skilled deployer (10% hit, PARADAENGI ext): ~{skilled_mo:.3f} ETH/mo (${skilled_mo*ETH_USD:.0f})")
    print(f"  Aggressive trader backtest: ~{trader['aggressive']['monthly_eth']:.3f} ETH/mo (${trader['aggressive']['monthly_usd']:.0f})")

    print(f"\nWrote {out_json}")


if __name__ == "__main__":
    main()
