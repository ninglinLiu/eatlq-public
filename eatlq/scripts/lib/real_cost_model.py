"""Real-cost model — research only, no live execution.

Aligns paper-shadow / dry-run cost accounting with whitepaper §交易执行:
  - Honeypot 检测 = Quoter V2 eth_call simulate sell
  - Slippage = QuoterV2 amountOut at real notional vs micro-baseline
  - Gas (Base L2) = real per-trade USD cost / notional, NOT a flat 0.2% proxy
  - Sell-path = round-trip simulate (buy -> sell same block snapshot)
  - Liquidity gate = pool liquidity USD must be >= 20x notional for slippage <5%

Designed to be importable from sprint086_* overlays without touching live services.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any

try:
    from scripts.lib.v3_quoterv2_struct import (  # type: ignore
        JsonRpcClient,
        QUOTER_V2_BASE,
        WETH_BASE,
        quote_exact_input_single_struct,
    )
except ImportError:
    from v3_quoterv2_struct import (  # type: ignore
        JsonRpcClient,
        QUOTER_V2_BASE,
        WETH_BASE,
        quote_exact_input_single_struct,
    )

WETH_DECIMALS = 18
DEFAULT_ETH_USD = float(os.environ.get("ETH_USD_NOTIONAL_PROXY", "3000"))

# Whitepaper-aligned defaults (research only). Override via env or constructor.
DEFAULT_MAX_SLIPPAGE_BUY = 0.03          # 3% per whitepaper-v1 §F4
DEFAULT_MAX_SLIPPAGE_SELL = 0.05         # 5% on stop-loss / forced exit
DEFAULT_MAX_HONEYPOT_LOSS = 0.10         # >10% sell vs buy → flag honeypot
DEFAULT_LIQUIDITY_DEPTH_MULT = 20        # liquidity must be >= 20× notional
DEFAULT_BASE_L2_GAS_USD_BUY = 0.05       # ~$0.05/swap at typical Base gas price
DEFAULT_BASE_L2_GAS_USD_SELL = 0.05
DEFAULT_RPC_LATENCY_SEC = 1.5            # observed ingestor→quoter latency

# Notional ladder used for size sensitivity (matches sprint056b spec)
NOTIONAL_LADDER_USD = (10.0, 25.0, 50.0, 100.0)


@dataclass
class CostBreakdown:
    """Per-trade cost decomposition in fractional units of notional."""

    notional_usd: float
    gas_buy_pct: float = 0.0
    gas_sell_pct: float = 0.0
    slippage_buy_pct: float = 0.0
    slippage_sell_pct: float = 0.0
    pool_fee_pct: float = 0.0
    sell_path_ok: bool = True
    sell_revert_reason: str = ""
    rpc_error: bool = False           # transient infra failure (NOT a honeypot)
    honeypot_flag: bool = False
    liquidity_depth_ok: bool = True
    liquidity_depth_ratio: float | None = None
    quoter_in: dict[str, Any] = field(default_factory=dict)
    quoter_out: dict[str, Any] = field(default_factory=dict)

    def round_trip_cost_pct(self) -> float:
        """Total round-trip cost as fraction of notional."""
        return (
            self.gas_buy_pct
            + self.gas_sell_pct
            + self.slippage_buy_pct
            + self.slippage_sell_pct
            + 2.0 * self.pool_fee_pct
        )

    def apply_to_gross_return(self, gross_return: float) -> float:
        """Subtract round-trip cost; apply sell-fail penalty (full loss minus partial).

        RPC failure is NOT counted as honeypot — return NaN-equivalent (None) so
        downstream aggregations can drop it instead of contaminating PF stats.
        """
        if self.rpc_error and not self.honeypot_flag:
            # Treat as missing data, not loss. Downstream should filter.
            return float("nan")
        if not self.sell_path_ok or self.honeypot_flag:
            return -1.0
        return gross_return - self.round_trip_cost_pct()


class RealCostModel:
    """Stateless-ish helper: quote-driven real-cost estimator.

    Caches per-(pool, fee, block) quotes so a single CSV replay
    does not hammer the RPC endpoint.
    """

    def __init__(
        self,
        rpc: JsonRpcClient,
        eth_usd: float = DEFAULT_ETH_USD,
        gas_usd_buy: float = DEFAULT_BASE_L2_GAS_USD_BUY,
        gas_usd_sell: float = DEFAULT_BASE_L2_GAS_USD_SELL,
        max_honeypot_loss: float = DEFAULT_MAX_HONEYPOT_LOSS,
        liquidity_depth_mult: float = DEFAULT_LIQUIDITY_DEPTH_MULT,
        quoter: str = QUOTER_V2_BASE,
        fallback_rpc: JsonRpcClient | None = None,
    ) -> None:
        self.rpc = rpc
        self.fallback_rpc = fallback_rpc
        self.eth_usd = float(eth_usd)
        self.gas_usd_buy = float(gas_usd_buy)
        self.gas_usd_sell = float(gas_usd_sell)
        self.max_honeypot_loss = float(max_honeypot_loss)
        self.liquidity_depth_mult = float(liquidity_depth_mult)
        self.quoter = quoter
        self._cache: dict[tuple, dict[str, Any]] = {}

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def usd_to_weth_wei(usd: float, eth_usd: float) -> int:
        if eth_usd <= 0:
            return 0
        return int(round((usd / eth_usd) * (10**WETH_DECIMALS)))

    @staticmethod
    def fee_bps_to_pct(fee_bps: int | str | None) -> float:
        try:
            return float(fee_bps) / 1_000_000.0
        except (TypeError, ValueError):
            return 0.0

    def _quote(
        self,
        token_in: str,
        token_out: str,
        amount_in_wei: int,
        fee_bps: int,
        block_identifier: int | str | None = None,
    ) -> dict[str, Any]:
        key = (token_in.lower(), token_out.lower(), int(amount_in_wei), int(fee_bps), str(block_identifier))
        if key in self._cache:
            return self._cache[key]
        out = quote_exact_input_single_struct(
            self.rpc,
            token_in,
            token_out,
            int(amount_in_wei),
            int(fee_bps),
            block_identifier=block_identifier,
            quoter=self.quoter,
        )
        if out.get("reason") == "rpc_error" and self.fallback_rpc is not None:
            out2 = quote_exact_input_single_struct(
                self.fallback_rpc,
                token_in,
                token_out,
                int(amount_in_wei),
                int(fee_bps),
                block_identifier=block_identifier,
                quoter=self.quoter,
            )
            if out2.get("ok") or out2.get("reason") != "rpc_error":
                out = out2
        self._cache[key] = out
        return out

    # ── main estimator ─────────────────────────────────────────────────────

    def estimate(
        self,
        token: str,
        fee_bps: int,
        notional_usd: float,
        baseline_amount_out_micro: float | None = None,
        block_identifier: int | str | None = None,
    ) -> CostBreakdown:
        """Estimate full round-trip cost at the given notional.

        Round-trip = WETH -> token (buy) + token -> WETH (sell same snapshot).
        Slippage at notional = (1 - (amount_out_at_notional / pro_rata_amount_out_micro)).
        Sell-path tested by quoting token -> WETH on the bought amount.
        Honeypot flagged if sell amount_out_weth < (1 - max_honeypot_loss) * buy_amount_in_weth.
        """
        breakdown = CostBreakdown(notional_usd=float(notional_usd))
        breakdown.pool_fee_pct = self.fee_bps_to_pct(fee_bps)

        # Real-USD gas cost as fraction of notional
        breakdown.gas_buy_pct = self.gas_usd_buy / max(notional_usd, 1e-9)
        breakdown.gas_sell_pct = self.gas_usd_sell / max(notional_usd, 1e-9)

        weth_in_wei = self.usd_to_weth_wei(notional_usd, self.eth_usd)
        if weth_in_wei <= 0:
            breakdown.sell_path_ok = False
            breakdown.sell_revert_reason = "zero_notional"
            return breakdown

        # 1) Buy quote at requested notional
        buy_q = self._quote(WETH_BASE, token, weth_in_wei, int(fee_bps), block_identifier)
        breakdown.quoter_in = {
            k: buy_q.get(k) for k in ("ok", "amount_out", "reason", "block_used")
        }
        if not buy_q.get("ok"):
            buy_reason = buy_q.get("reason", "")
            breakdown.sell_path_ok = False
            breakdown.sell_revert_reason = f"buy_quote_fail:{buy_reason}"
            if buy_reason == "rpc_error":
                breakdown.rpc_error = True
            return breakdown

        buy_amount_out_tokens = int(buy_q["amount_out"])

        # 2) Slippage at notional vs micro baseline (if provided)
        if baseline_amount_out_micro and baseline_amount_out_micro > 0:
            ratio = (buy_amount_out_tokens / weth_in_wei) / (
                baseline_amount_out_micro / max(self.usd_to_weth_wei(0.15, self.eth_usd), 1)
            )
            # ratio < 1 means we got fewer tokens per WETH at our notional → slippage
            breakdown.slippage_buy_pct = max(0.0, 1.0 - ratio)
        else:
            # Fall back to scaling: use NOTIONAL_LADDER_USD micro=10U baseline if available
            # If no baseline, assume slippage scales linearly past 10U as 0.4% per 10U above 10U
            ladder_base = 10.0
            if notional_usd > ladder_base:
                breakdown.slippage_buy_pct = min(0.10, 0.004 * ((notional_usd - ladder_base) / ladder_base))
            else:
                breakdown.slippage_buy_pct = 0.0

        # 3) Sell quote: token -> WETH at the bought amount, same block snapshot
        sell_q = self._quote(token, WETH_BASE, buy_amount_out_tokens, int(fee_bps), block_identifier)
        breakdown.quoter_out = {
            k: sell_q.get(k) for k in ("ok", "amount_out", "reason", "block_used")
        }
        if not sell_q.get("ok"):
            sell_reason = sell_q.get("reason", "")
            breakdown.sell_path_ok = False
            breakdown.sell_revert_reason = f"sell_quote_fail:{sell_reason}"
            if sell_reason == "rpc_error":
                breakdown.rpc_error = True
            else:
                # genuine quoter revert on sell → likely honeypot/dead pool
                breakdown.honeypot_flag = True
            return breakdown

        sell_amount_out_weth = int(sell_q["amount_out"])
        roundtrip_loss = 1.0 - (sell_amount_out_weth / weth_in_wei) if weth_in_wei else 1.0
        # Sell slippage = round-trip loss minus pool fee (2x) minus buy slippage
        observed_sell_slip = roundtrip_loss - 2.0 * breakdown.pool_fee_pct - breakdown.slippage_buy_pct
        breakdown.slippage_sell_pct = max(0.0, observed_sell_slip)

        if roundtrip_loss > self.max_honeypot_loss:
            breakdown.honeypot_flag = True

        # 4) Liquidity depth heuristic (round-trip loss as inverse liquidity proxy)
        if roundtrip_loss > 0:
            implied_depth_ratio = (2.0 * breakdown.pool_fee_pct) / max(roundtrip_loss, 1e-9)
            breakdown.liquidity_depth_ratio = round(implied_depth_ratio, 4)
            breakdown.liquidity_depth_ok = roundtrip_loss < (1.0 / self.liquidity_depth_mult)
        else:
            breakdown.liquidity_depth_ok = True

        return breakdown


def aggregate_breakdowns(breakdowns: list[CostBreakdown]) -> dict[str, float | int]:
    """Summarise a batch of CostBreakdowns for readout tables."""
    if not breakdowns:
        return {
            "n": 0, "mean_cost_pct": None, "honeypot_rate": None,
            "sell_fail_rate": None, "median_slippage_buy": None,
        }
    n = len(breakdowns)
    costs = [b.round_trip_cost_pct() for b in breakdowns]
    slip = sorted(b.slippage_buy_pct for b in breakdowns)
    return {
        "n": n,
        "mean_cost_pct": round(sum(costs) / n, 6),
        "max_cost_pct": round(max(costs), 6),
        "honeypot_rate": round(sum(1 for b in breakdowns if b.honeypot_flag) / n, 4),
        "sell_fail_rate": round(sum(1 for b in breakdowns if not b.sell_path_ok) / n, 4),
        "median_slippage_buy": round(slip[n // 2], 6),
        "median_slippage_sell": round(sorted(b.slippage_sell_pct for b in breakdowns)[n // 2], 6),
        "liquidity_depth_fail_rate": round(
            sum(1 for b in breakdowns if not b.liquidity_depth_ok) / n, 4
        ),
    }


# Public exports
__all__ = [
    "CostBreakdown",
    "RealCostModel",
    "NOTIONAL_LADDER_USD",
    "DEFAULT_ETH_USD",
    "DEFAULT_BASE_L2_GAS_USD_BUY",
    "DEFAULT_BASE_L2_GAS_USD_SELL",
    "DEFAULT_MAX_HONEYPOT_LOSS",
    "DEFAULT_LIQUIDITY_DEPTH_MULT",
    "aggregate_breakdowns",
]
