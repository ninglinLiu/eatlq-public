#!/usr/bin/env python3
"""GL8 retrospective PIT-F three-arm hindsight quantification.

Research-only.  Remote operations are restricted by gl_readonly_rpc.Rpc.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path("/opt/EATLQ")
sys.path.insert(0, str(ROOT / "scripts/research"))
sys.path.insert(0, str(ROOT / "scripts/research/base_lefttail"))
sys.path.insert(0, str(ROOT / "scripts/lib"))
from gl_readonly_rpc import Rpc, atomic_probe, balance_of, compile_atomic_bytecode, pool_fee, WETH  # noqa: E402
from v3_quoterv2_struct import QUOTER_V2_BASE, build_quote_exact_input_single_calldata  # noqa: E402
import live_config_rebacktest as L  # noqa: E402

PREREG = ROOT / "reports/GL8_preregistration_20260716.md"
PREREG_SHA = "70aca8dfe74e803efdfa928106a51c3cf09292932423a09823b4269243298b5b"
ERRATUM = ROOT / "reports/GL8_preregistration_erratum_fold_coverage_20260716.md"
ERRATUM_SHA = "a6f38c6319d768f6cf42061368fab1a7df6de2435f3a71550cf8aa15b2bfa6b3"
CHAIN_ERRATUM = ROOT / "reports/GL8_preregistration_erratum_chain_order_20260716.md"
CHAIN_ERRATUM_SHA = "8595938efe32aabeeaa68482980ce10e2202b251669deb6dd86e5451c1b42de0"
V3 = ROOT / "mvp/diagnostics/base_gl5v3_Fon644_archive_per_leg_20260716.json"
V2 = ROOT / "mvp/diagnostics/base_gl5v2_Fon644_per_leg_20260716.json"
LABELS = ROOT / "scripts/research/base_lefttail/_labeled_universe.json"
CREATORS = ROOT / "mvp/audits/funding_cluster_poc_20260624/deployer_funding_graph.json"
TOKEN_SKEL = ROOT / "scripts/research/base_lefttail/_token_skeleton_644.json"
DEPLOYERS = ROOT / "services/executor/fixtures/safe_rug_deployers.json"
SKELETONS = ROOT / "scripts/research/base_lefttail/_rug_seed_validated.json"
OUT = ROOT / "mvp/diagnostics/base_gl8_pit_three_arm_20260716.json"
PER = ROOT / "mvp/diagnostics/base_gl8_pit_three_arm_per_leg_20260716.json"
KNOWN = ROOT / "mvp/diagnostics/base_gl8_known_bad_at_20260716.json"
GOLDEN = ROOT / "mvp/diagnostics/base_gl8_golden_reconciliation_20260716.json"
LEDGER = ROOT / "mvp/diagnostics/base_gl8_rpc_full_ledger_20260716.jsonl"
LEDGER_SUM = ROOT / "mvp/diagnostics/base_gl8_rpc_ledger_20260716.json"

OFFSETS = (1, 5, 15)
ARMS = ("NoF", "PITF", "OracleF")
ARM_INDEX = {"NoF": 0, "PITF": 1, "OracleF": 2}
SPEND = 30_200_000_000_000_000
SIZE_USD = 50.09425
GAS_USD = 0.15
HAIRCUT = 0.02
PESS = -1.0 - GAS_USD / SIZE_USD - HAIRCUT
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PARA_2 = {
    "0x715c98cedbd0c242e257986535bbf6e1fba764be",
    "0xc8512b5324402be9ab986d011c9adef8992f076f",
}
FOLDS = (
    ("F01", "2025-10-01T00:00:00+00:00", "2025-11-01T00:00:00+00:00"),
    ("F02", "2025-11-01T00:00:00+00:00", "2025-12-01T00:00:00+00:00"),
    ("F03", "2025-12-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    ("F04", "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"),
    ("F05", "2026-02-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00"),
    ("F06", "2026-03-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00"),
    ("F07", "2026-04-01T00:00:00+00:00", "2026-05-01T00:00:00+00:00"),
    ("F08", "2026-05-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"),
    ("F09", "2026-06-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc(value: str) -> datetime:
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def fold_for(value: str) -> dict | None:
    t = utc(value)
    for fid, a, b in FOLDS:
        lo, hi = utc(a), utc(b)
        if lo <= t < hi:
            return {"id": fid, "start": a, "end_exclusive": b}
    return None


def exact_quote(rpc: Rpc, token: str, fee: int, qty: int, block: int) -> dict:
    data = build_quote_exact_input_single_calldata(token, WETH, qty, fee)
    try:
        raw, qh, rh = rpc.call("eth_call", [{"to": QUOTER_V2_BASE, "data": data}, hex(block)])
        value = int(raw[2:66], 16) if raw and len(raw) >= 66 else 0
        return {"ok": value > 0, "amount_in_token_raw": str(qty), "amount_out_weth_raw": str(value),
                "request_sha256": qh, "response_sha256": rh,
                "error_class": None if value > 0 else "zero_output"}
    except Exception as exc:
        return {"ok": False, "amount_in_token_raw": str(qty), "amount_out_weth_raw": None,
                "error_class": str(exc)}


def block_evidence(rpc: Rpc, block: int) -> dict:
    try:
        raw, qh, rh = rpc.call("eth_getBlockByNumber", [hex(block), False])
        ts = int(raw["timestamp"], 16)
        return {"ok": True, "number": int(raw["number"], 16), "hash": raw.get("hash"),
                "timestamp": ts, "utc": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                "request_sha256": qh, "response_sha256": rh}
    except Exception as exc:
        return {"ok": False, "error_class": str(exc)}


def transfer_evidence(rpc: Rpc, pool: str, block: int) -> dict:
    params = [{"address": WETH, "fromBlock": hex(block), "toBlock": hex(block), "topics": [TRANSFER_TOPIC]}]
    try:
        logs, qh, rh = rpc.call("eth_getLogs", params)
        needle = pool.lower().removeprefix("0x").rjust(64, "0")
        matches = []
        for x in logs or []:
            topics = [str(t).lower().removeprefix("0x") for t in x.get("topics", [])]
            if needle in topics[1:3]:
                matches.append(x)
        matches.sort(key=lambda x: (int(x.get("transactionIndex", "0x0"), 16),
                                    int(x.get("logIndex", "0x0"), 16), x.get("transactionHash", "")))
        pick = matches[0] if matches else None
        return {"ok": True, "matching_log_n": len(matches),
                "evidence_tx": pick.get("transactionHash") if pick else None,
                "evidence_log_index": int(pick["logIndex"], 16) if pick else None,
                "reason": None if pick else "NO_MATCHING_WETH_TRANSFER_LOG",
                "request_sha256": qh, "response_sha256": rh}
    except Exception as exc:
        return {"ok": False, "matching_log_n": None, "evidence_tx": None, "error_class": str(exc)}


def qty_for(v3row: dict, v2row: dict, off: int) -> tuple[int, str]:
    q = (((v3row["arm1"][str(off)].get("archive") or {}).get("exact_size_quote") or {})
         .get("amount_in_token_raw"))
    if q and int(q) > 0:
        return int(q), "GL5V3_EXACT_QUOTE"
    q = (v2row["arm1"][str(off)] or {}).get("position_qty_raw")
    if q and int(q) > 0:
        return int(q), "GL5V2_FROZEN_POSITION"
    return 0, "UNRESOLVED"


def pool_identity(row: dict, creators: dict, token_skel: dict) -> dict:
    token = row["token"].lower()
    dep = str((creators.get(token) or {}).get("creator") or "").lower() or None
    skel = token_skel.get(token)
    return {"deployer": dep, "skeleton": skel,
            "family": f"deployer:{dep}" if dep else (f"skeleton:{skel}" if skel else "identity:unresolved")}


def certify_rug(rpc: Rpc, bytecode: str, rug: dict, v3row: dict, v2row: dict) -> dict:
    pool, token = rug["pool"].lower(), rug["token"].lower()
    flip, entry_block = int(rug["flip_block"]), int(v3row["entry_block"])
    entry = balance_of(rpc, WETH, pool, entry_block)
    points = {str(b): balance_of(rpc, WETH, pool, b) for b in (flip, flip + 1, flip + 5, flip + 15)}
    fee = pool_fee(rpc, pool, entry_block)
    qty, qsrc = qty_for(v3row, v2row, 1)
    atomic = atomic_probe(rpc, bytecode, token, fee["fee"], SPEND, flip) if fee.get("ok") else {"ok": False, "error_class": "fee_missing"}
    quote = exact_quote(rpc, token, fee["fee"], qty, flip) if fee.get("ok") and qty > 0 else {"ok": False, "error_class": "fee_or_qty_missing"}
    block = block_evidence(rpc, flip)
    logs = transfer_evidence(rpc, pool, flip)
    den = int(entry.get("value_raw") or 0)
    ratios = {k: (int(v["value_raw"]) / den if den > 0 and v.get("ok") else None) for k, v in points.items()}
    reserve_ok = den > 0 and all(x is not None and x <= .05 for x in ratios.values())
    no_recovery = atomic.get("ok") and not atomic.get("sell_ok") and not quote.get("ok")
    certified = bool(reserve_ok and no_recovery and block.get("ok") and qty > 0)
    return {"pool": pool, "token": token, "flip_block": flip, "entry_block": entry_block,
            "status": "CERTIFIED_COLLAPSE" if certified else "UNRESOLVED_COLLAPSE_EVIDENCE",
            "certified": certified, "entry_weth": entry, "reserve_points": points,
            "reserve_ratios": ratios, "reserve_rule_pass": reserve_ok, "fee_evidence": fee,
            "qty_raw": str(qty), "qty_source": qsrc, "atomic": atomic, "exact_size_quote": quote,
            "no_positive_recovery": no_recovery, "block_evidence": block, "transfer_evidence": logs}


def refresh_unresolved_leg(rpc: Rpc, bytecode: str, v3row: dict, v2row: dict, off: int) -> dict:
    old = v3row["arm1"][str(off)]
    qty, qsrc = qty_for(v3row, v2row, off)
    if old.get("resolved"):
        return {"branch": old["branch"], "resolved": True, "ret": old["ret"],
                "optimistic_qv2_ret": old.get("optimistic_qv2_ret"), "qty_raw": str(qty),
                "qty_source": qsrc, "evidence_source": "GL5V3_REUSE", "target_block": old["target_block"]}
    pool, token = v3row["pool"], v3row["token"]
    target, entry_block = int(old["target_block"]), int(v3row["entry_block"])
    entry = balance_of(rpc, WETH, pool, entry_block)
    point = balance_of(rpc, WETH, pool, target)
    lag = balance_of(rpc, WETH, pool, target + 15)
    fee = pool_fee(rpc, pool, entry_block)
    atomic = atomic_probe(rpc, bytecode, token, fee["fee"], SPEND, target) if fee.get("ok") else {"ok": False, "error_class": "fee_missing"}
    quote = exact_quote(rpc, token, fee["fee"], qty, target) if fee.get("ok") and qty > 0 else {"ok": False, "error_class": "fee_or_qty_missing"}
    den = int(entry.get("value_raw") or 0)
    rr = int(point["value_raw"]) / den if den > 0 and point.get("ok") else None
    lr = int(lag["value_raw"]) / den if den > 0 and lag.get("ok") else None
    atomic_sell = bool(atomic.get("ok") and atomic.get("sell_ok"))
    quote_ok = bool(quote.get("ok") and int(quote.get("amount_out_weth_raw") or 0) > 0)
    evidence = {"entry_weth": entry, "target_weth": point, "target_plus_15_weth": lag,
                "target_ratio": rr, "target_plus_15_ratio": lr, "fee": fee,
                "atomic": atomic, "exact_size_quote": quote}
    if rr is not None and atomic_sell and quote_ok:
        ret = int(quote["amount_out_weth_raw"]) / SPEND - 1 - GAS_USD / SIZE_USD - HAIRCUT
        return {"branch": "GL8_ARCHIVE_ALIVE_ATOMIC_OK", "resolved": True, "ret": ret,
                "optimistic_qv2_ret": old.get("optimistic_qv2_ret"), "qty_raw": str(qty),
                "qty_source": qsrc, "evidence_source": "GL8_RPC_REFRESH", "target_block": target,
                "archive": evidence}
    if rr is not None and lr is not None and rr <= .05 and lr <= .05 and not atomic_sell and not quote_ok:
        return {"branch": "GL8_CONFIRMED_COLLAPSE", "resolved": True, "ret": PESS,
                "optimistic_qv2_ret": old.get("optimistic_qv2_ret"), "qty_raw": str(qty),
                "qty_source": qsrc, "evidence_source": "GL8_RPC_REFRESH", "target_block": target,
                "archive": evidence}
    return {"branch": "UNRESOLVED", "resolved": False, "ret": None,
            "optimistic_qv2_ret": old.get("optimistic_qv2_ret"), "qty_raw": str(qty),
            "qty_source": qsrc, "evidence_source": "GL8_RPC_REFRESH", "target_block": target,
            "archive": evidence}


def bootstrap(lower: list[float], arm: str, off: int) -> dict:
    if not lower:
        return {"paths": 0, "terminal_p5": None, "mdd_p95": None}
    rng = np.random.default_rng(20260716 + ARM_INDEX[arm] * 100 + off)
    arr = np.asarray(lower, dtype=float)
    sample = rng.choice(arr, size=(10_000, len(arr)), replace=True)
    eq = 100.0 + np.cumsum(sample, axis=1)
    peaks = np.maximum.accumulate(np.column_stack([np.full(10_000, 100.0), eq]), axis=1)[:, 1:]
    dd = np.clip((peaks - eq) / np.maximum(peaks, 1e-12), 0, 1)
    return {"paths": 10_000, "terminal_p5": float(np.quantile(eq[:, -1], .05)),
            "mdd_p95": float(np.quantile(dd.max(axis=1), .95))}


def arm_metrics(rows: list[dict], arm: str, off: int) -> dict:
    cells = [r["economics"][str(off)] for r in rows]
    lower = [float(c["ret"]) if c["resolved"] else PESS for c in cells]
    upper = [float(c["ret"]) if c["resolved"] else float(c.get("optimistic_qv2_ret") or 0.0) for c in cells]
    resolved = [float(c["ret"]) for c in cells if c["resolved"]]
    pos = np.maximum(np.asarray(lower), 0) if lower else np.asarray([])
    top1 = float(pos.max() / pos.sum()) if pos.size and pos.sum() > 0 else 0.0
    if len(lower) > 1:
        top_removed = list(lower); top_removed.pop(int(np.argmax(top_removed)))
        top_removed_mean = float(np.mean(top_removed))
    else:
        top_removed_mean = None
    branches = Counter(c["branch"] for c in cells)
    return {"selected_n": len(rows), "actual_real_sell_n": sum(c["branch"].startswith("REAL_SELL") for c in cells),
            "resolved_n": len(resolved), "unresolved_n": len(cells) - len(resolved),
            "population_mean_interval": [float(np.mean(lower)) if lower else None, float(np.mean(upper)) if upper else None],
            "median_interval": [float(np.median(lower)) if lower else None, float(np.median(upper)) if upper else None],
            "win_rate_interval": [float(np.mean(np.asarray(lower) > 0)) if lower else None,
                                  float(np.mean(np.asarray(upper) > 0)) if upper else None],
            "branches": dict(sorted(branches.items())),
            "confirmed_collapse_n": sum("CONFIRMED_COLLAPSE" in c["branch"] for c in cells),
            "confirmed_collapse_rate_interval": [sum("CONFIRMED_COLLAPSE" in c["branch"] for c in cells) / len(cells) if cells else None,
                                                   (sum("CONFIRMED_COLLAPSE" in c["branch"] for c in cells) + sum(not c["resolved"] for c in cells)) / len(cells) if cells else None],
            "top1_positive_contribution": top1, "top1_removed_lower_mean": top_removed_mean,
            "bootstrap_lower": bootstrap(lower, arm, off), "lower_vector_sha256": hashlib.sha256(json.dumps(lower, separators=(",", ":")).encode()).hexdigest()}


def add_alpha(metrics: dict) -> None:
    for scope in ("pooled", "folds"):
        if scope == "pooled":
            groups = [("pooled", metrics["pooled"])]
        else:
            groups = list(metrics["folds"].items())
        for _, group in groups:
            for off in map(str, OFFSETS):
                nof = group["NoF"][off]["population_mean_interval"]
                for arm in ARMS:
                    cur = group[arm][off]["population_mean_interval"]
                    if arm == "NoF":
                        alpha = [0.0, 0.0] if cur[0] is not None else [None, None]
                    elif cur[0] is None or nof[0] is None:
                        alpha = [None, None]
                    else:
                        alpha = [cur[0] - nof[1], cur[1] - nof[0]]
                    group[arm][off]["conditional_alpha_vs_NoF"] = alpha


def main() -> None:
    assert sha(PREREG) == PREREG_SHA
    assert sha(ERRATUM) == ERRATUM_SHA
    assert sha(CHAIN_ERRATUM) == CHAIN_ERRATUM_SHA
    v3 = json.loads(V3.read_text()); v2 = json.loads(V2.read_text())
    assert len(v3["rows"]) == len(v2["rows"]) == 644
    v3by = {r["pool"].lower(): r for r in v3["rows"]}; v2by = {r["pool"].lower(): r for r in v2["rows"]}
    assert set(v3by) == set(v2by)

    oracle_deps = {x.lower() for x in json.loads(DEPLOYERS.read_text())["deployers"]} | PARA_2
    oracle_skels = set(json.loads(SKELETONS.read_text())["safe_skeletons"])
    assert len(oracle_deps) == 247 and len(oracle_skels) == 33
    creators = json.loads(CREATORS.read_text())["token_creators"]
    token_skel = json.loads(TOKEN_SKEL.read_text())

    base = L.load_base_df(); q = L.run_lens(base, "qv2")
    deps0, sk0, creators0, token_skel0 = L._load_blacklists()
    masks = L.gate_masks(q, deps0, sk0, creators0, token_skel0)
    nof_mask = masks["model_depth"] & q.gate_fric & q.tradeable
    oracle_mask = masks["live_full"] & q.gate_fric & q.tradeable
    nof_set = set(q.loc[nof_mask, "pool"].str.lower())
    oracle_set = set(q.loc[oracle_mask, "pool"].str.lower())
    frozen_oracle = {r["pool"].lower() for r in v3["rows"] if r["selected_Fon"]}
    assert oracle_set == frozen_oracle and len(oracle_set) == 120
    assert nof_set <= set(v3by)

    rpc = Rpc(LEDGER, attempt_cap=60000, user_agent="EATLQ-GL8-readonly/1")
    bytecode = compile_atomic_bytecode()
    rug_rows = json.loads(LABELS.read_text())["rug_pools"]
    rug_rows = [r for r in rug_rows if r["pool"].lower() in v3by and isinstance(r.get("flip_block"), int)]
    certified = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = [ex.submit(certify_rug, rpc, bytecode, r, v3by[r["pool"].lower()], v2by[r["pool"].lower()]) for r in rug_rows]
        for f in as_completed(futures):
            certified.append(f.result())
    certified.sort(key=lambda x: (x["flip_block"], x["pool"]))

    cert_by_identity: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in certified:
        ident = pool_identity(c, creators, token_skel)
        c["identity"] = ident
        if c["certified"]:
            if ident["deployer"] in oracle_deps:
                cert_by_identity[("deployer", ident["deployer"])].append(c)
            if ident["skeleton"] in oracle_skels:
                cert_by_identity[("skeleton", ident["skeleton"])].append(c)

    identities = []
    for typ, vals in (("deployer", sorted(oracle_deps)), ("skeleton", sorted(oracle_skels))):
        for value in vals:
            xs = sorted(cert_by_identity.get((typ, value), []), key=lambda x: (x["flip_block"], x["pool"]))
            x = xs[0] if xs else None
            source = ("safe_rug_deployers.json" if typ == "deployer" and value not in PARA_2 else
                      "live_config_rebacktest.PARADAENGI_DEPLOYERS" if typ == "deployer" else "_rug_seed_validated.json")
            identities.append({"identity_type": typ, "identity": value, "source": source,
                               "status": "CERTIFIED" if x else "IDENTITY_UNRESOLVED",
                               "known_bad_block": x["flip_block"] if x else None,
                               "known_bad_time": x["block_evidence"].get("utc") if x else None,
                               "evidence_pool": x["pool"] if x else None, "evidence_token": x["token"] if x else None,
                               "evidence_tx": x["transfer_evidence"].get("evidence_tx") if x else None,
                               "certified_pool_n": len(xs), "evidence": x if x else None})
    known_map = {(x["identity_type"], x["identity"]): x for x in identities}
    KNOWN.write_text(json.dumps({"schema_version": 1, "task_id": "GL8-known-bad-at",
                                 "preregistration_sha256": PREREG_SHA, "erratum_sha256": ERRATUM_SHA,
                                 "chain_order_erratum_sha256": CHAIN_ERRATUM_SHA,
                                 "oracle_identity_counts": {"deployer": len(oracle_deps), "skeleton": len(oracle_skels)},
                                 "certified_rug_pool_n": sum(x["certified"] for x in certified),
                                 "rug_pool_evidence": certified, "identities": identities,
                                 "go_decision": "NOT_MADE"}, indent=2, sort_keys=True) + "\n")

    # Refresh only unresolved GL5v3 legs for the full NoF-selected population.
    economics: dict[str, dict] = {}
    jobs = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for pool in sorted(nof_set):
            economics[pool] = {}
            for off in OFFSETS:
                old = v3by[pool]["arm1"][str(off)]
                if old.get("resolved"):
                    economics[pool][str(off)] = refresh_unresolved_leg(rpc, bytecode, v3by[pool], v2by[pool], off)
                else:
                    jobs.append((pool, off, ex.submit(refresh_unresolved_leg, rpc, bytecode, v3by[pool], v2by[pool], off)))
        for pool, off, f in jobs:
            economics[pool][str(off)] = f.result()

    fold_start_blocks = {}
    for fid, _, _ in FOLDS:
        blocks = [int(r["entry_block"]) for r in v3["rows"]
                  if (fold_for(r["signal_time"]) or {}).get("id") == fid]
        fold_start_blocks[fid] = min(blocks) if blocks else None

    per_rows = []
    outside = 0
    for pool in sorted(v3by, key=lambda p: (v3by[p]["signal_time"], p)):
        row = v3by[pool]; fold = fold_for(row["signal_time"])
        if fold is None:
            outside += 1
        ident = pool_identity(row, creators, token_skel)
        common = pool in nof_set
        dep_oracle = ident["deployer"] in oracle_deps
        sk_oracle = ident["skeleton"] in oracle_skels
        active = []
        if fold:
            fstart_block = fold_start_blocks[fold["id"]]
            for typ, val in (("deployer", ident["deployer"]), ("skeleton", ident["skeleton"])):
                rec = known_map.get((typ, val))
                if rec and rec["status"] == "CERTIFIED" and fstart_block is not None and int(rec["known_bad_block"]) < fstart_block:
                    active.append({"identity_type": typ, "identity": val,
                                   "known_bad_block": rec["known_bad_block"], "known_bad_time": rec["known_bad_time"],
                                   "fold_start_block": fstart_block, "fold_start": fold["start"],
                                   "strict_before": True})
            assert fstart_block is None or fstart_block <= int(row["entry_block"])
            assert all(int(h["known_bad_block"]) < int(h["fold_start_block"]) <= int(row["entry_block"]) for h in active)
        selections = {"NoF": common, "PITF": common and not active,
                      "OracleF": common and not (dep_oracle or sk_oracle)}
        if common:
            econ = economics[pool]
        else:
            econ = {str(o): {"branch": "NOT_COMMON_GATE", "resolved": False, "ret": None,
                             "optimistic_qv2_ret": None, "qty_raw": "0", "qty_source": "NOT_APPLICABLE",
                             "evidence_source": "NOT_APPLICABLE", "target_block": v3by[pool]["arm1"][str(o)]["target_block"]} for o in OFFSETS}
        per_rows.append({"pool": pool, "token": row["token"], "signal_time": row["signal_time"],
                         "entry_block": row["entry_block"], "fold": fold, "common_gate_pass": common,
                         "identity": ident, "oracle_hits": {"deployer": dep_oracle, "skeleton": sk_oracle},
                         "pit_active_hits": active, "selections": selections, "economics": econ})
    assert outside == 0
    assert {r["pool"] for r in per_rows if r["selections"]["OracleF"]} == oracle_set

    metrics = {"folds": {}, "pooled": {}}
    for fid, a, b in FOLDS:
        metrics["folds"][fid] = {arm: {} for arm in ARMS}
        in_fold = [r for r in per_rows if r["fold"]["id"] == fid]
        for arm in ARMS:
            selected_rows = [r for r in in_fold if r["selections"][arm]]
            for off in OFFSETS:
                metrics["folds"][fid][arm][str(off)] = arm_metrics(selected_rows, arm, off)
    for arm in ARMS:
        metrics["pooled"][arm] = {}
        selected_rows = [r for r in per_rows if r["selections"][arm]]
        for off in OFFSETS:
            metrics["pooled"][arm][str(off)] = arm_metrics(selected_rows, arm, off)
    add_alpha(metrics)

    shares = {}
    for off in map(str, OFFSETS):
        pit = metrics["pooled"]["PITF"][off]["population_mean_interval"][0]
        oracle = metrics["pooled"]["OracleF"][off]["population_mean_interval"][0]
        shares[off] = {"PITF_edge_lower": pit, "OracleF_edge_lower": oracle,
                       "hindsight_share": (oracle - pit) / oracle if oracle is not None and oracle > 0 else None,
                       "denominator_status": "POSITIVE" if oracle is not None and oracle > 0 else "NONPOSITIVE_UNDEFINED"}
    primary = metrics["pooled"]["PITF"]["1"]
    powered_n = primary["resolved_n"]
    fold_floor = []
    for fid, _, _ in FOLDS:
        x = metrics["folds"][fid]["PITF"]["1"]
        if x["selected_n"]:
            fold_floor.append({"fold": fid, "selected_n": x["selected_n"],
                               "lower_mean": x["population_mean_interval"][0],
                               "pass": x["population_mean_interval"][0] >= -.10})
    hs = shares["1"]["hindsight_share"]
    supported = (primary["population_mean_interval"][0] > 0 and all(x["pass"] for x in fold_floor)
                 and primary["top1_removed_lower_mean"] is not None and primary["top1_removed_lower_mean"] > 0
                 and shares["1"]["OracleF_edge_lower"] > 0 and hs is not None and hs < .70)
    if powered_n < 30:
        verdict = "INSUFFICIENT_N"
    elif supported:
        verdict = "FORWARD_EDGE_SUPPORTED"
    else:
        verdict = "MOSTLY_HINDSIGHT"

    clean = [{"pool": r["pool"], "fold": r["fold"]["id"], "branch": r["economics"]["1"]["branch"],
              "ret": r["economics"]["1"]["ret"], "qty_source": r["economics"]["1"]["qty_source"]}
             for r in per_rows if r["selections"]["PITF"] and r["economics"]["1"]["resolved"]]
    gold_options = []
    for x in identities:
        if x["status"] == "CERTIFIED":
            rank = hashlib.sha256(f"GL8-golden|{x['identity_type']}|{x['identity']}".encode()).hexdigest()
            gold_options.append((rank, x))
    picks = []
    for rank, x in sorted(gold_options)[:5]:
        epool = x["evidence_pool"]
        affected = [r for r in per_rows if (r["identity"].get(x["identity_type"]) == x["identity"])]
        picks.append({"rank_sha256": rank, "identity_type": x["identity_type"], "identity": x["identity"],
                      "known_bad_block": x["known_bad_block"], "known_bad_time": x["known_bad_time"],
                      "evidence_pool": epool, "evidence_tx": x["evidence_tx"],
                      "affected_pool_n": len(affected),
                      "fold_decisions": [{"pool": r["pool"], "fold": r["fold"]["id"],
                                          "pit_active": any(h["identity_type"] == x["identity_type"] and h["identity"] == x["identity"] for h in r["pit_active_hits"]),
                                          "PITF_selected": r["selections"]["PITF"], "OracleF_selected": r["selections"]["OracleF"]} for r in affected]})
    GOLDEN.write_text(json.dumps({"schema_version": 1, "task_id": "GL8-golden", "selection_rule": "sha256(GL8-golden|type|identity)",
                                  "expected_n": 5, "actual_n": len(picks), "category_shortfall": len(picks) < 5,
                                  "rows": picks, "go_decision": "NOT_MADE"}, indent=2, sort_keys=True) + "\n")

    rpc_summary = rpc.summary(); LEDGER_SUM.write_text(json.dumps(rpc_summary, indent=2, sort_keys=True) + "\n")
    result = {"schema_version": 1, "task_id": "GL8", "preregistration_sha256": PREREG_SHA,
              "fold_coverage_erratum_sha256": ERRATUM_SHA,
              "chain_order_erratum_sha256": CHAIN_ERRATUM_SHA, "research_only": True,
              "step0": {"labeled_n": len(v3by), "common_NoF_n": len(nof_set), "OracleF_n": len(oracle_set),
                        "oracle_deployer_n": len(oracle_deps), "oracle_skeleton_n": len(oracle_skels),
                        "outside_fold_n": outside, "fold_n": len(FOLDS)},
              "known_bad_summary": {"rug_candidate_n": len(certified), "certified_rug_pool_n": sum(x["certified"] for x in certified),
                                    "certified_deployer_n": sum(x["identity_type"] == "deployer" and x["status"] == "CERTIFIED" for x in identities),
                                    "certified_skeleton_n": sum(x["identity_type"] == "skeleton" and x["status"] == "CERTIFIED" for x in identities)},
              "folds": [{"id": x[0], "start": x[1], "end_exclusive": x[2],
                         "fold_start_block": fold_start_blocks[x[0]]} for x in FOLDS],
              "metrics": metrics, "hindsight_share": shares,
              "powered": {"threshold": 30, "PITF_clean_resolved_plus1_n": powered_n, "pass": powered_n >= 30},
              "fold_floor_plus1": fold_floor,
              "verdict_candidate": verdict,
              "verdict_inputs": {"PITF_plus1_lower": primary["population_mean_interval"][0],
                                 "PITF_plus1_top1_removed_lower_mean": primary["top1_removed_lower_mean"],
                                 "hindsight_share_plus1": hs, "all_nonempty_fold_floor_pass": all(x["pass"] for x in fold_floor)},
              "clean_PITF_plus1": clean, "golden": {"path": str(GOLDEN.relative_to(ROOT)), "n": len(picks)},
              "source_hashes": {str(p.relative_to(ROOT)): sha(p) for p in (PREREG, ERRATUM, CHAIN_ERRATUM, V3, V2, LABELS, CREATORS, TOKEN_SKEL, DEPLOYERS, SKELETONS)},
              "rpc": rpc_summary, "wallet_level_boundary": "GL7v2 exact strategy-wallet sellability remains unresolved",
              "go_decision": "NOT_MADE"}
    PER.write_text(json.dumps({"schema_version": 1, "task_id": "GL8-per-leg", "preregistration_sha256": PREREG_SHA,
                               "fold_coverage_erratum_sha256": ERRATUM_SHA,
                               "chain_order_erratum_sha256": CHAIN_ERRATUM_SHA, "rows": per_rows,
                               "go_decision": "NOT_MADE"}, indent=2, sort_keys=True) + "\n")
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"step0": result["step0"], "known_bad": result["known_bad_summary"],
                      "pooled": metrics["pooled"], "hindsight_share": shares,
                      "powered": result["powered"], "verdict": verdict, "rpc": rpc_summary}, indent=2))


if __name__ == "__main__":
    main()
