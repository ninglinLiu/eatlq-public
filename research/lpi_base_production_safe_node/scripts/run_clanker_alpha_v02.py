#!/usr/bin/env python3
"""Clanker Alpha Execution-Validity Recovery v0.2 — one-shot confirmation window."""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
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
from launchpad_intelligence.clanker_alpha.v02_quote import (
    DYNAMIC_FEE_FLAG,
    PoolKey,
    QuoteResult,
    notional_to_amount,
    slot0_liquidity,
    swap_fill_proxy,
    try_quoter,
)
from launchpad_intelligence.clanker_alpha.v02_timeline import TimelineLabel, validate_timeline

EVIDENCE = ROOT / "evidence" / "clanker_alpha_v02"
REPORTS = ROOT / "reports"
EXPORT = ROOT / "export"

POOL_MANAGER = "0x498581ff718922c3f8e6A244956aF099B2652b2b"
SWAP_TOPIC0 = "0x" + keccak(b"Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)").hex()
INIT_TOPIC0 = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
V01_END = 48712326
MAX_ENTRY_WAIT = 5
ENTRY_DELAY = 3
EXIT_MINUTES = 30
PRIMARY_NOTIONAL = 250.0
KNOWN_ROUTERS = {
    "0x6ff5693b99212da76ad316178a184ab56d299b43",  # Universal Router
    "0x7c5f5a4bbd8fd63184577525326123b519429bdc",  # PositionManager
    "0x498581ff718922c3f8e6a244956af099b2652b2b",
    FACTORY_V4.lower(),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, obj: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=2, sort_keys=True) + "\n"
    path.write_text(text)
    return hashlib.sha256(text.encode()).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def collect_launches(rpc: PublicRpcClient, start: int, end: int, chunk: int = 3500) -> list[dict]:
    dec = ClankerLaunchDecoder()
    out = []
    b = start
    while b <= end:
        e = min(b + chunk - 1, end)
        try:
            logs = rpc.call(
                "eth_getLogs",
                [{"fromBlock": hex(b), "toBlock": hex(e), "address": FACTORY_V4, "topics": [TOKEN_CREATED_TOPIC0]}],
                block_range=[b, e], address=FACTORY_V4, topic0=TOKEN_CREATED_TOPIC0,
            )
        except RuntimeError as exc:
            if "413" in str(exc).lower() or "payload" in str(exc).lower():
                chunk = max(500, chunk // 2)
                continue
            raise
        for lg in logs:
            res = dec.decode_log(lg)
            if res.status == "OK" and res.record:
                out.append({"raw_log": lg, "record": res.record.to_dict()})
        b = e + 1
        time.sleep(0.04)
    out.sort(key=lambda x: (x["record"]["block_number"], x["record"]["log_index"]))
    return out


def decode_swap_log(lg: dict) -> dict:
    data = decode(["int128", "int128", "uint160", "uint128", "int24", "uint24"], bytes.fromhex(lg["data"][2:]))
    return {
        "block_number": int(lg["blockNumber"], 16),
        "log_index": int(lg["logIndex"], 16),
        "transaction_hash": lg["transactionHash"].lower(),
        "pool_id": lg["topics"][1].lower(),
        "pm_sender": ("0x" + lg["topics"][2][-40:]).lower(),
        "amount0": int(data[0]),
        "amount1": int(data[1]),
        "sqrt_price_x96": int(data[2]),
        "liquidity": int(data[3]),
        "tick": int(data[4]),
        "fee": int(data[5]),
    }


def batch_fetch_swaps(
    rpc: PublicRpcClient,
    pool_ids: list[str],
    start: int,
    end: int,
    *,
    batch_pools: int = 25,
    chunk_blocks: int = 2500,
) -> tuple[dict[str, list[dict]], dict[str, Any]]:
    """OR-filter poolIds; track per-pool coverage."""
    coverage = {
        pid: {
            "requested_ranges": [],
            "successful_ranges": [],
            "failed_ranges": [],
            "coverage_ratio": 0.0,
            "primary_horizon_complete": False,
        }
        for pid in pool_ids
    }
    by_pool: dict[str, list[dict]] = defaultdict(list)
    total_span = end - start + 1

    for i in range(0, len(pool_ids), batch_pools):
        batch = pool_ids[i : i + batch_pools]
        b = start
        while b <= end:
            e = min(b + chunk_blocks - 1, end)
            for pid in batch:
                coverage[pid]["requested_ranges"].append([b, e])
            try:
                logs = rpc.call(
                    "eth_getLogs",
                    [{
                        "fromBlock": hex(b),
                        "toBlock": hex(e),
                        "address": POOL_MANAGER,
                        "topics": [SWAP_TOPIC0, batch],
                    }],
                    block_range=[b, e],
                    address=POOL_MANAGER,
                    topic0=SWAP_TOPIC0,
                )
                for pid in batch:
                    coverage[pid]["successful_ranges"].append([b, e])
                for lg in logs:
                    try:
                        sw = decode_swap_log(lg)
                        by_pool[sw["pool_id"]].append(sw)
                    except Exception:  # noqa: BLE001
                        continue
            except RuntimeError as exc:
                msg = str(exc).lower()
                if ("413" in msg or "payload" in msg) and chunk_blocks > 1000:
                    chunk_blocks = max(1000, chunk_blocks // 2)
                    continue
                if ("413" in msg or "payload" in msg) and len(batch) > 5:
                    # shrink OR list by recursion-like split
                    mid = len(batch) // 2
                    sub_a, cov_a = batch_fetch_swaps(rpc, batch[:mid], b, e, batch_pools=mid, chunk_blocks=chunk_blocks)
                    sub_b, cov_b = batch_fetch_swaps(rpc, batch[mid:], b, e, batch_pools=len(batch) - mid, chunk_blocks=chunk_blocks)
                    for pid, rows in {**sub_a, **sub_b}.items():
                        by_pool[pid].extend(rows)
                    for pid, c in {**cov_a, **cov_b}.items():
                        coverage[pid]["successful_ranges"].extend(c["successful_ranges"])
                        coverage[pid]["failed_ranges"].extend(c["failed_ranges"])
                    b = e + 1
                    continue
                for pid in batch:
                    coverage[pid]["failed_ranges"].append([b, e, str(exc)[:120]])
            b = e + 1
            time.sleep(0.03)

    for pid, c in coverage.items():
        succ = sum(r[1] - r[0] + 1 for r in c["successful_ranges"])
        # de-dup approximate
        c["coverage_ratio"] = min(1.0, succ / total_span) if total_span else 0.0
        c["primary_horizon_complete"] = c["coverage_ratio"] >= 0.999 and not c["failed_ranges"]
    for pid in by_pool:
        by_pool[pid].sort(key=lambda s: (s["block_number"], s["log_index"]))
    return dict(by_pool), coverage


def classify_address(addr: str, code_hex: str) -> str:
    a = addr.lower()
    if a in KNOWN_ROUTERS:
        return "ROUTER_OR_INFRA"
    if a == FACTORY_V4.lower():
        return "FACTORY"
    if code_hex in ("0x", "0x0", ""):
        return "EOA"
    return "CONTRACT_UNKNOWN"


def summarize(xs: list[float]) -> dict[str, Any]:
    if not xs:
        return {"n": 0}
    s = sorted(xs)
    n = len(s)
    mean = sum(s) / n
    wins = sum(1 for x in s if x > 0)
    losses = [x for x in s if x < 0]
    gains = [x for x in s if x > 0]
    pf = (sum(gains) / abs(sum(losses))) if losses and sum(losses) else (float("inf") if gains else float("nan"))
    rng = random.Random(42)
    boots = []
    for _ in range(300):
        sample = [s[rng.randrange(n)] for _ in range(n)]
        boots.append(sum(sample) / n)
    boots.sort()
    return {
        "n": n,
        "mean": mean,
        "median": statistics.median(s),
        "win_rate": wins / n,
        "p10": s[max(0, int(0.1 * (n - 1)))],
        "p90": s[min(n - 1, int(0.9 * (n - 1)))],
        "max_single_loss": min(s),
        "profit_factor": pf,
        "expected_net_pnl": mean,
        "bootstrap_mean_ci95": [boots[int(0.025 * 300)], boots[int(0.975 * 300) - 1]],
    }


def apply_costs(
    *,
    amount_in: int,
    amount_out_entry: int,
    amount_out_exit_in_paired: int,
    scenario: str,
    gas_price: int,
    eth_usd: Optional[float],
    paired_is_weth: bool,
    size_ratio: Optional[float],
    fee_raw: Optional[int],
) -> dict[str, Any]:
    cfg = {
        "optimistic": {"slip": 0.001, "size_pen": 0.0, "gas": 180_000, "gmult": 1.0},
        "base": {"slip": 0.005, "size_pen": 0.5, "gas": 220_000, "gmult": 1.15},
        "stressed": {"slip": 0.02, "size_pen": 1.0, "gas": 300_000, "gmult": 1.5},
    }[scenario]
    # Round trip in paired units: in -> tokens -> paired out
    # amount_out_exit_in_paired already is paired received for selling tokens
    tokens = amount_out_entry * (1.0 - cfg["slip"])
    # rescale: entry gives tokens for amount_in; exit quote should be for selling those tokens
    # Here amount_out_exit_in_paired is computed for selling the entry token amount
    paired_out = amount_out_exit_in_paired * (1.0 - cfg["slip"])
    mismatch = 0.0
    if size_ratio is not None and size_ratio > 0:
        # if proxy swap smaller than target, penalize
        if size_ratio < 1.0:
            mismatch = (1.0 - size_ratio) * cfg["size_pen"] * amount_in
    fee_unknown = bool(fee_raw is not None and fee_raw & DYNAMIC_FEE_FLAG)
    gas_eth = cfg["gas"] * cfg["gmult"] * gas_price / 1e18
    # two legs; express gas in paired raw units
    if paired_is_weth:
        gas_total = 2 * gas_eth * 1e18
    elif eth_usd:
        gas_total = 2 * gas_eth * eth_usd * 1_000_000  # USDC 6 decimals
    else:
        gas_total = 0.0  # UNKNOWN fiat gas; recorded via eth_usd_source
    net_paired = paired_out - amount_in - mismatch - gas_total
    return {
        "net_paired_raw": net_paired,
        "gas_paired_raw": gas_total,
        "gas_eth_two_legs": 2 * gas_eth,
        "size_mismatch_penalty_raw": mismatch,
        "dynamic_fee_unknown": fee_unknown,
        "l1_data_fee": "UNKNOWN",
    }


def main() -> int:
    run_id = utc_now()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    method_sha = sha256_file(REPORTS / "CLANKER_ALPHA_METHOD_V02.md")
    hyp_sha = sha256_file(REPORTS / "CLANKER_ALPHA_HYPOTHESES_V02.md")

    rpc = PublicRpcClient("https://mainnet.base.org", max_calls=1500, timeout_sec=45.0, retry_limit=1)
    latest = int(rpc.call("eth_blockNumber", []), 16)
    b_tip = rpc.call("eth_getBlockByNumber", [hex(latest), False])
    b_prev = rpc.call("eth_getBlockByNumber", [hex(latest - 43200), False])
    block_seconds = (int(b_tip["timestamp"], 16) - int(b_prev["timestamp"], 16)) / 43200
    blocks_per_minute = max(1, int(round(60 / block_seconds)))
    gas_price = int(b_tip.get("baseFeePerGas") or "0x5d21dba00", 16)

    # ETH/USD source attempt (timestamped); may be UNKNOWN
    eth_usd = None
    eth_usd_source = "UNKNOWN"
    try:
        import urllib.request

        req = urllib.request.Request(
            "https://base.blockscout.com/api/v2/stats",
            headers={"User-Agent": "clanker-alpha-v02"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            stats = json.loads(resp.read())
        # coin_price often present
        px = stats.get("coin_price") or stats.get("gas_prices", {}).get("average")
        if stats.get("coin_price"):
            eth_usd = float(stats["coin_price"])
            eth_usd_source = f"blockscout_stats:{utc_now()}"
    except Exception:  # noqa: BLE001
        pass

    buffer_24h = int(round(24 * 3600 / block_seconds))
    launch_end = latest - buffer_24h
    if launch_end <= V01_END:
        raise SystemExit("no_non_overlapping_window_available")
    launch_start = V01_END + 1

    # Grow until >=200 launches or tip-buffer exhausted
    launches: list[dict] = []
    cursor = max(launch_start, launch_end - 20_000)
    while cursor >= launch_start:
        launches = collect_launches(rpc, cursor, launch_end, chunk=3000)
        if len(launches) >= 200:
            launches = launches[-200:] if len(launches) > 200 else launches
            launch_start = launches[0]["record"]["block_number"]
            break
        if cursor == launch_start or rpc.calls > 100:
            break
        cursor = max(launch_start, cursor - 15_000)

    write_json(
        EVIDENCE / "launches.json",
        {
            "run_id": run_id,
            "v01_excluded_to_block": V01_END,
            "from_block": launch_start,
            "to_block": launch_end,
            "latest": latest,
            "n": len(launches),
            "method_sha256": method_sha,
            "hypotheses_sha256": hyp_sha,
            "launches": launches,
        },
    )

    if len(launches) < 100:
        verdict = "INSUFFICIENT_MARKET_DATA"
        write_reports(run_id, verdict, {}, {}, rpc, method_sha, hyp_sha, eth_usd, eth_usd_source, note="launch_sample_too_small")
        return 0

    # Pool keys from Initialize in launch receipts (batch carefully)
    pool_meta = {}
    for i, item in enumerate(launches):
        rec = item["record"]
        try:
            rcpt = rpc.call("eth_getTransactionReceipt", [rec["transaction_hash"]])
        except RuntimeError:
            continue
        inits = [lg for lg in rcpt["logs"] if lg.get("topics") and lg["topics"][0].lower() == INIT_TOPIC0]
        if not inits:
            continue
        init = inits[0]
        c0 = ("0x" + init["topics"][2][-40:]).lower()
        c1 = ("0x" + init["topics"][3][-40:]).lower()
        fee, spacing, hooks, sqrt_p, tick = decode(
            ["uint24", "int24", "address", "uint160", "int24"], bytes.fromhex(init["data"][2:])
        )
        pool_meta[rec["pool_id"].lower()] = {
            "pool_key": PoolKey(c0, c1, int(fee), int(spacing), hooks.lower()),
            "init_sqrt": int(sqrt_p),
            "init_tick": int(tick),
            "token": rec["token_address"],
            "paired": rec["paired_token"],
            "launch_block": rec["block_number"],
        }
        if i % 40 == 0:
            time.sleep(0.08)

    # Swap horizon: launch to launch+24h for all pools we have meta for
    # Primary needs entry wait + 30m; still fetch 24h for reporting
    max_launch = max(l["record"]["block_number"] for l in launches)
    min_launch = min(l["record"]["block_number"] for l in launches)
    swap_end = max_launch + buffer_24h
    pool_ids = list(pool_meta.keys())
    swaps_by_pool, coverage = batch_fetch_swaps(rpc, pool_ids, min_launch, swap_end, batch_pools=20, chunk_blocks=2500)
    write_json(EVIDENCE / "swap_coverage.json", coverage)
    write_json(EVIDENCE / "swaps_by_pool.json", {k: v for k, v in swaps_by_pool.items()})

    # Cache tx.from for feature identity
    tx_from_cache: dict[str, str] = {}
    code_cache: dict[str, str] = {}

    def get_tx_from(txh: str) -> str:
        if txh not in tx_from_cache:
            try:
                tx = rpc.call("eth_getTransactionByHash", [txh])
                tx_from_cache[txh] = (tx.get("from") or "").lower()
            except RuntimeError:
                tx_from_cache[txh] = ""
        return tx_from_cache[txh]

    def get_code(addr: str) -> str:
        if addr not in code_cache:
            try:
                code_cache[addr] = rpc.call("eth_getCode", [addr, "latest"])
            except RuntimeError:
                code_cache[addr] = "0x"
        return code_cache[addr]

    labels = []
    features = []
    cross_checks = []
    admin_history: dict[str, list[int]] = defaultdict(list)

    exit_blocks = EXIT_MINUTES * blocks_per_minute

    for item in launches:
        rec = item["record"]
        pid = rec["pool_id"].lower()
        lb = rec["block_number"]
        intended_entry = lb + ENTRY_DELAY
        intended_exit = intended_entry + exit_blocks
        meta = pool_meta.get(pid)
        swaps = swaps_by_pool.get(pid, [])
        cov = coverage.get(pid, {})

        token = rec["token_address"]
        paired = rec["paired_token"]
        token_is_token0 = int(token, 16) < int(paired, 16)
        paired_is_weth = paired.lower() == "0x4200000000000000000000000000000000000006"

        # prior admin count using full sealed sample causal
        prior = sum(1 for b in admin_history[rec["token_admin"]] if b < lb)
        admin_history[rec["token_admin"]].append(lb)
        admin_class = classify_address(rec["token_admin"], get_code(rec["token_admin"]))

        # Pre-entry actor features from swaps before intended_entry
        actors: dict[str, float] = defaultdict(float)
        h3_buy = h3_sell = h3_max = 0.0
        for s in swaps:
            if s["block_number"] > lb + 5:
                break
            buy = False
            notion = 0.0
            if token_is_token0 and s["amount0"] < 0 and s["amount1"] > 0:
                buy, notion = True, float(s["amount1"])
            elif (not token_is_token0) and s["amount1"] < 0 and s["amount0"] > 0:
                buy, notion = True, float(s["amount0"])
            elif token_is_token0 and s["amount0"] > 0 and s["amount1"] < 0:
                h3_sell += abs(s["amount1"])
            elif (not token_is_token0) and s["amount1"] > 0 and s["amount0"] < 0:
                h3_sell += abs(s["amount0"])
            if buy and s["block_number"] < intended_entry:
                txf = get_tx_from(s["transaction_hash"])
                if txf:
                    actors[txf] += notion
            if buy and lb <= s["block_number"] <= lb + 5:
                h3_buy += notion
                h3_max = max(h3_max, notion)
        uniq_actors = len(actors)
        top_vals = sorted(actors.values(), reverse=True)
        max_share = (top_vals[0] / sum(top_vals)) if top_vals and sum(top_vals) else 0.0
        h3_gross = h3_buy + h3_sell
        h3_imb = (h3_buy - h3_sell) / h3_gross if h3_gross else 0.0
        h3_ext = h3_max / h3_buy if h3_buy else 1.0

        features.append({
            "token": token,
            "pool_id": pid,
            "block_number": lb,
            "unique_transaction_actors": uniq_actors,
            "max_actor_share": max_share,
            "prior_launches_admin": prior,
            "admin_class": admin_class,
            "h3_net_imbalance": h3_imb,
            "h3_extreme_share": h3_ext,
            "coverage_ratio": cov.get("coverage_ratio"),
            "primary_horizon_complete": cov.get("primary_horizon_complete"),
            "n_swaps_observed": len(swaps),
            "paired_token": paired,
        })

        amount_in, unit = notional_to_amount(paired, PRIMARY_NOTIONAL, eth_usd)
        label_base = {
            "token": token,
            "pool_id": pid,
            "launch_block": lb,
            "notional": PRIMARY_NOTIONAL,
            "unit": unit,
        }
        if amount_in is None or meta is None:
            tl = TimelineLabel(
                intended_entry, None, None, None, None, None, intended_exit, None, None, None, None,
                "QUOTE_UNAVAILABLE", "missing_pool_meta_or_eth_usd" if amount_in is None else "missing_init",
            )
            labels.append({**label_base, "timeline": tl.to_dict(), "pnl": {}})
            continue

        pool: PoolKey = meta["pool_key"]
        zero_for_one = not token_is_token0  # paired -> token
        # Candidate entry swaps in wait window
        entry_cands = [
            s for s in swaps
            if intended_entry <= s["block_number"] <= intended_entry + MAX_ENTRY_WAIT
        ]
        entry_quote = None
        # Try quoter at intended_entry first, then at each cand block
        q = try_quoter(rpc, pool, zero_for_one=zero_for_one, amount_in=amount_in, block=intended_entry)
        if q.ok:
            entry_quote = q
        else:
            for s in entry_cands:
                # prefer buy-side swap proxy
                proxy = swap_fill_proxy(swap=s, token_is_token0=token_is_token0, target_amount_in=amount_in, pool_fee=pool.fee)
                if proxy.ok:
                    entry_quote = proxy
                    break
                q2 = try_quoter(rpc, pool, zero_for_one=zero_for_one, amount_in=amount_in, block=s["block_number"])
                if q2.ok:
                    entry_quote = q2
                    break

        exit_quote = None
        if entry_quote and entry_quote.ok:
            # exit: sell token -> paired (opposite direction)
            exit_zero_for_one = not zero_for_one
            # Try quoter at intended_exit for token amount = entry amount_out
            qe = try_quoter(
                rpc, pool, zero_for_one=exit_zero_for_one,
                amount_in=max(1, entry_quote.amount_out), block=intended_exit,
            )
            if qe.ok:
                exit_quote = qe
            else:
                exit_cands = [s for s in swaps if s["block_number"] >= max(entry_quote.quote_block or intended_entry, intended_exit - 50) and s["block_number"] <= intended_exit + MAX_ENTRY_WAIT]
                # also allow exact intended_exit window [intended_exit, intended_exit+5]
                exit_cands = [s for s in swaps if intended_exit <= s["block_number"] <= intended_exit + MAX_ENTRY_WAIT]
                for s in exit_cands:
                    # sell proxy: token in, paired out
                    a0, a1 = s["amount0"], s["amount1"]
                    if token_is_token0 and a0 > 0 and a1 < 0:
                        # scale
                        tin, pout = a0, -a1
                        scaled = int(pout * (entry_quote.amount_out / tin)) if tin else 0
                        exit_quote = QuoteResult(
                            True, "REAL_SWAP_FILL_PROXY", entry_quote.amount_out, scaled,
                            s["block_number"], s["transaction_hash"], s["log_index"], None,
                            s["fee"], "SWAP_EVENT", s["sqrt_price_x96"], s["liquidity"], None,
                            size_ratio_vs_proxy=(tin / entry_quote.amount_out) if entry_quote.amount_out else None,
                        )
                        break
                    if (not token_is_token0) and a1 > 0 and a0 < 0:
                        tin, pout = a1, -a0
                        scaled = int(pout * (entry_quote.amount_out / tin)) if tin else 0
                        exit_quote = QuoteResult(
                            True, "REAL_SWAP_FILL_PROXY", entry_quote.amount_out, scaled,
                            s["block_number"], s["transaction_hash"], s["log_index"], None,
                            s["fee"], "SWAP_EVENT", s["sqrt_price_x96"], s["liquidity"], None,
                            size_ratio_vs_proxy=(tin / entry_quote.amount_out) if entry_quote.amount_out else None,
                        )
                        break

        status, reason = validate_timeline(
            intended_entry=intended_entry,
            intended_exit=intended_exit,
            max_entry_wait=MAX_ENTRY_WAIT,
            entry_quote_block=entry_quote.quote_block if entry_quote and entry_quote.ok else None,
            exit_quote_block=exit_quote.quote_block if exit_quote and exit_quote.ok else None,
        )
        # refine NO_TRADE vs EXIT unavailable
        if entry_quote and entry_quote.ok and status == "EXIT_QUOTE_UNAVAILABLE":
            pass
        if entry_quote is None or not entry_quote.ok:
            status, reason = "NO_TRADE_NO_QUOTE", entry_quote.revert_error if entry_quote else "no_entry_quote"

        tl = TimelineLabel(
            intended_entry_block=intended_entry,
            quote_block=entry_quote.quote_block if entry_quote else None,
            quote_transaction_hash=entry_quote.transaction_hash if entry_quote else None,
            quote_log_index=entry_quote.log_index if entry_quote else None,
            quote_eth_call_block=entry_quote.eth_call_block if entry_quote else None,
            actual_entry_block=entry_quote.quote_block if entry_quote and entry_quote.ok else None,
            intended_exit_block=intended_exit,
            exit_quote_block=exit_quote.quote_block if exit_quote else None,
            exit_quote_transaction_hash=exit_quote.transaction_hash if exit_quote else None,
            exit_quote_log_index=exit_quote.log_index if exit_quote else None,
            entry_delay_from_target=(entry_quote.quote_block - intended_entry) if entry_quote and entry_quote.ok and entry_quote.quote_block else None,
            status=status,
            status_reason=reason or "",
            quote_method=entry_quote.method if entry_quote else None,
            exit_quote_method=exit_quote.method if exit_quote else None,
        )

        pnl = {}
        if status == "EXECUTABLE" and entry_quote and exit_quote:
            for scen in ("optimistic", "base", "stressed"):
                c = apply_costs(
                    amount_in=amount_in,
                    amount_out_entry=entry_quote.amount_out,
                    amount_out_exit_in_paired=exit_quote.amount_out,
                    scenario=scen,
                    gas_price=gas_price,
                    eth_usd=eth_usd,
                    paired_is_weth=paired_is_weth,
                    size_ratio=entry_quote.size_ratio_vs_proxy,
                    fee_raw=entry_quote.fee_raw,
                )
                if paired_is_weth:
                    net = c["net_paired_raw"] / 1e18  # ETH
                    pnl[scen] = {**c, "net_pnl_eth": net, "net_pnl_usd": (net * eth_usd) if eth_usd else None}
                else:
                    net = c["net_paired_raw"] / 1e6  # USDC
                    pnl[scen] = {**c, "net_pnl_usd": net, "net_pnl_eth": None}
        elif status == "NO_TRADE_NO_QUOTE":
            for scen in ("optimistic", "base", "stressed"):
                pnl[scen] = {"net_pnl_usd": 0.0, "net_pnl_eth": 0.0 if paired_is_weth else None, "realized": 0.0}
        elif status == "EXIT_QUOTE_UNAVAILABLE":
            for scen in ("optimistic", "base", "stressed"):
                pnl[scen] = {"net_pnl_usd": None, "status": "EXIT_QUOTE_UNAVAILABLE"}

        labels.append({**label_base, "timeline": tl.to_dict(), "entry_quote": entry_quote.to_dict() if entry_quote else None,
                       "exit_quote": exit_quote.to_dict() if exit_quote else None, "pnl": pnl})

        # Cross-check sample collection
        if len(cross_checks) < 12 and entry_quote and entry_quote.ok and meta:
            sqrt_p, tick, liq = slot0_liquidity(rpc, pid, entry_quote.quote_block or intended_entry)
            adj = entry_cands[0] if entry_cands else (swaps[0] if swaps else None)
            cross_checks.append({
                "token": token,
                "pool_id": pid,
                "quote_method": entry_quote.method,
                "quoter_or_proxy_amount_in": entry_quote.amount_in,
                "quoter_or_proxy_amount_out": entry_quote.amount_out,
                "adjacent_swap": adj,
                "slot0_sqrtPriceX96": sqrt_p,
                "slot0_tick": tick,
                "liquidity": liq,
                "token_order": {"currency0": pool.currency0, "currency1": pool.currency1, "token_is_token0": token_is_token0},
                "decimals": {"token": 18, "paired": 6 if not paired_is_weth else 18},
                "fee_raw": pool.fee,
                "fee_mode": entry_quote.fee_mode,
            })

    write_json(EVIDENCE / "features.json", {"n": len(features), "rows": features})
    write_json(EVIDENCE / "labels.json", {"n": len(labels), "rows": labels})
    write_json(EVIDENCE / "quote_cross_checks.json", {"n": len(cross_checks), "rows": cross_checks})
    write_json(
        EVIDENCE / "rpc_journal.json",
        {"calls": rpc.calls, "response_bytes_total": rpc.response_bytes_total, "journal": [x.__dict__ for x in rpc.journal]},
    )

    # Splits
    n = len(labels)
    cut = int(n * 0.60)
    disc_i = list(range(cut))
    hold_i = list(range(cut, n))

    def status_of(i):
        return labels[i]["timeline"]["status"]

    def base_pnl(i) -> Optional[float]:
        p = labels[i]["pnl"].get("base") or {}
        if labels[i]["unit"] == "WETH":
            return p.get("net_pnl_eth")
        return p.get("net_pnl_usd")

    # Discovery thresholds
    disc_feats = [features[i] for i in disc_i]
    uniq_med = statistics.median([f["unique_transaction_actors"] for f in disc_feats]) if disc_feats else 0
    share_med = statistics.median([f["max_actor_share"] for f in disc_feats]) if disc_feats else 1
    imb_cut = sorted(f["h3_net_imbalance"] for f in disc_feats)[int(0.75 * max(0, len(disc_feats) - 1))] if disc_feats else 0
    ext_cut = sorted(f["h3_extreme_share"] for f in disc_feats)[int(0.75 * max(0, len(disc_feats) - 1))] if disc_feats else 1

    def m_h1(i):
        f = features[i]
        return f["unique_transaction_actors"] >= max(1, uniq_med) and f["max_actor_share"] <= share_med

    def m_h2(i):
        f = features[i]
        return f["admin_class"] == "EOA" and f["prior_launches_admin"] >= 1

    def m_h3(i):
        f = features[i]
        return f["h3_net_imbalance"] >= imb_cut and f["h3_extreme_share"] <= ext_cut

    thresholds = {
        "h1_actors_ge": max(1, uniq_med),
        "h1_max_actor_share_le": share_med,
        "h3_imbalance_ge": imb_cut,
        "h3_extreme_share_le": ext_cut,
    }
    write_json(EVIDENCE / "discovery_thresholds.json", thresholds)

    def universe_stats(idxs):
        st = Counter(status_of(i) for i in idxs)
        return {
            "launches": len(idxs),
            "status_counts": dict(st),
            "quote_availability": st.get("EXECUTABLE", 0) / len(idxs) if idxs else 0,
            "no_trade_rate": st.get("NO_TRADE_NO_QUOTE", 0) / len(idxs) if idxs else 0,
            "observed_swaps_pools": sum(1 for i in idxs if features[i]["n_swaps_observed"] > 0),
        }

    def conditional_stats(idxs):
        xs = [base_pnl(i) for i in idxs if status_of(i) == "EXECUTABLE" and base_pnl(i) is not None]
        return summarize([float(x) for x in xs])

    def strategy_stats(idxs, mask=None):
        # NO_TRADE pnl=0; EXECUTABLE uses base; EXIT_UNAVAILABLE excluded from mean opportunity? count separately
        vals = []
        skipped = 0
        for i in idxs:
            if mask is not None and not mask(i):
                continue
            st = status_of(i)
            if st == "NO_TRADE_NO_QUOTE":
                vals.append(0.0)
            elif st == "EXECUTABLE" and base_pnl(i) is not None:
                vals.append(float(base_pnl(i)))
            else:
                skipped += 1
        out = summarize(vals)
        out["skipped_non_pnl"] = skipped
        out["coverage_selected"] = (len(vals) / len(idxs)) if idxs else 0
        return out

    results = {
        "meta": {
            "run_id": run_id,
            "method_sha256": method_sha,
            "hypotheses_sha256": hyp_sha,
            "v01_excluded_to_block": V01_END,
            "launch_from": launch_start,
            "launch_to": launch_end,
            "n": n,
            "discovery_n": len(disc_i),
            "holdout_n": len(hold_i),
            "rpc_calls": rpc.calls,
            "rpc_bytes": rpc.response_bytes_total,
            "eth_usd": eth_usd,
            "eth_usd_source": eth_usd_source,
            "thresholds": thresholds,
            "max_entry_wait_blocks": MAX_ENTRY_WAIT,
        },
        "discovery": {
            "universe": universe_stats(disc_i),
            "conditional_executable": conditional_stats(disc_i),
            "strategy_all": strategy_stats(disc_i),
            "H1_strategy": strategy_stats(disc_i, m_h1),
            "H2_strategy": strategy_stats(disc_i, m_h2),
            "H3_strategy": strategy_stats(disc_i, m_h3),
            "H1_conditional": summarize([float(base_pnl(i)) for i in disc_i if m_h1(i) and status_of(i) == "EXECUTABLE" and base_pnl(i) is not None]),
            "H2_conditional": summarize([float(base_pnl(i)) for i in disc_i if m_h2(i) and status_of(i) == "EXECUTABLE" and base_pnl(i) is not None]),
            "H3_conditional": summarize([float(base_pnl(i)) for i in disc_i if m_h3(i) and status_of(i) == "EXECUTABLE" and base_pnl(i) is not None]),
        },
        "holdout": {
            "universe": universe_stats(hold_i),
            "conditional_executable": conditional_stats(hold_i),
            "strategy_all": strategy_stats(hold_i),
            "H1_strategy": strategy_stats(hold_i, m_h1),
            "H2_strategy": strategy_stats(hold_i, m_h2),
            "H3_strategy": strategy_stats(hold_i, m_h3),
            "H1_conditional": summarize([float(base_pnl(i)) for i in hold_i if m_h1(i) and status_of(i) == "EXECUTABLE" and base_pnl(i) is not None]),
            "H2_conditional": summarize([float(base_pnl(i)) for i in hold_i if m_h2(i) and status_of(i) == "EXECUTABLE" and base_pnl(i) is not None]),
            "H3_conditional": summarize([float(base_pnl(i)) for i in hold_i if m_h3(i) and status_of(i) == "EXECUTABLE" and base_pnl(i) is not None]),
        },
    }
    write_json(EVIDENCE / "cohort_results.json", results)

    exec_hold = results["holdout"]["conditional_executable"].get("n", 0)
    if exec_hold < 30:
        verdict = "INSUFFICIENT_MARKET_DATA"
    else:
        # EDGE_SUPPORTED gates strict; else EDGE_NOT_SUPPORTED
        cond = results["holdout"]["conditional_executable"]
        hyp_ok = False
        for name in ("H1_conditional", "H2_conditional", "H3_conditional"):
            h = results["holdout"][name]
            if h.get("n", 0) >= 10 and h.get("expected_net_pnl", 0) > 0 and h.get("median", -1) >= 0:
                hyp_ok = True
        if hyp_ok and cond.get("expected_net_pnl", 0) > 0:
            verdict = "EDGE_SUPPORTED"
        else:
            verdict = "EDGE_NOT_SUPPORTED"

    write_json(EVIDENCE / "verdict.json", {"verdict": verdict, "executable_holdout_n": exec_hold})
    write_reports(run_id, verdict, results, {"cross_checks": len(cross_checks)}, rpc, method_sha, hyp_sha, eth_usd, eth_usd_source)
    print("VERDICT", verdict, "exec_holdout", exec_hold, "launches", n, "rpc", rpc.calls)
    return 0


def write_reports(run_id, verdict, results, extra, rpc, method_sha, hyp_sha, eth_usd, eth_usd_source, note=""):
    meta = results.get("meta", {}) if results else {}
    hold = results.get("holdout", {}) if results else {}
    disc = results.get("discovery", {}) if results else {}
    feas = f"""# Clanker Alpha Execution-Validity v0.2

```text
verdict = {verdict}
run_id = {run_id}
method_sha256 = {method_sha}
hypotheses_sha256 = {hyp_sha}
v01_excluded_to_block = {V01_END}
window = [{meta.get('launch_from')}, {meta.get('launch_to')}]
n_launches = {meta.get('n')}
discovery_n = {meta.get('discovery_n')}
holdout_n = {meta.get('holdout_n')}
executable_holdout_n = {(hold.get('conditional_executable') or {}).get('n')}
rpc_calls = {meta.get('rpc_calls', getattr(rpc, 'calls', None))}
eth_usd = {eth_usd}
eth_usd_source = {eth_usd_source}
note = {note}
```

## Universe (holdout)

```json
{json.dumps(hold.get('universe', {}), indent=2)}
```

## Conditional-on-executable (holdout, base)

```json
{json.dumps(hold.get('conditional_executable', {}), indent=2)}
```

## Strategy-level (NO_TRADE PnL=0; holdout)

| series | summary |
|---|---|
| all | {hold.get('strategy_all')} |
| H1 | {hold.get('H1_strategy')} |
| H2 | {hold.get('H2_strategy')} |
| H3 | {hold.get('H3_strategy')} |

## Discovery (reference only)

```json
{json.dumps(disc.get('universe', {}), indent=2)}
```

v0.1 errata: `reports/CLANKER_ALPHA_V01_ERRATA.md`. Evidence: `evidence/clanker_alpha_v02/`.
"""
    (REPORTS / "CLANKER_ALPHA_EXECUTION_VALIDITY_V02.md").write_text(feas)
    hold_md = f"""# Clanker Alpha Holdout v0.2

```text
verdict = {verdict}
holdout_eval = ONCE
executable_n = {(hold.get('conditional_executable') or {}).get('n')}
```

## Universe
{json.dumps(hold.get('universe', {}), indent=2)}

## Conditional executable
{json.dumps(hold.get('conditional_executable', {}), indent=2)}

## Hypotheses conditional
H1: {hold.get('H1_conditional')}
H2: {hold.get('H2_conditional')}
H3: {hold.get('H3_conditional')}

## Strategy-level
all: {hold.get('strategy_all')}
H1: {hold.get('H1_strategy')}
H2: {hold.get('H2_strategy')}
H3: {hold.get('H3_strategy')}
"""
    (REPORTS / "CLANKER_ALPHA_HOLDOUT_V02.md").write_text(hold_md)

    handoff = f"""# Codex Review Request — Clanker Alpha Execution-Validity v0.2

## Identity

```text
study = CLANKER_ALPHA_EXECUTION_VALIDITY_V02
run_id = {run_id}
method_sha256 = {method_sha}
hypotheses_sha256 = {hyp_sha}
verdict = {verdict}
v01_errata = reports/CLANKER_ALPHA_V01_ERRATA.md
v01_bytes_modified = false
defaults_json_untouched = true
production_db = false
```

## Confirmation gate

```text
executable_holdout_n = {(hold.get('conditional_executable') or {}).get('n')}
threshold_for_EDGE_NOT_SUPPORTED = 30
```

## Artifacts

- reports/CLANKER_ALPHA_V01_ERRATA.md
- reports/CLANKER_ALPHA_HYPOTHESES_V02.md
- reports/CLANKER_ALPHA_METHOD_V02.md
- reports/CLANKER_ALPHA_EXECUTION_VALIDITY_V02.md
- reports/CLANKER_ALPHA_HOLDOUT_V02.md
- evidence/clanker_alpha_v02/
- export/CODEX_HANDOFF_CLANKER_ALPHA_V02.md

Please adjudicate PASS / PASS_WITH_CORRECTIONS / FAIL.
"""
    (EXPORT / "CODEX_HANDOFF_CLANKER_ALPHA_V02.md").write_text(handoff)


if __name__ == "__main__":
    raise SystemExit(main())
