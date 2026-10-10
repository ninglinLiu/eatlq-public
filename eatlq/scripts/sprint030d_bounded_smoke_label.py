#!/usr/bin/env python3
"""Bounded dashcam label execution — manifest-first, sequential RPC quotes.

Profiles:
  smoke                           — Sprint030D caps (≤5 pools, ≤250 rows).
  dryrun_top10                      — Sprint030E top-10 strict cohort (≤10 pools, ≤1000 rows).
  sprint032b_metadata_verified_smoke — Sprint032B metadata CSV + hard-quota manifest repair smoke.

Does not print RPC URLs or secrets.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DASHCAM_DEFAULT = REPO / "logs/dashcam.csv"
BUCKET_MS = 300_000
BUCKET_SEC = 300


@dataclass(frozen=True)
class RunProfile:
    name: str
    manifest_path: Path
    output_csv: Path
    summary_json: Path
    report_md: Path
    report_title: str
    manifest_kind: str
    summary_kind: str
    max_pools: int
    label_limit: int
    max_wall_sec: float
    max_conceptual_pairs: int
    max_rpc_round_trips: int
    min_attempted_pass: int
    max_attempted_pass: int
    min_labeled_soft: int
    pool_selection_rule: str
    manifest_extra_sources: tuple[str, ...]
    decision_flags: dict[str, Any]
    inline_manifest_samples: bool = False


PROFILE_SMOKE = RunProfile(
    name="smoke",
    manifest_path=REPO / "mvp/diagnostics/sprint030d_smoke_manifest.json",
    output_csv=REPO / "mvp/diagnostics/sprint030d_bounded_smoke_labels.csv",
    summary_json=REPO / "mvp/diagnostics/sprint030d_bounded_smoke_label_summary.json",
    report_md=REPO / "mvp/audits/sprint030d_bounded_smoke_label_execution_report.md",
    report_title="Sprint030D — Bounded smoke label execution report",
    manifest_kind="bounded_smoke_label_manifest",
    summary_kind="bounded_smoke_label_execution",
    max_pools=5,
    label_limit=250,
    max_wall_sec=1200.0,
    max_conceptual_pairs=600,
    max_rpc_round_trips=1_000_000,
    min_attempted_pass=1,
    max_attempted_pass=250,
    min_labeled_soft=50,
    pool_selection_rule="top_ranked_strict_intersection_pools_up_to_5_by_sprint028c_order",
    manifest_extra_sources=(),
    decision_flags={
        "FAILED_NO_7D_ALPHA_ACTIVE": True,
        "SMOKE_EXECUTION_ALLOWED": True,
        "LABELING_ALLOWED": "smoke_only",
        "TRAINING_ALLOWED": False,
        "H12_ABLATION_ALLOWED": False,
        "PHASE6_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "RETRAIN_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "MICRO_LIVE_EXECUTION_ALLOWED": False,
        "SMART_MONEY_GENERAL_LINE_OPEN": False,
        "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
    },
)

PROFILE_SPRINT031A_NEW_UNIVERSE = RunProfile(
    name="sprint031a_new_universe_smoke",
    manifest_path=REPO / "mvp/diagnostics/sprint031a_new_universe_smoke_manifest.json",
    output_csv=REPO / "mvp/diagnostics/sprint031a_new_universe_smoke_labels.csv",
    summary_json=REPO / "mvp/diagnostics/sprint031a_new_universe_smoke_label_summary.json",
    report_md=REPO / "mvp/diagnostics/sprint031a_new_universe_smoke_label_execution_report.md",
    report_title="Sprint031A — New-universe bounded smoke label execution (diagnostic)",
    manifest_kind="new_universe_bounded_smoke_manifest",
    summary_kind="sprint031a_new_universe_bounded_smoke_label_execution",
    max_pools=5,
    label_limit=250,
    max_wall_sec=1500.0,
    max_conceptual_pairs=600,
    max_rpc_round_trips=30000,
    min_attempted_pass=1,
    max_attempted_pass=250,
    min_labeled_soft=50,
    pool_selection_rule=(
        "precomputed_offline_manifest_mvp_diagnostics_sprint031a_new_universe_smoke_manifest_json "
        "(028c rank ∩ sidecar ∩ dashcam; excludes Sprint030E blended cohort pools)"
    ),
    manifest_extra_sources=(
        "mvp/diagnostics/sprint031a_return_to_universe_mining_summary.json",
        "mvp/audits/sprint030h_retimed_manifest_readout.md",
        "mvp/diagnostics/sprint030h_retimed_subset_readout_summary.json",
    ),
    decision_flags={
        "FAILED_NO_7D_ALPHA_ACTIVE": True,
        "RETURN_TO_UNIVERSE_MINING_ALLOWED": True,
        "NEW_COHORT_SMOKE_LABEL_ALLOWED": "bounded_only",
        "LABELING_ALLOWED": "sprint031a_bounded_smoke_manifest_only",
        "TRAINING_ALLOWED": False,
        "H12_ABLATION_ALLOWED": False,
        "PHASE6_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "RETRAIN_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "MICRO_LIVE_EXECUTION_ALLOWED": False,
        "SMART_MONEY_GENERAL_LINE_OPEN": False,
        "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
    },
)


PROFILE_DRYRUN_TOP10 = RunProfile(
    name="dryrun_top10",
    manifest_path=REPO / "mvp/diagnostics/sprint030e_top10_dryrun_manifest.json",
    output_csv=REPO / "mvp/diagnostics/sprint030e_top10_scoped_dryrun_labels.csv",
    summary_json=REPO / "mvp/diagnostics/sprint030e_top10_scoped_dryrun_label_summary.json",
    report_md=REPO / "mvp/audits/sprint030e_top10_scoped_dryrun_label_report.md",
    report_title="Sprint030E — Top10 scoped dry-run label report",
    manifest_kind="bounded_top10_scoped_dryrun_manifest",
    summary_kind="bounded_top10_scoped_dryrun_label_execution",
    max_pools=10,
    label_limit=1000,
    max_wall_sec=2700.0,
    max_conceptual_pairs=2300,
    max_rpc_round_trips=75000,
    min_attempted_pass=300,
    max_attempted_pass=1000,
    min_labeled_soft=50,
    pool_selection_rule="top_ranked_strict_intersection_pools_up_to_10_by_sprint028c_order",
    manifest_extra_sources=(
        "mvp/diagnostics/sprint030d_bounded_smoke_label_summary.json",
        "mvp/diagnostics/sprint030d_smoke_manifest.json",
        "mvp/audits/sprint030d_bounded_smoke_label_execution_report.md",
    ),
    decision_flags={
        "FAILED_NO_7D_ALPHA_ACTIVE": True,
        "SCOPED_DRYRUN_LABEL_EXECUTION_ALLOWED": True,
        "LABELING_ALLOWED": "scoped_dryrun_only",
        "TRAINING_ALLOWED": False,
        "H12_ABLATION_ALLOWED": False,
        "PHASE6_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "RETRAIN_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "MICRO_LIVE_EXECUTION_ALLOWED": False,
        "SMART_MONEY_GENERAL_LINE_OPEN": False,
        "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
    },
)


PROFILE_SPRINT032A_HARD_QUOTA_SMOKE = RunProfile(
    name="sprint032a_hard_quota_smoke",
    manifest_path=REPO / "mvp/diagnostics/sprint032a_hard_quota_smoke_manifest.json",
    output_csv=REPO / "mvp/diagnostics/sprint032a_hard_quota_smoke_labels.csv",
    summary_json=REPO / "mvp/diagnostics/sprint032a_hard_quota_smoke_label_summary.json",
    report_md=REPO / "mvp/audits/sprint032a_hard_quota_smoke_label_execution_report.md",
    report_title="Sprint032A — Hard-quota bounded smoke label execution",
    manifest_kind="hard_quota_new_universe_smoke_manifest",
    summary_kind="sprint032a_hard_quota_bounded_smoke_label_execution",
    max_pools=6,
    label_limit=250,
    max_wall_sec=1800.0,
    max_conceptual_pairs=700,
    max_rpc_round_trips=40_000,
    min_attempted_pass=1,
    max_attempted_pass=250,
    min_labeled_soft=114,
    pool_selection_rule=(
        "precomputed_manifest_mvp_diagnostics_sprint032a_hard_quota_smoke_manifest_json "
        "(dashcam overlap window + hard per-pool quota; CH unreachable sidecar intersection disjoint fallback)"
    ),
    manifest_extra_sources=(
        "mvp/diagnostics/sprint032a_hard_quota_universe_summary.json",
        "mvp/diagnostics/sprint032a_hard_quota_candidate_summary.csv",
        "mvp/audits/sprint031c_rebalanced_lineage_readout.md",
        "mvp/diagnostics/sprint031c_rebalanced_lineage_readout_summary.json",
        "mvp/audits/sprint031b_new_lineage_dryrun_report.md",
        "mvp/diagnostics/sprint031a_return_to_universe_mining_report.md",
        "mvp/audits/sprint030h_retimed_manifest_readout.md",
        "docs/label-semantics-freeze.md",
        "mvp/audits/failed_no_7d_alpha_guard.md",
    ),
    decision_flags={
        "FAILED_NO_7D_ALPHA_ACTIVE": True,
        "HARD_QUOTA_UNIVERSE_MINING_ALLOWED": True,
        "HARD_QUOTA_SMOKE_LABEL_ALLOWED": "bounded_only",
        "LABELING_ALLOWED": "sprint032a_hard_quota_bounded_manifest_only",
        "TRAINING_ALLOWED": False,
        "H12_ABLATION_ALLOWED": False,
        "PHASE6_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "RETRAIN_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "MICRO_LIVE_EXECUTION_ALLOWED": False,
        "SMART_MONEY_GENERAL_LINE_OPEN": False,
        "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
    },
)


PROFILE_SPRINT032B_METADATA_VERIFIED_SMOKE = RunProfile(
    name="sprint032b_metadata_verified_smoke",
    manifest_path=REPO / "mvp/diagnostics/sprint032b_metadata_verified_smoke_manifest.json",
    output_csv=REPO / "mvp/diagnostics/sprint032b_metadata_verified_smoke_labels.csv",
    summary_json=REPO / "mvp/diagnostics/sprint032b_metadata_verified_smoke_label_summary.json",
    report_md=REPO / "mvp/audits/sprint032b_metadata_verified_smoke_label_execution_report.md",
    report_title="Sprint032B — Metadata-verified bounded smoke label execution",
    manifest_kind="metadata_verified_hard_quota_smoke_manifest",
    summary_kind="sprint032b_metadata_verified_bounded_smoke_label_execution",
    max_pools=6,
    label_limit=250,
    max_wall_sec=2700.0,
    max_conceptual_pairs=700,
    max_rpc_round_trips=50_000,
    min_attempted_pass=1,
    max_attempted_pass=250,
    min_labeled_soft=114,
    pool_selection_rule=(
        "precomputed_manifest_mvp_diagnostics_sprint032b_metadata_verified_smoke_manifest_json "
        "with_pool_meta_from_mvp_diagnostics_sprint032b_pool_metadata_audit_csv_PASS_rows_only"
    ),
    manifest_extra_sources=(
        "mvp/diagnostics/sprint032b_failure_diagnosis_summary.json",
        "mvp/diagnostics/sprint032b_pool_metadata_audit.csv",
        "mvp/audits/sprint032a_hard_quota_universe_smoke_report.md",
        "mvp/diagnostics/sprint032a_hard_quota_smoke_manifest.json",
        "mvp/diagnostics/sprint032a_hard_quota_smoke_label_summary.json",
        "scripts/sprint032b_metadata_audit_manifest_builder.py",
        "docs/label-semantics-freeze.md",
        "mvp/audits/failed_no_7d_alpha_guard.md",
    ),
    decision_flags={
        "FAILED_NO_7D_ALPHA_ACTIVE": True,
        "LABEL_SEMANTICS_REPAIR_ALLOWED": True,
        "METADATA_VERIFIED_SMOKE_LABEL_ALLOWED": "bounded_only",
        "LABELING_ALLOWED": "sprint032b_metadata_verified_bounded_manifest_only",
        "TRAINING_ALLOWED": False,
        "H12_ABLATION_ALLOWED": False,
        "PHASE6_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "RETRAIN_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "MICRO_LIVE_EXECUTION_ALLOWED": False,
        "SMART_MONEY_GENERAL_LINE_OPEN": False,
        "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
    },
)


PROFILE_SPRINT032C_VIABILITY_VERIFIED_SMOKE = RunProfile(
    name="sprint032c_viability_verified_smoke",
    manifest_path=REPO / "mvp/diagnostics/sprint032c_viability_verified_smoke_manifest.json",
    output_csv=REPO / "mvp/diagnostics/sprint032c_viability_verified_smoke_labels.csv",
    summary_json=REPO / "mvp/diagnostics/sprint032c_viability_verified_smoke_label_summary.json",
    report_md=REPO / "mvp/audits/sprint032c_viability_verified_smoke_label_execution_report.md",
    report_title="Sprint032C — Historical-viability verified bounded smoke label execution",
    manifest_kind="viability_verified_hard_quota_smoke_manifest",
    summary_kind="sprint032c_viability_verified_bounded_smoke_label_execution",
    max_pools=6,
    label_limit=250,
    max_wall_sec=2700.0,
    max_conceptual_pairs=700,
    max_rpc_round_trips=70_000,
    min_attempted_pass=1,
    max_attempted_pass=250,
    min_labeled_soft=114,
    pool_selection_rule=(
        "precomputed_manifest_mvp_diagnostics_sprint032c_viability_verified_smoke_manifest_json "
        "with_historical_entry_viability_PASS_only_and_prelabel_pool_eligibility"
    ),
    manifest_extra_sources=(
        "mvp/diagnostics/sprint032c_historical_failure_autopsy.json",
        "mvp/diagnostics/sprint032c_historical_viability_audit.csv",
        "mvp/diagnostics/sprint032c_viability_verified_smoke_manifest.json",
        "scripts/sprint032c_historical_viability_builder.py",
        "scripts/sprint032b_metadata_audit_manifest_builder.py",
        "docs/label-semantics-freeze.md",
        "mvp/audits/failed_no_7d_alpha_guard.md",
    ),
    decision_flags={
        "FAILED_NO_7D_ALPHA_ACTIVE": True,
        "HISTORICAL_VIABILITY_REPAIR_ALLOWED": True,
        "VIABILITY_VERIFIED_SMOKE_LABEL_ALLOWED": "bounded_only",
        "LABELING_ALLOWED": "sprint032c_viability_verified_bounded_manifest_only",
        "TRAINING_ALLOWED": False,
        "H12_ABLATION_ALLOWED": False,
        "PHASE6_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "RETRAIN_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "MICRO_LIVE_EXECUTION_ALLOWED": False,
        "SMART_MONEY_GENERAL_LINE_OPEN": False,
        "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
    },
)


PROFILE_SPRINT035A_LIVE_ACTIVE_SCOPED_SMOKE = RunProfile(
    name="sprint035a_live_active_scoped_smoke",
    manifest_path=REPO / "mvp/diagnostics/sprint035b_three_pool_live_active_smoke_manifest.json",
    output_csv=REPO / "mvp/diagnostics/sprint035b_three_pool_live_active_smoke_labels.csv",
    summary_json=REPO / "mvp/diagnostics/sprint035b_three_pool_live_active_smoke_label_summary.json",
    report_md=REPO / "mvp/audits/sprint035b_three_pool_live_active_smoke_report.md",
    report_title="Sprint035B — Three-pool live-active scoped bounded smoke label execution",
    manifest_kind="three_pool_live_active_scoped_smoke_manifest",
    summary_kind="sprint035b_three_pool_live_active_bounded_smoke_label_execution",
    max_pools=3,
    label_limit=250,
    max_wall_sec=2700.0,
    max_conceptual_pairs=700,
    max_rpc_round_trips=60_000,
    min_attempted_pass=1,
    max_attempted_pass=250,
    min_labeled_soft=100,
    pool_selection_rule=(
        "precomputed_manifest_mvp_diagnostics_sprint035b_three_pool_live_active_smoke_manifest_json "
        "(sprint035a_viability_pass_pools_only_explicit_signal_ts_ms_when_bucket_cardinality_sparse)"
    ),
    manifest_extra_sources=(
        "mvp/diagnostics/sprint035b_three_pool_live_active_smoke_manifest.json",
        "mvp/diagnostics/sprint035a_live_active_mining_summary.json",
        "mvp/diagnostics/sprint035a_live_active_viability_audit.csv",
        "scripts/sprint035b_three_pool_live_active_manifest_builder.py",
        "scripts/sprint035a_live_active_pool_mining.py",
        "docs/label-semantics-freeze.md",
        "docs/ml-training-gate.md",
        "mvp/audits/failed_no_7d_alpha_guard.md",
    ),
    decision_flags={
        "FAILED_NO_7D_ALPHA_ACTIVE": True,
        "SPRINT034_DEAD_SCOPE_ABANDONED": True,
        "LIVE_ACTIVE_SCOPED_MANIFEST_LABEL_ALLOWED": "bounded_only",
        "LABELING_ALLOWED": "sprint035b_three_pool_live_active_scoped_manifest_only",
        "TRAINING_ALLOWED": False,
        "H12_ABLATION_ALLOWED": False,
        "PHASE6_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "RETRAIN_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "MICRO_LIVE_EXECUTION_ALLOWED": False,
        "SMART_MONEY_GENERAL_LINE_OPEN": False,
        "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
    },
)


PROFILE_SPRINT035C_EXTERNAL_LIVE_ACTIVE_SMOKE = RunProfile(
    name="sprint035c_external_live_active_smoke",
    manifest_path=REPO / "mvp/diagnostics/sprint035c_external_live_active_smoke_manifest.json",
    output_csv=REPO / "mvp/diagnostics/sprint035c_external_live_active_smoke_labels.csv",
    summary_json=REPO / "mvp/diagnostics/sprint035c_external_live_active_smoke_label_summary.json",
    report_md=REPO / "mvp/audits/sprint035c_external_live_active_smoke_label_report.md",
    report_title="Sprint035C — External live-active bounded smoke label execution",
    manifest_kind="external_live_active_scoped_smoke_manifest",
    summary_kind="sprint035c_external_live_active_bounded_smoke_label_execution",
    max_pools=6,
    label_limit=250,
    max_wall_sec=3600.0,
    max_conceptual_pairs=700,
    max_rpc_round_trips=80_000,
    min_attempted_pass=1,
    max_attempted_pass=250,
    min_labeled_soft=100,
    pool_selection_rule=(
        "precomputed_manifest_mvp_diagnostics_sprint035c_external_live_active_smoke_manifest_json "
        "(gecko_terminal_dex_uniswap-v3-base_plus_dexscreener_base_identity_factory_validated_clickhouse_buckets_inline_ts)"
    ),
    manifest_extra_sources=(
        "mvp/diagnostics/sprint035c_external_live_active_smoke_manifest.json",
        "mvp/diagnostics/sprint035c_external_candidate_pool_summary.csv",
        "mvp/diagnostics/sprint035c_pool_identity_validation.csv",
        "mvp/diagnostics/sprint035c_activity_viability_audit.csv",
        "scripts/sprint035c_external_live_active_pool_discovery.py",
        "mvp/diagnostics/sprint035a_live_active_mining_summary.json",
        "docs/label-semantics-freeze.md",
        "docs/ml-training-gate.md",
        "mvp/audits/failed_no_7d_alpha_guard.md",
    ),
    decision_flags={
        "FAILED_NO_7D_ALPHA_ACTIVE": True,
        "SPRINT034_DEAD_SCOPE_ABANDONED": True,
        "EXTERNAL_LIVE_ACTIVE_DISCOVERY_ALLOWED": True,
        "POOL_IDENTITY_VALIDATION_REQUIRED": True,
        "LABELING_ALLOWED": "sprint035c_external_manifest_only",
        "TRAINING_ALLOWED": False,
        "H12_ABLATION_ALLOWED": False,
        "PHASE6_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "RETRAIN_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "MICRO_LIVE_EXECUTION_ALLOWED": False,
        "SMART_MONEY_GENERAL_LINE_OPEN": False,
        "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
    },
    inline_manifest_samples=True,
)


PROFILE_SPRINT035D_MOMENTUM_FILTERED_SMOKE = RunProfile(
    name="sprint035d_momentum_filtered_smoke",
    manifest_path=REPO / "mvp/diagnostics/sprint035d_momentum_filtered_smoke_manifest.json",
    output_csv=REPO / "mvp/diagnostics/sprint035d_momentum_filtered_smoke_labels.csv",
    summary_json=REPO / "mvp/diagnostics/sprint035d_momentum_filtered_smoke_label_summary.json",
    report_md=REPO / "mvp/audits/sprint035d_momentum_filtered_smoke_label_report.md",
    report_title="Sprint035D — Momentum-filtered bounded smoke label execution",
    manifest_kind="momentum_filtered_external_scoped_smoke_manifest",
    summary_kind="sprint035d_momentum_filtered_bounded_smoke_label_execution",
    max_pools=6,
    label_limit=250,
    max_wall_sec=3600.0,
    max_conceptual_pairs=700,
    max_rpc_round_trips=80_000,
    min_attempted_pass=1,
    max_attempted_pass=250,
    min_labeled_soft=100,
    pool_selection_rule=(
        "precomputed_manifest_mvp_diagnostics_sprint035d_momentum_filtered_smoke_manifest_json "
        "(gecko_terminal_dex_uniswap-v3-base_plus_dexscreener_batch_momentum_scored_gate_ge_3_"
        "identity_factory_validated_rpc_synthetic_7d_buckets_inline_ts)"
    ),
    manifest_extra_sources=(
        "mvp/diagnostics/sprint035d_momentum_filtered_smoke_manifest.json",
        "mvp/diagnostics/sprint035d_momentum_filtered_candidate_summary.csv",
        "mvp/diagnostics/sprint035d_pool_identity_validation.csv",
        "mvp/diagnostics/sprint035d_activity_viability_audit.csv",
        "scripts/sprint035d_momentum_filtered_discovery.py",
        "scripts/sprint035c_external_live_active_pool_discovery.py",
        "docs/label-semantics-freeze.md",
        "docs/ml-training-gate.md",
        "mvp/audits/failed_no_7d_alpha_guard.md",
    ),
    decision_flags={
        "FAILED_NO_7D_ALPHA_ACTIVE": True,
        "SPRINT034_DEAD_SCOPE_ABANDONED": True,
        "EXTERNAL_LIVE_ACTIVE_DISCOVERY_ALLOWED": True,
        "MOMENTUM_FILTER_APPLIED": True,
        "POOL_IDENTITY_VALIDATION_REQUIRED": True,
        "LABELING_ALLOWED": "sprint035d_momentum_filtered_manifest_only",
        "TRAINING_ALLOWED": False,
        "H12_ABLATION_ALLOWED": False,
        "PHASE6_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "RETRAIN_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "MICRO_LIVE_EXECUTION_ALLOWED": False,
        "SMART_MONEY_GENERAL_LINE_OPEN": False,
        "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
    },
    inline_manifest_samples=True,
)


PROFILE_SPRINT035E_OHLCV_ANCHORED_SMOKE = RunProfile(
    name="sprint035e_ohlcv_anchored_smoke",
    manifest_path=REPO / "mvp/diagnostics/sprint035e_ohlcv_anchored_smoke_manifest.json",
    output_csv=REPO / "mvp/diagnostics/sprint035e_ohlcv_anchored_smoke_labels.csv",
    summary_json=REPO / "mvp/diagnostics/sprint035e_ohlcv_anchored_smoke_label_summary.json",
    report_md=REPO / "mvp/audits/sprint035e_ohlcv_anchored_smoke_label_report.md",
    report_title="Sprint035E — OHLCV-anchored tight momentum bounded smoke label execution",
    manifest_kind="ohlcv_anchored_tight_momentum_scoped_smoke_manifest",
    summary_kind="sprint035e_ohlcv_anchored_bounded_smoke_label_execution",
    max_pools=6,
    label_limit=250,
    max_wall_sec=3600.0,
    max_conceptual_pairs=700,
    max_rpc_round_trips=80_000,
    min_attempted_pass=1,
    max_attempted_pass=250,
    min_labeled_soft=100,
    pool_selection_rule=(
        "precomputed_manifest_mvp_diagnostics_sprint035e_ohlcv_anchored_smoke_manifest_json "
        "(gecko_terminal_dex_uniswap-v3-base_plus_dexscreener_batch_momentum_v2_scored_gate_ge_4_"
        "identity_factory_validated_ohlcv_bullish_candle_anchored_timestamps)"
    ),
    manifest_extra_sources=(
        "mvp/diagnostics/sprint035e_ohlcv_anchored_smoke_manifest.json",
        "mvp/diagnostics/sprint035e_ohlcv_anchored_candidate_summary.csv",
        "mvp/diagnostics/sprint035e_pool_identity_validation.csv",
        "mvp/diagnostics/sprint035e_activity_viability_audit.csv",
        "scripts/sprint035e_tight_momentum_ohlcv_discovery.py",
        "scripts/sprint035c_external_live_active_pool_discovery.py",
        "docs/label-semantics-freeze.md",
        "docs/ml-training-gate.md",
        "mvp/audits/failed_no_7d_alpha_guard.md",
    ),
    decision_flags={
        "FAILED_NO_7D_ALPHA_ACTIVE": True,
        "SPRINT034_DEAD_SCOPE_ABANDONED": True,
        "EXTERNAL_LIVE_ACTIVE_DISCOVERY_ALLOWED": True,
        "MOMENTUM_FILTER_V2_APPLIED": True,
        "OHLCV_ANCHORED_MANIFEST": True,
        "POOL_IDENTITY_VALIDATION_REQUIRED": True,
        "LABELING_ALLOWED": "sprint035e_ohlcv_anchored_manifest_only",
        "TRAINING_ALLOWED": False,
        "H12_ABLATION_ALLOWED": False,
        "PHASE6_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "RETRAIN_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "MICRO_LIVE_EXECUTION_ALLOWED": False,
        "SMART_MONEY_GENERAL_LINE_OPEN": False,
        "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
    },
    inline_manifest_samples=True,
)


PROFILE_SPRINT031B_NEW_LINEAGE_DRYRUN = RunProfile(
    name="sprint031b_new_lineage_dryrun",
    manifest_path=REPO / "mvp/diagnostics/sprint031b_new_lineage_dryrun_manifest.json",
    output_csv=REPO / "mvp/diagnostics/sprint031b_new_lineage_dryrun_labels.csv",
    summary_json=REPO / "mvp/diagnostics/sprint031b_new_lineage_dryrun_label_summary.json",
    report_md=REPO / "mvp/diagnostics/sprint031b_new_lineage_dryrun_label_execution_report.md",
    report_title="Sprint031B — New lineage bounded dry-run label execution",
    manifest_kind="new_lineage_bounded_dryrun_manifest",
    summary_kind="sprint031b_new_lineage_bounded_dryrun_label_execution",
    max_pools=8,
    label_limit=1000,
    max_wall_sec=3600.0,
    max_conceptual_pairs=2300,
    max_rpc_round_trips=120_000,
    min_attempted_pass=300,
    max_attempted_pass=1000,
    min_labeled_soft=50,
    pool_selection_rule=(
        "precomputed_manifest_mvp_diagnostics_sprint031b_new_lineage_dryrun_manifest_json "
        "(031a five-pool lineage expanded via sidecar∩dashcam chronological union)"
    ),
    manifest_extra_sources=(
        "mvp/diagnostics/sprint031a_new_universe_smoke_manifest.json",
        "mvp/diagnostics/sprint031a_new_universe_smoke_label_summary.json",
        "mvp/diagnostics/sprint031b_new_lineage_dryrun_manifest.json",
    ),
    decision_flags={
        "FAILED_NO_7D_ALPHA_ACTIVE": True,
        "NEW_LINEAGE_DRYRUN_ALLOWED": True,
        "LABELING_ALLOWED": "scoped_dryrun_only",
        "TRAINING_ALLOWED": False,
        "H12_ABLATION_ALLOWED": False,
        "PHASE6_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "RETRAIN_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "MICRO_LIVE_EXECUTION_ALLOWED": False,
        "SMART_MONEY_GENERAL_LINE_OPEN": False,
        "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
    },
)


def load_dotenv_files() -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in ("infra/compose/.env", ".env"):
        p = REPO / rel
        if not p.exists():
            continue
        for line in p.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_label_module():
    path = REPO / "scripts" / "label_dashcam_executable.py"
    spec = importlib.util.spec_from_file_location("label_dashcam_executable", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load label_dashcam_executable")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_SEL_TOKEN1 = "0xd21220a7"
_SEL_FEE_V3 = "0xddca3f43"


def _latest_block_num(rpc: Any) -> int:
    r = rpc.call("eth_getBlockByNumber", ["latest", False])
    return int(r["result"]["number"], 16)


def rpc_infer_pool_meta(mod: Any, rpc: Any, pool: str) -> dict[str, Any] | None:
    """Infer pool_type + legs without ClickHouse."""
    try:
        lb = _latest_block_num(rpc)
        t0 = mod.call_token0(rpc, pool, lb).lower()
        bh = hex(lb)
        r1 = rpc.call("eth_call", [{"to": pool, "data": _SEL_TOKEN1}, bh])
        if "error" in r1 or not r1.get("result"):
            return None
        raw = r1["result"]
        if raw in ("0x", "0x0"):
            return None
        t1 = ("0x" + raw[-40:]).lower()
        rfee = rpc.call("eth_call", [{"to": pool, "data": _SEL_FEE_V3}, bh])
        if "error" not in rfee and rfee.get("result"):
            ft = int(rfee["result"], 16)
            if 0 < ft < 1_000_000:
                fb = max(1, ft // 100)
                return {"pool_type": "v3", "t0": t0, "t1": t1, "fee_bps": fb, "fee_raw": ft}
        return {"pool_type": "v2", "t0": t0, "t1": t1, "fee_bps": 30}
    except Exception:
        return None


def strict_intersection_keys() -> tuple[list[tuple[str, int]], dict[str, int], list[str]]:
    """Return sorted list of (pool_lower, bucket_ms) from sidecar ∩ dashcam."""
    with open(REPO / "mvp/diagnostics/sprint028c_scoped_candidate_ranking_summary.json", encoding="utf-8") as f:
        rank_data = json.load(f)
    candidates = sorted(rank_data["candidates"], key=lambda x: x["rank"])
    ranked_pools = [c["pool_address_audit_key"].lower() for c in candidates]
    rank_by_pool = {c["pool_address_audit_key"].lower(): int(c["rank"]) for c in candidates}
    cohort = set(ranked_pools)

    sidecar_keys: set[tuple[str, int]] = set()
    with open(REPO / "mvp/diagnostics/sprint028b_v3_universe_sidecar_sample.csv", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            p = row["_pool_address_AUDIT_ONLY"].strip().lower()
            ts = int(row["_bucket_ts_ms_AUDIT_ONLY"])
            sidecar_keys.add((p, ts))

    dash_keys: set[tuple[str, int]] = set()
    dashcam_path = REPO / "logs/dashcam.csv"
    with dashcam_path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            pool = (row.get("token_address") or "").strip().lower()
            if pool not in cohort:
                continue
            try:
                ts_raw = int(float(row["timestamp"]))
            except (TypeError, ValueError):
                continue
            bms = (ts_raw // BUCKET_MS) * BUCKET_MS
            dash_keys.add((pool, bms))

    inter = sorted(sidecar_keys & dash_keys)
    return inter, rank_by_pool, ranked_pools


def select_manifest_rows(
    inter: list[tuple[str, int]],
    rank_by_pool: dict[str, int],
    ranked_pools: list[str],
    max_pools: int,
    label_limit: int,
) -> list[tuple[str, int, int]]:
    pools_with_keys: list[str] = []
    seen: set[str] = set()
    for p, _ in inter:
        if p not in seen:
            seen.add(p)
            pools_with_keys.append(p)
    ordered_by_rank = sorted(pools_with_keys, key=lambda po: rank_by_pool.get(po, 999))
    chosen_pools = set(ordered_by_rank[:max_pools])

    keys = [(p, bm) for p, bm in inter if p in chosen_pools]
    keys.sort(key=lambda t: (rank_by_pool.get(t[0], 999), t[1]))
    keys = keys[:label_limit]
    out: list[tuple[str, int, int]] = []
    for p, bm in keys:
        bucket_sec = bm // 1000
        out.append((p, bm, bucket_sec))
    return out


def write_manifest(profile: RunProfile, rows: list[tuple[str, int, int]], rank_by_pool: dict[str, int]) -> dict[str, Any]:
    by_pool = Counter(p for p, _, _ in rows)
    pools = sorted(set(by_pool.keys()), key=lambda po: rank_by_pool.get(po, 999))
    sources = [
        "mvp/diagnostics/sprint028c_scoped_candidate_ranking_summary.json",
        "mvp/diagnostics/sprint028b_v3_universe_sidecar_sample.csv",
        "logs/dashcam.csv",
        "mvp/diagnostics/sprint030b_dashcam_cohort_intersection_summary.json",
        "mvp/audits/sprint029a_scoped_validation_design.md",
        "mvp/audits/sprint029d_label_authorization_preflight.md",
        "scripts/sprint030d_bounded_smoke_label.py",
        *list(profile.manifest_extra_sources),
    ]
    manifest = {
        "kind": profile.manifest_kind,
        "source_artifacts": sources,
        "selected_pool_count": len(by_pool),
        "label_limit": profile.label_limit,
        "selected_rows_count": len(rows),
        "pool_selection_rule": profile.pool_selection_rule,
        "bucket_selection_rule": "deterministic_sort_by_rank_then_bucket_ms_then_truncate_to_label_limit",
        "pool_address_audit_keys": pools,
        "bucket_count_by_pool": dict(by_pool),
        "created_before_quote": True,
        "manifest_rows_preview_max20": [
            {"pool_audit_only": p, "bucket_ms": bm, "bucket_sec": bs} for p, bm, bs in rows[:20]
        ],
        "decision_flags": dict(profile.decision_flags),
    }
    profile.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    profile.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def load_precomputed_manifest_rows(
    manifest_path: Path,
) -> tuple[list[tuple[str, int, int]], dict[str, int]]:
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows: list[tuple[str, int, int]] = []
    for r in raw.get("manifest_rows") or []:
        p = str(r["pool_audit_only"]).lower()
        bm = int(r["bucket_ms"])
        bs = int(r["bucket_sec"])
        rows.append((p, bm, bs))
    rank_map = {str(p).lower(): i for i, p in enumerate(raw.get("pool_address_audit_keys") or [])}
    return rows, rank_map


def load_manifest_row_specs(
    manifest_path: Path,
) -> tuple[list[tuple[str, int, int, int | None]], dict[str, int]]:
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if raw.get("status") in (
        "TOO_SMALL_FOR_THREE_POOL_SMOKE",
        "TOO_SMALL_FOR_EXTERNAL_SMOKE",
        "READY_EXTERNAL_POOLS_BUT_NEED_BUCKET_BACKFILL",
        "NO_EXTERNAL_POOL_CANDIDATES_PASS_IDENTITY",
        "TOO_SMALL_FOR_MOMENTUM_SMOKE",
        "READY_MOMENTUM_POOLS_BUT_NEED_BUCKET_BACKFILL",
        "NO_MOMENTUM_CANDIDATES_PASS_FILTER",
        "NO_MOMENTUM_IDENTITY_PASS",
        "TOO_SMALL_FOR_OHLCV_SMOKE",
        "NO_MOMENTUM_CANDIDATES_PASS_FILTER_V2",
        "NO_MOMENTUM_IDENTITY_PASS_V2",
        "TOO_FEW_VIABLE_POOLS",
    ):
        return [], {}
    specs: list[tuple[str, int, int, int | None]] = []
    for r in raw.get("manifest_rows") or []:
        p = str(r["pool_audit_only"]).lower()
        bm = int(r["bucket_ms"])
        bs = int(r["bucket_sec"])
        ts_key = "signal_ts_ms_audit_only"
        raw_ts = r.get(ts_key)
        ts_opt: int | None
        if raw_ts is not None and str(raw_ts).strip().lstrip("-").isdigit():
            ts_opt = int(raw_ts)
        else:
            ts_opt = None
        specs.append((p, bm, bs, ts_opt))
    rk = {str(p).lower(): i for i, p in enumerate(raw.get("pool_address_audit_keys") or [])}
    return specs, rk


def three_pool_live_active_manifest_shape_ok(rows: list[tuple[str, int, int]]) -> tuple[bool, str]:
    """Sprint035B — three pools, 120–250 rows, per-pool cap 90, top1/top2 share caps."""
    n = len(rows)
    if n < 120 or n > 250:
        return False, "rows_not_in_120_250"
    pools = {p for p, _, _ in rows}
    if len(pools) != 3:
        return False, "pool_count_not_three"
    ctr = Counter(p for p, _, _ in rows)
    if max(ctr.values()) > 90:
        return False, "per_pool_gt_90"
    sh = sorted(ctr.values(), reverse=True)
    top1 = sh[0] / n
    top2 = (sh[0] + sh[1]) / n
    if top1 > 0.45 + 1e-12:
        return False, "top1_share_gt_cap"
    if top2 > 0.75 + 1e-12:
        return False, "top2_share_gt_cap"
    return True, "ok"


def external_live_active_manifest_shape_ok(rows: list[tuple[str, int, int]]) -> tuple[bool, str]:
    """Sprint035C — 3–6 pools, 120–250 rows, per-pool ≤90, top1≤0.45; top2≤0.75 (3 pools) or top3≤0.70 (4–6)."""
    n = len(rows)
    pools = {p for p, _, _ in rows}
    np = len(pools)
    if n < 120 or n > 250:
        return False, "rows_not_in_120_250"
    if np < 3 or np > 6:
        return False, "pool_count_not_in_3_6"
    ctr = Counter(p for p, _, _ in rows)
    if max(ctr.values()) > 90:
        return False, "per_pool_gt_90"
    sh = sorted(ctr.values(), reverse=True)
    top1 = sh[0] / n
    if top1 > 0.45 + 1e-12:
        return False, "top1_share_gt_cap"
    if np == 3:
        top2 = (sh[0] + sh[1]) / n
        if top2 > 0.75 + 1e-12:
            return False, "top2_share_gt_cap_three_pool"
    else:
        top3 = sum(sh[: min(3, len(sh))]) / n
        if top3 > 0.70 + 1e-12:
            return False, "top3_share_gt_cap_multi_pool"
    return True, "ok"


def resolve_samples_three_pool_specs(
    dashcam_path: Path,
    specs: list[tuple[str, int, int, int | None]],
) -> tuple[list[dict[str, Any]], int]:
    """Map manifest specs to dashcam samples in deterministic manifest order."""
    idx_exact: dict[tuple[str, int], dict[str, Any]] = {}
    idx_bucket_min: dict[tuple[str, int], dict[str, Any]] = {}
    if not dashcam_path.is_file():
        return [], len(specs)
    with dashcam_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                ts_ms = int(float(row.get("timestamp", 0)))
            except (TypeError, ValueError):
                continue
            if ts_ms <= 0:
                continue
            pool = (row.get("token_address") or "").strip().lower()
            if not pool.startswith("0x"):
                continue
            bsec = (ts_ms // 1000 // BUCKET_SEC) * BUCKET_SEC
            enriched = dict(row)
            enriched["_ts"] = ts_ms
            enriched["_bucket"] = bsec
            idx_exact[(pool, ts_ms)] = enriched
            k = (pool, bsec)
            cur = idx_bucket_min.get(k)
            if cur is None or ts_ms < cur["_ts"]:
                idx_bucket_min[k] = enriched
    out: list[dict[str, Any]] = []
    missing_ct = 0
    for p, bm, bs, ts_opt in specs:
        row = None
        if ts_opt is not None:
            row = idx_exact.get((p, ts_opt))
        if row is None:
            row = idx_bucket_min.get((p, bs))
        if row is None:
            missing_ct += 1
            continue
        ok_bs = row["_bucket"]
        if ok_bs != bs or (ts_opt is not None and row["_ts"] != ts_opt):
            missing_ct += 1
            continue
        out.append(row)
    return out, missing_ct


def inline_samples_from_manifest_specs(
    specs: list[tuple[str, int, int, int | None]],
) -> tuple[list[dict[str, Any]], int]:
    """Dashcam-less rows for CH-backed external manifests (explicit signal_ts_ms + bucket_sec)."""
    out: list[dict[str, Any]] = []
    missing = 0
    for p, _bm, bs, ts_opt in specs:
        if ts_opt is None:
            missing += 1
            continue
        if ts_opt // 1000 // BUCKET_SEC * BUCKET_SEC != bs:
            missing += 1
            continue
        out.append(
            {
                "token_address": p,
                "timestamp": str(ts_opt),
                "symbol": "",
                "_ts": ts_opt,
                "_bucket": bs,
            }
        )
    return out, missing


def hard_quota_manifest_shape_ok(rows: list[tuple[str, int, int]]) -> tuple[bool, str]:
    """Sprint032A frozen manifest gates (pre-quote)."""
    if len(rows) < 120 or len(rows) > 250:
        return False, "rows_outside_120_250"
    pools = {p for p, _, _ in rows}
    if len(pools) < 4 or len(pools) > 6:
        return False, "pool_count_not_in_4_6"
    ctr = Counter(p for p, _, _ in rows)
    if max(ctr.values()) > 60:
        return False, "per_pool_row_cap_gt_60"
    total = len(rows)
    shares = sorted(ctr.values(), reverse=True)
    top1 = shares[0] / total
    top3 = sum(shares[:3]) / total
    if top1 > 0.30 + 1e-12:
        return False, "top1_share_gt_cap"
    if top3 > 0.70 + 1e-12:
        return False, "top3_share_gt_cap"
    return True, "ok"


def load_verified_pool_meta_audit_csv(path: Path) -> dict[str, dict[str, Any]]:
    """Load verified metadata CSV into label meta dicts.

    Supports:
    - Sprint032B metadata audit CSV (`metadata_status`)
    - Sprint032C historical viability CSV (`viability_status` + metadata columns)
    """
    out: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return out
    rows_by_pool: dict[str, list[dict[str, str]]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pool = (row.get("pool_address_audit_key") or "").strip().lower()
            if not pool:
                continue
            rows_by_pool.setdefault(pool, []).append(row)

    for pool, rows in rows_by_pool.items():
        # For historical viability CSV: require no FAIL rows for a pool.
        if any((r.get("viability_status") or "").strip().upper() == "FAIL" for r in rows):
            continue
        # For metadata CSV: require at least one metadata_status PASS row.
        if not any((r.get("metadata_status") or "").strip().upper() == "PASS" for r in rows):
            continue
        chosen = next(
            (
                r
                for r in rows
                if (r.get("metadata_status") or "").strip().upper() == "PASS"
                and (r.get("resolved_pool_type") or "").strip().lower() in ("v2", "v3")
            ),
            None,
        )
        if chosen is None:
            continue
        pt = (chosen.get("resolved_pool_type") or "").strip().lower()
        meta_r: dict[str, Any] = {
            "pool_type": pt,
            "t0": (chosen.get("token0") or "").strip().lower(),
            "t1": (chosen.get("token1") or "").strip().lower(),
        }
        fr = (chosen.get("fee_raw_uint24") or "").strip()
        if fr.isdigit():
            meta_r["fee_raw"] = int(fr)
            meta_r["fee_bps"] = max(1, int(fr) // 100)
        else:
            meta_r["fee_bps"] = 30
        out[pool] = meta_r
    return out


def smoke_gate_passed() -> tuple[bool, str]:
    p = REPO / "mvp/diagnostics/sprint030d_bounded_smoke_label_summary.json"
    if not p.exists():
        return False, "missing_sprint030d_summary"
    sj = json.loads(p.read_text(encoding="utf-8"))
    if sj.get("status") != "PASS":
        return False, "smoke_status_not_pass"
    if not sj.get("acceptance_gates_pass"):
        return False, "acceptance_gates_false"
    if float(sj.get("clean_rate", 0)) < 0.95:
        return False, "clean_rate_low"
    if float(sj.get("failure_rate", 1)) > 0.05:
        return False, "failure_rate_high"
    if int(sj.get("labeled_rows", 0)) < 50:
        return False, "labeled_rows_small"
    return True, "ok"


class CountingRPC:
    def __init__(self, inner: Any):
        self._inner = inner
        self.rpc_round_trips = 0

    def call(self, method: str, params: list):
        self.rpc_round_trips += 1
        return self._inner.call(method, params)

    def batch(self, calls: list):
        self.rpc_round_trips += len(calls)
        return self._inner.batch(calls)


def load_samples_for_manifest(
    dashcam_path: Path,
    wanted: set[tuple[str, int]],
) -> dict[tuple[str, int], dict]:
    by_bucket: dict[tuple[str, int], dict] = {}
    with dashcam_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = int(float(row.get("timestamp", 0)))
            except (TypeError, ValueError):
                continue
            if ts <= 0:
                continue
            pool = (row.get("token_address") or "").lower()
            if not pool.startswith("0x"):
                continue
            bucket_sec = (ts // 1000 // BUCKET_SEC) * BUCKET_SEC
            key = (pool, bucket_sec)
            if key not in wanted:
                continue
            row["_ts"] = ts
            row["_bucket"] = bucket_sec
            cur = by_bucket.get(key)
            if cur is None or ts < cur["_ts"]:
                by_bucket[key] = row
    return by_bucket


def _read_smoke_readout_lines() -> list[str]:
    sp = REPO / "mvp/diagnostics/sprint030d_bounded_smoke_label_summary.json"
    if not sp.exists():
        return ["- Sprint030D summary missing."]
    sj = json.loads(sp.read_text(encoding="utf-8"))
    return [
        f"- attempted_rows: **{sj.get('attempted_rows')}**",
        f"- labeled_rows: **{sj.get('labeled_rows')}**",
        f"- clean_rate: **{sj.get('clean_rate')}**",
        f"- failure_rate: **{sj.get('failure_rate')}**",
        f"- quote_failed_count: **{sj.get('quote_failed_count')}**",
        f"- label_distribution: **{sj.get('label_distribution')}**",
        f"- wall_time_sec: **{sj.get('wall_time_sec')}**",
        f"- conceptual_entry_exit_pairs_budget: **{sj.get('conceptual_entry_exit_pairs_budget')}**",
        f"- approx_eth_rpc_round_trips_total: **{sj.get('approx_eth_rpc_round_trips_total')}**",
        f"- smoke_gate_storage_status: **{sj.get('status')}** / acceptance_gates_pass="
        f"**{sj.get('acceptance_gates_pass')}**",
    ]


def run_profile(profile: RunProfile, manifest_only: bool, dashcam_path: Path) -> int:
    three_specs_ordered: list[tuple[str, int, int, int | None]] | None = None
    if profile.name == "sprint031a_new_universe_smoke":
        if not profile.manifest_path.is_file():
            print("BLOCKED_MISSING_MANIFEST", profile.manifest_path.relative_to(REPO), file=sys.stderr)
            return 9
        rows, rank_by_pool = load_precomputed_manifest_rows(profile.manifest_path)
    elif profile.name == "sprint035a_live_active_scoped_smoke":
        if not profile.manifest_path.is_file():
            print("BLOCKED_MISSING_MANIFEST", profile.manifest_path.relative_to(REPO), file=sys.stderr)
            return 9
        three_specs_ordered, rank_by_pool = load_manifest_row_specs(profile.manifest_path)
        if not three_specs_ordered:
            print("BLOCKED_MANIFEST_TOO_SMALL_THREE_POOL", file=sys.stderr)
            return 22
        rows = [(p, bm, bs) for p, bm, bs, _ts in three_specs_ordered]
        ok_tp, why_tp = three_pool_live_active_manifest_shape_ok(rows)
        if not ok_tp:
            print("BLOCKED_THREE_POOL_MANIFEST_SHAPE", why_tp, file=sys.stderr)
            return 23
    elif profile.name in (
        "sprint035c_external_live_active_smoke",
        "sprint035d_momentum_filtered_smoke",
        "sprint035e_ohlcv_anchored_smoke",
    ):
        if not profile.manifest_path.is_file():
            print("BLOCKED_MISSING_MANIFEST", profile.manifest_path.relative_to(REPO), file=sys.stderr)
            return 9
        three_specs_ordered, rank_by_pool = load_manifest_row_specs(profile.manifest_path)
        if not three_specs_ordered:
            print("BLOCKED_MANIFEST_EMPTY_OR_STUB_EXTERNAL", file=sys.stderr)
            return 24
        rows = [(p, bm, bs) for p, bm, bs, _ts in three_specs_ordered]
        ok_ex, why_ex = external_live_active_manifest_shape_ok(rows)
        if not ok_ex:
            print("BLOCKED_EXTERNAL_MANIFEST_SHAPE", why_ex, file=sys.stderr)
            return 25
    elif profile.name in (
        "sprint032a_hard_quota_smoke",
        "sprint032b_metadata_verified_smoke",
        "sprint032c_viability_verified_smoke",
    ):
        if not profile.manifest_path.is_file():
            print("BLOCKED_MISSING_MANIFEST", profile.manifest_path.relative_to(REPO), file=sys.stderr)
            return 9
        rows, rank_by_pool = load_precomputed_manifest_rows(profile.manifest_path)
        ok_hq, why_hq = hard_quota_manifest_shape_ok(rows)
        if not ok_hq:
            print("BLOCKED_HARD_QUOTA_MANIFEST", why_hq, file=sys.stderr)
            return 19
    elif profile.name == "sprint031b_new_lineage_dryrun":
        if not profile.manifest_path.is_file():
            print("BLOCKED_MISSING_MANIFEST", profile.manifest_path.relative_to(REPO), file=sys.stderr)
            return 9
        rows, rank_by_pool = load_precomputed_manifest_rows(profile.manifest_path)
    else:
        inter, rank_by_pool, ranked_pools = strict_intersection_keys()
        rows = select_manifest_rows(
            inter, rank_by_pool, ranked_pools, max_pools=profile.max_pools, label_limit=profile.label_limit
        )
        write_manifest(profile, rows, rank_by_pool)

    conceptual_pre = len(rows) * 2
    if profile.name == "dryrun_top10":
        ok_smoke, reason = smoke_gate_passed()
        if not ok_smoke:
            print("BLOCKED_SMOKE_NOT_PASS", reason, file=sys.stderr)
            return 10
        if len(rows) < 300:
            print("TOO_SMALL_FOR_DRYRUN manifest_rows", len(rows), file=sys.stderr)
            return 11
        if conceptual_pre > profile.max_conceptual_pairs:
            print("BLOCKED_CAP_EXCEEDED conceptual_pairs_estimate", conceptual_pre, file=sys.stderr)
            return 12

    if profile.name == "sprint031b_new_lineage_dryrun":
        ok_smoke, reason = smoke_gate_passed()
        if not ok_smoke:
            print("BLOCKED_SMOKE_NOT_PASS", reason, file=sys.stderr)
            return 15
        if len(rows) < 300:
            print("TOO_SMALL_FOR_DRYRUN manifest_rows", len(rows), file=sys.stderr)
            return 16
        if len(rows) > 1000:
            print("BLOCKED_CAP_EXCEEDED manifest_rows_gt_1000", len(rows), file=sys.stderr)
            return 17
        if conceptual_pre > profile.max_conceptual_pairs:
            print("BLOCKED_CAP_EXCEEDED conceptual_pairs_estimate", conceptual_pre, file=sys.stderr)
            return 18

    if profile.name == "sprint031a_new_universe_smoke":
        if len(rows) < 100:
            print("BLOCKED_MANIFEST_TOO_SMALL_FOR_SMOKE", len(rows), file=sys.stderr)
            return 13
        if conceptual_pre > profile.max_conceptual_pairs:
            print("BLOCKED_CAP_EXCEEDED conceptual_pairs_estimate", conceptual_pre, file=sys.stderr)
            return 14

    if profile.name in (
        "sprint032a_hard_quota_smoke",
        "sprint032b_metadata_verified_smoke",
        "sprint032c_viability_verified_smoke",
        "sprint035a_live_active_scoped_smoke",
        "sprint035c_external_live_active_smoke",
    ):
        if conceptual_pre > profile.max_conceptual_pairs:
            print("BLOCKED_CAP_EXCEEDED conceptual_pairs_estimate", conceptual_pre, file=sys.stderr)
            return 20

    wanted_sec = {(p, bs) for p, _, bs in rows}
    dashcam_miss_for_skip: int | None = None
    if manifest_only:
        rel = profile.manifest_path.relative_to(REPO)
        if profile.name in (
            "sprint031a_new_universe_smoke",
            "sprint031b_new_lineage_dryrun",
            "sprint032a_hard_quota_smoke",
            "sprint032b_metadata_verified_smoke",
            "sprint032c_viability_verified_smoke",
            "sprint035a_live_active_scoped_smoke",
            "sprint035c_external_live_active_smoke",
        ):
            print("precomputed manifest (no rewrite):", rel)
        else:
            print("manifest written:", rel)
        return 0

    env = {**load_dotenv_files(), **os.environ}
    rpc_a = "LABEL_RPC_URL"
    rpc_b = "EVM_HTTP_URL"
    rpc_url = env.get(rpc_a) or env.get(rpc_b) or ""
    if not rpc_url:
        print("BLOCKED_MISSING_RPC_ENV", file=sys.stderr)
        return 2

    if len(rows) > profile.label_limit or len({p for p, _, _ in rows}) > profile.max_pools:
        print("BLOCKED_CAP_EXCEEDED manifest_shape", file=sys.stderr)
        return 3

    mod = load_label_module()
    needs_dashcam = not profile.inline_manifest_samples
    if needs_dashcam and not dashcam_path.exists():
        print("BLOCKED missing dashcam", file=sys.stderr)
        return 4

    samples: list[dict] = []
    missing: list[tuple[str, int]] = []
    if three_specs_ordered is not None and profile.inline_manifest_samples:
        samples, dm = inline_samples_from_manifest_specs(three_specs_ordered)
        dashcam_miss_for_skip = dm
    elif three_specs_ordered is not None:
        samples, dm = resolve_samples_three_pool_specs(dashcam_path, three_specs_ordered)
        dashcam_miss_for_skip = dm
    else:
        by_s = load_samples_for_manifest(dashcam_path, wanted_sec)
        for p, bm, bs in rows:
            row = by_s.get((p, bs))
            if row is None:
                missing.append((p, bs))
                continue
            samples.append(row)
        dashcam_miss_for_skip = len(missing)

    chain_id = int(env.get("CHAIN_ID", "8453"))
    exit_horizon_sec = int(env.get("LABEL_EXIT_HORIZON_SEC", "1800"))
    notional_wei = int(env.get("LABEL_NOTIONAL_WEI", str(int(0.0005e18))))
    pool_types_env = env.get("LABEL_POOL_TYPES", "v2,v3").strip()
    supported_types = {t.strip().lower() for t in pool_types_env.split(",") if t.strip()}

    rpc_plain = mod.RPC(rpc_url)
    rpc = CountingRPC(rpc_plain)
    r = rpc.call("eth_chainId", [])
    chid = int(r["result"], 16)
    if chid != chain_id:
        print("BLOCKED chain mismatch", file=sys.stderr)
        return 5

    now_ms = int(time.time() * 1000)
    min_age_ms = (exit_horizon_sec + 120) * 1000
    samples = [s for s in samples if now_ms - s["_ts"] >= min_age_ms]

    pools_meta_set = {s["token_address"].lower() for s in samples}
    meta: dict[str, dict[str, Any]] = {}
    infer_failed: list[str] = []
    if profile.name in ("sprint032b_metadata_verified_smoke", "sprint032c_viability_verified_smoke"):
        audit_csv = (
            REPO / "mvp/diagnostics/sprint032b_pool_metadata_audit.csv"
            if profile.name == "sprint032b_metadata_verified_smoke"
            else REPO / "mvp/diagnostics/sprint032c_historical_viability_audit.csv"
        )
        verified_map = load_verified_pool_meta_audit_csv(audit_csv)
        for pool in sorted(pools_meta_set):
            row_meta = verified_map.get(pool)
            if row_meta is None:
                infer_failed.append(pool)
            else:
                meta[pool] = row_meta
    else:
        for pool in sorted(pools_meta_set):
            m = rpc_infer_pool_meta(mod, rpc, pool)
            if m:
                meta[pool] = m
            else:
                infer_failed.append(pool)
    before = len(samples)
    samples = [
        s
        for s in samples
        if meta.get(s["token_address"].lower(), {}).get("pool_type") in supported_types
    ]
    dropped_type = before - len(samples)

    if profile.name in ("sprint032b_metadata_verified_smoke", "sprint032c_viability_verified_smoke") and infer_failed:
        print("BLOCKED_VERIFIED_META_GAP", infer_failed, file=sys.stderr)
        return 21

    if not samples:
        print("BLOCKED no samples after filters age/type/meta", file=sys.stderr)
        return 6

    ts_min = min(s["_bucket"] for s in samples)
    ts_max = max(s["_bucket"] for s in samples) + exit_horizon_sec
    t_wall0 = time.time()
    bn_arr, ts_arr = mod.build_ts_grid(rpc, ts_min, ts_max)
    if time.time() - t_wall0 > profile.max_wall_sec:
        print("BLOCKED_CAP_EXCEEDED ts_grid_wall_time", file=sys.stderr)
        return 7

    WETH = mod.WETH_BASE

    profile.output_csv.parent.mkdir(parents=True, exist_ok=True)
    labeled = 0
    quote_failed = 0
    skipped_pre_label = int(dashcam_miss_for_skip or 0) + dropped_type + len(infer_failed)
    reason_counts: Counter[str] = Counter()
    quoter_like_calls = 0

    with profile.output_csv.open("w", newline="", encoding="utf-8") as f_out:
        w = csv.writer(f_out)
        w.writerow(
            [
                "timestamp_ms",
                "pool_address",
                "bucket_sec",
                "symbol",
                "label",
                "net_return",
                "label_type",
            ]
        )

        for sample in samples:
            if labeled + quote_failed >= profile.label_limit:
                break
            ts = sample["_ts"]
            pool = sample["token_address"].lower()
            signal_ts_sec = ts // 1000
            exit_ts_sec = signal_ts_sec + exit_horizon_sec
            entry_block = mod.ts_to_block(bn_arr, ts_arr, signal_ts_sec)
            exit_block = mod.ts_to_block(bn_arr, ts_arr, exit_ts_sec)
            m = meta.get(pool, {"pool_type": "", "t0": "", "t1": "", "fee_bps": 30})
            q_before = rpc.rpc_round_trips
            label, net, lt = mod.label_one(rpc, pool, m, WETH, notional_wei, entry_block, exit_block)
            quoter_like_calls += rpc.rpc_round_trips - q_before
            reason_counts[lt] += 1
            if label < 0:
                quote_failed += 1
                continue
            w.writerow(
                [
                    ts,
                    pool,
                    sample["_bucket"],
                    sample.get("symbol", ""),
                    label,
                    round(net, 6),
                    lt,
                ]
            )
            labeled += 1

            if time.time() - t_wall0 > profile.max_wall_sec:
                print("BLOCKED_CAP_EXCEEDED wall_time_budget", file=sys.stderr)
                return 8

    wall_time_sec = time.time() - t_wall0
    attempted = labeled + quote_failed
    clean_rate = labeled / attempted if attempted else 0.0
    failure_rate = quote_failed / attempted if attempted else 0.0
    pairs_budget = attempted * 2

    labels_hist = {"label_0": 0, "label_1": 0}
    nets: list[float] = []
    per_pool: dict[str, Counter[str]] = {}
    if profile.output_csv.exists():
        with profile.output_csv.open(encoding="utf-8") as f:
            rd = csv.DictReader(f)
            for row in rd:
                pool_a = row["pool_address"].lower()
                per_pool.setdefault(pool_a, Counter())
                try:
                    lb = int(row["label"])
                    nr = float(row["net_return"])
                except Exception:
                    continue
                nets.append(nr)
                per_pool[pool_a][f"label_{lb}"] += 1
                if lb == 1:
                    labels_hist["label_1"] += 1
                else:
                    labels_hist["label_0"] += 1

    net_summary: dict[str, Any] = {}
    if nets:
        net_summary = {
            "mean_net_return_labeled_rows": round(statistics.mean(nets), 6),
            "median_net_return_labeled_rows": round(statistics.median(nets), 6),
        }

    per_pool_out = {k: dict(v) for k, v in sorted(per_pool.items())}

    hard_quota_attempt_band_ok = (
        attempted >= 100
        if profile.name in (
            "sprint035a_live_active_scoped_smoke",
            "sprint035c_external_live_active_smoke",
            "sprint035d_momentum_filtered_smoke",
            "sprint035e_ohlcv_anchored_smoke",
        )
        else (
            attempted >= 120
            if profile.name in (
                "sprint032a_hard_quota_smoke",
                "sprint032b_metadata_verified_smoke",
                "sprint032c_viability_verified_smoke",
            )
            else True
        )
    )

    if profile.name in (
        "smoke",
        "sprint031a_new_universe_smoke",
        "sprint032a_hard_quota_smoke",
        "sprint032b_metadata_verified_smoke",
        "sprint032c_viability_verified_smoke",
        "sprint035a_live_active_scoped_smoke",
        "sprint035c_external_live_active_smoke",
        "sprint035d_momentum_filtered_smoke",
    ):
        gates_pass = (
            attempted > 0
            and labeled >= profile.min_labeled_soft
            and attempted <= profile.max_attempted_pass
            and pairs_budget <= profile.max_conceptual_pairs
            and clean_rate >= 0.95
            and failure_rate <= 0.05
            and wall_time_sec <= profile.max_wall_sec
            and labeled <= profile.label_limit
            and hard_quota_attempt_band_ok
        )
        status = "FAIL"
        if attempted == 0:
            status = "FAIL"
        elif wall_time_sec > profile.max_wall_sec or pairs_budget > profile.max_conceptual_pairs or attempted > profile.max_attempted_pass:
            status = "BLOCKED_CAP_EXCEEDED"
        elif labeled < profile.min_labeled_soft:
            status = "TOO_SMALL_FOR_SMOKE"
        elif clean_rate < 0.95 or failure_rate > 0.05:
            fail_exc = sum(reason_counts[k] for k in reason_counts if "rpc" in k.lower() or "exception" in k.lower())
            status = "NEEDS_RPC_REPAIR" if fail_exc > quote_failed * 0.5 else "NEEDS_LABEL_SEMANTICS_REPAIR"
        elif gates_pass:
            status = "PASS"
        rpc_note = False
    else:
        core_ok = (
            attempted > 0
            and clean_rate >= 0.95
            and failure_rate <= 0.05
            and wall_time_sec <= profile.max_wall_sec
            and pairs_budget <= profile.max_conceptual_pairs
            and attempted <= profile.max_attempted_pass
            and labeled >= 50
            and 300 <= attempted <= 1000
        )
        rpc_high = rpc.rpc_round_trips > profile.max_rpc_round_trips
        if attempted < 300:
            status = "TOO_SMALL_FOR_DRYRUN"
        elif wall_time_sec > profile.max_wall_sec or pairs_budget > profile.max_conceptual_pairs or attempted > profile.max_attempted_pass:
            status = "BLOCKED_CAP_EXCEEDED"
        elif clean_rate < 0.95 or failure_rate > 0.05:
            fail_exc = sum(reason_counts[k] for k in reason_counts if "rpc" in k.lower() or "exception" in k.lower())
            status = "NEEDS_RPC_REPAIR" if fail_exc > quote_failed * 0.5 else "NEEDS_LABEL_SEMANTICS_REPAIR"
        elif core_ok and not rpc_high:
            status = "PASS"
        elif core_ok and rpc_high:
            status = "PASS_WITH_RPC_OPTIMIZATION_NEEDED"
        else:
            status = "FAIL"
        gates_pass = status in ("PASS", "PASS_WITH_RPC_OPTIMIZATION_NEEDED")
        rpc_note = rpc_high

    if profile.name == "sprint031a_new_universe_smoke" and rpc.rpc_round_trips > profile.max_rpc_round_trips:
        status = "BLOCKED_CAP_EXCEEDED"
        gates_pass = False
    elif profile.name == "sprint032a_hard_quota_smoke" and rpc.rpc_round_trips > profile.max_rpc_round_trips:
        status = "BLOCKED_CAP_EXCEEDED"
        gates_pass = False
    elif profile.name == "sprint032b_metadata_verified_smoke" and rpc.rpc_round_trips > profile.max_rpc_round_trips:
        status = "BLOCKED_CAP_EXCEEDED"
        gates_pass = False
    elif profile.name == "sprint032c_viability_verified_smoke" and rpc.rpc_round_trips > profile.max_rpc_round_trips:
        status = "BLOCKED_CAP_EXCEEDED"
        gates_pass = False
    elif profile.name == "sprint035a_live_active_scoped_smoke" and rpc.rpc_round_trips > profile.max_rpc_round_trips:
        status = "BLOCKED_CAP_EXCEEDED"
        gates_pass = False
    elif profile.name in (
        "sprint035c_external_live_active_smoke",
        "sprint035d_momentum_filtered_smoke",
        "sprint035e_ohlcv_anchored_smoke",
    ) and rpc.rpc_round_trips > profile.max_rpc_round_trips:
        status = "BLOCKED_CAP_EXCEEDED"
        gates_pass = False
    elif profile.name == "sprint031b_new_lineage_dryrun" and rpc.rpc_round_trips > profile.max_rpc_round_trips:
        status = "BLOCKED_CAP_EXCEEDED"
        gates_pass = False

    summary_obj: dict[str, Any] = {
        "kind": profile.summary_kind,
        "status": status,
        "manifest_path": str(profile.manifest_path.relative_to(REPO)),
        "profile": profile.name,
        "selected_pool_count": len({p for p, _, _ in rows}),
        "manifest_selected_rows": len(rows),
        "dashcam_rows_resolved_post_filters": len(samples),
        "rpc_pool_meta_infer_failed": infer_failed,
        "attempted_rows": attempted,
        "labeled_rows": labeled,
        "skipped_rows_pre_quote": skipped_pre_label,
        "quote_failed_count": quote_failed,
        "quote_success_count": labeled,
        "clean_rate": round(clean_rate, 4),
        "failure_rate": round(failure_rate, 4),
        "conceptual_quote_pairs": pairs_budget,
        "approx_eth_calls_during_label_loop_delta_sum": quoter_like_calls,
        "approx_eth_rpc_round_trips_total": rpc.rpc_round_trips,
        "wall_time_sec": round(wall_time_sec, 2),
        "label_distribution": labels_hist,
        "reason_counts": dict(reason_counts),
        "net_return_summary_if_available": net_summary,
        "per_pool_label_counts": per_pool_out,
        "caveats": [
            "not_alpha_not_economic_proof",
            "quote_failed_omitted_from_csv_not_encoded_as_label_0",
            "sequential_label_one_no_parallel_workers",
            "failed_no_7d_alpha_not_superseded",
            "pool_meta_onchain_inference_when_clickhouse_unreachable",
        ],
        "decision_flags": dict(profile.decision_flags),
        "acceptance_gates_pass": gates_pass,
        "rpc_round_trips_exceed_soft_cap": rpc_note if profile.name in ("dryrun_top10", "sprint031b_new_lineage_dryrun") else False,
        "recommended_next_step": (
            "Sprint030F scoped economic readout on labeled CSV (no training) + optional ts_grid cache before expansion"
            if profile.name == "dryrun_top10"
            else (
                "Sprint031B: scoped dry-run charter on this cohort if pipeline metrics PASS and economics are non-catastrophic "
                "(still no training/H12/live)."
                if profile.name == "sprint031a_new_universe_smoke"
                else (
                    "Sprint031C: GPT-5.5-led expansion/economic review design only — still no training/H12/live/promotion "
                    "until gates explicitly change."
                    if profile.name == "sprint031b_new_lineage_dryrun"
                    else (
                        "Sprint032C: optional GPT-5.5-led scope review on metadata-verified cohort economics "
                        "(still no training/H12/live/promotion)."
                        if profile.name == "sprint032b_metadata_verified_smoke"
                        else (
                                "Sprint032D: optional GPT-5.5 review on historical-viability cohort only "
                                "(still no training/H12/live/promotion)."
                                if profile.name == "sprint032c_viability_verified_smoke"
                                else (
                                    "Sprint035F: GPT-5.5-focused economics review of OHLCV-anchored cohort; "
                                    "still no training/H12/live/promotion."
                                    if profile.name == "sprint035e_ohlcv_anchored_smoke"
                                    else (
                                    "Sprint035E: GPT-5.5-focused economics review of momentum V2-filtered cohort; "
                                    "still no training/H12/live/promotion."
                                    if profile.name == "sprint035d_momentum_filtered_smoke"
                                    else (
                                    "Sprint035D: external cohort expansion or GPT-5.5-focused economics review (external manifest cohort only); "
                                    "still no training/H12/live/promotion."
                                    if profile.name == "sprint035c_external_live_active_smoke"
                                    else (
                                    "Sprint035B: scoped readout / tight cap review on live-active cohort economics only "
                                    "(still no training/H12/live/promotion)."
                                    if profile.name == "sprint035a_live_active_scoped_smoke"
                                    else (
                                        "Sprint032B: GPT-5.5 optional economic/ch cohort review only under FAILED_NO_7D_ALPHA guard "
                                        "(still no training/H12/live/promotion)."
                                        if profile.name == "sprint032a_hard_quota_smoke"
                                        else (
                                            "If PASS: scoped dry-run charter or LABEL_LIMIT raise under new caps; "
                                            "if NEEDS_RPC_REPAIR: endpoint/latency; if TOO_SMALL_FOR_SMOKE: manifest repair."
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
            )
        ),
    }
    if profile.name in ("smoke", "sprint031a_new_universe_smoke"):
        summary_obj["conceptual_entry_exit_pairs_budget"] = pairs_budget
        summary_obj["smoke_quote_pair_ceiling"] = 600
    elif profile.name in (
        "sprint032a_hard_quota_smoke",
        "sprint032b_metadata_verified_smoke",
        "sprint032c_viability_verified_smoke",
        "sprint035a_live_active_scoped_smoke",
        "sprint035c_external_live_active_smoke",
    ):
        summary_obj["conceptual_entry_exit_pairs_budget"] = pairs_budget
        summary_obj["smoke_quote_pair_ceiling"] = 700

    if profile.name == "sprint032b_metadata_verified_smoke":
        summary_obj["caveats"] = [
            "not_alpha_not_economic_proof",
            "quote_failed_omitted_from_csv_not_encoded_as_label_0",
            "sequential_label_one_no_parallel_workers",
            "failed_no_7d_alpha_not_superseded",
            "pool_meta_from_mvp_diagnostics_sprint032b_pool_metadata_audit_csv_PASS_rows_fee_raw_for_v3",
        ]
    elif profile.name == "sprint032c_viability_verified_smoke":
        summary_obj["caveats"] = [
            "not_alpha_not_economic_proof",
            "quote_failed_omitted_from_csv_not_encoded_as_label_0",
            "sequential_label_one_no_parallel_workers",
            "failed_no_7d_alpha_not_superseded",
            "historical_entry_viability_gating_prelabel_from_mvp_diagnostics_sprint032c_historical_viability_audit_csv",
        ]
    elif profile.name == "sprint035a_live_active_scoped_smoke":
        summary_obj["caveats"] = [
            "not_alpha_not_economic_proof",
            "quote_failed_omitted_from_csv_not_encoded_as_label_0",
            "sequential_label_one_no_parallel_workers",
            "failed_no_7d_alpha_not_superseded",
            "live_active_ch_24h_60d_gates_excluding_sprint034_dead_pool_scope_pool_meta_on_chain_infer",
        ]
    elif profile.name in ("sprint035c_external_live_active_smoke", "sprint035d_momentum_filtered_smoke"):
        summary_obj["caveats"] = [
            "not_alpha_not_economic_proof",
            "quote_failed_omitted_from_csv_not_encoded_as_label_0",
            "sequential_label_one_no_parallel_workers",
            "failed_no_7d_alpha_not_superseded",
            "external_discovery_is_candidate_only_factory_and_ch_timestamps_are_truth_for_manifest_rows",
            *(
                ["momentum_filter_applied_pricechange_bsr_txns1h_ohlcv_slope_score_ge_3"]
                if profile.name == "sprint035d_momentum_filtered_smoke"
                else []
            ),
        ]

    profile.summary_json.write_text(json.dumps(summary_obj, indent=2), encoding="utf-8")

    smoke_lines = _read_smoke_readout_lines()
    dry_md_extra = []
    if profile.name == "dryrun_top10":
        dry_md_extra = [
            "## Sprint030D smoke readout (storage)",
            "",
            *smoke_lines,
            "",
            "## Dry-run boundary",
            "",
            "- Top-10 strict cohort; ≤1000 rows; conceptual pairs ≤2300; RPC round trips soft cap 75000.",
            "- Sequential labeling only; no training / H12 / live / promotion.",
            "",
            "## Recommended Sprint030F",
            "",
            "- Scoped economic readout over `mvp/diagnostics/sprint030e_top10_scoped_dryrun_labels.csv` under `docs/ml-training-gate.md` posture (readout ≠ training).",
            "- If `PASS_WITH_RPC_OPTIMIZATION_NEEDED`: cache or narrow ts→block grid before scaling rows.",
            "",
        ]

    if profile.name not in (
        "sprint031a_new_universe_smoke",
        "sprint031b_new_lineage_dryrun",
        "sprint032a_hard_quota_smoke",
        "sprint032b_metadata_verified_smoke",
        "sprint032c_viability_verified_smoke",
    ):
        profile.report_md.parent.mkdir(parents=True, exist_ok=True)
        profile.report_md.write_text(
            "\n".join(
                [
                    f"# {profile.report_title}",
                    "",
                    "**Generated:** bounded label execution — **not training**, **not H12**, **not Phase6**, **not live**, **not alpha proof**, **`FAILED_NO_7D_ALPHA` not superseded**.",
                    "",
                    *dry_md_extra,
                    "## Execution boundary",
                    "",
                    "- Sequential `label_one` calls only (no ThreadPoolExecutor in wrapper).",
                    f"- Manifest frozen before quotes (`{profile.manifest_path.relative_to(REPO)}`).",
                    "- `quote_failed` (`label<0`) omitted from CSV — **not** `label=0`.",
                    "- Rows with executable negative economics may appear as `label=0` with `label_type` such as `executable_exit_drained`.",
                    "",
                    "## Manifest freeze rule",
                    "",
                    f"- {profile.pool_selection_rule}; deterministic bucket ordering; no post-quote cherry-pick.",
                    "",
                    "## Label semantics",
                    "",
                    "- `LABEL_EXIT_HORIZON_SEC=1800`, `LABEL_BUCKET_SEC=300`, `ROUND_TRIP_COST=0.006`, `LABEL_NOTIONAL_WEI=0.0005 ETH`, pool types `v2,v3`.",
                    "",
                    "## Key metrics",
                    "",
                    f"- Status: **{status}**",
                    f"- Attempted label loops: **{attempted}**",
                    f"- Labeled rows written: **{labeled}**",
                    f"- Quote failed (skipped): **{quote_failed}**",
                    f"- Clean rate: **{clean_rate:.4f}**",
                    f"- Failure rate: **{failure_rate:.4f}**",
                    f"- Conceptual quote pairs (2×attempted): **{pairs_budget}**",
                    f"- Wall time (s): **{wall_time_sec:.1f}**",
                    f"- Approx JSON-RPC round trips (incl. batched block reads for ts grid): **{rpc.rpc_round_trips}**",
                    "",
                    "## Label distribution",
                    "",
                    f"- **{labels_hist}**",
                    "",
                    "## Per-pool label counts",
                    "",
                    f"- See summary JSON `per_pool_label_counts`; pools selected: **{summary_obj['selected_pool_count']}**.",
                    "",
                    "## What this does NOT prove",
                    "",
                    "- No strategy alpha, no live readiness, no model promotion.",
                    "",
                    "## Decision flags",
                    "",
                    "```",
                    *[
                        f"{k}={'true' if v is True else 'false' if v is False else v}"
                        for k, v in profile.decision_flags.items()
                    ],
                    "```",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    print("status:", status)
    print("labeled:", labeled, "quote_failed:", quote_failed, "clean_rate:", round(clean_rate, 4))
    print("wall_sec:", round(wall_time_sec, 1), "rpc_round_trips:", rpc.rpc_round_trips)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest-only", action="store_true")
    ap.add_argument("--dashcam", type=Path, default=DASHCAM_DEFAULT)
    ap.add_argument(
        "--profile",
        choices=(
            "smoke",
            "dryrun_top10",
            "sprint031a_new_universe_smoke",
            "sprint031b_new_lineage_dryrun",
            "sprint032a_hard_quota_smoke",
            "sprint032b_metadata_verified_smoke",
            "sprint032c_viability_verified_smoke",
            "sprint035a_live_active_scoped_smoke",
            "sprint035c_external_live_active_smoke",
            "sprint035d_momentum_filtered_smoke",
            "sprint035e_ohlcv_anchored_smoke",
        ),
        default="smoke",
    )
    args = ap.parse_args()

    if args.profile == "smoke":
        profile = PROFILE_SMOKE
    elif args.profile == "dryrun_top10":
        profile = PROFILE_DRYRUN_TOP10
    elif args.profile == "sprint031a_new_universe_smoke":
        profile = PROFILE_SPRINT031A_NEW_UNIVERSE
    elif args.profile == "sprint032a_hard_quota_smoke":
        profile = PROFILE_SPRINT032A_HARD_QUOTA_SMOKE
    elif args.profile == "sprint032b_metadata_verified_smoke":
        profile = PROFILE_SPRINT032B_METADATA_VERIFIED_SMOKE
    elif args.profile == "sprint032c_viability_verified_smoke":
        profile = PROFILE_SPRINT032C_VIABILITY_VERIFIED_SMOKE
    elif args.profile == "sprint035a_live_active_scoped_smoke":
        profile = PROFILE_SPRINT035A_LIVE_ACTIVE_SCOPED_SMOKE
    elif args.profile == "sprint035c_external_live_active_smoke":
        profile = PROFILE_SPRINT035C_EXTERNAL_LIVE_ACTIVE_SMOKE
    elif args.profile == "sprint035d_momentum_filtered_smoke":
        profile = PROFILE_SPRINT035D_MOMENTUM_FILTERED_SMOKE
    elif args.profile == "sprint035e_ohlcv_anchored_smoke":
        profile = PROFILE_SPRINT035E_OHLCV_ANCHORED_SMOKE
    else:
        profile = PROFILE_SPRINT031B_NEW_LINEAGE_DRYRUN
    return run_profile(profile, args.manifest_only, args.dashcam)


if __name__ == "__main__":
    raise SystemExit(main())
