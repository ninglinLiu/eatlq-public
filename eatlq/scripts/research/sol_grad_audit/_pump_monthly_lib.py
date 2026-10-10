#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared utilities for 30-day Pump monthly collect + label pipeline."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import importlib.util

OUT_DIR = Path(__file__).resolve().parent
DATA_ROOT = OUT_DIR / "data" / "pump_30d"
RAW_ROOT = DATA_ROOT / "raw"
RPC_DB = DATA_ROOT / "rpc_cache.sqlite"
META_ROOT = DATA_ROOT / "meta"

DATE_LO = "2026-05-17"
DATE_HI = "2026-06-15"
SNAPSHOT_T = [5, 15, 30, 60, 90, 180, 300]
EARLY_WINDOW_S = 300
PATH_WINDOW_S = 300
COST_RT = 0.03
ENTRY_S = 15
EXIT_S = 90
GRAD_VSOL = 85.0
FATAL25, FATAL50 = -0.25, -0.50

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATE_DISC = bytes.fromhex("d6904cec5f8b31b4")
MINT_AUTHORITY = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def iter_days(lo: str = DATE_LO, hi: str = DATE_HI) -> list[str]:
    d0 = datetime.strptime(lo, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    d1 = datetime.strptime(hi, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    out = []
    cur = d0
    while cur <= d1:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def day_unix_window(day: str) -> tuple[int, int]:
    lo = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    hi = lo + timedelta(days=1)
    return int(lo.timestamp()), int(hi.timestamp())


def b58enc(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


def load_base_rpc():
    spec = importlib.util.spec_from_file_location("samp", OUT_DIR.parent / "sol" / "sol_sample_creates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def decode_trade_event(raw: bytes, mint_b: bytes) -> dict | None:
    need = 8 + 32 + 8 + 8 + 1 + 32 + 8 + 8 + 8
    if len(raw) < need or raw[8:40] != mint_b:
        return None
    off = 8 + 32 + 8 + 8 + 1 + 32
    ts = struct.unpack_from("<q", raw, off)[0]
    off += 8
    v_sol = struct.unpack_from("<Q", raw, off)[0] / 1e9
    v_tok = struct.unpack_from("<Q", raw, off + 8)[0] / 1e6
    if not (1.0 <= v_sol <= 5000.0 and 1e5 <= v_tok <= 2e9):
        return None
    sol_amt = struct.unpack_from("<Q", raw, 40)[0] / 1e9
    is_buy = int(raw[56])
    buyer = b58enc(raw[57:89])
    price = v_sol / v_tok
    return {
        "sol": round(sol_amt, 6),
        "is_buy": is_buy,
        "buyer": buyer,
        "ts": ts,
        "vSol": round(v_sol, 6),
        "vTok": round(v_tok, 2),
        "price": price,
    }


def parse_create_tx(tx: dict | None) -> dict | None:
    if not tx or tx.get("meta", {}).get("err"):
        return None
    msg = tx["transaction"]["message"]
    ak = msg.get("accountKeys", [])
    fee_payer = ak[0]["pubkey"] if ak and isinstance(ak[0], dict) else (ak[0] if ak else None)
    samp = load_base_rpc()
    for ins in msg.get("instructions", []):
        if ins.get("programId") != PUMP:
            continue
        data = ins.get("data", "")
        try:
            is_create = data and samp.b58dec(data)[:8] == CREATE_DISC
        except Exception:
            is_create = False
        if not is_create:
            continue
        accs = ins.get("accounts", [])
        if len(accs) < 3:
            continue
        return {
            "mint": accs[0],
            "creator": fee_payer,
            "bonding_curve": accs[2],
            "create_sig": None,
        }
    return None


def price_at_or_before(trades: list[dict], t_target: float) -> dict | None:
    cand = [t for t in trades if t.get("block_time", 0) <= t_target and t.get("price")]
    if not cand:
        cand = [t for t in trades if t.get("block_time", 0) >= t_target and t.get("price")]
    if not cand:
        return None
    best = min(cand, key=lambda t: abs(t["block_time"] - t_target))
    return best


def net_ret(p_entry: float, p_exit: float, cost: float = COST_RT) -> float | None:
    if not p_entry or not p_exit or p_entry <= 0:
        return None
    gross = p_exit / p_entry
    return gross * (1.0 - cost) - 1.0


class RpcCacheDB:
    def __init__(self, path: Path = RPC_DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=60)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=60000")
        self._pending_stats = 0
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rpc_cache (
                cache_key TEXT PRIMARY KEY,
                method TEXT NOT NULL,
                params_json TEXT NOT NULL,
                response_json TEXT,
                fetched_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rpc_stats (
                day TEXT NOT NULL,
                method TEXT NOT NULL,
                artifact TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                cache_hits INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, method, artifact)
            )
            """
        )
        self.conn.commit()

    @staticmethod
    def key(method: str, params: Any) -> str:
        blob = json.dumps([method, params], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, method: str, params: Any) -> Any | None:
        k = self.key(method, params)
        row = self.conn.execute("SELECT response_json FROM rpc_cache WHERE cache_key=?", (k,)).fetchone()
        if row is None:
            return None
        if row[0] is None:
            return None
        return json.loads(row[0])

    def put(self, method: str, params: Any, response: Any) -> None:
        k = self.key(method, params)
        self.conn.execute(
            "INSERT OR REPLACE INTO rpc_cache(cache_key, method, params_json, response_json, fetched_at) VALUES (?,?,?,?,?)",
            (k, method, json.dumps(params), json.dumps(response) if response is not None else None, datetime.now(timezone.utc).isoformat()),
        )

    def bump_stat(self, day: str, method: str, artifact: str, hit: bool) -> None:
        row = self.conn.execute(
            "SELECT calls, cache_hits FROM rpc_stats WHERE day=? AND method=? AND artifact=?",
            (day, method, artifact),
        ).fetchone()
        if row:
            calls, hits = row
            calls += 0 if hit else 1
            hits += 1 if hit else 0
            self.conn.execute(
                "UPDATE rpc_stats SET calls=?, cache_hits=? WHERE day=? AND method=? AND artifact=?",
                (calls, hits, day, method, artifact),
            )
        else:
            self.conn.execute(
                "INSERT INTO rpc_stats(day, method, artifact, calls, cache_hits) VALUES (?,?,?,?,?)",
                (day, method, artifact, 0 if hit else 1, 1 if hit else 0),
            )
        self._pending_stats += 1
        if self._pending_stats >= 50:
            self.commit()

    def stats_rows(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT day, method, artifact, calls, cache_hits FROM rpc_stats ORDER BY day, method, artifact"
        ).fetchall()
        return [
            {"day": r[0], "method": r[1], "artifact": r[2], "calls": r[3], "cache_hits": r[4]}
            for r in rows
        ]

    def commit(self) -> None:
        self.conn.commit()
        self._pending_stats = 0

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


class CachedRPC:
    """Solana RPC with sqlite-backed response cache."""

    TX_OPTS = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}

    def __init__(self, day: str, artifact: str = "collect", min_interval: float = 0.20, db: RpcCacheDB | None = None):
        self.day = day
        self.artifact = artifact
        self.db = db or RpcCacheDB()
        self._owns_db = db is None
        samp = load_base_rpc()
        self._inner = samp.RPC(samp.rpc_url(), min_interval=min_interval)
        self.network_calls = 0

    def call(self, method: str, params: Any) -> Any:
        cached = self.db.get(method, params)
        if cached is not None:
            self.db.bump_stat(self.day, method, self.artifact, hit=True)
            return cached
        result = self._inner(method, params)
        self.network_calls += 1
        self.db.put(method, params, result)
        self.db.bump_stat(self.day, method, self.artifact, hit=False)
        self.db.commit()
        return result

    def get_transaction(self, sig: str) -> Any:
        return self.call("getTransaction", [sig, self.TX_OPTS])

    def get_signatures_for_address(self, address: str, before: str | None = None, limit: int = 1000) -> list:
        opts: dict = {"limit": limit}
        if before:
            opts["before"] = before
        return self.call("getSignaturesForAddress", [address, opts]) or []

    def decode_tx_trade(self, sig: str, mint_b: bytes) -> dict | None:
        tx = self.get_transaction(sig)
        if not tx or tx.get("meta", {}).get("err"):
            return None
        for line in tx.get("meta", {}).get("logMessages", []) or []:
            if not line.startswith("Program data:"):
                continue
            try:
                raw = base64.b64decode(line.split("Program data: ")[1])
            except Exception:
                continue
            ev = decode_trade_event(raw, mint_b)
            if ev:
                ev["sig"] = sig
                return ev
        return None

    def close(self) -> None:
        if self._owns_db:
            self.db.close()


def day_dir(day: str) -> Path:
    p = RAW_ROOT / day
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
