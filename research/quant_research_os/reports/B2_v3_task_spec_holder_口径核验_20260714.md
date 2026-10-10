# B2-v3 任务书：受益账户"持币口径"三关核验（下发 codex）

> 审查型指挥 Agent 下发　2026-07-14　背景：B2-v2 已把 beneficial coverage 做到 92.25% 且 golden 跑绿；但审查认定 EOA 自证 13.65% 的 FAIL 是"签名人≠持币人"的口径分歧、非方法错。下一步不是放宽判据，而是**先把三个残留风险核实**，再重定验收。
> 铁律：纯回测/只读，不下单/签名/广播/改 live；不自判 GO；断言用 `==` 不许改 `>=`；数据缺失一律 adverse；RPC 只读；产出可复现 + hash ledger。Base 不适配处照实回报。

## 关卡 A — 合约钱包桶集中度（防身份坍缩复发，最高优先级）
92.25% 里 60.28% 是 `contract_wallet`。若某个**未进排除名单的共享合约**被当成受益人，会把成千上万用户坍缩成一个身份——正是旧 sender 翻车的病根。`infra_leak=0` 只证明不撞已知名单，不排除未知共享合约。

要求：
1. 对每个 `account_code_class IN ('contract_wallet','eip7702')` 的 `resolved_account`，在锁定窗内算：`swaps_count`、`distinct_tx_from_count`（喂给它的不同签名者数）、`distinct_target_token_count`。
2. **预注册判据**（跑前冻结）：`distinct_tx_from_count >= 20` 或 `swaps_count` 进全体 resolved 前 0.1% 的地址，标 `suspected_shared_infra`。独占智能钱包应只有极少数固定签名者；共享合约会有大量不同签名者——这是判别关键。
3. 报出：被标地址清单 + 各自 swaps 占 beneficial 的比例；把它们移入排除名单后**重算 coverage**，报前后差值。
4. **通过条件**：剔除疑似共享合约后 beneficial coverage 仍 `>= 90%`，且**单一 resolved_account 占 beneficial swaps 比例 `< 1%`**（无一家独大）。不满足则如实报 `NEEDS_REVIEW`，不许把共享合约留在分子里凑 90%。

## 关卡 B — 刻画 2,050 条"打给别人"的 EOA（持币 vs 签名）
这批（`third_party_eoa_recipient`）是单笔 swap、归到真实 EOA 但 ≠ tx_from：有人签名、币打到另一个钱包。需判定这是不是身份碎片化风险。

要求：对每条的 `resolved_account`（收币 EOA）在窗内算并分桶：
- `self_second_wallet`：该 EOA 自己也作为 tx_from 签过名 / 之后卖出过同 token（是活跃交易者的第二钱包）；
- `deposit_sink`：多个不同 sender 打入、自己从不签名（疑似 CEX/中转/归集地址）；
- `one_off`：仅此一次、无后续。
报三桶占比。**结论只描述，不据此改归因**；用于判断"持币口径"做 PnL 时会不会把一个交易者拆成多个身份。

## 关卡 C — 抽验救回的多跳（golden 补强）
`transfer_subgraph` 救回 5,383 条，golden 只覆盖 1 个多跳案例。

要求：从 `attribution_source='transfer_subgraph'` 且 `swap_rows>1` 的行里，按区块均匀抽 **3–5 个**，每个用独立 archive `balanceOf` 在 `balance_start_block / end_block` 边界 `==` 核对 `resolved_account` 的 `net_delta_raw`；写进 golden fixture 与回归测试（`==` 断言），进 hash ledger。任一对不上 → 该子图救回逻辑记 `NEEDS_REVIEW`，回滚这批到 ambiguous。

## 重定验收（三关通过后才生效）
把归因层验收从"EOA resolved `== tx_from` ≤2%"**改为**：
1. balanceOf golden 全过（含 UR-EOA / 7702 / 合约钱包 / 多跳，≥5 例）；
2. 关卡 A 通过（无共享合约坍缩，coverage ≥90%，无一家 >1%）；
3. `ambiguous <= 3%`、`infra_leak == 0`；
4. EOA `resolved == tx_from` 仅作**参考指标**报出（连同关卡 B 三桶），不作硬门。

满足 1–3 → 归因层 `PASS_AS_ATTRIBUTION`；否则 `NEEDS_REVIEW`。仍**不判 GO**、不碰 RH merit。

## 产出
- 更新 `base_transfer_net_b2_audit.json`：新增 `contract_wallet_concentration`、`third_party_eoa_recipient_characterization`、`transfer_subgraph_golden`；
- 更新 golden fixture + 回归测试（15→≥18，`==`）；跑绿 `EATLQ_RUN_B2_RPC=1 EATLQ_RUN_B2_CH=1` 并附 test_evidence；
- hash ledger；中文回报三关数值 + 布尔 + 证据路径，不只写总裁决。

## 与 RH1 的关系
三关全过前，RH1 不得改用 B2。三关过后，RH1 才可在 `base_cha_wallet_swaps` 持币口径上重跑，且**必须先补 RH1 的 FIFO realized PnL 链上 golden**（见 `RH1_review_verdict_20260714.md`）。在此之前维持铁律：**禁止对 Base 聪明钱下"有/没有 edge"结论，merit=UNTESTED。**

---

## 执行回执（2026-07-14）

- 关卡 A：**FAIL**。80 个疑似共享合约进入独立研究排除名单；coverage 92.250897% → 47.809355%，最大剩余账户占 4.185030%，两个条件均未过。
- 关卡 B：描述完成。2,050 行分为 self_second_wallet 529（25.804878%）、deposit_sink 1,162（56.682927%）、one_off 93（4.536585%）；另有 266 行（9 个账户）不满足三个定义，按 adverse 记 `adverse_unclassified`，未强制塞桶。
- 关卡 C：**FAIL_ROLLBACK_REQUIRED**。五分位均匀抽 5 例，archive `balanceOf` 边界 1/5 对上、4/5 未对上；整批 `transfer_subgraph` 已回滚到 ambiguous。
- 回滚后 beneficial coverage=82.877094%，ambiguous=11.604500%，所以新验收口径不生效。
- `EATLQ_RUN_B2_RPC=1 EATLQ_RUN_B2_CH=1`：18/18 PASS；这些测试精确固化 adverse golden 与回滚状态。

最终：`NEEDS_REVIEW_HOLDER_COLLAPSE_AND_SUBGRAPH_GOLDEN_FAIL / NO_GO`。RH1 仍不得使用 B2，merit=`UNTESTED`。
