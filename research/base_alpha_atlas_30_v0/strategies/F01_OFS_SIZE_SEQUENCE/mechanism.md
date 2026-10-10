# F01_OFS_SIZE_SEQUENCE Mechanism

After a large signed trade, a burst of tiny same-sign trades indicates informed follow-through vs spoof-like fade.

Counterparty: uninformed_retail_and_LP

## Why distinct
Uses ordered size transition, not aggregate buy_pressure_ratio used in older LASZLO features.

## Falsification
permute trade sizes within window keeping signs; edge should vanish
