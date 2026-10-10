# B2 结案裁决：transfer-net 覆盖率救援证伪（持币口径坍缩）

> 审查型指挥 Agent　2026-07-14　我已逐行核实 codex 的 B2-v3 三关产物（audit JSON 尾部汇总、shared_infra 名单、golden、test_evidence）。
> 裁决：**背书 codex 的 `NEEDS_REVIEW_HOLDER_COLLAPSE_AND_SUBGRAPH_GOLDEN_FAIL / NO-GO`。transfer-net 的"0.65%→90%"覆盖率救援，按当前口径证伪。**

## 0. 一句话
之前那个"92.25% 覆盖率"是**两重虚高**堆出来的：①把大量用户坍缩进共享智能合约当成一个"受益人"（旧 sender bug 换皮）；②多跳救回 80% 是错的。挤掉水分后，Base 上能干净归到"单个真实交易者"的 swap 只剩 **~47.8%（且仍不干净）**，离 90% 差很远。**这不是 codex 没做好，是 Base 的散户订单流本身结构性难以按钱包归因。**

## 1. 三关核实（我独立读产物确认，非转述 summary）

**关卡 A｜合约钱包集中度 = FAIL（最致命）**
- 92.25% 里 60% 是合约钱包。集中度一查：**80 个地址是共享合约冒充受益人**。
- 头号 `0x51c7…2a7f`：**3,722 笔 swap、199 个不同签名者**、占 beneficial 7.03%。199 个不同的人签名把币打进同一个地址——这不是"一个聪明钱"，是共享合约。
- 名单里直接抓到 **Balancer Vault**（`0xba12222222228d8ba445958a75a0704d566bf2c8`）和 Balancer V3 Vault（`0xba13…9ba9`）——**协议金库被当成了"聪明钱受益人"**，铁证归因在坍缩，且排除名单本就不全。
- 只剔除这 80 个，coverage 从 92.25% → **47.81%**；剩余最大账户仍占 **4.19%**（>1% 门）——说明阈值以下还有更多共享合约没抓完。双门（≥90% 且无一家>1%）不满足。

**关卡 B｜"打给别人"的 2,050 条 EOA = 已刻画**
- deposit sink（归集/中转，非交易者本人）**1,162，56.7%**；second wallet 529（25.8%）；one-off 93；无法归类 266（adverse 保留）。
- 含义：即便在 EOA 上，"持币口径"把过半的币归给了**中转/归集地址而非交易决策者**——用它做 PnL 会把身份打碎。

**关卡 C｜多跳救回 golden = FAIL**
- 均匀抽 5 个 `transfer_subgraph` 救回案例，独立 balanceOf 全窗对账**只有 1/5 对上**（4/5 错）。
- 按任务书铁律：**全部 5,383 条子图救回已回滚，可信子图行现在精确=0**。

**回滚后真实状态**：beneficial 82.88%、ambiguous 11.60%、infra_leak 0（但这 82.88% 仍含未剔除的共享合约，真实干净覆盖 ≤47.81%）。18/18 测试通过，`EATLQ_RUN_B2_RPC=1/_CH=1` 跑绿。

## 2. 我的裁决（审查角色）
- **背书 NO-GO**。三关是我下的、判据预注册、断言 `==`、golden 独立、回滚照铁律执行——这是一次教科书式的诚实证伪。codex 没有把 FAIL 包装成 PASS，记功。
- **纠正历史记录**：此前(含我上一版)对"82.88%/92.25% 覆盖率"的谨慎背书**作废**。transfer-net 净流向法在 Base 智能钱包主导的市场里，无法把共享合约后面的真实用户拆开；tx_from 是 bundler、收币方常是共享合约或中转地址。

## 3. 市场结构结论（要上报指挥的真正 takeaway）
**Base 的散户/智能钱包订单流结构性难以按单钱包归因。** 三条独立证据：
1. tx_from 高置信只覆盖 0.65%（bundler 问题）；
2. 净流向 60% 落在合约钱包，其中头部是共享合约/协议金库（坍缩）；
3. 即便 EOA，56.7% 的币流向归集/中转地址而非决策者。

→ **任何依赖"识别单个聪明钱钱包"的策略（跟单/聪明钱狙击）在 Base 上先天缺地基**，不是调参能救。这限制的是一整类策略，不止 RH1。

## 4. 连带结论
- RH1 仍**不得**用 B2；RH1 的 FIFO PnL 也仍缺链上 golden。
- Base 聪明钱 merit = **UNTESTED**，维持铁律：**禁止下"有/没有 edge"结论**。
- transfer-net 覆盖率救援线：**结案（证伪当前口径）**。若还要救覆盖率，只剩重投入的深水区（解析共享合约/solver 内部真实 end-user、或 call-trace 归因），性价比需指挥拍板。

## 5. 证据路径
- `mvp/diagnostics/base_transfer_net_b2_audit.json`（尾部 `contract_wallet_concentration`/`subgraph_golden`/`holder_v3_verdict`）
- `configs/research/base_holder_suspected_shared_infra_20260714.json`（80 个共享合约 + 证据）
- `tests/fixtures/base_transfer_net_b2_golden.json`、`mvp/diagnostics/base_transfer_net_b2_test_evidence_20260714.json`
- `reports/base_transfer_net_attribution_b2_20260714.md`
