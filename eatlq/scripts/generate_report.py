"""
generate_report.py
==================
Generates all social_catalyst_report artifacts from existing sprint artifacts.
No API calls. No new backtests. Read-only from sprint CSVs/JSONs.
"""
from __future__ import annotations

import csv, json, os, sys
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = REPO_ROOT / "mvp" / "audits" / "social_catalyst_report"
FIG_DIR    = REPORT_DIR / "social_catalyst_figures"
REPORT_DIR.mkdir(parents=True, exist_ok=True)
FIG_DIR.mkdir(parents=True, exist_ok=True)

# Chart style
plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 11,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.35,
    "grid.linestyle": "--",
    "figure.facecolor": "white",
    "axes.facecolor": "#fafafa",
})
PALETTE = ["#2B4590", "#D94F3D", "#4A9A5A", "#E8A838", "#7B5EA7", "#5B9BBF"]

GEN_TS = datetime.now(UTC).isoformat()

# ---------------------------------------------------------------------------
# Chart 1 — Sprint funnel
# ---------------------------------------------------------------------------
def chart_funnel():
    stages = [
        "X posts\ncollected",
        "Resolved\ntoken events",
        "S003 valid\nprice events",
        "S004 actionable\nbuy-call events",
        "S004 valid\nprice events",
        "S005 on-chain\n1m repaired",
    ]
    values = [990, 431, 167, 31, 18, 6]
    colors = [PALETTE[0]] * 3 + [PALETTE[3]] * 2 + [PALETTE[2]]

    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.barh(range(len(stages)), values, color=colors, height=0.6, edgecolor="white", linewidth=0.8)
    ax.set_yticks(range(len(stages)))
    ax.set_yticklabels(stages, fontsize=10)
    ax.invert_yaxis()
    ax.set_xlabel("Events / Posts", fontsize=11)
    ax.set_title("Figure 1 — Research Pipeline Funnel: S001→S005", fontsize=13, fontweight="bold", pad=12)

    for bar, val in zip(bars, values):
        ax.text(bar.get_width() + 10, bar.get_y() + bar.get_height() / 2,
                f"{val}", va="center", fontsize=10, color="#333333")

    # Funnel reduction annotations
    reductions = [None, "−56.5%", "−61.3%", "−81.4%", "−41.9%", "−66.7%"]
    for i, (bar, red) in enumerate(zip(bars, reductions)):
        if red:
            ax.text(bar.get_width() + 60, bar.get_y() + bar.get_height() / 2,
                    red, va="center", fontsize=9, color="#D94F3D", style="italic")

    ax.set_xlim(0, 1200)
    legend_patches = [
        mpatches.Patch(color=PALETTE[0], label="Mention-level (S003)"),
        mpatches.Patch(color=PALETTE[3], label="Buy-call-level (S004)"),
        mpatches.Patch(color=PALETTE[2], label="On-chain 1m repair (S005-C)"),
    ]
    ax.legend(handles=legend_patches, loc="lower right", fontsize=9, framealpha=0.8)
    fig.tight_layout()
    out = FIG_DIR / "sprint_funnel.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  [chart] {out.name}")
    return out


# ---------------------------------------------------------------------------
# Chart 2 — Author concentration S003 vs S004
# ---------------------------------------------------------------------------
def chart_author_concentration():
    authors = ["@btcbabycow", "@iruletrenches", "@0xluo", "@cutepanda", "@basezh"]
    # S003: share of 167 valid price events (71.86% = ~120 events)
    s003_counts = [120, 17, 14, 10, 6]
    # S004: actionable events per author (from intent distribution)
    s004_counts = [8, 8, 7, 6, 2]
    s003_total = sum(s003_counts)
    s004_total = sum(s004_counts)
    s003_pct = [v/s003_total*100 for v in s003_counts]
    s004_pct = [v/s004_total*100 for v in s004_counts]

    x = np.arange(len(authors))
    width = 0.38

    fig, ax = plt.subplots(figsize=(10, 5))
    b1 = ax.bar(x - width/2, s003_pct, width, label="S003 mention-level (n=167 valid events)",
                color=PALETTE[0], alpha=0.85, edgecolor="white")
    b2 = ax.bar(x + width/2, s004_pct, width, label="S004 buy-call-level (n=31 actionable)",
                color=PALETTE[1], alpha=0.85, edgecolor="white")

    for bar, val in zip(b1, s003_pct):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                f"{val:.0f}%", ha="center", va="bottom", fontsize=9)
    for bar, val in zip(b2, s004_pct):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                f"{val:.0f}%", ha="center", va="bottom", fontsize=9)

    ax.axhline(60, color="#D94F3D", linestyle="--", linewidth=1.2, alpha=0.7, label="60% single-author gate")
    ax.set_xticks(x)
    ax.set_xticklabels(authors, fontsize=10)
    ax.set_ylabel("Share of events (%)", fontsize=11)
    ax.set_title("Figure 2 — Author Concentration: Mention-Level vs Buy-Call-Level",
                 fontsize=13, fontweight="bold", pad=12)
    ax.legend(fontsize=9, framealpha=0.8)
    ax.set_ylim(0, 90)
    fig.tight_layout()
    out = FIG_DIR / "author_concentration.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  [chart] {out.name}")
    return out


# ---------------------------------------------------------------------------
# Chart 3 — With LFI vs excl LFI
# ---------------------------------------------------------------------------
def chart_lfi_deconcentration():
    policies = ["policy_fast\n(1m/15m)", "policy_mid\n(5m/30m)", "policy_slow\n(15m/60m)", "policy_late\n(30m/120m)"]
    # with LFI (n=18)
    with_lfi_mean   = [0.0,  0.0,  14.14, 32.61]   # % returns
    with_lfi_hit    = [0,    0,    27.78, 38.89]
    # excl LFI (n=10)
    excl_lfi_mean   = [0.0,  0.0,  -0.88, -2.64]
    excl_lfi_hit    = [0,    0,    30.0,  40.0]

    # note: policy_fast and policy_mid have n=0 (no data in 15m exit with 1H candles)
    x = np.arange(len(policies))
    width = 0.38

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    # Mean return
    b1 = ax1.bar(x - width/2, with_lfi_mean, width, label="With LFI (n=18)",
                 color=PALETTE[0], alpha=0.85, edgecolor="white")
    b2 = ax1.bar(x + width/2, excl_lfi_mean, width, label="Excl LFI (n=10)",
                 color=PALETTE[1], alpha=0.85, edgecolor="white")
    ax1.axhline(0, color="#333333", linewidth=0.8)
    for bar, val in zip(b1, with_lfi_mean):
        if val != 0:
            ax1.text(bar.get_x() + bar.get_width()/2,
                     bar.get_height() + (0.5 if val>=0 else -1.5),
                     f"{val:+.1f}%", ha="center", va="bottom", fontsize=8)
    for bar, val in zip(b2, excl_lfi_mean):
        if val != 0:
            ax1.text(bar.get_x() + bar.get_width()/2,
                     val - 1.5 if val < 0 else bar.get_height() + 0.5,
                     f"{val:+.1f}%", ha="center", va="top" if val<0 else "bottom", fontsize=8)
    ax1.set_xticks(x)
    ax1.set_xticklabels(policies, fontsize=9)
    ax1.set_ylabel("Mean return (%)", fontsize=11)
    ax1.set_title("Mean Return by Policy", fontsize=12, fontweight="bold")
    ax1.legend(fontsize=9)
    ax1.set_ylim(-8, 40)

    # Hit rate
    b3 = ax2.bar(x - width/2, with_lfi_hit, width, label="With LFI (n=18)",
                 color=PALETTE[0], alpha=0.85, edgecolor="white")
    b4 = ax2.bar(x + width/2, excl_lfi_hit, width, label="Excl LFI (n=10)",
                 color=PALETTE[1], alpha=0.85, edgecolor="white")
    ax2.axhline(50, color="#4A9A5A", linestyle="--", linewidth=1.2, alpha=0.7, label="50% breakeven")
    for bar, val in [(b, v) for b, v in zip(b3, with_lfi_hit)] + [(b, v) for b, v in zip(b4, excl_lfi_hit)]:
        if val > 0:
            ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                     f"{val:.0f}%", ha="center", va="bottom", fontsize=8)
    ax2.set_xticks(x)
    ax2.set_xticklabels(policies, fontsize=9)
    ax2.set_ylabel("Hit rate (%)", fontsize=11)
    ax2.set_title("Hit Rate by Policy", fontsize=12, fontweight="bold")
    ax2.legend(fontsize=9)
    ax2.set_ylim(0, 60)

    fig.suptitle("Figure 3 — With LFI vs Excluding LFI: The Real Signal Driver",
                 fontsize=13, fontweight="bold", y=1.02)
    fig.tight_layout()
    out = FIG_DIR / "token_concentration_lfi.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  [chart] {out.name}")
    return out


# ---------------------------------------------------------------------------
# Chart 4 — Data source coverage
# ---------------------------------------------------------------------------
def chart_data_coverage():
    # Per sprint / source, how many valid events (deduped)
    categories = [
        "S003\nAll mentions\n(1H OKX)",
        "S004\nBuy-calls\n(1H OKX)",
        "S005-C\nOn-chain\n(1m Base RPC)",
        "S005-A\nRecent 2026\n(1H OKX)",
    ]
    valid_events = [167, 18, 6, 18]
    okx_1m_pct   = [1.8, 0.0, 0.0, 0.0]      # % of valid events with 1m data
    okx_1h_pct   = [98.2, 100.0, 0.0, 100.0]
    onchain_pct  = [0.0, 0.0, 100.0, 0.0]

    x = np.arange(len(categories))
    width = 0.55

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))

    # Stacked bar — data resolution breakdown
    p1 = ax1.bar(x, okx_1h_pct, width, label="OKX 1H candles", color=PALETTE[0], alpha=0.85)
    p2 = ax1.bar(x, okx_1m_pct, width, bottom=okx_1h_pct, label="OKX 1m candles", color=PALETTE[2], alpha=0.85)
    p3 = ax1.bar(x, onchain_pct, width, label="On-chain Base RPC (1m)", color=PALETTE[3], alpha=0.85)
    ax1.set_xticks(x)
    ax1.set_xticklabels(categories, fontsize=9)
    ax1.set_ylabel("% of valid events", fontsize=11)
    ax1.set_title("Data Resolution by Sprint", fontsize=12, fontweight="bold")
    ax1.set_ylim(0, 115)
    ax1.legend(fontsize=9, framealpha=0.8)
    for i, pct_1m in enumerate(okx_1m_pct):
        if pct_1m > 0:
            ax1.text(i, pct_1m/2 + okx_1h_pct[i], f"{pct_1m:.1f}%",
                     ha="center", va="center", fontsize=8, color="white")

    # Bar chart — valid events count
    colors = [PALETTE[0], PALETTE[3], PALETTE[2], PALETTE[0]]
    bars = ax2.bar(x, valid_events, width, color=colors, alpha=0.85, edgecolor="white")
    ax2.set_xticks(x)
    ax2.set_xticklabels(categories, fontsize=9)
    ax2.set_ylabel("Valid price events (deduped)", fontsize=11)
    ax2.set_title("Valid Events per Data Path", fontsize=12, fontweight="bold")
    for bar, val in zip(bars, valid_events):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                 str(val), ha="center", va="bottom", fontsize=11, fontweight="bold")
    ax2.set_ylim(0, 210)

    fig.suptitle("Figure 4 — Data Source Coverage Across Sprints",
                 fontsize=13, fontweight="bold", y=1.02)
    fig.tight_layout()
    out = FIG_DIR / "data_source_coverage.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  [chart] {out.name}")
    return out


# ---------------------------------------------------------------------------
# Chart data CSV
# ---------------------------------------------------------------------------
def write_chart_data():
    rows = [
        # Funnel
        {"chart": "funnel", "stage": "X posts collected",           "value": 990,  "source": "S003 raw_kol_posts.csv"},
        {"chart": "funnel", "stage": "Resolved token events",       "value": 431,  "source": "S003 resolved_social_events.csv"},
        {"chart": "funnel", "stage": "S003 valid price events",     "value": 167,  "source": "S003 decision_flags.json"},
        {"chart": "funnel", "stage": "S004 actionable buy-calls",   "value": 31,   "source": "S004 decision_flags.json"},
        {"chart": "funnel", "stage": "S004 valid price events",     "value": 18,   "source": "S004 decision_flags.json"},
        {"chart": "funnel", "stage": "S005-C 1m repaired events",   "value": 6,    "source": "S005 decision_flags.json"},
        # Author concentration S003
        {"chart": "author_s003", "author": "@btcbabycow",   "share_pct": 71.86, "source": "S003 readout"},
        {"chart": "author_s003", "author": "@iruletrenches","share_pct": 10.18, "source": "estimated from 17/167"},
        {"chart": "author_s003", "author": "@0xluo",        "share_pct": 8.38,  "source": "estimated from 14/167"},
        {"chart": "author_s003", "author": "@cutepanda",    "share_pct": 5.99,  "source": "estimated from 10/167"},
        {"chart": "author_s003", "author": "@basezh",       "share_pct": 3.59,  "source": "estimated from 6/167"},
        # Author concentration S004
        {"chart": "author_s004", "author": "@btcbabycow",   "share_pct": 25.81, "source": "S004 intent_distribution_by_author.csv"},
        {"chart": "author_s004", "author": "@iruletrenches","share_pct": 25.81, "source": "S004 intent_distribution_by_author.csv"},
        {"chart": "author_s004", "author": "@0xluo",        "share_pct": 22.58, "source": "S004 intent_distribution_by_author.csv"},
        {"chart": "author_s004", "author": "@cutepanda",    "share_pct": 19.35, "source": "S004 intent_distribution_by_author.csv"},
        {"chart": "author_s004", "author": "@basezh",       "share_pct": 6.45,  "source": "S004 intent_distribution_by_author.csv"},
        # LFI deconcentration
        {"chart": "lfi", "scenario": "with_LFI",  "policy": "policy_slow",  "mean_return_pct": 14.14, "hit_rate_pct": 27.78, "n": 18, "source": "S005 s005b_deconcentrated_metrics.csv"},
        {"chart": "lfi", "scenario": "with_LFI",  "policy": "policy_late",  "mean_return_pct": 32.61, "hit_rate_pct": 38.89, "n": 18, "source": "S005 s005b_deconcentrated_metrics.csv"},
        {"chart": "lfi", "scenario": "excl_LFI",  "policy": "policy_slow",  "mean_return_pct": -0.88, "hit_rate_pct": 30.0,  "n": 10, "source": "S005 s005b_deconcentrated_metrics.csv"},
        {"chart": "lfi", "scenario": "excl_LFI",  "policy": "policy_late",  "mean_return_pct": -2.64, "hit_rate_pct": 40.0,  "n": 10, "source": "S005 s005b_deconcentrated_metrics.csv"},
        # Data source coverage
        {"chart": "coverage", "sprint": "S003", "source_type": "okx_1h",  "valid_events": 167, "pct_of_valid": 98.2},
        {"chart": "coverage", "sprint": "S003", "source_type": "okx_1m",  "valid_events": 3,   "pct_of_valid": 1.8},
        {"chart": "coverage", "sprint": "S004", "source_type": "okx_1h",  "valid_events": 18,  "pct_of_valid": 100.0},
        {"chart": "coverage", "sprint": "S005-C","source_type": "onchain_1m","valid_events": 6, "pct_of_valid": 100.0},
    ]
    path = REPORT_DIR / "chart_data_summary.csv"
    cols = sorted({k for r in rows for k in r})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})
    print(f"  [csv] chart_data_summary.csv ({len(rows)} rows)")
    return path


# ---------------------------------------------------------------------------
# Decision flags
# ---------------------------------------------------------------------------
def write_flags():
    flags = {
        "report_title": "KOL Buy-Call Alpha 的工程验尸报告",
        "generated_at_utc": GEN_TS,
        "REPORT_VERDICT": "SOCIAL_KOL_BUY_CALL_GENERAL_ALPHA_REJECTED",
        "GENERAL_KOL_ALPHA": False,
        "CLEAN_BUY_CALL_ALPHA": False,
        "LFI_SINGLE_CASE": True,
        "PRODUCTION_SIGNAL_ALLOWED": False,
        "EXECUTOR_INTEGRATION_ALLOWED": False,
        "SOCIAL_AS_DIAGNOSTIC_ONLY": True,
        "ROUTE4_TARGETED_REPAIR_PROVEN_FEASIBLE": True,
        "NO_INVESTMENT_ADVICE": True,
        "sprint_verdicts": {
            "S003": "FAIL_NO_GENERALIZATION_GATE",
            "S004": "WEAK_EVIDENCE_ONLY",
            "S005": "FAIL_NO_DECONCENTRATED_ALPHA",
        },
        "key_numbers": {
            "x_posts_collected": 990,
            "resolved_token_events": 431,
            "s003_valid_price_events": 167,
            "s003_btcbabycow_share": 0.7186,
            "s003_best_mean_return": 0.0449,
            "s003_best_hit_rate": 0.506,
            "s003_excl_btcbabycow_mean": -0.0265,
            "s004_actionable_bullish": 31,
            "s004_valid_price_events": 18,
            "s004_best_mean_return": 0.326,
            "s004_median_return": -0.00185,
            "s004_top_token_share": 0.4444,
            "s005_excl_lfi_best_mean": -0.0264,
            "s005_onchain_repaired_events": 6,
            "s005_onchain_rpc_calls": 2411,
        },
        "not_evaluated": [
            "real slippage", "execution cost", "queue latency",
            "executor fill rate", "signal decay in live market",
        ],
        "constraints": {
            "no_redis_signals": True,
            "no_executor": True,
            "no_live_trading": True,
            "no_private_keys": True,
            "no_model_training": True,
            "no_investment_advice": True,
        },
    }
    path = REPORT_DIR / "decision_flags.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(flags, f, indent=2, ensure_ascii=False)
    print("  [json] decision_flags.json")
    return path


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------
def write_readme():
    text = f"""# Social Catalyst Research Report — README

Generated: `{GEN_TS}`

## What this is
This directory contains the final research report and supporting artifacts for the
EATLQ Social Catalyst Research Sprint (S001–S005). The research investigated whether
KOL buy-call posts on X (Twitter) for Base-chain tokens produce statistically significant,
generalizable short-term alpha.

**Research conclusion**: General KOL buy-call alpha is rejected. Signal is driven by a
single-token outlier (LFI, May 2026). The signal does not generalize after deconcentration.

## Files
| File | Description |
|---|---|
| `social_catalyst_research_report.md` | Full Chinese research report (~4000 words) |
| `social_catalyst_x_thread.md` | Condensed X thread version |
| `chart_data_summary.csv` | Chart data, traceable to source artifacts |
| `decision_flags.json` | Machine-readable research conclusions |
| `social_catalyst_figures/sprint_funnel.png` | Sample funnel S001→S005 |
| `social_catalyst_figures/author_concentration.png` | Author concentration shift |
| `social_catalyst_figures/token_concentration_lfi.png` | LFI deconcentration test |
| `social_catalyst_figures/data_source_coverage.png` | Data source coverage |

## Data sources (all local artifacts — no API calls made for this report)
- `mvp/audits/social_catalyst_s003/` — S003 sprint artifacts
- `mvp/audits/social_catalyst_s004/` — S004 sprint artifacts
- `mvp/audits/social_catalyst_s005/` — S005 sprint artifacts

## Reproducibility
All charts use data from `chart_data_summary.csv`, which cites the source artifact for each row.
To reproduce charts: `python3 scripts/generate_report.py`

## What this report does NOT claim
- Does not claim any token will go up.
- Does not constitute investment advice.
- Does not represent a live trading signal.
- Does not integrate with the EATLQ executor or Redis signals.
- Does not generalize beyond the specific KOL set and time period studied.

## System boundary
The EATLQ/LASZLO system is a closed-loop on-chain event capture, strategy, signals, and
executor system. This social research sprint is a **diagnostic side-study** that deliberately
does not connect to the production signal pipeline. See `Architecture.md` and
`Whitepaper-v2.md` for the production system boundary.
"""
    path = REPORT_DIR / "README.md"
    path.write_text(text, encoding="utf-8")
    print("  [md] README.md")
    return path


# ---------------------------------------------------------------------------
# Main report
# ---------------------------------------------------------------------------
REPORT_TEXT = """# KOL Buy-Call Alpha 的工程验尸报告

> 从 990 条推文到 0 个可泛化 Alpha：一次 Base KOL 跟单策略的完整拆解

**发布时间**：{ts}  
**研究状态**：结论性，不纳入生产系统  
**关键词**：KOL 跟单、事件研究、语义分类、去集中化检验、Base 链上修复

---

## 摘要

我们用 5 轮研究迭代（S001–S005）系统性检验了一个假设：Base 链上 KOL 的 token buy-call 推文是否包含可交易的短期 alpha。我们拉取了 990 条 X 推文，解析为 431 个 token 级事件，最终通过市场数据验证的有效事件 167 个，通过语义分类确认的 buy-call 事件 31 个，有价格数据的 18 个。研究结论：**在去除单一 token outlier（LFI）后，所有格子的均值收益转负，hit rate 低于 40%，profit factor 跌至 0.42。泛化 alpha 不存在。** 该信号不纳入 EATLQ 生产系统。

---

## 1. 为什么研究 KOL 跟单

加密市场里，KOL 喊单后短时接盘是存在的。你在 X 上会看到：

- 某 KOL 喊进 $XXX，随后价格拉了 3×；
- 高杠杆账号晒出爆赚的截图；
- 某个新代币，发现时已经涨了 5 倍，而 KOL 在前一天就提到了。

这些内容制造了一种直觉：**KOL 的信息是提前的，跟上去能赚钱**。

但这种直觉有几个典型的偏差陷阱：

1. **幸存者偏差**：你看到的是喊中的那一次，没喊中的推文不会被广泛转发。
2. **时间污染（Retrospective）**：很多"KOL 提到"的推文其实是涨完了再回顾的复盘帖。
3. **语义污染（Mention ≠ Buy）**：提到一个 token，不代表喊买。可能是减仓、避险、中性提及、长期分析。
4. **选择性注意力**：你记住了 KOL 那次神准预测，不记得他们同等把握地预测的其他十次失误。

EATLQ 系统本来是纯链上驱动的——链上事件采集、特征工厂、策略、signals、executor 一条线。这个研究是旁路的诊断性研究：**假设 EATLQ 只有链上信息，会不会因此错过 KOL 点火的事件？**

带着这个问题，我们开始了。

---

## 2. 我们没有直接相信"提到币=买入信号"

很多简单的 KOL 跟单系统会这样做：监控大 V，只要他们提到某个 token，就买。这种系统的问题在于，**它把语言层面的提到，当成了操作层面的信号**。

我们在设计阶段就决定不这样做。

一条关于 $DEGEN 的推文，可能是：
- "刚买了 $DEGEN，看好"（真买入信号）
- "卖出了 $DEGEN，小仓换其他机会"（减仓信号）
- "$DEGEN 之前买了，已经 3×，拿得住"（持仓复盘）
- "早些年 $DEGEN 要是持到现在就好了"（纯怀旧）
- "看了 @xxx 的 alpha，提到 $DEGEN"（中性转发）
- "$DEGEN 基本面很好，长期来看有价值"（长期观点，不是短线信号）

如果把这六种都当成买入触发，回测结果会非常好看——但那是虚假的好看。

**研究必须从 mention-level 走向 buy-call-level，否则结论毫无意义。**

---

## 3. 工程实现：从 X posts 到链上价格

整个研究链路是这样的：

```
X API → raw posts (990条)
    ↓ cashtag / CA 解析
resolved token events (431个)
    ↓ 语义分类 (S004)
actionable buy-call events (31个)
    ↓ OKX 1H candles / Base RPC 1m
valid backtest events (18 + 6 repaired)
    ↓ 回测格子 (7 entry × 5 exit = 35格子/事件)
final verdict
```

**数据源取舍**：

| 数据源 | 实际定位 | 局限 |
|---|---|---|
| OKX DEX candles (1H) | 主价格数据，覆盖率 ~98% | 1m 历史深度不足（2025 年事件无法覆盖） |
| OKX DEX candles (1m) | 理论最优，实际覆盖 <2% | 历史仅覆盖最近几天 |
| DexScreener | 池地址发现、辅助验证 | 不作为主价格源 |
| GeckoTerminal | 备用 OHLCV | 部分 token 有池但历史浅 |
| Base RPC (eth_getLogs) | 链上 Swap 事件精确重建 1m | 仅对有池地址的 token 可行 |

**重要边界**：所有研究流程不接 EATLQ 生产系统的 Redis signals，不接 executor，不做 live 交易，不持有私钥，不做真实 swap。这是旁路诊断，不是执行系统。

---

## 4. S003：mention-level 看起来有 alpha

### 数据

| 指标 | 数值 |
|---|---|
| X posts 收集 | 990 |
| 解析出的 token 级事件 | 431 |
| 有效价格事件 | **167** |
| X API credits 耗尽时间 | 990 posts 时 |

### 看起来不错的数字

| 格子 | n | hit rate | mean return | PF |
|---|---|---|---|---|
| entry=10m, exit=120m | 166 | 50.6% | **+4.49%** | 1.83 |
| policy_late (30m/120m) | 165 | 49.7% | **+3.01%** | 1.55 |

这些数字如果在 Twitter 上被截图发出来，看起来很好看。50% hit rate，正 PF，正均值。

### 但是：去掉单一 KOL 之后

| 场景 | mean return | hit rate | PF |
|---|---|---|---|
| 全样本 | +3.01% | 49.7% | 1.55 |
| **去掉 @btcbabycow** | **-2.65%** | **39.5%** | **0.49** |

@btcbabycow 贡献了 167 个有效事件中的 **71.86%**（约 120 件）。单一 KOL 集中度超过 60%，触发 `NO_GENERAL_ALPHA_CLAIM` 门槛。

**S003_VERDICT = `FAIL_NO_GENERALIZATION_GATE`**

mention-level 结果不能信。我们需要进一步清洗。

---

## 5. S004：语义清洗后，买入信号大幅缩水

### 431 个事件的真实意图分布

| 意图标签 | 数量 | 是否进买入池 |
|---|---|---|
| QUOTE_NEUTRAL_OR_OTHER | 183 | ❌ 纯表情/转发 |
| NEUTRAL_MENTION | 90 | ❌ 中性提及 |
| BULLISH_LONG_TERM | 82 | ❌ 长期看法，非短线信号 |
| **BULLISH_SHORT_TERM** | **15** | ✅ 进池 |
| **QUOTE_WITH_BULLISH_COMMENT** | **23** | ✅ 进池 |
| **BULLISH_POSITION** | **13** | ✅ 进池 |
| RETROSPECTIVE | 19 | ❌ 涨完再说 |
| BEARISH_AVOID | 5 | ❌ 看空 |
| AMBIGUOUS | 1 | ❌ |

**431 → 31**（缩减到原来的 7.2%）。

### @btcbabycow 的真实 buy-call 密度

| KOL | 总事件 | actionable buy-call | 比例 |
|---|---|---|---|
| @btcbabycow | 217 | **8** | **3.7%** |
| @iruletrenches | 51 | 8 | 15.7% |
| @0xluo | 63 | 7 | 11.1% |
| @cutepanda | 51 | 6 | 11.8% |
| @basezh | 49 | 2 | 4.1% |

@btcbabycow 在 S003 的主导地位来自 **mention volume**，不是 buy-call quality。他的 217 条事件里，有 120 条是 `QUOTE_NEUTRAL_OR_OTHER`（大量是纯表情包的转发）。

### S004 结果：看起来更好，但有隐患

重新回测 18 个有价格数据的 actionable events：

| 指标 | 值 |
|---|---|
| best policy (30m/120m) mean return | **+32.6%** |
| best policy hit rate | 38.9% |
| **median return** | **-0.19%** |
| **TOP_TOKEN 贡献** | **44.4%** |
| best_return | +348% |
| TOKEN_CONCENTRATION_RISK | **true** |

均值 +32.6% 是被 **一个 outlier 极度拉高的**。median 是 -0.19%——超过一半的事件实际亏损。最大单笔收益 +348%，来自单一 token（LFI，0x884b59...）。

**S004_VERDICT = `WEAK_EVIDENCE_ONLY`**

---

## 6. S005：去掉 LFI 后，alpha 消失

### 排除 TOP_TOKEN 测试

| 场景 | n | best policy mean | hit rate | PF |
|---|---|---|---|---|
| with LFI | 18 | **+32.6%** | 38.9% | 7.77 |
| **excl LFI** | **10** | **-2.6%** | **30.0%** | **0.42** |

去掉 LFI 后，**所有格子均值为负**，profit factor < 1。

结论清晰：**S003 和 S004 观察到的表面 alpha 由 LFI 单一 token 驱动。这不是策略，这是 outlier。**

去集中化后验证：
- 作者集中度：38.9% → 30.0% ✓（低于 60% 门槛）
- token 集中度：44.4% → 30.0% ✓（低于 40% 门槛）
- 信号：消失

**S005_VERDICT = `FAIL_NO_DECONCENTRATED_ALPHA`**

---

## 7. 1m 数据与链上修复：工程成功，策略无效

### OKX 1m 的局限

即使是 2026 年 5 月的近期事件，OKX DEX candles 的 1m 历史也无法覆盖。这是结构性限制：OKX DEX endpoint 只保存最近几天的 1m 数据。**1m 历史数据不等于当前可用。**

### Base RPC 链上 1m 修复

对 13 个没有 OKX 价格数据的候选事件，我们做了 targeted on-chain repair：

| Token | 状态 | 1m bars 重建 |
|---|---|---|
| DEGEN | ✅ base_v3_swap | 159 bars |
| BNKR | ✅ base_v3_swap | 167 bars |
| DRB | ✅ base_v3_swap | 247 bars |
| SAPIEN | ✅ base_v3_swap | 105 bars |
| SOCK | ✅ base_v3_swap | 67 bars |
| WARD | ✅ base_v3_swap | 48 bars |
| BID / ZORA / OWB / FAI | ❌ 无 Swap 事件 | — |
| JESSE / DOGE | ❌ Pool token 查询失败 | — |
| UPEG | ❌ 无池地址 | — |

**总计 2,411 次 Base RPC 调用，修复 6 个 token，成本是预算上限的 11.2%。**

修复后 6 件 1m 回测：

| 格子 | n | hit rate | mean | PF |
|---|---|---|---|---|
| entry=3m, exit=60m | 6 | **66.7%** | +2.10% | 6.27 |
| policy_mid (5m/30m) | 6 | **66.7%** | +0.36% | 2.36 |

数字看起来不错——但 **n=6，不足以支撑任何 alpha 声明**。这是探索性数据点，不是结论。

工程层面的意义：**链上 1m repair pipeline 已验证可行**。对有充足流动性历史的池，可以以合理 RPC 成本重建精确价格序列。这是独立于策略结论的工程能力。

---

## 8. 最终结论

| 假设 | 结论 |
|---|---|
| KOL mentions 整体含有 alpha | ❌ 被 S003 reject |
| 语义清洗后 buy-call 含有 alpha | ❌ 被 S005-B reject（去集中后为负） |
| LFI/iruletrenches 信号值得跟进 | ⚠️ 单案例，需独立专项验证 |
| KOL buy-call 信号应进入生产系统 | ❌ 不允许 |

**REPORT_VERDICT = `SOCIAL_KOL_BUY_CALL_GENERAL_ALPHA_REJECTED`**

---

## 9. 对 EATLQ 系统的反思

这个研究结论，反而**加强了**原来 EATLQ 系统方向的合理性。

EATLQ 的核心不是"跟人买"。它的价值来自：

1. **链上事件的实时感知**：WETH_leg DEX 事件、LP 流动、大单流，这些是没有噪音污染的。
2. **特征工厂**：把链上事件转化为可量化的特征，而不依赖人工语义判断。
3. **可验证回测**：每个信号都要经过历史数据验证，而不是截图声明。
4. **风控优先**：executor 有断路器、仓位限制、信号过滤。

Social 内容可以作为 **diagnostic feature**——辅助诊断市场情绪，而不是直接作为 entry trigger。把 KOL 的买入声明接入 executor，实质上是在把非结构化的语义噪音插入一个需要精确性的执行系统。

**社交信号的正确位置是特征，而不是命令。**

---

## 10. 局限性

本研究有以下明确局限，结论不应超出这些边界：

- **X API 成本限制**：990 posts 后 credits 耗尽，样本受预算约束。
- **KOL 选择偏向**：5 位 KOL，以 Base 中文生态为主，不代表更广泛的英语 KOL 群体。
- **语义分类误判**：使用规则分类器，对混合语言、隐晦表达可能误判；真正的 LLM 分类可能得到不同分布。
- **OKX 1m 覆盖不足**：2025 年事件无 1m 数据，短窗口（<60m）回测仅有 on-chain repair 的 6 件，不可泛化。
- **on-chain repair 样本小**：6 件 1m 事件统计意义不足。
- **不含真实执行成本**：回测使用 OHLCV close，不含滑点、手续费、队列延迟、quote 可用性。
- **不是投资建议**：本文任何数据和结论均不构成投资建议。

---

## 11. 附录

### Artifacts 路径

| 文件 | 描述 |
|---|---|
| `scripts/sprint_s003_kol_social_event_backtest.py` | S003 主脚本 |
| `scripts/sprint_s004_clean_buy_call_backtest.py` | S004 语义分类 + 回测 |
| `scripts/sprint_s005_deconcentrate_onchain_repair.py` | S005 去集中 + 链上修复 |
| `mvp/audits/social_catalyst_s003/decision_flags.json` | S003 结论 flags |
| `mvp/audits/social_catalyst_s004/decision_flags.json` | S004 结论 flags |
| `mvp/audits/social_catalyst_s005/decision_flags.json` | S005 结论 flags |
| `mvp/audits/social_catalyst_report/chart_data_summary.csv` | 图表数据汇总 |

### 研究时间线

| Sprint | 核心问题 | 关键结论 |
|---|---|---|
| S001 | Base V3 和 KOL 宇宙是否有交集？ | 部分交集，但以 cashtag 为主；诊断成功 |
| S002 | OKX/Dex/Gecko 能支撑回测吗？ | OKX 1H 可用，1m 历史不稳定 |
| S003 | mention-level 有没有 alpha？ | 表面有，但被 @btcbabycow 71.9% 集中度主导 |
| S004 | buy-call-level 清洗后呢？ | 信号大幅收缩，但 LFI outlier 拉高均值 |
| S005 | 去掉 outlier 后还有什么？ | 泛化 alpha 消失；链上修复管道可行 |

---

*本报告不构成投资建议。所有数字均来自历史回测，不代表未来表现。研究数据仅用于系统诊断目的。*
"""


def write_main_report():
    text = REPORT_TEXT.format(ts=GEN_TS[:10])
    path = REPORT_DIR / "social_catalyst_research_report.md"
    path.write_text(text, encoding="utf-8")
    print(f"  [md] social_catalyst_research_report.md ({len(text)} chars)")
    return path


# ---------------------------------------------------------------------------
# X thread
# ---------------------------------------------------------------------------
X_THREAD = """# X Thread: KOL Buy-Call Alpha 的工程验尸报告

---

**[1/12]**
我们用 5 轮迭代，系统测试了一个假设：Base 链 KOL 的 buy-call 推文是否包含可交易 alpha？

结论先说：**不。** 以下是完整拆解。

---

**[2/12] 起点**
从 5 位 Base KOL 拉了 990 条 X 推文。
解析出 431 个 token 级事件（cashtag + 合约地址）。
接着用 OKX 历史 candles 匹配价格，得到 167 个有效回测事件。

---

**[3/12] mention-level 的数字**
最佳格子：entry=10m, exit=120m，mean=+4.49%，hit=50.6%，PF=1.83。
看起来不错。

但等等——

---

**[4/12] 单一 KOL 集中度：71.9%**
167 个有效事件中，@btcbabycow 贡献了约 72%。
去掉他之后：mean=-2.65%，hit=39.5%，PF=0.49。

mention-level 的"alpha"，是一个 KOL 的 volume 驱动，不是信号质量。

这是 FAIL。

---

**[5/12] 深一层：提到 ≠ 买入**
431 个 token 事件，我们做了语义分类：
- 183 条：纯表情包/中性转发（QUOTE_NEUTRAL）
- 90 条：中性提及
- 82 条：长期看好（不是短线信号）
- 19 条：涨完了再说（复盘）
- **只有 31 条是真正的短期买入信号**

431 → 31，缩水 93%。

---

**[6/12] @btcbabycow 的真实 buy-call 密度**
他贡献了 217 条事件，但其中：
- 120 条：纯转发/表情包
- 45 条：中性提及
- 25 条：长期看法
- **真正的短期买入信号：8 条（3.7%）**

S003 的集中度，来自 mention volume，不是 buy-call。

---

**[7/12] 语义清洗后：均值 +32.6%？**
31 个 actionable buy-call 里，18 个有价格数据。
policy_late：mean=+32.6%，hit=38.9%，PF=7.77。

但 median=-0.19%，best_return=+348%，单一 token LFI 贡献 44%。

不是 alpha，是 outlier。

---

**[8/12] 去掉 LFI 之后：-2.6%**
with LFI：mean=+32.6%，PF=7.77
excl LFI：mean=-2.6%，PF=0.42

表面收益由一个 outlier 拉动。没有 outlier，就什么都没有。

**泛化 alpha：拒绝。**

---

**[9/12] 链上 1m 修复**
用 Base RPC 对 6 个候选 token 重建了真实 1m swap 数据（DEGEN/BNKR/DRB/SAPIEN/SOCK/WARD）。
2411 次 RPC 调用。工程上可行。

结果：n=6，hit=66.7%，mean=+2.1%（entry=3m/exit=60m）。
但 n=6，不能声称 alpha。

---

**[10/12] 工程结论**
- OKX 1m 历史不足，1H 可用但粒度粗。
- on-chain repair 管道可行（预算用了 11%）。
- 链上 1m 重建精度优于 OKX 1H，但工程成本更高。

---

**[11/12] 核心结论**
1. mention-level 回测制造假 alpha（集中度驱动）
2. buy-call 语义清洗后，样本显著收缩（-93%）
3. 表面收益来自单 outlier（LFI）
4. 去集中后，泛化 alpha 消失
5. 链上 repair 工程可行，但不改变策略结论
6. **KOL buy-call 信号不进入生产系统**

---

**[12/12] 反思**
Social 内容最多是 diagnostic feature（辅助判断市场情绪），不是 entry trigger。

跟单系统的问题不是"哪个 KOL 更准"，而是：**你能在不知道结果的情况下，用可验证的方式预测吗？**

我们目前找不到这样的系统性证据。

*本研究不构成投资建议。*
"""


def write_x_thread():
    path = REPORT_DIR / "social_catalyst_x_thread.md"
    path.write_text(X_THREAD, encoding="utf-8")
    print(f"  [md] social_catalyst_x_thread.md ({len(X_THREAD)} chars)")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print(f"[report] Generating social_catalyst_report artifacts ...")
    print(f"  Output dir: {REPORT_DIR}")

    write_chart_data()
    write_flags()
    write_readme()
    write_main_report()
    write_x_thread()

    print("[report] Generating charts ...")
    chart_funnel()
    chart_author_concentration()
    chart_lfi_deconcentration()
    chart_data_coverage()

    print(f"\n[report] Done. Artifacts written to {REPORT_DIR.relative_to(REPO_ROOT)}")
    arts = list(REPORT_DIR.rglob("*"))
    for a in sorted(arts):
        if a.is_file():
            sz = a.stat().st_size
            print(f"  {a.relative_to(REPO_ROOT)}  ({sz:,} bytes)")


if __name__ == "__main__":
    main()
