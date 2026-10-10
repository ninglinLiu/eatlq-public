"""
Sprint 073 — A6_diversity_ge20 + 4h exit + age gate: Research Dry-Run Sidecar
==============================================================================
Research-only strategy account that runs alongside the existing paper shadow.

Strategy rules (A6_diversity_4h):
  Entry gate 1: buyer_diversity_score >= DIVERSITY_GATE (pre-entry only window,
                NO lookahead: blocks [entry_block-200, entry_block-1])
  Entry gate 2: pool_age_sec in [AGE_MIN_SEC, AGE_MAX_SEC]
  Exit horizon: 4h from entry (reads net_return_4h_ag from paper shadow CSV)
  Cooldown:     4h from EXIT time per pool (exit-anchor)
  Stake:        10.0 USD equivalent per trade

NO live trades. NO wallet access. Reads paper shadow CSV + ClickHouse only.

Cron: add alongside existing paper shadow:
  */5 * * * * cd /opt/EATLQ && python3 scripts/sprint073_a6_diversity_4h_dryrun.py \
             --once >> logs/sprint073_a6_dryrun.log 2>&1

State:   logs/sprint073_a6_dryrun_state.json
Output:  mvp/audits/sprint073_a6_dryrun/
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── Config ────────────────────────────────────────────────────────────────
REPO              = Path("/opt/EATLQ")
SRC_CSV           = REPO / "logs/sprint055_paper_shadow.csv"
STATE_FILE        = REPO / "logs/sprint073_a6_dryrun_state.json"
OUT_DIR           = REPO / "mvp/audits/sprint073_a6_dryrun"
SUMMARY_JSON      = OUT_DIR / "sprint073_a6_dryrun_summary.json"

# Strategy gates
DIVERSITY_GATE    = 20           # pre-only buyer diversity score threshold
AGE_MIN_SEC       = 3_600        # pool must be ≥ 1h old
AGE_MAX_SEC       = 100_000      # pool must be ≤ ~27.8h old (100k sec)
EXIT_HORIZON_SEC  = 4 * 3600    # 4h exit
COOLDOWN_SEC      = 4 * 3600    # 4h exit-anchor cooldown per pool
DIVERSITY_WINDOW  = 200          # blocks BEFORE entry (pre-only, no lookahead)
STAKE             = 10.0
INITIAL_EQ        = 100.0

CHAIN_ID = 8453
MEM_CAP  = "max_memory_usage=3000000000"

OUT_DIR.mkdir(parents=True, exist_ok=True)

# ─── Helpers ───────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[sprint073_a6_dryrun] {msg}", flush=True)


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def parse_dt(s: str) -> datetime:
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s.replace("Z", "+00:00"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {s!r}")


def ch(sql: str, timeout: int = 45) -> tuple[str, str]:
    r = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
         "--database", "eatlq", "--format", "TSV",
         "--query", sql],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout.strip(), r.stderr.strip()


# ─── State I/O ─────────────────────────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "strategy_id":     "A6_diversity_ge20_4h_exit_age_gate",
        "version":         "sprint073",
        "created_at":      utc_now().isoformat(),
        "last_run":        None,
        "run_count":       0,
        "equity":          INITIAL_EQ,
        "open_positions":  {},   # signal_key -> position dict
        "pool_last_exit":  {},   # pool -> ISO exit time
        "trades":          [],   # closed trade records
        # All processed signal keys (opened + gate-rejected) — prevents re-processing
        "processed_signals": [],
        "stats": {
            "total_trades": 0, "wins": 0, "losses": 0,
            "total_gain": 0.0, "total_loss": 0.0,
            "signals_seen": 0, "gates_passed": 0,
            "gate_diversity_fail": 0, "gate_age_fail": 0,
            "gate_cooldown_fail": 0,
        },
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return _empty_state()


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ─── Paper shadow CSV reader ─────────────────────────────────────────────

def load_csv_rows() -> list[dict]:
    """Load all rows from paper shadow CSV that have entry_quote_ok=True."""
    rows = []
    with open(SRC_CSV) as f:
        for r in csv.DictReader(f):
            if r.get("entry_quote_ok") != "True":
                continue
            try:
                eb = int(r.get("entry_block", 0))
                if eb == 0:
                    continue
                age = float(r.get("pool_age_sec", 0) or 0)
                rows.append({
                    "pool":         r["pool"],
                    "signal_time":  r["signal_time"],
                    "entry_block":  eb,
                    "pool_age_sec": age,
                    # 4h exit data (may be empty for recent signals)
                    "exit_4h_ok":      r.get("exit_4h_ok", ""),
                    "net_return_4h_ag": r.get("net_return_4h_ag", ""),
                    # 30m exit (for cooldown tracking)
                    "exit_30m_ok": r.get("exit_30m_ok", ""),
                })
            except (ValueError, KeyError):
                continue
    return rows


# ─── Diversity score (pre-only, no lookahead) ───────────────────────────

def fetch_diversity_pre_only(pool: str, entry_block: int) -> int | None:
    """
    SUM(per-block distinct recipients) in [entry_block - DIVERSITY_WINDOW, entry_block - 1].

    This matches the backtest definition exactly:
      - For each block in the window, count DISTINCT recipient addresses.
      - Sum those per-block counts across the entire window.
    This is a "buyer diversity score" that rewards both breadth (many unique buyers)
    and activity (many buy events across multiple blocks).

    Window: [entry_block-200, entry_block-1] — pre-entry only, NO lookahead.
    """
    lo = max(0, entry_block - DIVERSITY_WINDOW)
    hi = entry_block - 1
    sql = f"""
SELECT sum(per_block_div)
FROM (
    SELECT block_number, count(DISTINCT recipient) AS per_block_div
    FROM eatlq.uniswap_v3_swaps
    WHERE chain_id = {CHAIN_ID}
      AND pool_address = '{pool}'
      AND block_number BETWEEN {lo} AND {hi}
    GROUP BY block_number
)
SETTINGS {MEM_CAP}
"""
    out, err = ch(sql)
    out = out.strip()
    if out and out not in ("", "\\N", "NULL"):
        try:
            return int(float(out))
        except ValueError:
            pass
    return None


# ─── Signal key ─────────────────────────────────────────────────────────

def sig_key(pool: str, signal_time: str) -> str:
    return f"{pool}::{signal_time}"


# ─── Main run ────────────────────────────────────────────────────────────

def run_once(args) -> None:
    state = load_state()
    now   = utc_now()

    state["last_run"] = now.isoformat()
    state["run_count"] = state.get("run_count", 0) + 1

    rows = load_csv_rows()
    log(f"CSV rows: {len(rows)} | open positions: {len(state['open_positions'])} | equity: {state['equity']:.4f}")

    all_known: set[str] = (
        set(state["open_positions"].keys())
        | {t["signal_key"] for t in state.get("trades", [])}
        | set(state.get("processed_signals", []))
    )
    if "processed_signals" not in state:
        state["processed_signals"] = []

    new_signals = 0
    for row in rows:
        sk = sig_key(row["pool"], row["signal_time"])
        if sk in all_known:
            continue  # already fully processed

        # Mark as seen immediately so re-runs don't recount
        state["processed_signals"].append(sk)
        all_known.add(sk)
        state["stats"]["signals_seen"] += 1
        new_signals += 1
        pool       = row["pool"]
        sig_time   = row["signal_time"]
        entry_blk  = row["entry_block"]
        age_sec    = row["pool_age_sec"]

        # Gate 1: pool age
        if not (AGE_MIN_SEC <= age_sec <= AGE_MAX_SEC):
            state["stats"]["gate_age_fail"] += 1
            continue

        # Gate 2: cooldown (exit-anchor)
        last_exit_str = state["pool_last_exit"].get(pool)
        if last_exit_str:
            last_exit = parse_dt(last_exit_str)
            sig_dt = parse_dt(sig_time)
            if sig_dt < last_exit + timedelta(seconds=COOLDOWN_SEC):
                state["stats"]["gate_cooldown_fail"] += 1
                continue

        # Gate 3: buyer diversity (pre-only, no lookahead)
        div_score = fetch_diversity_pre_only(pool, entry_blk)
        if div_score is None or div_score < DIVERSITY_GATE:
            state["stats"]["gate_diversity_fail"] += 1
            continue

        # All gates passed — open position
        state["stats"]["gates_passed"] += 1
        entry_dt = parse_dt(sig_time)
        expected_exit = (entry_dt + timedelta(seconds=EXIT_HORIZON_SEC)).isoformat()

        state["open_positions"][sk] = {
            "signal_key":     sk,
            "pool":           pool,
            "signal_time":    sig_time,
            "entry_block":    entry_blk,
            "pool_age_sec":   age_sec,
            "diversity_score": div_score,
            "stake":          STAKE,
            "expected_exit":  expected_exit,
        }
        log(f"  OPEN  {pool[:12]}… t={sig_time} div={div_score} age={age_sec:.0f}s exit@{expected_exit}")

    if new_signals:
        log(f"New signals processed: {new_signals}")

    # ── Close matured positions ──────────────────────────────────────────
    # Build CSV lookup for 4h returns
    csv_by_key = {sig_key(r["pool"], r["signal_time"]): r for r in rows}

    positions_to_close = []
    for sk, pos in state["open_positions"].items():
        expected_exit_dt = parse_dt(pos["expected_exit"])
        if now < expected_exit_dt:
            continue  # not yet matured

        # Check if 4h return is available in CSV
        csv_row = csv_by_key.get(sk)
        if not csv_row:
            log(f"  WARNING: position {sk} not in CSV, skip close")
            continue

        ret4h_str = csv_row.get("net_return_4h_ag", "")
        exit_ok   = csv_row.get("exit_4h_ok", "")

        if exit_ok != "True" or not ret4h_str:
            continue  # 4h label not yet available

        positions_to_close.append(sk)

    for sk in positions_to_close:
        pos      = state["open_positions"].pop(sk)
        csv_row  = csv_by_key[sk]
        ret4h    = float(csv_row["net_return_4h_ag"])
        pnl      = pos["stake"] * ret4h
        win      = ret4h > 0
        exit_dt  = (parse_dt(pos["signal_time"]) + timedelta(seconds=EXIT_HORIZON_SEC)).isoformat()

        state["equity"] += pnl
        state["pool_last_exit"][pos["pool"]] = exit_dt

        stats = state["stats"]
        stats["total_trades"] += 1
        if win:
            stats["wins"]       += 1
            stats["total_gain"] += pnl
        else:
            stats["losses"]     += 1
            stats["total_loss"] += abs(pnl)

        trade_record = {
            "signal_key":      sk,
            "pool":            pos["pool"],
            "signal_time":     pos["signal_time"],
            "expected_exit":   pos["expected_exit"],
            "diversity_score": pos["diversity_score"],
            "pool_age_sec":    pos["pool_age_sec"],
            "stake":           pos["stake"],
            "ret_4h":          ret4h,
            "pnl":             round(pnl, 6),
            "win":             win,
            "equity_after":    round(state["equity"], 4),
        }
        state["trades"].append(trade_record)
        log(f"  CLOSE {pos['pool'][:12]}… ret4h={ret4h:.4f} pnl={pnl:.4f} eq={state['equity']:.4f}")

    # ── Compute live stats ────────────────────────────────────────────────
    stats   = state["stats"]
    n       = stats["total_trades"]
    wins    = stats["wins"]
    gains   = stats["total_gain"]
    losses  = stats["total_loss"]
    pf      = gains / losses if losses > 0 else float("inf")
    wr      = wins / n if n > 0 else 0.0
    ret_pct = (state["equity"] - INITIAL_EQ) / INITIAL_EQ * 100

    log(f"  SUMMARY: trades={n} WR={wr:.1%} PF={pf:.4f} eq={state['equity']:.4f} ret={ret_pct:.2f}%")
    log(f"  GATES:   seen={stats['signals_seen']} passed={stats['gates_passed']} "
        f"age_fail={stats['gate_age_fail']} div_fail={stats['gate_diversity_fail']} "
        f"cd_fail={stats['gate_cooldown_fail']}")
    log(f"  OPEN:    {len(state['open_positions'])} positions pending")

    save_state(state)

    # ── Write summary JSON ───────────────────────────────────────────────
    summary = {
        "strategy_id":     state["strategy_id"],
        "last_run_utc":    state["last_run"],
        "run_count":       state["run_count"],
        "equity":          round(state["equity"], 4),
        "return_pct":      round(ret_pct, 2),
        "total_trades":    n,
        "win_rate":        round(wr, 4),
        "profit_factor":   round(pf, 4) if pf != float("inf") else "inf",
        "open_positions":  len(state["open_positions"]),
        "stats":           stats,
        "config": {
            "diversity_gate":    DIVERSITY_GATE,
            "diversity_window":  f"[entry_block-{DIVERSITY_WINDOW}, entry_block-1] (pre-only, no lookahead)",
            "age_gate_sec":      [AGE_MIN_SEC, AGE_MAX_SEC],
            "exit_horizon_sec":  EXIT_HORIZON_SEC,
            "cooldown_sec":      COOLDOWN_SEC,
            "stake_usd":         STAKE,
        },
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2, default=str))


# ─── Entrypoint ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one iteration then exit")
    parser.add_argument("--reset", action="store_true", help="Reset state file and start fresh")
    args = parser.parse_args()

    if args.reset:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        log("State reset.")

    if args.once:
        run_once(args)
        return

    while True:
        run_once(args)
        time.sleep(300)  # 5 minutes


if __name__ == "__main__":
    main()
