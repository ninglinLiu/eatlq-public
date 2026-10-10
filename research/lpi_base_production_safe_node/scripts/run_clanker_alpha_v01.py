#!/usr/bin/env python3
"""Clanker Alpha Feasibility Study v0.1 — public Base RPC only."""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eth_abi import decode
from eth_hash.auto import keccak

from launchpad_intelligence.clanker_alpha.decoder import (
    FACTORY_V4,
    TOKEN_CREATED_TOPIC0,
    ClankerLaunchDecoder,
)
from launchpad_intelligence.clanker_alpha.rpc_public import PublicRpcClient

EVIDENCE = ROOT / "evidence" / "clanker_alpha_v01"
RAW = ROOT / "reports" / "raw" / "clanker_alpha_v01"
REPORTS = ROOT / "reports"
EXPORT = ROOT / "export"

POOL_MANAGER = "0x498581ff718922c3f8e6A244956aF099B2652b2b"
SWAP_TOPIC0 = "0x" + keccak(
    b"Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
).hex()
TRANSFER_TOPIC0 = "0x" + keccak(b"Transfer(address,address,uint256)").hex()
WETH = "0x4200000000000000000000000000000000000006".lower()
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913".lower()

# Pre-registered primary evaluation point (see hypotheses doc).
PRIMARY_ENTRY_DELAY_BLOCKS = 3
PRIMARY_EXIT_MINUTES = 30
PRIMARY_NOTIONAL_USD = 250.0
BLOCKS_PER_DAY = 43200  # ~2s Base blocks; calibrated in-run
BASE_BLOCK_SECONDS = 2.0

COST = {
    "optimistic": {"slippage": 0.001, "impact": 0.0, "gas_units": 180_000, "gas_mult": 1.0},
    "base": {"slippage": 0.005, "impact": 0.003, "gas_units": 220_000, "gas_mult": 1.15},
    "stressed": {"slippage": 0.02, "impact": 0.015, "gas_units": 300_000, "gas_mult": 1.5},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, obj: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=2, sort_keys=True) + "\n"
    path.write_text(text)
    return hashlib.sha256(text.encode()).hexdigest()


def tick_to_price(tick: int) -> float:
    return 1.0001 ** tick


def sqrt_price_x96_to_price(sqrt_price_x96: int) -> float:
    ratio = sqrt_price_x96 / (2**96)
    return ratio * ratio


def token_price_in_paired(token: str, paired: str, tick_if_token0_is_clanker: int, sqrt_price_x96: Optional[int]) -> float:
    """Return paired-units per 1 token (human, 1e18 token / 1e18 or 1e6 paired adjusted later)."""
    token_l = token.lower()
    paired_l = paired.lower()
    token_is_token0 = int(token_l, 16) < int(paired_l, 16)
    if sqrt_price_x96 is not None:
        p = sqrt_price_x96_to_price(sqrt_price_x96)  # token1 per token0 in raw wei terms
        return p if token_is_token0 else (1.0 / p if p > 0 else 0.0)
    # Fallback from configured tick.
    p = tick_to_price(tick_if_token0_is_clanker)  # assumes token0=clanker
    if token_is_token0:
        return p
    return 1.0 / p if p > 0 else 0.0


def paired_decimals(paired: str) -> int:
    p = paired.lower()
    if p == USDC:
        return 6
    return 18


def paired_usd(paired: str, eth_usd: float) -> float:
    p = paired.lower()
    if p == USDC:
        return 1.0
    if p == WETH:
        return eth_usd
    return float("nan")


@dataclass
class SwapPoint:
    block_number: int
    log_index: int
    sqrt_price_x96: int
    amount0: int
    amount1: int
    liquidity: int
    tick: int
    fee: int
    sender: str


def decode_swap(log: dict[str, Any]) -> SwapPoint:
    data = decode(
        ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
        bytes.fromhex(log["data"][2:]),
    )
    sender = "0x" + log["topics"][2][-40:]
    return SwapPoint(
        block_number=int(log["blockNumber"], 16),
        log_index=int(log["logIndex"], 16),
        amount0=int(data[0]),
        amount1=int(data[1]),
        sqrt_price_x96=int(data[2]),
        liquidity=int(data[3]),
        tick=int(data[4]),
        fee=int(data[5]),
        sender=sender.lower(),
    )


def collect_launches(rpc: PublicRpcClient, from_block: int, to_block: int, chunk: int = 4000) -> list[dict[str, Any]]:
    decoder = ClankerLaunchDecoder()
    out: list[dict[str, Any]] = []
    b = from_block
    while b <= to_block:
        e = min(b + chunk - 1, to_block)
        try:
            logs = rpc.call(
                "eth_getLogs",
                [
                    {
                        "fromBlock": hex(b),
                        "toBlock": hex(e),
                        "address": FACTORY_V4,
                        "topics": [TOKEN_CREATED_TOPIC0],
                    }
                ],
                block_range=[b, e],
                address=FACTORY_V4,
                topic0=TOKEN_CREATED_TOPIC0,
            )
        except RuntimeError as exc:
            if "413" in str(exc) or "Payload" in str(exc):
                chunk = max(500, chunk // 2)
                continue
            raise
        for lg in logs:
            res = decoder.decode_log(lg)
            if res.status == "OK" and res.record is not None:
                out.append({"raw_log": lg, "record": res.record.to_dict()})
        b = e + 1
        time.sleep(0.05)
    out.sort(key=lambda x: (x["record"]["block_number"], x["record"]["log_index"]))
    return out


def fetch_swaps(
    rpc: PublicRpcClient,
    pool_id: str,
    start: int,
    end: int,
    *,
    endpoints: Optional[list[str]] = None,
    chunk: int = 2_500,
) -> list[SwapPoint]:
    """Fetch Swap logs for one poolId using small fixed chunks (public RPCs 413 easily)."""
    points: list[SwapPoint] = []
    b = start
    ep_i = 0
    while b <= end:
        e = min(b + chunk - 1, end)
        try:
            logs = rpc.call(
                "eth_getLogs",
                [
                    {
                        "fromBlock": hex(b),
                        "toBlock": hex(e),
                        "address": POOL_MANAGER,
                        "topics": [SWAP_TOPIC0, pool_id],
                    }
                ],
                block_range=[b, e],
                address=POOL_MANAGER,
                topic0=SWAP_TOPIC0,
            )
        except RuntimeError as exc:
            msg = str(exc).lower()
            if endpoints:
                ep_i = (ep_i + 1) % len(endpoints)
                rpc.endpoint = endpoints[ep_i]
            if ("413" in msg or "payload" in msg or "too large" in msg) and chunk > 1_000:
                chunk = max(1_000, chunk // 2)
                continue
            # skip failed chunk
            b = e + 1
            time.sleep(0.1)
            continue
        for lg in logs:
            try:
                points.append(decode_swap(lg))
            except Exception:  # noqa: BLE001
                continue
        b = e + 1
        time.sleep(0.03)
    points.sort(key=lambda p: (p.block_number, p.log_index))
    return points


def price_at_or_after(swaps: list[SwapPoint], block: int, token: str, paired: str, starting_tick: int) -> tuple[Optional[float], str]:
    for s in swaps:
        if s.block_number >= block:
            px = token_price_in_paired(token, paired, starting_tick, s.sqrt_price_x96)
            return px, "swap_sqrtPriceX96"
    # no future swap — try last swap before as stale mark (not valid for entry)
    return None, "NO_PRICE"


def price_at_or_before(swaps: list[SwapPoint], block: int, token: str, paired: str, starting_tick: int) -> tuple[Optional[float], str]:
    chosen = None
    for s in swaps:
        if s.block_number <= block:
            chosen = s
        else:
            break
    if chosen is None:
        return None, "NO_PRICE"
    px = token_price_in_paired(token, paired, starting_tick, chosen.sqrt_price_x96)
    return px, "swap_sqrtPriceX96"


def gas_cost_usd(gas_price_wei: int, eth_usd: float, scenario: str) -> float:
    cfg = COST[scenario]
    gas = cfg["gas_units"] * cfg["gas_mult"]
    eth = gas * gas_price_wei / 1e18
    return eth * eth_usd


def trade_net_pnl(
    *,
    entry_px_paired_per_token: float,
    exit_px_paired_per_token: float,
    paired: str,
    notional_usd: float,
    eth_usd: float,
    gas_price_wei: int,
    scenario: str,
    entered: bool,
    exited: bool,
) -> dict[str, Any]:
    """Round-trip PnL in USD; fail-closed on missing fills."""
    pud = paired_usd(paired, eth_usd)
    if math.isnan(pud):
        return {"net_pnl_usd": 0.0, "status": "UNKNOWN_PAIR_USD", "return_pct": 0.0}

    dec = paired_decimals(paired)
    # entry_px is paired_wei_units per token_wei roughly from univ3 math;
    # normalize: price_paired_human per token_human ≈ entry_px * 10^(token_dec-paired_dec) with token_dec=18
    scale = 10 ** (18 - dec)
    entry_human = entry_px_paired_per_token * scale  # paired human per 1 token
    exit_human = exit_px_paired_per_token * scale

    cfg = COST[scenario]
    g_entry = gas_cost_usd(gas_price_wei, eth_usd, scenario)
    g_exit = gas_cost_usd(gas_price_wei, eth_usd, scenario)

    if not entered:
        return {
            "net_pnl_usd": -g_entry,
            "status": "NO_ENTRY",
            "return_pct": -g_entry / notional_usd,
            "entry_human": entry_human,
            "exit_human": exit_human,
        }

    # Buy tokens with notional in paired terms.
    paired_in = notional_usd / pud
    # worsen entry, improve-worse exit
    buy_px = entry_human * (1.0 + cfg["slippage"] + cfg["impact"])
    if buy_px <= 0:
        return {"net_pnl_usd": -g_entry, "status": "BAD_ENTRY_PX", "return_pct": -1.0}
    tokens = paired_in / buy_px
    pool_fee = 0.0  # fee partly inside swap price; add explicit buffer in stressed via impact

    if not exited or exit_human <= 0:
        # inventory worthless at horizon
        proceeds = 0.0
        status = "NO_EXIT_MARK_ZERO"
    else:
        sell_px = exit_human * (1.0 - cfg["slippage"] - cfg["impact"])
        proceeds = tokens * max(sell_px, 0.0)
        status = "FILLED"

    net_paired = proceeds - paired_in
    net_usd = net_paired * pud - g_entry - g_exit - pool_fee
    return {
        "net_pnl_usd": net_usd,
        "status": status,
        "return_pct": net_usd / notional_usd,
        "entry_human": entry_human,
        "exit_human": exit_human,
        "tokens": tokens,
        "gas_entry_usd": g_entry,
        "gas_exit_usd": g_exit,
    }


def bootstrap_ci(values: list[float], n: int = 400, alpha: float = 0.05) -> tuple[float, float]:
    if not values:
        return (float("nan"), float("nan"))
    rng = random.Random(42)
    means = []
    for _ in range(n):
        sample = [values[rng.randrange(len(values))] for _ in range(len(values))]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int(alpha / 2 * n)]
    hi = means[int((1 - alpha / 2) * n) - 1]
    return lo, hi


def summarize(pnls: list[float]) -> dict[str, Any]:
    if not pnls:
        return {"n": 0}
    xs = sorted(pnls)
    n = len(xs)
    mean = sum(xs) / n
    wins = sum(1 for x in xs if x > 0)
    losses = [x for x in xs if x < 0]
    gains = [x for x in xs if x > 0]
    profit_factor = (sum(gains) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf") if gains else float("nan")
    # approximate max drawdown on cumulative sum in time order provided
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for x in pnls:
        cum += x
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    lo, hi = bootstrap_ci(pnls)

    def pct(p: float) -> float:
        if n == 1:
            return xs[0]
        i = (n - 1) * p
        lo_i = int(math.floor(i))
        hi_i = int(math.ceil(i))
        if lo_i == hi_i:
            return xs[lo_i]
        return xs[lo_i] * (hi_i - i) + xs[hi_i] * (i - lo_i)

    return {
        "n": n,
        "mean": mean,
        "median": statistics.median(xs),
        "win_rate": wins / n,
        "p10": pct(0.10),
        "p25": pct(0.25),
        "p75": pct(0.75),
        "p90": pct(0.90),
        "max_single_loss": min(xs),
        "max_drawdown_approx": mdd,
        "profit_factor": profit_factor,
        "expected_net_pnl": mean,
        "bootstrap_mean_ci95": [lo, hi],
        "sum": sum(xs),
        "top1_contribution": max(xs) / sum(xs) if sum(xs) != 0 else float("nan"),
    }


def without_top_k(pnls: list[float], k: int) -> list[float]:
    if len(pnls) <= k:
        return []
    # remove k largest winners
    idxs = sorted(range(len(pnls)), key=lambda i: pnls[i], reverse=True)
    drop = set(idxs[:k])
    return [pnls[i] for i in range(len(pnls)) if i not in drop]


def main() -> int:
    run_id = utc_now()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)

    # Stick to the official public Base endpoint; aggregators 403/rate-limit under getLogs load.
    endpoints = ["https://mainnet.base.org"]
    rpc = PublicRpcClient(endpoints[0], max_calls=1000, timeout_sec=45.0, retry_limit=1)
    latest = None
    for ep in endpoints:
        rpc.endpoint = ep
        try:
            latest = int(rpc.call("eth_blockNumber", []), 16)
            break
        except RuntimeError:
            continue
    if latest is None:
        raise SystemExit("no_public_rpc_available")
    # Calibrate block time from two blocks ~1 day apart if possible.
    b1 = rpc.call("eth_getBlockByNumber", [hex(latest), False])
    b0 = rpc.call("eth_getBlockByNumber", [hex(max(1, latest - BLOCKS_PER_DAY)), False])
    dt = int(b1["timestamp"], 16) - int(b0["timestamp"], 16)
    block_seconds = dt / BLOCKS_PER_DAY if dt > 0 else BASE_BLOCK_SECONDS
    blocks_per_minute = max(1, int(round(60.0 / block_seconds)))
    eth_usd = 1820.0  # study freeze; also note UNKNOWN if feed unavailable
    gas_price = int(b1.get("baseFeePerGas") or "0x3b9aca00", 16)  # fallback 1 gwei

    # Launch window ends 24h before tip so all exit horizons exist.
    buffer_24h = int(round(24 * 3600 / block_seconds))
    launch_end = latest - buffer_24h
    # Resume sealed launch sample if present and large enough.
    prior_path = EVIDENCE / "launches.json"
    launches: list[dict[str, Any]] = []
    launch_start = 0
    if prior_path.exists():
        prior = json.loads(prior_path.read_text())
        if int(prior.get("n") or 0) >= 100 and prior.get("launches"):
            launches = prior["launches"]
            # Keep a completable contiguous sample under public RPC budget.
            if len(launches) > 120:
                launches = launches[-120:]
            launch_start = launches[0]["record"]["block_number"]
            launch_end = int(prior["to_block"])
            print(f"resuming launches n={len(launches)} window=[{launch_start},{launch_end}]")

    if len(launches) < 100:
        target_n = 250
        factory_from = 31526699
        launch_start = max(factory_from, launch_end - 15_000)
        while True:
            launches = collect_launches(rpc, launch_start, launch_end, chunk=3500)
            if len(launches) >= target_n:
                break
            if launch_start <= factory_from or rpc.calls > 80:
                break
            launch_start = max(factory_from, launch_start - 10_000)

        if len(launches) > 250:
            # Most recent contiguous 250 (time-ordered; no success cherry-pick).
            launches = launches[-250:]
            launch_start = launches[0]["record"]["block_number"]

        write_json(
            EVIDENCE / "launches.json",
            {
                "run_id": run_id,
                "from_block": launch_start,
                "to_block": launch_end,
                "latest_at_run": latest,
                "n": len(launches),
                "launches": launches,
            },
        )

    if len(launches) < 100:
        verdict = "INSUFFICIENT_MARKET_DATA"
        write_reports_insufficient(run_id, rpc, launches, launch_start, launch_end, latest, verdict, block_seconds)
        return 0

    # Fetch swaps per launch for [launch, launch+24h].
    swap_store: dict[str, list[dict[str, Any]]] = {}
    feature_rows = []
    label_rows = []

    # Creator history for H2
    admin_history: dict[str, list[int]] = defaultdict(list)
    sender_history: dict[str, list[int]] = defaultdict(list)

    for i, item in enumerate(launches):
        if i > 0 and i % 20 == 0:
            print(f"progress {i}/{len(launches)} rpc_calls={rpc.calls} fails={sum(1 for x in rpc.journal if not x.ok)}")
            time.sleep(0.25)
        rec = item["record"]
        pool_id = rec["pool_id"]
        lb = rec["block_number"]
        end_b = lb + buffer_24h
        # Probe first ~5k blocks; only scan full 24h when the pool actually trades.
        probe_end = min(end_b, lb + 4_999)
        swaps = fetch_swaps(rpc, pool_id, lb, probe_end, endpoints=endpoints, chunk=2_500)
        if swaps and probe_end < end_b:
            swaps.extend(
                fetch_swaps(rpc, pool_id, probe_end + 1, end_b, endpoints=endpoints, chunk=2_500)
            )
            swaps.sort(key=lambda p: (p.block_number, p.log_index))
        elif not swaps and probe_end < end_b:
            # one sparse late check so silent 24h activity is not assumed away without evidence
            late_start = max(probe_end + 1, end_b - 2_499)
            late = fetch_swaps(rpc, pool_id, late_start, end_b, endpoints=endpoints, chunk=2_500)
            if late:
                # backfill middle if late activity exists
                mid = fetch_swaps(
                    rpc, pool_id, probe_end + 1, late_start - 1, endpoints=endpoints, chunk=2_500
                )
                swaps = mid + late
                swaps.sort(key=lambda p: (p.block_number, p.log_index))
        swap_store[rec["token_address"]] = [asdict(s) for s in swaps]

        # Features at primary entry (launch+3): use swaps with block < entry
        entry_block = lb + PRIMARY_ENTRY_DELAY_BLOCKS
        pre = [s for s in swaps if s.block_number < entry_block]
        # buy heuristic: token is token0 -> amount0<0 means token out (buy token); else amount1<0
        token = rec["token_address"]
        paired = rec["paired_token"]
        token_is_token0 = int(token, 16) < int(paired, 16)
        buy_vol = 0.0
        sell_vol = 0.0
        buyers: dict[str, float] = defaultdict(float)
        max_single = 0.0
        for s in pre:
            # paired amount spent to buy token
            if token_is_token0:
                # buying token0 => amount0 negative (out), amount1 positive (in)
                if s.amount0 < 0 and s.amount1 > 0:
                    notional = abs(s.amount1)
                    buy_vol += notional
                    buyers[s.sender] += notional
                    max_single = max(max_single, notional)
                elif s.amount0 > 0 and s.amount1 < 0:
                    sell_vol += abs(s.amount1)
            else:
                if s.amount1 < 0 and s.amount0 > 0:
                    notional = abs(s.amount0)
                    buy_vol += notional
                    buyers[s.sender] += notional
                    max_single = max(max_single, notional)
                elif s.amount1 > 0 and s.amount0 < 0:
                    sell_vol += abs(s.amount0)

        gross = buy_vol + sell_vol
        uniq = len(buyers)
        top_vals = sorted(buyers.values(), reverse=True)
        max_share = (top_vals[0] / buy_vol) if buy_vol > 0 else 0.0
        top3_share = (sum(top_vals[:3]) / buy_vol) if buy_vol > 0 else 0.0
        imbalance = ((buy_vol - sell_vol) / gross) if gross > 0 else 0.0

        # H3 window: first 5 blocks inclusive after launch, entry at +6
        h3_end = lb + 5
        h3_swaps = [s for s in swaps if lb <= s.block_number <= h3_end]
        h3_buy = h3_sell = 0.0
        h3_max = 0.0
        for s in h3_swaps:
            if token_is_token0:
                if s.amount0 < 0 and s.amount1 > 0:
                    h3_buy += abs(s.amount1)
                    h3_max = max(h3_max, abs(s.amount1))
                elif s.amount0 > 0 and s.amount1 < 0:
                    h3_sell += abs(s.amount1)
            else:
                if s.amount1 < 0 and s.amount0 > 0:
                    h3_buy += abs(s.amount0)
                    h3_max = max(h3_max, abs(s.amount0))
                elif s.amount1 > 0 and s.amount0 < 0:
                    h3_sell += abs(s.amount0)
        h3_gross = h3_buy + h3_sell
        h3_imb = ((h3_buy - h3_sell) / h3_gross) if h3_gross > 0 else 0.0
        h3_extreme = (h3_max / h3_buy) if h3_buy > 0 else 1.0

        prior_admin = sum(1 for b in admin_history[rec["token_admin"]] if b < lb)
        prior_sender = sum(1 for b in sender_history[rec["msg_sender"]] if b < lb)
        admin_history[rec["token_admin"]].append(lb)
        sender_history[rec["msg_sender"]].append(lb)

        feature_rows.append(
            {
                "token": token,
                "block_number": lb,
                "log_index": rec["log_index"],
                "unique_buyers_pre_entry": uniq,
                "max_buyer_share": max_share,
                "top3_buyer_share": top3_share,
                "buy_sell_imbalance_pre_entry": imbalance,
                "prior_launches_admin": prior_admin,
                "prior_launches_sender": prior_sender,
                "h3_net_imbalance": h3_imb,
                "h3_extreme_share": h3_extreme,
                "h3_buy_vol": h3_buy,
                "n_swaps_24h": len(swaps),
                "paired_token": paired,
            }
        )

        # Labels for multiple delays/horizons/scenarios/notionals — store primary densely, others summary.
        entry_delays = {"1b": 1, "3b": 3, "5b": 5, "30s": max(1, int(round(30 / block_seconds))), "60s": max(1, int(round(60 / block_seconds)))}
        exit_horizons = {
            "5m": 5 * blocks_per_minute,
            "30m": 30 * blocks_per_minute,
            "2h": 120 * blocks_per_minute,
            "24h": buffer_24h,
        }
        row_labels: dict[str, Any] = {"token": token, "block_number": lb}
        for dname, dblocks in entry_delays.items():
            eb = lb + dblocks
            entry_px, entry_src = price_at_or_after(swaps, eb, token, paired, rec["starting_tick"])
            for hname, hblocks in exit_horizons.items():
                xb = eb + hblocks
                exit_px, exit_src = price_at_or_before(swaps, xb, token, paired, rec["starting_tick"])
                # If exit mark missing but entry existed, mark zero
                for scenario in COST:
                    for notional in (100.0, 250.0, 500.0):
                        key = f"{dname}|{hname}|{scenario}|{int(notional)}"
                        if entry_px is None:
                            pnl = trade_net_pnl(
                                entry_px_paired_per_token=0.0,
                                exit_px_paired_per_token=0.0,
                                paired=paired,
                                notional_usd=notional,
                                eth_usd=eth_usd,
                                gas_price_wei=gas_price,
                                scenario=scenario,
                                entered=False,
                                exited=False,
                            )
                        else:
                            pnl = trade_net_pnl(
                                entry_px_paired_per_token=entry_px,
                                exit_px_paired_per_token=exit_px or 0.0,
                                paired=paired,
                                notional_usd=notional,
                                eth_usd=eth_usd,
                                gas_price_wei=gas_price,
                                scenario=scenario,
                                entered=True,
                                exited=exit_px is not None and exit_px > 0,
                            )
                        row_labels[key] = {
                            **pnl,
                            "entry_src": entry_src,
                            "exit_src": exit_src if exit_px is not None else "NO_PRICE",
                            "entry_block": eb,
                            "exit_block": xb,
                        }
        label_rows.append(row_labels)
        if rpc.calls >= 950:
            break

    # If we broke early due to RPC, truncate features/labels consistently
    n_done = len(feature_rows)
    launches = launches[:n_done]

    write_json(EVIDENCE / "swaps_by_token.json", {"n_tokens": len(swap_store), "swaps": swap_store})
    write_json(EVIDENCE / "features.json", {"n": len(feature_rows), "rows": feature_rows})
    write_json(EVIDENCE / "labels_primary_grid.json", {"n": len(label_rows), "rows": label_rows})
    write_json(
        EVIDENCE / "rpc_journal.json",
        {
            "endpoint": rpc.endpoint,
            "endpoint_class": rpc.endpoint_class,
            "calls": rpc.calls,
            "response_bytes_total": rpc.response_bytes_total,
            "journal": [asdict(j) for j in rpc.journal],
        },
    )

    # Discovery / holdout split
    n = len(feature_rows)
    cut = int(n * 0.60)
    discovery_idx = list(range(cut))
    holdout_idx = list(range(cut, n))

    primary_key = f"3b|30m|base|{int(PRIMARY_NOTIONAL_USD)}"

    def series(idxs: list[int], key: str = primary_key) -> list[float]:
        return [float(label_rows[i][key]["net_pnl_usd"]) for i in idxs]

    # Thresholds from discovery only
    disc_feats = [feature_rows[i] for i in discovery_idx]
    # H1: unique buyers >= median discovery AND max_buyer_share <= median discovery
    uniq_med = statistics.median([f["unique_buyers_pre_entry"] for f in disc_feats]) if disc_feats else 0
    conc_med = statistics.median([f["max_buyer_share"] for f in disc_feats]) if disc_feats else 1
    # H3: imbalance >= 75th pct discovery and extreme_share <= 75th pct
    imb_vals = sorted(f["h3_net_imbalance"] for f in disc_feats)
    ext_vals = sorted(f["h3_extreme_share"] for f in disc_feats)

    def pct_val(vals: list[float], p: float) -> float:
        if not vals:
            return 0.0
        i = int(math.floor((len(vals) - 1) * p))
        return vals[i]

    h3_imb_cut = pct_val(imb_vals, 0.75)
    h3_ext_cut = pct_val(ext_vals, 0.75)

    def mask_h1(i: int) -> bool:
        f = feature_rows[i]
        return f["unique_buyers_pre_entry"] >= uniq_med and f["max_buyer_share"] <= conc_med and f["unique_buyers_pre_entry"] > 0

    def mask_h2(i: int) -> bool:
        f = feature_rows[i]
        return f["prior_launches_admin"] >= 1 or f["prior_launches_sender"] >= 1

    def mask_h3(i: int) -> bool:
        f = feature_rows[i]
        return f["h3_net_imbalance"] >= h3_imb_cut and f["h3_extreme_share"] <= h3_ext_cut and f["h3_buy_vol"] > 0

    thresholds = {
        "h1_unique_buyers_ge": uniq_med,
        "h1_max_buyer_share_le": conc_med,
        "h3_imbalance_ge": h3_imb_cut,
        "h3_extreme_share_le": h3_ext_cut,
        "primary_key": primary_key,
    }
    write_json(EVIDENCE / "discovery_thresholds.json", thresholds)

    def eval_cohort(idxs: list[int], mask_fn) -> dict[str, Any]:
        selected = [i for i in idxs if mask_fn(i)]
        pnls = series(selected)
        cov = len(selected) / len(idxs) if idxs else 0.0
        summary = summarize(pnls)
        summary["coverage"] = cov
        summary["n_selected"] = len(selected)
        summary["without_top1"] = summarize(without_top_k(pnls, 1))
        summary["without_top3"] = summarize(without_top_k(pnls, 3))
        return summary

    results = {
        "meta": {
            "run_id": run_id,
            "n_launches": n,
            "discovery_n": len(discovery_idx),
            "holdout_n": len(holdout_idx),
            "launch_from": launch_start,
            "launch_to": launch_end,
            "latest": latest,
            "block_seconds_est": block_seconds,
            "eth_usd_freeze": eth_usd,
            "gas_price_wei": gas_price,
            "rpc_calls": rpc.calls,
            "rpc_response_bytes": rpc.response_bytes_total,
            "thresholds": thresholds,
        },
        "discovery": {
            "buy_all": summarize(series(discovery_idx)),
            "H1": eval_cohort(discovery_idx, mask_h1),
            "H2": eval_cohort(discovery_idx, mask_h2),
            "H3": eval_cohort(discovery_idx, mask_h3),
        },
        "holdout": {
            "buy_all": summarize(series(holdout_idx)),
            "H1": eval_cohort(holdout_idx, mask_h1),
            "H2": eval_cohort(holdout_idx, mask_h2),
            "H3": eval_cohort(holdout_idx, mask_h3),
        },
    }

    # Sensitivity on holdout for entry delays / scenarios / notionals
    sensitivity = {}
    for dname in ("1b", "3b", "5b", "30s", "60s"):
        for scenario in ("optimistic", "base", "stressed"):
            for notional in (100, 250, 500):
                key = f"{dname}|30m|{scenario}|{notional}"
                sensitivity[key] = {
                    "buy_all": summarize(series(holdout_idx, key)),
                    "H1": summarize([float(label_rows[i][key]["net_pnl_usd"]) for i in holdout_idx if mask_h1(i)]),
                    "H2": summarize([float(label_rows[i][key]["net_pnl_usd"]) for i in holdout_idx if mask_h2(i)]),
                    "H3": summarize([float(label_rows[i][key]["net_pnl_usd"]) for i in holdout_idx if mask_h3(i)]),
                }
    results["holdout_sensitivity_30m"] = sensitivity
    write_json(EVIDENCE / "cohort_results.json", results)

    verdict = adjudicate(results)
    strongest_counter = find_counterexample(label_rows, feature_rows, holdout_idx, primary_key)

    write_markdown_reports(run_id, results, verdict, strongest_counter, launches, rpc, block_seconds)
    write_handoff(run_id, results, verdict, strongest_counter)
    print("VERDICT", verdict)
    print("launches", n, "rpc", rpc.calls, "bytes", rpc.response_bytes_total)
    return 0


def adjudicate(results: dict[str, Any]) -> str:
    hold = results["holdout"]
    # EDGE_SUPPORTED if any hypothesis meets gates on holdout base primary
    supported = False
    any_positive_hyp = False
    for name in ("H1", "H2", "H3"):
        h = hold[name]
        if h.get("n_selected", 0) < 10:
            continue
        mean = h.get("expected_net_pnl", 0.0)
        med = h.get("median", 0.0)
        w1 = h.get("without_top1", {}).get("expected_net_pnl", float("nan"))
        if mean > 0 and med >= -0.05 * 250 and (isinstance(w1, float) and w1 > 0):
            any_positive_hyp = True
            # check sensitivity: at least two entry delays positive for this hyp under base/250
            sens = results.get("holdout_sensitivity_30m", {})
            pos_delays = 0
            for d in ("1b", "3b", "5b", "30s", "60s"):
                key = f"{d}|30m|base|250"
                m = sens.get(key, {}).get(name, {}).get("expected_net_pnl")
                if m is not None and m > 0:
                    pos_delays += 1
            stressed = sens.get("3b|30m|stressed|250", {}).get(name, {}).get("expected_net_pnl")
            if pos_delays >= 2 and stressed is not None and stressed > -50:
                supported = True
    buy_all = hold["buy_all"].get("expected_net_pnl", 0.0)
    if supported:
        return "EDGE_SUPPORTED"
    # If we have enough data but no edge
    if results["meta"]["n_launches"] >= 100:
        return "EDGE_NOT_SUPPORTED"
    return "INSUFFICIENT_MARKET_DATA"


def find_counterexample(label_rows, feature_rows, holdout_idx, primary_key):
    worst = None
    for i in holdout_idx:
        pnl = float(label_rows[i][primary_key]["net_pnl_usd"])
        if worst is None or pnl < worst["net_pnl_usd"]:
            worst = {
                "token": feature_rows[i]["token"],
                "block_number": feature_rows[i]["block_number"],
                "net_pnl_usd": pnl,
                "status": label_rows[i][primary_key].get("status"),
                "features": feature_rows[i],
            }
    return worst


def write_reports_insufficient(run_id, rpc, launches, launch_start, launch_end, latest, verdict, block_seconds):
    body = f"""# Clanker Alpha Feasibility v0.1

```text
verdict = {verdict}
run_id = {run_id}
n_launches = {len(launches)}
window = [{launch_start}, {launch_end}]
latest = {latest}
rpc_calls = {rpc.calls}
```

Sample below minimum (100). No economic edge claim is permitted.
"""
    (REPORTS / "CLANKER_ALPHA_FEASIBILITY_V01.md").write_text(body)
    (REPORTS / "CLANKER_ALPHA_HOLDOUT_RESULTS_V01.md").write_text(
        f"# Holdout\n\nverdict = {verdict}\nn = {len(launches)}\n"
    )
    write_handoff(run_id, {"meta": {"n_launches": len(launches), "rpc_calls": rpc.calls}}, verdict, None)


def write_markdown_reports(run_id, results, verdict, counter, launches, rpc, block_seconds):
    meta = results["meta"]
    h = results["holdout"]
    d = results["discovery"]

    def fmt(s):
        if not s or s.get("n", 0) == 0:
            return "n=0"
        return (
            f"n={s.get('n_selected', s.get('n'))} cov={s.get('coverage', 1):.2f} "
            f"mean={s.get('mean', float('nan')):.4f} med={s.get('median', float('nan')):.4f} "
            f"win={s.get('win_rate', float('nan')):.3f} p10={s.get('p10', float('nan')):.4f} "
            f"p90={s.get('p90', float('nan')):.4f} maxLoss={s.get('max_single_loss', float('nan')):.4f} "
            f"PF={s.get('profit_factor', float('nan'))} CI={s.get('bootstrap_mean_ci95')}"
        )

    feas = f"""# Clanker Alpha Feasibility v0.1

```text
verdict = {verdict}
run_id = {run_id}
factory = {FACTORY_V4}
version = 4
n_launches = {meta['n_launches']}
window_blocks = [{meta['launch_from']}, {meta['launch_to']}]
discovery_n = {meta['discovery_n']}
holdout_n = {meta['holdout_n']}
primary = entry+3 blocks / exit+30m / notional $250 / cost=base
block_seconds_est = {meta['block_seconds_est']:.4f}
eth_usd_freeze = {meta['eth_usd_freeze']}
rpc_calls = {meta['rpc_calls']}
rpc_response_bytes = {meta['rpc_response_bytes']}
```

## Method (short)

- Universe: verified Clanker v4 `TokenCreated` only (see contract dossier).
- Prices: Uniswap v4 PoolManager `Swap` `sqrtPriceX96` marks; missing entry → `-gas`; missing exit after entry → inventory marked 0.
- Costs: optimistic/base/stressed slippage, impact, gas (see `evidence/clanker_alpha_v01/`).
- Split: time-ordered 60% discovery / 40% holdout; thresholds fit on discovery only.
- Hypotheses frozen in `reports/CLANKER_ALPHA_HYPOTHESES_V01.md` before PnL.

## Buy-all (base / primary)

| split | summary |
|---|---|
| discovery | {fmt(d['buy_all'])} |
| holdout | {fmt(h['buy_all'])} |

## Hypotheses on holdout (primary)

| hyp | summary |
|---|---|
| H1 | {fmt(h['H1'])} |
| H2 | {fmt(h['H2'])} |
| H3 | {fmt(h['H3'])} |

## Discovery thresholds (frozen after discovery fit)

```json
{json.dumps(meta['thresholds'], indent=2)}
```

## Strongest counterexample (holdout, primary)

```json
{json.dumps(counter, indent=2)}
```

## Verdict rationale

`{verdict}` under pre-registered gates: holdout base-cost expectancy, median, top-winner robustness, multi-delay positivity, and stressed-cost sanity.

Evidence directory: `evidence/clanker_alpha_v01/`.
"""
    (REPORTS / "CLANKER_ALPHA_FEASIBILITY_V01.md").write_text(feas)

    holdout_md = f"""# Clanker Alpha Holdout Results v0.1

```text
verdict = {verdict}
primary_key = {meta['thresholds']['primary_key']}
holdout_n = {meta['holdout_n']}
```

## Primary (3b / 30m / base / $250)

### Buy all
{fmt(h['buy_all'])}

### H1 breadth vs concentration
{fmt(h['H1'])}
without top1: {fmt(h['H1'].get('without_top1', {}))}
without top3: {fmt(h['H1'].get('without_top3', {}))}

### H2 repeat creator
{fmt(h['H2'])}
without top1: {fmt(h['H2'].get('without_top1', {}))}
without top3: {fmt(h['H2'].get('without_top3', {}))}

### H3 early momentum
{fmt(h['H3'])}
without top1: {fmt(h['H3'].get('without_top1', {}))}
without top3: {fmt(h['H3'].get('without_top3', {}))}

## Discovery (for comparison only; not for verdict)

### Buy all
{fmt(d['buy_all'])}
### H1 {fmt(d['H1'])}
### H2 {fmt(d['H2'])}
### H3 {fmt(d['H3'])}

## Sensitivity (holdout, exit=30m)

See machine-readable `evidence/clanker_alpha_v01/cohort_results.json` → `holdout_sensitivity_30m`.
"""
    (REPORTS / "CLANKER_ALPHA_HOLDOUT_RESULTS_V01.md").write_text(holdout_md)
    write_json(RAW / f"results_{run_id}.json", results)


def write_handoff(run_id, results, verdict, counter):
    meta = results.get("meta", {})
    hold = results.get("holdout", {})
    disc = results.get("discovery", {})

    def g(split, name, field="expected_net_pnl"):
        try:
            return split[name].get(field)
        except Exception:
            return None

    text = f"""# Codex Review Request — Clanker Alpha Feasibility v0.1

Cursor completed the isolated public-RPC Clanker alpha feasibility study.

## Identity

```text
study = CLANKER_ALPHA_FEASIBILITY_STUDY_V01
run_id = {run_id}
factory = {FACTORY_V4}
factory_version = 4
defaults_json_untouched = true
production_clickhouse_queried = false
wallet_or_orders = false
```

## Ten adjudication questions

1. **Factories/versions verified:** Base Clanker v4 `0xE85A59c628F7d27878ACeB4bf3b35733630083a9` (official README + Blockscout verified + live log/ABI match).
2. **Still UNKNOWN:** deployment tx hash; all pre-v4 factories/event layouts.
3. **Sample window:** blocks [{meta.get('launch_from')}, {meta.get('launch_to')}] (24h buffer before tip).
4. **Launches:** n={meta.get('n_launches')} (see evidence); swaps/features/labels recorded per token.
5. **Buy-all holdout expected net PnL (primary base $250):** {g(hold, 'buy_all')}
6. **Hypotheses discovery vs holdout (primary mean):**
   - H1 disc={g(disc,'H1')} hold={g(hold,'H1')}
   - H2 disc={g(disc,'H2')} hold={g(hold,'H2')}
   - H3 disc={g(disc,'H3')} hold={g(hold,'H3')}
7. **Most realistic point:** pre-registered entry+3b / exit+30m / $250 / base cost (sensitivities in evidence).
8. **Costs:** optimistic/base/stressed slippage+impact+gas; marks from Uni v4 Swap sqrtPriceX96; missing entry/exit not dropped.
9. **Strongest counterexample:** {json.dumps(counter, sort_keys=True) if counter else 'n/a'}
10. **Verdict:** `{verdict}`

## Artifacts

- reports/CLANKER_ALPHA_HYPOTHESES_V01.md
- config/clanker_verified_registry.v0.1.json
- reports/CLANKER_CONTRACT_SOURCE_DOSSIER.md
- reports/CLANKER_ALPHA_FEASIBILITY_V01.md
- reports/CLANKER_ALPHA_HOLDOUT_RESULTS_V01.md
- evidence/clanker_alpha_v01/
- golden/clanker_alpha/
- export/CODEX_HANDOFF_CLANKER_ALPHA_V01.md

Please adjudicate PASS / PASS_WITH_CORRECTIONS / FAIL against the economic standard (not test count).
"""
    (EXPORT / "CODEX_HANDOFF_CLANKER_ALPHA_V01.md").write_text(text)


if __name__ == "__main__":
    raise SystemExit(main())
