# R1_PILOT — 粘住路径 + 指认日志

**日期：** 2026-08-16  
**口径：** [`../GOAL_R1.md`](../GOAL_R1.md)（主口径仍是 [`../GOAL.md`](../GOAL.md)，未改）  
**宇宙：** 22 事件，未增删  
**POWER：** `UNDERPOWERED`  
**路径过线时刻：** `DATA_INSUFFICIENT`  
**LIVE：** 禁止。无 PnL。R0 `scores.csv` / `outcomes.csv` 未回写。

---

## 0. 一句话

路径仪器能跑：牛来有完整小时线，22 个事件里 **0 个 `no_ohlcv`**。但「份额第一次过线发生在何时」几乎全被 GeckoTerminal 公共小时窗截断——14 场命名赛里 **13 个 `left_censored`，只有 MAGA 一个 `observed`，且那一次也是窗内重交叉，不是出生过线**。带时间戳的公开指认仍是 **0**，I1=高仍是 **0**。不能用本轮加强「先发≠粘住」，也不能开交易权。

---

## 1. 本轮只回答的三问

| 问 | 答 |
|---|---|
| 主场链上，成交份额第一次过线在何时？过线后有没有再碎？ | **时刻不够。** 13/14 场赛的第一根非尘埃小时已经过线。再碎：可计算的 14 场全部 `broke=0`，但左截断下这不构成「没碎」的证据。 |
| 过线合约是不是主场链上最早的那个？ | 原始标签 **10/14 =**。其中 2 个 `=` 的反例是窗口/同名污染（pump、harris），不可当发现。另外 2 个（官方 TRUMP、后出现的 HAWK）与 R0 收集快照同向，但仍是左截断。 |
| 有没有点时公开指认，且早于过线？ | **没有。** `dated=0`，I1=高 = 0。官网/推特 URL 未充数。 |

---

## 2. 状态计数

| status | n | 事件 |
|---|---|---|
| `observed` | 1 | E15 MAGA |
| `left_censored` | 13 | 牛来、Moodeng、Chillguy、Fartcoin、Neiro、Pnutt、pump 词、Bonk、TRUMP 碎片、Harris、Biden、Goatseus、HawkTuah |
| `single_series` | 6 | Clanker、Virtuals、Brett、Toshi、山东学、WEN（主场都只有 1 个同名） |
| `no_primary` | 2 | 撸官、夫妻文学 |
| `no_ohlcv` / `never` / `unsupported_network` | 0 | — |

牛来：6 条小时序列，第一根非尘埃小时份额 0.7982 → `E01_niulai__bsc__0xbeea1d61`。**路径仪器未 FAIL。**

`single_series` 不是数据坏了，是主场链上只有一个同名代币，构不成命名赛。

---

## 3. 两个「最早」不得混用

| 时钟 | 最早的定义 | 粘住的定义 | 最早=粘住 |
|---|---|---|---|
| R0 收集快照 | **全链**最早 `pair_created_at` | 主场 24h 量份额 | **8 / 19** |
| R1 路径 | **主场链**最早 | 小时份额第一次过线 | 原始 **10 / 14** |

牛来是教科书：**路径上 BSC 最早 = `0xbeea…7777`（eq=1）**；R0 说「最早≠粘住」是因为全链最早在 Robinhood。同一事实，两个时钟，两句都对。

路径粘住 ≠ 收集粘住的事件（不要调参，只记账）：

| 事件 | 收集粘住 | 路径粘住 | 怎么读 |
|---|---|---|---|
| E09 pump 词 | `9593dfjslk`（官方 PUMP，2026-08 才创建） | `5ngy33drvn`（2026-07-12 窗内 100%） | 同名污染 + 左截断。官方代币当时还不存在。 |
| E14 Harris | 未过线（46% vs 45%） | `3lt2dbbd5b`「The Cancer Bull」，创建时刻≈过线时刻 | 尘埃窗里新垃圾币冒充过线。 |
| E15 MAGA | `0x6aa56e1d` | `0x576e2bed` | 不同时钟：24h 快照 vs 2026-01 窗内过线。 |
| E16 Biden | `0x80999b02` | `0xc548e905` | 同上。 |

可信的「主场更早的那个不是路径粘住」、且与收集快照同向：E13 官方 TRUMP（`6p6xghyf7a`）vs 更早的 MAGA 站 TRUMP；E18 后出现的 `HAWKThXR…` vs 更早的 `4gfe6mbdor`。两者都是 `left_censored`，**没看见过线发生**。

---

## 4. 牛来（必测）

- 主场 BSC，选 8 个，拿到 6 条小时线。
- 第一根非尘埃小时 = 池子出生所在小时（`t_attach=1786723200` = 2026-08-14 16:00 UTC；`pair_created_at` 约 16:18）。`hours_from_earliest_to_attach=-0.31` 是小时桶对齐，不是「过线早于创建」。
- 状态是 `left_censored`，因为**看得见的第一小时已经过线**，不是磁带被剪。对一个新币，这等于「从第 0 小时就粘住」。
- 主场最早 = 路径粘住 = `0xBEEA1D618e533a387D941F58a7d4c9b7bD377777`。
- DexScreener 社交字段是 `x.com/search?q=$牛来`，不是指向该 CA 的带时间戳指认。

---

## 5. 唯一的 `observed`：MAGA

E15 在可得磁带里先有未过线小时，再于 2026-01-02 20:00 UTC 过线到 `0x576e2BeD…`，份额 0.9962。  
`hours_from_earliest_to_attach≈20998`（约 2.4 年）。这是 **GT 窗内的重交叉**，不是 2023 年出生过线。收集时钟上现在赢的是另一个 MAGA。不当成「我们看见了发行权形成」。

---

## 6. 指认

| 项 | 值 |
|---|---|
| `i1_pointing.csv` 行 | 1395（缓存社交重复多，24 个合约） |
| `dated=1` | **0** |
| `i1_r1.csv` I1=高 | **0** |
| R0 创建顺序并列 | 37 空 / 3 否（TRUMP 粘住、Harris 伪粘住、HAWK 粘住） |

无时间戳的官网/推特/Telegram **未**打成高。`pairCreatedAt` 明确标成「不是指认」。

---

## 7. 仪器缺口（不是研究发现）

1. **GeckoTerminal 公共小时窗左截断。** 多数老币的 `t_attach` 落在 2026-01-20 07:00 UTC 一带；更深翻页返回 HTTP 401。官方说明：公共 API [约 30 次/分钟](https://apiguide.geckoterminal.com/faq)；Analyst 及以上才从 2021-09 起查历史，且 [单次最多约 6 个月](https://docs.coingecko.com/v3.0.1/reference/pool-ohlcv-contract-address)。本轮无付费 key，诚实记为左截断。
2. **DexScreener `pair_address` ≠ GT 池。** 第一轮大量 `no_ohlcv` 是这个。已加「对地址无 K 线则回退 GT 最优池」。Bonk / WEN 等因此才有序列。
3. **429 不得当成功缓存。** 已修：429 重试且不落盘；旧 429 JSON 已清。
4. **单池成交 ≠ 代币全市场成交。** 份额用一条池的小时 USD 量，多池代币会被低估。
5. **`pump` / `harris` 同名污染。** 路径时钟会把窗内唯一非尘埃的克隆标成粘住。这是别名问题，不是发行权发现。
6. **指认源不够。** DexScreener `info.socials` 没有可解析时间戳。要 I1=高，需要带日期的公开文本（归档推文、链上公告），本轮没有去翻。

---

## 8. 预登记对照（不调参）

GOAL_R1 写过：若 `observed` 子集里「最早=路径粘住」明显高于或低于收集快照 8/19，只描述。

`observed` 只有 1 个，且 eq=1。样本不够做对照。  
14 个有路径粘住的事件里原始 10/14，**高于** 8/19，但时钟不同、且含 2 个伪反例。不调阈值，不改宇宙。

---

## 9. 对操作心理的含义（仍不是交易建议）

缺的两只钟——「何时过线」和「过线前有没有人指着这个 CA」——本轮都没装上。  
R0 的结构事实还在：同名克隆是常态；牛来在 BSC 上粘在 `0xbeea…7777`。  
默认观察者（延迟外人、无预置弹药）仍然没有可兑现轨道。没有轨道，就不是你的市场。

---

## 10. 文件

- `GOAL_R1.md`
- `src/r1_attach_path.py` `src/r1_pointing.py` `src/http_cache.py`
- `data/tables/attach_path.csv` `attach_hourly_qa.json`
- `data/tables/i1_pointing.csv` `i1_r1.csv`
- 本报告
