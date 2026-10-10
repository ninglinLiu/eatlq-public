#!/usr/bin/env python3
"""Reusable, fail-closed, entry-state structural sellability gates for GL9.

Every remote read is pinned to ``entry_block`` or earlier and goes through the
archive-only :mod:`gl_readonly_rpc` allowlist.  Functions in this module accept
decision-time state only; outcome labels, rug flags, future blocks and returns
are intentionally absent from their APIs.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gl_readonly_rpc import Rpc, atomic_probe, balance_of


ROOT = Path("/opt/EATLQ")
WETH = "0x4200000000000000000000000000000000000006"
NPM = "0x03a520b32c04bf3beef7beb72e919cf822ed34f1"
HELPER = "0x000000000000000000000000000000000000b609"
CALL_FROM = "0x000000000000000000000000000000000000b607"
DEAD = "0x000000000000000000000000000000000000dead"
SPEND = 30_200_000_000_000_000
SPEND_CAP = 5_000_000_000_000_000_000
RECOVERY_MIN = 0.90
QTY_LO = 0.98
QTY_HI = 1.02
K_RESERVE = 15
K_OPEN = 30
LOG_CHUNK = 2_000

MINT_TOPIC = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
INCREASE_TOPIC = "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
INITIALIZE_TOPIC = "0x98636036cb66a9c19a37435efc1e90142190214e8abeb821bdba3f2990dd4c95"
POSITIONS_SELECTOR = "99fbab88"
OWNER_OF_SELECTOR = "6352211e"
RUNTIME_CODE_SELECTOR = "cb80965c"  # runtimeCode(address)

OWNER_SELECTORS = {"8da5cb5b": "owner", "893d20e8": "getOwner"}
DANGEROUS_SELECTORS = {
    "5c975abb": "paused", "8456cb59": "pause", "3f4ba83a": "unpause",
    "c2e5ec04": "setTradingEnabled", "8a8c523c": "enableTrading", "17700f01": "disableTrading",
    "f9f92be4": "blacklist", "1a895266": "unBlacklist", "fe575a87": "isBlacklisted",
    "153b0d1e": "setBlacklist", "342aa8b5": "setBot", "bfd79284": "bots",
    "40c10f19": "mint_address", "a0712d68": "mint_uint",
    "3659cfe6": "upgradeTo", "4f1ef286": "upgradeToAndCall", "5c60da1b": "implementation",
    "2e5bb6ff": "setTax", "c647b20e": "setTaxes", "dc1052e2": "setBuyTax",
    "8cd09d50": "setSellTax", "0b78f9c0": "setFees", "69fe0e2d": "setFee",
    "c0246668": "excludeFromFees",
}
EIP1167_PREFIX = bytes.fromhex("363d3d373d3d3d363d73")
EIP1167_SUFFIX = bytes.fromhex("5af43d82803e903d91602b57fd5bf3")


@dataclass(frozen=True)
class DecisionInput:
    """All fields permitted to influence a GL9 pre-trade decision."""

    pool: str
    token: str
    entry_block: int
    fee: int
    historical_qty_raw: int
    qty_source: str


def compile_code_reader_runtime() -> str:
    """Compile the local read-only helper and return its deployed runtime hex."""
    out = Path("/tmp/gl9_code_reader_forgeout")
    art = out / "EntryCodeReader.sol/EntryCodeReaderGL9.json"
    if not art.exists():
        subprocess.run(
            [str(Path("/root/.foundry/bin/forge")), "build", "--contracts",
             "scripts/research/base_gl9/EntryCodeReader.sol", "--out", str(out),
             "--cache-path", "/tmp/gl9_code_reader_forgecache"],
            cwd=ROOT, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return json.loads(art.read_text())["deployedBytecode"]["object"].removeprefix("0x")


def _decode_abi_bytes(raw: str) -> bytes:
    data = bytes.fromhex(raw.removeprefix("0x"))
    if len(data) < 64:
        raise ValueError("short ABI bytes return")
    offset = int.from_bytes(data[:32], "big")
    if offset + 32 > len(data):
        raise ValueError("bad ABI offset")
    size = int.from_bytes(data[offset:offset + 32], "big")
    start = offset + 32
    if size <= 0 or size > 1_000_000 or start + size > len(data):
        raise ValueError("bad runtime code size")
    return data[start:start + size]


def runtime_code_at(rpc: Rpc, helper_runtime: str, token: str, entry_block: int) -> dict[str, Any]:
    """Read token runtime code at entry using only eth_call + code state override."""
    data = "0x" + RUNTIME_CODE_SELECTOR + token.lower().removeprefix("0x").rjust(64, "0")
    tx = {"from": CALL_FROM, "to": HELPER, "data": data, "gas": hex(25_000_000)}
    override = {HELPER: {"code": "0x" + helper_runtime}}
    try:
        raw, qh, rh = rpc.call("eth_call", [tx, hex(entry_block), override])
        code = _decode_abi_bytes(raw)
        return {"ok": True, "code_hex": "0x" + code.hex(), "code_size": len(code),
                "evidence_block": entry_block, "request_sha256": qh, "response_sha256": rh}
    except Exception as exc:
        return {"ok": False, "code_hex": None, "code_size": 0, "evidence_block": entry_block,
                "error_class": str(exc)}


def executable_opcodes(code: bytes) -> list[int]:
    """Disassemble enough EVM to exclude PUSH payload bytes from opcode scans."""
    out: list[int] = []
    i = 0
    while i < len(code):
        op = code[i]
        out.append(op)
        i += 1 + (op - 0x5F if 0x60 <= op <= 0x7F else 0)
    return out


def executable_region(code: bytes) -> tuple[bytes, dict[str, Any]]:
    """Remove a standard Solidity CBOR metadata suffix before opcode scanning."""
    if len(code) >= 3:
        size = int.from_bytes(code[-2:], "big")
        start = len(code) - size - 2
        if size > 0 and start > 0 and 0xA0 <= code[start] <= 0xBF:
            return code[:start], {"metadata_stripped": True, "metadata_size": size + 2}
    return code, {"metadata_stripped": False, "metadata_size": 0}


def pushed_selectors(code: bytes) -> set[str]:
    """Return literal PUSH4 values from executable bytecode."""
    found: set[str] = set()
    i = 0
    while i < len(code):
        op = code[i]
        if op == 0x63 and i + 4 < len(code):
            found.add(code[i + 1:i + 5].hex())
        i += 1 + (op - 0x5F if 0x60 <= op <= 0x7F else 0)
    return found


def _call_address(rpc: Rpc, token: str, selector: str, block: int) -> dict[str, Any]:
    try:
        raw, qh, rh = rpc.call("eth_call", [{"to": token, "data": "0x" + selector}, hex(block)])
        if not raw or len(raw) < 66:
            raise ValueError("short address return")
        value = "0x" + raw[-40:].lower()
        return {"ok": True, "address": value, "evidence_block": block,
                "request_sha256": qh, "response_sha256": rh}
    except Exception as exc:
        return {"ok": False, "address": None, "evidence_block": block, "error_class": str(exc)}


def evaluate_l2(rpc: Rpc, helper_runtime: str, item: DecisionInput) -> dict[str, Any]:
    """Fail-closed runtime-code/admin-surface assessment at entry."""
    evidence = runtime_code_at(rpc, helper_runtime, item.token, item.entry_block)
    if not evidence["ok"]:
        return {"pass": False, "status": "UNRESOLVED_REJECT", "reasons": ["runtime_code_unavailable"],
                "evidence": evidence, "evidence_block": item.entry_block}
    code = bytes.fromhex(evidence["code_hex"][2:])
    executable, metadata = executable_region(code)
    selectors = pushed_selectors(executable)
    dangerous = sorted({DANGEROUS_SELECTORS[x] for x in selectors & DANGEROUS_SELECTORS.keys()})
    opcodes = set(executable_opcodes(executable))
    reasons = list(dangerous)
    if 0xF4 in opcodes:
        reasons.append("delegatecall_opcode")
    if 0xFF in opcodes:
        reasons.append("selfdestruct_opcode")
    if EIP1167_PREFIX in executable and EIP1167_SUFFIX in executable:
        reasons.append("eip1167_proxy_pattern")
    owner_calls = {}
    owner_unresolved = False
    for selector, name in OWNER_SELECTORS.items():
        if selector in selectors:
            call = _call_address(rpc, item.token, selector, item.entry_block)
            owner_calls[name] = call
            if not call["ok"]:
                owner_unresolved = True
            elif call["address"] != "0x" + "0" * 40:
                reasons.append(f"{name}_not_renounced")
    if owner_unresolved:
        reasons.append("owner_surface_unresolved")
    passed = not reasons
    return {"pass": passed, "status": "PASS" if passed else "REJECT",
            "reasons": sorted(set(reasons)), "runtime_code_size": len(code),
            "executable_code_size": len(executable), **metadata,
            "pushed_selectors": sorted(selectors), "dangerous_selectors": dangerous,
            "owner_calls": owner_calls, "evidence": {k: v for k, v in evidence.items() if k != "code_hex"},
            "runtime_code_sha256": __import__("hashlib").sha256(code).hexdigest(),
            "evidence_block": item.entry_block}


def converge_historical_qty(rpc: Rpc, atomic_bytecode: str, item: DecisionInput) -> dict[str, Any]:
    """Run entry-state atomic round trips scaled to the frozen historical quantity."""
    if item.fee <= 0 or item.historical_qty_raw <= 0:
        return {"pass": False, "status": "UNRESOLVED_REJECT", "reason": "fee_or_qty_missing",
                "evidence_block": item.entry_block, "iterations": []}
    baseline = atomic_probe(rpc, atomic_bytecode, item.token, item.fee, SPEND, item.entry_block)
    iterations = [{"spend_wei": str(SPEND), **baseline}]
    got = int(baseline.get("received_target_raw") or 0)
    if not baseline.get("ok") or not baseline.get("buy_ok") or got <= 0:
        return {"pass": False, "status": "REJECT" if baseline.get("ok") else "UNRESOLVED_REJECT",
                "reason": "baseline_buy_unavailable", "evidence_block": item.entry_block,
                "target_qty_raw": str(item.historical_qty_raw), "qty_source": item.qty_source,
                "iterations": iterations}
    spend = max(1, int(SPEND * item.historical_qty_raw / got))
    for _ in range(4):
        if spend > SPEND_CAP:
            return {"pass": False, "status": "REJECT", "reason": "spend_cap_exceeded",
                    "evidence_block": item.entry_block, "target_qty_raw": str(item.historical_qty_raw),
                    "qty_source": item.qty_source, "iterations": iterations}
        probe = atomic_probe(rpc, atomic_bytecode, item.token, item.fee, spend, item.entry_block)
        iterations.append({"spend_wei": str(spend), **probe})
        if not probe.get("ok"):
            return {"pass": False, "status": "UNRESOLVED_REJECT", "reason": "atomic_rpc_unresolved",
                    "evidence_block": item.entry_block, "target_qty_raw": str(item.historical_qty_raw),
                    "qty_source": item.qty_source, "iterations": iterations}
        actual = int(probe.get("received_target_raw") or 0)
        if actual <= 0:
            return {"pass": False, "status": "REJECT", "reason": "zero_buy_receive",
                    "evidence_block": item.entry_block, "target_qty_raw": str(item.historical_qty_raw),
                    "qty_source": item.qty_source, "iterations": iterations}
        ratio = actual / item.historical_qty_raw
        if QTY_LO <= ratio <= QTY_HI:
            recovery = probe.get("recovery_ratio")
            passed = bool(probe.get("buy_ok") and probe.get("sell_ok") and recovery is not None
                          and recovery >= RECOVERY_MIN)
            return {"pass": passed, "status": "PASS" if passed else "REJECT",
                    "reason": "ok" if passed else "sell_or_recovery_below_threshold",
                    "evidence_block": item.entry_block, "target_qty_raw": str(item.historical_qty_raw),
                    "actual_qty_raw": str(actual), "qty_ratio": ratio, "qty_source": item.qty_source,
                    "spend_wei": str(spend), "recovery_ratio": recovery, "iterations": iterations}
        spend = max(1, int(spend * item.historical_qty_raw / actual))
    return {"pass": False, "status": "UNRESOLVED_REJECT", "reason": "qty_tolerance_not_converged",
            "evidence_block": item.entry_block, "target_qty_raw": str(item.historical_qty_raw),
            "qty_source": item.qty_source, "iterations": iterations}


def _get_logs(rpc: Rpc, address: str, start: int, end: int, topic0: str) -> dict[str, Any]:
    params = [{"address": address, "fromBlock": hex(start), "toBlock": hex(end), "topics": [topic0]}]
    try:
        logs, qh, rh = rpc.call("eth_getLogs", params)
        return {"ok": True, "logs": logs or [], "from_block": start, "to_block": end,
                "request_sha256": qh, "response_sha256": rh}
    except Exception as exc:
        return {"ok": False, "logs": [], "from_block": start, "to_block": end,
                "error_class": str(exc)}


def _get_logs_range(rpc: Rpc, address: str, start: int, end: int, topic0: str) -> dict[str, Any]:
    """Read a complete inclusive interval in frozen 2,000-block pages."""
    if start < 0 or end < start:
        return {"ok": False, "logs": [], "from_block": start, "to_block": end,
                "pages": [], "error_class": "invalid_log_range"}
    logs = []
    pages = []
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + LOG_CHUNK - 1)
        page = _get_logs(rpc, address, cursor, stop, topic0)
        pages.append({k: v for k, v in page.items() if k != "logs"})
        if not page["ok"]:
            return {"ok": False, "logs": [], "from_block": start, "to_block": end,
                    "pages": pages, "error_class": page.get("error_class")}
        logs.extend(page["logs"])
        cursor = stop + 1
    logs.sort(key=lambda x: (int(x.get("blockNumber", "0x0"), 16),
                             int(x.get("transactionIndex", "0x0"), 16),
                             int(x.get("logIndex", "0x0"), 16)))
    return {"ok": True, "logs": logs, "from_block": start, "to_block": end, "pages": pages}


def find_initialize(rpc: Rpc, item: DecisionInput) -> dict[str, Any]:
    """Find the unique V3 Initialize event using only pre-entry paged logs."""
    high = item.entry_block
    low = max(0, item.entry_block - LOG_CHUNK + 1)
    all_pages = []
    while True:
        interval = _get_logs_range(rpc, item.pool, low, high, INITIALIZE_TOPIC)
        all_pages.extend(interval.get("pages") or [])
        if not interval["ok"]:
            return {"ok": False, "logs": [], "error_class": interval.get("error_class"),
                    "pages": all_pages, "from_block": low, "to_block": item.entry_block}
        blocks = sorted({int(x["blockNumber"], 16) for x in interval["logs"]})
        if blocks:
            return {"ok": len(blocks) == 1, "logs": interval["logs"],
                    "initialize_block": blocks[0] if len(blocks) == 1 else None,
                    "distinct_block_n": len(blocks), "pages": all_pages,
                    "from_block": low, "to_block": item.entry_block,
                    "error_class": None if len(blocks) == 1 else "multiple_initialize_blocks"}
        if low == 0:
            return {"ok": False, "logs": [], "error_class": "initialize_not_found",
                    "pages": all_pages, "from_block": 0, "to_block": item.entry_block}
        high = low - 1
        low = max(0, low - LOG_CHUNK)


def _call_words(rpc: Rpc, to: str, data: str, block: int) -> dict[str, Any]:
    try:
        raw, qh, rh = rpc.call("eth_call", [{"to": to, "data": data}, hex(block)])
        b = bytes.fromhex(raw.removeprefix("0x"))
        if len(b) < 32 or len(b) % 32:
            raise ValueError("bad word return")
        return {"ok": True, "words": [int.from_bytes(b[i:i + 32], "big") for i in range(0, len(b), 32)],
                "evidence_block": block, "request_sha256": qh, "response_sha256": rh}
    except Exception as exc:
        return {"ok": False, "words": [], "evidence_block": block, "error_class": str(exc)}


def evaluate_l3(rpc: Rpc, item: DecisionInput) -> dict[str, Any]:
    """Resolve entry-time reserve trend and V3 position-NFT custody, fail closed."""
    prior_block = item.entry_block - K_RESERVE
    prior = balance_of(rpc, WETH, item.pool, prior_block) if prior_block >= 0 else {"ok": False}
    entry = balance_of(rpc, WETH, item.pool, item.entry_block)
    den = int(prior.get("value_raw") or 0)
    ratio = int(entry.get("value_raw") or 0) / den if den > 0 and entry.get("ok") else None
    reserve_pass = ratio is not None and int(entry.get("value_raw") or 0) > 0 and ratio >= 0.95

    initialize = find_initialize(rpc, item)
    mint = (_get_logs_range(rpc, item.pool, int(initialize["initialize_block"]), item.entry_block, MINT_TOPIC)
            if initialize.get("ok") else {"ok": False, "logs": [], "from_block": None,
                                           "to_block": item.entry_block, "pages": [],
                                           "error_class": "initialize_unresolved"})
    mapping_complete = mint["ok"] and bool(mint["logs"])
    tx_blocks: dict[str, int] = {}
    for log in mint["logs"]:
        tx_blocks[str(log.get("transactionHash", "")).lower()] = int(log["blockNumber"], 16)
    increase_by_tx: dict[str, list[int]] = {}
    increase_evidence = []
    for block in sorted(set(tx_blocks.values())):
        inc = _get_logs_range(rpc, NPM, block, block, INCREASE_TOPIC)
        increase_evidence.append({k: v for k, v in inc.items() if k != "logs"})
        if not inc["ok"]:
            mapping_complete = False
            continue
        for log in inc["logs"]:
            if len(log.get("topics") or []) >= 2:
                tx = str(log.get("transactionHash", "")).lower()
                increase_by_tx.setdefault(tx, []).append(int(log["topics"][1], 16))
    token_ids: set[int] = set()
    for tx in tx_blocks:
        ids = increase_by_tx.get(tx, [])
        if not ids:
            mapping_complete = False
        token_ids.update(ids)

    positions = []
    active = []
    target_set = {int(item.token, 16), int(WETH, 16)}
    for token_id in sorted(token_ids):
        pos = _call_words(rpc, NPM, "0x" + POSITIONS_SELECTOR + f"{token_id:064x}", item.entry_block)
        rec: dict[str, Any] = {"token_id": str(token_id), "position_evidence": pos}
        if not pos["ok"] or len(pos["words"]) < 8:
            mapping_complete = False
            rec["status"] = "POSITION_UNRESOLVED"
            positions.append(rec)
            continue
        words = pos["words"]
        matches = {words[2], words[3]} == target_set and words[4] == item.fee
        rec.update({"matches_pool": matches, "liquidity_raw": str(words[7])})
        if matches and words[7] > 0:
            owner = _call_words(rpc, NPM, "0x" + OWNER_OF_SELECTOR + f"{token_id:064x}", item.entry_block)
            owner_addr = "0x" + f"{owner['words'][0]:064x}"[-40:] if owner["ok"] and owner["words"] else None
            safe = owner_addr == DEAD
            rec.update({"owner_evidence": owner, "owner": owner_addr, "safe_custody": safe})
            if owner_addr is None:
                mapping_complete = False
            active.append(rec)
        positions.append(rec)
    lp_pass = mapping_complete and bool(active) and all(x.get("safe_custody") for x in active)
    passed = reserve_pass and lp_pass
    reasons = []
    if not reserve_pass:
        reasons.append("reserve_unresolved_or_declining")
    if not mapping_complete:
        reasons.append("lp_mapping_unresolved")
    elif not active:
        reasons.append("no_active_position")
    elif not all(x.get("safe_custody") for x in active):
        reasons.append("active_lp_not_burn_custodied")
    return {"pass": passed, "status": "PASS" if passed else "REJECT",
            "reasons": reasons, "evidence_block": item.entry_block,
            "reserve": {"lookback_block": prior_block, "entry_block": item.entry_block,
                        "lookback": prior, "entry": entry, "ratio": ratio, "pass": reserve_pass},
            "lp": {"initialize_evidence": {k: v for k, v in initialize.items() if k != "logs"},
                   "mint_log_evidence": {k: v for k, v in mint.items() if k != "logs"},
                   "mint_log_n": len(mint["logs"]), "increase_evidence": increase_evidence,
                   "mapping_complete": mapping_complete, "token_id_n": len(token_ids),
                   "active_position_n": len(active), "positions": positions, "pass": lp_pass}}


def evaluate_l4(rpc: Rpc, atomic_bytecode: str, item: DecisionInput) -> dict[str, Any]:
    """Require pool age >=30 blocks and independently repeat the L1 check."""
    init = find_initialize(rpc, item)
    init_block = init.get("initialize_block") if init.get("ok") else None
    unique = bool(init.get("ok"))
    age = item.entry_block - init_block if init_block is not None else None
    age_pass = unique and age is not None and age >= K_OPEN
    repeat = converge_historical_qty(rpc, atomic_bytecode, item)
    passed = age_pass and repeat["pass"]
    return {"pass": passed, "status": "PASS" if passed else "REJECT",
            "reasons": (["initialize_unresolved_or_too_young"] if not age_pass else [])
                       + (["repeat_l1_fail"] if not repeat["pass"] else []),
            "evidence_block": item.entry_block, "initialize_log_evidence": {k: v for k, v in init.items() if k != "logs"},
            "initialize_log_n": len(init["logs"]), "initialize_block": init_block,
            "age_blocks": age, "age_pass": age_pass, "repeat_l1": repeat}


def evaluate_external_snapshot(snapshot: dict[str, Any] | None, signal_time: str) -> dict[str, Any]:
    """Admit external data only when an explicit publication time predates entry time."""
    if not isinstance(snapshot, dict):
        return {"status": "EXTERNAL_UNAVAILABLE_PIT", "eligible": False, "critical": []}
    published = snapshot.get("published_at")
    provenance = snapshot.get("snapshot_provenance")
    if not published or not provenance or str(published) >= str(signal_time):
        return {"status": "EXTERNAL_UNAVAILABLE_PIT", "eligible": False, "critical": []}
    critical = list(snapshot.get("critical") or [])
    return {"status": "EXTERNAL_ELIGIBLE_PIT", "eligible": True, "critical": critical,
            "published_at": published, "snapshot_provenance": provenance}
