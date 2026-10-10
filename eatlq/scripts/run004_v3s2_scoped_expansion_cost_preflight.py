"""Run-004 V3S-2 — scoped 5m expansion cost preflight (static only).

Reads V3S-1 / V3R-31F / V3R-29B artifacts. Freezes ONE strategy universe, estimates
RPC/time/cost to reach 500 / 2000 clean rows. Does NOT quote, train, expand, or scan.

See outputs:
  mvp/diagnostics/run004_v3s2_scoped_expansion_cost_preflight_summary.json
  mvp/runs/run-004-v3s2-scoped-expansion-cost-preflight.md
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active for V3S-2 preflight."

V3S1 = ROOT / "mvp/diagnostics/run004_v3s1_scoped_5m_feasibility_summary.json"
V3S1_MD = ROOT / "mvp/runs/run-004-v3s1-scoped-5m-feasibility.md"
V31F = ROOT / "mvp/diagnostics/run004_v3r31f_failure_consolidation_summary.json"
V29B_SUM = ROOT / "mvp/diagnostics/run004_v3r29b_small_timestamp_dataset_rebalance_summary.json"
V29B_ROWS = ROOT / "mvp/diagnostics/run004_v3r29b_small_timestamp_dataset_rebalance_rows.csv"

OUT_JSON = ROOT / "mvp/diagnostics/run004_v3s2_scoped_expansion_cost_preflight_summary.json"
OUT_MD = ROOT / "mvp/runs/run-004-v3s2-scoped-expansion-cost-preflight.md"

# V3R-29B measured marginal quoting (new attempts only).
RPC_USED_V29B = 1130
NEWLY_ATTEMPTED_V29B = 82
NEWLY_CLEAN_V29B = 29

# JsonRpcClient default delay is 0.04s; V3R-29B sets inner.delay_s = 0.01 for that run.
DELAY_ASSUMED_S = 0.085  # all-in sec per eth_call (sleep + HTTP + retries cushion)

# USD brackets per 1e6 eth_call-equivalents (provider-dependent; user must confirm).
PRICE_LOW_PER_M = 5.0
PRICE_MID_PER_M = 15.0
PRICE_HIGH_PER_M = 70.0  # above ~70/M, 75k calls can exceed $5

WALL_TIME_CONFIRM_MIN = 20.0
API_COST_CONFIRM_USD = 5.0


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def notional_cap(rows: list[dict[str, Any]]) -> float:
    return max((_f(r.get("selected_notional_weth")) for r in rows), default=0.0)


def is_at_cap(r: dict[str, Any], cap: float) -> bool:
    return cap > 0 and abs(_f(r.get("selected_notional_weth")) - cap) <= 1e-17


def y5(r: dict[str, Any]) -> int:
    return 1 if _f(r.get("label_return_5m")) > 0 else 0


def y30(r: dict[str, Any]) -> int:
    return 1 if _f(r.get("label_return_30m")) > 0 else 0


def chrono_drift_30m(sub: list[dict[str, Any]]) -> float | None:
    tr = [r for r in sub if r.get("chronological_split") == "train"]
    ho = [r for r in sub if r.get("chronological_split") == "holdout"]
    if not tr or not ho:
        return None
    pr_tr = sum(y30(r) for r in tr) / len(tr)
    pr_ho = sum(y30(r) for r in ho) / len(ho)
    return round(pr_ho - pr_tr, 4)


def decay_warning(sub: list[dict[str, Any]]) -> str | None:
    d = chrono_drift_30m(sub)
    if d is None:
        return "chronological holdout missing for this slice; 30m decay diagnostic unavailable on frozen split."
    if d >= 0.12:
        return (
            f"30m chronological drift +{d}: holdout more positive than train — decay/risk context for 5m-only scope, not a hard fail."
        )
    if d <= -0.08:
        return f"30m chronological drift {d}: holdout weaker than train; monitor label quality."
    return None


def estimate_expansion(
    current_clean: int,
    target_clean: int,
    clean_rate: float,
    rpc_per_attempt: float,
) -> dict[str, Any]:
    need = max(0, target_clean - current_clean)
    if need == 0:
        return {
            "additional_clean_rows_needed": 0,
            "estimated_candidate_attempts": 0,
            "estimated_rpc_calls": 0,
            "estimated_wall_time_seconds": 0.0,
            "estimated_api_cost_usd_low": 0.0,
            "estimated_api_cost_usd_mid": 0.0,
            "estimated_api_cost_usd_high": 0.0,
        }
    attempts = math.ceil(need / max(1e-9, clean_rate))
    rpc = int(math.ceil(attempts * rpc_per_attempt))
    wall_s = rpc * DELAY_ASSUMED_S
    return {
        "additional_clean_rows_needed": need,
        "estimated_candidate_attempts": attempts,
        "estimated_rpc_calls": rpc,
        "estimated_wall_time_seconds": round(wall_s, 1),
        "estimated_wall_time_minutes": round(wall_s / 60.0, 2),
        "estimated_api_cost_usd_low": round(rpc * PRICE_LOW_PER_M / 1e6, 4),
        "estimated_api_cost_usd_mid": round(rpc * PRICE_MID_PER_M / 1e6, 4),
        "estimated_api_cost_usd_high": round(rpc * PRICE_HIGH_PER_M / 1e6, 4),
    }


def main() -> int:
    for p in (V3S1, V31F, V29B_SUM, V29B_ROWS):
        if not p.exists():
            print(f"missing {p}", file=sys.stderr)
            return 2

    v3s1 = json.loads(V3S1.read_text(encoding="utf-8"))
    v31f = json.loads(V31F.read_text(encoding="utf-8"))
    v29b = json.loads(V29B_SUM.read_text(encoding="utf-8"))
    rows = load_csv(V29B_ROWS)

    cap = notional_cap(rows)
    scoped = [r for r in rows if is_at_cap(r, cap)]
    n = len(scoped)
    pools = len({r.get("pool_address", "") for r in scoped if r.get("pool_address")})
    tokens = len({r.get("token_address", "") for r in scoped if r.get("token_address")})
    pr5 = round(sum(y5(r) for r in scoped) / n, 4) if n else None
    pr30 = round(sum(y30(r) for r in scoped) / n, 4) if n else None
    dw = decay_warning(scoped)

    rpc_per_attempt = RPC_USED_V29B / max(1, NEWLY_ATTEMPTED_V29B)
    clean_rate_point = NEWLY_CLEAN_V29B / max(1, NEWLY_ATTEMPTED_V29B)
    clean_rate_low = max(0.08, clean_rate_point * 0.65)
    clean_rate_high = min(0.55, clean_rate_point * 1.15)

    est_500 = estimate_expansion(n, 500, clean_rate_point, rpc_per_attempt)
    est_500_pess = estimate_expansion(n, 500, clean_rate_low, rpc_per_attempt)
    est_2000 = estimate_expansion(n, 2000, clean_rate_point, rpc_per_attempt)
    est_2000_pess = estimate_expansion(n, 2000, clean_rate_low, rpc_per_attempt)

    candidate_pool_note = (
        "Explicit raw candidate pool exists from V3R-24R-style `source_inventory` / "
        "V3R-29 expansion pipeline (thousands of preselected rows before clean filter); "
        "V3S-2 expansion would filter attempts to WETH notional-at-cap only at quote time."
    )

    why_scope = (
        "V3S-1: largest interpretable slice with n>=100 (notional **at_cap**); matches capped "
        "adaptive notional policy; 22 pools in-slice vs single-pool scope; meets hard gate "
        "rows>=100 without relying on fee/source cells under 100. Dual-horizon general model "
        "remains rejected (V3R-31F); this scope is **5m-primary** feasibility only."
    )

    selected_scope: dict[str, Any] = {
        "scope_type": "notional_bucket",
        "scope_key": "at_cap",
        "definition": (
            "Rows where selected_notional_weth equals observed dataset notional cap "
            f"({cap:g} WETH) within 1e-17 absolute tolerance."
        ),
        "notional_cap_weth": cap,
        "aligned_with_v3s1_scope_type": "notional_bucket",
    }

    wall_500 = est_500["estimated_wall_time_minutes"]
    wall_2000 = est_2000["estimated_wall_time_minutes"]
    cost_high_500 = est_500["estimated_api_cost_usd_high"]
    cost_high_2000 = est_2000["estimated_api_cost_usd_high"]

    ask_time = wall_500 > WALL_TIME_CONFIRM_MIN or wall_2000 > WALL_TIME_CONFIRM_MIN
    ask_cost = cost_high_500 > API_COST_CONFIRM_USD or cost_high_2000 > API_COST_CONFIRM_USD

    row_gate_ok = n >= 100
    stop_conditions = [
        f"RPC budget ceiling per run (e.g. <= {v29b.get('rpc_limit', 9000)} from V3R-29B pattern not sufficient for full expansion — plan batched runs).",
        "Clean rows in scope stop rising while failure_reason_distribution stable → pause and audit.",
        "Consecutive attempt batches with clean_rate below pessimistic floor (see clean_rate_low).",
        "Provider 429 / rate-limit storm → backoff and ASK_USER.",
        "No training / backtest claims until clean rows >= target tier (500 tiny model; 2000 scoped backtest discussion only).",
    ]

    max_loss_usd = round(
        est_2000_pess["estimated_rpc_calls"] * PRICE_HIGH_PER_M / 1e6,
        2,
    )

    training_note = (
        f"Current scoped clean rows {n} < 500: **no training** allowed per V3S-2 gate. "
        "500 clean rows → tiny scoped model feasibility only. 2000 → scoped backtest discussion."
    )

    go_no_go = "NO_GO" if not row_gate_ok else "CONDITIONAL_GO_PREFLIGHT_ONLY"
    if ask_time or ask_cost:
        go_no_go = "ASK_USER_CONFIRMATION_BEFORE_ANY_RUN"

    payload: dict[str, Any] = {
        "run": "run004_v3s2_scoped_expansion_cost_preflight",
        "research_only": True,
        "no_quote": True,
        "no_training": True,
        "no_dataset_expansion_executed": True,
        "no_chain_scan": True,
        "no_mainline_live_shadow_promote": True,
        "generator_script": "scripts/run004_v3s2_scoped_expansion_cost_preflight.py",
        "v3r31f_context_loaded": True,
        "inputs": {
            "v3s1_summary": str(V3S1.relative_to(ROOT)),
            "v3s1_md": str(V3S1_MD.relative_to(ROOT)),
            "v3r31f_summary": str(V31F.relative_to(ROOT)),
            "v3r29b_summary": str(V29B_SUM.relative_to(ROOT)),
            "v3r29b_rows": str(V29B_ROWS.relative_to(ROOT)),
        },
        "selected_scope": selected_scope,
        "why_this_scope": why_scope,
        "current_rows": n,
        "current_pools": pools,
        "current_tokens": tokens,
        "current_5m_positive_rate": pr5,
        "current_30m_positive_rate": pr30,
        "current_30m_decay_warning": dw,
        "candidate_pool_supplement_source": candidate_pool_note,
        "v3r29b_marginal_benchmark": {
            "newly_attempted_rows": NEWLY_ATTEMPTED_V29B,
            "newly_clean_rows": NEWLY_CLEAN_V29B,
            "estimated_clean_rate_point": round(clean_rate_point, 4),
            "estimated_clean_rate_low": round(clean_rate_low, 4),
            "estimated_clean_rate_high": round(clean_rate_high, 4),
            "rpc_used_for_attempts": RPC_USED_V29B,
            "rpc_per_attempt_estimate": round(rpc_per_attempt, 3),
        },
        "target_clean_rows_500_cost_estimate": {
            "interpretation": "tiny scoped model feasibility tier only",
            "mid_assumption": est_500,
            "pessimistic_clean_rate": est_500_pess,
        },
        "target_clean_rows_2000_cost_estimate": {
            "interpretation": "scoped backtest discussion tier",
            "mid_assumption": est_2000,
            "pessimistic_clean_rate": est_2000_pess,
        },
        "estimated_candidate_needed_mid_500": est_500["estimated_candidate_attempts"],
        "estimated_candidate_needed_mid_2000": est_2000["estimated_candidate_attempts"],
        "estimated_clean_rate": round(clean_rate_point, 4),
        "expansion_mid_scenario_summary": {
            "to_500_clean_rows": {
                "estimated_candidate_attempts": est_500["estimated_candidate_attempts"],
                "estimated_rpc_calls": est_500["estimated_rpc_calls"],
                "estimated_wall_time_minutes": est_500["estimated_wall_time_minutes"],
                "estimated_api_cost_usd_bracket_low_high": [
                    est_500["estimated_api_cost_usd_low"],
                    est_500["estimated_api_cost_usd_high"],
                ],
            },
            "to_2000_clean_rows": {
                "estimated_candidate_attempts": est_2000["estimated_candidate_attempts"],
                "estimated_rpc_calls": est_2000["estimated_rpc_calls"],
                "estimated_wall_time_minutes": est_2000["estimated_wall_time_minutes"],
                "estimated_api_cost_usd_bracket_low_high": [
                    est_2000["estimated_api_cost_usd_low"],
                    est_2000["estimated_api_cost_usd_high"],
                ],
            },
        },
        "pricing_assumptions_usd_per_million_calls": {
            "low": PRICE_LOW_PER_M,
            "mid": PRICE_MID_PER_M,
            "high": PRICE_HIGH_PER_M,
            "note": "Replace with invoice/CU terms from your RPC vendor; high tier triggers $5 confirmation gate.",
        },
        "delay_assumption_seconds_per_rpc": DELAY_ASSUMED_S,
        "wall_time_confirmation_minutes_threshold": WALL_TIME_CONFIRM_MIN,
        "api_cost_confirmation_usd_threshold": API_COST_CONFIRM_USD,
        "requires_user_confirmation": bool(ask_time or ask_cost),
        "requires_user_confirmation_reasons": [
            *(["estimated_wall_time_exceeds_20min_mid_assumption"] if ask_time else []),
            *(["estimated_api_cost_high_tier_exceeds_5usd"] if ask_cost else []),
        ],
        "max_loss_if_failed_usd": max_loss_usd,
        "max_loss_note": (
            "Upper bound API spend if pessimistic clean rate holds and all attempts bill at high $/M; "
            "does not include engineering time or repeated batch overhead."
        ),
        "stop_conditions": stop_conditions,
        "training_gate_note": training_note,
        "go_no_go": go_no_go,
        "go_no_go_explanation": (
            "Preflight only — never auto-run. Rows>=100 satisfied for at_cap scope. "
            "Mid assumptions imply >20min wall and/or high-tier $/M may exceed policy — force user confirmation."
            if go_no_go == "ASK_USER_CONFIRMATION_BEFORE_ANY_RUN"
            else "Scope meets row floor; cost/time are estimates — confirm with provider and charter before V3S-2 execution patch."
        ),
        "final_qa_md": [],  # filled below
    }

    qa = [
        (
            f"**最值得扩的单一 scope？** 冻结为 **notional_bucket / at_cap**（`selected_notional_weth == {cap:g}`），"
            "当前在 V3R-29B 中共 **{0}** 行（唯一同时满足 V3S-2「>=100」且与 capped-quote 策略一致的自然切片）。"
        ).format(n),
        f"**当前样本：** scoped **{n}** 行，**{pools}** pools，**{tokens}** tokens，5m 阳性率 **{pr5}**。",
        (
            f"**扩到 500 clean（mid clean-rate {clean_rate_point:.3f}）：** 约 **{est_500['estimated_candidate_attempts']}** 次候选尝试，"
            f"**~{est_500['estimated_rpc_calls']}** RPC， wall **~{wall_500} min**（{DELAY_ASSUMED_S}s/RPC 粗算），"
            f"费用约 **${est_500['estimated_api_cost_usd_low']:.2f}–${est_500['estimated_api_cost_usd_high']:.2f}**（$/M 见 JSON）。"
        ),
        (
            f"**扩到 2000 clean（mid）：** 约 **{est_2000['estimated_candidate_attempts']}** 次尝试，**~{est_2000['estimated_rpc_calls']}** RPC，"
            f"wall **~{wall_2000} min**，费用约 **${est_2000['estimated_api_cost_usd_low']:.2f}–${est_2000['estimated_api_cost_usd_high']:.2f}**。"
        ),
        (
            f"**最坏浪费（悲观 clean-rate + high $/M）：** 仍由 **~{est_2000_pess['estimated_rpc_calls']}** RPC 量级封顶，"
            f"上界约 **${max_loss_usd}**（仅 API 粗算，不含人力/重试）。"
        ),
        (
            "**是否值得继续？** 仅当接受 **5m-primary、显式 notional-at-cap 战场** 且愿意批处理 RPC/预算时，**理论上可进入**下一补丁（真正扩容仍禁止在本轮执行）。"
            "全样本仍 **<500**，**不允许训练**。"
        ),
        (
            "**是否需要用户确认？** "
            + (
                "**是** — mid 假设下 wall time 可能超过 20min，且在高 $/M 假设下费用需对照账单；禁止无人值守直接跑。"
                if (ask_time or ask_cost)
                else "**建议仍确认** — 单价与 CU 以供应商为准。"
            )
        ),
    ]
    payload["final_qa_md"] = qa

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")

    md_lines = [
        "# Run-004 V3S-2 — Scoped 5m expansion cost preflight",
        "",
        "**Static preflight only.** No quote, no training, no expansion, no chain scan, no large CSV.",
        "",
        f"**Frozen scope:** `{selected_scope['scope_type']}` / `{selected_scope['scope_key']}` — {selected_scope['definition']}",
        "",
        "## Why this scope",
        "",
        why_scope,
        "",
        "## Current observability (V3R-29B rows, scoped)",
        "",
        f"- rows: **{n}**, pools: **{pools}**, tokens: **{tokens}**",
        f"- 5m positive rate: **{pr5}**",
        f"- 30m positive rate (diagnostic): **{pr30}**",
        f"- 30m decay note: {dw or 'none'}",
        "",
        "## Cost model (from V3R-29B marginal benchmark)",
        "",
        f"- `newly_clean / newly_attempted` = **{NEWLY_CLEAN_V29B} / {NEWLY_ATTEMPTED_V29B}** ⇒ clean rate **~{clean_rate_point:.3f}** (range **{clean_rate_low:.3f}–{clean_rate_high:.3f}**)",
        f"- `rpc_used / attempts` = **{RPC_USED_V29B} / {NEWLY_ATTEMPTED_V29B}** ⇒ **~{rpc_per_attempt:.2f}** RPC per candidate attempt",
        f"- Wall time model: **{DELAY_ASSUMED_S}s** × RPC (all-in cushion; replace with measured p95)",
        f"- API $: **${PRICE_LOW_PER_M}–${PRICE_HIGH_PER_M}** per 1e6 calls (confirm with invoice)",
        "",
        "## Targets",
        "",
        "### 500 clean rows (tiny scoped model feasibility — **not** full backtest)",
        "",
        json.dumps(est_500, indent=2),
        "",
        "Pessimistic clean rate:",
        "",
        json.dumps(est_500_pess, indent=2),
        "",
        "### 2000 clean rows (scoped **backtest** discussion tier)",
        "",
        json.dumps(est_2000, indent=2),
        "",
        "Pessimistic clean rate:",
        "",
        json.dumps(est_2000_pess, indent=2),
        "",
        "## Gates",
        "",
        training_note,
        "",
        f"- `go_no_go`: **`{go_no_go}`**",
        f"- `requires_user_confirmation`: **{payload['requires_user_confirmation']}** ({', '.join(payload['requires_user_confirmation_reasons']) or 'policy'})",
        f"- `max_loss_if_failed_usd` (upper-ish): **${max_loss_usd}**",
        "",
        "## Stop conditions",
        "",
        *[f"- {s}" for s in stop_conditions],
        "",
        "## Final answers",
        "",
        *[f"{i}. {t}" for i, t in enumerate(qa, 1)],
        "",
    ]
    OUT_MD.write_text("\n".join(md_lines), encoding="utf-8")

    print("Wrote", OUT_JSON.relative_to(ROOT))
    print("Wrote", OUT_MD.relative_to(ROOT))
    print("go_no_go:", go_no_go)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
