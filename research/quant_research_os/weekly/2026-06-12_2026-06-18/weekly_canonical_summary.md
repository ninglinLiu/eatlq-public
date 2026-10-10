# Weekly Canonical Summary — 2026-06-12 to 2026-06-18

## Week arc (11 threads)

| Thread | Period | Canonical anchors |
|--------|--------|-------------------|
| A. 系统体检 / 3月失败审查 | 6/12 | UTW-013, FLW-011, DCW-007, CC-07 |
| B. forced-seller 六层失败 | 6/12–13 | HYW-009, CNW-010, FLW-008, RPW-014 |
| C. 上实盘压力 / P0-P3 | 6/13 | DCW-008, EXW-009, RPW-015 |
| D. 零 onset 纠错 → sprint097 | 6/13 | HYW-010, CNW-009, FLW-007, RPW-013 |
| E. fee=100 / dom_share | 6/13–14 | CNW-012 (UNVALIDATED) |
| F. Solana sprint098–101 | 6/17–18 | EXW-010, HYW-002 |
| G. deployer vs operator | 6/18 | HYW-003, FLW-001, RPW-002 |
| H. 月化 15% 复盘 | 6/18 | UTW-001–005, CNW-002 |
| I. 白皮书 / hard gate | 6/18 | UTW-007–009, CNW-008 |
| J. KEYSTONE | 6/18 | HYW-006, EXW-004–006, CNW-006 |
| K. paper-forward + 25U | 6/18 | DCW-005/006, FLW-006 |

---

## Top 20 long-reusable knowledge

1. **RPW-002** — deployer reputation ≠ operator accumulation
2. **RPW-005** — realized sellable labels, not paper ATH
3. **RPW-004** — ML 剪左尾，不选均值赢家（KEYSTONE）
4. **RPW-007** — TRAIN lock → holdout
5. **RPW-001** — 旧 sprint 结论须 regime 重验
6. **RPW-013** — 管道断流 ≠ 市场枯竭
7. **RPW-014** — forced-seller 信息敏感方叙事已 REJECT
8. **RPW-015** — 体检/审计 ≠ blind live
9. **RPW-003 / UTW-009** — convex 策略慎用 top1-removed gate
10. **CNW-009** — 零 onset 纠错：市场有池，generic EV 仍负
11. **CNW-010** — forced-seller 永久 REJECT
12. **CNW-006** — KEYSTONE holdout +59.7%（backtest only）
13. **CNW-004** — operator accumulation Solana falsified
14. **FLW-001** — 假设 misidentification guardrail
15. **FLW-007** — 查 ingestor 再判市场死亡
16. **FLW-009** — universe/fee 编码 bug 历史教训
17. **FLW-006** — 授权 ≠ 成交确认
18. **UTW-013** — 第一性原理：找 alpha 非 blind 送钱
19. **UTW-008** — 旧文档结论非永恒
20. **MTW-001 / MTW-003** — holdout 协议 + KEYSTONE 方法

---

## Top 10 failure / guardrail

| ID | Guardrail |
|----|-----------|
| FLW-007 | Verify pipeline liveness before "market dead" claims |
| FLW-008 | Dump ≠ dumb money; forced-seller rejected |
| FLW-009 | Cross-check universe/fee encoding before verdict |
| FLW-010 | Narrow optimistic reexam overridden by sprint097 |
| FLW-011 | Clarify user intent: audit vs go-live |
| FLW-001 | operational_definition before citing falsification |
| FLW-002 | Use realized EV labels |
| FLW-003 | Venue-specific mechanics (Solana bonding curve) |
| FLW-004 | No TRAIN-only deployment |
| FLW-006 | execution_confirmed required for live claims |

---

## Supersession / invalidation chain

```text
HYW-010 (zero onset market dead) → INVALIDATED by CNW-009 (pipeline fix)
HYW-012 (meta_reexam optimism) → SUPERSEDED by CNW-011 (sprint097)
HYW-004 (generic mean EV) → SUPERSEDED by HYW-006 pivot (KEYSTONE)
HYW-009 (forced-seller) → INVALIDATED by CNW-010
HYW-003 (operator accum) → INVALIDATED by CNW-004
```

---

## Unresolved conflicts

See `source_conflicts.md` — CFW-01 through CFW-04 preserved, not merged.
