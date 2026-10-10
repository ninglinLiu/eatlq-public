# Commander Opening + Wave-1 Task Spec (for Codex)

Role: **审查型指挥 Agent**（宪章 `/root/宪章.md`）  
Project: `BASE_ALPHA_ATLAS_30_v0`  
Date UTC: 2026-07-18  
Self-GO: **forbidden**. Live/training/funding: **human only**.

---

## 0. Memory load status

| Required by charter | Local status |
|---|---|
| 宪章 | Loaded (`/root/宪章.md`, also `KEYSTONE_阅读包/03_角色宪章.md`) |
| `quant_golden_lessons_CHA.md` | **MISSING by exact name**; proxy loaded: `KEYSTONE_阅读包/05_黄金铁律清单.md` |
| `cross_project_synthesis_CHA_x_EATLQ.md` | **MISSING on this VPS** — mark incomplete |
| 家底报告 | Loaded (`KEYSTONE_阅读包/15_家底调研报告.md`) |
| Phase 0.5 atlas | Loaded under `/root/base_alpha_atlas_30_v0/reports/` |

Incomplete CHA memory ≠ stop, but any cross-project claim from CHA synthesis is **unverified here**.

---

## 1. Phase 0.5 review (commander, not celebratory)

### What is solid
- 30 cards exist with formulas + param **spaces** (not chosen winners).
- Design orthogonality: no exact mechanism+entry+exit triple collision.
- Heavy gates correctly still false: no prod DB, no big copy, no panel, no backtest.
- C-class parked from Wave1 — correct.
- KEYSTONE-overlap age sleeves flagged high conflict — correct.

### Caveats that kill excitement early
1. **Scaffold ≠ strategy.** `runner.py` still uses synthetic `trigger=1` feature. Zero economic signal implemented. “Phase 0.5 done” ≠ any edge evidence.
2. **Data class honesty is provisional.** `A_OFFLINE_DATA_LIKELY_AVAILABLE` is filename-level guess. Until header/schema probe proves columns, A may demote to B/D.
3. **Wave1 contains hard-to-measure sleeves:**
   - `F10_PLP_HIGHVOL_LOWTREND` needs fee income realization; APR-only would be automatic FAIL.
   - `F05_REG_NEWPOOL_DILUTION` “edge from avoidance” needs a carefully pre-registered counterfactual basket; easy to artifact.
4. **Execution realism (宪章铁律):** holding 3–12 **minutes** is OK as *hold horizon*. Main fill model must be **next real swap / +1 block style seconds**, not mid-price or “assume fill at signal bar close.” Minute delay as *execution latency* = 打回.
5. **Missing≠−100%.** Unresolved exits stay unresolved until evidence hierarchy exhausted (GL1-13 spirit).
6. **Live conflict:** even Wave1 shares Base V3 WETH venues with KEYSTONE. Research OK; co-deploy on same wallet/nonce = REJECT_LIVE_CONFLICT regardless of EV.

### Verdict on Phase 0.5
`CONDITIONAL_PASS_DESIGN_SCAFFOLD` — good enough to commission **one** bounded Wave1 prep task to Codex.  
Not pass for edge. Not pass for panel build. Not pass for backtest.

---

## 2. Human decision required (do not proceed without choice)

Pick **one**:

| Option | What happens next | Risk to live |
|---|---|---|
| **A** | Codex: Wave1 **data feasibility probe only** (headers/schema/row estimates on small files; no full copy; no CH business scan) | Lowest |
| **B** | Codex: Implement **real signal_fn** for first Wave1 strategy (`F03_VOL_JUMP_CONTINUE`) on **synthetic+tiny fixture only**; no market data | None to live |
| **C** | Re-run passive host gate; only if pass, authorize **tiny** offline subset panel for Wave1-A strategies | Medium (IO/CPU) |
| **D** | Pause Atlas; commander only reviews KEYSTONE live P&L / pretrade reject pattern | None |
| **E** | Something else (you specify) | ? |

Default recommendation: **A then B**, never C until host gate clean. Commander will not self-start C.

---

## 3. Task Spec for Codex — only if Human selects A

### Title
`ATLAS-W1-A1 Data Feasibility Probe (no backtest)`

### Goal
For Wave1 IDs only, prove whether required columns exist in **already-local small/medium files** without loading 100MB+ wholesale and without production CH business queries.

Wave1:
1. `F03_VOL_JUMP_CONTINUE`
2. `F06_REV_VOL_BEFORE_PRICE`
3. `F05_REG_NEWPOOL_DILUTION`
4. `F01_OFS_SIGN_RUNLEN`
5. `F10_PLP_HIGHVOL_LOWTREND`
6. `F02_LP_POST_MINT_DRIFT`

### Hard laws
- Do not modify `/opt/EATLQ`.
- Do not query ClickHouse business tables.
- Do not Redis write; do not touch secrets.
- Do not copy any single file ≥ 50MB into the atlas tree.
- Do not build shared panel.
- Do not backtest / select parameters / look at PnL.
- nice=19, ionice idle, single process, timeout ≤ 10 minutes total.
- If executor/bridge/container unhealthy → stop and set `PAUSED_LIVE_PRIORITY`.

### Allowed probes
- `ls/stat` sizes
- Read **first ≤200 lines / ≤64KB** of candidate CSV/TSV/JSON for header sniffing
- Parse schema docs under `/opt/EATLQ/docs` and `infra/clickhouse/init/*.sql` **filenames + CREATE column lists only** (SQL files are small)
- Write results only under `/root/base_alpha_atlas_30_v0/reports/` and `docs/inventory/`

### Deliverables
1. `reports/BASE_ALPHA_ATLAS_WAVE1_DATA_FEASIBILITY.md`
2. Per strategy: `strategies/<id>/data_manifest.json` updated with `feasibility = READY|PARTIAL|BLOCKED` and evidence paths
3. Revised class if needed: demote false A→B/D with reason
4. Explicit list: “what tiny export would unblock PARTIAL” (block range + columns + LIMIT) — **do not run export**

### Pre-registered acceptance
- PASS if ≥2 Wave1 strategies are `READY` or `PARTIAL` with concrete next export ≤50MB estimated
- FAIL (task) if probe required forbidden IO — stop
- No strategy may be called alpha

### Estimands / realism notes Codex must echo
- Fill model for any later backtest (not this task): next tradable event after decision, seconds-scale delay primary; minute delay stress-only
- UNKNOWN costs stay UNKNOWN

---

## 4. Task Spec for Codex — only if Human selects B

### Title
`ATLAS-W1-B1 Implement F03_VOL_JUMP_CONTINUE signal on fixtures`

### Goal
Replace synthetic trigger with real formula from preregistration, tested only on `tests/fixtures` synthetic OHLCV JSON (<100KB).

### Hard laws
Same as A; plus `backtest_enabled=false` remains. No reading production files.

### Deliverables
- Updated `strategies/F03_VOL_JUMP_CONTINUE/runner.py` implementing jump_flag/jump_sign
- Fixture bars with known jumps
- unittest proving: jump detected, arm0/arm1 sides, PIT (no future bars)
- No parameter search over real data

### Acceptance
- Tests pass <2s
- No import of clickhouse/web3/redis clients

---

## 5. What commander will do after Codex returns
- Diff-read code and feasibility report line by line
- Reject mid-price fills, missing=−100%, minute execution latency as primary
- Upgrade human only for C (panel) or any export touching prod CH

---

## 6. Stop
Waiting for human option **A/B/C/D/E**. No Codex task auto-dispatched by this document alone.
