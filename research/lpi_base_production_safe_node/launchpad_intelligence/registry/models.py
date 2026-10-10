"""Registry and contract-version models for Base launchpads."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..errors import ContractError
from ..schema.unknown import UNKNOWN, Unknown, is_unknown


@dataclass(frozen=True)
class LaunchpadRecord:
    launchpad_id: str
    chain_id: int
    display_name: str
    status: str = "UNVERIFIED"
    research_priority: str = "P0"
    notes: str = ""

    def validate(self) -> None:
        if not self.launchpad_id:
            raise ContractError("launchpad_id required")
        if self.chain_id != 8453:
            raise ContractError("Base node registry accepts chain_id=8453 only")


@dataclass(frozen=True)
class ContractVersion:
    launchpad_id: str
    version_id: str
    chain_id: int
    factory_address: str | Unknown
    start_block: int | Unknown = UNKNOWN
    end_block: int | Unknown | None = None
    router: str | Unknown | None = None
    fee_locker: str | Unknown | None = None
    lp_locker: str | Unknown | None = None
    hook: str | Unknown | None = None
    abi_hash: str | Unknown | None = None
    verified_source: str | Unknown | None = None
    economics: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.launchpad_id or not self.version_id:
            raise ContractError("launchpad_id and version_id required")
        if self.chain_id != 8453:
            raise ContractError("version chain_id must be 8453")
        if is_unknown(self.factory_address):
            # Allowed: factory may be UNKNOWN until evidence arrives.
            return
        if not isinstance(self.factory_address, str) or not self.factory_address:
            raise ContractError("factory_address must be non-empty string or UNKNOWN")
        if (
            isinstance(self.start_block, int)
            and isinstance(self.end_block, int)
            and self.end_block < self.start_block
        ):
            raise ContractError("end_block precedes start_block")


class Registry:
    def __init__(
        self,
        launchpads: Iterable[LaunchpadRecord] = (),
        versions: Iterable[ContractVersion] = (),
    ) -> None:
        self._launchpads: dict[str, LaunchpadRecord] = {}
        self._versions: dict[tuple[str, str], ContractVersion] = {}
        for item in launchpads:
            self.add_launchpad(item)
        for version in versions:
            self.add_version(version)

    def add_launchpad(self, record: LaunchpadRecord) -> None:
        record.validate()
        self._launchpads[record.launchpad_id] = record

    def add_version(self, version: ContractVersion) -> None:
        version.validate()
        if version.launchpad_id not in self._launchpads:
            raise ContractError(f"unknown launchpad_id for version: {version.launchpad_id}")
        self._versions[(version.launchpad_id, version.version_id)] = version

    def get_launchpad(self, launchpad_id: str) -> LaunchpadRecord | None:
        return self._launchpads.get(launchpad_id)

    def get_version(self, launchpad_id: str, version_id: str) -> ContractVersion | None:
        return self._versions.get((launchpad_id, version_id))

    def list_launchpads(self) -> list[LaunchpadRecord]:
        return sorted(self._launchpads.values(), key=lambda x: x.launchpad_id)

    def list_versions(self, launchpad_id: str | None = None) -> list[ContractVersion]:
        values = list(self._versions.values())
        if launchpad_id is not None:
            values = [v for v in values if v.launchpad_id == launchpad_id]
        return sorted(values, key=lambda v: (v.launchpad_id, v.version_id))


def bootstrap_base_registry() -> Registry:
    """Minimal Base registry scaffold — addresses remain UNKNOWN until verified."""
    registry = Registry(
        launchpads=[
            LaunchpadRecord("clanker", 8453, "Clanker", status="CANDIDATE", research_priority="P0"),
            LaunchpadRecord("doppler", 8453, "Doppler", status="PLANNED", research_priority="P0"),
            LaunchpadRecord("flaunch", 8453, "Flaunch", status="PLANNED", research_priority="P0"),
        ]
    )
    registry.add_version(
        ContractVersion(
            launchpad_id="clanker",
            version_id="v4",
            chain_id=8453,
            factory_address=UNKNOWN.__class__("FACTORY_UNVERIFIED"),
            start_block=UNKNOWN.__class__("START_BLOCK_UNVERIFIED"),
            verified_source=UNKNOWN.__class__("SOURCE_UNVERIFIED"),
        )
    )
    registry.add_version(
        ContractVersion(
            launchpad_id="clanker",
            version_id="v4.1",
            chain_id=8453,
            factory_address=UNKNOWN.__class__("FACTORY_UNVERIFIED"),
            start_block=UNKNOWN.__class__("START_BLOCK_UNVERIFIED"),
            verified_source=UNKNOWN.__class__("SOURCE_UNVERIFIED"),
        )
    )
    return registry
