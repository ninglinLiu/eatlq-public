"""Frozen parameters for the stop-fill measurement. See ../GOAL.md."""

WETH = "0x4200000000000000000000000000000000000006"

# $50 at the panel's median ETH price of 1,865. GOAL.md section 4.
SPEND_ETH = 0.0268
SPEND_WEI = int(round(SPEND_ETH * 1e18))

PANEL_START, PANEL_END = "2025-08-17", "2026-08-17"

# GOAL.md section 2: the pools a +200% strategy would trade.
TARGET_NET_BPS = 20000
GAS_BPS = 9.7

# GOAL.md section 3: episode definition.
PEAK_LOOKBACK_H = 24
STOP_WINDOW_H = 24
STOP_DROP = 0.70          # -30% from the peak close
MAX_EPISODES = 200

# GOAL.md section 6: verdict thresholds.
FILL_OK_SELL_RATE = 0.95
FILL_OK_MED_STOP = -0.35
FILL_BAD_SELL_RATE = 0.80
FILL_BAD_MED_STOP = -0.60
