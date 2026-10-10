"""Pre-trade gates — must pass BEFORE any live buy is signed.

Gate stack (all observable pre-entry, no look-ahead):
  0. cluster blacklist (F) — same deployer/bytecode-skeleton as a known rug; cheapest, runs first
  1. external risk — GoPlus/Honeypot.is/proxy/admin red flags; reject risky tokens
  2. honeypot — sell-path simulation; reject if unsellable
  3. friction — QuoterV2 round-trip at pending state; reject if cost > threshold
  4. deep-pool floor — reject pools with WETH depth < EXECUTOR_MIN_POOL_WETH
  5. executable sell — real-gas sell simulation; reject gas-bomb honeypots

These gates mirror the keystone backtest: depth-gate + left-tail filter.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from web3 import Web3

import cluster_blacklist
from external_risk import ExternalRiskPolicy, assess_external_token_risk
from registry import PoolInfo, WETH_BASE, get_redis
from swap import _get_token_balance, check_honeypot, quote_sell_eth_wei, simulate_real_sell

log = logging.getLogger(__name__)

# Deep-pool floor (①): verified-optimal "free" gate — fatal rate 2.9%→1.8% at
# near-flat alpha (gate_sweep report). Pools shallower than this WETH depth at
# entry are the cheapest honeypot/rug substrate; reject them. 0 disables.
MIN_POOL_WETH_ETH = float(os.environ.get("EXECUTOR_MIN_POOL_WETH", "3.0") or 0)
# If depth cannot be read, fail-open by default (other gates still apply); the
# friction/sell sims already exercise real depth.
DEPTH_GATE_FAIL_CLOSED = os.environ.get("EXECUTOR_MIN_POOL_WETH_FAIL_CLOSED", "0").lower() in ("1", "true", "yes")

# Max round-trip friction to enter (same as keystone: -5% default, -2% strict)
DEFAULT_FRICTION_MIN = float(os.environ.get("EXECUTOR_FRICTION_MIN", "-0.05"))
# Skip friction gate if Quoter fails (fail-closed in live, fail-open only in dry-run)
FRICTION_FAIL_CLOSED = os.environ.get("EXECUTOR_FRICTION_FAIL_CLOSED", "1").lower() in ("1", "true", "yes")

# Executable-sell gate: replay the real router sell at realistic gas (catches
# gas-bomb honeypots that pass QuoterV2 but revert on a real tx). On by default.
SELL_SIM_ENABLED = os.environ.get("EXECUTOR_SELL_SIM", "1").lower() in ("1", "true", "yes")
# If the sim cannot run (slot undetected / rpc error / unsupported pool type),
# fail-open by default (other gates still apply). Set 1 to hard-block instead.
SELL_SIM_FAIL_CLOSED = os.environ.get("EXECUTOR_SELL_SIM_FAIL_CLOSED", "0").lower() in ("1", "true", "yes")
EXTERNAL_RISK_POLICY = ExternalRiskPolicy()


@dataclass
class GateResult:
    ok: bool
    reason: str
    friction: Optional[float] = None
    expected_out_wei: Optional[int] = None
    canary_usd: Optional[float] = None  # when set, caller should cap stake to this


def _quote_buy_eth(w3: Web3, pool: PoolInfo, amount_eth_wei: int) -> Optional[int]:
    """Expected token out for WETH->token via QuoterV2 (V3) or reserve math (V2)."""
    if pool.pool_type in ("v3", "aero_cl"):
        from swap import _QUOTER_ABI, QUOTER_V2_BASE
        quoter = w3.eth.contract(
            address=Web3.to_checksum_address(QUOTER_V2_BASE), abi=_QUOTER_ABI,
        )
        token_out = Web3.to_checksum_address(pool.non_weth_token)
        weth = Web3.to_checksum_address(WETH_BASE)
        fee = pool.fee_bps * 100
        try:
            result = quoter.functions.quoteExactInputSingle(
                (weth, token_out, amount_eth_wei, fee, 0)
            ).call()
            return int(result[0])
        except Exception as e:
            log.warning("buy quote failed pool=%s: %s", pool.pool_address, e)
            return None
    # V2: use a small reserve-based estimate via sell quote inverted — skip for now
    return None


def friction_gate(w3: Web3, pool: PoolInfo, amount_eth_wei: int,
                  friction_min: float = DEFAULT_FRICTION_MIN) -> GateResult:
    """Live round-trip friction check: buy quote then immediate sell quote."""
    token_out = _quote_buy_eth(w3, pool, amount_eth_wei)
    if token_out is None or token_out <= 0:
        if FRICTION_FAIL_CLOSED:
            return GateResult(False, "buy_quote_fail")
        return GateResult(True, "buy_quote_skip")
    sell_eth = quote_sell_eth_wei(w3, pool, token_out)
    if sell_eth <= 0:
        return GateResult(False, "sell_quote_fail_honeypot", expected_out_wei=token_out)
    friction = sell_eth / amount_eth_wei - 1.0
    if friction < friction_min:
        return GateResult(False, f"friction_{friction:.3f}_below_{friction_min}", friction=friction)
    return GateResult(True, "ok", friction=friction, expected_out_wei=token_out)


def honeypot_gate(w3: Web3, pool: PoolInfo) -> GateResult:
    if check_honeypot(w3, pool.non_weth_token, pool):
        return GateResult(False, "honeypot")
    return GateResult(True, "ok")


def external_risk_gate(pool: PoolInfo) -> GateResult:
    """Off-chain token security aggregation (BUY-only).

    This catches known scanner/pattern risks that pure QuoterV2 cannot model:
    proxy/admin upgradeability, blacklist/pause/mint controls, extreme taxes, and
    third-party honeypot simulation flags. API unavailability is fail-closed by
    default because a missed BUY is cheaper than a trapped position.
    """
    risk = assess_external_token_risk(pool.non_weth_token, policy=EXTERNAL_RISK_POLICY)
    if not risk.ok:
        log.warning(
            "external risk REJECT token=%s pool=%s critical=%s warnings=%s",
            pool.non_weth_token,
            pool.pool_address,
            risk.critical,
            risk.warnings,
        )
        return GateResult(False, f"external_risk_{risk.reason}")
    if risk.warnings:
        log.info("external risk WARN token=%s warnings=%s", pool.non_weth_token, risk.warnings)
    if risk.data_missing and risk.canary_usd and risk.canary_usd > 0:
        log.warning(
            "external risk UNKNOWN -> canary $%.0f token=%s reason=%s",
            risk.canary_usd, pool.non_weth_token, risk.reason,
        )
        return GateResult(True, risk.reason, canary_usd=risk.canary_usd)
    return GateResult(True, risk.reason)


def executable_sell_gate(
    w3: Web3, pool: PoolInfo, token_amount_wei: int, wallet: Optional[str] = None,
) -> GateResult:
    """Replay the real router sell at realistic gas (gas-bomb honeypot defense).

    Unlike the QuoterV2 honeypot/friction checks (which run at near-unlimited gas
    and can be fooled), this simulates a sell tx at a realistic gas cap via state
    override. A revert here means a real sell would be impossible -> hard block.
    """
    if not SELL_SIM_ENABLED:
        return GateResult(True, "sell_sim_disabled")
    if not token_amount_wei or token_amount_wei <= 0:
        return GateResult(True, "sell_sim_skip_no_amount")
    try:
        ok, reason = simulate_real_sell(w3, pool, token_amount_wei, wallet=wallet)
    except Exception as e:
        log.warning("executable_sell_gate error pool=%s: %s", pool.pool_address, e)
        return GateResult(not SELL_SIM_FAIL_CLOSED, "sell_sim_error")
    if ok:
        # "sim_unavailable_*" means the storage-slot probe failed, i.e. we could
        # not run the test — NOT that the token failed it. Treating "unknown" as
        # "unsellable" rejected BRIAN (2026-07-05: 191 ETH volume, 10x in 30min,
        # non-standard token storage layout). Fail-open is the default because
        # honeypot_gate already passed upstream on a quote-based check that needs
        # no slots; what is lost is only gas-bomb detection at realistic gas, and
        # the post-buy realgas probe re-tests that and ABANDONs + blacklists.
        if reason.startswith("sim_unavailable") or reason.startswith("sim_rpc_error"):
            if SELL_SIM_FAIL_CLOSED:
                return GateResult(False, reason)
            log.warning(
                "executable_sell_gate COULD NOT RUN pool=%s token=%s reason=%s "
                "(fail-open; gas-bomb check deferred to post-buy realgas probe)",
                pool.pool_address, pool.non_weth_token, reason,
            )
        return GateResult(True, reason)
    return GateResult(False, reason)


def cluster_blacklist_gate(w3: Web3, pool: PoolInfo, deployer: Optional[str] = None) -> GateResult:
    """F: block repeat shots from a known rug cluster (same deployer EOA or same
    token bytecode skeleton). Cheapest gate — one get_code call — so it runs
    first. Fail-open (a Redis hiccup must not freeze trading)."""
    r = get_redis()
    blocked, reason = cluster_blacklist.is_blacklisted(r, w3, pool, deployer=deployer)
    if blocked:
        log.error("pretrade REJECT (cluster) pool=%s token=%s reason=%s",
                  pool.pool_address, pool.non_weth_token, reason)
        return GateResult(False, f"cluster_{reason}")
    return GateResult(True, "ok")


def pool_depth_gate(w3: Web3, pool: PoolInfo, min_weth_eth: float = MIN_POOL_WETH_ETH) -> GateResult:
    """① Deep-pool floor: reject pools whose live WETH depth < min_weth_eth.

    Verified the single "free" pre-entry gate (gate_sweep): cuts fatal rate
    2.9%→1.8% with near-flat alpha. WETH is physically custodied at the pool
    address for v2/v3/aero, so balanceOf on pre-trade state is causal.
    """
    if min_weth_eth <= 0:
        return GateResult(True, "depth_gate_disabled")
    try:
        bal = _get_token_balance(w3, WETH_BASE, pool.pool_address)
    except Exception as e:
        log.warning("pool depth read failed pool=%s: %s", pool.pool_address, e)
        if DEPTH_GATE_FAIL_CLOSED:
            return GateResult(False, "depth_read_fail")
        return GateResult(True, "depth_read_skip")
    depth_eth = (int(bal) if bal else 0) / 1e18
    if depth_eth < min_weth_eth:
        return GateResult(False, f"shallow_pool_{depth_eth:.3f}E_below_{min_weth_eth:g}E")
    return GateResult(True, f"depth_{depth_eth:.3f}E")


def run_pretrade_gates(
    w3: Web3, pool: PoolInfo, amount_eth_wei: int, wallet: Optional[str] = None,
    deployer: Optional[str] = None,
) -> GateResult:
    """Full pre-trade gate stack. Returns first failure or ok.

    On success the returned GateResult carries ``canary_usd`` when external risk
    data was missing (unknown token) so the caller can shrink the stake.
    """
    cb = cluster_blacklist_gate(w3, pool, deployer=deployer)
    if not cb.ok:
        return cb
    er = external_risk_gate(pool)
    if not er.ok:
        log.warning("pretrade REJECT pool=%s reason=%s", pool.pool_address, er.reason)
        return er
    canary_usd = er.canary_usd
    hp = honeypot_gate(w3, pool)
    if not hp.ok:
        log.warning("pretrade REJECT pool=%s reason=%s", pool.pool_address, hp.reason)
        return hp
    fr = friction_gate(w3, pool, amount_eth_wei)
    if not fr.ok:
        log.warning(
            "pretrade REJECT pool=%s reason=%s friction=%s",
            pool.pool_address, fr.reason, fr.friction,
        )
        return fr
    # ① Deep-pool floor — cheap depth read, before the expensive sell sim.
    dg = pool_depth_gate(w3, pool)
    if not dg.ok:
        log.warning("pretrade REJECT pool=%s reason=%s (shallow pool)", pool.pool_address, dg.reason)
        return GateResult(False, dg.reason, friction=fr.friction, expected_out_wei=fr.expected_out_wei)
    # Executable-sell sim at the REAL notional we'd hold (buy-quote token_out).
    es = executable_sell_gate(w3, pool, fr.expected_out_wei or 0, wallet=wallet)
    if not es.ok:
        log.warning(
            "pretrade REJECT pool=%s reason=%s (gas-bomb/unsellable at realistic gas)",
            pool.pool_address, es.reason,
        )
        return GateResult(False, es.reason, friction=fr.friction, expected_out_wei=fr.expected_out_wei)
    log.info(
        "pretrade PASS pool=%s friction=%.4f expected_out=%s depth=%s sell_sim=%s canary=%s",
        pool.pool_address,
        fr.friction or 0,
        fr.expected_out_wei,
        dg.reason,
        es.reason,
        canary_usd,
    )
    fr.canary_usd = canary_usd
    return fr
