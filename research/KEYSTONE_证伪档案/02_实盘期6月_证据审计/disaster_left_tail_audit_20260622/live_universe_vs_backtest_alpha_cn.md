# Live 成交 universe vs 回测 alpha 对齐审计（只读，2026-06-22）

只读。目标：查清 live executor 到底成交过什么，和回测验证的 universe（131 笔）是否一致。

## 1. Live executor 实际成交清单
证据：`exec:trades`（Redis list，315 行）。其中 298 行 `dry_run=true`（纸面），
**17 行带真实 `tx_hash`（真·上链）**，无其它 live ledger（已扫 `*trade*/*fill*/*tx*/positions:open*`，仅 `exec:trades`）。

真·上链交易全貌（n=17 笔：8 买 / 9 卖，全部 2026-03-30）：

| 池 | 类型 | token | 性质 | 笔数 | 单笔规模 | 结果 |
|---|---|---|---|---:|---|---|
| `0x82321f…e2d0` | aero_cl | AERO/WETH | 老币·主流池 | 14 | $0.50–0.76 | 全部微亏(-$0.01~-0.02, time_decay) |
| `0x7f670f…c2e6` | aero_vamm | AERO/WETH | 老币·主流池 | 3 | $0.51 | 微亏(take_profit -$0.01) |

- 唯一成交 token = **AERO**（1 个）。唯一成交池 = **2 个 AERO/WETH 池**。
- 全部是「已存在老币的主流池」，**没有一笔是新 memecoin 微盘**。
- 2026-03-30 之后再无真·上链记录，只剩 dry-run。

## 2. AERO 真伪（地址比对 + 链上核验）
链上 `eth_call`（chain_id=8453，executor READ RPC）：

| 字段 | 值 |
|---|---|
| address | `0x940181a94A35A4569E4529A3CDfB74e38FD98631` |
| symbol | `AERO` |
| name | `Aerodrome` |
| decimals | 18 |
| totalSupply | ~1.92B |
| has_code | 4736 bytes |

判定：**真·官方 Aerodrome AERO**（地址即 Aerodrome 规范 AERO 合约，链上 name/symbol 吻合）。不是冒牌。

## 3. 重叠度（带 n）
回测样本 = `_check2_base_trades.tsv`（131 笔，131 池 / 131 token，均为 holdout 期模型选中的新微盘）。

| 维度 | live | backtest | 交集 |
|---|---:|---:|---:|
| 池 | 2 | 131 | **0** |
| token | 1 (AERO) | 131 | **0**（AERO 不在回测 token 集）|

重叠度 = **0/2 池 = 0%**，0/1 token = 0%。

## 4. 判定
**FAIL — live 不代表回测 alpha。**

回测验证的边（131 笔）是「池龄~10min 的新 memecoin 微盘凸性狙击」；而 executor 唯一真·上链的 17 笔
全是 AERO（Base 头部 DEX 治理币）主流深池的小额刷单，与回测 universe **零重叠**，且结果是一路微亏
（深池高效、无 memecoin 凸性右尾，只剩手续费/摩擦的 bleed）——这恰好是「交错了 universe → 拿不到边」的实证。

## 一句话结论
**Live 至今从未交易过回测验证的那个 alpha：17/17 真实成交全是官方 AERO 主流池，与 131 笔回测样本重叠 0%——信号源/选池逻辑与回测 universe 脱节。**

## 残余风险 / UNKNOWN
- (c) n=17 笔 / 2 池 / 1 token 极小，**不能作为改参数依据**；但「每一笔都是 AERO、重叠 0%」是 100% 分类信号，方向无歧义。
- 这 17 笔是 2026-03-30 的；此后只有 dry-run。当前 signal_bridge 选池逻辑「现在会选什么」未由真实成交证明 → 需 forward 实测（恢复买入后头 N 笔记录池龄/token 类别，验证是否真落到微盘 alpha）。
- 测不了的：为何 2026-03 会去刷 AERO（信号源配置/历史）——属历史配置考古，本审计未展开。
