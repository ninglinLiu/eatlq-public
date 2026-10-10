#!/usr/bin/env python3
"""
Sprint075 — Top-10 PROMOTE_DRYRUN Forward Dry-Run Harness
==========================================================
Runs isolated forward-only evaluation for the 10 PROMOTE_DRYRUN candidates
from Sprint074. Reads new signals from sprint055_paper_shadow.csv and closes
positions when the exit label (1h or 2h) becomes available.

Separation contract:
  - FORWARD_CUTOFF marks the boundary; signals before it are historical
    (metrics pre-loaded from factory scoreboard, NOT recomputed here).
  - Forward slice is what drives live PF / WR / EV / scoreboard updates.
  - No Redis writes, no live trades, no production signals.
  - Each strategy has an isolated state file under logs/sprint075/.

Cron (every 5 minutes, offset +4 from paper shadow):
  4-59/5 * * * * cd /opt/EATLQ && python3 scripts/sprint075_forward_harness.py \\
                  --once >> logs/sprint075/harness_cron.log 2>&1

Outputs:
  logs/sprint075/{strategy_id}_state.json        (per-strategy)
  mvp/audits/sprint075_forward_harness/scoreboard.json
  mvp/audits/sprint075_forward_harness/scoreboard.csv
  mvp/audits/sprint075_forward_harness/sprint075_readout.md
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

REPO = Path(__file__).resolve().parent.parent
SRC_CSV = REPO / "logs" / "sprint055_paper_shadow.csv"
STATE_DIR = REPO / "logs" / "sprint075"
OUT_DIR = REPO / "mvp" / "audits" / "sprint075_forward_harness"
FACTORY_SCOREBOARD = REPO / "mvp" / "audits" / "sprint074_alpha_factory" / "alpha_factory_scoreboard.json"

STATE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Cutoff: signals strictly after this timestamp are "forward"
# Set to end of the initial historical backfill run (2026-05-15 14:00 UTC)
FORWARD_CUTOFF = "2026-05-15T14:00:00+00:00"

GAS_PCT = 0.002
TRADE_SIZE = 10.0
INITIAL_EQ = 100.0

# ─── Strategy specs (derived from Sprint074 PROMOTE_DRYRUN selection) ───────
#
# All 10 use:  age ∈ [1800, 86400] sec, cooldown=1800s entry-anchor, heat≥0, bpr≥0
# Distinguishing axes: exit_horizon, fee_cap, pv_min (≥0 = non-negative filter),
#                      momentum_gate (BPR≥0.55 AND PV>0)

STRATEGIES: list[dict] = [
    dict(
        strategy_id="S074_021",
        exit_horizon="1h",
        fee_max_bps=None,
        pv_min=None,
        momentum_gate=False,
        hist_pf=2.5291, hist_wr=0.609, hist_n=156, hist_composite=0.5282,
    ),
    dict(
        strategy_id="S074_022",
        exit_horizon="2h",
        fee_max_bps=None,
        pv_min=None,
        momentum_gate=False,
        hist_pf=3.7669, hist_wr=0.6467, hist_n=150, hist_composite=0.5881,
    ),
    dict(
        strategy_id="S074_027",
        exit_horizon="2h",
        fee_max_bps=None,
        pv_min=None,
        momentum_gate=True,
        hist_pf=3.5511, hist_wr=0.6465, hist_n=99, hist_composite=0.5748,
    ),
    dict(
        strategy_id="S074_032",
        exit_horizon="2h",
        fee_max_bps=None,
        pv_min=0.0,
        momentum_gate=False,
        hist_pf=3.2098, hist_wr=0.6378, hist_n=127, hist_composite=0.5654,
    ),
    dict(
        strategy_id="S074_037",
        exit_horizon="2h",
        fee_max_bps=None,
        pv_min=0.0,
        momentum_gate=True,
        hist_pf=3.5511, hist_wr=0.6465, hist_n=99, hist_composite=0.5748,
    ),
    dict(
        strategy_id="S074_061",
        exit_horizon="1h",
        fee_max_bps=100.0,
        pv_min=None,
        momentum_gate=False,
        hist_pf=2.5291, hist_wr=0.609, hist_n=156, hist_composite=0.5282,
    ),
    dict(
        strategy_id="S074_062",
        exit_horizon="2h",
        fee_max_bps=100.0,
        pv_min=None,
        momentum_gate=False,
        hist_pf=3.7669, hist_wr=0.6467, hist_n=150, hist_composite=0.5881,
    ),
    dict(
        strategy_id="S074_067",
        exit_horizon="2h",
        fee_max_bps=100.0,
        pv_min=None,
        momentum_gate=True,
        hist_pf=3.5511, hist_wr=0.6465, hist_n=99, hist_composite=0.5748,
    ),
    dict(
        strategy_id="S074_072",
        exit_horizon="2h",
        fee_max_bps=100.0,
        pv_min=0.0,
        momentum_gate=False,
        hist_pf=3.2098, hist_wr=0.6378, hist_n=127, hist_composite=0.5654,
    ),
    dict(
        strategy_id="S074_077",
        exit_horizon="2h",
        fee_max_bps=100.0,
        pv_min=0.0,
        momentum_gate=True,
        hist_pf=3.5511, hist_wr=0.6465, hist_n=99, hist_composite=0.5748,
    ),
]

# Common entry gates (shared across all 10)
AGE_MIN_SEC = 1800.0
AGE_MAX_SEC = 86400.0
COOLDOWN_SEC = 1800
MAX_CONCURRENT = 3


# ─── Helpers ─────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def log(msg: str) -> None:
    print(f"[s075] {utc_now().strftime('%H:%M:%SZ')} {msg}", flush=True)


def parse_dt(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19], fmt[:-2] if fmt.endswith("%z") else fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)


def sig_key(pool: str, signal_time: str) -> str:
    return f"{pool}::{signal_time}"


# ─── CSV loading ─────────────────────────────────────────────────────────────

def load_csv() -> tuple[list[dict], list[dict]]:
    """Return (historical_rows, forward_rows) split by FORWARD_CUTOFF."""
    cutoff = parse_dt(FORWARD_CUTOFF)
    hist, fwd = [], []
    with open(SRC_CSV) as f:
        for r in csv.DictReader(f):
            if r.get("entry_quote_ok") != "True":
                continue
            try:
                row = {
                    "pool": r["pool"],
                    "signal_time": r["signal_time"],
                    "fee_bps": float(r.get("fee_bps") or 0),
                    "pool_age_sec": float(r["pool_age_sec"]) if r.get("pool_age_sec") else None,
                    "activity_heat": float(r.get("activity_heat") or 0),
                    "buy_pressure_ratio": float(r.get("buy_pressure_ratio") or 0),
                    "price_velocity_5m": float(r.get("price_velocity_5m") or 0),
                }
                for h in ("1h", "2h", "4h", "6h"):
                    ok_k, ag_k = f"exit_{h}_ok", f"net_return_{h}_ag"
                    row[f"ok_{h}"] = r.get(ok_k) == "True"
                    row[f"retag_{h}"] = float(r[ag_k]) if r.get(ag_k) else None
                sig_dt = parse_dt(r["signal_time"])
                (fwd if sig_dt > cutoff else hist).append(row)
            except (ValueError, KeyError):
                continue
    return sorted(hist, key=lambda x: x["signal_time"]), \
           sorted(fwd, key=lambda x: x["signal_time"])


# ─── State I/O ───────────────────────────────────────────────────────────────

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
        "strategy_id": sid,
        "created_at": utc_now().isoformat(),
        "last_run": None,
        "run_count": 0,
        "equity": INITIAL_EQ,
        "open_positions": {},      # sig_key -> {pool, signal_time, entry_time, expected_exit}
        "pool_last_entry": {},     # pool -> ISO entry time (cooldown anchor)
        "processed_signals": [],   # all seen sig_keys (prevents re-processing)
        "trades": [],              # closed trade records
        "stats": {
            "forward_n": 0, "wins": 0, "losses": 0,
            "total_gain": 0.0, "total_loss_abs": 0.0,
            "skipped_cooldown": 0, "skipped_age": 0,
            "skipped_fee": 0, "skipped_momentum": 0,
            "skipped_pv": 0, "skipped_no_label": 0,
            "skipped_concurrent": 0,
        },
    }


def save_state(state: dict) -> None:
    _state_path(state["strategy_id"]).write_text(json.dumps(state, indent=2, default=str))


# ─── Entry gate ──────────────────────────────────────────────────────────────

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
    return True, ""


# ─── Per-strategy run ────────────────────────────────────────────────────────

def run_strategy(spec: dict, fwd_rows: list[dict]) -> dict:
    sid = spec["strategy_id"]
    horizon = spec["exit_horizon"]
    state = load_state(sid)
    state["last_run"] = utc_now().isoformat()
    state["run_count"] = state.get("run_count", 0) + 1

    all_known: set[str] = set(state.get("processed_signals", [])) \
        | set(state["open_positions"].keys()) \
        | {t["sig_key"] for t in state["trades"]}
    if "processed_signals" not in state:
        state["processed_signals"] = []

    stats = state["stats"]
    new_entries = 0

    # ── Process new signals ──────────────────────────────────────────────────
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

        # Cooldown (entry-anchor)
        last_entry = state["pool_last_entry"].get(row["pool"])
        if last_entry:
            elapsed = (parse_dt(row["signal_time"]) - parse_dt(last_entry)).total_seconds()
            if elapsed < COOLDOWN_SEC:
                stats["skipped_cooldown"] = stats.get("skipped_cooldown", 0) + 1
                continue

        # Concurrent cap
        if row["pool"] in state["open_positions"]:
            stats["skipped_concurrent"] = stats.get("skipped_concurrent", 0) + 1
            continue
        if len(state["open_positions"]) >= MAX_CONCURRENT:
            stats["skipped_concurrent"] = stats.get("skipped_concurrent", 0) + 1
            continue

        expected_exit = (parse_dt(row["signal_time"])
                         + timedelta(hours=int(horizon[:-1]))).isoformat()
        state["open_positions"][sk] = {
            "sig_key": sk,
            "pool": row["pool"],
            "signal_time": row["signal_time"],
            "expected_exit": expected_exit,
            "fee_bps": row["fee_bps"],
            "pool_age_sec": row.get("pool_age_sec"),
            "bpr": row["buy_pressure_ratio"],
            "pv": row["price_velocity_5m"],
        }
        state["pool_last_entry"][row["pool"]] = row["signal_time"]
        new_entries += 1

    # ── Close matured positions ───────────────────────────────────────────────
    csv_by_sk = {sig_key(r["pool"], r["signal_time"]): r for r in fwd_rows}
    to_close = []
    now = utc_now()

    for sk, pos in state["open_positions"].items():
        exp = parse_dt(pos["expected_exit"])
        if now < exp:
            continue
        row = csv_by_sk.get(sk)
        if row is None:
            continue
        ret = row.get(f"retag_{horizon}")
        if row.get(f"ok_{horizon}") != True or ret is None:
            stats["skipped_no_label"] = stats.get("skipped_no_label", 0) + 1
            continue
        to_close.append((sk, pos, ret))

    for sk, pos, ret in to_close:
        state["open_positions"].pop(sk)
        pnl = TRADE_SIZE * ret
        state["equity"] = round(state["equity"] + pnl, 6)
        win = pnl > 0
        stats["forward_n"] = stats.get("forward_n", 0) + 1
        if win:
            stats["wins"] = stats.get("wins", 0) + 1
            stats["total_gain"] = stats.get("total_gain", 0.0) + pnl
        else:
            stats["losses"] = stats.get("losses", 0) + 1
            stats["total_loss_abs"] = stats.get("total_loss_abs", 0.0) + abs(pnl)
        state["trades"].append({
            "sig_key": sk,
            "pool": pos["pool"],
            "signal_time": pos["signal_time"],
            "ret_ag": round(ret, 6),
            "pnl": round(pnl, 6),
            "win": win,
        })

    save_state(state)
    return state


# ─── Per-strategy forward metrics ────────────────────────────────────────────

def forward_metrics(state: dict, spec: dict) -> dict:
    trades = state["trades"]
    n = len(trades)
    stats = state["stats"]

    if n == 0:
        return {
            "forward_n": 0, "pf": None, "wr": None, "ev": None,
            "payoff_ratio": None, "max_drawdown": None,
            "top_pool_share": None, "quote_fail_proxy": None,
            "equity": state["equity"],
        }

    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [abs(t["pnl"]) for t in trades if t["pnl"] <= 0]
    rets = [t["ret_ag"] for t in trades]
    pf = round(sum(wins) / sum(losses), 4) if losses else None
    wr = round(len(wins) / n, 4)
    ev = round(statistics.mean(rets), 6)
    mean_win = statistics.mean(wins) if wins else 0.0
    mean_loss = statistics.mean(losses) if losses else 0.0
    payoff = round(mean_win / mean_loss, 4) if mean_loss > 0 else None

    by_pool: dict[str, int] = defaultdict(int)
    for t in trades:
        by_pool[t["pool"]] += 1
    top_share = round(max(by_pool.values()) / n, 4) if by_pool else None

    eq = INITIAL_EQ
    peak = eq
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x["signal_time"]):
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    # Quote fail proxy: signals seen that couldn't close due to missing label
    seen = stats.get("forward_n", 0) + stats.get("skipped_no_label", 0)
    qf = round(stats.get("skipped_no_label", 0) / max(seen, 1), 4)

    return {
        "forward_n": n,
        "pf": pf,
        "wr": wr,
        "ev": ev,
        "payoff_ratio": payoff,
        "max_drawdown": round(max_dd, 4),
        "top_pool_share": top_share,
        "quote_fail_proxy": qf,
        "equity": round(state["equity"], 4),
        "open_positions": len(state["open_positions"]),
    }


# ─── Scoreboard ──────────────────────────────────────────────────────────────

def build_scoreboard(results: list[tuple[dict, dict, dict]]) -> list[dict]:
    rows = []
    for spec, state, fwd in results:
        hard_exit = spec["exit_horizon"] in ("4h", "6h", "12h", "24h")
        entry_desc = (
            f"age[{int(AGE_MIN_SEC)},{int(AGE_MAX_SEC)}]"
            + (f" fee≤{int(spec['fee_max_bps'])}" if spec["fee_max_bps"] else "")
            + (" pv≥0" if spec["pv_min"] is not None else "")
            + (" momentum" if spec["momentum_gate"] else "")
        )
        rows.append({
            "strategy_id": spec["strategy_id"],
            "exit_horizon": spec["exit_horizon"],
            "entry_description": entry_desc,
            "HARD_EXIT_REQUIRED": hard_exit,
            # Historical (from factory backtest)
            "hist_n": spec["hist_n"],
            "hist_pf": spec["hist_pf"],
            "hist_wr": spec["hist_wr"],
            "hist_composite": spec["hist_composite"],
            # Forward only
            "fwd_n": fwd["forward_n"],
            "fwd_pf": fwd["pf"],
            "fwd_wr": fwd["wr"],
            "fwd_ev": fwd["ev"],
            "fwd_payoff_ratio": fwd["payoff_ratio"],
            "fwd_max_drawdown": fwd["max_drawdown"],
            "fwd_top_pool_share": fwd["top_pool_share"],
            "fwd_quote_fail_proxy": fwd["quote_fail_proxy"],
            "fwd_equity": fwd["equity"],
            "fwd_open_positions": fwd["open_positions"],
            "last_run_utc": state["last_run"],
            "run_count": state["run_count"],
        })
    rows.sort(key=lambda r: (r["fwd_n"] or 0) * -1)
    return rows


def write_scoreboard(rows: list[dict]) -> None:
    (OUT_DIR / "scoreboard.json").write_text(json.dumps(rows, indent=2, default=str))
    if rows:
        fields = list(rows[0].keys())
        with open(OUT_DIR / "scoreboard.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)


def write_readout(rows: list[dict], hist_rows: int, fwd_rows: int) -> None:
    now_str = utc_now().strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Sprint075 — Top-10 Forward Dry-Run Scoreboard",
        "",
        f"**Updated:** {now_str}  ",
        f"**CSV total rows:** {hist_rows + fwd_rows}  (historical: {hist_rows}, forward: {fwd_rows})  ",
        f"**Forward cutoff:** {FORWARD_CUTOFF}  ",
        "**No live trading. No Redis signals. Isolated from paper shadow.**",
        "",
        "## Forward Performance (sorted by fwd_n desc)",
        "",
        "| ID | Horizon | Entry | fwd_n | fwd_PF | fwd_WR | fwd_EV | fwd_DD | top_pool | equity | open |",
        "|-----|---------|-------|-------|--------|--------|--------|--------|----------|--------|------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['strategy_id']} | {r['exit_horizon']} | {r['entry_description'][:30]} "
            f"| {r['fwd_n']} | {r['fwd_pf'] or '—'} | {r['fwd_wr'] or '—'} "
            f"| {r['fwd_ev'] or '—'} | {r['fwd_max_drawdown'] or '—'} "
            f"| {r['fwd_top_pool_share'] or '—'} | {r['fwd_equity']:.1f} | {r['fwd_open_positions']} |"
        )

    lines += [
        "",
        "## Historical Backtest (Sprint074 factory, pre-cutoff)",
        "",
        "| ID | Horizon | hist_n | hist_PF | hist_WR | hist_composite | HARD_EXIT_REQUIRED |",
        "|-----|---------|--------|---------|---------|----------------|-------------------|",
    ]
    for r in sorted(rows, key=lambda x: -x["hist_composite"]):
        he = "⚠️ YES" if r["HARD_EXIT_REQUIRED"] else "no"
        lines.append(
            f"| {r['strategy_id']} | {r['exit_horizon']} | {r['hist_n']} "
            f"| {r['hist_pf']} | {r['hist_wr']} | {r['hist_composite']} | {he} |"
        )

    lines += [
        "",
        "## Interpretation",
        "",
        "- **Historical** = paper-label backtest on pre-cutoff CSV; NOT mixed into fwd metrics.",
        "- **Forward** = signals observed after cutoff, closed when label available.",
        "- Small fwd_n → low confidence; watch for ≥20 trades before drawing conclusions.",
        f"- All 10 strategies have `HARD_EXIT_REQUIRED=False` (1h/2h exits).",
        "",
        "---",
        f"*Generated by sprint075_forward_harness.py at {now_str}*",
    ]
    (OUT_DIR / "sprint075_readout.md").write_text("\n".join(lines))


# ─── Main ────────────────────────────────────────────────────────────────────

def run_once() -> None:
    hist_rows, fwd_rows = load_csv()
    log(f"CSV: hist={len(hist_rows)} fwd={len(fwd_rows)}")

    results: list[tuple[dict, dict, dict]] = []
    for spec in STRATEGIES:
        state = run_strategy(spec, fwd_rows)
        fwd = forward_metrics(state, spec)
        results.append((spec, state, fwd))
        log(
            f"  {spec['strategy_id']} fwd_n={fwd['forward_n']} "
            f"pf={fwd['pf']} wr={fwd['wr']} eq={fwd['equity']:.2f} open={fwd['open_positions']}"
        )

    rows = build_scoreboard(results)
    write_scoreboard(rows)
    write_readout(rows, len(hist_rows), len(fwd_rows))
    log(f"Scoreboard written → {OUT_DIR / 'scoreboard.json'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--reset", action="store_true", help="Delete all per-strategy state files")
    args = parser.parse_args()

    if args.reset:
        for p in STATE_DIR.glob("*_state.json"):
            p.unlink()
        log("All state files deleted.")

    if args.once:
        run_once()
        return

    while True:
        run_once()
        time.sleep(300)


if __name__ == "__main__":
    main()
