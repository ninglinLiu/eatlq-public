#!/usr/bin/env python3
"""Assemble the bounded Base B3 account/entity feasibility verdict.

This script only combines frozen research artifacts.  It does not call RPCs,
train a model, or authorize live/canary use.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


HIGH = "ACCOUNT_HIGH_CONFIDENCE"
USEROP_METHOD = "erc4337_userop_sender_log_boundary"
DIRECT_CODE_CLASSES = {"eoa", "eip7702"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def known_shared_error_cases(
    trace: dict[str, Any],
    sample: dict[str, Any],
    suspect_config: dict[str, Any],
) -> list[dict[str, str]]:
    """Return golden rows that resolve to known/shared infrastructure.

    This is deliberately a bounded *known shared infrastructure* check.  A
    zero result is not evidence that an undiscovered shared contract cannot
    exist outside the 100-row golden.
    """

    sample_by_tx = {row["tx_hash"]: row for row in sample["rows"]}
    suspects = {
        row["address"].lower()
        for row in suspect_config.get("addresses", [])
        if row.get("address")
    }
    failures: list[dict[str, str]] = []
    for golden in trace["rows"]:
        row = sample_by_tx[golden["tx_hash"]]
        resolved = row["resolved_account_or_entity"].lower()
        method = row["resolution_method"]
        reason = ""
        if resolved in suspects:
            reason = "resolved address is in the pre-registered B2 shared-infrastructure exclusion set"
        elif method == USEROP_METHOD:
            if golden.get("matching_account_subtree_count") != 1:
                reason = "target pool call is not isolated to exactly one resolved UserOp sender subtree"
        elif row.get("tx_signer_code_class_at_swap_block") not in DIRECT_CODE_CLASSES:
            reason = "direct resolved signer is neither historical EOA nor historical EIP-7702"
        elif resolved == row.get("tx_to", "").lower():
            reason = "resolved direct signer collapses to called router/infrastructure"
        if reason:
            failures.append({"tx_hash": row["tx_hash"], "resolved": resolved, "reason": reason})
    return failures


def assemble(
    sample: dict[str, Any],
    trace: dict[str, Any],
    inventory: dict[str, Any],
    latency: dict[str, Any],
    suspect_config: dict[str, Any],
) -> dict[str, Any]:
    high_rows = [row for row in sample["rows"] if row["resolution_confidence"] == HIGH]
    shared_failures = known_shared_error_cases(trace, sample, suspect_config)
    golden_n = trace["sample_count"]
    shared_error_pct = 100.0 * len(shared_failures) / golden_n
    account_counts = Counter(row["resolved_account_or_entity"].lower() for row in high_rows)
    top_account, top_count = account_counts.most_common(1)[0]
    gates = {
        "precision": {
            "value_pct": trace["precision_pct"],
            "threshold": ">=95%",
            "passed": bool(trace["precision_gate_95"]),
            "denominator": golden_n,
        },
        "high_confidence_coverage": {
            "value_pct": sample["high_confidence_coverage_pct"],
            "threshold": ">=65%",
            "passed": bool(sample["coverage_gate_65"]),
            "denominator": sample["sample"]["size"],
        },
        "account_inventory_pnl_reconstructability": {
            "value_pct": inventory["reconstructable_account_pct"],
            "threshold": ">=80%",
            "passed": bool(inventory["pnl_reconstructable_gate_80"]),
            "denominator": inventory["unique_accounts"],
        },
        "known_shared_infrastructure_error": {
            "value_pct": round(shared_error_pct, 6),
            "threshold": "<1%",
            "passed": shared_error_pct < 1.0,
            "denominator": golden_n,
            "scope": "100-row trace golden against pre-registered known shared infrastructure and execution-boundary checks",
        },
        "receipt_userop_parse_latency": {
            "value_p95_seconds": latency["p95_seconds"],
            "threshold": "<=2s",
            "passed": bool(latency["latency_gate"]),
            "denominator": latency["sample_count"],
        },
    }
    local_gates_pass = all(gate["passed"] for gate in gates.values())
    return {
        "schema_version": 1,
        "sop_applied": True,
        "alpha_id": "AR-B3-ACCOUNT-ENTITY-ATTRIBUTION",
        "family": "F3/F7",
        "next_gate": "EXTERNAL_VENDOR_CROSSCHECK_THEN_RESEARCH_ONLY_PNL_HISTORY",
        "decision_flags": {"training_allowed": False, "live_allowed": False},
        "research_only": True,
        "no_go": True,
        "objective": "resolve a persistent trading account/entity, not a natural person or original EOA",
        "target_universe": sample["target_universe"],
        "window": sample["window"],
        "sample": sample["sample"],
        "resolution_counts": sample["resolution_counts"],
        "route_family_counts": sample["route_family_counts"],
        "high_confidence_unique_accounts": len(account_counts),
        "direct_account_types": {
            "eoa_rows": sum(
                row["tx_signer_code_class_at_swap_block"] == "eoa"
                for row in high_rows if row["resolution_method"] != USEROP_METHOD
            ),
            "eip7702_rows": sum(
                row["tx_signer_code_class_at_swap_block"] == "eip7702"
                for row in high_rows if row["resolution_method"] != USEROP_METHOD
            ),
            "erc4337_userop_sender_rows": sum(row["resolution_method"] == USEROP_METHOD for row in high_rows),
        },
        "concentration_reference": {
            "top_account": top_account,
            "top_account_rows": top_count,
            "top_account_share_of_high_confidence_pct": round(100.0 * top_count / len(high_rows), 6),
            "top_account_historical_type": "eoa",
            "note": "reference only; this B3 gate concerns shared-infrastructure errors, not active-trader concentration",
        },
        "gates": gates,
        "known_shared_infrastructure_error_cases": shared_failures,
        "local_numeric_gates_pass": local_gates_pass,
        "external_cross_validation": sample["external_cross_validation"],
        "external_crosscheck_complete": False,
        "verdict": (
            "LOCAL_FEASIBILITY_PASS_EXTERNAL_CROSSCHECK_PENDING"
            if local_gates_pass else "LOCAL_FEASIBILITY_FAIL"
        ),
        "interpretation": (
            "B1/B2 universal attribution failed, but bounded protocol/account-type account-entity attribution is locally feasible. "
            "This is not a strategy-edge result and does not identify natural persons."
        ),
        "limitations": [
            "The zero known-shared error rate is a 100-row golden estimate, not a proof over all Base contracts.",
            "Inventory/PnL feasibility covers sampled target-universe transactions, not complete lifetime history.",
            "Nansen and GMGN were not used as golden truth; Nansen cross-validation remains not run without an authorized key.",
            "No Base smart-money edge test was run; RH1 merit remains UNTESTED.",
        ],
        "rh1_merit": "UNTESTED",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--latency", type=Path, required=True)
    parser.add_argument("--suspects", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = assemble(
        load_json(args.sample),
        load_json(args.trace),
        load_json(args.inventory),
        load_json(args.latency),
        load_json(args.suspects),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"], "gates": result["gates"]}, indent=2))


if __name__ == "__main__":
    main()
