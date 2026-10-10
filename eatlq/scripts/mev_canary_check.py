#!/usr/bin/env python3
"""MEV canary: 用 SEND RPC 发一笔极小 WETH self-transfer，检查是否出现在 public mempool。

用法 (在 VPS 上，.env 已配置 EXECUTOR_PRIVATE_KEY):
  cd /opt/EATLQ
  EXECUTOR_READ_RPC_URL=... EXECUTOR_SEND_RPC_URL=... EXECUTOR_PRIVATE_KEY=0x... \
    python3 scripts/mev_canary_check.py

通过标准: tx 在 basescan 上可见且已确认，但在 public mempool explorer (如 mempool.base.org) 上
在 pending 阶段不可见。若 pending 阶段公开可见 → MEV 保护未生效。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, "/opt/EATLQ/services/executor/src")
from eth_account import Account
from web3 import Web3
from tx_sender import init_send_lane, send_raw_transaction, wait_for_receipt


def main() -> None:
    read_url = os.environ.get("EXECUTOR_READ_RPC_URL") or os.environ.get("EXECUTOR_RPC_URL", "")
    pk = os.environ.get("EXECUTOR_PRIVATE_KEY", "").strip()
    if not read_url or not pk:
        print("ERROR: 需要 EXECUTOR_READ_RPC_URL 和 EXECUTOR_PRIVATE_KEY")
        sys.exit(1)

    w3, _ = init_send_lane(read_url)
    acct = Account.from_key(pk)
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    chain_id = w3.eth.chain_id

    tx = {
        "from": acct.address,
        "to": acct.address,
        "value": 0,
        "nonce": nonce,
        "chainId": chain_id,
        "gas": 21000,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": max(w3.eth.gas_price // 10, 1_000_000),
    }
    signed = acct.sign_transaction(tx)
    print(f"Sending canary self-transfer from {acct.address} via SEND lane...")
    tx_hash = send_raw_transaction(signed.raw_transaction, w3)
    h = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    print(f"TX hash: {h}")
    print(f"Basescan: https://basescan.org/tx/{h}")
    print("")
    print("在 tx 还在 pending 时 (前 2 秒)，去 https://mempool.base.org 搜索此 hash。")
    print("若 pending 阶段可见 → MEV 保护可能未生效。")
    print("若只有 confirmed 后在 basescan 可见 → 正常。")

    receipt = wait_for_receipt(w3, tx_hash, timeout_sec=30)
    print(f"Confirmed in block {receipt['blockNumber']} status={receipt['status']}")


if __name__ == "__main__":
    main()
