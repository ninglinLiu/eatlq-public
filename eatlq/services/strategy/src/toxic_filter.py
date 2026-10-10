from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Set


def load_mev_blacklist(path: Path, chain_id: int) -> Set[str]:
    """
    Loads chain-specific blacklist from a JSON-compatible YAML file.
    File format:
    {
      "8453": { "mev_searcher": ["0x..."], "aggregator": ["0x..."] }
    }
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return set()
    if not raw:
        return set()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return set()

    chain_payload = parsed.get(str(chain_id)) or parsed.get(chain_id) or {}
    if not isinstance(chain_payload, dict):
        return set()

    out: Set[str] = set()
    for values in chain_payload.values():
        if not isinstance(values, list):
            continue
        for addr in values:
            if isinstance(addr, str):
                cleaned = addr.lower().strip()
                if cleaned.startswith("0x") and len(cleaned) == 42:
                    out.add(cleaned)
    return out
