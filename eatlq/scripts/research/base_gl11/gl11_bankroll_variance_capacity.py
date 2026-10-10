#!/usr/bin/env python3
"""GL11 offline bankroll/variance/capacity audit with a GL10-L1 arm."""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path("/opt/EATLQ")
sys.path.insert(0, str(ROOT / "scripts/research/base_gl11"))
from bankroll_core import (B0, CANDIDATES, PESS, bootstrap_indices, concurrency_filter,
                           contiguous_months, empirical_metrics, kelly_grid, month_groups,
                           n8_interpretation, parse_utc, simulate, viability_label)  # noqa:E402

PREREG = ROOT / "reports/GL11_preregistration_20260716.md"
ERRATUM = ROOT / "reports/GL11_preregistration_erratum_capacity_unresolved_20260716.md"
PHASE_ERRATUM = ROOT / "reports/GL11_preregistration_erratum_month_block_phase_20260716.md"
NOTIONAL_ERRATUM = ROOT / "reports/GL11_preregistration_erratum_entry_notional_scale_20260716.md"
GL8 = ROOT / "mvp/diagnostics/base_gl8_pit_three_arm_per_leg_20260716.json"
V3 = ROOT / "mvp/diagnostics/base_gl5v3_Fon644_archive_per_leg_20260716.json"
V2 = ROOT / "mvp/diagnostics/base_gl5v2_Fon644_per_leg_20260716.json"
GL10 = ROOT / "mvp/diagnostics/base_gl10_reactive_exit_20260716.json"
GL10PER = ROOT / "mvp/diagnostics/base_gl10_reactive_exit_per_leg_20260716.json"
LABELS = ROOT / "scripts/research/base_lefttail/_labeled_universe.json"
OUT = ROOT / "mvp/diagnostics/base_gl11_bankroll_variance_capacity_20260716.json"
PER = ROOT / "mvp/diagnostics/base_gl11_bankroll_variance_capacity_per_leg_20260716.json"
LEDGER = ROOT / "mvp/diagnostics/base_gl11_rpc_full_ledger_20260716.jsonl"
LEDGER_SUM = ROOT / "mvp/diagnostics/base_gl11_rpc_ledger_20260716.json"
PREREG_SHA = "0ae0a33443c8ac66a36504ded6d4920a1fe5e42f737cd18d55eb5b7d6273e8b7"
ERRATUM_SHA = "73da6b55a69e1f370892f1f4e2a23e39f7874db527f68e2a42d6ff959aede2a4"
PHASE_ERRATUM_SHA = "0f323200ba0ea9e1b83e6e2b3769e66b38bb34b4ecbae187debf35e64a43ee8a"
NOTIONAL_ERRATUM_SHA = "9c1f1e454c034b1ac5c6388b2d98d9d402658129793a8b9570a87286a3dca4b5"
FRACTIONS = (.0025, .005, .01, .02, .05)
CONCURRENCY = (1, 3, 5)
CAPS = (25.0, 50.0, 100.0, 250.0, 500.0)
ARMS = ("BASELINE_LOWER", "BASELINE_UPPER", "GL10_L1_LOWER", "GL10_L1_UPPER")
N_PATHS = 10_000
HORIZON = 1024
SEED = 2026071611


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cell_bounds(cell: dict) -> tuple[float, float]:
    if cell.get("resolved") and cell.get("ret") is not None:
        x = float(cell["ret"])
        return x, x
    return PESS, float(cell.get("optimistic_qv2_ret", PESS))


def entry_position_weth(v2: dict, qty_raw: int | None) -> float | None:
    if qty_raw is None or qty_raw <= 0:
        return None
    for off in ("1", "5", "15"):
        proxy = (((v2.get("arm1") or {}).get(off) or {}).get("entry_proxy") or {})
        weth, token = proxy.get("weth_raw"), proxy.get("token_raw")
        if weth is not None and token is not None and int(token) != 0:
            return abs(int(weth)) * int(qty_raw) / abs(int(token))
    return None


def gl10_overlay(gl8: dict, v2: dict, reaction: dict) -> dict:
    cell = dict(gl8["economics"]["1"])
    if reaction.get("status") != "ESCAPABLE":
        return cell
    rb, target = int(reaction["reaction_block"]), int(cell["target_block"])
    if rb > target:
        return cell
    old = (v2.get("arm1") or {}).get("1") or {}
    if cell.get("branch") == "REAL_SELL_CARRY_FORWARD":
        real_block = ((old.get("first_real_sell") or {}).get("block"))
        if real_block is None or int(real_block) <= rb:
            return cell
    x = float(reaction["reactive_ret"])
    return {"resolved": True, "ret": x, "optimistic_qv2_ret": x,
            "branch": "GL10_REACTIVE_EXIT", "target_block": rb}


def qstats(values: list[float]) -> dict:
    a = np.asarray(values, dtype=float)
    return {"n": int(a.size), "p5": float(np.quantile(a, .05)), "p50": float(np.median(a)),
            "mean": float(np.mean(a)), "p95": float(np.quantile(a, .95))}


def main() -> None:
    assert (sha(PREREG) == PREREG_SHA and sha(ERRATUM) == ERRATUM_SHA
            and sha(PHASE_ERRATUM) == PHASE_ERRATUM_SHA and sha(NOTIONAL_ERRATUM) == NOTIONAL_ERRATUM_SHA)
    gl8rows = json.loads(GL8.read_text())["rows"]
    v3rows = json.loads(V3.read_text())["rows"]
    v2rows = json.loads(V2.read_text())["rows"]
    gl10_result = json.loads(GL10.read_text())
    gl10rows = json.loads(GL10PER.read_text())["rows"]
    assert gl10_result["verdict_candidate"] == "REACTIVE_EXIT_INEFFECTIVE"
    v3by = {x["pool"].lower(): x for x in v3rows}
    v2by = {x["pool"].lower(): x for x in v2rows}
    g10by = {x["pool"].lower(): x for x in gl10rows}
    rugs = {x["pool"].lower() for x in json.loads(LABELS.read_text())["rug_pools"]}
    l0 = sorted((x for x in gl8rows if x["common_gate_pass"]),
                key=lambda x: (parse_utc(x["signal_time"]), x["entry_block"], x["pool"]))

    rows = []
    for g in l0:
        pool = g["pool"].lower()
        v3, v2 = v3by[pool], v2by[pool]
        base_lo, base_hi = cell_bounds(g["economics"]["1"])
        reactive_cell = gl10_overlay(g, v2, g10by[pool]["reactions"]["1"]) if pool in g10by else dict(g["economics"]["1"])
        react_lo, react_hi = cell_bounds(reactive_cell)
        qty = g["economics"]["1"].get("qty_raw")
        spend = entry_position_weth(v2, int(qty) if qty is not None else None)
        reserve = (v3.get("entry_weth_evidence") or {}).get("value_raw")
        if spend and spend > 0 and reserve:
            ethusd = 50.0 / (spend / 1e18)
            capacity = .01 * (int(reserve) / 1e18) * ethusd
            cap_status = "RESOLVED"
        else:
            ethusd, capacity, cap_status = None, None, "CAPACITY_UNRESOLVED"
        entry_time = parse_utc(g["signal_time"])
        target = int(g["economics"]["1"]["target_block"])
        duration_s = max(1, target - int(g["entry_block"])) * 2
        rows.append({"pool": pool, "entry_block": int(g["entry_block"]), "target_block": target,
                     "signal_time": entry_time.isoformat(), "entry_time": entry_time,
                     "exit_time": entry_time + timedelta(seconds=duration_s), "duration_seconds": duration_s,
                     "capacity_status": cap_status, "capacity_usd": capacity, "implied_ethusd": ethusd,
                     "strategy_entry_weth_raw": spend,
                     "rug_outcome": pool in rugs,
                     "returns": {"BASELINE_LOWER": base_lo, "BASELINE_UPPER": base_hi,
                                 "GL10_L1_LOWER": react_lo, "GL10_L1_UPPER": react_hi}})

    rug_flags = np.asarray([x["rug_outcome"] for x in rows], dtype=bool)
    distributions = {arm: empirical_metrics(np.asarray([x["returns"][arm] for x in rows]), rug_flags)
                     for arm in ARMS}
    # Offset sensitivity remains baseline-only and complete-population.
    offset_sensitivity = {}
    for off in (1, 5, 15):
        lower, upper = zip(*(cell_bounds(x["economics"][str(off)]) for x in l0))
        offset_sensitivity[str(off)] = {
            "lower": empirical_metrics(np.asarray(lower), rug_flags),
            "upper": empirical_metrics(np.asarray(upper), rug_flags),
        }
    kelly = {arm: kelly_grid(np.asarray([x["returns"][arm] for x in rows])) for arm in ARMS}

    executable = [x for x in rows if x["capacity_status"] == "RESOLVED" and x["capacity_usd"] > 0]
    admitted = {c: concurrency_filter(executable, c) for c in CONCURRENCY}
    admitted_pools = {c: {x["pool"] for x in v} for c, v in admitted.items()}
    for row in rows:
        row["admitted_by_concurrency"] = {str(c): row["pool"] in admitted_pools[c] for c in CONCURRENCY}

    sampled_by_c, local_by_c = {}, {}
    for c in CONCURRENCY:
        local = admitted[c]
        _, groups = month_groups(local)
        sampled_by_c[c] = bootstrap_indices(groups, N_PATHS, HORIZON, SEED + c)
        local_by_c[c] = local

    grid = []
    total = len(ARMS) * len(CONCURRENCY) * len(FRACTIONS) * len(CAPS)
    done = 0
    for arm in ARMS:
        for c in CONCURRENCY:
            local = local_by_c[c]
            vals = np.asarray([x["returns"][arm] for x in local], dtype=float)
            capacities = np.asarray([x["capacity_usd"] for x in local], dtype=float)
            sampled = sampled_by_c[c]
            for f in FRACTIONS:
                for cap in CAPS:
                    mc = simulate(vals, capacities, sampled, f, cap)
                    grid.append({"arm": arm, "fraction": f, "concurrency": c, "dollar_cap": cap,
                                 "admitted_empirical_n": len(local), "paths": N_PATHS,
                                 "horizon_trades": HORIZON, **mc})
                    done += 1
                    if done % 25 == 0:
                        print(f"GL11 grid {done}/{total}", flush=True)

    survivable = {}
    for arm in ARMS:
        survivable[arm] = {}
        for c in CONCURRENCY:
            survivable[arm][str(c)] = {}
            for cap in CAPS:
                ok = [x["fraction"] for x in grid if x["arm"] == arm and x["concurrency"] == c
                      and x["dollar_cap"] == cap and x["ruin_probability"] <= .05]
                survivable[arm][str(c)][str(int(cap))] = max(ok) if ok else None

    common = [f for f in FRACTIONS if all(next(x for x in grid if x["arm"] == "BASELINE_LOWER"
                                                and x["concurrency"] == c and x["dollar_cap"] == 50.0
                                                and x["fraction"] == f)["ruin_probability"] <= .05
                                         for c in CONCURRENCY)]
    main_f = max(common) if common else None
    selected = (next(x for x in grid if x["arm"] == "BASELINE_LOWER" and x["concurrency"] == 1
                     and x["dollar_cap"] == 50.0 and x["fraction"] == main_f) if main_f is not None else None)

    start, end = min(x["entry_time"] for x in executable), max(x["entry_time"] for x in executable)
    calendar = contiguous_months(start, end)
    frequency, capacity_monthly, turnover50 = {}, {}, {}
    for c in CONCURRENCY:
        counts = Counter(x["entry_time"].strftime("%Y-%m") for x in admitted[c])
        capsum = Counter()
        turnsum = Counter()
        for x in admitted[c]:
            month = x["entry_time"].strftime("%Y-%m")
            capsum[month] += x["capacity_usd"]
            turnsum[month] += min(50.0, x["capacity_usd"])
        frequency[str(c)] = qstats([counts[m] for m in calendar])
        capacity_monthly[str(c)] = qstats([capsum[m] for m in calendar])
        turnover50[str(c)] = qstats([turnsum[m] for m in calendar])

    monthly = None
    p8 = None
    n8_label = None
    if main_f is not None:
        local = admitted[1]
        _, groups = month_groups(local)
        months = sorted(groups)
        month_pnls, month_counts = [], []
        vals = np.asarray([x["returns"]["BASELINE_LOWER"] for x in local])
        capsarr = np.asarray([x["capacity_usd"] for x in local])
        for m in calendar:
            bank = B0
            for i in groups.get(m, []):
                stake = min(main_f * bank, 50.0, capsarr[i])
                bank += stake * vals[i]
            month_pnls.append(bank - B0)
            month_counts.append(len(groups.get(m, [])))
        rng = np.random.default_rng(SEED + 8000)
        choices = rng.integers(0, len(calendar), 10_000)
        sampled_pnl = np.asarray(month_pnls)[choices]
        sampled_count = np.asarray(month_counts)[choices]
        monthly = {"pnl_p5": float(np.quantile(sampled_pnl, .05)),
                   "pnl_p50": float(np.median(sampled_pnl)), "pnl_p95": float(np.quantile(sampled_pnl, .95)),
                   "loss_month_probability": float(np.mean(sampled_pnl < 0)),
                   "accepted_signals_p5": float(np.quantile(sampled_count, .05)),
                   "accepted_signals_p50": float(np.median(sampled_count)),
                   "accepted_signals_p95": float(np.quantile(sampled_count, .95)),
                   "historical_month_pnls": dict(zip(calendar, month_pnls))}
        p8_idx = bootstrap_indices(groups, 200_000, 8, SEED + 8)
        p8_mc = simulate(vals, capsarr, p8_idx, main_f, 50.0, candidates=(8,))
        p8 = 1.0 - p8_mc["p_final_above_initial"] - float(np.mean(np.zeros(1)))
        # Recompute strict loss to keep ties out of P(loss).
        bank = np.full(200_000, B0)
        for t in range(8):
            idx = p8_idx[:, t]
            stake = np.minimum(np.minimum(main_f * bank, 50.0), capsarr[idx])
            bank += stake * vals[idx]
        p8 = float(np.mean(bank < B0))
        n8_label = n8_interpretation(p8)

    required = selected["required_n_95"] if selected else None
    freq_med = frequency["1"]["p50"] if selected else None
    months_required = (required / freq_med if required is not None and freq_med and freq_med > 0 else None)
    viability = viability_label(main_f, required, months_required,
                                monthly["pnl_p50"] if monthly is not None else None)

    # All four distributions at the exact same selected operating point.
    selected_by_arm, monthly_by_arm, p8_by_arm = {}, {}, {}
    gl10_selected = None
    if main_f is not None:
        local = admitted[1]
        _, groups = month_groups(local)
        rng = np.random.default_rng(SEED + 8000)
        choices = rng.integers(0, len(calendar), 10_000)
        p8_idx = bootstrap_indices(groups, 200_000, 8, SEED + 8)
        capsarr = np.asarray([x["capacity_usd"] for x in local])
        for arm in ARMS:
            selected_by_arm[arm] = next(x for x in grid if x["arm"] == arm and x["concurrency"] == 1
                                        and x["dollar_cap"] == 50.0 and x["fraction"] == main_f)
            vals = np.asarray([x["returns"][arm] for x in local])
            month_pnls, month_counts = [], []
            for m in calendar:
                bank = B0
                for i in groups.get(m, []):
                    stake = min(main_f * bank, 50.0, capsarr[i])
                    bank += stake * vals[i]
                month_pnls.append(bank - B0)
                month_counts.append(len(groups.get(m, [])))
            sampled_pnl = np.asarray(month_pnls)[choices]
            sampled_count = np.asarray(month_counts)[choices]
            monthly_by_arm[arm] = {
                "pnl_p5": float(np.quantile(sampled_pnl, .05)),
                "pnl_p50": float(np.median(sampled_pnl)),
                "pnl_p95": float(np.quantile(sampled_pnl, .95)),
                "loss_month_probability": float(np.mean(sampled_pnl < 0)),
                "accepted_signals_p5": float(np.quantile(sampled_count, .05)),
                "accepted_signals_p50": float(np.median(sampled_count)),
                "accepted_signals_p95": float(np.quantile(sampled_count, .95)),
                "historical_month_pnls": dict(zip(calendar, month_pnls)),
            }
            bank = np.full(200_000, B0)
            for t in range(8):
                idx = p8_idx[:, t]
                stake = np.minimum(np.minimum(main_f * bank, 50.0), capsarr[idx])
                bank += stake * vals[idx]
            p_loss = float(np.mean(bank < B0))
            p8_by_arm[arm] = {
                "p_loss": p_loss,
                "interpretation": n8_interpretation(p_loss),
                "paths": 200_000,
            }
        gl10_selected = selected_by_arm["GL10_L1_LOWER"]
        assert monthly_by_arm["BASELINE_LOWER"] == monthly
        assert p8_by_arm["BASELINE_LOWER"]["p_loss"] == p8

    serial_rows = []
    for x in rows:
        y = dict(x)
        y["entry_time"], y["exit_time"] = x["entry_time"].isoformat(), x["exit_time"].isoformat()
        serial_rows.append(y)
    PER.write_text(json.dumps({"schema_version": 1, "task_id": "GL11-per-leg",
                               "preregistration_sha256": PREREG_SHA, "erratum_sha256": ERRATUM_SHA,
                               "month_block_phase_erratum_sha256": PHASE_ERRATUM_SHA,
                               "entry_notional_scale_erratum_sha256": NOTIONAL_ERRATUM_SHA,
                               "selection_before_rug_join": True, "rows": serial_rows,
                               "go_decision": "NOT_MADE"}, indent=2, sort_keys=True) + "\n")

    LEDGER.write_text("")
    rpc = {"endpoint_alias": "OFFLINE_NO_RPC", "logical_calls": 0, "http_attempts": 0,
           "attempt_started_records": 0, "attempt_finished_records": 0, "ledger_complete": True,
           "methods": {}, "allowed_methods": ["eth_call", "eth_getBalance", "eth_getBlockByNumber", "eth_getLogs"],
           "public_fallback_used": False, "send_sign_broadcast_calls": 0,
           "ledger_sha256": hashlib.sha256(b"").hexdigest()}
    LEDGER_SUM.write_text(json.dumps(rpc, indent=2, sort_keys=True) + "\n")
    result = {"schema_version": 1, "task_id": "GL11", "research_only": True,
              "preregistration_sha256": PREREG_SHA, "erratum_sha256": ERRATUM_SHA,
              "month_block_phase_erratum_sha256": PHASE_ERRATUM_SHA,
              "entry_notional_scale_erratum_sha256": NOTIONAL_ERRATUM_SHA,
              "step0": {"L0_n": len(rows), "capacity_resolved_n": len(executable),
                        "capacity_unresolved_n": len(rows) - len(executable),
                        "gl10_dependency_verdict": gl10_result["verdict_candidate"], "model_fit_executed": False},
              "constants": {"initial_bankroll": B0, "ruin_line": 2000, "paths": N_PATHS,
                            "horizon_trades": HORIZON, "seed": SEED, "fractions": list(FRACTIONS),
                            "concurrency": list(CONCURRENCY), "dollar_caps": list(CAPS),
                            "capacity_pool_fraction": .01, "nominal_reference_usd": 50,
                            "block_seconds_for_concurrency": 2, "PESS": PESS},
              "empirical_distributions": distributions, "offset_sensitivity": offset_sensitivity,
              "kelly": kelly, "concurrency_admission_n": {str(c): len(admitted[c]) for c in CONCURRENCY},
              "grid": grid, "survivable_fraction_by_arm_concurrency_cap": survivable,
              "main_survivable_fraction": main_f,
              "selected_initial_effective_fraction_cap": min(main_f, 50.0 / B0) if main_f is not None else None,
              "selected_operating_point": selected,
              "selected_operating_points_by_arm": selected_by_arm,
              "gl10_l1_parallel_operating_point": gl10_selected,
              "calendar_months": calendar, "signal_frequency_monthly": frequency,
              "capacity_ceiling_monthly_usd": capacity_monthly,
              "turnover_at_50_cap_monthly_usd": turnover50,
              "selected_monthly_pnl": monthly, "selected_monthly_pnl_by_arm": monthly_by_arm,
              "required_n_95": required,
              "months_required_at_median_frequency": months_required,
              "p_8_cumulative_loss": p8, "n8_interpretation": n8_label,
              "p_8_cumulative_loss_by_arm": p8_by_arm,
              "viability_label": viability, "rpc": rpc,
              "source_hashes": {str(p.relative_to(ROOT)): sha(p) for p in
                                (PREREG, ERRATUM, PHASE_ERRATUM, NOTIONAL_ERRATUM,
                                 GL8, V3, V2, GL10, GL10PER, LABELS)},
              "boundaries": ["historical distribution may decay", "generic simulation does not close wallet-level sellability",
                             "returns above $50 are linear capacity extrapolations", "descriptive point is not GO"],
              "go_decision": "NOT_MADE"}
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"step0": result["step0"], "distributions": distributions,
                      "admission": result["concurrency_admission_n"], "main_f": main_f,
                      "selected": selected, "gl10_parallel": gl10_selected,
                      "frequency": frequency, "capacity": capacity_monthly, "monthly": monthly,
                      "required_n": required, "months_required": months_required, "p8": p8,
                      "n8": n8_label, "viability": viability, "rpc": rpc}, indent=2), flush=True)


if __name__ == "__main__":
    main()
