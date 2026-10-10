#!/usr/bin/env python3
"""USDC net of the five traced Base liquidations. Read-only receipts."""
import json
import subprocess
from pathlib import Path

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
OLD = Path("/root/midnight-research")
OUT = Path(__file__).resolve().parents[1] / "data" / "receipt_usdc_nets.json"


def addr(topic: str) -> str:
    return "0x" + topic[-40:]


def main() -> None:
    txs = json.loads((OLD / "raw/top_liq_receipts_summary.json").read_text())
    rows = json.loads((OLD / "data/base_liquidations_flat.json").read_text())
    bytx = {}
    for r in rows:
        bytx.setdefault(r["tx"].lower(), []).append(r)
    out = []
    for t in txs:
        raw = subprocess.check_output(
            ["cast", "rpc", "eth_getTransactionReceipt", t["tx"], "--rpc-url", "https://mainnet.base.org"],
            text=True,
        )
        rec = json.loads(raw)
        nets = {}
        for lg in rec["logs"]:
            if lg["address"].lower() != USDC or not lg["topics"] or lg["topics"][0].lower() != TOPIC:
                continue
            frm, to = addr(lg["topics"][1]), addr(lg["topics"][2])
            val = int(lg["data"], 16)
            nets[frm] = nets.get(frm, 0) - val
            nets[to] = nets.get(to, 0) + val
        who = t["from"].lower()
        contract = t["to"].lower()
        surplus = nets.get(who, 0)
        if contract != "0xadedd8ab6de832766fedf0fac4992e5c4d3ea18a":
            surplus = nets.get(contract, 0)
        out.append({
            "tx": t["tx"],
            "surplus_raw": surplus,
            "midnight_net_raw": nets.get("0xadedd8ab6de832766fedf0fac4992e5c4d3ea18a", 0),
            "repaid_indexed": sum(e["repaid"] for e in bytx.get(t["tx"].lower(), [])),
        })
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    print(sum(x["surplus_raw"] for x in out))


if __name__ == "__main__":
    main()
