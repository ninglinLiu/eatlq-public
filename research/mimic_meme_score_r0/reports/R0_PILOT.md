# R0_PILOT — Mimic 点时试点（仪器重构后）

**日期：** 2026-08-15  
**口径：** [`../GOAL.md`](../GOAL.md)  
**仪器：** [`R0_INSTRUMENT.md`](R0_INSTRUMENT.md)  
**POWER：** `UNDERPOWERED`  
**LIVE：** 禁止。无 PnL。

---

## 0. 一句话

同名克隆是常态，不是例外。梗分高的 12 个事件全部碎片化；主场链上能标出粘住的 19 个里，只有 8 个是「最早那个」赢了。牛来：56 个同名代币，BSC 主场 91% 粘在 `0xbeea…7777`，但最早出现的是 Robinhood 上的另一个合约。

上一版「12/22 M 高 I 塌」数字碰巧相同，**定义已经换过**：空分不再算塌。真正多出来的信息是克隆密度和「先发 ≠ 粘住」。

---

## 1. 收集（同名过滤后）

| 项 | 值 |
|---|---|
| 事件 | 22（未增删） |
| 同名代币 `contracts_all` | **354** |
| 打分集合（每事件最早 10） | 177 |
| 零命中 | E19 撸官、E21 夫妻文学（近失；不再用「夫妻」污染） |
| 单命中 | E20 山东学 → I 状态 `unknown` |
| Sol JSONL | 仍无明文 name，未使用 |

牛来冒烟：56 个同名、3 条链、BSC 28 个。搜索词能建克隆表。

---

## 2. 打分

| 项 | 值 |
|---|---|
| scores 行 | 1682 |
| 空分率 | 61.4%（I2/I3/I4、S1/S2/S3、E3/E4 仍空） |
| M | 事件级，clock=`t0_narrative` |
| I1 / I5 | 合约级，clock=`pit_create_order` |
| E | 事件级观察者常数，不再按合约复制 |

I 状态：

- `fragmented` 19
- `no_contract` 2
- `unknown` 1（山东学，只有 1 个合约，I1 只能空）
- `schelling_claimed` 0（没有任何合约的 I1 打成「高」——没有指认日志）

---

## 3. 模式（新定义）

**M 高：** M1/M4/M7 至少两维为高 → 12 个事件。  
**I 塌：** `fragmented`（同名≥3 且存在 I1=否 且无 I1=高）。空 ≠ 塌。

M 高 ∩ I 塌 = **12 / 12**。能搜到的高燃料词，在本仪器下全部碎。这不能证明「梗分无效」，只能描述：高 M 没有带来唯一发行权。

粘住（主场链、收集时钟，不是点时）：

| 项 | 值 |
|---|---|
| 能标出粘住 | 19 / 22 |
| 最早合约 = 粘住合约 | **8 / 19** |
| 牛来 | 主场份额 0.913 → `E01_niulai__bsc__0xbeea1d61`；最早是 robinhood，不相等 |

右尾、兑现结构：一律 `empty`。未测。

---

## 4. 对三问的回答

1. **点时分能否写下？** 能。空分仍过半。I1/I5 现在有创建顺序证据，不再靠 M3 或「收了 10 个」。
2. **克隆表能否建立？** 能。354 个同名代币。「再发一个牛来」在 19/22 个事件里是已发生的结构。
3. **M 高而 I 塌？** 在新定义下，12 个高 M 事件全部碎片化。**UNDERPOWERED**，且碎片化本身被 DexScreener 截断（约 30 对/次）放大，只能当描述。

更硬的描述性结果是第 4 句：**先发经常不是粘住。** 8/19。牛来是教科书。

---

## 5. 预登记裁决

全部 `UNDERPOWERED`。方向上仍更像「发行权 / 命名赛」而不是八点审美，但本轮没有独立的 I1=高，也没有可兑现 PnL，不能改名、不能开交易权。

---

## 6. 文件

- `events/aliases.csv` `events/m_scores.csv`
- `data/tables/contracts_all.csv` `contracts.csv` `scores.csv` `outcomes.csv` `event_outcomes.csv`
- `data/tables/collect_qa.json` `pattern_summary.json`
- `src/common.py` `collect_search.py` `score_and_label.py`
