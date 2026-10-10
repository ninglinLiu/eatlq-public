# B3 任务书（Base 适配 · 加固版）— RH「2 分钟龄加速」信号证伪

> **来源**：RH/CHA 侧候选信号 `structure_T2_liqgrow2`（4 天短样本 pooled +16.3%，top1% 仅 4.3%，四窗 +13/+16/+20/+24%）。
> **本 VPS 角色**：Base = 该信号的**证伪场**（真实 mint/burn 储备 + 真实持仓 + 7 个月多 regime）。
> **性质**：纯回测/研究。**禁止**下单/签名/广播；**禁止**启动/重启任何 live/executor/strategy 服务或改 live 配置；**禁止** codex 自判 GO；所有断言可复现。
> **加固人**：本 VPS 审查型 Agent，按《审查型指挥 Agent·角色宪章》§4 + 本项目 `permanent_guardrails.jsonl` / `research_principles.jsonl` + 家底报告 §5/§6 适配。
> **前置铁律**：本任务只回答「真储备/真 holder 下信号还成不成立？跨 regime 稳不稳？」；任何结果都**不触发 GO/训练/实盘**。

---

## 0. 一句话目标
在 Base 真实链上数据上复现并**尽力证伪** RH 的「2 分钟龄加速」信号：把 RH 只能代理的 liquidity（成交额/2）与 holder（transfer 接收地址）替换成**真实储备**与**真实持仓地址数**，在**跨真 regime 的多个不重叠窗口**上跑，用**真实成交撮合**入场/出场，并与**同龄无条件基线**对照，判定这是条件性 alpha 还是市场 beta / 代理偏差 / 单一时代过拟合。

---

## 0.5 建立在 B2 之上（数据现实 + 复用 + 边界，2026-07-14 B2 核验后新增）

B2（transfer-net 归因）已完成并经本 VPS 审查 Agent 逐行核验，裁决 **FAIL**（覆盖率救援未成立）。B3 必须继承 B2 的三条硬事实，否则会重蹈覆辙或高估可行性：

1. **本地 `erc20_transfers` 对历史几乎为空**（B2 实测：全表仅 ~28,939 行，且在评估窗口之前就断了；锁定窗内 `erc20_transfers_decoded` = **0 行**）。→ **B3 的真储备/真 holder 不能假设本地有 transfer 数据**，必须像 B2 一样用 archive RPC `eth_getLogs` 回填。**复用 B2 的 `fetch_target_transfers_logs` + `eatlq.base_target_transfers_b2` 机制**（按分片取 token、再按 `(tx_hash/token)` 精确过滤）。
   - **B3 的省钱点（与 B2 不同）**：B3 目标 token 是**刚创建 ~2min 的新池 token**，其**整条 transfer 历史 = 从 token genesis 到 `snapshot_block(T)` 的极短一段**。只需回填每个 token「诞生→2/5min」这一小片，而非 B2 那种全窗口回填。逐 token 边界回填成本远低于 B2。**codex 必须先报回填规模估算（token 数 × 每 token 早期 transfer 数 × RPC 调用）再动手**。

2. **B3 不依赖 B2 失败的那一环**。B2 FAIL 的是「resolved beneficial account」（哪个地址是交易者）——67.72% 与 tx_from 不一致，主因是受益方常落到**有 code 的合约**（AA/托管/结算，未打标）。**B3 是池级结构信号，不需要解出交易者身份**，因此 B2 的归因失败**不阻断 B3**。B3 只用到 B2 已 golden 验证的**余额数学**（transfer 净流 == `balanceOf`，池/地址都只是地址）。→ 报告里要写明这一点,避免人类误以为 B2 FAIL 连累 B3。

3. **B2 的余额 golden 只在 USDC（干净 6 位小数、无转账钩子）上验证过**。B3 的 memecoin target token 常有 **fee-on-transfer / rebase / 异常 decimals**，会**打破「Transfer 事件求和 == balanceOf」这条恒等式**。→ 见 §7：B3 golden **必须含至少 1 个 fee-on-transfer/非标准 token**，若恒等式不成立就如实记为储备重建的已知偏差并量化，不许假装 == 成立。

**从 B2 复用的资产**：`base_target_transfers_b2`（原始整数 transfer）、`base_account_code_b2`（`eth_getCode` EOA/smart_account 打标，供 holder 敏感性分析）、`configs/research/base_attribution_exclusions.json`（37 条基础设施+零地址，holder 去噪用）、archive RPC balanceOf 边界对账法、回归 == 断言范式。

---

## 1. 开工前：schema 自核对（不许凭记忆，照抄真实列名）
codex 必须先读并在报告里回抄以下真实列名，与本任务引用一致后才动手：
- `infra/clickhouse/init/003_normalized_events.sql`
  - `erc20_transfers`：`token_address, sender, recipient, amount_raw, block_number, tx_hash, log_index`。
  - `uniswap_v3_swaps`：`pool_address, amount0_raw, amount1_raw, sqrt_price_x96_raw, liquidity_raw, tick_raw`（signed int256，用 `eatlq_hex_to_int256`）。
  - `uniswap_v2_swaps`（含 Aerodrome vAMM 复用同表）：`pair_address, amount0_in_raw/amount1_in_raw/amount0_out_raw/amount1_out_raw`。
  - `uniswap_v3_mints/burns`：`pool_address, amount0_raw, amount1_raw, liquidity_raw, tick_lower/upper_raw`。
  - `uniswap_v2_mints/burns`：`pair_address, amount0_raw, amount1_raw`。
- `013_decoded_views.sql`：`erc20_transfers_decoded.amount`（uint256 数值）、`uniswap_v*_swaps_decoded`、`eatlq_hex_to_uint256/int256`。
- `026_discovered_pools.sql`：`pool_address, pool_type, token0/1_address, token0/1_decimals, fee_bps, has_weth_leg, created_block, created_tx_hash`。
- `035_block_ts_map.sql`：`(chain_id, block_number) -> block_time`（真实链上时间；本任务的 T=2min 全靠它）。
- `configs/research/base_attribution_exclusions.json`（B1 单一权威基础设施清单，本任务复用于 holder 去噪）。

> 若某表在目标窗口缺列/缺数据，**报出来，不许静默降级或换代理**。

---

## 2. 信号定义（移植自 RH，全部用 Base 真实字段）

对每个新池（`discovered_pools`，`has_weth_leg=1` 优先；V3 为主，V2/Aerodrome 同法各自单列）：

### 2.1 池龄快照（无前视地锚定 T）
- `created_time = block_ts_map(created_block)` 的真实链上时间；若 `created_block` 无真实 `block_time` → **丢弃该池并计数**，不许用 `received_at_ms`/arrival time 兜底。
- 定义 `snapshot_block(T)` = **满足 `block_time ≤ created_time + T` 的最大区块**（T=2min 主，T=5min 对照）。
- 半窗切点：`created_time + 60s`（前半 / 后半分钟）。
- **所有特征只允许用 `block_number ≤ snapshot_block(T)` 的事件**。任何用到 `> snapshot_block` 数据的特征 = 前视，直接判 FAIL。

### 2.2 三个加速特征（真实字段，前/后半分钟）
1. **成交加速**：`vol_2nd_half / vol_1st_half ≥ 2`（`vol_1st_half > 0`），成交额由 swap 的 WETH 腿真实数量（`uniswap_v*_swaps` amount，按 decimals 归一）求和。
2. **liquidity 加速（RH 的 liqgrow2，必须用真实储备）**：见 §3。定义 `reserve_growth = reserve_WETH(created_time+120s) / reserve_WETH(created_time+60s)`（或 RH 原定义口径，须在报告里写死）。**不许用「成交额/2」代理**。
3. **holder 加速（必须用真实持仓）**：见 §4。`holder_2nd_half / holder_1st_half`（或 RH 原口径）。**不许用「transfer 接收地址计数」代理**。
- **总量门**：总真实成交额 ≥ 阈值（阈值在 §8 预注册，禁止看结果再改）。

> RH 侧只有代理，故本任务的**核心交付**是「真实版 vs 代理版」对比（§6.3）——量化代理带来的偏差、以及信号在换成真值后是否还活着。

---

## 3. liquidity = **真实储备**（审查重点①，且我已修正 RH/用户原话）

> **⚠️ 指挥判定（对用户原话「由 mint/burn 事件重建储备」的修正，须照此执行）**：在 Base 上，池内 token/WETH 储备**不只受 mint/burn 影响，每一笔 swap、以及 donation/fee/直接转账都会改变储备**。**只用 mint/burn 事件重建储备是错的**（会漏掉 swap 驱动的储备变动，而本信号恰恰发生在成交活跃的头 2 分钟）。因此：

- **主口径（真储备）**：`reserve(token, pool, block B)` = 该 token 向 `pool_address` 的**累计净流入**，由 `erc20_transfers_decoded` 求得：
  `reserve = Σ(amount 收入 pool_address) − Σ(amount 转出 pool_address)`，`token_address ∈ {WETH, target_token}`，`block_number ≤ B`。
  - 这**等价于**链上 `balanceOf(token, pool_address)@B`，且天然包含 mint+burn+swap+donation+fee 的净效果，**并复用 B1/B2 已验证的 balanceOf 重建方法（池只是一个普通地址）**。
  - `liquidity` 主指标 = **WETH 腿储备**（quote-side depth，经济意义上的池深）；target-token 储备一并报出。
- **事件侧交叉校验（次口径，必须与主口径对账）**：用 `mints/burns/swaps` 的 amount 事件重建同一储备序列，与主口径 transfer-net 在 golden 池上**精确相等（==，整数）**。若不等 → 报根因（fee-on-transfer？rebase？漏事件？），不许平均掩盖。
- **golden 对账**：见 §7，用 archive RPC `balanceOf(pool)` 在 `snapshot_block` 边界与主口径整数相等。

**审查将 grep**：若脚本里 liquidity 特征来自「成交额」「volume/2」「notional/2」等 flow 代理而非上述 stock 储备 → 判 FAIL 打回。

---

## 4. holder = **真实持仓地址数**（审查重点②）

- `holders(token, block B)` = 在 `block_number ≤ B` 的 `erc20_transfers_decoded` 上，按地址求 target_token 累计净额，**净额 > 0 的去噪地址数**。
- **去噪（必须排除）**：`pool_address` 自身、B1 基础设施清单（Router/聚合器/Permit2/NPM/Quoter/EntryPoint）、零地址 `0x0…0`、销毁地址 `0x…dead`、WETH/稳定币合约、token 合约自身。
- **不许**用「`erc20_transfers` 里出现过的 recipient 去重计数」代理（那会把中转、临时收币、已清仓地址都算成 holder，且忽略卖出）。
- **golden 对账**：见 §7，抽 ≥3 个地址用 archive RPC `balanceOf` 在 `snapshot_block` 与重建净额整数相等；holder 总数在 golden 池上与「逐地址 balanceOf>0 计数」一致。
- **托管/合约污染敏感性（继承 B2 教训）**：B2 证明相当比例的受益方是**有 code 的合约（AA/托管/结算，未打标）**，这类合约可能替多个用户持币 → 把「多用户」压成「1 个 holder」，扭曲 holder 加速。故 holder 必须**同时报两版**：`holders_all`（所有 balanceOf>0 去噪地址）与 `holders_eoa_only`（用 `base_account_code_b2`/`eth_getCode` 滤掉有 code 的地址）。两版差异过大 → 标注该窗 holder 信号受托管污染、降置信。

**审查将检查**：holder 特征的分子是不是「distinct recipient」而非「balanceOf>0 地址」。是 → FAIL 打回。

---

## 5. 入场 / 出场 = **真实成交**（审查重点④，家底 §5 + FL6-001/RP6-002/RPW-005）

### 5.1 入场
- `signal_time = created_time + T`。入场时点 = `signal_time + 固定延迟 Δ`（Δ 预注册，见 §8；对照家底实测 ingest 滞后 avg 68s / p95 99s，Δ 不许取 0）。
- 成交价 = 入场时点之后**该池的下一笔真实 swap**（`uniswap_v*_swaps`，`block ≥ entry_block`）的**实际成交价**（由该 swap 的 `amount0/amount1` + decimals 解出，带方向）。
- **禁止**用 QuoterV2 报价 / tick 中价 / sqrt_price 现价作为成交价（家底 §5：QuoterV2 对 transfer-hook 蜜罐系统性盲，会虚构可成交）。
- 报出该笔 swap 自身的 WETH 规模，证明我们假设的仓位量级落在一笔真实成交之内（不夸大可成交性）。

### 5.2 出场（tp_ladder，但必须是**真实可卖**，不是纸面阶梯）
- tp_ladder：+30% / +60% / +120% 各 1/3；余量固定时限后下一笔真实成交。
- **每一档只能用「触发后该池真实发生的、方向为卖、成交价达到或穿越目标价」的 swap 来兑现**；填不到就是没卖掉，继续持有。
- **不可卖 / rug 处理（关键证伪杠杆 — 已按 CHA 准则 Q1/Q5/D5/D6 修正，2026-07-14）**：
  - 若快照后该池**不再出现卖方向真实成交** / 储备（§3）塌向 0 → 在本回测里按 **adverse 保守口径**记已实现亏损（地板 −100% 或最后一笔真实可卖价），**不许从样本中删除**（FL6-005 幸存者偏差 + RPW-005）。
  - **但必须如实标注为 `unverifiable_adverse`,不得写成"该币是蜜罐"的既成事实**。CHA C3/D6/Q5:数据是订阅式非全链(家底自承 28% 创建池未采到、mint/burn 仅 7 池),"没观测到卖出" 可能是**没采到**,不是卖不掉;可保守跳过/计亏,不可反推蜜罐结论。
  - **"卖得掉" 本身在 CHA 里永不由历史链上卖单证明**(D5:买/卖计数类启发式有假阴性)。历史第三方卖出只作背景启发式,**不作充分判据**。真正的可卖性只有两层:①进场前每笔原子 `eth_call` 买→卖 roundtrip sell-sim(D5,且仍只是 pre-trade sim,注意 QuoterV2 报价 ≠ sell-sim,见 §5.3);②唯一真证明 = forward live + 真实 size(FLW-006)。故本 backtest **只能证伪(在 adverse 口径下信号垮=真垮),不能认证可卖/可 GO**。
- 另跑一个**不封顶/持有到时限**的出场变体并报全分布——右尾凸性策略封顶阶梯可能砍掉产生 EV 的极右尾（RPW-003/010），须让人类看到两种口径。

**审查将检查**：出场是不是「只要价格曾达到 +X% 就算成交」的纸面撮合、以及不可卖样本是否被悄悄剔除。

### 5.3 QuoterV2 ≠ sell-sim（CHA C1/D5 + Base FL6-001 认定，非本 agent 私推）
- QuoterV2 `quoteExactInputSingle` 只算 AMM 价格数学:卖出 quote 里池子乐观先转出 WETH、回调 revert 拿数,**输入侧那笔"卖出土狗币"的转账从不结算**,故对**卖出型 transfer-hook 蜜罐结构性失明**。
- 状态:**机制自洽 + 已被 Base 两次真金蜜罐实亏(−100%×2, FL6-001)印证 + 与 CHA C1/D5 一致**。要干净证明,做判决性实验:对已知蜜罐样本比 QuoterV2 报价 vs 真实原子 `eth_call` sell-sim 的分歧率,量化它谎报多少(单列任务,见回报)。
- 本 B3 入场/出场**一律禁用 QuoterV2**;可卖性判据依 §5.2。

---

## 6. 无条件基线 & 真实/代理对比（审查重点⑤，RPW-006 case-control 强制）

### 6.1 无条件同龄基线
- 在**同样的窗口、同样的入场/出场机制**下，对「同龄 2min 的随机/全量新池买入」建基线（不施加加速信号条件）。
- 信号 alpha = 条件组收益 − 无条件基线收益。**必须证明是条件性 alpha，而非「买任何 2min 新池都涨」的市场 beta**。

### 6.2 分层基线
- 至少再给一个「只过总量门、不过三加速」的中间基线，隔离「加速」本身的增量 vs「大盘/流动性门槛」的增量。

### 6.3 真实版 vs 代理版（核心交付）
- 同一套池、同一窗口，跑两版：
  - **代理版**：liquidity=成交额/2，holder=transfer 接收地址去重（复刻 RH 口径）。
  - **真实版**：liquidity=真储备（§3），holder=真持仓（§4）。
- 量化：信号命中集合差异、pooled/per-window 收益差异、top1% 贡献差异。回答「RH 的 +16.3% 有多少是代理假象」。

---

## 7. Golden 对账（独立于工具口径，家底 §3 缺 golden 归因基准，本任务补上）
- 抽 **3–5 个池**，`tests/fixtures/` 落 golden fixture。**必须含至少 1 个 fee-on-transfer / 非标准 decimals / rebase 的 memecoin token**（B2 golden 只覆盖了干净的 USDC，未压测转账钩子）。若该 token 上「Transfer 求和 == balanceOf」恒等式不成立，**如实记为储备重建的已知偏差并量化其大小**，不许假装 == 成立；并说明这类 token 在信号命中集里的占比。
- 每池在 `snapshot_block(2min)` 边界：archive RPC `balanceOf(pool, WETH)` 与 `balanceOf(pool, target_token)` == 主口径重建储备（**整数精确相等**，对标准 token）。
- holder：每池抽 ≥3 个持有地址，archive RPC `balanceOf@snapshot_block` == 重建净额（整数）；holder 计数与「逐地址 balanceOf>0」一致。
- ≥2 笔入场/出场 swap 在 Basescan 人工核对 `block/tx_hash/amount/方向`，报告附链接。
- **禁止**只把 view 输出与另一段 SQL 自比（工具口径自比不算 golden）。

---

## 8. 预注册判据（跑之前定死，看到结果不许改；宪章 §4 + RPW-007）

> **⚠️ 指挥裁定（2026-07-14，回应 codex 第 1 步「卡住」报告，覆盖任何"必须 7 个月"的死板读法）**：
> **A. 时长不是判据,跨 regime 才是。** 用当前**能严格无前视锚定 T=2min 的干净 universe**即可——即 codex 报的 **3,331 池 / 3,330 token（2026-02-27 → 2026-06-08，约 3.3 个月）**。不为凑 7 个月去回填全库 51,225 token（153,913 次 RPC）或扩 `block_ts_map`。**唯一不可放松的是:这 3.3 个月必须被切成 ≥2–3 个不重叠窗口,且用外生度量证明它们确属不同 regime**（家底已记录 rug edge 逐月衰减、5 月 rug 供给一度断供 → Feb–Jun 大概率含牛/衰减/横盘变化,但**须 codex 用度量证明,不许口头断言**）。若这 3.3 个月经度量确认是**单一 regime**,才升级讨论是否扩 map;不是先扩。
> **B. token genesis 是伪死结,砍掉。** 本信号的三个特征都是**比率/增量**（`reserve_growth`、`vol_2nd/1st`、holder 加速）——**只需 `created_block → snapshot_block(T)` 这一小段 pool-era transfer,不需要 token 从 genesis 起的完整历史**。因此 codex 提的「eth_getCode 二分定位部署块（26 calls/token、8.6万–133万次）」**不批准、不需要**。储备重建只看流入/流出**池地址**（池在 created_block 前不存在,天然从 created_block 起算）；holder 增量只看窗口内新达 net>0 的地址。
>   - **premint/空投兜底(便宜)**：holder 绝对量若受 pool 前预铸影响,用**有界回看**（`fromBlock = created_block − 约1天区块`,仍 1 次 eth_getLogs/token）捕获临近预铸即可,不做无界 genesis 扫描。
>   - **「旧 token 新建池」单独处理**：codex 已发现这类池（token 远早于建池、带存量 holder,会污染"新池 holder 加速"）。**检测（created_block 前该 token 已有 transfer）→ 主 universe 只留"鲜 token（部署≈建池）",旧-token-新池单列报告**,不混入主判据。
> **C. 批准的 RPC 成本**：仅**当前 3,331 池子集的 transfer 回填 ≈ 9,992 次 eth_getLogs**（token 全历史 1 次/token + 池双向 2 次/池）。5min 对照复用同请求。**不批**全库上限与 genesis 二分。codex 若要跑一个小成本探针先校准分片倍率,可批 ≤ 500 次的一次性探针,报回结果再定。

codex 在**跑任何回测前**，先把下列全部写进报告并冻结（数值由 codex 依 RH 原始定义 + Base 数据规模填，填完即锁）：
- 总成交额阈值、Δ 入场延迟、tp_ladder 时限、reserve/holder 加速阈值（=2）。
- 窗口：在上述 3.3 个月干净 universe 内切 **≥2–3 个不重叠窗口**，每窗由**外生市场变量**（预注册：如 ETH 趋势 + 全网新池创建率/大盘 memecoin 成交额）标注 regime，**报每窗外生度量值 + 每窗池数 + 预估条件命中 n**（每窗命中 n<20 打问号,须提示）。**不许都压在同一小段行情**（宪章「过拟合于单一时代」+ RPW-001）。

**右尾 PASS（预注册，四项全中才算「真值下信号成立」）**：
1. pooled mean > 0（真实版）；
2. **每窗不崩**：设明确下限（如每窗 mean ≥ 预注册地板，且 ≥ 同窗无条件基线）；
3. **top1% 贡献低**（广谱，非几笔撑起来；RH 侧 top1%=4.3% 作参照，Base 须自证）；
4. MC 终值 p5 达标 + 回撤 p95 达标（bootstrap/MC 预注册）。

**NEEDS_REVIEW**：真实版 PASS 但代理版差异巨大（提示 RH 结论是代理假象）、或某窗 n 过小（<20 打问号，n=1~4 不下结论）、或 golden 有不符。
**FAIL（信号被证伪）**：真实版 pooled ≤ 0、或不优于无条件基线、或只在单一 regime 成立、或收益全靠 top1%、或跨窗非单调（噪声）。

> 判据禁用「中位数>0」「方差最小化」等左尾/对称市场口径（宪章 §4 + RPW-003）。

---

## 9. 必须产出
1. 新增 CH view/表（research，命名如 `eatlq.base_pool_2min_snapshot_b3`；`--install` 只碰 research 表，不动 live）。
2. 快照/特征/回测脚本（research）+ 可复现命令。
3. 真实版 vs 代理版 + 无条件/分层基线 结果对比（JSON + md，含每窗 n、regime 度量、top1% 贡献、MC/回撤）。
4. Golden fixture + 3–5 池 balanceOf 边界对账 + Basescan 锚点。
5. 数据覆盖体检（FLW-007）：`erc20_transfers`/`mints`/`burns`/`swaps` 在每窗的 min/max block、行数；确认 2min 快照块均有真实 `block_time`（无 arrival fallback），报丢弃池数。
6. 回归测试（`unittest`），断言见 §10。
7. 研究报告 md：明确写 "research-only, no GO"；结论回答两问（真值下成不成立 / 跨 regime 稳不稳）。

---

## 10. 回归测试断言（宪章点名：用 == 不许偷改 >=）
必须包含且用**精确相等**：
- golden 每池 `reserve_reconstructed(WETH/token)@snapshot_block == balanceOf 边界原始整数`（整数，禁 `assertAlmostEqual`）。
- 事件侧储备重建 == transfer-net 主口径（golden 池，整数）。
- holder：golden 地址 `net_balance == balanceOf`（整数）；holder 计数 == 逐地址 balanceOf>0 计数。
- **无前视**：断言所有特征输入 `block_number ≤ snapshot_block`；断言无 `received_at_ms`/arrival-time fallback（只用 `block_ts_map`/真实 `block_time`）。
- 基础设施/池/零/dead 地址**不得**计入 holder（`assert count == 0`）。
- 入场/出场价来自真实 swap 行（用固定 fixture swap 断言），**断言未调用 QuoterV2/中价**。
- 不可卖 fixture 池：断言其被计为已实现亏损而**非被剔除**。
- 窗口不重叠：断言窗口区间交集为空。

> 审查方（我）会 grep 测试文件里的 `>=` / `assertGreater` / `assertAlmostEqual` / `assertLessEqual`，出现在本应 `==` 的护栏处即视为放水，打回。

---

## 11. 铁律（重申）
- 纯回测研究：**不下单 / 不签名 / 不广播 / 不启动或重启任何执行服务 / 不改 live 配置**。
- codex **不自判 GO**；任何结果都不触发训练/实盘/投钱。
- 所有结论配人类可独立复核锚点（golden、Basescan、回归断言、可复现命令）。
- codex 完成后**逐项回报**，由本 VPS 审查 Agent 逐行核验，再交人类做跨项目综合。

---

## 附：本任务与 RH 的关键差异（供人类跨项目综合时对齐口径）
| 维度 | RH（4 天短样本） | Base（本任务） |
|---|---|---|
| liquidity | 成交额/2（flow 代理） | **真储备**（transfer-net = balanceOf(pool)，含 swap） |
| holder | transfer 接收地址计数 | **真持仓**（balanceOf>0 去噪地址数） |
| 成交 | （RH 口径） | **触发后下一笔真实 swap**，非 QuoterV2/中价 |
| 可卖性 | 未知（大概率纸面阶梯） | **真实卖方向 swap 兑现；不可卖=已实现亏损，不剔除** |
| 样本时代 | 4 天单一行情 | **≥2–3 个跨 regime 不重叠窗口（3.3 个月干净 universe）** |
| 基线 | （RH 口径） | **无条件同龄 case-control + 分层基线** |

---

## 附二：第 2 步（预注册）审查记录 + 稀有性阶段结论（2026-07-14，本 VPS 审查 Agent）

**状态**：回填与回测**未执行**，停在锁参数前。以下为审查记录，供人类 / 跨项目综合 agent 使用。

### 已核验（codex 第 2 步产出，逐项审）
- **窗口/regime**：3,331 池干净 universe（2026-02-27 → 06-08）切三窗：W1(407 池, ETH+4.75%, 横盘/launch衰减)、W2(1,547 池, ETH+8.76%, 横盘/供给暴增+需求低迷)、W3(1,377 池, ETH−30%, 熊市/launch成交回升)。**宏观其实只两档(横盘、熊)**，W1/W2 差异在 launch 微观结构——如实记，不包装成三独立宏观 regime。
- **冻结参数**：总量门 vol1+vol2≥0.01 WETH；仓位 0.001 WETH；三加速阈值均 =2；Δ入场延迟=120s(→最早入场池龄240s，>家底 ingest p95≈99s，偏保守可接受)；TP +30/+60/+120%×1/3，60min 时限，60–120min 终局卖出，未售 −100% 不剔除；不封顶变体；bootstrap 1万次 seed=20260714。
- **纪律**：seed 预注册、拒绝移窗/降门槛造样本、swap recipient 只作工作量估算不进真实特征、真实/代理 Jaccard 门 + EOA-only vs all-holder 门 + freshness 门齐备。**codex 主动自曝 v1 无法取得干净跨-regime PASS**（诚实,非提前裁定）。

### 打回 codex 的 2 项（修好才允许回填）
1. **`ROUND_TRIP_COST=0.006` 严重低估成本**：0.001 WETH(~$3.5)仓位下，家底 gas ~$0.15/往返 ≈ 4%，尚未算薄池滑点。低估成本 = 右尾假阳性来源。须改为绝对美元 gas + 真实滑点，或放大仓位并给依据。
2. **判据优先级歧义**：须定死「样本不足(n<20)= 未测试 → NEEDS_REVIEW/INCONCLUSIVE，不得当 FAIL/证伪」；FAIL 仅留给「有足够样本的窗实测 ≤0」或「W3 自身崩」。避免把"没测成"误报成"打死了"。

### 稀有性阶段结论（关键区分，勿过度解读）
触发计数(成交加速门+总量门，代理估计)：**W1≈17、W2=0、W3≈57，仅 W3 达 n≥20；真实版三加速过滤后只会更少**。

小 n 有**两个来源，权重不同**：
- **(a) universe 被 `block_ts_map` 覆盖卡在 3,331/51,225 池(~6.5%)** = **工具限制,不是市场事实**。扩 map 约可放大 ~15x → n 可到 250–1100。
- **(b) 每池触发率本身低且 regime 依赖**：~4%(W1) → **0%(W2, 供给暴增却零成交)** → ~4%(W3, 熊市) = **真实市场事实**。

**因此的诚实判定**：
- 这是对**「可移植跨-regime 稳定 alpha」的部分负面证据**——信号触发**频率被 regime 门控**，在供给暴增/需求低迷的 W2 整整一个月**零机会**；触发集中在 W3(熊市+launch回升)这一窄条件。对照 RH 4 天密集样本,提示 RH 的强度大概率活在"触发密集的某个 regime"。
- 但**尚未触及每笔 EV / merit**：真实储备/真实 holder / 真实成交撮合 / 真实成本的回测**没跑**,且 n 太小本就不能下 EV 结论。故 merit = **INCONCLUSIVE(未测试)**,不是 FAIL(证伪)。
- **不能用 (a) 冒充 (b)**：不能因"只instrument了6.5%的池"就说"信号稀有"。稀有性主张只能建立在触发**率**(每池%)上,而它确实 regime 依赖。

### 交人类的问题
「值不值得继续」= 是否愿意投 RPC 扩 `block_ts_map` universe 去换一个 powered 的跨-regime merit 判据。参考家底既有事实(Base edge 逐月衰减、小资金结构性不匹配、rug 供给 5 月断供),该信号又稀有且 regime 门控 → 继续的边际价值需人类权衡。本 Agent 不自判 GO。
