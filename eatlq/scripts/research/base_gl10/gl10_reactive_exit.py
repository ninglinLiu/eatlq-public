#!/usr/bin/env python3
"""GL10 causal reserve-onset and read-only reactive-exit reconstruction."""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

ROOT = Path("/opt/EATLQ")
sys.path.insert(0, str(ROOT / "scripts/research"))
sys.path.insert(0, str(ROOT / "scripts/research/base_gl10"))
from gl_readonly_rpc import Rpc, WETH, atomic_probe, balance_of, compile_atomic_bytecode  # noqa:E402
from reactive_exit_core import COST, LAGS, OFFSETS, PESS, first_onset, vector_metrics, verdict  # noqa:E402

PREREG = ROOT / "reports/GL10_preregistration_20260716.md"
ERRATUM = ROOT / "reports/GL10_preregistration_erratum_time_order_20260716.md"
LABELS = ROOT / "scripts/research/base_lefttail/_labeled_universe.json"
V2 = ROOT / "mvp/diagnostics/base_gl5v2_Fon644_per_leg_20260716.json"
V3 = ROOT / "mvp/diagnostics/base_gl5v3_Fon644_archive_per_leg_20260716.json"
GL8 = ROOT / "mvp/diagnostics/base_gl8_pit_three_arm_per_leg_20260716.json"
OUT = ROOT / "mvp/diagnostics/base_gl10_reactive_exit_20260716.json"
PER = ROOT / "mvp/diagnostics/base_gl10_reactive_exit_per_leg_20260716.json"
GOLDEN = ROOT / "mvp/diagnostics/base_gl10_golden_reconciliation_20260716.json"
LEDGER = ROOT / "mvp/diagnostics/base_gl10_rpc_full_ledger_20260716.jsonl"
LEDGER_SUM = ROOT / "mvp/diagnostics/base_gl10_rpc_ledger_20260716.json"
PREREG_SHA = "1c2eff5173dda874eb32ffe360abdfada4922fcb30655a021eb2b6168e4bd62b"
ERRATUM_SHA = "6600d839f1b63e9f870be08cd96af24122f20f9fa7032e7879f29b83ace71257"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PAGE = 2000


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def topic_address(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def log_pages(rpc: Rpc, pool: str, start: int, end: int) -> tuple[list[dict], list[dict]]:
    evidence, merged = [], {}
    ptopic = topic_address(pool)
    for lo in range(start, end + 1, PAGE):
        hi = min(end, lo + PAGE - 1)
        for direction, topics in (("out", [TRANSFER, ptopic]), ("in", [TRANSFER, None, ptopic])):
            params = [{"address": WETH, "fromBlock": hex(lo), "toBlock": hex(hi), "topics": topics}]
            raw, qh, rh = rpc.call("eth_getLogs", params)
            evidence.append({"direction": direction, "from_block": lo, "to_block": hi,
                             "request_sha256": qh, "response_sha256": rh, "log_n": len(raw)})
            for log in raw:
                key = (log.get("transactionHash"), log.get("logIndex"))
                merged[key] = log
    return list(merged.values()), evidence


def reconstruct_onset(rpc: Rpc, base: dict) -> dict:
    entry, flip, pool = base["entry_block"], base["flip_block"], base["pool"]
    if not isinstance(entry, int) or not isinstance(flip, int) or flip < entry:
        return {"status": "INVALID_BLOCK_RANGE", "onset_block": None, "evidence_max_block": entry}
    anchor = balance_of(rpc, WETH, pool, entry - 1)
    if not anchor.get("ok") or anchor.get("value_raw") is None:
        return {"status": "ANCHOR_UNRESOLVED", "onset_block": None,
                "entry_block": entry, "flip_block": flip, "anchor": anchor}
    try:
        logs, pages = log_pages(rpc, pool, entry, flip)
    except Exception as exc:
        return {"status": "LOGS_UNRESOLVED", "onset_block": None, "entry_block": entry,
                "flip_block": flip, "error_class": type(exc).__name__}
    net = defaultdict(int)
    ordered = sorted(logs, key=lambda x: (int(x["blockNumber"], 16),
                                          int(x.get("transactionIndex", "0x0"), 16),
                                          int(x.get("logIndex", "0x0"), 16)))
    for log in ordered:
        topics = log.get("topics", [])
        if len(topics) < 3:
            return {"status": "MALFORMED_LOG", "onset_block": None, "pages": pages}
        amount = int(log.get("data", "0x0"), 16)
        from_addr = "0x" + topics[1][-40:].lower()
        to_addr = "0x" + topics[2][-40:].lower()
        block = int(log["blockNumber"], 16)
        if to_addr == pool:
            net[block] += amount
        if from_addr == pool:
            net[block] -= amount
    onset = first_onset(entry, int(anchor["value_raw"]), dict(net))
    onset.update({"entry_block": entry, "flip_block": flip, "anchor_block": entry - 1,
                  "anchor_balance_raw": anchor["value_raw"], "anchor_request_sha256": anchor.get("request_sha256"),
                  "anchor_response_sha256": anchor.get("response_sha256"), "unique_transfer_logs": len(ordered),
                  "log_pages": pages, "log_to_block_max": max((x["to_block"] for x in pages), default=entry - 1),
                  "block_net": {str(k): str(v) for k, v in sorted(net.items())}})
    if onset.get("onset_block") is not None:
        check = balance_of(rpc, WETH, pool, int(onset["onset_block"]))
        expected = int(onset["balance_after_raw"])
        onset["onset_balance_check"] = check
        if not check.get("ok") or check.get("value_raw") is None:
            onset["status"] = "ONSET_BALANCE_UNRESOLVED"
            onset["onset_block"] = None
        elif int(check["value_raw"]) != expected:
            onset["status"] = "ONSET_BALANCE_MISMATCH"
            onset["onset_block"] = None
    return onset


def frozen_qty(v3: dict, v2: dict) -> tuple[int | None, str]:
    for off in ("1", "5", "15"):
        q = (((v3.get("arm1") or {}).get(off) or {}).get("archive") or {}).get("exact_size_quote") or {}
        if q.get("amount_in_token_raw") is not None:
            return int(q["amount_in_token_raw"]), "GL5V3_EXACT_QUOTE"
    for off in ("1", "5", "15"):
        q = ((v2.get("arm1") or {}).get(off) or {}).get("position_qty_raw")
        if q is not None:
            return int(q), "GL5V2_FROZEN_POSITION"
    return None, "UNRESOLVED"


def entry_spend(v2: dict) -> int | None:
    for off in ("1", "5", "15"):
        x = (((v2.get("arm1") or {}).get(off) or {}).get("entry_proxy") or {}).get("weth_raw")
        if x is not None:
            return int(x)
    return None


def reactive_probe(rpc: Rpc, bytecode: str, base: dict, onset: dict, lag: int) -> dict:
    block = int(onset["onset_block"]) + lag
    target, spend, fee = base.get("qty_raw"), base.get("entry_spend_raw"), base.get("fee")
    bare = {"lag_blocks": lag, "reaction_block": block, "evidence_max_block": block}
    if target is None or spend is None or fee is None:
        return {**bare, "status": "UNRESOLVED", "reason": "fee_qty_or_spend_missing", "attempts": []}
    current, seen, attempts = int(spend), set(), []
    for idx in range(1, 7):
        current = max(1, min(5 * 10**18, current))
        if current in seen:
            break
        seen.add(current)
        probe = atomic_probe(rpc, bytecode, base["token"], int(fee), current, block)
        got = int(probe.get("received_target_raw") or 0)
        ratio = got / int(target) if target else None
        attempts.append({"attempt": idx, "spend_raw": str(current), "qty_ratio": ratio, "probe": probe})
        if not probe.get("ok"):
            err = str(probe.get("error_class", ""))
            if "rpc_revert" in err:
                return {**bare, "status": "NO_EXIT", "reason": "rpc_revert", "attempts": attempts}
            return {**bare, "status": "UNRESOLVED", "reason": err or "rpc_unresolved", "attempts": attempts}
        if not probe.get("buy_ok") or got <= 0:
            return {**bare, "status": "NO_EXIT", "reason": "buy_failed", "attempts": attempts}
        if 0.98 <= ratio <= 1.02:
            recovered = int(probe.get("recovered_weth_raw") or 0)
            if not probe.get("sell_ok"):
                return {**bare, "status": "NO_EXIT", "reason": "sell_failed", "attempts": attempts,
                        "qty_ratio": ratio}
            if recovered <= 0:
                return {**bare, "status": "NO_EXIT", "reason": "zero_recovery", "attempts": attempts,
                        "qty_ratio": ratio}
            recovery = recovered / int(spend)
            ret = max(PESS, recovery - 1.0 - COST)
            return {**bare, "status": "ESCAPABLE", "reason": "exact_qty_positive_recovery",
                    "attempts": attempts, "qty_ratio": ratio, "recovered_weth_raw": str(recovered),
                    "recovery_ratio": recovery, "reactive_ret": ret}
        nxt = round(current * int(target) / got)
        if nxt == current:
            break
        current = nxt
    return {**bare, "status": "UNRESOLVED", "reason": "qty_not_converged", "attempts": attempts}


def baseline_cell(row: dict, off: int) -> dict:
    cell = dict(row["economics"][str(off)])
    return cell


def apply_overlay(gl8row: dict, v2row: dict, off: int, reaction: dict | None) -> tuple[dict, str]:
    cell = baseline_cell(gl8row, off)
    if not reaction or reaction.get("status") != "ESCAPABLE":
        return cell, "BASELINE_REACTION_NOT_SUCCESSFUL"
    target_block = int(cell["target_block"])
    reaction_block = int(reaction["reaction_block"])
    if reaction_block > target_block:
        return cell, "BASELINE_TARGET_EARLIER"
    old = ((v2row.get("arm1") or {}).get(str(off)) or {})
    if cell.get("branch") == "REAL_SELL_CARRY_FORWARD":
        real_block = ((old.get("first_real_sell") or {}).get("block"))
        if real_block is None or int(real_block) <= reaction_block:
            return cell, "REAL_SELL_EARLIER_OR_UNTIMED"
    return {"branch": "GL10_REACTIVE_EXIT", "resolved": True, "ret": float(reaction["reactive_ret"]),
            "optimistic_qv2_ret": float(reaction["reactive_ret"]), "target_block": reaction_block,
            "evidence_source": "GL10_ARCHIVE_ATOMIC_HISTORICAL_QTY"}, "REACTIVE_EXIT_EARLIER"


def main() -> None:
    assert sha(PREREG) == PREREG_SHA and sha(ERRATUM) == ERRATUM_SHA
    labels = json.loads(LABELS.read_text())
    v2rows = json.loads(V2.read_text())["rows"]
    v3rows = json.loads(V3.read_text())["rows"]
    gl8rows = json.loads(GL8.read_text())["rows"]
    v2by, v3by, gl8by = ({x["pool"].lower(): x for x in rows} for rows in (v2rows, v3rows, gl8rows))
    rugs = {x["pool"].lower(): x for x in labels["rug_pools"]}
    assert set(rugs) <= set(v2by) and set(rugs) <= set(v3by)
    l0 = [x for x in gl8rows if x["common_gate_pass"]]

    bases = []
    for pool, lab in sorted(rugs.items()):
        v3, v2 = v3by[pool], v2by[pool]
        qty, source = frozen_qty(v3, v2)
        bases.append({"pool": pool, "token": v3["token"].lower(), "entry_block": int(v3["entry_block"]),
                      "trigger_block": int(v3["trigger_block"]), "flip_block": int(lab["flip_block"]),
                      "fee": v3.get("fee_evidence", {}).get("fee"), "qty_raw": qty,
                      "qty_source": source, "entry_spend_raw": entry_spend(v2),
                      "mechanism": lab.get("mechanism"), "label": lab.get("label")})

    rpc = Rpc(LEDGER, attempt_cap=60000, user_agent="EATLQ-GL10-readonly/1")
    onsets = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        fut = {ex.submit(reconstruct_onset, rpc, base): base["pool"] for base in bases}
        for idx, done in enumerate(as_completed(fut), 1):
            onsets[fut[done]] = done.result()
            if idx % 20 == 0:
                print(f"GL10 onset {idx}/{len(bases)}", flush=True)

    bytecode = compile_atomic_bytecode()
    reactions = {x["pool"]: {} for x in bases}
    jobs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for base in bases:
            onset = onsets[base["pool"]]
            if onset.get("onset_block") is None:
                for lag in LAGS:
                    reactions[base["pool"]][str(lag)] = {
                        "lag_blocks": lag, "reaction_block": None, "evidence_max_block": onset.get("evidence_max_block"),
                        "status": "UNRESOLVED", "reason": onset["status"], "attempts": []}
                continue
            for lag in LAGS:
                jobs.append((base["pool"], lag, ex.submit(reactive_probe, rpc, bytecode, base, onset, lag)))
        for idx, (pool, lag, done) in enumerate(jobs, 1):
            reactions[pool][str(lag)] = done.result()
            if idx % 50 == 0:
                print(f"GL10 probes {idx}/{len(jobs)}", flush=True)

    # Decision evidence is complete before joining frozen economics into overlays.
    per_rows = []
    for base in bases:
        per_rows.append({**base, "onset": onsets[base["pool"]], "reactions": reactions[base["pool"]]})
    bypool = {x["pool"]: x for x in per_rows}

    baseline_metrics, lag_rows = {}, []
    l0_rugs = [x for x in l0 if x["pool"].lower() in rugs]
    for off in OFFSETS:
        baseline_metrics[str(off)] = vector_metrics([baseline_cell(x, off) for x in l0])
    for lag in LAGS:
        status = Counter(x["reactions"][str(lag)]["status"] for x in per_rows)
        reason = Counter(x["reactions"][str(lag)]["reason"] for x in per_rows)
        econ = {}
        overlay_counts = Counter()
        for off in OFFSETS:
            cells = []
            for row in l0:
                pool = row["pool"].lower()
                if pool in bypool:
                    cell, why = apply_overlay(row, v2by[pool], off, bypool[pool]["reactions"][str(lag)])
                    overlay_counts[f"{off}:{why}"] += 1
                else:
                    cell = baseline_cell(row, off)
                cells.append(cell)
            econ[str(off)] = vector_metrics(cells)
        baseline_rug_lower, reactive_rug_lower = [], []
        for row in l0_rugs:
            pool = row["pool"].lower()
            old = baseline_cell(row, 1)
            new, _ = apply_overlay(row, v2by[pool], 1, bypool[pool]["reactions"][str(lag)])
            baseline_rug_lower.append(float(old["ret"]) if old.get("resolved") else PESS)
            reactive_rug_lower.append(float(new["ret"]) if new.get("resolved") else PESS)
        base_med = float(np.median(baseline_rug_lower)) if baseline_rug_lower else None
        new_med = float(np.median(reactive_rug_lower)) if reactive_rug_lower else None
        lag_rows.append({"lag_blocks": lag, "escapable_n": status["ESCAPABLE"],
                         "revert_n": status["NO_EXIT"], "no_exit_n": status["NO_EXIT"],
                         "revert_definition": "atomic guard failed: buy/sell revert or zero recovery; see reason_counts",
                         "unresolved_n": status["UNRESOLVED"],
                         "evaluable_n": status["ESCAPABLE"] + status["NO_EXIT"],
                         "status_counts": dict(sorted(status.items())), "reason_counts": dict(sorted(reason.items())),
                         "escapable_rate_evaluable": status["ESCAPABLE"] / (status["ESCAPABLE"] + status["NO_EXIT"])
                         if status["ESCAPABLE"] + status["NO_EXIT"] else None,
                         "rug_baseline_median_lower": base_med, "rug_reactive_median_lower": new_med,
                         "rug_median_improvement_lower": new_med - base_med if base_med is not None else None,
                         "economics": econ, "overlay_counts": dict(sorted(overlay_counts.items()))})

    final_verdict, eligible = verdict(lag_rows, baseline_metrics["1"])
    best = None
    eligible_rows = [x for x in lag_rows if x["lag_blocks"] in eligible]
    if eligible_rows:
        best = sorted(eligible_rows, key=lambda x: (-x["economics"]["1"]["pooled_mean_interval"][0],
                                                   -x["rug_median_improvement_lower"],
                                                   -x["escapable_rate_evaluable"], x["lag_blocks"]))[0]["lag_blocks"]

    golden_pools = [p for _, p in sorted((hashlib.sha256(f"GL10-golden|{p}".encode()).hexdigest(), p)
                                         for p in rugs)[:5]]
    golden_rows = [{"rank_sha256": hashlib.sha256(f"GL10-golden|{p}".encode()).hexdigest(),
                    "pool": p, "token": bypool[p]["token"], "entry_block": bypool[p]["entry_block"],
                    "flip_block": bypool[p]["flip_block"], "onset": bypool[p]["onset"],
                    "reactions": bypool[p]["reactions"]} for p in golden_pools]
    GOLDEN.write_text(json.dumps({"schema_version": 1, "task_id": "GL10-golden",
                                  "selection_rule": "sha256(GL10-golden|pool)", "rows": golden_rows,
                                  "go_decision": "NOT_MADE"}, indent=2, sort_keys=True) + "\n")

    rpc_summary = rpc.summary()
    LEDGER_SUM.write_text(json.dumps(rpc_summary, indent=2, sort_keys=True) + "\n")
    PER.write_text(json.dumps({"schema_version": 1, "task_id": "GL10-per-leg",
                               "preregistration_sha256": PREREG_SHA, "erratum_sha256": ERRATUM_SHA,
                               "decision_then_economics_join": True, "rows": per_rows,
                               "go_decision": "NOT_MADE"}, indent=2, sort_keys=True) + "\n")
    result = {"schema_version": 1, "task_id": "GL10", "research_only": True,
              "preregistration_sha256": PREREG_SHA, "erratum_sha256": ERRATUM_SHA,
              "step0": {"labeled_rug_n": len(rugs), "L0_n": len(l0), "L0_rug_n": len(l0_rugs),
                        "decision_then_economics_join": True, "model_fit_executed": False},
              "constants": {"lags": list(LAGS), "offsets": list(OFFSETS), "single_drop": .30,
                            "rolling_3_drop": .50, "qty_tolerance": [.98, 1.02], "qty_attempts": 6,
                            "spend_cap_wei": str(5 * 10**18), "PESS": PESS, "COST": COST,
                            "powered_n": 30, "median_improvement": .20},
              "onset_status_counts": dict(sorted(Counter(x["onset"]["status"] for x in per_rows).items())),
              "qty_source_counts": dict(sorted(Counter(x["qty_source"] for x in per_rows).items())),
              "baseline_economics": baseline_metrics, "lags": lag_rows,
              "eligible_lags": eligible, "best_eligible_lag": best, "verdict_candidate": final_verdict,
              "golden": {"path": str(GOLDEN.relative_to(ROOT)), "n": len(golden_rows)},
              "rpc": rpc_summary,
              "source_hashes": {str(p.relative_to(ROOT)): sha(p) for p in (PREREG, ERRATUM, LABELS, V2, V3, GL8)},
              "wallet_level_boundary": "generic atomic replay does not prove exact strategy-wallet execution",
              "go_decision": "NOT_MADE"}
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"step0": result["step0"], "onset": result["onset_status_counts"],
                      "lags": [{k: x[k] for k in ("lag_blocks", "escapable_n", "no_exit_n", "unresolved_n",
                                                          "rug_median_improvement_lower")} for x in lag_rows],
                      "verdict": final_verdict, "rpc": rpc_summary}, indent=2), flush=True)


if __name__ == "__main__":
    main()
