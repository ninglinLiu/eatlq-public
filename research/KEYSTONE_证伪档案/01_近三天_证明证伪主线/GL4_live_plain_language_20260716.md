---
sop_applied: true
alpha_id: AR-GL4-LIVE-RECON
family: F0/F1/F4/F7
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL4 人话版

这个月两个真钱钱包一共实际买入 0.169920 WETH，按每笔买入块上的链上 ETH/USD 约 $288.16；其中两组 USDC 受控烟测和一组 NEXIO 真正卖回，三组严格已实现合计亏 0.000011451892 WETH（约 $0.03），0/3 获胜。其余 JOTCHUA、BOT 和三笔 PARADAENGI 没有卖出 receipt，代币仍完整躺在钱包，但池 WETH 到窗口末都坍塌至少 99%；按窗口末同尺寸链上可执行报价（PARADAENGI 三笔明确 revert 为零）经济净值合计约亏 0.160235862229 WETH / $271.51，8 个已判定入口 0 胜。所以真金亏损的主因不是已观测到的报价—成交滑点，而是 rug/撤池真实兑现，加上 F、trail、仓位规模、模型与 GL3 估计量并不一致；+56% 还能保留为冻结历史样本的报价型有界事实，却不能再叫真钱或前瞻 edge，GL1-02 在这个小样本实盘尺度应判 FAIL。
