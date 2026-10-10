# Clanker Alpha Holdout Results v0.1

```text
verdict = EDGE_NOT_SUPPORTED
primary_key = 3b|30m|base|250
holdout_n = 48
```

## Primary (3b / 30m / base / $250)

### Buy all
n=48 cov=1.00 mean=-5.6436 med=-0.0023 win=0.000 p10=-3.9729 p90=-0.0023 maxLoss=-250.0046 PF=0.0 CI=[-16.32685367710601, -0.18704193672289726]

### H1 breadth vs concentration
n=0
without top1: n=0
without top3: n=0

### H2 repeat creator
n=41 cov=0.85 mean=-0.2186 med=-0.0023 win=0.000 p10=-0.0023 p90=-0.0023 maxLoss=-4.4589 PF=0.0 CI=[-0.6500300241651684, -0.0023022999999999997]
without top1: n=40 cov=1.00 mean=-0.2240 med=-0.0023 win=0.000 p10=-0.0023 p90=-0.0023 maxLoss=-4.4589 PF=0.0 CI=[-0.557092097635258, -0.0023022999999999997]
without top3: n=38 cov=1.00 mean=-0.2357 med=-0.0023 win=0.000 p10=-0.0023 p90=-0.0023 maxLoss=-4.4589 PF=0.0 CI=[-0.5862915606686926, -0.0023022999999999997]

### H3 early momentum
n=0
without top1: n=0
without top3: n=0

## Discovery (for comparison only; not for verdict)

### Buy all
n=72 cov=1.00 mean=-0.4601 med=-0.0023 win=0.014 p10=-2.8552 p90=-0.0023 maxLoss=-4.2438 PF=0.01655785139948969 CI=[-0.7404002718608264, -0.18437274163861772]
### H1 n=0
### H2 n=62 cov=0.86 mean=-0.1681 med=-0.0023 win=0.016 p10=-0.0023 p90=-0.0023 maxLoss=-4.2438 PF=0.05079901769754425 CI=[-0.38596521380197907, 0.006731190183529675]
### H3 n=0

## Sensitivity (holdout, exit=30m)

See machine-readable `evidence/clanker_alpha_v01/cohort_results.json` → `holdout_sensitivity_30m`.
