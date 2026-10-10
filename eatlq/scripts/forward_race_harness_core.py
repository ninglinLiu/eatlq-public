"""
Shared forward-only race harness primitives (Sprint077+).
Label-gated closes + optional hard horizon synthetic exit for long holds.
No Redis, no production signals.
"""
from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

FORWARD_CUTOFF_DEFAULT = "2026-05-15T14:00:00+00:00"
TRADE_SIZE = 10.0
INITIAL_EQ = 100.0
AGE_MIN_SEC = 1800.0
AGE_MAX_SEC = 86400.0
COOLDOWN_SEC = 1800
MAX_CONCURRENT = 3

# After horizon time, wait this long for CSV label; then force synthetic flat exit (paper/dry-run).
HARD_EXIT_LABEL_WAIT_SEC = 3600

HORIZONS = ("30m", "1h", "2h", "4h", "6h", "12h", "24h")


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


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


def horizon_delta(h: str) -> timedelta:
    h = h.strip().lower()
    if h.endswith("m"):
        return timedelta(minutes=int(h[:-1]))
    if h.endswith("h"):
        return timedelta(hours=int(h[:-1]))
    raise ValueError(f"bad horizon {h}")


def load_csv_split(
    csv_path: Path,
    forward_cutoff_iso: str,
    *,
    fee_bps_in: set[int] | None = None,
    fee_max_bps: float | None = None,
    require_sender_gated: bool | None = None,
    exclude_pools: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    cutoff = parse_dt(forward_cutoff_iso)
    hist, fwd = [], []
    excl = {p.lower() for p in (exclude_pools or set())}
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            if r.get("entry_quote_ok") != "True":
                continue
            pool = (r.get("pool") or "").lower()
            if pool in excl:
                continue
            try:
                fee = float(r.get("fee_bps") or 0)
                if fee_bps_in is not None and int(fee) not in fee_bps_in:
                    continue
                if fee_max_bps is not None and fee > fee_max_bps:
                    continue
                if require_sender_gated is True and r.get("is_0x6ff5") != "True":
                    continue
                if require_sender_gated is False and r.get("is_0x6ff5") == "True":
                    continue
                row = {
                    "pool": r["pool"],
                    "signal_time": r["signal_time"],
                    "fee_bps": fee,
                    "pool_age_sec": float(r["pool_age_sec"]) if r.get("pool_age_sec") else None,
                    "activity_heat": float(r.get("activity_heat") or 0),
                    "buy_pressure_ratio": float(r.get("buy_pressure_ratio") or 0),
                    "price_velocity_5m": float(r.get("price_velocity_5m") or 0),
                    "is_0x6ff5": r.get("is_0x6ff5") == "True",
                    "composite_flag": r.get("composite_flag", "False") in ("True", "true", "1"),
                    "dominant_sender": r.get("dominant_sender") or "",
                }
                for h in HORIZONS:
                    ok_k, ag_k = f"exit_{h}_ok", f"net_return_{h}_ag"
                    row[f"ok_{h}"] = r.get(ok_k) == "True"
                    v = r.get(ag_k)
                    row[f"retag_{h}"] = float(v) if v not in (None, "", "\\N") else None
                sig_dt = parse_dt(r["signal_time"])
                (fwd if sig_dt > cutoff else hist).append(row)
            except (ValueError, KeyError):
                continue
    hist.sort(key=lambda x: x["signal_time"])
    fwd.sort(key=lambda x: x["signal_time"])
    return hist, fwd


def passes_entry(row: dict, spec: dict, *, pool_trade_counts: dict[str, int] | None = None) -> tuple[bool, str]:
    age = row.get("pool_age_sec")
    age_min = spec.get("age_min_sec", AGE_MIN_SEC)
    age_max = spec.get("age_max_sec", AGE_MAX_SEC)
    if age is None or not (age_min <= age <= age_max):
        return False, "age"
    if spec.get("fee_bps_in") is not None and int(row["fee_bps"]) not in spec["fee_bps_in"]:
        return False, "fee_tier"
    if spec.get("fee_max_bps") is not None and row["fee_bps"] > spec["fee_max_bps"]:
        return False, "fee"
    if spec.get("require_sender_gated") and not row.get("is_0x6ff5"):
        return False, "sender"
    if spec.get("require_composite") and not row.get("composite_flag"):
        return False, "composite"
    if spec.get("exclude_toxic_pools"):
        if row["pool"].lower() in spec["exclude_toxic_pools"]:
            return False, "toxic"
    if spec.get("pv_min") is not None and row["price_velocity_5m"] < spec["pv_min"]:
        return False, "pv"
    if spec.get("momentum_gate"):
        if not (row["buy_pressure_ratio"] >= 0.55 and row["price_velocity_5m"] > 0):
            return False, "momentum"
    if spec.get("max_trades_per_pool") is not None and pool_trade_counts is not None:
        cap = int(spec["max_trades_per_pool"])
        if pool_trade_counts.get(row["pool"].lower(), 0) >= cap:
            return False, "pool_cap"
    return True, ""


def forward_metrics(state: dict, stats: dict) -> dict:
    trades = state["trades"]
    n = len(trades)
    if n == 0:
        return {
            "forward_n": 0,
            "pf": None,
            "wr": None,
            "ev": None,
            "payoff_ratio": None,
            "max_drawdown": None,
            "top_pool_share": None,
            "quote_fail_proxy": None,
            "equity": state["equity"],
            "hard_exit_forced_count": stats.get("hard_exit_forced", 0),
        }

    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [abs(t["pnl"]) for t in trades if t["pnl"] < 0]
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
        "hard_exit_forced_count": stats.get("hard_exit_forced", 0),
    }


def empty_state(sid: str) -> dict:
    return {
        "strategy_id": sid,
        "harness": "forward_race",
        "created_at": utc_now().isoformat(),
        "last_run": None,
        "run_count": 0,
        "equity": INITIAL_EQ,
        "open_positions": {},
        "pool_last_entry": {},
        "processed_signals": [],
        "trades": [],
        "hard_exit_intents": [],
        "stats": {
            "forward_n": 0,
            "wins": 0,
            "losses": 0,
            "total_gain": 0.0,
            "total_loss_abs": 0.0,
            "skipped_cooldown": 0,
            "skipped_age": 0,
            "skipped_fee": 0,
            "skipped_momentum": 0,
            "skipped_pv": 0,
            "skipped_no_label": 0,
            "skipped_concurrent": 0,
            "hard_exit_forced": 0,
        },
    }


def load_state(state_dir: Path, sid: str) -> dict:
    p = state_dir / f"{sid}_state.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    st = empty_state(sid)
    st["strategy_id"] = sid
    return st


def save_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{state['strategy_id']}_state.json").write_text(
        json.dumps(state, indent=2, default=str)
    )


def wants_hard_exit(spec: dict) -> bool:
    if spec.get("hard_horizon_exit"):
        return True
    h = spec.get("exit_horizon", "")
    return h in ("4h", "6h", "12h", "24h")


def run_strategy(
    spec: dict,
    fwd_rows: list[dict],
    *,
    state_dir: Path,
    log_prefix: str = "[harness]",
) -> dict:
    """Run one strategy tick: entries + closes (label or hard synthetic)."""
    sid = spec["strategy_id"]
    horizon = spec["exit_horizon"]
    state = load_state(state_dir, sid)
    state["last_run"] = utc_now().isoformat()
    state["run_count"] = state.get("run_count", 0) + 1
    if "hard_exit_intents" not in state:
        state["hard_exit_intents"] = []

    all_known: set[str] = set(state.get("processed_signals", [])) | set(state["open_positions"].keys()) | {
        t["sig_key"] for t in state["trades"]
    }
    if "processed_signals" not in state:
        state["processed_signals"] = []

    stats = state["stats"]
    if "hard_exit_forced" not in stats:
        stats["hard_exit_forced"] = 0
    pool_trade_counts: dict[str, int] = defaultdict(int)
    for t in state.get("trades", []):
        pool_trade_counts[(t.get("pool") or "").lower()] += 1
    for pos in state.get("open_positions", {}).values():
        pool_trade_counts[(pos.get("pool") or "").lower()] += 1

    csv_by_sk = {sig_key(r["pool"], r["signal_time"]): r for r in fwd_rows}
    now = utc_now()
    hard_mode = wants_hard_exit(spec)

    # New signals
    for row in fwd_rows:
        sk = sig_key(row["pool"], row["signal_time"])
        if sk in all_known:
            continue
        state["processed_signals"].append(sk)
        all_known.add(sk)

        ok, reason = passes_entry(row, spec, pool_trade_counts=pool_trade_counts)
        if not ok:
            stats[f"skipped_{reason}"] = stats.get(f"skipped_{reason}", 0) + 1
            continue

        last_entry = state["pool_last_entry"].get(row["pool"])
        if last_entry:
            elapsed = (parse_dt(row["signal_time"]) - parse_dt(last_entry)).total_seconds()
            if elapsed < COOLDOWN_SEC:
                stats["skipped_cooldown"] = stats.get("skipped_cooldown", 0) + 1
                continue

        if row["pool"] in state["open_positions"]:
            stats["skipped_concurrent"] = stats.get("skipped_concurrent", 0) + 1
            continue
        if len(state["open_positions"]) >= MAX_CONCURRENT:
            stats["skipped_concurrent"] = stats.get("skipped_concurrent", 0) + 1
            continue

        exp = parse_dt(row["signal_time"]) + horizon_delta(horizon)
        state["open_positions"][sk] = {
            "sig_key": sk,
            "pool": row["pool"],
            "signal_time": row["signal_time"],
            "expected_exit": exp.isoformat(),
            "fee_bps": row["fee_bps"],
            "pool_age_sec": row.get("pool_age_sec"),
            "bpr": row["buy_pressure_ratio"],
            "pv": row["price_velocity_5m"],
        }
        state["pool_last_entry"][row["pool"]] = row["signal_time"]

    # Close matured
    to_close: list[tuple[str, dict, float, str]] = []

    for sk, pos in list(state["open_positions"].items()):
        exp = parse_dt(pos["expected_exit"])
        if now < exp:
            continue
        row = csv_by_sk.get(sk)
        if row is None:
            continue

        ret = row.get(f"retag_{horizon}")
        label_ok = row.get(f"ok_{horizon}") is True and ret is not None

        if label_ok:
            to_close.append((sk, pos, float(ret), "LABEL_EXIT"))
            continue

        if hard_mode and (now - exp).total_seconds() >= HARD_EXIT_LABEL_WAIT_SEC:
            intent = {
                "ts_utc": now.isoformat(),
                "strategy_id": sid,
                "pool": pos["pool"],
                "signal_time": pos["signal_time"],
                "intent": "SCHEDULED_CLOSE_SYNTHETIC_FLAT",
                "dry_run": True,
                "redis_writes": False,
                "production_signals": False,
                "reason": "hard_horizon_exit_no_label_after_grace",
                "exit_horizon": horizon,
            }
            state["hard_exit_intents"].append(intent)
            to_close.append((sk, pos, 0.0, "HARD_SYNTHETIC_ZERO"))
        else:
            stats["skipped_no_label"] = stats.get("skipped_no_label", 0) + 1

    for sk, pos, ret, kind in to_close:
        state["open_positions"].pop(sk, None)
        pnl = TRADE_SIZE * ret
        state["equity"] = round(state["equity"] + pnl, 6)
        win = pnl > 0
        stats["forward_n"] = stats.get("forward_n", 0) + 1
        if kind == "HARD_SYNTHETIC_ZERO":
            stats["hard_exit_forced"] = stats.get("hard_exit_forced", 0) + 1
        if pnl > 0:
            stats["wins"] = stats.get("wins", 0) + 1
            stats["total_gain"] = stats.get("total_gain", 0.0) + pnl
        elif pnl < 0:
            stats["losses"] = stats.get("losses", 0) + 1
            stats["total_loss_abs"] = stats.get("total_loss_abs", 0.0) + abs(pnl)
        # pnl == 0: breakeven — counts toward forward_n only (no win/loss PF distortion)
        state["trades"].append(
            {
                "sig_key": sk,
                "pool": pos["pool"],
                "signal_time": pos["signal_time"],
                "ret_ag": round(ret, 6),
                "pnl": round(pnl, 6),
                "win": win if pnl != 0 else None,
                "exit_kind": kind,
            }
        )

    save_state(state_dir, state)
    return state


def append_summary(state_dir: Path, sid: str, summary: dict) -> None:
    d = state_dir / "summaries"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}_last_summary.json").write_text(json.dumps(summary, indent=2, default=str))
