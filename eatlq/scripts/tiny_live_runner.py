#!/usr/bin/env python3
"""
Tiny live runner — Path A micro live execution.

Bypasses missing executor main.py; tails paper shadow CSV and executes
D_entry_anchor_30m_fixed rules on a whitelist of pools.

Session-only private key via getpass (never written to disk).
"""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]
EXECUTOR_SRC = REPO / "services" / "executor" / "src"
sys.path.insert(0, str(EXECUTOR_SRC))

from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

from registry import WETH_BASE, PoolInfo, lookup, set_redis  # noqa: E402
from swap import execute_buy, execute_sell  # noqa: E402
import preflight  # noqa: E402

SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
from tiny_live_filters import check_paper_safety  # noqa: E402

HEALTH_DASHBOARD = REPO / "mvp" / "audits" / "alpha_health" / "dashboard.json"

log = logging.getLogger("tiny_live")

OUT_FIELDS = [
    "run_id", "signal_time", "pool", "token_other", "fee_bps",
    "entry_block", "entry_tx", "entry_time", "exit_time",
    "exit_tx", "gas_wei_total", "weth_in_wei", "weth_out_wei",
    "pnl_pct", "pnl_u", "paper_predicted_pct", "status", "error",
]


@dataclass
class OpenPosition:
    signal_time: str
    pool: str
    pool_info: PoolInfo
    token_amount: int
    weth_in_wei: int
    entry_tx: str
    entry_block: str
    entry_time: float
    paper_predicted_pct: float
    gas_wei: int = 0


@dataclass
class RunnerState:
    seen_keys: set[str] = field(default_factory=set)
    pool_last_entry: dict[str, float] = field(default_factory=dict)
    open_positions: dict[str, OpenPosition] = field(default_factory=dict)
    closed_count: int = 0
    cumulative_pnl_u: float = 0.0
    stop_requested: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


def _parse_bool(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _eth_price_usd() -> float:
    return float(os.environ.get("EXECUTOR_ETH_PRICE_USD", "2000"))


def _wei_to_u(wei: int) -> float:
    eth = wei / 1e18
    return eth * _eth_price_usd()


def _load_whitelist(path: Path) -> set[str]:
    data = json.loads(path.read_text())
    return {p.lower() for p in data["pools"]}


def _pool_from_row(row: dict) -> Optional[PoolInfo]:
    pool = (row.get("pool") or "").lower()
    if not pool:
        return None
    found = lookup(pool)
    if found is not None:
        return found
    token_other = (row.get("token_other") or "").lower()
    if not token_other:
        return None
    fee = int(row.get("fee_bps") or 10000)
    weth_is_token1 = str(row.get("weth_is_token1", "")).lower() in ("true", "1", "yes")
    if weth_is_token1:
        t0, t1 = token_other, WETH_BASE.lower()
    else:
        t0, t1 = WETH_BASE.lower(), token_other
    return PoolInfo(
        pool_address=pool,
        pool_type="v3",
        router="0x2626664c2603336E57B271c5C0b26F421741e481",
        token0=t0,
        token0_symbol="?",
        token0_decimals=18,
        token1=t1,
        token1_symbol="?",
        token1_decimals=18,
        fee_bps=fee,
    )


def _redis_panic_active() -> bool:
    url = os.environ.get("REDIS_URL", "redis://172.18.0.10:6379/0")
    try:
        import redis
        r = redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
        val = r.get("SYSTEM_PANIC")
        return bool(val and str(val).strip())
    except Exception as e:
        log.debug("redis panic check failed: %s", e)
        # Fallback: docker exec when runner is on host
        try:
            import subprocess
            proc = subprocess.run(
                ["docker", "exec", "eatlq-redis-1", "redis-cli", "GET", "SYSTEM_PANIC"],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0:
                val = (proc.stdout or "").strip()
                return bool(val and val != "(nil)")
        except Exception:
            pass
        return False


def _signal_key(row: dict, source: str = "") -> str:
    return f"{source}|{row.get('signal_time')}|{row.get('pool')}|{row.get('entry_block')}"


def _tail_new_rows(csv_path: Path, offset: int) -> tuple[list[dict], int]:
    if not csv_path.exists():
        return [], offset
    with open(csv_path, "rb") as f:
        f.seek(offset)
        chunk = f.read()
        new_offset = f.tell()
    if not chunk:
        return [], new_offset
    text = chunk.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if offset > 0 and lines:
        # partial line at start — drop first fragment
        if not text.startswith("\n") and offset > 0:
            lines = lines[1:]
    if not lines:
        return [], new_offset
    reader = csv.DictReader(lines)
    return list(reader), new_offset


def _append_out(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def _load_dynamic_whitelist() -> set[str]:
    if not HEALTH_DASHBOARD.exists():
        log.warning("dashboard missing — dynamic whitelist empty")
        return set()
    data = json.loads(HEALTH_DASHBOARD.read_text())
    pools = data.get("summary", {}).get("dynamic_whitelist_pools", [])
    return {p.lower() for p in pools}


def _check_line_alive(line_id: str) -> tuple[bool, str]:
    if not HEALTH_DASHBOARD.exists():
        return False, "dashboard_missing"
    data = json.loads(HEALTH_DASHBOARD.read_text())
    for line in data.get("lines", []):
        if line.get("line_id") == line_id:
            status = line.get("status", "")
            if status == "ALIVE":
                return True, status
            return False, status
    return False, "line_not_found"


def _eligible_signal(
    row: dict,
    whitelist: set[str],
    runner_start: float,
    state: RunnerState,
    cooldown_sec: int,
    source: str = "",
    *,
    skip_time_check: bool = False,
) -> tuple[bool, str]:
    pool = (row.get("pool") or "").lower()
    if whitelist and pool not in whitelist:
        return False, "not_whitelisted"
    if str(row.get("entry_quote_ok", "")).lower() not in ("true", "1"):
        return False, "entry_quote_not_ok"
    safe, reason = check_paper_safety(row)
    if not safe:
        return False, reason
    if not skip_time_check:
        try:
            st = datetime.fromisoformat(row["signal_time"].replace("Z", "+00:00"))
            if st.timestamp() < runner_start - 60:
                return False, "too_old"
        except Exception:
            return False, "bad_signal_time"
    key = _signal_key(row, source)
    if key in state.seen_keys:
        return False, "duplicate"
    if pool in state.open_positions:
        return False, "pool_active"
    last = state.pool_last_entry.get(pool, 0)
    if time.time() - last < cooldown_sec:
        return False, "cooldown"
    return True, "ok"


class TinyLiveRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.whitelist_mode = args.whitelist_mode
        if self.whitelist_mode == "static":
            self.whitelist = _load_whitelist(Path(args.pool_whitelist_file))
        else:
            self.whitelist = _load_dynamic_whitelist()
        self.state = RunnerState()
        self.runner_start = time.time()
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.paper_csvs = [Path(p) for p in args.paper_csv]
        self.csv_offsets: dict[str, int] = {}
        for p in self.paper_csvs:
            self.csv_offsets[str(p)] = p.stat().st_size if p.exists() else 0
        self.last_whitelist_refresh = time.time()
        self.w3: Optional[Web3] = None
        self.account = None
        self.preflight_flags: dict = {}
        self.dry_run = args.dry_run or _parse_bool(os.environ.get("EXECUTOR_DRY_RUN"))

    def _refresh_whitelist_if_needed(self) -> None:
        if self.whitelist_mode != "dynamic":
            return
        if time.time() - self.last_whitelist_refresh < self.args.whitelist_refresh_seconds:
            return
        self.whitelist = _load_dynamic_whitelist()
        self.last_whitelist_refresh = time.time()
        log.info("dynamic whitelist refreshed: %d pools", len(self.whitelist))

    def setup(self) -> None:
        if self.args.require_line_alive:
            ok, status = _check_line_alive(self.args.require_line_alive)
            if not ok:
                log.error(
                    "require-line-alive=%s status=%s — refusing to start",
                    self.args.require_line_alive, status,
                )
                sys.exit(1)
            log.info("line %s is ALIVE", self.args.require_line_alive)

        if self.whitelist_mode == "dynamic" and not self.whitelist:
            log.error("dynamic whitelist empty — no ALIVE lines in dashboard")
            sys.exit(1)

        if self.dry_run:
            os.environ.setdefault("EXECUTOR_DRY_RUN", "1")
            os.environ.setdefault("LIVE_TRADING_MODE", "false")
        else:
            os.environ["EXECUTOR_DRY_RUN"] = "0"
            if not _parse_bool(os.environ.get("LIVE_TRADING_MODE")):
                log.error("LIVE_TRADING_MODE must be true for live run")
                sys.exit(1)
            pk = getpass.getpass("Session private key (hex, not saved): ").strip()
            if pk.startswith("0x"):
                pk = pk[2:]
            os.environ["EXECUTOR_PRIVATE_KEY"] = pk
            self.account = Account.from_key(pk)
            log.info("wallet %s (session-only)", self.account.address)

        panic = _redis_panic_active()
        self.preflight_flags = preflight.run_preflight_checks(panic_active=panic)

        rpc = (
            os.environ.get("EXECUTOR_RPC_URL")
            or os.environ.get("EVM_HTTP_URL")
            or os.environ.get("LABEL_RPC_URL")
        )
        if not rpc:
            log.error("No RPC URL in env")
            sys.exit(1)
        self.w3 = Web3(Web3.HTTPProvider(rpc))
        if not self.w3.is_connected():
            log.error("RPC not connected: %s", rpc)
            sys.exit(1)

        try:
            import redis
            set_redis(redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")))
        except Exception as e:
            log.warning("redis registry bootstrap skipped: %s", e)

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._on_stop)

    def _on_stop(self, *_: Any) -> None:
        log.warning("stop signal — will exit after closing positions")
        self.state.stop_requested = True

    def _check_kill(self, trade_pnl_u: float | None = None) -> Optional[str]:
        if _redis_panic_active():
            return "SYSTEM_PANIC"
        if trade_pnl_u is not None and trade_pnl_u <= -self.args.max_loss_per_trade_u:
            return f"single_trade_loss_{trade_pnl_u:.2f}U"
        if self.state.cumulative_pnl_u <= -self.args.max_loss_u:
            return f"cumulative_loss_{self.state.cumulative_pnl_u:.2f}U"
        if self.state.closed_count >= self.args.max_trades:
            return "max_trades_reached"
        return None

    def _enter(self, row: dict, pool_info: PoolInfo) -> Optional[OpenPosition]:
        stake_wei = int(self.args.stake_eth * 1e18)
        paper_pct = 0.0
        try:
            if row.get("net_return_30m_real"):
                paper_pct = float(row["net_return_30m_real"]) * 100
            elif row.get("net_return_30m_ag"):
                paper_pct = float(row["net_return_30m_ag"]) * 100
        except (TypeError, ValueError):
            pass

        if self.dry_run:
            log.info(
                "DRY RUN BUY pool=%s stake=%.6f ETH paper_30m=%.2f%%",
                pool_info.pool_address, self.args.stake_eth, paper_pct,
            )
            return OpenPosition(
                signal_time=row["signal_time"],
                pool=pool_info.pool_address,
                pool_info=pool_info,
                token_amount=10**15,
                weth_in_wei=stake_wei,
                entry_tx="DRY_RUN",
                entry_block=row.get("entry_block", ""),
                entry_time=time.time(),
                paper_predicted_pct=paper_pct,
            )

        assert self.w3 and self.account
        res = execute_buy(self.w3, self.account, pool_info, stake_wei)
        if not res.success:
            log.error("buy failed: %s", res.error)
            _append_out(
                Path(self.args.out_csv),
                {
                    "run_id": self.run_id,
                    "signal_time": row["signal_time"],
                    "pool": pool_info.pool_address,
                    "status": "entry_failed",
                    "error": res.error or "buy_failed",
                },
            )
            return None
        log.info("BUY ok tx=%s tokens=%s gas=%s", res.tx_hash, res.amount_out, res.gas_cost_wei)
        return OpenPosition(
            signal_time=row["signal_time"],
            pool=pool_info.pool_address,
            pool_info=pool_info,
            token_amount=res.amount_out,
            weth_in_wei=stake_wei,
            entry_tx=res.tx_hash or "",
            entry_block=row.get("entry_block", ""),
            entry_time=time.time(),
            paper_predicted_pct=paper_pct,
            gas_wei=res.gas_cost_wei,
        )

    def _exit(self, pos: OpenPosition) -> dict:
        if self.dry_run:
            log.info("DRY RUN SELL pool=%s after %ds hold", pos.pool, self.args.hold_seconds)
            pnl_pct = pos.paper_predicted_pct * 0.5
            pnl_u = self.args.stake_eth * _eth_price_usd() * (pnl_pct / 100)
            return {
                "exit_tx": "DRY_RUN",
                "gas_wei_total": 0,
                "weth_out_wei": int(pos.weth_in_wei * (1 + pnl_pct / 100)),
                "pnl_pct": pnl_pct,
                "pnl_u": pnl_u,
                "status": "dry_run_closed",
            }

        assert self.w3 and self.account
        res = execute_sell(
            self.w3, self.account, pos.pool_info, pos.token_amount, reason="time_decay",
        )
        gas_total = pos.gas_wei + res.gas_cost_wei
        weth_out = res.amount_out if res.success else 0
        pnl_wei = weth_out - pos.weth_in_wei
        pnl_u = _wei_to_u(pnl_wei) - _wei_to_u(gas_total)
        pnl_pct = (pnl_wei / pos.weth_in_wei * 100) if pos.weth_in_wei else 0.0
        status = "closed" if res.success else "exit_failed"
        if not res.success:
            log.error("sell failed: %s", res.error)
        return {
            "exit_tx": res.tx_hash or "",
            "gas_wei_total": gas_total,
            "weth_out_wei": weth_out,
            "pnl_pct": pnl_pct,
            "pnl_u": pnl_u,
            "status": status,
            "error": res.error or "",
        }

    def _schedule_exit(self, pos: OpenPosition) -> None:
        def _worker() -> None:
            time.sleep(self.args.hold_seconds)
            with self.state.lock:
                if pos.pool not in self.state.open_positions:
                    return
            result = self._exit(pos)
            with self.state.lock:
                self.state.open_positions.pop(pos.pool, None)
                self.state.closed_count += 1
                self.state.cumulative_pnl_u += result["pnl_u"]
            row = {
                "run_id": self.run_id,
                "signal_time": pos.signal_time,
                "pool": pos.pool,
                "entry_tx": pos.entry_tx,
                "entry_time": datetime.fromtimestamp(pos.entry_time, tz=timezone.utc).isoformat(),
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "paper_predicted_pct": round(pos.paper_predicted_pct, 4),
                **result,
            }
            _append_out(Path(self.args.out_csv), row)
            log.info(
                "CLOSED pool=%s pnl_u=%.3f cumulative=%.3f n=%d",
                pos.pool, result["pnl_u"], self.state.cumulative_pnl_u, self.state.closed_count,
            )
            kill = self._check_kill(result["pnl_u"])
            if kill:
                log.error("kill switch: %s", kill)
                self.state.stop_requested = True

        threading.Thread(target=_worker, daemon=True).start()

    def _close_all_open(self) -> None:
        with self.state.lock:
            positions = list(self.state.open_positions.values())
        for pos in positions:
            log.warning("emergency close pool=%s", pos.pool)
            result = self._exit(pos)
            with self.state.lock:
                self.state.open_positions.pop(pos.pool, None)
            _append_out(
                Path(self.args.out_csv),
                {
                    "run_id": self.run_id,
                    "signal_time": pos.signal_time,
                    "pool": pos.pool,
                    "status": "emergency_close",
                    **result,
                },
            )

    def run(self) -> None:
        self.setup()
        log.info(
            "tiny_live_runner start run_id=%s dry_run=%s mode=%s whitelist=%d pools max_trades=%d",
            self.run_id, self.dry_run, self.whitelist_mode,
            len(self.whitelist), self.args.max_trades,
        )
        last_signal_warn = time.time()

        while not self.state.stop_requested:
            self._refresh_whitelist_if_needed()
            if self.whitelist_mode == "dynamic" and not self.whitelist:
                log.error("dynamic whitelist became empty — stopping")
                break

            if _redis_panic_active():
                log.error("SYSTEM_PANIC active — stopping")
                self._close_all_open()
                break

            kill = self._check_kill()
            if kill:
                log.info("stopping: %s", kill)
                break

            got_signal = False
            for paper in self.paper_csvs:
                src = str(paper)
                offset = self.csv_offsets.get(src, 0)
                rows, new_offset = _tail_new_rows(paper, offset)
                self.csv_offsets[src] = new_offset
                for row in rows:
                    ok, reason = _eligible_signal(
                        row, self.whitelist, self.runner_start, self.state,
                        self.args.cooldown_seconds, source=src,
                    )
                    if not ok:
                        continue
                    got_signal = True
                    pool_info = _pool_from_row(row)
                    if pool_info is None:
                        log.warning("cannot resolve pool %s", row.get("pool"))
                        continue
                    key = _signal_key(row, src)
                with self.state.lock:
                    if len(self.state.open_positions) >= self.args.max_concurrent:
                        continue
                    pos = self._enter(row, pool_info)
                    if pos is None:
                        continue
                    self.state.seen_keys.add(key)
                    self.state.open_positions[pos.pool] = pos
                    self.state.pool_last_entry[pos.pool] = time.time()
                    self._schedule_exit(pos)
                    log.info("entered pool=%s (open=%d)", pos.pool, len(self.state.open_positions))

            if got_signal:
                last_signal_warn = time.time()
            elif time.time() - last_signal_warn > 1800:
                log.warning("no new eligible signals for 30min (still running)")

            time.sleep(self.args.poll_seconds)

        if self.state.open_positions:
            self._close_all_open()
        log.info(
            "done closed=%d cumulative_pnl_u=%.3f",
            self.state.closed_count, self.state.cumulative_pnl_u,
        )


def replay_filter_check(csv_path: Path, whitelist: set[str]) -> None:
    """Replay historical rows through safety + entry filters (no time gate)."""
    rows = list(csv.DictReader(open(csv_path)))
    state = RunnerState()
    passed: list[tuple[dict, str]] = []
    rejected: dict[str, int] = {}
    for row in rows:
        ok, reason = _eligible_signal(
            row, whitelist, time.time(), state, 0, source=str(csv_path),
            skip_time_check=True,
        )
        if ok:
            passed.append((row, reason))
            state.seen_keys.add(_signal_key(row, str(csv_path)))
        else:
            rejected[reason] = rejected.get(reason, 0) + 1
    print(f"replay {csv_path.name}: total={len(rows)} passed={len(passed)}")
    print(f"  rejected: {dict(sorted(rejected.items(), key=lambda x: -x[1]))}")
    for row, _ in passed:
        pool = row.get("pool", "")[:18]
        st = row.get("signal_time", "")[:19]
        print(f"  PASS {st} {pool}...")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description="Tiny live runner — Path A")
    p.add_argument("--stake-eth", type=float, default=0.0015)
    p.add_argument("--max-trades", type=int, default=5)
    p.add_argument("--max-loss-u", type=float, default=5.0)
    p.add_argument("--max-loss-per-trade-u", type=float, default=2.0)
    p.add_argument("--max-concurrent", type=int, default=3)
    p.add_argument("--hold-seconds", type=int, default=1800)
    p.add_argument("--cooldown-seconds", type=int, default=1800)
    p.add_argument("--poll-seconds", type=int, default=10)
    p.add_argument("--pool-whitelist-file", default="configs/tiny_live_whitelist.json")
    p.add_argument(
        "--paper-csv", action="append", default=[],
        help="Paper shadow CSV (repeatable)",
    )
    p.add_argument("--out-csv", default="logs/tiny_live_runner.csv")
    p.add_argument("--dry-run", action="store_true", help="Simulate entries/exits, no chain txs")
    p.add_argument(
        "--whitelist-mode", choices=("static", "dynamic"), default="static",
        help="static=json file; dynamic=ALIVE lines from alpha_health dashboard",
    )
    p.add_argument(
        "--whitelist-refresh-seconds", type=int, default=1800,
        help="Refresh dynamic whitelist interval",
    )
    p.add_argument(
        "--require-line-alive", default="",
        help="Exit unless this line_id is ALIVE in dashboard (e.g. sprint087_expanded)",
    )
    p.add_argument(
        "--replay-check", default="",
        help="Replay filter on CSV and exit (e.g. logs/sprint087_expanded_paper_shadow.csv)",
    )
    args = p.parse_args()
    if not args.paper_csv:
        args.paper_csv = ["logs/sprint055_paper_shadow.csv"]
    if args.replay_check:
        wl = _load_whitelist(Path(args.pool_whitelist_file))
        if args.whitelist_mode == "dynamic":
            wl = _load_dynamic_whitelist() or wl
        replay_filter_check(Path(args.replay_check), wl)
        return
    TinyLiveRunner(args).run()


if __name__ == "__main__":
    main()
