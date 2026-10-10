---
sop_applied: true
alpha_id: AR-SP085-WALLET-COND-POOL-ENTRY-V0
family: F3
next_gate: Phase 1 historical rolling OOS dataset
decision_flags:
  training_allowed: false
  live_allowed: false
  wallet_direct_copy_open: false
  wallet_conditioned_research_open: true
  wallet_conditioned_live_allowed: false
  paper_forward_allowed: false
  model_promotion_allowed: false
---

# Wallet-Conditioned Pool Entry Model v0 — Strategic Charter

> **Status:** RESEARCH_ONLY — Route C of SOP Family F3 (see `docs/alpha-sop-decision-tree.md` §7.5).  
> **Supersedes:** direct smart-wallet copy (Route A, closed Sprint083).  
> **Does not replace:** Base V3 onset / pool-entry quality mainline (Sprint084).  
> **Sprint workspace:** `mvp/audits/sprint085_wallet_conditional_entry/`

---

## 钱包路线重启方案：从"聪明钱包跟单"改造成"钱包条件化入场模型"

这条路线不再叫 smart-wallet copy，也不再相信"历史 ROI 最高的钱包未来还能继续带我赚钱"。旧版本的问题是把钱包当成神，把历史胜利者当成可复制对象，结果容易掉进幸存者偏差、单池暴富、whale-size 不可复制、延迟后收益衰减、未闭环持仓虚高这些陷阱。新版本必须把钱包降级成一种条件信号源：钱包本身不值得信，只有当某类钱包行为出现在某类池子结构、某类 onset、某类流动性、某类买压、某类延迟成本、某类退出条件下，仍然能在我的真实执行条件里产生正期望，它才有价值。

核心目标不是找"最牛钱包"，而是找：

在我能观察到它之后，
我用自己的 size 延迟 60/120/300 秒复制，
扣除 gas、滑点、Quoter、失败卖出、持仓时间限制后，
是否仍然有可重复、低集中度、可闭环的正收益。

### 一、钱包不再按总 ROI 排名，而按可复制能力排名

以后钱包评分不看单纯 ROI，也不看"赚过多少钱"。钱包必须按滚动窗口计算，而且只能使用当时已经可见的数据。每 7 天 freeze 一次钱包 universe，用过去 30 天或 60 天已经闭环的交易来排名，然后观察未来 7/14/30 天表现。不能今天回看过去 90 天找赢家，再说它未来也会赢。

钱包基础门槛应包括：

- closed trades 数量足够
- 交易 token / pool 数量足够
- 最大单池利润占比不能过高
- 最大单 token 利润占比不能过高
- 最大单笔交易利润占比不能过高
- 去掉最大赢家后仍然赚钱
- 去掉最大池后仍然赚钱
- 去掉最大 token 后仍然赚钱
- 延迟 60/120/300 秒后收益仍然存在
- 用 10U / 25U / 100U 模拟后仍然能成交
- T+N 内有完整退出或可判定 MTM

不选 top 1% 神钱包，优先选中高分、稳定、多交易对、多闭环、低集中度的钱包。极端暴富钱包不是主目标，因为它们更可能是 insider、单池奇迹、早期埋伏、whale-size 特权或不可复制噪音。

### 二、训练样本不是"钱包"，而是"我当时能不能做的一笔候选交易"

每一行训练数据不应该是一整个钱包，而应该是一笔 candidate trade：

某个时间点，
某个钱包买入某个池，
我在链上看到这笔交易后，
延迟 X 秒，
以我的资金规模尝试复制，
未来 30m / 4h / 24h / 7d 是否赚钱。

样本必须带上三类信息。

**第一类是钱包侧特征：**

- rolling closed-loop PF
- rolling win rate
- rolling Sharpe
- 过去 30/60 天 closed trade count
- token diversity
- pool diversity
- top_pool_profit_share
- top_token_profit_share
- median hold time
- median trade size
- delay survival score
- 过去失败交易比例

**第二类是池子侧特征：**

- price_velocity_5m
- buy_pressure_ratio
- activity_heat
- token_volatility_5m
- pool_age_sec
- fee_tier
- liquidity / quoteability
- buyer diversity
- same-block toxic flow
- early concentration
- onset_lag

**第三类是复制侧特征：**

- detection_delay_sec
- copy_delay_sec
- 10U / 25U / 100U notional 下的 Quoter amountOut
- slippage
- gas cost
- 是否能卖出
- 是否 honeypot / sell revert
- 是否 whale-size 不可复制

最后的标签不能是"这个钱包历史赚了没"，而必须是：

我在实际可观察时间点复制它，
用我的 size，
在指定 horizon 内退出，
扣成本后是否赚钱。

### 三、必须同时训练成功和失败，不训练幸存者神话

不能只拿成功交易训练。成功样本只能告诉模型"成功者长什么样"，但不能告诉模型"失败者是不是也长这样"。如果成功交易和失败交易都有高 buy pressure、高 heat、新池、聪明钱包买入，那只看成功样本就会把噪音当成 alpha。

正确做法是让模型学会区分：

- 钱包买了且我复制后赚钱
- 钱包买了但我复制后亏钱
- 钱包买了但延迟后 edge 消失
- 钱包买了但 size 不可复制
- 钱包买了但卖不出去
- 钱包买了但收益来自单池极端事件
- 类似池子里钱包信号出现但没有后续收益

可以做两个模型，也可以做一个模型。两个模型的结构是：

- **success_model：** 识别什么情况下成功概率高
- **failure_model：** 识别什么情况下失败概率高

最终只做：

- success_score 高
- failure_score 低
- delay_survival 高
- size_transferability 高
- quoteability 高
- concentration_risk 低

但失败模型必须有失败样本。否则所谓"高置信度"只是模型自嗨。

### 四、四个 trap 直接写进 gate，不靠主观判断绕过

**幸存者偏差的处理方式：**
所有钱包排名必须 rolling freeze-time。每 7 天重新排名一次，只使用当时已经闭环的数据。历史 top wallet 只能进入候选池，不能直接证明未来可复制。

**延迟与抢跑的处理方式：**
所有标签都必须按复制延迟重算，至少测试 60s、120s、300s。若钱包成交价赚钱，但复制价不赚钱，判定为不可复制。

**size 不可移植的处理方式：**
所有收益都必须用我的资金规模模拟，至少测试 10U、25U、100U。whale-size 成交不能直接迁移到小资金；如果只有大钱包自己能吃到价格，我不能复制，就不能作为 alpha。

**闭环判定的处理方式：**
所有交易必须在 T+N 内闭环，N 可取 30m、4h、24h、7d、30d。没有卖出的仓位不能直接按浮盈算胜利。长期未卖出的钱包可能是信仰仓、早期持有者、锁仓者，不是可复制交易者。

### 五、钱包路线的三种用途必须分开

**第一种：direct smart-wallet copy。**
这条主线关闭。历史 top 钱包直接跟单已经被证明风险太高，容易变成单主体、单池、whale-size、延迟衰减叙事。除非未来有非常强的 forward 证据，否则不再作为主线。

**第二种：wallet diagnostic。**
这条保留。钱包特征用于识别毒性、抢跑、单主体、whale artifact、copy-delay 衰减、deployer/funder/router 关系、资金来源异常。它服务于 pool-entry quality，不直接给 entry alpha。

**第三种：wallet-conditioned pool entry model。**
这条可以作为支线重启。它不问"哪个钱包值得跟"，而问"某类钱包行为在某类池子结构里是否提高我的可复制胜率"。这条线可以训练，但必须从交易级样本、失败样本、rolling OOS、delay-adjusted label 开始。

### 六、训练与回测必须按时间滚动，不能偷看未来

每 7 天滚动重排钱包、重训模型可以做，但必须遵守标签成熟期。比如要训练 7d 标签，就只能使用已经过完 7 天观察期的交易；要训练 30d 标签，就只能使用 30 天前已经闭环的数据。不能拿最近刚发生的交易训练，因为它的失败可能还没暴露。

推荐节奏：

- 每周一 freeze wallet universe
- 用过去已闭环的 30/60 天数据训练
- 未来 7 天只 paper shadow / dry-run
- 下周评估 forward-only 表现
- 成熟标签再进入下一轮训练

评价时必须严格区分：

- historical replay
- rolling OOS
- paper shadow
- forward dry-run
- live-market shadow
- canary live

任何 historical replay 都不能冒充 forward proof。

### 七、进入 paper-shadow 前的最低条件

wallet-conditioned 模型不能一训练就接 paper-forward。先做 shadow：

- rolling OOS 至少覆盖多个 freeze windows
- forward candidate trades 数量足够
- remove top wallet / pool / token 后仍然正
- 60s / 120s delay 后仍然正
- 10U / 25U notional 后仍然正
- Quoter 成功率可接受
- sell path 成功率可接受
- 失败样本覆盖足够

若这些不满足，继续作为 research / diagnostic，不进入 SOP promotion。

### 八、进入 dry-run / human live review 的条件

paper shadow 通过后，才进入 dry-run。dry-run 必须有完整仓位状态、entry、exit、费用、滑点、cooldown、止损、失败卖出处理。不能只记录"信号后涨了"。

进入 human live review 至少需要：

- forward-only n >= 100
- win rate >= 50%
- PF >= 1.2 或更高
- top_pool_share < 25%-35%
- top_wallet_share 不集中
- top_token_share 不集中
- delay-adjusted PF 仍然正
- size-adjusted PF 仍然正
- sell_path_ok
- panic_gate_ok
- allowance cap 人工确认
- private key protocol 人工确认

满足这些也不是自动实盘，只能进入 READY_FOR_HUMAN_LIVE_REVIEW。真正实盘只能从 10U/小额 canary、单笔人工批准开始。

### 九、和当前主线的关系

当前主线仍然是：

SOP old forward line
+
Base V3 pool-entry quality

钱包路线不抢主线资源。它的地位是：

- direct copy：关闭
- diagnostic：保留
- wallet-conditioned ML：支线重启

如果它要重启，必须用已有 SOP 守门，不允许因为"钱包叙事性感强"而跳过 paper shadow / dry-run / forward gate。

### 十、最终执行方向

下一步如果要重新打开钱包研究，不要再做"找 top wallet"。要做一个新的研究包：

**Wallet-Conditioned Pool Entry Model v0**

目标不是找到神钱包，而是生成一个交易级数据集，验证：

钱包行为 + 池级 onset + 可复制延迟 + 我的 size
是否能形成稳定、低集中度、可闭环的正期望。

第一阶段只做历史重构和 rolling OOS；第二阶段 paper shadow；第三阶段 dry-run；第四阶段才是 SOP review。任何阶段只要出现单池暴富、延迟后消失、size 不可复制、未闭环收益虚高，就降级为诊断器，不继续烧主线预算。

**最终原则：**

- 不训练赢家神话。
- 不复制历史 ROI。
- 不相信单池暴富。
- 不跳过失败样本。
- 不把钱包当神。
- 只验证：在我的真实执行条件下，某类钱包行为是否仍然有剩余 edge。

---

## 附录：与 SOP / Sprint083 / Sprint084 / Sprint061 关系映射

| 本 charter 章节 | 仓库 artifact / SOP 锚点 | 关系 |
|---|---|---|
| 五·第一种 direct copy 关闭 | `mvp/audits/sprint083_fee500_paper_shadow/v3_mainline_decision_flags.json` (`SMART_WALLET_DIRECT_COPY_STATUS=REJECT_NOT_COPYABLE`) | Route A 已封档；SOP §7.5 Route A |
| 五·第二种 wallet diagnostic | `scripts/sprint080_sender_backfill.py`; alpha_009 (0x6ff5 presence); `infra/clickhouse/init/038_recipient_profile_views.sql` | Route B 保留；服务 F1/F2 onset gates |
| 五·第三种 wallet-conditioned ML | 本文件 + `mvp/audits/sprint085_wallet_conditional_entry/` | Route C 重启；SOP §7.5 Route C |
| 一·四 trap gates | SOP §1.1 Hard Reject + Sprint083 copyability closure flags | 四个 trap 已写入 closure 证据 |
| 六·rolling OOS / freeze-time | `scripts/v3_smart_wallet_roundtrip_dataset_builder.py`; `scripts/forward_race_harness_core.py` | Phase 1 复用现有 builder + forward harness |
| 七·paper shadow 门槛 | `scripts/sprint055_fee100_paper_shadow.py` schema; Phase 3 新增 `sprint085_wallet_cond_paper_shadow.py` | 新 cron **新增**，不替换 sprint055/068 |
| 八·human live review | `mvp/audits/sprint061/live_readiness_closure_matrix.csv` | allowance cap / private key / PANIC 人工 gate |
| 九·与主线关系 | `mvp/audits/sprint084_base_v3_onset_pool_regime_handoff.md`; `mvp/audits/sprint084_pool_entry_quality/` | 叠在 pool-entry quality 之上，不抢资源 |
| 全局 guard | `docs/alpha-sop-decision-tree.md` §0.4; `mvp/audits/failed_no_7d_alpha_guard.md` | `TRAINING_ALLOWED=false`, `LIVE_ALLOWED=false` 直至 supersession |

**正在运行的 shadow / dry-run（本路线不得停止）：**

| 组件 | 脚本 | 输出 |
|---|---|---|
| Track A fee100 paper shadow | `scripts/sprint055_fee100_paper_shadow.py --once` | `logs/sprint055_paper_shadow.csv` |
| Multi-alpha shadow (含 alpha_009) | `scripts/sprint068_multi_alpha_runner.py --once` | `logs/sprint068/alpha_*_paper_shadow.csv` |
| Daily monitor | `scripts/sprint056d_paper_shadow_monitor.py` | `mvp/diagnostics/sprint055/paper_shadow_daily_*.json` |
| Forward harness | `scripts/sprint077_forward_harness.py --once` | `logs/sprint077/` |
| Forward dry-run ledger | `scripts/sprint069_forward_dryrun.py` | `mvp/audits/sprint069_forward_dryrun/` |
