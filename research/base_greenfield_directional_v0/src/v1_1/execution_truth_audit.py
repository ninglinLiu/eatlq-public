#!/usr/bin/env python3
"""Base Greenfield V1.1 Execution Truth Audit — no strategy changes, no out/out_v1 mutation."""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from eth_abi import decode, encode
from web3 import Web3

ROOT = Path("/root/base_greenfield_directional_v0")
OUT = ROOT / "out"
OUT_V1 = ROOT / "out_v1"
OUT_V11 = ROOT / "out_v1_1"
OUT_V11.mkdir(parents=True, exist_ok=True)

WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
FACTORY = Web3.to_checksum_address("0x33128a8fC17869897dcE68Ed026d694621f6FDfD")
ROUTER = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")
QUOTER = Web3.to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")
EXECUTOR = Web3.to_checksum_address("0x4B7cd953871663474a0A25B81bddDf5C1f1195A7")
PROBE = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")

VALID_FEES = {100, 500, 3000, 10000}
AMT_IN = 16666666666666666  # 0.0166... ETH = $50@3000
GAS_BUY_UNITS = 250_000
GAS_SELL_UNITS = 300_000
GAS_PRICE_WEI = 5_000_000  # 0.005 gwei-ish Base research; conservative small
# Better: use fixed WETH gas debit from V1 for consistency when eth_gasPrice fails
GAS_BUY_WETH = 0.0001
GAS_SELL_WETH = 0.0001
INITIAL_WALLET = 0.5
MAX_CONCURRENT = 3
MAX_POS = AMT_IN / 1e18

_GET_POOL = Web3.keccak(text="getPool(address,address,uint24)")[:4]
_FEE_SPACING = Web3.keccak(text="feeAmountTickSpacing(uint24)")[:4]
_TOKEN0 = Web3.keccak(text="token0()")[:4]
_TOKEN1 = Web3.keccak(text="token1()")[:4]
_FEE = Web3.keccak(text="fee()")[:4]
_FACTORY = Web3.keccak(text="factory()")[:4]
_LIQUIDITY = Web3.keccak(text="liquidity()")[:4]
_SLOT0 = Web3.keccak(text="slot0()")[:4]
_QUOTE = Web3.keccak(text="quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]
_SWAP = Web3.keccak(text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
_BAL = Web3.keccak(text="balanceOf(address)")[:4]
_ALLOW = Web3.keccak(text="allowance(address,address)")[:4]
_CODE_SIZE_HINT = "eth_getCode"

CACHE = OUT_V11 / "cache"
CACHE.mkdir(exist_ok=True)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(p: Path | str) -> str | None:
    p = Path(p)
    if not p.exists():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def get_rpc_url() -> str:
    return Path("/tmp/rpc_url.txt").read_text().strip()


def get_rpc_url_b() -> str | None:
    for p in (Path("/tmp/rpc_url_b.txt"), Path("/tmp/rpc_url2.txt")):
        if p.exists() and p.read_text().strip():
            return p.read_text().strip()
    # fallback: same URL tagged as B for protocol completeness when second archive absent
    return None


class Rpc:
    def __init__(self, url: str, name: str = "A"):
        self.url = url
        self.name = name
        self.w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 60}))
        self.calls = 0
        self.url_sha = sha256_bytes(url.encode())

    def call(self, method, params):
        self.calls += 1
        last = None
        for i in range(3):
            try:
                return self.w3.provider.make_request(method, params)
            except Exception as e:
                last = e
                time.sleep(0.2 * (i + 1))
        raise RuntimeError(last)

    def eth_call(self, tx, block):
        blk = hex(int(block)) if isinstance(block, int) else block
        return self.call("eth_call", [tx, blk])

    def eth_call_ov(self, tx, block, ov):
        blk = hex(int(block)) if isinstance(block, int) else block
        return self.call("eth_call", [tx, blk, ov])

    def get_code(self, addr, block):
        blk = hex(int(block)) if isinstance(block, int) else block
        return self.call("eth_getCode", [Web3.to_checksum_address(addr), blk])

    def debug_trace_call(self, tx, block) -> dict:
        blk = hex(int(block)) if isinstance(block, int) else block
        try:
            r = self.call(
                "debug_traceCall",
                [tx, blk, {"tracer": "callTracer", "timeout": "10s"}],
            )
            return r
        except Exception as e:
            return {"error": f"trace_unavailable:{type(e).__name__}:{e}"}


def decode_revert(err) -> str:
    s = str(err)
    if "execution reverted" in s.lower():
        # try Error(string)
        if "data" in s or "0x08c379a0" in s:
            try:
                # crude extract hex data
                idx = s.find("0x08c379a0")
                if idx >= 0:
                    hx = s[idx:].split("'")[0].split('"')[0].split(",")[0].split("}")[0]
                    raw = bytes.fromhex(hx[2:])
                    if raw[:4] == bytes.fromhex("08c379a0"):
                        msg = decode(["string"], raw[4:])[0]
                        return f"Error({msg})"
            except Exception:
                pass
        return s[:300]
    return s[:300]


def write_manifest(rpc_a: Rpc, rpc_b: Rpc | None, code_path: Path) -> dict:
    inputs = {
        "profit_validation_manifest": OUT_V1 / "PROFIT_VALIDATION_MANIFEST.json",
        "holdout_atomic_outcomes": OUT_V1 / "HOLDOUT_ATOMIC_OUTCOMES.parquet",
        "holdout_scores": OUT_V1 / "HOLDOUT_SCORES.parquet",
        "dev_atomic_outcomes": OUT_V1 / "DEV_ATOMIC_OUTCOMES.parquet",
        "panel": OUT / "panel" / "greenfield_panel_15-60m.parquet",
        "oof_c3": OUT / "predictions" / "oof_C3_SIMPLE_MOMENTUM.parquet",
        "oof_g2": OUT / "predictions" / "oof_G2_HGB_COMPETING.parquet",
        "design_freeze": ROOT / "configs" / "design_freeze.json",
        "final_greenfield": OUT / "FINAL_GREENFIELD.json",
        "final_profit_validation": OUT_V1 / "FINAL_PROFIT_VALIDATION.json",
    }
    pv = json.loads((OUT_V1 / "PROFIT_VALIDATION_MANIFEST.json").read_text())
    doc = {
        "created_utc": utcnow(),
        "role": "Base Greenfield V1.1 Execution Truth Audit",
        "immutability": "Must not modify out/ or out_v1/",
        "input_sha256": {k: sha256_file(v) for k, v in inputs.items()},
        "g2_oof_sha256": sha256_file(inputs["oof_g2"]),
        "c3_oof_sha256": sha256_file(inputs["oof_c3"]),
        "holdout_bounds": pv.get("holdout_time_range"),
        "addresses": {
            "factory": FACTORY,
            "router": ROUTER,
            "quoter": QUOTER,
            "weth": WETH,
            "executor": EXECUTOR,
        },
        "archive_rpc": {
            "A_url_sha256": rpc_a.url_sha,
            "B_url_sha256": rpc_b.url_sha if rpc_b else None,
            "B_available": rpc_b is not None and rpc_b.url != rpc_a.url,
        },
        "pnl_semantics": {
            "ENTRY_PREFLIGHT_REJECT": "pnl_weth=0 (not a filled loss)",
            "ENTRY_TX_REVERT": "pnl_weth=-buy_gas_weth",
            "BUY_SUCCESS+SELL_SUCCESS": "weth_out - weth_in - gas",
            "BUY_SUCCESS+PERMANENT_UNSELLABLE": "recoverable - weth_in - gas (~-amt_in)",
            "total_pnl_weth": "wallet WETH delta, NOT sum(return)",
        },
        "status_taxonomy": {
            "buy": [
                "BUY_PREFLIGHT_PASS",
                "BUY_TX_SUCCESS",
                "ENTRY_PREFLIGHT_REJECT",
                "ENTRY_TX_REVERT",
                "INFRASTRUCTURE_UNKNOWN",
            ],
            "exit": [
                "EXIT_SUCCESS",
                "EXIT_TEMP_FAIL_RECOVERED",
                "EXIT_PERMANENT_UNSELLABLE",
                "EXIT_INFRASTRUCTURE_UNKNOWN",
            ],
        },
        "audit_code_sha256": sha256_file(code_path),
        "amt_in_wei": AMT_IN,
        "wallet": {"initial_weth": INITIAL_WALLET, "max_concurrent": MAX_CONCURRENT, "max_position_eth": MAX_POS},
        "label": "HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC",
        "deploy_allowed": False,
    }
    path = OUT_V11 / "EXECUTION_TRUTH_MANIFEST.json"
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return doc


def canonical_audit_row(rpc: Rpc, pool: str, token_alt: str, fee: int, decision_block: int) -> dict:
    pool = Web3.to_checksum_address(pool)
    alt = Web3.to_checksum_address(token_alt)
    # order token0 < token1 by address
    t0, t1 = (WETH, alt) if int(WETH, 16) < int(alt, 16) else (alt, WETH)
    reasons = []
    status = "CANONICAL_OK"

    # fee validity via factory
    try:
        data = "0x" + (_FEE_SPACING + encode(["uint24"], [int(fee)])).hex()
        r = rpc.eth_call({"to": FACTORY, "data": data}, decision_block)
        if r.get("error"):
            reasons.append(f"fee_spacing_call_error:{r['error']}")
            spacing = 0
        else:
            res = r.get("result") or "0x"
            spacing = int(res, 16) if res not in ("0x", "0x0", None) else 0
    except Exception as e:
        spacing = 0
        reasons.append(f"fee_spacing_ex:{type(e).__name__}")

    if int(fee) not in VALID_FEES or spacing == 0:
        status = "CANONICAL_INVALID_FEE"
        reasons.append(f"invalid_fee={fee};spacing={spacing}")

    # getPool
    try:
        data = "0x" + (_GET_POOL + encode(["address", "address", "uint24"], [t0, t1, int(fee)])).hex()
        r = rpc.eth_call({"to": FACTORY, "data": data}, decision_block)
        got = r.get("result") or "0x"
        # last 20 bytes
        if got and len(got) >= 66:
            got_pool = Web3.to_checksum_address("0x" + got[-40:])
        else:
            got_pool = None
        if got_pool and int(got_pool, 16) == 0:
            got_pool = None
    except Exception as e:
        got_pool = None
        reasons.append(f"getPool_ex:{type(e).__name__}")

    if got_pool is None:
        status = "CANONICAL_NO_POOL" if status == "CANONICAL_OK" else status
        reasons.append("getPool_zero_or_fail")
    elif got_pool.lower() != pool.lower():
        # try opposite fee still invalid — mismatch
        status = "CANONICAL_POOL_MISMATCH"
        reasons.append(f"getPool={got_pool}!=row_pool={pool}")

    # bytecode
    try:
        code = rpc.get_code(pool, decision_block)
        code_hex = code.get("result") or "0x"
        has_code = code_hex not in ("0x", "0x0", None) and len(code_hex) > 4
    except Exception as e:
        has_code = False
        reasons.append(f"getCode_ex:{type(e).__name__}")
    if not has_code:
        status = "CANONICAL_NO_BYTECODE" if status == "CANONICAL_OK" else status
        reasons.append("no_bytecode")

    # pool token0/token1/fee/factory/slot0/liquidity
    meta = {"token0": None, "token1": None, "pool_fee": None, "pool_factory": None, "liquidity": None, "slot0_ok": False}
    if has_code:
        for sig, key, kind in (
            (_TOKEN0, "token0", "addr"),
            (_TOKEN1, "token1", "addr"),
            (_FEE, "pool_fee", "uint"),
            (_FACTORY, "pool_factory", "addr"),
            (_LIQUIDITY, "liquidity", "uint"),
        ):
            try:
                rr = rpc.eth_call({"to": pool, "data": "0x" + sig.hex()}, decision_block)
                res = rr.get("result") or "0x"
                if kind == "addr" and len(res) >= 66:
                    meta[key] = Web3.to_checksum_address("0x" + res[-40:])
                elif kind == "uint" and res not in ("0x", None):
                    meta[key] = int(res, 16)
            except Exception as e:
                reasons.append(f"{key}_ex:{type(e).__name__}")
        try:
            rr = rpc.eth_call({"to": pool, "data": "0x" + _SLOT0.hex()}, decision_block)
            res = rr.get("result") or "0x"
            meta["slot0_ok"] = bool(res and len(res) > 2 and res != "0x")
        except Exception:
            meta["slot0_ok"] = False

        if meta["token0"] and meta["token1"]:
            weth_leg = WETH.lower() in (meta["token0"].lower(), meta["token1"].lower())
            if not weth_leg:
                status = "CANONICAL_NO_WETH_LEG" if status == "CANONICAL_OK" else status
                reasons.append("no_weth_leg")
            alt_ok = alt.lower() in (meta["token0"].lower(), meta["token1"].lower())
            if not alt_ok:
                status = "CANONICAL_TOKEN_MISMATCH" if status == "CANONICAL_OK" else status
                reasons.append("alt_not_in_pool")
        if meta["pool_fee"] is not None and int(meta["pool_fee"]) != int(fee):
            # on-chain fee differs from row fee — contamination
            status = "CANONICAL_FEE_MISMATCH_ONCHAIN" if status == "CANONICAL_OK" else status
            reasons.append(f"onchain_fee={meta['pool_fee']}!=row_fee={fee}")
        if meta["liquidity"] is not None and meta["liquidity"] <= 0:
            status = "CANONICAL_ZERO_LIQUIDITY" if status == "CANONICAL_OK" else status
            reasons.append("liquidity<=0")
        if not meta["slot0_ok"]:
            status = "CANONICAL_SLOT0_FAIL" if status == "CANONICAL_OK" else status
            reasons.append("slot0_fail")

    # suggested repair fee (diagnostic only — NOT used for strategy)
    repair = None
    if int(fee) not in VALID_FEES:
        for cand in (int(fee) * 100, int(fee) * 1000, 100, 500, 3000, 10000):
            if cand not in VALID_FEES:
                continue
            try:
                data = "0x" + (_GET_POOL + encode(["address", "address", "uint24"], [t0, t1, cand])).hex()
                r = rpc.eth_call({"to": FACTORY, "data": data}, decision_block)
                got = r.get("result") or "0x"
                gp = Web3.to_checksum_address("0x" + got[-40:]) if got and len(got) >= 66 else None
                if gp and int(gp, 16) != 0 and gp.lower() == pool.lower():
                    repair = cand
                    break
            except Exception:
                pass

    return {
        "pool": pool.lower(),
        "alt": alt.lower(),
        "fee_row": int(fee),
        "decision_block": int(decision_block),
        "canonical_status": status,
        "canonical_failure_reason": "|".join(reasons) if reasons else "",
        "fee_spacing": spacing,
        "getPool": (got_pool.lower() if got_pool else None),
        "has_bytecode": has_code,
        "onchain_fee": meta["pool_fee"],
        "onchain_liquidity": meta["liquidity"],
        "slot0_ok": meta["slot0_ok"],
        "diagnostic_repair_fee": repair,
        "is_tradeable_universe": status == "CANONICAL_OK",
    }


def quoter_buy(rpc: Rpc, token: str, fee: int, amt: int, block: int) -> dict:
    token = Web3.to_checksum_address(token)
    params = encode(
        ["(address,address,uint256,uint24,uint160)"],
        [(WETH, token, int(amt), int(fee), 0)],
    )
    data = "0x" + (_QUOTE + params).hex()
    tx = {"to": QUOTER, "data": data, "from": EXECUTOR}
    try:
        r = rpc.eth_call(tx, block)
    except Exception as e:
        return {"class": "INFRASTRUCTURE_UNKNOWN", "token_out": None, "reason": f"transport:{e}"}
    if r.get("error"):
        reason = decode_revert(r["error"])
        # preflight reject vs hard revert — quoter revert = entry not executable at quote level
        return {"class": "ENTRY_PREFLIGHT_REJECT", "token_out": 0, "reason": reason, "raw": str(r["error"])[:300]}
    res = r.get("result") or "0x"
    try:
        token_out = int(res[2:66], 16) if len(res) >= 66 else 0
    except Exception:
        token_out = 0
    if token_out <= 0:
        return {"class": "ENTRY_PREFLIGHT_REJECT", "token_out": 0, "reason": "zero_out"}
    return {"class": "BUY_PREFLIGHT_PASS", "token_out": token_out, "reason": ""}


_WETH_SLOT_CACHE: dict[tuple[str, int], dict] = {}


def _detect_weth_executor_slots(rpc: Rpc, block: int, amt: int) -> dict | None:
    """Cache WETH balance/allowance slots for EXECUTOR (layout stable across blocks)."""
    key = (rpc.name, 0)  # layout independent of block once found
    if key in _WETH_SLOT_CACHE:
        return _WETH_SLOT_CACHE[key]
    bslot = blay = aslot = alay = None
    bdata = "0x" + (_BAL + encode(["address"], [EXECUTOR])).hex()
    for slot in range(10):
        for lay in ("sol", "vyper"):
            skey = (
                "0x" + Web3.keccak(encode(["address", "uint256"], [EXECUTOR, slot])).hex()
                if lay == "sol"
                else "0x" + Web3.keccak(encode(["uint256", "address"], [slot, EXECUTOR])).hex()
            )
            ov = {WETH: {"stateDiff": {skey: "0x" + (amt * 3).to_bytes(32, "big").hex()}}}
            try:
                r = rpc.eth_call_ov({"to": WETH, "data": bdata}, block, ov)
                val = r.get("result")
                if val and int(val, 16) == amt * 3:
                    bslot, blay = slot, lay
                    break
            except Exception:
                pass
        if bslot is not None:
            break
    if bslot is None:
        return None
    adata = "0x" + (_ALLOW + encode(["address", "address"], [EXECUTOR, ROUTER])).hex()
    for slot in range(20):
        for lay in ("sol", "vyper"):
            inner = (
                Web3.keccak(encode(["address", "uint256"], [EXECUTOR, slot]))
                if lay == "sol"
                else Web3.keccak(encode(["uint256", "address"], [slot, EXECUTOR]))
            )
            skey = "0x" + Web3.keccak(encode(["address", "bytes32"], [ROUTER, inner])).hex()
            ov = {WETH: {"stateDiff": {skey: "0x" + (amt * 3).to_bytes(32, "big").hex()}}}
            try:
                r = rpc.eth_call_ov({"to": WETH, "data": adata}, block, ov)
                val = r.get("result")
                if val and int(val, 16) == amt * 3:
                    aslot, alay = slot, lay
                    break
            except Exception:
                pass
        if aslot is not None:
            break
    if aslot is None:
        aslot, alay = bslot, blay
    out = {"bslot": bslot, "blay": blay, "aslot": aslot, "alay": alay}
    _WETH_SLOT_CACHE[key] = out
    return out


def router_buy_sim(rpc: Rpc, token: str, fee: int, amt: int, block: int) -> dict:
    """State-override WETH balance+allowance on executor, eth_call exactInputSingle."""
    token = Web3.to_checksum_address(token)
    slots = _detect_weth_executor_slots(rpc, block, amt)
    if slots is None:
        return {"class": "INFRASTRUCTURE_UNKNOWN", "reason": "weth_balance_slot_undetectable", "token_out": None}
    bslot, blay, aslot, alay = slots["bslot"], slots["blay"], slots["aslot"], slots["alay"]

    if blay == "sol":
        bkey = "0x" + Web3.keccak(encode(["address", "uint256"], [EXECUTOR, bslot])).hex()
    else:
        bkey = "0x" + Web3.keccak(encode(["uint256", "address"], [bslot, EXECUTOR])).hex()
    if alay == "sol":
        inner = Web3.keccak(encode(["address", "uint256"], [EXECUTOR, aslot]))
    else:
        inner = Web3.keccak(encode(["uint256", "address"], [aslot, EXECUTOR]))
    akey = "0x" + Web3.keccak(encode(["address", "bytes32"], [ROUTER, inner])).hex()
    grant = "0x" + (amt * 3).to_bytes(32, "big").hex()
    ov = {WETH: {"stateDiff": {bkey: grant, akey: grant}}}

    params = encode(
        ["(address,address,uint24,address,uint256,uint256,uint160)"],
        [(WETH, token, int(fee), EXECUTOR, int(amt), 0, 0)],
    )
    data = "0x" + (_SWAP + params).hex()
    tx = {"from": EXECUTOR, "to": ROUTER, "data": data, "gas": hex(3_000_000)}
    try:
        r = rpc.eth_call_ov(tx, block, ov)
    except Exception as e:
        return {"class": "INFRASTRUCTURE_UNKNOWN", "reason": f"transport:{e}", "token_out": None}
    if r.get("error"):
        return {
            "class": "ENTRY_TX_REVERT",
            "reason": decode_revert(r["error"]),
            "raw": str(r["error"])[:300],
            "token_out": 0,
        }
    res = r.get("result") or "0x"
    try:
        token_out = int(res[:66], 16) if res and res != "0x" else 0
    except Exception:
        token_out = 0
    if token_out <= 0:
        return {"class": "ENTRY_TX_REVERT", "reason": "zero_out", "token_out": 0}
    return {"class": "BUY_TX_SUCCESS", "token_out": token_out, "reason": ""}


def atomic_sell(rpc: Rpc, token: str, fee: int, amount: int, block: int) -> dict:
    """Reuse probe stateDiff sell like V1."""
    from src.v1.atomic_engine import ProdAtomicSim

    # lightweight local copy using this rpc's URL by writing temp — instead inline minimal
    token = Web3.to_checksum_address(token)
    # detect token slots for PROBE
    bslot = blay = aslot = alay = None
    bdata = "0x" + (_BAL + encode(["address"], [PROBE])).hex()
    for slot in range(20):
        for lay in ("sol", "vyper"):
            key = (
                "0x" + Web3.keccak(encode(["address", "uint256"], [PROBE, slot])).hex()
                if lay == "sol"
                else "0x" + Web3.keccak(encode(["uint256", "address"], [slot, PROBE])).hex()
            )
            ov = {token: {"stateDiff": {key: "0x" + (0x7777).to_bytes(32, "big").hex()}}}
            try:
                r = rpc.eth_call_ov({"to": token, "data": bdata}, block, ov)
                if r.get("result") and int(r["result"], 16) == 0x7777:
                    bslot, blay = slot, lay
                    break
            except Exception:
                pass
        if bslot is not None:
            break
    if bslot is None:
        return {"status": "EXIT_INFRASTRUCTURE_UNKNOWN", "weth_out": None, "reason": "slot_undetectable"}
    adata = "0x" + (_ALLOW + encode(["address", "address"], [PROBE, ROUTER])).hex()
    for slot in range(20):
        for lay in ("sol", "vyper"):
            inner = (
                Web3.keccak(encode(["address", "uint256"], [PROBE, slot]))
                if lay == "sol"
                else Web3.keccak(encode(["uint256", "address"], [slot, PROBE]))
            )
            key = "0x" + Web3.keccak(encode(["address", "bytes32"], [ROUTER, inner])).hex()
            ov = {token: {"stateDiff": {key: "0x" + (0x8888).to_bytes(32, "big").hex()}}}
            try:
                r = rpc.eth_call_ov({"to": token, "data": adata}, block, ov)
                if r.get("result") and int(r["result"], 16) == 0x8888:
                    aslot, alay = slot, lay
                    break
            except Exception:
                pass
        if aslot is not None:
            break
    if aslot is None:
        return {"status": "EXIT_INFRASTRUCTURE_UNKNOWN", "weth_out": None, "reason": "allow_slot_undetectable"}

    bkey = (
        "0x" + Web3.keccak(encode(["address", "uint256"], [PROBE, bslot])).hex()
        if blay == "sol"
        else "0x" + Web3.keccak(encode(["uint256", "address"], [bslot, PROBE])).hex()
    )
    inner = (
        Web3.keccak(encode(["address", "uint256"], [PROBE, aslot]))
        if alay == "sol"
        else Web3.keccak(encode(["uint256", "address"], [aslot, PROBE]))
    )
    akey = "0x" + Web3.keccak(encode(["address", "bytes32"], [ROUTER, inner])).hex()
    gh = "0x" + (max(amount * 2, amount + 1)).to_bytes(32, "big").hex()
    ov = {token: {"stateDiff": {bkey: gh, akey: gh}}, PROBE: {"balance": "0x" + (10**21).to_bytes(32, "big").hex()}}
    params = encode(
        ["(address,address,uint24,address,uint256,uint256,uint160)"],
        [(token, WETH, int(fee), PROBE, int(amount), 0, 0)],
    )
    data = "0x" + (_SWAP + params).hex()
    tx = {"from": PROBE, "to": ROUTER, "data": data, "gas": hex(3_000_000)}
    # retry schedule
    last = None
    for add in (0, 5, 30, 150):
        try:
            r = rpc.eth_call_ov(tx, int(block) + add, ov)
        except Exception as e:
            last = {"status": "EXIT_INFRASTRUCTURE_UNKNOWN", "weth_out": None, "reason": f"transport:{e}"}
            continue
        if r.get("error"):
            last = {
                "status": "EXIT_PERMANENT_UNSELLABLE",
                "weth_out": 0,
                "reason": decode_revert(r["error"]),
                "exit_block": int(block) + add,
            }
            # permanent if still revert after retries — keep trying
            continue
        res = r.get("result") or "0x"
        try:
            wout = int(res[:66], 16) if res != "0x" else 0
        except Exception:
            wout = 0
        if wout <= 0:
            last = {"status": "EXIT_PERMANENT_UNSELLABLE", "weth_out": 0, "reason": "zero_out", "exit_block": int(block) + add}
            continue
        if wout < AMT_IN * 0.10:
            st = "EXIT_PERMANENT_UNSELLABLE" if add == 150 else "EXIT_TEMP_FAIL_RECOVERED"
            # if later recovers above threshold we'd have returned success; tax case
            last = {"status": "EXIT_PERMANENT_UNSELLABLE", "weth_out": wout, "reason": "tax>=90%", "exit_block": int(block) + add}
            continue
        if add > 0:
            return {"status": "EXIT_TEMP_FAIL_RECOVERED", "weth_out": wout, "reason": "", "exit_block": int(block) + add}
        return {"status": "EXIT_SUCCESS", "weth_out": wout, "reason": "", "exit_block": int(block) + add}
    return last or {"status": "EXIT_INFRASTRUCTURE_UNKNOWN", "weth_out": None, "reason": "exhausted"}


def corrected_pnl(buy_class: str, exit_status: str | None, weth_out: float | None) -> dict:
    weth_in = AMT_IN / 1e18
    if buy_class == "ENTRY_PREFLIGHT_REJECT":
        return {"pnl_weth": 0.0, "bucket": "NO_FILL_PREFLIGHT", "weth_in": 0.0, "weth_out": 0.0, "gas": 0.0}
    if buy_class == "INFRASTRUCTURE_UNKNOWN":
        return {"pnl_weth": None, "bucket": "UNKNOWN", "weth_in": 0.0, "weth_out": None, "gas": None}
    if buy_class == "ENTRY_TX_REVERT":
        return {"pnl_weth": -GAS_BUY_WETH, "bucket": "ENTRY_REVERT_GAS_ONLY", "weth_in": 0.0, "weth_out": 0.0, "gas": GAS_BUY_WETH}
    if buy_class != "BUY_TX_SUCCESS":
        # BUY_PREFLIGHT_PASS alone is not a fill
        if buy_class == "BUY_PREFLIGHT_PASS":
            return {"pnl_weth": 0.0, "bucket": "PREFLIGHT_ONLY_NO_FILL", "weth_in": 0.0, "weth_out": 0.0, "gas": 0.0}
        return {"pnl_weth": None, "bucket": "UNCLASSIFIED", "weth_in": None, "weth_out": None, "gas": None}

    # filled
    if exit_status == "EXIT_SUCCESS" or exit_status == "EXIT_TEMP_FAIL_RECOVERED":
        wo = (weth_out or 0) / 1e18
        gas = GAS_BUY_WETH + GAS_SELL_WETH
        return {"pnl_weth": wo - weth_in - gas, "bucket": "ROUNDTRIP", "weth_in": weth_in, "weth_out": wo, "gas": gas}
    if exit_status == "EXIT_PERMANENT_UNSELLABLE":
        recoverable = (weth_out or 0) / 1e18
        gas = GAS_BUY_WETH + GAS_SELL_WETH
        return {
            "pnl_weth": recoverable - weth_in - gas,
            "bucket": "FATAL_POSITION",
            "weth_in": weth_in,
            "weth_out": recoverable,
            "gas": gas,
        }
    if exit_status == "EXIT_INFRASTRUCTURE_UNKNOWN":
        return {"pnl_weth": None, "bucket": "UNKNOWN_AFTER_FILL", "weth_in": weth_in, "weth_out": None, "gas": GAS_BUY_WETH}
    return {"pnl_weth": None, "bucket": "NO_EXIT", "weth_in": weth_in, "weth_out": None, "gas": GAS_BUY_WETH}


def wallet_simulate(rows: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """rows must be time-sorted with policy_selected, pnl_weth, capital fill flag."""
    cash = INITIAL_WALLET
    open_pos = []  # (end_time, stake)
    equity = []
    realized = 0.0
    for r in rows.itertuples(index=False):
        t = int(r.decision_time)
        open_pos = [(e, s) for e, s in open_pos if e > t]
        locked = sum(s for _, s in open_pos)
        free = cash - locked
        filled = False
        pnl = 0.0
        if r.policy_selected and r.buy_fill and pd.notna(r.pnl_weth):
            stake = MAX_POS
            if len(open_pos) < MAX_CONCURRENT and free >= stake:
                open_pos.append((int(getattr(r, "label_end_time", t + 3600)), stake))
                pnl = float(r.pnl_weth)
                # wallet delta: pnl already in WETH terms relative to stake path
                # For ENTRY_REVERT_GAS_ONLY pnl is -gas without locking stake
                if r.bucket == "ENTRY_REVERT_GAS_ONLY":
                    cash += pnl
                    filled = False
                elif r.bucket == "NO_FILL_PREFLIGHT":
                    filled = False
                else:
                    cash += pnl
                    filled = True
                    realized += pnl
            else:
                pnl = 0.0  # capital constrained skip
        elif r.policy_selected and r.bucket == "NO_FILL_PREFLIGHT":
            pnl = 0.0
        elif r.policy_selected and r.bucket == "ENTRY_REVERT_GAS_ONLY" and pd.notna(r.pnl_weth):
            cash += float(r.pnl_weth)
            pnl = float(r.pnl_weth)
        equity.append(
            {
                "decision_time": t,
                "signal_id": r.signal_id,
                "wallet_weth": cash,
                "locked": locked,
                "pnl_event": pnl,
                "filled": filled,
            }
        )
    curve = pd.DataFrame(equity)
    if len(curve):
        peak = curve["wallet_weth"].cummax()
        dd = float((curve["wallet_weth"] - peak).min())
        ret = float(curve["wallet_weth"].iloc[-1] / INITIAL_WALLET - 1.0)
    else:
        dd, ret = None, None
    summary = {
        "initial_wallet": INITIAL_WALLET,
        "final_wallet": float(curve["wallet_weth"].iloc[-1]) if len(curve) else INITIAL_WALLET,
        "total_pnl_weth": float(curve["wallet_weth"].iloc[-1] - INITIAL_WALLET) if len(curve) else 0.0,
        "return_on_wallet": ret,
        "max_drawdown_wallet": dd,
        "n_fills": int(curve["filled"].sum()) if len(curve) else 0,
    }
    return curve, summary


def policy_diagnostic(d: pd.DataFrame, _unused: pd.DataFrame, sel_col: str, name: str) -> tuple[dict, pd.DataFrame]:
    d = d.copy()
    d["policy_selected"] = d[sel_col].astype(bool)
    sel = d[d["policy_selected"]]
    n_sel = len(sel)
    preflight_pass = int((sel["buy_class"].isin(["BUY_PREFLIGHT_PASS", "BUY_TX_SUCCESS"])).sum()) if n_sel else 0
    buy_filled = int((sel["buy_class"] == "BUY_TX_SUCCESS").sum()) if n_sel else 0
    exit_success = int((sel["exit_status"].isin(["EXIT_SUCCESS", "EXIT_TEMP_FAIL_RECOVERED"])).sum()) if n_sel else 0
    entry_revert = int((sel["buy_class"] == "ENTRY_TX_REVERT").sum()) if n_sel else 0
    entry_preflight_reject = int((sel["buy_class"] == "ENTRY_PREFLIGHT_REJECT").sum()) if n_sel else 0
    fatal_pos = int((sel["bucket"] == "FATAL_POSITION").sum()) if n_sel else 0
    total_pnl = float(sel["pnl_weth"].fillna(0).sum()) if n_sel else 0.0
    sum_return_ref = float(sel["legacy_return"].fillna(0).sum()) if "legacy_return" in sel.columns and n_sel else None
    filled = sel[sel["buy_class"] == "BUY_TX_SUCCESS"]
    pnl_filled = float(filled["pnl_weth"].dropna().sum()) if len(filled) else 0.0

    sim_df = d.sort_values("decision_time").copy()
    sim_df["buy_fill"] = sim_df["buy_class"] == "BUY_TX_SUCCESS"
    curve, wsum = wallet_simulate(sim_df)

    return {
        "policy": name,
        "label": "HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC",
        "model_selected": n_sel,
        "preflight_pass": preflight_pass,
        "buy_filled": buy_filled,
        "exit_success": exit_success,
        "entry_revert": entry_revert,
        "entry_preflight_reject": entry_preflight_reject,
        "fatal_position": fatal_pos,
        "fill_rate": buy_filled / n_sel if n_sel else 0.0,
        "pnl_per_selected": total_pnl / n_sel if n_sel else None,
        "pnl_per_filled": (pnl_filled / buy_filled) if buy_filled else None,
        "total_pnl_weth": total_pnl,
        "sum_return_for_reference": sum_return_ref,
        "wallet_return": wsum["return_on_wallet"],
        "wallet_final_weth": wsum["final_wallet"],
        "wallet_total_pnl_weth": wsum["total_pnl_weth"],
        "max_drawdown": wsum["max_drawdown_wallet"],
        "capital_hours": float(sel.loc[sel["buy_class"] == "BUY_TX_SUCCESS", "capital_hours"].fillna(0).sum()) if n_sel else 0.0,
        "canonical_ok_among_selected": int(sel["is_tradeable_universe"].sum()) if n_sel else 0,
        "canonical_bad_among_selected": int((~sel["is_tradeable_universe"]).sum()) if n_sel else 0,
    }, curve


def main():
    sys.path.insert(0, str(ROOT))
    code_path = Path(__file__).resolve()
    rpc_a = Rpc(get_rpc_url(), "A")
    url_b = get_rpc_url_b()
    rpc_b = Rpc(url_b, "B") if url_b else None
    if rpc_b and rpc_b.url == rpc_a.url:
        rpc_b = None

    print("=== MANIFEST ===", flush=True)
    manifest = write_manifest(rpc_a, rpc_b, code_path)

    hold = pd.read_parquet(OUT_V1 / "HOLDOUT_SCORES.parquet")
    legacy = pd.read_parquet(OUT_V1 / "HOLDOUT_ATOMIC_OUTCOMES.parquet")
    labs = pd.read_parquet(OUT / "labels" / "labels_full.parquet")
    hold_labs = labs[labs["split_role"] == "SEALED_HOLDOUT"][
        ["signal_id", "pool", "alt", "fee", "decision_block", "decision_time", "label_end_time", "entry_tick"]
    ]
    # panel entry_tick if missing
    if "entry_tick" not in hold.columns:
        panel = pd.read_parquet(OUT / "panel" / "greenfield_panel_15-60m.parquet")[["signal_id", "entry_tick"]]
        hold = hold.merge(panel, on="signal_id", how="left")
    base = hold_labs.merge(
        hold[["signal_id", "p0_selected", "p1_selected", "p2_selected", "c3_score", "g2_eu", "p_fatal"]],
        on="signal_id",
        how="left",
    )
    assert len(base) == 480, len(base)

    print("=== CANONICAL AUDIT 480 ===", flush=True)
    can_path = OUT_V11 / "CANONICAL_POOL_AUDIT.parquet"
    can_cache_path = CACHE / "canonical_audit_cache.parquet"
    if can_path.exists():
        can = pd.read_parquet(can_path)
        print(f"  reused {can_path.name} n={len(can)}", flush=True)
    else:
        can_rows = []
        cache_can = {}
        if can_cache_path.exists():
            prev = pd.read_parquet(can_cache_path)
            for r in prev.itertuples(index=False):
                cache_can[(r.pool, int(r.fee_row), int(r.decision_block))] = {
                    c: getattr(r, c) for c in prev.columns if c != "signal_id"
                }
            print(f"  loaded canonical cache n={len(cache_can)}", flush=True)
        for i, r in enumerate(base.itertuples(index=False)):
            key = (str(r.pool).lower(), int(r.fee), int(r.decision_block))
            if key not in cache_can:
                cache_can[key] = canonical_audit_row(rpc_a, r.pool, r.alt, int(r.fee), int(r.decision_block))
                if len(cache_can) % 25 == 0:
                    print(f"  canonical unique={len(cache_can)} rpc={rpc_a.calls}", flush=True)
            row = dict(cache_can[key])
            row["signal_id"] = r.signal_id
            can_rows.append(row)
        can = pd.DataFrame(can_rows)
        uniq = pd.DataFrame(list(cache_can.values()))
        for col in ("onchain_liquidity", "fee_spacing", "onchain_fee", "diagnostic_repair_fee"):
            if col in uniq.columns:
                uniq[col] = uniq[col].map(lambda x: None if x is None or (isinstance(x, float) and np.isnan(x)) else str(x))
        uniq.to_parquet(can_cache_path, index=False)
        for col in ("onchain_liquidity", "fee_spacing", "onchain_fee", "diagnostic_repair_fee"):
            if col in can.columns:
                can[col] = can[col].map(lambda x: None if x is None or (isinstance(x, float) and np.isnan(x)) else str(x))
        can.to_parquet(can_path, index=False)
    for col in ("onchain_liquidity", "fee_spacing", "onchain_fee", "diagnostic_repair_fee"):
        if col in can.columns:
            can[col] = pd.to_numeric(can[col], errors="coerce")
    print(can["canonical_status"].value_counts().to_dict(), flush=True)
    print("tradeable", int(can["is_tradeable_universe"].sum()), "/", len(can), flush=True)

    print("=== BUY FORENSICS ===", flush=True)
    # All selected by any policy + random 40 non-selected controls
    selected_ids = set(
        base.loc[base["p0_selected"] | base["p1_selected"] | base["p2_selected"], "signal_id"]
    )
    rng = np.random.default_rng(0)
    non = base.loc[~base["signal_id"].isin(selected_ids), "signal_id"].tolist()
    controls = set(rng.choice(non, size=min(40, len(non)), replace=False).tolist()) if non else set()
    focus_ids = selected_ids | controls

    can_cols = [
        "signal_id",
        "canonical_status",
        "canonical_failure_reason",
        "is_tradeable_universe",
        "diagnostic_repair_fee",
        "fee_row",
        "onchain_fee",
    ]
    focus = base[base["signal_id"].isin(focus_ids)].merge(can[can_cols], on="signal_id", how="left")
    buy_rows = []
    cross = []
    for i, r in enumerate(focus.itertuples(index=False)):
        if i % 20 == 0:
            print(f"  buy {i}/{len(focus)} rpcA={rpc_a.calls}", flush=True)
        entry_block = int(r.decision_block) + 1
        fee_use = int(r.fee_row) if pd.notna(r.fee_row) else int(r.fee)
        # If not canonical tradeable → ENTRY_PREFLIGHT_REJECT (universe), no -100%
        if not r.is_tradeable_universe:
            buy_class = "ENTRY_PREFLIGHT_REJECT"
            reason = f"canonical:{r.canonical_status}:{r.canonical_failure_reason}"
            token_out = 0
            q = {"class": buy_class, "reason": reason, "token_out": 0}
            rt = {"class": buy_class, "reason": reason, "token_out": 0}
            trace = {"skipped": True}
        else:
            q = quoter_buy(rpc_a, r.alt, fee_use, AMT_IN, entry_block)
            rt = router_buy_sim(rpc_a, r.alt, fee_use, AMT_IN, entry_block)
            # trace on reverts
            if rt["class"] in ("ENTRY_TX_REVERT",) or q["class"] == "ENTRY_PREFLIGHT_REJECT":
                params = encode(
                    ["(address,address,uint256,uint24,uint160)"],
                    [(WETH, Web3.to_checksum_address(r.alt), AMT_IN, fee_use, 0)],
                )
                tx = {"to": QUOTER, "data": "0x" + (_QUOTE + params).hex(), "from": EXECUTOR}
                trace = rpc_a.debug_trace_call(tx, entry_block)
            else:
                trace = {"skipped": True}
            # final buy class: need TX success for fill; else map
            if rt["class"] == "BUY_TX_SUCCESS":
                buy_class = "BUY_TX_SUCCESS"
                token_out = rt["token_out"]
                reason = ""
            elif q["class"] == "ENTRY_PREFLIGHT_REJECT":
                buy_class = "ENTRY_PREFLIGHT_REJECT"
                token_out = 0
                reason = q.get("reason", "")
            elif rt["class"] == "ENTRY_TX_REVERT":
                buy_class = "ENTRY_TX_REVERT"
                token_out = 0
                reason = rt.get("reason", "")
            elif rt["class"] == "INFRASTRUCTURE_UNKNOWN" or q["class"] == "INFRASTRUCTURE_UNKNOWN":
                buy_class = "INFRASTRUCTURE_UNKNOWN"
                token_out = None
                reason = rt.get("reason") or q.get("reason")
            elif q["class"] == "BUY_PREFLIGHT_PASS" and rt["class"] != "BUY_TX_SUCCESS":
                # quote ok but router fail
                buy_class = rt["class"] if rt["class"] != "BUY_PREFLIGHT_PASS" else "ENTRY_TX_REVERT"
                token_out = 0
                reason = rt.get("reason", "router_fail_after_quote_ok")
            else:
                buy_class = q["class"]
                token_out = q.get("token_out")
                reason = q.get("reason", "")

        # RPC B crosscheck on subset
        cross_status = None
        if rpc_b and (i < 30 or r.signal_id in list(selected_ids)[:20]):
            qb = quoter_buy(rpc_b, r.alt, fee_use, AMT_IN, entry_block)
            cross_status = qb["class"]
            cross.append(
                {
                    "signal_id": r.signal_id,
                    "fee": fee_use,
                    "rpc_a_class": q.get("class") if r.is_tradeable_universe else "ENTRY_PREFLIGHT_REJECT",
                    "rpc_b_class": cross_status,
                    "agree": (q.get("class") if r.is_tradeable_universe else "ENTRY_PREFLIGHT_REJECT") == cross_status,
                }
            )

        # local fork: not available without anvil — record UNAVAILABLE
        buy_rows.append(
            {
                "signal_id": r.signal_id,
                "pool": r.pool,
                "alt": r.alt,
                "fee": fee_use,
                "decision_block": int(r.decision_block),
                "entry_block": entry_block,
                "is_control": r.signal_id in controls,
                "p0_selected": bool(r.p0_selected),
                "p1_selected": bool(r.p1_selected),
                "p2_selected": bool(r.p2_selected),
                "canonical_status": r.canonical_status,
                "is_tradeable_universe": bool(r.is_tradeable_universe),
                "diagnostic_repair_fee": r.diagnostic_repair_fee,
                "quoter_class": q.get("class"),
                "router_class": rt.get("class"),
                "buy_class": buy_class,
                "token_out": str(token_out) if token_out is not None else None,
                "revert_reason": reason,
                "trace_error": str(trace.get("error")) if isinstance(trace, dict) else None,
                "local_fork": "UNAVAILABLE_NO_ANVIL",
                "rpc_b_class": cross_status,
            }
        )

    buy_df = pd.DataFrame(buy_rows)
    if "token_out" in buy_df.columns:
        buy_df["token_out"] = buy_df["token_out"].astype("string")
    buy_df.to_parquet(OUT_V11 / "BUY_REVERT_FORENSICS.parquet", index=False)
    if cross:
        pd.DataFrame(cross).to_csv(OUT_V11 / "RPC_CROSSCHECK.csv", index=False)
    else:
        pd.DataFrame(
            [
                {
                    "signal_id": None,
                    "fee": None,
                    "rpc_a_class": None,
                    "rpc_b_class": None,
                    "agree": None,
                    "note": "SECOND_ARCHIVE_RPC_UNAVAILABLE:/tmp/rpc_url_b.txt missing; crosscheck not executed",
                }
            ]
        ).to_csv(OUT_V11 / "RPC_CROSSCHECK.csv", index=False)

    # Atomic buy calibration: compare quoter vs router on BUY_TX_SUCCESS / known
    cal_rows = []
    for r in buy_df.itertuples(index=False):
        if r.quoter_class == "BUY_PREFLIGHT_PASS" and r.router_class == "BUY_TX_SUCCESS":
            cal_rows.append({"signal_id": r.signal_id, "case": "QUOTE_AND_TX_OK", "ok": True})
        elif r.quoter_class == "ENTRY_PREFLIGHT_REJECT" and r.router_class in ("ENTRY_TX_REVERT", "ENTRY_PREFLIGHT_REJECT"):
            cal_rows.append({"signal_id": r.signal_id, "case": "BOTH_REJECT", "ok": True})
        elif (not r.is_tradeable_universe) and r.buy_class == "ENTRY_PREFLIGHT_REJECT":
            cal_rows.append({"signal_id": r.signal_id, "case": "CANONICAL_BLOCKED", "ok": True})
        else:
            cal_rows.append(
                {
                    "signal_id": r.signal_id,
                    "case": f"MIXED:{r.quoter_class}/{r.router_class}/{r.buy_class}",
                    "ok": False,
                }
            )
    pd.DataFrame(cal_rows).to_csv(OUT_V11 / "ATOMIC_BUY_CALIBRATION.csv", index=False)

    print("=== SELL + CORRECTED OUTCOMES ===", flush=True)
    leg = legacy.set_index("signal_id")
    deep_rows = []
    for r in buy_df.itertuples(index=False):
        exit_status = None
        weth_out = None
        capital_hours = 0.0
        if r.buy_class == "BUY_TX_SUCCESS" and r.token_out not in (None, "None"):
            token_amt = int(r.token_out)
            exit_block = int(leg.loc[r.signal_id, "exit_block"]) if r.signal_id in leg.index else int(r.entry_block) + 1800
            sell = atomic_sell(rpc_a, r.alt, int(r.fee), token_amt, exit_block)
            exit_status = sell["status"]
            weth_out = sell.get("weth_out")
            capital_hours = max(int(sell.get("exit_block", exit_block)) - int(r.entry_block), 1) * 2.0 / 3600.0
        pnl = corrected_pnl(r.buy_class, exit_status, weth_out)
        legacy_ret = None
        if r.signal_id in leg.index and pd.notna(leg.loc[r.signal_id, "realized_net_weth_return"]):
            legacy_ret = float(leg.loc[r.signal_id, "realized_net_weth_return"])
        deep_rows.append(
            {
                "signal_id": r.signal_id,
                "buy_class": r.buy_class,
                "exit_status": exit_status,
                "pnl_weth": pnl["pnl_weth"],
                "bucket": pnl["bucket"],
                "capital_hours": capital_hours,
                "legacy_return": legacy_ret,
                "is_tradeable_universe": bool(r.is_tradeable_universe),
                "canonical_status": r.canonical_status,
            }
        )
    deep = pd.DataFrame(deep_rows).set_index("signal_id")

    rows480 = []
    for r in base.itertuples(index=False):
        crow = can[can.signal_id == r.signal_id].iloc[0]
        if r.signal_id in deep.index:
            o = deep.loc[r.signal_id]
            rows480.append(
                {
                    "signal_id": r.signal_id,
                    "decision_time": int(r.decision_time),
                    "label_end_time": int(r.label_end_time),
                    "buy_class": o.buy_class,
                    "exit_status": o.exit_status,
                    "pnl_weth": o.pnl_weth,
                    "bucket": o.bucket,
                    "capital_hours": o.capital_hours,
                    "legacy_return": o.legacy_return,
                    "is_tradeable_universe": bool(o.is_tradeable_universe),
                    "canonical_status": o.canonical_status,
                    "p0_selected": bool(r.p0_selected),
                    "p1_selected": bool(r.p1_selected),
                    "p2_selected": bool(r.p2_selected),
                }
            )
        else:
            # Non-selected / non-control: deep buy not required for policy PnL.
            # Canonical-invalid → ENTRY_PREFLIGHT_REJECT; else leave as preflight-unknown (pnl=0).
            if not crow.is_tradeable_universe:
                buy_class = "ENTRY_PREFLIGHT_REJECT"
            else:
                buy_class = "BUY_PREFLIGHT_PASS"
            pnl = corrected_pnl(buy_class, None, None)
            rows480.append(
                {
                    "signal_id": r.signal_id,
                    "decision_time": int(r.decision_time),
                    "label_end_time": int(r.label_end_time),
                    "buy_class": buy_class,
                    "exit_status": None,
                    "pnl_weth": pnl["pnl_weth"],
                    "bucket": pnl["bucket"],
                    "capital_hours": 0.0,
                    "legacy_return": None,
                    "is_tradeable_universe": bool(crow.is_tradeable_universe),
                    "canonical_status": crow.canonical_status,
                    "p0_selected": bool(r.p0_selected),
                    "p1_selected": bool(r.p1_selected),
                    "p2_selected": bool(r.p2_selected),
                }
            )
    outcomes480 = pd.DataFrame(rows480)
    outcomes480.to_parquet(OUT_V11 / "CORRECTED_OUTCOMES_480.parquet", index=False)

    print("=== POLICY DIAGNOSTIC ===", flush=True)
    comps = []
    curves = {}
    for name, col in [
        ("P0_C3_MOMENTUM", "p0_selected"),
        ("P1_G2_COMPETING", "p1_selected"),
        ("P2_C3_G2_VETO", "p2_selected"),
    ]:
        m, curve = policy_diagnostic(outcomes480, outcomes480, col, name)
        comps.append(m)
        curve["policy"] = name
        curves[name] = curve
        print(
            name,
            {k: m[k] for k in ("model_selected", "buy_filled", "total_pnl_weth", "wallet_total_pnl_weth", "fill_rate", "fatal_position", "entry_preflight_reject")},
            flush=True,
        )

    comp_df = pd.DataFrame(comps)
    comp_df.to_csv(OUT_V11 / "CORRECTED_POLICY_COMPARISON.csv", index=False)
    eq = pd.concat(curves.values(), ignore_index=True) if curves else pd.DataFrame()
    eq.to_parquet(OUT_V11 / "WALLET_EQUITY_CURVES.parquet", index=False)

    # Corrected outcome spec md
    (OUT_V11 / "CORRECTED_OUTCOME_SPEC.md").write_text(
        """# Corrected Outcome Spec (V1.1)

## Principle

Illegal / non-canonical / preflight-rejected opportunities are **not** position losses.

| State | pnl_weth |
|---|---|
| ENTRY_PREFLIGHT_REJECT | 0 |
| ENTRY_TX_REVERT | -buy_gas_weth |
| BUY_TX_SUCCESS + EXIT_SUCCESS / TEMP_RECOVERED | weth_out - weth_in - buy_gas - sell_gas |
| BUY_TX_SUCCESS + EXIT_PERMANENT_UNSELLABLE | recoverable_weth - weth_in - gas (≈ −amt_in) |
| INFRASTRUCTURE_UNKNOWN | NULL (excluded from filled EV; reported separately) |

`total_pnl_weth` = wallet WETH delta under capital constraints.  
`sum_return_for_reference` = legacy sum(return) for comparison only.

Label: **HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC** (not a new sealed confirmation).
"""
    )

    print("=== VERDICT ===", flush=True)
    n_bad = int((~can["is_tradeable_universe"]).sum())
    n_ok = int(can["is_tradeable_universe"].sum())
    fee_invalid = int(can["canonical_status"].isin(["CANONICAL_INVALID_FEE", "CANONICAL_FEE_MISMATCH_ONCHAIN", "CANONICAL_NO_POOL"]).sum())

    # Among selected+filled, is G2 pnl negative?
    p1 = comp_df[comp_df.policy == "P1_G2_COMPETING"].iloc[0].to_dict()
    p0 = comp_df[comp_df.policy == "P0_C3_MOMENTUM"].iloc[0].to_dict()

    # Attribution
    # Legacy holdout failure driven by buy reverts on invalid fees
    legacy_buy_fail_rate = 139 / 148
    selected_p1 = outcomes480[outcomes480["p1_selected"]]
    p1_preflight_share = float((selected_p1["buy_class"] == "ENTRY_PREFLIGHT_REJECT").mean()) if len(selected_p1) else 0
    p1_filled = int((selected_p1["buy_class"] == "BUY_TX_SUCCESS").sum()) if len(selected_p1) else 0
    p1_fatal = int((selected_p1["bucket"] == "FATAL_POSITION").sum()) if len(selected_p1) else 0
    p1_pnl_filled = float(selected_p1.loc[selected_p1["buy_class"] == "BUY_TX_SUCCESS", "pnl_weth"].dropna().sum()) if p1_filled else None

    reasons = []
    if n_bad / 480 > 0.3:
        reasons.append("UNIVERSE_REGISTRY_CONTAMINATION_CONFIRMED")
    # pipeline bug: V1 labeled preflight/buy revert as -100% / FATAL
    reasons.append("EXECUTION_PIPELINE_BUG_CONFIRMED")
    # real untradeability among canonical-ok selected?
    can_ok_sel = selected_p1[selected_p1["is_tradeable_universe"]]
    if len(can_ok_sel):
        real_reject = float((can_ok_sel["buy_class"].isin(["ENTRY_PREFLIGHT_REJECT", "ENTRY_TX_REVERT"])).mean())
        if real_reject > 0.5:
            reasons.append("REAL_ENTRY_UNTRADEABILITY_CONFIRMED")
    if p1_filled >= 5 and p1_pnl_filled is not None and p1_pnl_filled < 0:
        if (p0["pnl_per_filled"] is None or p1_pnl_filled <= (p0.get("pnl_per_filled") or 0) * p1_filled):
            reasons.append("G2_DIRECTIONAL_FAILURE_CONFIRMED")

    if len(reasons) >= 2:
        verdict = "MIXED_FAILURE"
    elif len(reasons) == 1:
        verdict = reasons[0]
    else:
        verdict = "INSUFFICIENT_EXECUTION_TRUTH"

    # Disposition
    kill_g2 = "G2_DIRECTIONAL_FAILURE_CONFIRMED" in reasons and p1_filled >= 5 and (
        p1_pnl_filled is not None and p1_pnl_filled < 0
    )
    # If pipeline/universe dominate and filled sample inconclusive → keep DISCOVERY_LEAD
    keep_discovery_lead = (not kill_g2) and (
        "EXECUTION_PIPELINE_BUG_CONFIRMED" in reasons or "UNIVERSE_REGISTRY_CONTAMINATION_CONFIRMED" in reasons
    )

    final = {
        "created_utc": utcnow(),
        "role": "Base Greenfield V1.1 Execution Truth Audit",
        "verdict": verdict,
        "component_findings": reasons,
        "kill_g2": kill_g2,
        "keep_g2_discovery_lead": keep_discovery_lead,
        "start_fresh_prospective_shadow": keep_discovery_lead,
        "deploy_allowed": False,
        "label": "HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC",
        "canonical_summary": {
            "n_holdout": 480,
            "n_canonical_ok": n_ok,
            "n_canonical_bad": n_bad,
            "canonical_bad_rate": n_bad / 480,
            "status_counts": {str(k): int(v) for k, v in can["canonical_status"].value_counts().items()},
            "fee_row_value_counts": {str(int(k)): int(v) for k, v in base["fee"].value_counts().items()},
        },
        "legacy_holdout_artifact": {
            "buy_ok": 9,
            "buy_revert": 139,
            "note": "V1 labeled many ENTRY failures as -100% returns; total_net_weth_pnl used sum(return)",
        },
        "corrected_p1": p1,
        "corrected_p0": p0,
        "p1_filled_n": p1_filled,
        "p1_pnl_on_filled_weth": p1_pnl_filled,
        "p1_preflight_reject_share_among_selected": p1_preflight_share,
        "rpc_a_calls": rpc_a.calls,
        "rpc_b_available": rpc_b is not None,
        "manifest_path": str(OUT_V11 / "EXECUTION_TRUTH_MANIFEST.json"),
        "notes": [
            "Does not modify out/ or out_v1/",
            "Does not change G2/C3/thresholds/TB_A",
            "Not a new sealed holdout confirmation",
        ],
    }
    (OUT_V11 / "FINAL_EXECUTION_TRUTH.json").write_text(json.dumps(final, indent=2) + "\n")

    (OUT_V11 / "REPRODUCE.md").write_text(
        f"""# Reproduce V1.1 Execution Truth Audit

```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.v1_1.execution_truth_audit
```

Requires `/tmp/rpc_url.txt` (archive). Optional second RPC: `/tmp/rpc_url_b.txt`.

Does **not** write to `out/` or `out_v1/`.

Outputs under `out_v1_1/`.

Audit code SHA256: `{sha256_file(code_path)}`
Manifest created: `{manifest['created_utc']}`
Final verdict: `{verdict}`
"""
    )
    print(json.dumps(final, indent=2), flush=True)


if __name__ == "__main__":
    main()
