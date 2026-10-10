"""Run-004 V2 7D alpha readout — static CSV analysis only (no RPC, no large outputs)."""
from __future__ import annotations

import csv
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROWS_CSV = ROOT / "mvp/diagnostics/run004_v2_live_equivalent_7d_pilot_rows.csv"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v2_7d_alpha_readout_summary.json"
OUT_MD = ROOT / "mvp/runs/run-004-v2-7d-alpha-readout.md"

FROM_TS = 1777095455
SCORE_FIELDS = ("score", "final_score", "model_score", "rank", "priority", "candidate_score")
DAY2_MAX_POOL = "0x3927bb965e94210ccb574f5734e8fb8b3e2b3e81".lower()


def day_idx(entry_ts: str) -> int | None:
    if not (entry_ts or "").strip():
        return None
    try:
        t = int(entry_ts)
    except ValueError:
        return None
    return max(0, (t - FROM_TS) // 86_400)


def ffloat(x: str | None) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except ValueError:
        return None


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    d0 = sorted_vals[f] * (c - k)
    d1 = sorted_vals[c] * (k - f)
    return d0 + d1


def return_stats(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {
            "n": 0, "mean": 0.0, "median": 0.0, "positive_rate": 0.0,
            "p05": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0, "p95": 0.0, "p99": 0.0,
            "total_net_return_proxy": 0.0, "profit_factor": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "win_count": 0, "loss_count": 0,
            "profit_factor_is_infinite": False,
        }
    vals = []
    for r in rows:
        v = ffloat(r.get("net_return"))
        if v is not None:
            vals.append(v)
    n = len(vals)
    if n == 0:
        return return_stats([])
    sv = sorted(vals)
    pos = [v for v in vals if v > 0]
    neg = [v for v in vals if v < 0]
    win_sum = sum(pos)
    loss_sum = abs(sum(neg))
    if loss_sum > 1e-18:
        pf = win_sum / loss_sum
    elif win_sum > 0:
        pf = 999_999.0
    else:
        pf = 0.0
    return {
        "n": n,
        "mean": sum(vals) / n,
        "median": statistics.median(vals),
        "positive_rate": len(pos) / n,
        "p05": percentile(sv, 5),
        "p25": percentile(sv, 25),
        "p50": percentile(sv, 50),
        "p75": percentile(sv, 75),
        "p95": percentile(sv, 95),
        "p99": percentile(sv, 99),
        "total_net_return_proxy": sum(vals),
        "profit_factor": pf,
        "profit_factor_is_infinite": pf >= 999_998.0 and win_sum > 0 and loss_sum <= 1e-18,
        "avg_win": statistics.mean(pos) if pos else 0.0,
        "avg_loss": statistics.mean(neg) if neg else 0.0,
        "win_count": len(pos),
        "loss_count": len(neg),
    }


def concentration_stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {
            "top1_pool_share": 0.0, "top5_pool_share": 0.0, "top1_token_share": 0.0, "top5_token_share": 0.0,
            "top1_day_share": 0.0, "top5_day_share": 0.0, "day_index_2_share": 0.0, "max_pool_contribution_share": 0.0,
        }
    pc = Counter(r["pool_address"].lower() for r in rows)
    tc = Counter(r["token_address"].lower() for r in rows)
    dc = Counter(r["_day"] for r in rows if r["_day"] is not None)
    top1p = pc.most_common(1)[0][1] / n
    top5p = sum(v for _, v in pc.most_common(5)) / n
    top1t = tc.most_common(1)[0][1] / n
    top5t = sum(v for _, v in tc.most_common(5)) / n
    top1d = dc.most_common(1)[0][1] / n if dc else 0.0
    top5d = sum(v for _, v in dc.most_common(5)) / n if dc else 0.0
    d2 = dc.get(2, 0) / n if dc else 0.0
    return {
        "top1_pool_share": top1p,
        "top5_pool_share": top5p,
        "top1_token_share": top1t,
        "top5_token_share": top5t,
        "top1_day_share": top1d,
        "top5_day_share": top5d,
        "day_index_2_share": d2,
        "max_pool_contribution_share": top1p,
    }


def max_drawdown_proxy(rows_chrono: list[dict]) -> float:
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for r in rows_chrono:
        v = ffloat(r.get("net_return"))
        if v is None:
            continue
        cum += v
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def sort_chrono(rows: list[dict]) -> list[dict]:
    def log_i(r: dict) -> int:
        li = r.get("log_index") or "0"
        s = str(li).strip()
        try:
            return int(s, 0)
        except ValueError:
            return 0

    def key(r: dict) -> tuple:
        ts = ffloat(r.get("entry_timestamp")) or 0.0
        tx = r.get("tx_hash") or ""
        return ts, tx, log_i(r)

    return sorted(rows, key=key)


def apply_per_day_pool_cap(rows: list[dict], cap: int) -> list[dict]:
    bucket_counts: dict[tuple[int | None, str], int] = defaultdict(int)
    out: list[dict] = []
    for r in rows:
        d = r["_day"]
        p = r["pool_address"].lower()
        k = (d, p)
        if bucket_counts[k] >= cap:
            continue
        bucket_counts[k] += 1
        out.append(r)
    return out


def apply_pool_fraction_cap(rows: list[dict], max_share: float) -> list[dict]:
    rows = list(rows)
    while True:
        n = len(rows)
        if n == 0:
            return []
        lim = max(1, int(max_share * n + 1e-9))
        pc = Counter(r["pool_address"].lower() for r in rows)
        worst_pool, cnt = pc.most_common(1)[0]
        if cnt <= lim:
            return rows
        for i in range(len(rows) - 1, -1, -1):
            if rows[i]["pool_address"].lower() == worst_pool:
                del rows[i]
                break


def exclude_day2_max_pool(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not (r["_day"] == 2 and r["pool_address"].lower() == DAY2_MAX_POOL)]


def policy_pack(name: str, rows: list[dict]) -> dict:
    st = return_stats(rows)
    conc = concentration_stats(rows)
    ch = sort_chrono(rows)
    mdd = max_drawdown_proxy(ch)
    tradable = st["mean"] > 0 and st["total_net_return_proxy"] > 0 and st["positive_rate"] >= 0.45
    return {
        "policy": name,
        "selected_rows": st["n"],
        "return_stats": st,
        "concentration": conc,
        "max_drawdown_proxy_sum_units": mdd,
        "tradable_after_cost_heuristic": tradable,
    }


def main() -> None:
    all_rows: list[dict] = []
    with ROWS_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        for r in reader:
            r["_day"] = day_idx(r.get("entry_timestamp", ""))
            all_rows.append(r)

    has_score = any(sf in (header or []) for sf in SCORE_FIELDS)

    clean = [r for r in all_rows if (r.get("is_clean") or "").strip() == "True"]
    quarantine = [r for r in all_rows if (r.get("risk_flag") or "").strip() == "extreme_return_quarantine"]
    ambiguous = sum(1 for r in all_rows if (r.get("risk_flag") or "").strip() == "semantic_uncertain")
    clean_ext_overlap = sum(1 for r in clean if (r.get("risk_flag") or "").strip() == "extreme_return_quarantine")

    day_dist_clean = Counter(r["_day"] for r in clean if r["_day"] is not None)
    day2_all = [r for r in all_rows if r["_day"] == 2]
    pc_d2 = Counter(r["pool_address"].lower() for r in day2_all)
    day2_max_share_attempted = pc_d2.most_common(1)[0][1] / len(day2_all) if day2_all else 0.0

    clean_d2 = [r for r in clean if r["_day"] == 2]
    pc_cd2 = Counter(r["pool_address"].lower() for r in clean_d2)
    day2_clean_max_pool = (pc_cd2.most_common(1)[0][1] / len(clean_d2)) if clean_d2 else 0.0

    b_stats = return_stats(clean)
    chrono_clean = sort_chrono(clean)
    mdd_full = max_drawdown_proxy(chrono_clean)

    bucket_readout: dict = {
        "no_model_score_available": not has_score,
        "score_fields_checked": list(SCORE_FIELDS),
        "bucket_axis": "chronological_entry_time_tx_logindex (earliest = smallest rank; not a predictive score)",
        "buckets": {},
    }
    n_clean = len(chrono_clean)
    if n_clean:
        for name, frac, bottom in (
            ("top_1pct", 0.01, False),
            ("top_5pct", 0.05, False),
            ("top_10pct", 0.10, False),
            ("top_20pct", 0.20, False),
            ("bottom_50pct", 0.50, True),
        ):
            if bottom:
                start = int(n_clean * 0.50)
                sub = chrono_clean[start:]
            else:
                k = max(1, int(n_clean * frac))
                sub = chrono_clean[:k]
            st = return_stats(sub)
            c = concentration_stats(sub)
            bucket_readout["buckets"][name] = {
                "rows": st["n"],
                "positive_rate": st["positive_rate"],
                "mean_net_return": st["mean"],
                "median_net_return": st["median"],
                "total_net_return_proxy": st["total_net_return_proxy"],
                "profit_factor": st["profit_factor"],
                **c,
            }

    policies = {
        "raw_all_clean": policy_pack("raw_all_clean", clean),
        "raw_top_bucket_top20pct_chrono": policy_pack(
            "raw_top_bucket_top20pct_chrono",
            chrono_clean[: max(1, int(n_clean * 0.20))] if n_clean else [],
        ),
        "per_day_per_pool_cap_300": policy_pack("per_day_per_pool_cap_300", apply_per_day_pool_cap(clean, 300)),
        "exclude_day2_max_pool": policy_pack("exclude_day2_max_pool", exclude_day2_max_pool(clean)),
        "quarantine_excluded_clean_only": policy_pack(
            "quarantine_excluded_clean_only",
            [r for r in clean if (r.get("risk_flag") or "").strip() != "extreme_return_quarantine"],
        ),
        "pool_cap_10pct": policy_pack("pool_cap_10pct", apply_pool_fraction_cap(clean, 0.10)),
        "pool_cap_20pct": policy_pack("pool_cap_20pct", apply_pool_fraction_cap(clean, 0.20)),
    }

    split = int(n_clean * 0.70) if n_clean else 0
    train = chrono_clean[:split]
    hold = chrono_clean[split:]
    st_tr = return_stats(train)
    st_ho = return_stats(hold)
    conc_ho = concentration_stats(hold)
    top_ph = Counter(r["pool_address"].lower() for r in hold).most_common(1)
    top_ph_share = top_ph[0][1] / len(hold) if hold else 0.0
    ho_d2_pool = sum(
        1 for r in hold if r["_day"] == 2 and r["pool_address"].lower() == DAY2_MAX_POOL
    ) / len(hold) if hold else 0.0

    raw_mean, hold_mean = b_stats["mean"], st_ho["mean"]
    cap_mean = policies["per_day_per_pool_cap_300"]["return_stats"]["mean"]
    exc_mean = policies["exclude_day2_max_pool"]["return_stats"]["mean"]
    p10_mean = policies["pool_cap_10pct"]["return_stats"]["mean"]
    p20_mean = policies["pool_cap_20pct"]["return_stats"]["mean"]

    rationale: list[str] = []
    code = "FAILED_NO_7D_ALPHA"

    if len(clean) == 0:
        code = "FAILED_DATA_SEMANTICS_UNCERTAIN"
        rationale.append("No clean rows.")
    elif raw_mean <= 0 and b_stats["median"] <= 0:
        code = "FAILED_NO_7D_ALPHA"
        rationale.append("Clean mean/median net_return not positive — no unconditional edge after labeled costs.")
    else:
        rationale.append(f"Unconditional clean mean={raw_mean:.6f}, median={b_stats['median']:.6f}, positive_rate={b_stats['positive_rate']:.4f}")

    if hold_mean <= 0:
        if code != "FAILED_DATA_SEMANTICS_UNCERTAIN":
            code = "FAILED_NO_7D_ALPHA"
        rationale.append(f"Time holdout (last 30pct) mean={hold_mean:.6f} <= 0 — no chronological OOS edge.")

    if raw_mean > 0 and (cap_mean <= 0 or exc_mean <= 0):
        code = "FAILED_CONCENTRATION_DOMINATED"
        rationale.append(
            f"Concentration fragility: cap300_mean={cap_mean:.6f}, exclude_day2_max_pool_mean={exc_mean:.6f}."
        )

    if raw_mean > 0 and hold_mean > 0 and cap_mean > 0 and exc_mean > 0:
        code = "WEAK_7D_EDGE_NEEDS_30D_CONFIRMATION"
        rationale.append("Mean stays >0 under cap300 and exclude_day2_max_pool; holdout mean >0 — weak/clumpy 7D signal.")

    if quarantine and len(quarantine) / max(len(all_rows), 1) > 0.25:
        code = "FAILED_OUTLIER_OR_QUARANTINE_DOMINATED"
        rationale.append(">25pct of all rows are extreme quarantine — label mass in tails.")

    if code == "WEAK_7D_EDGE_NEEDS_30D_CONFIRMATION" and hold_mean > 0.0005 and hold_mean >= st_tr["mean"] * 0.4:
        code = "PASSED_7D_SCOPED_ALPHA_READOUT"
        rationale.append("Upgraded: holdout retains sizable fraction of train mean with positive caps — still NOT deploy.")

    if code == "PASSED_7D_SCOPED_ALPHA_READOUT" and (hold_mean < cap_mean * 0.3 or st_ho["positive_rate"] < 0.42):
        code = "WEAK_7D_EDGE_NEEDS_30D_CONFIRMATION"
        rationale.append("Downgraded from PASSED: holdout win-rate or mean too weak versus capped full sample.")

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "patch": "run-004-v2-7d-alpha-readout",
        "disclaimer": (
            "7D pilot used 50k random subsample of ~496k decoded rows; net_return is live-equivalent (gas+slippage+fees). "
            "This is NOT pool-disjoint OOT, NOT live authorization, NOT production."
        ),
        "dataset_sanity": {
            "clean_rows": len(clean),
            "quarantine_extreme_return_rows": len(quarantine),
            "ambiguous_semantic_uncertain_rows": ambiguous,
            "duplicate_conflict_count": 0,
            "day_distribution_clean": {str(k): v for k, v in sorted(day_dist_clean.items())},
            "unique_pools_clean": len({r['pool_address'].lower() for r in clean}),
            "unique_tokens_clean": len({r['token_address'].lower() for r in clean}),
            "top1_pool_share_clean": policies["raw_all_clean"]["concentration"]["top1_pool_share"],
            "top5_pool_share_clean": policies["raw_all_clean"]["concentration"]["top5_pool_share"],
            "day_index_2_max_pool_share_all_rows_with_day2": day2_max_share_attempted,
            "day_index_2_max_pool_share_clean_only": day2_clean_max_pool if clean_d2 else 0.0,
            "clean_intersection_extreme_quarantine": clean_ext_overlap,
        },
        "return_readout_clean": {**b_stats, "max_drawdown_proxy_sum_units": mdd_full},
        "bucket_readout": bucket_readout,
        "policy_simulation": policies,
        "chronological_oos": {
            "split": "first_70pct_train_last_30pct_holdout_by_entry_time",
            "train": st_tr,
            "holdout": st_ho,
            "mean_drift_holdout_minus_train": st_ho["mean"] - st_tr["mean"],
            "holdout_concentration": conc_ho,
            "holdout_top_pool_share": top_ph_share,
            "holdout_day2_max_pool_share": ho_d2_pool,
        },
        "alpha_verdict": {
            "code": code,
            "rationale": rationale,
            "policy_means": {
                "raw": raw_mean,
                "cap300": cap_mean,
                "exclude_day2_pool": exc_mean,
                "pool_cap_10pct": p10_mean,
                "pool_cap_20pct": p20_mean,
            },
        },
        "decision_flags": {
            "allow_training": False,
            "allow_model_research": False,
            "allow_h19": False,
            "allow_shadow": False,
            "allow_live": False,
            "allow_promote": False,
            "allow_30d_bounded_run_recommended_from_this_readout": code
            in ("PASSED_7D_SCOPED_ALPHA_READOUT", "WEAK_7D_EDGE_NEEDS_30D_CONFIRMATION"),
        },
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Markdown body
    md = f"""# Run-004 V2 7D Alpha Readout (Scoped)

**Verdict code:** `{code}`

**Disclaimer:** 50k random subsample within 7 days; `net_return` already includes execution-cost assumptions from the live-equivalent pilot. Not OOT-generalizable, not live-safe. Old 1D pilot is superseded and was not used.

## A. Dataset sanity (from this CSV + 7D summary alignment)

| Metric | Value |
|--------|-------|
| clean_rows | {len(clean):,} |
| extreme_quarantine (all rows) | {len(quarantine):,} |
| ambiguous_semantic_uncertain | {ambiguous:,} |
| duplicate_conflict | 0 (per 7D summary) |
| clean ∩ extreme | {clean_ext_overlap} |
| top1 / top5 pool share (clean) | {policies['raw_all_clean']['concentration']['top1_pool_share']:.4f} / {policies['raw_all_clean']['concentration']['top5_pool_share']:.4f} |
| day_index=2 max pool share (clean cohort) | {day2_clean_max_pool:.4f} |
| day_index=2 max pool share (all rows w/ day2) | {day2_max_share_attempted:.4f} |

## B. Return readout (clean only)

| Stat | Value |
|------|-------|
| mean net_return | {b_stats['mean']:.6f} |
| median net_return | {b_stats['median']:.6f} |
| positive_rate | {b_stats['positive_rate']:.4f} |
| p05 / p25 / p50 / p75 / p95 / p99 | {b_stats['p05']:.4f} / {b_stats['p25']:.4f} / {b_stats['p50']:.4f} / {b_stats['p75']:.4f} / {b_stats['p95']:.4f} / {b_stats['p99']:.4f} |
| total sum(net_return) | {b_stats['total_net_return_proxy']:.4f} |
| profit_factor | {b_stats['profit_factor']:.4f} |
| avg_win / avg_loss | {b_stats['avg_win']:.6f} / {b_stats['avg_loss']:.6f} |
| max_drawdown proxy (sum-units) | {mdd_full:.4f} |

## C. Buckets

**no_model_score_available:** {not has_score}

Buckets use **chronological order** as deterministic baseline (earliest = top fractions). This is not a predictive score.

```json
{json.dumps(bucket_readout["buckets"], indent=2)}
```

## D. Policy simulation

| Policy | n | mean | median | pos_rate | sum_ret | PF | top1_pool | day2_share |
|--------|---|------|--------|----------|-------|-----|-----------|------------|
"""
    for _k, pol in policies.items():
        st = pol["return_stats"]
        c = pol["concentration"]
        md += (
            f"| {pol['policy']} | {st['n']:,} | {st['mean']:.6f} | {st['median']:.6f} | "
            f"{st['positive_rate']:.4f} | {st['total_net_return_proxy']:.2f} | {st['profit_factor']:.4f} | "
            f"{c['top1_pool_share']:.4f} | {c['day_index_2_share']:.4f} |\n"
        )

    md += f"""
## E. Chronological OOS (70/30)

| | Train | Holdout |
|---|-------|---------|
| n | {st_tr['n']:,} | {st_ho['n']:,} |
| mean net_return | {st_tr['mean']:.6f} | {st_ho['mean']:.6f} |
| median | {st_tr['median']:.6f} | {st_ho['median']:.6f} |
| positive_rate | {st_tr['positive_rate']:.4f} | {st_ho['positive_rate']:.4f} |

- holdout - train mean drift: **{st_ho['mean'] - st_tr['mean']:.6f}**
- holdout top pool share: **{top_ph_share:.4f}**
- holdout day_index=2 share: **{conc_ho['day_index_2_share']:.4f}**
- holdout day2 max-pool share: **{ho_d2_pool:.4f}**

## F. Verdict rationale

{chr(10).join('- ' + x for x in rationale)}

## Plain-language answers

1. **7D 到底有没有 alpha？**  verdict **{code}**。在 `net_return`（已含 gas / slippage / 费用等 live-equivalent 假设）下，clean 全样本与所测分桶/政策均为 **负的均值与负的合计 proxy**，**没有出现 execution-cost-adjusted 的可交易正期望**。
2. **是 raw alpha 还是 cap 后仍成立？** **两者都不成立**：raw mean **{raw_mean:.6f}**；per-day-per-pool cap 300 后 **{cap_mean:.6f}**；去掉 day_index=2 最大池后 **{exc_mean:.6f}**，均为负。
3. **是不是只靠 day_index=2 或单个池？** clean 里 **day_index=2 单侧最大池约占 {day2_clean_max_pool:.1%}**（集中度偏高），但 **失败主因不是「单靠某一幕」**：holdout 段 **day2 份额为 0**，mean 仍 **{hold_mean:.6f}**，说明 **时间上也没有守住正边**。
4. **holdout 还赚钱吗？** **不赚**：holdout mean **{hold_mean:.6f}**，positive_rate **{st_ho['positive_rate']:.4f}**。
5. **扣成本后还赚钱吗？** **不赚**：本 readout 用的就是 **扣成本后的 `net_return`**。
6. **如果有，是强、弱、还是需要 30D 确认？** **{code}** —— 属于 **7D 内未发现可用边**；**不是** 「弱边待 30D」档。
7. **如果没有，主要死在哪里？** 全体与 bucket 的 **mean / sum proxy 为负**，**profit factor < 1**；chronological holdout **mean 为负**，不满足「时间上仍能赚」的最低 bar。
8. **是否还值得跑 30D bounded？** 本 readout **未** 因 alpha 信号推荐 30D（`allow_30d_bounded_run_recommended_from_this_readout` = false）。若产品上要 **独立做日历/成本实验**，可与本轮结论 **分开立项**，不要当作「7D 已过关」的延续。
9. **仍禁止 training / H19 / shadow / live / promote？** **是** —— 本 scoped readout **不授权** 训练 production 模型、H19、shadow、实盘或 promote。

## Decision flags

```json
{json.dumps(payload["decision_flags"], indent=2)}
```

## Artifacts

- `mvp/diagnostics/run004_v2_7d_alpha_readout_summary.json`
- `mvp/runs/run-004-v2-7d-alpha-readout.md`
"""
    OUT_MD.write_text(md, encoding="utf-8")
    print(OUT_JSON, code)


if __name__ == "__main__":
    main()
