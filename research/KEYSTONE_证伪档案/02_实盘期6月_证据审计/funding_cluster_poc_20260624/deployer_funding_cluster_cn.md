# 资金源聚类 + 全量 deployer 黑名单（2026-06-24）

状态：**已完成** — 245 个零误杀 deployer EOA 已种入 live F 黑名单；资金源聚类图谱已产出。

---

## 数据源与 API

| 来源 | 用途 | 结果 |
|---|---|---|
| **Blockscout** (`base.blockscout.com`) | `getcontractcreation` + `txlist` | ✅ 主路径，免费可用 |
| **Basescan / Etherscan V2**（用户 key） | 同上 | ❌ Free tier 不支持 Base chain（需升级 paid plan） |
| 644 标注集 | FP 验证 | `_labeled_universe.json` + `_uni_shards/` |

脚本：`scripts/research/base_lefttail/mine_rug_deployer_funding.py`

---

## 1. Deployer 挖掘 + 零误杀验证

对 **262 rug + 378 clean** token 拉 contract creator：

| 指标 | 数值 |
|---|---|
| rug token | 262 |
| 解析到 creator 的 rug token | 253 |
| 唯一 rug deployer EOA | 246 |
| 唯一 clean deployer EOA | 143 |
| **rug ∩ clean 重叠 deployer（危险）** | **1** |
| **零误杀可种 deployer** | **245** |
| rug token 覆盖率（safe deployer） | **245/262 = 93.5%** |

### 唯一重叠 deployer（已排除）

```
0xeda60b619af7b3114cf8143348e42efb0f1f15b6
  rug tokens:  8
  clean tokens: 2  ← 若拉黑会误杀 2 个 clean 赢家
```

### 未覆盖的 17 个 rug token

- 9 个：Blockscout 无 creator 记录（索引缺失 / 403 限流）
- 8 个：creator = 上述重叠 deployer（故意不种）

**PARADAENGI**（2026-06-23 live 事件，不在 644 标注集）的 2 个 deployer 仍由 `seed_paradaengi()` 单独维护。

---

## 2. 资金源聚类

对每个 rug deployer 取**首笔入账原生 ETH** 的 `from` 作为 funder：

| 指标 | 数值 |
|---|---|
| 有 funder 记录的 rug deployer | 246/246 |
| **funder 连接 ≥2 rug deployer 的簇** | **18** |
| 簇内 deployer 总数 | 41 |

### Top 3 资金簇

| Funder | Rug deployers |
|---|---|
| `0x1ce8d9a76e…` | 4 |
| `0x1b8ef3ca0f…` | 3 |
| `0x11703d5bb2…` | 3 |

### PARADAENGI 资金链（手工验证）

| Deployer | 首笔 ETH 来自 |
|---|---|
| `0x715c98ce…` | `0x37bfc19a…`（3.56 ETH） |
| `0xc8512b53…` | （见 `deployer_funding_graph.json`） |

**设计决策**：live 只拉黑 **deployer EOA**（样本内零误杀），**不拉黑 funder** — funder 可能是交易所/混币/无辜中转，误杀风险高。资金簇用于审计与后续「新 EOA + 已知 funder」预警，不直接入闸。

---

## 3. Live 部署

| Redis 键 | 内容 |
|---|---|
| `executor:blacklist:deployer` | 245 safe + 2 PARADAENGI = **247** |
| `executor:blacklist:bytecode` | 33 safe skeletons（上轮已种） |
| `executor:deployer_cache:{token}` | preflight 解析 creator 缓存（90d TTL） |

代码变更：

- `services/executor/fixtures/safe_rug_deployers.json` — 245 EOA
- `cluster_blacklist.seed_safe_rug_deployers()` — 启动 idempotent 写入
- `cluster_blacklist.resolve_token_deployer()` — preflight 自动查 creator（Blockscout + Redis cache，fail-open）

---

## 4. 与 skeleton 黑名单对比

| 维度 | Skeleton（33） | Deployer（245） |
|---|---|---|
| 历史 rug 覆盖 | 38.2% | **93.5%** |
| 644 集误杀 | 0 | **0** |
| 换 EOA 攻击 | 挡不住 | 单 EOA 可挡；新 EOA 需资金簇/E 窗 |
| preflight 成本 | 1× `get_code` | cache miss 时 +1 explorer HTTP |

---

## 产物路径

- `deployer_funding_graph.json` — 完整 token→creator→funder 图 + 18 资金簇
- `deployer_blacklist_validated.json` — safe/unsafe deployer 列表
- `scripts/research/base_lefttail/_deployer_seed_validated.json` — 研究副本

---

## Basescan key 说明

用户提供的 key 已写入 `infra/compose/.env`（`BASESCAN_API_KEY`，gitignore）。当前 **Free tier 不支持 Base**；executor 默认走 Blockscout。升级 Etherscan paid plan 后无需改代码即可切到 V2。
