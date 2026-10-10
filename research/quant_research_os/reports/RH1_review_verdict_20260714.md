# RH1 审查裁决（审查型指挥 Agent · 逐行核验）

> 日期：2026-07-14　审查对象：codex 的 Base-RH1 聪明钱跟单证伪
> 我亲自读过的证据：`scripts/research/base_rh1_smartmoney.py`、`..._core.py`、`tests/test_base_rh1_smartmoney.py`、`reports/base_rh1_smartmoney_falsification_20260714.md`、`mvp/diagnostics/base_transfer_net_b2_audit.json`（B2 交叉证据）。
> 我的角色：不写代码、不判 GO；只给信任度评估 + 交人类的锚点。

---

## 0. 一句话裁决

**codex 的最终结论 `INCONCLUSIVE_COVERAGE_AND_HISTORY` 可信、方向诚实（没有把没测成写成证伪，没有假 PASS，RPC=0，测试用 `==`）——但报告有两处 framing 需要下修，且它据以放弃 transfer-net 的理由基于对一个数字的误读。真正的 headline 不是"聪明钱跟单证伪"，而是"Base 上 ~58% 交易者是智能钱包，tx_from 口径系统性漏掉他们"。**

信任度：**方法卫生 = 高；结论保守性 = 高；两处 framing 过度 + 一处口径误判需纠正。不 GO（本就无 GO 项）。**

---

## 1. 逐行核验结果（对照宪章第四节）

| 反射项 | 结论 | 证据 |
|---|---|---|
| 前视偏差 | **通过**。snapshot 严格取买入前已平仓 episode；选择器只用 split 前 train 窗；`eval_end=t1−7200` 留出场观察 | `_core.py:152-163` build_snapshots；`base_rh1_smartmoney.py:196-201,253` |
| 判据预注册 | **通过**。7 道右尾门跑前冻结；n<20 → INCONCLUSIVE_SMALL_N，未看结果改阈值 | draft §4；`base_rh1_smartmoney.py:382-384` |
| 成交幻觉 | **通过**。入场=延迟后首笔真实成交；TP 需真实 sell 且容量≥tranche；卖不掉记 −100% 不删样本 | `_core.py:190-232`；test `:71-100` |
| 归因真实性（EOA） | **部分/有偏**。用 tx_from 高覆盖窗、排除 infra，未把 router 当 EOA——但**系统性漏掉智能钱包**（见 §2） | `base_rh1_smartmoney.py:112-132` |
| golden 对账 | **缺口**。attribution golden 继承 B1（2 个 EOA）；**新写的 FIFO PnL 逻辑无任何链上/Basescan golden**，只有合成单测 | 报告 §9 只有 hash ledger，无 balanceOf；`_core.py:117-149` FIFO 仅被合成 fixture 测 |
| 样本噪声 | **诚实**。n=0～4，正确标 NEGATIVE_SMALL_N / INCONCLUSIVE，未 powered | 报告 §4-5 |
| 护栏放水（==/>=） | **通过**。测试断言全 `assertEqual`，无偷改 `>=`；代码里的 `>=` 是业务阈值非断言 | `tests:43-100` |
| 执行真实性 | RPC=0、只读 CH、未碰 live；延迟主口径按人类"10s"冻结（未实测校验，报告已标为乐观上界） | `base_rh1_smartmoney.py:45-55`；draft §7.2 |

---

## 2. 必须纠正的三点（我作为审查方的增量）

### 2.1【口径误判】放弃 transfer-net 的理由站不住
RH1 报告 §6 说"B2 beneficial coverage 82.88%、与 tx.from 不一致 67.72%，没通过归因门"，据此拒用 transfer-net、退回 15.4h tx_from 窗。

**但 B2 审计 JSON 本身显示**（`base_transfer_net_b2_audit.json`）：
- `coverage_beneficial 82.88%` / `verified_eoa 24.70%` / `smart_account 58.18%`
- `tx_from mismatch 67.72%`，其中 **`beneficial_account_has_code = 28232 / 31929 ≈ 88%`**

即 67.72% 的"不一致"里约 88% 是因为**受益账户是智能合约钱包**——这些交易的 `tx_from` 本就是 bundler/EntryPoint，不是交易者。**在人类已锁定的"受益账户"口径下，transfer-net 与 tx_from 在这批账户上分歧是正确行为，不是 bug。** 用 tx_from 当 transfer-net 的黄金基准，对这 58% 账户是循环错误。

→ 结论：**transfer-net 未被真正证伪**；它把覆盖率从 0.65% 提到 ~83%，只是①还没到 90%②golden 尚未在智能钱包上做。RH1 "退回 tx_from-only" 是把一个口径分歧误当方法失败。

### 2.2【framing 过度】"复现 RH 去集中化教训"被高估
"35 个盈利钱包、top1<50% 后剩 0" 主要是 **<1 天窗口的机械产物**：每钱包已平仓 episode 极少，一笔自然占利润的 ~100%，`top1_profit_share` 机械趋近 1，去集中门**无论钱包真实质量如何都不可能过**。这不是关于 Base 聪明钱的发现，而是"窗口太短，连去集中都没资格测"。codex 已标 INCONCLUSIVE（好），但"复现 RH 最重要教训"这句应下修为"窗口不足以测去集中"。

### 2.3【选择偏差未披露】tx_from-only 使样本偏向 EOA 少数派
承 2.1：Base ~58% 交易者是智能钱包，其 `tx_from`=bundler/EntryPoint（EntryPoint 在 B1 排除表内→被当 `excluded_wallet` 丢弃，或若走未知 bundler 则有坍缩风险）。因此 RH1 的"3,404 钱包 / 15.4h"样本**系统性偏向 EOA 交易者，漏掉智能钱包主力**。报告未披露此偏差。需用 `coverage.drop_reasons.excluded_wallet` 计数确认丢弃规模（我未逐条读 RH1 JSON 的该字段，建议下轮补）。

---

## 3. 交人类的三项标准答复

**① 覆盖率前后**
- RH1 本身**未做覆盖率救援**：刻意只用 15.4h 高置信 tx_from 窗（57,426 笔 V3 raw → 18,840 笔 WETH-土狗可用成交 / 3,404 钱包 / 28 池），即全史 0.65% 切片。
- B2（另一任务，transfer-net）**确实把可归因覆盖从 0.65% 提到 82.88%**（any 87.35%），但 <90% 目标；其中 verified-EOA 仅 24.70%、smart_account 58.18%。**覆盖率救援方向有效，尚未达标，且被 RH1 误判为失败。**

**② golden 是否对上**
- attribution（哪个钱包）：继承 B1，2 个 EOA 钱包 balanceOf 区块边界精确对上；本轮未新增。
- **FIFO 已实现 PnL（决定"35/0"计数的逻辑）：无任何链上 golden**，只在合成 2-3 笔 fixture 上单测。→ "35 盈利钱包 / 0 去集中"这组数字**尚无链上真值背书**。
- B2 的 balanceOf golden 是否覆盖智能钱包：**我尚未逐行审 B2，未确认**。

**③ 还漏了哪类 swap**
1. 窗口外全史（8.11M tx 的 ~99.35%）——无高置信 tx_from。
2. **智能合约钱包交易者（~58%）**——tx_from-only 系统性漏掉（最重要）。
3. V2 + Aerodrome swap 全部——脚本只查 `uniswap_v3_swaps_decoded`（`base_rh1_smartmoney.py:74`），与 draft §1"V3/V2/Aero"不符，属未披露的范围收窄。
4. 非 WETH 计价土狗（USDC 等）——38,064 笔按 `not_weth_memecoin_pair` 丢弃。
5. 窗口内无前置买入的卖出（左删失）——episode PnL 被截断，episode 计数偏低。

---

## 4. 其它须记录的小问题
- `base_rh1_smartmoney.py:447` verdict.reason 硬编码 "34 pools"，与实际计算的 28 池不符——陈旧字面量，需改。
- 入场 `simulate_copy` 取延迟后首笔成交不区分买/卖方向（`_core.py:190-197`）；notional 仅 0.001 WETH 影响小，但严谨应取可买入方向。
- TP 允许同一笔大额 sell print 连续填满 3 档（`_core.py:210-215`）——略乐观，小额下可接受。

---

## 5. 建议的下一步（供人类拍板，我不替决）
覆盖率救援的真正障碍不是"transfer-net 不准"，而是"Base 是智能钱包主导的市场，tx_from 不是真值"。据此三条路线（详见对话中的选项）：
1. **重判 B2**：按"受益账户"口径重核 transfer-net，在**智能钱包**上补 balanceOf golden；把"与 tx_from 一致"降级为参考、不作硬门。
2. **扩 tx_from 历史**（codex 建议）：按月回填——但这只救 EOA 少数派，不解决 58% 智能钱包，性价比存疑。
3. **先做 B2 逐行审**再决定 RH：我尚未审 B2 代码，只读了它的产物 JSON。

在覆盖率与智能钱包归因未定之前，**维持裁决背景的铁律：禁止对 Base 聪明钱下任何"有/没有 edge"结论。** merit 保持 UNTESTED。
