from __future__ import annotations

from typing import Any

from .evm import EVMAdapter


class SolanaAdapter(EVMAdapter):
    """Solana semantic guard; never infers creator from an EVM-style tx_from."""

    chain = "solana"

    def detect_launch(self, raw: dict[str, Any]) -> bool:
        required = {"program_id", "instruction_discriminator", "inner_instructions", "mint", "pda"}
        return required.issubset(raw) and bool(raw.get("program_id"))

    def identify_creator(self, raw: dict[str, Any]) -> dict[str, Any]:
        signer = raw.get("metadata_signer") or raw.get("creator_fee_account_owner")
        if signer:
            return {"address": signer, "confidence": "hard_attribution", "basis": "solana_signer_or_fee_account"}
        return {"address": None, "confidence": "unresolved", "basis": "no_solana_semantic_evidence"}

