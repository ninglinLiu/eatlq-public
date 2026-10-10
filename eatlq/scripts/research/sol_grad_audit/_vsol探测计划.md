# vSOL 探测计划（第一轮不执行 RPC）

## 目标
判断真实 vSOL 轨迹能否找到「溢价更低、毕业率相近或更高、扣除成本后 EV 更好」的子集。

## 停止条件
- vSOL 与 n_trades / sol_in / 溢价 Spearman > 0.85 → 不追加 RPC
- vSOL 子集 EV 不优于现有 n180+alive 规则 → 停止

## 候选子集

- **存活且n180>=100**：n=250 grad=4.80%
- **n60>=45**：n=541 grad=2.77%
- **n60>=35且溢价<=1**：n=498 grad=0.80%

## 预估 RPC
- 每币 1–3 getTransaction（early TradeEvent decode）
- 3 个子集合计 ~400–600 币 → **~800–1800 RPC**（一次性，需委托人批准）

## 提取字段
- vSol, vTok, ts, isBuy, solAmt per trade
- vSol velocity, distance to graduation threshold (~85 SOL real)

## 共线性预检（零 RPC）
用现有 early_buys 的 sol 累计作 vSOL 代理；若代理已无法改善 EV，真实 vSOL 优先级降低。
