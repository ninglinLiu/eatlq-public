# Mature MCap Census V0（成熟币 ≥\$1M 普查）

生成时间见 `CENSUS_SUMMARY.json`。  
**不是**新池 Rescue；`deploy_allowed=false`。

## 问题

Base 上 UniV3（WETH 腿）里，有多少代币看起来曾达到约 **\$1M** 规模，值得考虑「低买高卖」而不是新盘狙击？

## 方法（仓库无历史市值，故用混合代理）

1. **成交量候选**：累计 WETH 腿成交 × \$3000 ≥ **\$100k** → 556 个（已排除 WETH/原生零地址）
2. **DexScreener 当前** `marketCap` / `fdv`（只有 current，没有 ATH）
3. **链上峰值 FDV 代理**：tick 价格的 **q99.9** × 当前 `totalSupply`（去掉极端脏 tick；FDV 上限 \$100B）
4. **保守命中**：非稳定币，且  
   `当前 DS mcap/fdv ≥ \$1M` **或** `(链上峰值 FDV≥\$1M 且 累计成交≥\$1M)`

## 结果（v2）

| 指标 | N |
|---|---:|
| 成交量候选（≥\$100k） | 556 |
| DexScreener 当前仍有 Base pair | 31 |
| 当前 DS 市值 ≥ \$1M（非稳定币） | 6 |
| 链上峰值 FDV≥\$1M 且成交≥\$1M | 36 |
| **保守命中 ≥\$1M** | **40** |

清单：`HIT_1M_CONSERVATIVE.csv`  
全量候选：`MATURE_MCAP_CANDIDATES.csv`

## 局限

- DexScreener 大量「曾经很活跃、现在搜不到 Base pair」（525/556）→ 历史 ATH 覆盖不足  
- 链上峰值是 **FDV 代理**，不是严格流通市值；供应增发历史未建模  
- 下一步若做策略：必须冻结「**首次达到 \$1M 之后**」才可交易的 PIT 时点，再测低买高卖  

## 复现

```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.mature_mcap.census_ath_proxy
```
