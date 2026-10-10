#!/usr/bin/env python3
"""Sprint088 — Historical backtest with Sprint087 gates + real-cost + trailing 2h.

Replays the last N days of POOL ONSETS (not the contaminated sprint055 CSV)
through the full Sprint087 pipeline:

  1. Universe: all WETH-leg pools, fee_bps ∈ {100, 500, 3000, 10000},
     pool_age ≤ POOL_MAX_AGE, onset window detected when heat ≥ ONSET_THRESH/5m
  2. Hard exclude is_0x6ff5=True pools (via sprint080 sender map)
  3. First-signal-only: one entry per pool over the entire backtest window
  4. Entry quote at the HISTORICAL block where heat first crossed threshold
  5. Real cost at $10 notional via QuoterV2 sell-sim (same block)
  6. Forward horizon quotes at entry_block + {5m..6h}
  7. Trailing 2h batched exit (TP +5%/+15%, trailing pull 3%, SL -10%, time 2h)
  8. Aggregate: unique_pools, top_pool_share, real PF, sell_fail_rate

Pass criteria (same as sprint087 forward):
  unique_pools ≥ 30 AND top_pool_share ≤ 0.15 AND real_pf_2h ≥ 1.3 AND n ≥ 30

Output: mvp/audits/sprint088_historical_backtest/
  - per_signal.csv          (one row per backtested signal)
  - summary.json            (aggregate per fee + overall)
  - readout.md              (verdict + recommended next step)

This is NOT live. It is historical replay. Numbers come from QuoterV2 eth_call
on archive nodes at past blocks, identical to what live execution would have seen.
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
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from real_cost_model import RealCostModel  # type: ignore  # noqa: E402
from v3_quoterv2_struct import (  # type: ignore  # noqa: E402
    JsonRpcClient, quote_exact_input_single_struct, WETH_BASE,
)

CH_CONTAINER = "eatlq-clickhouse-1"
CHAIN_ID = 8453
SENDER_0X6FF5 = "0x6ff5693b99212da76ad316178a184ab56d299b43"
WETH_LC = WETH_BASE.lower()

EXIT_LABELS = ["5m", "15m", "30m", "1h", "2h"]
EXIT_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200}

SENDER_MAP_PATH = REPO / "logs/sprint080_sender_map.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint088 historical backtest")
    p.add_argument("--lookback-days", type=int, default=14)
    p.add_argument("--pool-max-age-sec", type=int, default=172800)
    p.add_argument("--onset-thresh", type=int, default=5)
    p.add_argument("--bucket-minutes", type=int, default=5,
                   help="Onset heat bucket size (5=default; 30=slow window H2)")
    p.add_argument("--fee-bps-list", default="",
                   help="Comma fee tiers for pool_meta filter, e.g. 100,500,3000")
    p.add_argument("--onset-max-sec", type=int, default=7200)
    p.add_argument("--max-pools", type=int, default=300)
    p.add_argument("--notional-usd", type=float, default=10.0)
    p.add_argument("--per-leg-cost-pct", type=float, default=0.012)
    p.add_argument("--exclude-0x6ff5", action="store_true", default=True)
    p.add_argument("--no-exclude-0x6ff5", action="store_true")
    p.add_argument("--rpc-url", default=os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", ""))
    p.add_argument("--rpc-delay", type=float, default=0.05)
    p.add_argument("--out-dir", default="mvp/audits/sprint088_historical_backtest")
    return p.parse_args()


def _ch(sql: str, timeout: int = 60) -> list[list[str]]:
    fmt = "TabSeparated" if "FORMAT TabSeparated" in sql else "CSV"
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client", "--format", fmt, "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH: {r.stderr.strip()[:400]}")
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


def _load_sender_map() -> dict:
    if not SENDER_MAP_PATH.exists():
        return {}
    try:
        raw = json.loads(SENDER_MAP_PATH.read_text())
        return {k.lower(): v for k, v in raw.items()}
    except Exception:
        return {}


def discover_historical_onsets(args) -> list[dict]:
    """Find first-onset windows per pool in the last N days.

    Onset detection: for each pool, find the EARLIEST 5m bucket where
    heat ≥ ONSET_THRESH and pool_age at that moment ≤ pool_max_age_sec and
    onset_lag (window_start - first_swap) ≤ onset_max_sec.

    Returns one row per pool (first-signal-only built in via earliest bucket).
    """
    bucket_fn = (
        "toStartOfFiveMinutes(block_time)"
        if args.bucket_minutes <= 5
        else f"toStartOfInterval(block_time, INTERVAL {args.bucket_minutes} minute)"
    )
    fee_list = args.fee_bps_list.strip()
    if fee_list:
        fees = ",".join(str(int(x.strip())) for x in fee_list.split(",") if x.strip())
        fee_clause = f"AND pm.fee_bps IN ({fees})"
    else:
        # discovered_pools.fee_bps is in basis points (0.01% units):
        #   1=0.01%, 5=0.05%, 30=0.30%, 100=1.00%
        # NOT the Uniswap V3 protocol fee encoding (100/500/3000/10000).
        fee_clause = "AND pm.fee_bps IN (1, 5, 30, 100)"
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
    toUnixTimestamp({bucket_fn}) AS wts,
    count() AS heat,
    min(toUnixTimestamp(block_time)) AS bucket_min_ts,
    min(block_number) AS bucket_min_block
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
    b.wts AS wts,
    b.heat AS heat,
    b.bucket_min_ts AS entry_ts,
    b.bucket_min_block AS entry_block,
    fs.first_ts AS first_ts,
    (b.wts - fs.first_ts) AS onset_lag,
    (b.bucket_min_ts - fs.first_ts) AS pool_age_at_entry
  FROM buckets b
  INNER JOIN first_swap fs ON b.pool_address = fs.pool_address
  WHERE (b.bucket_min_ts - fs.first_ts) <= {args.pool_max_age_sec}
    AND (b.wts - fs.first_ts) <= {args.onset_max_sec}
),
earliest AS (
  SELECT pool, min(wts) AS min_wts
  FROM qualifying
  GROUP BY pool
),
first_qualifying AS (
  SELECT
    q.pool AS pool,
    q.wts AS wts,
    q.heat AS heat,
    q.entry_ts AS entry_ts,
    q.entry_block AS entry_block,
    q.first_ts AS first_ts,
    q.onset_lag AS onset_lag,
    q.pool_age_at_entry AS pool_age_at_entry
  FROM qualifying q
  INNER JOIN earliest e ON q.pool = e.pool AND q.wts = e.min_wts
),
pool_meta AS (
  SELECT
    lower(pool_address) AS pool,
    lower(token0_address) AS token0,
    lower(token1_address) AS token1,
    fee_bps
  FROM eatlq.discovered_pools
)
SELECT
  fq.pool AS pool,
  fq.entry_ts AS entry_ts,
  fq.entry_block AS entry_block,
  fq.onset_lag AS onset_lag,
  fq.pool_age_at_entry AS pool_age_at_entry,
  fq.heat AS heat,
  pm.token0 AS token0,
  pm.token1 AS token1,
  pm.fee_bps AS fee_bps
FROM first_qualifying fq
INNER JOIN pool_meta pm ON lower(fq.pool) = pm.pool
WHERE (pm.token0 = '{WETH_LC}' OR pm.token1 = '{WETH_LC}')
  {fee_clause}
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
        token = d["token1"] if d["token0"] == WETH_LC else d["token0"]
        out.append({
            "pool": d["pool"].lower(),
            "token": token.lower(),
            "fee_bps_meta": int(d["fee_bps"]),
            "entry_ts": int(d["entry_ts"]),
            "entry_block": int(d["entry_block"]),
            "onset_lag": int(d["onset_lag"]),
            "pool_age_at_entry": int(d["pool_age_at_entry"]),
            "heat": int(d["heat"]),
            "weth_is_token1": d["token0"] == WETH_LC,
        })
    return out


SEL_FEE = "0xddca3f43"


def onchain_pool_fee(primary, fallback, pool: str) -> int | None:
    for client in (primary, fallback):
        if client is None:
            continue
        try:
            res = client.call("eth_call", [{"to": pool, "data": SEL_FEE}, "latest"], timeout=15)
            if res and res != "0x":
                return int(res, 16)
        except Exception:
            continue
    return None


def _try_quote(primary, fallback, *args, **kwargs):
    q = quote_exact_input_single_struct(primary, *args, **kwargs)
    if q.get("reason") == "rpc_error" and fallback is not None:
        q2 = quote_exact_input_single_struct(fallback, *args, **kwargs)
        if q2.get("ok") or q2.get("reason") != "rpc_error":
            return q2
    return q


def block_at_unix(ts: int) -> int | None:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = (
        f"SELECT block_number FROM eatlq.uniswap_v3_swaps "
        f"WHERE block_time >= toDateTime('{dt}', 'UTC') "
        f"ORDER BY block_time ASC LIMIT 1 FORMAT TabSeparated"
    )
    try:
        rows = _ch(sql, timeout=15)
        if rows and rows[0]:
            return int(rows[0][0])
    except Exception:
        return None
    return None


def replay_horizons(primary, fallback, token, fee_bps, entry_block, entry_ts):
    """Quote WETH->token at entry_block (micro 0.0005 WETH baseline for price),
    then sell at entry_block + each horizon's block."""
    out = {h: None for h in EXIT_LABELS}
    micro_in = int(0.0005 * 10**18)
    buy_q = _try_quote(primary, fallback, WETH_BASE, token, micro_in, fee_bps,
                        block_identifier=entry_block)
    if not buy_q.get("ok") or buy_q.get("amount_out", 0) <= 0:
        return out, buy_q.get("reason", "")
    tokens_bought = int(buy_q["amount_out"])

    for h in EXIT_LABELS:
        exit_ts = entry_ts + EXIT_SECONDS[h]
        exit_block = block_at_unix(exit_ts)
        if not exit_block:
            continue
        sell_q = _try_quote(primary, fallback, token, WETH_BASE, tokens_bought, fee_bps,
                            block_identifier=exit_block)
        if not sell_q.get("ok") or sell_q.get("amount_out", 0) <= 0:
            if sell_q.get("reason") == "rpc_error":
                continue
            out[h] = -1.0
            continue
        weth_back = int(sell_q["amount_out"])
        out[h] = round((weth_back - micro_in) / micro_in, 6)
    return out, ""


def apply_trailing_2h(horizons: dict, sell_ok: bool, cost_pct: float,
                       per_leg: float = 0.012,
                       tp1=0.05, tp1_sz=0.30, tp2=0.15, tp2_sz=0.30,
                       sl=-0.10, time_h="2h", trailing=0.03) -> dict:
    if not sell_ok:
        return {"net": -1.0, "exit": "sell_blocked", "legs": 0}
    traj = [(h, EXIT_SECONDS[h], horizons[h])
            for h in EXIT_LABELS if horizons.get(h) is not None]
    if not traj:
        return {"net": 0.0, "exit": "no_data", "legs": 0}
    limit = EXIT_SECONDS[time_h]
    remaining = 1.0
    realised = 0.0
    peak = 0.0
    tp1_hit = tp2_hit = False
    exit_reason = "time"
    legs = 0

    for label, sec, ret in traj:
        if sec > limit:
            break
        peak = max(peak, ret)
        if remaining > 0 and ret <= sl:
            realised += remaining * (ret - per_leg); legs += 1
            remaining = 0; exit_reason = "sl"; break
        if remaining > 0 and trailing is not None and peak >= tp1 and (peak - ret) >= trailing:
            realised += remaining * (ret - per_leg); legs += 1
            remaining = 0; exit_reason = "trailing"; break
        if remaining > 0 and not tp2_hit and ret >= tp2:
            sz = min(remaining, tp2_sz)
            realised += sz * (ret - per_leg); remaining -= sz; tp2_hit = True; legs += 1
        if remaining > 0 and not tp1_hit and ret >= tp1:
            sz = min(remaining, tp1_sz)
            realised += sz * (ret - per_leg); remaining -= sz; tp1_hit = True; legs += 1

    if remaining > 0:
        last_ret = None
        for _, sec, ret in traj:
            if sec <= limit:
                last_ret = ret
        if last_ret is not None:
            realised += remaining * (last_ret - per_leg); legs += 1

    realised -= cost_pct
    return {"net": round(realised, 6), "exit": exit_reason, "legs": legs,
            "tp1_hit": tp1_hit, "tp2_hit": tp2_hit}


def pf(vals):
    pos = sum(v for v in vals if v > 0)
    neg = abs(sum(v for v in vals if v < 0))
    if neg > 0:
        return round(pos / neg, 4)
    if pos > 0:
        return float("inf")
    return None


def main() -> int:
    args = parse_args()
    if args.no_exclude_0x6ff5:
        args.exclude_0x6ff5 = False
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    fee_list = args.fee_bps_list.strip()
    rpc_url = args.rpc_url or _load_env_rpc()
    if not rpc_url:
        print("ERROR: no RPC URL", file=sys.stderr); return 2
    rpc = JsonRpcClient(rpc_url, delay_s=args.rpc_delay)
    fallback = JsonRpcClient("https://mainnet.base.org", delay_s=max(args.rpc_delay, 0.1))
    cost_model = RealCostModel(rpc=rpc, fallback_rpc=fallback)
    sender_map = _load_sender_map()

    started = time.time()
    print(f"[discover] lookback={args.lookback_days}d "
          f"pool_max_age={args.pool_max_age_sec}s onset_thresh={args.onset_thresh} "
          f"max_pools={args.max_pools}", flush=True)
    onsets = discover_historical_onsets(args)
    print(f"[discover] {len(onsets)} qualifying pool onsets found", flush=True)

    results = []
    skipped = {"is_0x6ff5": 0, "fee_mismatch": 0, "buy_quote_fail": 0}
    for i, p in enumerate(onsets, 1):
        if args.exclude_0x6ff5:
            sm = sender_map.get(p["pool"], {})
            if str(sm.get("dominant_sender", "")).lower() == SENDER_0X6FF5:
                skipped["is_0x6ff5"] += 1
                continue
        # On-chain fee check — RPC returns protocol encoding (100/500/3000/10000).
        # fee_bps_meta from discovered_pools uses basis-point encoding (1/5/30/100).
        # Convert meta→protocol for Quoter calls; guard against unknown tiers.
        fee_on = onchain_pool_fee(rpc, fallback, p["pool"])
        _meta_to_proto = {1: 100, 5: 500, 30: 3000, 100: 10000}
        fee = fee_on if fee_on else _meta_to_proto.get(p["fee_bps_meta"], 0)
        if fee not in (100, 500, 3000, 10000):
            skipped["fee_mismatch"] += 1
            continue

        entry_ts = p["entry_ts"]
        entry_block = p["entry_block"]
        cb = cost_model.estimate(token=p["token"], fee_bps=fee,
                                  notional_usd=args.notional_usd,
                                  block_identifier=entry_block)
        horizons, replay_err = replay_horizons(rpc, fallback, p["token"], fee,
                                                entry_block, entry_ts)
        outcome = apply_trailing_2h(
            horizons,
            sell_ok=cb.sell_path_ok and not cb.honeypot_flag,
            cost_pct=cb.round_trip_cost_pct(),
            per_leg=args.per_leg_cost_pct,
        )
        results.append({
            "pool": p["pool"],
            "token": p["token"],
            "fee_bps": fee,
            "fee_bps_meta": p["fee_bps_meta"],
            "entry_ts": entry_ts,
            "entry_block": entry_block,
            "heat": p["heat"],
            "onset_lag_sec": p["onset_lag"],
            "pool_age_at_entry_sec": p["pool_age_at_entry"],
            "cost_total_pct": round(cb.round_trip_cost_pct(), 6),
            "sell_path_ok": cb.sell_path_ok,
            "honeypot_flag": cb.honeypot_flag,
            "rpc_error": cb.rpc_error,
            **{f"ret_{h}": horizons[h] for h in EXIT_LABELS},
            "trailing_2h_net": outcome["net"],
            "exit_reason": outcome["exit"],
            "tp1_hit": outcome.get("tp1_hit"),
            "tp2_hit": outcome.get("tp2_hit"),
        })
        if i % 25 == 0:
            print(f"  [progress] {i}/{len(onsets)} elapsed={int(time.time()-started)}s", flush=True)

    # Write per-signal CSV
    if results:
        keys = list(results[0].keys())
        with (out_dir / "per_signal.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(results)

    # Aggregate
    decidable = [r for r in results if not r["rpc_error"]]
    nets = [r["trailing_2h_net"] for r in decidable
            if isinstance(r["trailing_2h_net"], (int, float))]
    pool_share: dict = {}
    for r in decidable:
        pool_share[r["pool"]] = pool_share.get(r["pool"], 0) + 1
    top_pool_share = (max(pool_share.values()) / len(decidable)) if decidable else 0.0

    # Per fee tier
    by_fee = {}
    for fee in (100, 500, 3000, 10000):
        sub = [r for r in decidable if r["fee_bps"] == fee]
        sub_nets = [r["trailing_2h_net"] for r in sub if isinstance(r["trailing_2h_net"], (int, float))]
        by_fee[str(fee)] = {
            "n": len(sub),
            "unique_pools": len({r["pool"] for r in sub}),
            "mean": round(sum(sub_nets) / max(len(sub_nets), 1), 6) if sub_nets else None,
            "win_rate": round(sum(1 for v in sub_nets if v > 0) / max(len(sub_nets), 1), 4) if sub_nets else None,
            "pf": pf(sub_nets),
            "sell_fail_rate": round(sum(1 for r in sub if not r["sell_path_ok"]) / max(len(sub), 1), 4),
        }

    # Diversity + verdict
    pass_diversity = (len(pool_share) >= 30 and top_pool_share <= 0.15)
    overall_pf = pf(nets)
    pass_pf = overall_pf is not None and overall_pf != float("inf") and overall_pf >= 1.3
    pass_n = len(nets) >= 30
    verdict = "PASS" if (pass_diversity and pass_pf and pass_n) else "FAIL"

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "alpha_id": "AR-SP088-HISTORICAL-BACKTEST",
        "lookback_days": args.lookback_days,
        "params": {
            "pool_max_age_sec": args.pool_max_age_sec,
            "onset_thresh": args.onset_thresh,
            "bucket_minutes": args.bucket_minutes,
            "fee_bps_list": fee_list or "100,500,3000,10000",
            "onset_max_sec": args.onset_max_sec,
            "notional_usd": args.notional_usd,
            "per_leg_cost_pct": args.per_leg_cost_pct,
            "exclude_0x6ff5": args.exclude_0x6ff5,
            "policy": "trailing_5_pull3_2h",
        },
        "discovery": {
            "onsets_total": len(onsets),
            "skipped": skipped,
            "results_total": len(results),
            "decidable": len(decidable),
            "rpc_error_count": len(results) - len(decidable),
        },
        "diversity": {
            "unique_pools": len(pool_share),
            "top_pool_share": round(top_pool_share, 4),
            "unique_pools_min": 30,
            "top_pool_share_max": 0.15,
            "passes": pass_diversity,
        },
        "trailing_2h_overall": {
            "n": len(nets),
            "mean_return": round(sum(nets) / max(len(nets), 1), 6) if nets else None,
            "win_rate": round(sum(1 for v in nets if v > 0) / max(len(nets), 1), 4) if nets else None,
            "pf": overall_pf,
            "pf_threshold": 1.3,
            "n_min": 30,
            "passes_pf": pass_pf,
            "passes_n": pass_n,
            "sell_fail_rate": round(sum(1 for r in decidable if not r["sell_path_ok"]) / max(len(decidable), 1), 4),
            "honeypot_rate": round(sum(1 for r in decidable if r["honeypot_flag"]) / max(len(decidable), 1), 4),
        },
        "by_fee": by_fee,
        "verdict": verdict,
        "elapsed_sec": round(time.time() - started, 1),
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    # Markdown readout
    next_step = (
        "PASS — proceed to (a) 24-48h forward sanity on `logs/sprint087_expanded_paper_shadow.csv`; "
        "(b) human Sprint061 gates (allowance cap, private key protocol, panic clear, max position); "
        "(c) canary at $5–10 notional with kill-switch."
        if verdict == "PASS" else
        "FAIL — see by_fee breakdown to identify if any tier still has edge; "
        "consider widening lookback or relaxing first-signal-only window if pool count too low."
    )

    md = [
        f"# Sprint088 — Historical Backtest ({args.lookback_days}d)",
        "",
        f"Generated: {summary['generated_at_utc']}  ",
        f"Policy: **trailing_5_pull3_2h** · Notional: **${args.notional_usd:.0f}U** · "
        f"Exclude 0x6ff5: **{args.exclude_0x6ff5}**",
        "",
        f"## Verdict: **{verdict}**",
        "",
        next_step,
        "",
        "## Diversity gate",
        "",
        f"- unique_pools: **{summary['diversity']['unique_pools']}** (min 30) → "
        f"{'✓' if summary['diversity']['unique_pools'] >= 30 else '✗'}",
        f"- top_pool_share: **{summary['diversity']['top_pool_share']}** (max 0.15) → "
        f"{'✓' if summary['diversity']['top_pool_share'] <= 0.15 else '✗'}",
        "",
        "## Trailing 2h (real cost @ $10)",
        "",
        f"- n = **{summary['trailing_2h_overall']['n']}** (min 30) → "
        f"{'✓' if pass_n else '✗'}",
        f"- mean = {summary['trailing_2h_overall']['mean_return']}",
        f"- win_rate = {summary['trailing_2h_overall']['win_rate']}",
        f"- **PF = {summary['trailing_2h_overall']['pf']}** (threshold 1.3) → "
        f"{'✓' if pass_pf else '✗'}",
        f"- sell_fail_rate = {summary['trailing_2h_overall']['sell_fail_rate']}",
        f"- honeypot_rate = {summary['trailing_2h_overall']['honeypot_rate']}",
        "",
        "## Per fee tier",
        "",
        "| fee_bps | n | unique pools | mean | WR | PF | sell_fail |",
        "|---|--:|--:|--:|--:|--:|--:|",
    ]
    for fee, s in summary["by_fee"].items():
        md.append(f"| {fee} | {s['n']} | {s['unique_pools']} | {s['mean']} | {s['win_rate']} | {s['pf']} | {s['sell_fail_rate']} |")
    md += [
        "",
        "## Discovery",
        "",
        f"- onsets scanned: {summary['discovery']['onsets_total']}",
        f"- skipped: {summary['discovery']['skipped']}",
        f"- decidable: {summary['discovery']['decidable']}",
        f"- rpc errors (excluded): {summary['discovery']['rpc_error_count']}",
        f"- elapsed: {summary['elapsed_sec']}s",
        "",
        "## Full summary",
        "",
        "```json",
        json.dumps(summary, indent=2),
        "```",
    ]
    (out_dir / "readout.md").write_text("\n".join(md) + "\n")
    print(json.dumps({"verdict": verdict, "pf": overall_pf, "n": len(nets),
                       "unique_pools": len(pool_share), "top_pool_share": round(top_pool_share, 4)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
