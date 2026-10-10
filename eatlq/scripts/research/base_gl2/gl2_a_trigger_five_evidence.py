#!/usr/bin/env python3
"""Settle GL1-12 trigger-exit unresolved rows with explicit E1-E5 evidence."""
from __future__ import annotations

import bisect
import hashlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path("/opt/EATLQ")
DIAG = ROOT / "mvp/diagnostics"
CORE = DIAG / "base_reaudit10_seconds_rpc_20260715.json"
OUT = DIAG / "base_gl2_a_trigger_five_evidence_rpc_20260715.json"
SUMMARY = DIAG / "base_gl2_a_rpc_ledger_20260715.json"
WINDOWS = (1, 5, 15)
WETH = "0x4200000000000000000000000000000000000006"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BURN = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"
COLLECT = "0x70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0"
CUTOFF = 46865124
PREREG_SHA256 = "331009ad147eedc5189fa9f7ab5651b479d5065ec6cd29532816ecbf6e17ed07"


def module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(value)
    return value


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def topic_address(address: str) -> str:
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


def log_value(log: dict) -> int:
    return int(log.get("data", "0x0"), 16)


def direction(swap: dict, orient: int):
    weth, token = (swap["a0"], swap["a1"]) if orient == -1 else (swap["a1"], swap["a0"])
    if weth < 0 and token > 0:
        return "sell", -weth, token
    if weth > 0 and token < 0:
        return "buy", weth, -token
    return "other", abs(weth), abs(token)


def first_sell(swaps: dict, pool: str, orient: int, block: int):
    rows = swaps.get(pool, [])
    index = bisect.bisect_left([r["block"] for r in rows], block)
    for row in rows[index:]:
        if row["block"] > CUTOFF:
            break
        side, weth, token = direction(row, orient)
        if side == "sell" and weth > 0 and token > 0:
            return {**row, "weth_out_raw": str(weth), "token_in_raw": str(token)}
    return None


def receipt_sell_evidence(rpc, tx: str, pool: str, token: str):
    try:
        raw, meta = rpc.call("eth_getTransactionReceipt", [tx])
    except Exception as exc:
        return {"status": "unknown", "error_class": "rpc_error", "error_sha256": hashlib.sha256(str(exc).encode()).hexdigest()}
    logs = raw.get("logs", []) if raw else []
    pool_swap = any(log.get("address", "").lower() == pool and len(log.get("topics", [])) and log["topics"][0].lower().startswith("0xc42079f9") for log in logs)
    weth_out = sum(log_value(log) for log in logs if log.get("address", "").lower() == WETH and len(log.get("topics", [])) >= 3 and log["topics"][0].lower() == TRANSFER and log["topics"][1].lower() == topic_address(pool))
    token_in = sum(log_value(log) for log in logs if log.get("address", "").lower() == token and len(log.get("topics", [])) >= 3 and log["topics"][0].lower() == TRANSFER and log["topics"][2].lower() == topic_address(pool))
    return {
        "status": "measured",
        "receipt_status": int(raw.get("status", "0x0"), 16),
        "pool_swap_log": pool_swap,
        "weth_out_raw": str(weth_out),
        "token_in_raw": str(token_in),
        "verified_true_sell": bool(int(raw.get("status", "0x0"), 16) == 1 and pool_swap and weth_out > 0 and token_in > 0),
        **meta,
    }


def direct_resolution(row: dict, point: dict, swap_rows: dict) -> bool:
    entry = next(p for p in row["entry_points"] if p["offset_blocks"] == 1)["atomic"]
    if not entry.get("buy_ok") or int(entry.get("received_target", 0) or 0) <= 0:
        return True
    guard = point["fresh_atomic_guard"]
    quote = point["fresh_exact_size_quote"]
    if guard.get("status") == "measured" and guard.get("sell_ok") and guard.get("recovery_ratio", 0) > 0.1 and quote.get("status") == "measured" and int(quote.get("amount_out", 0) or 0) > 0:
        return True
    # A real print can confirm sellability, but cannot price the synthetic
    # holding without the exact-size quote.
    if quote.get("status") == "measured" and int(quote.get("amount_out", 0) or 0) > 0:
        return first_sell(swap_rows, row["pool"], int(row["orient"]), int(point["scheduled_block"])) is not None
    return False


def collect_row(rpc, old, code: str, swaps: dict, row: dict) -> dict:
    entry_point = next(p for p in row["entry_points"] if p["offset_blocks"] == 1)
    qty = int(entry_point["atomic"].get("received_target", 0) or 0)
    fee = int(row["fee_evidence"]["fee"])
    old_target_points = {p["offset_blocks"]: p for p in row["exit_points"] if p["offset_blocks"] in WINDOWS}
    target_points = {}
    for window in WINDOWS:
        block = int(row["trigger_block"]) + window
        atomic = old.safe(lambda b=block: old.atomic_at(rpc, code, row["token"], fee, b), "gl2a_fresh_trigger_atomic")
        quote = {"status": "not_applicable", "reason": "no_true_entry_holding"}
        if qty:
            quote = old.safe(lambda b=block: old.quote_at(rpc, row["token"], fee, qty, b), "gl2a_fresh_trigger_exact_quote")
        target_points[window] = {
            "scheduled_block": block,
            "fresh_atomic_guard": atomic,
            "fresh_exact_size_quote": quote,
            "prior_core_atomic_guard": old_target_points[window]["atomic_guard"],
            "prior_core_exact_size_quote": old_target_points[window].get("exact_size_quotes_by_entry_offset", {}).get("1", {}),
        }
    unresolved = [w for w, p in target_points.items() if not direct_resolution(row, p, swaps)]
    base = {
        "pool": row["pool"], "token": row["token"], "orient": int(row["orient"]),
        "selected": bool(row["selected"]), "entry_block": int(entry_point["scheduled_block"]),
        "trigger_block": int(row["trigger_block"]), "true_position_token_raw": str(qty) if qty else None,
        "fresh_trigger_points": {str(k): v for k, v in target_points.items()},
        "unresolved_windows_before_five_evidence": sorted(unresolved), "windows": {},
    }
    if not unresolved:
        return base

    end = max(target_points[w]["scheduled_block"] for w in unresolved)
    pool_topic = topic_address(row["pool"])
    balances = {}
    for block in sorted({int(entry_point["scheduled_block"]), *(int(target_points[w]["scheduled_block"]) for w in unresolved)}):
        balances[str(block)] = old.safe(lambda b=block: old.balance_at(rpc, WETH, row["pool"], b), "gl2a_pool_weth_balance")
    pool_logs = old.safe(lambda: old.logs_at(rpc, row["pool"], int(entry_point["scheduled_block"]), to_block=end), "gl2a_pool_mechanism_logs")
    weth_out = old.safe(lambda: old.logs_at(rpc, WETH, int(entry_point["scheduled_block"]), [TRANSFER, pool_topic], end), "gl2a_weth_out_logs")
    token_logs = old.safe(lambda: old.logs_at(rpc, row["token"], int(entry_point["scheduled_block"]), [TRANSFER], end), "gl2a_token_logs")
    burn_collect = [log for log in pool_logs.get("logs", []) if log.get("topics") and log["topics"][0].lower() in (BURN, COLLECT)]
    outgoing = sorted(weth_out.get("logs", []), key=log_value, reverse=True)
    direct_txs = {log.get("transactionHash") for log in burn_collect}
    candidate = next((log for log in outgoing if log.get("transactionHash") in direct_txs), None)
    candidate_receipt = {"status": "not_applicable"}
    candidate_trace = {"status": "not_applicable"}
    if candidate:
        tx = candidate["transactionHash"]
        try:
            receipt, meta = rpc.call("eth_getTransactionReceipt", [tx])
            candidate_receipt = {"status": "measured", "tx": tx, "block": int(receipt["blockNumber"], 16), "receipt_status": int(receipt["status"], 16), **meta}
        except Exception as exc:
            candidate_receipt = {"status": "unknown", "error_sha256": hashlib.sha256(str(exc).encode()).hexdigest()}
        try:
            trace, meta = rpc.call("debug_traceTransaction", [tx, {"tracer": "callTracer", "timeout": "30s"}], timeout=180)
            candidate_trace = {"status": "measured", "trace_sha256": hashlib.sha256(json.dumps(trace, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), **meta}
        except Exception as exc:
            candidate_trace = {"status": "unknown", "error_sha256": hashlib.sha256(str(exc).encode()).hexdigest()}

    later = {w: first_sell(swaps, row["pool"], int(row["orient"]), int(target_points[w]["scheduled_block"])) for w in unresolved}
    receipts = {}
    for tx in sorted({v["tx"] for v in later.values() if v}):
        receipts[tx] = receipt_sell_evidence(rpc, tx, row["pool"], row["token"])

    no_later_any = any(v is None for v in later.values())
    cutoff_atomic = {"status": "not_applicable"}
    cutoff_quote = {"status": "not_applicable"}
    if no_later_any and qty:
        cutoff_atomic = old.safe(lambda: old.atomic_at(rpc, code, row["token"], fee, CUTOFF), "gl2a_cutoff_atomic")
        cutoff_quote = old.safe(lambda: old.quote_at(rpc, row["token"], fee, qty, CUTOFF), "gl2a_cutoff_exact_quote")

    pre_balance = balances[str(entry_point["scheduled_block"])]
    for window in unresolved:
        point = target_points[window]
        post_balance = balances[str(point["scheduled_block"])]
        pre_raw = int(pre_balance.get("raw", 0) or 0) if pre_balance.get("status") == "measured" else None
        post_raw = int(post_balance.get("raw", 0) or 0) if post_balance.get("status") == "measured" else None
        ratio = post_raw / pre_raw if pre_raw else None
        candidate_before_target = bool(candidate and int(candidate.get("blockNumber", "0x0"), 16) <= int(point["scheduled_block"]))
        e1 = bool(ratio is not None and ratio <= 0.05 and candidate_before_target and candidate_receipt.get("receipt_status") == 1 and candidate_trace.get("status") == "measured")
        guard = point["fresh_atomic_guard"]
        quote = point["fresh_exact_size_quote"]
        e2_pre = bool(entry_point["atomic"].get("sell_ok") and entry_point["atomic"].get("recovery_ratio", 0) > 0.1)
        e2_post = bool(not guard.get("sell_ok") or quote.get("status") != "measured" or int(quote.get("amount_out", 0) or 0) == 0)
        trade = later[window]
        trade_receipt = receipts.get(trade["tx"]) if trade else None
        later_verified = bool(trade and trade_receipt and trade_receipt.get("verified_true_sell"))
        later_unknown = bool(trade and (not trade_receipt or trade_receipt.get("status") != "measured"))
        cutoff_recovered = bool(cutoff_atomic.get("sell_ok") and cutoff_quote.get("status") == "measured" and int(cutoff_quote.get("amount_out", 0) or 0) > 0)
        e3_no_later = None if later_unknown else not later_verified
        recovered = later_verified or cutoff_recovered
        e4_no_recovery = None if later_unknown else not recovered
        e5 = int(point["scheduled_block"]) <= CUTOFF
        confirmed = bool(e1 and e2_pre and e2_post and e3_no_later is True and e4_no_recovery is True and e5)
        base["windows"][str(window)] = {
            "target_block": int(point["scheduled_block"]),
            "E1": {"mechanism_confirmed": e1, "pre_pool_weth_raw": None if pre_raw is None else str(pre_raw), "post_pool_weth_raw": None if post_raw is None else str(post_raw), "post_pre_ratio": ratio, "burn_collect_logs": len(burn_collect), "weth_out_logs": len(outgoing), "candidate_tx": candidate.get("transactionHash") if candidate_before_target else None, "candidate_receipt": candidate_receipt, "candidate_trace": candidate_trace, "attempted": True},
            "E2": {"pre_exact_atomic_sellable": e2_pre, "post_failure": e2_post, "post_atomic_guard": guard, "post_exact_size_quote": quote, "same_true_position_token_raw": str(qty), "attempted": True},
            "E3": {"later_real_sell_found": trade is not None, "later_real_sell": trade, "receipt_verification": trade_receipt, "no_later_real_sell_through_cutoff": e3_no_later, "scan_sources": ["base_b5_real_swaps_20260714.tsv", "base_b8_future_swaps_20260714.tsv"], "scan_cutoff_block": CUTOFF, "attempted": True},
            "E4": {"recovered": recovered, "no_recovery_through_cutoff": e4_no_recovery, "cutoff_atomic_guard": cutoff_atomic, "cutoff_exact_size_quote": cutoff_quote, "attempted": True},
            "E5": {"observation_complete": e5, "cutoff_block": CUTOFF, "cutoff_timestamp": "2026-06-08T18:29:40Z", "attempted": True},
            "confirmed_nonrecovering_rug": confirmed,
            "adjudication": "CONFIRMED_NONRECOVERING_RUG" if confirmed else "RECOVERED_OR_TEMPORARY" if recovered else "UNRESOLVED_FIVE_EVIDENCE_INCOMPLETE",
        }
    base["shared_rpc_evidence"] = {"balances": balances, "pool_logs": pool_logs, "weth_out_logs": weth_out, "token_logs": token_logs}
    return base


def main() -> None:
    collector = module(ROOT / "scripts/research/base_gl2/gl2_a_five_evidence_rpc.py", "gl2a_collector")
    old = module(ROOT / "scripts/research/base_reaudit10/reaudit10_rpc.py", "gl2a_old_rpc")
    analyzer = module(ROOT / "scripts/research/base_reaudit10/reaudit10_analyze.py", "gl2a_swaps")
    rpc = collector.Rpc(collector.env_value("EXECUTOR_READ_RPC_URL"), reset_ledger=False)
    code = old.compile_bytecode()
    core = json.loads(CORE.read_text())
    swaps = analyzer.load_swaps()
    rows = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(collect_row, rpc, old, code, swaps, row) for row in core["rows"]]
        for i, future in enumerate(as_completed(futures), 1):
            rows.append(future.result())
            if i % 50 == 0 or i == len(futures):
                print(f"[GL2-A trigger E1-E5] {i}/{len(futures)} attempts={rpc.attempts}", flush=True)
    rows.sort(key=lambda r: r["pool"])
    before = {str(w): sum(w in r["unresolved_windows_before_five_evidence"] for r in rows) for w in WINDOWS}
    confirmed = {str(w): sum(r["windows"].get(str(w), {}).get("confirmed_nonrecovering_rug", False) for r in rows) for w in WINDOWS}
    recovered = {str(w): sum(r["windows"].get(str(w), {}).get("adjudication") == "RECOVERED_OR_TEMPORARY" for r in rows) for w in WINDOWS}
    payload = {"schema_version": 1, "research_only": True, "preregistration_sha256": PREREG_SHA256, "timeline": {"entry": "entry_block+1", "exit": "trigger_block+1/+5/+15", "role": "GL1-12 authoritative estimand"}, "cutoff_block": CUTOFF, "summary": {"rows": 431, "unresolved_before_five_evidence": before, "confirmed_nonrecovering_rug": confirmed, "recovered_or_temporary": recovered}, "rows": rows, "source_hashes": {"base_b5_real_swaps_20260714.tsv": sha(DIAG / "base_b5_real_swaps_20260714.tsv"), "base_b8_future_swaps_20260714.tsv": sha(DIAG / "base_b8_future_swaps_20260714.tsv"), "base_reaudit10_seconds_rpc_20260715.json": sha(CORE)}}
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    ledger = {"schema_version": 3, "research_only": True, "preregistration_sha256": PREREG_SHA256, "phases": ["all_431_prereg_text_b0_ladder", "gl1_trigger_unresolved_E1_E5"], "rpc": rpc.summary(), "outputs": {str(OUT.relative_to(ROOT)): sha(OUT), "mvp/diagnostics/base_gl2_a_five_evidence_rpc_20260715.json": sha(DIAG / "base_gl2_a_five_evidence_rpc_20260715.json")}}
    SUMMARY.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"summary": payload["summary"], "rpc": ledger["rpc"]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
