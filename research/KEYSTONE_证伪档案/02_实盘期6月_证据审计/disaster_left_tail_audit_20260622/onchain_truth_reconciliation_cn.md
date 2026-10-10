# 链上真相核对：实盘到底成交过什么（只读，2026-06-22）

方法：不信任 Redis/报告计数。取 **当前** executor 钱包地址，用 Base RPC + Blockscout 拉全量链上交易，
解码 status=1 的 router swap（Transfer 日志 + 原生 ETH value）。与 `exec:trades`、n=8、17笔AERO、BOT 蜜罐逐一对账。

## 钱包身份（关键发现）

| 钱包 | 地址 | 来源 | nonce | 链上 router swap |
|---|---|---|---:|---:|
| **当前 executor**（`.env` PK） | `0x4B7cd953871663474a0A25B81bddDf5C1f1195A7` | 容器内 `Account.from_key(EXECUTOR_PRIVATE_KEY)` | **0** | **0** |
| **历史 executor**（`.env.save` PK） | `0x3a0DE7b4e6Ea125FFA3155b0026E5F717A1e86EE` | 旧私钥；`exec:trades` 17 笔 tx 的实际 `from` | 106 | **48** |

当前钱包仅有 1 笔**入账**（0.65 ETH，block 47664987，from `0x39591e7c…`），**从未发出任何交易**。

---

## 1. 当前 executor 钱包 — 链上真实成交清单

**判定：0 笔。** 无买、无卖、无 swap。`LIVE_TRADING_MODE=true` 配置下，**此地址从未真 live 过**。

---

## 2. 历史 executor 钱包 — 链上真实成交清单（n=48 status=1 router swap）

证据：Blockscout txlist n=107 + RPC receipt 解码。代币按 Transfer 日志 net 到钱包判定买/卖。

### 按 token 汇总

| token | 地址 | 买 | 卖 | 合计 |
|---|---|---:|---:|---:|
| AERO | `0x940181a94a35a4569e4529a3cdfb74e38fd98631` | 13 | 11 | **24** |
| USDC | `0x833589fcd6edb6e08f4c7c32d4f71b54bda02913` | 14 | 6 | 20 |
| USDbC | `0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca` | 2 | 1 | 3 |
| JOTCHUA | `0xe8d95a1d1a66f60c264c54e1bfc6fad066495ff9` | 1 | 0 | 1 |
| **BOT** | `0x24e39227655af8712a1cecbd5cb1221616954170` | **1** | **0** | **1** |
| NEXIO | `0xf0e5c61f4ca7577f735cb8313a1df348fe494e12` | 1 | 1 | 2 |

### 完整清单（48 笔，UTC）

| 时间 | 买/卖 | token | 数量 | ETH≈ | tx hash |
|---|---|---|---:|---:|---|
| 2026-03-29 05:57:05 | buy | AERO | 6.0979 | 0.001000 | `0xc812…`→`0x2a7639…` |
| 2026-03-29 05:58:23 | sell | AERO | 6.0979 | — | `0xd71b62…` |
| 2026-03-29 06:14:51 | buy | USDC | 5.0095 | 0.002500 | `0x94f17e…` |
| … | … | USDC/USDbC | … | … | （3/29 共 20 笔稳定币测试，略） |
| 2026-03-30 09:18:57 | buy | AERO | 2.3600 | 0.000381 | `0xc812679e…` ← exec:trades #1 |
| 2026-03-30 09:49:23 | sell | AERO | 2.3600 | — | `0x0250c812…` |
| … | … | AERO | … | … | （3/30 共 17 笔 = exec:trades 全集，全部 AERO） |
| 2026-06-18 13:27:13 | buy | JOTCHUA | 863.99 | 0.002667 | `0xd58507…` |
| **2026-06-21 12:29:21** | **buy** | **BOT** | **90146.69** | **0.014553** | **`0xea6a3062…`** |
| 2026-06-22 08:33:43 | buy | NEXIO | 594452.46 | 0.002500 | `0x553fcd47…` |
| 2026-06-22 08:36:15 | sell | NEXIO | 594452.46 | — | `0xb8468729…` |

（完整 48 行见运行产物 `/tmp/chain_truth_swaps.json`）

### BOT 蜜罐补充（链上）

- BOT 买入 1 笔成功（上表）。
- BOT 卖出：**0 笔成功**；旧钱包另有 **48 笔 status=0 的 router 失败 tx**（2026-06-21 15:15–15:34 密集 revert），与蜜罐 incident 叙述一致。
- 旧钱包当前仍持有 **90146.69 BOT**（`balanceOf` 链上读数）。

---

## 3. 对账表：报告数字 vs 链上真相

| 声称 / 来源 | 报告 n | 链上真相 | 判定 |
|---|---:|---|---|
| **true_live_forward_executor_n=8**（sprint087 / sprint083 口径：`dry_run=true` + S074 + post-cutoff 成功 BUY） | 8 | **0**（8 笔均无 `tx_hash`，纯纸面） | ❌ 不是链上成交；是 executor dry-run 计数 |
| **exec:trades 带 tx_hash**（无 dry_run 标记的 17 行） | 17 | **17** 笔 status=1 存在 | ⚠️ 链上真实，但 **from=旧钱包 0x3a0de…**，**不是当前 executor** |
| **「17 笔 AERO live」**（上轮 audit 结论） | 17 | 17 笔 tx **确实全是 AERO**（3/30 子集） | ⚠️ token 判对，但 **钱包 attribution 错**（旧钱包）；且旧钱包全历史 AERO=**24** 笔非 17 |
| **BOT 蜜罐 incident** | 1 buy | **1 buy** on 旧钱包 + 48 failed sells | ✅ 买入链上真实；卖出全 revert；BOT 仍卡钱包 |
| **当前 executor 真 live** | 隐含 ≥8 或 ≥17 | **0** | ❌ **从未上链** |

### exec:trades 17 笔 vs 链上（逐笔）

17/17 tx hash 在链上 status=1 ✅；17/17 解码为 **AERO** ✅；17/17 `from` = 旧钱包 ✅；**0/17 from 当前 executor** ❌。

### n=8 八笔 dry-run（均无 tx_hash）

2026-05-17 八笔 `S074_*_6ff5` 信号、~$0.38 纸面 size、池为 memecoin 微盘（LSTEAK/CHEESE 等）——**零链上痕迹**，属 shadow/dry-run。

---

## 4. 结论（带 n）

### 当前 executor 钱包（用户问的「executor 钱包」）

| 指标 | 数量 |
|---|---:|
| **真 live 成交总笔数（status=1 swap）** | **0** |
| AERO | 0 |
| BOT | 0 |
| 其他 | 0 |

### 历史 executor 钱包（链上确有成交，但已换 PK）

| 指标 | 数量 |
|---|---:|
| 真 live 成交总笔数 | **48**（31 买 + 17 卖） |
| AERO | **24**（13 买 + 11 卖） |
| BOT | **1** 买 + **0** 成功卖（+48 revert 卖 attempts） |
| 其他 | **23**（USDC 20 + USDbC 3 + JOTCHUA 1 + NEXIO 2） |

## 一句话

**当前 executor 地址 `0x4B7cd…` 链上真 live = 0 笔；此前所有「live」计数（n=8 纸面、17 tx_hash）均不归属此钱包——17 笔 AERO 与 1 笔 BOT 买入发生在已废弃的旧钱包 `0x3a0de…` 上。**

## 判定

**FAIL** — Redis/报告把旧钱包链上活动误当当前 executor live；当前 PK 从未 broadcast 过 swap。

## 仍未知 / 测量方案

- (c) 旧钱包 3/29 USDC 测试是否算「策略 live」——链上是真 swap，但是 infra 测试而非 KEYSTONE alpha；n=20，不能当 alpha 绩效。
- 钱包轮换时间点与是否故意迁移：`.env.save` vs `.env` PK 不同，需 ops 确认；不影响「当前 executor = 0」结论。

## changed files/commands

只读。运行：`docker exec eatlq-executor-1` 取地址；Blockscout API + QuickNode RPC 解码；未改 env/实盘。
