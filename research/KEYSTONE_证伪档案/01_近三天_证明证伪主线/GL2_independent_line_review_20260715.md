---
sop_applied: true
alpha_id: AR-GL2-THREE-OPENS
family: F1/F3/F4/F6/F7
next_gate: HUMAN_LINE_REVIEW_ONLY
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL2 三条历史 OPEN 独立逐行终审

审查角色只读取 A/B/C 的预注册、实现、机器结果、报告、测试和 hash；没有改写三条结果、GL1 账本或 registry。分级对象是原 GL1 主张，而不是某个工程子门。最终裁决为：`GL1-12=OPEN`、`GL1-19=OPEN`、`GL1-18=OPEN`。三条都获得了可复核的缩窄事实，但没有一条满足升为 PINNED/IRON 的完整识别条件；`GO=NOT_MADE`。

## GL1-12｜完整无条件 EV 与 KEYSTONE alpha/beta

**分级：OPEN。**

1. 预注册正文 SHA256=`331009ad147eedc5189fa9f7ab5651b479d5065ec6cd29532816ecbf6e17ed07`，早于本轮脚本、RPC 和结果。正文却把退出写成 `b0+2/+6/+16`；GL1-12 中待解释的约 +56% 来自 `trigger_block+1/+5/+15`。立即 round-trip 与约四小时持有后的秒级退出不是同一 estimand。最终结果正确地把立即 ladder 降为旁证，没有拿它解释 +56%。这个前冻结 estimand 缺陷本身阻止把本轮称为无瑕疵的 GL1-12 定案。
2. 权威 trigger 锚下，全宇宙逐窗为 `282/431、279/431、278/431` resolved，残余 unresolved 为 `149、152、153`；KEYSTONE 为 `112/116、110/116、109/116` resolved。缺失没有插补为 0 或 -100%。
3. 已定价子集的同窗分解可复算：+1 的 beta=`+3.3086%`、条件 alpha=`+56.5323pp`、KEYSTONE=`+59.8409%`；+5 为 `+3.5113% / +57.6609pp / +61.1722%`；+15 为 `+3.9426% / +58.2282pp / +62.1708%`。这只说明已定价子集的带符号占比约 `93.66%–94.47%` 来自选择差、`5.53%–6.34%` 来自同时窗 beta；它不是 431 行完整宇宙的 alpha/beta 分解。
4. trigger 五证据初筛曾把 `141/143/145` 个点标成 rug。复核发现：完整 swap 索引虽覆盖 153 个未决池、11,287 个事件且未发现截止前后续卖单，但 E4 只有 cutoff 单点 atomic，不能排除中间曾恢复。因此全部初筛 rug 已降回 unresolved，confirmed rug=`0/0/0`，没有一条被记 -100%。此降级符合冻结铁律。
5. 回归测试 8/8 通过；A hash ledger 逐项校验通过。测试能证明当前保守结果自洽，不能消除 149–153 个未知。

**权威答案：**完整无条件 EV 仍未识别。只能保留 resolved-only 的逐窗点估计；不能把“约 94% 是 alpha”外推为完整 431 宇宙事实。

**OPEN 解决条件：**在 trigger 锚对残余行证明 E4 的全区间无恢复（状态变化边界/流动性回流事件与对应原子复测，而非仅 cutoff 单点），或给出不依赖点插补的有界识别结论；另需重新前冻结与 GL1 trigger estimand 完全一致的规则。

## GL1-19｜Base 聪明钱可复制 alpha

**分级：OPEN。merit=`UNTESTED_ELIGIBILITY_CONTAMINATED`。**

1. 阶段一预注册 SHA256=`ec4884127c3b97b33082a053097313cad07b5065d5add01ae548f9c335fbbd3f`。分布工件先生成并冻结为 `b80d466a71ea4ed1b9b4e4f59a56485e3b0e426fc0d503483e33bf6f315b8f7b`；阶段二阈值预注册 SHA256=`a5e6ef0a46245348eb96b50de7243716dce3c746b3074c8a4716e2dc686aa4e2` 及术语/mtime 勘误 SHA256=`f7d40c61297dd10947da8c5a9d15a508bcec52d144d2492e93e997247ab94da8` 均早于阶段二代码与结果。阈值没有按前向收益回调。
2. EOA 子集 mismatch=`0/859`，trace EOA golden=`73/73`；智能账户差异只作参考。目标 token 历史 `balanceOf`/receipt net 为 `20/20` 原始整数相等，补充 WETH pool cash-leg 为 `20/20` 原始整数相等，5 个 FIFO golden 钱包通过冻结整数断言。这证明限定样本内的归因与库存/现金腿重建工程门可行。
3. 该检查仍以 B3 的确定性 1,000 笔样本为中心，不等于把历史 B2 全量 11.60% multi-hop/ambiguous 缺口逐条关闭；工程门通过不能自动升级 RH 可复制收益主张。
4. 分布冻结后得到 5 个资格钱包，其中 2 个存在 `oversell_raw>0` 的左截断库存。前向 17 池、4,355 条真实 swap 中仅出现 4 个 smart buy signal，而且 4 条全部来自这 2 个左截断钱包；3 个完整库存资格钱包的前向信号数为 0。
5. 污染组的描述性结果是：+1/+5 为 4/4 resolved、mean=`-5.3695%`；+15 mean=`-5.6041%`，三窗 win rate 都为 0。baseline 只有 2/4 resolved，mean 约 `-9.99%/-10.12%`。资格污染、n=4 和不对称 unresolved 使 smart-vs-baseline 差值不能识别 edge；既不能判“有”，也不能判“没有”。
6. 后续真实 swap 仅作为 `TRUE_SWAP_MARKET_REFERENCE_SIMULATED_FILL`，不是我方真实 fill。Nansen=`NOT_RUN_MISSING_AUTHORITY_AND_API_KEY`，没有购买或伪造外部结果。
7. B 回归测试 6/6 通过，hash ledger 逐项校验通过；这些断言固定了污染与 `UNTESTED` 边界，没有把描述性负收益升级成 edge 结论。

**权威答案：**归因/FIFO 的限定工程门通过；可复制收益仍未测试成功。本轮负的 4 条污染样本不是 Base 聪明钱有无 edge 的结论。

**OPEN 解决条件：**冻结并排除/补齐左截断库存后，让完整库存资格账户产生有功效的前向信号；baseline 使用同一可解析覆盖并解决不对称缺失；再报告逐窗分布与右尾。Nansen 仍是外部交叉验证残余，不是本地真值替代品。

## GL1-18｜任意天气门是否无效

**分级：OPEN。**

1. 预注册 SHA256=`2ee3684425e66a42366b5f291c6fd016304d3c295bf263cdf8daecb1e0f662ea` 早于脚本与结果。五指标、训练/OOS 切分、训练分位阈值、10,000 次同 OOS eligible 日且同开工天数随机门、O1/O2 和稳定规则均已前冻结。
2. `eth_realized_volatility_7d` 的阈值仅从训练段派生；OOS KEYSTONE +1 为 `22` finite、`2` unresolved、mean=`+15.1164%`，随机门 median=`+19.1432%`、p95=`+26.7727%`，不胜随机。+5/+15 及子窗也不满足冻结稳定规则，机器状态=`NO_STABLE_OOS_WIN`。
3. `pool_creation_rate_7d` 的训练高 q=0.80 门在 OOS 开 0/55 天，状态=`OOS_UNPOWERED`，不能作为失败门的有效证据。
4. `aggregate_weth_net_inflow_7d`、`base_gas_regime_7d`、`whole_universe_rug_rate_7d` 均因冻结要求下缺少可审计、无前视、全覆盖本地源而为 `UNAVAILABLE_NOT_TESTED`；没有被填零或拿稀疏代理代替。
5. 因此 5 个预注册指标中仅 2 个进入 OOS 机器路径、只有 1 个有基本功效，stable win 数为 0。可以说“已可测的门没有稳定跑赢随机”，不能说“一篮子五门 OOS 均不胜随机”，更不能说所有未来天气门永远无效。C hash ledger 与 6/6 测试均通过。

**权威答案：**没有找到预注册且有功效的稳定 OOS 天气门，但完整篮子未被测试完；原 GL1-18 的普遍主张仍未识别。

**OPEN 解决条件：**按已冻结的无前视定义补齐 WETH 全网净流、Base block-header gas census 和带 point-in-time settlement 的全宇宙 rug-rate 源；让 pool-creation 门取得 OOS 功效后，重新跑同开工占比随机对照。不得在看到收益后换指标、方向或阈值。

## 最终安全裁决

- 三条均为历史研究事实的缩窄，不授权训练、下单、签名、广播、canary/live 或 Redis/signals 修改。
- `GL1-12=OPEN`，`GL1-19=OPEN`，`GL1-18=OPEN`。
- `GO=NOT_MADE`；`training_allowed=false`；`live_allowed=false`；`self_go_allowed=false`。
- 无单源、无完整锚点的主张没有升为 IRON。
