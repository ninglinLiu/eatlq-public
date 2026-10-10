#!/usr/bin/env python3
"""Generate the reproducible B2 attribution audit JSON from research tables."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ch_helper import ch_query

CHAIN_ID = 8453
LOCKED_START = 46389458
LOCKED_END = 46417105
LOCKED_PROTOCOLS = "('uniswap_v3','aero_cl')"
CREDIBLE = "('transfer_net','transfer_subgraph','tx_from_confirmed')"


def one(sql: str) -> dict[str, Any]:
    rows = ch_query(sql, timeout=900)
    if len(rows) != 1:
        raise RuntimeError(f"expected one row, got {len(rows)}")
    return rows[0]


def integerize(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: int(value) if isinstance(value, str) and value.lstrip("-").isdigit() else value
        for key, value in row.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    scope = (
        f"chain_id={CHAIN_ID} AND block_number BETWEEN {LOCKED_START} AND {LOCKED_END} "
        f"AND protocol IN {LOCKED_PROTOCOLS}"
    )
    totals = integerize(one(f"""
        SELECT count() AS denominator,
               countIf(resolved_account!='') AS any_count,
               countIf(attribution_source IN {CREDIBLE}) AS beneficial_count,
               countIf(attribution_source IN {CREDIBLE} AND account_code_class='eoa') AS verified_eoa_count,
               countIf(attribution_source IN {CREDIBLE} AND account_code_class='eip7702') AS eip7702_count,
               countIf(attribution_source IN {CREDIBLE} AND account_code_class='contract_wallet') AS contract_wallet_count
        FROM eatlq.base_cha_wallet_swaps FINAL WHERE {scope}
    """))
    denominator = totals["denominator"]
    coverage = {
        **totals,
        "coverage_any_pct": round(100 * totals["any_count"] / denominator, 6),
        "coverage_beneficial_pct": round(100 * totals["beneficial_count"] / denominator, 6),
        "coverage_verified_eoa_pct": round(100 * totals["verified_eoa_count"] / denominator, 6),
        "coverage_eip7702_pct": round(100 * totals["eip7702_count"] / denominator, 6),
        "coverage_contract_wallet_pct": round(100 * totals["contract_wallet_count"] / denominator, 6),
    }

    residual_rows = ch_query(f"""
        SELECT residual_bucket,count() AS count
        FROM eatlq.base_cha_wallet_swaps FINAL WHERE {scope}
        GROUP BY residual_bucket ORDER BY residual_bucket
    """)
    residual = []
    for row in residual_rows:
        count = int(row["count"])
        bucket_sql = row["residual_bucket"].replace("'", "\\'")
        examples = ch_query(f"""
            SELECT tx_hash,block_number,protocol,attribution_source
            FROM eatlq.base_cha_wallet_swaps FINAL
            WHERE {scope} AND residual_bucket='{bucket_sql}'
            ORDER BY block_number,tx_hash,log_index LIMIT 2
        """)
        residual.append({
            "bucket": row["residual_bucket"],
            "count": count,
            "pct": round(100 * count / denominator, 6),
            "examples": [integerize(example) for example in examples],
        })
    if not any(row["bucket"] == "no_transfer" for row in residual):
        residual.append({"bucket": "no_transfer", "count": 0, "pct": 0.0, "examples": []})
        residual.sort(key=lambda row: row["bucket"])

    exclusions = "SELECT address FROM eatlq.base_wallet_attribution_exclusions FINAL WHERE chain_id=8453"
    old_new = integerize(one(f"""
        SELECT
          uniqExact(swap_sender) AS old_distinct_identities,
          uniqExactIf(swap_sender, swap_sender NOT IN ({exclusions})) AS old_noninfra_identities,
          countIf(swap_sender NOT IN ({exclusions})) AS old_attributed_swaps,
          countIf(swap_sender IN ({exclusions})) AS router_collapsed_swaps,
          countIf(swap_sender NOT IN ({exclusions}) AND side='buy') AS old_buy_rows,
          countIf(swap_sender NOT IN ({exclusions}) AND side='sell') AS old_sell_rows,
          uniqExactIf(resolved_account, attribution_source IN {CREDIBLE}) AS new_beneficial_identities,
          countIf(attribution_source IN {CREDIBLE}) AS new_attributed_swaps,
          countIf(attribution_source IN {CREDIBLE} AND side='buy') AS new_buy_rows,
          countIf(attribution_source IN {CREDIBLE} AND side='sell') AS new_sell_rows
        FROM eatlq.base_cha_wallet_swaps FINAL WHERE {scope}
    """))

    overlap = integerize(one(f"""
        SELECT
          countIf(tx_from!='' AND tx_from NOT IN ({exclusions}) AND attribution_source IN {CREDIBLE}
                  AND account_code_class='eoa') AS eligible,
          countIf(tx_from!='' AND tx_from NOT IN ({exclusions}) AND attribution_source IN {CREDIBLE}
                  AND account_code_class='eoa'
                  AND resolved_account=tx_from) AS matches,
          countIf(tx_from!='' AND tx_from NOT IN ({exclusions}) AND attribution_source IN {CREDIBLE}
                  AND account_code_class='eoa'
                  AND resolved_account!=tx_from) AS mismatches
        FROM eatlq.base_cha_wallet_swaps FINAL WHERE {scope}
    """))
    overlap["mismatch_pct"] = round(100 * overlap["mismatches"] / overlap["eligible"], 6) if overlap["eligible"] else None

    smart_reference = []
    for code_class in ("eip7702", "contract_wallet"):
        row = integerize(one(f"""
            SELECT '{code_class}' AS account_code_class,
              countIf(s.tx_from!='' AND s.tx_from NOT IN ({exclusions}) AND s.attribution_source IN {CREDIBLE}
                      AND s.account_code_class='{code_class}') AS eligible,
              countIf(s.tx_from!='' AND s.tx_from NOT IN ({exclusions}) AND s.attribution_source IN {CREDIBLE}
                      AND s.account_code_class='{code_class}' AND s.resolved_account=s.tx_from) AS matches,
              countIf(s.tx_from!='' AND s.tx_from NOT IN ({exclusions}) AND s.attribution_source IN {CREDIBLE}
                      AND s.account_code_class='{code_class}' AND s.resolved_account!=s.tx_from) AS mismatches
            FROM (SELECT * FROM eatlq.base_cha_wallet_swaps FINAL) AS s
            WHERE {scope.replace('protocol', 's.protocol').replace('chain_id', 's.chain_id').replace('block_number', 's.block_number')}
        """))
        row["mismatch_pct"] = round(100 * row["mismatches"] / row["eligible"], 6) if row["eligible"] else None
        smart_reference.append(row)

    mismatch_rows = ch_query(f"""
        WITH tx_counts AS
        (
          SELECT tx_hash,count() AS swap_rows
          FROM eatlq.base_cha_wallet_swaps FINAL
          WHERE {scope}
          GROUP BY tx_hash
        )
        SELECT s.tx_hash,s.block_number,s.log_index,s.protocol,s.side,s.target_token_address,
               s.resolved_account,s.account_code_class,s.tx_from,s.attribution_source,
               s.net_delta_raw,t.swap_rows,
               multiIf(t.swap_rows>1,'multi_swap_or_multihop',
                       s.account_code_class='eip7702','eip7702_reference',
                       s.account_code_class='contract_wallet','contract_wallet_reference',
                       'third_party_eoa_recipient') AS diagnostic_bucket
        FROM (SELECT * FROM eatlq.base_cha_wallet_swaps FINAL) AS s
        INNER JOIN tx_counts AS t ON s.tx_hash=t.tx_hash
        WHERE {scope.replace('protocol', 's.protocol').replace('chain_id', 's.chain_id').replace('block_number', 's.block_number')}
          AND s.tx_from!='' AND s.tx_from NOT IN ({exclusions})
          AND s.attribution_source IN {CREDIBLE} AND s.resolved_account!=s.tx_from
        ORDER BY s.block_number,s.tx_hash,s.log_index
    """, timeout=900)
    mismatch_rows = [integerize(row) for row in mismatch_rows]

    mismatch_buckets: dict[str, int] = {}
    for row in mismatch_rows:
        bucket = row["diagnostic_bucket"]
        mismatch_buckets[bucket] = mismatch_buckets.get(bucket, 0) + 1

    infra_leak = int(one(f"""
        SELECT count() AS count FROM
        (SELECT * FROM eatlq.base_cha_wallet_swaps FINAL) AS s
        INNER JOIN (SELECT * FROM eatlq.base_wallet_attribution_exclusions FINAL) AS e
          ON s.chain_id=e.chain_id AND lower(s.resolved_account)=lower(e.address)
        WHERE s.resolved_account!='' AND {scope.replace('protocol', 's.protocol').replace('chain_id', 's.chain_id').replace('block_number', 's.block_number')}
    """)["count"])

    ambiguous_pct = next(
        (row["pct"] for row in residual if row["bucket"] == "ambiguous"), 0.0
    )
    verdict = "PASS"
    if coverage["coverage_beneficial_pct"] < 90.0:
        verdict = "FAIL"
    elif overlap["mismatch_pct"] is None or overlap["mismatch_pct"] > 2.0:
        verdict = "FAIL"
    if infra_leak:
        verdict = "NEEDS_REVIEW" if verdict == "PASS" else verdict

    result = {
        "research_only": True,
        "no_go": True,
        "window": {"start_block": LOCKED_START, "end_block": LOCKED_END},
        "criterion_scope": ["uniswap_v3", "aero_cl"],
        "coverage": coverage,
        "old_vs_new": old_new,
        "residual_partition": residual,
        "residual_count_sum": sum(row["count"] for row in residual),
        "tx_from_overlap": overlap,
        "tx_from_smart_account_reference": smart_reference,
        "tx_from_mismatch_buckets": mismatch_buckets,
        "tx_from_mismatches": mismatch_rows,
        "infrastructure_resolved_count": infra_leak,
        "pre_registered_verdict": verdict,
        "account_code_observation": {
            "block": LOCKED_END,
            "mode": "end_block_single_point",
            "hard_gate_scope": "EOA subset only",
            "bias": "EIP-7702 delegation may change within the window; end-block code can misclassify earlier swap-time state.",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "coverage", "old_vs_new", "tx_from_overlap", "tx_from_mismatch_buckets",
        "infrastructure_resolved_count", "pre_registered_verdict"
    )}, indent=2))


if __name__ == "__main__":
    main()
