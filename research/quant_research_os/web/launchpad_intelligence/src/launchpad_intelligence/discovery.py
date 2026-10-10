from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from .registry import Registry


@dataclass(frozen=True)
class DiscoveryResult:
    chain: str
    factory_or_program_id: str
    launchpad_id: str
    version_id: str
    classification: str
    bytecode_or_program_hash: str | None
    evidence_count: int


class UnknownFactoryDetector:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def classify(self, events: Iterable[dict[str, Any]]) -> list[DiscoveryResult]:
        grouped: Counter[tuple[str, str, str | None, int]] = Counter()
        for event in events:
            if event.get("operation") not in {"CREATE", "CREATE2", "FACTORY_EVENT", "PROGRAM_INSTRUCTION"}:
                continue
            chain = str(event["chain"])
            factory = str(event.get("factory") or event.get("program_id") or "UNKNOWN")
            fingerprint = event.get("bytecode_hash") or event.get("program_hash")
            grouped[(chain, factory, fingerprint, int(event.get("block_number_or_slot", 0)))] += 1
        results = []
        for (chain, factory, fingerprint, block), count in grouped.items():
            version = self.registry.match_factory(chain, factory, block)
            if version is None and fingerprint:
                version = self.registry.match_fingerprint(chain, fingerprint)
            results.append(DiscoveryResult(
                chain=chain,
                factory_or_program_id=factory,
                launchpad_id=version.launchpad_id if version else "UNKNOWN",
                version_id=version.version_id if version else "UNKNOWN",
                classification="ATTRIBUTED" if version else "UNCLASSIFIED",
                bytecode_or_program_hash=fingerprint,
                evidence_count=count,
            ))
        return sorted(results, key=lambda row: (row.chain, row.factory_or_program_id))


def contract_version_alerts(results: Iterable[DiscoveryResult], registry: Registry) -> list[dict[str, Any]]:
    alerts = []
    for row in results:
        fingerprint_known = row.bytecode_or_program_hash and registry.match_fingerprint(row.chain, row.bytecode_or_program_hash)
        if row.classification == "UNCLASSIFIED" or not fingerprint_known:
            alerts.append({
                "severity": "REVIEW",
                "chain": row.chain,
                "factory_or_program_id": row.factory_or_program_id,
                "fingerprint": row.bytecode_or_program_hash,
                "reason": "UNKNOWN_FACTORY" if row.classification == "UNCLASSIFIED" else "NEW_CONTRACT_VERSION",
            })
    return alerts


def launchpad_market_share(results: Iterable[DiscoveryResult]) -> dict[str, float]:
    counts = Counter(row.launchpad_id for row in results for _ in range(row.evidence_count))
    total = sum(counts.values())
    return {name: count / total for name, count in sorted(counts.items())} if total else {}


def unclassified_launch_queue(results: Iterable[DiscoveryResult]) -> list[dict[str, Any]]:
    return [row.__dict__ for row in results if row.classification == "UNCLASSIFIED"]

