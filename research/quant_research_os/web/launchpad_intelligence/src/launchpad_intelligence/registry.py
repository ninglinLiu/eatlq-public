from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .models import Provenance, ValidationError


ECONOMIC_FIELDS = (
    "launch_model", "quote_asset", "initial_supply", "initial_market_cap_rule",
    "virtual_reserves", "real_reserves", "curve_formula", "creator_allocation",
    "creator_vesting", "buy_fee", "sell_fee", "creator_fee_share",
    "protocol_fee_share", "lp_fee_share", "graduation_rule", "graduation_threshold",
    "graduation_destination", "lp_owner", "lp_lock_rule", "lp_burn_rule",
    "mint_authority", "freeze_authority", "upgradeability", "blacklist_capability",
    "transfer_tax", "custom_hook_or_extension", "anti_sniper_rule",
    "automated_trader", "buyback_rule",
)


def load_json_yaml(path: Path) -> dict[str, Any]:
    """Load the repository's JSON-subset YAML without a third-party dependency."""
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Launchpad:
    chain: str
    launchpad_id: str
    launchpad_name: str
    protocol_family: str
    canonical_platform: str
    official_domain: str | None = None
    official_social_accounts: tuple[str, ...] = ()
    status: str = "UNVERIFIED"
    first_seen_block: int | None = None
    last_seen_block: int | None = None
    latest_verified_at: str | None = None
    research_priority: str = "P2"
    source_confidence: float = 0.0


@dataclass(frozen=True)
class ContractVersion:
    launchpad_id: str
    version_id: str
    chain: str
    factory_or_program_id: str
    router: str | None = None
    pool_manager: str | None = None
    migration_contract: str | None = None
    fee_vault: str | None = None
    liquidity_locker: str | None = None
    implementation: str | None = None
    bytecode_hash: str | None = None
    abi_or_idl_hash: str | None = None
    start_block: int = 0
    end_block: int | None = None
    upgrade_tx: str | None = None
    verified_source: str | None = None
    economics: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.factory_or_program_id:
            raise ValidationError("version requires a factory/program identifier")
        unknown = set(self.economics) - set(ECONOMIC_FIELDS)
        if unknown:
            raise ValidationError(f"unknown economic fields: {sorted(unknown)}")
        if self.end_block is not None and self.end_block < self.start_block:
            raise ValidationError("version end precedes start")


class Registry:
    def __init__(
        self,
        launchpads: Iterable[Launchpad] = (),
        versions: Iterable[ContractVersion] = (),
    ) -> None:
        self.launchpads = {item.launchpad_id: item for item in launchpads}
        self.versions: dict[str, list[ContractVersion]] = {}
        for version in versions:
            self.add_version(version)

    @classmethod
    def from_config(cls, config_dir: Path) -> "Registry":
        launchpad_rows = load_json_yaml(config_dir / "launchpads.yaml")["launchpads"]
        version_rows = load_json_yaml(config_dir / "contract_versions.yaml")["versions"]
        return cls(
            (Launchpad(**row) for row in launchpad_rows),
            (ContractVersion(**row) for row in version_rows),
        )

    def add_version(self, version: ContractVersion) -> None:
        version.validate()
        siblings = self.versions.setdefault(version.launchpad_id, [])
        for existing in siblings:
            existing_end = existing.end_block if existing.end_block is not None else 2**63 - 1
            new_end = version.end_block if version.end_block is not None else 2**63 - 1
            if max(existing.start_block, version.start_block) <= min(existing_end, new_end):
                raise ValidationError("contract version block ranges overlap")
        siblings.append(version)
        siblings.sort(key=lambda item: item.start_block)

    def resolve(self, launchpad_id: str, block_number: int) -> ContractVersion | None:
        for version in self.versions.get(launchpad_id, []):
            if version.start_block <= block_number and (
                version.end_block is None or block_number <= version.end_block
            ):
                return version
        return None

    def match_factory(self, chain: str, address: str, block_number: int) -> ContractVersion | None:
        needle = address.lower()
        for versions in self.versions.values():
            for version in versions:
                if version.chain == chain and version.factory_or_program_id.lower() == needle:
                    if version.start_block <= block_number and (
                        version.end_block is None or block_number <= version.end_block
                    ):
                        return version
        return None

    def match_fingerprint(self, chain: str, bytecode_hash: str) -> ContractVersion | None:
        for versions in self.versions.values():
            for version in versions:
                if version.chain == chain and version.bytecode_hash == bytecode_hash:
                    return version
        return None


def provenance_from_payload(
    payload: bytes,
    *,
    source: str,
    source_tier: Any,
    retrieved_at: str,
    effective_at: str,
    confidence: float,
) -> Provenance:
    return Provenance(
        source=source,
        source_tier=source_tier,
        retrieved_at=retrieved_at,
        effective_at=effective_at,
        confidence=confidence,
        raw_payload_hash=hashlib.sha256(payload).hexdigest(),
    )
