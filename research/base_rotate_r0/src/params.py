"""Frozen parameters for the cross-token rotation market-making test.

Every number here was written before any net-return figure was computed.
See ../GOAL.md for the preregistration these implement.
"""

PANEL_START = "2025-08-17"
PANEL_END = "2026-08-17"
ENTRY_START = "2025-09-17"          # 30 days reserved for the signal window

TOTAL_CAP_USD = 5000.0

# --- signal ---
SIGNAL_WIN_BUCKETS = 96             # 24h of 15-minute buckets
VOL_WIN_BUCKETS = 672               # 7d, for the per-pool sigma that sets range width

# --- grid (all cells enumerated in advance) ---
DELTAS = [1, 4, 16, 96, 672]        # scan/hold interval in 15-min buckets
DELTA_NAMES = {1: "15min", 4: "1h", 16: "4h", 96: "24h", 672: "7d"}
KS = [1, 3, 5]                      # number of pools held simultaneously
THETAS = [1.0, 1.5, 2.0]            # switch only if candidate yield > theta * incumbent
CS = [0.5, 1.0]                     # range half-width = c * sigma * sqrt(hold)

# --- gates ---
MAX_SHARE = 0.10                    # position liquidity <= 10% of pool q10 active liquidity
COST_CAP_BPS = 100.0                # point-in-time round-trip cost ceiling at $50
MIN_H = 0.005                       # floor on range half-width (log), 50 bps
MAX_H = 1.50                        # ceiling on range half-width (log)

# --- gas (286 real Base receipts; conservative direction) ---
GAS_SWITCH = 1_620_000              # burn + mint + 2 inventory swaps
GAS_RECENTER = 1_000_000            # burn + mint, same pool
GAS_USD_PER_UNIT = 1.61e-7

WETH = "0x4200000000000000000000000000000000000006"
