"""Golden applicability classification and offline validator."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..errors import ContractError
from ..schema.unknown import UNKNOWN, is_unknown


class GoldenApplicability(StrEnum):
    REQUIRED = "REQUIRED"
    CONDITIONAL = "CONDITIONAL"
    NOT_APPLICABLE_WITH_EVIDENCE = "NOT_APPLICABLE_WITH_EVIDENCE"
    OPTIONAL_ANOMALY = "OPTIONAL_ANOMALY"


class GoldenVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    SKIPPED_NOT_APPLICABLE = "SKIPPED_NOT_APPLICABLE"


@dataclass(frozen=True)
class GoldenCase:
    case_id: str
    launchpad_id: str
    category: str
    applicability: GoldenApplicability
    required_fields: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
    notes: str = ""


@dataclass
class GoldenResult:
    case_id: str
    verdict: GoldenVerdict
    missing_fields: list[str] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    detail: str = ""


CLANKER_SEED_CASES: tuple[GoldenCase, ...] = (
    GoldenCase("clanker.creation", "clanker", "creation", GoldenApplicability.REQUIRED, ("token", "factory", "tx")),
    GoldenCase(
        "clanker.dev_buy_if_applicable",
        "clanker",
        "dev_buy_if_applicable",
        GoldenApplicability.CONDITIONAL,
        ("token", "buyer"),
    ),
    GoldenCase("clanker.normal_buy", "clanker", "normal_buy", GoldenApplicability.REQUIRED, ("token", "trader", "pool")),
    GoldenCase("clanker.normal_sell", "clanker", "normal_sell", GoldenApplicability.REQUIRED, ("token", "trader", "pool")),
    GoldenCase("clanker.fee_accrual", "clanker", "fee_accrual", GoldenApplicability.REQUIRED, ("token", "fee_amount")),
    GoldenCase("clanker.fee_claim", "clanker", "fee_claim", GoldenApplicability.REQUIRED, ("token", "claimant")),
    GoldenCase(
        "clanker.pool_creation", "clanker", "pool_creation", GoldenApplicability.REQUIRED, ("token", "pool")
    ),
    GoldenCase("clanker.lp_lock", "clanker", "lp_lock", GoldenApplicability.CONDITIONAL, ("pool", "locker")),
    GoldenCase(
        "clanker.creator_or_reward_recipient",
        "clanker",
        "creator_or_reward_recipient",
        GoldenApplicability.REQUIRED,
        ("token", "recipient"),
    ),
    GoldenCase(
        "clanker.extension_path",
        "clanker",
        "extension_path",
        GoldenApplicability.OPTIONAL_ANOMALY,
        ("token",),
    ),
    GoldenCase(
        "clanker.bonding_curve",
        "clanker",
        "bonding_curve",
        GoldenApplicability.NOT_APPLICABLE_WITH_EVIDENCE,
        (),
        evidence_refs=("PENDING_SOURCE_ABI_AND_CALL_PATH",),
        notes="Must not mark N/A without evidence; remains UNKNOWN until proven.",
    ),
)


class GoldenValidator:
    def __init__(self, cases: tuple[GoldenCase, ...] | None = None) -> None:
        self.cases = cases or CLANKER_SEED_CASES

    def validate_case(self, case: GoldenCase, observed: dict[str, Any]) -> GoldenResult:
        if case.applicability == GoldenApplicability.NOT_APPLICABLE_WITH_EVIDENCE:
            if not case.evidence_refs:
                return GoldenResult(
                    case.case_id,
                    GoldenVerdict.UNKNOWN,
                    detail="N/A claimed without evidence_refs",
                )
            # Evidence refs present but not yet verified against chain/source.
            if any(ref.startswith("PENDING_") for ref in case.evidence_refs):
                return GoldenResult(
                    case.case_id,
                    GoldenVerdict.UNKNOWN,
                    detail="NOT_APPLICABLE pending verified evidence",
                )
            return GoldenResult(case.case_id, GoldenVerdict.SKIPPED_NOT_APPLICABLE, detail="evidence present")

        missing: list[str] = []
        unknowns: list[str] = []
        for field_name in case.required_fields:
            if field_name not in observed:
                missing.append(field_name)
            elif is_unknown(observed[field_name]) or observed[field_name] is UNKNOWN:
                unknowns.append(field_name)
            elif observed[field_name] in ("", None):
                raise ContractError(f"{field_name} empty; use UNKNOWN sentinel instead of blank")

        if missing:
            return GoldenResult(case.case_id, GoldenVerdict.FAIL, missing_fields=missing, detail="missing fields")
        if unknowns:
            return GoldenResult(
                case.case_id, GoldenVerdict.UNKNOWN, unknown_fields=unknowns, detail="UNKNOWN preserved"
            )
        return GoldenResult(case.case_id, GoldenVerdict.PASS, detail="all required fields present")

    def validate_all(self, observations: dict[str, dict[str, Any]]) -> list[GoldenResult]:
        results: list[GoldenResult] = []
        for case in self.cases:
            results.append(self.validate_case(case, observations.get(case.case_id, {})))
        return results
