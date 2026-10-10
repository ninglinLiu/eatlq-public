#!/usr/bin/env python3
"""B8 pinned archive evidence. Allowed methods are read-only; no signing/send path exists."""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import itertools
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path("/opt/EATLQ")
OUT = ROOT / "mvp/diagnostics/base_b8_rpc_evidence_20260714.json"
PREREG = ROOT / "configs/research/base_b8_honest_execution_preregistration_20260714.json"
SPEND = 10**15
FROM = "0x000000000000000000000000000000000000b808"
MAX_ATTEMPTS = 6000
DELAYS = (0, 30, 60)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

sys.path.insert(0, str(ROOT / "scripts/lib"))
from v3_quoterv2_struct import build_quote_exact_input_single_calldata, QUOTER_V2_BASE, WETH_BASE


class Rpc:
    allowed = {"eth_blockNumber", "eth_getBlockByNumber", "eth_getTransactionReceipt", "eth_call"}

    def __init__(self, url):
        self.url = url
        self.lock = threading.Lock()
        self.logical = 0
        self.attempts = 0
        self.methods = Counter()

    def call(self, method, params):
        if method not in self.allowed:
            raise RuntimeError(f"forbidden method {method}")
        with self.lock:
            self.logical += 1
            ident = self.logical
        last = None
        for retry in range(3):
            with self.lock:
                if self.attempts >= MAX_ATTEMPTS:
                    raise RuntimeError("RPC_ATTEMPT_HARD_CAP")
                self.attempts += 1
                self.methods[method] += 1
            try:
                body = json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}).encode()
                req = Request(self.url, data=body, headers={"content-type": "application/json", "user-agent": "EATLQ-B8-readonly/1"})
                with urlopen(req, timeout=120) as resp:
                    obj = json.load(resp)
                if obj.get("error"):
                    raise RuntimeError(str(obj["error"]))
                result = obj.get("result")
                req_hash = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                res_hash = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                return result, req_hash, res_hash
            except Exception as exc:
                last = exc
                time.sleep(0.35 * (retry + 1))
        raise RuntimeError(str(last)[:300])


def b5_module():
    path = ROOT / "scripts/research/base_b5/b5_offline_reverify.py"
    spec = importlib.util.spec_from_file_location("b5_for_b8_rpc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bytecode():
    out = Path("/tmp/b8forgeout")
    art = out / "AtomicRoundTrip.sol/AtomicRoundTripCreateB4.json"
    if not art.exists():
        subprocess.run(
            [str(Path.home() / ".foundry/bin/forge"), "build", "--contracts", "scripts/research/base_b4/AtomicRoundTrip.sol", "--out", str(out), "--cache-path", "/tmp/b8forgecache"],
            cwd=ROOT,
            check=True,
        )
    return json.loads(art.read_text())["bytecode"]["object"].removeprefix("0x")


def atomic_data(code, token, fee):
    return "0x" + code + token.removeprefix("0x").rjust(64, "0") + f"{fee:064x}" + f"{SPEND:064x}"


def decode_atomic(raw):
    if not raw or len(raw) < 2 + 16 * 64:
        raise ValueError("short atomic return")
    words = [int(raw[2 + i * 64:2 + (i + 1) * 64], 16) for i in range(16)]
    keys = ["flags", "router_buy_out", "target_before", "target_after_buy", "target_after_sell", "router_sell_out", "weth_after_deposit", "weth_after_buy", "weth_after_sell", "pin_block", "chain_id", "parent_block_hash", "weth_approve_error_hash", "buy_error_hash", "target_approve_error_hash", "sell_error_hash"]
    return dict(zip(keys, words))


def parse_swap(z):
    pool, block, ms, tx, log_index, a0, a1, tick = z
    return {"pool": pool, "block": int(block), "ms": int(ms), "tx": tx, "log_index": int(log_index), "a0": int(a0), "a1": int(a1), "tick": int(tick)}


def prepare_rows():
    mod = b5_module()
    df, _, _ = mod.load_base()
    df["regime"] = df.t0.map(mod.regime)
    X, y = mod.features(df)
    prob, cut, _ = mod.fit_forward(df, X, y, list(X.columns))
    df["selected"] = (prob <= cut) & df.fric.notna() & (df.fric >= -0.05) & df.regime.notna()
    scoped = {r.pool: r for r in df[df.regime.notna()].itertuples(index=False)}
    entries = {}
    with open(mod.SWAPS) as fh:
        for pool, group in itertools.groupby(csv.reader(fh, delimiter="\t"), key=lambda x: x[0]):
            if pool not in scoped:
                continue
            row = scoped[pool]
            for z in group:
                swap = parse_swap(z)
                side, price, weth, token = mod.direction_price(swap, int(row.orient))
                if int(row.entry_block) <= swap["block"] <= int(row.entry_block) + 900 and side == "buy" and weth >= SPEND:
                    qty = SPEND * token // weth
                    entries[pool] = {"block": swap["block"], "tx": swap["tx"], "log_index": swap["log_index"], "weth_raw": str(weth), "token_raw": str(token), "position_qty": str(qty), "unit_price_weth": price}
                    break
    rows = []
    for pool, row in scoped.items():
        trigger = int(row.legacy_exb) if not (isinstance(row.legacy_exb, float) and math.isnan(row.legacy_exb)) else int(row.entry_block) + 7200
        rows.append({
            "pool": pool,
            "token": row.alt.lower(),
            "orient": int(row.orient),
            "regime": row.regime,
            "signal_time": row.t0.isoformat(),
            "entry_block": int(row.entry_block),
            "trigger_block": trigger,
            "trigger_source": "legacy_exb" if not (isinstance(row.legacy_exb, float) and math.isnan(row.legacy_exb)) else "hard_timeout_entry_plus_7200",
            "selected": bool(row.selected),
            "entry": entries.get(pool),
        })
    entered_selected = [r for r in rows if r["selected"] and r["entry"]]
    holder_sample = {r["pool"] for r in sorted(entered_selected, key=lambda x: hashlib.sha256(("B8-holder|" + x["pool"]).encode()).hexdigest())[:20]}
    for row in rows:
        row["holder_sample"] = row["pool"] in holder_sample
    assert len(rows) == 431 and sum(r["selected"] for r in rows) == 116 and len(holder_sample) == 20
    return rows


def block_evidence(rpc, block):
    raw, req_h, res_h = rpc.call("eth_getBlockByNumber", [hex(block), False])
    return {"number": block, "hash": raw.get("hash"), "parentHash": raw.get("parentHash"), "timestamp": int(raw["timestamp"], 16), "request_sha256": req_h, "response_sha256": res_h}


def atomic_evidence(rpc, code, row, fee, block):
    tx = {"from": FROM, "data": atomic_data(code, row["token"], fee), "value": hex(SPEND), "gas": hex(20_000_000)}
    raw, req_h, res_h = rpc.call("eth_call", [tx, hex(block), {FROM: {"balance": hex(10**18)}}])
    dec = decode_atomic(raw)
    received = dec["target_after_buy"] - dec["target_before"]
    buy_ok = bool(dec["flags"] & 2) and received > 0
    sell_ok = bool(dec["flags"] & 8) and dec["weth_after_sell"] > dec["weth_after_buy"]
    recovered = dec["weth_after_sell"] - dec["weth_after_buy"] if sell_ok else 0
    return {"status": "measured", "buy_ok": buy_ok, "sell_ok": sell_ok, "received_target": str(received), "recovered_weth": str(recovered), "flags": dec["flags"], "pin_block_returned": dec["pin_block"], "parent_block_hash_returned": f"0x{dec['parent_block_hash']:064x}", "request_sha256": req_h, "response_sha256": res_h}


def qv2_evidence(rpc, token, fee, qty, block):
    data = build_quote_exact_input_single_calldata(token, WETH_BASE, qty, fee)
    raw, req_h, res_h = rpc.call("eth_call", [{"to": QUOTER_V2_BASE, "data": data}, hex(block)])
    amount = int(raw[2:66], 16) if raw and len(raw) >= 66 else 0
    return {"status": "measured", "amount_in": str(qty), "amount_out": str(amount), "request_sha256": req_h, "response_sha256": res_h}


def holder_evidence(rpc, row):
    out = {"status": "not_sampled"}
    if not row["holder_sample"] or not row["entry"]:
        return out
    try:
        receipt, req_h, res_h = rpc.call("eth_getTransactionReceipt", [row["entry"]["tx"]])
        recipient = None
        for log in receipt.get("logs", []):
            topics = [x.lower() for x in log.get("topics", [])]
            if log.get("address", "").lower() == row["token"] and len(topics) >= 3 and topics[0] == TRANSFER_TOPIC and ("0x" + topics[1][-40:]) == row["pool"]:
                recipient = "0x" + topics[2][-40:]
                break
        out = {"status": "receipt_measured", "entry_tx": row["entry"]["tx"], "receipt_request_sha256": req_h, "receipt_response_sha256": res_h, "recipient": recipient}
        if not recipient:
            out["status"] = "recipient_not_found"
            return out
        balance_data = "0x70a08231" + recipient.removeprefix("0x").rjust(64, "0")
        bal_raw, bal_req, bal_res = rpc.call("eth_call", [{"to": row["token"], "data": balance_data}, hex(row["entry"]["block"])])
        balance = int(bal_raw, 16)
        out.update(balance_raw=str(balance), balance_request_sha256=bal_req, balance_response_sha256=bal_res)
        if balance <= 0:
            out["status"] = "zero_balance_at_pinned_entry_block"
            return out
        amount = min(balance, int(row["entry"]["position_qty"]))
        transfer_data = "0xa9059cbb" + row["pool"].removeprefix("0x").rjust(64, "0") + f"{amount:064x}"
        tr_raw, tr_req, tr_res = rpc.call("eth_call", [{"from": recipient, "to": row["token"], "data": transfer_data, "gas": hex(2_000_000)}, hex(row["entry"]["block"])])
        ok = tr_raw in ("0x", "0x" + "0" * 63 + "1") or (tr_raw and int(tr_raw, 16) == 1)
        out.update(status="measured", transfer_to_pool_ok=bool(ok), transfer_amount_raw=str(amount), transfer_request_sha256=tr_req, transfer_response_sha256=tr_res)
    except Exception as exc:
        out.update(status="unknown", error=f"{type(exc).__name__}:{str(exc)[:220]}")
    return out


def work(rpc, code, row):
    out = dict(row)
    out["points"] = []
    try:
        raw, req_h, res_h = rpc.call("eth_call", [{"to": row["pool"], "data": "0xddca3f43"}, hex(row["entry_block"])])
        fee = int(raw, 16)
        out.update(fee=fee, fee_request_sha256=req_h, fee_response_sha256=res_h)
    except Exception as exc:
        out.update(status="unknown", error=f"fee:{type(exc).__name__}:{str(exc)[:220]}")
        return out
    for delay in DELAYS:
        block = row["trigger_block"] + delay * 30
        point = {"delay_minutes": delay, "scheduled_block": block}
        try:
            point["block"] = block_evidence(rpc, block)
        except Exception as exc:
            point["block"] = {"number": block, "status": "unknown", "error": f"{type(exc).__name__}:{str(exc)[:180]}"}
        try:
            point["atomic"] = atomic_evidence(rpc, code, row, fee, block)
        except Exception as exc:
            point["atomic"] = {"status": "unknown", "sell_ok": None, "error": f"{type(exc).__name__}:{str(exc)[:220]}"}
        if delay == 30 and row["selected"] and row["entry"]:
            qty = int(row["entry"]["position_qty"])
            point["capacity_qv2"] = {}
            for multiple in (1, 3, 10):
                try:
                    point["capacity_qv2"][str(multiple)] = qv2_evidence(rpc, row["token"], fee, qty * multiple, block)
                except Exception as exc:
                    point["capacity_qv2"][str(multiple)] = {"status": "unknown", "amount_in": str(qty * multiple), "error": f"{type(exc).__name__}:{str(exc)[:200]}"}
        out["points"].append(point)
    out["holder_address_check"] = holder_evidence(rpc, row)
    return out


def main():
    url = os.environ.get("FORK_ARCHIVE_RPC") or os.environ.get("LABEL_RPC_URL")
    if not url:
        raise RuntimeError("FORK_ARCHIVE_RPC required")
    expected = hashlib.sha256(PREREG.read_bytes()).hexdigest()
    frozen = (ROOT / "mvp/diagnostics/base_b8_preregistration_20260714.sha256").read_text().split()[0]
    if expected != frozen:
        raise RuntimeError("preregistration hash mismatch")
    rows = prepare_rows()
    rpc = Rpc(url)
    head, _, head_hash = rpc.call("eth_blockNumber", [])
    code = bytecode()
    result = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(work, rpc, code, row) for row in rows]
        for i, future in enumerate(as_completed(futures), 1):
            result.append(future.result())
            if i % 25 == 0:
                print(f"B8 RPC {i}/{len(rows)} logical={rpc.logical} attempts={rpc.attempts}", flush=True)
    result.sort(key=lambda x: x["pool"])
    summary = {
        "rows": len(result),
        "selected": sum(x["selected"] for x in result),
        "entered_selected": sum(x["selected"] and x["entry"] is not None for x in result),
        "holder_sample": sum(x["holder_sample"] for x in result),
        "atomic_measured": {str(delay): sum(next(p for p in x.get("points", []) if p["delay_minutes"] == delay)["atomic"].get("status") == "measured" for x in result if x.get("points")) for delay in DELAYS},
    }
    payload = {
        "schema_version": 1,
        "research_only": True,
        "posture": "pinned archive read-only calls; no sign/send/broadcast code path",
        "preregistration_sha256": expected,
        "head_at_start": {"number": int(head, 16), "response_sha256": head_hash},
        "rpc": {"logical_calls": rpc.logical, "http_attempts": rpc.attempts, "api_credits": rpc.attempts * 20, "hard_cap_attempts": MAX_ATTEMPTS, "methods": dict(rpc.methods)},
        "summary": summary,
        "rows": result,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"out": str(OUT), "rpc": payload["rpc"], "summary": summary}, indent=2))


if __name__ == "__main__":
    main()
