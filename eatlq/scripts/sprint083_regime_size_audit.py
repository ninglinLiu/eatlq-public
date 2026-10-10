#!/usr/bin/env python3
"""Regime split + size portability audit (no training, no live)."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

REPO = Path(__file__).resolve().parent.parent

DEFAULT_DATASET = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_copyability_safe_prep_expanded_v2/copyability_safe_dataset_sample.csv"
)
DEFAULT_TOP10 = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_copyability_safe_xgb_smoke_expanded_v2/selected_top10_copyability.csv"
)
DEFAULT_OUT = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_copyability_safe_xgb_smoke_expanded_v2"
)

LABEL_CLIP = 1.0
SIZE_THRESHOLDS = [1.0, 5.0, 20.0, 50.0]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Regime + size audit")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--top10", default=DEFAULT_TOP10)
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    return p.parse_args()


def as_float(v) -> float | None:
    if v in (None, "", "\\N"):
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def as_bool(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


def clip_ret(v: float | None) -> float | None:
    if v is None:
        return None
    if abs(v) > LABEL_CLIP:
        return LABEL_CLIP if v > 0 else -LABEL_CLIP
    return v


def pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(math.ceil(q * len(s))) - 1))
    return s[i]


def pf_proxy(returns: list[float]) -> float | None:
    wins = sum(r for r in returns if r > 0)
    losses = abs(sum(r for r in returns if r < 0))
    if losses > 0:
        return wins / losses
    if wins > 0:
        return float("inf")
    return None


def top_share(vals: list[str]) -> float | None:
    if not vals:
        return None
    c = Counter(vals)
    return max(c.values()) / len(vals)


def label_metrics(rows: list[dict], ret_col: str, clip: bool = True) -> dict:
    rets = []
    for r in rows:
        v = as_float(r.get(ret_col))
        if v is None:
            continue
        rets.append(clip_ret(v) if clip else v)
    rets = [x for x in rets if x is not None]
    if not rets:
        return {
            "n": 0,
            "mean": None,
            "hit_rate": None,
            "pf_proxy": None,
        }
    pf = pf_proxy(rets)
    return {
        "n": len(rets),
        "mean": round(mean(rets), 6),
        "hit_rate": round(sum(1 for x in rets if x > 0) / len(rets), 4),
        "pf_proxy": round(pf, 4) if pf is not None and math.isfinite(pf) else pf,
    }


def pnl_metrics(rows: list[dict]) -> dict:
    pnls = [as_float(r.get("realized_wallet_pnl_weth")) for r in rows]
    pnls = [x for x in pnls if x is not None]
    if not pnls:
        return {"n": 0, "mean": None, "hit_rate": None, "pf_proxy": None}
    pf = pf_proxy(pnls)
    return {
        "n": len(pnls),
        "mean": round(mean(pnls), 6),
        "hit_rate": round(sum(1 for x in pnls if x > 0) / len(pnls), 4),
        "pf_proxy": round(pf, 4) if pf is not None and math.isfinite(pf) else pf,
    }


def subset_summary(rows: list[dict], regime_name: str) -> dict:
    pools = [str(r.get("pool", "")).lower() for r in rows if r.get("pool")]
    tokens = [str(r.get("token", "")).lower() for r in rows if r.get("token")]
    wallets = [str(r.get("wallet", "")).lower() for r in rows if r.get("wallet")]
    closed = sum(1 for r in rows if as_bool(r.get("closed_loop_eligible")))
    weth = [as_float(r.get("amount_weth")) for r in rows]
    weth = [x for x in weth if x is not None]
    return {
        "regime": regime_name,
        "rows": len(rows),
        "unique_pools": len(set(pools)),
        "unique_tokens": len(set(tokens)),
        "unique_wallets": len(set(wallets)),
        "top_pool_share": round(top_share(pools), 4) if pools else None,
        "top_token_share": round(top_share(tokens), 4) if tokens else None,
        "top_wallet_share": round(top_share(wallets), 4) if wallets else None,
        "closed_loop_coverage": round(closed / len(rows), 4) if rows else None,
        "amount_weth_p50": round(pct(weth, 0.5), 6) if weth else None,
        "amount_weth_p90": round(pct(weth, 0.9), 6) if weth else None,
        "amount_weth_p95": round(pct(weth, 0.95), 6) if weth else None,
        "amount_weth_max": round(max(weth), 6) if weth else None,
        "copy_delay_60s": label_metrics(rows, "copy_delay_60s"),
        "copy_delay_120s": label_metrics(rows, "copy_delay_120s"),
        "realized_wallet_pnl_weth": pnl_metrics(rows),
    }


def dominant_subject(rows: list[dict], key: str) -> tuple[str, float]:
    vals = [str(r.get(key, "")).lower() for r in rows if r.get(key)]
    if not vals:
        return "", 0.0
    c = Counter(vals)
    top, cnt = c.most_common(1)[0]
    return top, cnt / len(vals)


def decide_status(
    summaries: list[dict],
    size_rows: list[dict],
    autopsy: dict,
    full_rows: list[dict],
) -> tuple[str, str]:
    full_m120 = label_metrics(full_rows, "copy_delay_120s")
    whale = next((s for s in summaries if s["regime"] == "bucket_whale"), None)
    non_whale = [s for s in summaries if s["regime"].startswith("bucket_") and "whale" not in s["regime"]]
    portable_20 = next((s for s in size_rows if s["regime"] == "size_lte_20_weth"), None)
    portable_5 = next((s for s in size_rows if s["regime"] == "size_lte_5_weth"), None)

    top10_driven = autopsy.get("single_subject_driven", False)
    whale_only = False
    if whale and portable_20:
        w_hit = whale["copy_delay_120s"].get("hit_rate") or 0
        p_hit = portable_20["copy_delay_120s"].get("hit_rate") or 0
        w_mean = whale["copy_delay_120s"].get("mean") or 0
        p_mean = portable_20["copy_delay_120s"].get("mean") or 0
        if w_hit > 0.55 and p_hit < 0.52 and w_mean > p_mean + 0.002:
            whale_only = True

    if top10_driven or autopsy.get("top_wallet_share_in_top10", 0) >= 0.45:
        return "SINGLE_SUBJECT_ARTIFACT", "REJECT_NOT_COPYABLE"
    if whale_only:
        return "WHALE_ONLY_SIGNAL", "REGIME_SPLIT_REQUIRED"
    if portable_5 and (portable_5["copy_delay_120s"].get("hit_rate") or 0) >= 0.52:
        if portable_5["top_pool_share"] and portable_5["top_pool_share"] < 0.25:
            return "GENERAL_SIGNAL_POSSIBLE", "SIZE_FILTERED_RETRAIN_REVIEW_READY"
    if portable_20 and (portable_20["copy_delay_120s"].get("hit_rate") or 0) >= 0.52:
        return "REGIME_SPLIT_REQUIRED", "REGIME_SPLIT_REQUIRED"
    if (full_m120.get("hit_rate") or 0) < 0.52:
        return "REJECT_NOT_COPYABLE", "REJECT_NOT_COPYABLE"
    return "REGIME_SPLIT_REQUIRED", "FEATURE_REPAIR_REQUIRED"


def flatten_summary(s: dict) -> dict:
    flat = {k: v for k, v in s.items() if k not in ("copy_delay_60s", "copy_delay_120s", "realized_wallet_pnl_weth")}
    for prefix, block in (
        ("cd60_", s.get("copy_delay_60s", {})),
        ("cd120_", s.get("copy_delay_120s", {})),
        ("pnl_", s.get("realized_wallet_pnl_weth", {})),
    ):
        for k, v in block.items():
            flat[f"{prefix}{k}"] = v
    return flat


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = list(csv.DictReader((REPO / args.dataset).open()))
    top10 = list(csv.DictReader((REPO / args.top10).open())) if (REPO / args.top10).exists() else []

    summaries: list[dict] = []
    summaries.append(subset_summary(rows, "full_dataset"))

    # by trade_size_bucket
    buckets = Counter(str(r.get("trade_size_bucket", "") or "unknown") for r in rows)
    for b in sorted(buckets):
        sub = [r for r in rows if (str(r.get("trade_size_bucket", "") or "unknown") == b)]
        summaries.append(subset_summary(sub, f"bucket_{b}"))

    # non-whale
    summaries.append(
        subset_summary(
            [r for r in rows if str(r.get("trade_size_bucket", "")).lower() != "whale"],
            "bucket_non_whale",
        )
    )

    dom_pool, dom_pool_share = dominant_subject(rows, "pool")
    dom_token, dom_token_share = dominant_subject(rows, "token")
    dom_wallet, dom_wallet_share = dominant_subject(rows, "wallet")

    summaries.append(subset_summary([r for r in rows if str(r.get("pool", "")).lower() == dom_pool], "pool_dominant"))
    summaries.append(subset_summary([r for r in rows if str(r.get("pool", "")).lower() != dom_pool], "pool_non_dominant"))
    summaries.append(subset_summary([r for r in rows if str(r.get("token", "")).lower() == dom_token], "token_dominant"))
    summaries.append(subset_summary([r for r in rows if str(r.get("token", "")).lower() != dom_token], "token_non_dominant"))
    summaries.append(subset_summary([r for r in rows if str(r.get("wallet", "")).lower() == dom_wallet], "wallet_dominant"))
    summaries.append(subset_summary([r for r in rows if str(r.get("wallet", "")).lower() != dom_wallet], "wallet_non_dominant"))

    summaries.append(subset_summary([r for r in rows if as_bool(r.get("closed_loop_eligible"))], "closed_loop_true"))
    summaries.append(subset_summary([r for r in rows if not as_bool(r.get("closed_loop_eligible"))], "closed_loop_false"))

    # size thresholds
    size_summaries: list[dict] = []
    for thr in SIZE_THRESHOLDS:
        sub = [r for r in rows if (as_float(r.get("amount_weth")) or 999) <= thr]
        size_summaries.append(subset_summary(sub, f"size_lte_{int(thr) if thr == int(thr) else thr}_weth"))
    size_summaries.append(
        subset_summary(
            [r for r in rows if str(r.get("trade_size_bucket", "")).lower() != "whale"],
            "size_portable_no_whale",
        )
    )

    # top10 autopsy
    autopsy_rows: list[dict] = []
    if top10:
        t_pools = [str(r.get("pool", "")).lower() for r in top10 if r.get("pool")]
        t_tokens = [str(r.get("token", "")).lower() for r in top10 if r.get("token")]
        t_wallets = [str(r.get("wallet", "")).lower() for r in top10 if r.get("wallet")]
        top_pool = Counter(t_pools).most_common(1)[0][0] if t_pools else ""
        top_token = Counter(t_tokens).most_common(1)[0][0] if t_tokens else ""
        top_wallet = Counter(t_wallets).most_common(1)[0][0] if t_wallets else ""

        def subject_row(col: str, subject_id: str) -> dict:
            in_top10 = sum(1 for r in top10 if str(r.get(col, "")).lower() == subject_id)
            in_full = [r for r in rows if str(r.get(col, "")).lower() == subject_id]
            share_full = len(in_full) / len(rows) if rows else 0
            cd120 = label_metrics(in_full, "copy_delay_120s")
            cd60 = label_metrics(in_full, "copy_delay_60s")
            pnl = pnl_metrics(in_full)
            top10_sub = [r for r in top10 if str(r.get(col, "")).lower() == subject_id]
            return {
                "subject_type": col,
                "subject_id": subject_id,
                "top10_rows": in_top10,
                "top10_share_of_selected": round(in_top10 / len(top10), 4) if top10 else None,
                "full_dataset_rows": len(in_full),
                "full_dataset_share": round(share_full, 4),
                "top10_mean_copy_delay_120s": label_metrics(top10_sub, "copy_delay_120s")["mean"],
                "full_mean_copy_delay_120s": cd120["mean"],
                "full_hit_rate_copy_delay_120s": cd120["hit_rate"],
                "full_pf_copy_delay_120s": cd120["pf_proxy"],
                "full_mean_copy_delay_60s": cd60["mean"],
                "full_mean_realized_pnl_weth": pnl["mean"],
            }

        for col, sid in (("pool", top_pool), ("token", top_token), ("wallet", top_wallet)):
            autopsy_rows.append(subject_row(col, sid))

        top_wallet_share_top10 = top_share(t_wallets) or 0
        top_pool_share_top10 = top_share(t_pools) or 0
        single_subject = max(top_wallet_share_top10, top_pool_share_top10) >= 0.45
    else:
        top_pool = top_token = top_wallet = ""
        top_wallet_share_top10 = top_pool_share_top10 = 0
        single_subject = False

    signal_status, next_step = decide_status(summaries + size_summaries, size_summaries, {
        "single_subject_driven": single_subject,
        "top_wallet_share_in_top10": top_wallet_share_top10,
        "top_pool_share_in_top10": top_pool_share_top10,
        "dominant_pool": dom_pool,
        "dominant_wallet": dom_wallet,
    }, rows)

    flags = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "REGIME_SIZE_AUDIT_DONE": True,
        "SIZE_PORTABILITY_REVIEWED": True,
        "SELECTED_TOP10_AUTOPSY_DONE": bool(top10),
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "PAPER_FORWARD_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "REGIME_SIGNAL_STATUS": signal_status,
        "NEXT_STEP": next_step,
        "dominant_subjects_full_dataset": {
            "pool": dom_pool,
            "pool_share": round(dom_pool_share, 4),
            "token": dom_token,
            "token_share": round(dom_token_share, 4),
            "wallet": dom_wallet,
            "wallet_share": round(dom_wallet_share, 4),
        },
        "top10_dominant_subjects": {
            "pool": top_pool,
            "token": top_token,
            "wallet": top_wallet,
            "top_pool_share_in_top10": top_pool_share_top10,
            "top_wallet_share_in_top10": top_wallet_share_top10,
        },
        "single_subject_driven": single_subject,
    }

    all_flat = [flatten_summary(s) for s in summaries + size_summaries]
    with (out_dir / "regime_size_summary.csv").open("w", newline="") as f:
        if all_flat:
            w = csv.DictWriter(f, fieldnames=list(all_flat[0].keys()))
            w.writeheader()
            w.writerows(all_flat)

    if autopsy_rows:
        with (out_dir / "selected_top10_subject_autopsy.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(autopsy_rows[0].keys()))
            w.writeheader()
            w.writerows(autopsy_rows)

    (out_dir / "regime_size_audit_flags.json").write_text(json.dumps(flags, indent=2) + "\n", encoding="utf-8")

    md = [
        "# Regime + Size Portability Audit",
        "",
        f"Generated: {flags['generated_at_utc']}",
        "",
        "## Executive conclusion",
        "",
        f"**{signal_status}** — NEXT_STEP=`{next_step}`. No training in this step.",
        "",
        "## Size bucket readout",
        "",
        "| regime | rows | amount_weth p50 | cd120 mean | cd120 hit | top_pool |",
        "|--------|------|-----------------|------------|-----------|----------|",
    ]
    for s in summaries:
        if s["regime"].startswith("bucket_"):
            md.append(
                f"| {s['regime']} | {s['rows']} | {s.get('amount_weth_p50')} | "
                f"{s['copy_delay_120s'].get('mean')} | {s['copy_delay_120s'].get('hit_rate')} | {s.get('top_pool_share')} |"
            )
    md.extend(["", "## Regime split readout", "", "| regime | rows | uniq pools | cd120 hit | pnl mean | top_wallet |", "|--------|------|------------|-----------|----------|------------|"])
    for s in summaries:
        if not s["regime"].startswith("bucket_"):
            md.append(
                f"| {s['regime']} | {s['rows']} | {s['unique_pools']} | {s['copy_delay_120s'].get('hit_rate')} | "
                f"{s['realized_wallet_pnl_weth'].get('mean')} | {s.get('top_wallet_share')} |"
            )
    md.extend(["", "## Size-portable subsets", ""])
    for s in size_summaries:
        md.append(
            f"- **{s['regime']}**: rows={s['rows']}, p50={s.get('amount_weth_p50')}, "
            f"cd120_hit={s['copy_delay_120s'].get('hit_rate')}, top_pool={s.get('top_pool_share')}"
        )
    md.extend(["", "## Selected top10 autopsy", "", f"single_subject_driven={single_subject}", ""])
    for ar in autopsy_rows:
        md.append(f"- {ar['subject_type']} `{ar['subject_id'][:18]}...` top10_share={ar['top10_share_of_selected']} full_share={ar['full_dataset_share']}")
    md.extend(["", "## Decision flags", "", "```json", json.dumps(flags, indent=2), "```"])
    (out_dir / "regime_size_audit.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(json.dumps({"REGIME_SIGNAL_STATUS": signal_status, "NEXT_STEP": next_step}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
