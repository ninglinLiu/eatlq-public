"""Shared buyer (tx.from) vs router sender semantics for wallet_profile_v0."""

from __future__ import annotations

from base_attribution import CHAIN_ID, EXCLUDED_ADDRESSES

# Compatibility alias. New code should import EXCLUDED_ADDRESSES directly.
FORBIDDEN_BUYERS = EXCLUDED_ADDRESSES


def buyer_join(alias: str = "s", tf_alias: str = "tf") -> str:
    """LEFT JOIN fragment for tx_from lookup."""
    return f"""
    LEFT JOIN (
        SELECT chain_id, tx_hash, anyLast(tx_from) AS tx_from
        FROM eatlq.tx_from_by_hash
        GROUP BY chain_id, tx_hash
    ) AS {tf_alias}
        ON {alias}.chain_id = {tf_alias}.chain_id
       AND lower({alias}.tx_hash) = lower({tf_alias}.tx_hash)
    """


def buyer_address_expr(alias: str = "s", tf_alias: str = "tf") -> str:
    """Canonical SQL expression: tx.from, then recipient, then sender."""
    excluded = ", ".join(f"'{address}'" for address in sorted(EXCLUDED_ADDRESSES))
    return f"""multiIf(
        match(lower({tf_alias}.tx_from), '^0x[0-9a-f]{{40}}$')
            AND lower({tf_alias}.tx_from) NOT IN ({excluded}), lower({tf_alias}.tx_from),
        match(lower({alias}.recipient), '^0x[0-9a-f]{{40}}$')
            AND lower({alias}.recipient) NOT IN ({excluded}), lower({alias}.recipient),
        match(lower({alias}.sender), '^0x[0-9a-f]{{40}}$')
            AND lower({alias}.sender) NOT IN ({excluded}), lower({alias}.sender),
        ''
    )"""


def buyer_address_filtered(alias: str = "s", tf_alias: str = "tf") -> str:
    """Buyer address excluding quote/router forbidden set."""
    inner = buyer_address_expr(alias, tf_alias)
    return inner
