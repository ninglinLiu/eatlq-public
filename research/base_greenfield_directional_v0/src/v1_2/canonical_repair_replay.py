#!/usr/bin/env python3
"""V1.2 canonical metadata repair replay.

This module never changes discovery artifacts, model scores, selections, barriers,
or execution sizing.  It repairs only execution metadata from the pool contract.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
from eth_abi import encode
from web3 import Web3

ROOT = Path("/root/base_greenfield_directional_v0")
OUT = ROOT / "out"
OUT_V1 = ROOT / "out_v1"
OUT_V11 = ROOT / "out_v1_1"
OUT_V12 = ROOT / "out_v1_2"
CACHE = OUT_V12 / "cache"
OUT_V12.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)

WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
EXPECTED_FACTORY = Web3.to_checksum_address("0x33128a8fC17869897dcE68Ed026d694621f6FDfD")
ROUTER = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")
QUOTER = Web3.to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")
EXECUTOR = Web3.to_checksum_address("0x4B7cd953871663474a0A25B81bddDf5C1f1195A7")

AMOUNT_IN = 16_666_666_666_666_666
WETH_IN = AMOUNT_IN / 1e18
INITIAL_WALLET = 0.5
MAX_CONCURRENT = 3
H = 3600
TP = 0.50
SL = 0.35
ENTRY_DELAY_BLOCKS = 1
GAS_BUY_WETH = 0.0001
GAS_SELL_WETH = 0.0001
GAS_LIMIT = 3_000_000
ECONOMIC_TAX = 0.90
RETRY_BLOCKS = (0, 5, 30, 150)
LN1P = math.log(1.0001)

_TOKEN0 = Web3.keccak(text="token0()")[:4]
_TOKEN1 = Web3.keccak(text="token1()")[:4]
_FEE = Web3.keccak(text="fee()")[:4]
_FACTORY = Web3.keccak(text="factory()")[:4]
_GET_POOL = Web3.keccak(text="getPool(address,address,uint24)")[:4]
_QUOTE = Web3.keccak(text="quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]
_SWAP = Web3.keccak(text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
_BAL = Web3.keccak(text="balanceOf(address)")[:4]
_ALLOW = Web3.keccak(text="allowance(address,address)")[:4]

ALLOWED_REPAIR_CLASSES = {
    "METADATA_MATCH",
    "REPAIRABLE_FEE_UNIT_MISMATCH",
    "TRUE_INVALID_POOL",
    "POOL_ADDRESS_MISMATCH",
    "INFRASTRUCTURE_UNKNOWN",
}
ALLOWED_VERDICTS = {
    "EXECUTION_ONLY_CONTAMINATION_G2_REPLAY_POSITIVE",
    "EXECUTION_ONLY_CONTAMINATION_G2_REPLAY_NEGATIVE",
    "MODEL_LINEAGE_INVALIDATED",
    "SELL_TRUTH_INCOMPLETE",
    "TRUE_15_60M_UNTRADEABILITY",
    "INSUFFICIENT_REPAIRED_SAMPLE",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def to_addr(result: str | None) -> str | None:
    if not result or result in ("0x", "0x0") or len(result) < 42:
        return None
    try:
        a = Web3.to_checksum_address("0x" + result[-40:])
        return None if int(a, 16) == 0 else a
    except Exception:
        return None


def to_uint(result: str | None) -> int | None:
    if not result or result in ("0x", None):
        return None
    try:
        return int(result, 16)
    except Exception:
        return None


def word(value: int) -> str:
    return "0x" + int(value).to_bytes(32, "big").hex()


def sol_map_key(owner: str, slot: int) -> str:
    return "0x" + Web3.keccak(encode(["address", "uint256"], [owner, slot])).hex()


def vyper_map_key(owner: str, slot: int) -> str:
    return "0x" + Web3.keccak(encode(["uint256", "address"], [slot, owner])).hex()


def nested_key(owner: str, spender: str, slot: int, layout: str) -> str:
    inner = (
        Web3.keccak(encode(["address", "uint256"], [owner, slot]))
        if layout == "sol"
        else Web3.keccak(encode(["uint256", "address"], [slot, owner]))
    )
    return "0x" + Web3.keccak(encode(["address", "bytes32"], [spender, inner])).hex()


class Rpc:
    def __init__(self, url: str, name: str):
        self.url = url
        self.name = name
        self.url_sha256 = hashlib.sha256(url.encode()).hexdigest()
        self.session = requests.Session()
        self.seq = 1
        self.calls = 0

    def one(self, method: str, params: list, retries: int = 3) -> dict:
        last: Exception | None = None
        for attempt in range(retries):
            self.calls += 1
            payload = {"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params}
            self.seq += 1
            try:
                res = self.session.post(self.url, json=payload, timeout=90)
                res.raise_for_status()
                return res.json()
            except Exception as exc:
                last = exc
                time.sleep(0.25 * (attempt + 1))
        return {"error": {"message": f"transport:{type(last).__name__}:{last}"}}

    def batch(self, calls: list[tuple[str, list]], chunk_size: int = 100) -> list[dict]:
        out: list[dict] = []
        # The archive endpoint counts each JSON-RPC element and enforces 50/s.
        # Keep headroom for concurrent Anvil upstream reads and direct calls.
        chunk_size = min(int(chunk_size), 10)
        for start in range(0, len(calls), chunk_size):
            chunk = calls[start : start + chunk_size]
            payload = []
            ids = []
            for method, params in chunk:
                rid = self.seq
                self.seq += 1
                ids.append(rid)
                payload.append({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            last: Exception | None = None
            got = None
            for attempt in range(3):
                self.calls += len(chunk)
                try:
                    res = self.session.post(self.url, json=payload, timeout=120)
                    res.raise_for_status()
                    body = res.json()
                    if not isinstance(body, list):
                        raise RuntimeError(f"batch_not_list:{str(body)[:200]}")
                    got = {int(x["id"]): x for x in body}
                    break
                except Exception as exc:
                    last = exc
                    time.sleep(0.4 * (attempt + 1))
            if got is None:
                out.extend({"error": {"message": f"transport:{type(last).__name__}:{last}"}} for _ in chunk)
            else:
                out.extend(got.get(rid, {"error": {"message": "missing_batch_response"}}) for rid in ids)
            time.sleep(max(len(chunk) / 18.0, 0.10))
        return out

    def eth_call(self, tx: dict, block: int, override: dict | None = None) -> dict:
        params: list[Any] = [tx, hex(int(block))]
        if override is not None:
            params.append(override)
        return self.one("eth_call", params)


def load_rpc_b() -> str | None:
    for p in (Path("/tmp/rpc_url_b.txt"), Path("/tmp/rpc_url2.txt")):
        if p.exists() and p.read_text().strip():
            return p.read_text().strip()
    return None


def metadata_audit(panel: pd.DataFrame, rpc: Rpc) -> pd.DataFrame:
    """Read token0/token1/fee/factory from every panel row's pool at its PIT block."""
    cache_path = CACHE / "canonical_metadata_direct.parquet"
    prior_good = pd.DataFrame()
    full_panel = panel.copy()
    if cache_path.exists():
        cached = pd.read_parquet(cache_path)
        if (
            len(cached) == len(panel)
            and cached["signal_id"].tolist() == panel["signal_id"].tolist()
            and not cached["repair_class"].eq("INFRASTRUCTURE_UNKNOWN").any()
        ):
            print(f"[metadata] reuse direct cache rows={len(cached)}", flush=True)
            return cached
        if len(cached) == len(panel) and set(cached["signal_id"]) == set(panel["signal_id"]):
            prior_good = cached[~cached["repair_class"].eq("INFRASTRUCTURE_UNKNOWN")].copy()
            panel = panel[~panel["signal_id"].isin(prior_good["signal_id"])].copy()
            print(f"[metadata] resume good={len(prior_good)} retry={len(panel)}", flush=True)

    calls: list[tuple[str, list]] = []
    selectors = (_TOKEN0, _TOKEN1, _FEE, _FACTORY)
    for r in panel.itertuples(index=False):
        for selector in selectors:
            calls.append(("eth_call", [{"to": Web3.to_checksum_address(r.pool), "data": "0x" + selector.hex()}, hex(int(r.decision_block))]))
    print(f"[metadata] direct pool calls={len(calls)}", flush=True)
    phase1 = rpc.batch(calls)

    prelim = []
    factory_calls: list[tuple[str, list] | None] = []
    for i, r in enumerate(panel.itertuples(index=False)):
        rr = phase1[i * 4 : i * 4 + 4]
        errs = [str(x.get("error"))[:400] for x in rr if x.get("error")]
        t0 = to_addr(rr[0].get("result")) if not rr[0].get("error") else None
        t1 = to_addr(rr[1].get("result")) if not rr[1].get("error") else None
        fee = to_uint(rr[2].get("result")) if not rr[2].get("error") else None
        fac = to_addr(rr[3].get("result")) if not rr[3].get("error") else None
        prelim.append((t0, t1, fee, fac, errs))
        if t0 and t1 and fee is not None and fac:
            data = "0x" + (_GET_POOL + encode(["address", "address", "uint24"], [t0, t1, fee])).hex()
            factory_calls.append(("eth_call", [{"to": fac, "data": data}, hex(int(r.decision_block))]))
        else:
            factory_calls.append(None)

    active = [x for x in factory_calls if x is not None]
    print(f"[metadata] canonical factory validations={len(active)}", flush=True)
    active_results = iter(rpc.batch(active))
    phase2 = [next(active_results) if x is not None else {"error": {"message": "metadata_unavailable"}} for x in factory_calls]

    rows = []
    for r, meta, fr in zip(panel.itertuples(index=False), prelim, phase2):
        t0, t1, fee_can, fac, errors = meta
        gp = to_addr(fr.get("result")) if not fr.get("error") else None
        pool = Web3.to_checksum_address(r.pool)
        raw_alt = Web3.to_checksum_address(r.alt)
        raw_tokens_match = bool(t0 and t1 and {t0.lower(), t1.lower()} == {WETH.lower(), raw_alt.lower()})
        factory_match = bool(fac and fac.lower() == EXPECTED_FACTORY.lower())
        validated = bool(gp and gp.lower() == pool.lower())
        if errors or fee_can is None or not t0 or not t1 or not fac or fr.get("error"):
            repair_class = "INFRASTRUCTURE_UNKNOWN"
            status = "CANONICAL_CALL_FAILED"
            reason = "|".join(errors + ([str(fr.get("error"))[:400]] if fr.get("error") else []))
        elif gp is None:
            repair_class = "TRUE_INVALID_POOL"
            status = "FACTORY_GETPOOL_ZERO"
            reason = "canonical_factory.getPool returned zero"
        elif gp.lower() != pool.lower():
            repair_class = "POOL_ADDRESS_MISMATCH"
            status = "FACTORY_POOL_ADDRESS_MISMATCH"
            reason = f"factory_getPool={gp.lower()}"
        elif not factory_match or WETH.lower() not in {t0.lower(), t1.lower()}:
            repair_class = "TRUE_INVALID_POOL"
            status = "OUTSIDE_FROZEN_UNISWAP_V3_WETH_UNIVERSE"
            reason = f"expected_factory_match={factory_match};weth_leg={WETH.lower() in {t0.lower(), t1.lower()}}"
        elif not raw_tokens_match:
            repair_class = "POOL_ADDRESS_MISMATCH"
            status = "REGISTRY_TOKEN_POOL_MISMATCH"
            reason = "registry alt/WETH pair differs from pool.token0/token1"
        elif int(r.fee) != int(fee_can):
            repair_class = "REPAIRABLE_FEE_UNIT_MISMATCH"
            status = "FACTORY_VALIDATED_REPAIRED"
            reason = f"fee_raw={int(r.fee)};pool_fee={int(fee_can)}"
        else:
            repair_class = "METADATA_MATCH"
            status = "FACTORY_VALIDATED_MATCH"
            reason = ""
        explicit_check = "NOT_SPECIAL_CASE"
        if int(r.fee) == 1:
            explicit_check = "PASS_1_TO_100" if fee_can == 100 else f"OBSERVED_1_TO_{fee_can}"
        elif int(r.fee) == 5:
            explicit_check = "PASS_5_TO_500" if fee_can == 500 else f"OBSERVED_5_TO_{fee_can}"
        elif int(r.fee) == 30:
            explicit_check = "PASS_30_TO_3000" if fee_can == 3000 else f"OBSERVED_30_TO_{fee_can}"
        elif int(r.fee) == 100:
            explicit_check = "PASS_100_TO_100" if fee_can == 100 else ("PASS_100_TO_10000" if fee_can == 10000 else f"OBSERVED_100_TO_{fee_can}")
        canonical_alt = None
        if t0 and t1 and WETH.lower() in (t0.lower(), t1.lower()):
            canonical_alt = t1 if t0.lower() == WETH.lower() else t0
        rows.append({
            "signal_id": r.signal_id,
            "split_role": r.split_role,
            "pool": pool.lower(),
            "decision_block": int(r.decision_block),
            "decision_time": int(r.decision_time),
            "registry_alt": str(r.alt).lower(),
            "fee_raw": int(r.fee),
            "canonical_token0": t0.lower() if t0 else None,
            "canonical_token1": t1.lower() if t1 else None,
            "canonical_alt": canonical_alt.lower() if canonical_alt else None,
            "canonical_fee": int(fee_can) if fee_can is not None else None,
            "canonical_factory": fac.lower() if fac else None,
            "factory_getPool": gp.lower() if gp else None,
            "factory_validation": validated,
            "expected_factory_match": factory_match,
            "raw_tokens_match": raw_tokens_match,
            "canonical_status": status,
            "repair_class": repair_class,
            "repair_reason": reason,
            "explicit_fee_case_check": explicit_check,
            "direct_pool_read": True,
        })
    out = pd.concat([prior_good, pd.DataFrame(rows)], ignore_index=True)
    order = {sid: i for i, sid in enumerate(full_panel["signal_id"])}
    out["_order"] = out["signal_id"].map(order)
    out = out.sort_values("_order").drop(columns="_order").reset_index(drop=True)
    assert len(out) == len(full_panel) == 2399
    assert set(out["repair_class"]).issubset(ALLOWED_REPAIR_CLASSES)
    out.to_parquet(cache_path, index=False)
    return out


def lineage_impact() -> pd.DataFrame:
    rows = [
        ("pool_discovery", "src/build_panel.py:48-82", "fee_bps is selected/carried; pool inclusion and swap join use pool address/type/WETH leg", False, "NONE"),
        ("eligibility", "src/build_panel.py:145-199", "swap count, PIT minute bars, two-sided swap signs; fee is not referenced", False, "NONE"),
        ("swap_features", "src/build_panel.py:180-228", "tick/amount/sender/recipient values keyed by pool; fee is not referenced", False, "NONE"),
        ("liquidity_features", "src/build_panel.py:201-228", "liquidity_raw and its PIT transforms; fee is not referenced", False, "NONE"),
        ("executable_depth_features", "src/build_panel.py:201-228; src/features.py:17-29", "no Quoter depth feature exists; vol_depth is pre_vol/liq_log", False, "NONE"),
        ("friction", "configs/design_freeze.json:51-55; src/build_panel.py", "threshold is frozen but no fee-dependent friction field is computed/applied in panel construction", False, "NONE"),
        ("G2_features", "src/features.py:17-75; src/models_routes.py:293-320", "fee is metadata only and excluded from FEATURE_COLS", False, "NONE"),
        ("C3_feature", "src/models_routes.py:219-230", "C3 score is early_ret only", False, "NONE"),
        ("tick_path_labels", "src/build_labels.py:23-205", "forward tick/liquidity path keyed by pool; fee is not referenced", False, "NONE"),
        ("atomic_labels", "src/build_labels.py:191-229", "Discovery atomic_net_return is tick-path derived; fee enters calibration smoke only and does not alter labels", False, "NONE"),
        ("v1_atomic_execution", "src/v1/atomic_engine.py:64-199", "registry fee enters Quoter and Router calldata for buy/sell", True, "EXECUTION_ONLY"),
    ]
    d = pd.DataFrame(rows, columns=["field", "evidence", "finding", "dirty_fee_impact", "impact_scope"])
    d["lineage_verdict"] = "EXECUTION_ONLY_CONTAMINATION"
    return d


def parse_i256(x: Any) -> int:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0
    s = str(x)
    try:
        v = int(s, 16) if s.lower().startswith("0x") else int(float(s)) if "." in s else int(s)
        return v - 2**256 if v >= 2**255 else v
    except Exception:
        return 0


def load_swap_groups() -> dict[str, pd.DataFrame]:
    s = pd.read_parquet(OUT / "panel" / "early_swaps.parquet")
    s["tick_i"] = [parse_i256(x) for x in s["tick"]]
    return {str(k).lower(): g.sort_values("ts") for k, g in s.groupby("pool", sort=False)}


def barrier_exit(row: Any, swaps: dict[str, pd.DataFrame]) -> tuple[int, str, float, float, float]:
    entry_block = int(row.decision_block) + ENTRY_DELAY_BLOCKS
    g = swaps.get(str(row.pool).lower())
    if g is None or not np.isfinite(float(row.entry_tick)) or float(row.entry_tick) == 0:
        return entry_block + H // 2, "TIMEOUT", 0.0, 0.0, 0.0
    ts = g["ts"].to_numpy(dtype=np.int64)
    ticks = g["tick_i"].to_numpy(dtype=np.float64)
    blocks = pd.to_numeric(g["block_number"], errors="coerce").fillna(entry_block).astype(int).to_numpy()
    i0 = int(np.searchsorted(ts, int(row.decision_time), side="right"))
    i1 = int(np.searchsorted(ts, int(row.decision_time) + H, side="right"))
    if i1 <= i0:
        return entry_block + H // 2, "TIMEOUT", 0.0, 0.0, 0.0
    rets = (ticks[i0:i1] - float(row.entry_tick)) * LN1P
    fblocks = blocks[i0:i1]
    mfe, mae = float(np.nanmax(rets)), float(np.nanmin(rets))
    for i, ret in enumerate(rets):
        if ret >= TP:
            return max(int(fblocks[i]), entry_block), "TP_FIRST", mfe, mae, float(ret)
        if ret <= -SL:
            return max(int(fblocks[i]), entry_block), "SL_FIRST", mfe, mae, float(ret)
    return max(int(fblocks[-1]), entry_block), "TIMEOUT", mfe, mae, float(rets[-1])


def detect_slots(rpc: Rpc, token: str, owner: str, spender: str, block: int) -> dict | None:
    layouts_path = CACHE / "storage_layouts.json"
    layouts = json.loads(layouts_path.read_text()) if layouts_path.exists() else {}
    key_name = f"{token.lower()}:{owner.lower()}:{spender.lower()}"
    if key_name in layouts:
        return layouts[key_name]
    token = Web3.to_checksum_address(token)
    marker_b, marker_a = 0x777733331111, 0x888844442222
    bdata = "0x" + (_BAL + encode(["address"], [owner])).hex()
    bmatch = None
    for slot_range in (range(5), range(5, 20)):
        bcalls = []
        bkeys = []
        for slot in slot_range:
            for layout in ("sol", "vyper"):
                skey = sol_map_key(owner, slot) if layout == "sol" else vyper_map_key(owner, slot)
                ov = {token: {"stateDiff": {skey: word(marker_b)}}}
                bcalls.append(("eth_call", [{"to": token, "data": bdata}, hex(int(block)), ov]))
                bkeys.append((slot, layout, skey))
        bres = rpc.batch(bcalls, chunk_size=10)
        for meta, res in zip(bkeys, bres):
            if not res.get("error") and to_uint(res.get("result")) == marker_b:
                bmatch = meta
                break
        if bmatch is not None:
            break
    if bmatch is None:
        return None
    adata = "0x" + (_ALLOW + encode(["address", "address"], [owner, spender])).hex()
    amatch = None
    for slot_range in (range(5), range(5, 20)):
        acalls = []
        akeys = []
        for slot in slot_range:
            for layout in ("sol", "vyper"):
                skey = nested_key(owner, spender, slot, layout)
                ov = {token: {"stateDiff": {skey: word(marker_a)}}}
                acalls.append(("eth_call", [{"to": token, "data": adata}, hex(int(block)), ov]))
                akeys.append((slot, layout, skey))
        ares = rpc.batch(acalls, chunk_size=10)
        for meta, res in zip(akeys, ares):
            if not res.get("error") and to_uint(res.get("result")) == marker_a:
                amatch = meta
                break
        if amatch is not None:
            break
    if amatch is None:
        return None
    found = {"balance_slot": bmatch[0], "balance_layout": bmatch[1], "allowance_slot": amatch[0], "allowance_layout": amatch[1]}
    layouts[key_name] = found
    layouts_path.write_text(json.dumps(layouts, indent=2) + "\n")
    return found


def state_override(token: str, owner: str, spender: str, slots: dict, amount: int) -> dict:
    bkey = sol_map_key(owner, slots["balance_slot"]) if slots["balance_layout"] == "sol" else vyper_map_key(owner, slots["balance_slot"])
    akey = nested_key(owner, spender, slots["allowance_slot"], slots["allowance_layout"])
    grant = max(int(amount) * 3, int(amount) + 1)
    return {Web3.to_checksum_address(token): {"stateDiff": {bkey: word(grant), akey: word(grant)}}}


def quote_buy(rpc: Rpc, token: str, fee: int, block: int) -> dict:
    params = encode(["(address,address,uint256,uint24,uint160)"], [(WETH, token, AMOUNT_IN, int(fee), 0)])
    tx = {"from": EXECUTOR, "to": QUOTER, "data": "0x" + (_QUOTE + params).hex()}
    res = rpc.eth_call(tx, block)
    if res.get("error"):
        msg = str(res["error"])[:500]
        infra = "transport:" in msg or "missing trie" in msg.lower() or "header not found" in msg.lower()
        return {"status": "INFRASTRUCTURE_UNKNOWN" if infra else "ENTRY_PREFLIGHT_REJECT", "amount": None if infra else 0, "reason": msg}
    raw = res.get("result") or "0x"
    amount = int(raw[2:66], 16) if len(raw) >= 66 else 0
    return {"status": "BUY_PREFLIGHT_PASS" if amount > 0 else "ENTRY_PREFLIGHT_REJECT", "amount": amount, "reason": "" if amount > 0 else "zero_out"}


def buy_calldata(token: str, fee: int) -> str:
    params = encode(["(address,address,uint24,address,uint256,uint256,uint160)"], [(WETH, token, int(fee), EXECUTOR, AMOUNT_IN, 0, 0)])
    return "0x" + (_SWAP + params).hex()


def sell_calldata(token: str, fee: int, amount: int) -> str:
    params = encode(["(address,address,uint24,address,uint256,uint256,uint160)"], [(token, WETH, int(fee), EXECUTOR, int(amount), 0, 0)])
    return "0x" + (_SWAP + params).hex()


def router_call(rpc: Rpc, calldata: str, block: int, override: dict) -> dict:
    tx = {"from": EXECUTOR, "to": ROUTER, "data": calldata, "gas": hex(GAS_LIMIT)}
    res = rpc.eth_call(tx, block, override)
    if res.get("error"):
        msg = str(res["error"])[:800]
        infra = "transport:" in msg or "missing trie" in msg.lower() or "header not found" in msg.lower() or "timeout" in msg.lower()
        return {"status": "INFRASTRUCTURE_UNKNOWN" if infra else "TX_REVERT", "amount": None if infra else 0, "reason": msg}
    raw = res.get("result") or "0x"
    amount = int(raw[2:66], 16) if len(raw) >= 66 else 0
    return {"status": "SUCCESS" if amount > 0 else "TX_REVERT", "amount": amount, "reason": "" if amount > 0 else "zero_out"}


def pnl_for(buy_status: str, exit_status: str | None, weth_out_wei: int | None) -> tuple[float | None, float]:
    if buy_status in ("ENTRY_PREFLIGHT_REJECT", "EXCLUDED_TRUE_INVALID_POOL"):
        return 0.0, 0.0
    if buy_status == "ENTRY_TX_REVERT":
        return -GAS_BUY_WETH, GAS_BUY_WETH
    if buy_status == "INFRASTRUCTURE_UNKNOWN":
        return None, 0.0
    if buy_status != "BUY_SUCCESS":
        return None, 0.0
    if exit_status in ("EXIT_SUCCESS", "EXIT_TEMP_FAIL_RECOVERED", "PERMANENT_UNSELLABLE"):
        recoverable = (weth_out_wei or 0) / 1e18
        gas = GAS_BUY_WETH + GAS_SELL_WETH
        return recoverable - WETH_IN - gas, gas
    return None, GAS_BUY_WETH


def replay_holdout(hold: pd.DataFrame, metadata: pd.DataFrame, panel: pd.DataFrame, swaps: dict[str, pd.DataFrame], rpc: Rpc) -> pd.DataFrame:
    cache_path = CACHE / "repaired_holdout_checkpoint.parquet"
    existing: dict[str, dict] = {}
    if cache_path.exists():
        old = pd.read_parquet(cache_path)
        if not old["repair_class"].eq("INFRASTRUCTURE_UNKNOWN").any():
            existing = {r.signal_id: r._asdict() for r in old.itertuples(index=False)}
        print(f"[replay] checkpoint rows={len(existing)}", flush=True)
    pcols = ["signal_id", "pool", "alt", "fee", "decision_block", "decision_time", "entry_tick"]
    base = hold.merge(panel[pcols], on="signal_id", suffixes=("", "_panel"), validate="one_to_one")
    mcols = ["signal_id", "canonical_token0", "canonical_token1", "canonical_alt", "canonical_fee", "canonical_factory", "canonical_status", "repair_class", "factory_validation"]
    base = base.merge(metadata[mcols], on="signal_id", validate="one_to_one")
    rows = list(existing.values())
    done = set(existing)
    weth_slots = None
    for i, r in enumerate(base.itertuples(index=False)):
        if r.signal_id in done:
            continue
        entry_block = int(r.decision_block) + ENTRY_DELAY_BLOCKS
        exit_block, tick_event, mfe, mae, path_ret = barrier_exit(r, swaps)
        fee_can = int(r.canonical_fee) if pd.notna(r.canonical_fee) else None
        token = Web3.to_checksum_address(r.canonical_alt) if isinstance(r.canonical_alt, str) and r.canonical_alt else None
        qstatus = None
        buy_status = "INFRASTRUCTURE_UNKNOWN"
        actual = None
        exit_status = None
        weth_out = None
        buy_reason = ""
        exit_reason = ""
        exit_attempt_block = exit_block
        if r.repair_class == "TRUE_INVALID_POOL":
            buy_status = "EXCLUDED_TRUE_INVALID_POOL"
            buy_reason = r.canonical_status
        elif fee_can is None or token is None or r.repair_class == "INFRASTRUCTURE_UNKNOWN":
            buy_status = "INFRASTRUCTURE_UNKNOWN"
            buy_reason = "canonical_metadata_unavailable"
        else:
            q = quote_buy(rpc, token, fee_can, entry_block)
            qstatus = q["status"]
            if weth_slots is None:
                weth_slots = detect_slots(rpc, WETH, EXECUTOR, ROUTER, entry_block)
            if weth_slots is None:
                buy_status = "INFRASTRUCTURE_UNKNOWN"
                buy_reason = "weth_storage_layout_unknown"
            else:
                bov = state_override(WETH, EXECUTOR, ROUTER, weth_slots, AMOUNT_IN)
                br = router_call(rpc, buy_calldata(token, fee_can), entry_block, bov)
                if br["status"] == "SUCCESS":
                    buy_status = "BUY_SUCCESS"
                    actual = int(br["amount"])
                elif br["status"] == "INFRASTRUCTURE_UNKNOWN" or qstatus == "INFRASTRUCTURE_UNKNOWN":
                    buy_status = "INFRASTRUCTURE_UNKNOWN"
                    buy_reason = br.get("reason") or q.get("reason", "")
                elif qstatus == "ENTRY_PREFLIGHT_REJECT":
                    buy_status = "ENTRY_PREFLIGHT_REJECT"
                    buy_reason = q.get("reason", "")
                else:
                    buy_status = "ENTRY_TX_REVERT"
                    buy_reason = br.get("reason", "")
            if buy_status == "BUY_SUCCESS" and actual:
                tslots = detect_slots(rpc, token, EXECUTOR, ROUTER, exit_block)
                if tslots is None:
                    exit_status = "EXIT_INFRASTRUCTURE_UNKNOWN"
                    exit_reason = "token_storage_layout_unknown"
                else:
                    sov = state_override(token, EXECUTOR, ROUTER, tslots, actual)
                    sr = None
                    for add in RETRY_BLOCKS:
                        exit_attempt_block = int(exit_block) + add
                        sr = router_call(rpc, sell_calldata(token, fee_can, actual), exit_attempt_block, sov)
                        if sr["status"] != "INFRASTRUCTURE_UNKNOWN":
                            break
                    if sr is None or sr["status"] == "INFRASTRUCTURE_UNKNOWN":
                        exit_status = "EXIT_INFRASTRUCTURE_UNKNOWN"
                        exit_reason = (sr or {}).get("reason", "retry_exhausted")
                    elif sr["status"] == "TX_REVERT":
                        exit_status = "PERMANENT_UNSELLABLE"
                        weth_out = 0
                        exit_reason = sr.get("reason", "")
                    else:
                        weth_out = int(sr["amount"])
                        if weth_out < AMOUNT_IN * (1.0 - ECONOMIC_TAX):
                            exit_status = "PERMANENT_UNSELLABLE"
                            exit_reason = "tax>=90%"
                        else:
                            exit_status = "EXIT_TEMP_FAIL_RECOVERED" if exit_attempt_block != exit_block else "EXIT_SUCCESS"
        pnl, gas = pnl_for(buy_status, exit_status, weth_out)
        capital_hours = (max(exit_attempt_block - entry_block, 1) * 2 / 3600) if buy_status == "BUY_SUCCESS" else 0.0
        rows.append({
            "signal_id": r.signal_id,
            "pool": str(r.pool).lower(),
            "decision_time": int(r.decision_time),
            "decision_block": int(r.decision_block),
            "entry_block": entry_block,
            "exit_block": int(exit_attempt_block),
            "label_end_time": int(r.label_end_time),
            "canonical_status": r.canonical_status,
            "fee_raw": int(r.fee),
            "fee_canonical": fee_can,
            "canonical_token0": r.canonical_token0,
            "canonical_token1": r.canonical_token1,
            "canonical_alt": str(token).lower() if token else None,
            "repair_class": r.repair_class,
            "buy_preflight_status": qstatus,
            "buy_status": buy_status,
            "actual_token_received": str(actual) if actual is not None else None,
            "exit_status": exit_status,
            "weth_out": str(weth_out) if weth_out is not None else None,
            "gas_weth": gas,
            "pnl_weth": pnl,
            "capital_hours": capital_hours,
            "tick_path_event": tick_event,
            "mfe": mfe,
            "mae": mae,
            "tick_path_return": path_ret,
            "buy_reason": buy_reason,
            "exit_reason": exit_reason,
            "p0_selected": bool(r.p0_selected),
            "p1_selected": bool(r.p1_selected),
            "p2_selected": bool(r.p2_selected),
            "executor": EXECUTOR.lower(),
            "router": ROUTER.lower(),
            "amount_in_wei": str(AMOUNT_IN),
        })
        if len(rows) % 20 == 0:
            pd.DataFrame(rows).sort_values("decision_time").to_parquet(cache_path, index=False)
            print(f"[replay] {len(rows)}/480 rpc_calls={rpc.calls}", flush=True)
    out = pd.DataFrame(rows).sort_values("decision_time").reset_index(drop=True)
    assert len(out) == 480 and out.signal_id.nunique() == 480
    out.to_parquet(cache_path, index=False)
    return out


class LocalFork:
    def __init__(self, upstream_url: str, port: int = 18545):
        self.upstream_url = upstream_url
        self.url = f"http://127.0.0.1:{port}"
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.rpc = Rpc(self.url, "LOCAL_FORK")

    def start(self, block: int) -> None:
        log = (CACHE / "anvil.log").open("ab")
        anvil = str(Path("/root/.foundry/bin/anvil"))
        self.proc = subprocess.Popen(
            [anvil, "--fork-url", self.upstream_url, "--fork-block-number", str(int(block)), "--port", str(self.port), "--silent"],
            stdout=log,
            stderr=log,
            env={**os.environ, "RUST_LOG": "error"},
        )
        for _ in range(80):
            if self.rpc.one("eth_chainId", [], retries=1).get("result"):
                return
            time.sleep(0.25)
        raise RuntimeError("anvil_start_timeout")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def reset(self, block: int) -> bool:
        r = self.rpc.one("anvil_reset", [{"forking": {"jsonRpcUrl": self.upstream_url, "blockNumber": int(block)}}])
        return not bool(r.get("error"))

    def set_storage(self, token: str, key: str, value: str) -> bool:
        r = self.rpc.one("anvil_setStorageAt", [token, key, value])
        return not bool(r.get("error"))

    def execute(self, token_in: str, token_out: str, amount: int, calldata: str, block: int, slots: dict) -> dict:
        if not self.reset(block):
            return {"status": "LOCAL_FORK_RESET_FAILED"}
        self.rpc.one("anvil_impersonateAccount", [EXECUTOR])
        self.rpc.one("anvil_setBalance", [EXECUTOR, word(10**21)])
        ov = state_override(token_in, EXECUTOR, ROUTER, slots, amount)
        diffs = ov[Web3.to_checksum_address(token_in)]["stateDiff"]
        for key, value in diffs.items():
            if not self.set_storage(Web3.to_checksum_address(token_in), key, value):
                return {"status": "LOCAL_FORK_STORAGE_FAILED"}
        bdata = "0x" + (_BAL + encode(["address"], [EXECUTOR])).hex()
        before_r = self.rpc.one("eth_call", [{"to": token_out, "data": bdata}, "latest"])
        before = to_uint(before_r.get("result")) or 0
        tx = {"from": EXECUTOR, "to": ROUTER, "data": calldata, "gas": hex(GAS_LIMIT)}
        sent = self.rpc.one("eth_sendTransaction", [tx])
        if sent.get("error"):
            return {"status": "LOCAL_FORK_TX_REVERT", "reason": str(sent["error"])[:500]}
        rc = {}
        for _ in range(100):
            receipt = self.rpc.one("eth_getTransactionReceipt", [sent.get("result")])
            if receipt.get("result") is not None:
                rc = receipt["result"]
                break
            time.sleep(0.05)
        if not rc:
            return {"status": "LOCAL_FORK_RECEIPT_TIMEOUT", "tx_hash": sent.get("result")}
        after_r = self.rpc.one("eth_call", [{"to": token_out, "data": bdata}, "latest"])
        after = to_uint(after_r.get("result")) or 0
        return {
            "status": "LOCAL_FORK_TX_SUCCESS" if int(rc.get("status", "0x0"), 16) == 1 else "LOCAL_FORK_TX_REVERT",
            "amount_received": max(after - before, 0),
            "gas_used": int(rc.get("gasUsed", "0x0"), 16),
            "tx_hash": sent.get("result"),
        }


def trace_buy(rpc: Rpc, row: Any, weth_slots: dict | None) -> dict:
    if weth_slots is None:
        return {"status": "TRACE_STATE_OVERRIDE_UNAVAILABLE", "gas_used": None}
    ov = state_override(WETH, EXECUTOR, ROUTER, weth_slots, AMOUNT_IN)
    tx = {"from": EXECUTOR, "to": ROUTER, "data": buy_calldata(Web3.to_checksum_address(row.canonical_alt), int(row.fee_canonical)), "gas": hex(GAS_LIMIT)}
    config = {"tracer": "callTracer", "timeout": "20s", "stateOverrides": ov}
    res = rpc.one("debug_traceCall", [tx, hex(int(row.entry_block)), config])
    if res.get("error"):
        return {"status": "TRACE_UNAVAILABLE", "gas_used": None, "reason": str(res["error"])[:500]}
    tr = res.get("result") or {}
    failed = bool(tr.get("error") or tr.get("revertReason"))
    return {"status": "TRACE_REVERT" if failed else "TRACE_SUCCESS", "gas_used": int(tr.get("gasUsed", "0x0"), 16), "reason": str(tr.get("error") or tr.get("revertReason") or "")[:500]}


def crosscheck(replay: pd.DataFrame, rpc_a: Rpc, rpc_b: Rpc | None) -> pd.DataFrame:
    selected = replay[replay["p0_selected"] | replay["p1_selected"] | replay["p2_selected"]].copy()
    if selected.empty:
        return pd.DataFrame()
    first = selected.iloc[0]
    weth_slots = detect_slots(rpc_a, WETH, EXECUTOR, ROUTER, int(first.entry_block))
    token_layouts = json.loads((CACHE / "storage_layouts.json").read_text()) if (CACHE / "storage_layouts.json").exists() else {}
    fork = LocalFork(rpc_a.url)
    fork_available = True
    fork_error = ""
    try:
        fork.start(int(first.entry_block))
    except Exception as exc:
        fork_available = False
        fork_error = f"{type(exc).__name__}:{exc}"
    rows = []
    try:
        for i, r in enumerate(selected.itertuples(index=False)):
            trace = trace_buy(rpc_a, r, weth_slots) if r.fee_canonical is not None and r.canonical_alt else {"status": "TRACE_NOT_APPLICABLE", "gas_used": None}
            b_status = "RPC_B_UNAVAILABLE"
            b_buy = None
            b_exit = None
            if rpc_b is not None and r.fee_canonical is not None and r.canonical_alt:
                bw = detect_slots(rpc_b, WETH, EXECUTOR, ROUTER, int(r.entry_block))
                if bw:
                    br = router_call(rpc_b, buy_calldata(Web3.to_checksum_address(r.canonical_alt), int(r.fee_canonical)), int(r.entry_block), state_override(WETH, EXECUTOR, ROUTER, bw, AMOUNT_IN))
                    b_buy = br["status"]
                    b_status = "RPC_B_EXECUTED"
                    if br["status"] == "SUCCESS" and r.actual_token_received:
                        ts = detect_slots(rpc_b, Web3.to_checksum_address(r.canonical_alt), EXECUTOR, ROUTER, int(r.exit_block))
                        if ts:
                            sr = router_call(rpc_b, sell_calldata(Web3.to_checksum_address(r.canonical_alt), int(r.fee_canonical), int(r.actual_token_received)), int(r.exit_block), state_override(Web3.to_checksum_address(r.canonical_alt), EXECUTOR, ROUTER, ts, int(r.actual_token_received)))
                            b_exit = sr["status"]
            local_buy = {"status": "LOCAL_FORK_UNAVAILABLE", "reason": fork_error}
            local_sell = {"status": "LOCAL_FORK_NOT_RUN"}
            if fork_available and r.fee_canonical is not None and r.canonical_alt and weth_slots:
                local_buy = fork.execute(WETH, Web3.to_checksum_address(r.canonical_alt), AMOUNT_IN, buy_calldata(Web3.to_checksum_address(r.canonical_alt), int(r.fee_canonical)), int(r.entry_block), weth_slots)
                amount_local = int(local_buy.get("amount_received") or 0)
                layout_key = f"{str(r.canonical_alt).lower()}:{EXECUTOR.lower()}:{ROUTER.lower()}"
                tslots = token_layouts.get(layout_key)
                if local_buy.get("status") == "LOCAL_FORK_TX_SUCCESS" and amount_local > 0 and tslots:
                    local_sell = fork.execute(Web3.to_checksum_address(r.canonical_alt), WETH, amount_local, sell_calldata(Web3.to_checksum_address(r.canonical_alt), int(r.fee_canonical), amount_local), int(r.exit_block), tslots)
            rows.append({
                "signal_id": r.signal_id,
                "repair_class": r.repair_class,
                "rpc_a_buy_status": r.buy_status,
                "rpc_a_exit_status": r.exit_status,
                "rpc_b_status": b_status,
                "rpc_b_buy_status": b_buy,
                "rpc_b_exit_status": b_exit,
                "debug_trace_status": trace.get("status"),
                "debug_trace_gas_used": trace.get("gas_used"),
                "debug_trace_reason": trace.get("reason", ""),
                "local_fork_buy_status": local_buy.get("status"),
                "local_fork_buy_received": str(local_buy.get("amount_received")) if local_buy.get("amount_received") is not None else None,
                "local_fork_buy_gas_used": local_buy.get("gas_used"),
                "local_fork_sell_status": local_sell.get("status"),
                "local_fork_weth_received": str(local_sell.get("amount_received")) if local_sell.get("amount_received") is not None else None,
                "local_fork_sell_gas_used": local_sell.get("gas_used"),
                "execution_truth_status": "COMPLETE" if rpc_b is not None and trace.get("status") in ("TRACE_SUCCESS", "TRACE_REVERT") and str(local_buy.get("status", "")).startswith("LOCAL_FORK_TX") else "EXECUTION_TRUTH_INCOMPLETE",
            })
            if (i + 1) % 10 == 0:
                pd.DataFrame(rows).to_csv(CACHE / "crosscheck_checkpoint.csv", index=False)
                print(f"[crosscheck] {i+1}/{len(selected)}", flush=True)
    finally:
        fork.stop()
    return pd.DataFrame(rows)


def wallet_policy(replay: pd.DataFrame, sel_col: str, policy: str) -> tuple[dict, pd.DataFrame, set[str]]:
    sel = replay[replay[sel_col]].sort_values("decision_time").copy()
    wallet = INITIAL_WALLET
    open_pos: list[dict] = []
    events = []
    contributions = []
    executed_fills = 0
    executed_exit_success = 0
    executed_fatal = 0
    capital_hours = 0.0
    fatal_ids: set[str] = set()
    if len(sel):
        events.append({
            "policy": policy,
            "event_time": int(sel["decision_time"].min()) - 1,
            "signal_id": "INITIAL_WALLET",
            "event": "INITIAL",
            "wallet_weth": INITIAL_WALLET,
            "equity_weth": INITIAL_WALLET,
            "delta_weth": 0.0,
            "open_positions": 0,
        })

    def settle(until: int) -> None:
        nonlocal wallet
        due = sorted([x for x in open_pos if x["exit_time"] <= until], key=lambda x: x["exit_time"])
        for pos in due:
            delta = float(pos["recoverable"]) - GAS_SELL_WETH
            wallet += delta
            contribution = delta - WETH_IN - GAS_BUY_WETH
            contributions.append((pos["signal_id"], contribution))
            open_pos.remove(pos)
            equity = wallet + len(open_pos) * WETH_IN
            events.append({"policy": policy, "event_time": pos["exit_time"], "signal_id": pos["signal_id"], "event": "EXIT", "wallet_weth": wallet, "equity_weth": equity, "delta_weth": delta, "open_positions": len(open_pos)})

    for r in sel.itertuples(index=False):
        settle(int(r.decision_time))
        if r.buy_status == "ENTRY_TX_REVERT":
            wallet -= GAS_BUY_WETH
            contributions.append((r.signal_id, -GAS_BUY_WETH))
            events.append({"policy": policy, "event_time": int(r.decision_time), "signal_id": r.signal_id, "event": "ENTRY_TX_REVERT", "wallet_weth": wallet, "equity_weth": wallet + len(open_pos) * WETH_IN, "delta_weth": -GAS_BUY_WETH, "open_positions": len(open_pos)})
        elif r.buy_status == "BUY_SUCCESS" and pd.notna(r.pnl_weth):
            if len(open_pos) >= MAX_CONCURRENT or wallet < WETH_IN + GAS_BUY_WETH:
                events.append({"policy": policy, "event_time": int(r.decision_time), "signal_id": r.signal_id, "event": "CAPITAL_CONSTRAINED_SKIP", "wallet_weth": wallet, "equity_weth": wallet + len(open_pos) * WETH_IN, "delta_weth": 0.0, "open_positions": len(open_pos)})
                continue
            wallet -= WETH_IN + GAS_BUY_WETH
            executed_fills += 1
            capital_hours += float(r.capital_hours)
            recoverable = float(int(r.weth_out) / 1e18) if r.weth_out is not None and pd.notna(r.weth_out) else 0.0
            exit_time = int(r.decision_time + max(int(r.exit_block) - int(r.entry_block), 1) * 2)
            open_pos.append({"signal_id": r.signal_id, "exit_time": exit_time, "recoverable": recoverable})
            if r.exit_status == "PERMANENT_UNSELLABLE":
                fatal_ids.add(r.signal_id)
                executed_fatal += 1
            elif r.exit_status in ("EXIT_SUCCESS", "EXIT_TEMP_FAIL_RECOVERED"):
                executed_exit_success += 1
            events.append({"policy": policy, "event_time": int(r.decision_time), "signal_id": r.signal_id, "event": "BUY_SUCCESS", "wallet_weth": wallet, "equity_weth": wallet + len(open_pos) * WETH_IN, "delta_weth": -(WETH_IN + GAS_BUY_WETH), "open_positions": len(open_pos)})
    settle(2**63 - 1)
    curve = pd.DataFrame(events).sort_values(["event_time", "event"]).reset_index(drop=True) if events else pd.DataFrame(columns=["policy", "event_time", "signal_id", "event", "wallet_weth", "equity_weth", "delta_weth", "open_positions"])
    if len(curve):
        peak = curve.equity_weth.cummax()
        max_dd = float((curve.equity_weth - peak).min())
    else:
        max_dd = 0.0
    contrib = sorted(contributions, key=lambda x: x[1], reverse=True)
    total = float(wallet - INITIAL_WALLET)
    top1_pnl = sum(x[1] for x in contrib[:1])
    top5_pnl = sum(x[1] for x in contrib[:5])
    repaired = sel[sel.repair_class == "REPAIRABLE_FEE_UNIT_MISMATCH"]
    known_filled = sel[(sel.buy_status == "BUY_SUCCESS") & sel.pnl_weth.notna()]
    atomic_buy_filled = int((sel.buy_status == "BUY_SUCCESS").sum())
    resolved_filled = executed_fills
    metric = {
        "policy": policy,
        "label": "HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC",
        "model_selected": int(len(sel)),
        "repairable_selected": int((sel.repair_class == "REPAIRABLE_FEE_UNIT_MISMATCH").sum()),
        "true_invalid_selected": int((sel.repair_class == "TRUE_INVALID_POOL").sum()),
        "buy_filled": atomic_buy_filled,
        "resolved_filled": resolved_filled,
        "atomic_exit_success": int(known_filled.exit_status.isin(["EXIT_SUCCESS", "EXIT_TEMP_FAIL_RECOVERED"]).sum()),
        "exit_success": executed_exit_success,
        "atomic_fatal_position": int(known_filled.exit_status.eq("PERMANENT_UNSELLABLE").sum()),
        "fatal_position": executed_fatal,
        "fill_rate": atomic_buy_filled / len(sel) if len(sel) else 0.0,
        "pnl_per_selected": total / len(sel) if len(sel) else None,
        "pnl_per_filled": total / resolved_filled if resolved_filled else None,
        "pnl_per_resolved_filled": total / resolved_filled if resolved_filled else None,
        "total_pnl_weth": total,
        "known_sell_wallet_delta_weth": total,
        "total_pnl_complete": bool(int(sel.pnl_weth.isna().sum()) == 0),
        "pnl_scope": "ALL_SELECTED" if int(sel.pnl_weth.isna().sum()) == 0 else "RESOLVED_SELL_TRUTH_ONLY",
        "wallet_return": total / INITIAL_WALLET,
        "max_drawdown": max_dd,
        "capital_hours": float(sel.loc[sel.buy_status == "BUY_SUCCESS", "capital_hours"].fillna(0).sum()),
        "top1_share": top1_pnl / total if total != 0 else None,
        "top5_share": top5_pnl / total if total != 0 else None,
        "pnl_ex_top5": total - top5_pnl,
        "repaired_atomic_filled": int((repaired.buy_status == "BUY_SUCCESS").sum()),
        "unknown_selected": int(sel.pnl_weth.isna().sum()),
    }
    return metric, curve, fatal_ids


def main() -> None:
    input_paths = {
        "panel": OUT / "panel" / "greenfield_panel_15-60m.parquet",
        "holdout_scores": OUT_V1 / "HOLDOUT_SCORES.parquet",
        "v11_pool_audit": OUT_V11 / "CANONICAL_POOL_AUDIT.parquet",
        "v11_final": OUT_V11 / "FINAL_EXECUTION_TRUTH.json",
    }
    rpc_a_url = Path("/tmp/rpc_url.txt").read_text().strip()
    rpc_a = Rpc(rpc_a_url, "A")
    rpc_b_url = load_rpc_b()
    rpc_b = Rpc(rpc_b_url, "B") if rpc_b_url and rpc_b_url != rpc_a_url else None
    panel = pd.read_parquet(input_paths["panel"])
    hold = pd.read_parquet(input_paths["holdout_scores"])
    assert len(panel) == 2399 and len(hold) == 480
    assert set(hold.signal_id).issubset(set(panel.signal_id))

    print("=== CANONICAL METADATA ALL ===", flush=True)
    metadata = metadata_audit(panel, rpc_a)
    metadata.to_parquet(OUT_V12 / "CANONICAL_METADATA_ALL.parquet", index=False)
    print(metadata.repair_class.value_counts().to_dict(), flush=True)

    print("=== FIELD LINEAGE IMPACT ===", flush=True)
    lineage = lineage_impact()
    lineage.to_csv(OUT_V12 / "FIELD_LINEAGE_IMPACT.csv", index=False)
    lineage_verdict = "MODEL_LINEAGE_INVALIDATED" if lineage.loc[lineage.field != "v1_atomic_execution", "dirty_fee_impact"].any() else "EXECUTION_ONLY_CONTAMINATION"
    print(lineage_verdict, flush=True)

    print("=== REPAIRED ATOMIC REPLAY 480 ===", flush=True)
    swaps = load_swap_groups()
    replay = replay_holdout(hold, metadata, panel, swaps, rpc_a)
    replay.to_parquet(OUT_V12 / "REPAIRED_HOLDOUT_OUTCOMES.parquet", index=False)
    print(replay.buy_status.value_counts(dropna=False).to_dict(), flush=True)
    print(replay.exit_status.value_counts(dropna=False).to_dict(), flush=True)

    print("=== RPC / TRACE / LOCAL FORK CROSSCHECK ===", flush=True)
    cross = crosscheck(replay, rpc_a, rpc_b)
    cross.to_csv(OUT_V12 / "RPC_FORK_CROSSCHECK.csv", index=False)
    execution_truth_status = "COMPLETE" if len(cross) and cross.execution_truth_status.eq("COMPLETE").all() else "EXECUTION_TRUTH_INCOMPLETE"
    print(execution_truth_status, flush=True)

    print("=== FROZEN POLICY DIAGNOSTIC ===", flush=True)
    policy_defs = [("P0_C3_MOMENTUM", "p0_selected"), ("P1_G2_COMPETING", "p1_selected"), ("P2_C3_G2_VETO", "p2_selected")]
    metrics = []
    curves = []
    fatal_sets: dict[str, set[str]] = {}
    for name, col in policy_defs:
        metric, curve, fatal = wallet_policy(replay, col, name)
        metrics.append(metric)
        curves.append(curve)
        fatal_sets[name] = fatal
    overlap = {}
    for a, _ in policy_defs:
        overlap[a] = {b: len(fatal_sets[a] & fatal_sets[b]) for b, _ in policy_defs if b != a}
    for metric in metrics:
        metric["fatal_overlap_between_policies"] = json.dumps(overlap[metric["policy"]], sort_keys=True)
    comp = pd.DataFrame(metrics)
    comp.to_csv(OUT_V12 / "REPAIRED_POLICY_COMPARISON.csv", index=False)
    pd.concat(curves, ignore_index=True).to_parquet(OUT_V12 / "WALLET_EQUITY_CURVES.parquet", index=False)
    print(comp.to_string(index=False), flush=True)

    p1 = comp[comp.policy == "P1_G2_COMPETING"].iloc[0]
    p1_rows = replay[replay.p1_selected & (replay.buy_status == "BUY_SUCCESS") & replay.pnl_weth.notna()]
    g2_filled_pnl = float(p1_rows.pnl_weth.sum())
    repaired_g2_filled = int((p1_rows.repair_class == "REPAIRABLE_FEE_UNIT_MISMATCH").sum())
    kill_g2 = bool(len(p1_rows) > 0 and g2_filled_pnl < 0)
    non_top5_dominated = bool(pd.notna(p1.pnl_ex_top5) and float(p1.pnl_ex_top5) > 0)
    start_shadow = bool(len(p1_rows) > 0 and g2_filled_pnl > 0 and non_top5_dominated)
    rebuild_v2 = lineage_verdict == "MODEL_LINEAGE_INVALIDATED"
    if rebuild_v2:
        verdict = "MODEL_LINEAGE_INVALIDATED"
    elif execution_truth_status != "COMPLETE" or replay.loc[replay.buy_status == "BUY_SUCCESS", "exit_status"].eq("EXIT_INFRASTRUCTURE_UNKNOWN").any():
        verdict = "SELL_TRUTH_INCOMPLETE"
    elif len(p1_rows) == 0 or repaired_g2_filled == 0:
        verdict = "INSUFFICIENT_REPAIRED_SAMPLE"
    elif replay.loc[replay.p1_selected, "buy_status"].isin(["ENTRY_PREFLIGHT_REJECT", "ENTRY_TX_REVERT"]).all():
        verdict = "TRUE_15_60M_UNTRADEABILITY"
    elif g2_filled_pnl < 0:
        verdict = "EXECUTION_ONLY_CONTAMINATION_G2_REPLAY_NEGATIVE"
    else:
        verdict = "EXECUTION_ONLY_CONTAMINATION_G2_REPLAY_POSITIVE"
    assert verdict in ALLOWED_VERDICTS

    final = {
        "created_utc": utcnow(),
        "role": "Base Greenfield V1.2 Canonical Metadata Repair Replay Agent",
        "verdict": verdict,
        "lineage_verdict": lineage_verdict,
        "execution_truth_status": execution_truth_status,
        "result_label": "HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC",
        "sealed_confirmation": False,
        "kill_g2": kill_g2,
        "start_prospective_shadow": start_shadow,
        "rebuild_greenfield_v2": rebuild_v2,
        "deploy_allowed": False,
        "frozen": {
            "policies": [x[0] for x in policy_defs],
            "scores_unchanged": True,
            "selected_decisions_unchanged": True,
            "H_seconds": H,
            "TB_A": {"tp": TP, "sl": SL},
            "amountIn_wei": str(AMOUNT_IN),
            "wallet_weth": INITIAL_WALLET,
            "max_concurrent": MAX_CONCURRENT,
            "executor": EXECUTOR.lower(),
            "router": ROUTER.lower(),
            "gas_policy_weth": {"buy": GAS_BUY_WETH, "sell": GAS_SELL_WETH},
            "retry_blocks_on_infrastructure_unknown": list(RETRY_BLOCKS),
        },
        "canonical_metadata": {
            "historical_panel_rows": int(len(metadata)),
            "holdout_rows_in_panel": int(metadata.signal_id.isin(hold.signal_id).sum()),
            "repair_class_counts_all": {str(k): int(v) for k, v in metadata.repair_class.value_counts().items()},
            "repair_class_counts_holdout": {str(k): int(v) for k, v in metadata[metadata.signal_id.isin(hold.signal_id)].repair_class.value_counts().items()},
            "factory_validated_all": int(metadata.factory_validation.sum()),
        },
        "replay": {
            "rows": int(len(replay)),
            "buy_status_counts": {str(k): int(v) for k, v in replay.buy_status.value_counts(dropna=False).items()},
            "exit_status_counts": {str(k): int(v) for k, v in replay.exit_status.value_counts(dropna=False).items()},
            "g2_buy_filled_n": int((replay.p1_selected & replay.buy_status.eq("BUY_SUCCESS")).sum()),
            "g2_filled_n": int(len(p1_rows)),
            "g2_repaired_filled_n": repaired_g2_filled,
            "g2_filled_pnl_weth": g2_filled_pnl,
            "g2_non_top5_dominated": non_top5_dominated,
        },
        "double_execution": {
            "selected_union_rows": int(len(cross)),
            "archive_rpc_a": "EXECUTED",
            "archive_rpc_b": "EXECUTED" if rpc_b else "UNAVAILABLE",
            "debug_trace_rows": int(cross.debug_trace_status.isin(["TRACE_SUCCESS", "TRACE_REVERT"]).sum()) if len(cross) else 0,
            "local_fork_buy_rows": int(cross.local_fork_buy_status.astype(str).str.startswith("LOCAL_FORK_TX").sum()) if len(cross) else 0,
            "local_fork_sell_rows": int(cross.local_fork_sell_status.astype(str).str.startswith("LOCAL_FORK_TX").sum()) if len(cross) else 0,
        },
        "policies": metrics,
        "input_sha256": {k: sha256_file(v) for k, v in input_paths.items()},
        "rpc": {"A_url_sha256": rpc_a.url_sha256, "B_url_sha256": rpc_b.url_sha256 if rpc_b else None, "A_calls": rpc_a.calls, "B_calls": rpc_b.calls if rpc_b else 0},
        "outputs": [
            "CANONICAL_METADATA_ALL.parquet", "FIELD_LINEAGE_IMPACT.csv", "REPAIRED_HOLDOUT_OUTCOMES.parquet",
            "RPC_FORK_CROSSCHECK.csv", "REPAIRED_POLICY_COMPARISON.csv", "WALLET_EQUITY_CURVES.parquet",
            "FINAL_CANONICAL_REPLAY.json", "REPRODUCE.md",
        ],
    }
    (OUT_V12 / "FINAL_CANONICAL_REPLAY.json").write_text(json.dumps(final, indent=2) + "\n")
    code_hash = sha256_file(Path(__file__))
    reproduce = f"""# Base Greenfield V1.2 Canonical Repair Replay\n\nLabel: **HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC**. This is not sealed confirmation.\n\n```bash\ncd /root/base_greenfield_directional_v0\n.venv/bin/python -m src.v1_2.canonical_repair_replay 2>&1 | tee out_v1_2/RUN.log\n```\n\nInputs remain read-only under `out/`, `out_v1/`, and `out_v1_1/`. Archive RPC A is read from `/tmp/rpc_url.txt`; an independent RPC B is read from `/tmp/rpc_url_b.txt` or `/tmp/rpc_url2.txt`. Local-fork transactions require `/root/.foundry/bin/anvil`.\n\nThe 2,399-row historical panel already contains all 480 holdout rows, so `CANONICAL_METADATA_ALL.parquet` has 2,399 unique signal rows, not 2,879 duplicated rows. Every row performs direct `token0/token1/fee/factory` calls at its decision block followed by canonical factory `getPool`.\n\nFrozen execution: H={H}s, TB_A=+50%/-35%, amountIn={AMOUNT_IN} wei, wallet=0.5 WETH, max_concurrent=3, executor={EXECUTOR.lower()}, router={ROUTER.lower()}.\n\nGas debits and infrastructure retry schedule are inherited from the frozen V1 execution implementation: 0.0001 WETH per attempted leg and retry blocks [0,5,30,150] only for infrastructure-unknown sell calls.\n\nCode SHA256: `{code_hash}`  \nFinal verdict: `{verdict}`  \nExecution truth: `{execution_truth_status}`\n"""
    (OUT_V12 / "REPRODUCE.md").write_text(reproduce)
    print(json.dumps(final, indent=2), flush=True)


if __name__ == "__main__":
    main()
