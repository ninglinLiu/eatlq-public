#!/usr/bin/env python3
"""Account-level inventory/PnL reconstructability golden for B3 trace cases."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from base_b3_entity_attribution import load_rpc_url, throttled_batch
from base_transfer_net_attribution import RpcClient

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def transfer_net_for(logs: list[dict[str, Any]], token: str, account: str) -> int:
    net = 0
    for log in logs:
        topics = [str(topic).lower() for topic in (log.get("topics") or [])]
        data = str(log.get("data") or "")
        if (
            str(log.get("address") or "").lower() != token
            or len(topics) != 3
            or topics[0] != TRANSFER_TOPIC
            or len(data) != 66
        ):
            continue
        sender = "0x" + topics[1][-40:]
        recipient = "0x" + topics[2][-40:]
        amount = int(data, 16)
        if sender == account:
            net -= amount
        if recipient == account:
            net += amount
    return net


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--trace-golden", type=Path, required=True)
    parser.add_argument("--rpc-env-file", type=Path, required=True)
    parser.add_argument("--rpc-env-key", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = json.loads(args.sample.read_text(encoding="utf-8"))
    trace = json.loads(args.trace_golden.read_text(encoding="utf-8"))
    source_by_tx = {row["tx_hash"]: row for row in source["rows"]}
    cases = [source_by_tx[row["tx_hash"]] for row in trace["rows"]]
    rpc = RpcClient(load_rpc_url(args.rpc_env_file, args.rpc_env_key))
    receipts = []
    for offset in range(0, len(cases), 20):
        receipts.extend(throttled_batch(
            rpc,
            [("eth_getTransactionReceipt", [row["tx_hash"]]) for row in cases[offset:offset + 20]],
        ))
    calls = []
    for row in cases:
        calldata = "0x70a08231" + row["resolved_account_or_entity"][2:].rjust(64, "0")
        request = {"to": row["target_token_address"], "data": calldata}
        block = int(row["block_number"])
        calls.extend([
            ("eth_call", [request, hex(block - 1)]),
            ("eth_call", [request, hex(block)]),
        ])
    balances = []
    for offset in range(0, len(calls), 40):
        balances.extend(throttled_batch(rpc, calls[offset:offset + 40]))
    output_rows = []
    for index, (row, receipt) in enumerate(zip(cases, receipts)):
        account = row["resolved_account_or_entity"]
        tx_net = transfer_net_for(
            (receipt or {}).get("logs") or [], row["target_token_address"], account
        )
        start_balance = int(balances[2 * index], 16)
        end_balance = int(balances[2 * index + 1], 16)
        balance_delta = end_balance - start_balance
        direction_match = tx_net > 0 if row["side"] == "buy" else tx_net < 0 if row["side"] == "sell" else False
        exact_inventory = tx_net == balance_delta and direction_match
        output_rows.append({
            "tx_hash": row["tx_hash"],
            "block_number": row["block_number"],
            "route_family": row["route_family"],
            "account": account,
            "target_token": row["target_token_address"],
            "side": row["side"],
            "tx_transfer_net_raw": str(tx_net),
            "balance_before_raw": str(start_balance),
            "balance_after_raw": str(end_balance),
            "block_balance_delta_raw": str(balance_delta),
            "direction_match": direction_match,
            "exact_inventory_delta": exact_inventory,
            "pnl_reconstructable": exact_inventory,
            "pnl_basis": "exact account inventory delta + associated WETH pool swap amounts; gas payer retained separately",
        })
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in output_rows:
        by_account[row["account"]].append(row)
    account_results = [
        {
            "account": account,
            "sampled_transactions": len(rows),
            "all_sampled_inventory_exact": all(row["exact_inventory_delta"] for row in rows),
        }
        for account, rows in sorted(by_account.items())
    ]
    reconstructable = sum(row["all_sampled_inventory_exact"] for row in account_results)
    result = {
        "schema_version": 1,
        "research_only": True,
        "transaction_cases": len(output_rows),
        "unique_accounts": len(account_results),
        "reconstructable_accounts": reconstructable,
        "reconstructable_account_pct": round(100 * reconstructable / len(account_results), 6),
        "pnl_reconstructable_gate_80": reconstructable / len(account_results) >= 0.80,
        "scope_note": "Feasibility for sampled target-universe transactions, not a claim of complete lifetime wallet history.",
        "rpc_batch_calls": rpc.calls,
        "account_results": account_results,
        "rows": output_rows,
        "no_go": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "transaction_cases", "unique_accounts", "reconstructable_accounts",
        "reconstructable_account_pct", "pnl_reconstructable_gate_80"
    )}, indent=2))


if __name__ == "__main__":
    main()
