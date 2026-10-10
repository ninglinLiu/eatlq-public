# 事实核对

抓取与链上读取集中在 2026-10-07 08:02–09:39 UTC。示例 JSON 不当作行情。部署字节码优先于文档和博客。

## 部署

1. 结论：当前官方地址页上的 Midnight 有五条链的条目：Ethereum `0x471686c42792F93528B000beF54bC10E3aa2045f`、Arc `0x208786922BE56fDE2D1Fa60e6b9eC5D723e8d7b0`、Base `0xAdedD8ab6dE832766Fedf0FaC4992E5C4D3EA18A`、Robinhood Chain `0x6120765Ba5336150BbdDdD0Cd9108B5bFD369632`、Tempo `0x31Ff800b0FA4AA42071d4ae9D0392e84f256D83D`。API `GET /markets/chains` 当时只返回 1、8453、5042、4663，没有 Tempo。
   来源：docs.morpho.org 地址页的浏览器链接；`raw/chains.json`。
   时间：2026-10-07 08:05 UTC 左右。
   验证：Base 与 Ethereum 用 `eth_getCode` 确认有 24557 字节且两边哈希相同，SHA-256 `7884845d7adfa271bc0715c0f6205a006f053921128680defea915fa8a8a831d`。Arc、Robinhood、Tempo 没有逐链读代码。
   影响：研究主链用 Base。没有代码核对的链不拿来算利润。

2. 结论：Base 合约首次有代码的区块是 48286884，2026-07-06 18:38:35 UTC，区块哈希 `0x547541951a8cb44e40780639da4cec8e2a06a1c315b63d51a1580c3a434283ed`。Ethereum 是区块 25798183，2026-08-20 18:39:47 UTC。
   来源：`raw/deployment_blocks.json`，二分 `eth_getCode`。
   验证：已完成。Sourcify 对 Base 地址是 runtime match，编译器 `0.8.34+commit.80d5c536`，优化 466，EVM osaka，记录时间 `2026-07-06T22:38:44Z`。
   影响：历史从这次部署算起，不能用更早的源码常数。

3. 结论：`TIME_TO_MAX_LIF` 在已部署源码里是 60 minutes。博客写的 15 minutes 对应 Git 提交 `292e96ce2ed8fed003a32b39b7d3e53acafb532d`（2026-06-23）之前的源码。两次部署都晚于这次提交。
   来源：Sourcify `ConstantsLib.sol`；Etherscan Exact Match 页面；GitHub commit。
   验证：已核对源码与提交 diff。没有在字节码里单独反汇编这条常数，但两链运行时代码一致且验证源码都是 60 分钟。
   影响：到期激励按 60 分钟斜坡算。用 15 分钟会把利润提前约 4 倍。

4. 结论：文档示例里的 `0x2F7a3AA739ba5792Ce1b4eA046117f2C0095BCA6` 在 Base 上仍有代码，大小 24412 字节，不是当前订单簿返回的 `0xaded…`。
   来源：API 文档示例；`eth_getCode`；`raw/books_base.json` 的 `midnight` 字段。
   验证：代码存在，未反编译，未证明它的 LIF 常数。
   影响：不把这个地址的市场算进利润。

## 权限

5. 结论：`liquidatorGate == address(0)` 时任何人可以清算；非零时只有 `canLiquidate(msg.sender)` 为真的地址可以。`enterGate` 只限制增加债权或增加债务，不限制还款、减仓和清算。
   来源：`Midnight.sol` 的 `liquidate` 与 `take`；docs gates 页。
   验证：与源码一致。Base 1095 个市场里 14 个 gate 非零，且 `total_units` 都是 0。有债务的市场 gate 为 0。历史清算里 3 笔落在非零 gate 上，偿还额约 106 USDC。
   影响：当前有债务的 Base 市场，普通地址可以清算。没有验证过的非零 gate 不算我们能进。

## 清算规则

6. 结论：健康度路径要求 `debt > maxDebt`，激励立即用该抵押物的 `maxLif`。`lltv < 1` 时有 RCF 上限，公式是 `maxRepaid = (debt - maxDebt) * WAD^2 / (WAD^2 - lif * lltv)`，不是“只补到刚好健康”这句话本身。留下的抵押价值低于 `rcfThreshold` 时可以清完该抵押。
   来源：`Midnight.sol` `liquidate`；开发者文档 collateral-health-liquidations（2026-10-07 抓取）。
   验证：公式来自已核对的部署源码。文档对 RCF 的文字是简化。
   影响：健康度清算不能默认清掉整个仓位。

7. 结论：到期路径要求 `block.timestamp > maturity`，不要求不健康。激励 `min(maxLif, 1 + (maxLif-1) * elapsed / TIME_TO_MAX_LIF)`。没有 RCF。调用者可以只还一部分。
   来源：同一函数。开发者文档写“到期后没有部分清算、一次清完全部债务”。
   验证：源码允许指定 `repaidUnits` 或 `seizedAssets`。已索引事件里 `post_maturity=1` 且 `partial_liquidation` 的有 275 笔，`full_liquidation` 69 笔。文档这句话与部署和事件冲突，以部署和事件为准。
   影响：到期后可以部分清算。不能把文档的“必须一次清完”写进执行假设。

8. 结论：本轮链上配置返回的 liquidation cursor 只有 `0.30e18`。`maxLif = 1 / (1 - cursor * (1 - lltv))`，向下取整。LLTV 0.86 时 `maxLif = 1.043841336116910229`（约 4.384%）。LLTV 0.98 时 `maxLif = 1.006036217303822937`（约 0.604%）。开发者文档用 cursor 0.25 算出的 3.6% 和 6.1% 是示例，不是这些市场的数。
   来源：`ConstantsLib.maxLif`；`raw/chains.json`；`test/test_lif_identity.py` 已通过。
   验证：整数公式与单测一致。每个抵押物仍要用它自己的 lltv 和 cursor，没有全市场固定费率。
   影响：98% LLTV 的金库抵押，满一小时也只有约 0.6% 的预言机激励。

9. 结论：坏账在清算时从 `totalUnits` 扣除并提高 `lossFactor`，由该市场贷款人承担。已索引 599 笔的 `bad_debt` 都是 0。
   来源：`liquidate`；`data/summary.json`。
   验证：API 字段，没有逐笔回放坏账分支。
   影响：这批历史里，清算激励来自借款人抵押物，不是贷款人亏掉的本金。

## Callback

10. 结论：`liquidate` 先把抵押物转给 `receiver`，再调用 `callback.onLiquidate`，最后从 `payer` 划走 `repaidUnits`。`callback != 0` 时 `payer` 就是 callback。返回值必须是 `keccak256("morpho.midnight.callbackSuccess")`。
    来源：`Midnight.sol` 674–713 行；`ILiquidateCallback`。
    验证：本轮重新解码了五笔收据。发起人的 USDC 净额与留下的现金一致，Midnight 的 USDC 净额等于偿还额。父区块上 callback 余额为 0 是上一轮在区块 50500434 的读取，本轮没有重读。
    影响：自筹资成立的条件是回调返回前，callback 已经持有足够的贷款代币。不是任何抵押物都自动成立。

11. 结论：本地 anvil 在父区块 50500434 重放同一 calldata 时打满 800,000 gas 失败，没有复现 946 USDC。`cast run` 对公共 RPC 被中止，没有轨迹。
    来源：上一轮 `raw/replay_send1.json`。本轮没有再开节点。
    验证：失败已记录。历史收据仍然有效。
    影响：机制有历史成功样本，我们自己的回调实现没有在真实状态上跑通。

## 口径

12. 结论：`totalUnits` 是市场未提取债权。`take` 按债权净增加改它，`withdraw` 减少它，坏账减少它。`repay` 只减债务、增 `withdrawable`，不改 `totalUnits`。清算的正常偿还也不减 `totalUnits`。
    来源：`Midnight.sol` 的 `take`、`withdraw`、`repay`、`liquidate`。
    验证：Base 区块 52288300（2026-10-07 09:39:07 UTC，哈希 `0x1288e37d7ed53700d55881c73941794558fff733e4e04027cebb10f0e8dad8a3`），市场 `0x0b8fe70a…` 上 `totalUnits - withdrawable = 116601014892407`，等于该市场 API 债务之和。55 个有债务的市场里 28 个 `total_units` 大于债务之和，0 个小于。其余 27 个差额没有逐个读 `withdrawable`。
    影响：不能把 `total_units` 当成未偿债务、成交量或存款。债务用借款人 `debt`。

13. 结论：查询当时 Base 1095 个、Ethereum 981 个市场的结算费档和连续费都是 0。
    来源：市场列表里的 `settlement_fee_schedule` 与 `continuous_fee_rate`。
    验证：索引值，不是逐市场链上复核。
    影响：这批清算利润不扣协议费。以后费率可以变。

## 数据入口

14. 结论：市场、债务候选、清算事件、当前订单簿都可以从 `api.morpho.org` 读到。订单簿接口不提供历史撤单和未成交报价。未上市大市场对 `GET /v0/midnight/books/{id}` 返回 404。
    来源：08:03–08:10 UTC 的原始响应；大市场 404 已记录。
    验证：Base 清算扫描 1095/1095 成功。Ethereum 清算扫描 474/981 为 429，这些市场是未知不是零。公共 Base `eth_getLogs` 大区间返回 413，所以部署后到第一笔索引清算之间没有用日志补全。
    影响：清算历史在 Base 上足够做经济判断。报价研究没有历史带，不能倒推订单簿。

15. 结论：官方 `midnight-liquidation` 机器人只清算 `listed=true` 的市场，发送前要私钥。大额历史偿还在未上市市场。
    来源：`vendor/morpho-bots/bots/midnight-liquidation/README.md`，克隆于 2026-10-07。
    验证：读了说明和配置，没有运行签名入口。
    影响：参考机器人的覆盖范围小于协议允许的范围。未上市不等于我们没资格。
