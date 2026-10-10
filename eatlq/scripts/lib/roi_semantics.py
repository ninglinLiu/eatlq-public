"""Sprint057S — ROI Semantics Definitions (Task D).

Single source of truth for ROI terminology across all Sprint054–057S scripts.
Prohibits annualized/monthly/daily ROI from historical replay data.

Usage:
    from scripts.lib.roi_semantics import (
        historical_replay_roi, mean_return_per_trade, paper_roi,
        roi_display_label, ROI_FORBIDDEN_PHRASES, assert_no_forbidden_roi,
    )
"""
from __future__ import annotations
from typing import Optional


# ── Forbidden phrases that must NOT appear in readouts ────────────────────────────
ROI_FORBIDDEN_PHRASES = [
    "annualized ROI", "annualized roi",
    "monthly ROI",  "monthly roi",
    "daily ROI",    "daily roi",
    "production ROI", "production roi",
    "live ROI",     "live roi",
    "annualized return", "annual return",
    "APY", "APR",
]


def assert_no_forbidden_roi(text: str, source: str = "") -> None:
    """Raise ValueError if text contains any forbidden ROI phrase."""
    for phrase in ROI_FORBIDDEN_PHRASES:
        if phrase.lower() in text.lower():
            raise ValueError(
                f"Forbidden ROI phrase '{phrase}' found in {source}. "
                f"Use 'historical replay ROI' or 'paper ROI' only."
            )


# ── Core ROI computations ─────────────────────────────────────────────────────────

def historical_replay_roi(pnl_list: list[float], notional: float) -> Optional[float]:
    """Historical replay ROI = sum(pnl) / (n_trades * notional).

    This is the only allowed ROI for historical replay artifacts.
    It is NOT annualized, NOT monthly, NOT daily.
    It represents the aggregate return per unit of simulated notional deployed.

    Formula: sum(net_return_i * notional) / (n_trades * notional)
           = mean(net_return_i)
    """
    if not pnl_list or notional <= 0:
        return None
    n = len(pnl_list)
    total_pnl = sum(pnl_list) * notional
    total_inv  = n * notional
    return round(total_pnl / total_inv, 6)


def mean_return_per_trade(returns: list[float]) -> Optional[float]:
    """Mean net return per accepted simulated trade.

    Use this label instead of 'roi_pct' in all readouts to avoid ambiguity.
    """
    if not returns:
        return None
    return round(sum(returns) / len(returns), 6)


def portfolio_roi_requires_capital() -> str:
    """Reminder string: portfolio ROI cannot be computed from current artifacts.

    Portfolio ROI would require: starting capital, idle time, capital utilization,
    compounding rules, realized withdrawal schedule, gas + MEV costs.
    Current replay artifacts do NOT establish portfolio ROI.
    """
    return (
        "portfolio_roi_unavailable: requires starting capital, idle time, "
        "capital utilization, compounding, and realized live cost model. "
        "Not established by Sprint054-057 replay artifacts."
    )


def paper_roi(labeled_returns: list[float], gas_proxy: float = 0.002) -> Optional[float]:
    """Paper ROI = mean(net_return - gas_proxy) from real-time QuoterV2 quotes.

    Only valid for paper shadow rows where exit_ok=True and net_return is real.
    Label as 'paper ROI' in all outputs, never as annualized.
    """
    if not labeled_returns:
        return None
    net = [r - gas_proxy for r in labeled_returns]
    return round(sum(net) / len(net), 6)


def annualized_roi_unavailable() -> str:
    """Return the canonical 'not available' message for annualized ROI.

    Call this if callers ever request annualized figures.
    """
    return (
        "annualized_roi_unavailable: requires forward daily signal count, "
        "quoteability, idle capital, capital utilization, max concurrent positions, "
        "real gas, slippage, failed tx costs, and realized live exit behavior. "
        "None of these are established by current Sprint054-057 replay artifacts."
    )


# ── Display labels for readout headers ───────────────────────────────────────────

def roi_display_label(context: str) -> str:
    """Return the correct display label for ROI in readout headers.

    Args:
        context: one of 'replay', 'paper', 'horizon_sweep', 'monitor'
    """
    mapping = {
        "replay":        "historical_replay_roi_pct",
        "paper":         "paper_roi_pct",
        "horizon_sweep": "historical_replay_roi_pct",
        "monitor":       "paper_roi_pct",
    }
    return mapping.get(context, "roi_pct_undefined_context")


# ── Machine-readable semantics dict ──────────────────────────────────────────────

SEMANTICS_V2 = {
    "artifact":     "Sprint057S ROI Semantics v2",
    "verdict":      "ROI_MUST_BE_REPLAY_OR_PAPER_ONLY",
    "allowed_phrases": [
        "historical replay ROI",
        "paper ROI",
        "mean return per trade",
        "historical replay roi_pct",
        "paper roi_pct",
    ],
    "forbidden_phrases": ROI_FORBIDDEN_PHRASES,
    "canonical_report_text": (
        "ROI must be stated as 'historical replay ROI' (for backtests) or 'paper ROI' "
        "(for real-time paper shadow). It is NOT annualized, not monthly, not daily, "
        "and not proof of live profitability. See scripts/lib/roi_semantics.py for "
        "authoritative definitions."
    ),
    "replay_assumptions": {
        "notional":           "fixed per simulated trade (typically $10 in Sprint056/057 replay)",
        "compounding":        False,
        "capital_reuse":      "yes, after simulated position close; total_invested = n * notional",
        "same_pool_cooldown": "15 minutes in primary replay",
        "max_concurrent":     3,
        "gas_slippage_proxy": "0.2% round-trip common proxy; does not model MEV or sequencer effects",
        "real_tx_costs":      "not modeled",
        "failed_tx_costs":    "not modeled",
        "quote_failures":     "counted in quoteability but omitted from labeled success set",
    },
    "term_definitions": {
        "historical_replay_roi":       "sum(pnl) / sum(notional) for historical labeled cohort; NOT annualized",
        "mean_return_per_trade":       "mean(net_return) for accepted simulated trades; NOT annualized",
        "portfolio_roi_requires_capital": "unavailable — needs starting capital, idle time, utilization",
        "paper_roi":                   "mean(net_return - gas_proxy) from real-time QuoterV2 quotes; NOT annualized",
        "annualized_roi_unavailable":  "forbidden — requires forward live signal + cost model",
    },
}
