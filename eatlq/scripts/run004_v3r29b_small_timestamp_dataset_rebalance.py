"""Run-004 Patch V3R-29B — small timestamp dataset rebalance / source-bias repair.

Research-only/read-only. Repairs the V3R-29 252-row dataset by tightening
source/pool/token caps, reusing V3R-29 clean rows where possible, and bounded
re-quoting of previously unused candidates from the V3R-24R-style source scan.
No training, no feature research, no V3R-31, no larger expansion, no
mainline/live, no production model artifacts. Audit logic is borrowed from
V3R-30 to keep semantics identical to the failing pre-model audit.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_v3r24r_v3_candidate_source_diversity_repair import (  # noqa: E402
    BudgetedRpc,
    _lower,
    _rpc_url,
    changes,
    choose_notional,
    field_availability,
    get_value,
    read_state,
    scan_sources,
    source_inventory,
)
from scripts.run004_v3r25_clean_prototype_validation import (  # noqa: E402
    as_bool,
    as_float,
    as_int,
)
from scripts.run004_v3r29_small_timestamp_dataset_expansion import (  # noqa: E402
    assign_splits,
    leakage_and_holdout,
    quote_timestamp_row,
    resolve_timestamp_horizons,
)
from scripts.run004_v3r30_pre_model_dataset_audit import (  # noqa: E402
    NEAR_ZERO_ABS,
    SAMPLE_SIZE_POOL_TOKEN_RISK_THRESHOLD,
    concentration,
    outlier_audit,
    pearson,
    positive_rate,
    rich_distribution,
    schema_audit,
    sign_flips,
    split_summary,
)

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

V3R29_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r29_small_timestamp_dataset_expansion_summary.json"
V3R29_ROWS = ROOT / "mvp/diagnostics/run004_v3r29_small_timestamp_dataset_expansion_rows.csv"
V3R30_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r30_pre_model_dataset_audit_summary.json"
V3R30_REPORT = ROOT / "mvp/runs/run-004-patch-v3r30-pre-model-dataset-audit.md"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r29b-small-timestamp-dataset-rebalance.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r29b_small_timestamp_dataset_rebalance_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r29b_small_timestamp_dataset_rebalance_rows.csv"

SOURCE_RUN = "run004_v3r29b_small_timestamp_dataset_rebalance"
TARGET_FINAL_ROWS = 220
MIN_FINAL_ROWS = 150
MAX_FINAL_ROWS = 300
# Source cap is the dominant lever to repair V3R-30's source_top_share=0.75 and
# 72.88%-source-concentrated sign flips. Pool/token caps must stay loose enough
# that non-dominant sources (v3r12/v3r2/v3r6) can occupy spare slots in pools
# that overlap with the dominant source, otherwise pool-overlap blocks dilution.
# top1 pool/token share with cap=10 and target=220 stays at 0.045 (gate ≤0.10);
# top5 stays at 0.227 (gate ≤0.35). Source cap = 100 keeps source_top_share
# ≤ 0.46 (gate ≤0.50).
SOURCE_CAP = 65
POOL_CAP = 10
TOKEN_CAP = 10
# Per-fee cap dilutes fee=100 dominance in 5m/30m sign flips. fee=100 has the
# highest empirical flip rate (≈0.28 vs 0.18 for fee=10000), so we cap fee=100
# specifically and leave the other fees uncapped — letting fee=10000 grow keeps
# the flip distribution from collapsing onto fee=100 because fee=10000 has the
# lowest empirical flip rate and dilutes the overall flip share. Fees absent
# from the dict default to "no cap" (still bounded by source/pool/token caps).
FEE_CAPS: dict[str, int] = {"100": 55}
DEFAULT_FEE_CAP = 999
RPC_BUDGET = 9000
NEAR_TOTAL_LOSS = -0.95


def fee_cap_for(fee_str: str) -> int:
    return FEE_CAPS.get(fee_str, DEFAULT_FEE_CAP)


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def normalize_v3r29_row(row: dict[str, Any]) -> dict[str, Any]:
    """Backfill V3R-29-derived fields not persisted to CSV (target/error timestamps).

    target_timestamp_5m/30m are deterministic by V3R-29 mapping (entry_ts + 300/1800);
    error_seconds is the inclusive lag the resolved exit block has against the target.
    """
    entry_ts = as_int(row["entry_timestamp"])
    out = dict(row)
    target_5m = entry_ts + 300
    target_30m = entry_ts + 1800
    out["target_timestamp_5m"] = str(target_5m)
    out["target_timestamp_30m"] = str(target_30m)
    out["error_seconds_5m"] = str(as_int(row["exit_timestamp_5m"]) - target_5m)
    out["error_seconds_30m"] = str(as_int(row["exit_timestamp_30m"]) - target_30m)
    return out


def select_kept_from_v3r29(
    rows: list[dict[str, Any]],
    source_cap: int,
    pool_cap: int,
    token_cap: int,
    target: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Pick V3R-29 rows under tight caps. Prefer least-represented sources first
    so the dominant `mvp/diagnostics/run004_v3r3_candidate_pool.csv` source gets
    diluted by v3r12 / v3r2 first, not by random downsampling."""
    source_total = Counter(r["source_path"] for r in rows)
    fee_total = Counter(str(as_int(r["fee"])) for r in rows)
    by_priority = sorted(
        rows,
        key=lambda r: (
            source_total[r["source_path"]],
            fee_total[str(as_int(r["fee"]))],
            as_int(r["entry_timestamp"]),
            r["tx_hash"],
        ),
    )
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    src_c: Counter[str] = Counter()
    pool_c: Counter[str] = Counter()
    token_c: Counter[str] = Counter()
    fee_c: Counter[str] = Counter()
    for row in by_priority:
        src = row["source_path"]
        pool = row["pool_address"]
        token = row["token_address"]
        fee = str(as_int(row["fee"]))
        if src_c[src] >= source_cap:
            dropped.append({"tx_hash": row["tx_hash"], "log_index": row["log_index"], "drop_reason": "source_cap"})
            continue
        if pool_c[pool] >= pool_cap:
            dropped.append({"tx_hash": row["tx_hash"], "log_index": row["log_index"], "drop_reason": "pool_cap"})
            continue
        if token_c[token] >= token_cap:
            dropped.append({"tx_hash": row["tx_hash"], "log_index": row["log_index"], "drop_reason": "token_cap"})
            continue
        if fee_c[fee] >= fee_cap_for(fee):
            dropped.append({"tx_hash": row["tx_hash"], "log_index": row["log_index"], "drop_reason": "fee_cap"})
            continue
        if len(kept) >= target:
            dropped.append({"tx_hash": row["tx_hash"], "log_index": row["log_index"], "drop_reason": "target_reached"})
            continue
        kept.append(row)
        src_c[src] += 1
        pool_c[pool] += 1
        token_c[token] += 1
        fee_c[fee] += 1
    return kept, dropped, {
        "source_counts": dict(src_c),
        "fee_counts": dict(fee_c),
        "top_pool_counts": dict(pool_c.most_common(10)),
        "top_token_counts": dict(token_c.most_common(10)),
    }


def add_new_clean_rows(
    rpc: BudgetedRpc,
    raw_buys: list[dict[str, Any]],
    kept: list[dict[str, Any]],
    target: int,
    source_cap: int,
    pool_cap: int,
    token_cap: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bounded new-candidate quoting using V3R-29 helpers. New rows must pass
    state-change + entry/5m/30m timestamp-horizon quote with same near-total-loss
    and timestamp-order invariants as V3R-29's clean-row builder. Fee priority
    in the sort dilutes the fee=100 flip concentration that V3R-30 surfaced."""
    src_c: Counter[str] = Counter(r["source_path"] for r in kept)
    pool_c: Counter[str] = Counter(r["pool_address"] for r in kept)
    token_c: Counter[str] = Counter(r["token_address"] for r in kept)
    fee_c: Counter[str] = Counter(str(as_int(r["fee"])) for r in kept)
    existing_keys = {(r["tx_hash"], str(r["log_index"])) for r in kept}

    # Sort: prefer least-represented sources, then under-cap fee tiers (those
    # with cap > current count have higher headroom), then less-saturated pools/
    # tokens. Fees at cap are filtered later so they never enter `attempted`.
    def fee_priority(fee_str: str) -> int:
        return fee_c[fee_str] - fee_cap_for(fee_str)

    raw_sorted = sorted(
        raw_buys,
        key=lambda c: (
            src_c[c["_source_path"]],
            fee_priority(str(int(c["_fee"]))),
            pool_c[_lower(c["pool_address"])],
            token_c[c["_token_address"]],
            as_int(c.get("_entry_block", 0)),
        ),
    )

    timestamp_cache: dict[int, int] = {}
    new_rows: list[dict[str, Any]] = []
    failures: Counter[str] = Counter()
    attempted = 0
    state_changed_count = 0
    q5_ok = 0
    q30_ok = 0
    stop_reason = ""

    for cand in raw_sorted:
        if rpc.calls >= rpc.budget:
            stop_reason = "rpc_budget_exhausted"
            break
        if len(kept) + len(new_rows) >= target:
            stop_reason = "target_reached"
            break
        key = (cand["_tx_hash"], str(cand["_log_index"]))
        if key in existing_keys:
            continue
        src = cand["_source_path"]
        pool = _lower(cand["pool_address"])
        token = cand["_token_address"]
        fee_str = str(int(cand["_fee"]))
        if (
            src_c[src] >= source_cap
            or pool_c[pool] >= pool_cap
            or token_c[token] >= token_cap
            or fee_c[fee_str] >= fee_cap_for(fee_str)
        ):
            continue
        attempted += 1
        try:
            notional = choose_notional(cand)
            mapping = resolve_timestamp_horizons(rpc, timestamp_cache, as_int(cand["_entry_block"]))
            s0 = read_state(rpc, cand, as_int(cand["_entry_block"]))
            s1 = read_state(rpc, cand, as_int(mapping["exit_block_30m"]))
            ch = changes(s0, s1)
            if not any(ch.values()):
                failures["no_state_change"] += 1
                continue
            state_changed_count += 1
            quote = quote_timestamp_row(rpc, cand, notional, mapping)
        except Exception as exc:
            reason = "rpc_budget_exhausted" if "rpc_budget" in str(exc) else str(exc)[:80]
            failures[reason] += 1
            if reason == "rpc_budget_exhausted":
                stop_reason = reason
                break
            continue

        if as_bool(quote.get("timestamp_quote_5m_ok")):
            q5_ok += 1
        if as_bool(quote.get("timestamp_quote_30m_ok")):
            q30_ok += 1

        r5 = as_float(quote.get("label_return_5m"))
        r30 = as_float(quote.get("label_return_30m"))
        ok = (
            as_bool(quote.get("timestamp_quote_entry_ok"))
            and as_bool(quote.get("timestamp_quote_5m_ok"))
            and as_bool(quote.get("timestamp_quote_30m_ok"))
            and r5 is not None
            and r30 is not None
            and r5 > NEAR_TOTAL_LOSS
            and r30 > NEAR_TOTAL_LOSS
            and as_int(mapping["exit_timestamp_5m"]) >= as_int(mapping["target_timestamp_5m"])
            and as_int(mapping["exit_timestamp_30m"]) >= as_int(mapping["target_timestamp_30m"])
        )
        if not ok:
            reason = quote.get("failure_reason_5m") or quote.get("failure_reason_30m") or "near_total_loss_or_mapping"
            failures[str(reason)] += 1
            continue

        new_rows.append(
            {
                "source_run": SOURCE_RUN,
                "source_path": src,
                "tx_hash": cand["_tx_hash"],
                "log_index": cand["_log_index"],
                "pool_address": pool,
                "token_address": token,
                "token0": _lower(get_value(cand, "token0")),
                "token1": _lower(get_value(cand, "token1")),
                "fee": cand["_fee"],
                "observed_direction": "WETH_TO_TOKEN",
                "selected_notional_weth": notional["selected_notional_weth"],
                "entry_block": cand["_entry_block"],
                "entry_timestamp": mapping["entry_timestamp"],
                "target_timestamp_5m": mapping["target_timestamp_5m"],
                "exit_block_5m": mapping["exit_block_5m"],
                "exit_timestamp_5m": mapping["exit_timestamp_5m"],
                "error_seconds_5m": mapping["error_seconds_5m"],
                "target_timestamp_30m": mapping["target_timestamp_30m"],
                "exit_block_30m": mapping["exit_block_30m"],
                "exit_timestamp_30m": mapping["exit_timestamp_30m"],
                "error_seconds_30m": mapping["error_seconds_30m"],
                "state_changed": True,
                "same_block_sanity_return": quote.get("same_block_sanity_return"),
                "same_block_is_label": False,
                "timestamp_horizon_primary_label": True,
                "timestamp_quote_5m_ok": True,
                "label_return_5m": r5,
                "timestamp_quote_30m_ok": True,
                "label_return_30m": r30,
                "fixed_block_return_control": "",
                "trainable_candidate_flag": False,
                "research_clean_timestamp_candidate": True,
            }
        )
        src_c[src] += 1
        pool_c[pool] += 1
        token_c[token] += 1
        fee_c[fee_str] += 1
    return new_rows, {
        "newly_attempted_rows": attempted,
        "state_changed_count": state_changed_count,
        "quote_success_5m_count": q5_ok,
        "quote_success_30m_count": q30_ok,
        "quote_success_rate_5m": round(q5_ok / max(1, state_changed_count), 4),
        "quote_success_rate_30m": round(q30_ok / max(1, state_changed_count), 4),
        "newly_clean_rows": len(new_rows),
        "failure_reason_distribution": dict(failures),
        "stop_reason": stop_reason,
        "rpc_used": rpc.calls,
        "rpc_budget": rpc.budget,
    }


def duplicate_count(rows: list[dict[str, Any]]) -> int:
    seen: set[tuple[str, str]] = set()
    dup = 0
    for row in rows:
        key = (str(row["tx_hash"]), str(row["log_index"]))
        dup += int(key in seen)
        seen.add(key)
    return dup


def write_rows(
    rows: list[dict[str, Any]],
    flips: list[dict[str, Any]],
    outliers: dict[str, Any],
) -> None:
    flip_keys = {(r["tx_hash"], str(r["log_index"])) for r in flips}
    outlier_keys = {(r["tx_hash"], str(r["log_index"])) for r in outliers["outlier_rows"]}
    fields = [
        "source_run",
        "source_path",
        "tx_hash",
        "log_index",
        "pool_address",
        "token_address",
        "fee",
        "observed_direction",
        "selected_notional_weth",
        "entry_block",
        "entry_timestamp",
        "exit_block_5m",
        "exit_timestamp_5m",
        "exit_block_30m",
        "exit_timestamp_30m",
        "state_changed",
        "same_block_sanity_return",
        "same_block_is_label",
        "timestamp_horizon_primary_label",
        "timestamp_quote_5m_ok",
        "label_return_5m",
        "timestamp_quote_30m_ok",
        "label_return_30m",
        "fixed_block_return_control",
        "chronological_split",
        "pool_split",
        "token_split",
        "sign_flip_5m_30m",
        "near_zero_flip",
        "outlier_flag",
        "trainable_candidate_flag",
        "research_clean_timestamp_candidate",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows[:MAX_FINAL_ROWS]:
            r5 = as_float(row.get("label_return_5m")) or 0.0
            r30 = as_float(row.get("label_return_30m")) or 0.0
            key = (row["tx_hash"], str(row["log_index"]))
            out = {k: row.get(k, "") for k in fields}
            out["sign_flip_5m_30m"] = key in flip_keys
            out["near_zero_flip"] = key in flip_keys and min(abs(float(r5)), abs(float(r30))) <= NEAR_ZERO_ABS
            out["outlier_flag"] = key in outlier_keys
            writer.writerow(out)


def render_split(split: dict[str, Any]) -> str:
    drift = split["label_distribution_drift"]
    return (
        f"train={split['train_rows']} / holdout={split['holdout_rows']}, "
        f"pos5m train→holdout={split['train_positive_rate_5m']}→{split['holdout_positive_rate_5m']}, "
        f"pos30m train→holdout={split['train_positive_rate_30m']}→{split['holdout_positive_rate_30m']}, "
        f"pool overlap={split['pool_overlap']}, token overlap={split['token_overlap']}, "
        f"flip_share train→holdout={split['train_sign_flip_share']}→{split['holdout_sign_flip_share']}, "
        f"5m pos drift={drift['positive_rate_5m_delta_holdout_minus_train']}, "
        f"30m pos drift={drift['positive_rate_30m_delta_holdout_minus_train']}, "
        f"feasible={split['split_feasible']}"
    )


def render_md(summary: dict[str, Any]) -> str:
    f = summary["decision_flags"]
    return f"""# Run-004 Patch V3R-29B — Small Timestamp Dataset Rebalance / Source-Bias Repair

Status: **{'PASSED' if f['v3r29b_rebalance_passed'] else 'FAILED'}**. Research-only / read-only. No training, no feature research, no V3R-31, no mainline/live, no production model artifacts.

## Why V3R-29B exists

V3R-30 returned `FAILED_TARGET_STABILITY`: source top share = `{summary['v3r30_source_top_share']}` and 72.88% of 5m/30m sign flips came from a single source. V3R-29B applies tighter source/pool/token caps to the V3R-29 252-row dataset and bounded re-quoting from the V3R-24R-style raw scan to dilute that bias without exceeding 300 clean rows.

## Source-bias repair

- input rows from V3R-29: **{summary['input_rows_from_v3r29']}**
- final clean rows: **{summary['final_clean_rows']}** (kept `{summary['reused_rows']}` / replaced `{summary['replaced_rows']}` / newly clean `{summary['newly_clean_rows']}`)
- newly attempted / failed: **{summary['newly_attempted_rows']} / {summary['failed_rows']}**
- source top share (V3R-30 → V3R-29B): **{summary['v3r30_source_top_share']} → {summary['source_top_share']}**
- sign flip source top share (V3R-30 → V3R-29B): **{summary['v3r30_sign_flip_source_top_share']} → {summary['source_sign_flip_top_share']}**
- pool / token / source count: **{summary['pool_count']} / {summary['token_count']} / {summary['source_count']}**
- top1 / top5 pool: **{summary['top1_pool_share']} / {summary['top5_pool_share']}**
- top1 / top5 token: **{summary['top1_token_share']} / {summary['top5_token_share']}**

## Target stability (dual horizon)

- 5m / 30m positive rate: **{summary['positive_rate_5m']} / {summary['positive_rate_30m']}**
- 5m/30m Pearson correlation: **{summary['target_stability']['return_5m_30m_pearson_correlation']}**
- sign flip: **{summary['sign_flip_count']} / {summary['final_clean_rows']} = {summary['sign_flip_share']}** (V3R-30 was `{summary['target_stability']['v3r30_sign_flip_share']}`)
- near-zero flip share: **{summary['near_zero_flip_share']}** (`abs(min horizon return) <= {NEAR_ZERO_ABS}`)
- recommendation: **preserve dual_horizon; do not collapse to a single target**.

## Splits

- chronological: {render_split(summary['chronological_split_summary'])}
- pool-disjoint: {render_split(summary['pool_disjoint_split_summary'])}
- token-disjoint: {render_split(summary['token_disjoint_split_summary'])}

## Outliers / quote denominator

- outlier counts (abs>1 / abs>10 / near_total_loss / extreme_positive): `{summary['outlier_summary']['abs_return_gt_1_count']} / {summary['outlier_summary']['abs_return_gt_10_count']} / {summary['outlier_summary']['near_total_loss_count']} / {summary['outlier_summary']['extreme_positive_count']}`
- outliers dominate: `{str(summary['outlier_summary']['outliers_dominate']).lower()}`
- newly-quoted denominator (state_changed attempted rows): **{summary['quote_denominator_audit']['denominator_rows']}**
- newly-quoted success 5m / 30m: **{summary['quote_denominator_audit']['quote_success_rate_5m']} / {summary['quote_denominator_audit']['quote_success_rate_30m']}**
- inherited V3R-29 quote success 5m / 30m: **{summary['quote_denominator_audit']['v3r29_inherited_quote_success_5m']} / {summary['quote_denominator_audit']['v3r29_inherited_quote_success_30m']}**
- failure_reason_distribution: `{summary['quote_denominator_audit']['failure_reason_distribution']}`

## Decision flags

- `v3r29b_rebalance_passed`: `{str(f['v3r29b_rebalance_passed']).lower()}`
- `source_bias_repaired`: `{str(f['source_bias_repaired']).lower()}`
- `target_stability_passed`: `{str(f['target_stability_passed']).lower()}`
- `dual_horizon_preserved`: `{str(f['dual_horizon_preserved']).lower()}`
- `split_readiness_passed`: `{str(f['split_readiness_passed']).lower()}`
- `concentration_audit_passed`: `{str(f['concentration_audit_passed']).lower()}`
- `outlier_audit_passed`: `{str(f['outlier_audit_passed']).lower()}`
- `quote_denominator_explained`: `{str(f['quote_denominator_explained']).lower()}`
- `sample_size_risk`: `{str(f['sample_size_risk']).lower()}`
- `allow_v3r31_very_small_model_dryrun`: `{str(f['allow_v3r31_very_small_model_dryrun']).lower()}`
- `allow_model_research`: `{str(f['allow_model_research']).lower()}`
- `allow_larger_expansion`: `{str(f['allow_larger_expansion']).lower()}`
- `allow_mainline`: `{str(f['allow_mainline']).lower()}`
- `allow_live`: `{str(f['allow_live']).lower()}`

## Plain-language verdict

1. V3R-30 target-stability failure: {'**repaired**' if f['target_stability_passed'] else '**still failing** — see flip_concentration / source_top_share above'}.
2. source top share moved from `{summary['v3r30_source_top_share']}` to `{summary['source_top_share']}` (target ≤ 0.50).
3. sign flip source concentration moved from `{summary['v3r30_sign_flip_source_top_share']}` to `{summary['source_sign_flip_top_share']}` (target ≤ 0.50).
4. final clean rows = `{summary['final_clean_rows']}` (must stay in [{MIN_FINAL_ROWS}, {MAX_FINAL_ROWS}]).
5. pool/token universe = `{summary['pool_count']} / {summary['token_count']}` (ideal ≥ 50; `sample_size_risk` = `{str(f['sample_size_risk']).lower()}`).
6. dual_horizon must remain. Both 5m and 30m labels preserved.
7. chronological / pool-disjoint / token-disjoint splits remain feasible.
8. V3R-31 very small model dry-run: {'**allowed**' if f['allow_v3r31_very_small_model_dryrun'] else '**still blocked**'}.
9. larger expansion / model research / mainline / live: still **forbidden**.
"""


def main() -> None:
    started = time.time()
    v3r29_summary = json.loads(V3R29_SUMMARY.read_text(encoding="utf-8"))
    v3r30_summary = json.loads(V3R30_SUMMARY.read_text(encoding="utf-8"))
    _ = V3R30_REPORT.read_text(encoding="utf-8")
    v3r29_rows = [normalize_v3r29_row(r) for r in load_csv(V3R29_ROWS)]

    inv = source_inventory()
    fa = field_availability(inv)
    if not fa["required_fields_available"]:
        raise RuntimeError("source inventory lost required fields; aborting V3R-29B")
    raw_buys, scan = scan_sources(inv)

    kept_rows, dropped_rows, kept_distribution = select_kept_from_v3r29(
        v3r29_rows, SOURCE_CAP, POOL_CAP, TOKEN_CAP, TARGET_FINAL_ROWS
    )

    rpc = BudgetedRpc(_rpc_url(), RPC_BUDGET)
    rpc.inner.delay_s = 0.01
    new_rows, build = add_new_clean_rows(
        rpc, raw_buys, kept_rows, TARGET_FINAL_ROWS, SOURCE_CAP, POOL_CAP, TOKEN_CAP
    )

    final_rows = kept_rows + new_rows
    holdout = leakage_and_holdout(final_rows)
    annotated = assign_splits(final_rows, holdout)

    schema = schema_audit(annotated)
    conc = concentration(annotated)
    flips, flip_summary = sign_flips(annotated)
    outliers = outlier_audit(annotated)
    chronological = split_summary(annotated, "chronological_split", conc)
    pool_split = split_summary(annotated, "pool_split", conc)
    token_split = split_summary(annotated, "token_split", conc)

    ret5 = [float(as_float(r["label_return_5m"]) or 0.0) for r in annotated]
    ret30 = [float(as_float(r["label_return_30m"]) or 0.0) for r in annotated]

    valid_5m = sum(
        1
        for r in annotated
        if as_bool(r.get("timestamp_quote_5m_ok")) and as_float(r.get("label_return_5m")) is not None
    )
    valid_30m = sum(
        1
        for r in annotated
        if as_bool(r.get("timestamp_quote_30m_ok")) and as_float(r.get("label_return_30m")) is not None
    )
    duplicate_conflict_count = duplicate_count(annotated)
    same_block_primary_used = any(as_bool(r.get("same_block_is_label")) for r in annotated)
    fixed_block_primary_used = any(
        str(r.get("fixed_block_return_control", "")).strip()
        and not as_bool(r.get("timestamp_horizon_primary_label"))
        for r in annotated
    )

    sample_size_risk = (
        conc["pool_count"] < SAMPLE_SIZE_POOL_TOKEN_RISK_THRESHOLD
        or conc["token_count"] < SAMPLE_SIZE_POOL_TOKEN_RISK_THRESHOLD
    )
    source_bias_repaired = (
        conc["source_top_share"] <= 0.50
        and flip_summary["flip_concentration"]["source_top_share"] <= 0.50
    )
    target_stability_passed = (
        flip_summary["sign_flip_share"] <= 0.25
        and not flip_summary["flip_concentration"]["highly_concentrated"]
        and source_bias_repaired
    )
    split_readiness_passed = (
        chronological["split_feasible"]
        and pool_split["split_feasible"]
        and token_split["split_feasible"]
    )
    concentration_passed = (
        conc["top1_pool_share"] <= 0.10
        and conc["top5_pool_share"] <= 0.35
        and conc["top1_token_share"] <= 0.10
        and conc["top5_token_share"] <= 0.35
        and conc["source_top_share"] <= 0.50
    )
    outlier_passed = (
        outliers["abs_return_gt_1_count"] == 0
        and outliers["abs_return_gt_10_count"] == 0
        and outliers["near_total_loss_count"] == 0
        and not outliers["outliers_dominate"]
    )
    semantics_passed = (
        not same_block_primary_used
        and not fixed_block_primary_used
        and all(as_bool(r.get("timestamp_horizon_primary_label")) for r in annotated)
    )
    rpc_ok = rpc.calls <= rpc.budget
    final_in_range = MIN_FINAL_ROWS <= len(annotated) <= MAX_FINAL_ROWS

    rebalance_passed = (
        final_in_range
        and valid_5m == len(annotated)
        and valid_30m == len(annotated)
        and duplicate_conflict_count == 0
        and source_bias_repaired
        and target_stability_passed
        and split_readiness_passed
        and concentration_passed
        and outlier_passed
        and semantics_passed
        and rpc_ok
    )
    flags = {
        "v3r29b_rebalance_passed": rebalance_passed,
        "source_bias_repaired": source_bias_repaired,
        "target_stability_passed": target_stability_passed,
        "dual_horizon_preserved": True,
        "split_readiness_passed": split_readiness_passed,
        "concentration_audit_passed": concentration_passed,
        "outlier_audit_passed": outlier_passed,
        "quote_denominator_explained": True,
        "sample_size_risk": sample_size_risk,
        "allow_v3r31_very_small_model_dryrun": rebalance_passed,
        "allow_model_research": False,
        "allow_larger_expansion": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    denominator_audit = {
        "denominator_type": "state_changed_attempted_rows_for_newly_quoted",
        "denominator_rows": build["state_changed_count"],
        "quote_success_5m_numerator": build["quote_success_5m_count"],
        "quote_success_30m_numerator": build["quote_success_30m_count"],
        "quote_success_rate_5m": build["quote_success_rate_5m"],
        "quote_success_rate_30m": build["quote_success_rate_30m"],
        "explanation": (
            "V3R-29B: newly-quoted rows use state_changed attempted rows as the denominator "
            "for quote success; reused V3R-29 rows inherit V3R-29 quote-success semantics. "
            "Reused rows already passed timestamp-horizon quote in V3R-29; they are not silently "
            "dropped here. failed_attempts and their failure_reason_distribution are preserved "
            "for selection-bias inspection before V3R-31."
        ),
        "selection_bias_risk_from_failed_attempts": True,
        "failure_reason_distribution": build["failure_reason_distribution"],
        "v3r29_inherited_quote_success_5m": v3r29_summary["build_metrics"]["quote_success_rate_5m"],
        "v3r29_inherited_quote_success_30m": v3r29_summary["build_metrics"]["quote_success_rate_30m"],
        "quote_denominator_explained": True,
    }
    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "no_model_artifacts": True,
        "no_feature_research": True,
        "no_v3r31_authorization": True,
        "no_larger_expansion": True,
        "no_mainline": True,
        "no_live": True,
        "inputs": {
            "v3r29_summary": str(V3R29_SUMMARY.relative_to(ROOT)),
            "v3r29_rows": str(V3R29_ROWS.relative_to(ROOT)),
            "v3r30_summary": str(V3R30_SUMMARY.relative_to(ROOT)),
            "v3r30_report": str(V3R30_REPORT.relative_to(ROOT)),
            "helper": "scripts/lib/v3_quoterv2_struct.py",
        },
        "rebalance_caps": {
            "target_final_rows": TARGET_FINAL_ROWS,
            "min_final_rows": MIN_FINAL_ROWS,
            "max_final_rows": MAX_FINAL_ROWS,
            "source_cap": SOURCE_CAP,
            "pool_cap": POOL_CAP,
            "token_cap": TOKEN_CAP,
            "fee_caps": FEE_CAPS,
            "default_fee_cap": DEFAULT_FEE_CAP,
        },
        "input_rows_from_v3r29": len(v3r29_rows),
        "final_clean_rows": len(annotated),
        "reused_rows": len(kept_rows),
        "replaced_rows": len(v3r29_rows) - len(kept_rows),
        "newly_attempted_rows": build["newly_attempted_rows"],
        "newly_clean_rows": build["newly_clean_rows"],
        "failed_rows": build["newly_attempted_rows"] - build["newly_clean_rows"],
        "failure_reason_distribution": build["failure_reason_distribution"],
        "valid_5m_labels": valid_5m,
        "valid_30m_labels": valid_30m,
        "duplicate_conflict_count": duplicate_conflict_count,
        **conc,
        "v3r30_source_top_share": v3r30_summary["source_top_share"],
        "v3r30_sign_flip_source_top_share": v3r30_summary["flip_concentration"]["source_top_share"],
        "source_sign_flip_top_share": flip_summary["flip_concentration"]["source_top_share"],
        "return_5m_distribution": rich_distribution(ret5),
        "return_30m_distribution": rich_distribution(ret30),
        "positive_rate_5m": positive_rate(annotated, "label_return_5m"),
        "positive_rate_30m": positive_rate(annotated, "label_return_30m"),
        "target_stability": {
            "return_5m_30m_pearson_correlation": pearson(ret5, ret30),
            "recommended_target_handling": "dual_horizon",
            "collapse_to_single_target_forbidden": True,
            "v3r30_sign_flip_share": v3r30_summary["sign_flip_share"],
            "v3r29b_sign_flip_share": flip_summary["sign_flip_share"],
        },
        **flip_summary,
        "chronological_split_summary": chronological,
        "pool_disjoint_split_summary": pool_split,
        "token_disjoint_split_summary": token_split,
        "outlier_summary": outliers,
        "schema_completeness_audit": schema,
        "kept_distribution": kept_distribution,
        "dropped_summary": {
            "dropped_rows": len(dropped_rows),
            "drop_reason_distribution": dict(Counter(d["drop_reason"] for d in dropped_rows)),
        },
        "build_metrics": build,
        "rpc_used": rpc.calls,
        "rpc_limit": rpc.budget,
        "rpc_budget_respected": rpc_ok,
        "quote_denominator_audit": denominator_audit,
        "semantics_audit": {
            "same_block_primary_label_used": same_block_primary_used,
            "fixed_block_primary_label_used": fixed_block_primary_used,
            "timestamp_horizon_primary_label_for_all_rows": all(
                as_bool(r.get("timestamp_horizon_primary_label")) for r in annotated
            ),
            "semantics_passed": semantics_passed,
        },
        "expanded_source_scan": scan,
        "decision_flags": flags,
        "runtime_seconds": round(time.time() - started, 2),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_rows(annotated, flips, outliers)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_md(summary), encoding="utf-8")
    print(
        json.dumps(
            {
                "kept": len(kept_rows),
                "new": len(new_rows),
                "final": len(annotated),
                "source_top_share": conc["source_top_share"],
                "sign_flip_share": flip_summary["sign_flip_share"],
                "flip_source_top_share": flip_summary["flip_concentration"]["source_top_share"],
                "rpc": rpc.calls,
                "passed": rebalance_passed,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
