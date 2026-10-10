---
sop_applied: pending
alpha_id: AR-GL3-CLOSE-GL12
family: F1/F4/F7
next_gate: HUMAN_LINE_REVIEW_ONLY
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL3 · 收口 GL1-12（钉死 KEYSTONE 是真 alpha 还是搭 beta）

> 审查/指挥 agent 下发。针对性小而深任务，一次做完，最后审。全程先预注册后看结果。
> 上一轮(GL2-A)已在正确 trigger 锚下得出:瞎买全宇宙 resolved +3.31%、KEYSTONE +59.84%、条件 alpha +56.5pp;但 149/152/153 个残余 unresolved 未补齐,全宇宙 EV 未识别,只能 OPEN。本轮把它收口。

## 0. 要修的两个洞（GL2-A 自己招出来的）
1. **预注册口径没对齐**:GL2-A 冻结正文把出场写成 `b0+2/+6/+16`(即时阶梯),而 GL1-12 的 +56% 是 `trigger_block+1/+5/+15`。**本轮重新预注册,正文与计算口径完全一致(entry=entry_block+1,exit=trigger_block+1/+5/+15,与 reaudit10 +56% 同源)**,冻结哈希在前。
2. **E4 无恢复只查了 cutoff 单点**:149 个残余里初筛 141–145 个本是 rug,因"单点原子不能排除中间恢复"被降回 unresolved。本轮用**全区间证据**把它们尽量落地。

## 1. 核心工作:对残余 unresolved 做全区间无恢复扫描
对每个残余池(在各窗的 149/152/153),从出场块(trigger+N)到 cutoff:
1. 用 **mint/burn 事件 + 池内真实储备轨迹**(net ERC20 transfer 到池 = balanceOf(pool)@block,即我们已建的"真储备"口径)重建流动性时间线。
2. 判定出场块之后**流动性是否曾回流**(任何使其重新可卖的 mint/reflow):
   - **无回流 + 全程无真实卖出 + 出场块原子可卖模拟 revert** → **CONFIRMED 持续不可卖(带机制+全区间无恢复证据)= 真亏 −100%**(此时符合"五证据确认且不恢复才 −100%",因为已穷尽真实证据)。
   - **有回流(块 X)** → 在 X 处原子复测;可卖则按"首个可卖机会延迟出场价"如实定价(明确标注 delayed-exit)。
   - **数据真缺口、确实无从得知** → 才保留 unresolved(预期此后极少)。
3. 效率:LP-pull 是阶跃,储备一旦归零、之后无 mint 即永久不可卖——**事件扫描即可结论,不必逐块**。

## 2. 有界识别（就算个别补不齐也要能下结论）
补齐后若仍有残余 unresolved,给**全宇宙 EV 的有界区间**:
- 悲观界:残余全记 −100%。
- 乐观界:残余全记 resolved 子集中位(甚至 p75)。
报两个界 + 逐窗。**判据**:若**乐观界下的瞎买全宇宙 EV 仍远低于 KEYSTONE**(alpha 区间下沿仍显著为正),则 alpha/beta 结论**与残余无关地成立**,GL1-12 可钉 IRON;否则给 PINNED + 区间。

## 3. 交付
1. 新预注册文件 + SHA256(口径与 reaudit10 +56% 完全一致;残余裁定规则、回流判定、delayed-exit 定价、有界区间规则,全部做前冻结)。
2. 结果:补齐后各窗 resolved/unresolved/confirmed-rug、全宇宙 EV(点或有界区间)、KEYSTONE−瞎买逐窗 alpha 分解、KEYSTONE 内部 alpha 占比 vs beta 占比。
3. 更新 GL1 账本 GL1-12 分级(IRON/PINNED)+ 锚点 + 残余说明。
4. 人话版一段:瞎买到底赚不赚、KEYSTONE 的 +56% 里多少是真本事。
5. 可复现 hash;RPC 只用于本次储备轨迹扫描且要 ledger;回归测试(断言 ==)。

## 4. 铁律
纯只读+重核;不下单/签名/广播/改 live/signals/Redis;executor 关闭;不自判 GO;RPC 用 prettiest-blue-aura(orbital 已死);**只有全区间确认无恢复才可 −100%,数据缺口仍保留 unresolved,不默认全损**;报价永不单独充当成交;预注册在前,禁止看结果改规则(违者作废);可复现。

## 5. 审查 agent 终审保留
逐行审:①新预注册口径是否真与 +56% 同源;②残余降 −100% 是否都带"全区间无恢复"证据(不是又靠单点/靠插补);③回流处是否真原子复测;④有界区间是否诚实(悲观/乐观都给);⑤alpha 是否在同窗同龄人群算。**分级最终由审查 agent 定,codex 不自我背书。**
