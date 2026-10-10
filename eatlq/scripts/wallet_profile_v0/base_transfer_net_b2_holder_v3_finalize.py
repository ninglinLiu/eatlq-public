#!/usr/bin/env python3
"""Merge B2-v3 holder pre-rollback gates with the adverse rollback audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pre-rollback", type=Path, required=True)
    parser.add_argument("--post-rollback", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pre = json.loads(args.pre_rollback.read_text(encoding="utf-8"))
    post = json.loads(args.post_rollback.read_text(encoding="utf-8"))
    result = {
        **post,
        "holder_v3": True,
        "contract_wallet_concentration": pre["contract_wallet_concentration"],
        "third_party_eoa_recipient_characterization": pre["third_party_eoa_recipient_characterization"],
        "transfer_subgraph_golden": pre["transfer_subgraph_golden"],
        "post_rollback": {
            "transfer_subgraph_credible_rows": 0,
            "coverage": post["coverage"],
            "residual_partition": post["residual_partition"],
            "reason": "4/5 uniformly sampled subgraph cases failed independent full-window balanceOf equality",
        },
        "all_three_gates_pass": False,
        "acceptance_redirection_effective": False,
        "holder_v3_verdict": "NEEDS_REVIEW_HOLDER_COLLAPSE_AND_SUBGRAPH_GOLDEN_FAIL",
        "rh1_can_use_b2": False,
        "rh1_merit": "UNTESTED",
        "no_go": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "holder_v3_verdict": result["holder_v3_verdict"],
        "gate_a": result["contract_wallet_concentration"]["status"],
        "gate_b": result["third_party_eoa_recipient_characterization"]["status"],
        "gate_c": result["transfer_subgraph_golden"]["status"],
        "post_rollback_coverage_pct": result["coverage"]["coverage_beneficial_pct"],
    }, indent=2))


if __name__ == "__main__":
    main()
