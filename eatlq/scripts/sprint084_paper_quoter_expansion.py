#!/usr/bin/env python3
"""Sprint084 — A+B paper/Quoter expansion (research-only, no live trading).

- Bounded Quoter historical labels → audit supplement CSV (not production logs).
- Optional CH onset supplemental rows for near-miss stratum dilution.
- Paper-shadow collection plan + health (observation only).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lib.v3_quoterv2_struct import WETH_BASE, JsonRpcClient, quote_exact_input_single_struct
from sprint084_pool_entry_quality_economic_label_backfill import (  # noqa: E402
    GAS_PCT,
    HORIZON_30M,
    PAPER_QUOTER_SOURCES,
    SOURCE_QUOTER,
    ch_block_at,
    count_paper_quoter_pools,
    fetch_pool_registry,
    floor_5m_key,
    load_prep_rows,
    load_rpc,
    parse_dt,
    q,
    quoter_round_trip,
    run_ch,
    sql_dt,
)

DEFAULT_AUTOPSY = "mvp/audits/sprint084_pool_entry_quality/regime_failure_autopsy_v1"
DEFAULT_PREP = (
    "mvp/audits/sprint084_pool_entry_quality/prep_v3/"
    "pool_entry_quality_candidate_dataset.csv"
)
DEFAULT_PREP_EXPANDED = (
    "mvp/audits/sprint084_pool_entry_quality/paper_quoter_expansion_v1/"
    "prep_v3_plus_expansion.csv"
)
DEFAULT_BACKFILLED = (
    "mvp/audits/sprint084_pool_entry_quality/economic_label_backfill_v2/"
    "pool_entry_quality_candidate_dataset_backfilled.csv"
)
DEFAULT_OUT = "mvp/audits/sprint084_pool_entry_quality/paper_quoter_expansion_v1"

VELOCITY_NEAR_MISS = (-0.3, 0.0)
MIN_PAPER_QUOTER_POOLS_TARGET = 25
IDEAL_PAPER_QUOTER_POOLS = 35


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint084 paper/Quoter expansion A+B")
    p.add_argument("--autopsy-dir", default=DEFAULT_AUTOPSY)
    p.add_argument("--prep-dataset", default="")
    p.add_argument("--backfilled-dataset", default=DEFAULT_BACKFILLED)
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    p.add_argument("--top-pools", type=int, default=15)
    p.add_argument("--prefer-fees", default="500,10000")
    p.add_argument("--max-quoter-rows", type=int, default=120)
    p.add_argument("--max-rpc-calls", type=int, default=2000)
    p.add_argument("--max-runtime-sec", type=int, default=900)
    p.add_argument("--rpc-fail-abort-rate", type=float, default=0.92)
    p.add_argument("--supplement-ch-rows-per-pool", type=int, default=4)
    p.add_argument("--supplement-max-pools", type=int, default=35)
    p.add_argument("--skip-ch-supplement", action="store_true")
    p.add_argument("--skip-quoter", action="store_true")
    p.add_argument("--paper-shadow-once", action="store_true", help="Run one sprint055 poll cycle")
    return p.parse_args()


def as_float(v: Any) -> float | None:
    if v in (None, "", "\\N"):
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def velocity_in_near_miss(r: dict) -> bool:
    v = as_float(r.get("price_velocity_5m"))
    return v is not None and VELOCITY_NEAR_MISS[0] <= v < VELOCITY_NEAR_MISS[1]


def select_top_pools(
    prep_rows: list[dict],
    backfilled_rows: list[dict],
    expansion_targets: list[dict],
    registry: dict[str, dict],
    prefer_fees: set[int],
    top_n: int,
) -> list[dict]:
    pq = {
        r["pool"].lower()
        for r in backfilled_rows
        if (r.get("T_POOL_FWD_30M_source") or "") in PAPER_QUOTER_SOURCES
    }
    nm_counts = Counter(
        r["pool"].lower() for r in backfilled_rows if velocity_in_near_miss(r)
    )
    scored: list[tuple[float, str, dict]] = []
    seen: set[str] = set()

    def score_pool(pool: str, fee: int | None, pri: float) -> None:
        pool = pool.lower()
        if pool in seen:
            return
        seen.add(pool)
        meta = registry.get(pool, {})
        fee_v = fee if fee is not None else meta.get("fee_bps")
        in_pq = pool in pq
        nm = nm_counts.get(pool, 0)
        fee_bonus = 10 if fee_v in prefer_fees else 0
        s = pri + fee_bonus + nm * 2 + (5 if not in_pq else 0)
        scored.append(
            (
                s,
                pool,
                {
                    "pool": pool,
                    "fee_bps": fee_v,
                    "in_paper_quoter_before": in_pq,
                    "near_miss_stratum_rows": nm,
                    "priority_score": s,
                },
            )
        )

    for t in expansion_targets:
        pool = (t.get("pool") or "").lower()
        fee_raw = t.get("fee_tier_mode")
        fee = int(fee_raw) if fee_raw and fee_raw.isdigit() else registry.get(pool, {}).get("fee_bps")
        pv_med = as_float(t.get("price_velocity_median"))
        near = 3 if pv_med is not None and VELOCITY_NEAR_MISS[0] <= pv_med < VELOCITY_NEAR_MISS[1] else 0
        score_pool(pool, fee, float(t.get("priority_score") or 0) + near)

    for r in prep_rows:
        pool = r["pool"].lower()
        fee = registry.get(pool, {}).get("fee_bps")
        if fee not in prefer_fees:
            continue
        score_pool(pool, fee, nm_counts.get(pool, 0))

    scored.sort(key=lambda x: -x[0])
    chosen = [x[2] for x in scored[:top_n]]
    # Ensure we include all prefer-fee pools not yet in pq up to top_n
    for x in sorted(scored, key=lambda x: (-(0 if x[2]["fee_bps"] in prefer_fees else 1), -x[0])):
        if len(chosen) >= top_n:
            break
        if x[2] not in chosen:
            chosen.append(x[2])
    return chosen[:top_n]


def fetch_ch_supplement_rows(
    pool: str,
    per_pool_cap: int,
    activity_min: int = 5,
) -> list[dict]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=60)
    raw = run_ch(
        f"""
        SELECT window_start, price_velocity_5m, buy_pressure_ratio, activity_heat,
               token_volatility_5m
        FROM pool_features_5m
        WHERE chain_id = 8453
          AND lower(pool_address) = {q(pool.lower())}
          AND window_start >= toDateTime({q(sql_dt(start))}, 'UTC')
          AND window_start <= toDateTime({q(sql_dt(end))}, 'UTC')
          AND activity_heat >= {activity_min}
        ORDER BY window_start
        FORMAT TSVWithNames
        """,
        timeout=120,
    )
    out: list[dict] = []
    for r in raw:
        pv = as_float(r.get("price_velocity_5m"))
        if pv is None or not (VELOCITY_NEAR_MISS[0] <= pv < VELOCITY_NEAR_MISS[1]):
            continue
        t5 = datetime.fromisoformat(str(r["window_start"]).replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
        if t5.tzinfo is None:
            t5 = t5.replace(tzinfo=timezone.utc)
        et = t5.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        out.append(
            {
                "entry_id": f"exp_ch_{pool[:10]}_{et}",
                "source": "ch_pool_onset_candidate",
                "entry_time": et,
                "pool": pool.lower(),
                "token_other": "",
                "fee_bps": None,
                "fee_tier": None,
                "price_velocity_5m": pv,
                "buy_pressure_ratio": r.get("buy_pressure_ratio"),
                "activity_heat": r.get("activity_heat"),
                "token_volatility_5m": r.get("token_volatility_5m"),
                "pool_age_sec": None,
                "net_flow_liquidity_proxy": None,
                "onset_lag_sec": None,
                "T_POOL_FWD_30M": None,
                "T_POOL_FWD_4H": None,
                "T_POOL_FWD_30M_source": None,
                "T_POOL_FWD_4H_source": None,
                "T_ONSET_PASS": "true",
                "T_ENTRY_QUOTE_OK": None,
                "wallet_diagnostic_joined": "false",
                "expansion_supplement": "ch_near_miss_dilution_v1",
            }
        )
    if len(out) > per_pool_cap:
        step = max(1, len(out) // per_pool_cap)
        out = [out[i] for i in range(0, len(out), step)][:per_pool_cap]
    return out


def quoter_label_rows(
    rows: list[dict],
    registry: dict[str, dict],
    rpc: JsonRpcClient,
    target_pools: set[str],
    max_rows: int,
    max_calls: int,
    deadline: float,
    fail_abort_rate: float,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "rpc_attempted": 0,
        "rpc_filled": 0,
        "rpc_failed": 0,
        "quoter_revert_pools": set(),
        "pools_filled": set(),
    }
    pool_revert: set[str] = set()
    pool_rows = [r for r in rows if r["pool"].lower() in target_pools]
    by_pool: dict[str, list[dict]] = defaultdict(list)
    for r in pool_rows:
        by_pool[r["pool"].lower()].append(r)
    ordered: list[dict] = []
    # Pass 1: one mature row per pool (any stratum)
    for pool in sorted(by_pool):
        items = sorted(by_pool[pool], key=lambda x: x.get("entry_time", ""))
        ordered.append(items[len(items) // 2])
    # Pass 2: extra near-miss stratum rows
    for pool in sorted(by_pool, key=lambda p: -len(by_pool[p])):
        nm = [r for r in by_pool[pool] if velocity_in_near_miss(r)]
        for r in nm[:2]:
            if r not in ordered:
                ordered.append(r)

    for r in ordered:
        if time.time() > deadline or stats["rpc_attempted"] >= max_rows:
            break
        if rpc.calls >= max_calls:
            break
        pool = r["pool"].lower()
        if pool in pool_revert:
            continue
        meta = registry.get(pool, {})
        token = (r.get("token_other") or meta.get("token_other") or "").lower()
        fee = r.get("fee_bps") or r.get("fee_tier") or meta.get("fee_bps")
        if not token or fee in (None, ""):
            stats["rpc_failed"] += 1
            continue
        try:
            fee_bps = int(float(fee))
        except (TypeError, ValueError):
            continue
        entry_dt = parse_dt(r["entry_time"])
        if entry_dt + timedelta(seconds=HORIZON_30M) > datetime.now(timezone.utc):
            continue
        stats["rpc_attempted"] += 1
        eb = ch_block_at(pool, entry_dt)
        xb = ch_block_at(pool, entry_dt + timedelta(seconds=HORIZON_30M))
        if eb is None or xb is None:
            stats["rpc_failed"] += 1
            continue
        nr30, _ = quoter_round_trip(rpc, token, fee_bps, eb, xb)
        if nr30 is None:
            stats["rpc_failed"] += 1
            pool_revert.add(pool)
            stats["quoter_revert_pools"].add(pool)
        else:
            r["T_POOL_FWD_30M"] = nr30
            r["T_POOL_FWD_30M_source"] = SOURCE_QUOTER
            stats["rpc_filled"] += 1
            stats["pools_filled"].add(pool)
    stats["quoter_revert_pools"] = sorted(stats["quoter_revert_pools"])
    stats["pools_filled"] = sorted(stats["pools_filled"])
    return stats


def paper_shadow_row_from_quoter(r: dict, registry: dict[str, dict]) -> dict:
    pool = r["pool"].lower()
    meta = registry.get(pool, {})
    fee = r.get("fee_bps") or r.get("fee_tier") or meta.get("fee_bps")
    token = (r.get("token_other") or meta.get("token_other") or "").lower()
    weth_is_token1 = meta.get("weth_is_token1", True)
    nr = r.get("T_POOL_FWD_30M")
    return {
        "signal_time": r["entry_time"],
        "pool": pool,
        "token_other": token,
        "fee_bps": fee,
        "weth_is_token1": weth_is_token1,
        "pool_age_sec": r.get("pool_age_sec", ""),
        "onset_lag_sec": r.get("onset_lag_sec", ""),
        "activity_heat": r.get("activity_heat", ""),
        "buy_pressure_ratio": r.get("buy_pressure_ratio", ""),
        "price_velocity_5m": r.get("price_velocity_5m", ""),
        "composite_flag": "False",
        "entry_quote_ok": "True",
        "exit_30m_ok": "true" if nr is not None else "false",
        "net_return_30m": nr if nr is not None else "",
        "exit_4h_ok": "false",
        "net_return_4h": "",
        "gas_proxy_pct": GAS_PCT,
        "expansion_source": "sprint084_quoter_historical_v1",
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def run_paper_shadow_once(out_dir: Path, top_pools: list[str]) -> dict[str, Any]:
    import os
    import subprocess

    log_csv = out_dir / "sprint084_expansion_paper_shadow.csv"
    env = os.environ.copy()
    env["FEE_FILTER_ONLY"] = "500"
    env["TRACK_SUFFIX"] = "_084exp"
    env["LOG_CSV_OVERRIDE"] = str(log_csv.name)
    # Observation-only single poll; does not enable live trading.
    cmd = [sys.executable, str(SCRIPTS / "sprint055_fee100_paper_shadow.py"), "--once"]
    try:
        cp = subprocess.run(
            cmd,
            cwd=str(REPO),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return {
            "ok": cp.returncode == 0,
            "returncode": cp.returncode,
            "log_csv": str(log_csv.relative_to(REPO)),
            "stdout_tail": (cp.stdout or "")[-500:],
            "stderr_tail": (cp.stderr or "")[-500:],
            "watch_pools": top_pools,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    prefer_fees = {int(x) for x in args.prefer_fees.split(",") if x.strip()}

    prep_path = REPO / (
        args.prep_dataset
        or (
            DEFAULT_PREP_EXPANDED
            if (REPO / DEFAULT_PREP_EXPANDED).is_file()
            else DEFAULT_PREP
        )
    )
    expansion_targets = load_prep_rows(REPO / args.autopsy_dir / "expansion_target_pools.csv")
    prep_rows = load_prep_rows(prep_path)
    backfilled = load_prep_rows(REPO / args.backfilled_dataset)
    pools_all = {r["pool"].lower() for r in prep_rows}
    registry = fetch_pool_registry(pools_all, union_discovered=True)

    pq_before = count_paper_quoter_pools(backfilled)
    top_pool_meta = select_top_pools(
        prep_rows, backfilled, expansion_targets, registry, prefer_fees, args.top_pools
    )
    write_csv(out_dir / "expansion_top15_pools.csv", top_pool_meta)
    target_pools = {p["pool"] for p in top_pool_meta}

    # Merge base = prep + existing keys from backfilled
    existing_keys = {(r["pool"].lower(), floor_5m_key(r["entry_time"])) for r in prep_rows}
    merged = list(prep_rows)
    supplemental: list[dict] = []

    if not args.skip_ch_supplement:
        nm_counts = Counter(
            r["pool"].lower() for r in backfilled if velocity_in_near_miss(r)
        )
        top10_pools = {p for p, _ in nm_counts.most_common(10)}
        dilute_candidates = [
            p for p, n in sorted(nm_counts.items(), key=lambda x: x[1]) if n <= 3 and p not in top10_pools
        ][: args.supplement_max_pools]
        for pool in dilute_candidates:
            for row in fetch_ch_supplement_rows(pool, args.supplement_ch_rows_per_pool):
                key = (row["pool"], floor_5m_key(row["entry_time"]))
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                supplemental.append(row)
                merged.append(row)

    quoter_stats: dict[str, Any] = {"skipped": args.skip_quoter}
    paper_supplement: list[dict] = []
    if not args.skip_quoter:
        rpc = load_rpc()
        if not rpc:
            quoter_stats["error"] = "rpc_unavailable"
        else:
            deadline = time.time() + args.max_runtime_sec
            quoter_stats = quoter_label_rows(
                merged,
                registry,
                rpc,
                target_pools,
                args.max_quoter_rows,
                args.max_rpc_calls,
                deadline,
                args.rpc_fail_abort_rate,
            )
            for r in merged:
                if r.get("T_POOL_FWD_30M_source") == SOURCE_QUOTER:
                    paper_supplement.append(paper_shadow_row_from_quoter(r, registry))

    write_csv(out_dir / "historical_quoter_paper_shadow_supplement.csv", paper_supplement)
    write_csv(out_dir / "supplemental_pool_entries.csv", supplemental)
    write_csv(out_dir / "prep_v3_plus_expansion.csv", merged)

    ps_once: dict[str, Any] = {"skipped": True}
    if args.paper_shadow_once:
        ps_once = run_paper_shadow_once(out_dir, sorted(target_pools))

    flags = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "SPRINT084_PAPER_QUOTER_EXPANSION_DONE": True,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "PAPER_FORWARD_ALLOWED": "false",
        "MODEL_PROMOTION_ALLOWED": False,
        "NO_ALPHA_CLAIM": True,
        "paper_quoter_pools_before": pq_before,
        "target_pool_count": len(target_pools),
        "quoter_stats": quoter_stats,
        "supplemental_rows_added": len(supplemental),
        "paper_supplement_rows": len(paper_supplement),
        "merged_row_count": len(merged),
        "paper_shadow_once": ps_once,
        "min_paper_quoter_target": MIN_PAPER_QUOTER_POOLS_TARGET,
        "ideal_paper_quoter_target": IDEAL_PAPER_QUOTER_POOLS,
        "RECOMMENDED_NEXT_ACTION": "rerun_backfill_regime_v4",
    }
    (out_dir / "paper_quoter_expansion_flags.json").write_text(json.dumps(flags, indent=2) + "\n")

    plan = f"""# Sprint084 paper-shadow collection plan (observation only)

**Not paper-forward. Not live trading. No executor.**

## Contract
- QuoterV2 30m semantics per `docs/label-semantics-freeze.md`.
- `ch_sqrt_price_mtm_v1` is research MTM only — not live-forward Quoter.

## Top {len(top_pool_meta)} target pools
See `expansion_top15_pools.csv`.

## Bounded live paper-shadow (observation)
```bash
cd /opt/EATLQ
export FEE_FILTER_ONLY=500
export TRACK_SUFFIX=_084exp
export LOG_CSV_OVERRIDE=sprint084_expansion_paper_shadow.csv
# cron: */5 * * * * 
python3 scripts/sprint055_fee100_paper_shadow.py --once
```
Log: `mvp/audits/sprint084_pool_entry_quality/paper_quoter_expansion_v1/sprint084_expansion_paper_shadow.csv`

## Health check
```bash
python3 scripts/sprint084_paper_shadow_watch_health.py \\
  --expansion-dir mvp/audits/sprint084_pool_entry_quality/paper_quoter_expansion_v1
```

## Historical Quoter (this run)
Supplement: `historical_quoter_paper_shadow_supplement.csv` → consumed by economic_label_backfill_v3.
"""
    (out_dir / "paper_shadow_collection_plan.md").write_text(plan)

    print(json.dumps(flags, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
