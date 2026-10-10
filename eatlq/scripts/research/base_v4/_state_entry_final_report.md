# V4 State Entry Final Probe

**Entry rule:** signal_block + 1 (no lookahead)  
**Forward path:** tick mark-to-market entry → +7200 blocks  
**Exit:** hard_stop −50%, trail 25%, time 7200  
**Cost:** gas $0.15/$50 stake + 2% haircut + entry roundtrip friction

## Detection

| | n |
|--|--|
| clean scanned | 112 |
| wave2 confirmed | 4 |

Reasons: `{'wave1_thin': 20, 'too_few_swaps': 63, 'no_wave2_confirmation': 15, 'wave2_confirmed': 4, 'lp_pull_wave1': 4, 'dead_after_wave1': 6}`

## Core table (confirmed + path)

| Entry | n | med MFE | P(MFE>5%) | P(MFE>10%) | P(MFE>25%) | med MAE | med trail | trail winrate |
|-------|---|---------|-----------|------------|------------|---------|-----------|---------------|
| wave2+1 | 4 | 0.0041 | 0.25 | 0.25 | 0.25 | -0.0063 | -0.0051 | 0.25 |

**Executable roundtrip MFE>5%:** 0/4  
**Tick-only MFE>5% (roundtrip still negative):** 1/4

## Verdict

- FINAL: 1/4 show tick MFE>5% but executable roundtrip MFE≤0 — sqrtPrice peaks are NOT capturable; not an exit/timing fix.

## Interpretation

- **MFE=0** → no peak after state entry; exit tuning cannot help.
- **MFE>0 but adj winrate=0** → peak exists but trail/cost/slippage eats it.
- **adj winrate>0** → only then discuss live wiring.
