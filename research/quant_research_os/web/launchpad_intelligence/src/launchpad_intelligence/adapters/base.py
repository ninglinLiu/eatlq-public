from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

from ..models import CanonicalEvent


class AdapterError(ValueError):
    pass


class LaunchpadAdapter(ABC):
    """Uniform launchpad adapter contract; implementations are pure and offline."""

    launchpad_id: str
    chain: str

    @abstractmethod
    def detect_launch(self, raw: dict[str, Any]) -> bool: ...

    @abstractmethod
    def parse_launch(self, raw: dict[str, Any]) -> CanonicalEvent: ...

    @abstractmethod
    def identify_creator(self, raw: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def identify_initial_funder(self, raw: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def parse_primary_trade(self, raw: dict[str, Any]) -> CanonicalEvent: ...

    @abstractmethod
    def reconstruct_curve_state(self, raw: dict[str, Any]) -> dict[str, Decimal]: ...

    @abstractmethod
    def calculate_primary_price(self, raw: dict[str, Any]) -> Decimal: ...

    @abstractmethod
    def parse_graduation(self, raw: dict[str, Any]) -> CanonicalEvent: ...

    @abstractmethod
    def identify_destination_pool(self, raw: dict[str, Any]) -> str | None: ...

    @abstractmethod
    def parse_liquidity_position(self, raw: dict[str, Any]) -> CanonicalEvent: ...

    @abstractmethod
    def parse_fee_flow(self, raw: dict[str, Any]) -> CanonicalEvent: ...

    @abstractmethod
    def identify_fee_recipients(self, raw: dict[str, Any]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def detect_creator_sale(self, raw: dict[str, Any]) -> bool: ...

    @abstractmethod
    def simulate_buy(self, state: dict[str, Any], quote_in: Decimal) -> dict[str, Decimal]: ...

    @abstractmethod
    def simulate_sell(self, state: dict[str, Any], token_in: Decimal) -> dict[str, Decimal]: ...

    @abstractmethod
    def simulate_round_trip(self, state: dict[str, Any], quote_in: Decimal) -> dict[str, Decimal]: ...

    @abstractmethod
    def classify_terminal_state(self, facts: dict[str, Any]) -> str: ...

