#!/usr/bin/env python3
"""Pre-rollback holder-semantics audit for B2-v3 (research-only)."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from base_attribution import EXCLUDED_ADDRESSES
from ch_helper import ch_query

CHAIN_ID = 8453
START = 46389458
END = 46417105
DENOMINATOR = 57426
CREDIBLE = {"transfer_net", "transfer_subgraph", "tx_from_confirmed"}
FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/base_transfer_net_b2_golden.json"


def load_rows() -> list[dict[str, Any]]:
    return ch_query(f"""
        SELECT tx_hash,block_number,target_token_address,side,resolved_account,
               tx_from,account_code_class,attribution_source
        FROM eatlq.base_cha_wallet_swaps FINAL
        WHERE chain_id={CHAIN_ID} AND block_number BETWEEN {START} AND {END}
          AND protocol IN ('uniswap_v3','aero_cl')
    """, timeout=900)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exclusion-output", type=Path, required=True)
    args = parser.parse_args()

    rows = load_rows()
    beneficial = [
        row for row in rows
        if row["attribution_source"] in CREDIBLE and row["resolved_account"]
    ]
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in beneficial:
        by_account[row["resolved_account"]].append(row)
    overall_rank = {
        account: rank
        for rank, (account, _) in enumerate(
            sorted(by_account.items(), key=lambda item: (-len(item[1]), item[0])), 1
        )
    }
    top_n = math.ceil(len(by_account) * 0.001)
    profiles = []
    for account, account_rows in by_account.items():
        code_class = Counter(row["account_code_class"] for row in account_rows).most_common(1)[0][0]
        if code_class not in ("contract_wallet", "eip7702"):
            continue
        profile = {
            "address": account,
            "account_code_class": code_class,
            "swaps_count": len(account_rows),
            "distinct_tx_from_count": len({row["tx_from"] for row in account_rows if row["tx_from"]}),
            "distinct_target_token_count": len({row["target_token_address"] for row in account_rows}),
            "overall_volume_rank": overall_rank[account],
        }
        profile["flag_signer_count"] = profile["distinct_tx_from_count"] >= 20
        profile["flag_top_0_1pct"] = profile["overall_volume_rank"] <= top_n
        profile["suspected_shared_infra"] = profile["flag_signer_count"] or profile["flag_top_0_1pct"]
        profile["beneficial_share_pct"] = round(100 * profile["swaps_count"] / len(beneficial), 6)
        profiles.append(profile)
    flagged = sorted(
        (profile for profile in profiles if profile["suspected_shared_infra"]),
        key=lambda profile: (-profile["swaps_count"], profile["address"]),
    )
    flagged_addresses = {profile["address"] for profile in flagged}
    adjusted = [row for row in beneficial if row["resolved_account"] not in flagged_addresses]
    adjusted_counts = Counter(row["resolved_account"] for row in adjusted)
    max_account, max_count = adjusted_counts.most_common(1)[0] if adjusted_counts else ("", 0)
    gate_a = {
        "status": "PASS" if 100 * len(adjusted) / DENOMINATOR >= 90 and 100 * max_count / len(adjusted) < 1 else "FAIL",
        "pre_registered_rule": "distinct_tx_from_count >= 20 OR overall resolved-account volume rank in top 0.1%",
        "resolved_account_count": len(by_account),
        "top_0_1pct_count": top_n,
        "suspected_shared_infra_count": len(flagged),
        "suspected_shared_infra": flagged,
        "beneficial_before": len(beneficial),
        "beneficial_coverage_before_pct": round(100 * len(beneficial) / DENOMINATOR, 6),
        "removed_swaps": len(beneficial) - len(adjusted),
        "beneficial_after": len(adjusted),
        "beneficial_coverage_after_pct": round(100 * len(adjusted) / DENOMINATOR, 6),
        "largest_remaining_account": max_account,
        "largest_remaining_account_swaps": max_count,
        "largest_remaining_account_share_pct": round(100 * max_count / len(adjusted), 6) if adjusted else None,
        "coverage_gate_90": 100 * len(adjusted) / DENOMINATOR >= 90,
        "single_account_lt_1pct_gate": bool(adjusted and 100 * max_count / len(adjusted) < 1),
    }

    tx_counts = Counter(row["tx_hash"] for row in rows)
    mismatch_rows = [
        row for row in beneficial
        if row["account_code_class"] == "eoa"
        and row["tx_from"] and row["tx_from"] not in EXCLUDED_ADDRESSES
        and row["resolved_account"] != row["tx_from"]
        and tx_counts[row["tx_hash"]] == 1
    ]
    signed = {row["tx_from"] for row in rows if row["tx_from"]}
    sells: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in beneficial:
        if row["side"] == "sell":
            sells[(row["resolved_account"], row["target_token_address"])].append(int(row["block_number"]))
    mismatch_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in mismatch_rows:
        mismatch_by_account[row["resolved_account"]].append(row)
    bucket_rows: Counter[str] = Counter()
    bucket_accounts: Counter[str] = Counter()
    account_characterization = []
    for account, account_rows in mismatch_by_account.items():
        self_signed = account in signed
        later_sell = any(
            any(block > int(row["block_number"]) for block in sells[(account, row["target_token_address"])])
            for row in account_rows
        )
        feeder_count = len({row["tx_from"] for row in account_rows})
        if self_signed or later_sell:
            bucket = "self_second_wallet"
        elif feeder_count >= 2:
            bucket = "deposit_sink"
        elif len(account_rows) == 1:
            bucket = "one_off"
        else:
            bucket = "adverse_unclassified"
        bucket_rows[bucket] += len(account_rows)
        bucket_accounts[bucket] += 1
        account_characterization.append({
            "address": account,
            "bucket": bucket,
            "mismatch_rows": len(account_rows),
            "distinct_feeder_tx_from_count": feeder_count,
            "self_signed_in_window": self_signed,
            "later_sold_same_token": later_sell,
        })
    bucket_summary = [
        {
            "bucket": bucket,
            "rows": bucket_rows[bucket],
            "row_pct": round(100 * bucket_rows[bucket] / len(mismatch_rows), 6),
            "accounts": bucket_accounts[bucket],
        }
        for bucket in ("self_second_wallet", "deposit_sink", "one_off", "adverse_unclassified")
    ]
    gate_b = {
        "status": "DESCRIPTIVE_ONLY",
        "population_rows": len(mismatch_rows),
        "population_accounts": len(mismatch_by_account),
        "buckets": bucket_summary,
        "accounts": sorted(account_characterization, key=lambda row: (row["bucket"], row["address"])),
        "note": "Nine repeated same-feeder accounts fit none of the three requested definitions and remain adverse_unclassified; no forced relabeling.",
    }

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    subgraph_cases = fixture["transfer_subgraph_golden_audit"]
    matched = sum(bool(case["matches_materialized"]) for case in subgraph_cases)
    gate_c = {
        "status": "FAIL_ROLLBACK_REQUIRED" if matched != len(subgraph_cases) else "PASS",
        "sample_method": "five block-ordered quantile picks among multi-swap transaction subgraph resolutions, excluding Gate-A suspects and requiring one materialized account-token tx",
        "sample_count": len(subgraph_cases),
        "matched_count": matched,
        "mismatched_count": len(subgraph_cases) - matched,
        "cases": subgraph_cases,
        "rollback_all_transfer_subgraph": matched != len(subgraph_cases),
    }

    result = {
        "schema_version": 1,
        "research_only": True,
        "no_go": True,
        "window": {"start_block": START, "end_block": END, "denominator": DENOMINATOR},
        "contract_wallet_concentration": gate_a,
        "third_party_eoa_recipient_characterization": gate_b,
        "transfer_subgraph_golden": gate_c,
        "all_three_gates_pass": gate_a["status"] == "PASS" and gate_c["status"] == "PASS",
        "acceptance_redirection_effective": False,
        "verdict": "NEEDS_REVIEW_HOLDER_COLLAPSE_AND_SUBGRAPH_GOLDEN_FAIL",
        "rh1_merit": "UNTESTED",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    exclusion = {
        "schema_version": 1,
        "chain_id": CHAIN_ID,
        "status": "suspected_shared_infra_research_exclusion",
        "pre_registered_rule": gate_a["pre_registered_rule"],
        "addresses": [
            {"address": row["address"], "category": "suspected_shared_infra", "evidence": row}
            for row in flagged
        ],
    }
    args.exclusion_output.parent.mkdir(parents=True, exist_ok=True)
    args.exclusion_output.write_text(json.dumps(exclusion, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "gate_a": {key: gate_a[key] for key in (
            "status", "suspected_shared_infra_count", "beneficial_coverage_before_pct",
            "beneficial_coverage_after_pct", "largest_remaining_account_share_pct"
        )},
        "gate_b": bucket_summary,
        "gate_c": {key: gate_c[key] for key in ("status", "matched_count", "mismatched_count")},
        "verdict": result["verdict"],
    }, indent=2))


if __name__ == "__main__":
    main()
