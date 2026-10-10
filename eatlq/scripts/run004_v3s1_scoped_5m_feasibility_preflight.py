"""Run-004 V3S-1 — scoped 5m micro-alpha feasibility preflight (static only).

Reads V3R-29B / V3R-29 rows plus V3R-30 / V3R-31 summaries. No training, no RPC,
no expansion, no large CSV output. Emits JSON + markdown feasibility reports.

Primary target: 5m. 30m: decay/risk diagnostic only (not a hard gate).
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ROWS_29B = ROOT / "mvp/diagnostics/run004_v3r29b_small_timestamp_dataset_rebalance_rows.csv"
ROWS_29 = ROOT / "mvp/diagnostics/run004_v3r29_small_timestamp_dataset_expansion_rows.csv"
SUM_30 = ROOT / "mvp/diagnostics/run004_v3r30_pre_model_dataset_audit_summary.json"
SUM_31 = ROOT / "mvp/diagnostics/run004_v3r31_very_small_model_dryrun_summary.json"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3s1_scoped_5m_feasibility_summary.json"
OUT_MD = ROOT / "mvp/runs/run-004-v3s1-scoped-5m-feasibility.md"

TIER_INSUFFICIENT = "insufficient_lt_100"
TIER_VERY_WEAK = "very_weak_100_499"
TIER_TINY_MODEL = "tiny_model_500_1999"
TIER_BACKTEST = "backtest_ready_2000_plus"


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _flip(r: dict[str, Any]) -> bool:
    v = r.get("sign_flip_5m_30m", "")
    return str(v).lower() == "true"


def _y5(r: dict[str, Any]) -> int:
    return 1 if _f(r.get("label_return_5m")) > 0 else 0


def _y30(r: dict[str, Any]) -> int:
    return 1 if _f(r.get("label_return_30m")) > 0 else 0


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    return float(statistics.median(vals))


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def capacity_tier(n: int) -> str:
    if n < 100:
        return TIER_INSUFFICIENT
    if n < 500:
        return TIER_VERY_WEAK
    if n < 2000:
        return TIER_TINY_MODEL
    return TIER_BACKTEST


def capacity_msg(n: int) -> str:
    if n < 100:
        return f"n={n}<100: below minimum weak-pilot band; severe overfit / no OOS stability."
    if n < 500:
        return (
            f"n={n}: very weak pilot only (V3S-1 gate: >=100). "
            f"Need ~{max(0, 500 - n)} more clean rows for tiny scoped model tier; "
            f"~{max(0, 2000 - n)} for serious scoped backtest."
        )
    if n < 2000:
        return (
            f"n={n}: tiny scoped model / narrow backtest possible. "
            f"~{2000 - n} more rows recommended for robust scoped backtest."
        )
    return f"n={n}: meets backtest_ready tier (>=2000)."


def chrono_stats(sub: list[dict[str, Any]]) -> dict[str, Any]:
    tr = [r for r in sub if r.get("chronological_split") == "train"]
    ho = [r for r in sub if r.get("chronological_split") == "holdout"]
    n_tr, n_ho = len(tr), len(ho)
    pr5_tr = sum(_y5(r) for r in tr) / n_tr if n_tr else None
    pr5_ho = sum(_y5(r) for r in ho) / n_ho if n_ho else None
    pr30_tr = sum(_y30(r) for r in tr) / n_tr if n_tr else None
    pr30_ho = sum(_y30(r) for r in ho) / n_ho if n_ho else None
    drift5 = (
        round(pr5_ho - pr5_tr, 4) if pr5_tr is not None and pr5_ho is not None else None
    )
    drift30 = (
        round(pr30_ho - pr30_tr, 4)
        if pr30_tr is not None and pr30_ho is not None
        else None
    )
    decay_warning = None
    if drift30 is not None and drift5 is not None:
        if drift30 >= 0.12 and drift30 > drift5 + 0.05:
            decay_warning = (
                f"30m holdout positive rate uplift vs train (drift_30m={drift30}) "
                f"materially larger than 5m drift ({drift5}); treat 30m as decay/risk context, not a hard fail."
            )
        elif drift30 <= -0.08:
            decay_warning = (
                f"30m holdout weaker than train (drift_30m={drift30}); monitor holdout quality for 5m-only scope."
            )
    return {
        "chrono_train_rows": n_tr,
        "chrono_holdout_rows": n_ho,
        "chrono_train_positive_rate_5m": round(pr5_tr, 4) if pr5_tr is not None else None,
        "chrono_holdout_positive_rate_5m": round(pr5_ho, 4) if pr5_ho is not None else None,
        "chrono_drift_5m": drift5,
        "chrono_train_positive_rate_30m": round(pr30_tr, 4) if pr30_tr is not None else None,
        "chrono_holdout_positive_rate_30m": round(pr30_ho, 4) if pr30_ho is not None else None,
        "chrono_drift_30m_diagnostic": drift30,
        "decay_risk_note_30m": decay_warning,
    }


def top_share(counter: Counter, n: int) -> float | None:
    if n <= 0 or not counter:
        return None
    return round(counter.most_common(1)[0][1] / n, 4)


def scope_record(scope_type: str, scope_key: str, sub: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(sub)
    pools = {r.get("pool_address", "") for r in sub}
    tokens = {r.get("token_address", "") for r in sub}
    pools.discard("")
    tokens.discard("")
    r5 = [_f(r.get("label_return_5m")) for r in sub]
    r30 = [_f(r.get("label_return_30m")) for r in sub]
    pc = Counter(r.get("pool_address", "") for r in sub)
    tc = Counter(r.get("token_address", "") for r in sub)
    flip_n = sum(1 for r in sub if _flip(r))
    rec: dict[str, Any] = {
        "scope_type": scope_type,
        "scope_key": scope_key,
        "rows": n,
        "pools": len(pools),
        "tokens": len(tokens),
        "positive_rate_5m": round(sum(_y5(r) for r in sub) / n, 4) if n else None,
        "positive_rate_30m": round(sum(_y30(r) for r in sub) / n, 4) if n else None,
        "median_return_5m": round(m, 6) if (m := _median(r5)) is not None else None,
        "median_return_30m": round(m, 6) if (m := _median(r30)) is not None else None,
        "sign_flip_share": round(flip_n / n, 4) if n else None,
        "top_pool_share": top_share(pc, n),
        "top_token_share": top_share(tc, n),
        "capacity_tier": capacity_tier(n),
        "estimated_capacity_warning": capacity_msg(n),
        "enough_for_scoped_pilot": n >= 100,
        "enough_for_tiny_scoped_model": n >= 500,
        "enough_for_scoped_backtest": n >= 2000,
    }
    rec.update(chrono_stats(sub))
    n_ho = rec["chrono_holdout_rows"]
    rec["chronological_oos_within_scope"] = n_ho >= 5
    if n_ho == 0:
        rec["chronological_oos_warning"] = (
            "All rows in this scope are on chronological train (or split missing); "
            "cannot assess time OOS within scope on frozen V3R-29B split."
        )
    elif n_ho < 5:
        rec["chronological_oos_warning"] = (
            f"Only {n_ho} chronological holdout rows in scope; OOS estimates very noisy."
        )
    else:
        rec["chronological_oos_warning"] = None
    return rec


def group_by(rows: list[dict[str, Any]], key_fn) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        k = key_fn(r)
        out.setdefault(k, []).append(r)
    return out


def notional_bucket(r: dict[str, Any], cap: float) -> str:
    v = _f(r.get("selected_notional_weth"))
    if cap > 0 and abs(v - cap) <= 1e-15:
        return "at_cap"
    if v < 1e-10:
        return "dust"
    if v < 1e-5:
        return "sub_1e5"
    return "other_notional"


def hour_bucket(r: dict[str, Any]) -> str:
    ts = int(_f(r.get("entry_timestamp")))
    h = (ts % 86400) // 3600
    q = h // 6
    return f"hour_quadrant_{q}"


def expanding_pr_bucket(rows_sorted: list[dict[str, Any]]) -> dict[int, str]:
    """Assign each row index a low/med/high bucket by expanding 5m positive rate."""
    n = len(rows_sorted)
    exp: list[float] = []
    pos_run = 0
    for i in range(n):
        if i == 0:
            exp.append(0.5)
        else:
            exp.append(pos_run / i)
        pos_run += _y5(rows_sorted[i])
    # tertiles of exp[1:] or full
    body = exp[1:] if n > 1 else [0.5]
    sorted_e = sorted(body)
    lo = sorted_e[len(sorted_e) // 3]
    hi = sorted_e[(2 * len(sorted_e)) // 3]

    def b(x: float) -> str:
        if x < lo:
            return "expanding_pr_low"
        if x < hi:
            return "expanding_pr_mid"
        return "expanding_pr_high"

    idx_to_bucket: dict[int, str] = {}
    for i in range(n):
        idx_to_bucket[i] = b(exp[i])
    return idx_to_bucket


def return_tertile_bins(rows: list[dict[str, Any]]) -> dict[int, str]:
    vals = [(i, _f(r.get("label_return_5m"))) for i, r in enumerate(rows)]
    vals.sort(key=lambda t: t[1])
    n = len(vals)
    if n == 0:
        return {}
    a, b = n // 3, (2 * n) // 3
    order_to_bucket: dict[int, str] = {}
    for rank, (orig_i, _) in enumerate(vals):
        if rank < a:
            order_to_bucket[orig_i] = "ret5m_tertile_low"
        elif rank < b:
            order_to_bucket[orig_i] = "ret5m_tertile_mid"
        else:
            order_to_bucket[orig_i] = "ret5m_tertile_high"
    return order_to_bucket


def analyze_dataset(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    cap = max((_f(r.get("selected_notional_weth")) for r in rows), default=0.0)

    by_fee = group_by(rows, lambda r: str(r.get("fee", "")))
    by_src = group_by(rows, lambda r: str(r.get("source_path", "")))
    by_sf = group_by(rows, lambda r: f"{r.get('source_path','')}|{r.get('fee','')}")
    by_pool = group_by(rows, lambda r: str(r.get("pool_address", "")))
    by_notional = group_by(rows, lambda r: notional_bucket(r, cap))
    by_hour = group_by(rows, hour_bucket)

    rows_chrono = sorted(rows, key=lambda r: int(_f(r.get("entry_timestamp"))))
    idx_to_pr_b = expanding_pr_bucket(rows_chrono)
    inv_pr: dict[str, list[dict[str, Any]]] = {"expanding_pr_low": [], "expanding_pr_mid": [], "expanding_pr_high": []}
    for i, r in enumerate(rows_chrono):
        inv_pr[idx_to_pr_b[i]].append(r)

    ret_bins = return_tertile_bins(rows)
    inv_ret: dict[str, list[dict[str, Any]]] = {
        "ret5m_tertile_low": [],
        "ret5m_tertile_mid": [],
        "ret5m_tertile_high": [],
    }
    for i, r in enumerate(rows):
        inv_ret[ret_bins.get(i, "ret5m_tertile_mid")].append(r)

    scopes: list[dict[str, Any]] = []

    def add_many(st: str, d: dict[str, list[dict[str, Any]]]):
        for k, sub in sorted(d.items(), key=lambda kv: -len(kv[1])):
            scopes.append(scope_record(st, k, sub))

    add_many("fee", by_fee)
    add_many("source", by_src)
    add_many("source_fee", by_sf)
    add_many("pool", by_pool)
    add_many("notional_bucket", by_notional)
    add_many("entry_hour_bucket", by_hour)
    add_many("expanding_5m_positive_rate_bucket", inv_pr)
    add_many("label_return_5m_tertile", inv_ret)

    universe = scope_record("universe_all_rows", name, rows)

    return {
        "dataset_name": name,
        "total_rows": len(rows),
        "notional_cap_observed": cap,
        "scopes_flat": scopes,
        "universe": universe,
        "pool_scopes_min_rows_10": [
            s for s in scopes if s["scope_type"] == "pool" and s["rows"] >= 10
        ],
    }


def pick_candidates(an29b: dict[str, Any]) -> dict[str, Any]:
    """Rank scopes for 5m scoped continuation."""
    flat = an29b["scopes_flat"]
    structural = [
        s
        for s in flat
        if s["scope_type"] in ("source_fee", "fee", "source", "pool")
    ]
    structural.sort(key=lambda s: (-s["rows"], abs(s.get("chrono_drift_5m") or 0)))
    notional = [s for s in flat if s["scope_type"] == "notional_bucket"]
    notional.sort(key=lambda s: -s["rows"])
    with_oos = [s for s in flat if s.get("chrono_holdout_rows", 0) >= 8]
    with_oos.sort(
        key=lambda s: (
            -s["chrono_holdout_rows"],
            -s["rows"],
            abs(s.get("chrono_drift_5m") or 1.0),
        )
    )
    return {
        "ranked_structural": structural[:15],
        "largest_notional_bucket": notional[0] if notional else None,
        "best_chronological_oos_slice": with_oos[0] if with_oos else None,
        "top_candidate_by_rows": structural[0] if structural else None,
    }


def render_md(payload: dict[str, Any]) -> str:
    a29b = payload["analysis_v3r29b"]
    a29 = payload.get("analysis_v3r29")
    dec = payload["final_answers"]
    lines: list[str] = []
    lines.append("# Run-004 V3S-1 — Scoped 5m micro-alpha feasibility preflight")
    lines.append("")
    lines.append("**Static analysis only.** No training, no quote, no expansion, no chain scan. ")
    lines.append("5m is **primary**; 30m is **decay/risk diagnostic** only (not a hard fail). ")
    lines.append("Concentration is **scope definition**, not an automatic failure when scoped explicitly.")
    lines.append("")
    lines.append("## Inputs")
    lines.append("")
    for k, v in payload["inputs"].items():
        lines.append(f"- {k}: `{v}`")
    lines.append("")
    lines.append("## Capacity gates (V3S-1)")
    lines.append("")
    lines.append("- `<100` clean rows in scope: insufficient for weak pilot")
    lines.append("- `>=100`: very weak pilot / documentation only")
    lines.append("- `>=500`: tiny scoped model tier")
    lines.append("- `>=2000`: serious scoped backtest tier")
    lines.append("")
    lines.append("## Universe summary (V3R-29B, rebalanced)")
    lines.append("")
    u = a29b["universe"]
    lines.append(f"- rows: {u['rows']}, pools: {u['pools']}, tokens: {u['tokens']}")
    lines.append(
        f"- positive_rate_5m / 30m: {u['positive_rate_5m']} / {u['positive_rate_30m']}"
    )
    lines.append(
        f"- chronological: train={u['chrono_train_rows']} holdout={u['chrono_holdout_rows']} "
        f"drift_5m={u['chrono_drift_5m']} drift_30m(diagnostic)={u['chrono_drift_30m_diagnostic']}"
    )
    lines.append(f"- sign_flip_share: {u['sign_flip_share']}")
    lines.append(f"- capacity_tier: `{u['capacity_tier']}` — {u['estimated_capacity_warning']}")
    lines.append("")
    if a29:
        u29 = a29["universe"]
        lines.append("## Universe summary (V3R-29 pre-rebalance reference)")
        lines.append("")
        lines.append(f"- rows: {u29['rows']} (reference only; V3R-30 failed stability on this set)")
        lines.append(
            f"- chronological drift_5m: {u29['chrono_drift_5m']}, drift_30m: {u29['chrono_drift_30m_diagnostic']}"
        )
        lines.append("")
    lines.append("## Scope tables (V3R-29B)")
    lines.append("")
    lines.append("Each row: scope_type / scope_key / rows / pools / tokens / pr_5m / pr_30m / medians / flip / chrono / top shares / tier.")
    lines.append("")
    for s in sorted(a29b["scopes_flat"], key=lambda x: (-x["rows"], x["scope_type"], x["scope_key"]))[
        :40
    ]:
        lines.append(
            f"- **{s['scope_type']}** `{s['scope_key'][:64]}{'...' if len(s['scope_key'])>64 else ''}` | "
            f"n={s['rows']} pools={s['pools']} tokens={s['tokens']} | "
            f"pr5={s['positive_rate_5m']} pr30={s['positive_rate_30m']} | "
            f"med5={s['median_return_5m']} med30={s['median_return_30m']} | flip={s['sign_flip_share']} | "
            f"chrono tr/ho={s['chrono_train_rows']}/{s['chrono_holdout_rows']} drift5={s['chrono_drift_5m']} | "
            f"top_pool/top_tok={s['top_pool_share']}/{s['top_token_share']} | **{s['capacity_tier']}**"
        )
    if len(a29b["scopes_flat"]) > 40:
        lines.append(f"- … plus {len(a29b['scopes_flat']) - 40} more scopes in JSON.")
    lines.append("")
    lines.append("## Final answers")
    lines.append("")
    for i, text in enumerate(dec["qa"], 1):
        lines.append(f"{i}. {text}")
    lines.append("")
    lines.append("## Forbidden / not implied")
    lines.append("")
    lines.append("- No generalizable dual-horizon model claim (V3R-31 remains failed on that bar).")
    lines.append("- No mainline / live / promote / production model artifact from this preflight.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    if not ROWS_29B.exists():
        print(f"missing {ROWS_29B}", file=sys.stderr)
        return 2
    rows_b = load_rows(ROWS_29B)
    rows_29 = load_rows(ROWS_29) if ROWS_29.exists() else []

    summary_30 = json.loads(SUM_30.read_text(encoding="utf-8")) if SUM_30.exists() else {}
    summary_31 = json.loads(SUM_31.read_text(encoding="utf-8")) if SUM_31.exists() else {}

    an_b = analyze_dataset(rows_b, "v3r29b_rebalance")
    an_29 = analyze_dataset(rows_29, "v3r29_expansion") if rows_29 else None

    candidates = pick_candidates(an_b)
    best_rows = candidates.get("top_candidate_by_rows")
    best_oos = candidates.get("best_chronological_oos_slice")
    notional_top = candidates.get("largest_notional_bucket")

    n_b = an_b["universe"]["rows"]

    need_tiny = max(0, 500 - n_b)
    need_bt = max(0, 2000 - n_b)

    # Hypothesis worth pursuing only with explicit scope + expansion; inner scopes mostly <100 rows.
    worth_continue = n_b >= 100

    enough_now = n_b >= 500
    rpc_worth = (
        "边际、取决于 charter：当前 V3R-29B 全宇宙仅 151 行干净样本，距 tiny-model（500）还差约 "
        f"{need_tiny} 行，距严肃 scoped backtest（2000）还差约 {need_bt} 行。"
        "仅在 V3S-2 中**冻结** fee/source/notional 等 scope、预算 RPC、并写明 clean 接受规则时，扩容才有明确 ROI；"
        "否则多数自然切片在**固定** chronological split 下 holdout=0，时间维 OOS 近乎幻象。"
    )

    next_step = (
        "优先 **V3S-2**（研究向）显式 scope 下的 scoped data expansion；若无法论证 scope ROI，则**暂停**泛化 V3 model 路线。"
        if not enough_now
        else "进入极窄的 scoped 建模宪章（仍为 research-only）。"
    )

    br = best_rows or {}
    bo = best_oos or {}
    nt = notional_top or {}

    src_n = int(br.get("rows") or 0) if br else 0
    sk_br = str(br.get("scope_key") or "") if br else ""
    oos_key = str(bo.get("scope_key") or "") if bo else ""
    oos_type = str(bo.get("scope_type") or "") if bo else ""
    oos_ho = int(bo.get("chrono_holdout_rows") or 0) if bo else 0

    qa = [
        (
            "**是否存在值得继续的 5m scoped strategy 战场？** "
            "仅作为**假设**值得：全量 V3R-29B 宇宙满足 `>=100` 行的极弱 pilot 下限，但**任一更窄的** "
            "fee/source/pool 切片大多 `<100` 行，无法独立承担 scoped pilot；需在 V3S-2 中把「战场」"
            "写成显式 scope 并扩容 clean rows。"
        ),
        (
            "**若以「行数最大的可命名切片」粗排：** `notional_bucket` **at_cap** "
            f"（n={nt.get('rows', 'n/a')}）。"
            "**若以 fee/source/pool 族单列行数：** "
            f"当前最大为 **{sk_br.split('/')[-1] if sk_br else 'n/a'}**（scope_type={br.get('scope_type') if br else 'n/a'}, n={src_n}）。"
            "**若以「chronological OOS within scope」（冻结 split）：** 优先参考 "
            f"`best_chronological_oos_slice` → **{oos_type}** `{oos_key}` "
            f"（chrono_holdout_rows={oos_ho}）。"
            "注意：大行数 source 切片常出现 **chrono_holdout=0**，与「时间 OOS」验证重点冲突。"
        ),
        (
            f"**现有样本够不够？** **不够**：全宇宙 n={n_b}<500（tiny scoped model），<{2000}（serious backtest）；"
            "绝大多数子 scope `<100`，低于你方 very weak 下限。"
        ),
        (
            f"**需扩到多少 clean rows？** 再需约 **{need_tiny}** 行达 500（tiny model）、约 **{need_bt}** 行达 2000（scoped backtest）；"
            "且应在**同一显式 scope**内积累，而不是混用未声明的全局。"
        ),
        f"**RPC/API 扩容是否值得？** {rpc_worth}",
        f"**下一步：** {next_step}",
    ]

    if enough_now:
        qa = ["(unexpected) universe already >=500 rows — re-run with updated data"]

    payload: dict[str, Any] = {
        "run": "run004_v3s1_scoped_5m_feasibility_preflight",
        "research_only": True,
        "primary_target": "5m",
        "horizon_30m_role": "decay_risk_diagnostic_only",
        "validation_focus": "chronological_oos_within_explicit_scope",
        "generator_script": "scripts/run004_v3s1_scoped_5m_feasibility_preflight.py",
        "inputs": {
            "v3r29b_rows": str(ROWS_29B.relative_to(ROOT)),
            "v3r29_rows": str(ROWS_29.relative_to(ROOT)) if ROWS_29.exists() else None,
            "v3r30_summary": str(SUM_30.relative_to(ROOT)) if SUM_30.exists() else None,
            "v3r31_summary": str(SUM_31.relative_to(ROOT)) if SUM_31.exists() else None,
        },
        "v3r31_context": {
            "decision_label": summary_31.get("decision_label"),
            "note": "V3R-31 failed general dual-horizon model; V3S-1 reframes to explicit scope + chronological OOS.",
        },
        "v3r30_context": {
            "input_rows_reference": summary_30.get("input_rows"),
            "note": "V3R-29 252-row set failed target stability pre-rebalance; 29B repairs bias at 151 rows.",
        },
        "analysis_v3r29b": an_b,
        "analysis_v3r29": an_29,
        "scope_candidates_ranked": candidates,
        "final_answers": {
            "worth_continuing_scoped_5m_hypothesis": worth_continue,
            "enough_samples_for_tiny_model_now": enough_now,
            "clean_rows_needed_to_500": need_tiny,
            "clean_rows_needed_to_2000": need_bt,
            "rpc_expansion_cost_judgement": rpc_worth,
            "recommended_next_patch": "V3S-2_scoped_data_expansion" if need_tiny > 0 else "scoped_modeling_charter_only",
            "qa": qa,
        },
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    OUT_MD.write_text(render_md(payload), encoding="utf-8")

    print("Wrote", OUT_JSON.relative_to(ROOT))
    print("Wrote", OUT_MD.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
