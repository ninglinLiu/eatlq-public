#!/usr/bin/env python3
"""Generate Phase 0.5 strategy scaffolds and reports from strategies_catalog.

Offline-safe: no DB/RPC/large-file access.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestration.strategies_catalog import FAMILIES, STRATEGIES  # noqa: E402

STRAT_DIR = ROOT / "strategies"
REPORTS = ROOT / "reports"
DOCS = ROOT / "docs"
TESTS = ROOT / "tests"
FIX = TESTS / "fixtures"


TEMPLATE_STRATEGY_PY = '''\
"""Static scaffold for {strategy_id}.

database_access_enabled = false
rpc_access_enabled = false
large_file_access_enabled = false
backtest_enabled = false
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from orchestration.common import (
    ACCESS_FLAGS,
    Bar,
    CostEstimate,
    OrderIntent,
    Signal,
    assert_offline_safe,
)

STRATEGY_ID = "{strategy_id}"
CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text())


def signal_fn(bars: Sequence[Bar], params: Mapping[str, float]) -> list[Signal]:
    """Deterministic placeholder: emits FLAT unless synthetic fixture sets feature trigger=1."""
    assert_offline_safe()
    out: list[Signal] = []
    for b in bars:
        trig = float(b.features.get("trigger", 0.0))
        if trig >= 1.0:
            out.append(
                Signal(
                    strategy_id=STRATEGY_ID,
                    pool_id=b.pool_id,
                    side="BUY",
                    strength=float(b.features.get("strength", 1.0)),
                    event_time=b.event_time,
                    feature_available_at=b.feature_available_at,
                    meta={{"params_used": dict(params)}},
                )
            )
    return out


def entry_fn(signal: Signal, params: Mapping[str, float]) -> OrderIntent | None:
    assert_offline_safe()
    if signal.side != "BUY":
        return None
    delay = int(params.get("execution_delay_bars", 1))
    notional = float(params.get("notional_usd", 50))
    return OrderIntent(
        strategy_id=STRATEGY_ID,
        pool_id=signal.pool_id,
        side="BUY",
        notional_usd=notional,
        decision_time=signal.event_time,
        earliest_exec_time=signal.event_time + delay,
        reason="scaffold_entry",
    )


def exit_fn(position: Mapping[str, Any], bars: Sequence[Bar], params: Mapping[str, float]) -> OrderIntent | None:
    assert_offline_safe()
    if not bars:
        return None
    max_hold = int(params.get("maximum_hold_bars", 5))
    opened = int(position.get("entry_time", bars[0].event_time))
    if bars[-1].event_time - opened >= max_hold:
        return OrderIntent(
            strategy_id=STRATEGY_ID,
            pool_id=str(position.get("pool_id", bars[-1].pool_id)),
            side="SELL",
            notional_usd=float(position.get("notional_usd", 50)),
            decision_time=bars[-1].event_time,
            earliest_exec_time=bars[-1].event_time + 1,
            reason="scaffold_max_hold",
        )
    return None


def cost_fn(intent: OrderIntent, params: Mapping[str, float]) -> CostEstimate:
    assert_offline_safe()
    # UNKNOWN fields remain listed; numeric placeholders are explicit scaffold defaults only.
    return CostEstimate(
        gas_usd=float(params.get("gas_usd_placeholder", 0.15)),
        protocol_fee_usd=float(params.get("protocol_fee_usd_placeholder", 0.0)),
        lp_fee_usd=float(params.get("lp_fee_usd_placeholder", 0.0)),
        slippage_usd=float(params.get("slippage_usd_placeholder", 0.0)),
        impact_usd=float(params.get("impact_usd_placeholder", 0.0)),
        mev_usd=float(params.get("mev_usd_placeholder", 0.0)),
        revert_prob=float(params.get("revert_prob_placeholder", 0.05)),
        failed_sell_prob=float(params.get("failed_sell_prob_placeholder", 0.10)),
        unknown_fields=("broadcast_latency_ms", "calibrated_slippage_distribution"),
    )


def run_scaffold(bars: Sequence[Bar] | None = None) -> dict[str, Any]:
    """Empty runner: wires interfaces on optional synthetic bars; never loads production data."""
    assert_offline_safe()
    assert ACCESS_FLAGS["backtest_enabled"] is False
    cfg = load_config()
    params = {{"notional_usd": 50, "execution_delay_bars": 1, "maximum_hold_bars": 5}}
    bars = list(bars or [])
    signals = signal_fn(bars, params)
    intents = [entry_fn(s, params) for s in signals]
    intents = [i for i in intents if i is not None]
    costs = [cost_fn(i, params) for i in intents]
    return {{
        "strategy_id": STRATEGY_ID,
        "n_bars": len(bars),
        "n_signals": len(signals),
        "n_intents": len(intents),
        "n_costs": len(costs),
        "param_search_space_keys": list(cfg.get("parameter_search_space", {{}}).keys()),
        "backtest_enabled": False,
    }}


if __name__ == "__main__":
    print(json.dumps(run_scaffold(), indent=2))
'''


def write_strategy_dirs() -> None:
    for s in STRATEGIES:
        d = STRAT_DIR / s["strategy_id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "tests").mkdir(exist_ok=True)
        (d / "results").mkdir(exist_ok=True)
        (d / "falsification").mkdir(exist_ok=True)
        cfg = {
            "strategy_id": s["strategy_id"],
            "family": s["family"],
            "name": s["name"],
            "parameter_search_space": s["parameter_search_space"],
            "param_combo_count": s["param_combo_count"],
            "access_flags": {
                "database_access_enabled": False,
                "rpc_access_enabled": False,
                "large_file_access_enabled": False,
                "backtest_enabled": False,
            },
        }
        (d / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
        (d / "runner.py").write_text(TEMPLATE_STRATEGY_PY.format(strategy_id=s["strategy_id"]))
        (d / "__init__.py").write_text(f'"""Scaffold package for {s["strategy_id"]}."""\n')
        # preregistration / mechanism
        (d / "preregistration.md").write_text(
            f"""# {s['strategy_id']} — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `{s['family']}` {s['family_name']}
- name: {s['name']}
- economic_mechanism: {s['economic_mechanism']}
- counterparty: {s['counterparty']}

## Universe / Timing
- universe: {s['universe']}
- observation_time: {s['observation_time']}
- feature_available_at: {s['feature_available_at']}

## Feature formulas
"""
            + "\n".join(f"- `{k}` = {v}" for k, v in s["exact_feature_formulas"].items())
            + f"""

## Parameter search space (not selected)
```json
{json.dumps(s['parameter_search_space'], indent=2)}
```
combo_count = {s['param_combo_count']} (hard cap 512)

## Execution skeleton
- entry: {s['entry']}
- execution_delay: {s['execution_delay']}
- position_size: {s['position_size']}
- exit: {s['exit']}
- stop: {s['stop']}
- take_profit: {s['take_profit']}
- maximum_hold: {s['maximum_hold']}
- repeat_entry: {s['repeat_entry']}
- cooldown: {s['cooldown']}

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {json.dumps(s['failure_handling'])}
- main_falsification: {s['main_falsification']}
- difference_from_prior_failed_strategies: {s['difference_from_prior_failed_strategies']}

## Live conflict notes
{s['live_conflict_note']}
"""
        )
        (d / "mechanism.md").write_text(
            f"""# {s['strategy_id']} Mechanism

{s['economic_mechanism']}

Counterparty: {s['counterparty']}

## Why distinct
{s['difference_from_prior_failed_strategies']}

## Falsification
{s['main_falsification']}
"""
        )
        (d / "data_manifest.json").write_text(
            json.dumps(
                {
                    "strategy_id": s["strategy_id"],
                    "data_class": s["data_class"],
                    "required_tables_or_files": s["required_tables_or_files"],
                    "required_columns": s["required_columns"],
                    "required_time_resolution": s["required_time_resolution"],
                    "point_in_time_requirements": s["point_in_time_requirements"],
                    "minimum_history": s["minimum_history"],
                    "estimated_rows": s["estimated_rows"],
                    "estimated_read_bytes": s["estimated_read_bytes"],
                    "whether_current_diagnostics_may_support_it": s[
                        "whether_current_diagnostics_may_support_it"
                    ],
                    "whether_production_database_would_be_required": s[
                        "whether_production_database_would_be_required"
                    ],
                    "missing_data": s["missing_data"],
                },
                indent=2,
            )
            + "\n"
        )
        (d / "verdict.json").write_text(
            json.dumps(
                {
                    "strategy_id": s["strategy_id"],
                    "verdict": "DESIGN_SCAFFOLD_ONLY",
                    "stage": "PHASE_0_5",
                    "eligible_for_compute": s["data_class"] != "C_PRODUCTION_DB_HEAVY",
                },
                indent=2,
            )
            + "\n"
        )
        (d / "tests" / "test_scaffold.py").write_text(
            f'''\
from strategies.{s["strategy_id"]}.runner import run_scaffold, signal_fn, entry_fn
from orchestration.common import Bar


def test_run_scaffold_empty():
    out = run_scaffold([])
    assert out["strategy_id"] == "{s['strategy_id']}"
    assert out["n_signals"] == 0
    assert out["backtest_enabled"] is False


def test_synthetic_trigger():
    bars = [
        Bar(
            pool_id="0xpool",
            event_time=100,
            known_at=100,
            feature_available_at=100,
            features={{"trigger": 1.0, "strength": 0.8}},
        )
    ]
    sigs = signal_fn(bars, {{}})
    assert len(sigs) == 1
    intent = entry_fn(sigs[0], {{"notional_usd": 50, "execution_delay_bars": 1}})
    assert intent is not None
    assert intent.side == "BUY"
'''
        )


def score_total(s: dict) -> float:
    sc = s["scores"]
    # Higher better for most; invert risk/cost fields
    return (
        sc["economic_plausibility"]
        + sc["novelty"]
        + sc["data_readiness"]
        + sc["execution_feasibility"]
        + sc["capacity"]
        + (5 - sc["expected_gross_edge_required"])
        + (5 - sc["lookahead_risk"])
        + (5 - sc["overfitting_risk"])
        + (5 - sc["live_conflict_risk"])
        + (5 - sc["estimated_compute_cost"])
    )


def assign_waves() -> dict[str, list[str]]:
    # Wave1 constraints from prompt + family diversity
    candidates = []
    for s in STRATEGIES:
        if s["data_class"] == "C_PRODUCTION_DB_HEAVY":
            continue
        if s["data_class"] == "D_DATA_UNAVAILABLE":
            continue
        if s["scores"]["live_conflict_risk"] >= 5:
            continue
        if s["strategy_id"] == "F08_EVT_POOL_AGE_RULE":
            continue  # contrast only
        candidates.append(s)

    def ready_key(s):
        return (0 if s["data_class"].startswith("A_") else 1, -score_total(s))

    candidates.sort(key=ready_key)
    wave1: list[str] = []
    used_families: set[str] = set()
    # pass 1: at most one per family
    for s in candidates:
        if len(wave1) >= 6:
            break
        if s["family"] in used_families:
            continue
        wave1.append(s["strategy_id"])
        used_families.add(s["family"])
    # pass 2: fill remaining by score
    for s in candidates:
        if len(wave1) >= 6:
            break
        if s["strategy_id"] not in wave1:
            wave1.append(s["strategy_id"])
    rest = [s for s in candidates if s["strategy_id"] not in wave1]
    wave2 = [s["strategy_id"] for s in rest[:9]]
    parking = [
        s["strategy_id"]
        for s in STRATEGIES
        if s["strategy_id"] not in wave1 and s["strategy_id"] not in wave2
    ]
    return {"wave1": wave1, "wave2": wave2, "parking": parking}


def orthogonality_pairs() -> list[dict]:
    """Mechanism-level audit; flag near-duplicates."""
    flags = []
    # Designed intentional near-checks
    checks = [
        ("F02_LP_POST_MINT_DRIFT", "F06_REV_LIQ_REINJECT", "mint-related but silence+resume joint vs mint-only"),
        ("F02_LP_POST_MINT_DRIFT", "F08_EVT_MINT_BURST", "single mint z vs multi-owner burst"),
        ("F01_OFS_SIGN_RUNLEN", "F01_OFS_INTERARRIVAL", "run length vs inter-arrival compression"),
        ("F03_VOL_COMPRESS_EXPAND", "F03_VOL_JUMP_CONTINUE", "compress-expand vs jump"),
        ("F04_XS_SAME_AGE_RS", "F08_EVT_POOL_AGE_RULE", "RS vs deterministic age+impact rule"),
        ("F10_PLP_HIGHVOL_LOWTREND", "F10_PLP_EVENT_WINDOW", "screen LP vs event-scheduled LP"),
        ("F06_REV_SILENCE_FIRST_TRADE", "F06_REV_VOL_BEFORE_PRICE", "first trade vs vol-before-price"),
    ]
    for a, b, note in checks:
        sa, sb = next(x for x in STRATEGIES if x["strategy_id"] == a), next(
            x for x in STRATEGIES if x["strategy_id"] == b
        )
        same_mech = sa["economic_mechanism"] == sb["economic_mechanism"]
        flags.append(
            {
                "a": a,
                "b": b,
                "identical_mechanism": same_mech,
                "identical_entry": sa["entry"] == sb["entry"],
                "identical_exit": sa["exit"] == sb["exit"],
                "verdict": "DISTINCT_OK" if not same_mech else "REPLACE_REQUIRED",
                "note": note,
            }
        )
    # Full pairwise cheap hash of (mechanism, entry, exit)
    seen = {}
    for s in STRATEGIES:
        key = (s["economic_mechanism"], s["entry"], s["exit"])
        if key in seen:
            flags.append(
                {
                    "a": seen[key],
                    "b": s["strategy_id"],
                    "identical_mechanism": True,
                    "identical_entry": True,
                    "identical_exit": True,
                    "verdict": "REPLACE_REQUIRED",
                    "note": "exact triple collision",
                }
            )
        else:
            seen[key] = s["strategy_id"]
    return flags


def write_reports(waves: dict[str, list[str]], ortho: list[dict]) -> None:
    REPORTS.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)

    # Atlas
    lines = [
        "# BASE_ALPHA_ATLAS_STRATEGY_ATLAS_v0",
        "",
        "Phase 0.5 design atlas. No parameters selected. No historical PnL inspected.",
        "",
        f"Total strategies: {len(STRATEGIES)}",
        "",
    ]
    for fam, title in FAMILIES.items():
        lines += [f"## {fam} — {title}", ""]
        for s in STRATEGIES:
            if s["family"] != fam:
                continue
            lines += [
                f"### {s['strategy_id']} — {s['name']}",
                "",
                f"- **economic_mechanism**: {s['economic_mechanism']}",
                f"- **counterparty**: {s['counterparty']}",
                f"- **universe**: {s['universe']}",
                f"- **observation_time**: {s['observation_time']}",
                f"- **feature_available_at**: {s['feature_available_at']}",
                f"- **entry**: {s['entry']}",
                f"- **execution_delay**: {s['execution_delay']}",
                f"- **position_size**: {s['position_size']}",
                f"- **exit**: {s['exit']}",
                f"- **stop**: {s['stop']}",
                f"- **take_profit**: {s['take_profit']}",
                f"- **maximum_hold**: {s['maximum_hold']}",
                f"- **repeat_entry**: {s['repeat_entry']}",
                f"- **cooldown**: {s['cooldown']}",
                f"- **param_combo_count**: {s['param_combo_count']}",
                f"- **data_class**: `{s['data_class']}`",
                f"- **main_falsification**: {s['main_falsification']}",
                f"- **diff_vs_prior_failures**: {s['difference_from_prior_failed_strategies']}",
                f"- **live_conflict_note**: {s['live_conflict_note']}",
                "",
                "Feature formulas:",
            ]
            for k, v in s["exact_feature_formulas"].items():
                lines.append(f"  - `{k}` = {v}")
            lines += [
                "",
                "Parameter search space:",
                "```json",
                json.dumps(s["parameter_search_space"], indent=2),
                "```",
                "",
            ]
    (REPORTS / "BASE_ALPHA_ATLAS_STRATEGY_ATLAS_v0.md").write_text("\n".join(lines) + "\n")

    # Orthogonality
    bad = [o for o in ortho if o["verdict"] != "DISTINCT_OK"]
    o_lines = [
        "# BASE_ALPHA_ATLAS_ORTHOGONALITY_DESIGN_AUDIT",
        "",
        "Mechanism-level audit (Phase 0.5). No trade-overlap statistics computed (no data).",
        "",
        f"Exact triple collisions (mechanism+entry+exit): {sum(1 for o in ortho if o.get('note')=='exact triple collision')}",
        f"REPLACE_REQUIRED flags: {len(bad)}",
        "",
        "## Pairwise focused checks",
        "",
        "| A | B | identical_mechanism | identical_entry | identical_exit | verdict | note |",
        "|---|---|'---'|---|---|---|---|",
    ]
    # fix table header typo
    o_lines[-1] = "| A | B | identical_mechanism | identical_entry | identical_exit | verdict | note |"
    o_lines.append("|---|---|---|---|---|---|---|")
    for o in ortho:
        o_lines.append(
            f"| {o['a']} | {o['b']} | {o['identical_mechanism']} | {o['identical_entry']} | {o['identical_exit']} | {o['verdict']} | {o['note']} |"
        )
    o_lines += [
        "",
        "## Family internal distinctness",
        "",
        "Each family contains 3 strategies with distinct primary mechanism axes:",
        "",
        "1. F01: run-length / size-sequence / inter-arrival",
        "2. F02: post-mint / post-burn / depth-lead",
        "3. F03: compress-expand / jump / failed-break fade",
        "4. F04: leader→second / same-age RS / old→new migrate",
        "5. F05: breadth / dilution avoidance / gas congestion",
        "6. F06: silence first trade / vol-before-price / liq re-inject",
        "7. F07: sync new wallets / early-holder release / HHI decline",
        "8. F08: age rule contrast / mint burst / fee-tier migration",
        "9. F09: impact asymmetry / notional curve / stale quote",
        "10. F10: fee vs IL / highvol-lowtrend LP / event-window LP",
        "",
        "## Live Alpha orthogonality",
        "",
        "PROTECTED_LIVE_ALPHA (KEYSTONE): young-pool window + CH features + ML `p_fatal` → Redis signals.",
        "Atlas strategies that share young-pool timing (esp. `F04_XS_SAME_AGE_RS`, `F08_EVT_POOL_AGE_RULE`) are marked high live_conflict_risk and excluded from Wave 1 even if statistically interesting later.",
        "",
        f"## Verdict",
        "",
        (
            "`ORTHOGONALITY_DESIGN_PASS`"
            if not bad
            else "`ORTHOGONALITY_DESIGN_NEEDS_REPLACEMENT`"
        ),
        "",
    ]
    (REPORTS / "BASE_ALPHA_ATLAS_ORTHOGONALITY_DESIGN_AUDIT.md").write_text("\n".join(o_lines) + "\n")

    # Data matrix
    d_lines = [
        "# BASE_ALPHA_ATLAS_DATA_REQUIREMENTS_MATRIX",
        "",
        "Filename/schema-level only. No large diagnostics content read. No production business queries.",
        "",
        "| strategy_id | class | tables/files | columns | resolution | PIT | min_history | est_rows | est_bytes | diagnostics_maybe | prod_db_required | missing |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    counts = {"A_OFFLINE_DATA_LIKELY_AVAILABLE": 0, "B_SMALL_BOUNDED_EXPORT_REQUIRED": 0, "C_PRODUCTION_DB_HEAVY": 0, "D_DATA_UNAVAILABLE": 0}
    for s in STRATEGIES:
        counts[s["data_class"]] = counts.get(s["data_class"], 0) + 1
        d_lines.append(
            "| {strategy_id} | `{data_class}` | {tables} | {cols} | {res} | {pit} | {mh} | {er} | {eb} | {diag} | {pdb} | {miss} |".format(
                strategy_id=s["strategy_id"],
                data_class=s["data_class"],
                tables="; ".join(s["required_tables_or_files"])[:80],
                cols="; ".join(s["required_columns"])[:60],
                res=s["required_time_resolution"],
                pit=str(s["point_in_time_requirements"])[:40],
                mh=s["minimum_history"],
                er=s["estimated_rows"],
                eb=s["estimated_read_bytes"],
                diag=str(s["whether_current_diagnostics_may_support_it"])[:40],
                pdb=s["whether_production_database_would_be_required"],
                miss="; ".join(s["missing_data"])[:50],
            )
        )
    d_lines += [
        "",
        "## Class counts",
        "",
        *[f"- `{k}`: {v}" for k, v in counts.items()],
        "",
        "## Rule",
        "",
        "Class `C_PRODUCTION_DB_HEAVY` MUST NOT enter first compute wave.",
        "Class `D_DATA_UNAVAILABLE` parked until data appears offline.",
        "",
    ]
    (REPORTS / "BASE_ALPHA_ATLAS_DATA_REQUIREMENTS_MATRIX.md").write_text("\n".join(d_lines) + "\n")

    # Wave priority
    w_lines = [
        "# BASE_ALPHA_ATLAS_WAVE_PRIORITY",
        "",
        "Compute order only — **not** promotion to Paper/Live.",
        "",
        "## Scoring dimensions (1-5)",
        "`economic_plausibility, novelty, data_readiness, expected_gross_edge_required, execution_feasibility, capacity, lookahead_risk, overfitting_risk, live_conflict_risk, estimated_compute_cost`",
        "",
        "Composite used for ranking favors readiness & feasibility; penalizes live conflict, compute, lookahead, overfitting.",
        "",
        "## Wave 1 (max 6)",
        "",
    ]
    for i, sid in enumerate(waves["wave1"], 1):
        s = next(x for x in STRATEGIES if x["strategy_id"] == sid)
        w_lines.append(
            f"{i}. `{sid}` — class `{s['data_class']}` — score {score_total(s):.0f} — {s['name']}"
        )
    w_lines += ["", "## Wave 2 (max 9)", ""]
    for i, sid in enumerate(waves["wave2"], 1):
        s = next(x for x in STRATEGIES if x["strategy_id"] == sid)
        w_lines.append(
            f"{i}. `{sid}` — class `{s['data_class']}` — score {score_total(s):.0f} — {s['name']}"
        )
    w_lines += ["", "## Parking", ""]
    for sid in waves["parking"]:
        s = next(x for x in STRATEGIES if x["strategy_id"] == sid)
        w_lines.append(f"- `{sid}` — `{s['data_class']}` — live_conflict={s['scores']['live_conflict_risk']} — {s['name']}")
    w_lines += [
        "",
        "## Wave 1 selection rationale",
        "",
        "- Prefer `A_OFFLINE_DATA_LIKELY_AVAILABLE` then light `B_`",
        "- Exclude `C_PRODUCTION_DB_HEAVY` and `D_DATA_UNAVAILABLE`",
        "- Exclude live_conflict_risk=5 and KEYSTONE-contrast age rule from Wave 1 execution candidates",
        "- Favor quick falsifiability and lower compute",
        "",
        "```json",
        json.dumps(waves, indent=2),
        "```",
        "",
    ]
    (REPORTS / "BASE_ALPHA_ATLAS_WAVE_PRIORITY.md").write_text("\n".join(w_lines) + "\n")
    (ROOT / "orchestration" / "wave_priority.json").write_text(json.dumps(waves, indent=2) + "\n")

    # Panel spec
    (DOCS / "BASE_ALPHA_ATLAS_SHARED_PANEL_SPEC.md").write_text(
        """# BASE_ALPHA_ATLAS_SHARED_PANEL_SPEC

Status: `DESIGN_ONLY` — build forbidden until `ready_for_shared_panel_build=true`.

## Goal

One immutable panel consumed read-only by all strategies:

```text
shared_panel/base_alpha_atlas_shared_panel.parquet
```

## Minimal necessary fields

### Keys / PIT
- `pool_id`
- `event_time` (unix s)
- `block_number` (nullable)
- `log_index` (nullable)
- `bar_close_time`
- `known_at`
- `feature_available_at`  # must be >= all input event times used

### Market
- `open, high, low, close` at 1m (and optional 5m/15m columns prefixed)
- `volume_eth`, `buy_volume_eth`, `sell_volume_eth`
- `trade_count`, `buy_count`, `sell_count`
- `vwap_eth`

### Microstructure (nullable if sparse)
- `mean_interarrival_s`, `median_interarrival_s`
- `sign_run_len`, `large_trade_flag`
- `size_p90_eth`

### LP / events (nullable)
- `mint_eth_proxy`, `burn_eth_proxy`
- `event_mint_burst_k`
- `pool_age_minutes`
- `first_swap_time`

### Cross-section / regime (nullable, join-as-of)
- `xs_ret_1h_rank`
- `breadth_active_pools_1h`
- `new_pools_1h`
- `base_fee_z` (if/when available)

### Execution proxies (nullable)
- `impact_buy_proxy`, `impact_sell_proxy`
- `depth_proxy`

### Cost placeholders (not filled with 0)
- columns may be absent; evaluation must keep UNKNOWN

## Sources (design mapping)

| Field group | Likely offline diagnostics | Needs bounded export | Needs heavy prod DB |
|---|---|---|---|
| OHLCV 1m | sprint035e/f ohlcv* names | maybe rebuild from swaps | if coverage holes |
| swaps microstructure | base_b5_real_swaps*.tsv name | yes bounded | full history CH |
| mints/burns | docs mention decoded tables | yes | yes if not copied |
| wallet/HHI | controller/transfer audits (HUGE) | no for Wave1 | yes |
| base_fee | not found by name | external archive | maybe |

## Partitioning
- Hive-style: `panel_date=YYYY-MM-DD/pool_shard=00..0F`
- Sorted by `(pool_id, bar_close_time)`
- Row group target ~64-128MB

## Expected size (order-of-magnitude, not measured)
- 30d × 500 pools × 1m bars ≈ 2.16e7 rows
- ~50-120 bytes/row compressed → **~1-3 GB** parquet (estimate only)
- Wave1 may start with **subset panel** 7-14d × top-N pools to stay <<1GB

## One-build / thirty-read pattern
1. Builder job (future, gated) writes panel + `manifest.json` (sha256, row counts, column dict, PIT checks)
2. Strategies receive `panel_path` + column projection list
3. Runners must `read_parquet(columns=needed)` only
4. Forbidden: per-strategy raw CH scan or re-deriving OHLCV from raw logs

## PIT rules
- `feature_available_at >= max(known_at of inputs)`
- as-of joins for XS/regime columns
- no `FINAL` full-table semantics in builder queries when gate opens; prefer bounded incremental

## Explicit non-goals now
- no build
- no 325MB diagnostics ingestion
- no production `GROUP BY pool_address` full scans
"""
    )

    (REPORTS / "BASE_ALPHA_ATLAS_PANEL_BUILD_PREFLIGHT.md").write_text(
        """# BASE_ALPHA_ATLAS_PANEL_BUILD_PREFLIGHT

```text
ready_for_shared_panel_build = false
ready_for_large_file_copy = false
ready_for_production_database_access = false
```

## Preflight checklist (all must be true later)

1. `SAFE_TO_BEGIN` style host gate passes without idle<10% streaks
2. ClickHouse not in multi-core research contention (live bridge load acknowledged)
3. Offline sources for Wave1 columns identified with sha256 after **small** header-only probes
4. Builder runs nice=19 ionice-idle, threads=1, mem<=384MB, timeout<=20m, checkpointable
5. Output only under `/root/base_alpha_atlas_30_v0/shared_panel/`
6. No strategy runner reads raw production tables

## Current blockers

- Phase -1–0 resource pause still valid for heavy IO
- No verified offline parquet panel exists
- Several Wave2/Parking fields require mints/wallet/fee tapes

## Allowed now

- This design document
- Synthetic fixtures for interface tests

## Forbidden now

- Building `base_alpha_atlas_shared_panel.parquet`
- Copying 100MB+ diagnostics into `normalized/`
- Querying `eatlq.uniswap_v3_swaps_decoded` or related business tables
"""
    )


def write_fixture_and_global_tests() -> None:
    FIX.mkdir(parents=True, exist_ok=True)
    (FIX / "synthetic_bars.json").write_text(
        json.dumps(
            [
                {
                    "pool_id": "0xsynthetic",
                    "event_time": 1_700_000_000 + i * 60,
                    "known_at": 1_700_000_000 + i * 60,
                    "feature_available_at": 1_700_000_000 + i * 60,
                    "features": {"trigger": 1.0 if i == 3 else 0.0, "strength": 0.5},
                }
                for i in range(6)
            ],
            indent=2,
        )
        + "\n"
    )
    (TESTS / "__init__.py").write_text("")
    (TESTS / "test_phase05_catalog.py").write_text(
        '''\
"""Small offline fixture tests for Phase 0.5 (unittest, no pytest required)."""
from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path

from orchestration.common import ACCESS_FLAGS, Bar, assert_offline_safe
from orchestration.strategies_catalog import FAMILIES, STRATEGIES, by_id

ROOT = Path(__file__).resolve().parents[1]


class Phase05CatalogTests(unittest.TestCase):
    def test_thirty_strategies_ten_families(self):
        self.assertEqual(len(STRATEGIES), 30)
        self.assertEqual(len(FAMILIES), 10)
        for fam in FAMILIES:
            self.assertEqual(sum(1 for s in STRATEGIES if s["family"] == fam), 3)

    def test_param_caps_and_ids_unique(self):
        ids = [s["strategy_id"] for s in STRATEGIES]
        self.assertEqual(len(ids), len(set(ids)))
        for s in STRATEGIES:
            self.assertLessEqual(s["param_combo_count"], 512)
            self.assertLessEqual(len(s["parameter_search_space"]), 5)

    def test_access_flags_safe(self):
        assert_offline_safe()
        self.assertFalse(ACCESS_FLAGS["database_access_enabled"])
        self.assertFalse(ACCESS_FLAGS["backtest_enabled"])

    def test_wave_file_constraints(self):
        waves = json.loads((ROOT / "orchestration" / "wave_priority.json").read_text())
        self.assertLessEqual(len(waves["wave1"]), 6)
        self.assertLessEqual(len(waves["wave2"]), 9)
        idmap = by_id()
        for sid in waves["wave1"]:
            self.assertNotEqual(idmap[sid]["data_class"], "C_PRODUCTION_DB_HEAVY")
            self.assertNotEqual(idmap[sid]["data_class"], "D_DATA_UNAVAILABLE")
            self.assertLess(idmap[sid]["scores"]["live_conflict_risk"], 5)

    def test_each_strategy_scaffold_importable(self):
        for s in STRATEGIES:
            mod = importlib.import_module(f"strategies.{s['strategy_id']}.runner")
            out = mod.run_scaffold([])
            self.assertEqual(out["n_bars"], 0)
            bars = [
                Bar(
                    pool_id="0xpool",
                    event_time=100,
                    known_at=100,
                    feature_available_at=100,
                    features={"trigger": 1.0, "strength": 0.8},
                )
            ]
            sigs = mod.signal_fn(bars, {})
            self.assertEqual(len(sigs), 1)


if __name__ == "__main__":
    unittest.main()
'''
    )
    (ROOT / "orchestration" / "__init__.py").write_text('"""Atlas orchestration package."""\n')


def write_phase05_result(waves: dict, ortho: list[dict]) -> None:
    bad = [o for o in ortho if o["verdict"] != "DISTINCT_OK"]
    (REPORTS / "BASE_ALPHA_ATLAS_PHASE_0_5_RESULT.md").write_text(
        f"""# BASE_ALPHA_ATLAS_PHASE_0_5_RESULT

## Phase 0.5 — Thirty-Strategy Design and Static Research Scaffold

```text
ready_for_strategy_design = true
ready_for_static_scaffolding = true
ready_for_small_fixture_tests = true
ready_for_wave1_execution_plan = true

ready_for_large_file_copy = false
ready_for_shared_panel_build = false
ready_for_production_database_access = false
ready_for_backtest = false
```

## Completed

1. 30 strategy cards across 10 families (catalog + per-strategy preregistration)
2. Orthogonality design audit — REPLACE_REQUIRED={len(bad)}
3. Data requirements matrix with A/B/C/D classes
4. Shared panel **spec** + build preflight (no build)
5. Static scaffolds under `strategies/<id>/` with config, interfaces, empty runner
6. Synthetic fixture tests
7. Wave1/2/Parking compute order

## Explicitly not done

- No production business table queries
- No large diagnostics reads/copies
- No panel parquet build
- No historical backtests / parameter selection / PnL inspection

## Key paths

- `reports/BASE_ALPHA_ATLAS_STRATEGY_ATLAS_v0.md`
- `reports/BASE_ALPHA_ATLAS_ORTHOGONALITY_DESIGN_AUDIT.md`
- `reports/BASE_ALPHA_ATLAS_DATA_REQUIREMENTS_MATRIX.md`
- `reports/BASE_ALPHA_ATLAS_WAVE_PRIORITY.md`
- `docs/BASE_ALPHA_ATLAS_SHARED_PANEL_SPEC.md`
- `reports/BASE_ALPHA_ATLAS_PANEL_BUILD_PREFLIGHT.md`
- `orchestration/strategies_catalog.py`
- `orchestration/wave_priority.json`

## Wave 1 IDs

{chr(10).join('- `' + x + '`' for x in waves['wave1'])}

## Stop

Phase 0.5 complete. Do **not** auto-start data access or backtests.

## Health snapshots (short)

See `checkpoints/phase05_health_start.txt` and `checkpoints/phase05_health_end.txt`.

## Fixture tests

```bash
PYTHONPATH=/root/base_alpha_atlas_30_v0 python3 -m unittest discover -s tests -v
```
"""
    )


def main() -> None:
    write_strategy_dirs()
    waves = assign_waves()
    ortho = orthogonality_pairs()
    write_reports(waves, ortho)
    write_fixture_and_global_tests()
    write_phase05_result(waves, ortho)
    # dump catalog json
    (ROOT / "orchestration" / "strategies_catalog.json").write_text(
        json.dumps(STRATEGIES, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "strategies": len(STRATEGIES),
                "wave1": waves["wave1"],
                "ortho_bad": sum(1 for o in ortho if o["verdict"] != "DISTINCT_OK"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
