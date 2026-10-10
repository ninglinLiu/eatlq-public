"""v0.2 trade timeline validation (fail-closed)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass
class TimelineLabel:
    intended_entry_block: int
    quote_block: Optional[int]
    quote_transaction_hash: Optional[str]
    quote_log_index: Optional[int]
    quote_eth_call_block: Optional[int]
    actual_entry_block: Optional[int]
    intended_exit_block: int
    exit_quote_block: Optional[int]
    exit_quote_transaction_hash: Optional[str]
    exit_quote_log_index: Optional[int]
    entry_delay_from_target: Optional[int]
    status: str
    status_reason: str
    quote_method: Optional[str] = None
    exit_quote_method: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_timeline(
    *,
    intended_entry: int,
    intended_exit: int,
    max_entry_wait: int,
    entry_quote_block: Optional[int],
    exit_quote_block: Optional[int],
) -> tuple[str, str]:
    if entry_quote_block is None:
        return "NO_TRADE_NO_QUOTE", "no_entry_quote_in_wait_window"
    if entry_quote_block < intended_entry:
        return "INVALID_TIMELINE", "entry_quote_before_intended_entry"
    if entry_quote_block > intended_entry + max_entry_wait:
        return "NO_TRADE_NO_QUOTE", "entry_quote_beyond_max_wait"
    if entry_quote_block > intended_exit:
        return "INVALID_TIMELINE", "entry_after_intended_exit"
    if exit_quote_block is None:
        return "EXIT_QUOTE_UNAVAILABLE", "no_exit_quote_without_forced_zero"
    if exit_quote_block < entry_quote_block:
        return "INVALID_TIMELINE", "exit_before_actual_entry"
    return "EXECUTABLE", "timeline_ok"
