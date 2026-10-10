#!/usr/bin/env python3
"""READ-ONLY forensics on the smoke round-trip: latency, slippage, gas, sandwich."""
import sys
from pathlib import Path
from web3 import Web3

REPO = Path("/opt/EATLQ")
sys.path.insert(0, str(REPO / "services" / "executor" / "src"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
from v3_quoterv2_struct import quote_exact_input_single_struct  # noqa

ENV = {}
for ln in (REPO / "infra/compose/.env").read_text().splitlines():
    if "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        ENV[k] = v
w3 = Web3(Web3.HTTPProvider(ENV.get("EXECUTOR_RPC_URL")))

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
POOL = Web3.to_checksum_address("0xd0b53d9277642d899df5c87a3966a349a798f224")
BUY = "0xd925db0d3d1956d91ee37a106f28e53835c6a8c7921280c0c642b5c02ad2547f"
SELL = "0xa755efd0287a7e51d0d7f2f86c2bfc5b656f4ff9dc00173b0894b6a98e81e39b"
SUBMIT_BUY = 1782150859.131  # epoch when script started (buy submitted ~immediately)

SWAP_TOPIC = w3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int128)").hex()


def leg(txh, label):
    r = w3.eth.get_transaction_receipt(txh)
    tx = w3.eth.get_transaction(txh)
    blk = w3.eth.get_block(r["blockNumber"])
    gas_used = r["gasUsed"]
    eff = r.get("effectiveGasPrice") or tx.get("gasPrice")
    gas_eth = gas_used * eff / 1e18
    print(f"\n[{label}] tx={txh[:14]}… block={r['blockNumber']} ts={blk['timestamp']} "
          f"status={r['status']}")
    print(f"  gas_used={gas_used} eff_gas_price_gwei={eff/1e9:.4f} gas_eth={gas_eth:.8f}")
    # same-pool swaps in this block (sandwich detector)
    pool_swaps = 0
    my_swaps = 0
    for lg in blk_logs(r["blockNumber"]):
        if lg["address"].lower() == POOL.lower():
            pool_swaps += 1
            if lg["transactionHash"].hex().lower().lstrip("0x") == txh.lower():
                my_swaps += 1
    print(f"  swaps on WETH/USDC pool in same block: {pool_swaps} (mine: {my_swaps})")
    return r, blk, gas_eth, pool_swaps


_LOGCACHE = {}
def blk_logs(bn):
    if bn not in _LOGCACHE:
        _LOGCACHE[bn] = w3.eth.get_logs({"fromBlock": bn, "toBlock": bn,
                                          "address": POOL, "topics": [SWAP_TOPIC]})
    return _LOGCACHE[bn]


rb, bb, gb, sb = leg(BUY, "BUY")
rs, bs, gs, ss = leg(SELL, "SELL")

# latency: submit epoch -> buy block timestamp
print("\n=== LATENCY ===")
print(f"  buy submit epoch  : {SUBMIT_BUY:.3f}")
print(f"  buy block ts      : {bb['timestamp']}")
print(f"  submit->inclusion : {bb['timestamp'] - SUBMIT_BUY:+.2f} s (buy)")

# QuoterV2 re-quote at buy block for fill-vs-quote deviation
from v3_quoterv2_struct import JsonRpcClient  # noqa
amt_in = int(0.003 * 1e18)
try:
    rpc = JsonRpcClient(ENV.get("EXECUTOR_RPC_URL"))
    q = quote_exact_input_single_struct(rpc, WETH, USDC, amt_in, 500,
                                        block_identifier=rb["blockNumber"])
    qout = q["amount_out"] if q.get("ok") else None
    print("\n=== QUOTE vs FILL (buy) ===")
    print(f"  quoterv2 @buyblock usdc: {qout/1e6 if qout else None}")
    print(f"  actual fill usdc       : 5.212422  (from script)")
    if qout:
        dev = (5.212422 - qout/1e6) / (qout/1e6) * 100
        print(f"  fill-vs-quote deviation: {dev:+.4f}%")
except Exception as e:
    print("quoterv2 re-quote skipped:", e)

print("\n=== GAS (USD @ ETH~$1734) ===")
print(f"  buy  gas = {gb:.8f} ETH (~${gb*1734:.4f})")
print(f"  sell gas = {gs:.8f} ETH (~${gs*1734:.4f})")
print(f"  total    = {gb+gs:.8f} ETH (~${(gb+gs)*1734:.4f})")
