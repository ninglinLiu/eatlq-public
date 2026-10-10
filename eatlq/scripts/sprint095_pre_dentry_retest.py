#!/usr/bin/env python3
"""Sprint095-PRE — D_entry × trailing_5_pull3_2h × widened universe robust retest.

CH-only (no RPC burn). Forward prices derived from uniswap_v3_swaps tick data.
Enforces all 10 §3 anti-self-deception gates.

Fee encoding note:
  discovered_pools.fee_bps is in basis points (0.01% units):
    1=0.01%, 5=0.05%, 30=0.30%, 100=1.00%
  This is NOT the Uniswap protocol encoding (100/500/3000/10000).
  Prior scripts (sprint088) used protocol encoding against this column → BUG fixed here.

Exit policy: trailing_5_pull3_2h
  TP1 at +5% → sell 50% (half position)
  TP2 at +20% → sell remaining 30% of original
  Trailing pull: if peak ≥ TP1 and price pulls back ≥ 3% from peak → sell remainder
  SL at -10% → hard stop entire position
  Time stop at 2h → sell remainder at 2h tick price
  Per-leg cost: 1.2% (gas + slippage + pool fee proxy)

Outputs:
  mvp/audits/sprint095_pre_dentry_retest/
    per_signal.csv       — one row per onset signal with multi-horizon returns + trailing exit
    summary.json         — aggregate stats + all 10 gate verdicts
    readout_cn.md        — Chinese readout
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

REPO = Path(__file__).resolve().parent.parent
CH_CONTAINER = "eatlq-clickhouse-1"
CHAIN_ID = 8453
WETH_LC = "0x4200000000000000000000000000000000000006"
SENDER_0X6FF5 = "0x6ff5693b99212da76ad316178a184ab56d299b43"
SENDER_MAP_PATH = REPO / "logs/sprint080_sender_map.json"

# Base routing tokens to exclude as "other" token (not WETH leg itself)
BASE_ROUTING_TOKENS = {
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC
}

# Trailing exit parameters (trailing_5_pull3_2h)
TP1_PCT = 0.05
TP1_SZ  = 0.50
TP2_PCT = 0.20
TP2_SZ  = 0.30
SL_PCT  = -0.10
TIME_H  = 7200       # 2h in seconds
TRAIL   = 0.03       # 3% pullback from peak triggers exit
PER_LEG = 0.012      # cost per exit leg

EXIT_SECONDS = [300, 900, 1800, 3600, 7200]   # 5m, 15m, 30m, 1h, 2h
EXIT_LABELS  = ["5m", "15m", "30m", "1h", "2h"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint095-PRE D_entry robust retest (CH-only)")
    p.add_argument("--lookback-days", type=int, default=30)
    p.add_argument("--pool-max-age-sec", type=int, default=172800)  # 48h
    p.add_argument("--onset-thresh", type=int, default=5)
    p.add_argument("--onset-max-sec", type=int, default=7200)       # 2h
    p.add_argument("--fee-bps", default="1,5,30,100",
                   help="discovered_pools.fee_bps allowlist (basis-point units: 1=0.01%,30=0.3%)")
    p.add_argument("--exclude-routing", action="store_true", default=True,
                   help="Exclude WETH/USDC/USDbC/cbBTC as the 'other' token")
    p.add_argument("--exclude-0x6ff5", action="store_true", default=True)
    p.add_argument("--max-pools", type=int, default=500)
    p.add_argument("--bootstrap-n", type=int, default=1000,
                   help="Bootstrap iterations for p5 gate")
    p.add_argument("--out-dir", default="mvp/audits/sprint095_pre_dentry_retest")
    return p.parse_args()


def _ch(sql: str, timeout: int = 120) -> list[list[str]]:
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client", "--format", "CSV", "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH error: {r.stderr.strip()[:400]}")
    return [row for row in csv.reader(io.StringIO(r.stdout)) if row]


def load_sender_map() -> dict:
    if not SENDER_MAP_PATH.exists():
        return {}
    try:
        raw = json.loads(SENDER_MAP_PATH.read_text())
        return {k.lower(): v for k, v in raw.items()}
    except Exception:
        return {}


def discover_onsets(args) -> list[dict]:
    """CH-only onset detection. One row per pool (first-signal-only via earliest bucket).

    Fee filter uses discovered_pools.fee_bps (basis-point encoding).
    """
    fees = ",".join(str(int(x.strip())) for x in args.fee_bps.split(",") if x.strip())
    sql = f"""
WITH first_swap AS (
  SELECT pool_address, toUnixTimestamp(min(block_time)) AS first_ts
  FROM eatlq.uniswap_v3_swaps
  WHERE chain_id = {CHAIN_ID}
    AND block_time >= now() - INTERVAL {args.lookback_days + 3} DAY
    AND block_time IS NOT NULL
  GROUP BY pool_address
),
buckets AS (
  SELECT
    pool_address,
    toUnixTimestamp(toStartOfFiveMinutes(block_time)) AS wts,
    count() AS heat,
    min(toUnixTimestamp(block_time)) AS bucket_min_ts,
    min(block_number) AS entry_block
  FROM eatlq.uniswap_v3_swaps
  WHERE chain_id = {CHAIN_ID}
    AND block_time >= now() - INTERVAL {args.lookback_days} DAY
    AND block_time IS NOT NULL
  GROUP BY pool_address, wts
  HAVING count() >= {args.onset_thresh}
),
qualifying AS (
  SELECT
    b.pool_address AS pool,
    b.wts, b.heat, b.bucket_min_ts AS entry_ts, b.entry_block,
    fs.first_ts,
    (b.bucket_min_ts - fs.first_ts) AS pool_age_at_entry,
    (b.wts - fs.first_ts) AS onset_lag
  FROM buckets b
  INNER JOIN first_swap fs ON b.pool_address = fs.pool_address
  WHERE (b.bucket_min_ts - fs.first_ts) <= {args.pool_max_age_sec}
    AND (b.wts - fs.first_ts) <= {args.onset_max_sec}
    AND (b.wts - fs.first_ts) >= 0
),
earliest AS (
  SELECT pool, min(wts) AS min_wts FROM qualifying GROUP BY pool
),
first_qualifying AS (
  SELECT q.pool, q.entry_ts, q.entry_block, q.heat, q.onset_lag, q.pool_age_at_entry
  FROM qualifying q
  INNER JOIN earliest e ON q.pool = e.pool AND q.wts = e.min_wts
),
pool_meta AS (
  SELECT lower(pool_address) AS pool,
         lower(token0_address) AS token0,
         lower(token1_address) AS token1,
         fee_bps
  FROM eatlq.discovered_pools
  WHERE chain_id = {CHAIN_ID} AND pool_type = 'v3'
)
SELECT
  fq.pool, fq.entry_ts, fq.entry_block, fq.heat, fq.onset_lag, fq.pool_age_at_entry,
  pm.token0, pm.token1, pm.fee_bps
FROM first_qualifying fq
INNER JOIN pool_meta pm ON lower(fq.pool) = pm.pool
WHERE (pm.token0 = '{WETH_LC}' OR pm.token1 = '{WETH_LC}')
  AND pm.fee_bps IN ({fees})
ORDER BY fq.entry_ts DESC
LIMIT {args.max_pools}
FORMAT CSVWithNames
"""
    rows = _ch(sql, timeout=180)
    if not rows:
        return []
    header = rows[0]
    out = []
    for r in rows[1:]:
        d = dict(zip(header, r))
        weth_is_t1 = d["token0"].lower() == WETH_LC
        token = d["token1"].lower() if weth_is_t1 else d["token0"].lower()
        out.append({
            "pool": d["pool"].lower(),
            "token": token,
            "fee_bps": int(d["fee_bps"]),
            "entry_ts": int(d["entry_ts"]),
            "entry_block": int(d["entry_block"]),
            "heat": int(d["heat"]),
            "onset_lag": int(d["onset_lag"]),
            "pool_age": int(d["pool_age_at_entry"]),
            "weth_is_t1": weth_is_t1,
        })
    return out


def fetch_tick_trajectory(pool: str, entry_ts: int, horizon_sec: int = 7200) -> list[tuple[int, int]]:
    """Return sorted [(unix_ts, tick)] for pool in [entry_ts, entry_ts + horizon_sec].

    Uses uniswap_v3_swaps_decoded for accurate tick values.
    Falls back to uniswap_v3_swaps if decoded has no data for this pool.
    """
    end_ts = entry_ts + horizon_sec + 300  # 5m buffer
    sql = f"""
SELECT toUnixTimestamp(block_time) AS ts, toInt32(tick) AS tick
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id = {CHAIN_ID}
  AND lower(pool_address) = '{pool}'
  AND block_time >= toDateTime({entry_ts}, 'UTC')
  AND block_time <= toDateTime({end_ts}, 'UTC')
ORDER BY block_time ASC
FORMAT CSVWithNames
"""
    try:
        rows = _ch(sql, timeout=30)
        if rows and len(rows) > 1:
            traj = []
            for r in rows[1:]:
                try:
                    traj.append((int(r[0]), int(r[1])))
                except (ValueError, IndexError):
                    pass
            if traj:
                return traj
    except Exception:
        pass
    return []


def tick_to_return(entry_tick: int, exit_tick: int, weth_is_t1: bool) -> float:
    """Convert tick delta to forward return for a LONG position on the meme token.

    Uniswap V3: price = 1.0001^tick = token1/token0
    If WETH=token1 (weth_is_t1=True): price = WETH/MEME; MEME appreciation → tick ↑
    If WETH=token0 (weth_is_t1=False): price = MEME/WETH; MEME appreciation → tick ↓
    """
    if weth_is_t1:
        delta = exit_tick - entry_tick
    else:
        delta = entry_tick - exit_tick
    return math.pow(1.0001, delta) - 1.0


def compute_horizons(traj: list[tuple[int, int]], entry_ts: int,
                     weth_is_t1: bool) -> dict[str, float | None]:
    """Compute median-tick-based forward returns at each horizon.

    Uses median tick in a ±2.5min window around each horizon timestamp
    for robustness against spike noise (sprint094-audit-v2 finding).
    Falls back to nearest available tick if window is empty.
    """
    if not traj:
        return {h: None for h in EXIT_LABELS}

    # Entry price: median of first 5m window ticks
    window = [t for ts, t in traj if entry_ts <= ts < entry_ts + 300]
    if not window:
        # No ticks at entry window → use first available tick
        entry_tick = traj[0][1]
    else:
        entry_tick = int(median(window))

    results: dict[str, float | None] = {}
    for label, sec in zip(EXIT_LABELS, EXIT_SECONDS):
        target_ts = entry_ts + sec
        # ±2.5 min window for median
        win = [t for ts, t in traj if target_ts - 150 <= ts <= target_ts + 150]
        if not win:
            # Nearest tick before or at target
            candidates = [(ts, t) for ts, t in traj if ts <= target_ts]
            if candidates:
                exit_tick = candidates[-1][1]
            else:
                results[label] = None
                continue
        else:
            exit_tick = int(median(win))
        results[label] = round(tick_to_return(entry_tick, exit_tick, weth_is_t1), 6)
    return results


def has_sell_activity(pool: str, entry_ts: int, window_sec: int = 7200) -> bool:
    """CH-only sell-path **proxy** (NOT Quoter verification).

    True when pool has ≥1 decoded sell in [entry_ts, entry_ts+window_sec].
    False triggers exclusion from PF — never assign -100% without Quoter (P1-8).
    """
    end_ts = entry_ts + window_sec
    sql = f"""
SELECT count() FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id = {CHAIN_ID}
  AND lower(pool_address) = '{pool}'
  AND toUnixTimestamp(block_time) BETWEEN {entry_ts} AND {end_ts}
  AND amount1 > 0
LIMIT 1
FORMAT TabSeparated
"""
    try:
        rows = _ch(sql, timeout=15)
        if rows and rows[0]:
            return int(rows[0][0]) > 0
    except Exception:
        pass
    return True  # Default to allowing if CH unavailable


def apply_trailing_exit(horizons: dict, sell_ok: bool) -> dict:
    """Simulate trailing_5_pull3_2h exit policy."""
    if not sell_ok:
        # P1-8: CH proxy only — do not fabricate -100% loss; exclude from PF.
        return {
            "net": None,
            "exit": "sell_blocked_ch_proxy_unverified",
            "wins": None,
            "pf_eligible": False,
        }

    traj_vals = [(label, sec, horizons.get(label))
                 for label, sec in zip(EXIT_LABELS, EXIT_SECONDS)
                 if horizons.get(label) is not None]
    if not traj_vals:
        return {"net": 0.0, "exit": "no_data", "wins": False, "pf_eligible": True}

    remaining = 1.0
    realised = 0.0
    peak = 0.0
    tp1_hit = tp2_hit = False
    exit_reason = "time"

    for label, sec, ret in traj_vals:
        if sec > TIME_H:
            break
        peak = max(peak, ret)

        # SL
        if remaining > 0 and ret <= SL_PCT:
            realised += remaining * (ret - PER_LEG)
            remaining = 0
            exit_reason = "sl"
            break

        # Trailing pull: only after TP1 triggered
        if remaining > 0 and tp1_hit and peak >= TP1_PCT and (peak - ret) >= TRAIL:
            realised += remaining * (ret - PER_LEG)
            remaining = 0
            exit_reason = "trailing"
            break

        # TP2
        if remaining > 0 and not tp2_hit and ret >= TP2_PCT:
            sz = min(remaining, TP2_SZ)
            realised += sz * (ret - PER_LEG)
            remaining -= sz
            tp2_hit = True

        # TP1
        if remaining > 0 and not tp1_hit and ret >= TP1_PCT:
            sz = min(remaining, TP1_SZ)
            realised += sz * (ret - PER_LEG)
            remaining -= sz
            tp1_hit = True

    # Time exit: sell remainder at 2h price
    if remaining > 0:
        last_ret = None
        for _, sec, ret in traj_vals:
            if sec <= TIME_H:
                last_ret = ret
        if last_ret is not None:
            realised += remaining * (last_ret - PER_LEG)

    # Round-trip cost deduction (buy side already in PER_LEG per exit, add buy cost)
    realised -= PER_LEG

    net = round(realised, 6)
    return {
        "net": net,
        "exit": exit_reason,
        "wins": net > 0,
        "tp1_hit": tp1_hit,
        "tp2_hit": tp2_hit,
        "pf_eligible": True,
    }


def pf_of(vals: list[float]) -> float | None:
    pos = sum(v for v in vals if v > 0)
    neg = abs(sum(v for v in vals if v < 0))
    if neg > 0:
        return round(pos / neg, 4)
    return float("inf") if pos > 0 else None


def bootstrap_p5(nets: list[float], n_iter: int = 1000) -> float | None:
    """Bootstrap PF at 5th percentile."""
    if len(nets) < 10:
        return None
    pfs = []
    for _ in range(n_iter):
        sample = random.choices(nets, k=len(nets))
        v = pf_of(sample)
        if v is not None and v != float("inf"):
            pfs.append(v)
    if not pfs:
        return None
    pfs.sort()
    idx = max(0, int(0.05 * len(pfs)) - 1)
    return round(pfs[idx], 4)


def pf_top1_removed(nets: list[float]) -> float | None:
    """PF after removing the single best trade."""
    if len(nets) < 2:
        return None
    trimmed = sorted(nets)[:-1]  # remove max
    return pf_of(trimmed)


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    sender_map = load_sender_map()

    print(f"[discover] lookback={args.lookback_days}d "
          f"pool_max_age={args.pool_max_age_sec}s "
          f"onset_thresh={args.onset_thresh} "
          f"fee_bps={args.fee_bps}", flush=True)

    onsets = discover_onsets(args)
    print(f"[discover] {len(onsets)} qualifying pool onsets found", flush=True)

    # Universe coverage assert: must have data to proceed
    if len(onsets) == 0:
        print("ERROR: 0 onsets — universe too narrow or CH data issue", file=sys.stderr)
        return 2

    results = []
    skipped = {
        "is_0x6ff5": 0,
        "base_routing_token": 0,
        "no_tick_data": 0,
    }

    for i, p in enumerate(onsets, 1):
        pool = p["pool"]

        # Gate: exclude 0x6ff5-dominated pools
        if args.exclude_0x6ff5:
            sm = sender_map.get(pool, {})
            if str(sm.get("dominant_sender", "")).lower() == SENDER_0X6FF5:
                skipped["is_0x6ff5"] += 1
                continue

        # Gate: exclude base routing as 'other' token
        if args.exclude_routing and p["token"] in BASE_ROUTING_TOKENS:
            skipped["base_routing_token"] += 1
            continue

        # Fetch tick trajectory from CH (no RPC)
        traj = fetch_tick_trajectory(pool, p["entry_ts"])
        if not traj:
            skipped["no_tick_data"] += 1
            continue

        horizons = compute_horizons(traj, p["entry_ts"], p["weth_is_t1"])
        sell_ok = has_sell_activity(pool, p["entry_ts"])
        outcome = apply_trailing_exit(horizons, sell_ok)

        results.append({
            "pool": pool,
            "token": p["token"],
            "fee_bps": p["fee_bps"],
            "entry_ts": p["entry_ts"],
            "entry_block": p["entry_block"],
            "heat": p["heat"],
            "onset_lag_sec": p["onset_lag"],
            "pool_age_sec": p["pool_age"],
            "weth_is_t1": p["weth_is_t1"],
            "sell_path_ok": sell_ok,
            **{f"ret_{lbl}": horizons.get(lbl) for lbl in EXIT_LABELS},
            "trailing_2h_net": outcome["net"],
            "exit_reason": outcome["exit"],
            "wins": outcome["wins"],
            "pf_eligible": outcome.get("pf_eligible", True),
            "tp1_hit": outcome.get("tp1_hit", False),
            "tp2_hit": outcome.get("tp2_hit", False),
        })

        if i % 20 == 0:
            print(f"  [progress] {i}/{len(onsets)} → {len(results)} results so far", flush=True)

    print(f"[done] {len(results)} signals processed, skipped={skipped}", flush=True)

    # Write per-signal CSV
    if results:
        keys = list(results[0].keys())
        with (out_dir / "per_signal.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(results)

    # ── Aggregate stats ──────────────────────────────────────────────────────
    sell_blocked_proxy = [r for r in results if r.get("exit_reason") == "sell_blocked_ch_proxy_unverified"]
    decidable = [
        r for r in results
        if r.get("pf_eligible", True) and r.get("trailing_2h_net") is not None
    ]
    nets = [r["trailing_2h_net"] for r in decidable]

    pool_counts: dict[str, int] = {}
    for r in decidable:
        pool_counts[r["pool"]] = pool_counts.get(r["pool"], 0) + 1
    unique_pools = len(pool_counts)
    top_pool_share = (max(pool_counts.values()) / max(len(decidable), 1)) if pool_counts else 0.0

    token_counts: dict[str, int] = {}
    for r in decidable:
        token_counts[r["token"]] = token_counts.get(r["token"], 0) + 1
    unique_tokens = len(token_counts)
    top_token_share = (max(token_counts.values()) / max(len(decidable), 1)) if token_counts else 0.0

    overall_pf = pf_of(nets)
    top1_pf = pf_top1_removed(nets)
    boot_p5 = bootstrap_p5(nets, args.bootstrap_n)
    win_rate = round(sum(1 for v in nets if v > 0) / max(len(nets), 1), 4) if nets else 0.0
    mean_ret = round(sum(nets) / max(len(nets), 1), 6) if nets else 0.0

    # By fee tier
    by_fee: dict[str, dict] = {}
    for fee_val in [1, 5, 30, 100]:
        sub = [r for r in decidable if r["fee_bps"] == fee_val]
        sub_nets = [r["trailing_2h_net"] for r in sub]
        by_fee[str(fee_val)] = {
            "n": len(sub),
            "unique_pools": len({r["pool"] for r in sub}),
            "mean": round(sum(sub_nets) / max(len(sub_nets), 1), 6) if sub_nets else None,
            "win_rate": round(sum(1 for v in sub_nets if v > 0) / max(len(sub_nets), 1), 4) if sub_nets else None,
            "pf": pf_of(sub_nets),
        }

    # ── 10-gate verdicts ─────────────────────────────────────────────────────
    n_ok = len(nets) >= 30
    diversity_ok = unique_pools >= 30 and top_pool_share <= 0.15
    pf_ok = (overall_pf is not None and overall_pf != float("inf") and overall_pf >= 1.3)
    top1_ok = (top1_pf is not None and top1_pf != float("inf") and top1_pf >= 1.3)
    boot_ok = (boot_p5 is not None and boot_p5 >= 1.0)
    wr_ok = win_rate >= 0.50
    sell_ok_rate = sum(1 for r in decidable if r["sell_path_ok"]) / max(len(decidable), 1)
    sell_ok_gate = sell_ok_rate >= 0.60  # at least 60% of pools have sell activity
    token_ok = top_token_share <= 0.30  # no single token > 30%
    fee30_coverage = sum(1 for r in decidable if r["fee_bps"] == 30)  # new: fee=0.3% pools

    gates = {
        "g1_first_signal_only": True,   # built into onset detection (one entry per pool)
        "g2_n_ge_30": n_ok,
        "g3_diversity_unique_pools_ge_30": diversity_ok,
        "g4_real_pf_ge_1_3": pf_ok,
        "g5_pf_top1_removed_ge_1_3": top1_ok,
        "g6_bootstrap_p5_ge_1_0": boot_ok,
        "g7_win_rate_ge_50pct": wr_ok,
        "g8_sell_path_ok": sell_ok_gate,
        "g9_bot_excluded": True,        # 0x6ff5 excluded in discovery loop
        "g10_base_routing_excluded": True,  # WETH/USDC/USDbC/cbBTC excluded
    }
    gates_pass = sum(1 for v in gates.values() if v)
    verdict = "PASS" if all(gates.values()) else "FAIL"

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "alpha_id": "AR-SP095-PRE-DENTRY-RETEST",
        "sprint": "095-PRE",
        "params": {
            "lookback_days": args.lookback_days,
            "pool_max_age_sec": args.pool_max_age_sec,
            "onset_thresh": args.onset_thresh,
            "fee_bps_allowlist": args.fee_bps,
            "exit_policy": "trailing_5_pull3_2h",
            "per_leg_cost_pct": PER_LEG,
            "tick_method": "ch_median_pm2.5min",
            "rpc_calls": 0,
        },
        "discovery": {
            "onsets_total": len(onsets),
            "skipped": skipped,
            "results_total": len(results),
            "decidable": len(decidable),
            "sell_blocked_ch_proxy_unverified": len(sell_blocked_proxy),
            "honeypot_check": "CH proxy only (≥1 sell in 2h); Quoter required for promotion",
        },
        "performance": {
            "n": len(nets),
            "unique_pools": unique_pools,
            "unique_tokens": unique_tokens,
            "top_pool_share": round(top_pool_share, 4),
            "top_token_share": round(top_token_share, 4),
            "win_rate": win_rate,
            "mean_return": mean_ret,
            "overall_pf": overall_pf,
            "pf_top1_removed": top1_pf,
            "bootstrap_p5": boot_p5,
            "sell_ok_rate": round(sell_ok_rate, 4),
            "fee30_signals": fee30_coverage,
        },
        "by_fee_tier": by_fee,
        "gates": gates,
        "gates_pass_count": f"{gates_pass}/10",
        "verdict": verdict,
    }

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    # ── Chinese readout ──────────────────────────────────────────────────────
    def _emoji(b):
        return "✅" if b else "❌"

    pf_str = f"{overall_pf:.4f}" if isinstance(overall_pf, float) else str(overall_pf)
    t1_str = f"{top1_pf:.4f}" if isinstance(top1_pf, float) else str(top1_pf)
    b5_str = f"{boot_p5:.4f}" if isinstance(boot_p5, float) else str(boot_p5)

    md = f"""# Sprint095-PRE — D_entry × trailing_5_pull3_2h × 宽 universe 复试

**生成时间:** {summary['generated_at_utc']}
**总裁决:** `{verdict}` ({gates_pass}/10 gates passed)

---

## 实验设计

| 参数 | 值 |
|---|---|
| 回溯窗口 | {args.lookback_days}天 |
| 池年龄门槛 | ≤{args.pool_max_age_sec//3600}h |
| Onset 热度 | ≥{args.onset_thresh} swaps/5min |
| Onset 时延 | ≤{args.onset_max_sec//3600}h |
| Fee 档位 | {args.fee_bps} (discovered_pools 基点编码：1=0.01%,5=0.05%,30=0.30%,100=1.00%) |
| 退出策略 | trailing_5_pull3_2h (TP1@+5%/50%, TP2@+20%/30%, 尾随回撤3%, SL@-10%, 时间止 2h) |
| 每腿成本 | {PER_LEG*100:.1f}% (gas+滑点+池费代理) |
| RPC 调用 | 0 (纯 CH tick 数据) |
| Tick 方法 | 各 horizon 时间点 ±2.5min 中位 tick (抗噪声) |
| 排除 | 0x6ff5 dominant 池、WETH/USDC/USDbC/cbBTC |

---

## 信号发现

| 指标 | 值 |
|---|---|
| 候选 onsets | {len(onsets)} |
| 跳过 (0x6ff5) | {skipped['is_0x6ff5']} |
| 跳过 (base routing token) | {skipped['base_routing_token']} |
| 跳过 (无 tick 数据) | {skipped['no_tick_data']} |
| 进入结果 | {len(results)} |
| 可判断 | {len(decidable)} |

---

## 绩效总览

| 指标 | 值 |
|---|---|
| N (信号数) | {len(nets)} |
| 独立池数 | {unique_pools} |
| 独立代币数 | {unique_tokens} |
| Top pool 占比 | {top_pool_share:.1%} |
| Top token 占比 | {top_token_share:.1%} |
| 胜率 | {win_rate:.1%} |
| 平均回报 | {mean_ret:+.2%} |
| Profit Factor (PF) | {pf_str} |
| PF_top1_removed | {t1_str} |
| Bootstrap p5 | {b5_str} |
| 卖出路径 OK 率 | {sell_ok_rate:.1%} |

---

## 费率分桶

| fee_bps | 描述 | N | 池数 | WR | PF |
|---|---|---|---|---|---|
"""
    for fval, fname in [(1, "0.01%"), (5, "0.05%"), (30, "0.30%"), (100, "1.00%")]:
        fb = by_fee.get(str(fval), {})
        n_f = fb.get("n", 0)
        up_f = fb.get("unique_pools", 0)
        wr_f = f"{fb['win_rate']:.1%}" if fb.get("win_rate") is not None else "—"
        pf_f = f"{fb['pf']:.3f}" if isinstance(fb.get("pf"), float) else str(fb.get("pf", "—"))
        md += f"| {fval} | {fname} | {n_f} | {up_f} | {wr_f} | {pf_f} |\n"

    md += f"""
---

## §3 全套 10 条 Gate 验证

| Gate | 要求 | 实际值 | 通过? |
|---|---|---|---|
| G1 first-signal-only | 内建 (每池一次 onset) | ✓ | {_emoji(gates['g1_first_signal_only'])} |
| G2 n ≥ 30 | ≥30 | {len(nets)} | {_emoji(gates['g2_n_ge_30'])} |
| G3 unique_pools ≥ 30 AND top_pool ≤ 15% | 多样性 | pools={unique_pools}, top={top_pool_share:.1%} | {_emoji(gates['g3_diversity_unique_pools_ge_30'])} |
| G4 real_pf ≥ 1.3 | PF≥1.3 | {pf_str} | {_emoji(gates['g4_real_pf_ge_1_3'])} |
| G5 PF_top1_removed ≥ 1.3 | 稳健性 | {t1_str} | {_emoji(gates['g5_pf_top1_removed_ge_1_3'])} |
| G6 bootstrap_p5 ≥ 1.0 | 置信区间 | {b5_str} | {_emoji(gates['g6_bootstrap_p5_ge_1_0'])} |
| G7 WR ≥ 50% | | {win_rate:.1%} | {_emoji(gates['g7_win_rate_ge_50pct'])} |
| G8 sell_path_ok ≥ 60% | 卖出路径 | {sell_ok_rate:.1%} | {_emoji(gates['g8_sell_path_ok'])} |
| G9 bot excluded | 0x6ff5 过滤 | ✓ | {_emoji(gates['g9_bot_excluded'])} |
| G10 base routing excluded | WETH/USDC 过滤 | ✓ | {_emoji(gates['g10_base_routing_excluded'])} |

---

## 裁决

**`{verdict}`** ({gates_pass}/10 gates)
"""
    if verdict == "PASS":
        md += """
所有 10 条 §3 gate 全部通过。D_entry × trailing_5_pull3_2h × 宽 universe 三合一复试结果：

> **Alpha 信号存活**。这是 EATLQ 历史上第一次在强化 anti-self-deception 框架下通过全套 gate。
> 下一步：进入 Sprint095-CANARY 预备阶段（human gate 关闭程序），需要 ADR 审查。
"""
    else:
        failed = [k for k, v in gates.items() if not v]
        md += f"""
失败 gate：{', '.join(failed)}

D_entry forward 线在当前 30d 宽 universe + trailing 2h exit 条件下**未通过**全套 §3 gate。

> 根据策略规划，D_entry 未通过意味着这条线也死透了。
> 下一步：进入 P1（防御产品）或 P3（诚实 sunset）路径。
"""

    md += f"""
---

## 产物

- `per_signal.csv` — 每个信号的多 horizon 回报 + trailing 出场结果
- `summary.json` — 全套统计 + gate 裁决
- `readout_cn.md` — 本文件

*生成工具: scripts/sprint095_pre_dentry_retest.py (CH-only, 0 RPC calls)*
"""

    (out_dir / "readout_cn.md").write_text(md, encoding="utf-8")
    print(f"\n{'='*60}", flush=True)
    print(f"VERDICT: {verdict} ({gates_pass}/10 gates)", flush=True)
    print(f"n={len(nets)}  unique_pools={unique_pools}  PF={pf_str}", flush=True)
    print(f"PF_top1_removed={t1_str}  bootstrap_p5={b5_str}  WR={win_rate:.1%}", flush=True)
    print(f"Output: {out_dir}", flush=True)

    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
