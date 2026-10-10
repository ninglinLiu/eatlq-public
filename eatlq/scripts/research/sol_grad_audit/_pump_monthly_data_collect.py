#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
30-day Pump.fun 通用数据采集（2026-05-17 → 2026-06-15 UTC）。

- 枚举全部 token creations（mint-authority sig 分页）
- 每 token：创建后 5min 内 TradeEvent 全解码
- T+5/15/30/60/90/180/300 snapshots
- RPC 响应 sqlite 缓存，禁止重复 getTransaction
- 按 UTC 日分区，checkpoint resume

用法:
  python3 _pump_monthly_data_collect.py --status
  python3 _pump_monthly_data_collect.py --day 2026-05-17
  python3 _pump_monthly_data_collect.py --all
  python3 _pump_monthly_data_collect.py --report
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from _pump_monthly_lib import (
    DATA_ROOT,
    DATE_HI,
    DATE_LO,
    EARLY_WINDOW_S,
    META_ROOT,
    MINT_AUTHORITY,
    PATH_WINDOW_S,
    RAW_ROOT,
    SNAPSHOT_T,
    CachedRPC,
    RpcCacheDB,
    append_jsonl,
    day_dir,
    day_unix_window,
    decode_trade_event,
    iter_days,
    load_base_rpc,
    load_json,
    load_jsonl,
    parse_create_tx,
    price_at_or_before,
    save_json,
)

MAX_SIG_PAGES_ENUM = 500


def pages_for_day(day: str, hi: str = DATE_HI) -> int:
    """Older days need more sig pages when paging backward from chain head."""
    d = datetime.strptime(day, "%Y-%m-%d")
    d_hi = datetime.strptime(hi, "%Y-%m-%d")
    days_back = max(0, (d_hi - d).days)
    return max(MAX_SIG_PAGES_ENUM, 400 + days_back * 60)
MAX_SIG_PAGES_BC = 12


def checkpoint_path(day: str) -> Path:
    return day_dir(day) / "checkpoint.json"


def load_checkpoint(day: str) -> dict:
    cp = checkpoint_path(day)
    if cp.exists():
        return load_json(cp)
    return {
        "day": day,
        "phase": "pending",
        "creates_enumerated": False,
        "creates_resolved": False,
        "tokens_done": [],
        "tokens_failed": [],
        "n_creates": 0,
    }


def save_checkpoint(day: str, cp: dict) -> None:
    cp["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(checkpoint_path(day), cp)


def enumerate_day_creates(rpc: CachedRPC, day: str, max_pages: int = MAX_SIG_PAGES_ENUM) -> list[dict]:
    lo, hi = day_unix_window(day)
    out: list[dict] = []
    before = None
    pages = 0
    while pages < max_pages:
        sigs = rpc.get_signatures_for_address(MINT_AUTHORITY, before=before)
        pages += 1
        if not sigs:
            break
        before = sigs[-1]["signature"]
        oldest = sigs[-1].get("blockTime") or 0
        for s in sigs:
            bt = s.get("blockTime")
            if bt is None or s.get("err"):
                continue
            if lo <= bt < hi:
                out.append(
                    {
                        "sig": s["signature"],
                        "slot": s.get("slot"),
                        "block_time": bt,
                        "day": day,
                    }
                )
        if oldest and oldest < lo:
            break
        if pages % 25 == 0:
            print(f"  [{day}] enum pages={pages} hits={len(out)} net_rpc={rpc.network_calls}", flush=True)
    return out


def resolve_create(rpc: CachedRPC, sig: str, block_time: int) -> dict | None:
    tx = rpc.get_transaction(sig)
    info = parse_create_tx(tx)
    if not info:
        return None
    info["create_sig"] = sig
    info["created_ms"] = int(block_time * 1000)
    info["block_time"] = block_time
    return info


def collect_bc_trades(
    rpc: CachedRPC,
    bonding_curve: str,
    created_s: float,
    mint_b: bytes,
    window_s: float = PATH_WINDOW_S,
) -> tuple[list[dict], bool]:
    """Collect decoded trades with block_time in [created, created+window_s]."""
    end_t = created_s + window_s + 5
    rows: list[tuple[float, str, bool]] = []
    before = None
    reached = False
    for _ in range(MAX_SIG_PAGES_BC):
        sigs = rpc.get_signatures_for_address(bonding_curve, before=before)
        if not sigs:
            reached = True
            break
        before = sigs[-1]["signature"]
        oldest = sigs[-1].get("blockTime") or 0
        for s in sigs:
            bt = s.get("blockTime")
            if bt is None:
                continue
            rows.append((bt, s["signature"], bool(s.get("err"))))
        if oldest <= created_s - 10:
            reached = True
            break
        if len(sigs) < 1000:
            reached = True
            break

    rows.sort()
    decoded: list[dict] = []
    for bt, sig, err in rows:
        if err:
            continue
        if bt < created_s - 2 or bt > end_t:
            continue
        ev = rpc.decode_tx_trade(sig, mint_b)
        if not ev:
            continue
        t_rel = round(bt - created_s, 3)
        decoded.append(
            {
                "sig": sig,
                "block_time": bt,
                "t_rel": t_rel,
                "is_buy": ev["is_buy"],
                "buyer": ev["buyer"],
                "sol": ev["sol"],
                "price": ev["price"],
                "vSol": ev["vSol"],
                "vTok": ev["vTok"],
                "ts": ev["ts"],
            }
        )
    return decoded, reached


def build_snapshots(trades: list[dict], created_s: float) -> dict[str, dict | None]:
    snaps: dict[str, dict | None] = {}
    for t in SNAPSHOT_T:
        target = created_s + t
        row = price_at_or_before(trades, target)
        if row:
            snaps[str(t)] = {
                "t": t,
                "t_rel": row["t_rel"],
                "block_time": row["block_time"],
                "price": row["price"],
                "vSol": row["vSol"],
                "vTok": row["vTok"],
                "sig": row["sig"],
            }
        else:
            snaps[str(t)] = None
    return snaps


def early_buyer_history(trades: list[dict], window_s: float = EARLY_WINDOW_S) -> list[dict]:
    buyers: dict[str, dict] = {}
    for tr in trades:
        if tr["t_rel"] > window_s or not tr.get("is_buy"):
            continue
        b = tr["buyer"]
        rec = buyers.setdefault(
            b,
            {"buyer": b, "n_buys": 0, "sol_sum": 0.0, "first_t_rel": tr["t_rel"], "last_t_rel": tr["t_rel"]},
        )
        rec["n_buys"] += 1
        rec["sol_sum"] += tr.get("sol") or 0
        rec["first_t_rel"] = min(rec["first_t_rel"], tr["t_rel"])
        rec["last_t_rel"] = max(rec["last_t_rel"], tr["t_rel"])
    return sorted(buyers.values(), key=lambda x: x["first_t_rel"])


def collect_token(rpc: CachedRPC, create_row: dict) -> dict:
    samp = load_base_rpc()
    mint_b = samp.b58dec(create_row["mint"])
    created_s = create_row["created_ms"] / 1000.0
    trades, reached = collect_bc_trades(rpc, create_row["bonding_curve"], created_s, mint_b)
    early = [t for t in trades if t["t_rel"] <= EARLY_WINDOW_S]
    max_vsol = max((t["vSol"] for t in trades), default=0.0)
    return {
        **create_row,
        "day": create_row.get("day"),
        "reached_birth": reached,
        "n_trades_5m": len(early),
        "n_trades_path": len(trades),
        "early_trades": early,
        "path_trades": trades,
        "snapshots": build_snapshots(trades, created_s),
        "early_buyers": early_buyer_history(early),
        "max_vSol": max_vsol,
        "graduated_on_curve": max_vsol >= 85.0,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def token_path(day: str, mint: str) -> Path:
    return day_dir(day) / "tokens" / f"{mint}.json"


def collect_day(day: str, db: RpcCacheDB, max_tokens: int | None = None, force: bool = False, max_pages: int = MAX_SIG_PAGES_ENUM) -> dict:
    cp = load_checkpoint(day)
    dpath = day_dir(day)
    creates_path = dpath / "creates.jsonl"
    tokens_dir = dpath / "tokens"
    tokens_dir.mkdir(parents=True, exist_ok=True)

    rpc = CachedRPC(day=day, artifact="collect", db=db)
    summary = {"day": day, "network_rpc": 0, "creates": 0, "tokens_ok": 0, "tokens_skip": 0, "tokens_fail": 0}

    try:
        # ── enumerate ──
        if force or not cp.get("creates_enumerated") or not creates_path.exists() or (
            cp.get("n_creates", 0) == 0 and max_tokens is None
        ):
            print(f"[{day}] enumerate creates (max_pages={max_pages})...", flush=True)
            creates = enumerate_day_creates(rpc, day, max_pages=max_pages)
            with open(creates_path, "w", encoding="utf-8") as f:
                for c in creates:
                    f.write(json.dumps(c) + "\n")
            cp["creates_enumerated"] = True
            cp["n_creates"] = len(creates)
            save_checkpoint(day, cp)
            print(f"[{day}] enumerated {len(creates)} create sigs", flush=True)
        else:
            creates = load_jsonl(creates_path)
            print(f"[{day}] resume: {len(creates)} create sigs cached", flush=True)

        # ── resolve create txs ──
        resolved_path = dpath / "creates_resolved.jsonl"
        resolved: list[dict] = []
        done_mints = set(cp.get("tokens_done", []))

        if force or not cp.get("creates_resolved") or not resolved_path.exists():
            print(f"[{day}] resolve create txs...", flush=True)
            resolve_limit = len(creates)
            if max_tokens is not None:
                resolve_limit = min(len(creates), max(max_tokens * 20, max_tokens))
            with open(resolved_path, "w", encoding="utf-8") as rf:
                for i, c in enumerate(creates[:resolve_limit]):
                    info = resolve_create(rpc, c["sig"], c["block_time"])
                    if not info:
                        continue
                    info["day"] = day
                    resolved.append(info)
                    rf.write(json.dumps(info) + "\n")
                    if max_tokens is not None and len(resolved) >= max_tokens:
                        break
                    if (i + 1) % 200 == 0:
                        print(f"  [{day}] resolved {i+1}/{resolve_limit} ok={len(resolved)}", flush=True)
            cp["creates_resolved"] = max_tokens is None
            cp["creates_resolved_partial"] = max_tokens is not None
            save_checkpoint(day, cp)
        else:
            resolved = load_jsonl(resolved_path)
            if max_tokens is None and cp.get("creates_resolved_partial"):
                print(f"[{day}] redo full resolve (prior partial)", flush=True)
                cp["creates_resolved"] = False
                save_checkpoint(day, cp)
                return collect_day(day, db, max_tokens=max_tokens, force=True, max_pages=max_pages)

        if max_tokens is not None:
            resolved = resolved[:max_tokens]

        summary["creates"] = len(resolved)

        # ── per-token trade collection ──
        for i, cr in enumerate(resolved):
            mint = cr["mint"]
            if mint in done_mints and not force:
                summary["tokens_skip"] += 1
                continue
            outp = token_path(day, mint)
            try:
                if outp.exists() and not force:
                    summary["tokens_skip"] += 1
                    done_mints.add(mint)
                    continue
                tok = collect_token(rpc, cr)
                save_json(outp, tok)
                done_mints.add(mint)
                summary["tokens_ok"] += 1
            except Exception as e:
                summary["tokens_fail"] += 1
                cp.setdefault("tokens_failed", []).append({"mint": mint, "error": str(e)[:200]})
                print(f"  [{day}] FAIL {mint[:8]}… {e}", flush=True)

            if (i + 1) % 50 == 0:
                cp["tokens_done"] = sorted(done_mints)
                cp["phase"] = "tokens"
                save_checkpoint(day, cp)
                print(
                    f"  [{day}] tokens {i+1}/{len(resolved)} ok={summary['tokens_ok']} "
                    f"net_rpc={rpc.network_calls}",
                    flush=True,
                )

        cp["phase"] = "done"
        cp["tokens_done"] = sorted(done_mints)
        save_checkpoint(day, cp)
        summary["network_rpc"] = rpc.network_calls
        append_jsonl(META_ROOT / "day_summaries.jsonl", summary)
        print(f"[{day}] done creates={summary['creates']} tokens_ok={summary['tokens_ok']} net_rpc={summary['network_rpc']}")
        return summary
    finally:
        rpc.close()


def status_report() -> dict:
    days = iter_days()
    rep = {"days": [], "date_lo": DATE_LO, "date_hi": DATE_HI, "total_creates": 0, "total_tokens": 0}
    for day in days:
        cp = load_checkpoint(day)
        creates_n = cp.get("n_creates", 0)
        if (day_dir(day) / "creates.jsonl").exists() and not creates_n:
            creates_n = len(load_jsonl(day_dir(day) / "creates.jsonl"))
        tokens_n = len(cp.get("tokens_done", []))
        rep["days"].append(
            {
                "day": day,
                "phase": cp.get("phase", "pending"),
                "creates": creates_n,
                "tokens_done": tokens_n,
                "tokens_failed": len(cp.get("tokens_failed", [])),
            }
        )
        rep["total_creates"] += creates_n
        rep["total_tokens"] += tokens_n
    return rep


def write_rpc_report(db: RpcCacheDB) -> Path:
    rows = db.stats_rows()
    by_day: dict[str, dict] = {}
    for r in rows:
        d = r["day"]
        by_day.setdefault(d, {"day": d, "methods": {}, "total_calls": 0, "total_hits": 0})
        by_day[d]["methods"].setdefault(r["method"], {"calls": 0, "cache_hits": 0, "artifacts": {}})
        m = by_day[d]["methods"][r["method"]]
        m["calls"] += r["calls"]
        m["cache_hits"] += r["cache_hits"]
        m["artifacts"][r["artifact"]] = {"calls": r["calls"], "cache_hits": r["cache_hits"]}
        by_day[d]["total_calls"] += r["calls"]
        by_day[d]["total_hits"] += r["cache_hits"]

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "by_day": sorted(by_day.values(), key=lambda x: x["day"]),
        "raw_rows": rows,
        "grand_total_calls": sum(r["calls"] for r in rows),
        "grand_total_cache_hits": sum(r["cache_hits"] for r in rows),
    }
    path = META_ROOT / "rpc_cost_report.json"
    save_json(path, out)
    return path


def print_status(rep: dict) -> None:
    print(f"=== Pump 30d collect status ({DATE_LO} → {DATE_HI}) ===")
    print(f"{'day':<12} {'phase':<10} {'creates':>8} {'tokens':>8} {'failed':>8}")
    for d in rep["days"]:
        print(
            f"{d['day']:<12} {d['phase']:<10} {d['creates']:>8} {d['tokens_done']:>8} {d['tokens_failed']:>8}"
        )
    print(f"\nTotal creates={rep['total_creates']} tokens={rep['total_tokens']}")


def main():
    ap = argparse.ArgumentParser(description="30-day Pump monthly data collect")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--all", action="store_true", help="collect all days in window")
    ap.add_argument("--day", type=str, default="", help="single UTC day YYYY-MM-DD")
    ap.add_argument("--max-tokens", type=int, default=0, help="limit tokens per day (0=all)")
    ap.add_argument("--force", action="store_true", help="re-collect even if checkpoint done")
    ap.add_argument("--report", action="store_true", help="write rpc cost report")
    ap.add_argument("--max-pages", type=int, default=MAX_SIG_PAGES_ENUM)
    args = ap.parse_args()

    max_sig_pages = args.max_pages

    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    META_ROOT.mkdir(parents=True, exist_ok=True)
    db = RpcCacheDB()

    if args.status or (not args.all and not args.day and not args.report):
        rep = status_report()
        print_status(rep)
        save_json(META_ROOT / "collection_status.json", rep)

    if args.report:
        path = write_rpc_report(db)
        print(f"RPC report -> {path}")

    days = [args.day] if args.day else (iter_days() if args.all else [])
    max_tok = args.max_tokens if args.max_tokens > 0 else None

    for day in days:
        if day not in iter_days():
            print(f"Skip {day}: outside [{DATE_LO}, {DATE_HI}]")
            continue
        day_pages = max_sig_pages if args.max_pages != MAX_SIG_PAGES_ENUM else pages_for_day(day)
        collect_day(day, db, max_tokens=max_tok, force=args.force, max_pages=day_pages)

    if args.all or args.day:
        write_rpc_report(db)
        rep = status_report()
        save_json(META_ROOT / "collection_status.json", rep)

    db.close()


if __name__ == "__main__":
    main()
