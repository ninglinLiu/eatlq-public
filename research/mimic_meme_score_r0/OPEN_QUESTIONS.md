# OPEN_QUESTIONS — Mimic R0 / R1

异议与待决写这里。**禁止改 `GOAL.md` 已冻结口径。**

## 开放

1. **t0 时间锚精度**：事件 `t0` 用文化/搜索出生锚；合约用 `pair_created_at`。Web2 热搜日与首池日仍未对齐到同一时钟。
2. **Solana name 字段**：`creates_labeled.jsonl` 无明文 name/symbol，Sol 侧本轮仍只靠 DexScreener。
3. **同图匹配**：未做 image URI 聚类。
4. **E 层对照**：仍只有「无预置弹药」一套。
5. **粘住阈值**：主场链份额 ≥50%，或 ≥35% 且 ≥第二名 1.4 倍。Harris 收集时钟 46% 未过；路径时钟把窗内新币标成粘住——两钟不得合并。
6. **DexScreener 搜索截断**：单次 search 约 30 对，`contracts_all` 不是全历史同名宇宙，I5 是下限。
7. **别名是否算改宇宙**：未增删 22 个事件；搜索词收紧写在 `events/aliases.csv`。`pump`→`pumpfun` 是过滤，不是换事件。R1 显示 `pump` / `harris` 仍会把无关同名灌进路径粘住。
8. **GT 公共小时窗**：老币第一根可得小时已过线 → `left_censored`。深翻页 401。付费 onchain 历史（约 6 个月/次，Analyst+）本轮未用。见 [GT FAQ](https://apiguide.geckoterminal.com/faq) 与 [OHLCV 文档](https://docs.coingecko.com/v3.0.1/reference/pool-ohlcv-contract-address)。
9. **主场最早 vs 全链最早**：牛来路径 eq=1（BSC 最早=粘住）；R0 8/19 用的是全链最早（Robinhood）。对照必须分列。
10. **带日期指认**：DexScreener socials 无时间戳。I1=高 需要归档推文/链上公告，本轮没有人工翻档。

## 已决（R0 重构后）

- N=10 仍是打分集合；粘住改在 `contracts_all` 的主场链上算。
- **空分不算 I 塌。**
- I1 不得从 M3 推导。
- 主真值 = `GOAL.md`。
- 仪器说明见 `reports/R0_INSTRUMENT.md`。
