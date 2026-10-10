from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import Confidence, ValidationError, parse_time


NODE_ROLES = {
    "creator", "deployer", "metadata_signer", "initial_funder", "fee_receiver",
    "lp_owner", "migration_caller", "first_buyer", "sniper", "bundle_cluster",
    "treasury", "social_owner", "platform_operator", "automated_trading_agent",
}
EDGE_TYPES = {
    "funded", "deployed", "received_fees", "received_tokens", "shared_nonce_pattern",
    "shared_funder", "shared_withdrawal_address", "same_metadata", "same_social_account",
    "same_bytecode", "same_bundle", "same_lp_manager",
}


@dataclass(frozen=True)
class EntityNode:
    entity_id: str
    role: str
    address: str | None
    confidence: Confidence
    effective_at: str
    information_available_at: str
    evidence: tuple[str, ...]

    def validate(self) -> None:
        if self.role not in NODE_ROLES:
            raise ValidationError(f"unsupported entity role {self.role}")
        if parse_time(self.effective_at) > parse_time(self.information_available_at):
            raise ValidationError("attribution uses future information")
        if self.confidence == Confidence.HARD and not self.evidence:
            raise ValidationError("hard attribution requires evidence")


@dataclass(frozen=True)
class EntityEdge:
    source_entity_id: str
    target_entity_id: str
    edge_type: str
    confidence: Confidence
    information_available_at: str
    evidence: tuple[str, ...]

    def validate(self) -> None:
        if self.edge_type not in EDGE_TYPES:
            raise ValidationError(f"unsupported entity edge {self.edge_type}")
        if self.confidence == Confidence.HARD and not self.evidence:
            raise ValidationError("hard edge requires evidence")


class AttributionGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, EntityNode] = {}
        self.edges: list[EntityEdge] = []

    def add_node(self, node: EntityNode) -> None:
        node.validate()
        self.nodes[node.entity_id] = node

    def add_edge(self, edge: EntityEdge) -> None:
        edge.validate()
        if edge.source_entity_id not in self.nodes or edge.target_entity_id not in self.nodes:
            raise ValidationError("edge endpoint is not registered")
        self.edges.append(edge)

    def point_in_time(self, cutoff: str) -> "AttributionGraph":
        cutoff_time = parse_time(cutoff)
        result = AttributionGraph()
        for node in self.nodes.values():
            if parse_time(node.information_available_at) <= cutoff_time:
                result.add_node(node)
        for edge in self.edges:
            if (
                parse_time(edge.information_available_at) <= cutoff_time
                and edge.source_entity_id in result.nodes
                and edge.target_entity_id in result.nodes
            ):
                result.add_edge(edge)
        return result

    def cluster(self, seeds: Iterable[str], min_confidence: Confidence = Confidence.PROBABILISTIC) -> set[str]:
        rank = {Confidence.UNRESOLVED: 0, Confidence.PROBABILISTIC: 1, Confidence.HARD: 2}
        found = set(seeds)
        changed = True
        while changed:
            changed = False
            for edge in self.edges:
                if rank[edge.confidence] < rank[min_confidence]:
                    continue
                if edge.source_entity_id in found and edge.target_entity_id not in found:
                    found.add(edge.target_entity_id)
                    changed = True
                if edge.target_entity_id in found and edge.source_entity_id not in found:
                    found.add(edge.source_entity_id)
                    changed = True
        return found
