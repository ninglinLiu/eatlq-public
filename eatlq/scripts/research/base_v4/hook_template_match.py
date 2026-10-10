"""Hook template matching for listener (0 RPC — uses hook_templates.json + bytecode cache)."""
from __future__ import annotations

import json
import os
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(HERE, "hook_templates.json")
BYTECODE_CACHE = "/tmp/v4_hook_bytecode_cache.json"

_TEMPLATE_CACHE: tuple[dict[str, str], dict[str, str], dict] | None = None


def load_template_maps() -> tuple[dict[str, str], dict[str, str]]:
    if not os.path.exists(TEMPLATE_PATH):
        return {}, {}
    data = json.load(open(TEMPLATE_PATH))
    by_hash, by_addr = {}, {}
    for t in data.get("templates", []):
        name = t["name"]
        if t.get("runtime_hash"):
            by_hash[t["runtime_hash"].lower()] = name
        if t.get("address"):
            by_addr[t["address"].lower()] = name
    return by_hash, by_addr


def load_bytecode_cache() -> dict:
    if os.path.exists(BYTECODE_CACHE):
        try:
            return json.load(open(BYTECODE_CACHE))
        except Exception:
            pass
    return {}


def _ensure_cache() -> tuple[dict[str, str], dict[str, str], dict]:
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None:
        by_hash, by_addr = load_template_maps()
        _TEMPLATE_CACHE = (by_hash, by_addr, load_bytecode_cache())
    return _TEMPLATE_CACHE


def match_hook_template(hooks_addr: str) -> dict:
    from hook_flags import classify_hook_bucket, decode_hook

    h = (hooks_addr or "").lower()
    by_hash, by_addr, cache = _ensure_cache()
    prof = decode_hook(hooks_addr)
    tmpl: Optional[str] = by_addr.get(h)
    match_via = "address" if tmpl else None
    if not tmpl:
        bc = cache.get(h, {})
        rh = (bc.get("runtime_hash") or "").lower()
        if rh and rh in by_hash:
            tmpl = by_hash[rh]
            match_via = "runtime_hash"
    bucket = classify_hook_bucket(prof, template_match=tmpl)
    return {
        "template_name": tmpl,
        "template_match_via": match_via,
        "hook_bucket": bucket,
        "hook_family": f"template:{tmpl}" if tmpl else bucket,
    }


def enrich_pool_row(row: dict) -> dict:
    out = dict(row)
    if out.get("hooks"):
        out["hook_template"] = match_hook_template(out["hooks"])
    return out
