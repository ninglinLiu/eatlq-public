---
task_id: GL5-v3
status: INCONCLUSIVE_STRONGLY_SUPPORTS_EDGE_SURVIVAL
target_object: 2026-06-24_Fon_644_to_120
research_only: true
live_allowed: false
broadcast_allowed: false
go_decision: NOT_MADE
wallet_level_sellability: NOT_ESTABLISHED_POOL_LEVEL_ONLY
---

# GL5v3：F-on 644/120 archive 可实现出场解析

## 裁定候选

**形式裁定仍为 `INCONCLUSIVE`，但 edge 幸存得到强支持，edge 崩塌被当前双边界排除。** 这不是含糊折中，而是两个不同问题有不同识别程度：

- F-on 的**确认池级 rug 为 `0/120`，但严格率界尚未收成点**：+1 为 `[0%,2.50%]`，+5/+15 为 `[0%,0.83%]`。独立审查纠正了一个 post-hoc 强度越界：只见 target 储备高于 5%、但 atomic revert 的腿，按预注册 Rule 5 仍须 rug-unresolved，不能另创 reserve-only 安全分支。唯一一条 target WETH 储备跌逾 95% 的腿仍能 atomic roundtrip 和 exact-size 卖出，所以也不能硬记 rug。
- 完整 120 笔收益尚有 +1/+5/+15 各 7/5/2 条没有满足冻结优先级的历史仓位出场价，不能给假点估计。但 +1 population 双界已经从 GL5v2 的 `[−36.07%, +143.70%]` 收窄到 **`[+136.73%, +147.70%]`**；+5 为 `[+139.61%, +148.81%]`，+15 为 `[+147.72%, +148.96%]`。三个区间全体大于零。
- 完整同龄 644 baseline 仍有 154/156/146 条收益未决，主要因为冻结 GL5v2 数据没有第三方 entry BUY proxy，不能用 archive 新买仓位替代。不过按同一悲观/乐观规则，条件 alpha 的完整双界也全体大于零：+1 `[+86.46pp,+125.21pp]`、+5 `[+93.87pp,+130.44pp]`、+15 `[+116.88pp,+142.24pp]`。

继承的预注册规定任一 primary offset `unresolved>0` 就不得写 `EDGE_SURVIVES`，所以形式标签不越线；但 `EDGE_COLLAPSES` 已不相容于完整 population 下界。`GO=NOT_MADE`。

## 0. 对象与预注册未变

唯一对象仍是 2026-06-24 的 644 labeled pools → 120 F-on pass：247 deployer blacklist、33 skeleton blacklist、0.0302 ETH（固定 $50.09425）仓位、gas $0.15、额外 haircut 2%、legacy −50% hard stop/35% trail/7200-block timeout、offset +1/+5/+15。没有换成 431/116 的零 F `+56%` 对象。

Arm-0 继续逐值 `==`：120 pass、月化 `+8.5743804128%`、单笔均值 `+142.8024144341%`、MDD `−0.3931798477%`、fatal 2/120。旧 QV2 证据仍是 120/120 无守卫 quote，fork lens 是同一 120/120 模拟，原脚本真实 SELL=0。

GL5v3 增量预注册在首个 RPC 前冻结，SHA256 `da489d3f3f4ad8b40ac48849661116264ac1a6e98f5a9835b9cdc60f07882412`；继承 GL5v2 SHA `c3d411bc07509df4e791b2094cb9145cdb43d19626633fdcb3bedb645bef970e`。本轮没有结果后调阈值或换 endpoint。

## 1. 解析方法与证据优先级

每池读取 entry block 的 `WETH.balanceOf(pool)`，每腿固定读取 `target−1,target,target+1,target+5,target+15`；collapse 主阈值为 target/entry `<=0.05`，敏感性为 `<=0.10/0.01`。无 full-size 真卖时，在 target 做 0.0302 ETH atomic buy→sell guard；历史仓位 qty 仍由 GL5v2 的第三方 entry BUY proxy 换算，再做同块 exact-size QuoterV2 卖出。

优先级严格是：full-size canonical SELL → 储备持续坍塌且无正回收 → 储备存活+atomic sell+历史 qty exact quote → unresolved。低储备但 atomic/exact-size 真能回收，按残值而非 −100%；RPC/entry/quote 缺口也不按满额盈利。

这里必须分开两个状态：

1. `rug_adjudication` 回答池到出场点有没有坍塌或仍有可卖路径。
2. `return_resolved` 回答有没有合规的历史仓位出场价。

一个池可以被证明没 rug，但因为缺 entry proxy 或 exact-size quote 而收益仍 unresolved。GL5v2 把这两类未决混在一个 rug upper bound 里；archive 证据使本轮能够把它们拆开。

## 2. Arm-1：完整 120 的结果

| offset | 真 SELL carry | archive alive + atomic | 低储备但 atomic 可卖 | 收益 unresolved | resolved mean | 完整 mean 双界 | Arm-0→Arm-1 delta 界 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| +1 | 31 | 81 | 1 | 7 | +151.54% | **[+136.73%, +147.70%]** | [−6.07pp,+4.90pp] |
| +5 | 34 | 80 | 1 | 5 | +150.13% | **[+139.61%, +148.81%]** | [−3.19pp,+6.01pp] |
| +15 | 38 | 79 | 1 | 2 | +151.95% | **[+147.72%, +148.96%]** | [+4.91pp,+6.15pp] |

这些是平均单笔 return，不是月化。+1 的下界把 7 条未决全部记为 `−102.2994%`；上界才沿用旧 Arm-0 quote。即便采用预注册的最坏替代，完整 120 的均值仍为 +136.73%。因此 archive 结果不支持“旧平均利润大半是无法卖出的报价幻觉”。

未决没有被隐藏：+1 的 7 条包括 archive target 点偶发读取失败、atomic 与 exact quote 证据不同步、或缺 entry proxy；其中 4 条已有严格 pool-level 非坍塌证据，另 3 条连 rug 仍未决。+15 只剩 2 条，其中一条有 atomic sell 但无冻结 entry qty，另一条有正 exact quote 但 atomic guard 未成功；前者 rug 已否认、后者 rug 仍未决，二者都不能为凑收益点估计越级。

## 3. F-on 是否挡 rug：严格识别界

| offset | confirmed collapse | confirmed not collapsed | rug unresolved | pool-level rug rate |
|---:|---:|---:|---:|---:|
| +1 | 0 | 117 | 3 | **[0.00%,2.50%]** |
| +5 | 0 | 119 | 1 | **[0.00%,0.83%]** |
| +15 | 0 | 119 | 1 | **[0.00%,0.83%]** |

敏感性扫描在三个 offset 都找到 1 条 target reserve loss≥90% 且≥95%，loss≥99% 为 0；该腿的 atomic buy、atomic sell 与历史 qty exact quote 都成功，故是 `ARCHIVE_IMPAIRED_BUT_ATOMIC_OK`，不是 rug。这个反例正说明“低储备=必定全损”也会错。

所以，在这份历史 F-on 120 内没有确认 pool-level rug，且上界已从 48.33% 压到 2.50%（主 +1 档）；这是对“F-on ⇒ rug 很低”的强支持，不是 rug 精确为零的证明。它也与 aggressive F-off 曾放入 207 个 rug 的对象形成方向一致的配置对照，但不把两个不同 population 当随机实验。

## 4. 同龄 644 baseline 与条件 alpha

| offset | resolved / 644 | return unresolved | confirmed collapse | rug rate 界 | baseline mean 界 | F-on − baseline alpha 界 |
|---:|---:|---:|---:|---:|---:|---:|
| +1 | 490 | 154 | 24 | [3.73%,6.83%] | [+22.49%,+50.27%] | **[+86.46pp,+125.21pp]** |
| +5 | 488 | 156 | 43 | [6.68%,8.85%] | [+18.38%,+45.74%] | **[+93.87pp,+130.44pp]** |
| +15 | 498 | 146 | 86 | [13.35%,15.84%] | [+6.72%,+30.84%] | **[+116.88pp,+142.24pp]** |

这不再是 GL5v2 的 64–65 条真实 SELL 稀疏子集 alpha；区间用完整 120 与完整 644 的逐行悲观/乐观替代计算。它仍不是单一点，因为 baseline 有 114 条每个 offset 缺冻结 entry proxy，另有少量 RPC/atomic/quote 硬缺口。不得把区间中点宣传成“真正点 alpha”，但区间符号已经识别为正。

baseline 的 pool-level rug 识别明显收紧：+1 为 24 confirmed、600 non-collapse、20 unresolved；+5 为 43/587/14；+15 为 86/542/16。这里也没有把“只有储备尚存但 atomic 未成功”的腿默认成安全或全损。

## 5. 报价幻觉分解

| offset | Arm-0 quote/sim 依赖 | resolved pairs | confirmed quote-washed rug | 正利润 illusion share |
|---:|---:|---:|---:|---:|
| +1 | 100% | 113 | 0 | −3.35% |
| +5 | 100% | 115 | 0 | −4.02% |
| +15 | 100% | 118 | 0 | −3.90% |

负数代表 archive 可实现值在已解决 pair 上略高于旧 quote，不是声称报价能创造负幻觉。关键量级是：确认“报价把 rug 洗白”的腿为 `0/120`；完整收益的最坏界仍强正。当前证据因此反对“F-on headline 大半是报价救济 rug”的解释。

## 6. 右尾裁决

三个 offset 的 resolved-only pooled mean、去 top1 mean、top1 正利润贡献≤25%、所有 powered rolling 30d mean≥−10%、10,000 次 bootstrap terminal p5>100R、MDD p95≤20R 全部通过：

| offset | n | 去 top1 mean | top1 贡献 | powered windows | terminal p5 | MDD p95 | unresolved=0 | 形式状态 |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| +1 | 113 | +131.88% | 14.13% | 4/4 pass | 233.76R | 1.54R | FAIL | INCONCLUSIVE |
| +5 | 115 | +130.78% | 14.03% | 4/4 pass | 235.04R | 1.52R | FAIL | INCONCLUSIVE |
| +15 | 118 | +133.13% | 13.57% | 2/2 pass | 241.48R | 1.43R | FAIL | INCONCLUSIVE |

只有 `unresolved==0` 一项失败。按冻结纪律，不能把“所有数值门都过且双界强正”改写成形式 `PASS`。

## 7. Golden、RPC ledger 与安全

授权 archive endpoint 当前 head 为 48,701,005。共 14,025 个 logical read calls、18,055 个 HTTP attempts；方法实绩只有 `eth_call=14,024` 与 `eth_getBlockByNumber=1`。allowlist 内虽预留 `eth_getBalance/getLogs`，实绩为 0。public fallback=false，send/sign/broadcast=0。

RPC ledger 只保存 method、参数 SHA、响应 SHA、结果状态与 attempt；endpoint 只写 `GL5V3_ARCHIVE_RPC_secret_omitted`。ledger SHA256 为 `2db34b77ab1a604fc7263ce1697c29284056f913738d17bf645b997406c7c6c8`。输出秘密扫描通过。

有一项勘误必须显式保留：计数器记录 18,055 次 HTTP attempt，但 JSONL 实际 17,034 行。客户端的 transport-error 分支遗漏了连续重试中的 1,021 个**中间失败 attempt**；所有 13,668 个成功响应 hash、最终 unresolved 结果和 logical method 计数仍在。缺失行无法事后无歧义还原，因此没有伪造补写，ledger 标为 `complete=false`。这不改变逐腿数值，却违反了“每个 attempt 均落账”的预注册工件要求，严重性由独立审查裁定。

Golden 按冻结 hash 排序选 5：2 条 archive-alive atomic-ok、1 条收益 unresolved、另以 1 alive 与 1 real SELL 补齐。F-on 没有 confirmed collapse，无法凑“2 collapse”，已披露类别 shortfall；没有为满足样本愿望去 baseline 偷换 cohort。

回归：`.venv/bin/pytest -q tests/test_base_gl5v3.py`，`9 passed in 1.60s`。断言覆盖 Arm-0、分支优先级、collapse 等号、atomic revert、双界、rug/收益分离、population 计数、ledger allowlist/SHA、URL 不泄露与 GO。

## 8. 结论边界

本轮对历史 F-on 对象的核心结论是：**确认 pool-level rug 为 0、主档严格上界 2.5%，完整收益与条件 alpha 的诚实双界全为正；archive 证据强烈支持保守 F-on 历史 edge 幸存，而不支持报价把 rug 洗白。** rug 与收益都尚有严格未决，因此不能落章 `EDGE_SURVIVES`，机器裁定保持 `INCONCLUSIVE`。

最重要的剩余缺口没有被 archive 补掉：`balanceOf(pool)` 与 atomic 构造器证明的是**池子层面/模拟地址层面**能卖，不能证明策略钱包不受 token 地址级 blacklist、transfer hook、限额或路径依赖。entry 仍是第三方 BUY proxy；atomic 也不含真实抢跑、MEV 与竞争。要证明“你的策略钱包一定卖得掉”，需要策略钱包级历史证据或受控真钱验证，不能由本轮外推。

全程未下单、签名、广播、改 live/signals/Redis、启停 executor 或自判 GO。最终接受与行动由独立审查 agent 和人类决定。`GO=NOT_MADE`。

## 复现

```bash
cd /opt/EATLQ
sha256sum -c mvp/diagnostics/base_gl5v3_preregistration_20260716.sha256
python3 scripts/research/base_gl5v3/gl5v3_archive_resolve.py
python3 scripts/research/base_gl5v3/gl5v3_finalize.py
.venv/bin/pytest -q tests/test_base_gl5v3.py
```

复现需要任务书中的同一授权 archive endpoint；程序从任务书内存解析，不在命令行或 artifact 展开 URL。离线审核可直接用逐腿 JSON、RPC ledger 哈希和回归测试，无需重发 RPC。
