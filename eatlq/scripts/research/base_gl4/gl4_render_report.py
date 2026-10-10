#!/usr/bin/env python3
"""Render the receipt-first GL4 ledger into the required human tables."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path("/opt/EATLQ")
LEDGER = ROOT / "mvp/diagnostics/base_gl4_live_truth_ledger_20260716.json"
OUT = ROOT / "reports/GL4_live_track_record_reconciliation_20260716.md"
PLAIN = ROOT / "reports/GL4_live_plain_language_20260716.md"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pct(x: float) -> str:
    return f"{100*x:.2f}%"


def weth(x) -> str:
    return "UNKNOWN" if x is None else f"{x:.12f}"


def usd(x) -> str:
    return "UNKNOWN" if x is None else f"${x:,.2f}"


def short(tx: str) -> str:
    return f"`{tx[:10]}…{tx[-8:]}`"


def main() -> None:
    d = json.loads(LEDGER.read_text())
    a = d["aggregate"]
    tx = {x["tx_hash"]: x for x in d["transactions"]}
    closed = {x["position_id"]: x for x in d["closed_roundtrips"]}
    plain = (
        f"这个月两个真钱钱包一共实际买入 {a['cumulative_invested_weth']:.6f} WETH，按每笔买入块上的链上 ETH/USD 约 {usd(a['cumulative_invested_usd_at_buy_oracle'])}；"
        f"其中两组 USDC 受控烟测和一组 NEXIO 真正卖回，三组严格已实现合计亏 {weth(abs(a['closed_realized_pnl_weth_all_associated']))} WETH（约 {usd(abs(a['closed_realized_pnl_usd_receipt_time_cashflow']))}），0/3 获胜。"
        f"其余 JOTCHUA、BOT 和三笔 PARADAENGI 没有卖出 receipt，代币仍完整躺在钱包，但池 WETH 到窗口末都坍塌至少 99%；按窗口末同尺寸链上可执行报价（PARADAENGI 三笔明确 revert 为零）经济净值合计约亏 {weth(abs(a['receipt_plus_end_executable_mark_economic_pnl_weth']))} WETH / {usd(abs(a['receipt_time_oracle_economic_pnl_usd']))}，8 个已判定入口 0 胜。"
        "所以真金亏损的主因不是已观测到的报价—成交滑点，而是 rug/撤池真实兑现，加上 F、trail、仓位规模、模型与 GL3 估计量并不一致；+56% 还能保留为冻结历史样本的报价型有界事实，却不能再叫真钱或前瞻 edge，GL1-02 在这个小样本实盘尺度应判 FAIL。"
    )
    PLAIN.write_text("\n".join([
        "---", "sop_applied: true", "alpha_id: AR-GL4-LIVE-RECON", "family: F0/F1/F4/F7",
        "decision_flags:", "  training_allowed: false", "  live_allowed: false", "  self_go_allowed: false", "---", "",
        "# GL4 人话版", "", plain, "",
    ]))

    lines = [
        "---", "sop_applied: true", "alpha_id: AR-GL4-LIVE-RECON", "family: F0/F1/F4/F7",
        "next_gate: INDEPENDENT_LINE_REVIEW_THEN_NO_GO_LEDGER_INTEGRATION", "decision_flags:",
        "  training_allowed: false", "  live_allowed: false", "  self_go_allowed: false", "---", "",
        "# GL4 · 实盘真相账本：receipt 重建 2026-06-10→2026-07-13", "",
        "> 纯历史、只读。没有下单、签名、广播、启动 executor、写 signals/Redis 或修改 live/canary；`GO=NOT_MADE`。报价只作未卖库存的显式经济标记，绝不冒充成交。", "",
        "## 一句话给人", "", plain, "",
        "## 口径先于结果", "",
        f"预注册 SHA256=`{d['preregistration']['sha256']}`。冻结规则要求 `dry_run=false/等价武装状态 + Base tx hash + status=1 receipt + 钱包资产移动` 四项同时满足；approval、status=0、模拟、报价和 dry-run 都不是成交。严格已实现只认成功卖出 receipt；未卖库存另列链上可执行经济标记。", "",
        f"机器全量清单读取 {d['corpus_manifest']['files']} 个文件 / {d['corpus_manifest']['bytes']:,} bytes，指纹 `{d['corpus_manifest']['path_sha256_stream_sha256']}`；独立只读复核另以较窄去重口径完整读取 {d['independent_full_corpus_read']['files']} 个文件 / {d['independent_full_corpus_read']['bytes']:,} bytes，指纹 `{d['independent_full_corpus_read']['path_sha256_stream_sha256']}`。两者都不是抽样。", "",
        "## A. 真金战绩", "",
        "|指标|receipt 结果|", "|---|---:|",
        f"|成功真实 swap 腿|{a['successful_swap_fill_legs']}（买 {a['successful_buy_fills']} / 卖 {a['successful_sell_fills']}）|",
        f"|累计真实买入本金|{weth(a['cumulative_invested_weth'])} WETH / {usd(a['cumulative_invested_usd_at_buy_oracle'])}|",
        f"|排除两组蓝筹烟测后的真钱/自然 canary 本金|{weth(a['natural_or_canary_invested_weth_excluding_bluechip_smokes'])} WETH / {usd(a['natural_or_canary_invested_usd_excluding_bluechip_smokes'])}|",
        f"|严格闭环已实现 PnL|{weth(a['closed_realized_pnl_weth_all_associated'])} WETH / {usd(a['closed_realized_pnl_usd_receipt_time_cashflow'])}|",
        f"|闭环胜率 / 最坏闭环|{a['closed_wins']}/{a['closed_roundtrips']} = {pct(a['closed_win_rate'])} / {weth(a['worst_closed_trade_weth'])} WETH|",
        f"|未卖但链上确认流动性坍塌|{a['confirmed_stranded_positions']} 仓；本金 {weth(a['confirmed_stranded_principal_weth'])} WETH；窗口末可执行标记 {weth(a['confirmed_stranded_end_executable_mark_weth'])} WETH|",
        f"|receipt + 窗口末可执行经济 PnL|{weth(a['receipt_plus_end_executable_mark_economic_pnl_weth'])} WETH / {usd(a['receipt_time_oracle_economic_pnl_usd'])}|",
        f"|全部已判入口胜率 / 最坏单笔经济结果|{a['resolved_entry_wins']}/{a['resolved_entry_outcomes']} = {pct(a['resolved_entry_win_rate'])} / {weth(a['worst_economic_entry_weth'])} WETH|",
        f"|BOT 失败卖出|{a['failed_sell_attempts_non_fills']} 次，均 status=0、不是成交；烧 gas {weth(a['failed_sell_attempt_gas_weth'])} WETH|",
        f"|全部交易相关真实 gas|{weth(a['trade_related_gas_weth'])} WETH / {usd(a['trade_related_gas_usd_at_receipt_oracles'])}|", "",
        "### 逐入口结果", "",
        "|入口|段|买入 receipt|真 in / 买入块 USD|真 out 或窗口末精确卖出标记|真实 gas|PnL|裁定|", "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for buy in sorted((x for x in d["real_successful_fill_legs"] if x["side"] == "BUY"), key=lambda x: x["block"]):
        pid = f"{buy['trade_name']}:{buy['segment']}"
        if pid in closed:
            c = closed[pid]
            gas = c["fill_leg_gas_weth"] + c["associated_approval_gas_weth"]
            result = f"{weth(c['proceeds_weth'])} WETH receipt"
            pnl = f"{weth(c['realized_pnl_weth_all_associated'])} WETH / {usd(c['realized_pnl_usd_receipt_time_cashflow'])}"
            verdict = "CLOSED_REALIZED_LOSS"
        else:
            p = next(x for x in d["open_positions"] if x["buy_tx"] == buy["tx_hash"])
            gas = buy["gas_weth"] + p["associated_failed_or_approval_gas_weth"]
            q = p["end_exact_size_qv2"]
            result = f"{weth(p['end_executable_mark_weth'])} WETH ({q['status']})"
            pnl = f"{weth(p['economic_pnl_weth'])} WETH / {usd(p['economic_pnl_usd_receipt_oracle'])}"
            verdict = "UNSOLD; STRANDED_LIQUIDITY_COLLAPSE"
        lines.append(f"|{buy['trade_name']}|{buy['segment']}|{short(buy['tx_hash'])}|{weth(buy['principal_weth'])} / {usd(buy['principal_usd'])}|{result}|{weth(gas)}|{pnl}|{verdict}|")
    lines += ["", "### 11 条成功成交腿（receipt 原值）", "",
              "|UTC|方向|标的|tx|block|ETH/WETH in|ETH/WETH out|token amount|gas WETH|链上 ETH/USD|", "|---|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for x in d["real_successful_fill_legs"]:
        lines.append(f"|{x['timestamp_utc']}|{x['side']}|{x['trade_name']}|{short(x['tx_hash'])}|{x['block']}|{weth(x.get('principal_weth', 0.0))}|{weth(x.get('proceeds_weth', 0.0))}|{x['token_amount']:.8f}|{weth(x['gas_weth'])}|{usd(x['oracle']['eth_usd'])}|")
    lines += ["", "**账本纠错。** `live_ledger.csv` 只有 JOTCHUA 一行且写 `amount_out=0`；实际 receipt 收到 863.9855 个代币。`canary_executor.py` 随后因 `tokens<=0` 把仓位从 state 丢掉，没有 SELL。BOT 的 48 次卖出全部 status=0。早期审计称当前 executor nonce=0/no swaps 只是在 6 月 22 日早时点成立，不能覆盖其后钱包历史。", "",
              "## B. 开盘 / 暂停 / 重启", "", "|时间 UTC|状态|白纸原因|精度 / 锚点|", "|---|---|---|---|"]
    for e in d["timeline"]:
        lines.append(f"|{e['event_utc']}|{e['state']}|{e['reason']}|{e['precision']}; `{e['anchor']}`|")
    lines += ["", "6 月 29 日以后没有合格的 runtime banner、panic 记录或部署日志，因此没有把“事故后必然暂停/重启”的人类记忆倒填成事实。6 月 25–27 的 artifact 只证明 bridge outage 和零笔 would-have-bought，不足以证明 executor/panic pause。", "",
              "## C. 实际配置 pin 与 +56% 估计量", "", "### 实际 runtime 分段", "", "|段|live pin|模型/规模|F / trail / E|主要门|证据|", "|---|---|---|---|---|---|"]
    for s in d["runtime_configuration_segments"]:
        lines.append(f"|{s['segment']}|{s['runtime_pin']}|{s['model']}; size={s['size_weth']}; conc={s['concurrency']}|F={s['F']}; trail={s['trail']}; E={s['E']}|{s['entry_safety']}|`{s['evidence']}`|")
    lines += ["", "### 实盘 vs GL3 / reaudit10 +56%", "", "|字段|实际实盘|+56% 冻结估计量|相同？|", "|---|---|---|---:|"]
    for c in d["actual_vs_plus56_estimand"]:
        lines.append(f"|{c['field']}|{c['actual_live']}|{c['GL3_plus56']}|{'是' if c['same'] else '**否**'}|")
    lines += ["", "“策略没变过”据此为假。最多只能说已核的 6/22–6/29 live entry pickle/params 哈希稳定；风控与退出至少两次改变。aggressive 回测不是保守回测的小参数同口径：它关闭 historical F，故意放入保守版会拦掉的 207 个已知 rug；报告也明说这些 rug 仍能盈利是核心、样本内上界假设。06-29 第三笔 PARA 在这个 F-off 段进入并最终精确卖出 revert，是这个假设的直接真钱反例。", "",
              "## D. 预注册归因分桶", "", "|互斥桶|WETH|证据与量级|", "|---|---:|---|"]
    w = d["attribution_waterfall"]
    lines += [
        f"|执行 / 滑点 gap|{weth(w['execution_gap']['identified_amount'])}|两组深池受控烟测自报 swap deviation 0.0%；自然单没有全部可复现的同块原子反事实，未知部分按预注册进 residual，不能填零。没有证据表明它足以翻转 +56%。|",
        f"|rug 兑现|{weth(w['rug_realization']['amount'])}|五仓完整代币仍在钱包、池 WETH 窗口末坍塌≥99%；JOT/BOT 只计精确末窗 QV2 余值，三 PARA 明确 revert=0。占经济损失 {pct(abs(w['rug_realization']['amount']/w['actual']))}。|",
        f"|regime / 衰减|{weth(w['regime_decay']['identified_amount'])}|非加性诊断：真钱在 GL3 样本结束后，aggressive June OOS 过闸 n=0；但没有非-rug 亏损量可独立归入此桶，故识别量为 0，不等于无 regime 风险。|",
        f"|选择 / 择时|{weth(w['selection_timing']['identified_amount'])}|缺完整候选全集与精确开停窗反事实，按预注册识别量 0、状态 UNKNOWN；不拿停盘期间故事凑解释。|",
        f"|小样本 / 未识别 residual|{weth(w['small_sample_residual']['amount'])}|闭环损失、成功/失败/approval gas 及所有无法逐笔识别的执行或 regime 分量。n=8、0 胜只是诊断，不证明“纯噪声”。|",
        f"|**严格恒等式**|**{weth(w['identity_check'])}**|`execution + rug + regime + selection + residual == actual`，与实际 `{weth(w['actual'])}` 逐浮点值完全相等。|", "",
        "## E. 对质裁定（待独立逐行终审落最终 grade）", "",
        "1. **GL1-02：FAIL at observed tiny live scale。** 成功 receipt 不是零：三组闭环净值全负；把链上流动性坍塌的五个未卖仓按末窗精确可执行值标记后，整体仍约 -0.160236 WETH / -$271.51。", "",
        "2. **没有证据把 GL3 的冻结历史算术本身推翻。** GL3 的 116 子集、.001 WETH、旧模型/退出和历史原子报价，与本月 live 不是同一个估计量；因此 +56% / GL1-12 只能保留为有界历史事实，不能再外推为真钱或未来 edge。", "",
        "3. **真钱结果推翻了“活-待真钱延迟”的实盘含义，并要求降级 aggressive 叙事。** 真钱已经跑过且亏；两组受控深池没有可见 fill gap，几乎全部损失来自撤池/rug 兑现。F-off 的 207-rug 盈利假设是样本内 artifact，不是可用于 GO 的保守证据。", "",
        "4. **相容不是背书。** 历史 +56 与真金亏可以因配置、规模、时段和 rug 机制不同而逻辑相容；这只避免把不同 estimand 粗暴相减，不会把负实盘重新说成正 edge。", "",
        "建议最终分级：GL1-02=`PINNED / FAIL_AT_OBSERVED_TINY_LIVE_SCALE`；GL1-01 与 GL1-12 的历史 grade 保留，但 scope 必须加粗限定“历史/报价型/非实盘/非前瞻”。最终 grade 由独立审查 agent 决定，本文不自我背书。", "",
        "## 证据等级与限制", "",
        "- receipt、tx、block、Transfer/WETH logs 和 Chainlink 历史 round 是主证据；旧 narrative 计数若冲突已被覆盖。", "- 三个真正卖回的闭环是严格 realized；五个未卖仓不是 realized sell，经济结果使用窗口末 exact-size QV2，只有明确 revert 才记零。", "- JOTCHUA/BOT 仍有极小末窗报价，所以没有默认全损；PARADAENGI 三笔的零值有明确 execution revert。", "- 6/29 后 runtime 为 UNKNOWN；没有拿当前源码默认值回填。", "- USD 是每笔 receipt block 的 Base Chainlink ETH/USD；跨时现金流 USD 合计用于人类量级，不替代 WETH 原生 PnL。", "",
        "## SOP / 安全核对", "", "- 预注册先于 outcome 读取，SHA sidecar 已冻结；没有训练或改模型。", "- `failed_no_7d`、label freeze、feature store、strategy/executor 分支只作历史源读取；本轮不改代码路径、不产 live signal。", "- executor 未启动；无 send/sign/broadcast RPC；无 Redis 读写；无 `.env`/私钥输出。", "- 没有复用 paper 指标覆盖 real PnL；Sprint075–079/086 与 `mvp/runs` 明确排除为 paper/dry-run/harness。", "- `training_allowed=false`、`live_allowed=false`、`self_go_allowed=false`、`GO=NOT_MADE`。", "",
        "## 可复现锚点", "",
        f"- 机器账本 SHA256=`{sha(LEDGER)}`", f"- corpus manifest SHA256=`{d['corpus_manifest']['sha256']}`",
        f"- RPC full ledger SHA256=`{sha(ROOT / 'mvp/diagnostics/base_gl4_rpc_full_ledger_20260716.jsonl')}`",
        f"- live model SHA256=`{d['model_artifacts']['live_model_sha256']}`；params SHA256=`{d['model_artifacts']['live_params_sha256']}`",
        f"- 预注册 SHA256=`{d['preregistration']['sha256']}`", "",
        "重跑：先由操作者仅向进程注入 `EXECUTOR_READ_RPC_URL`（必须含 `prettiest-blue-aura`，脚本不读真实 `.env`），再运行 `python3 scripts/research/base_gl4/gl4_live_reconciliation.py && python3 scripts/research/base_gl4/gl4_render_report.py`。回归测试另见 `tests/test_base_gl4_live_reconciliation.py`。", "",
    ]
    OUT.write_text("\n".join(lines))
    print(json.dumps({"report": str(OUT.relative_to(ROOT)), "report_sha256": sha(OUT), "plain": str(PLAIN.relative_to(ROOT)), "plain_sha256": sha(PLAIN)}, indent=2))


if __name__ == "__main__":
    main()
