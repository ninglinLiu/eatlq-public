from __future__ import annotations

from dataclasses import fields
from decimal import Decimal, InvalidOperation
from typing import Any

from ..models import CanonicalEvent, Confidence, EventContext, ValidationError
from ..registry import Registry
from .base import AdapterError, LaunchpadAdapter


def _decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise AdapterError(f"invalid decimal {name}") from exc
    if parsed < 0:
        raise AdapterError(f"{name} must be non-negative")
    return parsed


class EVMAdapter(LaunchpadAdapter):
    launchpad_id = "generic_evm"
    chain = "unknown"
    launch_model = "constant_product_fixture"

    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def _context(self, raw: dict[str, Any]) -> EventContext:
        keys = {item.name for item in fields(EventContext)}
        values = {key: raw.get(key) for key in keys}
        values["launchpad_id"] = raw.get("launchpad_id", self.launchpad_id)
        values["pool"] = raw.get("pool")
        values["label_time"] = raw.get("label_time")
        context = EventContext(**values)
        context.validate()
        if context.chain != self.chain or context.launchpad_id != self.launchpad_id:
            raise AdapterError("event chain/launchpad does not match adapter")
        return context

    def _event(self, raw: dict[str, Any], event_type: str) -> CanonicalEvent:
        event = CanonicalEvent(
            context=self._context(raw),
            event_type=event_type,
            payload=dict(raw.get("payload", {})),
        )
        event.validate()
        return event

    def detect_launch(self, raw: dict[str, Any]) -> bool:
        if raw.get("operation") not in {"CREATE", "CREATE2", "FACTORY_EVENT"}:
            return False
        block = int(raw.get("block_number_or_slot", -1))
        factory = str(raw.get("factory", ""))
        version = self.registry.match_factory(self.chain, factory, block) if factory else None
        if version and version.launchpad_id == self.launchpad_id:
            return True
        fingerprint = raw.get("bytecode_hash")
        version = self.registry.match_fingerprint(self.chain, fingerprint) if fingerprint else None
        return bool(version and version.launchpad_id == self.launchpad_id)

    def parse_launch(self, raw: dict[str, Any]) -> CanonicalEvent:
        if not self.detect_launch(raw):
            raise AdapterError("launch is not attributable to this adapter")
        return self._event(raw, "TOKEN_CREATED")

    def identify_creator(self, raw: dict[str, Any]) -> dict[str, Any]:
        evidence = raw.get("creator_event") or raw.get("metadata_signer")
        if evidence:
            return {"address": evidence, "confidence": Confidence.HARD, "basis": "protocol_event_or_metadata"}
        candidate = raw.get("transaction_sender")
        return {
            "address": candidate,
            "confidence": Confidence.PROBABILISTIC if candidate else Confidence.UNRESOLVED,
            "basis": "transaction_sender_candidate_only" if candidate else "missing_evidence",
        }

    def identify_initial_funder(self, raw: dict[str, Any]) -> dict[str, Any]:
        transfers = raw.get("native_or_quote_transfers", [])
        deployer = raw.get("deployer")
        candidates = [row for row in transfers if row.get("to") == deployer and _decimal(row.get("amount", 0), "amount") > 0]
        if not candidates:
            return {"address": None, "confidence": Confidence.UNRESOLVED, "basis": "no_predeployment_funding_trace"}
        earliest = min(candidates, key=lambda row: (row.get("block", 0), row.get("index", 0)))
        return {"address": earliest.get("from"), "confidence": Confidence.HARD, "basis": "predeployment_value_trace"}

    def parse_primary_trade(self, raw: dict[str, Any]) -> CanonicalEvent:
        side = raw.get("payload", {}).get("side")
        if side not in {"BUY", "SELL"}:
            raise AdapterError("primary trade side must be BUY or SELL")
        return self._event(raw, f"PRIMARY_{side}")

    def reconstruct_curve_state(self, raw: dict[str, Any]) -> dict[str, Decimal]:
        state = raw.get("curve_state", raw)
        return {
            "quote_reserve": _decimal(state.get("quote_reserve"), "quote_reserve"),
            "token_reserve": _decimal(state.get("token_reserve"), "token_reserve"),
            "fee_rate": _decimal(state.get("fee_rate", 0), "fee_rate"),
        }

    def calculate_primary_price(self, raw: dict[str, Any]) -> Decimal:
        state = self.reconstruct_curve_state(raw)
        if state["token_reserve"] == 0:
            raise AdapterError("token reserve is zero")
        return state["quote_reserve"] / state["token_reserve"]

    def parse_graduation(self, raw: dict[str, Any]) -> CanonicalEvent:
        if not raw.get("payload", {}).get("threshold_reached"):
            raise AdapterError("graduation threshold evidence missing")
        return self._event(raw, "GRADUATED")

    def identify_destination_pool(self, raw: dict[str, Any]) -> str | None:
        payload = raw.get("payload", {})
        return payload.get("pool_created_event") or payload.get("migration_destination")

    def parse_liquidity_position(self, raw: dict[str, Any]) -> CanonicalEvent:
        payload = raw.get("payload", {})
        if not (payload.get("lp_token_id") or payload.get("lp_owner") or payload.get("liquidity")):
            raise AdapterError("liquidity position evidence missing")
        return self._event(raw, "LIQUIDITY_POSITION")

    def parse_fee_flow(self, raw: dict[str, Any]) -> CanonicalEvent:
        recipients = self.identify_fee_recipients(raw)
        if not recipients:
            raise AdapterError("fee transfer evidence missing")
        event = self._event(raw, "FEE_FLOW")
        event.payload["recipients"] = recipients
        return event

    def identify_fee_recipients(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        recipients = []
        for row in raw.get("payload", {}).get("fee_transfers", []):
            amount = _decimal(row.get("amount", 0), "fee amount")
            if amount > 0 and row.get("to"):
                recipients.append({"address": row["to"], "amount": str(amount), "role": row.get("role", "UNKNOWN")})
        return recipients

    def detect_creator_sale(self, raw: dict[str, Any]) -> bool:
        creator = self.identify_creator(raw).get("address")
        seller = raw.get("payload", {}).get("seller")
        return bool(creator and seller and creator.lower() == seller.lower())

    def simulate_buy(self, state: dict[str, Any], quote_in: Decimal) -> dict[str, Decimal]:
        curve = self.reconstruct_curve_state(state)
        amount_in = _decimal(quote_in, "quote_in")
        fee = amount_in * curve["fee_rate"]
        net = amount_in - fee
        k = curve["quote_reserve"] * curve["token_reserve"]
        new_quote = curve["quote_reserve"] + net
        if new_quote == 0:
            raise AdapterError("empty curve")
        token_out = curve["token_reserve"] - (k / new_quote)
        return {"token_out": token_out, "fee": fee, "new_quote_reserve": new_quote, "new_token_reserve": curve["token_reserve"] - token_out}

    def simulate_sell(self, state: dict[str, Any], token_in: Decimal) -> dict[str, Decimal]:
        curve = self.reconstruct_curve_state(state)
        amount_in = _decimal(token_in, "token_in")
        k = curve["quote_reserve"] * curve["token_reserve"]
        new_token = curve["token_reserve"] + amount_in
        gross = curve["quote_reserve"] - (k / new_token)
        fee = gross * curve["fee_rate"]
        return {"quote_out": gross - fee, "fee": fee, "new_quote_reserve": curve["quote_reserve"] - gross, "new_token_reserve": new_token}

    def simulate_round_trip(self, state: dict[str, Any], quote_in: Decimal) -> dict[str, Decimal]:
        buy = self.simulate_buy(state, quote_in)
        after_buy = {
            "quote_reserve": buy["new_quote_reserve"],
            "token_reserve": buy["new_token_reserve"],
            "fee_rate": state.get("fee_rate", state.get("curve_state", {}).get("fee_rate", 0)),
        }
        sell = self.simulate_sell(after_buy, buy["token_out"])
        return {
            "quote_in": _decimal(quote_in, "quote_in"),
            "quote_out": sell["quote_out"],
            "total_fee": buy["fee"] + sell["fee"],
            "net_return": sell["quote_out"] / _decimal(quote_in, "quote_in") - Decimal(1),
        }

    def classify_terminal_state(self, facts: dict[str, Any]) -> str:
        ordered = (
            ("lp_removed", "LP_RUGGED"), ("mint_attack", "DEAD"),
            ("sell_blocked", "SELL_BLOCKED"), ("bundle_exit", "BUNDLED_EXIT"),
            ("creator_dump", "CREATOR_DUMPED"), ("fee_extraction_death", "FEE_EXTRACTION_DEATH"),
            ("migration_failed", "MIGRATION_FAILED"), ("abandoned", "ABANDONED"),
            ("dead", "DEAD"),
        )
        for flag, state in ordered:
            if facts.get(flag):
                return state
        return "UNKNOWN_TERMINAL"

