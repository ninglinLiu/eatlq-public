# F 集群黑名单 — 字节码骨架误杀验证（2026-06-24）

状态：只读挖掘 + 验证；据此对 live F 黑名单做了一处修正（PARADAENGI 改 deployer-only），见末尾。SYSTEM_PANIC 状态不受影响。

---

## 背景

用户问：能否回测找出所有 rug「惯犯」，把它们的字节码骨架全部种入 F 黑名单，提前拦截未来克隆？

F 黑名单的 `bytecode` 键 = **opcode-skeleton hash**（剥离 CBOR 元数据 + 屏蔽所有 PUSH 操作数），设计意图是「同源克隆即便常量不同也命中同一 hash」。

---

## 挖掘结果（262 个历史 rug token，archive `eth_getCode` + 骨架归一化）

| 指标 | 数值 |
|---|---|
| rug token（delayed_rug，全部有 code，无自毁） | 262 |
| 不同骨架 | 67 |
| 「惯犯簇」（≥2 个 rug 共享骨架） | 39 |
| 属于某个重复簇的 rug | 234 / 262 = **89%** |
| 最大簇 | 同一骨架印了 **30** 个 rug |

→ **结论 1：rug 高度模板化，89% 是克隆。**「找出惯犯」在数据上成立。

---

## ★ 误杀验证（关键，阻止了一次灾难性误种）

把 378 个 clean 赢家（fork-replay 标注为全程可卖）的骨架也算出来，与 67 个 rug 骨架求交集：

| 指标 | 数值 |
|---|---|
| clean 赢家 token | 378 |
| clean 骨架 | 73 |
| **rug ∩ clean 重叠骨架（危险）** | **34 / 67** |
| 若把 67 个全种 → 会误杀的 clean 赢家 | **291** |
| **真正安全（仅 rug、不碰 clean）骨架** | **33 / 67** |
| 33 个安全骨架覆盖的 rug | **100 / 262 = 38.2%** |

### 最刺眼的重叠

```
0xa7df50c8…  rug_n=1   clean_n=19    ← PARADAENGI 自己的骨架！
0xd4e7eae4…  rug_n=12  clean_n=186   ← 超通用模板（疑似标准 ERC20）
0xc0da9a5b…  rug_n=30  clean_n=6     ← 最大 rug 簇也碰 clean
0x1a4b652a…  rug_n=4   clean_n=15
0x7698b0f4…  rug_n=10  clean_n=12
```

→ **结论 2：opcode-skeleton 作为克隆信号太粗。** 屏蔽所有 PUSH 操作数后，同编译器/同标准模板（OpenZeppelin ERC20 等）的币骨架全相同，rug 与正常币共用。「89% 覆盖」具有误导性——大部分覆盖来自会误杀正常币的骨架。**零误杀可种的只有 38.2%。**

---

## ★ 现役 live 隐患（已据此修正）

`seed_paradaengi()` 把 PARADAENGI 骨架 `0xa7df50c84afb0036…` 种进了 live `executor:blacklist:bytecode`。  
本验证发现该骨架与 **19 个 clean 赢家** 撞 → live F 闸可能正在**误杀约 5%（19/378）的正常 memecoin**。

之前实现报告只验证过「不撞 WETH/USDC 蓝筹」，**未验证撞不撞真实 memecoin 赢家**——而它撞得很厉害。根因同上：骨架归一化过激。

### 修正（用户决定：switch_deployer）
- 从 live Redis `SREM` 掉 PARADAENGI 骨架 + 删 meta。
- `seed_paradaengi()` 改为**只种 2 个 deployer EOA**，不再种骨架。
- rebuild + redeploy 使之持久。

### 修正的代价（诚实标注）
- 已知的第 2 个 PARADAENGI（EOA 已知）→ deployer 黑名单仍能挡。
- **全新第 3 个 EOA 的 PARADAENGI 克隆 → deployer 挡不住、骨架也不再种 → 漏**。
- 即：换 deployer-only 后，PARADAENGI 克隆保护**变弱**，换来「不误杀 5% 正常币」。这是用户在 alpha vs 安全间做的取舍。

---

## 对「能否全拉黑惯犯」的最终回答

- **能找出**模板克隆（89% rug 是克隆）。
- **不能全拉黑**——34/67 模板正常币也在用，拉黑会误杀 291 个赢家。
- **零误杀可种的只有 33 个骨架，覆盖 38.2% rug**（已存档，待用户决定是否种）。
- 骨架 hash 本身是个**脆弱信号**，需要更细的克隆判据。

---

## 后续（建议，未实施）

1. **更细的克隆信号**替代 opcode-skeleton：如 opcode n-gram / 模糊哈希 / 函数选择子集合相似度——在「捕获真克隆」与「不撞标准模板」之间取平衡。先在 644 标注集上测「rug 召回 vs clean 误杀」曲线。
2. **LLM 源码/反编译定量分析（用户提议）**：对**已验证合约**拉源码、对**闭源**反编译，用便宜 LLM 标记 mint/blacklist/setTrading/高税/anti-sell 等合约级蜜罐特征。
   - 价值：比脆弱的骨架更准的**合约级蜜罐**信号，适合接替 skeleton 闸。
   - 边界：**抓不到 LP 撤池**（99% 主导威胁，无恶意源码）；依赖源码可得/反编译质量；闭源仍盲。
3. 若要种 33 个安全骨架：覆盖 38.2% rug、样本内 0 误杀，可作为低风险增量。

---

## changed files / commands（本轮）
- 只读挖掘：`scripts/research/base_lefttail/` 下临时脚本；产物 `_rug_skeleton_seed.json` / `_rug_seed_safety.json` / `_rug_seed_validated.json`
- live 修正：`services/executor/src/cluster_blacklist.py`（`seed_paradaengi` 改 deployer-only）+ Redis `SREM executor:blacklist:bytecode <para_skel>` + rebuild/redeploy
- 数据源：`_labeled_universe.json`（262 rug）、`_uni_shards/_uni_s{0..3}.json`（378 clean + 262 rug 标注）、archive `eth_getCode`
