"""Frozen R0 constants. Do not edit after the first signal/PnL run."""
from __future__ import annotations

import math

WETH = "0x4200000000000000000000000000000000000006"
CHAIN_ID = 8453
PRE_SEC = 600
SEARCH_SEC = 3600
HOLD_BLOCKS = 1800
MIN_PRE = 5
DUMP_LOG = math.log(0.80)
RECOVER_LOG = math.log(0.95)
HARD_STOP = math.log(0.70)
TRAIL = 0.25
SPEND_ETH = 0.02
SPEND_WEI = int(SPEND_ETH * 1e18)
FRIC_FLOOR = -0.10
FEE_BPS_OK = (1, 5, 30, 100)
FEE_RAW_OK = (100, 500, 3000, 10000)
FWD_PULL_SEC = PRE_SEC + SEARCH_SEC + 2 * HOLD_BLOCKS * 2  # ~3h+
LN1P = math.log(1.0001)
