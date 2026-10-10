# Base Morpho Credit R0–R4

只读研究。最高状态是 `SHADOW_CANDIDATE`。本目录所有入口必须检查 `NO_LIVE.lock`。

## Codex 执行顺序（不可并行抢跑）

1. `GOAL.md` — R0 仪器。不过门，后面全部作废。
2. `GOAL_R1_rate_utilization.md` — 利率 / 利用率拐点。
3. `GOAL_R2_oracle_spot.md` — 预言机价 vs 现货。
4. `GOAL_R3_health_paths.md` — 健康度路径（回避，不是清算赛跑）。
5. `GOAL_R4_orthogonality_f03.md` — 与 F03 是否正交。

R1 / R2 / R3 在 R0 通过后可以串行；R4 必须等 R1 或 R2 至少交出一块可用面板。

不要修改任何 `GOAL*.md`。异议写进 `OPEN_QUESTIONS.md`。
