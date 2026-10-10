#!/usr/bin/env python3
"""Phase 0 universe viability census. No alpha training or old-line signals."""
from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from eth_abi import encode
from web3 import Web3

ROOT = Path("/root/base_greenfield_directional_v0")
sys.path.insert(0, str(ROOT))

from src.common import ch_query  # noqa: E402
from src.v1_2.canonical_repair_replay import (  # execution primitives only; no scores/outcomes
    EXECUTOR,
    EXPECTED_FACTORY,
    QUOTER,
    ROUTER,
    WETH,
    Rpc,
    _ALLOW,
    _BAL,
    _FEE,
    _FACTORY,
    _GET_POOL,
    _QUOTE,
    _SWAP,
    _TOKEN0,
    _TOKEN1,
    nested_key,
    sol_map_key,
    state_override,
    to_addr,
    to_uint,
    vyper_map_key,
    word,
)

OUT = ROOT / "out_v2"
CACHE = OUT / "cache"
OUT.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)
FREEZE_PATH = ROOT / "configs" / "v2_phase0_freeze.json"

LN1P = math.log(1.0001)
GAS_LIMIT = 3_000_000
_SLOT0 = Web3.keccak(text="slot0()")[:4]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_i256(x: Any) -> int:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0
    s = str(x)
    try:
        v = int(s, 16) if s.lower().startswith("0x") else int(float(s)) if "." in s else int(s)
        return v - 2**256 if v >= 2**255 else v
    except Exception:
        return 0


def is_infra(error: Any) -> bool:
    s = str(error).lower()
    return any(x in s for x in ("transport:", "timeout", "limited", "request limit", "missing trie", "header not found", "-32007", "-32013"))


def export_raw() -> tuple[pd.DataFrame, pd.DataFrame]:
    pools_path = CACHE / "label_design_pools.parquet"
    swaps_path = CACHE / "label_design_swaps_126h.parquet"
    if pools_path.exists() and swaps_path.exists():
        pools = pd.read_parquet(pools_path)
        swaps = pd.read_parquet(swaps_path)
        print(f"[raw] reuse pools={len(pools)} swaps={len(swaps)}", flush=True)
        return pools, swaps
    pools_sql = """
    SELECT lower(d.pool_address) pool,
           any(lower(d.token0_address)) registry_token0,
           any(lower(d.token1_address)) registry_token1,
           any(toUInt64(d.fee_bps)) fee_raw,
           any(lower(d.created_tx_hash)) created_tx_hash,
           min(s.block_time) first_swap_time,
           toUnixTimestamp(min(s.block_time)) first_swap_ts,
           min(s.block_number) first_swap_block
    FROM eatlq.discovered_pools d
    INNER JOIN eatlq.uniswap_v3_swaps_decoded s ON lower(s.pool_address)=lower(d.pool_address)
    WHERE d.pool_type='v3' AND d.has_weth_leg=1 AND s.removed=0
    GROUP BY pool
    HAVING first_swap_time>=toDateTime('2025-10-01 00:00:00')
       AND first_swap_time<toDateTime('2025-12-01 14:30:00')
    """
    pools = ch_query(pools_sql, timeout=1200)
    swaps_sql = """
    WITH pools AS (
      SELECT lower(d.pool_address) pool, min(s.block_time) first_swap_time
      FROM eatlq.discovered_pools d
      INNER JOIN eatlq.uniswap_v3_swaps_decoded s ON lower(s.pool_address)=lower(d.pool_address)
      WHERE d.pool_type='v3' AND d.has_weth_leg=1 AND s.removed=0
      GROUP BY pool
      HAVING first_swap_time>=toDateTime('2025-10-01 00:00:00')
         AND first_swap_time<toDateTime('2025-12-01 14:30:00')
    )
    SELECT lower(s.pool_address) pool, toUnixTimestamp(s.block_time) ts,
           s.block_number, s.tick_raw tick, s.liquidity_raw liquidity,
           s.amount0_raw amount0, s.amount1_raw amount1,
           lower(s.sender) sender, lower(s.recipient) recipient
    FROM eatlq.uniswap_v3_swaps_decoded s
    INNER JOIN pools p ON lower(s.pool_address)=p.pool
    WHERE s.removed=0 AND s.block_time>=p.first_swap_time
      AND s.block_time<=p.first_swap_time+INTERVAL 126 HOUR
    """
    swaps = ch_query(swaps_sql, timeout=1800)
    pools.to_parquet(pools_path, index=False)
    swaps.to_parquet(swaps_path, index=False)
    print(f"[raw] exported pools={len(pools)} swaps={len(swaps)}", flush=True)
    return pools, swaps


def build_candidates(pools: pd.DataFrame, swaps: pd.DataFrame, freeze: dict) -> pd.DataFrame:
    path = CACHE / "phase0_candidates.parquet"
    if path.exists():
        d = pd.read_parquet(path)
        if "block_mapping_version" in d and d.block_mapping_version.eq("GLOBAL_V3_MINUTE_ASOF_V1").all():
            print(f"[candidates] reuse rows={len(d)}", flush=True)
            return d
    swaps = swaps.copy()
    swaps["tick_i"] = [parse_i256(x) for x in swaps.tick]
    swaps["liq_i"] = [parse_i256(x) for x in swaps.liquidity]
    groups = {p: g.sort_values("ts") for p, g in swaps.groupby("pool", sort=False)}
    clock_path = CACHE / "global_v3_minute_block_clock.parquet"
    if clock_path.exists():
        global_blocks = pd.read_parquet(clock_path)
    else:
        global_blocks = ch_query("""
        SELECT toUnixTimestamp(toStartOfMinute(block_time)) minute_ts,
               toUnixTimestamp(max(block_time)) observed_ts,
               argMax(block_number, block_time) block_number
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE removed=0 AND block_time>=toDateTime('2025-09-30 00:00:00')
          AND block_time<toDateTime('2025-12-02 00:00:00')
        GROUP BY minute_ts ORDER BY minute_ts
        """, timeout=1200)
        global_blocks.to_parquet(clock_path, index=False)
    # At a timestamp use the latest globally observed V3 swap block, always PIT.
    gb_ts = global_blocks.observed_ts.to_numpy(dtype=np.int64)
    gb_bn = global_blocks.block_number.to_numpy(dtype=np.int64)
    end_ts = int(pd.Timestamp(freeze["label_design_end_utc"]).timestamp())
    horizon = int(freeze["census_future_horizon_seconds"])
    rows = []
    for p in pools.itertuples(index=False):
        g = groups.get(p.pool)
        if g is None:
            continue
        ts = g.ts.to_numpy(dtype=np.int64)
        ticks = g.tick_i.to_numpy(dtype=np.float64)
        liqs = g.liq_i.to_numpy(dtype=np.float64)
        for bucket, age_s in freeze["age_anchors_seconds"].items():
            decision_time = int(p.first_swap_ts) + int(age_s)
            future_time = decision_time + horizon
            if future_time >= end_ts:
                continue
            gi = int(np.searchsorted(gb_ts, decision_time, side="right") - 1)
            gf = int(np.searchsorted(gb_ts, future_time, side="right") - 1)
            pi = int(np.searchsorted(ts, decision_time, side="right"))
            pf = int(np.searchsorted(ts, future_time, side="right"))
            if gi < 0 or gf < 0 or pi <= 0:
                continue
            pre_ts = ts[:pi]
            f_ticks = ticks[pi:pf]
            active_minutes = int(len(np.unique(pre_ts // 60)))
            enough = bool(pi >= int(freeze["pit_history"]["min_swaps"]) and active_minutes >= int(freeze["pit_history"]["min_active_minutes"]))
            rows.append({
                "opportunity_id": hashlib.sha256(f"{p.pool}|{decision_time}|{bucket}".encode()).hexdigest()[:24],
                "age_bucket": bucket,
                "age_s": int(age_s),
                "pool": p.pool,
                "registry_token0": p.registry_token0,
                "registry_token1": p.registry_token1,
                "fee_raw": int(p.fee_raw),
                "created_tx_hash": p.created_tx_hash,
                "first_swap_ts": int(p.first_swap_ts),
                "decision_time": decision_time,
                "future_time": future_time,
                "decision_block": int(gb_bn[gi]),
                "future_block": int(gb_bn[gf]),
                "decision_block_lag_s": int(decision_time - gb_ts[gi]),
                "future_block_lag_s": int(future_time - gb_ts[gf]),
                "block_mapping_version": "GLOBAL_V3_MINUTE_ASOF_V1",
                "n_swaps_pre": pi,
                "active_minutes_pre": active_minutes,
                "enough_pit_history": enough,
                "entry_tick": float(ticks[pi - 1]),
                "entry_liquidity": float(liqs[pi - 1]),
                "future_ticks_json": json.dumps([int(x) for x in f_ticks.tolist()]),
            })
    d = pd.DataFrame(rows).sort_values(["age_bucket", "decision_time", "pool"]).reset_index(drop=True)
    d.to_parquet(path, index=False)
    print(f"[candidates] rows={len(d)} {d.age_bucket.value_counts().to_dict()}", flush=True)
    return d


def canonical_metadata(candidates: pd.DataFrame, rpc: Rpc) -> pd.DataFrame:
    path = CACHE / "canonical_pools.parquet"
    if path.exists():
        d = pd.read_parquet(path)
        if not d.canonical_status.eq("INFRASTRUCTURE_UNKNOWN").any():
            print(f"[canonical] reuse pools={len(d)}", flush=True)
            return d
    first = candidates.sort_values("decision_time").drop_duplicates("pool")
    calls = []
    for r in first.itertuples(index=False):
        for selector in (_TOKEN0, _TOKEN1, _FEE, _FACTORY):
            calls.append(("eth_call", [{"to": Web3.to_checksum_address(r.pool), "data": "0x" + selector.hex()}, hex(int(r.decision_block))]))
    print(f"[canonical] direct getters={len(calls)}", flush=True)
    got = rpc.batch(calls)
    prelim = []
    fcalls = []
    for i, r in enumerate(first.itertuples(index=False)):
        x = got[i * 4 : i * 4 + 4]
        t0 = to_addr(x[0].get("result")) if not x[0].get("error") else None
        t1 = to_addr(x[1].get("result")) if not x[1].get("error") else None
        fee = to_uint(x[2].get("result")) if not x[2].get("error") else None
        fac = to_addr(x[3].get("result")) if not x[3].get("error") else None
        errs = [str(z.get("error")) for z in x if z.get("error")]
        prelim.append((r, t0, t1, fee, fac, errs))
        if t0 and t1 and fee is not None and fac:
            data = "0x" + (_GET_POOL + encode(["address", "address", "uint24"], [t0, t1, fee])).hex()
            fcalls.append(("eth_call", [{"to": fac, "data": data}, hex(int(r.decision_block))]))
        else:
            fcalls.append(None)
    active = rpc.batch([x for x in fcalls if x is not None])
    it = iter(active)
    fres = [next(it) if x is not None else {"error": "metadata_missing"} for x in fcalls]
    rows = []
    for (r, t0, t1, fee, fac, errs), fr in zip(prelim, fres):
        gp = to_addr(fr.get("result")) if not fr.get("error") else None
        valid = bool(
            t0 and t1 and fee is not None and fac and gp
            and fac.lower() == EXPECTED_FACTORY.lower()
            and gp.lower() == str(r.pool).lower()
            and WETH.lower() in (t0.lower(), t1.lower())
        )
        infra = bool(errs or (fr.get("error") and is_infra(fr.get("error"))))
        status = "CANONICAL_VALID" if valid else ("INFRASTRUCTURE_UNKNOWN" if infra else "CANONICAL_INVALID")
        alt = None
        if t0 and t1 and WETH.lower() in (t0.lower(), t1.lower()):
            alt = t1 if t0.lower() == WETH.lower() else t0
        rows.append({
            "pool": str(r.pool).lower(), "canonical_token0": t0.lower() if t0 else None,
            "canonical_token1": t1.lower() if t1 else None, "canonical_alt": alt.lower() if alt else None,
            "canonical_fee": int(fee) if fee is not None else None, "canonical_factory": fac.lower() if fac else None,
            "factory_getPool": gp.lower() if gp else None, "canonical_status": status,
            "canonical_valid": valid, "metadata_error": "|".join(errs),
        })
    d = pd.DataFrame(rows)
    d.to_parquet(path, index=False)
    print(f"[canonical] {d.canonical_status.value_counts().to_dict()}", flush=True)
    return d


def deployers(pools: pd.DataFrame, rpc: Rpc) -> pd.DataFrame:
    path = CACHE / "pool_deployers.parquet"
    if path.exists():
        return pd.read_parquet(path)
    calls = [("eth_getTransactionByHash", [str(r.created_tx_hash)]) for r in pools.itertuples(index=False)]
    got = rpc.batch(calls)
    rows = []
    for r, x in zip(pools.itertuples(index=False), got):
        tx = x.get("result") or {}
        rows.append({"pool": r.pool, "deployer": str(tx.get("from")).lower() if tx.get("from") else None, "deployer_status": "KNOWN" if tx.get("from") else ("INFRASTRUCTURE_UNKNOWN" if x.get("error") else "NOT_FOUND")})
    d = pd.DataFrame(rows)
    d.to_parquet(path, index=False)
    return d


def detect_slots_v2(rpc: Rpc, token: str, block: int) -> dict | None:
    path = CACHE / "storage_layouts.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    name = str(token).lower()
    if name in cache:
        return cache[name]
    token = Web3.to_checksum_address(token)
    bdata = "0x" + (_BAL + encode(["address"], [EXECUTOR])).hex()
    adata = "0x" + (_ALLOW + encode(["address", "address"], [EXECUTOR, ROUTER])).hex()
    bmatch = None
    for slots in (range(5), range(5, 20)):
        calls, meta = [], []
        for slot in slots:
            for layout in ("sol", "vyper"):
                key = sol_map_key(EXECUTOR, slot) if layout == "sol" else vyper_map_key(EXECUTOR, slot)
                calls.append(("eth_call", [{"to": token, "data": bdata}, hex(int(block)), {token: {"stateDiff": {key: word(0x77773333)}}}]))
                meta.append((slot, layout))
        for m, x in zip(meta, rpc.batch(calls)):
            if not x.get("error") and to_uint(x.get("result")) == 0x77773333:
                bmatch = m
                break
        if bmatch:
            break
    if not bmatch:
        return None
    amatch = None
    for slots in (range(5), range(5, 20)):
        calls, meta = [], []
        for slot in slots:
            for layout in ("sol", "vyper"):
                key = nested_key(EXECUTOR, ROUTER, slot, layout)
                calls.append(("eth_call", [{"to": token, "data": adata}, hex(int(block)), {token: {"stateDiff": {key: word(0x88884444)}}}]))
                meta.append((slot, layout))
        for m, x in zip(meta, rpc.batch(calls)):
            if not x.get("error") and to_uint(x.get("result")) == 0x88884444:
                amatch = m
                break
        if amatch:
            break
    if not amatch:
        return None
    out = {"balance_slot": bmatch[0], "balance_layout": bmatch[1], "allowance_slot": amatch[0], "allowance_layout": amatch[1]}
    cache[name] = out
    path.write_text(json.dumps(cache, indent=2) + "\n")
    return out


def quote(rpc: Rpc, token: str, fee: int, amount: int, block: int) -> dict:
    data = "0x" + (_QUOTE + encode(["(address,address,uint256,uint24,uint160)"], [(WETH, token, int(amount), int(fee), 0)])).hex()
    x = rpc.eth_call({"from": EXECUTOR, "to": QUOTER, "data": data}, block)
    time.sleep(0.06)
    if x.get("error"):
        return {"status": "INFRASTRUCTURE_UNKNOWN" if is_infra(x["error"]) else "REVERT", "amount": None, "error": str(x["error"])[:300]}
    raw = x.get("result") or "0x"
    amt = int(raw[2:66], 16) if len(raw) >= 66 else 0
    return {"status": "SUCCESS" if amt > 0 else "REVERT", "amount": amt, "error": ""}


def swap_call(rpc: Rpc, token_in: str, token_out: str, fee: int, amount: int, block: int, slots: dict) -> dict:
    ov = state_override(token_in, EXECUTOR, ROUTER, slots, amount)
    data = "0x" + (_SWAP + encode(["(address,address,uint24,address,uint256,uint256,uint160)"], [(token_in, token_out, int(fee), EXECUTOR, int(amount), 0, 0)])).hex()
    x = rpc.eth_call({"from": EXECUTOR, "to": ROUTER, "data": data, "gas": hex(GAS_LIMIT)}, block, ov)
    time.sleep(0.06)
    if x.get("error"):
        return {"status": "INFRASTRUCTURE_UNKNOWN" if is_infra(x["error"]) else "REVERT", "amount": None, "error": str(x["error"])[:300]}
    raw = x.get("result") or "0x"
    amt = int(raw[2:66], 16) if len(raw) >= 66 else 0
    return {"status": "SUCCESS" if amt > 0 else "REVERT", "amount": amt, "error": ""}


def slot0(rpc: Rpc, pool: str, block: int) -> tuple[int | None, str]:
    x = rpc.eth_call({"to": Web3.to_checksum_address(pool), "data": "0x" + _SLOT0.hex()}, block)
    time.sleep(0.04)
    if x.get("error"):
        return None, "INFRASTRUCTURE_UNKNOWN" if is_infra(x["error"]) else "REVERT"
    raw = x.get("result") or "0x"
    return (int(raw[2:66], 16), "SUCCESS") if len(raw) >= 66 else (None, "REVERT")


def expected_alt(amount_weth: int, sqrt_x96: int, weth_is_token0: bool) -> float:
    price = (sqrt_x96 / 2**96) ** 2
    return float(amount_weth * price if weth_is_token0 else amount_weth / max(price, 1e-300))


def expected_weth(amount_alt: int, sqrt_x96: int, weth_is_token0: bool) -> float:
    price = (sqrt_x96 / 2**96) ** 2
    return float(amount_alt / max(price, 1e-300) if weth_is_token0 else amount_alt * price)


def atomic_census(candidates: pd.DataFrame, metadata: pd.DataFrame, freeze: dict, rpc: Rpc) -> pd.DataFrame:
    path = CACHE / "atomic_checkpoint.parquet"
    old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    if len(old) and ("decision_block" not in old.columns or "future_block" not in old.columns):
        old = pd.DataFrame()
    done = set(old.opportunity_id) if len(old) else set()
    rows = old.to_dict("records") if len(old) else []
    d = candidates.merge(metadata, on="pool", how="left", validate="many_to_one")
    amount_in = int(freeze["amount_in_wei"])
    weth_slots = detect_slots_v2(rpc, WETH, int(d.decision_block.min()))
    for r in d.itertuples(index=False):
        if r.opportunity_id in done:
            continue
        base = {
            "opportunity_id": r.opportunity_id, "age_bucket": r.age_bucket, "pool": r.pool,
            "decision_block": int(r.decision_block), "future_block": int(r.future_block),
            "canonical_status": r.canonical_status, "canonical_valid": bool(r.canonical_valid),
            "fee_canonical": r.canonical_fee, "canonical_alt": r.canonical_alt,
        }
        result = {
            **base, "buy_status": "NOT_AUDITED", "actual_token_received": None,
            "current_sell_status": "NOT_AUDITED", "current_weth_out": None,
            "future_sell_status": "NOT_AUDITED", "future_weth_out": None,
            "future_permanent_unsellable": False, "infrastructure_unknown": False,
            "entry_slippage": None, "current_exit_slippage": None, "future_exit_slippage": None,
            "executable_depth_weth": 0.0, "production_depth_pass": False, "atomic_error": "",
        }
        block_ok = int(r.decision_block_lag_s) <= int(freeze["block_mapping"]["max_lag_seconds"]) and int(r.future_block_lag_s) <= int(freeze["block_mapping"]["max_lag_seconds"])
        if not r.enough_pit_history:
            rows.append(result)
            continue
        if not block_ok:
            result["buy_status"] = "INFRASTRUCTURE_UNKNOWN"
            result["current_sell_status"] = "INFRASTRUCTURE_UNKNOWN"
            result["future_sell_status"] = "INFRASTRUCTURE_UNKNOWN"
            result["infrastructure_unknown"] = True
            result["atomic_error"] = "global_block_clock_lag_exceeds_120s"
            rows.append(result)
            continue
        if not r.canonical_valid or not r.canonical_alt or pd.isna(r.canonical_fee) or weth_slots is None:
            if r.canonical_status == "INFRASTRUCTURE_UNKNOWN" or weth_slots is None:
                result["infrastructure_unknown"] = True
            rows.append(result)
            continue
        token = Web3.to_checksum_address(r.canonical_alt)
        fee = int(r.canonical_fee)
        weth0 = str(r.canonical_token0).lower() == WETH.lower()
        sq0, sq0_status = slot0(rpc, r.pool, int(r.decision_block))
        sqf, sqf_status = slot0(rpc, r.pool, int(r.future_block))
        depth = 0.0
        depth_errors = []
        for mult in freeze["depth_multiples"]:
            q = quote(rpc, token, fee, amount_in * int(mult), int(r.decision_block))
            if q["status"] == "SUCCESS" and sq0:
                exp = expected_alt(amount_in * int(mult), sq0, weth0)
                slip = 1.0 - float(q["amount"]) / max(exp, 1e-300)
                if slip <= float(freeze["depth_slippage_ceiling"]):
                    depth = amount_in * int(mult) / 1e18
            elif q["status"] == "INFRASTRUCTURE_UNKNOWN":
                depth_errors.append(q["error"])
        buy = swap_call(rpc, WETH, token, fee, amount_in, int(r.decision_block), weth_slots)
        result["buy_status"] = buy["status"]
        result["executable_depth_weth"] = depth
        if buy["status"] == "INFRASTRUCTURE_UNKNOWN" or sq0_status == "INFRASTRUCTURE_UNKNOWN" or sqf_status == "INFRASTRUCTURE_UNKNOWN" or depth_errors:
            result["infrastructure_unknown"] = True
        if buy["status"] != "SUCCESS":
            result["atomic_error"] = buy.get("error", "") or "|".join(depth_errors)
            rows.append(result)
            continue
        token_amt = int(buy["amount"])
        result["actual_token_received"] = str(token_amt)
        if sq0:
            result["entry_slippage"] = 1.0 - token_amt / max(expected_alt(amount_in, sq0, weth0), 1e-300)
        tslots = detect_slots_v2(rpc, token, int(r.decision_block))
        if tslots is None:
            result["current_sell_status"] = "INFRASTRUCTURE_UNKNOWN"
            result["future_sell_status"] = "INFRASTRUCTURE_UNKNOWN"
            result["infrastructure_unknown"] = True
            result["atomic_error"] = "token_storage_layout_unknown"
            rows.append(result)
            continue
        cur = swap_call(rpc, token, WETH, fee, token_amt, int(r.decision_block), tslots)
        fut = swap_call(rpc, token, WETH, fee, token_amt, int(r.future_block), tslots)
        result["current_sell_status"] = cur["status"]
        result["future_sell_status"] = fut["status"]
        result["current_weth_out"] = str(cur["amount"]) if cur.get("amount") is not None else None
        result["future_weth_out"] = str(fut["amount"]) if fut.get("amount") is not None else None
        if cur["status"] == "INFRASTRUCTURE_UNKNOWN" or fut["status"] == "INFRASTRUCTURE_UNKNOWN":
            result["infrastructure_unknown"] = True
        floor = amount_in * float(freeze["economic_unsellable_recovery_floor"])
        result["future_permanent_unsellable"] = bool(fut["status"] == "REVERT" or (fut["status"] == "SUCCESS" and int(fut["amount"]) < floor))
        if sq0 and cur["status"] == "SUCCESS":
            result["current_exit_slippage"] = 1.0 - int(cur["amount"]) / max(expected_weth(token_amt, sq0, weth0), 1e-300)
        if sqf and fut["status"] == "SUCCESS":
            result["future_exit_slippage"] = 1.0 - int(fut["amount"]) / max(expected_weth(token_amt, sqf, weth0), 1e-300)
        result["production_depth_pass"] = bool(depth >= amount_in / 1e18 and buy["status"] == "SUCCESS" and cur["status"] == "SUCCESS")
        result["atomic_error"] = "|".join(x for x in (cur.get("error", ""), fut.get("error", ""), "|".join(depth_errors)) if x)
        rows.append(result)
        if len(rows) % 20 == 0:
            pd.DataFrame(rows).to_parquet(path, index=False)
            print(f"[atomic] {len(rows)}/{len(d)} rpc_calls={rpc.calls}", flush=True)
    out = pd.DataFrame(rows)
    out.to_parquet(path, index=False)
    return out


def qstats(s: pd.Series) -> dict:
    x = pd.to_numeric(s, errors="coerce").dropna()
    return {"n": int(len(x)), "q05": float(x.quantile(.05)) if len(x) else None, "q25": float(x.quantile(.25)) if len(x) else None, "q50": float(x.quantile(.50)) if len(x) else None, "q75": float(x.quantile(.75)) if len(x) else None, "q95": float(x.quantile(.95)) if len(x) else None}


def summarize(candidates: pd.DataFrame, atomic: pd.DataFrame, deploy: pd.DataFrame, metadata: pd.DataFrame, freeze: dict) -> tuple[pd.DataFrame, dict]:
    # Candidate timestamps/blocks are the lineage authority. The atomic checkpoint
    # repeats execution blocks only for cache invalidation; omit duplicate x/y
    # columns from the final audit table.
    atomic_payload = atomic.drop(
        columns=[c for c in ("decision_block", "future_block") if c in atomic.columns]
    )
    d = candidates.merge(
        atomic_payload,
        on=["opportunity_id", "age_bucket", "pool"],
        how="left",
        validate="one_to_one",
    )
    d = d.merge(metadata[["pool", "canonical_token0", "canonical_token1"]], on="pool", how="left", validate="many_to_one")
    d = d.merge(deploy, on="pool", how="left", validate="many_to_one")
    # Correct alt-price path orientation from canonical token ordering.
    mfe, mae = [], []
    for r in d.itertuples(index=False):
        ticks = np.array(json.loads(r.future_ticks_json), dtype=np.float64)
        if len(ticks) == 0 or not r.canonical_valid:
            mfe.append(np.nan); mae.append(np.nan); continue
        sign = -1.0 if str(r.canonical_token0).lower() == WETH.lower() else 1.0
        rets = sign * (ticks - float(r.entry_tick)) * LN1P
        mfe.append(float(np.max(rets))); mae.append(float(np.min(rets)))
    d["mfe_6h"] = mfe; d["mae_6h"] = mae
    d.to_parquet(OUT / "PHASE0_CENSUS_OPPORTUNITIES.parquet", index=False)
    period_days = (pd.Timestamp(freeze["label_design_end_utc"]) - pd.Timestamp(freeze["label_design_start_utc"])).total_seconds() / 86400
    rows = []
    details = {}
    for bucket, g in d.groupby("age_bucket", sort=False):
        pit = g[g.enough_pit_history]
        canonical = pit[pit.canonical_valid]
        audited = canonical[canonical.buy_status != "NOT_AUDITED"]
        buys = audited[audited.buy_status == "SUCCESS"]
        future_resolved = buys.future_sell_status.isin(["SUCCESS", "REVERT"])
        starts = np.sort(pit.decision_time.to_numpy(dtype=np.int64))
        conc = np.array([i - np.searchsorted(starts, t - int(freeze["census_future_horizon_seconds"]), side="right") + 1 for i, t in enumerate(starts)]) if len(starts) else np.array([])
        metrics = {
            "age_bucket": bucket,
            "raw_independent_opportunities": int(len(g)),
            "independent_opportunities": int(len(pit)),
            "opportunities_per_day": len(pit) / period_days,
            "opportunities_per_week": len(pit) / period_days * 7,
            "concurrent_q50": float(np.quantile(conc, .5)) if len(conc) else None,
            "concurrent_q90": float(np.quantile(conc, .9)) if len(conc) else None,
            "concurrent_q99": float(np.quantile(conc, .99)) if len(conc) else None,
            "concurrent_max": int(np.max(conc)) if len(conc) else 0,
            "canonical_metadata_coverage": float(pit.canonical_valid.mean()) if len(pit) else 0.0,
            "atomic_buy_success": float(audited.buy_status.eq("SUCCESS").mean()) if len(audited) else 0.0,
            "current_atomic_sell_success": float(buys.current_sell_status.eq("SUCCESS").mean()) if len(buys) else 0.0,
            "future_permanent_unsellable_rate": float(buys.future_permanent_unsellable.mean()) if len(buys) else 0.0,
            "future_sell_resolution_rate": float(future_resolved.mean()) if len(buys) else 0.0,
            "infrastructure_unknown_rate": float(audited.infrastructure_unknown.mean()) if len(audited) else 1.0,
            "production_depth_pass_rate": float(audited.production_depth_pass.mean()) if len(audited) else 0.0,
            "pool_breadth": int(pit.pool.nunique()),
            "token_breadth": int(pit.canonical_alt.nunique()),
            "deployer_breadth": int(pit.deployer.dropna().nunique()),
            "deployer_coverage": float(pit.deployer.notna().mean()) if len(pit) else 0.0,
            "entry_slippage_q50": qstats(buys.entry_slippage)["q50"],
            "entry_slippage_q95": qstats(buys.entry_slippage)["q95"],
            "exit_slippage_q50": qstats(buys.current_exit_slippage)["q50"],
            "exit_slippage_q95": qstats(buys.current_exit_slippage)["q95"],
            "executable_depth_weth_q50": qstats(audited.executable_depth_weth)["q50"],
            "executable_depth_weth_q05": qstats(audited.executable_depth_weth)["q05"],
            "mfe_q25": qstats(pit.mfe_6h)["q25"], "mfe_q50": qstats(pit.mfe_6h)["q50"], "mfe_q75": qstats(pit.mfe_6h)["q75"],
            "mae_q25": qstats(pit.mae_6h)["q25"], "mae_q50": qstats(pit.mae_6h)["q50"], "mae_q75": qstats(pit.mae_6h)["q75"],
        }
        rows.append(metrics)
        details[bucket] = {
            "concurrent_candidate_count": qstats(pd.Series(conc)),
            "entry_slippage": qstats(buys.entry_slippage), "current_exit_slippage": qstats(buys.current_exit_slippage),
            "future_exit_slippage": qstats(buys.future_exit_slippage), "executable_depth_weth": qstats(audited.executable_depth_weth),
            "mfe_6h": qstats(pit.mfe_6h), "mae_6h": qstats(pit.mae_6h),
        }
    summary = pd.DataFrame(rows)
    gates = freeze["viability_gates"]
    checks = {}
    for r in summary.itertuples(index=False):
        if r.age_bucket.endswith("NEGATIVE_CONTROL"):
            checks[r.age_bucket] = {"eligible_for_promotion": False, "reason": "CLOSED_NEGATIVE_CONTROL"}
            continue
        c = {
            "min_independent_opportunities": r.independent_opportunities >= gates["min_independent_opportunities"],
            "min_opportunities_per_week": r.opportunities_per_week >= gates["min_opportunities_per_week"],
            "min_pool_breadth": r.pool_breadth >= gates["min_pool_breadth"],
            "min_token_breadth": r.token_breadth >= gates["min_token_breadth"],
            "min_deployer_breadth": r.deployer_breadth >= gates["min_deployer_breadth"],
            "min_deployer_coverage": r.deployer_coverage >= gates["min_deployer_coverage"],
            "min_canonical_metadata_coverage": r.canonical_metadata_coverage >= gates["min_canonical_metadata_coverage"],
            "min_atomic_buy_success": r.atomic_buy_success >= gates["min_atomic_buy_success"],
            "min_current_atomic_sell_success": r.current_atomic_sell_success >= gates["min_current_atomic_sell_success"],
            "max_future_permanent_unsellable_rate": r.future_permanent_unsellable_rate <= gates["max_future_permanent_unsellable_rate"],
            "max_infrastructure_unknown_rate": r.infrastructure_unknown_rate <= gates["max_infrastructure_unknown_rate"],
            "min_future_sell_resolution_rate": r.future_sell_resolution_rate >= gates["min_future_sell_resolution_rate"],
            "min_production_depth_pass_rate": r.production_depth_pass_rate >= gates["min_production_depth_pass_rate"],
        }
        checks[r.age_bucket] = {**c, "eligible_for_promotion": bool(all(c.values()))}
    viable = [b for b, c in checks.items() if c.get("eligible_for_promotion")]
    promoted = None
    if viable:
        v = summary[summary.age_bucket.isin(viable)].sort_values(
            ["current_atomic_sell_success", "future_sell_resolution_rate", "future_permanent_unsellable_rate", "independent_opportunities", "age_bucket"],
            ascending=[False, False, True, False, True],
        )
        promoted = str(v.iloc[0].age_bucket)
    verdict = "PHASE0_SINGLE_UNIVERSE_PROMOTED" if promoted else "NO_VIABLE_BASE_NEW_POOL_UNIVERSE"
    summary["viability_pass"] = summary.age_bucket.map(lambda b: bool(checks[b].get("eligible_for_promotion")))
    summary["promoted"] = summary.age_bucket.eq(promoted) if promoted else False
    summary.to_csv(OUT / "PHASE0_UNIVERSE_CENSUS.csv", index=False)
    final = {
        "created_utc": utcnow(), "role": "Base Greenfield V2 Orthogonal Directional Research Agent",
        "phase": "PHASE_0_UNIVERSE_VIABILITY_CENSUS", "verdict": verdict,
        "promoted_universe": promoted, "stop_research": promoted is None,
        "alpha_models_trained": False, "old_line_inputs_used": False, "deploy_allowed": False,
        "freeze_sha256": sha256_file(FREEZE_PATH), "viability_checks": checks,
        "distribution_details": details,
    }
    (OUT / "PHASE0_FINAL.json").write_text(json.dumps(final, indent=2) + "\n")
    return summary, final


def main() -> None:
    freeze = json.loads(FREEZE_PATH.read_text())
    rpc = Rpc(Path("/tmp/rpc_url.txt").read_text().strip(), "V2_PHASE0")
    pools, swaps = export_raw()
    candidates = build_candidates(pools, swaps, freeze)
    meta = canonical_metadata(candidates, rpc)
    dep = deployers(pools, rpc)
    atomic = atomic_census(candidates, meta, freeze, rpc)
    summary, final = summarize(candidates, atomic, dep, meta, freeze)
    (OUT / "REPRODUCE.md").write_text(
        "# Reproduce Base Greenfield V2 Phase 0\n\n"
        "```bash\ncd /root/base_greenfield_directional_v0\n"
        ".venv/bin/python -m src.v2.phase0_census 2>&1 | tee out_v2/RUN.log\n```\n\n"
        "Phase 0 trains no alpha model. Closed KEYSTONE/F10/G2/C3/P2 artifacts are not read. "
        "The only imported V1.2 code is generic RPC/state-override execution plumbing.\n\n"
        f"Freeze SHA256: `{sha256_file(FREEZE_PATH)}`  \nCode SHA256: `{sha256_file(Path(__file__))}`  \nVerdict: `{final['verdict']}`\n"
    )
    print(summary.to_string(index=False), flush=True)
    print(json.dumps(final, indent=2), flush=True)


if __name__ == "__main__":
    main()
