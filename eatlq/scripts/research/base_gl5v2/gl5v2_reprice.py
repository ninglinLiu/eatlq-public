#!/usr/bin/env python3
"""GL5v2 offline repricing for the frozen 2026-06-24 644/120 F-on object.

No RPC client is created here.  Historical canonical swaps are read from local
ClickHouse; no service, Redis, signal, live config, signer, or sender is used.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import math
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np


ROOT = Path("/opt/EATLQ")
LEFT = ROOT / "scripts/research/base_lefttail"
DIAG = ROOT / "mvp/diagnostics"
PREREG = ROOT / "reports/GL5v2_preregistration_20260716.md"
PREREG_SHA = "c3d411bc07509df4e791b2094cb9145cdb43d19626633fdcb3bedb645bef970e"
SOURCE = ROOT / "mvp/audits/live_config_rebacktest_20260624/live_config_rebacktest.json"
SOURCE_SCRIPT = LEFT / "live_config_rebacktest.py"
OUT = DIAG / "base_gl5v2_Fon644_repricing_20260716.json"
PER = DIAG / "base_gl5v2_Fon644_per_leg_20260716.json"

OFFSETS = (1, 5, 15)
SIZE_WEI = 30_200_000_000_000_000
SIZE_ETH = 0.0302
SIZE_USD = 50.09425
GAS_USD_RT = 0.15
HAIRCUT = 0.02
PESS = -1.0 - GAS_USD_RT / SIZE_USD - HAIRCUT
LN1P = math.log(1.0001)
SEED = 20260716


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


def exact_tree(actual, expected, path="") -> list[dict]:
    out = []
    if isinstance(expected, dict):
        for key, value in expected.items():
            out.extend(exact_tree(actual[key], value, f"{path}.{key}" if path else key))
    elif isinstance(expected, list):
        out.append({"path": path, "actual": actual, "expected": expected,
                    "python_equal": actual == expected})
    else:
        err = abs(actual - expected) if isinstance(actual, (int, float)) and isinstance(expected, (int, float)) else None
        out.append({"path": path, "actual": actual, "expected": expected,
                    "python_equal": actual == expected,
                    "absolute_error": err,
                    "within_tolerance": err is None or err <= 1e-15})
    return out


def reproduce_arm0(L):
    source = json.loads(SOURCE.read_text())
    deployers, skeletons, creators, token_skel = L._load_blacklists()
    base = L.load_base_df()
    lenses = {}
    selected = {}
    frames = {}
    assertions = []
    for lens in ("qv2", "fork"):
        df = L.run_lens(base, lens)
        masks = L.gate_masks(df, deployers, skeletons, creators, token_skel)
        fric_ok = df.gate_fric
        live = masks["live_full"] & fric_ok
        got = {
            "live_full_gates": L.metrics(df, live, L.SIZE_USD),
            "gate_breakdown_pass_model": {k: int(v.sum()) for k, v in masks.items()},
            "blocked_by_reason": {
                "depth": int(masks["blocked_depth"].sum()),
                "fric_fail": int((masks["model_depth"] & ~fric_ok).sum()),
                "deployer": int((masks["blocked_deployer"] & fric_ok).sum()),
                "skeleton_only": int((masks["blocked_skeleton"] & fric_ok).sum()),
            },
            "fork_crosstab_on_live_passed": L.fork_crosstab(df, live),
        }
        exp = source["lenses"][lens]
        for key in got:
            assertions.extend(exact_tree(got[key], exp[key], f"{lens}.{key}"))
        rows = df[live].copy().sort_values(["t0", "pool"])
        selected[lens] = set(rows.pool)
        frames[lens] = df
        lenses[lens] = got
    assert selected["qv2"] == selected["fork"]
    assert len(selected["qv2"]) == 120
    assert len(base) == 644
    assert len(deployers) == 247 and len(skeletons) == 33
    arm0_pass = all(x["python_equal"] for x in assertions)
    return base, frames, sorted(selected["qv2"]), lenses, assertions, arm0_pass


def ch_query_swaps(rows: list[dict]) -> tuple[dict[str, list[dict]], dict]:
    clauses = []
    for r in rows:
        lo = int(r["entry_block"])
        hi = int(r["trigger_block"]) + 45
        clauses.append(f"(lower(pool_address)='{r['pool']}' AND block_number BETWEEN {lo} AND {hi})")
    where = " OR ".join(clauses)
    query = f"""SELECT lower(pool_address),block_number,toUnixTimestamp64Milli(block_time),lower(tx_hash),log_index,toString(amount0),toString(amount1),tick
FROM eatlq.uniswap_v3_swaps_decoded FINAL
WHERE chain_id=8453 AND removed=0 AND ({where})
ORDER BY pool_address,block_number,log_index FORMAT TabSeparated"""
    proc = subprocess.run(
        ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client", "--query", query],
        text=True, capture_output=True, timeout=600,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr[-2000:])
    out = defaultdict(list)
    for z in csv.reader(io.StringIO(proc.stdout), delimiter="\t"):
        pool, block, ms, tx, li, a0, a1, tick = z
        out[pool].append({"block": int(block), "ms": int(ms), "tx": tx,
                          "log_index": int(li), "a0": int(a0), "a1": int(a1),
                          "tick": int(tick)})
    canon = "\n".join("\t".join(z) for z in csv.reader(io.StringIO(proc.stdout), delimiter="\t"))
    return out, {
        "rows": sum(len(v) for v in out.values()),
        "pools_with_rows": len(out),
        "canonical_tsv_sha256": hashlib.sha256(canon.encode()).hexdigest(),
        "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
    }


def direction(s: dict, orient: int):
    weth, tok = (s["a0"], s["a1"]) if orient == -1 else (s["a1"], s["a0"])
    if weth > 0 and tok < 0:
        return "buy", weth / -tok, weth, -tok
    if weth < 0 and tok > 0:
        return "sell", -weth / tok, -weth, tok
    return "other", None, abs(weth), abs(tok)


def collapse_at(pre: int | None, post: int | None, threshold: float) -> bool | None:
    if pre is None or post is None or pre <= 0:
        return None
    return post / pre <= (1.0 - threshold) + 1e-15


def priority_branch(real_sell_resolved: bool, collapse_confirmed: bool,
                    survival_confirmed: bool, atomic_quote_available: bool) -> str:
    """Frozen 1 > 2a > 2b > 3 evidence priority, kept pure for regression."""
    if real_sell_resolved:
        return "REAL_SELL"
    if collapse_confirmed:
        return "CONFIRMED_COLLAPSE"
    if survival_confirmed and atomic_quote_available:
        return "SURVIVED_ATOMIC_QUOTE"
    return "UNRESOLVED"


def classify(row: dict, swaps: list[dict], offset: int) -> dict:
    target = int(row["trigger_block"]) + offset
    orient = int(row["orient"])
    entry_hi = min(int(row["entry_block"]) + 900, target)
    entry = None
    for s in swaps:
        if s["block"] < int(row["entry_block"]):
            continue
        if s["block"] > entry_hi:
            break
        side, px, weth, tok = direction(s, orient)
        if side == "buy" and weth > 0 and tok > 0:
            entry = {**s, "price_weth_per_token": px, "weth_raw": str(weth), "token_raw": str(tok)}
            break
    sells = []
    later_sells = []
    for s in swaps:
        side, px, weth, tok = direction(s, orient)
        if side != "sell":
            continue
        rec = {**s, "price_weth_per_token": px, "weth_raw": str(weth), "token_raw": str(tok)}
        if target <= s["block"] <= target + 15:
            sells.append(rec)
        if target <= s["block"] <= target + 30:
            later_sells.append(rec)
    first = sells[0] if sells else None
    old_q = row.get("arm0_qv2_ret")
    old_f = row.get("arm0_fork_ret")
    base = {
        "target_block": target,
        "offset_blocks": offset,
        "entry_proxy": entry,
        "first_real_sell": first,
        "real_sell_count_window": len(sells),
        "real_sell_count_lag30": len(later_sells),
        "reserve_scan": {"status": "INCOMPLETE_NO_FULL_TRANSFER_NET_OR_ARCHIVE_RPC",
                         "collapse_90": None, "collapse_95": None, "collapse_99": None},
        "atomic_at_target": "UNAVAILABLE_NO_INJECTED_ARCHIVE_RPC_AND_NO_FROZEN_OFFSET_QUOTE",
        "optimistic_qv2_ret": old_q,
        "optimistic_fork_ret": old_f,
        "pessimistic_ret": PESS,
    }
    if first and entry:
        position_qty = SIZE_WEI * int(entry["token_raw"]) // int(entry["weth_raw"])
        sell_qty = int(first["token_raw"])
        coverage = sell_qty / position_qty if position_qty else 0.0
        base.update(position_qty_raw=str(position_qty), sell_size_coverage=coverage)
        if coverage >= 1.0:
            gross = first["price_weth_per_token"] / entry["price_weth_per_token"] - 1.0
            ret = gross - GAS_USD_RT / SIZE_USD - HAIRCUT
            base.update(branch=priority_branch(True, False, False, False), resolved=True, gross=gross, ret=ret,
                        evidence_strength="CANONICAL_SWAP_LOG_SIZE_COVERED")
            return base
        base.update(branch="UNRESOLVED_SIZE", resolved=False, ret=None,
                    survival_evidence=True, evidence_strength="REAL_SELL_BELOW_COUNTERFACTUAL_SIZE")
        return base
    if first and not entry:
        base.update(branch="UNRESOLVED_ENTRY_PRICE", resolved=False, ret=None,
                    survival_evidence=True, evidence_strength="REAL_SELL_WITHOUT_ENTRY_PRICE")
        return base
    if later_sells:
        base.update(branch="SURVIVAL_ONLY_UNRESOLVED", resolved=False, ret=None,
                    survival_evidence=True, evidence_strength="LATER_CANONICAL_SELL")
        return base
    base.update(branch=priority_branch(False, False, False, False), resolved=False, ret=None,
                survival_evidence=False, evidence_strength="NO_COMPLETE_RESERVE_OR_TARGET_ATOMIC")
    return base


def basic_summary(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0, "mean": None, "median": None, "win_rate": None,
                "top1_removed_mean": None, "top1_contribution": None}
    a = np.asarray(vals, dtype=float)
    order = np.argsort(a)[::-1]
    k = max(1, math.ceil(0.01 * len(a)))
    rem = np.delete(a, order[:k])
    positive = np.maximum(a, 0)
    return {
        "n": len(vals), "mean": float(a.mean()), "median": float(np.median(a)),
        "win_rate": float((a > 0).mean()),
        "top1_removed_mean": float(rem.mean()) if len(rem) else None,
        "top1_contribution": float(positive[order[:k]].sum() / positive.sum()) if positive.sum() else 0.0,
    }


def rolling(rows: list[dict], offset: int) -> list[dict]:
    times = [datetime.fromisoformat(r["signal_time"]) for r in rows]
    if not times:
        return []
    start, end = min(times), max(times)
    out = []
    i = 0
    while start + timedelta(days=15 * i) <= end:
        lo = start + timedelta(days=15 * i)
        hi = lo + timedelta(days=30)
        vals = [r["arm1"][str(offset)]["ret"] for r in rows
                if lo <= datetime.fromisoformat(r["signal_time"]) < hi
                and r["arm1"][str(offset)]["resolved"]]
        out.append({"id": f"R{i:02}", "start": lo.isoformat(), "end_exclusive": hi.isoformat(),
                    **basic_summary(vals)})
        i += 1
    return out


def monte_carlo(vals: list[float], offset: int) -> dict:
    if not vals:
        return {"paths": 0, "terminal_p5": None, "max_drawdown_p95": None}
    rng = np.random.default_rng(SEED + offset)
    arr = rng.choice(np.asarray(vals), size=(10_000, len(vals)), replace=True)
    eq = 100 + np.cumsum(arr, axis=1)
    peaks = np.maximum.accumulate(np.column_stack([np.full(10_000, 100.0), eq]), axis=1)[:, 1:]
    dd = np.max(peaks - eq, axis=1)
    return {"paths": 10_000, "terminal_p5": float(np.quantile(eq[:, -1], 0.05)),
            "max_drawdown_p95": float(np.quantile(dd, 0.95))}


def finalize_arm(rows: list[dict], offset: int, cohort: str) -> dict:
    cells = [r["arm1"][str(offset)] for r in rows]
    vals = [c["ret"] for c in cells if c["resolved"]]
    oldq = [r["arm0_qv2_ret"] for r in rows if r.get("arm0_qv2_ret") is not None]
    oldf = [r["arm0_fork_ret"] for r in rows if r.get("arm0_fork_ret") is not None]
    fallback_q = float(np.median(oldq)) if oldq else 0.0
    fallback_f = float(np.median(oldf)) if oldf else fallback_q
    pess_vals, opt_q, opt_f = [], [], []
    for r, c in zip(rows, cells):
        if c["resolved"]:
            pess_vals.append(c["ret"]); opt_q.append(c["ret"]); opt_f.append(c["ret"])
        else:
            pess_vals.append(PESS)
            opt_q.append(r["arm0_qv2_ret"] if r.get("arm0_qv2_ret") is not None else fallback_q)
            opt_f.append(r["arm0_fork_ret"] if r.get("arm0_fork_ret") is not None else fallback_f)
    roll = rolling(rows, offset)
    powered = [x for x in roll if x["n"] >= 20]
    s = basic_summary(vals)
    mc = monte_carlo(vals, offset)
    unresolved = len(cells) - len(vals)
    gates = {
        "pooled_mean_gt_0": s["mean"] is not None and s["mean"] > 0,
        "top1_removed_mean_gt_0": s["top1_removed_mean"] is not None and s["top1_removed_mean"] > 0,
        "top1_contribution_le_25pct": s["top1_contribution"] is not None and s["top1_contribution"] <= 0.25,
        "every_powered_rolling_mean_ge_minus_10pct": all(x["mean"] >= -0.10 for x in powered) if powered else None,
        "mc_terminal_p5_gt_100R": mc["terminal_p5"] is not None and mc["terminal_p5"] > 100,
        "mc_mdd_p95_le_20R": mc["max_drawdown_p95"] is not None and mc["max_drawdown_p95"] <= 20,
        "unresolved_eq_0": unresolved == 0,
    }
    status = "INCONCLUSIVE" if unresolved or any(v is None for v in gates.values()) else "PASS" if all(gates.values()) else "FAIL"
    return {
        "cohort": cohort, "offset_blocks": offset, "point_resolved_only": s,
        "unresolved": unresolved,
        "population_mean_interval_qv2": [float(np.mean(pess_vals)), float(np.mean(opt_q))],
        "population_mean_interval_fork": [float(np.mean(pess_vals)), float(np.mean(opt_f))],
        "branches": dict(sorted(Counter(c["branch"] for c in cells).items())),
        "survival_evidence_n": sum(bool(c.get("survival_evidence")) or c["branch"] == "REAL_SELL" for c in cells),
        "confirmed_collapse_n": sum(c["branch"] == "CONFIRMED_COLLAPSE" for c in cells),
        "rolling": roll, "monte_carlo": mc, "gates": gates, "status": status,
    }


def main():
    assert sha(PREREG) == PREREG_SHA
    L = load_module(SOURCE_SCRIPT, "gl5v2_original_live_config")
    base, frames, selected_pools, arm0_lenses, arm0_assertions, arm0_pass = reproduce_arm0(L)
    if not arm0_pass:
        raise SystemExit("STOP_ARM0_REPRODUCTION_DRIFT")

    priced = json.loads((LEFT / "_priced_live.json").read_text())
    realgas = json.loads((LEFT / "_realgas_replay.json").read_text())
    labels = L._load_labels()
    qframe = frames["qv2"].set_index("pool")
    fframe = frames["fork"].set_index("pool")
    rows = []
    for r in base.sort_values(["t0", "pool"]).itertuples(index=False):
        p = r.pool.lower()
        pr = priced.get(p) or {}
        rg = realgas.get(p) or {}
        trigger = int(pr["exb"]) if pr.get("exb") is not None else int(r.entry_block) + 7200
        rows.append({
            "pool": p, "token": r.token.lower(), "orient": int(r.orient),
            "signal_time": r.t0.isoformat(), "entry_block": int(r.entry_block),
            "trigger_block": trigger, "trigger_reason": pr.get("exwhy") or "fallback_7200",
            "selected_Fon": p in set(selected_pools), "label": r.label,
            "mechanism": r.mechanism if isinstance(r.mechanism, str) else None,
            "flip_offset": None if np.isnan(r.flip_offset) else int(r.flip_offset),
            "arm0_qv2_ret": float(qframe.loc[p, "ret"]) if bool(qframe.loc[p, "tradeable"]) else None,
            "arm0_fork_ret": float(fframe.loc[p, "ret"]) if bool(fframe.loc[p, "tradeable"]) else None,
            "arm0_source_evidence_qv2": "UNGUARDED_QV2_QUOTE" if bool(qframe.loc[p, "tradeable"]) else "NO_QV2_RETURN",
            "arm0_source_evidence_fork": "FORK_SIMULATION" if bool(fframe.loc[p, "tradeable"]) else "NO_FORK_RETURN",
            "frozen_fork": {"status": rg.get("status"), "entry_sell": rg.get("entry_sell"),
                            "exit_sell": rg.get("exit_sell"), "exit_block": rg.get("exit_block")},
        })
    swaps, ch_meta = ch_query_swaps(rows)
    for r in rows:
        r["arm1"] = {str(o): classify(r, swaps.get(r["pool"], []), o) for o in OFFSETS}

    selected_rows = [r for r in rows if r["selected_Fon"]]
    assert len(rows) == 644 and len(selected_rows) == 120
    arm1 = {str(o): finalize_arm(selected_rows, o, "Fon_selected_120") for o in OFFSETS}
    baseline = {str(o): finalize_arm(rows, o, "labeled_universe_644") for o in OFFSETS}
    for o in OFFSETS:
        arm1[str(o)]["resolved_alpha_vs_baseline"] = (
            None if arm1[str(o)]["point_resolved_only"]["mean"] is None or baseline[str(o)]["point_resolved_only"]["mean"] is None
            else arm1[str(o)]["point_resolved_only"]["mean"] - baseline[str(o)]["point_resolved_only"]["mean"]
        )

    source_evidence = {
        "qv2_lens": dict(Counter(r["arm0_source_evidence_qv2"] for r in selected_rows)),
        "fork_lens": dict(Counter(r["arm0_source_evidence_fork"] for r in selected_rows)),
        "true_receipt_sell_used_by_original": 0,
        "atomic_guarded_quote_used_by_original": 0,
        "confirmed_rug_used_by_original": 0,
        "unresolved_retained_by_original": 0,
        "fork_crosstab_n": arm0_lenses["qv2"]["fork_crosstab_on_live_passed"]["n"],
        "interpretation": "Each lens has 120 trades; do not sum them to 240. QV2 is 100% unguarded quote, fork is 100% simulation, and neither is a real receipt sell.",
    }

    decomp = {}
    for o in OFFSETS:
        cells = [(r, r["arm1"][str(o)]) for r in selected_rows]
        resolved_pairs = [(r, c) for r, c in cells if c["resolved"] and r.get("arm0_qv2_ret") is not None]
        den = sum(max(0.0, r["arm0_qv2_ret"]) for r, _ in resolved_pairs)
        illusion = sum(max(0.0, r["arm0_qv2_ret"]) - max(0.0, c["ret"]) for r, c in resolved_pairs)
        unresolved_no_survival = sum(not c["resolved"] and not c.get("survival_evidence") for _, c in cells)
        confirmed = sum(c["branch"] == "CONFIRMED_COLLAPSE" for _, c in cells)
        decomp[str(o)] = {
            "original_quote_or_simulation_dependency": 1.0,
            "resolved_pair_n": len(resolved_pairs),
            "positive_profit_illusion_share_resolved_pairs": None if den == 0 else illusion / den,
            "confirmed_collapse_n": confirmed,
            "survival_evidence_n": sum(c["resolved"] or c.get("survival_evidence") for _, c in cells),
            "rug_relevant_unresolved_no_survival": unresolved_no_survival,
            "Fon_rug_rate_interval": [confirmed / 120, (confirmed + unresolved_no_survival) / 120],
            "old_label_delayed_rug_in_selected": sum(r["label"] == "delayed_rug" for r in selected_rows),
            "aggressive_external_contrast_rug_entries": 207,
        }

    formal_statuses = [arm1[str(o)]["status"] for o in OFFSETS]
    illusion_values = [decomp[str(o)]["positive_profit_illusion_share_resolved_pairs"] for o in OFFSETS]
    if all(x == "PASS" for x in formal_statuses) and all(x is not None and x <= 0.10 for x in illusion_values):
        verdict = "EDGE_SURVIVES"
    elif any(arm1[str(o)]["point_resolved_only"]["mean"] is not None and arm1[str(o)]["point_resolved_only"]["mean"] <= 0 for o in OFFSETS) or any(x is not None and x >= 0.50 for x in illusion_values):
        verdict = "EDGE_COLLAPSES"
    else:
        verdict = "INCONCLUSIVE"

    golden_candidates = []
    for r in selected_rows:
        for o in OFFSETS:
            c = r["arm1"][str(o)]
            rank = hashlib.sha256(f"GL5v2-golden|{r['pool']}:{o}".encode()).hexdigest()
            golden_candidates.append((rank, r, o, c))
    golden = []
    for rank, r, o, c in sorted(golden_candidates):
        if len(golden) == 5:
            break
        golden.append({"pool": r["pool"], "offset": o, "rank_sha256": rank,
                       "branch": c["branch"], "entry_proxy": c["entry_proxy"],
                       "first_real_sell": c["first_real_sell"],
                       "frozen_fork": r["frozen_fork"],
                       "rpc_status": "RPC_NOT_RUN_ENV_UNSET"})

    result = {
        "schema_version": 1, "task_id": "GL5-v2", "research_only": True,
        "go_decision": "NOT_MADE",
        "decision_flags": {"training_allowed": False, "live_allowed": False,
                           "broadcast_allowed": False},
        "preregistration_sha256": PREREG_SHA,
        "target_object": "2026-06-24_Fon_644_to_120",
        "arm0_reproduction": {"status": "PASS", "lenses": arm0_lenses,
                              "assertion_count": len(arm0_assertions),
                              "all_python_equal": all(x["python_equal"] for x in arm0_assertions),
                              "assertions": arm0_assertions},
        "arm0_source_evidence_composition": source_evidence,
        "arm1": arm1, "baseline": baseline, "decomposition": decomp,
        "verdict_candidate": verdict,
        "data_coverage": {"clickhouse_swaps": ch_meta,
                          "archive_rpc": "NOT_RUN_ENV_UNSET", "new_rpc_calls": 0,
                          "reserve_scan_limitation": "No complete transfer-net/balanceOf interval for all 644; no collapse or survival is inferred from missing data."},
        "golden": {"selection": golden, "rpc_status": "RPC_NOT_RUN_ENV_UNSET",
                   "category_shortfall_disclosed": True},
        "source_hashes": {str(p.relative_to(ROOT)): sha(p) for p in (
            PREREG, SOURCE, SOURCE_SCRIPT, LEFT / "_priced_live.json",
            LEFT / "_realgas_replay.json", LEFT / "_entry_weth_644.json",
            LEFT / "_token_skeleton_644.json", LEFT / "_rug_seed_validated.json",
            ROOT / "services/executor/fixtures/safe_rug_deployers.json",
            ROOT / "mvp/audits/funding_cluster_poc_20260624/deployer_funding_graph.json",
            LEFT / "_pools.tsv", LEFT / "_fwdpath.tsv")},
    }
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    PER.write_text(json.dumps({"schema_version": 1, "task_id": "GL5-v2",
                               "preregistration_sha256": PREREG_SHA,
                               "go_decision": "NOT_MADE", "rows": rows},
                              indent=2, sort_keys=True) + "\n")
    print(json.dumps({"arm0_pass": arm0_pass, "selected": len(selected_rows),
                      "universe": len(rows), "arm1": arm1, "verdict": verdict,
                      "new_rpc_calls": 0}, indent=2, default=str))


if __name__ == "__main__":
    main()
