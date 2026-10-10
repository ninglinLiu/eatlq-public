#!/usr/bin/env python3
"""
SOP promotion inventory — read-only consolidation of shadow / dry-run / forward artifacts.

Does NOT train, promote, live-trade, write signals, or touch private keys.
Outputs: mvp/audits/sop_promotion_inventory/{csv,json,md}
"""
from __future__ import annotations

import csv
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "mvp" / "audits" / "sop_promotion_inventory"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Sprint076-aligned concentration gates
POOL_CONC_MAX = 0.50
SENDER_CONC_MAX = 0.70
TOKEN_CONC_MAX = 0.50
PF_MIN_REVIEW = 1.2
WR_MIN_REVIEW = 0.50
FORWARD_MATURE_N = 100
PLUMBING_N = 20
EARLY_HINT_N = 50


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def pf_from_returns(rets: list[float]) -> float | None:
    wins = [r for r in rets if r > 0]
    losses = [abs(r) for r in rets if r < 0]
    if not losses:
        return None
    return round(sum(wins) / sum(losses), 6)


def max_drawdown_from_returns(rets: list[float], stake: float = 10.0) -> float | None:
    if not rets:
        return None
    eq = 100.0
    peak = eq
    max_dd = 0.0
    for r in rets:
        eq += r * stake
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return round(max_dd, 6)


def top_share(values: list[str]) -> float | None:
    if not values:
        return None
    c = Counter(values)
    return round(max(c.values()) / len(values), 6)


def forward_metrics_from_trades(trades: list[dict]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {
            "forward_n": 0,
            "closed_trades": 0,
            "pf": None,
            "win_rate": None,
            "mean_return": None,
            "max_drawdown": None,
            "top_pool_share": None,
            "top_sender_share": None,
            "top_token_share": None,
        }
    rets = [float(t.get("ret_ag") or t.get("net_return_30m_ag") or 0) for t in trades]
    wins = [r for r in rets if r > 0]
    pools = [str(t.get("pool") or "") for t in trades if t.get("pool")]
    senders = [
        str(t.get("dominant_sender") or t.get("sender") or "").lower()
        for t in trades
        if t.get("dominant_sender") or t.get("sender")
    ]
    tokens = [str(t.get("token_other") or t.get("token") or "") for t in trades if t.get("token_other") or t.get("token")]
    return {
        "forward_n": n,
        "closed_trades": n,
        "pf": pf_from_returns(rets),
        "win_rate": round(len(wins) / n, 6),
        "mean_return": round(statistics.mean(rets), 6),
        "max_drawdown": max_drawdown_from_returns(rets),
        "top_pool_share": top_share(pools),
        "top_sender_share": top_share(senders) if senders else None,
        "top_token_share": top_share(tokens) if tokens else None,
    }


def metrics_from_paper_csv(path: Path, ret_col: str = "net_return_30m_ag") -> dict[str, Any]:
    if not path.exists():
        return forward_metrics_from_trades([])
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("entry_quote_ok", "True").lower() not in ("true", "1", "yes"):
                continue
            try:
                ret = float(row.get(ret_col) or "")
            except (TypeError, ValueError):
                continue
            if row.get(f"exit_{ret_col.split('_')[1]}_ok", "True").lower() in ("false", "0", ""):
                # fallback: require 30m ok column when present
                if row.get("exit_30m_ok", "").lower() == "false":
                    continue
            rows.append(
                {
                    "pool": row.get("pool"),
                    "token_other": row.get("token_other"),
                    "dominant_sender": row.get("dominant_sender"),
                    "ret_ag": ret,
                }
            )
    return forward_metrics_from_trades(rows)


def assign_promotion_flag(row: dict[str, Any]) -> str:
    mode = row.get("source_mode") or ""
    fn = int(row.get("forward_n") or 0)
    replay_smoke = row.get("whether_replay_smoke") in (True, "true", "True")
    forward_only = row.get("forward_only") in (True, "true", "True")

    if mode in ("historical_replay", "historical_label_backtest") or replay_smoke:
        return "REJECT_NOT_FORWARD"
    if row.get("reject_concentrated"):
        return "REJECT_CONCENTRATED"
    # Paper shadow: labeled live-pool evidence — never counts as forward-only mature
    if mode == "paper_shadow":
        if row.get("whether_historical_blended") in (True, "true", "True"):
            return "REJECT_NOT_FORWARD"
        return "EARLY_HINT" if fn >= PLUMBING_N else "PLUMBING_ONLY"
    if not forward_only:
        return "REJECT_NOT_FORWARD"
    if fn < PLUMBING_N:
        return "PLUMBING_ONLY"
    if fn < FORWARD_MATURE_N:
        return "EARLY_HINT"
    if row.get("human_live_review_eligible"):
        return "HUMAN_LIVE_REVIEW_CANDIDATE"
    if row.get("forward_mature_metrics_ok"):
        return "FORWARD_MATURE"
    return "EARLY_HINT"


def concentration_reject(m: dict[str, Any], sid: str = "", desc: str = "") -> bool:
    blob = f"{sid} {desc}".lower()
    if "0x6ff5" in blob or "is_0x6ff5" in blob or "_6ff5" in blob or blob.endswith("6ff5"):
        return True
    for key, thr in (
        ("top_pool_share", POOL_CONC_MAX),
        ("top_sender_share", SENDER_CONC_MAX),
        ("top_token_share", TOKEN_CONC_MAX),
    ):
        v = m.get(key)
        if v is not None and float(v) > thr:
            return True
    return False


def load_execution_gates() -> dict[str, Any]:
    gates: dict[str, Any] = {
        "sell_path_ok": None,
        "panic_gate_ok": None,
        "allowance_gate_ok": None,
        "private_key_gate_ok": None,
        "true_live_forward_executor_n": None,
        "canary_decision": None,
    }
    n_path = REPO / "mvp" / "audits" / "execution_readiness" / "n_accounting.json"
    if n_path.exists():
        n_doc = json.loads(n_path.read_text())
        gates["true_live_forward_executor_n"] = n_doc.get("true_live_forward_executor_n")
    canary_path = REPO / "mvp" / "audits" / "execution_readiness" / "canary_decision.json"
    if canary_path.exists():
        c = json.loads(canary_path.read_text())
        gates["canary_decision"] = c.get("canary_decision")
        gr = c.get("gate_results") or {}
        gates["sell_path_ok"] = (gr.get("SELL_CLOSE_PASS") or {}).get("status") == "PASS"
        gates["panic_gate_ok"] = (gr.get("SAFETY_PASS") or {}).get("status") == "PASS"
    # Human blockers from sprint061 — not auto-closed
    gates["allowance_gate_ok"] = False
    gates["private_key_gate_ok"] = False  # must remain human session-only
    return gates


def load_sprint076_rows() -> list[dict]:
    p = REPO / "mvp" / "audits" / "sprint076_forward_race" / "sprint076_forward_leaderboard.json"
    if not p.exists():
        return []
    bundle = json.loads(p.read_text())
    rows = []
    for r in bundle.get("views", {}).get("forward_only", []):
        rows.append(
            {
                "strategy_key": r["strategy_id"],
                "display_name": r["strategy_id"],
                "lifecycle": "active",
                "source_mode": "forward_dryrun",
                "artifact_paths": "logs/sprint075|077/*_state.json; mvp/audits/sprint076_forward_race/",
                "forward_n": r.get("forward_n"),
                "closed_trades": r.get("forward_n"),
                "pf": r.get("pf"),
                "win_rate": r.get("wr"),
                "mean_return": r.get("ev"),
                "max_drawdown": r.get("max_drawdown"),
                "top_pool_share": r.get("top_pool_share"),
                "top_sender_share": r.get("top_sender_share"),
                "top_token_share": None,
                "whether_historical_blended": False,
                "whether_replay_smoke": False,
                "family": r.get("family"),
                "sop_concentration_flags": r.get("sop_concentration_flags") or [],
                "quote_fail_rate": r.get("quote_fail_rate"),
                "promotion_allowed_forward": r.get("promotion_allowed_forward"),
            }
        )
    return rows


def load_sprint068_alphas() -> list[dict]:
    reg_path = REPO / "mvp" / "diagnostics" / "sprint068" / "alpha_registry.json"
    if not reg_path.exists():
        return []
    reg = json.loads(reg_path.read_text())
    rows = []
    for a in reg.get("alphas", []):
        aid = a["alpha_id"]
        csv_rel = a.get("output_csv", "")
        csv_path = REPO / csv_rel if csv_rel else None
        if aid == "alpha_001":
            csv_path = REPO / "logs" / "sprint055_paper_shadow.csv"
        m = metrics_from_paper_csv(csv_path) if csv_path else forward_metrics_from_trades([])
        cm = a.get("current_metrics") or {}
        if cm and (m.get("forward_n") or 0) == 0:
            m = {
                "forward_n": cm.get("labeled_30m") or cm.get("signal_count"),
                "closed_trades": cm.get("labeled_30m") or cm.get("signal_count"),
                "pf": cm.get("profit_factor_30m"),
                "win_rate": cm.get("win_rate_30m"),
                "mean_return": cm.get("mean_return_30m"),
                "max_drawdown": None,
                "top_pool_share": cm.get("top_pool_share"),
                "top_sender_share": None,
                "top_token_share": cm.get("top_token_share"),
            }
        rows.append(
            {
                "strategy_key": aid,
                "display_name": a.get("name", aid),
                "lifecycle": a.get("status", "UNKNOWN").lower(),
                "source_mode": "paper_shadow",
                "artifact_paths": f"{csv_rel}; mvp/diagnostics/sprint068/{aid}_state.json",
                **m,
                "whether_historical_blended": False,
                "whether_replay_smoke": False,
                "alpha_type": a.get("alpha_type"),
            }
        )
    return rows


def load_sprint069_dryrun() -> list[dict]:
    st_path = REPO / "mvp" / "audits" / "sprint069_forward_dryrun" / "forward_dryrun_state.json"
    if not st_path.exists():
        return []
    try:
        st = json.loads(st_path.read_text())
    except json.JSONDecodeError:
        return [
            {
                "strategy_key": "sprint069_forward_dryrun_state",
                "display_name": "Sprint069 forward dry-run ledger (parse error)",
                "lifecycle": "stale",
                "source_mode": "forward_dryrun",
                "artifact_paths": str(st_path),
                "forward_n": 0,
                "closed_trades": 0,
                "pf": None,
                "win_rate": None,
                "mean_return": None,
                "max_drawdown": None,
                "top_pool_share": None,
                "top_sender_share": None,
                "top_token_share": None,
                "whether_historical_blended": False,
                "whether_replay_smoke": False,
                "note": "forward_dryrun_state.json failed JSON parse; use sprint076 state files",
            }
        ]
    rows = []
    for sid, acct in (st.get("accounts") or {}).items():
        trades = acct.get("closed_trades") or []
        m = forward_metrics_from_trades(trades)
        rows.append(
            {
                "strategy_key": sid,
                "display_name": sid,
                "lifecycle": st.get("status", "RUNNING").lower(),
                "source_mode": "forward_dryrun",
                "artifact_paths": "mvp/audits/sprint069_forward_dryrun/forward_dryrun_state.json",
                **m,
                "whether_historical_blended": False,
                "whether_replay_smoke": False,
            }
        )
    return rows


def load_g3_shadow() -> list[dict]:
    rows = []
    diag = REPO / "mvp" / "diagnostics" / "run004_patch_g3_4_shadow_observation_start.json"
    if diag.exists():
        d = json.loads(diag.read_text())
        rows.append(
            {
                "strategy_key": "run004_g3_4_pilot",
                "display_name": "Run004 G3.4 shadow pilot (holdout CSV replay)",
                "lifecycle": "historical",
                "source_mode": "historical_replay",
                "artifact_paths": str(d),
                "forward_n": 0,
                "closed_trades": (d.get("task_b_manual_pilot_start") or {})
                .get("metrics_from_delegate_report", {})
                .get("selected_rows"),
                "pf": None,
                "win_rate": None,
                "mean_return": None,
                "max_drawdown": None,
                "top_pool_share": None,
                "top_sender_share": None,
                "top_token_share": None,
                "whether_historical_blended": True,
                "whether_replay_smoke": True,
                "note": d.get("task_b_manual_pilot_start", {}).get("input_note"),
            }
        )
    g2 = REPO / "mvp" / "diagnostics" / "run004_g2_shadow_observation_summary_g34_start.json"
    if g2.exists():
        rows.append(
            {
                "strategy_key": "run004_g2_shadow",
                "display_name": "Run004 G2 score-only shadow",
                "lifecycle": "historical",
                "source_mode": "historical_replay",
                "artifact_paths": str(g2),
                "forward_n": 0,
                "closed_trades": None,
                "pf": None,
                "win_rate": None,
                "mean_return": None,
                "max_drawdown": None,
                "top_pool_share": None,
                "top_sender_share": None,
                "top_token_share": None,
                "whether_historical_blended": True,
                "whether_replay_smoke": True,
            }
        )
    return rows


def load_sprint072_historical() -> list[dict]:
    p = REPO / "mvp" / "audits" / "sprint072_alpha_waterfall" / "exit_horizon_sweep_results.json"
    if not p.exists():
        return []
    doc = json.loads(p.read_text())
    rows = []
    items: list[tuple[str, dict]] = []
    if isinstance(doc, list):
        for block in doc:
            if isinstance(block, dict):
                items.append((block.get("label") or "unknown", block))
    elif isinstance(doc, dict):
        raw = doc.get("strategies") or doc.get("results") or {}
        if isinstance(raw, dict):
            items = [(k, v) for k, v in raw.items() if isinstance(v, dict)]
    for sid, block in items:
        pools = block.get("pool_stats") or {}
        pool_ns = [v.get("n", 0) for v in pools.values() if isinstance(v, dict)]
        top_pool = round(max(pool_ns) / sum(pool_ns), 6) if pool_ns and sum(pool_ns) else None
        rows.append(
            {
                "strategy_key": sid,
                "display_name": sid,
                "lifecycle": "historical",
                "source_mode": "historical_replay",
                "artifact_paths": "mvp/audits/sprint072_alpha_waterfall/exit_horizon_sweep_results.json",
                "forward_n": 0,
                "closed_trades": block.get("n_trades") or block.get("n") or block.get("trade_count"),
                "pf": block.get("profit_factor") or block.get("pf") or block.get("PF"),
                "win_rate": block.get("win_rate") or block.get("wr"),
                "mean_return": block.get("mean_ret") or block.get("mean_return"),
                "max_drawdown": block.get("max_dd") or block.get("max_drawdown"),
                "top_pool_share": top_pool,
                "top_sender_share": None,
                "top_token_share": None,
                "whether_historical_blended": True,
                "whether_replay_smoke": False,
            }
        )
    return rows


def load_live_market_shadow() -> list[dict]:
    gates = load_execution_gates()
    n = gates.get("true_live_forward_executor_n") or 0
    return [
        {
            "strategy_key": "executor_true_live_forward",
            "display_name": "Executor dry-run (true live-forward post-cutoff)",
            "lifecycle": "active",
            "source_mode": "live_market_shadow",
            "artifact_paths": "mvp/audits/execution_readiness/n_accounting.json; exec:trades",
            "forward_n": n,
            "closed_trades": n,
            "pf": None,
            "win_rate": None,
            "mean_return": None,
            "max_drawdown": None,
            "top_pool_share": None,
            "top_sender_share": None,
            "top_token_share": None,
            "whether_historical_blended": False,
            "whether_replay_smoke": False,
        }
    ]


def load_sprint067() -> list[dict]:
    p = REPO / "mvp" / "audits" / "sprint067_paper_shadow_counterfactual" / "sprint067_counterfactual_results.json"
    if not p.exists():
        return []
    doc = json.loads(p.read_text())
    rows = []
    for sid, block in (doc.get("strategies") or {}).items():
        rows.append(
            {
                "strategy_key": f"sprint067_{sid}",
                "display_name": sid,
                "lifecycle": "historical",
                "source_mode": "paper_shadow",
                "artifact_paths": str(p),
                "forward_n": 0,
                "closed_trades": block.get("trade_count"),
                "pf": block.get("PF"),
                "win_rate": block.get("win_rate"),
                "mean_return": block.get("mean_return_pct"),
                "max_drawdown": block.get("max_drawdown_pct"),
                "top_pool_share": None,
                "top_sender_share": None,
                "top_token_share": None,
                "whether_historical_blended": True,
                "whether_replay_smoke": False,
                "note": block.get("note"),
            }
        )
    return rows


def enrich_row(row: dict[str, Any], exec_gates: dict[str, Any]) -> dict[str, Any]:
    m = {k: row.get(k) for k in (
        "forward_n", "pf", "win_rate", "mean_return", "max_drawdown",
        "top_pool_share", "top_sender_share", "top_token_share",
    )}
    sid = str(row.get("strategy_key", ""))
    desc = str(row.get("display_name", ""))
    row["reject_concentrated"] = concentration_reject(m, sid, desc)
    fn = int(row.get("forward_n") or 0)
    mode = row.get("source_mode")
    forward_only = mode in ("forward_dryrun", "live_market_shadow") and not row.get("whether_historical_blended")
    row["forward_only"] = forward_only
    row["forward_mature_metrics_ok"] = bool(
        forward_only
        and fn >= FORWARD_MATURE_N
        and (row.get("pf") or 0) >= PF_MIN_REVIEW
        and (row.get("win_rate") or 0) >= WR_MIN_REVIEW
        and not row["reject_concentrated"]
    )
    row["human_live_review_eligible"] = (
        row["forward_mature_metrics_ok"]
        and exec_gates.get("sell_path_ok")
        and exec_gates.get("panic_gate_ok") is not False
        and exec_gates.get("allowance_gate_ok")
        and exec_gates.get("private_key_gate_ok")
    )
    row["promotion_flag"] = assign_promotion_flag(row)
    return row


def main() -> None:
    exec_gates = load_execution_gates()
    seen: set[str] = set()
    inventory: list[dict[str, Any]] = []

    def add_batch(batch: list[dict], source_tag: str) -> None:
        for r in batch:
            key = f"{r['strategy_key']}::{r['source_mode']}"
            if key in seen:
                continue
            seen.add(key)
            r["inventory_source"] = source_tag
            inventory.append(enrich_row(r, exec_gates))

    add_batch(load_sprint076_rows(), "sprint076_leaderboard")
    add_batch(load_sprint068_alphas(), "sprint068_registry")
    add_batch(load_sprint069_dryrun(), "sprint069_forward_dryrun")
    add_batch(load_g3_shadow(), "g3_shadow")
    add_batch(load_sprint072_historical(), "sprint072_waterfall")
    add_batch(load_sprint067(), "sprint067_counterfactual")
    add_batch(load_live_market_shadow(), "execution_readiness")

    # State-file-only strategies not in sprint076 watchlist
    for state_dir, harness in (
        (REPO / "logs" / "sprint075", "sprint075"),
        (REPO / "logs" / "sprint077", "sprint077"),
        (REPO / "logs" / "sprint079", "sprint079"),
    ):
        if not state_dir.exists():
            continue
        for sp in state_dir.glob("*_state.json"):
            sid = sp.stem.replace("_state", "")
            key = f"{sid}::forward_dryrun"
            if key in seen:
                continue
            try:
                st = json.loads(sp.read_text())
            except Exception:
                continue
            trades = st.get("trades") or []
            m = forward_metrics_from_trades(trades)
            row = {
                "strategy_key": sid,
                "display_name": sid,
                "lifecycle": "active",
                "source_mode": "forward_dryrun",
                "artifact_paths": str(sp.relative_to(REPO)),
                **m,
                "whether_historical_blended": False,
                "whether_replay_smoke": False,
                "harness": harness,
            }
            seen.add(key)
            row["inventory_source"] = f"{harness}_state_orphan"
            inventory.append(enrich_row(row, exec_gates))

    inventory.sort(key=lambda r: (r["source_mode"], r["strategy_key"]))

    csv_fields = [
        "strategy_key",
        "display_name",
        "lifecycle",
        "source_mode",
        "promotion_flag",
        "forward_n",
        "closed_trades",
        "pf",
        "win_rate",
        "mean_return",
        "max_drawdown",
        "top_pool_share",
        "top_sender_share",
        "top_token_share",
        "whether_historical_blended",
        "whether_replay_smoke",
        "forward_only",
        "reject_concentrated",
        "forward_mature_metrics_ok",
        "human_live_review_eligible",
        "inventory_source",
        "artifact_paths",
    ]
    csv_path = OUT_DIR / "sop_promotion_inventory.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(inventory)

    forward_ge_100 = [r for r in inventory if r.get("forward_only") and int(r.get("forward_n") or 0) >= 100]
    candidates_hlr = [r for r in inventory if r.get("promotion_flag") == "HUMAN_LIVE_REVIEW_CANDIDATE"]
    canary_candidates = [
        r
        for r in inventory
        if r.get("source_mode") == "live_market_shadow"
        and int(r.get("forward_n") or 0) >= 25
        and not r.get("reject_concentrated")
    ]

    flags = {
        "generated_at_utc": utc_now_iso(),
        "safety": {
            "training": False,
            "promote": False,
            "live": False,
            "signals_written": False,
            "private_key_touched": False,
        },
        "inventory_row_count": len(inventory),
        "source_mode_counts": dict(Counter(r["source_mode"] for r in inventory)),
        "promotion_flag_counts": dict(Counter(r["promotion_flag"] for r in inventory)),
        "forward_n_ge_100_exists": len(forward_ge_100) > 0,
        "forward_n_ge_100_strategies": [
            {"strategy_key": r["strategy_key"], "forward_n": r["forward_n"], "source_mode": r["source_mode"]}
            for r in forward_ge_100
        ],
        "max_forward_only_n": max(
            (int(r.get("forward_n") or 0) for r in inventory if r.get("forward_only")),
            default=0,
        ),
        "human_live_review_candidates": [
            {"strategy_key": r["strategy_key"], "forward_n": r["forward_n"]} for r in candidates_hlr
        ],
        "canary_candidate_exists": len(canary_candidates) > 0,
        "canary_candidates": canary_candidates,
        "execution_gates": exec_gates,
        "decision": {
            "has_human_live_review_candidate": len(candidates_hlr) > 0,
            "has_canary_candidate": len(canary_candidates) > 0,
            "canary_decision_from_artifact": exec_gates.get("canary_decision"),
            "blocked_reasons": [],
        },
    }

    if not flags["forward_n_ge_100_exists"]:
        flags["decision"]["blocked_reasons"].append(
            f"No forward-only strategy with closed_trades/forward_n >= {FORWARD_MATURE_N} "
            f"(max forward-only n={flags['max_forward_only_n']})"
        )
    if not flags["decision"]["has_human_live_review_candidate"]:
        flags["decision"]["blocked_reasons"].append(
            "No strategy meets HUMAN_LIVE_REVIEW_CANDIDATE (requires n>=100, PF>=1.2, WR>=0.50, "
            "concentration pass, sell/panic/allowance/private-key gates)"
        )
    if not flags["decision"]["has_canary_candidate"]:
        flags["decision"]["blocked_reasons"].append(
            "No live_market_shadow strategy with true_live_forward n>=25 without concentration reject"
        )
    flags["decision"]["verdict"] = (
        "NO_PROMOTION_NO_CANARY"
        if not flags["decision"]["has_human_live_review_candidate"] and not flags["decision"]["has_canary_candidate"]
        else "REVIEW_QUEUE_NON_EMPTY"
    )

    flags["final_decision_flags"] = {
        "PLUMBING_ONLY_ANY": flags["promotion_flag_counts"].get("PLUMBING_ONLY", 0) > 0,
        "EARLY_HINT_ANY": flags["promotion_flag_counts"].get("EARLY_HINT", 0) > 0,
        "FORWARD_MATURE_ANY": flags["promotion_flag_counts"].get("FORWARD_MATURE", 0) > 0,
        "HUMAN_LIVE_REVIEW_CANDIDATE_ANY": flags["decision"]["has_human_live_review_candidate"],
        "REJECT_CONCENTRATED_ANY": flags["promotion_flag_counts"].get("REJECT_CONCENTRATED", 0) > 0,
        "REJECT_NOT_FORWARD_ANY": flags["promotion_flag_counts"].get("REJECT_NOT_FORWARD", 0) > 0,
        "FORWARD_N_GE_100": flags["forward_n_ge_100_exists"],
        "CANARY_CANDIDATE": flags["decision"]["has_canary_candidate"],
        "CANARY_BLOCKED": not flags["decision"]["has_canary_candidate"],
        "PROMOTE_FORBIDDEN": True,
        "LIVE_FORBIDDEN": True,
        "TRAINING_FORBIDDEN": True,
        "VERDICT": flags["decision"]["verdict"],
    }

    flags_path = OUT_DIR / "sop_promotion_flags.json"
    flags_path.write_text(json.dumps(flags, indent=2, default=str))

    by_mode: dict[str, list[str]] = {}
    for r in inventory:
        by_mode.setdefault(r["source_mode"], []).append(
            f"- **{r['strategy_key']}** ({r['promotion_flag']}) n={r.get('forward_n')}"
        )

    md_lines = [
        "# SOP promotion inventory readout",
        "",
        f"- **generated_at_utc:** {flags['generated_at_utc']}",
        f"- **rows:** {len(inventory)}",
        f"- **verdict:** `{flags['decision']['verdict']}`",
        "",
        "## Mode separation (strict)",
        "",
        "| Mode | Meaning | Count |",
        "|------|---------|------:|",
    ]
    for mode, cnt in sorted(flags["source_mode_counts"].items()):
        md_lines.append(f"| `{mode}` | See SOP §forward paths | {cnt} |")

    md_lines += [
        "",
        "## Promotion flag counts",
        "",
        "| Flag | Count |",
        "|------|------:|",
    ]
    for fl, cnt in sorted(flags["promotion_flag_counts"].items()):
        md_lines.append(f"| `{fl}` | {cnt} |")

    md_lines += [
        "",
        "## Forward n≥100 check",
        "",
        f"- **Any forward-only n≥100:** {flags['forward_n_ge_100_exists']}",
        f"- **Max forward-only n:** {flags['max_forward_only_n']}",
    ]
    if forward_ge_100:
        for r in forward_ge_100:
            md_lines.append(f"  - {r['strategy_key']}: n={r['forward_n']}")
    else:
        md_lines.append("- None. Sprint076 top forward harness max n≈52 (`S074_020`).")

    md_lines += [
        "",
        "## Human live review / canary",
        "",
        f"- **HUMAN_LIVE_REVIEW_CANDIDATE count:** {len(candidates_hlr)}",
        f"- **Canary candidate (live_market_shadow n≥25):** {flags['canary_candidate_exists']}",
        f"- **Artifact canary_decision:** `{exec_gates.get('canary_decision')}`",
        f"- **true_live_forward_executor_n:** {exec_gates.get('true_live_forward_executor_n')}",
        "",
        "### Blocked reasons",
        "",
    ]
    for b in flags["decision"]["blocked_reasons"]:
        md_lines.append(f"- {b}")

    md_lines += [
        "",
        "## Top forward dry-run (n≥20, not concentrated)",
        "",
        "| strategy | n | PF | WR | EV | pool_share | flag |",
        "|----------|--:|---:|---:|---:|-----------:|------|",
    ]
    fwd_rank = sorted(
        [r for r in inventory if r.get("source_mode") == "forward_dryrun"],
        key=lambda x: (-(int(x.get("forward_n") or 0)), -(float(x.get("mean_return") or -999))),
    )
    for r in fwd_rank[:12]:
        if int(r.get("forward_n") or 0) < 20:
            continue
        md_lines.append(
            f"| {r['strategy_key']} | {r.get('forward_n')} | {r.get('pf')} | {r.get('win_rate')} | "
            f"{r.get('mean_return')} | {r.get('top_pool_share')} | {r.get('promotion_flag')} |"
        )

    md_lines += [
        "",
        "## Artifacts",
        "",
        f"- `{csv_path}`",
        f"- `{flags_path}`",
        "",
        "## Policy reminders",
        "",
        "- Historical replay / sprint072 sweep / G3 holdout CSV ≠ forward proof.",
        "- Paper shadow (sprint055/068) = live-pool QuoterV2 labels; not executor true-live-forward N.",
        "- Forward dry-run = sprint075/077/079 state `trades[]` after harness freeze.",
        "- Live market shadow = executor dry-run post-cutoff (`true_live_forward_executor_n`).",
        "",
    ]

    readout_path = OUT_DIR / "sop_promotion_readout.md"
    readout_path.write_text("\n".join(md_lines))

    print(json.dumps({"ok": True, "out_dir": str(OUT_DIR), "rows": len(inventory), "verdict": flags["decision"]["verdict"]}, indent=2))


if __name__ == "__main__":
    main()
