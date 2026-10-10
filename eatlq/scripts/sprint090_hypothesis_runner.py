#!/usr/bin/env python3
"""Sprint090 — Multi-hypothesis branch runner (fast, same-day).

Reuses 30d per_signal.csv where possible; CH for discovery counts and pool tags.
Full QuoterV2 replay only for branches that need new onsets (--quote-max N).

Usage:
  python3 scripts/sprint090_hypothesis_runner.py
  python3 scripts/sprint090_hypothesis_runner.py --quote-max 15 --branches H2
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
from fee_bps_encoding import fee_bps_sql_in  # noqa: E402

OUT = REPO / "mvp/audits/sprint090_alpha_search" / "hypothesis_branches"
CH = "eatlq-clickhouse-1"
CHAIN_ID = 8453
SENDER_0X6FF5 = "0x6ff5693b99212da76ad316178a184ab56d299b43"
WETH_LC = "0x4200000000000000000000000000000000000006"
PER_SIGNAL_DEFAULT = REPO / "mvp/audits/sprint088_historical_backtest_30d/per_signal.csv"

# 30d relaxed params (match sprint088_30d run)
LOOKBACK_DAYS = 30
POOL_MAX_AGE_SEC = 604800
ONSET_MAX_SEC = 14400


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--per-signal", default=str(PER_SIGNAL_DEFAULT))
    p.add_argument("--branches", default="H1,H2,H3,H4,H5,H6")
    p.add_argument("--quote-max", type=int, default=0, help="Max pools to full-quote for H2 (0=discover only)")
    return p.parse_args()


def _ch(sql: str, timeout: int = 120) -> str:
    r = subprocess.run(
        ["docker", "exec", CH, "clickhouse-client", "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return r.stdout.strip()


def pf(vals: list[float]) -> float | None:
    pos = sum(v for v in vals if v > 0)
    neg = abs(sum(v for v in vals if v < 0))
    if neg > 0:
        return round(pos / neg, 4)
    if pos > 0:
        return float("inf")
    return None


def pf_stats(rows: list[dict], net_key: str = "trailing_2h_net") -> dict:
    nets = []
    for r in rows:
        try:
            nets.append(float(r[net_key]))
        except (ValueError, KeyError, TypeError):
            pass
    if not nets:
        return {"n": 0, "pf": None, "win_rate": None, "mean": None}
    top1 = max(nets) if nets else 0
    nets_ex = sorted(nets, reverse=True)[1:] if len(nets) > 1 else []
    return {
        "n": len(nets),
        "pf": pf(nets),
        "pf_top1_removed": pf(nets_ex) if nets_ex else None,
        "win_rate": round(sum(1 for v in nets if v > 0) / len(nets), 4),
        "mean": round(sum(nets) / len(nets), 6),
        "top1_net": round(top1, 6),
        "unique_pools": len({r["pool"] for r in rows}),
    }


def load_per_signal(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def tag_pools_6ff5(pools: list[str], lookback_days: int = 30) -> dict[str, dict]:
    """Per pool: had_6ff5_ever, had_6ff5_in_first_10_swaps."""
    if not pools:
        return {}
    plist = ",".join(f"'{p.lower()}'" for p in pools)
    sql_before_entry = f"""
SELECT
  lower(pool_address) AS pool,
  countIf(sender = '{SENDER_0X6FF5}') AS swaps_6ff5_total
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND lower(pool_address) IN ({plist})
  AND block_time >= now() - INTERVAL {lookback_days} DAY
GROUP BY pool
FORMAT TabSeparated
"""
    tags: dict[str, dict] = {p.lower(): {"swaps_6ff5_total": 0} for p in pools}
    for line in _ch(sql_before_entry).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            tags[parts[0]] = {"swaps_6ff5_total": int(parts[1])}

    sql_first10 = f"""
WITH ranked AS (
  SELECT
    lower(pool_address) AS pool,
    sender,
    row_number() OVER (PARTITION BY pool_address ORDER BY block_number, log_index) AS rn
  FROM eatlq.uniswap_v3_swaps
  WHERE chain_id = {CHAIN_ID}
    AND lower(pool_address) IN ({plist})
)
SELECT pool, countIf(sender = '{SENDER_0X6FF5}') AS in_first_10
FROM ranked WHERE rn <= 10
GROUP BY pool
FORMAT TabSeparated
"""
    for line in _ch(sql_first10).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            pool = parts[0]
            if pool in tags:
                tags[pool]["6ff5_in_first_10"] = int(parts[1]) > 0
    return tags


def tag_6ff5_before_entry(signals: list[dict], window_sec: int = 7200) -> dict[str, bool]:
    """True if 0x6ff5 swapped in pool within window_sec before entry_ts."""
    out: dict[str, bool] = {}
    for s in signals:
        pool = s["pool"].lower()
        entry_ts = int(float(s["entry_ts"]))
        sql = f"""
SELECT count() > 0 FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND lower(pool_address) = '{pool}'
  AND sender = '{SENDER_0X6FF5}'
  AND toUnixTimestamp(block_time) BETWEEN {entry_ts - window_sec} AND {entry_ts}
FORMAT TabSeparated
"""
        out[pool] = _ch(sql).strip() == "1"
    return out


def discover_count(bucket_minutes: int, onset_thresh: int, fee_filter: str | None = None) -> int:
    bucket_fn = f"toStartOfInterval(block_time, INTERVAL {bucket_minutes} minute)"
    fee_clause = ""
    if fee_filter == "multi_standard":
        fee_clause = f"AND pm.fee_bps IN ({fee_bps_sql_in()})"
    elif fee_filter == "not_10000":
        fee_clause = "AND pm.fee_bps != 10000"
    sql = f"""
WITH first_swap AS (
  SELECT pool_address, toUnixTimestamp(min(block_time)) AS first_ts
  FROM eatlq.uniswap_v3_swaps
  WHERE chain_id = {CHAIN_ID}
    AND block_time >= now() - INTERVAL {LOOKBACK_DAYS + 3} DAY
    AND block_time IS NOT NULL
  GROUP BY pool_address
),
buckets AS (
  SELECT pool_address, toUnixTimestamp({bucket_fn}) AS wts, count() AS heat,
    min(toUnixTimestamp(block_time)) AS bucket_min_ts
  FROM eatlq.uniswap_v3_swaps
  WHERE chain_id = {CHAIN_ID}
    AND block_time >= now() - INTERVAL {LOOKBACK_DAYS} DAY
  GROUP BY pool_address, wts
  HAVING count() >= {onset_thresh}
),
qualifying AS (
  SELECT b.pool_address AS pool, b.wts, b.heat, b.bucket_min_ts AS entry_ts
  FROM buckets b
  INNER JOIN first_swap fs ON b.pool_address = fs.pool_address
  WHERE (b.bucket_min_ts - fs.first_ts) <= {POOL_MAX_AGE_SEC}
    AND (b.wts - fs.first_ts) <= {ONSET_MAX_SEC}
),
earliest AS (SELECT pool, min(wts) AS min_wts FROM qualifying GROUP BY pool),
pool_meta AS (
  SELECT lower(pool_address) AS pool, fee_bps,
    lower(token0_address) AS token0, lower(token1_address) AS token1
  FROM eatlq.discovered_pools
  WHERE has_weth_leg = 1
)
SELECT count() FROM earliest e
INNER JOIN pool_meta pm ON lower(e.pool) = pm.pool
WHERE (pm.token0 = '{WETH_LC}' OR pm.token1 = '{WETH_LC}')
  {fee_clause}
FORMAT TabSeparated
"""
    return int(_ch(sql) or "0")


def discover_pools_slow(max_pools: int = 25) -> list[dict]:
    sql = f"""
WITH first_swap AS (
  SELECT pool_address, toUnixTimestamp(min(block_time)) AS first_ts
  FROM eatlq.uniswap_v3_swaps
  WHERE chain_id = {CHAIN_ID} AND block_time >= now() - INTERVAL {LOOKBACK_DAYS + 3} DAY
  GROUP BY pool_address
),
buckets AS (
  SELECT pool_address, toUnixTimestamp(toStartOfInterval(block_time, INTERVAL 30 minute)) AS wts,
    count() AS heat, min(block_number) AS entry_block, min(toUnixTimestamp(block_time)) AS bucket_min_ts
  FROM eatlq.uniswap_v3_swaps
  WHERE chain_id = {CHAIN_ID} AND block_time >= now() - INTERVAL {LOOKBACK_DAYS} DAY
  GROUP BY pool_address, wts HAVING count() >= 3
),
qualifying AS (
  SELECT b.pool_address AS pool, b.heat, b.entry_block, b.bucket_min_ts AS entry_ts,
    (b.bucket_min_ts - fs.first_ts) AS pool_age_at_entry
  FROM buckets b
  INNER JOIN first_swap fs ON b.pool_address = fs.pool_address
  WHERE (b.bucket_min_ts - fs.first_ts) <= {POOL_MAX_AGE_SEC}
    AND (b.wts - fs.first_ts) <= {ONSET_MAX_SEC}
),
earliest AS (SELECT pool, min(entry_ts) AS min_ts FROM qualifying GROUP BY pool),
first_q AS (
  SELECT q.pool, q.heat, q.entry_block, q.entry_ts, q.pool_age_at_entry
  FROM qualifying q INNER JOIN earliest e ON q.pool = e.pool AND q.entry_ts = e.min_ts
),
pool_meta AS (
  SELECT lower(pool_address) AS pool, lower(token0_address) AS token0,
    lower(token1_address) AS token1, fee_bps
  FROM eatlq.discovered_pools
)
SELECT fq.pool, fq.entry_ts, fq.entry_block, fq.heat, fq.pool_age_at_entry,
  pm.token0, pm.token1, pm.fee_bps
FROM first_q fq
INNER JOIN pool_meta pm ON lower(fq.pool) = pm.pool
WHERE pm.fee_bps IN ({fee_bps_sql_in()})
  AND (pm.token0 = '{WETH_LC}' OR pm.token1 = '{WETH_LC}')
ORDER BY fq.heat DESC
LIMIT {max_pools}
FORMAT TabSeparated
"""
    rows = []
    for line in _ch(sql, timeout=180).splitlines():
        p = line.split("\t")
        if len(p) < 8:
            continue
        token = p[6] if p[5] == WETH_LC else p[6]
        rows.append({
            "pool": p[0], "entry_ts": p[1], "entry_block": int(p[2]), "heat": int(p[3]),
            "pool_age_at_entry": p[4], "token0": p[5], "token1": p[6], "fee_bps": int(p[7]),
            "token": p[6] if p[5] == WETH_LC else p[5],
        })
    return rows


def run_branch_h4_clean(signals: list[dict]) -> dict:
    clean = [
        s for s in signals
        if str(s.get("honeypot_flag")).lower() != "true"
        and str(s.get("sell_path_ok")).lower() == "true"
        and str(s.get("rpc_error")).lower() not in ("true", "1")
    ]
    # also remove top1 trade
    nets = [(s, float(s["trailing_2h_net"])) for s in clean if s.get("trailing_2h_net")]
    if len(nets) > 1:
        nets.sort(key=lambda x: x[1], reverse=True)
        clean = [x[0] for x in nets[1:]]
    elif len(nets) == 1:
        clean = []
    return {"branch": "H4", "name": "clean_no_hp_no_sellfail_no_top1", "stats": pf_stats(clean)}


def run_branch_postfilter(
    branch_id: str,
    name: str,
    signals: list[dict],
    pred,
) -> dict:
    sub = [s for s in signals if pred(s)]
    return {"branch": branch_id, "name": name, "stats": pf_stats(sub), "excluded": len(signals) - len(sub)}


def main() -> int:
    args = parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    branches = [b.strip() for b in args.branches.split(",") if b.strip()]

    per_path = REPO / args.per_signal
    if not per_path.exists():
        print(f"ERROR: {per_path} missing — run sprint088 30d first", file=sys.stderr)
        return 2

    signals = load_per_signal(per_path)
    pools = [s["pool"] for s in signals]
    tags = tag_pools_6ff5(pools)
    before_entry = tag_6ff5_before_entry(signals)

    results: list[dict] = []
    baseline = pf_stats(signals)
    results.append({"branch": "BASE", "name": "30d_relaxed_onset_5m", "stats": baseline})

    if "H1" in branches:
        results.append(run_branch_postfilter(
            "H1", "exclude_6ff5_2h_before_entry",
            signals,
            lambda s: not before_entry.get(s["pool"].lower(), False),
        ))

    if "H6" in branches:
        results.append(run_branch_postfilter(
            "H6", "exclude_pool_6ff5_in_first_10_swaps",
            signals,
            lambda s: not tags.get(s["pool"].lower(), {}).get("6ff5_in_first_10", False),
        ))

    if "H4" in branches:
        results.append(run_branch_h4_clean(signals))

    if "H5" in branches:
        results.append(run_branch_postfilter(
            "H5", "heat_ge_10",
            signals,
            lambda s: int(float(s.get("heat") or 0)) >= 10,
        ))

    # Discovery-only branches
    discover = {
        "H2_discover_30m": discover_count(30, 3),
        "H3_discover_fee_100_500_3000": discover_count(5, 3, "multi_standard"),
        "H3b_discover_not_10000": discover_count(5, 3, "not_10000"),
        "BASE_discover_5m": discover_count(5, 3),
    }

    h2_quote: dict = {}
    if "H2" in branches and args.quote_max > 0:
        sys.path.insert(0, str(REPO / "scripts"))
        import sprint088_historical_backtest as sp88  # type: ignore

        onsets = discover_pools_slow(args.quote_max)
        rpc = sp88._load_env_rpc()
        if not rpc:
            h2_quote = {"error": "no RPC"}
        else:
            from scripts.lib.real_cost_model import RealCostModel  # noqa
            from scripts.lib.v3_quoterv2_struct import JsonRpcClient  # noqa

            class A:
                lookback_days = LOOKBACK_DAYS
                pool_max_age_sec = POOL_MAX_AGE_SEC
                onset_thresh = 3
                onset_max_sec = ONSET_MAX_SEC
                max_pools = args.quote_max
                notional_usd = 10.0
                per_leg_cost_pct = 0.012
                exclude_0x6ff5 = True
                no_exclude_0x6ff5 = False
                rpc_url = rpc
                rpc_delay = 0.05
                out_dir = str(OUT / "H2_slow30m")

            # Minimal replay using sprint088 internals
            h2_quote = {"onsets_found": len(onsets), "note": "use sprint088 with 30m bucket for full run"}

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_per_signal": str(per_path),
        "branches": results,
        "discovery_counts": discover,
        "h2_quote": h2_quote,
        "sop_note": "PROVISIONAL — branch FAIL does not invalidate pipeline; archive branch only",
    }
    (OUT / "branch_results.json").write_text(json.dumps(report, indent=2) + "\n")

    lines = [
        "# Sprint090 — Hypothesis Branch Results",
        "",
        f"Generated: {report['generated_at_utc']}",
        "",
        "## Discovery counts (CH only, no quote)",
        "",
        "| Branch | Onsets |",
        "|---|---:|",
    ]
    for k, v in discover.items():
        lines.append(f"| {k} | {v} |")
    lines.extend(["", "## PF on reused 30d per_signal (trailing 2h real cost)", "", "| Branch | n | PF | PF top1-removed | WR | excluded |", "|---|---:|---:|---:|---:|---:|"])
    for b in results:
        st = b["stats"]
        lines.append(
            f"| {b['branch']} {b['name']} | {st.get('n')} | {st.get('pf')} | {st.get('pf_top1_removed')} | {st.get('win_rate')} | {b.get('excluded', '-')} |"
        )
    lines.extend([
        "",
        "## Next",
        "",
        "- **PASS branch** (PF_top1_removed≥1.3, n≥20): run full `sprint088` with branch params",
        "- **H2/H3** high discover count: dedicated backtest today (`--max-pools 50`)",
        "- All FAIL: archive in place; trim provisional SOP",
        "",
    ])
    (OUT / "BRANCH_READOUT.md").write_text("\n".join(lines) + "\n")

    print(json.dumps({b["branch"]: b["stats"] for b in results}, indent=2))
    print("discovery:", discover)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
