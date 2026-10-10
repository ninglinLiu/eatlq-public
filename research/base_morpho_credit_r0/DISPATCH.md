# 给 Codex 的派工单（按顺序粘贴，不要一次丢五个）

工作区：`/root/base_morpho_credit_r0/`  
模式：Goal。一次只执行一份 GOAL。R0 未 PASS 之前不要打开 R1–R4。

---

## 第 1 次会话（必须先做）

把下面整段作为 Codex 的 Goal：

```
执行 /root/base_morpho_credit_r0/GOAL.md

这是 Base Morpho Blue 信贷研究的 R0 仪器轮，不是策略轮。
工作区必须是 /root/base_morpho_credit_r0/。
先读 GOAL.md 全文，再读 README.md 与 NO_LIVE.lock。
不要读、不要执行 GOAL_R1 / R2 / R3 / R4，直到本轮 p0_instrument_report.md 裁决为 PASS。

硬约束按 GOAL §0 执行：禁止实盘、禁止清算、禁止未上架市场、禁止把 4% APY 当 alpha、禁止改 GOAL。
P0 通不过就停，写 FAIL 原因。不要顺手做利率或基差分析。

完成后只留下 GOAL 指定的交付物。异议写 OPEN_QUESTIONS.md。
```

---

## 第 2 次会话（R0 PASS 之后）

```
执行 /root/base_morpho_credit_r0/GOAL_R1_rate_utilization.md

先打开 p0_instrument_report.md。不是 PASS 就拒绝执行。
复用 R0 仪器，不要重写单位换算。
预登记写进 out/r1_preregister.json 之后再算 holdout。
主市场只有 cbBTC/USDC 86%。不要做清算。
```

---

## 第 3 次会话

```
执行 /root/base_morpho_credit_r0/GOAL_R2_oracle_spot.md

先确认 R0 PASS。没有 cbBTC 现货腿就 DATA_INSUFFICIENT 收工，不要用 WETH 冒充。
禁止清算抢跑设计。tick 当现货则主裁决不得优于 HEARTBEAT_ONLY / INCONCLUSIVE。
```

---

## 第 4 次会话

```
执行 /root/base_morpho_credit_r0/GOAL_R3_health_paths.md

研究借款人状态机，不是清算机器人。禁止 LIF 利润、禁止清算执行步骤。
归因用 onBehalf。HF 必须应计后。被清算的仓必须留在分母。
R0 没有事件且 API 也没有仓位，就 DATA_INSUFFICIENT。
```

---

## 第 5 次会话（R1 或 R2 至少有一块可用面板）

```
执行 /root/base_morpho_credit_r0/GOAL_R4_orthogonality_f03.md

只读 /root/base_wildcat_alpha/，不要改 F03，不要重训。
找不到信号表或对齐 n<200 就 UNDERPOWERED / DATA_INSUFFICIENT。
主模型只用 t-1 信贷特征。成本代理 120 bps。不许输出 LIVE_GO。
```

---

验收人按各 GOAL 的 §5 打回。四个后续任务可以在 R0 通过后串行；不要开四个 Codex 同时写同一工作区。
