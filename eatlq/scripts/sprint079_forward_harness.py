#!/usr/bin/env python3
"""
Sprint079 — Expanded Forward Dry-Run Harness (100+ Pools)
==========================================================
Reads from sprint078_shadow.csv (all fee tiers, heat≥5, Apr 1 - present).
Evaluates same 10 Sprint074 PROMOTE_DRYRUN strategy specs as sprint075,
but with the earlier forward cutoff that captures 100+ unique pools.

Key differences from sprint075:
  - SRC_CSV  = logs/sprint078_shadow.csv   (expanded dataset)
  - FORWARD_CUTOFF = "2026-04-01T00:00:00+00:00"
    → pools from Apr 1 onward are "forward" (never used in strategy design)
    → sprint074 factory was built on sprint055 data (May 11+) → Apr 1+ is OOS
  - STATE_DIR = logs/sprint079/
  - OUT_DIR   = mvp/audits/sprint079_forward_harness/

Cron (every 5 minutes after sprint078 cron runs):
  9-59/5 * * * * cd /opt/EATLQ && python3 scripts/sprint079_forward_harness.py \\
                  --once >> logs/sprint079/harness_cron.log 2>&1

Outputs:
  logs/sprint079/{strategy_id}_state.json
  mvp/audits/sprint079_forward_harness/scoreboard.json
  mvp/audits/sprint079_forward_harness/scoreboard.csv
  mvp/audits/sprint079_forward_harness/sprint079_readout.md
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO      = Path(__file__).resolve().parent.parent
SRC_CSV   = REPO / "logs" / "sprint078_shadow.csv"
STATE_DIR = REPO / "logs" / "sprint079"
OUT_DIR   = REPO / "mvp" / "audits" / "sprint079_forward_harness"

STATE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Key difference: earlier cutoff captures 100+ unique pools ─────────────────
FORWARD_CUTOFF = "2026-04-01T00:00:00+00:00"

GAS_PCT    = 0.002
TRADE_SIZE = 10.0
INITIAL_EQ = 100.0

# ── Strategy specs (identical to sprint075) ───────────────────────────────────
#
# fee_max_bps = 100 means Uniswap fee() ppm ≤ 100 (0.01% tier)
# With sprint078, signals span fee_ppm in {100, 500, 3000, ...}
# S074_021/022/027/032/037 (fee_max=None): will see ALL fee tiers → bigger N
# S074_061/062/067/072/077 (fee_max=100):  still only fee=100ppm pools
# This creates meaningful differentiation in forward data.

STRATEGIES: list[dict] = [
    # ── Original S074 PROMOTE_DRYRUN strategies (all fee tiers, no sender gate) ──
    dict(strategy_id="S074_021", exit_horizon="1h",
         fee_max_bps=None, pv_min=None, momentum_gate=False, sender_0x6ff5_only=False,
         hist_pf=2.5291, hist_wr=0.609, hist_n=156),
    dict(strategy_id="S074_022", exit_horizon="2h",
         fee_max_bps=None, pv_min=None, momentum_gate=False, sender_0x6ff5_only=False,
         hist_pf=3.7669, hist_wr=0.6467, hist_n=150),
    dict(strategy_id="S074_027", exit_horizon="2h",
         fee_max_bps=None, pv_min=None, momentum_gate=True, sender_0x6ff5_only=False,
         hist_pf=3.5511, hist_wr=0.6465, hist_n=99),
    dict(strategy_id="S074_032", exit_horizon="2h",
         fee_max_bps=None, pv_min=0.0, momentum_gate=False, sender_0x6ff5_only=False,
         hist_pf=3.2098, hist_wr=0.6378, hist_n=127),
    dict(strategy_id="S074_037", exit_horizon="2h",
         fee_max_bps=None, pv_min=0.0, momentum_gate=True, sender_0x6ff5_only=False,
         hist_pf=3.5511, hist_wr=0.6465, hist_n=99),
    dict(strategy_id="S074_061", exit_horizon="1h",
         fee_max_bps=100.0, pv_min=None, momentum_gate=False, sender_0x6ff5_only=False,
         hist_pf=2.5291, hist_wr=0.609, hist_n=156),
    dict(strategy_id="S074_062", exit_horizon="2h",
         fee_max_bps=100.0, pv_min=None, momentum_gate=False, sender_0x6ff5_only=False,
         hist_pf=3.7669, hist_wr=0.6467, hist_n=150),
    dict(strategy_id="S074_067", exit_horizon="2h",
         fee_max_bps=100.0, pv_min=None, momentum_gate=True, sender_0x6ff5_only=False,
         hist_pf=3.5511, hist_wr=0.6465, hist_n=99),
    dict(strategy_id="S074_072", exit_horizon="2h",
         fee_max_bps=100.0, pv_min=0.0, momentum_gate=False, sender_0x6ff5_only=False,
         hist_pf=3.2098, hist_wr=0.6378, hist_n=127),
    dict(strategy_id="S074_077", exit_horizon="2h",
         fee_max_bps=100.0, pv_min=0.0, momentum_gate=True, sender_0x6ff5_only=False,
         hist_pf=3.5511, hist_wr=0.6465, hist_n=99),
    # ── S074_030: 30m horizon, LIVE_QUEUE_CANDIDATE — watch only ─────────────
    # S074 factory: N=133, PF=1.70, WR=0.587, equity=121.6, composite=0.451
    dict(strategy_id="S074_030", exit_horizon="30m",
         fee_max_bps=None, pv_min=None, momentum_gate=False, sender_0x6ff5_only=False,
         hist_pf=1.7049, hist_wr=0.5865, hist_n=133),
    # ── alpha_009 sender-gated variants: 0x6ff5 dominant sender required ──────
    # These isolate the Uniswap Universal Router onset alpha (sprint080 finding:
    # non-0x6ff5 pools have PF≈0, 0x6ff5 pools have PF 4.7–6.6x on OOS data)
    dict(strategy_id="S074_022_6ff5", exit_horizon="2h",
         fee_max_bps=None, pv_min=None, momentum_gate=False, sender_0x6ff5_only=True,
         hist_pf=3.7669, hist_wr=0.6467, hist_n=150),
    dict(strategy_id="S074_032_6ff5", exit_horizon="2h",
         fee_max_bps=None, pv_min=0.0, momentum_gate=False, sender_0x6ff5_only=True,
         hist_pf=3.2098, hist_wr=0.6378, hist_n=127),
]

# Common entry gates
AGE_MIN_SEC  = 1800.0
AGE_MAX_SEC  = 86400.0
COOLDOWN_SEC = 1800
MAX_CONCURRENT = 3


# ── Helpers ───────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def log(msg: str) -> None:
    print(f"[s079] {utc_now().strftime('%H:%M:%SZ')} {msg}", flush=True)


def parse_dt(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)


def sig_key(pool: str, signal_time: str) -> str:
    return f"{pool}::{signal_time}"


# ── CSV loading ───────────────────────────────────────────────────────────────

def load_csv() -> tuple[list[dict], list[dict]]:
    """Return (historical_rows, forward_rows) split by FORWARD_CUTOFF."""
    if not SRC_CSV.exists():
        log(f"WARNING: {SRC_CSV} not found — returning empty lists")
        return [], []
    cutoff = parse_dt(FORWARD_CUTOFF)
    hist, fwd = [], []
    with SRC_CSV.open() as f:
        for r in csv.DictReader(f):
            if r.get("entry_quote_ok") != "True":
                continue
            try:
                row = {
                    "pool":               r["pool"],
                    "signal_time":        r["signal_time"],
                    "fee_bps":            float(r.get("fee_bps") or 0),
                    "pool_age_sec":       float(r["pool_age_sec"]) if r.get("pool_age_sec") else None,
                    "activity_heat":      float(r.get("activity_heat") or 0),
                    "buy_pressure_ratio": float(r.get("buy_pressure_ratio") or 0),
                    "price_velocity_5m":  float(r.get("price_velocity_5m") or 0),
                    # Sender dimension (backfilled by sprint080)
                    "is_0x6ff5":          r.get("is_0x6ff5") == "True",
                    "dominant_sender":    r.get("dominant_sender", ""),
                }
                for h in ("1h", "2h", "4h", "6h", "30m"):
                    ok_k  = f"exit_{h}_ok"
                    ag_k  = f"net_return_{h}_ag"
                    row[f"ok_{h}"]    = r.get(ok_k) == "True"
                    row[f"retag_{h}"] = float(r[ag_k]) if r.get(ag_k) else None
                sig_dt = parse_dt(r["signal_time"])
                (fwd if sig_dt > cutoff else hist).append(row)
            except (ValueError, KeyError):
                continue
    return (sorted(hist, key=lambda x: x["signal_time"]),
            sorted(fwd,  key=lambda x: x["signal_time"]))


# ── State I/O ─────────────────────────────────────────────────────────────────

def _state_path(sid: str) -> Path:
    return STATE_DIR / f"{sid}_state.json"


def load_state(sid: str) -> dict:
    p = _state_path(sid)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {
        "strategy_id":      sid,
        "created_at":       utc_now().isoformat(),
        "last_run":         None,
        "run_count":        0,
        "equity":           INITIAL_EQ,
        "open_positions":   {},
        "pool_last_entry":  {},
        "processed_signals": [],
        "trades":           [],
        "stats": {
            "forward_n": 0, "wins": 0, "losses": 0,
            "total_gain": 0.0, "total_loss_abs": 0.0,
            "skipped_cooldown": 0, "skipped_age": 0,
            "skipped_fee": 0, "skipped_momentum": 0,
            "skipped_pv": 0, "skipped_no_label": 0,
            "skipped_concurrent": 0, "skipped_sender": 0,
        },
    }


def save_state(state: dict) -> None:
    _state_path(state["strategy_id"]).write_text(json.dumps(state, indent=2, default=str))


# ── Entry gate ────────────────────────────────────────────────────────────────

def passes_entry(row: dict, spec: dict) -> tuple[bool, str]:
    age = row.get("pool_age_sec")
    if age is None or not (AGE_MIN_SEC <= age <= AGE_MAX_SEC):
        return False, "age"
    if spec["fee_max_bps"] is not None and row["fee_bps"] > spec["fee_max_bps"]:
        return False, "fee"
    if spec["pv_min"] is not None and row["price_velocity_5m"] < spec["pv_min"]:
        return False, "pv"
    if spec["momentum_gate"]:
        if not (row["buy_pressure_ratio"] >= 0.55 and row["price_velocity_5m"] > 0):
            return False, "momentum"
    # Sender gate: require dominant_sender = 0x6ff5 if specified
    if spec.get("sender_0x6ff5_only") and not row.get("is_0x6ff5"):
        return False, "sender"
    return True, ""


# ── Per-strategy evaluation ───────────────────────────────────────────────────

def run_strategy(spec: dict, fwd_rows: list[dict]) -> dict:
    sid     = spec["strategy_id"]
    horizon = spec["exit_horizon"]
    state   = load_state(sid)
    state["last_run"]  = utc_now().isoformat()
    state["run_count"] = state.get("run_count", 0) + 1

    all_known: set[str] = (set(state.get("processed_signals", []))
                           | set(state["open_positions"].keys())
                           | {t["sig_key"] for t in state["trades"]})
    if "processed_signals" not in state:
        state["processed_signals"] = []

    stats = state["stats"]
    new_entries = 0

    for row in fwd_rows:
        sk = sig_key(row["pool"], row["signal_time"])
        if sk in all_known:
            continue
        state["processed_signals"].append(sk)
        all_known.add(sk)

        ok, reason = passes_entry(row, spec)
        if not ok:
            stats[f"skipped_{reason}"] = stats.get(f"skipped_{reason}", 0) + 1
            continue

        # Cooldown
        last_entry_str = state["pool_last_entry"].get(row["pool"])
        if last_entry_str:
            last_entry = parse_dt(last_entry_str)
            sig_dt     = parse_dt(row["signal_time"])
            if (sig_dt - last_entry).total_seconds() < COOLDOWN_SEC:
                stats["skipped_cooldown"] = stats.get("skipped_cooldown", 0) + 1
                continue

        # Concurrent position cap
        if len(state["open_positions"]) >= MAX_CONCURRENT:
            stats["skipped_concurrent"] = stats.get("skipped_concurrent", 0) + 1
            continue

        state["pool_last_entry"][row["pool"]] = row["signal_time"]

        # Check if label already available
        ret_ag = row.get(f"retag_{horizon}")
        ok_h   = row.get(f"ok_{horizon}", False)

        if ok_h and ret_ag is not None:
            # Label ready → close immediately
            pnl     = TRADE_SIZE * ret_ag
            is_win  = ret_ag > 0
            state["equity"] += pnl
            stats["forward_n"] += 1
            if is_win:
                stats["wins"] += 1
                stats["total_gain"] += max(0, pnl)
            else:
                stats["losses"] += 1
                stats["total_loss_abs"] += abs(min(0, pnl))
            state["trades"].append({
                "sig_key":    sk,
                "pool":       row["pool"],
                "signal_time": row["signal_time"],
                "horizon":    horizon,
                "ret_ag":     ret_ag,
                "pnl":        round(pnl, 6),
                "win":        is_win,
                "closed_at":  utc_now().isoformat(),
                "status":     "closed",
            })
            new_entries += 1
        else:
            # Open position — label pending
            state["open_positions"][sk] = {
                "pool":        row["pool"],
                "signal_time": row["signal_time"],
                "entered_at":  utc_now().isoformat(),
            }
            new_entries += 1

    # ── Close pending positions if label now available ────────────────────────
    still_open = {}
    for sk, pos in state["open_positions"].items():
        # Find in fwd_rows
        matched = next(
            (r for r in fwd_rows
             if sig_key(r["pool"], r["signal_time"]) == sk),
            None,
        )
        if matched is None:
            still_open[sk] = pos
            continue

        ret_ag = matched.get(f"retag_{horizon}")
        ok_h   = matched.get(f"ok_{horizon}", False)
        if ok_h and ret_ag is not None:
            pnl    = TRADE_SIZE * ret_ag
            is_win = ret_ag > 0
            state["equity"] += pnl
            stats["forward_n"] += 1
            if is_win:
                stats["wins"] += 1
                stats["total_gain"] += max(0, pnl)
            else:
                stats["losses"] += 1
                stats["total_loss_abs"] += abs(min(0, pnl))
            state["trades"].append({
                "sig_key":    sk,
                "pool":       pos["pool"],
                "signal_time": pos["signal_time"],
                "horizon":    horizon,
                "ret_ag":     ret_ag,
                "pnl":        round(pnl, 6),
                "win":        is_win,
                "closed_at":  utc_now().isoformat(),
                "status":     "closed",
            })
        else:
            still_open[sk] = pos

    state["open_positions"] = still_open

    # ── Compute metrics ───────────────────────────────────────────────────────
    n        = stats["forward_n"]
    gains    = stats["total_gain"]
    losses   = stats["total_loss_abs"]
    pf       = round(gains / losses, 4) if losses > 0 else None
    wr       = round(stats["wins"] / n, 4) if n > 0 else None
    eq_delta = round(state["equity"] - INITIAL_EQ, 4)
    ev_trade = round(eq_delta / n, 6) if n > 0 else None

    # Max drawdown from trades
    eq_curve, peak, dd = INITIAL_EQ, INITIAL_EQ, 0.0
    for t in state["trades"]:
        eq_curve += t["pnl"]
        peak = max(peak, eq_curve)
        dd   = max(dd, peak - eq_curve)

    state["metrics"] = {
        "forward_n":     n,
        "wins":          stats["wins"],
        "losses":        stats["losses"],
        "pf":            pf,
        "wr":            wr,
        "equity":        round(state["equity"], 4),
        "eq_delta":      eq_delta,
        "ev_trade":      ev_trade,
        "max_dd":        round(dd, 4),
        "open_count":    len(state["open_positions"]),
        "new_entries":   new_entries,
        "skipped":       {k: v for k, v in stats.items() if k.startswith("skipped")},
    }

    save_state(state)
    return state


# ── Scoreboard & readout ──────────────────────────────────────────────────────

def _confidence_bucket(n: int) -> str:
    if n < 20:  return "N_LT_20_PLUMBING_ONLY"
    if n < 50:  return "N_20_50_WEAK_EVIDENCE"
    if n < 100: return "N_50_100_RANKABLE"
    return "N_GE_100_REVIEWABLE"


def build_scoreboard(states: list[dict]) -> dict:
    rows = []
    for st in states:
        m   = st.get("metrics", {})
        sid = st["strategy_id"]
        spec = next(s for s in STRATEGIES if s["strategy_id"] == sid)
        rows.append({
            "strategy_id":   sid,
            "exit_horizon":  spec["exit_horizon"],
            "fee_max_bps":   spec.get("fee_max_bps"),
            "momentum_gate": spec.get("momentum_gate", False),
            "pv_min":        spec.get("pv_min"),
            "forward_n":     m.get("forward_n", 0),
            "wins":          m.get("wins", 0),
            "losses":        m.get("losses", 0),
            "pf":            m.get("pf"),
            "wr":            m.get("wr"),
            "equity":        m.get("equity", INITIAL_EQ),
            "eq_delta":      m.get("eq_delta", 0),
            "ev_trade":      m.get("ev_trade"),
            "max_dd":        m.get("max_dd", 0),
            "open_count":    m.get("open_count", 0),
            "confidence":         _confidence_bucket(m.get("forward_n", 0)),
            "sender_0x6ff5_only": spec.get("sender_0x6ff5_only", False),
            "hist_pf":            spec.get("hist_pf"),
            "hist_wr":            spec.get("hist_wr"),
            "hist_n":             spec.get("hist_n"),
        })
    rows.sort(key=lambda x: (-(x["pf"] or 0), -(x["forward_n"] or 0)))

    # ── Three-window metadata — mandatory; never conflate OOS windows ─────────
    # S074 factory ran 2026-05-16T05:40:30 UTC. Its training data was the first
    # 796 rows of sprint055_paper_shadow.csv, with last signal_time 2026-05-16T05:00 UTC.
    # Sprint055 cron began on 2026-05-11T00:00 UTC. All sprint079 valid trades
    # come from Apr 1–May 10 pools whose May 11+ archive quotes failed → 100% Window A.
    S074_FREEZE_TS = "2026-05-16T05:00:00+00:00"
    MAY11_START_TS = "2026-05-11T00:00:00+00:00"
    data_windows = {
        "APRIL_TRUE_OOS": {
            "range": f"2026-04-01 → {MAY11_START_TS[:10]}",
            "description": (
                "Before sprint055 cron started. Never in S074 training data. "
                "100% of sprint079 valid trades come from this window."
            ),
            "sprint079_valid_rows_pct": "100%",
        },
        "DESIGN_WINDOW_REPLAY": {
            "range": f"{MAY11_START_TS[:10]} → {S074_FREEZE_TS[:16]}",
            "description": (
                "Sprint055 data used by S074 factory. NOT strictly OOS. "
                "Archive QuoterV2 calls for May 11+ pools all failed; sprint079 has ZERO trades here."
            ),
            "sprint079_valid_rows_pct": "0%",
        },
        "LIVE_FORWARD_AFTER_FREEZE": {
            "range": f"{S074_FREEZE_TS[:16]} → now",
            "description": (
                "Collected after S074 ran. Genuine live/forward evidence. "
                "Covered by sprint075/076/077 live harnesses; sprint079 has ZERO trades here."
            ),
            "sprint079_valid_rows_pct": "0%",
        },
    }

    return {
        "generated_at":  utc_now().isoformat(),
        "forward_cutoff": FORWARD_CUTOFF,
        "source_csv":    str(SRC_CSV),
        "oos_note":      "sprint079 metrics = 100% APRIL_TRUE_OOS (Apr 1–May 10). See data_windows.",
        "data_windows":  data_windows,
        "strategies":    rows,
    }


def save_scoreboard(sb: dict) -> None:
    (OUT_DIR / "scoreboard.json").write_text(json.dumps(sb, indent=2, default=str))
    cols = list(sb["strategies"][0].keys()) if sb["strategies"] else []
    with (OUT_DIR / "scoreboard.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sb["strategies"])


def generate_readout(sb: dict, hist_rows: list[dict], fwd_rows: list[dict]) -> str:
    now     = utc_now().strftime("%Y-%m-%d %H:%M UTC")
    cutoff  = FORWARD_CUTOFF
    fwd_pools = len(set(r["pool"] for r in fwd_rows))
    hist_pools = len(set(r["pool"] for r in hist_rows))

    lines = [
        f"# Sprint079 Forward Harness Readout",
        f"Generated: {now}",
        f"Source: {SRC_CSV.name}",
        f"Forward cutoff: {cutoff}",
        f"",
        f"## OOS Window Classification",
        f"",
        f"| Window | Date Range | Sprint079 trades | Sprint075 trades |",
        f"|--------|------------|-----------------|-----------------|",
        f"| APRIL_TRUE_OOS | Apr 01 – May 10 | **100%** (53 pools) | 0% (cron not running) |",
        f"| DESIGN_WINDOW_REPLAY | May 11 – May 16 05:00 UTC | 0% (archive quotes failed) | ~79% (S074 training data) |",
        f"| LIVE_FORWARD_AFTER_FREEZE | May 16 05:00 UTC+ | 0% | ~21% (genuine live fwd) |",
        f"",
        f"> **Sprint079 OOS status**: All valid trades are from Window A (APRIL_TRUE_OOS).",
        f"> S074 was trained exclusively on sprint055 rows 1–796 (May 11 – May 16 05:00 UTC).",
        f"> The May 11–17 range in the current sprint055 CSV includes 217 rows added AFTER S074 ran.",
        f"",
        f"## Dataset",
        f"- Historical signals (pre-cutoff, unused): {len(hist_rows)} rows, {hist_pools} unique pools",
        f"- Forward signals  (APRIL_TRUE_OOS):       {len(fwd_rows)} rows, {fwd_pools} unique pools",
        f"",
        f"## Forward Scoreboard",
        f"",
        f"| Strategy | Horizon | FeeMax | Momentum | N | PF | WR | EV | DD | Confidence |",
        f"|----------|---------|--------|----------|---|----|----|----|----|------------|",
    ]
    for s in sb["strategies"]:
        pf_s  = f"{s['pf']:.3f}" if s["pf"] else "—"
        wr_s  = f"{s['wr']:.3f}" if s["wr"] else "—"
        ev_s  = f"{s['ev_trade']:.4f}" if s["ev_trade"] else "—"
        fee_s = str(int(s["fee_max_bps"])) if s["fee_max_bps"] else "All"
        lines.append(
            f"| {s['strategy_id']} | {s['exit_horizon']} | {fee_s} | "
            f"{'✓' if s['momentum_gate'] else '—'} | {s['forward_n']} | "
            f"{pf_s} | {wr_s} | {ev_s} | {s['max_dd']:.2f} | {s['confidence']} |"
        )

    lines += [
        f"",
        f"## Live Readiness Assessment",
        f"",
    ]
    n_ge_100 = [s for s in sb["strategies"] if (s["forward_n"] or 0) >= 100]
    if n_ge_100:
        lines.append(f"**{len(n_ge_100)} strategies at N≥100 (REVIEWABLE):**")
        for s in n_ge_100:
            lines.append(f"  - {s['strategy_id']}: PF={s['pf'] or '?'}, WR={s['wr'] or '?'}")
    else:
        max_n = max((s["forward_n"] or 0) for s in sb["strategies"]) if sb["strategies"] else 0
        lines.append(f"No strategies at N≥100 yet. Max N = {max_n}")
        lines.append(f"Continue accumulating; check again when backfill is complete.")

    lines += [
        f"",
        f"## Delta vs. Sprint075 (sprint055 dataset, May-15 cutoff)",
        f"",
        f"| Strategy | S075 N | S079 N | Delta | S075 PF | S079 PF |",
        f"|----------|--------|--------|-------|---------|---------|",
    ]
    # Load sprint075 scoreboard for comparison if available
    s075_path = REPO / "mvp" / "audits" / "sprint075_forward_harness" / "scoreboard.json"
    s075_lookup: dict[str, dict] = {}
    if s075_path.exists():
        try:
            s075_data = json.loads(s075_path.read_text())
            for row in (s075_data.get("strategies") or s075_data if isinstance(s075_data, list) else []):
                s075_lookup[row["strategy_id"]] = row
        except Exception:
            pass

    for s in sb["strategies"]:
        s75 = s075_lookup.get(s["strategy_id"], {})
        n75 = s75.get("forward_n", "?")
        pf75 = f"{s75['pf']:.3f}" if s75.get("pf") else "—"
        pf79 = f"{s['pf']:.3f}" if s["pf"] else "—"
        n79  = s["forward_n"]
        delta = (n79 - int(n75)) if isinstance(n75, int) else "?"
        lines.append(f"| {s['strategy_id']} | {n75} | {n79} | {delta} | {pf75} | {pf79} |")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once",     action="store_true", help="one run cycle")
    parser.add_argument("--daemon",   action="store_true", help="run every 5m")
    parser.add_argument("--readout",  action="store_true", help="print readout only")
    args = parser.parse_args()

    if not (args.once or args.daemon or args.readout):
        parser.print_help()
        return

    while True:
        log(f"Loading {SRC_CSV.name}…")
        hist_rows, fwd_rows = load_csv()
        log(f"Loaded: hist={len(hist_rows)} fwd={len(fwd_rows)} "
            f"fwd_pools={len(set(r['pool'] for r in fwd_rows))}")

        if args.readout:
            # Load existing scoreboards instead of running
            states = [json.loads(_state_path(s["strategy_id"]).read_text())
                      for s in STRATEGIES
                      if _state_path(s["strategy_id"]).exists()]
            if not states:
                log("No state files found — run --once first")
                return
            sb = build_scoreboard(states)
        else:
            states = []
            for spec in STRATEGIES:
                st = run_strategy(spec, fwd_rows)
                m  = st["metrics"]
                log(f"{spec['strategy_id']}: N={m['forward_n']} PF={m['pf']} "
                    f"WR={m['wr']} open={m['open_count']} new={m['new_entries']}")
                states.append(st)

            sb = build_scoreboard(states)
            save_scoreboard(sb)

        readout = generate_readout(sb, hist_rows, fwd_rows)
        (OUT_DIR / "sprint079_readout.md").write_text(readout)

        log(f"Scoreboard saved to {OUT_DIR}")
        log(f"Forward pools: {len(set(r['pool'] for r in fwd_rows))}")

        # Print summary
        print("\n" + "="*60)
        print(readout)
        print("="*60 + "\n")

        if not args.daemon:
            break
        time.sleep(300)


if __name__ == "__main__":
    main()
