#!/usr/bin/env python3
"""Sprint035A — live-active scoped pool mining (Base V3, WETH-leg) with liquidity/activity gates.

Excludes Sprint034 same-three-pool dead cohort. Read-only ClickHouse; bounded RPC metadata + quotes.
Optional hard-quota manifest compatible with `sprint030d_bounded_smoke_label.py --profile sprint035a_live_active_scoped_smoke`.

Does not print RPC URLs or secrets.

Usage:
  python3 scripts/sprint035a_live_active_pool_mining.py --write
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import itertools
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BUCKET_MS = 300_000
BUCKET_SEC = 300
CHAIN_ID = 8453

SPRINT034_DEAD_POOLS: frozenset[str] = frozenset(
    {
        "0x5d2091e0b3e0b516e0f3abd75242e9287c253499",
        "0x8aed442e9c17746c0eb85dd22981ce8974182892",
        "0x1e98a167082332e5e89146662e34c3c99481e5e6",
    }
)

# gates (CH + dashcam + RPC); USD not available in CH → document proxy in summary
MIN_SWAPS_24H = 8
MIN_BUCKETS_5M_24H = 3
MIN_SWAPS_60D = 15
MIN_BUCKETS_5M_60D = 6
MIN_ACTIVE_DAYS_60D = 1
MIN_DASHCAM_BUCKETS_OVERALL = 2
MAX_CH_CANDIDATES = 120
MAX_VIABILITY_POOLS = 40

HISTORICAL_BUCKET_MAX_AGE_SEC = 60 * 86400

OUT_SUMMARY = REPO / "mvp/diagnostics/sprint035a_live_active_mining_summary.json"
OUT_VIAB_CSV = REPO / "mvp/diagnostics/sprint035a_live_active_viability_audit.csv"
OUT_MANIFEST = REPO / "mvp/diagnostics/sprint035a_live_active_scoped_manifest.json"
OUT_AUDIT = REPO / "mvp/audits/sprint035a_live_active_pool_mining_report.md"


def load_sprint033a_functions() -> Any:
    p = REPO / "scripts/sprint033a_ch_backed_hard_quota_pipeline.py"
    spec = importlib.util.spec_from_file_location("sprint033a_gate_bundle", p)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load sprint033a_ch_backed_hard_quota_pipeline")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_dotenv_files() -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in ("infra/compose/.env", ".env"):
        fp = REPO / rel
        if not fp.exists():
            continue
        for line in fp.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def ch_live_pool_candidates(m: Any) -> tuple[list[dict[str, Any]], str]:
    dead = "', '".join(sorted(SPRINT034_DEAD_POOLS))
    sql = f"""
SELECT
  pa AS pool_address_audit_key,
  toUInt64(swaps_24h) AS swaps_24h,
  toUInt32(buckets_5m_24h) AS buckets_5m_24h,
  toUInt64(swaps_60d) AS swaps_60d,
  toUInt32(buckets_5m_60d) AS buckets_5m_60d,
  toUInt32(active_days_60d) AS active_days_60d
FROM (
  SELECT
    lower(s.pool_address) AS pa,
    countIf(coalesce(s.block_time, s.received_at_ms) >= now() - INTERVAL 24 HOUR) AS swaps_24h,
    uniqExactIf(
      intDiv(toUnixTimestamp64Milli(coalesce(s.block_time, s.received_at_ms)), {BUCKET_MS}) * {BUCKET_MS},
      coalesce(s.block_time, s.received_at_ms) >= now() - INTERVAL 24 HOUR
    ) AS buckets_5m_24h,
    countIf(coalesce(s.block_time, s.received_at_ms) >= now() - INTERVAL 60 DAY) AS swaps_60d,
    uniqExactIf(
      intDiv(toUnixTimestamp64Milli(coalesce(s.block_time, s.received_at_ms)), {BUCKET_MS}) * {BUCKET_MS},
      coalesce(s.block_time, s.received_at_ms) >= now() - INTERVAL 60 DAY
    ) AS buckets_5m_60d,
    uniqExactIf(
      toDate(coalesce(s.block_time, s.received_at_ms)),
      coalesce(s.block_time, s.received_at_ms) >= now() - INTERVAL 60 DAY
    ) AS active_days_60d
  FROM eatlq.uniswap_v3_swaps_enriched AS s
  INNER JOIN eatlq.discovered_pools AS dp
    ON dp.chain_id = {CHAIN_ID}
    AND lower(dp.pool_address) = lower(s.pool_address)
    AND lower(dp.pool_type) = 'v3'
    AND dp.has_weth_leg = 1
  WHERE s.chain_id = {CHAIN_ID}
    AND s.removed = 0
    AND lower(s.pool_address) NOT IN ('{dead}')
  GROUP BY pa
  HAVING swaps_24h >= {MIN_SWAPS_24H}
    AND buckets_5m_24h >= {MIN_BUCKETS_5M_24H}
    AND swaps_60d >= {MIN_SWAPS_60D}
    AND buckets_5m_60d >= {MIN_BUCKETS_5M_60D}
    AND active_days_60d >= {MIN_ACTIVE_DAYS_60D}
) AS x
ORDER BY swaps_24h DESC, pa ASC
LIMIT {MAX_CH_CANDIDATES}
FORMAT TabSeparated
""".strip()
    body = m.ch_docker_query(sql, timeout_sec=180.0)
    rows: list[dict[str, Any]] = []
    if not body:
        return rows, sql
    for line in body.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        pa = parts[0].lower()
        if pa in SPRINT034_DEAD_POOLS:
            continue
        rows.append(
            {
                "pool_address": pa,
                "swaps_24h_ch": int(parts[1]),
                "buckets_5m_24h_ch": int(parts[2]),
                "swaps_60d_ch": int(parts[3]),
                "buckets_5m_60d_ch": int(parts[4]),
                "active_days_60d_ch": int(parts[5]),
            }
        )
    return rows, sql


def filter_recent_bucket_secs(sorted_bsecs: list[int], now_sec: int, max_age_sec: int) -> list[int]:
    lo = now_sec - max_age_sec
    return [x for x in sorted_bsecs if x >= lo]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    if not args.write:
        print("pass --write", file=sys.stderr)
        return 1

    t0 = time.time()
    m = load_sprint033a_functions()
    env = {**load_dotenv_files(), **dict(os.environ)}
    dashcam_path = REPO / "logs/dashcam.csv"

    ping = (m.ch_docker_query("SELECT 1 FORMAT TabSeparated") or "").strip()
    clickhouse_ok = ping == "1"

    gates_doc = {
        "min_swaps_24h_ch_no_zero_activity": MIN_SWAPS_24H,
        "min_recent_5m_buckets_24h_ch": MIN_BUCKETS_5M_24H,
        "min_swaps_60d_ch_historical_viability_anchor": MIN_SWAPS_60D,
        "min_buckets_5m_60d_ch": MIN_BUCKETS_5M_60D,
        "min_active_days_60d_ch": MIN_ACTIVE_DAYS_60D,
        "min_dashcam_distinct_buckets_across_overlap": MIN_DASHCAM_BUCKETS_OVERALL,
        "volume_usd_floor_policy": (
            "not_computable_in_clickhouse_here_use_swaps_24h_as_activity_volume_proxy_do_not_substitute_alpha_evidence"
        ),
        "liquidity_usd_floor_policy": (
            "usd_requires_external_feed_rpc_gates_pass_nonzero_sqrt_price_and_V3_liquidity_field_as_oracle_substitute_only"
        ),
        "historical_entry_viability_gate": (
            "dashcam_bucket_calendar_plus_ch_block_mapping_quoter_probe_recent_preference_with_full_scan_fallback"
        ),
        "current_quote_viability_gate": "same_entry_quote_at_latest_chain_head_after_metadata_pass",
        "sprint034_dead_pool_exclusion": sorted(SPRINT034_DEAD_POOLS),
    }

    manifest_obj: dict[str, Any] | None = None
    pass_list: list[str] = []
    viability_rows: list[dict[str, str]] = []
    hard_quota_contraction_note = ""
    final_status = "BLOCKED_CLICKHOUSE_UNAVAILABLE"
    ch_sql_audit = ""

    if not clickhouse_ok:
        OUT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
        OUT_SUMMARY.write_text(
            json.dumps(
                {
                    "kind": "sprint035a_live_active_mining_summary",
                    "generated_at_unix": int(time.time()),
                    "clickhouse_reachable": False,
                    "final_decision_status": final_status,
                    "live_activity_gates": gates_doc,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        OUT_AUDIT.parent.mkdir(parents=True, exist_ok=True)
        OUT_AUDIT.write_text(
            "# Sprint035A — live-active pool mining\n\n**BLOCKED_CLICKHOUSE_UNAVAILABLE**\n",
            encoding="utf-8",
        )
        return 0

    candidates, ch_sql_audit = ch_live_pool_candidates(m)
    pool_addrs = [c["pool_address"] for c in candidates]
    dash_secs = m.dashcam_buckets_for_pools(set(pool_addrs) if pool_addrs else set(), dashcam_path)

    scored: list[dict[str, Any]] = []
    for c in candidates:
        p = c["pool_address"]
        hits = len(dash_secs.get(p, ()))
        if hits < MIN_DASHCAM_BUCKETS_OVERALL:
            continue
        scored.append({**c, "dashcam_distinct_buckets": hits})

    scored.sort(key=lambda z: (-z["swaps_24h_ch"], -z["buckets_5m_24h_ch"], z["pool_address"]))

    rpc_url = (env.get("LABEL_RPC_URL") or env.get("EVM_HTTP_URL") or "").strip()

    overlap_lo = 0
    overlap_hi = 0
    overlap_valid = False
    overlap_relaxed = False
    union_secs: set[int] = set()
    for r in scored:
        union_secs |= dash_secs.get(r["pool_address"], set())
    if union_secs:
        overlap_lo = min(union_secs)
        overlap_hi = max(union_secs)
        overlap_valid = overlap_lo <= overlap_hi
    else:
        overlap_valid = False

    rpc_trips = 0
    quote_probes = 0
    mod_label = None
    rpc_plain = None

    final_status = "LIVE_COHORT_FOUND_NO_RPC_VIABILITY_SKIPPED"

    if not scored:
        final_status = "NO_POOLS_PASSED_LIVE_GATES"
    elif not rpc_url:
        final_status = "LIVE_COHORT_FOUND_NO_RPC_VIABILITY_SKIPPED"
    else:
        mod_label = m.load_label_mod()
        rpc_plain = mod_label.RPC(rpc_url)
        latest = rpc_plain.call("eth_getBlockByNumber", ["latest", False])
        rpc_trips += 1
        latest_bn = int(latest["result"]["number"], 16)
        now_sec = int(latest["result"].get("timestamp", "0x0"), 16)

        top_try = scored[:MAX_VIABILITY_POOLS]
        notional = int(os.environ.get("LABEL_NOTIONAL_WEI", str(int(0.0005e18))))

        for rec in top_try:
            pool = rec["pool_address"]
            bset = dash_secs.get(pool, set())

            def rowfail(reason: str, **extra: str) -> None:
                viability_rows.append(
                    {
                        "pool_address_audit_key": pool,
                        "swaps_24h_ch": str(rec["swaps_24h_ch"]),
                        "buckets_5m_24h_ch": str(rec["buckets_5m_24h_ch"]),
                        "metadata_status": extra.get("metadata_status", "UNKNOWN"),
                        "liquidity_rpc_gate_csv": extra.get("liquidity_rpc_gate_csv", ""),
                        "current_quote_pass": extra.get("current_quote_pass", ""),
                        "historical_entry_quote_pass_count": extra.get("hist_pass", "0"),
                        "historical_entry_quote_fail_count": extra.get("hist_fail", "0"),
                        "viability_status": "FAIL",
                        "failure_reason": reason,
                        "dashcam_buckets": str(len(bset)),
                    }
                )

            if not overlap_valid:
                rowfail("invalid_overlap_calendar_union", metadata_status="UNKNOWN")
                continue

            if not bset:
                rowfail("no_dashcam_buckets_for_pool")
                continue

            bsorted = sorted(bset)
            recent_bs = filter_recent_bucket_secs(bsorted, now_sec, HISTORICAL_BUCKET_MAX_AGE_SEC)
            if len(recent_bs) < 3:
                recent_bs = bsorted
            if len(recent_bs) < 2:
                rowfail("insufficient_dashcam_buckets_lt_2_pool_not_labelable_with_current_dashcam_density")
                continue

            rep = m.representative_buckets(recent_bs, overlap_lo, overlap_hi)
            bm_list = [x * 1000 for x in rep]
            blk_map = m.ch_pool_bucket_blocks(pool, bm_list)

            meta_latest = m.audit_pool_at_block(mod_label, rpc_plain, pool, latest_bn)
            rpc_trips += 6

            hist_pass = hist_fail = 0
            md_ok = meta_latest.get("metadata_status") == "PASS"
            weth_ok = meta_latest.get("weth_ok")
            pt = meta_latest.get("ptype")
            if pt == "v2":
                liq_csv_status = "PASS" if meta_latest.get("liq_status") == "missing_or_zero" else "FAIL"
            else:
                liq_gate = meta_latest.get("liq_status") == "ok" and meta_latest.get("slot_status") == "ok"
                liq_csv_status = "PASS" if liq_gate else "FAIL"

            lm = {
                "ptype": meta_latest.get("ptype", ""),
                "t0": meta_latest.get("t0", ""),
                "t1": meta_latest.get("t1", ""),
                "fee_bps": meta_latest.get("fee_bps", 30),
                "fee_raw": meta_latest.get("fee_raw"),
            }

            for bsec in rep:
                bn = blk_map.get(bsec * 1000)
                if bn is None:
                    hist_fail += 1
                    continue
                if quote_probes >= 4500:
                    hist_fail += 1
                    continue
                ok = m.entry_quote_pass(mod_label, rpc_plain, lm, pool, int(bn), notional)
                quote_probes += 1
                rpc_trips += 2 if lm.get("ptype") == "v2" else 1
                hist_pass += 1 if ok else 0
                hist_fail += 0 if ok else 1

            need = len(rep)
            if need >= 5:
                min_entry_ok = 3
            elif need >= 4:
                min_entry_ok = 3
            else:
                min_entry_ok = need

            current_ok = False
            if md_ok and weth_ok and pt == "v3" and lm.get("fee_raw") is not None:
                current_ok = bool(
                    m.entry_quote_pass(mod_label, rpc_plain, lm, pool, latest_bn, notional)
                )
                quote_probes += 1
                rpc_trips += 2 if lm.get("ptype") == "v2" else 1

            hist_ok = md_ok and weth_ok and hist_pass >= min_entry_ok and (hist_pass + hist_fail) == need
            viab = (
                "PASS"
                if hist_ok and current_ok and liq_csv_status == "PASS"
                else "FAIL"
            )
            reason = ""
            if viab == "FAIL":
                if not hist_ok:
                    reason = "historical_entry_quote_incomplete_or_zero"
                elif not current_ok:
                    reason = "current_head_quote_blocked_or_zero"
                elif liq_csv_status != "PASS":
                    reason = "on_chain_liquidity_or_slot_gate_fail_volume_proxy_was_nonzero_via_ch_only"
                else:
                    reason = "metadata_or_combo_failed"

            if viab == "PASS":
                pass_list.append(pool)

            viability_rows.append(
                {
                    "pool_address_audit_key": pool,
                    "swaps_24h_ch": str(rec["swaps_24h_ch"]),
                    "buckets_5m_24h_ch": str(rec["buckets_5m_24h_ch"]),
                    "metadata_status": meta_latest.get("metadata_status", "UNKNOWN"),
                    "liquidity_rpc_gate_csv": liq_csv_status,
                    "current_quote_pass": str(current_ok).lower(),
                    "historical_entry_quote_pass_count": str(hist_pass),
                    "historical_entry_quote_fail_count": str(hist_fail),
                    "viability_status": viab,
                    "failure_reason": reason,
                    "dashcam_buckets": str(len(bset)),
                }
            )

        swap_rank = {r["pool_address"]: int(r["swaps_24h_ch"]) for r in scored}
        pass_sorted = sorted(
            pass_list,
            key=lambda p: (
                -len({x for x in dash_secs.get(p, ()) if overlap_valid and overlap_lo <= x <= overlap_hi}),
                -swap_rank.get(p, 0),
                p,
            ),
        )
        pass_list = list(dict.fromkeys(pass_sorted))

        man_rows: list[tuple[str, int, int]] | None = None
        if pass_list:
            for n_pool in (6, 5, 4):
                found = False
                for combo in itertools.combinations(pass_list, n_pool):
                    mr = m.hard_quota_manifest_rows(list(combo), dash_secs, overlap_lo, overlap_hi)
                    if mr is not None:
                        man_rows = mr
                        found = True
                        break
                if found:
                    break

            final_status = (
                "PENDING_SMOKE_EXECUTION"
                if man_rows is not None
                else "VIABILITY_PASS_BUT_HARD_QUOTA_MANIFEST_NOT_FOUND"
            )
            if pass_list and man_rows is None:
                hard_quota_contraction_note = (
                    "sprint033a_hard_quota_overlap_contract_needs_four_to_six_pools_with_intersection_buckets; "
                    f"viability_pass_only_{len(pass_list)}"
                )

            if man_rows:
                ctrm = Counter(p for p, _, _ in man_rows)
                tot = sum(ctrm.values())
                sh = sorted(ctrm.values(), reverse=True)
                hard_gates = {
                    "top1_row_share_cap": 0.3,
                    "top3_row_share_cap": 0.7,
                    "computed_top1_row_share": round(sh[0] / tot, 6),
                    "computed_top3_row_share": round(sum(sh[:3]) / tot, 6),
                }
                manifest_obj = {
                    "kind": "live_active_scoped_hard_quota_smoke_manifest",
                    "created_before_quote": True,
                    "no_training": True,
                    "no_live": True,
                    "sprint_scope": "sprint035a_live_active_scoped_base_v3",
                    "source_artifacts": [
                        "scripts/sprint035a_live_active_pool_mining.py",
                        "scripts/sprint033a_ch_backed_hard_quota_pipeline.py",
                        "scripts/sprint030d_bounded_smoke_label.py",
                        "docs/label-semantics-freeze.md",
                        "docs/ml-training-gate.md",
                        "mvp/audits/failed_no_7d_alpha_guard.md",
                        "logs/dashcam.csv",
                    ],
                    "live_activity_gates_snapshot": gates_doc,
                    "selected_pool_count": len(ctrm),
                    "selected_rows_count": len(man_rows),
                    "label_limit": 250,
                    "per_pool_row_cap_enforced": 60,
                    "hard_quota_gates": hard_gates,
                    "pool_selection_rule": (
                        "ch_24h_plus_60d_density_join_discovered_v3_weth_minus_sprint034_dead_pools "
                        "dashcam_min_buckets_then_sprint033a_viability_plus_current_head_quote "
                        "hard_quota_overlap_manifest"
                    ),
                    "pool_address_audit_keys": sorted(ctrm.keys()),
                    "bucket_count_by_pool": dict(ctrm),
                    "overlap_window_ms_audit_only": {"start_ms": overlap_lo * 1000, "end_ms": overlap_hi * 1000},
                    "manifest_rows": [
                        {"pool_audit_only": p, "bucket_ms": bm, "bucket_sec": bs} for p, bm, bs in man_rows
                    ],
                }
                OUT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
                OUT_MANIFEST.write_text(json.dumps(manifest_obj, indent=2) + "\n", encoding="utf-8")
        else:
            final_status = "NO_VIABILITY_PASS_POOLS"

    fields = [
        "pool_address_audit_key",
        "swaps_24h_ch",
        "buckets_5m_24h_ch",
        "metadata_status",
        "liquidity_rpc_gate_csv",
        "current_quote_pass",
        "historical_entry_quote_pass_count",
        "historical_entry_quote_fail_count",
        "viability_status",
        "failure_reason",
        "dashcam_buckets",
    ]
    OUT_VIAB_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_VIAB_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for row in viability_rows:
            w.writerow({k: row.get(k, "") for k in fields})

    summary: dict[str, Any] = {
        "kind": "sprint035a_live_active_mining_summary",
        "generated_at_unix": int(time.time()),
        "elapsed_sec": round(time.time() - t0, 2),
        "clickhouse_reachable": clickhouse_ok,
        "ch_query_fingerprint_audit_only": "discovered_pools_join_uniswap_v3_swaps_enriched_24h_and_60d_windows",
        "ch_sql_audit_only_path": "embedded_in_script_sprint035a_live_active_pool_mining",
        "sprint034_dead_pools_excluded": sorted(SPRINT034_DEAD_POOLS),
        "live_activity_gates": gates_doc,
        "ch_candidate_rows_after_ch_join": len(candidates),
        "scored_rows_after_dashcam_floor": len(scored),
        "viability_pass_pool_count": len(pass_list),
        "viability_pass_pool_addresses": sorted(pass_list),
        "overlap_window_sec_audit_only": {
            "lo": overlap_lo,
            "hi": overlap_hi,
            "valid": overlap_valid,
            "relaxed_pairwise": overlap_relaxed,
        },
        "rpc_env_configured": bool(rpc_url),
        "approx_rpc_round_trips_viability_only": rpc_trips,
        "entry_quote_probes_viability": quote_probes,
        "hard_quota_manifest_built": manifest_obj is not None,
        "manifest_path": str(OUT_MANIFEST.relative_to(REPO)) if manifest_obj else "",
        "hard_quota_contraction_note": hard_quota_contraction_note,
        "final_decision_status": final_status,
        "decision_flags": {
            "FAILED_NO_7D_ALPHA_ACTIVE": True,
            "SPRINT034_SCOPE_ABANDONED": True,
            "LIVE_ACTIVE_SCOPED_MINING_ALLOWED": True,
            "BOUNDED_SMOKE_LABEL_ALLOWED_IF_MANIFEST": final_status == "PENDING_SMOKE_EXECUTION",
            "TRAINING_ALLOWED": False,
            "H12_ABLATION_ALLOWED": False,
            "PHASE6_ALLOWED": False,
            "LIVE_ALLOWED": False,
            "RETRAIN_ALLOWED": False,
            "MODEL_PROMOTION_ALLOWED": False,
            "SMART_MONEY_GENERAL_LINE_OPEN": False,
            "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
        },
    }

    OUT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    md_lines = [
        "# Sprint035A — live-active scoped pool mining (Base V3)",
        "",
        "Abandons Sprint034 same-three-pool scope; requires **current** CH 24h activity, 60d depth, dashcam bucket density, and (when RPC is configured) historical + head-gated quotes.",
        "",
        "## Gates (documented; not USD oracle proof)",
        "",
        f"- **24h activity proxy:** `swaps_24h_ch >= {MIN_SWAPS_24H}` and `buckets_5m_24h_ch >= {MIN_BUCKETS_5M_24H}`.",
        f"- **60d history anchor:** `swaps_60d_ch >= {MIN_SWAPS_60D}`, `buckets_5m_60d_ch >= {MIN_BUCKETS_5M_60D}`, `active_days_60d_ch >= {MIN_ACTIVE_DAYS_60D}`.",
        f"- **Dashcam:** `dashcam_distinct_buckets >= {MIN_DASHCAM_BUCKETS_OVERALL}` over full scan (cross-check, not paper substitute).",
        "- **Liquidity / quote:** V3 `liquidity` + `slot0` RPC gate and `entry_quote_pass` at mapped recent buckets + latest head — see viability CSV.",
        "",
        "## Excluded Sprint034 cohort",
        "",
        *(f"- `{p}`" for p in sorted(SPRINT034_DEAD_POOLS)),
        "",
        "## Run outcome (`final_decision_status`)",
        "",
        f"`{final_status}`",
        "",
        *(
            [
                f"**Hard-quota contraction:** `{hard_quota_contraction_note}`",
                "",
            ]
            if hard_quota_contraction_note
            else []
        ),
        "## Artifacts",
        "",
        f"- `{OUT_SUMMARY.relative_to(REPO)}`",
        f"- `{OUT_VIAB_CSV.relative_to(REPO)}`",
        (
            f"- `{OUT_MANIFEST.relative_to(REPO)}` — hard-quota overlap manifest for bounded smoke labeling."
            if manifest_obj
            else "- Manifest not emitted (gates or viability or hard-quota contraction failed)."
        ),
        "",
        "## Next bounded smoke labeling",
        "",
        "When `final_decision_status` is **`PENDING_SMOKE_EXECUTION`** and RPC env matches policy:",
        "",
        "```text",
        "python3 scripts/sprint030d_bounded_smoke_label.py --profile sprint035a_live_active_scoped_smoke",
        "```",
        "",
        "This sprint **does not** claim alpha, promotion, training, Phase6, micro-live, or full live.",
        "",
    ]
    OUT_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    OUT_AUDIT.write_text("\n".join(md_lines), encoding="utf-8")

    print(final_status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
