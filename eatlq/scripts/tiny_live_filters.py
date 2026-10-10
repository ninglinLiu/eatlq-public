#!/usr/bin/env python3
"""Shared paper signal safety filters for tiny live runner."""
from __future__ import annotations


def _has_field(row: dict, field: str) -> bool:
    v = row.get(field)
    return v is not None and str(v).strip() != ""


def check_paper_safety(row: dict, *, strict: bool = True) -> tuple[bool, str]:
    """
    Apply sprint087 safety columns when present.
    Missing columns on legacy sprint055 rows: pass with reason legacy_missing.
    """
    if _has_field(row, "honeypot_flag"):
        if str(row.get("honeypot_flag", "")).lower() in ("true", "1"):
            return False, "honeypot"
    elif strict:
        pass  # legacy: no column

    if _has_field(row, "sell_path_ok"):
        if str(row.get("sell_path_ok", "")).lower() not in ("true", "1"):
            return False, "sell_path_unverified"

    if _has_field(row, "liquidity_depth_ok"):
        if str(row.get("liquidity_depth_ok", "")).lower() not in ("true", "1"):
            return False, "liquidity_shallow"

    if _has_field(row, "is_0x6ff5"):
        if str(row.get("is_0x6ff5", "")).lower() in ("true", "1"):
            return False, "sender_blacklist"

    return True, "ok"
