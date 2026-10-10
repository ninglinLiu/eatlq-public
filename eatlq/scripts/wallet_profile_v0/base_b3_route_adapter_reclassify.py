#!/usr/bin/env python3
"""Reclassify a frozen B3 snapshot after verified route-adapter additions."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from base_b3_entity_attribution import route_family


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = json.loads(args.input.read_text(encoding="utf-8"))
    rows = result["rows"]
    pools = {row["venue_address"] for row in rows}
    for row in rows:
        family = route_family(row["tx_to"], pools, bool(row["userop_count"]))
        row["route_family"] = family
        if (
            row["resolution_confidence"] == "ENTITY_MEDIUM_CONFIDENCE"
            and family != "unknown_contract_or_route"
            and row["tx_signer_code_class_at_swap_block"] in ("eoa", "eip7702")
        ):
            row["resolution_confidence"] = "ACCOUNT_HIGH_CONFIDENCE"
            row["resolution_method"] = "direct_transaction_signer_verified_route_adapter"
    counts = Counter(row["resolution_confidence"] for row in rows)
    routes = Counter(row["route_family"] for row in rows)
    methods = Counter(row["resolution_method"] for row in rows)
    high = counts["ACCOUNT_HIGH_CONFIDENCE"]
    result["resolution_counts"] = dict(sorted(counts.items()))
    result["route_family_counts"] = dict(sorted(routes.items()))
    result["resolution_method_counts"] = dict(sorted(methods.items()))
    result["high_confidence_coverage_pct"] = round(100 * high / len(rows), 6)
    result["coverage_gate_65"] = high / len(rows) >= 0.65
    result["verified_adapter_additions"] = {
        "0xc8f6b8ba0dc0f175b568b99440b0867f69a29265": "Blockscout verified DexRouter",
        "0x0000000000001ff3684f28c67538d4d072c22734": "Blockscout verified AllowanceHolder",
        "0xfdf682f51fe81aa4898f0ae2163d8a55c127fbc7": "Blockscout verified UniversalRouter",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "resolution_counts": result["resolution_counts"],
        "route_family_counts": result["route_family_counts"],
        "high_confidence_coverage_pct": result["high_confidence_coverage_pct"],
    }, indent=2))


if __name__ == "__main__":
    main()
