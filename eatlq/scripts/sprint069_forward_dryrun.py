"""
sprint069_forward_dryrun.py
Forward dry-run sidecar for paper shadow.

Runs alongside sprint055_fee100_paper_shadow.py (paper shadow continues unchanged).
Reads new signals from logs/sprint055_paper_shadow.csv after freeze_time.
Maintains virtual account state for two strategies.

NO LIVE TRADING. NO THRESHOLD CHANGES. NO MODEL TRAINING.
DO NOT write private keys, RPC URLs, or API keys anywhere.

Strategies:
  D_entry_anchor_30m_fixed   — 10U/trade, 30m cooldown from ENTRY time, no score filter
                             (cooldown_anchor=entry_time — Sprint067 D-style; NOT A0 exit-anchor bug)
  A1_exit_anchor_1h_control  — 10U/trade, 1h cooldown from EXIT time, no score filter
                             (cooldown_anchor=exit_time — Sprint068 A1 control; separate from D)

Additional paper-shadow lines (e.g. alpha_009 sender-onset) use separate virtual accounts:
  D_entry_anchor_30m_fixed__alpha_009 , A1_exit_anchor_1h_control__alpha_009
so cooldown pools do not collide with the alpha_001 baseline CSV line.

Generated: sprint069, 2026-05-14 — patched sprint069b dogfood alpha_009 watcher
"""

import argparse
import csv
import fcntl
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT   = Path("/opt/EATLQ")
OUT_DIR     = REPO_ROOT / "mvp" / "audits" / "sprint069_forward_dryrun"
CONFIG_PATH = OUT_DIR / "forward_dryrun_config.json"
FREEZE_PATH = OUT_DIR / "forward_dryrun_freeze.json"
STATE_PATH  = OUT_DIR / "forward_dryrun_state.json"
LOCK_PATH   = Path("/tmp/sprint069_forward_dryrun.lock")
TRADES_PATH = OUT_DIR / "forward_dryrun_trades.csv"
POSITIONS_PATH = OUT_DIR / "forward_dryrun_positions.csv"
EQUITY_PATH = OUT_DIR / "forward_dryrun_equity_curve.csv"
DAILY_PATH  = OUT_DIR / "forward_dryrun_daily_summary.json"

TRADE_SIZE     = 10.0
INITIAL_EQUITY = 100.0
HOLD_SECONDS   = 1800

# Canonical line ids — primary CSV (sprint055) stays unprefixed in state for backward compat
BASELINE_LINE_ID = "alpha_001_baseline"


def line_account_key(strat_id: str, alpha_line_id: str) -> str:
    if alpha_line_id == BASELINE_LINE_ID:
        return strat_id
    return f"{strat_id}__{alpha_line_id}"


def iter_signal_sources(cfg: dict) -> list[tuple[str, Path]]:
    """(alpha_line_id, path relative to repo root). Primary always first."""
    primary_rel = cfg["signal_source"]["path"]
    out: list[tuple[str, Path]] = [(BASELINE_LINE_ID, REPO_ROOT / primary_rel)]
    for row in cfg.get("additional_signal_sources", []):
        aid = row.get("alpha_line_id") or row.get("alpha_id")
        rel = row.get("path")
        if not aid or not rel:
            continue
        out.append((aid, REPO_ROOT / rel))
    return out


# ─── Helpers ──────────────────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc)


def parse_dt(s):
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_freeze():
    with open(FREEZE_PATH) as f:
        return json.load(f)


def load_state():
    if not STATE_PATH.exists():
        return None
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        backup = STATE_PATH.with_suffix(
            f".corrupt_{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        STATE_PATH.replace(backup)
        print(f"[forward_dryrun] WARN: corrupt state moved to {backup}: {exc}", file=sys.stderr)
        return None


def count_forward_trades(state: dict) -> int:
    return sum(
        len([t for t in acct.get("closed_trades", []) if t.get("data_source") == "forward"])
        for acct in state.get("accounts", {}).values()
    )


def ensure_repair_baseline(state: dict) -> None:
    """Pin post-repair forward-N so cron growth is visible vs replay-inflated totals."""
    baseline = state.setdefault("repair_baseline", {})
    if baseline.get("forward_n_at_repair") is not None:
        return
    baseline.update({
        "established_at": state.get("initialized_at") or now_utc().isoformat(),
        "forward_n_at_repair": count_forward_trades(state),
        "run_count_at_repair": int(state.get("run_count") or 0),
        "note": "Incremental forward-N after state repair; not used for SOP promotion.",
    })


def update_incremental_metrics(state: dict) -> None:
    ensure_repair_baseline(state)
    baseline = state["repair_baseline"]
    forward_n = count_forward_trades(state)
    baseline_n = int(baseline.get("forward_n_at_repair") or 0)
    state["incremental_metrics"] = {
        "forward_n_total": forward_n,
        "forward_n_since_repair": max(0, forward_n - baseline_n),
        "run_count_since_repair": max(0, int(state.get("run_count") or 0) - int(baseline.get("run_count_at_repair") or 0)),
        "last_processed_by_line": dict(state.get("last_processed_by_line") or {}),
        "updated_at": now_utc().isoformat(),
    }


def save_state(state):
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    tmp.replace(STATE_PATH)


def read_csv_signals(csv_path: Path, alpha_line_id: str):
    """Return all rows from paper shadow CSV with exit_30m_ok=True.

    sprint055 (alpha_001) and sprint068 multi-alpha CSVs share the required columns.
    """
    rows = []
    if not csv_path.exists():
        return rows
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            if r.get("entry_quote_ok") != "True":
                continue
            if r.get("exit_30m_ok") != "True":
                continue
            rows.append({
                "signal_time":    r["signal_time"],
                "pool":           r["pool"],
                "ret_ag":         float(r["net_return_30m_ag"]),
                "ret_raw":        float(r["net_return_30m"]),
                "activity_heat":  float(r["activity_heat"]),
                "buy_pressure":   float(r["buy_pressure_ratio"]),
                "entry_block":    r["entry_block"],
                "exit_30m_block": r["exit_30m_block"],
                "gas_proxy_pct":  float(r["gas_proxy_pct"]),
                "_alpha_line":    alpha_line_id,
            })
    return sorted(rows, key=lambda x: x["signal_time"])


def read_csv_open_signals(csv_path: Path):
    """Return rows where entry_quote_ok=True but exit_30m_ok not yet True (pending exits)."""
    rows = []
    if not csv_path.exists():
        return rows
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            if r.get("entry_quote_ok") != "True":
                continue
            if r.get("exit_30m_ok") == "True":
                continue
            rows.append({
                "signal_time": r["signal_time"],
                "pool":        r["pool"],
                "entry_block": r["entry_block"],
                "activity_heat": float(r["activity_heat"]),
            })
    return sorted(rows, key=lambda x: x["signal_time"])


# ─── Account ──────────────────────────────────────────────────────────────────

def make_account(strategy_id, config_strat, display_strategy_id: str | None = None):
    sid = display_strategy_id or strategy_id
    return {
        "strategy_id":      sid,
        "role":             config_strat["role"],
        "cash":             INITIAL_EQUITY,
        "initial_equity":   INITIAL_EQUITY,
        "open_positions":   {},   # pool -> {entry_time, stake, signal_time}
        "closed_trades":    [],
        "equity_snapshots": [],
        "pool_last_entry":  {},   # pool -> ISO string
        "pool_last_exit":   {},   # pool -> ISO string
        "skipped_trades":   [],
        "stats": {
            "total_closed": 0,
            "total_wins": 0,
            "total_losses": 0,
            "gross_wins_u": 0.0,
            "gross_losses_u": 0.0,
            "realized_pnl": 0.0,
            "max_drawdown_pct": 0.0,
            "peak_equity": INITIAL_EQUITY,
            "skipped_capital": 0,
            "skipped_active_pool": 0,
            "skipped_cooldown": 0,
        },
        "forward_verdict": {
            "trade_count": 0,
            "win_rate": None,
            "PF": None,
            "verdict": "NOT_ENOUGH_TRADES",
        },
    }


def current_equity(acct):
    """cash + sum(open positions at cost — no unrealized mark)."""
    open_cost = sum(pos["stake"] for pos in acct["open_positions"].values())
    return acct["cash"] + open_cost


def mark_equity(acct, timestamp):
    eq = current_equity(acct)
    acct["equity_snapshots"].append({"time": timestamp, "equity": round(eq, 4)})
    # Update max drawdown
    stats = acct["stats"]
    if eq > stats["peak_equity"]:
        stats["peak_equity"] = eq
    dd = (stats["peak_equity"] - eq) / stats["peak_equity"]
    if dd > stats["max_drawdown_pct"] / 100:
        stats["max_drawdown_pct"] = round(dd * 100, 4)


# ─── Strategy execution ───────────────────────────────────────────────────────

def check_entry_eligible(acct, config_strat, pool, signal_time_str):
    """
    Returns (eligible: bool, skip_reason: str|None)
    Applies cooldown and active-position rules from config.
    Does NOT use future trade_count for sizing.
    """
    t = parse_dt(signal_time_str)
    cooldown_anchor  = config_strat["cooldown_anchor"]
    cooldown_seconds = config_strat["cooldown_seconds"]

    # 1. Same-pool active position
    if pool in acct["open_positions"]:
        return False, "SKIP_ACTIVE_SAME_POOL"

    # 2. Cooldown check
    if cooldown_anchor == "entry_time":
        last_t_str = acct["pool_last_entry"].get(pool)
        if last_t_str:
            elapsed = (t - parse_dt(last_t_str)).total_seconds()
            if elapsed < cooldown_seconds:
                return False, f"SKIP_COOLDOWN_ENTRY_ANCHOR ({elapsed:.0f}s < {cooldown_seconds}s)"
    elif cooldown_anchor == "exit_time":
        last_t_str = acct["pool_last_exit"].get(pool)
        if last_t_str:
            elapsed = (t - parse_dt(last_t_str)).total_seconds()
            if elapsed < cooldown_seconds:
                return False, f"SKIP_COOLDOWN_EXIT_ANCHOR ({elapsed:.0f}s < {cooldown_seconds}s)"

    # 3. Max concurrent positions
    if len(acct["open_positions"]) >= config_strat["max_concurrent_positions"]:
        return False, "SKIP_MAX_CONCURRENT"

    # 4. Capital
    if acct["cash"] < TRADE_SIZE:
        return False, f"SKIP_CAPITAL (cash={acct['cash']:.2f}U < {TRADE_SIZE}U)"

    return True, None


def enter_position(acct, pool, signal_time_str, stake):
    acct["cash"] -= stake
    acct["open_positions"][pool] = {
        "entry_time": signal_time_str,
        "expected_exit_time": (parse_dt(signal_time_str) + timedelta(seconds=HOLD_SECONDS)).isoformat(),
        "stake": stake,
        "pool": pool,
    }
    acct["pool_last_entry"][pool] = signal_time_str


def close_position(acct, pool, signal_row):
    pos = acct["open_positions"].pop(pool, None)
    if pos is None:
        return
    stake = pos["stake"]
    ret   = signal_row["ret_ag"]
    pnl   = stake * ret
    exit_time = (parse_dt(pos["entry_time"]) + timedelta(seconds=HOLD_SECONDS)).isoformat()

    acct["cash"] += stake + pnl
    acct["pool_last_exit"][pool] = exit_time

    trade_record = {
        "strategy_id":   acct["strategy_id"],
        "pool":          pool,
        "entry_time":    pos["entry_time"],
        "exit_time":     exit_time,
        "stake":         round(stake, 4),
        "ret_ag":        round(ret, 6),
        "pnl":           round(pnl, 4),
        "win":           pnl > 0,
        "entry_block":   signal_row.get("entry_block", ""),
        "exit_30m_block": signal_row.get("exit_30m_block", ""),
        "activity_heat": signal_row.get("activity_heat", ""),
        "alpha_line":    signal_row.get("_alpha_line", ""),
        "data_source":   signal_row.get("_source", "historical_sanity"),
    }
    acct["closed_trades"].append(trade_record)

    stats = acct["stats"]
    stats["total_closed"] += 1
    stats["realized_pnl"]  = round(stats["realized_pnl"] + pnl, 4)
    if pnl > 0:
        stats["total_wins"]    += 1
        stats["gross_wins_u"]   = round(stats["gross_wins_u"] + pnl, 4)
    else:
        stats["total_losses"]  += 1
        stats["gross_losses_u"] = round(stats["gross_losses_u"] + abs(pnl), 4)


def record_skip(acct, pool, signal_time_str, reason, data_source):
    acct["skipped_trades"].append({
        "pool": pool, "signal_time": signal_time_str,
        "reason": reason, "data_source": data_source,
    })
    stats = acct["stats"]
    if "CAPITAL" in reason:
        stats["skipped_capital"] += 1
    elif "ACTIVE_SAME_POOL" in reason:
        stats["skipped_active_pool"] += 1
    elif "COOLDOWN" in reason:
        stats["skipped_cooldown"] += 1


def compute_forward_verdict(acct):
    forward_trades = [t for t in acct["closed_trades"] if t["data_source"] == "forward"]
    n = len(forward_trades)
    acct["forward_verdict"]["trade_count"] = n
    if n < 10:
        acct["forward_verdict"]["verdict"] = "NOT_ENOUGH_TRADES"
        return
    rets   = [t["ret_ag"] for t in forward_trades]
    wins   = [t["pnl"] for t in forward_trades if t["pnl"] > 0]
    losses = [abs(t["pnl"]) for t in forward_trades if t["pnl"] <= 0]
    wr     = sum(1 for t in forward_trades if t["win"]) / n
    pf     = sum(wins) / sum(losses) if losses else float("inf")
    acct["forward_verdict"]["win_rate"] = round(wr, 4)
    acct["forward_verdict"]["PF"]       = round(pf, 4)
    acct["forward_verdict"]["verdict"]  = "NEEDS_MORE_TRADES" if n < 20 else (
        "PROFITABLE" if pf >= 1.2 and wr >= 0.50 else "LOSING"
    )


def process_signal_batch(accounts, configs, signals, data_source="historical_sanity"):
    """
    Process a batch of fully-labeled signals (entry_ok + exit_ok).
    Used for both historical sanity and forward processing (once exits are available).
    data_source: "historical_sanity" | "forward"
    """
    for sig in signals:
        pool         = sig["pool"]
        signal_time  = sig["signal_time"]
        sig["_source"] = data_source

        for strat_id, acct in accounts.items():
            config_strat = configs[strat_id]
            eligible, skip_reason = check_entry_eligible(acct, config_strat, pool, signal_time)
            if not eligible:
                record_skip(acct, pool, signal_time, skip_reason, data_source)
                continue
            enter_position(acct, pool, signal_time, TRADE_SIZE)
            close_position(acct, pool, sig)
            mark_equity(acct, signal_time)

    for strat_id, acct in accounts.items():
        compute_forward_verdict(acct)


# ─── Daily summary ────────────────────────────────────────────────────────────

def build_daily_summary(accounts, freeze_time_str, current_time_str, historical_sanity_cache=None):
    if historical_sanity_cache is None:
        historical_sanity_cache = {}
    summary = {
        "generated_at": current_time_str,
        "freeze_time": freeze_time_str,
        "strategies": {},
    }
    for strat_id, acct in accounts.items():
        stats = acct["stats"]
        n_closed = stats["total_closed"]
        fw_trades = [t for t in acct["closed_trades"] if t["data_source"] == "forward"]
        n_forward = len(fw_trades)
        eq = current_equity(acct)

        gross_wins   = stats["gross_wins_u"]
        gross_losses = stats["gross_losses_u"]
        pf = gross_wins / gross_losses if gross_losses > 0 else (
            float("inf") if gross_wins > 0 else None)
        wr = stats["total_wins"] / n_closed if n_closed > 0 else None

        pool_pnl = defaultdict(float)
        pool_n   = defaultdict(int)
        for t in acct["closed_trades"]:
            pool_pnl[t["pool"][:12]] += t["pnl"]
            pool_n[t["pool"][:12]]   += 1
        top_pool = max(pool_pnl, key=lambda p: pool_n[p]) if pool_pnl else None
        top_share = pool_pnl[top_pool] / stats["realized_pnl"] if (
            top_pool and stats["realized_pnl"] != 0) else None

        # Daily PnL by date
        daily = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
        for t in acct["closed_trades"]:
            d = t["entry_time"][:10]
            daily[d]["n"]    += 1
            daily[d]["pnl"]  += t["pnl"]
            daily[d]["wins"] += int(t["win"])

        max_loss = min((t["pnl"] for t in acct["closed_trades"]), default=None)

        # Use pre-computed historical sanity cache if accounts have no closed trades yet
        hs_cache = historical_sanity_cache.get(strat_id, {})
        has_local_history = any(t["data_source"] == "historical_sanity" for t in acct["closed_trades"])

        if has_local_history:
            hist_section = {
                "trade_count": len([t for t in acct["closed_trades"] if t["data_source"] == "historical_sanity"]),
                "win_rate": wr,
                "PF": round(pf, 4) if pf and pf != float("inf") else pf,
                "max_drawdown_pct": stats["max_drawdown_pct"],
                "max_single_loss_pct": round(min((t["ret_ag"] for t in acct["closed_trades"]), default=0)*100, 4),
                "realized_pnl": round(stats["realized_pnl"], 4),
                "pool_concentration": {p: {"n": pool_n[p], "pnl": round(pool_pnl[p], 4), "share_pct": round(pool_pnl[p]/stats["realized_pnl"]*100, 1) if stats["realized_pnl"] != 0 else None} for p in sorted(pool_n, key=lambda x: -pool_n[x])},
                "skipped_capital": stats["skipped_capital"],
                "skipped_active_pool": stats["skipped_active_pool"],
                "skipped_cooldown": stats["skipped_cooldown"],
                "daily_pnl": {d: {"n": v["n"], "pnl": round(v["pnl"], 4), "wr": round(v["wins"]/v["n"], 3)} for d, v in sorted(daily.items())},
                "label": "HISTORICAL_SANITY_ONLY — not used for forward verdict",
            }
        else:
            # Use pre-computed cache from state["historical_sanity"]
            hist_section = {**hs_cache, "label": "HISTORICAL_SANITY_ONLY (from state cache) — not used for forward verdict"}

        summary["strategies"][strat_id] = {
            "role":               acct["role"],
            "cash":               round(acct["cash"], 4),
            "open_positions":     len(acct["open_positions"]),
            "open_position_pools": list(acct["open_positions"].keys()),
            "current_equity":     round(eq, 4),
            "initial_equity":     INITIAL_EQUITY,
            "realized_pnl":       round(stats["realized_pnl"], 4),
            "total_return_pct":   round((eq - INITIAL_EQUITY) / INITIAL_EQUITY * 100, 4),
            "historical_sanity":  hist_section,
            "forward_only": {
                "trade_count": n_forward,
                "status": acct["forward_verdict"]["verdict"],
                "win_rate": acct["forward_verdict"].get("win_rate"),
                "PF": acct["forward_verdict"].get("PF"),
                "note": "Only trades with signal_time > freeze_time count toward forward verdict.",
            },
            "skipped_total": len(acct["skipped_trades"]),
        }
    return summary


# ─── Write artifacts ──────────────────────────────────────────────────────────

def write_trades_csv(accounts):
    all_trades = []
    for acct in accounts.values():
        all_trades.extend(acct["closed_trades"])
    if not all_trades:
        with open(TRADES_PATH, "w") as f:
            f.write("strategy_id,pool,entry_time,exit_time,stake,ret_ag,pnl,win,entry_block,exit_30m_block,activity_heat,alpha_line,data_source\n")
        return
    keys = ["strategy_id", "pool", "entry_time", "exit_time", "stake", "ret_ag", "pnl",
            "win", "entry_block", "exit_30m_block", "activity_heat", "alpha_line", "data_source"]
    with open(TRADES_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(all_trades, key=lambda x: (x["strategy_id"], x["entry_time"])))


def write_positions_csv(accounts):
    rows = []
    for acct in accounts.values():
        for pool, pos in acct["open_positions"].items():
            rows.append({
                "strategy_id": acct["strategy_id"],
                "pool": pool,
                "entry_time": pos["entry_time"],
                "expected_exit_time": pos["expected_exit_time"],
                "stake": pos["stake"],
                "status": "OPEN",
            })
    with open(POSITIONS_PATH, "w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        else:
            f.write("strategy_id,pool,entry_time,expected_exit_time,stake,status\n")


def write_equity_csv(accounts):
    rows = []
    for acct in accounts.values():
        for snap in acct["equity_snapshots"]:
            rows.append({
                "strategy_id": acct["strategy_id"],
                "time": snap["time"],
                "equity": snap["equity"],
            })
    with open(EQUITY_PATH, "w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=["strategy_id", "time", "equity"])
            w.writeheader()
            w.writerows(sorted(rows, key=lambda x: (x["strategy_id"], x["time"])))
        else:
            f.write("strategy_id,time,equity\n")


def write_daily_summary(summary):
    with open(DAILY_PATH, "w") as f:
        json.dump(summary, f, indent=2, default=str)


def write_state(state):
    save_state(state)


# ─── Main logic ───────────────────────────────────────────────────────────────

def init_state(config, freeze):
    """Initialize fresh state on first run — one account bundle per (strategy × alpha_line)."""
    state = {
        "schema_version": "sprint069_v1",
        "initialized_at": now_utc().isoformat(),
        "freeze_time": freeze["freeze_time"],
        "last_processed_by_line": {},
        "run_count": 0,
        "status": "INITIALIZED",
        "accounts": {},
    }
    sources = iter_signal_sources(config)
    for alpha_line_id, _p in sources:
        for strat_id, strat_cfg in config["strategies"].items():
            ak = line_account_key(strat_id, alpha_line_id)
            state["accounts"][ak] = make_account(
                strat_id, strat_cfg, display_strategy_id=ak
            )
    return state


def migrate_state_shape(state, config):
    """Backward compat: legacy single last_processed_signal + optional alpha_009 accounts."""
    state.setdefault("last_processed_by_line", {})
    lp_legacy = state.get("last_processed_signal")
    if lp_legacy is not None and BASELINE_LINE_ID not in state["last_processed_by_line"]:
        state["last_processed_by_line"][BASELINE_LINE_ID] = lp_legacy

    if state.get("historical_sanity_done") and "historical_sanity_done_by_line" not in state:
        state["historical_sanity_done_by_line"] = {BASELINE_LINE_ID: True}
    state.setdefault("historical_sanity_done_by_line", {})

    sources = iter_signal_sources(config)
    for alpha_line_id, _path in sources:
        for strat_id, strat_cfg in config["strategies"].items():
            ak = line_account_key(strat_id, alpha_line_id)
            if ak not in state["accounts"]:
                state["accounts"][ak] = make_account(
                    strat_id, strat_cfg, display_strategy_id=ak
                )


def run_once(historical_sanity=False, verbose=True):
    """Main sidecar entry point. Called by cron."""
    config = load_config()
    freeze = load_freeze()
    freeze_time = parse_dt(freeze["freeze_time"])
    current_time = now_utc()
    sources = iter_signal_sources(config)

    state = load_state()
    if state is None:
        state = init_state(config, freeze)
        if verbose:
            print(f"[forward_dryrun] Initialized fresh state at {current_time.isoformat()}")
    else:
        state["run_count"] = state.get("run_count", 0) + 1
        migrate_state_shape(state, config)

    accounts = state["accounts"]
    hs_done_map = state.setdefault("historical_sanity_done_by_line", {})

    # Legacy state: baseline historical pass already ran
    if state.get("historical_sanity_done") and BASELINE_LINE_ID not in hs_done_map:
        hs_done_map[BASELINE_LINE_ID] = True

    state.setdefault("historical_sanity", {})

    # ── Historical sanity per alpha_line (first attach / --sanity) ─────────────
    for alpha_line_id, csv_path in sources:
        if not csv_path.exists():
            if alpha_line_id == BASELINE_LINE_ID:
                if verbose:
                    print(f"[forward_dryrun] ERROR: baseline CSV missing: {csv_path}")
            elif verbose:
                print(
                    f"[forward_dryrun] WARN: optional signal CSV missing — line={alpha_line_id} path={csv_path} "
                    f"(watcher is configured; no alpha_009 forward history until file exists)"
                )
            continue

        must_run = historical_sanity or not hs_done_map.get(alpha_line_id, False)
        if not must_run:
            continue

        hist_all = read_csv_signals(csv_path, alpha_line_id)
        historical = [s for s in hist_all if parse_dt(s["signal_time"]) <= freeze_time]
        if verbose:
            print(
                f"[forward_dryrun] Historical sanity line={alpha_line_id} path={csv_path} "
                f"n_labeled={len(hist_all)} n_hist={len(historical)}"
            )

        sanity_accounts: dict[str, dict] = {}
        for strat_id, strat_cfg in config["strategies"].items():
            ak = line_account_key(strat_id, alpha_line_id)
            sanity_accounts[strat_id] = make_account(
                strat_id, strat_cfg, display_strategy_id=ak
            )
        process_signal_batch(sanity_accounts, config["strategies"], historical, "historical_sanity")

        for strat_id, acct in sanity_accounts.items():
            ak = line_account_key(strat_id, alpha_line_id)
            stats = acct["stats"]
            n = stats["total_closed"]
            wr = stats["total_wins"] / n if n > 0 else None
            pf = stats["gross_wins_u"] / stats["gross_losses_u"] if stats["gross_losses_u"] > 0 else None
            state["historical_sanity"][ak] = {
                "alpha_line": alpha_line_id,
                "trade_count": n,
                "equity": round(current_equity(acct), 4),
                "realized_pnl": round(stats["realized_pnl"], 4),
                "win_rate": round(wr, 4) if wr else None,
                "PF": round(pf, 4) if pf else None,
                "max_drawdown_pct": stats["max_drawdown_pct"],
                "skipped": {
                    "capital": stats["skipped_capital"],
                    "active_pool": stats["skipped_active_pool"],
                    "cooldown": stats["skipped_cooldown"],
                },
                "label": "HISTORICAL_SANITY_ONLY",
            }
            accounts[ak] = acct
            if verbose:
                wr_str = f"{wr:.3f}" if wr is not None else "N/A"
                pf_str = f"{pf:.3f}" if pf is not None else "N/A"
                print(
                    f"  [{ak}] historical sanity: trades={n}  "
                    f"equity={round(current_equity(acct), 2)}U  "
                    f"WR={wr_str}  PF={pf_str}"
                )
        hs_done_map[alpha_line_id] = True

    state["historical_sanity_done"] = all(
        (not p.exists() and lid != BASELINE_LINE_ID) or hs_done_map.get(lid, False)
        for lid, p in sources
    )

    # ── Forward (per line, ENTRY-anchor cooldown for D; EXIT for A1) ─────────
    last_map = state.setdefault("last_processed_by_line", {})
    if state.get("last_processed_signal") and BASELINE_LINE_ID not in last_map:
        last_map[BASELINE_LINE_ID] = state["last_processed_signal"]

    n_open_total = sum(len(read_csv_open_signals(p)) for _lid, p in sources)
    any_new_forward = False

    for alpha_line_id, csv_path in sources:
        if not csv_path.exists():
            continue

        all_sig = read_csv_signals(csv_path, alpha_line_id)
        forward = [s for s in all_sig if parse_dt(s["signal_time"]) > freeze_time]
        last_p = last_map.get(alpha_line_id)
        new_forward = [s for s in forward if last_p is None or s["signal_time"] > last_p]
        if not new_forward:
            continue
        any_new_forward = True
        if verbose:
            print(f"[forward_dryrun] line={alpha_line_id}: processing {len(new_forward)} new forward signal(s)...")
        sub_accounts = {
            sid: accounts[line_account_key(sid, alpha_line_id)]
            for sid in config["strategies"]
        }
        process_signal_batch(sub_accounts, config["strategies"], new_forward, "forward")
        last_map[alpha_line_id] = new_forward[-1]["signal_time"]
        if verbose:
            for sid, acct in sub_accounts.items():
                ak = line_account_key(sid, alpha_line_id)
                fw = [t for t in acct["closed_trades"] if t["data_source"] == "forward"]
                print(f"  [{ak}] forward trades so far: {len(fw)}")

    if not any_new_forward and verbose:
        print(
            f"[forward_dryrun] No new forward signals on any line. "
            f"Pending open rows (all lines): {n_open_total}.  Status: WAITING_FOR_SIGNAL"
        )

    n_forward_total = sum(
        len([t for t in acct["closed_trades"] if t["data_source"] == "forward"])
        for acct in accounts.values()
    )
    if n_forward_total == 0:
        state["status"] = "WAITING_FOR_SIGNAL"
    else:
        state["status"] = "RUNNING"

    state["last_run"] = current_time.isoformat()
    state["paper_shadow_continues"] = True

    update_incremental_metrics(state)

    write_trades_csv(accounts)
    write_positions_csv(accounts)
    write_equity_csv(accounts)

    summary = build_daily_summary(accounts, freeze["freeze_time"], current_time.isoformat(),
                                  state.get("historical_sanity", {}))
    write_daily_summary(summary)
    write_state(state)

    if verbose:
        print(f"[forward_dryrun] status={state['status']}  "
              f"run_count={state.get('run_count', 1)}  "
              f"last_run={state['last_run']}")
        print(f"[forward_dryrun] Artifacts written to {OUT_DIR}")

    return state


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sprint069 forward dry-run sidecar")
    parser.add_argument("--once",     action="store_true", help="Run one cycle (for cron)")
    parser.add_argument("--sanity",   action="store_true", help="Force re-run historical sanity")
    parser.add_argument("--status",   action="store_true", help="Print status only")
    parser.add_argument("--quiet",    action="store_true", help="Suppress verbose output")
    args = parser.parse_args()

    if args.status:
        state = load_state()
        if state is None:
            print("NOT_INITIALIZED")
        else:
            print(f"status={state.get('status')}  "
                  f"run_count={state.get('run_count',0)}  "
                  f"last_run={state.get('last_run','never')}")
        sys.exit(0)

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as lock_fh:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[forward_dryrun] Another instance is running; exiting.", file=sys.stderr)
            sys.exit(0)
        run_once(historical_sanity=args.sanity, verbose=not args.quiet)
