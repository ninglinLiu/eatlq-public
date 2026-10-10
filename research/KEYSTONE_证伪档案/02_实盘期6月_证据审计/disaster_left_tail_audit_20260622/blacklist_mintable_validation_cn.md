# 防拉黑/防增发门禁 有效性验证（只读，2026-06-22）

只读，不改 env、不改逻辑、不动实盘。验证刚开的 `BLOCK_BLACKLIST` / `BLOCK_MINTABLE` 对 KEYSTONE 到底有没有用。

## 先纠正一个数据陷阱
缓存 `_external_risk_current.json` 的 GoPlus 列被污染：508 个 token 里 448 个是 HTTP 429
`"too many requests"`（抓取时 0.15s 间隔被限流），**不是真·无数据**。所以缓存算出的「GoPlus 覆盖率
11.8%」是假的。下面用 2.5s 间隔 + 退避重新探针。

## 检查1 — API 覆盖率探针（FRESH，n=100，scope=holdout token）
脚本 `_check1_fresh_probe.py`

| 指标 | 结果 |
|---|---|
| GoPlus 有数据 | 92/100 (92%) |
| GoPlus 真·无数据 | 0/100 (0%) |
| GoPlus 限流(放弃) | 8/100 (8%) |
| Honeypot.is 有数据 | 58/100 (58%) |
| 两家都有 | 53/100 (53%) |
| **可判 blacklist/mintable** | **77/100 (77%)** |
| 实际命中 blacklist | 0/100 (0%) |
| 实际命中 mintable | 1/100 (1%) |
| 实际命中 honeypot | 1/100 (1%) |

结论：覆盖率其实很高（GoPlus≈92%，可判 blacklist/mintable≈77%），但**正向命中率极低（拉黑 0%、增发 1%）**。

## 检查2 — 带过滤器重回测（n=131 基准样本）
脚本 `_check2_filter.py`，基准集 = `risk_rebacktest.py` 的 `mode=none`（131 笔）。
过滤规则按我开的 live 语义：**只在 GoPlus 正向标 blacklist/mintable 时删单**（缺数据不删 → live 是 canary 缩仓，不丢）。

| 场景 | n | 毁灭率(≤-50%) | mean | 月化($1k cap) | MDD |
|---|---:|---:|---:|---:|---:|
| 不开过滤 | 131 | 5 (3.8%) | +0.542 | +57.1% | -6.7% |
| 开过滤 | 131 | 5 (3.8%) | +0.542 | +57.1% | -6.7% |

- 删掉的单：**0 笔**（107/131=81.7% 有可判数据，其中正向命中 = 0）。
- 既不是「专删坏单」也不是「无差别砍单」，而是**对这个历史样本完全 no-op**：毁灭率不变、月化不变、出单量不变。

## 综合结论
1. 门禁能看见大多数 token（覆盖率不是问题），但历史 KEYSTONE 选中的微盘 alpha 里**几乎没有被正向标记的拉黑/增发币** → 过滤器在历史样本上是「零成本、零收益」的纯保险。
2. 唯一观察到的真实命中是上一步 config-fix 里的 **AERO**（live exec 唯一成交过的币），它因 `is_mintable=1` 被 BLOCK_MINTABLE 拦——而 AERO 是 Aerodrome 治理币的合法排放，属误杀。

## 残余风险 / UNKNOWN（不能拿当前数据当结论）
- 探针是 **current-state**，不是「池龄~10min 进场那一刻」的 block-state。真·进场时 token 刚出生，GoPlus 很可能还没收录 → 实际进场覆盖率大概率**低于** 77%，门禁在最该起作用的瞬间可能是瞎的（此时 live 配置会让它走 canary，不是拦）。这是最大 UNKNOWN，需 forward 实测（每笔 live 记录进场那刻的 GoPlus/honeypot 应答）。
- 拉黑/增发型 rug 多数是**买入后**owner 才 toggle，pre-buy 扫描无论覆盖率多高都抓不到。这俩门禁主要挡「出生即坏」的币，挡不了「买后变坏」。

## 一句话建议（带 n）
**两个都留**：在 n=131 历史样本上 0 删单、毁灭率/月化零变化（纯保险，无 alpha 损失）；唯一代价是 `BLOCK_MINTABLE` 会误杀 AERO 这类合法增发币——若以后要做 AERO/蓝筹腿，再单独把 `BLOCK_MINTABLE` 关回 0 即可，`BLOCK_BLACKLIST` 可无条件保留。
