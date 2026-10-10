---
sop_applied: true
alpha_id: AR-GL2-B-SMARTMONEY
family: F3/F4/F7
next_gate: ATTRIBUTION_AND_FIFO_GOLDEN_THEN_DISTRIBUTION_ONLY
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL2-B · Base 账户实体归因与独立 FIFO 预注册

冻结时间：2026-07-15 UTC。本文的 SHA256 在任何 GL2-B 新结果计算前落盘；后续不得根据 copy-forward 收益修改本规则。

## 研究边界与人群

- 链：Base，chain id 8453。
- 固定历史窗：blocks `46,389,458..46,417,105`，与 B1/B2/B3/RH1 锚点一致。
- 归因检查全人群：该窗 `uniswap_v3_swaps_decoded` 的全部未 removed swap。
- 聪明钱历史资格人群：上述窗中 WETH/非 quote-token 的真实 Uniswap V3 swap；每行以 pool 的 signed `amount0/amount1` 得到真实 token 数量和 WETH 数量。禁止使用 recipient、首个 transfer recipient、报价或未来交易替代真实成交。
- 账户定义：可持续跟踪和重建持仓的账户/实体，不声称自然人或原始 EOA。
- 所有地址、数量、余额和 FIFO lot 先用整数 raw units；仅在报告显示时转换 decimals。WETH 原生计价。

## 冻结 resolver cascade

按以下优先级逐交易解析，不允许为了覆盖率选第一个 recipient：

1. receipt 中存在 `UserOperationEvent` 时，以 swap `log_index` 所在的唯一 UserOp event 边界取 indexed `sender`；多 UserOp 无唯一边界则 `AMBIGUOUS`。
2. 已知 EntryPoint、Universal Router、Uniswap Router、Aerodrome Router、0x/AllowanceHolder、DexRouter 等冻结 adapter 下，顶层历史 `eth_getCode(tx.from, block)` 为空码则账户为 EOA `tx.from`；代码以 `0xef0100` 开头则保留为 EIP-7702 账户实体。
3. 已知 solver/settlement、B2 的 80 个疑似共享基础设施地址和既有 attribution exclusion manifest 均不得作为交易账户。
4. 未知 route、非 EOA/7702 顶层 signer、边界冲突均留 `ENTITY_MEDIUM_CONFIDENCE`/`AMBIGUOUS`/`UNATTRIBUTABLE`，不强补。

EOA mismatch 只在历史代码为空的 EOA 子集比较 `resolved_account == tx.from`；智能账户/UserOp 分歧不作为 EOA mismatch。冻结通过门为 mismatch `<=2%`。golden 为确定性 route-family/side/时间分层 100 笔；每笔 trace 必须证明目标 pool call 在对应 root signer 或唯一 UserOp sender subtree 内。无法取得 trace 的样本保留 unresolved，不计为相等。

## 冻结独立 FIFO

- 只处理高置信账户交易。
- 账户目标 token 成交数量使用 receipt 中该账户的 ERC-20 `Transfer` raw net；必须与 `balanceOf(account, block)-balanceOf(account, block-1)` 同向且整数相等。不能相等的交易不进入 FIFO，并单列 unresolved。
- 同一账户/目标 token 的交易按 `(block_number, transaction_index, log_index, tx_hash)` 排序。
- 买入形成 FIFO lot：目标 token raw 增量、该交易与目标 pool/route 对应的真实 WETH 成本。卖出按最老 lot raw quantity 逐笔匹配，真实 WETH proceeds 按匹配数量比例分摊；超卖数量不创造负库存或虚构成本，只记 unresolved oversell。
- 同交易多池/多跳时按该交易所有相关目标 token 与 WETH swap legs/Transfer 闭环求和；无法形成唯一闭环则 unresolved。gas payer/gas 单列，不混入历史钱包选拔 PnL。
- 不把未卖出的浮盈作为 realized PnL，不把缺失或不可卖默认成全损。

链上 FIFO golden 固定抽 5 个满足以下条件的钱包：高置信、至少 2 次买与 1 次卖、至少一个闭合 FIFO lot；按钱包地址 SHA256 排序取前五。每个 golden 对所有入选交易做 `balanceOf` 前后整数对账，并断言脚本 FIFO 的 `bought_raw/sold_raw/remaining_raw/cost_raw/proceeds_raw` 与独立逐笔手算整数完全相等。少于 3 个合格钱包则本门不通过，不以放宽条件补样本。

## 阶段隔离

阶段一只允许：归因、trace/balance golden、FIFO、资格变量的历史分布扫描。资格变量只含资格截止时已经闭合的：`n_closed`、`realized_weth`、`winrate`、`profit_factor`、`median_pnl_weth`、`top1_profit_share`、`sell_buy_ratio`、历史跨度和活跃度。

阶段一严禁读取或汇总资格截止后的 copy 收益、+1/+5/+15 跟随结果或任何用未来价格构成的标签。分布工件 hash 落盘后，必须另建第二阶段阈值预注册并落 SHA，之后才允许计算前向收益。

## 只读与残余

- 只读 ClickHouse 与 `prettiest-blue-aura` RPC；禁止 orbital。
- 不下单、不签名、不广播、不改 live/signals/Redis，executor 保持关闭；不训练、不自判 GO。
- Nansen 无已授权 key 时记录 `NOT_RUN_MISSING_AUTHORITY_AND_API_KEY`，不购买、不伪造。
- 新 RPC method/tx/block/用途/成功状态写入脱敏 ledger，不记录 URL、token 或环境值。
- 本阶段证据最多支持归因/FIFO 工程门；最终分级由审查 agent 逐行决定。
