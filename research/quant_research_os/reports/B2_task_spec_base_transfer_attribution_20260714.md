# B2 任务书（Base 适配 · 加固版）— token_transfers 反向归因 & 覆盖率救援

> **来源**：CHA 侧 B2 草案，经本 VPS 审查型 Agent 按宪章第四节加固 + 适配 Base 真实 schema。
> **性质**：纯回测/研究。**禁止**下单/签名/广播；**禁止** codex 自判 GO；所有断言可复现。
> **前置裁决（不可动摇）**：B1 已证明旧 `swap.sender` 直接跟单失败主因是归因混淆（Router 坍缩）。**但在覆盖率救到可信 >90% 之前，禁止对 Base 聪明钱下任何"有/没有 edge"的结论。** 本任务只解决归因覆盖率与正确性，不碰 merit。
> **codex 铁律**：不启动/重启任何 live/executor/strategy 服务，不改任何 live 配置，只新增 research 表/视图/脚本/测试。开工前先对照实际 schema 核对列名（见 §7），不许凭记忆写表名。

---

## 0. 目标（一句话）
不依赖 `tx_from`（全史仅 ~0.65% 覆盖），改用**同 tx_hash 的 `erc20_transfers` 按目标 token 净流向反解真实交易账户**，把"可信归因的 swap 占比"从 0.65% 提到 **>90%**，并用链上 `balanceOf` golden 对账 + 回归测试自证。

---

## 1. 归因核心算法（必须照此实现，不许简化成"单条 transfer 收币方"）

对分析窗内每一笔 V3 / V2 / Aerodrome(vAMM+CL) swap：

1. **确定 quote 腿与 target token**：从 `discovered_pools` / `pool_registry` 取该池的 token0/token1；quote 腿 = WETH（或稳定币，若无 WETH 腿）；**target token = 非 quote 的那个 token**。
2. **在同 `tx_hash` 内，按地址对 target token 求净额**：
   `net_delta(addr) = Σ(transfer.amount 收入 addr) − Σ(transfer.amount 转出 addr)`，数据源 `erc20_transfers_decoded`（数值），仅统计 `token_address = target_token`。
3. **排除基础设施地址**（用 §3 单一权威清单）：池地址、Router、聚合器、Permit2、NPM、Quoter、EntryPoint、WETH 合约等。
4. **判定交易账户**：
   - 若排除基础设施后，**恰好一个**非基础设施地址 `net_delta > 0` → 该地址 = **买家**（`side='buy'`），其对应的 `net_delta < 0` 唯一非基础设施地址（若存在）= 卖家的对手；
   - 对卖出 swap，对称处理（唯一 `net_delta < 0` 的非基础设施地址 = 卖家）；
   - **若 ≥2 个非基础设施地址同向变动，或 0 个** → `attribution_source='ambiguous'` 或 `'unresolved'`，**不许猜、不许取第一个**。
5. **每笔 swap 落一行**，记录 provenance：`resolved_account, attribution_source ∈ {transfer_net, tx_from_confirmed, recipient_fallback, ambiguous, unresolved}`、`net_delta_raw`、`is_infra_only`、`account_code_class ∈ {eoa, smart_account, unknown}`（由 `eth_getCode` 判，见 §4 口径）。

> **不要**用 `swap.recipient` / `swap.sender` 作为主口径；它们只能作为 transfer 法失败时的显式 fallback，并单独标记，且**不得计入 >90% 的"可信覆盖"分子**。

---

## 2. 自证机制（这是本任务的灵魂，必须实现）

**用 0.65% 的 tx_from 重叠区作为 transfer 法的黄金验证集**：

- 在 `tx_from_by_hash` 有值的 swap 子集上，比较 §1 transfer 法解出的 `resolved_account` 与 `tx_from`。
- **回归断言**：在"目标 token 有唯一非基础设施净收益方 且 tx_from 非基础设施"的子集内，`resolved_account == tx_from` 的比例必须报出；理想 100%。**任何不等都要逐例列出根因**（多跳？Permit2？AA？fee-on-transfer？），不许用平均/阈值掩盖。
- 若不等率显著（预注册阈值：不等 > 2% 即触发人工复核），**本任务判 `NEEDS_REVIEW`，不许宣称覆盖率救援成功**。

---

## 3. 基础设施排除清单（单一权威源，不许散落）

- 复用 B1 已建的单一清单：`configs/research/base_attribution_exclusions.json`（B1 里 36 条，schema v1）。
- B2 若发现新的 Router/聚合器/中转合约，**只准往这个清单加**，并在任务报告里列出新增地址 + 判定依据（Basescan 标签 / 链上行为）。
- 物化 research 表 `eatlq.base_wallet_attribution_exclusions` 必须与 manifest **逐字节同步**（回归测试断言二者集合 `==`）。
- **额外要求**：WETH 合约地址、稳定币合约地址必须在清单内（它们会因 wrap/unwrap 产生 Transfer 噪声）。

---

## 4. EOA vs 智能账户口径（✅ 人类已锁定 2026-07-14：受益账户口径）

- **锁定口径**：解出的"交易账户"= 排除基础设施后的**受益账户**（beneficial account），**无论 EOA 还是智能账户都算 resolved，不许丢弃智能合约钱包**（Base 上 Coinbase Smart Wallet 等占散户主力）。
- 对每个受益账户用 `eth_getCode` 在窗口末区块打标 `eoa / smart_account`。
- **>90% 目标挂在 `coverage_beneficial` 上**（含智能账户）；`coverage_verified_eoa` 单列供参考，不作判据。

---

## 5. 覆盖率报告（三个口径，分母预注册，不许注水）

**分母（✅ 人类已锁定 2026-07-14）**：用 B1 的高覆盖区块窗（`block 46,389,458–46,417,105`，~57k swaps）为评估窗——先在此证明方法，`>90%` 目标只对该窗口生效；全史 8.11M tx **只做外推估计**，不作判据（避免被迫先扩 tx_from 回填、消耗大量 RPC）。

必须分别报出：
| 口径 | 定义 | 是否用于 >90% 判据 |
|---|---|---|
| `coverage_any` | 解出任意非空账户（含落到合约） | 否（参考） |
| **`coverage_beneficial`** | 排除基础设施后解出唯一受益账户（transfer_net 或 tx_from_confirmed） | **是（目标 >90%）** |
| `coverage_verified_eoa` | 上者中 `eth_getCode=0x` | 否（单列，供 EOA-only 口径参考） |

同时报**残差桶**（加起来 = 100%）：`ambiguous`（多跳/多买家）、`infra_only`（全是基础设施，无受益方）、`no_transfer`、`smart_account_only`。**"还漏了哪类 swap" = 这几个桶的占比 + 各举 2 个 tx_hash 例子。**

归因前后对比表（旧 vs 新）：distinct identities、非基础设施 identities、被 Router 坍缩的 swap 数、覆盖率三口径。

---

## 6. Golden 对账（独立于工具口径，硬案例覆盖）

- 选 **3–5 个已知钱包**，`tests/fixtures/` 落 golden fixture（延续 B1 的 `base_v3_resolved_eoa_golden.json` 风格）。
- **强制案例组合**：至少 1 个多跳 tx、1 个 Universal Router 路由、1 个智能合约钱包(AA/Coinbase Smart Wallet)。
- 每个钱包：用**历史 archive RPC `balanceOf`** 在窗口起止区块边界读原始整数余额，`delta_raw = balanceOf(end) − balanceOf(start)`，与 view 计算的 target-token 原始整数净额**精确相等（`==`）**。
- 至少 2 笔在 Basescan 上人工核对 `From / To / block / tx_hash` 与本地 resolved 行一致，报告里附 basescan 链接。
- **禁止**只把 view 输出和另一段 SQL 比（那是工具口径自比）。

---

## 7. 产出物 & schema 对照要求

**开工前** codex 必须先读并在报告里确认以下真实 schema 的列名（不许凭记忆）：
- `infra/clickhouse/init/003_normalized_events.sql`（`erc20_transfers`: token_address/sender/recipient/amount_raw；v2/v3 swaps）
- `013_decoded_views.sql`（`erc20_transfers_decoded`、`uniswap_v3_swaps_decoded`）
- `026_discovered_pools.sql`（token0/1_address、has_weth_leg、fee_bps）
- `040_tx_from_by_hash.sql`、`041_base_v3_resolved_wallet_swaps.sql`（B1 已建，本任务在其上扩展，不要另起炉灶）

**交付**：
1. 新增 CH view/表：`eatlq.base_cha_wallet_swaps`（含 §1 provenance 字段）+ 与 B1 view 的关系说明（复用 or 取代，需说明理由）。
2. 归因脚本（research，`--install` 只碰 research 表）。
3. 覆盖率前后对比 + 三口径 + 残差桶（JSON + md）。
4. Golden fixture + 3–5 钱包 balanceOf 边界对账。
5. 回归测试（`unittest`），断言见 §8。
6. 研究报告 md（含 basescan 锚点、可复现命令、明确写"research-only, no GO"）。

---

## 8. 回归测试断言（宪章点名：== 不许偷改 >=）

必须包含且用**精确相等**：
- golden 每个钱包 target-token `net_delta_raw == balanceOf 边界原始整数增量`（整数，禁止浮点近似 / `assertAlmostEqual`）。
- 基础设施地址集合 == manifest 集合。
- transfer 法在 tx_from 重叠区的 `resolved_account == tx_from`（报不等清单）。
- 基础设施地址**不得**作为 `resolved_account` 出现（`assert count == 0`）。
- 多跳/多买家 tx 判为 `ambiguous`（用固定 fixture tx 断言，不许被解成单一钱包）。
- 区块口径：只用 `block_number` 切窗；断言无 arrival-time fallback。

审查方（我）会 grep 测试文件中的 `>=` / `assertGreater` / `assertAlmostEqual` / `assertLessEqual`，出现即视为护栏放水，打回。

---

## 9. 预注册判据（跑之前定死，看到结果不许改）
- **PASS（覆盖率救援成立）**：`coverage_beneficial ≥ 90%`（分析窗）**且** tx_from 重叠区 `resolved==tx_from` 不等率 ≤ 2% **且** 3–5 golden 全部 `==` 对上 **且** 无基础设施混入 resolved。
- **NEEDS_REVIEW**：覆盖率达标但重叠区不等率 > 2%，或 golden 有不符，或 ambiguous 桶 > 10%。
- **FAIL**：coverage_beneficial < 90% 或 golden 对不上。
- 任何结果都**不触发 GO / 训练 / 实盘**；merit 仍冻结。

> codex 完成后回报，由本 VPS 审查 Agent 逐行核验，再交人类。
