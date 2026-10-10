# B20 dump-reclaim R0 读出

生成：2026-08-19  
预注册：`PREREGISTRATION.md`（收益出来之前冻结）  
代码：`/root/base_b20_dump_reclaim_r0/`

## 策略（官方臂 Arm A）

B20 的 **Uni V3 WETH 主池**，前 10 分钟砸 ≥20%，再在 1 小时内第一笔回到开盘 −5% 以内时买入 0.02 ETH。  
出场：−30% 硬止损 / 峰值回撤 25% / 最长约 1 小时。  
**不用** Keystone `p_fatal`。

尺子：入场块、出场块上 B4/B8 的 `AtomicRoundTrip` archive `eth_call`（新 EOA 走 SwapRouter02 真买真卖）+ QuoterV2 持有期定价。不广播。

## 宇宙

| 过滤 | n |
|---|---:|
| B20 × V3 WETH 主池 | 334 |
| 有成交 | 260 |
| 前 10 分钟 ≥5 笔 | 95 |
| 其中砸 ≥20% | 3 |
| **发出 Arm A** | **1** |
| 活过 10 分钟、可做 Arm B 定时对照 | 26 |

砸 ≥20% 的三只：HOLDFI（−88%，未恢复）、Beryl（−27%，未恢复）、**Basecat（−34%，恢复并发出）**。

## 官方结果

唯一一笔就是讨论里的 Basecat `0xf79478d5…` / `0xB200…4c27f6523082f41D01`。

| 项 | 值 |
|---|---|
| 入场块 | 50,019,975（砸完回到 −1.9%） |
| 出场 | 50,021,363 **trail** |
| 入场同块摩擦 | −1.94%（过 −10% 闸） |
| 出场仍可买卖 | 是 |
| **official_net** | **+9.89%**（扣估计 gas 后） |
| 本金 0.02 ETH | +0.00198 ETH |

这不是后来 tick 上看到的 +400%。1 小时 + 25% 回撤在反抽后很快出场，只吃到恢复后的一小段。

## 仪器

本地 anvil 1.7.1 买 B20 报 `OpcodeNotFound`（池里当时有 0.34 WETH，不是没池）。Anvil 没有 Beryl 的 B20 预编译。  
官方数字改走 archive 节点上的 AtomicRoundTrip，和仓库 B4/B8 同一把尺子。策略参数没改。

## 预注册裁决

**UNDERPOWERED** — 官方臂只有 1 笔，不足冻结的 8 笔。  
不能因为 Basecat 赚了 10% 就当可上线策略。样本就是我们讨论过的那只猪。

## 探索（不改官方臂）

前 10 分钟砸过门槛的池很少：−10% 也只有 4/95。这条线在 **V3 主池** 上几乎不响，猪更多在 V4，而 V4 fork 买卖还 blocked。

---

复现（不要打印 RPC URL）：

```bash
/root/base_b20_dump_reclaim_r0/run.sh
```
