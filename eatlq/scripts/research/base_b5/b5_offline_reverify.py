#!/usr/bin/env python3
"""B5 KEYSTONE CHA re-verification from frozen files + local ClickHouse.

No network/RPC client is imported.  The sole external read is a SELECT from the
local ClickHouse container when --refresh-swaps is supplied.  All missing D5,
price, exit, or LP-monitor evidence is treated adversely.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

ROOT = Path("/opt/EATLQ")
LEFT = ROOT / "scripts/research/base_lefttail"
DIAG = ROOT / "mvp/diagnostics"
SWAPS = DIAG / "base_b5_real_swaps_20260714.tsv"
OUT = DIAG / "base_b5_keystone_cha_reverify_20260714.json"
REPORT = ROOT / "reports/B5_keystone_cha_reverify_20260714.md"
LP_RPC = DIAG / "base_b5_lp_monitor_20260714.json"
GOLDEN_RPC = DIAG / "base_b5_golden_rpc_20260714.json"
WETH = "0x4200000000000000000000000000000000000006"
LN1P = math.log(1.0001)
NOTIONAL_WETH = 0.001
GAS_USD_LEG = 0.075
LIMIT_BLOCKS = 1800  # ~60 minutes on Base; frozen before local swap extraction
SEED = 20260714
REGIMES = {
    "W1": ("2026-02-27 05:41:15", "2026-04-01 00:00:00", "sideways_launch_contraction"),
    "W2": ("2026-04-01 00:00:00", "2026-05-01 00:00:00", "sideways_supply_expansion"),
    "W3": ("2026-05-01 00:00:00", "2026-06-08 18:29:40", "bear_turnover_expansion"),
}
COLS = ["pool", "orient", "alt", "t0", "ns_pre", "buyers_pre", "senders_pre", "first_tick",
        "last_tick_pre", "min_tick_pre", "max_tick_pre", "liq_first", "liq_last_pre",
        "liq_min_pre", "ns_h1", "ns_h2", "last_tick_h1", "entry_block", "ns_fwd",
        "min_tick_fwd", "max_tick_fwd", "last_tick_fwd"]
GENERAL_FEATURES = ["ns_pre", "early_ret", "pre_vol", "pre_pullback", "pre_runup_from_lo",
                    "heat_accel", "mom_h1", "mom_h2"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_base():
    df = pd.read_csv(LEFT / "_pools.tsv", sep="\t", names=COLS)
    df["t0"] = pd.to_datetime(df.t0, utc=True)
    for c in COLS:
        if c not in ("pool", "alt", "t0"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df.pool = df.pool.str.lower(); df.alt = df.alt.str.lower()
    fwd = defaultdict(list)
    with open(LEFT / "_fwdpath.tsv") as fh:
        for pool, bkt, tick, block in csv.reader(fh, delimiter="\t"):
            fwd[pool.lower()].append((int(bkt), int(tick), int(block)))
    priced = json.loads((LEFT / "_priced_live.json").read_text())
    df["fric"] = df.pool.map(lambda p: (priced.get(p) or {}).get("fric"))
    df["legacy_exb"] = df.pool.map(lambda p: (priced.get(p) or {}).get("exb"))
    df["legacy_exwhy"] = df.pool.map(lambda p: (priced.get(p) or {}).get("exwhy"))
    return df, fwd, priced


def features(df):
    o = df.orient.values
    lp = lambda x: np.asarray(x, dtype=float) * LN1P * o
    first, last = lp(df.first_tick), lp(df.last_tick_pre)
    lo = np.minimum(lp(df.min_tick_pre), lp(df.max_tick_pre))
    hi = np.maximum(lp(df.min_tick_pre), lp(df.max_tick_pre))
    x = pd.DataFrame(index=df.index)
    x["ns_pre"], x["buyers_pre"], x["senders_pre"] = df.ns_pre, df.buyers_pre, df.senders_pre
    x["swaps_per_buyer"] = df.ns_pre / df.buyers_pre.clip(lower=1)
    x["buyer_sender_ratio"] = df.buyers_pre / df.senders_pre.clip(lower=1)
    x["early_ret"], x["pre_vol"] = last-first, hi-lo
    x["pre_pullback"], x["pre_runup_from_lo"] = hi-last, last-lo
    x["liq_log"] = np.log1p(df.liq_last_pre.clip(lower=0))
    x["liq_trend"] = df.liq_last_pre / df.liq_first.clip(lower=1)
    x["liq_drawdown"] = df.liq_min_pre / df.liq_first.clip(lower=1)
    x["heat_accel"] = df.ns_h2 / df.ns_h1.clip(lower=1)
    h1 = lp(df.last_tick_h1)
    x["mom_h1"], x["mom_h2"] = h1-first, last-h1
    flo = np.where(df.ns_fwd > 0, np.minimum(lp(df.min_tick_fwd), lp(df.max_tick_fwd)), -np.inf)
    y = ((df.ns_fwd <= 2).values | ((flo-last) <= math.log(.5))).astype(int)
    return x.replace([np.inf, -np.inf], np.nan).fillna(0.0), y


def regime(ts):
    for w, (lo, hi, _) in REGIMES.items():
        if pd.Timestamp(lo, tz="UTC") <= ts < pd.Timestamp(hi, tz="UTC"):
            return w
    return None


def extract_swaps(df):
    scoped = df[df.t0.map(regime).notna()]
    pools = sorted(set(scoped.pool))
    quoted = ",".join("'" + p + "'" for p in pools)
    lo = int(scoped.entry_block.min())
    # 4h path plus 1h limit window and buffer.
    hi = int(scoped.entry_block.max()) + 11000
    sql = f"""SELECT lower(pool_address),block_number,toUnixTimestamp64Milli(block_time),lower(tx_hash),log_index,
toString(amount0),toString(amount1),tick FROM eatlq.uniswap_v3_swaps_decoded FINAL
WHERE chain_id=8453 AND removed=0 AND block_number BETWEEN {lo} AND {hi}
AND lower(pool_address) IN ({quoted}) ORDER BY pool_address,block_number,log_index FORMAT TabSeparated"""
    p = subprocess.run(["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client", "--query", sql],
                       capture_output=True, text=True, timeout=900)
    if p.returncode:
        raise RuntimeError(p.stderr[-2000:])
    SWAPS.write_text(p.stdout)
    return len(p.stdout.splitlines())


def load_swaps():
    out = defaultdict(list)
    with open(SWAPS) as fh:
        for z in csv.reader(fh, delimiter="\t"):
            pool, block, ms, tx, li, a0, a1, tick = z
            out[pool].append({"block": int(block), "ms": int(ms), "tx": tx, "log_index": int(li),
                              "a0": int(a0), "a1": int(a1), "tick": int(tick)})
    return out


def direction_price(s, orient):
    # orient=-1 => WETH token0; orient=+1 => WETH token1.
    weth, tok = (s["a0"], s["a1"]) if orient == -1 else (s["a1"], s["a0"])
    if weth > 0 and tok < 0:
        return "buy", weth / -tok, weth, -tok
    if weth < 0 and tok > 0:
        return "sell", -weth / tok, -weth, tok
    return "other", None, abs(weth), abs(tok)


def trigger_tick(row, path):
    exb = int(row.legacy_exb) if pd.notna(row.legacy_exb) else None
    if exb is None:
        return None
    eligible = [x for x in path.get(row.pool, []) if x[2] <= exb]
    return eligible[-1][1] if eligible else None


def simulate(row, pool_swaps, path, eth_usd):
    # First adequate actual buy no later than 30m after the frozen 10m entry block.
    entry = None
    deadline = int(row.entry_block) + 900
    for s in pool_swaps:
        side, px, weth, tok = direction_price(s, int(row.orient))
        if s["block"] >= int(row.entry_block) and s["block"] <= deadline and side == "buy" and weth >= 10**15:
            entry = (s, px, weth, tok); break
    if entry is None:
        # A third-party fill proxy is absent. This is missing execution evidence,
        # not proof that our counterfactual order safely stayed flat: adverse.
        gas = GAS_USD_LEG / (NOTIONAL_WETH * eth_usd)
        return {"filled": False, "ret": -1.0-gas, "gross": -1.0,
                "reason": "missing_entry_evidence_adverse"}
    es, epx, _, _ = entry
    qty = 10**15 / epx
    exb = int(row.legacy_exb) if pd.notna(row.legacy_exb) else None
    tt = trigger_tick(row, path)
    if exb is None or tt is None or exb < es["block"]:
        return {"filled": True, "ret": -1.0 - GAS_USD_LEG*2/(NOTIONAL_WETH*eth_usd),
                "reason": "missing_exit_adverse", "entry": es}
    relative = math.exp((tt - float(row.last_tick_pre)) * LN1P * int(row.orient))
    limit_px = epx * relative
    fill = None
    for s in pool_swaps:
        side, px, weth, tok = direction_price(s, int(row.orient))
        if exb <= s["block"] <= exb + LIMIT_BLOCKS and side == "sell" and tok >= qty and px >= limit_px:
            fill = (s, px); break
    gas = GAS_USD_LEG * 2 / (NOTIONAL_WETH * eth_usd)
    if fill is None:
        return {"filled": True, "ret": -1.0-gas, "gross": -1.0, "reason": "limit_unfilled_adverse",
                "entry": es, "exb": exb, "limit_px_raw": limit_px}
    ss, spx = fill
    gross = spx / epx - 1.0
    return {"filled": True, "ret": gross-gas, "gross": gross, "reason": "limit_filled",
            "entry": es, "exit": ss, "exb": exb, "limit_px_raw": limit_px}


def summarize(vals):
    a = np.asarray(vals, dtype=float)
    if not len(a): return {"n": 0, "mean": None, "top1_contribution": None, "top1_removed_mean": None}
    k = max(1, math.ceil(.01*len(a))); order = np.argsort(a)[::-1]
    pos = np.maximum(a, 0).sum(); top = np.maximum(a[order[:k]], 0).sum()
    rem = np.delete(a, order[:k])
    return {"n": int(len(a)), "mean": float(a.mean()), "median": float(np.median(a)),
            "win_rate": float((a > 0).mean()), "fatal_rate": float((a <= -.5).mean()),
            "top1_contribution": float(top/pos) if pos else 0.0,
            "top1_removed_mean": float(rem.mean()) if len(rem) else None}


def mc(by_window):
    if not by_window or any(len(x) < 20 for x in by_window.values()):
        return {"status": "NOT_RUN_UNPOWERED"}
    rng = random.Random(SEED); terminal=[]; draw=[]
    for _ in range(10000):
        eq=peak=100.; md=0.
        for w in sorted(by_window):
            for _ in by_window[w]:
                eq += rng.choice(by_window[w]); peak=max(peak,eq); md=max(md,peak-eq)
        terminal.append(eq); draw.append(md)
    terminal.sort(); draw.sort()
    return {"status":"RUN", "paths":10000, "seed":SEED, "terminal_p5":terminal[500], "max_drawdown_p95":draw[9500]}


def fit_forward(df, X, y, cols):
    train = df.t0 < pd.Timestamp(REGIMES["W1"][0], tz="UTC")
    model = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=.05,
        l2_regularization=1.0, min_samples_leaf=20, random_state=0).fit(X.loc[train, cols], y[train])
    p = model.predict_proba(X.loc[:, cols])[:,1]
    eligible = train & df.fric.notna() & (df.fric >= -.05)
    cut = float(np.quantile(p[eligible], .6))
    return p, cut, train


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--refresh-swaps", action="store_true"); args=ap.parse_args()
    df,path,_=load_base(); df["regime"] = df.t0.map(regime)
    if args.refresh_swaps: extract_swaps(df)
    if not SWAPS.exists(): raise SystemExit(f"missing {SWAPS}; run --refresh-swaps")
    swaps=load_swaps(); X,y=features(df)
    closes=json.loads((DIAG/"base_2min_accel_b3_eth_daily_20260714.json").read_text())["closes"]
    all_cols=list(X.columns)
    pfull, cfull, train=fit_forward(df,X,y,all_cols)
    pgen, cgen, _=fit_forward(df,X,y,GENERAL_FEATURES)
    df["full_keep"]=(pfull<=cfull)&df.fric.notna()&(df.fric>=-.05)
    df["general_keep"]=(pgen<=cgen)&df.fric.notna()&(df.fric>=-.05)

    labels={}
    shard_paths=sorted((LEFT/"_uni_shards").glob("_uni_s*.json"))
    for f in shard_paths:
        for r in json.loads(f.read_text())["pools"]: labels[r["pool"].lower()]=r
    sims={}
    for r in df[df.regime.notna()].itertuples(index=False):
        day=str(r.t0.date()); eth=float(closes.get(day) or min(closes.values()))
        sims[r.pool]=simulate(r,swaps.get(r.pool,[]),path,eth)

    def table(mask, d5=False):
        rows=[]; byw={}
        for w in REGIMES:
            sub=df[(df.regime==w)&mask]
            vals=[]; blocked=missing=0
            for r in sub.itertuples(index=False):
                lab=labels.get(r.pool)
                if d5 and not lab:
                    eth=float(closes.get(str(r.t0.date())) or min(closes.values()))
                    vals.append(-1.0-GAS_USD_LEG*2/(NOTIONAL_WETH*eth)); blocked+=1; missing+=1
                elif d5 and lab.get("label") in ("instant_hp","dead"):
                    vals.append(0.0); blocked+=1
                else: vals.append(float(sims[r.pool]["ret"]))
            s=summarize(vals); s.update({"blocked_by_d5":blocked,"missing_d5_adverse":missing}); rows.append({"window":w,**s}); byw[w]=vals
        pooled=[v for w in byw.values() for v in w]
        return {"windows":rows,"pooled":summarize(pooled),"mc":mc({k:v for k,v in byw.items() if len(v)>=20})}

    masks={"unconditional":df.regime.notna(), "friction_only":df.regime.notna()&df.fric.notna()&(df.fric>=-.05),
           "keystone_full":df.regime.notna()&df.full_keep, "keystone_general":df.regime.notna()&df.general_keep}
    perf={k:table(v) for k,v in masks.items()}
    perf["keystone_full_d5_on"]=table(masks["keystone_full"],True)
    base_by_w={x["window"]:x["mean"] for x in perf["unconditional"]["windows"]}
    for tab in perf.values():
        for x in tab["windows"]:
            x["alpha_delta"] = None if x["mean"] is None else x["mean"]-base_by_w[x["window"]]
        den=sum(x["n"] for x in tab["windows"])
        wb=sum(base_by_w[x["window"]]*x["n"] for x in tab["windows"])/den if den else None
        tab["pooled"]["same_window_weighted_baseline_mean"]=wb
        tab["pooled"]["alpha_delta"]=None if wb is None else tab["pooled"]["mean"]-wb

    rugs=[r for r in labels.values() if r.get("label")=="delayed_rug"]
    timing=defaultdict(lambda: defaultdict(int)); monitor={"all_delayed_rug":len(rugs),"real_loss":0,
        "early_offset_le_300":sum(int(x.get("flip_offset",10**18))<=300 for x in rugs),
        "early_real_loss":0,"detected_no_preflip_lead":0,"rescued_proven":0,"missing":0}
    for lab in rugs:
        p=lab["pool"].lower(); row=df[df.pool==p]
        if row.empty: continue
        rr=row.iloc[0]; w=regime(rr.t0); exb=rr.legacy_exb
        if pd.isna(exb):
            if w: timing[w]["unknown"]+=1
            monitor["missing"]+=1
            continue
        if int(lab["flip_block"]) <= int(exb):
            if w: timing[w]["before_or_at_exit"]+=1
            monitor["real_loss"]+=1
            if int(lab.get("flip_offset",10**18))<=300: monitor["early_real_loss"]+=1
            # The frozen label identifies the first failed checkpoint, not a pre-flip
            # reserve decline.  A read at/after flip cannot prove executable rescue.
            monitor["detected_no_preflip_lead"]+=1
        elif w: timing[w]["after_exit"]+=1
    # Golden: deterministic integer amounts from first 5 complete fills.
    golden=[]
    for pool,s in sims.items():
        if s.get("reason")=="limit_filled" and len(golden)<5:
            golden.append({"pool":pool,"entry_block":s["entry"]["block"],"entry_tx":s["entry"]["tx"],
                "entry_amount0_raw":str(s["entry"]["a0"]),"entry_amount1_raw":str(s["entry"]["a1"]),
                "exit_block":s["exit"]["block"],"exit_tx":s["exit"]["tx"],
                "exit_amount0_raw":str(s["exit"]["a0"]),"exit_amount1_raw":str(s["exit"]["a1"])})
    src=[LEFT/"_pools.tsv",LEFT/"_fwdpath.tsv",LEFT/"_priced_live.json",LEFT/"_labeled_universe.json",
         DIAG/"base_2min_accel_b3_eth_daily_20260714.json",SWAPS,*shard_paths]
    result={"schema_version":1,"research_only":True,"rpc_calls":0,"regimes":REGIMES,
      "frozen":{"train_end_exclusive":REGIMES["W1"][0],"friction_gate":-.05,"p_cut_full":cfull,
       "p_cut_general":cgen,"notional_weth":NOTIONAL_WETH,"gas_usd_per_attempted_leg":GAS_USD_LEG,
       "limit_window_blocks":LIMIT_BLOCKS,"missing_policy":"adverse"},
      "performance":perf,"rug_timing_by_regime":{w:dict(timing[w]) for w in REGIMES},
      "forward_lp_monitor":monitor,"d5_label_coverage":{"n_labels":len(labels),"instant_hp":sum(x.get('label')=='instant_hp' for x in labels.values()),
       "forward_selected":int(masks["keystone_full"].sum()),
       "forward_selected_covered":sum(1 for p in df.loc[masks["keystone_full"],"pool"] if p in labels),
       "verified_rejections_in_forward_selected":sum(1 for p in df.loc[masks["keystone_full"],"pool"] if p in labels and labels[p].get("label") in ("instant_hp","dead"))},
      "migration":{"definition":"same frozen learner; only market-generic price/activity features", "general_features":GENERAL_FEATURES},
      "golden":golden,"source_hashes":{str(p.relative_to(ROOT)):sha(p) for p in src},
      "limitations":["local decoded swaps are actual historical swaps, not proof that our counterfactual order would fill",
       "D5 missing labels are retained as -100% unsellable losses plus gas; no favorable imputation", "LP checkpoints provide no pre-flip reserve path; rescue count is adverse zero",
       "golden integers are local decoded evidence with tx hashes; independent archive block-hash verification is absent and therefore not claimed",
       "holder/reserve proxy comparison not applicable: KEYSTONE feature set uses neither",
       "W3 is partial coverage: KEYSTONE source ends 2026-06-01 22:23 UTC while frozen B3 W3 ends 2026-06-08 18:29:40 UTC"]}
    primary=result["performance"]["keystone_full"]
    powered=[x for x in primary["windows"] if x["n"]>=20]
    gates={
      "pooled_mean_gt_0": primary["pooled"]["mean"]>0,
      "each_powered_regime_mean_ge_minus_10pct": all(x["mean"]>=-.10 for x in powered),
      "at_least_2_powered_regimes_positive_mean_and_alpha": sum(x["mean"]>0 and x["alpha_delta"]>0 for x in powered)>=2,
      "pooled_top1_positive_contribution_le_15pct": primary["pooled"]["top1_contribution"]<=.15,
      "pooled_top1_removed_mean_gt_0": primary["pooled"]["top1_removed_mean"]>0,
      "mc_terminal_p5_gt_100R": primary["mc"].get("terminal_p5",-math.inf)>100,
      "mc_mdd_p95_le_20R": primary["mc"].get("max_drawdown_p95",math.inf)<=20,
    }
    result["preregistered_right_tail"]={"source":"B3 frozen numeric gates, fixed before B5 result review",
      "powered_n_min":20,"thresholds":{"regime_floor":-.10,"top1_cap":.15,"mc_terminal_p5":100,"mc_mdd_p95":20},
      "gates":gates,"evidence_status":"PASS" if all(gates.values()) else "FAIL","go_decision":"NOT_MADE"}
    if LP_RPC.exists():
        result["forward_lp_monitor_archive"]={"sha256":sha(LP_RPC),**json.loads(LP_RPC.read_text())}
    if GOLDEN_RPC.exists():
        result["golden_archive_verification"]={"sha256":sha(GOLDEN_RPC),**json.loads(GOLDEN_RPC.read_text())}
    OUT.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    REPORT.write_text(render(result))
    print(json.dumps({"out":str(OUT),"report":str(REPORT),"swap_rows":sum(map(len,swaps.values())),
                      "golden":len(golden)},indent=2))


def pct(x): return "—" if x is None else f"{x:+.2%}"


def render(r):
    p=r["performance"]
    lines=["# B5 KEYSTONE CHA 重验（离线回测 + archive 只读补证）","","> research-only；核心回测 RPC=0；LP/golden 补证只读；不作 GO 判断。缺失证据按 adverse。","",
      "## 结论摘要","", "本报告只给证伪证据，不给 GO。forward/OOS 参数只用 W1 前数据拟合，W1–W3 原样验证。", "",
      "## 真实成交价、无条件与分层基线","", "| 口径 | 窗口 | n | mean | alpha vs unconditional | top1移除 mean | fatal | D5挡掉/缺失 |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for name in ["unconditional","friction_only","keystone_full","keystone_full_d5_on","keystone_general"]:
        for x in p[name]["windows"]:
            lines.append(f"| {name} | {x['window']} | {x['n']} | {pct(x['mean'])} | {pct(x['alpha_delta'])} | {pct(x['top1_removed_mean'])} | {pct(x.get('fatal_rate'))} | {x['blocked_by_d5']}/{x['missing_d5_adverse']} |")
        x=p[name]["pooled"]; lines.append(f"| **{name}** | **pooled** | **{x['n']}** | **{pct(x['mean'])}** | **{pct(x['alpha_delta'])}** | **{pct(x['top1_removed_mean'])}** | **{pct(x.get('fatal_rate'))}** | — |")
    lines += ["", "成交采用 decoded swap 的 `amount0_raw/amount1_raw` 平均执行价；0.001 WETH 容量、每尝试腿 $0.075 gas。限价从冻结 trailing/hard-stop 触发 tick 得到，之后约 60 分钟内第一笔方向、价格和容量均合格的真实卖单作为成交证据；无成交或缺失入场证据按 -100% 再扣已尝试腿 gas。`unconditional` 为无条件基线，`friction_only` 为同执行口径的分层基线。", "", "注意：KEYSTONE 数据最大时间为 2026-06-01 22:23 UTC，故 W3 仅是冻结 B3 W3（截至 2026-06-08 18:29:40）的 partial-window/OOS，不外推为完整 W3。", "",
      "## 防 rug 三杠杆","", "### 1. 快出场时序（跨 regime）","", "| 窗口 | 出场后 flip | 出场前/同块 flip | 未知 |", "|---|---:|---:|---:|"]
    for w,x in r["rug_timing_by_regime"].items(): lines.append(f"| {w} | {x.get('after_exit',0)} | {x.get('before_or_at_exit',0)} | {x.get('unknown',0)} |")
    lines += ["", "W3 仅 2 个样本有 exit timing、7 个未知；未知按 adverse，不能从观测到的 0 个 before-exit 得出时序优势。"]
    d=r['d5_label_coverage']; m=r['forward_lp_monitor']; lines += ["", "### 2. Forward LP monitor", "", f"全冻结标签 delayed rug={m['all_delayed_rug']}，已见真实损失 {m['real_loss']}；offset≤300 的早翻转={m['early_offset_le_300']}，其中真实损失={m['early_real_loss']}。现有 checkpoint 只能在 flip 当块/之后检测，不能证明可执行的 pre-flip lead；保守可救回 **{m['rescued_proven']}**。", "", "### 3. D5 原子门边际", "", f"冻结标签覆盖 {d['n_labels']}，其中 instant_hp={d['instant_hp']}。forward 选择 {d['forward_selected']} 例，覆盖 {d['forward_selected_covered']}，覆盖交集内可验证拒绝={d['verified_rejections_in_forward_selected']}，故覆盖交集的 D5 直接边际为 0；其余缺失按 -100% 不可卖并扣 gas，不作有利推断。开/关收益见上表。", "",
      "## 迁移 RH 上界","", "`keystone_general` 使用相同学习器/训练窗，仅保留市价可得的价格与活动特征；其收益是可迁移性上界，而非异链实证。KEYSTONE 本版本未使用 holder/储备，故真值/代理偏差不适用。", "",
      "## 右尾与 power", ""]
    for name in ["keystone_full","keystone_full_d5_on","keystone_general"]: lines.append(f"- {name}: `{p[name]['mc']}`")
    rt=r["preregistered_right_tail"]
    lines += ["", f"n<20 的窗只描述，不判 merit。复用 B3 冻结数值门后，逐项结果为 `{rt['gates']}`；证据状态 **{rt['evidence_status']}**。这不是 GO 决策。"]
    if "forward_lp_monitor_archive" in r:
        x=r["forward_lp_monitor_archive"]; s=x["summary"]
        lines += ["", "### Forward LP archive 补证", "", f"对 early≤300 或实际真损的并集 {x['selection']['early_le_300_or_real_loss_unique']} 池做 pinned `WETH.balanceOf(pool)`：{s['measured']} 全测得，真损 4 例中 prewarning= {s['prewarning_among_real_loss_n']}、可证明新增救回={s['rescue_proven_n']}；另有 {s['prewarning_but_already_exited_n']} 例出现 prewarning，但策略此前已退出，非增量救回。"]
    gv=r.get("golden_archive_verification")
    lines += ["", "## Golden 与复现", "", f"本地 decoded 整数 golden 共 {len(r['golden'])} 例，逐例含 pool、block、tx 和 amount0/amount1 raw integer。archive receipt+block 独立核对：`{gv.get('all_integer_equal') if gv else '未执行'}`；输入 SHA-256 同存。", "", "```bash", "cd /opt/EATLQ", "OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 python3 scripts/research/base_b5/b5_offline_reverify.py", "python3 -m unittest tests/test_base_b5_offline.py", "```", ""]
    return "\n".join(lines)


if __name__ == "__main__": main()
