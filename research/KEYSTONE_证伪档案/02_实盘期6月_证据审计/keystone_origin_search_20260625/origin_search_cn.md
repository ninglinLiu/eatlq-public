# KEYSTONE 第一次发现 — 溯源报告（2026-06-25）

## ★ 一句话结论

**找到了**：KEYSTONE 左尾 pivot 的**原始用户对话**保存在 VPS Cursor transcript  
`/root/.cursor/projects/root/agent-transcripts/4cc3c07d-ffa3-44d8-8575-b5363eefe273/4cc3c07d-ffa3-44d8-8575-b5363eefe273.jsonl`  
**同一会话、2026-06-18 21:16–21:41 (UTC+8)** 内完成「右尾因子路线证伪 → 凸性/剪左尾 pivot → 用户说「开始」→ Agent 创建 `keystone_lefttail.py` 并命名 KEYSTONE」。

**置信度：HIGH**（有完整 user_query 全文 + 同会话 assistant 写码 + 文件 mtime/git 交叉验证）。

**未在 VPS 找到**：委托人记忆中的 **「889 笔」作为 user 口述**——889 首次出现在 **同会话稍后** Agent 对 `_priced_live.json` 全集的统计（过滤前 889 笔 ≈ 抛硬币），不是用户原话。131 / 68% / PF~9 在同会话 **2026-06-18 22:53 前后** 由 `keystone_live.py` holdout 输出。

0622 的 `fb885289` 等会话是 **live 后审计**，不是第一次发现。

---

## 1. 最可信的「用户原始提示词」

### ★★★ 主交付：同一会话内 pivot 链（按时间序）

**Transcript UUID**：`4cc3c07d-ffa3-44d8-8575-b5363eefe273`  
**路径**：`/root/.cursor/projects/root/agent-transcripts/4cc3c07d-ffa3-44d8-8575-b5363eefe273/4cc3c07d-ffa3-44d8-8575-b5363eefe273.jsonl`

---

#### (A) 概念 pivot — 从「单因子 alpha」到「赌 + AI 剪致命风险 + 低摩擦执行」（line 142）

`<timestamp>Thursday, Jun 18, 2026, 9:16 PM (UTC+8)</timestamp>`

```text
我发现了你脑子中的阿尔法就是一个结构性的可套利空间，而且你会用非常少的限制条件，比方说你觉得资金费率多头过于拥挤了，所以你要反向做空，或者 FEE=100 这是庄家容易操盘的标志，所以你要买入做多 但我在想，真实交易员是这样判断的吗？或者最顶尖的量化交易员，他们会只通过单一特征寻找阿尔法吗？我想是不会的。就是人脑，为什么就是说我不需要做所有的预算回测，但是我很坚信，比方说我做的这一单，我认为我做空它就能赚钱，哦，我只是看了一眼 K 线，看了一眼 K 线图，甚至我连这个代币就是所有东西都不了解，我就敢做空它，然后甚至能赚到百分之几十的回报，而大部分交易员都是这样的，你觉得是为什么？两者之间的差异在哪里？我的目标不是像你那样研究市场的宏观结构或者微观结构，而找到一个可套利的点。这个点我们一定找不到。注意啊，就是如果按照这条思路走下去，绝对是必死无疑。因为就是目前的 AI 平权导致所有人都可以做这件事情，更别说大型的量化基金了，就是不管是巴菲特还是什么，Jane straight 就是最高年化可以做到 20% 到百分之六七十，对吧，他们可套利空间，可是他们的量级是我们的成千上万倍，他们能做的我们大概率做不了，必须要找就是我们的优势是什么？说说实话，我们的唯一优势就是赌，就是敢于在不确定性面前大规模的风险暴露，然后利用 AI 帮我们过滤掉一些致命的风险，帮我们在 infra 还有 execution 上 friction降到最低，把本来我人脑能抓到的机会现在 AI 也能抓到了，就是我的目标月化 15% 这不是我的终极目标，我的终极目标可能是月化 100%。为什么做不到？我认为一定能做到，一定有人能做到，不管是幸存者偏差还是什么。如果是幸存者偏差，就找出他为什么成为那个幸存者的原因。难道是纯靠运气吗？纯靠运气，那我们就去研究运气，研究量化运气为什么我们的运气不能复制
```

---

#### (B) 回到白皮书 + 同意「四点机器」（line 144）

`<timestamp>Thursday, Jun 18, 2026, 9:28 PM (UTC+8)</timestamp>`

```text
回看我们这个项目，重新回到白皮书，审视白皮书，然后那个你所说的这这四点我完完全全的同意，我就是要用你帮我做这些事情。回看白皮书，回看白皮书，重新整理我们这一整个项目，重新思路思考一下，就是我们这两天绕得太远了，但是不断的陷入的是同一种类型的失败，也就是我们前面提到的单个可复制的优于随机的，Edge 是必然失败的，你说得很对，保持我们的容量小，速度摩擦最优，存活能力强。是最重要的事情。我们才找了几个信号啊，顶级量化要找几千个信号，为什么我们不能做到
```

（Agent 随后将四点归纳为：**剪致命左尾 / 抢早与摩擦 / 右尾管理 / 凸性仓位**。）

---

#### (C) 炸掉形状门 + 坚持 Base + 明确任务（line 151）

`<timestamp>Thursday, Jun 18, 2026, 9:37 PM (UTC+8)</timestamp>`

```text
首先我就要说了，你说This. Po在两个维度上都死了，这是过去文档在当下市场内得到的结论，而不是现在的结论。市场是瞬息万变的，它能熔断，它也能横盘，它也能爆拉，他不是人死而不可复生，universe是一直存在的，而且base的universe强度是很大的，为什么一定要换成Solana？这是第一个问题。第二个问题是什么叫first signal only PF top one remove必须要大于1.3，这个白皮书里面有吗？凭什么还有boost STARPP five必须要大于one 
这个是有道理的吗？而且Unique>30是什么意思，Top one<15%，凭什么，如果说我们的真正的阿尔法就是top one贡献的，你把top one踢了凭什么，就就比方说我们可能开100单，就是一个彩票的一个阿尔法模式，那你凭什么就把top one踢了，你的这些硬gate可能就是扼杀所有阿尔法最真正的原因。我建议你所有的10条反制气门全部给它炸毁。不存在什么团队，这全部是我一个人，我一个人做的，明白了吗？所以说，不仅是索拉，Na贝斯一样，要做你要做的事情，就如你前面所说，找地基，抢早管出场，凹性仓位，就这么简单，而不是硬加垃圾门槛。你可以在信号选择的时候优化，优化，优化，而不是最后门槛的时候卡我，一种自残的方式把我到窒息
```

---

#### (D) 执行令 — 写左尾地基实验（line 153）

`<timestamp>Thursday, Jun 18, 2026, 9:41 PM (UTC+8)</timestamp>`

```text
开始
```

**紧随其后的 Agent 行为（同 jsonl line 162）**：创建 `scripts/research/base_lefttail/keystone_lefttail.py`，文件头首次出现 **`KEYSTONE`** 代号与「left-tail predictor / convex thesis」。

---

#### (E) 追问 look-ahead + 要求算清现实账（line 168）

`<timestamp>Thursday, Jun 18, 2026, 10:35 PM (UTC+8)</timestamp>`

```text
首先我想知道你是通过什么东西剔除危险的，保留安全的，这是不是一个 look ahead
然后 N 就是样本数量 fatal 率不明白是什么意思，均值是单笔交易的均值吗？然后，这样的交易出现频率大概多少？你估算一下月化大概多少。然后，你所说的这些现实账，也就是一二，必须全部算清楚
```

**Agent 首次报出 holdout 数字（line 167，assistant）**：universe **1275** 池；TEST **510**；AUC **0.86**；剪左尾后均值 **+11.7%（毛）** 等。

---

#### (F) 加厚样本 + QuoterV2 出场 + 倾向当晚 live（line 188）

`<timestamp>Thursday, Jun 18, 2026, 10:53 PM (UTC+8)</timestamp>`

```text
探归档深度、把样本做厚：如果 RPC 能回溯到 2 月，我能把整个 TEST（510 池）真实定价 → 摩擦门子集涨到 ~100+，才能做 TRAIN→holdout 把这个"深度门策略"本身验稳。
加出场管理：现在是死扛 4h，右尾（+183%/+315%）没吃干净。用 tick 路径定出场时点、再用 QuoterV2 在该块真实卖出，测"骑涨+移动止盈"。这两个全做 如果可以的话 我们今晚就上实盘
```

**Agent 随后（line 192–198）**：写 `keystone_live.py`；holdout 报 **131 笔 / 胜率 68% / 致命率 3.8% / 中位 +30.5%**；并固化到 `docs/keystone-go-live.md`。

---

### 同会话更早背景（右尾证伪，非 KEYSTONE 命名，但同链）

同 transcript **line 1**（2026-06-18 16:18 UTC+8）用户问月化 15% 与失败是否真失败；当天下午 Agent 用 QuoterV2 闸刀证伪下影线/右尾彩票（line 52 等），与 **2026-06-13 Sprint097**（`mvp/audits/sprint097_expanded_realcost/verdict_cn.md`）右尾负 EV 结论一致。

---

## 2. 时间线证据

| 时间 (UTC+8) | 证据类型 | 路径 | 摘要 |
|---|---|---|---|
| 2026-06-13 | audit | `mvp/audits/sprint097_expanded_realcost/verdict_cn.md` | Base 右尾 onset 326 池真实成本 **负 EV**；「鱼塘有鱼但咬控盘者钩」 |
| 2026-06-16 | audit | `mvp/audits/sprint098_pivot_feasibility/feasibility_cn.md` | 主线 pivot 侦察 → 推荐 Solana（**非 KEYSTONE**） |
| 2026-06-18 16:18+ | **user transcript** | `…/4cc3c07d….jsonl` L1+ | 月化 15% 复盘 + 当日多战线右尾/ froth 证伪 |
| 2026-06-18 21:16 | **user transcript** | 同上 L142 | **凸性/赌/AI 剪致命风险** 宣言 |
| 2026-06-18 21:28–21:41 | **user transcript** | 同上 L144,L151,L153 | 白皮书四点 + 炸门 + **「开始」** |
| 2026-06-18 21:41+ | code | `keystone_lefttail.py` | Agent 创建并命名 **KEYSTONE** |
| 2026-06-18 22:35+ | assistant | 同上 L167 | 1275/510, AUC 0.86, 拱心石 tick 结果 |
| 2026-06-18 22:53+ | **user + code** | L188 + `keystone_live.py` | 全 universe QuoterV2 + 出场管理 |
| 2026-06-18 ~23:xx | assistant | 同上 L198 | **131 holdout / 68% WR / +59.7% mean** |
| 2026-06-19 06:03 | mtime | `keystone_lefttail.py` | 文件落盘时间 |
| 2026-06-19 16:01 | git | commit `90077ef` | 最早含 KEYSTONE 文件的 commit（live-canary hardening 大包） |
| 2026-06-20 04:08 | ops | signal-bridge（用户已知） | KEYSTONE live 部署窗口 |
| 2026-06-22+ | transcript | `fb885289…` | **0622 验证/审计**，非第一次发现 |

---

## 3. 第一次出现关键数字的位置

| 数字 | 最早位置 | 说明 |
|---|---|---|
| **1275** | transcript L167；`_pools.tsv` | 左尾 ML universe（2025-10→2026-06） |
| **510** | transcript L167 | holdout TEST 池数（时间切分） |
| **322** | transcript L198；`docs/keystone-go-live.md` | holdout 仅深度门（fric≥−5%） |
| **131** | transcript L198 (2026-06-18)；`keystone-go-live.md` | 深度门 + ML 左尾 holdout 笔数 |
| **68% 胜率** | 同上 | holdout 胜率 |
| **PF ~9** | 同会话稍后 Agent 分析 `_priced_live.json`（见 `/root/EATLQ_三对话内容汇编.md` §776：**PF 9.34**）；非 line 198 表格原文 | 131 笔过滤后 profit factor |
| **889** | Agent 对 `_priced_live.json` **过滤前全集**（汇编 md L733/L776）；VPS 上 `_priced_live.json` 现 **1275** 键 — **889 为当时子集/口径，未在 user_query 出现** | 「抛硬币」基线：WR 43%, PF 1.68, 22.5% silent rug |
| **217** | 同会话 walk-forward 扩展（后文）；`keystone_full_validation.py` | WF 池化样本，非首次 131 发现时刻 |
| **AUC 0.86** | transcript L167 | 左尾 ML holdout |

---

## 4. KEYSTONE 命名来源

| 次序 | 位置 | 证据 |
|---|---|---|
| **1** | `scripts/research/base_lefttail/keystone_lefttail.py` docstring L2 | `"""KEYSTONE — left-tail (death/collapse) predictor…` |
| **2** | 同上会话 Agent 中文汇报 | 称「**拱心石**（剪左尾）」 |
| **3** | `keystone_realcost.py` / `keystone_live.py` | 同前缀 KEYSTONE |
| **4** | `docs/keystone-go-live.md` | 2026-06-18 策略固化文档 |
| **5** | git `90077ef` 2026-06-19 | 首次进入版本库 |

**用户从未在 user_query 中打字 "KEYSTONE"**；代号由 Agent 在创建 `keystone_lefttail.py` 时写入（中文「拱心石」与用户「地基」表述对应）。

---

## 5. 未找到项与原因

| 项 | 状态 | 原因 |
|---|---|---|
| 2026-06-15~17 独立 KEYSTONE 对话 | **NOT FOUND** | VPS 上 Cursor transcript 该窗口无 KEYSTONE/左尾 user 消息；仅有 6/16 sprint098 Solana pivot audit |
| 用户口述「889 笔抛硬币」原文 | **NOT FOUND** | 889 仅出现在 Agent 事后统计；用户记忆可能混入了汇编/0622 叙述 |
| `/opt/EATLQ/project/` 镜像 | **NOT FOUND** | 目录不存在 |
| 6/13 当天 Cursor jsonl | **NOT FOUND** | 最早 opt-EATLQ transcript mtime 为 2026-04；6/13 工作可能在他机或 Codex/未同步会话 |
| 单独 Plan 模式 `.plan.md` | **NOT FOUND** | `.cursor/plans/` 仅 whitepaper 相关 |
| git commit message 含用户任务全文 | **NOT FOUND** | 仅 `90077ef` 工程 commit，body 为 live-canary，非研究任务 |

---

## 6. 建议委托人下一步

1. **本机 Cursor** 打开 transcript UUID `4cc3c07d-ffa3-44d8-8575-b5363eefe273`（或导出 jsonl）— 即第一次发现完整链。
2. 若需 **889 口径复现**：读 `_priced_live.json` 与 `build_labeled_universe.py` 注释（1275→644→131 漏斗）；889 可能为早期 priced 子集，需对当时 commit/缓存版本复算 — **当前 VPS 文件已为 1275**。
3. **0622 对话**（`fb885289`、`3e78f369`）作 live 后验证对照，不作「第一次发现」来源。
4. 6/13 右尾证伪原始对话：查 **委托人本机** 2026-06-13 前后 Cursor 导出；VPS 侧以 `sprint097` audit 为间接记录。

---

## 附录 A：执行的命令清单

```bash
# Git
cd /opt/EATLQ && git log --all --oneline -- scripts/research/base_lefttail/keystone_lefttail.py
cd /opt/EATLQ && git show 90077ef --stat

# mtime
find /opt/EATLQ -name '*keystone*' -printf '%T+ %p\n' | sort

# Grep 仓库
rg -i 'KEYSTONE|keystone_lefttail|889|131|左尾|p_fatal' /opt/EATLQ/mvp /opt/EATLQ/scripts/research/base_lefttail /opt/EATLQ/docs

# Transcript 发现
find /root/.cursor/projects -name '*.jsonl' | head -100
python3 # 解析 4cc3c07d jsonl 全部 user 消息 + 按 Jun14-21 关键词过滤

# Audits
ls -lt /opt/EATLQ/mvp/audits/ | head -40
```

---

## 附录 B：关键文件路径列表

| 类型 | 路径 |
|---|---|
| **主 transcript** | `/root/.cursor/projects/root/agent-transcripts/4cc3c07d-ffa3-44d8-8575-b5363eefe273/4cc3c07d-ffa3-44d8-8575-b5363eefe273.jsonl` |
| 左尾 ML 原型 | `/opt/EATLQ/scripts/research/base_lefttail/keystone_lefttail.py` |
| QuoterV2 真实成本 | `/opt/EATLQ/scripts/research/base_lefttail/keystone_realcost.py` |
| 深度门+出场 holdout | `/opt/EATLQ/scripts/research/base_lefttail/keystone_live.py` |
| 策略固化 | `/opt/EATLQ/docs/keystone-go-live.md` |
| 右尾证伪(6/13) | `/opt/EATLQ/mvp/audits/sprint097_expanded_realcost/verdict_cn.md` |
| 0622 审计(对照) | `/root/.cursor/projects/opt-EATLQ/agent-transcripts/fb885289-1bf1-400b-afd4-474e28a2e627/…` |
| 三对话汇编(889/131 叙述) | `/root/EATLQ_三对话内容汇编.md` |
| 本报告 | `/opt/EATLQ/mvp/audits/keystone_origin_search_20260625/origin_search_cn.md` |

---

## 附录 C：紧随「开始」后的 Agent 结论摘要（脱敏）

- **1275** pools → time split TEST **510**
- Holdout **AUC 0.86**（左尾 fatal 预测）
- 后接 QuoterV2：**322**（仅深度门）→ **131**（深度门+ML），胜率 **68%**，致命率 **3.8%**
- 后文 `_priced_live` 分析：**889** 过滤前 ≈ 抛硬币；**131** 过滤后 **PF 9.34**（Agent 结论，非 user 输入）

---

*报告生成：2026-06-25，只读检索，未改代码/env/实盘配置。*
