#!/usr/bin/env python3
"""Monthly share of MIN_PRE pools with entry depth >= 3 ETH (Oct25-Jun25)."""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keystone_live as K  # noqa
import live_config_rebacktest as L  # noqa
import risk_rebacktest as R  # noqa

OUT_DIR = "/opt/EATLQ/mvp/audits/aggressive_live_rebacktest_20260625"
COLS = [
    "pool", "orient", "alt", "t0", "ns_pre", "buyers_pre", "senders_pre", "first_tick",
    "last_tick_pre", "min_tick_pre", "max_tick_pre", "liq_first", "liq_last_pre",
    "liq_min_pre", "ns_h1", "ns_h2", "last_tick_h1", "entry_block", "ns_fwd",
    "min_tick_fwd", "max_tick_fwd", "last_tick_fwd",
]


def main():
    raw = pd.read_csv(os.path.join(HERE, "_pools.tsv"), sep="\t", names=COLS)
    raw["t0"] = pd.to_datetime(raw["t0"])
    ewd = json.load(open(os.path.join(HERE, "_entry_weth_644.json")))
    raw["entry_weth"] = raw.pool.map(lambda p: (ewd.get(p) or float("nan")) / 1e18)
    raw["depth_ge3"] = raw.entry_weth >= 3.0

    clf, X, _, _ = R.fit_model(raw)
    raw["p"] = clf.predict_proba(X)[:, 1]
    raw["p_le027"] = raw.p <= 0.27

    rows = []
    for m in sorted(raw.t0.dt.strftime("%Y-%m").unique()):
        if m >= "2026-06":
            continue  # old tsv cutoff; use RPC supplement
        sub = raw[raw.t0.dt.strftime("%Y-%m") == m]
        n = len(sub)
        d3 = int(sub.depth_ge3.sum())
        d3p = int((sub.depth_ge3 & sub.p_le027).sum())
        rows.append({
            "month": m,
            "min_pre_n": n,
            "depth_ge3_n": d3,
            "depth_ge3_pct": round(100 * d3 / n, 1) if n else 0.0,
            "depth3_and_p_n": d3p,
            "depth3_and_p_pct": round(100 * d3p / n, 1) if n else 0.0,
            "mean_depth_eth": round(float(sub.entry_weth.mean()), 2),
            "median_depth_eth": round(float(sub.entry_weth.median()), 3),
            "source": "pools.tsv + entry_weth cache",
        })

    jun_path = os.path.join(OUT_DIR, "june2026_rpc_supplement.json")
    jdf = pd.DataFrame(json.load(open(jun_path))["pools"])
    jdf["depth_ge3"] = jdf.entry_weth >= 3.0
    jdf["p_le027"] = jdf.p_fatal <= 0.27
    rows.append({
        "month": "2026-06",
        "min_pre_n": len(jdf),
        "depth_ge3_n": int(jdf.depth_ge3.sum()),
        "depth_ge3_pct": round(100 * jdf.depth_ge3.mean(), 1),
        "depth3_and_p_n": int((jdf.depth_ge3 & jdf.p_le027).sum()),
        "depth3_and_p_pct": round(100 * (jdf.depth_ge3 & jdf.p_le027).mean(), 1),
        "mean_depth_eth": round(float(jdf.entry_weth.mean()), 2),
        "median_depth_eth": round(float(jdf.entry_weth.median()), 3),
        "aggressive_pass_n": int(jdf.aggressive_live.sum()),
        "source": "june2026_rpc_supplement (6/1-6/25, archive RPC depth)",
    })

    df = pd.DataFrame(rows)
    out_json = os.path.join(OUT_DIR, "depth_ge3_share_monthly.json")
    df.to_json(out_json, orient="records", indent=2)

    md = os.path.join(OUT_DIR, "depth_ge3_share_monthly_cn.md")
    lines = [
        "# MIN_PRE 池：深池（≥3 ETH）占比逐月",
        "",
        "> 固定 MIN_PRE≥5 候选宇宙，看 **depth≥3 ETH** 占比是否下降。",
        "> 六月用 RPC 补全（6/1–6/25）；其余月来自 `_pools.tsv` + `_entry_weth_644.json`。",
        "",
        "| 月 | MIN_PRE n | depth≥3 n | **depth≥3 %** | depth≥3 且 p≤0.27 | 均值深度 | 中位深度 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['month']} | {r['min_pre_n']} | {r['depth_ge3_n']} | **{r['depth_ge3_pct']}%** | "
            f"{r['depth3_and_p_n']} ({r['depth3_and_p_pct']}%) | {r['mean_depth_eth']} | {r['median_depth_eth']} |"
        )
    lines += [
        "",
        "## 解读",
        "",
        "- **四月峰值**：depth≥3 占 **62.6%**（91 池中 57 个）→ 与激进闸成交 57 笔对齐。",
        "- **五月断崖**：占比 **8.5%**（141 池中仅 12 个），中位深度 **0.051 ETH** → 过闸 12 笔。",
        "- **六月（25 天）**：占比 **24.2%**（62 池中 15 个），介于四五月之间；激进全过 **6 笔**。",
        "- MIN_PRE 总量并未消失（五月 141、六月 25 天 62），**变的是深池比例**，不是池子总数。",
        "",
        f"JSON：`depth_ge3_share_monthly.json`",
    ]
    open(md, "w").write("\n".join(lines) + "\n")
    print(open(md).read())
    print(f"wrote {out_json}\n      {md}")


if __name__ == "__main__":
    main()
