# GOAL R1：粘住路径 + 指认日志

**发起人**：用户  
**执行者**：Cursor Agent  
**创建时间**：2026-08-16  
**状态**：完成（描述性；过线时刻 DATA_INSUFFICIENT）  
**工作区**：`/root/mimic_meme_score_r0/`  
**前置**：R0 仪器已重构（`reports/R0_INSTRUMENT.md`）。宇宙锁定。  
**主口径**：[`GOAL.md`](GOAL.md) 不改。本文件只加两个时钟。

---

## 0. 硬约束

1. **`NO_LIVE.lock` 缺失则退出。** 禁止签名、广播、swap、LP、授权、改 `/opt/EATLQ` live。
2. **禁止改 `GOAL.md`，禁止改 `events/EVENT_UNIVERSE.md` 事件名单，禁止改 R0 `scores.csv` / `outcomes.csv`。** R0 冻结。本轮只写新表。
3. **禁止交易回测、延迟买入、PnL、仓位、滑点。** 粘住是份额标签，不是收益。
4. **禁止用后来的量、上所、ATH 回填 I1=高。** I1=高 只允许：有**带时间戳**的公开指认，且 `t_point < t_attach`（无粘住则不得打高）。
5. **无时间戳的官网/推特链接 = 未标注，不是指认。** 记入 `i1_pointing.csv`，`dated=0`。
6. **禁止解锁宇宙、禁止从热榜补事件。**
7. **诚实优先。** GeckoTerminal 小时窗左截断、单序列、请求失败，写 `left_censored` / `single_series` / `no_ohlcv`，不许把收集时快照抄进路径表冒充历史。
8. **最高状态 `UNDERPOWERED` 描述。** 不许 LIVE_GO，不许改名「发行权分数」。

---

## 1. 本轮只回答

1. 主场链上，成交份额**第一次**过线发生在何时？过线之后有没有再碎？
2. 过线的那个合约，是不是主场链上**最早**的那个？
3. 有没有**点时**公开指认指向该地址，且早于过线？

不回答：买不买、有没有 alpha、庄是谁、怎么发币。

---

## 2. 预登记仪器

### 2.1 粘住路径

- 样本：R0 `contracts_all.csv` 里 `on_primary_chain=1` 的代币。
- 每事件最多取 **8** 个：按收集时 24h 量降序，并强制纳入主场链最早代币（若有创建时间）。
- 数据：GeckoTerminal 只读小时 OHLCV（USD volume）。响应落盘 `data/raw/gt_*`。
- 网络映射：`bsc→bsc`，`solana→solana`，`base→base`，`ethereum→eth`。其他主场本轮 `unsupported_network`。
- 尘埃小时：该小时主场样本合计成交 **< 500 USD** 不参与过线判定。
- 过线（与 R0 主场规则一致）：份额 ≥50%，或 ≥35% 且 ≥第二名 1.4 倍。
- 需要至少 **2** 条有效序列才叫命名赛；仅 1 条 = `single_series`。
- 状态：
  - `observed`：先有未过线小时，再出现过线。
  - `left_censored`：可得的第一根非尘埃小时已经过线（历史被截断，常见于老币）。
  - `never`：有赛、有数据、从未过线。
  - `single_series` / `no_ohlcv` / `unsupported_network` / `no_primary`。
- 再碎：过线后连续 6 个非尘埃小时份额 <30% → `broke=1`。
- 输出：`data/tables/attach_path.csv`（事件级）、`data/tables/attach_hourly_qa.json`（功效，不含逐小时全表除非需要复核牛来）。

### 2.2 指认日志

- 对象：每事件的路径粘住合约（若有）+ 主场最早合约。
- 来源：已缓存 DexScreener search 的 `info.socials` / `websites`；必要时只读 `latest/dex/tokens/{address}`。
- `dated=1` 仅当载荷里有可解析时间戳。推特 URL 本身不算 dated。
- I1 叠加写 `data/tables/i1_r1.csv`，**不回写** R0 `scores.csv`。
  - `i1_pointing=高` 仅 dated 且 `t_point < t_attach`。
  - 无 dated → `空`。
  - 创建顺序的「同链已有更早同名」仍是 R0 的 `否`，本轮只并列，不合并成神分。

### 2.3 预登记对照

- `earliest_equals_path_attach`：路径粘住合约 == 主场最早合约。
- 与 R0 `earliest_equals_attach`（收集快照）分列，不得混用。
- 若 `observed` 子集里「最早=路径粘住」的比例明显高于或低于快照 8/19，只描述，不调参。

---

## 3. 失败条件（写了就算完成，不是坏事）

- 牛来拿不到小时序列 → 路径仪器 FAIL，报告只写原因，不算「先发≠粘住」加强。
- 全部事件 `left_censored` 或 `no_ohlcv` → 路径 `DATA_INSUFFICIENT`。
- 零条 dated 指认 → I1=高 仍为 0，记为指认仪器缺口，不许用社交链接充数。

---

## 4. 交付

| 路径 | 作用 |
|---|---|
| `GOAL_R1.md` | 本口径 |
| `src/r1_attach_path.py` | 只读 GT 小时成交 |
| `src/r1_pointing.py` | 指认日志 |
| `data/tables/attach_path.csv` | 路径结果 |
| `data/tables/i1_pointing.csv` | 指认原始行 |
| `data/tables/i1_r1.csv` | I1 叠加（不覆盖 R0） |
| `reports/R1_PILOT.md` | 描述性报告 |

执行顺序：先写本 GOAL → 牛来冒烟 → 22 事件路径 → 指认 → 报告。
