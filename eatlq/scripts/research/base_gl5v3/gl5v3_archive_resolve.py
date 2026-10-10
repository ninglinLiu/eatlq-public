#!/usr/bin/env python3
"""GL5v3 archive-only resolution of the frozen GL5v2 644/120 F-on object.

Research only: the sole remote operations are allow-listed JSON-RPC reads.  The
archive URL is parsed from the task spec in memory and is never serialized.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np

ROOT = Path("/opt/EATLQ")
sys.path.insert(0, str(ROOT / "scripts/lib"))
from v3_quoterv2_struct import (  # noqa: E402
    QUOTER_V2_BASE, WETH_BASE, build_quote_exact_input_single_calldata,
)

SPEC = ROOT / "reports/GL5v3_task_spec_Fon644_archive_resolve_20260716.md"
PREREG = ROOT / "reports/GL5v3_preregistration_20260716.md"
IN_SUM = ROOT / "mvp/diagnostics/base_gl5v2_Fon644_repricing_20260716.json"
IN_LEGS = ROOT / "mvp/diagnostics/base_gl5v2_Fon644_per_leg_20260716.json"
OUT_SUM = ROOT / "mvp/diagnostics/base_gl5v3_Fon644_archive_resolve_20260716.json"
OUT_LEGS = ROOT / "mvp/diagnostics/base_gl5v3_Fon644_archive_per_leg_20260716.json"
LEDGER = ROOT / "mvp/diagnostics/base_gl5v3_rpc_full_ledger_20260716.jsonl"
LEDGER_SUM = ROOT / "mvp/diagnostics/base_gl5v3_rpc_ledger_summary_20260716.json"

PREREG_SHA = "da489d3f3f4ad8b40ac48849661116264ac1a6e98f5a9835b9cdc60f07882412"
GL5V2_PREREG_SHA = "c3d411bc07509df4e791b2094cb9145cdb43d19626633fdcb3bedb645bef970e"
OFFSETS = (1, 5, 15)
SPEND = 30_200_000_000_000_000
SIZE_USD = 50.09425
GAS_USD = 0.15
HAIRCUT = 0.02
PESS = -1.0 - GAS_USD / SIZE_USD - HAIRCUT
FROM = "0x000000000000000000000000000000000000b503"
FEE_SELECTOR = "0xddca3f43"
BALANCE_OF = "70a08231"
ALLOWED = {"eth_call", "eth_getBalance", "eth_getBlockByNumber", "eth_getLogs"}
MAX_HTTP_ATTEMPTS = 40000
SEED = 20260716


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def extract_rpc_url() -> str:
    match = re.search(r'export GL5V3_ARCHIVE_RPC="([^"]+)"', SPEC.read_text())
    if not match:
        raise RuntimeError("GL5V3 archive endpoint is absent from task spec")
    return match.group(1)


class Rpc:
    def __init__(self, url: str):
        self.url = url
        self.lock = threading.Lock()
        self.req_id = 0
        self.attempts = 0
        self.logical_calls = 0
        self.methods = Counter()
        LEDGER.unlink(missing_ok=True)

    def _record(self, rec: dict) -> None:
        with self.lock:
            with LEDGER.open("a") as fh:
                fh.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")

    def call(self, method: str, params: list, timeout: int = 120):
        if method not in ALLOWED:
            raise RuntimeError(f"forbidden RPC method: {method}")
        p_hash = canonical_hash(params)
        with self.lock:
            self.logical_calls += 1
            self.methods[method] += 1
            self.req_id += 1
            ident = self.req_id
        last_class = "unknown"
        for attempt in range(1, 4):
            with self.lock:
                self.attempts += 1
                attempt_no = self.attempts
                if attempt_no > MAX_HTTP_ATTEMPTS:
                    raise RuntimeError("pre-registered HTTP attempt cap exceeded")
            body = json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}).encode()
            rec = {
                "attempt_global": attempt_no,
                "attempt_for_call": attempt,
                "method": method,
                "params_sha256": p_hash,
                "utc": datetime.now(timezone.utc).isoformat(),
            }
            try:
                req = Request(self.url, data=body, headers={"content-type": "application/json", "user-agent": "EATLQ-GL5v3-readonly/1"})
                with urlopen(req, timeout=timeout) as response:
                    payload = json.load(response)
                if payload.get("error") is not None:
                    msg = str(payload["error"]).lower()
                    if "archive" in msg or "missing trie" in msg or "historical state" in msg:
                        last_class = "archive_unavailable"
                    elif "rate" in msg or "limit" in msg or "429" in msg:
                        last_class = "rate_limited"
                    elif "revert" in msg:
                        last_class = "rpc_revert"
                    else:
                        last_class = "rpc_error"
                    rec.update(success=False, error_class=last_class, response_sha256=canonical_hash(payload["error"]))
                    self._record(rec)
                    if last_class == "rpc_revert":
                        raise RpcRevert(last_class)
                    raise RuntimeError(last_class)
                result = payload.get("result")
                rec.update(success=True, error_class=None, response_sha256=canonical_hash(result))
                self._record(rec)
                return result, p_hash, rec["response_sha256"]
            except RpcRevert:
                raise
            except Exception as exc:
                if last_class == "unknown":
                    name = type(exc).__name__.lower()
                    last_class = "timeout" if "timeout" in name else "transport_error"
                    rec.update(success=False, error_class=last_class, response_sha256=None)
                    self._record(rec)
                if attempt < 3:
                    time.sleep(0.25 * attempt)
        raise RuntimeError(last_class)


class RpcRevert(RuntimeError):
    pass


def compile_creation_bytecode() -> str:
    out = Path("/tmp/b4forgeout")
    art = out / "AtomicRoundTrip.sol/AtomicRoundTripCreateB4.json"
    if not art.exists():
        subprocess.run([
            str(Path("/root/.foundry/bin/forge")), "build", "--contracts",
            "scripts/research/base_b4/AtomicRoundTrip.sol", "--out", str(out),
            "--cache-path", "/tmp/b4forgecache",
        ], cwd=ROOT, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return json.loads(art.read_text())["bytecode"]["object"].removeprefix("0x")


def atomic_calldata(bytecode: str, token: str, fee: int) -> str:
    return "0x" + bytecode + token.lower().removeprefix("0x").rjust(64, "0") + f"{fee:064x}" + f"{SPEND:064x}"


def decode_atomic(raw: str) -> dict:
    if not raw or len(raw) < 2 + 16 * 64:
        raise ValueError("short atomic return")
    words = [int(raw[2 + i * 64:2 + (i + 1) * 64], 16) for i in range(16)]
    keys = ["flags", "router_buy_out", "target_before", "target_after_buy", "target_after_sell",
            "router_sell_out", "weth_after_deposit", "weth_after_buy", "weth_after_sell", "pin_block",
            "chain_id", "parent_block_hash", "weth_approve_error_hash", "buy_error_hash",
            "target_approve_error_hash", "sell_error_hash"]
    return dict(zip(keys, words))


def balance_of(rpc: Rpc, token: str, owner: str, block: int) -> dict:
    data = "0x" + BALANCE_OF + owner.lower().removeprefix("0x").rjust(64, "0")
    try:
        raw, qh, rh = rpc.call("eth_call", [{"to": token, "data": data}, hex(block)])
        return {"ok": True, "value_raw": str(int(raw, 16)), "request_sha256": qh, "response_sha256": rh}
    except Exception as exc:
        return {"ok": False, "value_raw": None, "error_class": type(exc).__name__}


def pool_fee(rpc: Rpc, pool: str, block: int) -> dict:
    try:
        raw, qh, rh = rpc.call("eth_call", [{"to": pool, "data": FEE_SELECTOR}, hex(block)])
        return {"ok": True, "fee": int(raw, 16), "request_sha256": qh, "response_sha256": rh}
    except Exception as exc:
        return {"ok": False, "fee": None, "error_class": type(exc).__name__}


def atomic_probe(rpc: Rpc, bytecode: str, token: str, fee: int, block: int) -> dict:
    tx = {"from": FROM, "data": atomic_calldata(bytecode, token, fee), "value": hex(SPEND), "gas": hex(20_000_000)}
    try:
        raw, qh, rh = rpc.call("eth_call", [tx, hex(block), {FROM: {"balance": hex(10**20)}}])
        a = decode_atomic(raw)
        received = a["target_after_buy"] - a["target_before"]
        recovered = a["weth_after_sell"] - a["weth_after_buy"] if a["weth_after_sell"] > a["weth_after_buy"] else 0
        buy_ok = bool(a["flags"] & 2) and received > 0
        sell_ok = bool(a["flags"] & 8) and recovered > 0
        return {"ok": True, "buy_ok": buy_ok, "sell_ok": sell_ok, "received_target_raw": str(received),
                "recovered_weth_raw": str(recovered), "flags": a["flags"], "pin_block": a["pin_block"],
                "request_sha256": qh, "response_sha256": rh,
                "buy_error_hash": f"0x{a['buy_error_hash']:064x}", "sell_error_hash": f"0x{a['sell_error_hash']:064x}"}
    except Exception as exc:
        return {"ok": False, "buy_ok": False, "sell_ok": False, "error_class": type(exc).__name__}


def exact_quote(rpc: Rpc, token: str, fee: int, qty: int, block: int) -> dict:
    data = build_quote_exact_input_single_calldata(token, WETH_BASE, qty, fee)
    try:
        raw, qh, rh = rpc.call("eth_call", [{"to": QUOTER_V2_BASE, "data": data}, hex(block)])
        out = int(raw[2:66], 16) if raw and len(raw) >= 66 else 0
        return {"ok": out > 0, "amount_in_token_raw": str(qty), "amount_out_weth_raw": str(out),
                "request_sha256": qh, "response_sha256": rh, "error_class": None if out > 0 else "zero_output"}
    except Exception as exc:
        return {"ok": False, "amount_in_token_raw": str(qty), "amount_out_weth_raw": None,
                "error_class": type(exc).__name__}


def ratio(point: dict, entry: dict):
    if not point.get("ok") or not entry.get("ok"):
        return None
    den = int(entry["value_raw"])
    return int(point["value_raw"]) / den if den > 0 else None


def classify(old: dict, entry: dict, grid: dict, atomic: dict | None, quote: dict | None) -> dict:
    target = grid["target"]
    lag = grid["target_plus_15"]
    target_ratio = ratio(target, entry)
    lag_ratio = ratio(lag, entry)
    archive = {"entry_weth": entry, "grid": grid["points"], "target_ratio": target_ratio,
               "target_plus_15_ratio": lag_ratio, "atomic": atomic, "exact_size_quote": quote,
               "collapse_90": target_ratio is not None and target_ratio <= 0.10,
               "collapse_95": target_ratio is not None and target_ratio <= 0.05,
               "collapse_99": target_ratio is not None and target_ratio <= 0.01}
    base = {"target_block": old["target_block"], "old_branch": old["branch"], "archive": archive,
            "optimistic_qv2_ret": old.get("optimistic_qv2_ret"), "pessimistic_ret": PESS}
    if old.get("resolved") and old.get("branch") == "REAL_SELL":
        return {**base, "branch": "REAL_SELL_CARRY_FORWARD", "resolved": True, "ret": old["ret"],
                "resolution_basis": "canonical full-size SELL retained by priority"}
    atomic_sell = bool(atomic and atomic.get("sell_ok"))
    quote_ok = bool(quote and quote.get("ok") and int(quote["amount_out_weth_raw"]) > 0)
    if target_ratio is not None and atomic_sell and quote_ok:
        ret = int(quote["amount_out_weth_raw"]) / SPEND - 1.0 - GAS_USD / SIZE_USD - HAIRCUT
        branch = "ARCHIVE_ALIVE_ATOMIC_OK" if target_ratio > 0.05 else "ARCHIVE_IMPAIRED_BUT_ATOMIC_OK"
        return {**base, "branch": branch, "resolved": True, "ret": ret,
                "gross_recovery_ratio": int(quote["amount_out_weth_raw"]) / SPEND,
                "resolution_basis": "target reserve + atomic sell + historical-qty exact quote"}
    confirmed = (target_ratio is not None and lag_ratio is not None and target_ratio <= 0.05 and lag_ratio <= 0.05
                 and not atomic_sell and not quote_ok)
    if confirmed:
        return {**base, "branch": "CONFIRMED_COLLAPSE", "resolved": True, "ret": PESS,
                "gross_recovery_ratio": 0.0, "resolution_basis": "95% reserve collapse persisted through +15; no positive sell evidence"}
    return {**base, "branch": "UNRESOLVED", "resolved": False, "ret": None,
            "resolution_basis": "archive evidence did not satisfy a preregistered resolved branch"}


def probe_pool(rpc: Rpc, bytecode: str, row: dict) -> dict:
    entry_block = int(row["entry_block"])
    entry = balance_of(rpc, WETH_BASE, row["pool"], entry_block)
    fee = pool_fee(rpc, row["pool"], entry_block)
    out = {k: row[k] for k in ("pool", "token", "entry_block", "trigger_block", "signal_time", "selected_Fon", "label", "mechanism")}
    out["fee_evidence"] = fee
    out["entry_weth_evidence"] = entry
    out["arm0_qv2_ret"] = row.get("arm0_qv2_ret")
    out["arm1"] = {}
    for offset in OFFSETS:
        old = row["arm1"][str(offset)]
        target = int(old["target_block"])
        blocks = sorted(set([target - 1, target, target + 1, target + 5, target + 15]))
        points = {str(b): balance_of(rpc, WETH_BASE, row["pool"], b) for b in blocks}
        grid = {"points": points, "target": points[str(target)], "target_plus_15": points[str(target + 15)]}
        atomic = quote = None
        if not (old.get("resolved") and old.get("branch") == "REAL_SELL") and fee.get("ok"):
            atomic = atomic_probe(rpc, bytecode, row["token"], fee["fee"], target)
            qty = old.get("position_qty_raw")
            if qty is None and old.get("entry_proxy"):
                e = old["entry_proxy"]
                if int(e["weth_raw"]) > 0:
                    qty = str(SPEND * int(e["token_raw"]) // int(e["weth_raw"]))
            if qty is not None and int(qty) > 0:
                quote = exact_quote(rpc, row["token"], fee["fee"], int(qty), target)
        out["arm1"][str(offset)] = classify(old, entry, grid, atomic, quote)
    return out


def basic(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0, "mean": None, "median": None, "win_rate": None, "top1_removed_mean": None, "top1_contribution": None}
    a = np.asarray(vals, dtype=float)
    k = max(1, math.ceil(0.01 * len(a)))
    idx = np.argsort(a)[::-1]
    rem = np.delete(a, idx[:k])
    pos = np.maximum(a, 0)
    return {"n": len(vals), "mean": float(a.mean()), "median": float(np.median(a)), "win_rate": float((a > 0).mean()),
            "top1_removed_mean": float(rem.mean()) if len(rem) else None,
            "top1_contribution": float(pos[idx[:k]].sum() / pos.sum()) if pos.sum() else 0.0}


def population(rows: list[dict], offset: int) -> dict:
    cells = [r["arm1"][str(offset)] for r in rows]
    resolved = [c["ret"] for c in cells if c["resolved"]]
    pess = [c["ret"] if c["resolved"] else PESS for c in cells]
    optimistic = [c["ret"] if c["resolved"] else (c.get("optimistic_qv2_ret") if c.get("optimistic_qv2_ret") is not None else 0.0) for c in cells]
    counts = Counter(c["branch"] for c in cells)
    rug = counts["CONFIRMED_COLLAPSE"]
    unresolved = counts["UNRESOLVED"]
    main = {
        "n": len(cells), "branches": dict(sorted(counts.items())), "resolved_n": len(resolved), "unresolved_n": unresolved,
        "resolved_only": basic(resolved), "population_mean_interval": [float(np.mean(pess)), float(np.mean(optimistic))],
        "population_point_mean": float(np.mean(resolved)) if not unresolved else None,
        "confirmed_collapse_n": rug, "rug_rate_identification_interval": [rug / len(cells), (rug + unresolved) / len(cells)],
        "collapse_sensitivity": {
            "loss_ge_90pct_n": sum(c["archive"]["collapse_90"] is True for c in cells),
            "loss_ge_95pct_n": sum(c["archive"]["collapse_95"] is True for c in cells),
            "loss_ge_99pct_n": sum(c["archive"]["collapse_99"] is True for c in cells),
        },
    }
    return main


def choose_golden(rows: list[dict]) -> dict:
    candidates = []
    for row in rows:
        if not row["selected_Fon"]:
            continue
        for o in OFFSETS:
            c = row["arm1"][str(o)]
            candidates.append((hashlib.sha256(f"GL5v3-golden|{row['pool']}:{o}".encode()).hexdigest(), row, o, c))
    picks = []
    wants = [("CONFIRMED_COLLAPSE", 2), ("ARCHIVE_ALIVE_ATOMIC_OK", 2), ("UNRESOLVED", 1)]
    used = set()
    for branch, n in wants:
        options = sorted(x for x in candidates if x[3]["branch"] == branch and (x[1]["pool"], x[2]) not in used)
        for rank, row, o, c in options[:n]:
            used.add((row["pool"], o))
            a = c["archive"]
            picks.append({"pool": row["pool"], "offset": o, "branch": c["branch"], "rank_sha256": rank,
                          "entry_response_sha256": a["entry_weth"].get("response_sha256"),
                          "target_response_sha256": a["grid"][str(c["target_block"])].get("response_sha256"),
                          "lag15_response_sha256": a["grid"][str(c["target_block"] + 15)].get("response_sha256"),
                          "atomic_response_sha256": (a.get("atomic") or {}).get("response_sha256"),
                          "quote_response_sha256": (a.get("exact_size_quote") or {}).get("response_sha256")})
    if len(picks) < 5:
        for rank, row, o, c in sorted(candidates):
            if (row["pool"], o) in used:
                continue
            used.add((row["pool"], o))
            a = c["archive"]
            picks.append({"pool": row["pool"], "offset": o, "branch": c["branch"], "rank_sha256": rank,
                          "entry_response_sha256": a["entry_weth"].get("response_sha256"),
                          "target_response_sha256": a["grid"][str(c["target_block"])].get("response_sha256"),
                          "atomic_response_sha256": (a.get("atomic") or {}).get("response_sha256"),
                          "quote_response_sha256": (a.get("exact_size_quote") or {}).get("response_sha256")})
            if len(picks) == 5:
                break
    return {"selection": picks, "category_shortfall_disclosed": Counter(x["branch"] for x in picks) != Counter({"CONFIRMED_COLLAPSE": 2, "ARCHIVE_ALIVE_ATOMIC_OK": 2, "UNRESOLVED": 1})}


def main():
    if sha(PREREG) != PREREG_SHA:
        raise RuntimeError("pre-registration drift")
    old_sum = json.loads(IN_SUM.read_text())
    inp = json.loads(IN_LEGS.read_text())["rows"]
    arm0 = old_sum["arm0_reproduction"]
    if not arm0["all_python_equal"] or arm0["lenses"]["qv2"]["live_full_gates"]["mean_ret"] != 1.4280241443411608:
        raise RuntimeError("Arm-0 reproduction anchor drift")
    rpc = Rpc(extract_rpc_url())
    bytecode = compile_creation_bytecode()
    head, _, head_hash = rpc.call("eth_getBlockByNumber", ["latest", False])
    rows = []
    done = 0
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(probe_pool, rpc, bytecode, row): row["pool"] for row in inp}
        for future in as_completed(futures):
            rows.append(future.result())
            done += 1
            if done % 25 == 0 or done == len(inp):
                print(f"archive progress {done}/{len(inp)} pools; attempts={rpc.attempts}", flush=True)
    rows.sort(key=lambda r: (r["signal_time"], r["pool"]))
    selected = [r for r in rows if r["selected_Fon"]]
    selected_sum = {str(o): population(selected, o) for o in OFFSETS}
    baseline_sum = {str(o): population(rows, o) for o in OFFSETS}
    alpha = {}
    for o in OFFSETS:
        s, b = selected_sum[str(o)], baseline_sum[str(o)]
        if s["unresolved_n"] == 0 and b["unresolved_n"] == 0:
            alpha[str(o)] = {"fully_identified": True, "point": s["population_point_mean"] - b["population_point_mean"]}
        else:
            alpha[str(o)] = {"fully_identified": False,
                             "interval": [s["population_mean_interval"][0] - b["population_mean_interval"][1],
                                          s["population_mean_interval"][1] - b["population_mean_interval"][0]]}
    illusion = {}
    for o in OFFSETS:
        pairs = [(r["arm0_qv2_ret"], r["arm1"][str(o)]["ret"]) for r in selected
                 if r["arm0_qv2_ret"] is not None and r["arm1"][str(o)]["resolved"]]
        den = sum(max(0, a0) for a0, _ in pairs)
        washed = sum(max(0, a0) - max(0, a1) for a0, a1 in pairs)
        illusion[str(o)] = {"resolved_pair_n": len(pairs), "positive_profit_illusion_share": washed / den if den else None,
                            "confirmed_quote_washed_rug_n": sum(r["arm1"][str(o)]["branch"] == "CONFIRMED_COLLAPSE" for r in selected)}
    ledger_summary = {"schema_version": 1, "endpoint_id": "GL5V3_ARCHIVE_RPC_secret_omitted", "logical_calls": rpc.logical_calls,
                      "http_attempts": rpc.attempts, "methods": dict(sorted(rpc.methods.items())), "head_number": int(head["number"], 16),
                      "head_response_sha256": head_hash, "ledger_sha256": sha(LEDGER), "allowed_methods": sorted(ALLOWED),
                      "public_fallback_used": False, "send_sign_broadcast_calls": 0}
    LEDGER_SUM.write_text(json.dumps(ledger_summary, indent=2, sort_keys=True) + "\n")
    verdict = "INCONCLUSIVE" if any(selected_sum[str(o)]["unresolved_n"] for o in OFFSETS) else "PENDING_RIGHT_TAIL_REVIEW"
    result = {"schema_version": 1, "task_id": "GL5-v3", "target_object": "2026-06-24_Fon_644_to_120",
              "preregistration_sha256": PREREG_SHA, "inherits_gl5v2_preregistration_sha256": GL5V2_PREREG_SHA,
              "input_hashes": {str(IN_SUM.relative_to(ROOT)): sha(IN_SUM), str(IN_LEGS.relative_to(ROOT)): sha(IN_LEGS)},
              "arm0_reproduction": arm0, "arm1_archive_resolved": selected_sum, "baseline_archive_resolved": baseline_sum,
              "conditional_alpha": alpha, "decomposition": illusion, "golden": choose_golden(rows), "rpc": ledger_summary,
              "wallet_level_sellability": "NOT_ESTABLISHED_POOL_LEVEL_ONLY", "verdict_candidate": verdict,
              "research_only": True, "go_decision": "NOT_MADE"}
    OUT_LEGS.write_text(json.dumps({"schema_version": 1, "task_id": "GL5-v3", "preregistration_sha256": PREREG_SHA,
                                    "wallet_level_sellability": "NOT_ESTABLISHED_POOL_LEVEL_ONLY", "rows": rows,
                                    "go_decision": "NOT_MADE"}, indent=2, sort_keys=True) + "\n")
    OUT_SUM.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"outputs": [str(OUT_SUM.relative_to(ROOT)), str(OUT_LEGS.relative_to(ROOT)), str(LEDGER_SUM.relative_to(ROOT))],
                      "selected": selected_sum, "baseline": baseline_sum, "conditional_alpha": alpha,
                      "rpc": ledger_summary, "verdict_candidate": verdict, "go_decision": "NOT_MADE"}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
