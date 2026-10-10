---
sop_applied: true
alpha_id: AR-RM2-CAPSTONE-ADAPTER
family: F1/F4/F6/F7
next_gate: HUMAN_ONLY_RM1_CALIBRATION_AFTER_INDEPENDENT_ADAPTER_REVIEW
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# KEYSTONE 封账总览：从“活”到“死”、再到历史真 alpha

## 结论边界

本文只整理已经封账的 GL1/GL2/GL3 与 reaudit10，不重新计算、不改变分级。`IRON` 表示冻结历史人群、冻结执行口径下的事实已经不会被剩余未知项推翻；它不表示可部署、不表示未来仍有效，也不表示历史报价已经被真钱成交兑现。权威总账为 `mvp/diagnostics/base_gl1_golden_truth_ledger_gl3_20260715.json`，SHA256=`99f70bdaec1bb2ac076c52aead9b35078678b301ece88ed38c90211a0790635e`。

## 活 → 死 → 活 → 真 alpha

|阶段|当时裁决|真正使用的口径|翻转根因与现裁决|锚点|
|---|---|---|---|---|
|早期“活”|历史回测给出很高正收益|旧代理执行、较长持有、不同样本和尺寸|只能作为历史版本，不能与后来秒级、真持仓口径直接相减|`mvp/diagnostics/base_gl1_golden_truth_ledger_gl3_20260715.json` @ `99f70b…635e`（historical_claims）|
|2026-07-14“死”|KEYSTONE 全体约 −4.61%，W2/W3 很差；瞎买约 −68%|约 30 分钟退出；缺失、不可报价或 adverse 被保守并入坏结果|这里把“30 分钟策略表现”和“秒级能否成交”混成同一问题，并把查不到偷换为全损|`mvp/diagnostics/base_b5_keystone_cha_reverify_20260714.json` @ `90248330d2538938499059e322e4963e7ac51eb2500b6e48cf2b071c6f0530d7`|
|reaudit10“复活”|+1/+5/+15 块历史均值约 +56.6%/+56.6%/+57.7%|0.001 WETH；entry+1；exit+1/+5/+15；真卖优先，否则明确 atomic-guarded exact quote；确认 rug 才 −100%；未知保留|恢复了与真实自动执行相近的秒级 estimand，撤销 missing-as-loss；但完整瞎买基线和真钱执行仍未识别|`mvp/diagnostics/base_reaudit10_final_20260715.json` @ `7af0f54247956fa069b0d82bb686f0d9c09bdb76da07cf63e9eb21998c45bcb7`; raw rows `65126c…410`|
|GL2“仍不能定案”|resolved-only 条件 alpha 约 +56.5～+58.2pp，但 GL1-12 保持 OPEN|本应是 trigger+1/+5/+15；预注册正文却误写即时 b0+2/+6/+16；E4 只看 cutoff 单点|这次主动降级：口径正文不完全同源，且单点不能排除中途恢复；149/152/153 个 cell 保留未知|`mvp/diagnostics/base_gl2_a_universe_ev_20260715.json` @ `966cf597c837070a40a3cce9c2defcdf11f25230c97adb93c69892bb62c5f197`|
|GL3“历史真 alpha”|独立终审 `GL1-12=IRON`|全 431 个同龄候选；KEYSTONE 是冻结 116 子集；entry+1、trigger+1/+5/+15；WETH；残余用前冻结上下界|先冻结正确正文，再扫描退出至 cutoff 的全区间恢复边界。瞎买 beta 上界仍为负，KEYSTONE 下界仍为正，选择 alpha 下沿超过 +83pp|result `e42ac92d29e75fa1ad4fbd12e25365a1672f4aeeef50f6edffa84bd1fe5a32a9`; independent review `658155a81f8db5f89f7f6045026008fe8735ca21526abd7da9d868b29203fdb5`|

每次“变卦”都不是同一估计量下的数据自然翻转，而是样本、退出时点、缺失处理或证据等级发生了掉包。现行账本禁止再把这些口径混写。

## 现在可以当铁律使用的历史事实

1. **瞎买同龄候选历史 EV 严格为负（IRON）**。431 个同龄候选在 +1/+5/+15 出场窗的有界 EV 分别为 `[-32.41%,-30.08%]`、`[-32.99%,-30.99%]`、`[-32.96%,-30.98%]`。口径：0.001 WETH、entry+1、trigger+1/+5/+15、真卖优先/否则 guarded quote、未知不插补。锚点：GL3 result @ `e42ac92d…32a9`。
2. **KEYSTONE 冻结子集历史为正（GL1-01 PINNED；GL1-12 的相对 alpha 已 IRON）**。同三窗 KEYSTONE 有界 EV 为 `[+54.33%,+56.53%]`、`[+52.84%,+53.92%]`、`[+52.38%,+53.48%]`；相对同窗全宇宙的选择 alpha 区间为 `[+84.41%,+88.94%]`、`[+83.83%,+86.91%]`、`[+83.36%,+86.44%]`。锚点：GL3 result @ `e42ac92d…32a9`；终审 @ `658155a8…fdb5`。
3. **alpha 结论不依赖 411 个新 rug 定损（IRON 稳健性）**。独立审查将 411 个新 `-100%` 全部退回未知后，瞎买乐观界仍为 −1.03%/−0.99%/−0.74%，KEYSTONE 悲观界仍为 +54.33%/+52.84%/+52.38%。锚点：GL3 independent review @ `658155a8…fdb5`。
4. **W2 全域有毒，但 KEYSTONE 选中健康小岛**。全 W2 在 2m/10m 原子卖失败为 304/408（74.51%）与 311/408（76.23%），分级 IRON；KEYSTONE-selected W2 的 +1/+1 resolved n=60、均值 +54.08%、同窗 alpha +30.52pp、unresolved=1，分级 PINNED。生态总体不能替代选中子集。锚点：reaudit10 final @ `7af0f542…5bcb7`；raw @ `65126c50…b410`。
5. **QuoterV2“合理卖价但原子严重失败”的已测假阳性上界为 1.24%（PINNED）**。fresh 1,265 池中，2m 已测 disease cases n=306，分歧 0，另 2 个未知；Wilson 95% 上界 1.2399%。它是过滤器，不是真成交证明。锚点：`mvp/diagnostics/base_b4_arm_b_20260714.json` @ `3b7318fdb65dd7a42592620b565c3d0d0f70d97e960562e6fc4b9179e798499e`；reaudit10 final @ `7af0f542…5bcb7`。
6. **历史信号频率是爆发型（PINNED）**。冻结 116 个信号跨约 86.36 天，月计数 38/61/17；自然周包含 4 个零周，也出现单周 50。禁止把它压成稳定周均/月均，更不能外推未来频率。锚点：reaudit10 raw @ `65126c50…b410`。
7. **rug 必须过五证据（IRON）**。262 个 delayed-rug 候选按机制、机制前后同仓位原子可卖、后续真实成交、恢复扫描、截止完整性裁定为确认 LP rug 235、暂时不可卖后恢复 1、未知 26；未知不记全损。锚点：`mvp/diagnostics/base_reaudit10_rug_five_evidence_rpc_20260715.json` @ `5fcd049b16260148856f28bb7a625ca830c9ca21376ccb19d677858f59523b5c`；reaudit10 final @ `7af0f542…5bcb7`。
8. **D5、既有 forward LP monitor 与已测天气门的历史增量均为零或未胜随机，但安全职责仍在**。D5 五个入场偏移 blocked=0、mean delta=0（IRON），仍是强制防线；forward monitor 24 例中 real loss=4、提前预警命中=0、救援=0（PINNED）；当前冻结天气门开放 2/55 OOS 日、交易 0、未胜同开工占比随机门（PINNED）。锚点：reaudit10 final @ `7af0f542…5bcb7`; LP monitor @ `b3a61f25749ee60a35aebf16d14cc990c2a63053a229041e0f6666575e575ccb`; weather result @ `22f53e7b7083f62c84ecd24db8e2b80299120bd617189129b72b8d50af40e0f3`。

## 仍然 OPEN

|问题|为什么仍未知|关闭它需要什么|
|---|---|---|
|GL1-02 真钱净 edge|历史 atomic/exact quote 不是我方真实 fill；RM1 real tx hashes 为空|同策略 0.001 WETH 的人类逐笔 arm 真买真卖，记录 receipt、真实 gas、延迟和报价差；RM1 dryrun @ `a2c4dd02a81169e53d4fde1f450287895d6aec496c3161e73ec3e970d6a1c307`|
|GL1-06 未来频率|历史爆发分布不识别未来到达率|冻结 selector 的更长 forward 时间，周/月分布必须包含零区间；总账 @ `99f70bda…635e`|
|GL1-08 现实月收益|缺未来频率与真钱单笔净额两项联合分布|真钱往返 + forward 频率后做分布乘积，不能乘两个点均值；总账 @ `99f70bda…635e`|
|GL1-23 月尺度衰减|2–30 秒执行前沿不等于数月市场漂移|更长非重叠 forward/OOS 日历窗；总账 @ `99f70bda…635e`|
|GL1-19 RH 聪明钱|归因/FIFO 工程可用，但完整库存钱包没有 powered forward 信号；merit=UNTESTED|完整库存合格钱包、足量前向买入、同分辨率无条件基线；GL2-B final @ `b9a2ab9e1955857cdb891f0763fe342557c4e2c9a6ba32609cfdcb6a48c8612e`|
|GL1-18 天气门篮子|五项只两项进入 OOS，只有一项有基本功效；稳定胜者 0 不等于全部天气门无效|补齐三项 point-in-time 外生源并取得有功效 OOS 暴露；GL2-C result @ `22f53e7b…40e0f3`|

## 最后的防误读规则

- `IRON ≠ GO ≠ 可部署`。GL1-12 只钉住冻结历史有界事实。
- `PINNED` 只表示限定口径下有重复锚点，不允许越过 residual 或样本边界。
- 报价永远标为历史模拟报价；只有 receipt/log/balance delta 才能称真实成交。
- `live_allowed=false`、`self_go_allowed=false`；下一步只能是人类逐笔授权的极小额执行校准。
