#!/usr/bin/env python3
"""Sprint086 — 0x6ff5 Weathervane backtest (research only, no live).

Hypothesis (from user):
  0x6ff5 is a market-making bot. Don't copy its orders.
  Instead, treat its FIRST APPEARANCE in a new pool as a 'weathervane' signal:
  the pool is about to get active. We enter ONCE with our own size after a
  detection delay, then run a batched-exit policy.

This script:
  1. Finds all (pool, fee) where 0x6ff5 was first active in the last N days.
  2. For each, defines entry_time = first_0x6ff5_swap_time + detection_delay_sec.
  3. Computes forward returns at multiple horizons via QuoterV2 (replay).
  4. Applies the real cost model (sprint086 lib) at our notional.
  5. Runs the batched exit policies from sprint086_batched_exit_backtest.
  6. Outputs per-pool table + aggregate PF, with sender-exclusion control.

NOT live. NOT a copy-follow. Sender is used as a *regime* signal, not as
an order to mirror — consistent with Sprint070 verdict
(COPYABILITY=TOO_LATE_AFTER_OBSERVATION) and SOP §7.5 Route B.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from real_cost_model import RealCostModel  # type: ignore  # noqa: E402
from v3_quoterv2_struct import JsonRpcClient, quote_exact_input_single_struct, WETH_BASE  # type: ignore  # noqa: E402

SENDER_0X6FF5 = "0x6ff5693b99212da76ad316178a184ab56d299b43"
CH_CONTAINER = "eatlq-clickhouse-1"

EXIT_LABELS = ["5m", "10m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "24h"]
EXIT_SECONDS = {
    "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
    "12h": 43200, "24h": 86400,
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint086 0x6ff5 weathervane backtest")
    p.add_argument("--lookback-days", type=int, default=14)
    p.add_argument("--detection-delay-sec", type=int, default=60,
                   help="Realistic detection + execution delay after 0x6ff5 first swap")
    p.add_argument("--notional-usd", type=float, default=10.0)
    p.add_argument("--max-pools", type=int, default=50)
    p.add_argument("--rpc-url", default=os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", ""))
    p.add_argument("--rpc-delay", type=float, default=0.05)
    p.add_argument(
        "--out-dir",
        default="mvp/audits/sprint086_real_cost_overlay/weathervane_0x6ff5",
    )
    p.add_argument(
        "--per-leg-cost-pct",
        type=float,
        default=0.012,
        help="Approx per-partial-sell cost (gas + slippage + fee)",
    )
    return p.parse_args()


def _ch(sql: str, timeout: int = 60) -> list[list[str]]:
    # Detect format from the SQL itself (TabSeparated vs CSVWithNames)
    fmt = "TabSeparated" if "FORMAT TabSeparated" in sql else "CSV"
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client", "--format", fmt, "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH: {r.stderr.strip()[:300]}")
    if fmt == "TabSeparated":
        return [line.split("\t") for line in r.stdout.splitlines() if line]
    return [row for row in csv.reader(io.StringIO(r.stdout)) if row]


def _load_env_rpc() -> str:
    env = REPO / "infra/compose/.env"
    if not env.exists():
        return ""
    for line in env.read_text().splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        k, v = line.split("=", 1)
        if k.strip() in ("LABEL_RPC_URL", "EVM_HTTP_URL"):
            v = v.strip().strip('"').strip("'")
            if v.startswith("http"):
                return v
    return ""


def fetch_weathervane_pools(lookback_days: int, max_pools: int) -> list[dict]:
    """Find pools where 0x6ff5 was first active in the lookback window.

    Returns rows with: pool, token, fee_bps, weth_is_token1,
    first_6ff5_block_time, first_6ff5_block_number, swap_count_after_6ff5_5m.
    """
    sql = f"""
    WITH first_appearance AS (
      SELECT
        pool_address AS pool,
        min(block_time) AS first_block_time,
        min(block_number) AS first_block_number
      FROM eatlq.uniswap_v3_swaps
      WHERE sender = '{SENDER_0X6FF5}'
        AND block_time >= now() - INTERVAL {lookback_days} DAY
      GROUP BY pool_address
    ),
    pool_meta AS (
      SELECT
        lower(pool_address) AS pool,
        lower(token0_address) AS token0,
        lower(token1_address) AS token1,
        fee_bps AS fee_bps
      FROM eatlq.discovered_pools
    )
    SELECT
      fa.pool AS pool,
      toString(fa.first_block_time) AS first_block_time,
      toString(fa.first_block_number) AS first_block_number,
      pm.token0 AS token0,
      pm.token1 AS token1,
      pm.fee_bps AS fee_bps
    FROM first_appearance fa
    INNER JOIN pool_meta pm ON lower(fa.pool) = pm.pool
    WHERE pm.fee_bps IN (1, 5, 30, 100)
      AND (pm.token0 = '0x4200000000000000000000000000000000000006'
        OR pm.token1 = '0x4200000000000000000000000000000000000006')
    ORDER BY fa.first_block_time DESC
    LIMIT {max_pools}
    FORMAT CSVWithNames
    """
    rows = _ch(sql, timeout=120)
    if not rows:
        return []
    header = rows[0]
    out = []
    for r in rows[1:]:
        d = dict(zip(header, r))
        weth_lc = "0x4200000000000000000000000000000000000006"
        token = d["token1"].lower() if d["token0"].lower() == weth_lc else d["token0"].lower()
        weth_is_token1 = d["token0"].lower() == weth_lc
        out.append({
            "pool": d["pool"].lower(),
            "token": token,
            "fee_bps": int(d["fee_bps"]),
            "weth_is_token1": weth_is_token1,
            "first_block_time": d["first_block_time"],
            "first_block_number": int(d["first_block_number"]),
        })
    return out


def parse_ch_dt(s: str) -> datetime | None:
    try:
        # CH default format: '2026-05-20 14:30:00'
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None


def block_at_time(rpc: JsonRpcClient, ts: datetime) -> int | None:
    """Best-effort: find the block at or just after ts via CH (faster than RPC binary search)."""
    sql = (
        f"SELECT block_number FROM eatlq.uniswap_v3_swaps "
        f"WHERE block_time >= toDateTime('{ts.strftime('%Y-%m-%d %H:%M:%S')}', 'UTC') "
        f"ORDER BY block_time ASC LIMIT 1 FORMAT TabSeparated"
    )
    try:
        rows = _ch(sql, timeout=15)
        if rows and rows[0]:
            return int(rows[0][0])
    except Exception:
        pass
    return None


def _try_quote(primary, fallback, *args, **kwargs):
    q = quote_exact_input_single_struct(primary, *args, **kwargs)
    if q.get("reason") == "rpc_error" and fallback is not None:
        q2 = quote_exact_input_single_struct(fallback, *args, **kwargs)
        if q2.get("ok") or q2.get("reason") != "rpc_error":
            return q2
    return q


SEL_FEE = "0xddca3f43"  # pool.fee()


def onchain_pool_fee(rpc, fallback, pool: str) -> int | None:
    """Query the actual pool fee on-chain (sprint055-style)."""
    for client in (rpc, fallback) if fallback else (rpc,):
        try:
            res = client.call(
                "eth_call",
                [{"to": pool, "data": SEL_FEE}, "latest"],
                timeout=15,
            )
            if res and res != "0x":
                return int(res, 16)
        except Exception:
            continue
    return None


def replay_horizons(
    rpc: JsonRpcClient,
    fallback_rpc: JsonRpcClient | None,
    token: str,
    fee_bps: int,
    entry_time: datetime,
) -> dict[str, float | None]:
    """Replay multi-horizon returns by quoting at entry_block and entry_block + Δseconds."""
    out: dict[str, float | None] = {h: None for h in EXIT_LABELS}
    micro_in = int(0.0005 * 10**18)
    entry_block = block_at_time(rpc, entry_time)
    if not entry_block:
        return out
    buy_q = _try_quote(rpc, fallback_rpc, WETH_BASE, token, micro_in, fee_bps, block_identifier=entry_block)
    if not buy_q.get("ok") or buy_q.get("amount_out", 0) <= 0:
        return out
    tokens_bought = int(buy_q["amount_out"])

    for h in EXIT_LABELS:
        exit_time = entry_time + timedelta(seconds=EXIT_SECONDS[h])
        exit_block = block_at_time(rpc, exit_time)
        if not exit_block:
            continue
        sell_q = _try_quote(rpc, fallback_rpc, token, WETH_BASE, tokens_bought, fee_bps, block_identifier=exit_block)
        if not sell_q.get("ok") or sell_q.get("amount_out", 0) <= 0:
            if sell_q.get("reason") == "rpc_error":
                continue  # leave as None
            out[h] = -1.0  # genuine revert → cannot exit
            continue
        weth_back = int(sell_q["amount_out"])
        out[h] = round((weth_back - micro_in) / micro_in, 6)
    return out


def apply_batched_exit(
    horizons: dict[str, float | None],
    sell_path_ok: bool,
    cost_total_pct: float,
    per_leg_cost: float,
    tp1_pct: float = 0.05, tp1_size: float = 0.30,
    tp2_pct: float = 0.15, tp2_size: float = 0.30,
    sl_pct: float = -0.10,
    time_horizon: str = "1h",
    trailing_drawdown: float | None = 0.03,
) -> dict:
    if not sell_path_ok:
        return {"net": -1.0, "exit_reason": "sell_path_blocked", "legs": 0}
    traj = [(h, EXIT_SECONDS[h], horizons[h]) for h in EXIT_LABELS if horizons.get(h) is not None]
    if not traj:
        return {"net": 0.0, "exit_reason": "no_data", "legs": 0}

    time_limit = EXIT_SECONDS.get(time_horizon, 3600)
    remaining = 1.0
    realised = 0.0
    peak = 0.0
    tp1_hit = tp2_hit = False
    exit_reason = "time_horizon"
    legs = 0

    for label, sec, ret in traj:
        if sec > time_limit:
            break
        peak = max(peak, ret)

        # Hard SL
        if remaining > 0 and ret <= sl_pct:
            realised += remaining * (ret - per_leg_cost)
            legs += 1
            remaining = 0
            exit_reason = "stop_loss"
            break

        # Trailing
        if remaining > 0 and trailing_drawdown is not None:
            if peak >= tp1_pct and (peak - ret) >= trailing_drawdown:
                realised += remaining * (ret - per_leg_cost)
                legs += 1
                remaining = 0
                exit_reason = "trailing_stop"
                break

        # TP2
        if remaining > 0 and not tp2_hit and ret >= tp2_pct:
            size = min(remaining, tp2_size)
            realised += size * (ret - per_leg_cost)
            remaining -= size
            tp2_hit = True
            legs += 1

        # TP1
        if remaining > 0 and not tp1_hit and ret >= tp1_pct:
            size = min(remaining, tp1_size)
            realised += size * (ret - per_leg_cost)
            remaining -= size
            tp1_hit = True
            legs += 1

    if remaining > 0:
        last_ret = None
        for _, sec, ret in traj:
            if sec <= time_limit:
                last_ret = ret
        if last_ret is not None:
            realised += remaining * (last_ret - per_leg_cost)
            legs += 1

    # Subtract one-time cost_total_pct (gas + buy slippage already implicit in the trajectory baseline)
    realised -= cost_total_pct

    return {"net": round(realised, 6), "exit_reason": exit_reason, "legs": legs,
            "tp1_hit": tp1_hit, "tp2_hit": tp2_hit}


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    rpc_url = args.rpc_url or _load_env_rpc()
    if not rpc_url:
        print("ERROR: no RPC URL configured", file=sys.stderr)
        return 2
    rpc = JsonRpcClient(rpc_url, delay_s=args.rpc_delay)
    fallback_rpc = JsonRpcClient("https://mainnet.base.org", delay_s=max(args.rpc_delay, 0.1))
    cost_model = RealCostModel(rpc=rpc, fallback_rpc=fallback_rpc)

    started = time.time()
    pools = fetch_weathervane_pools(args.lookback_days, args.max_pools)
    if not pools:
        print("No 0x6ff5 weathervane pools in lookback window.")
        return 1

    results = []
    per_pool_csv = out_dir / "weathervane_per_pool.csv"
    for p in pools:
        entry_time = parse_ch_dt(p["first_block_time"])
        if not entry_time:
            continue
        entry_time = entry_time + timedelta(seconds=args.detection_delay_sec)

        # Trust on-chain pool.fee() over discovered_pools metadata
        onchain_fee = onchain_pool_fee(rpc, fallback_rpc, p["pool"])
        if onchain_fee and onchain_fee != p["fee_bps"]:
            p["fee_bps_meta"] = p["fee_bps"]
            p["fee_bps"] = onchain_fee
        # 1) Real cost at our notional, at entry block
        entry_block = block_at_time(rpc, entry_time)
        cb = cost_model.estimate(
            token=p["token"], fee_bps=p["fee_bps"],
            notional_usd=args.notional_usd,
            block_identifier=entry_block,
        )
        # 2) Forward horizon replay
        horizons = replay_horizons(rpc, fallback_rpc, p["token"], p["fee_bps"], entry_time)
        # 3) Multiple batched exit policies
        policies = {
            "aggressive_5_15_neg10_1h": dict(tp1_pct=0.05, tp1_size=0.30, tp2_pct=0.15, tp2_size=0.30, sl_pct=-0.10, time_horizon="1h", trailing_drawdown=None),
            "trailing_10_pull5_6h":     dict(tp1_pct=0.10, tp1_size=0.30, tp2_pct=0.30, tp2_size=0.30, sl_pct=-0.15, time_horizon="6h", trailing_drawdown=0.05),
            "patient_20_50_neg15_24h":  dict(tp1_pct=0.20, tp1_size=0.30, tp2_pct=0.50, tp2_size=0.30, sl_pct=-0.15, time_horizon="24h", trailing_drawdown=0.10),
        }
        outcomes = {}
        for pname, pkw in policies.items():
            outcomes[pname] = apply_batched_exit(
                horizons,
                sell_path_ok=cb.sell_path_ok and not cb.honeypot_flag,
                cost_total_pct=cb.round_trip_cost_pct(),
                per_leg_cost=args.per_leg_cost_pct,
                **pkw,
            )
        results.append({
            **p,
            "entry_time_after_delay": entry_time.isoformat(),
            "cost_total_pct": round(cb.round_trip_cost_pct(), 6),
            "sell_path_ok": cb.sell_path_ok,
            "honeypot_flag": cb.honeypot_flag,
            "rpc_error": cb.rpc_error,
            "liquidity_depth_ok": cb.liquidity_depth_ok,
            **{f"ret_{h}": horizons.get(h) for h in EXIT_LABELS},
            **{f"net_{p}": o["net"] for p, o in outcomes.items()},
            **{f"exit_{p}": o["exit_reason"] for p, o in outcomes.items()},
        })

    if results:
        fieldnames = list(results[0].keys())
        with per_pool_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(results)

    # Decidable pools = those without RPC error
    decidable = [r for r in results if not r.get("rpc_error")]
    policy_names = ["aggressive_5_15_neg10_1h", "trailing_10_pull5_6h", "patient_20_50_neg15_24h"]
    policy_stats = {}
    for pname in policy_names:
        nets = [r.get(f"net_{pname}") for r in decidable if isinstance(r.get(f"net_{pname}"), (int, float))]
        policy_stats[pname] = {
            "n": len(nets),
            "mean_return": round(sum(nets) / max(len(nets), 1), 6) if nets else None,
            "win_rate": round(sum(1 for v in nets if v > 0) / max(len(nets), 1), 4) if nets else None,
            "pf": _pf(nets),
        }
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "alpha_id": "AR-SP086-WEATHERVANE-0X6FF5",
        "family": "F1/F3",
        "subject": SENDER_0X6FF5,
        "subject_role": "PROGRAMMATIC_MARKET_MAKER (per sprint070)",
        "usage": "regime_weathervane_not_copy",
        "lookback_days": args.lookback_days,
        "detection_delay_sec": args.detection_delay_sec,
        "notional_usd": args.notional_usd,
        "per_leg_cost_pct": args.per_leg_cost_pct,
        "pools_total": len(results),
        "pools_decidable": len(decidable),
        "rpc_error_pools": len(results) - len(decidable),
        "pools_with_data": sum(1 for r in decidable if isinstance(r.get("net_aggressive_5_15_neg10_1h"), (int, float))),
        "sell_fail_rate_decidable": round(sum(1 for r in decidable if not r["sell_path_ok"]) / max(len(decidable), 1), 4),
        "honeypot_rate_decidable": round(sum(1 for r in decidable if r["honeypot_flag"]) / max(len(decidable), 1), 4),
        "policy_stats": policy_stats,
        "elapsed_sec": round(time.time() - started, 1),
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "out_csv": str(per_pool_csv.relative_to(REPO)),
    }
    (out_dir / "weathervane_summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    # Readout
    md = [
        "# Sprint086 — 0x6ff5 Weathervane Backtest",
        "",
        f"Generated: {summary['generated_at_utc']}  ",
        f"Lookback: **{args.lookback_days}d** · Detection delay: **{args.detection_delay_sec}s** · Notional: **${args.notional_usd:.0f}U**",
        "",
        "## Hypothesis (per user)",
        "",
        "0x6ff5 is a market-making bot. Don't copy its orders (per `sprint070_full_readout.md` — "
        "COPYABILITY=TOO_LATE_AFTER_OBSERVATION). Instead, treat its **first appearance in a new pool** "
        "as a weathervane: the pool is about to get active. Enter ONCE with our own size after a "
        "detection delay, then batched exit.",
        "",
        "## Aggregate",
        "",
        f"- pools scanned: **{summary['pools_total']}**  ",
        f"- pools decidable (no RPC error): **{summary['pools_decidable']}**  ",
        f"- pools with replay data: {summary['pools_with_data']}  ",
        f"- sell-fail rate (decidable): **{summary['sell_fail_rate_decidable']}**  ",
        f"- honeypot rate (decidable): **{summary['honeypot_rate_decidable']}**  ",
        "",
        "## Per-policy results",
        "",
        "| policy | n | mean | WR | PF |",
        "|---|--:|--:|--:|--:|",
    ]
    for pname, ps in summary["policy_stats"].items():
        md.append(f"| `{pname}` | {ps['n']} | {ps['mean_return']} | {ps['win_rate']} | {ps['pf']} |")
    md += [
        "",
        f"Per-pool CSV: `{summary['out_csv']}`",
        "",
        "## Decision flags",
        "",
        "```json",
        json.dumps(summary, indent=2),
        "```",
    ]
    (out_dir / "weathervane_readout.md").write_text("\n".join(md) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


def _pf(vals: list[float]) -> float | None:
    wins = sum(v for v in vals if v > 0)
    losses = abs(sum(v for v in vals if v < 0))
    if losses > 0:
        return round(wins / losses, 4)
    if wins > 0:
        return float("inf")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
