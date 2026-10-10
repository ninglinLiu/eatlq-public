---
sop_applied: true
alpha_id: AR-GL2-THREE-OPENS
family: F1/F3/F4/F6/F7
next_gate: KEEP_ALL_THREE_OPEN_UNTIL_RESIDUALS_CLOSE
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL2 人话版增量摘要

这次没有把三条 OPEN 硬关掉，反而把“为什么还不能关”钉得更清楚。

- A：能定价的同龄币里，大盘顺风约占 KEYSTONE 均值的 5%–6%，选币差约占 94%。但 431 个币仍有 149–153 个不知道中间是否恢复过，完整市场平均收益依然算不出来；所以“+56% 里究竟多少是真 alpha”只能对已定价子集说，不能对全宇宙说。
- B：账户归因与持仓/现金腿整数对账已经能做。问题出在资格：前向出现的 4 次买入全来自两个历史库存不完整的钱包，真正库存完整的三个钱包一次都没买。因此这 4 笔负回报不能证明聪明钱有 edge，也不能证明没有 edge。
- C：真正有数据且有基本功效的天气门只有 ETH 波动这一项，它没有稳定胜过随机门；另一个门一次都没开，三个门没有合格历史数据。现在只能说“已测门没找到稳定胜者”，不能说“五类天气门全部没用”。

独立审查最终给出：`GL1-12=OPEN`、`GL1-18=OPEN`、`GL1-19=OPEN`。没有 GO，不训练，不进 canary/live，也没有改 Redis、signals 或执行器。
