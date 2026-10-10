#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 30-day Pump 原始分区构建 features + labels parquet。

Labels: ret, MFE, MAE, hit100, hit300, hit1000, fatal25, fatal50, graduation
Features: snapshots T+5..300, early buyer stats, dev/creator history
Optional pump.fun API enrichment (零 Solana RPC).

用法:
  python3 _pump_monthly_label_build.py
  python3 _pump_monthly_label_build.py --no-api
  python3 _pump_monthly_label_build.py --report-only
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from _pump_monthly_lib import (
    COST_RT,
    DATA_ROOT,
    DATE_HI,
    DATE_LO,
    ENTRY_S,
    EXIT_S,
    FATAL25,
    FATAL50,
    GRAD_VSOL,
    META_ROOT,
    OUT_DIR,
    RAW_ROOT,
    SNAPSHOT_T,
    iter_days,
    load_json,
    load_jsonl,
    net_ret,
    save_json,
)

FEATURES_OUT = OUT_DIR / "pump_30d_features.parquet"
LABELS_OUT = OUT_DIR / "pump_30d_labels.parquet"
REPORT_OUT = OUT_DIR / "_pump_monthly_coverage_report.md"
API_CACHE = META_ROOT / "pump_api_cache.json"

PUMP_API = "https://frontend-api-v3.pump.fun/coins/{}"


def load_all_tokens(days: list[str] | None = None) -> list[dict]:
    days = days or iter_days()
    out: list[dict] = []
    for day in days:
        tdir = RAW_ROOT / day / "tokens"
        if not tdir.exists():
            continue
        for p in sorted(tdir.glob("*.json")):
            try:
                out.append(load_json(p))
            except Exception:
                pass
    return out


def fetch_api_meta(mint: str, cache: dict, sleep_s: float = 0.10) -> dict:
    if mint in cache:
        return cache[mint]
    meta: dict = {}
    try:
        req = urllib.request.Request(PUMP_API.format(mint), headers={"Accept": "application/json", "User-Agent": "pump30d/1"})
        d = json.loads(urllib.request.urlopen(req, timeout=12).read())
        meta = {
            "ath_mc": d.get("ath_market_cap"),
            "graduated_api": bool(d.get("complete")),
            "usd_market_cap": d.get("usd_market_cap"),
            "time_to_ath_s": (
                round((d["ath_market_cap_timestamp"] - d["created_timestamp"]) / 1000.0, 1)
                if d.get("ath_market_cap_timestamp") and d.get("created_timestamp")
                else None
            ),
        }
    except Exception:
        meta = {"ath_mc": None, "graduated_api": None, "usd_market_cap": None, "time_to_ath_s": None}
    cache[mint] = meta
    time.sleep(sleep_s)
    return meta


def path_prices(token: dict) -> list[tuple[float, float]]:
    pts = []
    for tr in token.get("path_trades") or []:
        if tr.get("price") and tr.get("t_rel") is not None:
            pts.append((float(tr["t_rel"]), float(tr["price"])))
    pts.sort()
    return pts


def label_row(token: dict, api: dict, dev_hist: dict) -> dict:
    snaps = token.get("snapshots") or {}
    p15 = (snaps.get("15") or {}).get("price")
    p90 = (snaps.get("90") or {}).get("price")
    ret = net_ret(p15, p90, COST_RT)

    pts = path_prices(token)
    entry_pts = [(t, p) for t, p in pts if t >= ENTRY_S and p > 0]
    rets = [(p / p15 - 1.0) if p15 and p15 > 0 else None for _, p in entry_pts]
    rets = [r for r in rets if r is not None]
    mfe = max(rets) if rets else None
    mae = min(rets) if rets else None
    if mfe is not None:
        mfe = mfe * (1 - COST_RT) - COST_RT / 2
    if mae is not None:
        mae = mae * (1 - COST_RT) - COST_RT / 2

    gross_max = max(rets) if rets else None
    hit100 = int(gross_max is not None and gross_max >= 1.0)
    hit300 = int(gross_max is not None and gross_max >= 3.0)
    hit1000 = int(gross_max is not None and gross_max >= 10.0)
    fatal25 = int(ret is not None and ret <= FATAL25)
    fatal50 = int(ret is not None and ret <= FATAL50)
    graduated = int(
        bool(token.get("graduated_on_curve"))
        or bool(api.get("graduated_api"))
        or float(token.get("max_vSol") or 0) >= GRAD_VSOL
    )

    creator = token.get("creator")
    prior = dev_hist.get(creator, []) if creator else []
    prior_before = [x for x in prior if x["created_ms"] < token.get("created_ms", 0)]

    return {
        "mint": token["mint"],
        "day": token.get("day"),
        "creator": creator,
        "created_ms": token.get("created_ms"),
        "ret_t15_t90": ret,
        "mfe_t15_t300": mfe,
        "mae_t15_t300": mae,
        "hit100": hit100,
        "hit300": hit300,
        "hit1000": hit1000,
        "fatal25": fatal25,
        "fatal50": fatal50,
        "graduation": graduated,
        "max_vSol": token.get("max_vSol"),
        "graduated_api": api.get("graduated_api"),
        "ath_mc": api.get("ath_mc"),
        "n_trades_5m": token.get("n_trades_5m"),
        "n_early_buyers": len(token.get("early_buyers") or []),
        "dev_past_created_count": len(prior_before),
    }


def feature_row(token: dict, dev_hist: dict) -> dict:
    snaps = token.get("snapshots") or {}
    row: dict = {
        "mint": token["mint"],
        "day": token.get("day"),
        "creator": token.get("creator"),
        "created_ms": token.get("created_ms"),
        "bonding_curve": token.get("bonding_curve"),
        "n_trades_5m": token.get("n_trades_5m"),
        "n_trades_path": token.get("n_trades_path"),
        "max_vSol": token.get("max_vSol"),
        "reached_birth": token.get("reached_birth"),
    }
    for t in SNAPSHOT_T:
        s = snaps.get(str(t)) or {}
        row[f"price_t{t}"] = s.get("price")
        row[f"vSol_t{t}"] = s.get("vSol")
        row[f"vTok_t{t}"] = s.get("vTok")
    eb = token.get("early_buyers") or []
    row["early_buyer_count"] = len(eb)
    row["early_buyer_sol_sum"] = sum(x.get("sol_sum") or 0 for x in eb)
    row["early_buyers_json"] = json.dumps(eb[:50])
    creator = token.get("creator")
    prior = dev_hist.get(creator, []) if creator else []
    prior_before = [x for x in prior if x["created_ms"] < token.get("created_ms", 0)]
    row["dev_past_created_count"] = len(prior_before)
    row["dev_prior_mints_json"] = json.dumps([x["mint"] for x in prior_before[:20]])
    return row


def build_dev_history(tokens: list[dict]) -> dict[str, list[dict]]:
    hist: dict[str, list] = defaultdict(list)
    for t in tokens:
        c = t.get("creator")
        if not c:
            continue
        hist[c].append({"mint": t["mint"], "created_ms": t.get("created_ms"), "day": t.get("day")})
    for c in hist:
        hist[c].sort(key=lambda x: x["created_ms"] or 0)
    return dict(hist)


def coverage_from_tokens(tokens: list[dict], days: list[str]) -> dict:
    by_day: dict[str, dict] = {}
    for day in days:
        by_day[day] = {"day": day, "tokens": 0, "with_snapshots": 0, "with_trades_5m": 0, "with_price_t15": 0}
    for t in tokens:
        day = t.get("day")
        if day not in by_day:
            continue
        by_day[day]["tokens"] += 1
        if t.get("snapshots"):
            by_day[day]["with_snapshots"] += 1
        if (t.get("n_trades_5m") or 0) > 0:
            by_day[day]["with_trades_5m"] += 1
        if (t.get("snapshots") or {}).get("15", {}) and (t["snapshots"]["15"] or {}).get("price"):
            by_day[day]["with_price_t15"] += 1
    return {
        "n_tokens": len(tokens),
        "by_day": [by_day[d] for d in days if d in by_day],
        "days_with_data": sum(1 for d in by_day.values() if d["tokens"] > 0),
        "total_days": len(days),
    }


def write_report(cov: dict, labels_df: pd.DataFrame, features_df: pd.DataFrame) -> None:
    lines = [
        "# Pump 30-Day 数据采集覆盖报告",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"窗口：**{DATE_LO} → {DATE_HI}**（30 UTC 天）",
        "",
        "## 总览",
        "",
        f"- 已采集 token 数：**{cov['n_tokens']}**",
        f"- 有数据 UTC 日：**{cov['days_with_data']} / {cov['total_days']}**",
        f"- Features parquet：`pump_30d_features.parquet`（{len(features_df)} rows）",
        f"- Labels parquet：`pump_30d_labels.parquet`（{len(labels_df)} rows）",
        "",
        "## 按日覆盖",
        "",
        "| day | tokens | snapshots | trades_5m | price_t15 |",
        "|-----|--------|-----------|-----------|-----------|",
    ]
    for d in cov["by_day"]:
        if d["tokens"] == 0 and d == cov["by_day"][-1]:
            continue
        lines.append(
            f"| {d['day']} | {d['tokens']} | {d['with_snapshots']} | {d['with_trades_5m']} | {d['with_price_t15']} |"
        )

    if len(labels_df):
        lines.extend(
            [
                "",
                "## Labels 汇总（T+15→T+90，cost 3%）",
                "",
                f"- median ret：{labels_df['ret_t15_t90'].median():+.1%}" if labels_df["ret_t15_t90"].notna().any() else "- median ret：n/a",
                f"- hit100：{labels_df['hit100'].mean():.1%}",
                f"- hit300：{labels_df['hit300'].mean():.1%}",
                f"- hit1000：{labels_df['hit1000'].mean():.1%}",
                f"- fatal25：{labels_df['fatal25'].mean():.1%}",
                f"- graduation：{labels_df['graduation'].mean():.1%}",
            ]
        )

    rpc_path = META_ROOT / "rpc_cost_report.json"
    if rpc_path.exists():
        rpc = load_json(rpc_path)
        lines.extend(
            [
                "",
                "## RPC Cost",
                "",
                f"- 网络 RPC calls（累计）：**{rpc.get('grand_total_calls', 0):,}**",
                f"- Cache hits：**{rpc.get('grand_total_cache_hits', 0):,}**",
                "",
                "详见 `data/pump_30d/meta/rpc_cost_report.json`",
            ]
        )

    lines.extend(
        [
            "",
            "## 产物路径",
            "",
            "```",
            f"data/pump_30d/raw/{{YYYY-MM-DD}}/creates.jsonl",
            f"data/pump_30d/raw/{{YYYY-MM-DD}}/tokens/{{mint}}.json",
            f"data/pump_30d/rpc_cache.sqlite",
            f"pump_30d_features.parquet",
            f"pump_30d_labels.parquet",
            "```",
            "",
            "## 下一步",
            "",
            "1. 跑完 30 天 collect：`python3 _pump_monthly_data_collect.py --all`",
            "2. 重建 labels：`python3 _pump_monthly_label_build.py`",
            "3. 在此报告确认覆盖 ≥30 天且 daily token 量稳定后再做策略筛选",
        ]
    )
    REPORT_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-api", action="store_true", help="skip pump.fun API enrichment")
    ap.add_argument("--report-only", action="store_true", help="regenerate coverage report from existing parquets")
    ap.add_argument("--day", type=str, default="", help="limit to one day")
    args = ap.parse_args()

    days = [args.day] if args.day else iter_days()
    tokens = load_all_tokens(days)

    if args.report_only and FEATURES_OUT.exists() and LABELS_OUT.exists():
        feat_df = pd.read_parquet(FEATURES_OUT)
        lab_df = pd.read_parquet(LABELS_OUT)
        cov = coverage_from_tokens(tokens, days)
        write_report(cov, lab_df, feat_df)
        print(f"Report -> {REPORT_OUT}")
        return

    if not tokens:
        print("No token files found. Run _pump_monthly_data_collect.py first.")
        cov = coverage_from_tokens([], days)
        write_report(cov, pd.DataFrame(), pd.DataFrame())
        return

    dev_hist = build_dev_history(tokens)
    api_cache: dict = {}
    if API_CACHE.exists():
        api_cache = load_json(API_CACHE)

    label_rows = []
    feature_rows = []
    for i, tok in enumerate(tokens):
        api = {} if args.no_api else fetch_api_meta(tok["mint"], api_cache)
        label_rows.append(label_row(tok, api, dev_hist))
        feature_rows.append(feature_row(tok, dev_hist))
        if not args.no_api and (i + 1) % 100 == 0:
            save_json(API_CACHE, api_cache)

    if not args.no_api:
        save_json(API_CACHE, api_cache)

    labels_df = pd.DataFrame(label_rows)
    features_df = pd.DataFrame(feature_rows)
    labels_df.to_parquet(LABELS_OUT, index=False)
    features_df.to_parquet(FEATURES_OUT, index=False)

    cov = coverage_from_tokens(tokens, days)
    save_json(META_ROOT / "label_build_summary.json", cov)
    save_json(META_ROOT / "dev_history.json", dev_hist)
    eb_all = {t["mint"]: t.get("early_buyers") or [] for t in tokens}
    save_json(META_ROOT / "early_buyer_history.json", eb_all)
    write_report(cov, labels_df, features_df)

    print(f"Features -> {FEATURES_OUT} ({len(features_df)} rows)")
    print(f"Labels   -> {LABELS_OUT} ({len(labels_df)} rows)")
    print(f"Report   -> {REPORT_OUT}")


if __name__ == "__main__":
    main()
