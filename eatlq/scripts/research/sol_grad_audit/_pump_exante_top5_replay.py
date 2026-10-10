#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ex-ante TEST top5% 链上 T+15→T+90 replay + 随机对照."""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from _grad_audit_lib import load_grids, grid_price
from _pump_exante_rank import add_labels, build_rows, feature_cols, train_score
from _pump_tail_realgas_replay import (
    ENTRY_S,
    EXIT_S,
    NOTIONAL_USD,
    SOL_USD,
    RPC,
    collect_trades,
    grid_net,
    load_meta,
    reserves_near,
    rpc_url,
    simulate_roundtrip,
    tx_reserves,
)
from _pump_tail_realgas_replay import b58dec  # noqa: F401

OUT_DIR = Path(__file__).resolve().parent


def load_done(*paths: Path) -> set[str]:
    done: set[str] = set()
    for p in paths:
        if not p.exists():
            continue
        if p.suffix == ".jsonl":
            for line in open(p):
                done.add(json.loads(line)["mint"])
        elif p.suffix == ".json":
            data = json.loads(p.read_text())
            for r in data.get("results", []):
                done.add(r["mint"])
    return done


def build_exante_cohort(seed: int = 42) -> tuple[list[dict], pd.DataFrame]:
    replay_prior = OUT_DIR / "_pump_tail_realgas_replay.json"
    rows = build_rows(replay_prior if replay_prior.exists() else None)
    for r in rows:
        add_labels(r)
    df = pd.DataFrame(rows)
    df = df[df["split"].isin(["TRAIN", "TEST"])].copy()
    fc = feature_cols(df, "all_exante")
    scores, _ = train_score(df, "label_100", fc)
    df["score"] = scores
    te = df[df["split"] == "TEST"].copy()
    k = max(1, int(len(te) * 0.05))
    top = te.sort_values("score", ascending=False).head(k)
    rest = te[~te["mint"].isin(top["mint"])]
    rng = random.Random(seed)
    rand = rest.sample(n=min(k, len(rest)), random_state=seed) if len(rest) >= k else rest

    cohort = []
    for i, (_, r) in enumerate(top.iterrows(), 1):
        cohort.append({
            "mint": r["mint"],
            "cohort": "exante_top5",
            "score": float(r["score"]),
            "rank": i,
            "grid_net_return": float(r["final_ret"]),
            "label_100": int(r["label_100"]),
            "label_300": int(r["label_300"]),
            "label_1000": int(r["label_1000"]),
        })
    for _, r in rand.iterrows():
        cohort.append({
            "mint": r["mint"],
            "cohort": "test_random5",
            "score": float(r["score"]),
            "grid_net_return": float(r["final_ret"]),
            "label_100": int(r["label_100"]),
            "label_300": int(r["label_300"]),
            "label_1000": int(r["label_1000"]),
        })
    return cohort, te


def replay_one(rpc: RPC, c: dict, meta: dict, grids: dict, tx_cache: dict, cost: float) -> dict:
    mint = c["mint"]
    m = meta.get(mint, {})
    bc = m.get("bonding_curve")
    created_ms = m.get("created_ms")
    if not bc or not created_ms:
        return {"mint": mint, "cohort": c["cohort"], "ok": False, "reason": "missing_bc_or_created"}

    created = created_ms / 1000.0
    entry_t = created + ENTRY_S
    exit_t = created + EXIT_S
    mint_b = b58dec(mint)
    trades, reached = collect_trades(rpc, bc, created - 5)
    ent = reserves_near(rpc, trades, entry_t, mint_b, tx_cache, before=True)
    ex = reserves_near(rpc, trades, exit_t, mint_b, tx_cache, before=True)
    grid_ret = grid_net(mint, grids, cost)
    sims = {}
    if ent and ex:
        for usd in NOTIONAL_USD:
            sol_in = usd / SOL_USD
            sims[f"{usd}U"] = simulate_roundtrip((ent[0], ent[1]), (ex[0], ex[1]), sol_in)
    sim10 = sims.get("10U", {})
    row = {
        "mint": mint,
        "cohort": c["cohort"],
        "exante_score": c.get("score"),
        "exante_rank": c.get("rank"),
        "grid_net_return": c.get("grid_net_return"),
        "grid_recalc": grid_ret,
        "label_100_grid": c.get("label_100"),
        "label_300_grid": c.get("label_300"),
        "label_1000_grid": c.get("label_1000"),
        "bonding_curve": bc,
        "created_ms": created_ms,
        "trades_collected": len(trades),
        "reached_birth": reached,
        "entry_reserves": {"vSol": ent[0], "vTok": ent[1], "sig": ent[2]} if ent else None,
        "exit_reserves": {"vSol": ex[0], "vTok": ex[1], "sig": ex[2]} if ex else None,
        "simulations": sims,
        "ok": bool(ent and ex and sim10.get("ok")),
        "real_return_10U": sim10.get("real_return"),
        "spot_return": sim10.get("spot_return"),
        "slippage_vs_spot_10U": sim10.get("slippage_vs_spot"),
        "tradability": "OK" if ent and ex else "NEEDS_RPC_REPLAY",
    }
    if ent and ex and grid_ret is not None and sim10.get("real_return") is not None:
        row["grid_vs_real_delta_10U"] = sim10["real_return"] - grid_ret
    return row


def cohort_stats(rows: list[dict], label: str) -> dict:
    sub = [r for r in rows if r.get("cohort") == label and r.get("real_return_10U") is not None]
    if not sub:
        return {"n": 0}
    rets = [r["real_return_10U"] for r in sub]
    n = len(rets)
    return {
        "n": n,
        "n_ok": sum(1 for r in sub if r.get("ok")),
        "mean": sum(rets) / n,
        "median": st.median(rets),
        "p_gt_100": sum(1 for x in rets if x >= 1.0) / n,
        "p_gt_300": sum(1 for x in rets if x >= 3.0) / n,
        "p_gt_1000": sum(1 for x in rets if x >= 10.0) / n,
        "p_positive": sum(1 for x in rets if x > 0) / n,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.03)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--output", default=str(OUT_DIR / "_pump_exante_top5_replay.json"))
    ap.add_argument("--checkpoint", default=str(OUT_DIR / "_pump_exante_top5_checkpoint.jsonl"))
    ap.add_argument("--skip-random", action="store_true", help="only replay exante_top5")
    args = ap.parse_args()

    cohort, te_all = build_exante_cohort(args.seed)
    if args.skip_random:
        cohort = [c for c in cohort if c["cohort"] == "exante_top5"]

    prior_cp = OUT_DIR / "_pump_tail_realgas_checkpoint.jsonl"
    prior_json = OUT_DIR / "_pump_tail_realgas_replay.json"
    done = load_done(Path(args.checkpoint), prior_cp, prior_json)

    # reuse prior full results when available
    prior_rows = {}
    for p in [prior_json, Path(args.checkpoint)]:
        if not p.exists():
            continue
        if p.suffix == ".json":
            for r in json.loads(p.read_text()).get("results", []):
                prior_rows[r["mint"]] = r
        else:
            for line in open(p):
                r = json.loads(line)
                prior_rows[r["mint"]] = r

    grids = load_grids()
    meta = load_meta()
    cp = Path(args.checkpoint)
    results: dict[str, dict] = {}
    for c in cohort:
        if c["mint"] in prior_rows and prior_rows[c["mint"]].get("real_return_10U") is not None:
            pr = prior_rows[c["mint"]].copy()
            pr["cohort"] = c["cohort"]
            pr["exante_score"] = c.get("score")
            pr["exante_rank"] = c.get("rank")
            pr["label_100_grid"] = c.get("label_100")
            pr["label_300_grid"] = c.get("label_300")
            pr["label_1000_grid"] = c.get("label_1000")
            results[c["mint"]] = pr
        elif c["mint"] in done and cp.exists():
            for line in open(cp):
                r = json.loads(line)
                if r["mint"] == c["mint"]:
                    results[c["mint"]] = r

    pending = [c for c in cohort if c["mint"] not in results]
    rpc = RPC(rpc_url(), min_interval=0.20)
    tx_cache: dict = {}

    print(f"[exante-replay] cohort={len(cohort)} cached={len(results)} pending={len(pending)} rpc_start={rpc.calls}")

    with open(cp, "a") as cpf:
        for i, c in enumerate(pending):
            row = replay_one(rpc, c, meta, grids, tx_cache, args.cost)
            cpf.write(json.dumps(row) + "\n")
            cpf.flush()
            results[c["mint"]] = row
            if (i + 1) % 5 == 0 or i + 1 == len(pending):
                print(f"  [{i+1}/{len(pending)}] rpc={rpc.calls} {c['cohort']} {c['mint'][:8]} ok={row.get('ok')}", flush=True)

    all_rows = [results[c["mint"]] for c in cohort if c["mint"] in results]
    top5 = cohort_stats(all_rows, "exante_top5")
    rand5 = cohort_stats(all_rows, "test_random5")
    test_base = {
        "n": len(te_all),
        "grid_p100": float(te_all["label_100"].mean()),
        "grid_p1000": float(te_all["label_1000"].mean()),
        "grid_mean": float(te_all["final_ret"].mean()),
    }

    lift_p100 = (top5["p_gt_100"] / rand5["p_gt_100"]) if rand5.get("p_gt_100") else None
    lift_p1000 = (top5["p_gt_1000"] / rand5["p_gt_1000"]) if rand5.get("p_gt_1000") else None
    sig = (
        top5.get("n", 0) >= 20
        and top5.get("p_gt_100", 0) > rand5.get("p_gt_100", 0) * 1.5
        and top5.get("median", -1) > rand5.get("median", 0)
    )

    summary = {
        "n_cohort": len(cohort),
        "n_replayed_new": len(pending),
        "rpc_calls": rpc.calls,
        "exante_top5": top5,
        "test_random5": rand5,
        "test_grid_baseline": test_base,
        "lift_real_p100_top5_vs_random": lift_p100,
        "lift_real_p1000_top5_vs_random": lift_p1000,
        "verdict": "PASS_EXANTE_LIFT" if sig else "WEAK_OR_NO_LIFT",
    }

    out = {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "entry_s": ENTRY_S,
            "exit_s": EXIT_S,
            "cost": args.cost,
            "notional_usd": NOTIONAL_USD,
            "rpc_calls": rpc.calls,
        },
        "summary": summary,
        "results": all_rows,
    }
    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    csv_path = OUT_DIR / "_pump_exante_top5_replay.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        fields = ["mint", "cohort", "ok", "exante_score", "exante_rank",
                  "grid_net_return", "real_return_10U", "spot_return",
                  "grid_vs_real_delta_10U", "label_100_grid", "label_1000_grid"]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)

    md = OUT_DIR / "_pump_exante_top5_replay_readout.md"
    lines = [
        "# Ex-ante Top5% 链上 Replay 验证",
        "",
        f"RPC：**{rpc.calls}** | 新跑：**{len(pending)}** | cohort：**{len(cohort)}**",
        "",
        "## 链上真实收益（10U, T+15→T+90）",
        "",
        "| 分组 | n | p(+100%) | p(+300%) | p(+1000%) | median | mean |",
        "|------|---|----------|----------|-----------|--------|------|",
    ]
    for name, stat in [("exante_top5", top5), ("test_random5", rand5)]:
        if stat.get("n"):
            lines.append(
                f"| {name} | {stat['n']} | {stat['p_gt_100']:.2%} | {stat['p_gt_300']:.2%} | "
                f"{stat['p_gt_1000']:.2%} | {stat['median']:+.3f} | {stat['mean']:+.3f} |"
            )
    lines.extend([
        "",
        f"## Grid 基线（TEST 全样本, n={test_base['n']}）",
        f"- grid p(+100%)={test_base['grid_p100']:.2%} | grid p(+1000%)={test_base['grid_p1000']:.2%}",
        "",
        f"## 裁定：**{summary['verdict']}**",
        f"- real p(+100%) lift top5/random = {lift_p100}",
        f"- real p(+1000%) lift top5/random = {lift_p1000}",
        "",
        "详见 `_pump_exante_top5_replay.json`",
    ])
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Done rpc={rpc.calls} verdict={summary['verdict']} -> {args.output}")


if __name__ == "__main__":
    main()
