#!/usr/bin/env python3
"""Stress replay for 167 F-blocked rug-but-profitable pools.

Uses archive eth_call only (real-gas sell probe + QuoterV2 quotes) — no broadcast txs,
no on-chain gas. Does consume archive RPC quota (same path as dynamic_size_realgas).

Scenarios:
  base50          $50 notional, buy@entry, sell@exb
  exit_p{60,300,600}   sell delayed by N blocks (same tokens from entry)
  entry_p{1,3,10}      buy delayed; sell@original exb (absolute calendar)
  entry_s{1,3,10}      buy delayed; sell@exb+delta (same hold duration)
  e_window          live E: every 3 blocks in POSTBUY window, depth<0.5E or sell fail -> exit
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd
from eth_abi import encode
from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import live_config_rebacktest as L  # noqa: E402
import keystone_live as K  # noqa: E402
import replay_realgas_sellsim as RG  # noqa: E402

SIZE_USD = 50.0
ETH_USD = K.ETH_USD
AMT50 = int(SIZE_USD / ETH_USD * 1e18)
FRIC_THR = -0.05
EXIT_DELAYS = (60, 300, 600)
ENTRY_DELAYS = (1, 3, 10)
E_WINDOW_BLOCKS = 900  # POSTBUY_RECHECK_WINDOW_SEC=1800 @ ~2s/block
E_RECHECK_STEP = 3  # EXECUTOR_POSTBUY_RECHECK_BLOCKS
DEPTH_COLLAPSE_WEI = int(0.5 * 1e18)

OUT_DIR = "/opt/EATLQ/mvp/audits/stress167_20260624"
CACHE_PATH = os.path.join(OUT_DIR, "_stress167_cache.json")
DYN_CACHE = os.path.join(HERE, "_dynamic_size_realgas.json")

WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
_BAL_SIG = Web3.keccak(text="balanceOf(address)")[:4]


def _block_tag(b: int) -> str:
    return hex(int(b))


def pool_weth_at(pool: str, block: int) -> Optional[int]:
    pool = Web3.to_checksum_address(pool)
    data = "0x" + (_BAL_SIG + encode(["address"], [pool])).hex()
    try:
        r = RG.w3.provider.make_request("eth_call", [{"to": WETH, "data": data}, _block_tag(block)])
        res = r.get("result")
        if not res or res == "0x":
            return None
        return int(res, 16)
    except Exception:
        return None


def quote_buy_at(alt: str, fee: int, block: int, amt: int = AMT50) -> Optional[int]:
    params = encode(["(address,address,uint256,uint24,uint160)"], [(RG.WETH, alt, amt, fee, 0)])
    data = "0x" + (RG._QUOTE_SIG + params).hex()
    try:
        r = RG.w3.provider.make_request("eth_call", [{"to": RG.QUOTER, "data": data}, _block_tag(block)])
        res = r.get("result")
        if not res or res == "0x":
            return None
        return RG._parse_uint(res[:66])
    except Exception:
        return None


def real_net(alt: str, fee: int, tokens: int, sell_block: int, amt: int = AMT50) -> dict[str, Any]:
    xv, xout = RG.realgas_sell_ok(alt, fee, tokens, sell_block)
    if xv == "ok":
        return {"exit_sell": xv, "real_net": xout / amt - 1.0}
    if xv == "revert":
        return {"exit_sell": xv, "real_net": -1.0}
    return {"exit_sell": xv, "real_net": None}


@dataclass
class PoolCtx:
    pool: str
    alt: str
    fee: int
    entry_block: int
    exit_block: int
    exwhy: str
    flip_block: int
    flip_offset: int


def load_167() -> list[PoolCtx]:
    labels: dict[str, dict] = {}
    for f in sorted(os.listdir(os.path.join(HERE, "_uni_shards"))):
        if not f.startswith("_uni_s"):
            continue
        for p in json.load(open(os.path.join(HERE, "_uni_shards", f)))["pools"]:
            labels[p["pool"]] = p

    priced = json.load(open(K.PCACHE))
    df = L.run_lens(L.load_base_df(), "qv2")
    dep, skel, creators, token_skel = L._load_blacklists()
    masks = L.gate_masks(df, dep, skel, creators, token_skel)
    sel = df[
        masks["model_depth"]
        & ~masks["live_full"]
        & df.gate_fric
        & df.tradeable
        & (df.label == "delayed_rug")
        & (df.ret > 0)
    ]
    out: list[PoolCtx] = []
    for _, r in sel.iterrows():
        pr = priced[r.pool]
        sh = labels[r.pool]
        out.append(
            PoolCtx(
                pool=r.pool,
                alt=Web3.to_checksum_address(r.alt),
                fee=int(pr["fee"]),
                entry_block=int(r.entry_block),
                exit_block=int(pr["exb"]),
                exwhy=pr.get("exwhy") or "",
                flip_block=int(sh["flip_block"]),
                flip_offset=int(sh["flip_offset"]),
            )
        )
    return out


def e_window_exit(ctx: PoolCtx, tokens: int) -> dict[str, Any]:
    start = ctx.entry_block
    end = min(ctx.exit_block, ctx.entry_block + E_WINDOW_BLOCKS)
    trigger_block = None
    trigger_reason = None
    for blk in range(start + E_RECHECK_STEP, end + 1, E_RECHECK_STEP):
        weth = pool_weth_at(ctx.pool, blk)
        if weth is not None and weth < DEPTH_COLLAPSE_WEI:
            trigger_block = blk
            trigger_reason = "depth_collapse"
            break
        xv, _ = RG.realgas_sell_ok(ctx.alt, ctx.fee, tokens, blk)
        if xv == "revert":
            trigger_block = blk
            trigger_reason = "sell_fail"
            break
    if trigger_block is None:
        trigger_block = ctx.exit_block
        trigger_reason = "planned_exb"
    return {
        "exit_block": trigger_block,
        "trigger_reason": trigger_reason,
        **real_net(ctx.alt, ctx.fee, tokens, trigger_block),
    }


def run_scenario(ctx: PoolCtx, scenario: str) -> dict[str, Any]:
    rec: dict[str, Any] = {"scenario": scenario, "status": "pending"}
    fee = ctx.fee
    if fee not in (100, 500, 3000, 10000):
        rec.update(status="bad_fee")
        return rec

    if scenario == "base50":
        eb, xb = ctx.entry_block, ctx.exit_block
    elif scenario.startswith("exit_p"):
        delta = int(scenario.split("_p")[1])
        eb, xb = ctx.entry_block, ctx.exit_block + delta
    elif scenario.startswith("entry_p"):
        delta = int(scenario.split("_p")[1])
        eb, xb = ctx.entry_block + delta, ctx.exit_block
    elif scenario.startswith("entry_s"):
        delta = int(scenario.split("_s")[1])
        eb, xb = ctx.entry_block + delta, ctx.exit_block + delta
    elif scenario == "e_window":
        tokens = quote_buy_at(ctx.alt, fee, ctx.entry_block)
        if not tokens or tokens <= 0:
            rec.update(status="entry_fail", entry_block=ctx.entry_block)
            return rec
        ev, _ = RG.realgas_sell_ok(ctx.alt, fee, tokens, ctx.entry_block)
        rec.update(
            entry_block=ctx.entry_block,
            entry_sell=ev,
            token_out=int(tokens),
            **e_window_exit(ctx, tokens),
            status="ok",
        )
        return rec
    else:
        rec.update(status="unknown_scenario")
        return rec

    if xb <= eb:
        rec.update(status="invalid_timing", entry_block=eb, exit_block=xb)
        return rec

    tokens = quote_buy_at(ctx.alt, fee, eb)
    if not tokens or tokens <= 0:
        rec.update(status="entry_fail", entry_block=eb, exit_block=xb)
        return rec

    ev, _ = RG.realgas_sell_ok(ctx.alt, fee, tokens, eb)
    sell = real_net(ctx.alt, fee, tokens, xb)
    rec.update(
        status="ok",
        entry_block=eb,
        exit_block=xb,
        entry_sell=ev,
        token_out=int(tokens),
        margin_to_flip=ctx.flip_block - xb,
        **sell,
    )
    return rec


def all_scenarios() -> list[str]:
    xs = ["base50"]
    xs += [f"exit_p{d}" for d in EXIT_DELAYS]
    xs += [f"entry_p{d}" for d in ENTRY_DELAYS]
    xs += [f"entry_s{d}" for d in ENTRY_DELAYS]
    xs += ["e_window"]
    return xs


def summarize(results: dict[str, dict]) -> dict[str, Any]:
    """Aggregate per scenario across pools."""
    scenarios = all_scenarios()
    base_by_pool = {k: v for k, v in results.items() if k.endswith(":base50")}

    out: dict[str, Any] = {}
    for sc in scenarios:
        nets = []
        fatals = 0
        ok = 0
        was_big_win = 0
        big_to_fatal = 0
        for key, rec in results.items():
            if not key.endswith(f":{sc}"):
                continue
            pool = key.rsplit(":", 1)[0]
            if rec.get("status") != "ok" or rec.get("real_net") is None:
                continue
            rn = float(rec["real_net"])
            nets.append(rn)
            ok += 1
            if rn <= -0.5:
                fatals += 1
            base = base_by_pool.get(f"{pool}:base50")
            if base and base.get("real_net") is not None and float(base["real_net"]) >= 0.60:
                was_big_win += 1
                if rn <= -0.5:
                    big_to_fatal += 1
        if not nets:
            out[sc] = {"n": 0}
            continue
        import numpy as np

        a = np.array(nets)
        out[sc] = {
            "n_ok": ok,
            "mean": float(a.mean()),
            "median": float(np.median(a)),
            "wr": float((a > 0).mean()),
            "fatal_rate": float((a <= -0.5).mean()),
            "total_pnl_usd": float((a * SIZE_USD).sum()),
            "was_base_ge60pct": was_big_win,
            "base_ge60_to_fatal": big_to_fatal,
        }
    return out


def write_report(results: dict, summary: dict, pools: list[PoolCtx]) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(results, open(os.path.join(OUT_DIR, "stress167_raw.json"), "w"), indent=2)
    json.dump(summary, open(os.path.join(OUT_DIR, "stress167_summary.json"), "w"), indent=2)

    lines = [
        "# 167 笔 F-blocked rug 盈利 — 压力 replay",
        "",
        f"- 规模: **${SIZE_USD:.0f}** notional, real-gas sell probe + QuoterV2",
        "- RPC: 仅 archive `eth_call`，无广播 tx / 无链上 gas",
        f"- 池数: {len(pools)}",
        "",
        "## 场景",
        "",
        "| 场景 | 说明 |",
        "|---|---|",
        "| base50 | 基准: buy@entry, sell@exb |",
        "| exit_p* | 出场延迟 +N blocks（同 entry 买到的 token） |",
        "| entry_p* | 进场延迟 +N blocks，sell@原 exb（绝对日历） |",
        "| entry_s* | 进场+出场同步延迟 +N blocks（持仓时长不变） |",
        f"| e_window | 每 {E_RECHECK_STEP} block 探测 depth<{DEPTH_COLLAPSE_WEI/1e18:.1f}E 或 sell fail → 强平 |",
        "",
        "## 汇总",
        "",
        "| 场景 | n | median | WR | fatal% | total $ | base≥60%→fatal |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for sc in all_scenarios():
        s = summary.get(sc, {})
        if not s.get("n_ok"):
            continue
        lines.append(
            f"| {sc} | {s['n_ok']} | {s['median']:+.1%} | {s['wr']:.0%} | "
            f"{s['fatal_rate']:.1%} | ${s['total_pnl_usd']:,.0f} | {s['base_ge60_to_fatal']}/{s['was_base_ge60pct']} |"
        )
    lines.append("")
    open(os.path.join(OUT_DIR, "stress167_cn.md"), "w").write("\n".join(lines) + "\n")


def main() -> None:
    pools = load_167()
    os.makedirs(OUT_DIR, exist_ok=True)
    cache: dict[str, dict] = json.load(open(CACHE_PATH)) if os.path.exists(CACHE_PATH) else {}
    dyn: dict[str, dict] = json.load(open(DYN_CACHE)) if os.path.exists(DYN_CACHE) else {}

    scenarios = all_scenarios()
    total = len(pools) * len(scenarios)
    done = sum(1 for p in pools for sc in scenarios if f"{p.pool}:{sc}" in cache)
    print(f"stress167 pools={len(pools)} scenarios={len(scenarios)} cached={done}/{total}", flush=True)

    t0 = time.time()
    for i, ctx in enumerate(pools):
        for sc in scenarios:
            key = f"{ctx.pool}:{sc}"
            if key in cache:
                continue
            cache[key] = run_scenario(ctx, sc)
            done += 1

            if sc == "base50" and cache[key].get("status") == "ok":
                dyn_key = f"{ctx.pool}:50"
                if dyn_key not in dyn:
                    rec = cache[key]
                    dyn[dyn_key] = {
                        "size_usd": 50,
                        "fee": ctx.fee,
                        "entry_block": rec["entry_block"],
                        "exit_block": rec["exit_block"],
                        "exwhy": ctx.exwhy,
                        "fric": None,
                        "net": None,
                        "real_net": rec.get("real_net"),
                        "entry_sell": rec.get("entry_sell"),
                        "exit_sell": rec.get("exit_sell"),
                        "token_out": rec.get("token_out"),
                        "why": "ok",
                    }

            if done % 10 == 0:
                json.dump(cache, open(CACHE_PATH, "w"))
                json.dump(dyn, open(DYN_CACHE, "w"), indent=2, sort_keys=True)
                el = time.time() - t0
                print(f"  {done}/{total} elapsed={el:.0f}s last={ctx.pool[:10]}:{sc}", flush=True)

    json.dump(cache, open(CACHE_PATH, "w"))
    json.dump(dyn, open(DYN_CACHE, "w"), indent=2, sort_keys=True)

    summary = summarize(cache)
    write_report(cache, summary, pools)
    print(json.dumps(summary, indent=2))
    print(f"wrote {OUT_DIR}/stress167_cn.md", flush=True)


if __name__ == "__main__":
    main()
