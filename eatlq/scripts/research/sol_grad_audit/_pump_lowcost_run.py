#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pump 低成本三阶段采集

  Phase A ($0): 复用 creates_labeled + smart_buyers + pump API 补 label
  Phase B (~$5-15): 30d × 300/天 → API label → earlypath → 稀疏 replay
  Phase C: top 5% 全 TradeEvent replay（仅 B 有信号时）
  Phase D: API hit100+ 右尾 timing autopsy（仅右尾 cohort）

用法:
  python3 _pump_lowcost_run.py --status
  python3 _pump_lowcost_run.py --phase a
  python3 _pump_lowcost_run.py --phase b [--day 2026-06-10] [--per-day 300]
  python3 _pump_lowcost_run.py --phase c [--top-frac 0.05]
  python3 _pump_lowcost_run.py --phase d
  python3 _pump_lowcost_run.py --phase e1
  python3 _pump_lowcost_run.py --phase e1b
  python3 _pump_lowcost_run.py --phase e1c [--target 800]
  python3 _pump_lowcost_run.py --phase e1d [--skip-collect] [--skip-replay]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from _pump_lowcost_lib import (
    DATE_HI,
    DATE_LO,
    LC_ROOT,
    RPC_DB,
    SAMPLE_PER_DAY,
    api_get,
    day_dir,
    earlypath_features,
    label_via_api,
    load_legacy_labeled,
    load_legacy_smart_buyers,
    mmdd,
    phase_a_manifest,
    resolve_create_rpc,
    sample_creates_for_day,
    sparse_snapshots,
)
from _pump_monthly_lib import CachedRPC, RpcCacheDB, iter_days, load_jsonl, save_json

OUT_DIR = Path(__file__).resolve().parent


def cmd_status() -> None:
    ma = phase_a_manifest()
    print("=== Phase A (existing, $0) ===")
    print(f"  labeled={ma['labeled_n']} smart_buyers={ma['smart_buyers_n']} overlap={ma['overlap_n']}")
    print(f"  smart_buyers days: {ma['by_day_smart_buyers']}")

    b_rows = list((LC_ROOT / "phase_b").glob("*/*.jsonl")) if (LC_ROOT / "phase_b").exists() else []
    b_days = sorted({p.parent.name for p in b_rows})
    print(f"\n=== Phase B ===")
    print(f"  days_with_output: {len(b_days)}")
    if b_days:
        print(f"  latest: {b_days[-3:]}")

    c_out = LC_ROOT / "phase_c" / "full_replay.json"
    print(f"\n=== Phase C ===")
    print(f"  full_replay: {'yes' if c_out.exists() else 'no'}")

    d_out = LC_ROOT / "phase_d" / "tail_autopsy.json"
    print(f"\n=== Phase D ===")
    print(f"  tail_autopsy: {'yes' if d_out.exists() else 'no'}")

    if RPC_DB.exists():
        import sqlite3

        c = sqlite3.connect(str(RPC_DB))
        row = c.execute("select sum(calls) from rpc_stats").fetchone()
        calls = row[0] or 0
        print(f"\n=== RPC (shared cache) ===")
        print(f"  network_calls={calls} est_credits≈{calls*30/1e6:.3f}M (~${max(0,calls*30/1e6-80)*0.62:.1f} overage on Build)")


def cmd_phase_a() -> None:
    LC_ROOT.mkdir(parents=True, exist_ok=True)
    out_dir = LC_ROOT / "phase_a"
    out_dir.mkdir(parents=True, exist_ok=True)

    labeled = load_legacy_labeled()
    sb = load_legacy_smart_buyers()
    manifest = phase_a_manifest()
    manifest["built_at"] = datetime.now(timezone.utc).isoformat()

    # unified rows for core 06-11~06-15
    rows = []
    for mint, lab in labeled.items():
        ms = lab.get("created_ms")
        if not ms:
            continue
        day = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if day < "2026-06-11" or day > "2026-06-15":
            continue
        sb_row = sb.get(mint, {})
        rows.append(
            {
                "mint": mint,
                "day": day,
                "creator": lab.get("creator"),
                "created_ms": ms,
                "ath_mc": lab.get("ath_mc"),
                "graduated": lab.get("graduated"),
                "bonding_curve": lab.get("bonding_curve"),
                "has_smart_buyers": mint in sb,
                "n_early_buys": len(sb_row.get("early_buys") or []),
                "has_grid": bool(sb_row.get("grid")),
                "source": "legacy",
            }
        )

    # API refresh only for core-window smart_buyers mints missing ath in labeled
    refreshed = 0
    for mint, sb_row in sb.items():
        day_sb = sb_row.get("day")
        if day_sb not in {"06-11", "06-12", "06-13", "06-14", "06-15"}:
            continue
        if mint in labeled and labeled[mint].get("ath_mc"):
            continue
        api = label_via_api(mint)
        if api:
            refreshed += 1
            day = datetime.fromtimestamp(api["created_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            rows.append({**api, "day": day, "has_smart_buyers": True, "n_early_buys": len(sb_row.get("early_buys") or []), "has_grid": bool(sb_row.get("grid")), "source": "api_refresh"})

    df = pd.DataFrame(rows).drop_duplicates(subset=["mint"], keep="last")
    parquet = OUT_DIR / "pump_lowcost_phase_a.parquet"
    df.to_parquet(parquet, index=False)
    manifest["rows"] = len(df)
    manifest["api_refreshed"] = refreshed
    save_json(out_dir / "manifest.json", manifest)

    print(f"Phase A done: {len(df)} rows -> {parquet}")
    print(f"  legacy overlap smart_buyers: {(df['has_smart_buyers']).sum() if 'has_smart_buyers' in df else 'n/a'}")
    print(f"  API refreshed: {refreshed}")


def cmd_phase_b(days: list[str], per_day: int, seed: int) -> None:
    rng = random.Random(seed)
    db = RpcCacheDB()
    all_results = []

    for day in days:
        cp_path = day_dir("phase_b", day) / "checkpoint.json"
        out_path = day_dir("phase_b", day) / "coins.jsonl"
        cp = json.loads(cp_path.read_text()) if cp_path.exists() else {"done_mints": []}
        done = set(cp.get("done_mints", []))
        if out_path.exists():
            for r in load_jsonl(out_path):
                if r.get("mint"):
                    done.add(r["mint"])
        print(f"[B] {day} sample up to {per_day}...", flush=True)

        # sample
        sample_path = day_dir("phase_b", day) / "sample_sigs.jsonl"
        if not sample_path.exists() or cp.get("n_sample", 0) < per_day:
            samples = sample_creates_for_day(day, per_day, rng)
            with open(sample_path, "w") as f:
                for s in samples:
                    f.write(json.dumps(s) + "\n")
            cp["n_sample"] = len(samples)
        else:
            samples = load_jsonl(sample_path)

        rpc = CachedRPC(day=day, artifact="lowcost_b", db=db)
        n_ok = 0
        with open(out_path, "a" if out_path.exists() and done else "w") as outf:
            for s in samples:
                cr = resolve_create_rpc(rpc, s["sig"], s["block_time"], day)
                if not cr:
                    continue
                mint_key = cr["mint"]
                if mint_key in done:
                    continue

                lab = label_via_api(mint_key)
                if not lab:
                    continue
                bc = lab.get("bonding_curve") or cr.get("bonding_curve")
                if not bc:
                    continue

                ep = earlypath_features(rpc, bc, lab["created_ms"] / 1000.0)
                sp = sparse_snapshots(rpc, mint_key, bc, lab["created_ms"])

                row = {
                    **lab,
                    **ep,
                    **sp,
                    "day": day,
                    "phase": "B",
                }
                outf.write(json.dumps(row, ensure_ascii=False) + "\n")
                outf.flush()
                done.add(mint_key)
                n_ok += 1
                all_results.append(row)

                if n_ok % 25 == 0:
                    cp["done_mints"] = sorted(done)
                    save_json(cp_path, cp)
                    print(f"  [{day}] {n_ok}/{len(samples)} net_rpc={rpc.network_calls}", flush=True)

        cp["done_mints"] = sorted(done)
        cp["phase"] = "done"
        save_json(cp_path, cp)
        rpc.close()
        print(f"[B] {day} done n={len(done)}", flush=True)

    db.close()

    # merge parquet
    rows = []
    for day in days:
        p = day_dir("phase_b", day) / "coins.jsonl"
        if p.exists():
            rows.extend(load_jsonl(p))
    if rows:
        pdf = pd.DataFrame(rows)
        pdf.to_parquet(OUT_DIR / "pump_lowcost_phase_b.parquet", index=False)
        print(f"Phase B parquet -> pump_lowcost_phase_b.parquet ({len(pdf)} rows)")


def cmd_phase_c(top_frac: float, score_col: str) -> None:
    b_path = OUT_DIR / "pump_lowcost_phase_b.parquet"
    if not b_path.exists():
        rows = []
        for p in sorted((LC_ROOT / "phase_b").glob("*/coins.jsonl")):
            rows.extend(load_jsonl(p))
        if not rows:
            print("Phase B data missing. Run --phase b first.")
            sys.exit(1)
        df = pd.DataFrame(rows)
    else:
        df = pd.read_parquet(b_path)

    if score_col not in df.columns:
        score_col = "n_trades_180s"
    df = df.dropna(subset=[score_col], how="all")
    df["score"] = df[score_col].fillna(0)
    k = max(1, int(len(df) * top_frac))
    top = df.nlargest(k, "score")

    print(f"Phase C: full replay top {top_frac:.0%} = {k} / {len(df)} coins")

    sys.path.insert(0, str(OUT_DIR))
    from _pump_exante_top5_replay import load_done, replay_one
    from _pump_tail_realgas_replay import load_meta
    from _grad_audit_lib import load_grids

    meta = load_meta()
    grids = load_grids()
    # Phase B mints not in legacy labeled/earlypath
    for _, row in df.iterrows():
        m = row["mint"]
        bc = row.get("bonding_curve")
        cms = row.get("created_ms")
        if bc and cms:
            meta.setdefault(m, {})
            meta[m].update({"mint": m, "bonding_curve": bc, "created_ms": int(cms)})

    tx_cache: dict = {}
    results = []
    from _pump_tail_realgas_replay import RPC, rpc_url

    inner = RPC(rpc_url(), min_interval=0.20)
    rpc_before = inner.calls

    for i, (_, row) in enumerate(top.iterrows()):
        day = row.get("day", "unknown")
        r = replay_one(
            inner,
            {"mint": row["mint"], "score": float(row["score"]), "cohort": "lowcost_top5"},
            meta,
            grids,
            tx_cache,
            0.03,
        )
        r["day"] = day
        r["phase"] = "C"
        r["score_n180"] = float(row["score"])
        r["ath_mc"] = row.get("ath_mc")
        results.append(r)
        if (i + 1) % 10 == 0:
            print(f"  [C] {i+1}/{k} rpc={inner.calls - rpc_before} ok={sum(1 for x in results if x.get('ok'))}", flush=True)

    rpc_used = inner.calls - rpc_before

    out = LC_ROOT / "phase_c" / "full_replay.json"
    save_json(
        out,
        {
            "meta": {
                "n": len(results),
                "top_frac": top_frac,
                "score_col": score_col,
                "rpc_calls": rpc_used,
                "n_ok": sum(1 for r in results if r.get("ok")),
                "n_priced": sum(1 for r in results if r.get("real_return_10U") is not None),
            },
            "results": results,
        },
    )
    pd.DataFrame(results).to_parquet(OUT_DIR / "pump_lowcost_phase_c.parquet", index=False)
    print(f"Phase C -> {out} ({len(results)} replays, ok={sum(1 for r in results if r.get('ok'))}, rpc={rpc_used})")


def main():
    ap = argparse.ArgumentParser(description="Pump low-cost Phase A/B/C")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--phase", choices=["a", "b", "c", "d", "e1", "e1b", "e1c", "e1d"], default="")
    ap.add_argument("--target", type=int, default=800, help="E1c stratified replay target")
    ap.add_argument("--day", type=str, default="", help="single day for phase B")
    ap.add_argument("--per-day", type=int, default=SAMPLE_PER_DAY)
    ap.add_argument("--top-frac", type=float, default=0.05)
    ap.add_argument("--score-col", type=str, default="n_trades_180s")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-replay", action="store_true", help="E1c/E1d: analysis only")
    ap.add_argument("--skip-collect", action="store_true", help="E1d: skip holdout Phase B collect")
    args = ap.parse_args()

    if args.status or not args.phase:
        cmd_status()
        if not args.phase:
            return

    if args.phase == "a":
        cmd_phase_a()
    elif args.phase == "b":
        days = [args.day] if args.day else iter_days()
        cmd_phase_b(days, args.per_day, args.seed)
    elif args.phase == "c":
        cmd_phase_c(args.top_frac, args.score_col)
    elif args.phase == "d":
        from _pump_lowcost_phase_d import run_phase_d

        run_phase_d()
    elif args.phase == "e1":
        from _pump_lowcost_phase_e1 import run_phase_e1

        run_phase_e1()
    elif args.phase == "e1b":
        from _pump_lowcost_phase_e1b import run_phase_e1b

        run_phase_e1b()
    elif args.phase == "e1c":
        from _pump_lowcost_phase_e1c import run_phase_e1c

        run_phase_e1c(n_target=args.target, skip_replay=args.skip_replay)
    elif args.phase == "e1d":
        from _pump_lowcost_phase_e1d import run_phase_e1d

        run_phase_e1d(skip_collect=args.skip_collect, skip_replay=args.skip_replay, per_day=args.per_day)


if __name__ == "__main__":
    main()
