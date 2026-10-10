#!/usr/bin/env python3
"""
MVL (Minimum Viable Live) canary — ONE real on-chain round trip to prove the
execution pipeline end-to-end with real money, bounded to cents of risk.

What it does:
  ETH --buy--> USDC  (Uniswap V3 WETH/USDC 0.05% pool, deepest liquidity on Base)
  USDC --sell--> ETH

Why WETH/USDC: deepest pool on Base → minimal slippage → the ~loss is only
fees+gas (cents). This proves sign→broadcast(dRPC)→fill→receipt→approval→
sell→unwrap, which has NEVER been exercised in 3 months of paper trading.
It does NOT validate alpha — it validates that we can actually transact.

SAFETY:
  - Reads RPC + key from infra/compose/.env (key never printed).
  - DRY by default. Sends real tx ONLY with --confirm AND env LIVE_CANARY_CONFIRM=YES.
  - Hard cap on ETH spent (default 0.0002 ETH; absolute max 0.001 ETH).
  - Aborts if wallet ETH < spend + gas reserve.

Usage:
  # 1) read-only preflight (no tx):
  python3 scripts/live/mvl_canary_roundtrip.py
  # 2) fire the real round trip:
  LIVE_CANARY_CONFIRM=YES python3 scripts/live/mvl_canary_roundtrip.py --confirm --eth 0.0002
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXEC_SRC = REPO / "services" / "executor" / "src"
sys.path.insert(0, str(EXEC_SRC))

from web3 import Web3  # noqa: E402
from eth_account import Account  # noqa: E402

from registry import PoolInfo, WETH_BASE, UNISWAP_V3_ROUTER  # noqa: E402
import swap as swapmod  # noqa: E402

# Canonical Base WETH/USDC 0.05% V3 pool (deepest liquidity)
POOL = PoolInfo(
    pool_address="0xd0b53d9277642d899df5c87a3966a349a798f224",
    pool_type="v3",
    router=UNISWAP_V3_ROUTER,
    token0=WETH_BASE,
    token0_symbol="WETH",
    token0_decimals=18,
    token1="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    token1_symbol="USDC",
    token1_decimals=6,
    fee_bps=5,  # meta encoding → protocol fee 500 (0.05%)
)

ABS_MAX_ETH = 0.001  # hard ceiling regardless of --eth
GAS_RESERVE_ETH = 0.00015  # keep this much ETH free for gas


def load_env() -> dict:
    env: dict[str, str] = {}
    p = REPO / "infra" / "compose" / ".env"
    for line in p.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eth", type=float, default=0.0002, help="ETH to spend on the buy leg")
    ap.add_argument("--confirm", action="store_true", help="actually send real transactions")
    ap.add_argument("--hold-sec", type=int, default=5, help="seconds to hold USDC before selling")
    args = ap.parse_args()

    if args.eth > ABS_MAX_ETH:
        print(f"ABORT: --eth {args.eth} exceeds hard ceiling {ABS_MAX_ETH}")
        return 2

    env = load_env()
    rpc = env.get("EXECUTOR_RPC_URL") or env.get("EVM_HTTP_URL")
    pk = env.get("EXECUTOR_PRIVATE_KEY", "")
    if not rpc or not pk:
        print("ABORT: missing EXECUTOR_RPC_URL or EXECUTOR_PRIVATE_KEY in .env")
        return 2

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    acct = Account.from_key(pk)
    spend_wei = int(args.eth * 1e18)

    print("=" * 60)
    print("MVL CANARY — WETH/USDC round trip")
    print(f"  wallet:   {acct.address}")
    print(f"  rpc:      {rpc.split('//',1)[-1].split('/')[0]}")
    print(f"  chain_id: {w3.eth.chain_id}")
    bal = w3.eth.get_balance(acct.address)
    print(f"  ETH bal:  {bal/1e18:.6f}")
    print(f"  buy spend:{args.eth} ETH ({spend_wei} wei)")

    # Quote check (read-only)
    expected_usdc = swapmod._estimate_buy_output_v3(w3, POOL, spend_wei)
    print(f"  quote:    {spend_wei} wei WETH -> {expected_usdc/1e6:.6f} USDC (est)")

    need = spend_wei + int(GAS_RESERVE_ETH * 1e18)
    if bal < need:
        print(f"ABORT: ETH balance {bal/1e18:.6f} < spend+gas_reserve {need/1e18:.6f}")
        print("  → fund the wallet with more ETH, or lower --eth")
        return 2
    if expected_usdc <= 0:
        print("ABORT: quoter returned 0 — pool/fee/RPC issue, not safe to send")
        return 2

    confirmed = args.confirm and env.get("LIVE_CANARY_CONFIRM") == "YES" \
        and os.environ.get("LIVE_CANARY_CONFIRM") == "YES"
    if not confirmed:
        print("-" * 60)
        print("DRY PREFLIGHT OK. No transaction sent.")
        print("To fire for real:")
        print("  LIVE_CANARY_CONFIRM=YES python3 scripts/live/mvl_canary_roundtrip.py --confirm --eth", args.eth)
        return 0

    print("-" * 60)
    print(">>> SENDING REAL BUY ...")
    buy = swapmod.execute_buy(w3, acct, POOL, spend_wei)
    print(f"  buy success={buy.success} tx={buy.tx_hash} usdc_out={buy.amount_out/1e6:.6f} "
          f"gas_wei={buy.gas_cost_wei} err={buy.error}")
    if not buy.success or buy.amount_out <= 0:
        print("ABORT after buy: not selling (buy failed or 0 out). Inspect tx.")
        return 1

    print(f">>> holding {args.hold_sec}s ...")
    time.sleep(args.hold_sec)

    print(">>> SENDING REAL SELL ...")
    sell = swapmod.execute_sell(w3, acct, POOL, buy.amount_out, reason="take_profit")
    print(f"  sell success={sell.success} tx={sell.tx_hash} eth_out={sell.amount_out/1e18:.6f} "
          f"gas_wei={sell.gas_cost_wei} err={sell.error}")

    # Round-trip accounting
    eth_in = spend_wei
    eth_out = sell.amount_out
    gas_total = (buy.gas_cost_wei or 0) + (sell.gas_cost_wei or 0)
    net_wei = eth_out - eth_in - gas_total
    print("=" * 60)
    print("ROUND TRIP COMPLETE")
    print(f"  eth_in:   {eth_in/1e18:.6f}")
    print(f"  eth_out:  {eth_out/1e18:.6f}")
    print(f"  gas:      {gas_total/1e18:.6f}")
    print(f"  net:      {net_wei/1e18:.6f} ETH  (negative = fees+slippage tuition)")
    print(f"  buy_tx:   https://basescan.org/tx/{buy.tx_hash}")
    print(f"  sell_tx:  https://basescan.org/tx/{sell.tx_hash}")
    return 0 if sell.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
