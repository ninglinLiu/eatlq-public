# Phase 5 补充 · 非 Initialize 进场窗（首 swap 后）

> 2026-06-25 · **0 研究计数器 RPC**（336 anvil replays）· 112 clean 池

---

## 问题

Initialize 进场无 alpha（中位 −2.6%，0% 正收益）。**换到 index 内首笔 swap 块进场，是否有右尾？**

---

## 方法

| 项 | 值 |
|---|---|
| 对比 | Initialize 块 vs **first_swap_block**（`/tmp/v4_pool_swap_index.json`） |
| 口径 | 0.003 ETH roundtrip，`ret = weth_net/spend − 1` |
| offsets | 首 swap 后 `[0, 30, 300]` |
| 分层 | 模板（RD-A/B/vanilla/…）；activity（init→swap ≤100 块 = fast） |

命令：

```bash
cd /opt/EATLQ/scripts/research/base_v4
bash run_in_cgroup.sh python3 v4_alpha_readout.py --mode alt-entry
```

---

## 结果（n=112 clean）

| 进场窗 | 正收益 | 中位 PnL | 右尾≥+100% | 致命≤−50% |
|--------|--------|----------|------------|-----------|
| **Initialize** | **0%** | **−2.6%** | 0% | 8.9% |
| **首 swap 后** | **0%** | **−2.6%** | 0% | 8.9% |

- **10/112 池** first_swap 比 Initialize **略好**（+8.9%），但 **无一转正收益**
- **中位 PnL 变化 init→first_swap：0.0**（整体无改善）

### RD-B（n=50）

| 进场窗 | 中位 PnL | 正收益 | 致命 |
|--------|----------|--------|------|
| Initialize | −2.6% | 0% | 0% |
| 首 swap | −2.6% | 0% | 0% |

全 offset 曲线平坦 — **无早进溢价**。

### 快活动 proxy（init→swap ≤100 块，n=9）

正收益 **0%**，中位 **−2.4%** — n 过小，但方向与全样本一致。

### 未匹配 RD（n=26）

Initialize 中位 **−7.8%**（致命 34.6%）→ 首 swap 中位 **−2.4%**（致命 38.5%）：**摩擦略减，仍无正 EV，致命未降**。

---

## 结论

1. **首 swap 进场不能解锁右尾** — 与 Initialize 同为 ~−2.6% 摩擦、0% 正收益。
2. **RD-B 仍只适合当 exit-safe 过滤器**，不是 alpha 来源。
3. **「深池/活动触发」proxy（fast swap）n=9 不足且仍负** — 未观测到右尾。
4. **建议**：V4 alpha 线 **暂降 RPC 档**；若继续，需 **非 roundtrip 网格口径**（如 V3 式 trail exit + 深池 WETH 门槛 + 真实 gas），或 **换 universe**（非 10k index 内池）。

---

## 产出

- `_alpha_alt_entry_cache.json`（112 池 × 336 replays）
- `_alpha_alt_entry_summary.json` / `_alpha_alt_entry_report.json`
- `v4_alpha_readout.py --mode alt-entry`

**仍不能上 live V4。**
