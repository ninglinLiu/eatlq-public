# B2 逐行重审裁决（按"受益账户"口径重判 transfer-net）

> 日期：2026-07-14　审查型指挥 Agent　人类已锁定：受益账户口径 + B1 高覆盖窗为评估窗
> 我亲自读过：`scripts/wallet_profile_v0/base_transfer_net_attribution.py`、`base_transfer_net_b2_audit.py`、`infra/clickhouse/init/042_base_cha_wallet_swaps.sql`、`tests/test_base_transfer_net_attribution.py`、`tests/fixtures/base_transfer_net_b2_golden.json`、`mvp/diagnostics/base_transfer_net_b2_audit.json`

---

## 0. 一句话重判

**transfer-net 作为"归因方法"是可信的、在硬案例上被独立 balanceOf golden 对上了、不漏基础设施、比旧 sender 多认出 22.6× 钱包身份。它没"失败"。真正没达标的是"把可信覆盖冲到 90%"——卡在 11.60% 的 ambiguous(多跳/多买家)桶。而"与 tx_from 不一致 67.72%"是一个被我自己写歪的判据造出来的假警报：其中 88% 是智能账户，tx_from 本就不该等于受益账户。**

对方法的裁决：**CONDITIONAL_PASS_AS_ATTRIBUTION / NOT_YET_90pct_COVERAGE**。不 GO，merit 仍冻结。

---

## 1. 逐行核验（对照宪章第四节 + 我下发的 B2 任务书）

| 检查项 | 结论 | 证据 |
|---|---|---|
| 归因算法=净流向法(非 naive 收币方) | **通过**。按 (tx_hash,target_token) 对地址求净额，排除 infra+池，唯一同向受益方才认；并列→ambiguous，不猜 | `base_transfer_net_attribution.py:130-196` |
| 拒绝把 router/聚合器当账户 | **通过**。infra=manifest∪pool；CH 测试断言 resolved 与排除表交集=0 | `:155`；`test_...:96-105` |
| 智能账户不丢、打标 | **通过**。eth_getCode 打 eoa/smart_account；覆盖率三口径分列 | `:509-550`、`:576-579` |
| golden 独立于工具口径 | **通过（强）**。opt-in RPC 测试真打 `eth_call balanceOf`(0x70a08231) 在区块边界，`==` 断言 balance_start/end 及 end−start==view_net_delta | `test_...:161-175` |
| golden 覆盖硬案例 | **通过（满足我 §6 要求）**。UR-EOA + **EIP-7702 智能账户(0xef0100)** + 双池多跳 + ambiguous fixture 全在 | `fixtures/base_transfer_net_b2_golden.json` |
| 断言 == 不许 >= | **通过**。全 `assertEqual`；无 `>=`/`assertGreater`/`assertAlmostEqual` | `test_...` 全文 |
| 区块口径无 arrival-time | **通过**。断言源码无 `received_at_ms`、用 `block_number BETWEEN` + archiver 时间 | `test_...:50-54` |
| 覆盖率分母诚实 | **通过**。分母=窗内全部 V3+aero_cl swap(57,426)，未注水；残差桶加总=分母 | `base_transfer_net_b2_audit.py:43-58`；JSON `residual_count_sum:57426` |

**结论：B2 的工程与验证卫生达标，且在两处超出我的任务书要求（多跳/7702 golden、mismatch 诊断桶）。**

---

## 2. 纠正 RH1 与旧口径的误读（本次重审的核心增量）

### 2.1 "67.72% 不一致 = 方法失败" 是假警报
审计 JSON `tx_from_overlap.mismatch_pct = 67.72%`，但 `tx_from_mismatch_buckets`：

| 诊断桶 | 数量 | 占 mismatch |
|---|---:|---:|
| `beneficial_account_has_code`（智能账户） | 28,232 | **88.4%** |
| `multi_swap_or_multihop` | 2,975 | 9.3% |
| `third_party_eoa_recipient` | 722 | 2.3% |

- 88.4% 的"不一致"是**受益账户是智能合约钱包**——这些交易 `tx_from`=bundler/EntryPoint/赞助方，**本就不等于受益账户**。在人类锁定的受益账户口径下，这是**正确分歧不是错误**。
- 我在 B2 任务书 §2 写的"resolved 必须 == tx_from，不一致 >2% 判 NEEDS_REVIEW"是**错的判据**：它把 tx_from 当成全体账户的真值，但 Base 上 tx_from 对 58% 智能账户不是真值。**codex 忠实实现了我的错门**（`base_transfer_net_b2_audit.py:151-157` 用全局 mismatch_pct 判 NEEDS_REVIEW），审计里加的诊断桶恰好暴露了这个错门——这点要给 codex 记功。

### 2.2 正确的自证口径：只在 EOA 子集上比 tx_from
把智能账户剔除后，真正值得查的不一致 = `multi_swap_or_multihop(2,975)` + `third_party_eoa_recipient(722)` = 3,697。
- EOA 子集近似不一致率 ≈ 3,697 /（matches 15,217 + 3,697）≈ **19.6%**，且**几乎全是多跳**，不是智能账户口径。
- 即：在 tx_from 可作真值的 EOA 上，transfer-net 单跳基本对得上，残差主要是**多跳归因**这一件事。

### 2.3 覆盖率的真障碍是多跳，不是方法错
- `coverage_beneficial = 82.88%`（<90%，此门确实 FAIL）。
- 缺口构成：`ambiguous 11.60%`(6,664，多跳/多买家) + `infra_only 5.48%`(3,145) + `unresolved 0.04%`。
- **要到 90%，唯一要啃的是 11.60% 的多跳/多买家桶**（同 §2.2 的残差同源）。这是真活，不是口径伪影。

### 2.4 旧口径失败被独立复现
`old_vs_new`：非基础设施身份 **273 → 6,165（22.6×）**；router 坍缩 swap **14,083（24.5%）**。独立于 B1 再次确认"旧 sender 直接跟单失败=归因混淆"。

---

## 3. 交人类的三项标准答复（B2 版）

**① 覆盖率前后**：旧 sender 非基础设施身份 273 → transfer-net 受益身份 6,165；可归因覆盖 0.65%(tx_from) → **82.88%(beneficial)**、any 87.35%。**方向成立，未达 90%**，差在多跳 11.60%。

**② golden 是否对上**：**对上，且是硬案例**。UR-EOA(delta −100000000)、EIP-7702 智能账户(+144982766)、双池多跳(−100000000) 三例，独立 archive `balanceOf` 区块边界 `==` view 净额；ambiguous fixture 判为 ambiguous/空账户。**唯一待确认回执：opt-in `EATLQ_RUN_B2_RPC=1` / `EATLQ_RUN_B2_CH=1` 是否真跑绿**（默认 6 测只跑纯逻辑，链上 golden 是 skipUnless；需 codex 附跑绿证据）。

**③ 还漏了哪类 swap**：多跳/多买家(ambiguous 11.60%)、target token 仅在 infra 间流动(infra_only 5.48%)、无法解出(unresolved 0.04%)；范围上 V2/aero_vamm 已入表但**审计判据只算 V3+aero_cl**；非 WETH/非白名单 quote 的池不进 target 解析。

---

## 4. 仍需 codex 处理的问题（下发给对面执行 agent）

> 铁律不变：纯回测/只读、不下单/签名、不自判 GO、断言用 `==`、数据缺失 adverse、Base 不适配处照实回报。

1. **重定自证判据（口径修正）**：把"transfer-net vs tx_from"一致性检查**重新限定在 EOA 子集**；智能账户分歧降级为**参考、不作硬门**。并把 `smart_account` 桶按 **EIP-7702（code 前缀 `0xef0100`）vs 真合约钱包**拆开——7702 是 EOA 控制，理论上 tx_from 常应等于受益账户，这批若仍不一致要单独查（可能是赞助/bundle）。预注册：EOA 单跳子集不一致率必须报出，目标 ≤2%。
2. **攻 11.60% 多跳桶以冲 90%**：对多 swap/多跳 tx，按**每个 swap 的转账子图**（in-leg/out-leg 配对到具体 pool）或退一步用 trace 归因，把能唯一归属的多跳 swap 从 ambiguous 救回；救不回的仍记 ambiguous，不许放宽成"取第一个"。报覆盖率前后与新残差桶。
3. **补跑并附证据**：`EATLQ_RUN_B2_RPC=1` 与 `EATLQ_RUN_B2_CH=1` 全绿输出（含 3 个 golden + ambiguous 物化断言），进 hash ledger。
4. **account_code 时点**：eth_getCode 现仅取 end_block；对 7702 这类可逐 tx 变化的委托，说明单点取值的偏差边界，必要时在 swap 所在块取 code。
5. **不得**回退到旧 sender 或用 recipient/sender fallback 冒充受益账户扩样本。

---

## 5. 对 RH1 的连带结论
RH1 以"B2 没过归因门"为由退回 15.4h tx_from-only 窗——**该理由基于 §2.1 的假警报，不成立**。但 RH1 现在也**不能**直接改用 B2：B2 覆盖 82.88% 且多跳桶未解，且 RH1 的 FIFO PnL 逻辑仍无链上 golden（见 `RH1_review_verdict_20260714.md`）。正确顺序：先按 §4 把 B2 多跳补到 ≥90% 且 EOA 子集自证 ≤2%，再让 RH1 在 `base_cha_wallet_swaps` 受益账户口径上重跑，届时才谈得上对 Base 聪明钱下 edge 结论。

**在此之前，裁决背景铁律维持：禁止对 Base 聪明钱下"有/没有 edge"结论。merit=UNTESTED，不 GO。**

---

## 6. 2026-07-14 执行回执（覆盖此前“待处理”状态）

已按 §4 实装并复跑：

- 硬自证已限定为 `account_code_class='eoa'`；EIP-7702 与真合约钱包只作参考诊断。
- `smart_account` 已拆成 `eip7702`（`0xef0100`）和 `contract_wallet`。
- 6,664 条原 ambiguous 中，以每 swap 日志区间、pool-touch 锚点和唯一单调转账子图严格救回 5,383 条；剩余 1,281 条仍为 ambiguous，没有采用“取第一个”。
- beneficial coverage 已由 82.877094% 提升到 **92.250897%**，通过 ≥90% 门。
- EOA-only 自证为 13,400 matches / 2,119 mismatches / 15,519 eligible，mismatch **13.654230%**，未通过 ≤2% 门。
- `EATLQ_RUN_B2_RPC=1 EATLQ_RUN_B2_CH=1` 实跑 **15/15 全绿**；包含 3 个 archive balance golden、7702 code prefix、CH 精确覆盖、EOA overlap、infra=0 与 adverse ambiguous 断言。
- `eth_getCode` 仍是 `end_block=46,417,105` 单点快照；7702 委托可在窗内变化，因此早期 swap 的代码类别存在单点取值偏差，不能当逐交易时点证明。

更新裁决：`B2_COVERAGE=PASS`，`B2_EOA_SELF_PROOF=FAIL`，总裁决 **`FAIL_EOA_SELF_PROOF / NO_GO`**。铁律继续生效：RH1 不得改用 B2；RH1 FIFO PnL 仍缺独立链上 golden；Base 聪明钱 `merit=UNTESTED`，禁止下“有 edge/没有 edge”结论。
