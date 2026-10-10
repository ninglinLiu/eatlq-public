---
sop_applied: true
alpha_id: AR-RH1-BASE-SMARTMONEY
family: F3/F4/F7
next_gate: EXPAND_HIGH_CONFIDENCE_TX_FROM_HISTORY_OR_ARCHIVE
decision_flags:
  training_allowed: false
  live_allowed: false
---

# Base 复现 RH「聪明钱跟单」证伪报告

日期：2026-07-14 UTC  
性质：research-only；只读 ClickHouse；未调用 RPC，未写 Redis，未启动/重启服务，未签名或广播交易，不作 GO。

## 一句话结论

**RH 方法的代码路径已在 Base 短窗复现，但策略 merit 为 `UNTESTED`：B2 的 EOA 自证硬门失败，且 FIFO realized PnL 缺独立链上 golden。禁止据此判断 Base 聪明钱有或没有 edge。**

最重要的结果不是“赚不赚钱”，而是 RH 教训在 Base 上原样重现：切点前 105 个钱包有至少 2 个闭合 token episode，35 个已实现盈利，但 **0 个**通过 `top1_profit_share < 50%`。也就是说，本窗口里所谓“历史赢家”全部依赖单笔/极少数暴击，无法形成 RH `deconcentrated` 聪明钱名单。

RH 五选择器探索电池中，能在严格 24h 新池、10 秒延迟、真实成交/真实卖向容量下产生信号的三个臂全部为负：abs-PnL n=1、−2.42%；median-PnL n=4、均值 −53.98%；risk-adjusted n=1、−2.42%。但 n=1～4，只能记为负方向证据，不能称 powered FAIL。

## 1. 研究对象与 RH 教训如何落到 Base

本任务不是研究 RH 链，而是在 Base/EATLQ 上复现 RH 方法。吸收 `/root/rh链聪明钱0714.md` 后冻结以下护栏：

1. 不用 `swap.sender` 作为钱包。Base 已由 B1 证明 sender 会把 99.4% resolved 钱包身份压进 router/共享执行合约。
2. 主口径只用 B1 高覆盖窗内 `tx_from_by_hash.tx_from`；不把全历史 recipient/sender fallback 冒充真钱包。
3. 每个切点只用切点前已闭合 episode 排名；切点后只验，不用未来收益建名单。
4. 复现 RH selector battery：`abs_pnl`、`winrate`、`median_pnl`、`deconcentrated`、`risk_adjusted`。
5. 主复制路径使用 10 秒延迟后的第一笔、容量足够的真实 swap 入场。
6. 出场为 +30%/+60%/+120% 各 1/3；只有真实卖向 swap 的价格和 token 数量都足够才成交；60 分钟余量等待到 120 分钟，仍无容量则按零回收，不删样本。
7. 固定 0.001 WETH；每个实际执行腿扣 0.0000215 WETH gas（约 $0.075/腿）。DEX fee 已含在真实 swap 均价中。
8. 同 token 只取首个信号；主宇宙要求信号时池首笔真实 swap 不超过 24h。

## 2. 真实 schema 与归因边界

| 逻辑字段 | Base 真源 |
|---|---|
| swap 时间/块/交易/池 | `eatlq.uniswap_v3_swaps_decoded.block_time/block_number/tx_hash/log_index/pool_address` |
| 真实成交数量 | `amount0/amount1`（signed Int256） |
| 钱包 | `eatlq.tx_from_by_hash.tx_from`，按 tx_hash 精确 join |
| pool token/decimals/fee | `eatlq.discovered_pools` 的 `token0/1_address`、`token0/1_decimals`、`fee_bps` |
| 基础设施/quote 排除 | `configs/research/base_attribution_exclusions.json` |
| 新池时间 | 同池全历史第一笔非空真实 `block_time` |

覆盖窗固定为 B1 golden 高覆盖块：`46,389,458～46,417,105`。该窗 tx.from 覆盖 52,493/52,989 tx；本脚本没有使用 arrival time fallback。

## 3. 覆盖体检

| 指标 | 数值 |
|---|---:|
| 原始 V3 swaps | 57,426 |
| 有 tx.from 的表内 tx | 52,493 |
| 严格 WETH/非 quote-token 可用成交 | 18,840 |
| 钱包 | 3,404 |
| 池 / token | 28 / 28 |
| 可用时间 | 2026-05-23 20:31:05 → 2026-05-24 11:52:05 |
| 无 tx.from 丢弃 | 522 swaps |
| 非 WETH 土狗对丢弃 | 38,064 swaps |

时间按 60/40 切：训练截至 2026-05-24 05:43:41；为保证每个信号有完整 120 分钟退出观察，OOS 只评到 09:52:05。

这不是跨 regime 数据：只有约 15.4 小时和 28 个池。全历史 V3 虽有约 811 万 tx，但 B1 报告确认 tx.from 只覆盖约 0.65%；用 fallback 扩成“七个月钱包史”会重复 RH 的 router/归因错误。

## 4. Point-in-time 钱包资格结果

训练切点前共 2,484 个钱包：

| 逐级条件 | 钱包数 |
|---|---:|
| 已闭合 episode ≥1 | 364 |
| 已闭合 episode ≥2 | 105 |
| 已闭合 episode ≥3 | 45 |
| episode ≥2 且 realized PnL>0 | 35 |
| 再要求 top1 正利润占比<50% | **0** |
| 再要求 sell/buy≤2 | **0** |
| 最大闭合 episode 数 | 28 |

因此 RH 主选择器 `deconcentrated top-30` 在 Base 高置信窗口没有名单，10 秒/30 秒/60 秒跟单、本人收益和随机钱包对照都没有合法样本。这里的 `n=0` 是 **INCONCLUSIVE_NO_ELIGIBLE_WALLETS**，不是收益 FAIL。

加固资格（episode≥3、跨度≥2h、正 PnL、胜率≥55%、PF≥1.2、top1<50%、反MM）同样为 0。30 天规范资格也必然为 0，因为数据窗不足一天。

## 5. RH 五选择器电池：10 秒真实成交探索臂

| 选择器 | 选中钱包 | 去重24h新池信号 | 可执行 n | mean | median | win | 去 top1 mean |
|---|---:|---:|---:|---:|---:|---:|---:|
| abs_pnl | 30 | 1 | 1 | −2.42% | −2.42% | 0% | N/A |
| winrate（closed≥15） | 1 | 0 | 0 | — | — | — | — |
| median_pnl | 30 | 4 | 4 | **−53.98%** | −55.67% | 0% | −71.16% |
| deconcentrated | 0 | 0 | 0 | — | — | — | — |
| risk_adjusted | 30 | 1 | 1 | −2.42% | −2.42% | 0% | N/A |

三个有成交的臂全部负，且没有一笔盈利；不过 n 最大只有 4，按预注册 `n<20` 只描述。不能用这张表宣称“聪明钱跟单已在 Base 统计证伪”。

## 6. 为什么这次不使用全历史 recipient/sender 造大样本

这正是 RH 合集最值得吸收的教训。RH 曾因 Universal Router 误归因漏掉 11.23% resolved 买入；Base 的问题更严重：B1 高覆盖窗中旧 sender 丢失 99.4% resolved 钱包身份。B2 transfer-net 虽提高覆盖，但 beneficial coverage 82.88%、与 tx.from 不一致 67.72%，也没有通过归因门。

因此本任务的短窗输出仅保留为失效诊断，不把 fallback 身份包装成“历史常胜钱包”，也不把小样本负值包装成“没有 edge”。扩大样本必须先让 B2 同时满足 coverage≥90% 与 EOA mismatch≤2%，并为 FIFO PnL 建立独立链上 golden。

## 7. 判据逐项

| RH1 门 | 结果 | 状态 |
|---|---|---|
| 10s pooled mean>0 | 主选择器 n=0 | 未测试 |
| 去 top1 后>0 | 主选择器 n=0 | 未测试 |
| 每 powered 子窗不破 floor | 无 powered 子窗 | 未测试 |
| ≥2 powered regime 正 mean+alpha | 仅15h单一窗 | 未测试 |
| 显著跑赢 matched random wallet | 无主信号，无法匹配 | 未测试 |
| 延迟税后>0 | 无同 episode 三线样本 | 未测试 |
| MC p5/MDD | 无 powered 样本 | 未测试 |
| 最终 OOS 为正 | 无主信号 | 未测试 |

最终裁决：

- `CANONICAL_MERIT = UNTESTED`
- `RH_DECONCENTRATED_BASE_REPLAY = INCONCLUSIVE_NO_ELIGIBLE_WALLETS`
- `SELECTOR_BATTERY_DIRECTION = INVALID_FOR_MERIT_DIAGNOSTIC_ONLY`

B2-v3 更新：92.250897% 的子图覆盖未通过 holder concentration 与新增 golden；5 个均匀 archive 样本仅 1/5 对上，已整批回滚。回滚后 coverage=82.877094%、ambiguous=11.604500%，所以 RH1 仍不得改用 B2。`eth_getCode` 仍为 end_block 单点快照，不能替代逐 swap 时点证明。
- `GO = false`

## 8. 可复现命令

```bash
cd /opt/EATLQ
python3 -m unittest -v tests.test_base_rh1_smartmoney
python3 scripts/research/base_rh1_smartmoney.py
jq '.' mvp/diagnostics/base_rh1_smartmoney_20260714.json
```

回归测试 6/6 通过。测试精确覆盖：episode PnL、point-in-time 快照、延迟后首笔真实成交、真实卖向容量、不可卖 adverse 不删样本。

## 9. 证据与 hash ledger

| artifact | SHA256 |
|---|---|
| `scripts/research/base_rh1_smartmoney.py` | `598b7e3b285285a1125285806348b17ebcded754dcef035f4292bf9031b193b9` |
| `scripts/research/base_rh1_smartmoney_core.py` | `d4e88a7636d89e6e028c2c84d2e7a46bd5ee7e85dd83ebfe8bf401c98b813ef2` |
| `tests/test_base_rh1_smartmoney.py` | `fd7ac812ac5e135cb46fefd701e033f2a558d9e6c74a9747064fa9b9124ab0b8` |
| `mvp/diagnostics/base_rh1_smartmoney_20260714.json` | `95c14c85c4afe5a353f7b7dcef0604e5197121763205bc24a31d1998bd019022` |
| `mvp/diagnostics/base_rh1_smartmoney_trades_20260714.csv` | `a6f5c5718736ba2f9d4d67db994891ae40b2a7d00b897ef9e3e5b32eb2236043` |

RPC ledger：本轮 RPC calls = 0；只读本地 ClickHouse。B1/B2 已有独立 RPC/golden ledger，本轮复用其冻结窗口，不重新打 RPC。

## 10. SOP 最终确认

1. `FAILED_NO_7D_ALPHA` 仍 active：是，本报告不 supersede。
2. label freeze 仍为 1800s、ROUND_TRIP_COST=0.006、pool types v2/v3：是；本任务未改 label，只用独立预注册退出。
3. FeatureStore canonical 特征：未使用、未修改。
4. smart_money/pool_score 信号路径：未修改，未写 signals。
5. executor pending/dead-letter/chain_id：未修改。
6. compose/env 与 readiness：未修改，未读取或输出 `.env`。
7. P4/P5 schema：未修改。
8. 已复用同类资产：B1 resolved EOA、B2 transfer-net 审计、旧 P3、RH selector 教训。
9. 私钥/RPC token/API key：未读取、未写入、未打印。
10. 未用模型指标做 supersession：是。

## 11. 下一证据门

若继续，先修复 B2 的 EOA mismatch 至 ≤2%，再为 RH1 FIFO realized PnL 补独立链上 golden。两门同时通过前，Base 聪明钱 `merit=UNTESTED`，不 GO。
