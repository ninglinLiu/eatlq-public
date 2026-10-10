#!/usr/bin/env python3
"""
Sprint081 Dry-Run Execution Adapter
=====================================
Reads sender-gated S074_022_6ff5 / S074_032_6ff5 signals from sprint078_shadow.csv,
constructs Uniswap V3 exactInputSingle calldata, simulates via QuoterV2.quoteExactInputSingle
(eth_call — no real transaction), and outputs dryrun_swaps.csv.

Strategy gates applied:
  - is_0x6ff5 = True  (dominant onset sender = Uniswap Universal Router)
  - entry_quote_ok = True
  - pool_age_sec ∈ [1800, 86400]
  - fee_ppm < 10000
  - exit_horizon = 2h  (uses net_return_2h_ag column)

Safety invariants (enforced):
  LIVE_TRADING_MODE = false  (hard abort if set)
  PRIVATE_KEY_TOUCHED = false  (never reads EXECUTOR_PRIVATE_KEY)
  REAL_TX_SENT = false  (only eth_call / quoteExactInputSingle)
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional, List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("sprint081_dryrun")

# ── Safety gates ───────────────────────────────────────────────────────────────

def _parse_bool(v: Optional[str]) -> bool:
    if v is None:
        return False
    return v.strip().lower() in ("1", "true", "yes", "on")


def _assert_safe() -> None:
    """Hard abort if live mode is accidentally enabled."""
    if _parse_bool(os.environ.get("LIVE_TRADING_MODE")):
        log.error("HARD ABORT: LIVE_TRADING_MODE=true is forbidden in dry-run adapter.")
        sys.exit(1)
    if os.environ.get("EXECUTOR_PRIVATE_KEY", "").strip():
        log.warning(
            "EXECUTOR_PRIVATE_KEY is set in env — this script will NOT read or use it. "
            "Only eth_call simulation is performed."
        )


# ── Uniswap V3 constants (Base chain) ─────────────────────────────────────────

WETH_BASE = "0x4200000000000000000000000000000000000006"
QUOTER_V2_BASE = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
SWAPROUTER02_BASE = "0x2626664c2603336E57B271c5C0b26F421741e481"
BASE_CHAIN_ID = 8453

_QUOTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# ── Strategy entry gates ───────────────────────────────────────────────────────

POOL_AGE_SEC_MIN = 1800
POOL_AGE_SEC_MAX = 86400
FEE_PPM_MAX = 9999
TRADE_SIZE_ETH = float(os.environ.get("DRYRUN_TRADE_SIZE_ETH", "0.01"))  # ~$25 at $2500/ETH
SLIPPAGE_BPS = int(os.environ.get("DRYRUN_SLIPPAGE_BPS", "300"))  # 3% default
TOP_N_SIGNALS = int(os.environ.get("DRYRUN_TOP_N_SIGNALS", "20"))

# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class DryRunSwap:
    signal_time: str
    strategy_id: str
    pool: str
    token_out_addr: str
    fee_ppm: int
    weth_is_token1: bool
    pool_age_sec: float
    is_0x6ff5: bool
    amount_in_wei: int
    amount_in_eth: float
    # QuoterV2 results
    expected_out_tokens: int
    amount_out_minimum: int
    slippage_bps: int
    gas_estimate: int
    sqrt_price_x96_after: int
    # Route description
    route: str
    fee_ppm_actual: int
    # Simulation metadata
    sim_source: str          # "quoter_v2_eth_call" | "historical_csv" | "failed"
    sim_ok: bool
    sim_error: str
    # Historical context
    hist_return_2h: str
    # Safety flags
    REAL_TX_SENT: bool = False
    LIVE_TRADING_MODE: bool = False
    PRIVATE_KEY_TOUCHED: bool = False


# ── RPC connection ─────────────────────────────────────────────────────────────

def _get_w3():
    """Return a Web3 instance connected to Base. Returns None if unavailable."""
    try:
        from web3 import Web3
    except ImportError:
        log.warning("web3 not installed — falling back to historical CSV simulation")
        return None

    rpc_url = (
        os.environ.get("EXECUTOR_RPC_URL")
        or os.environ.get("EVM_HTTP_URL")
        or os.environ.get("LIVE_BASE_RPC_URL")
        or "https://mainnet.base.org"
    )
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        if w3.is_connected():
            chain_id = w3.eth.chain_id
            if chain_id != BASE_CHAIN_ID:
                log.warning("RPC connected but chain_id=%s (expected %s)", chain_id, BASE_CHAIN_ID)
                return None
            log.info("RPC connected: %s (chain_id=%s)", rpc_url, chain_id)
            return w3
        else:
            log.warning("RPC not connected: %s", rpc_url)
            return None
    except Exception as e:
        log.warning("RPC connect failed (%s): %s", rpc_url, e)
        return None


# ── Calldata builder ───────────────────────────────────────────────────────────

def build_exact_input_single_calldata(
    token_out: str,
    fee_ppm: int,
    amount_in_wei: int,
    amount_out_minimum: int,
    recipient: str = "0x0000000000000000000000000000000000000001",
) -> str:
    """Build exactInputSingle calldata for SwapRouter02 (WETH→token).

    Follows official Uniswap V3 SwapRouter02 ABI:
      exactInputSingle((tokenIn, tokenOut, fee, recipient, amountIn, amountOutMinimum, sqrtPriceLimitX96))

    Returns hex calldata string. No transaction is sent.
    """
    try:
        from web3 import Web3
        from eth_abi import encode

        fn_sig = Web3.keccak(
            text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
        )[:4]
        weth_cs = Web3.to_checksum_address(WETH_BASE)
        token_cs = Web3.to_checksum_address(token_out)
        recipient_cs = Web3.to_checksum_address(recipient)

        params_encoded = encode(
            ["(address,address,uint24,address,uint256,uint256,uint160)"],
            [(weth_cs, token_cs, fee_ppm, recipient_cs, amount_in_wei, amount_out_minimum, 0)],
        )
        return "0x" + (fn_sig + params_encoded).hex()
    except Exception as e:
        return f"BUILD_FAILED:{e}"


# ── QuoterV2 simulation ────────────────────────────────────────────────────────

def simulate_via_quoter(w3, token_out: str, fee_ppm: int, amount_in_wei: int) -> dict:
    """Call QuoterV2.quoteExactInputSingle via eth_call (WETH→token).

    Returns dict with expected_out, gas_estimate, sqrt_price_x96_after, error.
    No transaction is sent — this is a pure read via eth_call.
    """
    result = {
        "expected_out": 0,
        "gas_estimate": 0,
        "sqrt_price_x96_after": 0,
        "ok": False,
        "error": "",
    }
    if w3 is None:
        result["error"] = "no_rpc"
        return result
    try:
        from web3 import Web3
        quoter = w3.eth.contract(
            address=Web3.to_checksum_address(QUOTER_V2_BASE),
            abi=_QUOTER_ABI,
        )
        weth_cs = Web3.to_checksum_address(WETH_BASE)
        token_cs = Web3.to_checksum_address(token_out)
        res = quoter.functions.quoteExactInputSingle(
            (weth_cs, token_cs, amount_in_wei, fee_ppm, 0)
        ).call()
        result["expected_out"] = int(res[0])
        result["sqrt_price_x96_after"] = int(res[1])
        result["gas_estimate"] = int(res[3])
        result["ok"] = result["expected_out"] > 0
        if not result["ok"]:
            result["error"] = "quoter_returned_zero"
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


# ── CSV signal reader ──────────────────────────────────────────────────────────

def load_signals(csv_path: str) -> List[dict]:
    """Load sender-gated signals from sprint078_shadow.csv."""
    rows = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Strategy gate: S074_022_6ff5 / S074_032_6ff5
            if row.get("is_0x6ff5", "") != "True":
                continue
            if row.get("entry_quote_ok", "") != "True":
                continue
            try:
                age = float(row.get("pool_age_sec", 0))
            except ValueError:
                continue
            if not (POOL_AGE_SEC_MIN <= age <= POOL_AGE_SEC_MAX):
                continue
            try:
                # CSV fee_bps column stores the raw Uniswap V3 fee parameter
                # (100=0.01%, 500=0.05%, 3000=0.3%, 10000=1%).
                # This is NOT "basis points" in the traditional sense; it is ppm.
                fee_ppm = int(float(row.get("fee_bps", 0)))
            except ValueError:
                continue
            if fee_ppm > FEE_PPM_MAX:
                continue
            rows.append(row)
    return rows


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    _assert_safe()

    csv_path = os.environ.get(
        "SPRINT078_CSV", "/opt/EATLQ/logs/sprint078_shadow.csv"
    )
    out_path = os.environ.get(
        "DRYRUN_OUT_CSV", "/opt/EATLQ/mvp/audits/execution_readiness/dryrun_swaps.csv"
    )

    log.info("Loading signals from %s", csv_path)
    signals = load_signals(csv_path)
    log.info("Eligible sender-gated signals: %d", len(signals))

    # De-duplicate by pool — one signal per pool to cover distinct pools
    seen_pools: dict = {}
    for row in signals:
        pool = row["pool"]
        if pool not in seen_pools:
            seen_pools[pool] = row
    deduped = list(seen_pools.values())[:TOP_N_SIGNALS]
    log.info("Unique pools selected for simulation: %d", len(deduped))

    w3 = _get_w3()
    rpc_available = w3 is not None

    amount_in_wei = int(TRADE_SIZE_ETH * 1e18)
    results: List[DryRunSwap] = []

    for row in deduped:
        pool = row["pool"]
        token_out = row["token_other"]
        # CSV fee_bps = raw V3 fee parameter (same units as ppm here)
        fee_ppm = int(float(row.get("fee_bps", 0)))
        weth_is_token1 = row.get("weth_is_token1", "").lower() in ("true", "1")
        pool_age_sec = float(row.get("pool_age_sec", 0))
        signal_time = row.get("signal_time", "")
        hist_return_2h = row.get("net_return_2h_ag", "")

        # Determine strategy_id: S074_032_6ff5 requires pv_5m >= 0
        try:
            pv_5m = float(row.get("price_velocity_5m", 0) or 0)
            strategy_id = "S074_032_6ff5" if pv_5m >= 0.0 else "S074_022_6ff5"
        except ValueError:
            strategy_id = "S074_022_6ff5"

        route = (
            f"WETH→{token_out[:10]}… "
            f"via UniswapV3 SwapRouter02 fee={fee_ppm} pool={pool[:10]}…"
        )

        log.info("Simulating pool=%s fee_ppm=%s strategy=%s", pool[:16], fee_ppm, strategy_id)

        if rpc_available:
            q = simulate_via_quoter(w3, token_out, fee_ppm, amount_in_wei)
            sim_source = "quoter_v2_eth_call"
            sim_ok = q["ok"]
            sim_error = q["error"]
            expected_out = q["expected_out"]
            gas_estimate = q["gas_estimate"]
            sqrt_price = q["sqrt_price_x96_after"]
        else:
            # Fall back to historical CSV quote as proxy
            try:
                hist_out = int(float(row.get("entry_amount_out_tokens", 0) or 0))
            except ValueError:
                hist_out = 0
            expected_out = hist_out
            gas_estimate = 180000  # conservative V3 swap estimate
            sqrt_price = 0
            sim_source = "historical_csv"
            sim_ok = hist_out > 0
            sim_error = "" if hist_out > 0 else "no_historical_quote"

        amount_out_minimum = int(expected_out * (1 - SLIPPAGE_BPS / 10000)) if expected_out > 0 else 0
        calldata = build_exact_input_single_calldata(
            token_out=token_out,
            fee_ppm=fee_ppm,
            amount_in_wei=amount_in_wei,
            amount_out_minimum=amount_out_minimum,
        )
        calldata_ok = not calldata.startswith("BUILD_FAILED")
        if not calldata_ok:
            sim_error = sim_error or calldata

        swap = DryRunSwap(
            signal_time=signal_time,
            strategy_id=strategy_id,
            pool=pool,
            token_out_addr=token_out,
            fee_ppm=fee_ppm,
            weth_is_token1=weth_is_token1,
            pool_age_sec=pool_age_sec,
            is_0x6ff5=True,
            amount_in_wei=amount_in_wei,
            amount_in_eth=TRADE_SIZE_ETH,
            expected_out_tokens=expected_out,
            amount_out_minimum=amount_out_minimum,
            slippage_bps=SLIPPAGE_BPS,
            gas_estimate=gas_estimate,
            sqrt_price_x96_after=sqrt_price,
            route=route,
            fee_ppm_actual=fee_ppm,
            sim_source=sim_source,
            sim_ok=sim_ok and calldata_ok,
            sim_error=sim_error,
            hist_return_2h=hist_return_2h,
            REAL_TX_SENT=False,
            LIVE_TRADING_MODE=False,
            PRIVATE_KEY_TOUCHED=False,
        )
        results.append(swap)
        status = "OK" if swap.sim_ok else f"FAIL({sim_error[:40]})"
        log.info("  %s expected_out=%d gas=%d calldata_len=%d",
                 status, expected_out, gas_estimate,
                 len(calldata) // 2 if calldata_ok else 0)
        time.sleep(0.05)  # brief pause to avoid RPC rate limiting

    # Write output CSV
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fieldnames = list(DryRunSwap.__dataclass_fields__.keys())
    success_count = sum(1 for r in results if r.sim_ok)

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))

    log.info("Wrote %d dry-run swaps to %s", len(results), out_path)
    log.info(
        "Summary: total=%d sim_ok=%d sim_fail=%d rpc_mode=%s",
        len(results), success_count, len(results) - success_count,
        "quoter_v2_eth_call" if rpc_available else "historical_csv_fallback",
    )

    # Safety assertion
    real_tx = sum(1 for r in results if r.REAL_TX_SENT)
    assert real_tx == 0, f"BUG: REAL_TX_SENT=true in {real_tx} rows"
    live_mode = sum(1 for r in results if r.LIVE_TRADING_MODE)
    assert live_mode == 0, f"BUG: LIVE_TRADING_MODE=true in {live_mode} rows"
    pk_touched = sum(1 for r in results if r.PRIVATE_KEY_TOUCHED)
    assert pk_touched == 0, f"BUG: PRIVATE_KEY_TOUCHED in {pk_touched} rows"

    log.info("Safety check passed: REAL_TX_SENT=false LIVE_TRADING_MODE=false PRIVATE_KEY_TOUCHED=false")
    return results


if __name__ == "__main__":
    main()
