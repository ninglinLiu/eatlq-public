#!/usr/bin/env python3
"""Fetch on-chain bytecode hashes for known OSS / labeled V4 hooks on Base."""
from __future__ import annotations

import hashlib
import json
import os
import sys

from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from research_rpc import get_research_rpc

TEMPLATE_PATH = os.path.join(HERE, "hook_templates.json")

# Known deployments / widely reused hooks on Base (sources in notes)
KNOWN_HOOKS = [
    {
        "name": "clanker_v4_hook",
        "source": "clanker.world / community",
        "address": "0x0469a4bd3724dc86c9542f4694c976da13c450c0",
        "notes": "Top reuse standard hook; AFTER_INIT+BEFORE/AFTER_SWAP",
    },
    {
        "name": "returns_delta_top_a",
        "source": "on-chain cluster",
        "address": "0xb429d62f8f3bFFb98CdB9569533eA23bF0Ba28CC",
        "notes": "Top RETURNS_DELTA; BEFORE+AFTER_SWAP_RETURNS_DELTA",
    },
    {
        "name": "returns_delta_top_b",
        "source": "on-chain cluster",
        "address": "0xBDF938149ac6a781F94FAa0ed45E6A0e984c6544",
        "notes": "AFTER_SWAP_RETURNS_DELTA dominant",
    },
    {
        "name": "returns_delta_top_c",
        "source": "on-chain cluster",
        "address": "0xd60D6B218116cFd801E28F78d011a203D2b068Cc",
        "notes": "RETURNS_DELTA family #3",
    },
]


def runtime_hash(code: bytes) -> str | None:
    return hashlib.sha256(code).hexdigest() if code else None


def build_templates() -> dict:
    rpc = get_research_rpc()
    templates = []
    for item in KNOWN_HOOKS:
        addr = Web3.to_checksum_address(item["address"])
        code = rpc.eth_get_code(addr)
        templates.append({
            **item,
            "address": addr,
            "bytecode_len": len(code),
            "runtime_hash": runtime_hash(code),
        })
    out = {
        "_note": "Runtime SHA256 of on-chain bytecode; match = same implementation",
        "templates": templates,
        "rpc_calls": rpc.count(),
    }
    json.dump(out, open(TEMPLATE_PATH, "w"), indent=2)
    return out


def main() -> int:
    out = build_templates()
    print(f"Wrote {len(out['templates'])} templates to {TEMPLATE_PATH}")
    for t in out["templates"]:
        print(f"  {t['name']:25s} hash={str(t['runtime_hash'])[:16]}… len={t['bytecode_len']}")
    print(f"rpc_calls={out['rpc_calls']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
