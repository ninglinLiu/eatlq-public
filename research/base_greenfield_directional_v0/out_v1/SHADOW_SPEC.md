# Shadow Spec (Prospective — zero capital)

## Start
From current Base tip block at recorder start.

## Policies (simultaneous, no live orders)

1. Primary: `P1_G2_COMPETING`
2. Secondaries: ['P0_C3_MOMENTUM', 'P2_C3_G2_VETO']

## Rules

- Every decision row is written to disk **before** outcome observation.
- Each decision file is SHA256-hashed; hash chain appended to `out_v1/shadow/ledger.jsonl`.
- No threshold / model / universe changes based on live PnL.
- No true money execution; dry-run only.

## Recorder entrypoint (not started by this validation run)

`src/v1/shadow_recorder.py` (to be launched separately under human approval).
