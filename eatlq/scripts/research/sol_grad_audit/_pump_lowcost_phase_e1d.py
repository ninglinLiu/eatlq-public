#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase E1d: Pre-registered bucket validation (holdout).

Frozen buckets (from E1c discovery — thresholds NOT tuned here):
  A: n_trades_15 in day q95–100 AND 0.35 <= progress_15 < 0.55
  B: n_trades_15 in day q95–100 AND 0 <= progress_15 < 0.35
  C: n_trades_15 in day q95–100 AND progress_15 >= 0.75  (overheat control)

Holdout cohort: dates NOT used in E1c rule discovery (06-02~06-15).
Prefer 06-16+; if unavailable, Phase B re-sample 2026-05-17~06-01 with fixed pagination.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from _grad_audit_lib import load_deployer, load_early_buys
from _pump_lowcost_lib import LC_ROOT, day_dir, save_json
from _pump_lowcost_phase_e1 import (
    build_feature_row,
    load_phase_d_meta,
    load_replay_map,
)
from _pump_lowcost_phase_e1c import cell_metrics
from _pump_monthly_lib import iter_days, load_jsonl
from _pump_tail_realgas_replay import RPC, b58dec, collect_trades, load_meta, reserves_near, rpc_url, simulate_roundtrip, SOL_USD

OUT_DIR = Path(__file__).resolve().parent
E1C_ROOT = LC_ROOT / "phase_e1c"
E1D_ROOT = LC_ROOT / "phase_e1d"

E1C_DISCOVERY_LO = "2026-06-02"
E1C_DISCOVERY_HI = "2026-06-15"
HOLDOUT_LO = "2026-05-17"
HOLDOUT_HI = "2026-06-01"
PREFERRED_HOLDOUT_LO = "2026-06-16"

BUCKETS = {
    "A": {"label": "sweet_spot", "progress_lo": 0.35, "progress_hi": 0.55},
    "B": {"label": "low_progress_high_n15", "progress_lo": 0.0, "progress_hi": 0.35},
    "C": {"label": "overheat_control", "progress_lo": 0.75, "progress_hi": 1.01},
}


def holdout_days() -> list[str]:
    preferred = [d for d in iter_days() if d >= PREFERRED_HOLDOUT_LO]
    if preferred:
        return preferred
    return [d for d in iter_days() if HOLDOUT_LO <= d <= HOLDOUT_HI]


def holdout_has_data() -> bool:
    return sum(1 for d in holdout_days() if _day_coin_count(d) > 0) > 0


def _day_coin_count(day: str) -> int:
    p = day_dir("phase_b", day) / "coins.jsonl"
    if not p.exists():
        return 0
    return sum(1 for _ in open(p))


def _day_needs_collect(day: str, per_day: int) -> bool:
    return _day_coin_count(day) < int(per_day * 0.9)


def _reset_failed_day(day: str) -> None:
    """Clear stale checkpoint when sampling failed or run was interrupted early."""
    cp_path = day_dir("phase_b", day) / "checkpoint.json"
    sample_path = day_dir("phase_b", day) / "sample_sigs.jsonl"
    n_coins = _day_coin_count(day)
    cp = json.loads(cp_path.read_text()) if cp_path.exists() else {}
    n_sample = cp.get("n_sample", 0)
    if n_sample == 0 and n_coins == 0:
        sample_path.unlink(missing_ok=True)
        cp_path.write_text(json.dumps({"done_mints": []}), encoding="utf-8")
    elif n_coins > 0 and cp.get("phase") == "done" and _day_needs_collect(day, 300):
        cp["phase"] = "collecting"
        save_json(cp_path, cp)


def ensure_holdout_phase_b(per_day: int = 300, seed: int = 42) -> None:
    days = holdout_days()
    for d in days:
        if _day_needs_collect(d, per_day):
            _reset_failed_day(d)
    need = [d for d in days if _day_needs_collect(d, per_day)]
    if not need:
        print(f"Phase E1d: holdout Phase B complete ({len(days)} days)", flush=True)
        return
    print(f"Phase E1d: collecting/resuming Phase B for {len(need)} holdout days: {need[0]}..{need[-1]}", flush=True)
    from _pump_lowcost_run import cmd_phase_b

    cmd_phase_b(need, per_day, seed)


def load_holdout_universe() -> pd.DataFrame:
    days = set(holdout_days())
    rows = []
    for p in sorted((LC_ROOT / "phase_b").glob("*/coins.jsonl")):
        day = p.parent.name
        if day not in days:
            continue
        rows.extend(load_jsonl(p))
    if not rows:
        raise SystemExit("Holdout Phase B empty — run collection first.")
    df = pd.DataFrame(rows)
    df["day"] = df["day"].fillna(df.get("day", ""))
    return df.drop_duplicates(subset=["mint"], keep="last")


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    eb_map = load_early_buys()
    deployer = load_deployer()
    d_meta = load_phase_d_meta()
    feats = []
    for _, row in df.iterrows():
        f = build_feature_row(row.to_dict(), eb_map, deployer, d_meta)
        f["mint"] = row["mint"]
        f["day"] = row["day"]
        f["bonding_curve"] = row.get("bonding_curve")
        f["created_ms"] = row.get("created_ms")
        feats.append(f)
    return pd.DataFrame(feats)


def day_n15_q95(feats: pd.DataFrame) -> pd.Series:
    """Per-day 95th percentile threshold for n_trades_15."""
    out = pd.Series(np.nan, index=feats.index)
    for day, sub in feats.groupby("day"):
        s = sub["n_trades_15"].dropna()
        if len(s) == 0:
            continue
        q = float(s.quantile(0.95))
        out.loc[sub.index] = q
    return out


def assign_buckets(feats: pd.DataFrame) -> pd.DataFrame:
    df = feats.copy()
    df["n15_q95"] = day_n15_q95(df)
    df["high_n15"] = df["n_trades_15"] >= df["n15_q95"]
    df["bucket"] = None
    for key, spec in BUCKETS.items():
        prog = df["progress_15"]
        mask = (
            df["high_n15"]
            & prog.notna()
            & (prog >= spec["progress_lo"])
            & (prog < spec["progress_hi"])
        )
        df.loc[mask, "bucket"] = key
    return df


def daily_breakdown(sub: pd.DataFrame) -> list[dict]:
    rows = []
    for day, g in sub.groupby("day"):
        m = cell_metrics(g)
        m["day"] = day
        rows.append(m)
    return sorted(rows, key=lambda x: x["day"])


def replay_batch(df: pd.DataFrame, checkpoint: Path) -> tuple[dict[str, float], int]:
    done: dict[str, float] = {}
    if checkpoint.exists():
        for line in open(checkpoint):
            r = json.loads(line)
            if r.get("real_return_10U") is not None:
                done[r["mint"]] = float(r["real_return_10U"])

    pending = df[~df["mint"].isin(done)]
    if len(pending) == 0:
        return done, 0

    meta = load_meta()
    for _, row in pending.iterrows():
        m = row["mint"]
        meta.setdefault(m, {})
        meta[m].update(
            {"mint": m, "bonding_curve": row.get("bonding_curve"), "created_ms": int(row.get("created_ms") or 0)}
        )

    rpc = RPC(rpc_url(), min_interval=0.20)
    tx_cache: dict = {}
    with open(checkpoint, "a") as cpf:
        for i, (_, row) in enumerate(pending.iterrows()):
            mint = row["mint"]
            bc = row.get("bonding_curve")
            cms = row.get("created_ms")
            rec = {"mint": mint, "day": row.get("day"), "bucket": row.get("bucket"), "ok": False}
            if bc and cms:
                created = cms / 1000.0
                mint_b = b58dec(mint)
                trades, _ = collect_trades(rpc, bc, created + 95)
                ent = reserves_near(rpc, trades, created + 15, mint_b, tx_cache, before=True)
                ex = reserves_near(rpc, trades, created + 90, mint_b, tx_cache, before=True)
                sim = simulate_roundtrip((ent[0], ent[1]), (ex[0], ex[1]), 10 / SOL_USD) if ent and ex else {}
                rec.update({"ok": sim.get("ok"), "real_return_10U": sim.get("real_return")})
                if rec.get("real_return_10U") is not None:
                    done[mint] = float(rec["real_return_10U"])
            cpf.write(json.dumps(rec) + "\n")
            cpf.flush()
            if (i + 1) % 10 == 0:
                print(f"  [E1d replay] {i+1}/{len(pending)} rpc={rpc.calls} priced={len(done)}", flush=True)
    return done, rpc.calls


def bucket_summary(sub: pd.DataFrame) -> dict:
    return cell_metrics(sub)


def write_readout(path: Path, summary: dict) -> None:
    s = summary
    lines = [
        "# Phase E1d: Pre-Registered Bucket Validation",
        "",
        f"> holdout: **{s['holdout_range']}** | validation days={s['n_holdout_days']} | RPC={s['rpc_calls']}",
        f"> E1c discovery window (excluded): {E1C_DISCOVERY_LO} ~ {E1C_DISCOVERY_HI}",
        "",
        "## Pre-registered buckets (frozen)",
        "",
        "| Bucket | Rule |",
        "|--------|------|",
        "| **A** | n_trades_15 ≥ day q95 AND 0.35 ≤ progress_15 < 0.55 |",
        "| **B** | n_trades_15 ≥ day q95 AND 0 ≤ progress_15 < 0.35 |",
        "| **C** | n_trades_15 ≥ day q95 AND progress_15 ≥ 0.75 (overheat) |",
        "",
        "## 1. Bucket outcomes (holdout full replay)",
        "",
        "| Bucket | n | p(trad) | p(big) | p(bad25) | p(fatal50) | median | mean | drop_top1 |",
        "|--------|---|---------|--------|----------|------------|--------|------|-----------|",
    ]
    for b in ["A", "B", "C"]:
        m = s["buckets"].get(b, {})
        lines.append(
            f"| {b} ({BUCKETS[b]['label']}) | {m.get('n_labeled', m.get('n', 0))} | "
            f"{_p(m.get('p_tradable_tail'))} | {_p(m.get('p_big_tail'))} | {_p(m.get('p_bad25'))} | "
            f"{_p(m.get('p_fatal50'))} | {_p(m.get('median_ret'), True)} | {_p(m.get('mean_ret'), True)} | "
            f"{_p(m.get('drop_top1_mean'), True)} |"
        )

    lines.extend(["", "## 2. A/B vs overheat C", ""])
    ab = s.get("ab_combined", {})
    c = s["buckets"].get("C", {})
    lines.append(f"- **A∪B** n={ab.get('n_labeled',0)} p(trad)={_p(ab.get('p_tradable_tail'))} p(bad25)={_p(ab.get('p_bad25'))} median={_p(ab.get('median_ret'), True)}")
    lines.append(f"- **C overheat** n={c.get('n_labeled',0)} p(trad)={_p(c.get('p_tradable_tail'))} p(bad25)={_p(c.get('p_bad25'))} median={_p(c.get('median_ret'), True)}")

    lines.extend(["", "## 3. Daily breakdown", ""])
    for bkey in ["A", "B", "C"]:
        lines.append(f"### Bucket {bkey}")
        lines.append("")
        lines.append("| day | n | p(trad) | p(bad25) | median |")
        lines.append("|-----|---|---------|----------|--------|")
        for d in s["daily"].get(bkey, []):
            lines.append(
                f"| {d['day']} | {d.get('n_labeled',0)} | {_p(d.get('p_tradable_tail'))} | "
                f"{_p(d.get('p_bad25'))} | {_p(d.get('median_ret'), True)} |"
            )
        lines.append("")

    lines.extend(["", "## 4. Verdict", "", s["verdict"], ""])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _p(v, signed=False):
    if v is None:
        return "NA"
    if signed:
        return f"{v*100:+.1f}%"
    return f"{v*100:.1f}%"


def run_phase_e1d(skip_collect: bool = False, skip_replay: bool = False, per_day: int = 300) -> None:
    E1D_ROOT.mkdir(parents=True, exist_ok=True)
    days = holdout_days()
    holdout_range = f"{days[0]}~{days[-1]}" if days else "none"

    if not skip_collect:
        ensure_holdout_phase_b(per_day=per_day)

    uni = load_holdout_universe()
    feats = build_features(uni)
    assigned = assign_buckets(feats)

    # Prior labels from E1 pipeline (holdout mints should mostly be fresh)
    replay_map = load_replay_map()
    e1c = E1C_ROOT / "stratified_replay.jsonl"
    if e1c.exists():
        for line in open(e1c):
            r = json.loads(line)
            if r.get("real_return_10U") is not None:
                replay_map[r["mint"]] = float(r["real_return_10U"])

    bucketed = assigned[assigned["bucket"].notna()].copy()
    print(f"Phase E1d: holdout universe={len(uni)} bucket hits A={int((bucketed['bucket']=='A').sum())} "
          f"B={int((bucketed['bucket']=='B').sum())} C={int((bucketed['bucket']=='C').sum())}", flush=True)

    rpc_used = 0
    cp = E1D_ROOT / "bucket_replay.jsonl"
    if not skip_replay and len(bucketed) > 0:
        new_labels, rpc_used = replay_batch(bucketed, cp)
        replay_map.update(new_labels)

    bucketed["full_ret_15_90"] = bucketed["mint"].map(replay_map)
    labeled = bucketed[bucketed["full_ret_15_90"].notna()].copy()

    buckets_out = {}
    daily_out = {}
    for bkey in BUCKETS:
        sub = labeled[labeled["bucket"] == bkey]
        buckets_out[bkey] = bucket_summary(sub)
        buckets_out[bkey]["bucket"] = bkey
        buckets_out[bkey]["rule"] = BUCKETS[bkey]["label"]
        daily_out[bkey] = daily_breakdown(sub)

    ab = labeled[labeled["bucket"].isin(["A", "B"])]
    ab_combined = bucket_summary(ab)

    # Verdict (pre-registered criteria — no threshold changes)
    a, b, c = buckets_out.get("A", {}), buckets_out.get("B", {}), buckets_out.get("C", {})
    na = a.get("n_labeled", 0)
    nb = b.get("n_labeled", 0)
    nc = c.get("n_labeled", 0)
    verdict_parts = [
        f"Holdout **{holdout_range}** ({len(days)} days, n_universe={len(uni)}).",
        f"Bucket A n={na} B n={nb} C n={nc} (full replay).",
    ]
    if na + nb + nc < 10:
        verdict_parts.append("**INSUFFICIENT n** for definitive validation.")
    else:
        pt_ab = ab_combined.get("p_tradable_tail")
        pt_c = c.get("p_tradable_tail")
        pb_ab = ab_combined.get("p_bad25")
        pb_c = c.get("p_bad25")
        if pt_ab is not None and pt_c is not None and pt_ab > pt_c and (pb_ab or 1) <= (pb_c or 1):
            verdict_parts.append(
                f"A∪B vs C: p(trad) {_p(pt_ab)}>{_p(pt_c)}, p(bad25) {_p(pb_ab)}≤{_p(pb_c)} — **directionally consistent** with E1c."
            )
        else:
            verdict_parts.append(
                f"A∪B p(trad)={_p(pt_ab)} p(bad25)={_p(pb_ab)} vs C p(trad)={_p(pt_c)} p(bad25)={_p(pb_c)} — **holdout does not confirm** E1c sweet-spot at pre-registered thresholds."
            )
    verdict_parts.append("Thresholds frozen; no post-hoc tuning.")

    summary = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "phase": "e1d",
        "holdout_range": holdout_range,
        "n_holdout_days": len(days),
        "holdout_days": days,
        "e1c_discovery_excluded": f"{E1C_DISCOVERY_LO}~{E1C_DISCOVERY_HI}",
        "n_universe": len(uni),
        "n_bucket_hits": int(len(bucketed)),
        "n_labeled": int(len(labeled)),
        "rpc_calls": rpc_used,
        "buckets": buckets_out,
        "ab_combined": ab_combined,
        "daily": daily_out,
        "bucket_manifest": bucketed[["mint", "day", "bucket", "n_trades_15", "progress_15", "n15_q95"]].to_dict(orient="records"),
        "verdict": " ".join(verdict_parts),
    }

    labeled.to_parquet(E1D_ROOT / "labeled_buckets.parquet", index=False)
    save_json(E1D_ROOT / "summary.json", summary)
    write_readout(OUT_DIR / "_pump_lowcost_phase_e1d_bucket_validation_readout.md", summary)
    print(f"Phase E1d done: labeled={len(labeled)} rpc={rpc_used}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-collect", action="store_true")
    ap.add_argument("--skip-replay", action="store_true")
    ap.add_argument("--per-day", type=int, default=300)
    args = ap.parse_args()
    run_phase_e1d(skip_collect=args.skip_collect, skip_replay=args.skip_replay, per_day=args.per_day)
