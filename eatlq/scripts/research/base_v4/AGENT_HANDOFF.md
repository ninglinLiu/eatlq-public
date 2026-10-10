# V4 Base 离线研究 — Agent 交接文档

> **读者**：懂链上/DeFi 的技术 agent  
> **委托人**：非技术背景，需要你帮忙判断「这研究值不值得继续、卡在哪、下一步该走哪条路」  
> **仓库路径**：`/opt/EATLQ/scripts/research/base_v4/`  
> **对话日期**：2026-06-24 前后  

---

## 1. 用一句话说我们在干什么

在 **Base 主网** 上做 **Uniswap V4 离线 alpha 研究**（监听新池、解码 hook、分类、fork 回放打标），目标是判断 **hook 维度能否当 V4 过滤器**，以及 **fork 回放能否给出可信的 entry/exit 标签**。

**明确不是**：上线策略、改 V3 live executor、动生产 Redis key。

---

## 2. 硬约束（违反 = 事故）

| 约束 | 现状 |
|------|------|
| 独立 RPC | `RESEARCH_RPC_URL` / QuickNode，与 live executor 分离 |
| RPC 预算 | 硬上限 **20,000** 次 `eth_*`；计数器 `/tmp/v4_research_rpc_counter.json`，当前 **7863** |
| 内存 | research cgroup `memory.max=2048MB` |
| CPU | ~50%（`cpu.max=400000/100000`）+ `nice -n 19` |
| Live 安全 | 跑 fork-replay 前读 `positions:open_pools`（docker redis），有持仓则停 |
| 禁止 | 读/写 live `.env`、改 executor 容器、并行 >1 anvil（selftest 串行） |

Live 容器名：`eatlq-executor-1`，Redis：`eatlq-redis-1`。

---

## 3. 已完成阶段

### Phase 1 — Pool Listener ✅

- 扫描 **14 天** `Initialize` 事件（PoolManager `0x498581fF718922c3f8e6A244956aF099B2652b2b`）
- **80,793** 个池，块范围约 `47,151,951 → 47,756,705`
- 输出：`/tmp/v4_created_pools.jsonl`（~60MB）
- Checkpoint：`/tmp/v4_listener_checkpoint.json`
- 脚本：`v4_pool_listener.py`

**14d 结构统计**（Initialize 计数，非 TVL）：
- WETH 池：32,989
- vanilla：20.4%
- RETURNS_DELTA hook：43.5%
- 唯一 hook 地址：7,785

### Phase 2 — Hook 分类 + 模板 ✅（部分）

- `hook_flags.py`：LSB 14-bit hook 标志解码
- `v4_hook_classifier.py`：聚类 + runtime bytecode hash
- `build_hook_templates.py` + `hook_templates.json`：4 个已知模板（含 clanker、Top-3 RETURNS_DELTA）
- 输出：`_hook_classification.json`
- **已修 bug**：classifier 模板匹配键从 `bytecode_hash` 改为 `runtime_hash`

### Phase 3 — Fork Replay ⚠️ 部分可用

三条路径都试过：

| 路径 | 做法 | 结果 |
|------|------|------|
| **A. Universal Router** | `v4_swap.py` 直接 V4_SWAP | 全 revert；Base 上几乎无 direct UR V4_SWAP（路由走 intermediary） |
| **B. PoolManager helper** | `V4ReplayHelper.sol` + `v4_pool_swap.py` roundtrip | Forge 单测通过；**roundtrip 在 Base fork 上 buy 后 WETH `transferFrom` 在 unlockCallback 内 revert**（官方 PoolSwapTest 同样复现） |
| **C. Tx replay（当前主力）** | `v4_tx_replay.py`：缓存 Swap tx + anvil `eth_call` impersonate | **91.7% 成功率**（24 池 batch）；**不计 research RPC**（走 archive fork） |

**Roundtrip 仍 0% tradeable** — 不能产出完整 entry→exit alpha 标签。

### Phase 4–5 ✅（基于有限标签）

- `v4_convexity.py` → `_convexity_report.json`
- `v4_decision_metrics.py` → `_decision_metrics.json`

---

## 4. 当前核心结论（请帮忙判断的三件事）

### (b) Hook 维度是否值得研究 — **倾向 YES**

- 14d hooked 池占比 **79.6%**，RETURNS_DELTA **43.5%** >> 25% 阈值
- Phase 5.3 readout：`worth_research_hook_filter`
- **局限**：Initialize 计数 ≠ TVL/成交量；未做 hook 与 rug 率的因果证明

### (c) Tx replay 能否当 sandbox ground truth — **倾向 PARTIAL YES**

- Batch n=24：**22 onchain_swap_ok / 2 replay_blocked**
- 按 tier：vanilla **100%** swap replay ok；returns_delta_high **81.2%**
- 失败多为 `0xf4059071` 或 allowance — 疑似聚合器/permit 路由，单 tx eth_call 无法复现
- Phase 5.1 readout：`tx_replay_viable`（但 roundtrip 仍 blocked）

### (a) 完整 roundtrip fork-replay alpha — **NO，仍 blocked**

- 即使 vanilla 池，官方 `PoolSwapTest.swap` 成功后 settle 失败
- 根因未定位；继续查主要耗 anvil 时间而非 RPC
- **不能**据此部署策略

---

## 5. 关键数字快照

```
RPC 用量：7863 / 20000
监听池数：80793（14d）
Swap index：542 池（500 块 lookback，1× eth_getLogs）
Fork labels：24 池（tx-only batch）
Selftest：5 池（3 Top RD hooks × ~2）
Live 持仓：0（跑 batch 时）
```

---

## 6. 文件地图

| 文件 | 用途 |
|------|------|
| `research_rpc.py` | RPC 计数 + 限速 |
| `v4_pool_listener.py` | Initialize 回溯 |
| `v4_hook_classifier.py` | Hook 分类 |
| `hook_templates.json` | 已知 hook 模板 |
| `v4_tx_replay.py` | Swap index + 单 tx eth_call 回放 |
| `v4_pool_swap.py` | PoolSwapTest roundtrip（blocked） |
| `v4_fork_replay.py` | selftest / batch / `--tx-only` |
| `v4_convexity.py` | Phase 4 |
| `v4_decision_metrics.py` | Phase 5 |
| `live_gate.py` | Redis/docker live 门控 |
| `run_in_cgroup.sh` | 资源限制 wrapper |
| `foundry/test/V4ReplayFork.t.sol` | 文档化 settle bug |

**产出 JSON**（同目录 `_*.json` + `/tmp/v4_*`）：
- `_fork_replay_labels.json` — 24 池 tx-only 标签
- `_fork_replay_selftest.json` — 5 池 selftest
- `_convexity_report.json`
- `_decision_metrics.json`
- `/tmp/v4_pool_swap_index.json` — poolId → {tx_hash, block}

---

## 7. 已知阻塞与未解问题

1. **Base fork WETH settle-in-unlock**  
   - 现象：`unlockCallback` 内 `WETH.transferFrom(user, PoolManager, amount)` revert  
   - 范围：我们的 helper + 官方 PoolSwapTest  
   - 隔离测试（forge）：纯 transferFrom、sync+transferFrom **在 unlock 外** 通过  

2. **UR 直接 swap 路径不可用**  
   - Base 路由结构不同；last 2000 blocks 无 direct V4_SWAP → UR  

3. **cast run 整块 replay**  
   - anvil OOM/断连；已弃用  

4. **Swap index 仅 500 块**  
   - stratified batch 只能抽到 **24** 池（目标 30）；扩 lookback = +2 RPC  

5. **样本量不足**  
   - n=24~25，不够做统计显著结论；v4.md 建议 n≥30 再下结论  

---

## 8. 给下一个 agent 的决策题

请委托人关心的，本质上是：

1. **Hook 过滤线是否继续投研究资源？**  
   - 现有证据：结构占比支持；缺 rug/alpha 因果链  

2. **Tx replay 标签是否足够替代 roundtrip？**  
   - 能证明「链上曾成功 swap」  
   - **不能**证明「我们能否 buy+sell 套利/退出」  

3. **Roundtrip settle bug 值不值得深挖？**  
   - 若目标是完整 alpha 标签 → 必须解  
   - 若目标是 hook 结构筛选 → 可先跳过  

4. **RPC 预算剩余 ~12k，优先花在哪？**  
   - 扩 swap index lookback（便宜）  
   - 扩 hook 模板 bytecode 库（中等）  
   - aggregator 渗透率 5.2（贵，未跑）  

---

## 9. 可复现命令

```bash
cd /opt/EATLQ/scripts/research/base_v4

# 查看 RPC 用量
cat /tmp/v4_research_rpc_counter.json

# 刷新 swap index（+2 RPC：blockNumber + getLogs）
python3 v4_fork_replay.py --build-index

# tx-only 打标（0 RPC，耗 anvil 时间）
bash run_in_cgroup.sh python3 v4_fork_replay.py --batch --tx-only --limit 30

# selftest（tx replay + roundtrip 对比）
bash run_in_cgroup.sh python3 v4_fork_replay.py --selftest

# Phase 4–5
python3 v4_convexity.py
python3 v4_decision_metrics.py
```

---

## 10. 诚实总评（可直接转述给委托人）

| 问题 | 答案 |
|------|------|
| 能上线吗？ | **不能**。无 roundtrip alpha，仅研究证据。 |
| Hook 研究值得继续吗？ | **值得探索**（category b），但非已验证策略。 |
| 最大技术风险？ | Roundtrip settle 在 Base fork 上系统性失败；tx replay 只是弱 ground truth。 |
| 最大运营风险？ | 误触 live executor / Redis — 目前门控正常，持仓为 0 时才会跑 replay。 |

---

## 11. 对话演进摘要（给 agent 的 context）

1. 用户要求 V4 离线研究 Phase 1–6，**不动 V3 live**，RPC ≤20k  
2. 完成 14d listener、hook 分类、模板库  
3. Fork-replay 三路尝试：UR 失败 → PoolManager roundtrip settle bug → **改用 tx replay**  
4. 用户强调 **省 RPC**；去掉 selftest 里 2000 块 getLogs，改 swap index 缓存  
5. tx-only batch 24 池，91.7% 成功；Phase 4–5 更新  
6. 用户要求交接文档 → 本文件  

**Transcript**（完整对话）：`/root/.cursor/projects/root/agent-transcripts/ea44e6b6-5b7e-4b2d-9faa-91998ab59427/ea44e6b6-5b7e-4b2d-9faa-91998ab59427.jsonl`

---

*文档由 research agent 自动生成，数字以运行时刻文件为准。*
