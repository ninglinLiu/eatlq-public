"""Run-004 Patch V3R-23 — new V3 candidate builder design.

Design-only/read-only. Reads prior summaries and CSV headers, then writes a
candidate-builder design report plus schema. No RPC, no candidate generation,
no training, and no production/runtime changes.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

V3R22_JSON = ROOT / "mvp/diagnostics/run004_v3r22_clean_buy_diversity_repair_summary.json"
V3R22_CSV = ROOT / "mvp/diagnostics/run004_v3r22_clean_buy_diversity_repair_rows.csv"
V3R21_JSON = ROOT / "mvp/diagnostics/run004_v3r21_v3_buy_source_label_quality_validation_summary.json"
V3R20_JSON = ROOT / "mvp/diagnostics/run004_v3r20_v3_candidate_source_repair_summary.json"
NOVEL_CSV = ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv"
HELPER = ROOT / "scripts/lib/v3_quoterv2_struct.py"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r23-new-v3-candidate-builder-design.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r23_new_v3_candidate_builder_design_summary.json"
OUT_SCHEMA = ROOT / "mvp/diagnostics/run004_v3r23_new_v3_candidate_schema.json"

SOURCE_RUN = "run004_v3r23_new_v3_candidate_builder_design"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as f:
        return next(csv.reader(f))


def field(name: str, meaning: str, required: bool, source: str, nullable: bool, quality: str) -> dict[str, Any]:
    return {
        "field": name,
        "meaning": meaning,
        "required": required,
        "source": source,
        "nullable": nullable,
        "quality_constraint": quality,
    }


def build_schema() -> dict[str, Any]:
    fields = [
        field("source_run", "builder/prototype identifier", True, "builder", False, "stable run id"),
        field("tx_hash", "source swap transaction hash", True, "Swap log", False, "dedupe key with log_index"),
        field("log_index", "source swap log index", True, "Swap log", False, "dedupe key with tx_hash"),
        field("block_number", "source observation block", True, "Swap log", False, "integer > 0"),
        field("timestamp", "source observation timestamp", False, "block timestamp mapping", True, "required for timestamp horizon; null only in fixed-block pilot"),
        field("pool_address", "Uniswap V3 pool address", True, "Swap log / pool metadata", False, "Base Uni V3 WETH pair"),
        field("token_address", "non-WETH token in WETH pair", True, "token0/token1", False, "must be token0 or token1 and not WETH"),
        field("token0", "pool token0", True, "IUniswapV3Pool.token0 / source metadata", False, "must match pool metadata"),
        field("token1", "pool token1", True, "IUniswapV3Pool.token1 / source metadata", False, "must match pool metadata"),
        field("fee", "V3 fee tier", True, "IUniswapV3Pool.fee / source metadata", False, "one of known V3 fee tiers"),
        field("amount0", "Swap event amount0 signed pool balance delta", True, "Uniswap V3 Swap event", False, "signed int; semantics per official Swap event"),
        field("amount1", "Swap event amount1 signed pool balance delta", True, "Uniswap V3 Swap event", False, "signed int; semantics per official Swap event"),
        field("token_in", "observed input token inferred from signed deltas/token fields", True, "direction inference", False, "must be verified, not guessed"),
        field("token_out", "observed output token inferred from signed deltas/token fields", True, "direction inference", False, "must be verified, not guessed"),
        field("observed_direction", "WETH_TO_TOKEN / TOKEN_TO_WETH / UNKNOWN", True, "direction filter", False, "only WETH_TO_TOKEN can enter long-entry bucket"),
        field("observed_weth_amount_raw", "raw WETH-side amount from source observation", True, "source field / inference", True, "null/0 allowed only with conservative default flag"),
        field("observed_weth_amount_weth", "human WETH-side amount", True, "derived", True, "observed_weth_amount_raw / 1e18"),
        field("selected_notional_weth", "notional used for label quote", True, "notional policy", False, "min(observed_weth, cap) or conservative default"),
        field("selected_notional_reason", "why notional was selected", True, "notional policy", False, "observed_capped or default_conservative"),
        field("notional_cap_weth", "cap applied to observed WETH amount", True, "config", False, "0.00001/0.00005/0.0001 sensitivity tracked"),
        field("entry_block", "entry quote block", True, "source block", False, "equals block_number for prototype"),
        field("exit_block", "future horizon exit block", True, "horizon policy", False, "derived from timestamp mapping or fixed-block pilot"),
        field("exit_block_policy", "timestamp_5m/timestamp_30m/fixed_block_pilot", True, "horizon policy", False, "must not mislabel fixed-block as time horizon"),
        field("state_changed", "whether pool state changed over horizon", True, "state prefilter", False, "true required for trainable candidate"),
        field("state_change_fields", "slot0/liquidity/WETH balance/token balance change booleans", True, "state prefilter", False, "at least one true for trainable candidate"),
        field("entry_quote_ok", "entry quote success", True, "QuoterV2 struct helper", False, "must be true for label candidate"),
        field("entry_amount_out", "token output from WETH entry quote", True, "QuoterV2 struct helper", True, "required if entry_quote_ok"),
        field("horizon_exit_quote_ok", "future exit quote success", True, "QuoterV2 struct helper", False, "must be true for trainable candidate"),
        field("horizon_exit_amount_out_weth", "WETH output at future exit", True, "QuoterV2 struct helper", True, "required if horizon_exit_quote_ok"),
        field("label_return", "future horizon return", True, "label policy", True, "only future horizon, never same-block"),
        field("same_block_sanity_return", "same-block roundtrip sanity return", True, "sanity quote", True, "never used as label_return"),
        field("same_block_is_label", "must always be false", True, "label policy", False, "hard gate false"),
        field("failure_reason", "failure bucket", True, "builder", True, "quote/state/direction/toxic reasons"),
        field("trainable_candidate_flag", "whether row may enter future trainability audit", True, "gates", False, "false unless all gates pass"),
        field("non_trainable_reason", "why not trainable", True, "gates", True, "required when trainable_candidate_flag=false"),
    ]
    return {
        "schema": "run004_v3r23_new_v3_candidate_schema",
        "version": 1,
        "field_semantics_reference": {
            "uniswap_v3_swap_event": "https://docs.uniswap.org/contracts/v3/reference/core/interfaces/pool/IUniswapV3PoolEvents",
            "uniswap_v3_core_pool_source": "https://github.com/Uniswap/v3-core/blob/main/contracts/UniswapV3Pool.sol",
            "quoter_v2_reference": "https://docs.uniswap.org/contracts/v3/reference/periphery/lens/QuoterV2",
            "local_quoter_helper": "scripts/lib/v3_quoterv2_struct.py",
        },
        "fields": fields,
    }


def build_summary() -> dict[str, Any]:
    v3r22 = load_json(V3R22_JSON)
    v3r21 = load_json(V3R21_JSON)
    v3r20 = load_json(V3R20_JSON)
    candidate_header = read_header(NOVEL_CSV)
    helper_text = HELPER.read_text(encoding="utf-8") if HELPER.exists() else ""
    schema = build_schema()

    old = {
        "usable_for_diagnostics_only": True,
        "old_corpus_reusable_for_training": False,
        "old_corpus_expandable": False,
        "failure_roots": {
            "sell_biased_source": {
                "token_to_weth_count": v3r20["metrics"]["token_to_weth_count"],
                "weth_to_token_count": v3r20["metrics"]["weth_to_token_count"],
                "source_sell_biased": v3r20["metrics"]["decision_flags"]["source_sell_biased"],
            },
            "buy_observations_too_few": v3r20["metrics"]["buy_candidate_count"],
            "clean_rows_too_few": v3r22["clean_candidate_universe"]["clean_candidate_count"],
            "pool_token_over_concentrated": {
                "top1_pool": v3r22["clean_candidate_universe"]["clean_top1_pool_share"],
                "top5_pool": v3r22["clean_candidate_universe"]["clean_top5_pool_share"],
                "top1_token": v3r22["clean_candidate_universe"]["clean_top1_token_share"],
                "top5_token": v3r22["clean_candidate_universe"]["clean_top5_token_share"],
            },
            "fee_tier_single_clean_source": v3r22["clean_candidate_universe"]["clean_candidate_fee_distribution"],
            "fixed_block_horizon_insufficient": True,
            "cannot_build_20_rows_top5_lte_055": not v3r22["decision_flags"]["diversity_gate_passed"],
        },
    }

    design = {
        "stage_1_raw_event_intake": {
            "accept": "Base Uniswap V3 WETH-pair Swap observations only",
            "required_fields": [
                "source_run", "tx_hash", "log_index", "block_number", "timestamp", "pool_address",
                "token0", "token1", "fee", "amount0", "amount1", "token_in", "token_out",
                "observed_direction", "observed_weth_amount_raw",
            ],
            "field_semantics": "amount0/amount1 are signed Uniswap V3 Swap event pool deltas; do not reinterpret without token0/token1 metadata.",
            "field_semantics_unknown_policy": "mark field_semantics_unknown and stop candidate promotion",
        },
        "stage_2_direction_filter": {
            "WETH_TO_TOKEN": "long-entry candidate bucket",
            "TOKEN_TO_WETH": "sell-observation/diagnostics only",
            "UNKNOWN": "non-trainable direction_unknown bucket",
            "rule": "derive from verified token0/token1/amount0/amount1/tokenIn/tokenOut/is_buy/is_sell fields only",
        },
        "stage_3_notional_policy": {
            "rule": "selected_notional = min(observed_weth_amount, cap)",
            "conservative_cap_weth": 0.00001,
            "normal_cap_weth": 0.00005,
            "upper_pilot_cap_weth": 0.0001,
            "missing_observed_policy": "default 0.00001 WETH, not high-confidence trainable",
        },
        "stage_4_horizon_policy": {
            "preferred": ["timestamp_5min", "timestamp_30min"],
            "fallback": "fixed_block_pilot only if timestamp->block mapping unavailable",
            "state_change_required": ["slot0_changed", "liquidity_changed", "weth_balance_changed", "token_balance_changed"],
            "no_state_change_policy": "non-trainable no_state_change bucket",
        },
        "stage_5_quote_label_policy": {
            "entry": "WETH -> token at entry_block",
            "exit": "token -> WETH at horizon exit_block",
            "label_return": "future_horizon_return only",
            "same_block": "sanity only; same_block_is_label=false",
            "quote_failure_policy": "bucket as failure; never hard-fill -100%",
            "selector": "c6a5026a",
            "helper_validated": "STRUCT_SELECTOR = \"c6a5026a\"" in helper_text,
        },
        "stage_6_toxic_failure_filters": [
            "horizon_quoter_revert", "zero_liquidity", "near_total_loss <= -0.95",
            "no_state_change", "direction_unknown", "sell_observation_not_buy_entry",
            "duplicate tx_hash/log_index", "excessive pool/token concentration", "missing_required_fields",
        ],
        "stage_7_diversity_controls": {
            "pool_cap": "applied during candidate construction",
            "token_cap": "applied during candidate construction",
            "top1_pool_share_lte": 0.20,
            "top5_pool_share_lte": 0.55,
            "top1_token_share_lte": 0.20,
            "top5_token_share_lte": 0.55,
            "fee_tier_stratification": True,
            "tx_log_dedupe": True,
            "fee_specific_dataset_policy": "allowed only with explicit dataset name",
        },
    }

    prototype = {
        "run": "V3R-24 bounded prototype",
        "do_not_execute_in_v3r23": True,
        "raw_buy_observation_target_max": 100,
        "source_filter": "WETH_TO_TOKEN only",
        "state_change_prefilter": True,
        "notional_cap_default_weth": 0.00005,
        "notional_sensitivity_weth": [0.00001, 0.0001],
        "pool_token_cap": True,
        "fee_tier_stratification": True,
        "output_row_cap": 100,
        "expected_minimum_clean_rows": 20,
        "rpc_budget": {
            "max_eth_calls": 1500,
            "stop_on_error_rate_gt": 0.20,
        },
        "artifact_limits": {
            "markdown": 1,
            "summary_json": 1,
            "schema_json": "reuse V3R-23 schema",
            "candidate_csv_max_rows": 100,
        },
        "go_no_go_gates": {
            "fields_sufficient": True,
            "direction_unknown_share_lte": 0.05,
            "state_changed_share_gte": 0.20,
            "horizon_quote_success_rate_gte": 0.80,
            "clean_rows_count_gte": 20,
            "clean_near_total_loss_share_lte": 0.10,
            "top1_pool_share_lte": 0.20,
            "top5_pool_share_lte": 0.55,
            "top1_token_share_lte": 0.20,
            "top5_token_share_lte": 0.55,
            "duplicate_conflict_count": 0,
        },
    }

    flags = {
        "v3r23_completed": True,
        "old_corpus_reusable_for_training": False,
        "old_corpus_diagnostics_only": True,
        "old_corpus_expandable": False,
        "new_candidate_builder_required": True,
        "direction_filter_designed": True,
        "notional_policy_designed": True,
        "horizon_state_change_policy_designed": True,
        "quote_label_policy_designed": True,
        "toxic_filter_policy_designed": True,
        "diversity_controls_designed": True,
        "schema_defined": True,
        "v3r24_prototype_plan_defined": True,
        "allow_v3r24_bounded_prototype": True,
        "allow_larger_expansion": False,
        "allow_model_research": False,
        "allow_mainline": False,
        "allow_live": False,
    }

    return {
        "run": SOURCE_RUN,
        "research_only": True,
        "design_only": True,
        "inputs": {
            "v3r22_summary": str(V3R22_JSON.relative_to(ROOT)),
            "v3r22_rows": str(V3R22_CSV.relative_to(ROOT)),
            "v3r21_summary": str(V3R21_JSON.relative_to(ROOT)),
            "v3r20_summary": str(V3R20_JSON.relative_to(ROOT)),
            "novel_candidates": str(NOVEL_CSV.relative_to(ROOT)),
            "quoter_helper": str(HELPER.relative_to(ROOT)),
        },
        "official_references": schema["field_semantics_reference"],
        "candidate_header": candidate_header,
        "old_corpus_failure_summary": old,
        "new_candidate_builder_design": design,
        "schema_file": str(OUT_SCHEMA.relative_to(ROOT)),
        "v3r24_prototype_plan": prototype,
        "decision_flags": flags,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    old = summary["old_corpus_failure_summary"]
    design = summary["new_candidate_builder_design"]
    proto = summary["v3r24_prototype_plan"]
    flags = summary["decision_flags"]
    flag_lines = "\n".join(f"- `{k}`: `{str(v).lower()}`" for k, v in flags.items())
    return f"""# Run-004 Patch V3R-23 — New V3 Candidate Builder Design

Status: **research-only / design-only**. No LIVE. No promote. No production signals. No executor/runtime behavior change. No H19. No retrain. No expansion.

## 1. Old Corpus Failure Summary

- diagnostics only: **{old['usable_for_diagnostics_only']}**
- reusable for training: **{old['old_corpus_reusable_for_training']}**
- expandable: **{old['old_corpus_expandable']}**
- sell-biased source: TOKEN_TO_WETH={old['failure_roots']['sell_biased_source']['token_to_weth_count']}, WETH_TO_TOKEN={old['failure_roots']['sell_biased_source']['weth_to_token_count']}
- clean rows too few: **{old['failure_roots']['clean_rows_too_few']}**
- pool/token concentration: `{old['failure_roots']['pool_token_over_concentrated']}`
- clean fee distribution: `{old['failure_roots']['fee_tier_single_clean_source']}`
- cannot build >=20 rows with top5 <= 0.55: **{old['failure_roots']['cannot_build_20_rows_top5_lte_055']}**

Conclusion: V3R-12 corpus is useful for diagnostics and regression tests only; it must not be expanded into training.

## 2. New Candidate Builder Design

Stage 1 — Raw Event Intake:
- accept Base Uniswap V3 WETH-pair Swap observations only
- preserve source_run, tx_hash, log_index, block_number, timestamp, pool, token0/token1, fee, amount0/amount1, inferred token_in/token_out, direction, WETH amount
- amount0/amount1 follow Uniswap V3 Swap event signed pool-delta semantics; unknown field semantics stop promotion

Stage 2 — Direction Filter:
- WETH_TO_TOKEN -> long-entry candidate bucket
- TOKEN_TO_WETH -> sell diagnostics only
- UNKNOWN -> non-trainable
- never reverse sell observations into long-entry labels

Stage 3 — Notional Policy:
- `{design['stage_3_notional_policy']['rule']}`
- caps: conservative={design['stage_3_notional_policy']['conservative_cap_weth']}, normal={design['stage_3_notional_policy']['normal_cap_weth']}, upper pilot={design['stage_3_notional_policy']['upper_pilot_cap_weth']} WETH

Stage 4 — Horizon Policy:
- prefer timestamp horizons: 5min / 30min
- fixed-block is only `fixed_block_pilot`, never called 30min
- require state change: slot0/liquidity/WETH balance/token balance

Stage 5 — Quote / Label:
- entry quote: WETH -> token at entry_block
- exit quote: token -> WETH at horizon exit_block
- label_return = future_horizon_return only
- same-block return is sanity only
- quote failures are failure buckets, not -100% labels

Stage 6 — Toxic / Failure Filters:
- `{design['stage_6_toxic_failure_filters']}`

Stage 7 — Diversity Controls:
- pool/token caps at construction time
- top1/top5 pool and token gates: 0.20 / 0.55
- fee-tier stratification and tx_hash/log_index dedupe
- fee-specific datasets must be explicitly named

## 3. Schema Summary

Schema artifact: `{summary['schema_file']}`

Required groups:
- source/provenance: source_run, tx_hash, log_index, block_number, timestamp
- pool/swap semantics: pool_address, token0, token1, fee, amount0, amount1, token_in, token_out, observed_direction
- notional: observed_weth_amount_raw, observed_weth_amount_weth, selected_notional_weth, selected_notional_reason, notional_cap_weth
- horizon/state: entry_block, exit_block, exit_block_policy, state_changed, state_change_fields
- label: entry_quote_ok, entry_amount_out, horizon_exit_quote_ok, horizon_exit_amount_out_weth, label_return
- sanity/failure: same_block_sanity_return, same_block_is_label, failure_reason, trainable_candidate_flag, non_trainable_reason

## 4. V3R-24 Prototype Plan

- raw buy observation target max: **{proto['raw_buy_observation_target_max']}**
- source filter: **{proto['source_filter']}**
- state-change prefilter: **{proto['state_change_prefilter']}**
- notional cap default: **{proto['notional_cap_default_weth']} WETH**
- sensitivity: `{proto['notional_sensitivity_weth']}`
- pool/token cap: **{proto['pool_token_cap']}**
- fee stratification: **{proto['fee_tier_stratification']}**
- output row cap: **{proto['output_row_cap']}**
- expected minimum clean rows: **{proto['expected_minimum_clean_rows']}**
- RPC max eth_calls: **{proto['rpc_budget']['max_eth_calls']}**
- go/no-go gates: `{proto['go_no_go_gates']}`

## 5. Required Answers

1. Old V3R-12 corpus cannot continue as training source because it is sell-biased, clean rows are fee10000-only and pool/token concentrated, and it cannot satisfy >=20 diversified clean rows.
2. Old corpus is diagnostics-only: **true**.
3. New builder must solve direction, notional, state-change horizon, toxic failures, and diversity at source.
4. Sell observations are kept in diagnostics only and never reversed into long-entry.
5. 0.0005 WETH issue is avoided by observed-size capped adaptive notional.
6. Horizon requires state_changed=true over entry->exit.
7. Pool/token concentration is controlled during candidate construction with caps and top1/top5 gates.
8. Fee tier single-source is handled by fee-tier stratification or explicit fee-specific dataset naming.
9. New schema is in `{summary['schema_file']}`.
10. V3R-24 should run a <=100-row bounded prototype with WETH_TO_TOKEN-only, state-change prefilter, adaptive notional, pool/token caps, and fee stratification.
11. Allow V3R-24 bounded prototype: **{flags['allow_v3r24_bounded_prototype']}**.
12. Expansion/model/mainline/live: **False / False / False / False**.

## 6. Decision Flags

{flag_lines}
"""


def main() -> None:
    summary = build_summary()
    schema = build_schema()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_SCHEMA.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_markdown(summary), encoding="utf-8")
    print(f"[done] wrote {OUT_SCHEMA.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_JSON.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_MD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
