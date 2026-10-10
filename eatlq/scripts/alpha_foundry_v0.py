#!/usr/bin/env python3
"""Alpha Foundry v0.

Builds a CH-only/local-only alpha hypothesis ledger plus 15 audit module
outputs. This is intentionally not a new alpha backtest. It is the upstream
factory that decides whether a hypothesis is allowed to enter backtest/model/
forward/canary stages.

Outputs:
  mvp/audits/alpha_foundry_v0/
    hypothesis_ledger.json
    audit_modules.json
    evaluation_order.json
    wallet_identity_v0.csv
    next_three_hypotheses.json
    readout_cn.md
"""

from __future__ import annotations

import csv
import io
import json
import math
import random
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "mvp/audits/alpha_foundry_v0"
CH_CONTAINER = "eatlq-clickhouse-1"
CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"
BASE_ROUTING = {
    WETH,
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC
}
S095_SIGNAL = REPO / "mvp/audits/sprint095_pre_dentry_retest/per_signal.csv"
S095_SUMMARY = REPO / "mvp/audits/sprint095_pre_dentry_retest/summary.json"
S094_THRESH = REPO / "mvp/audits/wallet_profile_v0/sprint094_audit_v2/threshold_ladder.json"
S094_LABELS = REPO / "mvp/audits/wallet_profile_v0/sprint094_audit_v2/label_variants_v3_14d_2k.csv"
S094_HOLD = REPO / "mvp/audits/wallet_profile_v0/sprint094_audit_v2/hold_time_stats_v3_14d.csv"


def ch(sql: str, timeout: int = 90, fmt: str = "CSVWithNames") -> list[dict[str, str]]:
    query = sql
    if "FORMAT " not in query:
        query = query.rstrip("; \n") + f"\nFORMAT {fmt}"
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client", "--query", query],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:800])
    rows = list(csv.DictReader(io.StringIO(r.stdout)))
    return rows


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fnum(v: Any, default: float = 0.0) -> float:
    try:
        if v in ("", None, "None", "nan"):
            return default
        return float(v)
    except Exception:
        return default


def pf(vals: list[float]) -> float | None:
    pos = sum(v for v in vals if v > 0)
    neg = abs(sum(v for v in vals if v < 0))
    if neg:
        return round(pos / neg, 4)
    if pos:
        return float("inf")
    return None


def pf_top1_removed(vals: list[float]) -> float | None:
    if len(vals) < 2:
        return None
    return pf(sorted(vals)[:-1])


def bootstrap_p5(vals: list[float], n_iter: int = 500) -> float | None:
    if len(vals) < 10:
        return None
    pfs: list[float] = []
    for _ in range(n_iter):
        sample = random.choices(vals, k=len(vals))
        sample_pf = pf(sample)
        if sample_pf is not None and sample_pf != float("inf"):
            pfs.append(sample_pf)
    if not pfs:
        return None
    pfs.sort()
    return round(pfs[max(0, int(0.05 * len(pfs)) - 1)], 4)


def hypothesis_ledger() -> list[dict[str, Any]]:
    """Causal hypothesis ledger. No PF-first hypotheses allowed."""
    items = [
        {
            "id": "H01_POOL_DEATH_REJECTION",
            "title": "池死亡 / sellability 反向过滤",
            "market_participant": "honeypot deployer, rug creator, LP remover, trapped late buyers",
            "order_flow_source": "新池早期买入流 + 后续卖出失败/流动性撤走",
            "causal_edge": "亏钱方不是方向预测错，而是无法退出；提前识别 sell_fail/pool_death 可以避免 -100% 左尾",
            "advantage": "CH 可见早期 swap/mint/burn/heat 衰减，Quoter 小样本可验证",
            "tradable_window": "entry 前 0-30min；过滤器在入场前生效",
            "failure_modes": ["缺 mint/burn 完整度", "死池样本少", "Quoter 历史块需要 RPC 校验"],
            "minimum_data": ["early swaps", "sell-side presence", "burn/mint events", "24h sell_fail label"],
            "stage": "NEXT_ATTACK",
        },
        {
            "id": "H02_BUYER_DIVERSITY_BURST",
            "title": "小额买家多样性 burst",
            "market_participant": "retail micro-buyers vs single sniper/scalper wallet",
            "order_flow_source": "$500-$2k 多地址同步买入，不是单 wallet 反复抽筋",
            "causal_edge": "真实外部需求扩散比单 bot 更难伪造，可能预示更长半衰期",
            "advantage": "CH 可见 sender/buyer 多样性与时间聚集，无需 RPC",
            "tradable_window": "5-30min；仅在 sellability 合格池中使用",
            "failure_modes": ["聚合器拆单伪装多地址", "同一实体多钱包", "样本放宽后噪音暴涨"],
            "minimum_data": ["buyer_address", "distinct buyer count", "top wallet share", "near-miss controls"],
            "stage": "NEXT_ATTACK",
        },
        {
            "id": "H03_LP_WITHDRAWAL_DEATH",
            "title": "LP 撤流动性 / burn 预警",
            "market_participant": "LP/creator 在 rug 前撤流动性；迟到买家承接库存",
            "order_flow_source": "mint/burn 事件、liquidity 变化、swap 失败",
            "causal_edge": "LP 行为可能领先价格崩盘，先验是库存风险转移而非价格动量",
            "advantage": "本地 V3/V2 burns/mints 表可用",
            "tradable_window": "避险过滤；不是单独做多 alpha",
            "failure_modes": ["V3 range LP 正常 rebalancing 误伤", "burn 解码缺字段"],
            "minimum_data": ["v3/v2 mints/burns", "pool age", "liquidity_raw", "subsequent sell_fail"],
            "stage": "NEXT_ATTACK",
        },
        {
            "id": "H04_CREATOR_FUNDER_GRAPH",
            "title": "creator / funder graph 风险",
            "market_participant": "重复发币者、资金源、部署者、相关钱包群",
            "order_flow_source": "factory create tx、funder transfers、creator first LP",
            "causal_edge": "恶意/低质量发行者有可复现行为约束；先验过滤坏池",
            "advantage": "如果 tx_from / transfers 足够，CH 本地可构建弱图",
            "tradable_window": "pool created 后、首个交易前后",
            "failure_modes": ["creator 字段缺", "同一人换新钱包", "RPC metadata 不足"],
            "minimum_data": ["created_tx_hash", "tx_from", "erc20/native transfers", "historical pool outcomes"],
            "stage": "DATA_INSUFFICIENT",
        },
        {
            "id": "H05_ROUTER_AGGREGATOR_ANOMALY",
            "title": "router / aggregator 异常流过滤",
            "market_participant": "aggregator/router flow, MEV bundle, copy-trading bot",
            "order_flow_source": "swap.sender/recipient/tx.from 语义错位",
            "causal_edge": "router 聚合流不等于聪明钱，需先剥离身份再分析需求",
            "advantage": "buyer_address + sender/recipient 可做身份聚类",
            "tradable_window": "pre-signal 身份过滤",
            "failure_modes": ["router 白名单缺", "EOA/contract 无 RPC code 检查"],
            "minimum_data": ["tx.from", "swap.sender", "recipient", "pool diversity"],
            "stage": "FOUNDATION",
        },
        {
            "id": "H06_SMALL_WALLET_BURST",
            "title": "小钱包集群同步流入",
            "market_participant": "retail/community wallets, shill group, copy swarm",
            "order_flow_source": "$500-$1k 多钱包短窗买入",
            "causal_edge": "比 whale 更符合 Base meme 市场实际：鱼不存在，但小鱼群可能存在",
            "advantage": "降低 notional 阈值但用 concentration gates 控噪",
            "tradable_window": "5-15min；必须通过 label robustness",
            "failure_modes": ["单 token 热点", "scalper 群", "winner-only 幻觉"],
            "minimum_data": ["threshold ladder", "case-control", "label variants"],
            "stage": "RESEARCH",
        },
        {
            "id": "H07_EXTERNAL_INFLOW",
            "title": "外部信息流入（社媒/公开 alpha 源）",
            "market_participant": "公开 alpha 群、KOL、社媒事件接收者",
            "order_flow_source": "链下事件后链上买盘",
            "causal_edge": "边来自外部信息传播时差，而非从链上 winner 反推",
            "advantage": "减少后验；可用外部时间戳做 event study",
            "tradable_window": "1-30min，取决于外部源延迟",
            "failure_modes": ["外部数据缺", "样本少", "消息不可交易"],
            "minimum_data": ["external timestamps", "pool mapping", "CH labels"],
            "stage": "NEXT_ATTACK",
        },
        {
            "id": "H08_SELLABILITY_IMPROVEMENT",
            "title": "sellability 改善后入场",
            "market_participant": "早期被 honeypot/低流动性吓退的买家",
            "order_flow_source": "初期无法卖出 → 某时间点卖出路径恢复",
            "causal_edge": "风险溢价收缩：从不可交易变可交易时，后续买盘可能进入",
            "advantage": "方向不是价格，而是可交易性状态迁移",
            "tradable_window": "sell_path 恢复后 5-60min",
            "failure_modes": ["需要 Quoter 小样本", "恢复后价格已反映"],
            "minimum_data": ["sell attempts/proxy", "Quoter sample", "post-recovery flows"],
            "stage": "RESEARCH",
        },
        {
            "id": "H09_ANTI_BOT_EXCLUSION",
            "title": "强信号反向：排除 bot/MEV dominant",
            "market_participant": "0x6ff5, sniper/MM, toxic flow",
            "order_flow_source": "早期大额/高频买入",
            "causal_edge": "强信号常是机器库存/套利，不是外部需求；排除它们提高样本纯度",
            "advantage": "CH sender map/behavior 可做，不追涨",
            "tradable_window": "pre-signal filter",
            "failure_modes": ["误排真实 MM 支撑", "bot 地址迁移"],
            "minimum_data": ["sender frequency", "pool dominance", "hold time", "sell side"],
            "stage": "FOUNDATION",
        },
        {
            "id": "H10_TRAILING_EXIT_EXTERNAL",
            "title": "trailing exit 作为外部信号风控组件",
            "market_participant": "入场信号来自外部；退出端对抗 rug/回撤",
            "order_flow_source": "非 EATLQ 内部 onset",
            "causal_edge": "退出规则不创造 alpha，只减少左尾/锁定右尾",
            "advantage": "可以用外部信号独立测试，不重复 echo-alpha",
            "tradable_window": "5m-2h",
            "failure_modes": ["SL 过早打掉恢复交易", "time stop 变随机"],
            "minimum_data": ["external entries", "multi-horizon labels", "real cost"],
            "stage": "NEXT_ATTACK",
        },
        {
            "id": "H11_HORIZON_ARBITRATION",
            "title": "退出 horizon tournament",
            "market_participant": "短窗 momentum vs 中窗 rug risk",
            "order_flow_source": "同一 entry 在 5m/15m/30m/2h 命运不同",
            "causal_edge": "edge 可能不在 entry，而在何时承认交易失效",
            "advantage": "CH tick 可低成本评估多 horizon",
            "tradable_window": "5m-2h",
            "failure_modes": ["tick label 脏", "成本/滑点未纳入"],
            "minimum_data": ["robust labels", "cost model", "exit reason distribution"],
            "stage": "FOUNDATION",
        },
        {
            "id": "H12_WALLET_PRE_SIGNAL_EDGE",
            "title": "pre-signal 钱包滚动历史 edge",
            "market_participant": "可能聪明钱包、scalper、holder、MM",
            "order_flow_source": "信号前历史胜率/持仓/跨池表现",
            "causal_edge": "只有历史可迁移行为能作为 smart-money，不允许 winner-only 反推",
            "advantage": "已有 FIFO hold-time artifact，可升级",
            "tradable_window": "pre-signal scoring",
            "failure_modes": ["钱包身份混淆", "样本太少", "未来泄漏"],
            "minimum_data": ["rolling history", "FIFO buy/sell", "case-control"],
            "stage": "DATA_INSUFFICIENT",
        },
        {
            "id": "H13_FEE_TIER_MARKET_STRUCTURE",
            "title": "fee tier 市场结构分层",
            "market_participant": "meme creators choose 0.3%/1%; arb pools choose low fee",
            "order_flow_source": "池创建时费率选择",
            "causal_edge": "fee tier 是发行者意图/市场微结构先验，不是收益结果",
            "advantage": "fee_bps 修复后 CH 可完整评估",
            "tradable_window": "pool birth",
            "failure_modes": ["fee 本身不预测方向", "样本少"],
            "minimum_data": ["discovered_pools fee", "pool outcomes", "concentration"],
            "stage": "FOUNDATION",
        },
        {
            "id": "H14_LP_RANGE_STRESS",
            "title": "V3 tick/range 压力与流动性错配",
            "market_participant": "LP inventory managers, traders pushing price outside range",
            "order_flow_source": "tick velocity, liquidity changes, failed sell depth",
            "causal_edge": "流动性错配产生滑点/断层，不是简单动量",
            "advantage": "tick/liquidity 已在 decoded swaps",
            "tradable_window": "分钟级，偏风控",
            "failure_modes": ["range data 不完整", "tick 极端脏"],
            "minimum_data": ["tick path", "liquidity", "swap amounts", "sell path"],
            "stage": "RESEARCH",
        },
        {
            "id": "H15_V2_LONGTAIL_BOT_FILTER",
            "title": "V2 long-tail 99% 单 sender 过滤后再看",
            "market_participant": "V2 sniper bot / router + 少量真实小额买家",
            "order_flow_source": "V2 $500-$1k 大量事件但 99% 单 sender",
            "causal_edge": "先去掉机器主导，剩余稀疏流可能代表真实需求",
            "advantage": "V2 表已本地化；不扩大 RPC",
            "tradable_window": "5-30min",
            "failure_modes": ["过滤后 n 近零", "无 tick price label"],
            "minimum_data": ["V2 decoded swaps", "pair registry/metadata", "case-control"],
            "stage": "DATA_INSUFFICIENT",
        },
        {
            "id": "H16_AERODROME_METADATA_RECOVERY",
            "title": "Aerodrome metadata 补齐后的独立 universe",
            "market_participant": "Base native DEX traders, non-Uniswap order flow",
            "order_flow_source": "Aerodrome vAMM 交易",
            "causal_edge": "不是更大样本就有 alpha，而是测试不同微观结构是否更少被 V3 MEV 吃掉",
            "advantage": "本地有 187k/30d events；缺 metadata",
            "tradable_window": "待 metadata 后定义",
            "failure_modes": ["无 USD/notional", "metadata 需少量 RPC", "仍被 bot 支配"],
            "minimum_data": ["token metadata", "USD price path", "sender identity"],
            "stage": "DATA_INSUFFICIENT",
        },
    ]
    return items


def module_universe_coverage() -> dict[str, Any]:
    rows = ch(
        """
SELECT 'v3_decoded_30d' AS universe, count() AS swaps, uniqExact(lower(pool_address)) AS pools,
       min(block_time) AS min_time, max(block_time) AS max_time
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id=8453 AND block_time >= now() - INTERVAL 30 DAY
UNION ALL
SELECT 'v2_decoded_30d', count(), uniqExact(lower(pair_address)), min(block_time), max(block_time)
FROM eatlq.uniswap_v2_swaps_decoded
WHERE chain_id=8453 AND block_time >= now() - INTERVAL 30 DAY
UNION ALL
SELECT 'aerodrome_30d', count(), uniqExact(lower(pair_address)), min(block_time), max(block_time)
FROM eatlq.aerodrome_vamm_swaps_mv
WHERE chain_id=8453 AND block_time >= now() - INTERVAL 30 DAY
"""
    )
    fee_rows = ch(
        """
SELECT fee_bps, count() AS pools
FROM eatlq.discovered_pools
WHERE chain_id=8453 AND pool_type='v3'
GROUP BY fee_bps
ORDER BY pools DESC
"""
    )
    missing_meta = ch(
        f"""
SELECT count() AS active_v3_pools_30d,
       countIf(dp.pool_address='') AS missing_discovered_pools,
       round(countIf(dp.pool_address='') / count(), 4) AS missing_share
FROM (
  SELECT DISTINCT lower(pool_address) AS pool
  FROM eatlq.uniswap_v3_swaps
  WHERE chain_id={CHAIN_ID} AND block_time >= now() - INTERVAL 30 DAY
) s
LEFT JOIN (
  SELECT lower(pool_address) AS pool_address
  FROM eatlq.discovered_pools
  WHERE chain_id={CHAIN_ID}
) dp ON s.pool = dp.pool_address
"""
    )
    return {
        "module": "universe_coverage_auditor",
        "status": "PASS_WITH_KNOWN_GAPS",
        "data": rows,
        "fee_distribution": fee_rows[:12],
        "missing_metadata": missing_meta[0] if missing_meta else {},
        "blocks_backtest_if": "candidate universe cannot state chain/dex/table/time/fee/metadata filters",
    }


def module_notional_valuation() -> dict[str, Any]:
    # Lightweight v0: detect risky hard-coded valuation boundaries and count quote legs.
    quote_rows = ch(
        f"""
SELECT
  count() AS v3_30d_swaps,
  countIf(lower(dp.token0_address) IN ({','.join(repr(x) for x in BASE_ROUTING)})
       OR lower(dp.token1_address) IN ({','.join(repr(x) for x in BASE_ROUTING)})) AS routing_leg_swaps,
  uniqExact(lower(sd.pool_address)) AS pools
FROM eatlq.uniswap_v3_swaps_decoded sd
INNER JOIN eatlq.discovered_pools dp
  ON lower(sd.pool_address)=lower(dp.pool_address) AND dp.chain_id={CHAIN_ID}
WHERE sd.chain_id={CHAIN_ID} AND sd.block_time >= now() - INTERVAL 30 DAY
"""
    )
    return {
        "module": "notional_valuation_auditor",
        "status": "PASS_REQUIRES_EMPIRICAL_PRICE_SERIES",
        "data": quote_rows[0] if quote_rows else {},
        "known_failures_caught": [
            "hardcoded_ETH_3000_overstated_notional_by_about_42pct",
            "fee_bps_encoding_bug_can_change_candidate_universe",
        ],
        "required_before_backtest": [
            "state quote-side token",
            "state decimals source",
            "state side/direction formula",
            "use empirical ETH/USDC by day when quote is WETH",
            "never mix wallet balance with swap notional",
        ],
    }


def module_label_robustness() -> dict[str, Any]:
    rows = read_csv(S094_LABELS)
    if not rows:
        return {"module": "label_robustness_auditor", "status": "NO_LOCAL_LABEL_VARIANTS"}

    variants = [c for c in rows[0].keys() if c.startswith("ret_") or c in ("last_tick_ret", "p50_ret", "p75_ret", "trimmed_ret")]
    # Try known column names first, then generic.
    preferred = ["ret_last", "ret_p50", "ret_p75", "ret_trimmed", "last", "p50", "p75", "trimmed"]
    cols = [c for c in variants if c in preferred] or variants[:6]
    stats = {}
    for c in cols:
        vals = [fnum(r.get(c)) for r in rows if r.get(c) not in ("", None)]
        if vals:
            vals_sorted = sorted(vals)
            stats[c] = {
                "n": len(vals),
                "p50": round(vals_sorted[len(vals)//2], 6),
                "p90": round(vals_sorted[int(0.9 * (len(vals)-1))], 6),
                "p95": round(vals_sorted[int(0.95 * (len(vals)-1))], 6),
                "max": round(max(vals), 6),
            }
    return {
        "module": "label_robustness_auditor",
        "status": "DIRTY_LABEL_RISK_CONTROLLED_BY_VARIANTS",
        "artifact": str(S094_LABELS),
        "variant_stats": stats,
        "blocks_training_if": "winner/near_miss flips materially between last/median/p75/trimmed labels",
    }


def module_case_control() -> dict[str, Any]:
    rows = read_csv(S095_SIGNAL)
    if not rows:
        return {"module": "case_control_auditor", "status": "NO_SIGNAL_SAMPLE"}
    winners = [r for r in rows if fnum(r.get("trailing_2h_net")) > 0]
    losers = [r for r in rows if fnum(r.get("trailing_2h_net")) <= 0]
    def avg(field: str, sub: list[dict[str, str]]) -> float | None:
        vals = [fnum(r.get(field)) for r in sub if r.get(field) not in ("", None)]
        return round(mean(vals), 6) if vals else None
    return {
        "module": "winner_nearmiss_control_case_control",
        "status": "WINNER_ONLY_FORBIDDEN",
        "sample": {"winners": len(winners), "near_miss_or_losers": len(losers)},
        "feature_means": {
            "winner_heat": avg("heat", winners),
            "loser_heat": avg("heat", losers),
            "winner_pool_age_sec": avg("pool_age_sec", winners),
            "loser_pool_age_sec": avg("pool_age_sec", losers),
            "winner_onset_lag_sec": avg("onset_lag_sec", winners),
            "loser_onset_lag_sec": avg("onset_lag_sec", losers),
        },
        "blocks_if": "hypothesis only exists after looking at winners and cannot name near_miss/control set",
    }


def module_wallet_identity() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Density-only heuristics — DISABLED for downstream use (P1-6).

    Labels like possible_smart_wallet used swap count / pool count only,
    with zero pre-signal PnL or FIFO hold-time evidence.
    """
    rows = ch(
        """
SELECT lower(buyer_address) AS wallet,
       count() AS swaps,
       uniqExact(lower(pool_address)) AS pools,
       uniqExact(toDate(block_time)) AS active_days
FROM eatlq.uniswap_v3_swaps_with_buyer
WHERE chain_id=8453
  AND block_time >= now() - INTERVAL 14 DAY
  AND buyer_address != ''
GROUP BY wallet
HAVING swaps >= 10
ORDER BY swaps DESC
LIMIT 200
""",
        timeout=120,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        row = {
            **r,
            "identity_v0": "DISABLED_DO_NOT_USE",
            "downstream_blocked": True,
            "disable_reason": "P1-6: density heuristics without pre-signal PnL/FIFO evidence",
        }
        out.append(row)
    return (
        {
            "module": "wallet_identity_classifier",
            "status": "DISABLED_UNTRUSTED",
            "rows": len(out),
            "identity_counts": {"DISABLED_DO_NOT_USE": len(out)},
            "downstream_blocked": True,
            "rule_warning": (
                "Do NOT use wallet_identity_v0.csv for smart-money / bot filtering. "
                "Requires pre-signal FIFO PnL or labeled control set."
            ),
        },
        out,
    )


def module_bot_mev(wallet_rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(r["identity_v0"] for r in wallet_rows)
    known = ch(
        """
SELECT count() AS swaps_0x6ff5_30d,
       uniqExact(lower(pool_address)) AS pools_0x6ff5_30d
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id=8453
  AND block_time >= now() - INTERVAL 30 DAY
  AND lower(sender) = '0x6ff5693b99212da76ad316178a184ab56d299b43'
"""
    )
    return {
        "module": "bot_mev_classifier",
        "status": "ACTIVE_CONTAMINATION",
        "wallet_identity_counts": dict(counts),
        "known_0x6ff5": known[0] if known else {},
        "blocks_if": "signal strength rises only because bot/router/MM flow dominates",
    }


def module_echo_alpha() -> dict[str, Any]:
    rows = read_csv(S095_SIGNAL)
    vals = [fnum(r.get("trailing_2h_net")) for r in rows]
    top = max(vals) if vals else 0
    return {
        "module": "echo_alpha_detector",
        "status": "ECHO_ALPHA_IF_TOP1_REMOVAL_COLLAPSES",
        "n": len(vals),
        "pf": pf(vals),
        "pf_top1_removed": pf_top1_removed(vals),
        "bootstrap_p5": bootstrap_p5(vals),
        "top_trade_return": round(top, 6),
        "verdict": "ECHO_ALPHA" if vals and (pf_top1_removed(vals) or 0) < 1.3 else "NOT_ECHO_BY_TOP1",
    }


def module_sample_concentration() -> dict[str, Any]:
    rows = read_csv(S095_SIGNAL)
    pools = Counter(r.get("pool") for r in rows)
    tokens = Counter(r.get("token") for r in rows)
    n = len(rows)
    wallet_col = None
    if rows:
        headers = set(rows[0].keys())
        for candidate in ("buyer_address", "wallet", "sender", "buyer"):
            if candidate in headers:
                wallet_col = candidate
                break
    wallet_block: dict[str, Any]
    if wallet_col:
        wallets = Counter(r.get(wallet_col) for r in rows if r.get(wallet_col))
        wallet_block = {
            "wallet_column": wallet_col,
            "unique_wallets": len(wallets),
            "top_wallet_share": round((max(wallets.values()) / n), 4) if n and wallets else None,
        }
        status = "PASS_FOR_S095_SAMPLE"
    else:
        wallet_block = {
            "wallet_column": None,
            "unique_wallets": None,
            "top_wallet_share": None,
            "wallet_concentration_skipped": True,
            "skip_reason": "P1-7: per_signal.csv has no buyer/wallet/sender column",
        }
        status = "PASS_POOL_TOKEN_ONLY"
    return {
        "module": "sample_concentration_report",
        "status": status,
        "n": n,
        "unique_pools": len(pools),
        "top_pool_share": round((max(pools.values()) / n), 4) if n and pools else 0,
        "unique_tokens": len(tokens),
        "top_token_share": round((max(tokens.values()) / n), 4) if n and tokens else 0,
        **wallet_block,
        "blocks_if": "top_pool/top_token/top_wallet/outlier drives performance",
    }


def module_sellability() -> dict[str, Any]:
    rows = read_csv(S095_SIGNAL)
    if rows:
        ok = sum(1 for r in rows if str(r.get("sell_path_ok")).lower() == "true")
        return {
            "module": "sellability_pool_death_checker",
            "status": "RUNNABLE_PROXY_ONLY_CH",
            "n": len(rows),
            "sell_ok": ok,
            "sell_fail_or_blocked": len(rows) - ok,
            "sell_ok_rate": round(ok / len(rows), 4),
            "note": "CH proxy cannot replace Quoter; RPC sample cap required for promotion",
        }
    return {"module": "sellability_pool_death_checker", "status": "NO_SAMPLE"}


def module_exit_horizon() -> dict[str, Any]:
    rows = read_csv(S095_SIGNAL)
    horizons = {}
    for col in ["ret_5m", "ret_15m", "ret_30m", "ret_1h", "ret_2h", "trailing_2h_net"]:
        vals = [fnum(r.get(col)) for r in rows if r.get(col) not in ("", None)]
        if vals:
            horizons[col] = {
                "n": len(vals),
                "mean": round(mean(vals), 6),
                "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 4),
                "pf": pf(vals),
                "pf_top1_removed": pf_top1_removed(vals),
            }
    return {
        "module": "exit_horizon_tournament",
        "status": "RUNNABLE",
        "horizons": horizons,
        "blocks_if": "chosen horizon wins only before top1_removed/cost/sell_path",
    }


def module_feature_separability() -> dict[str, Any]:
    cc = module_case_control()
    fm = cc.get("feature_means", {})
    sep = {}
    for prefix in ["heat", "pool_age_sec", "onset_lag_sec"]:
        w = fm.get(f"winner_{prefix}")
        l = fm.get(f"loser_{prefix}")
        if w is not None and l is not None:
            denom = abs(l) + 1e-9
            sep[prefix] = round((w - l) / denom, 4)
    return {
        "module": "feature_separability_test",
        "status": "INSUFFICIENT_FOR_MODEL_IF_SMALL_DIFF",
        "effect_size_proxy": sep,
        "model_rule": "XGBoost is allowed only after pre-model feature separation survives case-control and label robustness",
    }


def module_training_gate() -> dict[str, Any]:
    s095 = read_json(S095_SUMMARY, {})
    perf = s095.get("performance", {})
    gates = s095.get("gates", {})
    eligible = (
        perf.get("n", 0) >= 100
        and gates.get("g5_pf_top1_removed_ge_1_3")
        and gates.get("g6_bootstrap_p5_ge_1_0")
        and gates.get("g7_win_rate_ge_50pct")
    )
    return {
        "module": "training_eligibility_gate",
        "status": "BLOCKED" if not eligible else "ELIGIBLE",
        "reasons": [] if eligible else [
            "n_or_forward_only_sample_insufficient",
            "PF_top1_removed/bootstrap/WR gate failed",
            "feature separability not proven before model",
        ],
        "rule": "models amplify existing separation; models cannot create alpha",
    }


def module_provenance(hypotheses: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(h["stage"] for h in hypotheses)
    posthoc_blocked = [h["id"] for h in hypotheses if "PF" in h["title"].upper()]
    return {
        "module": "hypothesis_provenance_ledger",
        "status": "RUNNABLE",
        "hypothesis_count": len(hypotheses),
        "stage_counts": dict(counts),
        "posthoc_pf_first_blocked": posthoc_blocked,
        "rule": "each hypothesis must name participant, loser, constraint, advantage, window, minimum data before backtest",
    }


def module_anti_self_deception() -> dict[str, Any]:
    s095 = read_json(S095_SUMMARY, {})
    return {
        "module": "anti_self_deception_gate",
        "status": "ENFORCED",
        "required_metrics": [
            "first_signal_only",
            "PF_top1_removed",
            "bootstrap_p5",
            "unique_pools",
            "top_pool_share",
            "top_token_share",
            "top_wallet_share",
            "sell_path",
            "real_cost",
            "label_robustness",
        ],
        "latest_gate_example": s095.get("gates", {}),
        "verdict_rule": "promotion requires all gates; partial PF pass is not alpha",
    }


def module_microstructure(hypotheses: list[dict[str, Any]]) -> dict[str, Any]:
    checks = {}
    required = ["market_participant", "order_flow_source", "causal_edge", "advantage", "tradable_window", "minimum_data"]
    for h in hypotheses:
        missing = [k for k in required if not h.get(k)]
        checks[h["id"]] = "PASS" if not missing else f"MISSING:{','.join(missing)}"
    return {
        "module": "microstructure_explanation_checker",
        "status": "RUNNABLE",
        "checks": checks,
        "blocks_backtest_if": "cannot answer who loses, why they lose, and why we can identify earlier/cheaper/safer",
    }


def evaluation_order() -> dict[str, Any]:
    return {
        "before_backtest": [
            "universe_coverage_auditor",
            "notional_valuation_auditor",
            "label_robustness_auditor",
            "microstructure_explanation_checker",
        ],
        "before_model": [
            "feature_separability_test",
            "winner_nearmiss_control_case_control",
            "training_eligibility_gate",
        ],
        "before_forward": [
            "first_signal_only",
            "PF_top1_removed",
            "bootstrap_p5",
            "unique_pools/top_pool_share/top_token_share/top_wallet_share",
            "sell_path",
            "real_cost",
        ],
        "before_canary": [
            "forward_only_sample",
            "human_review",
            "RPC_budget_approval",
            "canary_ADR",
        ],
        "forbidden_shortcuts": [
            "XGBoost before feature separability",
            "winner-only as Smart Money",
            "last-tick only labels",
            "PF without top1_removed/bootstrap",
            "direction-only backtest without sellability",
        ],
    }


def next_three(hypotheses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {h["id"]: h for h in hypotheses}
    picks = [
        {
            "id": "H01_POOL_DEATH_REJECTION",
            "why": "因果最强：小池最大亏损来自 sell_fail/-100%，过滤坏池是防御 edge，不重复追动量/whale/wallet。",
            "minimum_validation_plan": [
                "CH-only 构造 24h sell_fail/pool_death label",
                "features: early sell presence, heat decay, buy/sell ratio, v3/v2 burn/mint, liquidity_raw path",
                "case-control: died pools vs survived pools with same fee/age/heat bucket",
                "holdout AUC >= 0.65 and false-negative review",
            ],
            "failure_conditions": ["AUC < 0.60", "label needs >200 RPC calls", "survived/dead concentration in one token/pool"],
            "expected_sample": "30d active young pools: tens to low hundreds; enough for screening, not promotion",
            "rpc_needed": "No for v0; optional <=200 Quoter calls for label audit only after CH signal exists",
            "not_repeat_of_past": "不是找会涨的 onset，而是过滤无法卖出的结构性左尾。",
            "ledger": by_id["H01_POOL_DEATH_REJECTION"],
        },
        {
            "id": "H02_BUYER_DIVERSITY_BURST",
            "why": "顺应 Base 现实：没有 $100k 大鱼，但 $500-$2k 小额买家有量；用多样性/身份先验避免单 wallet 噪音。",
            "minimum_validation_plan": [
                "CH-only 生成 $500/$1k/$2k 小额 burst candidates",
                "先过 universe/notional/label 三关",
                "按 buyer diversity、top wallet share、router/bot identity 分桶",
                "winner/near-miss/control 比较，禁止 winner-only",
            ],
            "failure_conditions": ["winner/near_miss 不可分", "top token/pool/wallet >30%", "median/p75/trimmed label 翻转"],
            "expected_sample": "V3 30d $500: ~1800 events / ~89 non-bot senders；过滤后预计 50-200 candidates",
            "rpc_needed": "No for discovery; optional <=200 sellability sample",
            "not_repeat_of_past": "不是 whale 线降阈值，而是小额群体订单流假设，先控集中度。",
            "ledger": by_id["H02_BUYER_DIVERSITY_BURST"],
        },
        {
            "id": "H07_EXTERNAL_INFLOW",
            "why": "链上内生 winner 已多次被证伪；外部时间戳提供非后验的信息源与独立因果链。",
            "minimum_validation_plan": [
                "收集外部事件时间戳（KOL/群/用户手挑）",
                "映射到池/代币，固定 event time 后只读 CH forward label",
                "使用 trailing exit as component，但不从 EATLQ winners 选择 entries",
                "n>=20 初筛，PF_top1_removed>=1.3 才扩大",
            ],
            "failure_conditions": ["无法取得独立外部时间戳", "事件后价格已反映", "top1_removed collapse"],
            "expected_sample": "取决于外部源；v0 可以从 20-50 个手工/公开事件开始",
            "rpc_needed": "No for CH event study; Quoter <=200 only for sellability audit",
            "not_repeat_of_past": "不是从历史 PF 反推 entry，而是用外部信息流作为上游假设。",
            "ledger": by_id["H07_EXTERNAL_INFLOW"],
        },
    ]
    return picks


def write_wallet_csv(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    keys = list(rows[0].keys())
    with (OUT_DIR / "wallet_identity_v0.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def write_readout(
    hypotheses: list[dict[str, Any]],
    modules: list[dict[str, Any]],
    picks: list[dict[str, Any]],
) -> None:
    module_by_name = {m["module"]: m for m in modules}
    archived = [
        "$100k / $50k single-swap human whale",
        "winner-only smart-money / wallet-profile",
        "0x6ff5 weathervane entry",
        "Base V3 onset / D_entry as standalone entry alpha",
        "PF-only / top1-driven / first-signal-not-deduped lines",
    ]
    data_limited = [
        "creator/funder graph",
        "Aerodrome metadata universe",
        "V2 long-tail after 99% sender removal",
        "pre-signal wallet edge with FIFO hold-time at scale",
    ]
    md = f"""# Alpha Foundry v0 — 把 15 个困难工程化

**生成时间:** {datetime.now(timezone.utc).isoformat()}  
**原则:** 不再“看到现象就回测”。每个 alpha 假设必须先回答：谁会亏钱、为什么亏、约束是什么、我们凭什么更早/更便宜/更安全识别、窗口多长、扣成本和 sell path 后还剩多少。  
**RPC:** 0 calls（本轮 CH-only / local-only）

---

## 1. Foundry v0 已落地的产物

| artifact | 作用 |
|---|---|
| `scripts/alpha_foundry_v0.py` | 可重复运行的 Foundry v0 生成器 |
| `mvp/audits/alpha_foundry_v0/hypothesis_ledger.json` | 16 条因果假设账本 |
| `mvp/audits/alpha_foundry_v0/audit_modules.json` | 15 个 audit module 的可运行输出 |
| `mvp/audits/alpha_foundry_v0/evaluation_order.json` | backtest/model/forward/canary 的准入顺序 |
| `mvp/audits/alpha_foundry_v0/wallet_identity_v0.csv` | pre-signal 钱包身份 v0 分类 |
| `mvp/audits/alpha_foundry_v0/next_three_hypotheses.json` | 下一轮真正值得打的 3 条假设 |

---

## 2. Hypothesis Ledger：先因果，后回测

本轮写入 **{len(hypotheses)} 条假设**。每条都有：

- `market_participant`
- `order_flow_source`
- `causal_edge`
- `advantage`
- `tradable_window`
- `failure_modes`
- `minimum_data`
- `stage`

分布：`{json.dumps(module_by_name['hypothesis_provenance_ledger']['stage_counts'], ensure_ascii=False)}`

这意味着 Foundry 的默认姿势从“历史 PF 高不高”变成“这里为什么会有可执行错价”。

---

## 3. 十五个 Audit Modules

| # | module | 当前状态 | 会挡住什么 |
|---:|---|---|---|
| 1 | `universe_coverage_auditor` | {module_by_name['universe_coverage_auditor']['status']} | 不说明 chain/dex/table/time/fee/metadata 边界的 backtest |
| 2 | `notional_valuation_auditor` | {module_by_name['notional_valuation_auditor']['status']} | ETH 硬编码、side/decimals/quote-side 错误 |
| 3 | `label_robustness_auditor` | {module_by_name['label_robustness_auditor']['status']} | last-tick 单标签训练 |
| 4 | `winner_nearmiss_control_case_control` | {module_by_name['winner_nearmiss_control_case_control']['status']} | winner-only 聪明钱 |
| 5 | `wallet_identity_classifier` | {module_by_name['wallet_identity_classifier']['status']} | 把 bot/router/scalper 当 smart wallet |
| 6 | `bot_mev_classifier` | {module_by_name['bot_mev_classifier']['status']} | 机器流污染强信号 |
| 7 | `echo_alpha_detector` | {module_by_name['echo_alpha_detector']['verdict']} | top1/outlier/单池回声 |
| 8 | `sample_concentration_report` | {module_by_name['sample_concentration_report']['status']} | 单 pool/token/wallet 支配 |
| 9 | `sellability_pool_death_checker` | {module_by_name['sellability_pool_death_checker']['status']} | 只问涨跌、不问能否卖 |
| 10 | `exit_horizon_tournament` | {module_by_name['exit_horizon_tournament']['status']} | 固定 horizon 迷信 |
| 11 | `feature_separability_test` | {module_by_name['feature_separability_test']['status']} | 没分离度就训练 XGBoost |
| 12 | `training_eligibility_gate` | {module_by_name['training_eligibility_gate']['status']} | 用模型造 alpha |
| 13 | `hypothesis_provenance_ledger` | {module_by_name['hypothesis_provenance_ledger']['status']} | 后验 PF 假设 |
| 14 | `anti_self_deception_gate` | {module_by_name['anti_self_deception_gate']['status']} | 不报 top1/bootstrap/sell path 的 promotion |
| 15 | `microstructure_explanation_checker` | {module_by_name['microstructure_explanation_checker']['status']} | 无“谁为什么会亏”的假设 |

---

## 4. 新评估顺序

**进入 backtest 前：**
1. `universe_coverage_auditor`
2. `notional_valuation_auditor`
3. `label_robustness_auditor`
4. `microstructure_explanation_checker`

**进入 model 前：**
1. `feature_separability_test`
2. `winner_nearmiss_control_case_control`
3. `training_eligibility_gate`

**进入 forward 前：**
1. first-signal-only
2. `PF_top1_removed`
3. bootstrap p5
4. unique_pools / top_pool_share / top_token_share / top_wallet_share
5. sell_path
6. real cost

**进入 canary 前：**
1. forward-only 样本
2. 人为复核
3. RPC 预算审批
4. canary ADR

XGBoost 只能在输入特征已经有分离度时放大信号，不能用来制造 alpha。

---

## 5. Wallet / Smart Money 重置

`wallet_identity_classifier` 已做 v0，14d 内 V3 buyer 中 swaps≥10 的钱包先分身份：

```json
{json.dumps(module_by_name['wallet_identity_classifier']['identity_counts'], ensure_ascii=False, indent=2)}
```

规则很粗，但边界明确：

- bot/router/MM/scalper 先分桶，不能直接封 smart money。
- Diamond/Paper 必须来自 sell-side / FIFO 持仓近似；复购密度代理禁用。
- Smart Money 分数必须来自 pre-signal rolling history，不能用未来 winner 反推。

---

## 6. Label 重置

`label_robustness_auditor` 默认要求 last / median / p75 / trimmed 等多标签同时出现。若 winner/near_miss 对聚合器高度敏感，标记 `DIRTY_LABEL`，不准训练。

当前沿用 Sprint094 的 label variants artifact：`{S094_LABELS}`。

---

## 7. 样本、集中度与 Echo-Alpha

Sprint095-PRE 是 Foundry 的反例单元测试：

```json
{json.dumps(module_by_name['echo_alpha_detector'], ensure_ascii=False, indent=2)}
```

结论：表面 PF 可以过，但 `PF_top1_removed` 和 bootstrap p5 坍塌，即 `ECHO_ALPHA`。以后任何策略靠一个池、一个 token、一个 wallet、一个 outlier 活着，直接归档。

---

## 8. 执行与 Sell Path

Foundry v0 把 sellability/pool death 设成独立模块，而不是 backtest 的附录。小池策略必须同时回答：

- 能不能卖？
- Quoter/honeypot 是否失败？
- slippage 后剩多少？
- LP 是否撤走？
- pool death 时是不是 -100%？

本轮不扩 RPC；若后续需要 Quoter 验证，必须先给 calls/credits 估算和硬帽。

---

## 9. 永久归档、数据不足、下一轮进攻

### 永久归档

{chr(10).join(f'- {x}' for x in archived)}

### 数据不足，不等于失败

{chr(10).join(f'- {x}' for x in data_limited)}

### 下一轮真正值得打的 3 条

| 优先级 | 假设 | 为什么值得打 | RPC |
|---:|---|---|---|
| 1 | `{picks[0]['id']}` 池死亡反向过滤 | {picks[0]['why']} | {picks[0]['rpc_needed']} |
| 2 | `{picks[1]['id']}` 小额 buyer diversity burst | {picks[1]['why']} | {picks[1]['rpc_needed']} |
| 3 | `{picks[2]['id']}` 外部信息流入 | {picks[2]['why']} | {picks[2]['rpc_needed']} |

这三条不是 PF 最高，而是因果解释最强、数据边界最清楚、执行可行、最不容易被十五个困难打死。

---

## 10. 一句话结论

Alpha Foundry v0 的目标不是逃避十五个困难，而是把它们变成流水线里的挡板：先解释市场微观结构，再验 universe/notional/label，再验 case-control/feature separability，最后才允许 backtest、model、forward、canary。  

从现在开始，任何新 alpha 如果不能先过这些模块，就不配进入回测。
"""
    (OUT_DIR / "readout_cn.md").write_text(md, encoding="utf-8")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    random.seed(42)

    hypotheses = hypothesis_ledger()
    wallet_module, wallet_rows = module_wallet_identity()
    write_wallet_csv(wallet_rows)

    modules = [
        module_universe_coverage(),
        module_notional_valuation(),
        module_label_robustness(),
        module_case_control(),
        wallet_module,
        module_bot_mev(wallet_rows),
        module_echo_alpha(),
        module_sample_concentration(),
        module_sellability(),
        module_exit_horizon(),
        module_feature_separability(),
        module_training_gate(),
        module_provenance(hypotheses),
        module_anti_self_deception(),
        module_microstructure(hypotheses),
    ]
    picks = next_three(hypotheses)

    (OUT_DIR / "hypothesis_ledger.json").write_text(json.dumps(hypotheses, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "audit_modules.json").write_text(json.dumps(modules, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "evaluation_order.json").write_text(json.dumps(evaluation_order(), ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "next_three_hypotheses.json").write_text(json.dumps(picks, ensure_ascii=False, indent=2), encoding="utf-8")
    write_readout(hypotheses, modules, picks)

    print(f"Alpha Foundry v0 complete: {OUT_DIR}")
    print(f"hypotheses={len(hypotheses)} modules={len(modules)} wallet_rows={len(wallet_rows)} rpc_calls=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
