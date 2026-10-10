# 资金源聚类 — 可行性 PoC（2026-06-24）

状态：只读探查。**结论：当前数据不足以实现，被基础设施阻塞。** 下面给出阻塞点 + 解锁所需。SYSTEM_PANIC 不受影响。

---

## 目标

rug deployer 每次换新 EOA（PARADAENGI 实测用了 2 个不同 EOA：`0x715c98ce…` / `0xc8512b53…`）。
若能找到"**谁给这些 EOA 打了启动资金（gas）**"，把同一上游出资钱包下的多个 rug deployer **连成一簇**，就能破掉"换 EOA"这招，扩大 F 黑名单覆盖面到 deployer 黑名单挡不住的范围。

---

## 可行性探查结果

| 需要的数据 | 是否可得 | 说明 |
|---|---|---|
| **rug deployer EOA（262 个 rug）** | ❌ 不可得 | 需合约创建交易；无 Basescan key、无 trace 批量推导 |
| **deployer 的出资人（首笔入账 ETH 的 sender）** | ❌ 不可得 | 启动资金是**原生 ETH**；需 explorer txlist 或原生转账/trace 表 |
| Basescan / Etherscan API key | ❌ 无 | `.env` 无 `BASESCAN_*` / `ETHERSCAN_*` |
| ClickHouse 原生转账 / traces / 带 value 的交易表 | ❌ 无 | 仅 `erc20_transfers*` + `raw_evm_logs` + `tx_from_by_hash`（无 value 字段） |
| ERC20 转账表能否代用 | ❌ 不能 | **实测**：2 个 PARADAENGI deployer 在 `erc20_transfers_decoded` 中**完全不出现**（gas 资金是原生 ETH，不经 ERC20）；查"共同 ERC20 出资人" = 空 |

### PoC 实测（决定性）
- 查 `erc20_transfers_decoded` 里 `recipient ∈ {两个 PARADAENGI deployer}` → **0 行**。
- 查两 deployer 的共同 ERC20 sender → **空**。
- → 原生 ETH 资金流不在任何现有表里，**ERC20 表代用不了**。

---

## 结论：被阻塞，不是不可行

资金源聚类的**思路是对的**（rug 团伙换 EOA 但启动资金常来自同几个源头），但**当前数据栈缺两样东西**，缺一不可：

1. **拿 deployer**：合约创建者 → 需 **Basescan `getcontractcreation`** 或对每个 token 做创建交易回溯。
2. **拿出资人**：deployer 的首笔入账原生 ETH 的 sender → 需 **Basescan `txlist`/`txlistinternal`** 或一张**原生转账/internal-tx/traces** 表。

### 解锁所需（按成本从低到高）
1. **加一个 Basescan API key**（最便宜）：`getcontractcreation`（拿 deployer）+ `txlistinternal`（拿原生入账出资人）。262 个 token + N 个 deployer，几百次 API 调用即可建出资金图谱。
2. **或** 在 ClickHouse 落一张**原生转账/traces 表**（成本高，需重新索引链上 internal tx）。
3. 有了上面任一，再做：deployer→funder 图，funder 出度≥2 即标记整簇，灌入 F 的 deployer 黑名单。

---

## 与任务 1/2 的关系（已完成部分给的间接证据）

- 任务 1 已证：**89% 的 rug 是模板克隆**（同 bytecode skeleton），但 skeleton 太粗会误杀（34/67 撞正常币）。
- 资金源聚类本是为绕过"换 EOA"、补 deployer 黑名单覆盖面——但它依赖的资金图谱数据当前不存在。
- **现实可行的下一步**（不依赖缺失数据）：
  - 用 **Basescan key** 先把 262 rug 的 deployer 全部解出来 → 直接灌 deployer 黑名单（EOA 不误杀正常币，零误杀，立即可用），覆盖面虽受"换 EOA"限制，但仍挡住惯犯复用的 EOA。
  - 之后再用同 key 拉出资人做聚类。

---

## changed files / commands（全只读）
- ClickHouse 探查：`SHOW DATABASES/TABLES`、`DESCRIBE erc20_transfers_decoded / tx_from_by_hash`、PARADAENGI deployer 对手方查询（0 行）
- RPC 探查：trace_/debug_ 方法存在但批量推导 262 deployer 成本过高
- 未改任何 env/逻辑/实盘
