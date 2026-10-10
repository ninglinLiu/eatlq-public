# B5 · 任务书:KEYSTONE 按 CHA 金律重验(D5 + 限价卖 + 跨 regime + forward)

> 审查 agent 下发 / codex 执行。纯回测/只读模拟,不下单/签名/广播/改 live。研究只证伪,不 GO。
> **排在 B4 之后**:B5 的可卖性门(D5)依赖 B4 先把 QuoterV2 vs sell-sim 分歧率钉死。B4 未过不发 B5。

## 0. 为什么重验

KEYSTONE 的 alpha 被"反复证明又反复证伪",很怪。诊断:历史回测/审计混用了 (a) QuoterV2-only 可卖性(B4 将证其对 honeypot 失明),(b) 单段行情,(c) 可能的中价/市价成交假设。用户两问:
1. **防 rug**:上 D5 原子 sell-sim 前置门后,左尾(rug/蜂窝)是否被实质砍掉,EV 是否翻正?
2. **迁移 RH**:KEYSTONE 的 edge 是**结构性可迁移**,还是**只吃 Base 特定微结构**(链上归因/2s 块/特定 DEX)?

## 1. 重验四支柱(全部预注册)

1. **可卖性门 = D5 原子 sell-sim**(替代 QuoterV2-only)。买入前 pin 当前块做买后即卖往返;卖不回 / 隐含税 ≥ 阈值 → 判 `unverifiable_adverse`,**不删行**,按不可卖计损。历史 rug 用 fork-replay/原子 ground-truth 打标,禁止用"有没有第三方卖过"当可卖证据(D5)。
2. **限价卖出场**(非市价/中价):出场用**限价单可成交的真实价**(下一笔真实成交里 ≥ 限价的那笔),模拟真实滑点/未成交;未成交余量按时限后真实成交或不可卖计。
3. **跨 regime**:复用 B3 的不重叠窗口切法(sideways / bear,至少 2 个可比 regime),每窗独立报,不允许压在单段。
4. **forward / OOS**:留出末段 holdout,in-sample 定参数、out-of-sample 只验,不回看改参。

其余:真储备/真 holder(相关处)、真实成交价、无前视、成本用 B3 冻结成本模型(真 gas、含费含冲击、$0.075/腿)。

## 2. 预注册右尾判据(禁止看结果改阈值)

pooled mean>0;每 regime 窗设明确下限不崩;top1% 贡献低;有 power 的窗(n≥20)才跑 MC 终值 p5 / 回撤 p95;n<20 只描述不判 merit(沿用 B3 判据优先级)。

## 3. 必须产出(两问各给硬答案)

> **B4 臂 A 已改写防 rug 方向(必读):** Base 左尾 260/262 是时间性 LP-pull,不是入场 transfer-hook;240/244 rug 在出场后才翻转,只有 4/262(1.5%)造成真损失。→ **点态 sell-sim(D5)不是 Base 防 rug 主杠杆,快出场时序 + forward LP 监控 才是。** D5 只对(罕见的)入场即蜂窝 + RH 迁移(异链蜂窝构成可能不同)有价值。B5 防 rug 必须同时测这三条,不能只测 D5 门。

- **防 rug(三杠杆分别量化)**:
  1. **快出场时序**:KEYSTONE 现有出场是否真躲过 rug —— 复算 flip_block vs 实际出场块,给"出场前翻转(真损)/出场后翻转(躲过)"占比,并检验该时序优势**跨 regime 是否仍成立**(B4 臂 A 只在单段)。
  2. **forward LP 监控**:模拟持有期内对 `WETH.balanceOf(pool)` 的 forward 读,能否在 flip_block 前后及时触发退出;量化能救回多少那 4/262 真损失 + 早翻转(offset≤300)样本。
  3. **D5 原子 sell-sim 门**:开/关两版收益对比 —— 量化左尾被砍多少、EV 是否翻正、挡掉样本里事后确为 rug 的比例(交叉 B4/fork ground-truth);**明确 D5 对 Base 左尾的边际贡献可能很小**(因入场谎报罕见),据实报。
- **迁移 RH**:把 KEYSTONE 特征拆成 (a) 依赖 Base 独有字段(链上归因/储备/holder) vs (b) 市价可得的通用特征;报"只用通用特征"的退化版收益 —— 通用版还剩多少 edge = 可迁移性上界。
- **无条件 + 分层基线**对照(同 B3)。
- 真储备/真 holder vs 代理版偏差(若 RH 版会用代理)。
- golden 3–5 例链上独立核对;回归测试,断言 ==。

## 4. 铁律

纯回测/只读模拟,pin archive block;不下单/签名/广播;不启动/重启 executor/strategy 服务;不改 live 配置;不自判 GO;所有结论可复现。任何"数据缺失"一律保守作 adverse,不作有利推断。
