#!/usr/bin/env python3
"""Fetch real on-chain goldens for Clanker v4 decoder (public RPC only)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eth_hash.auto import keccak

from launchpad_intelligence.clanker_alpha.decoder import (
    FACTORY_V4,
    TOKEN_CREATED_TOPIC0,
    ClankerLaunchDecoder,
)
from launchpad_intelligence.clanker_alpha.rpc_public import PublicRpcClient

OUT = ROOT / "golden" / "clanker_alpha"
WETH = "0x4200000000000000000000000000000000000006"
TRANSFER_TOPIC0 = "0x" + keccak(b"Transfer(address,address,uint256)").hex()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rpc = PublicRpcClient("https://mainnet.base.org", max_calls=50)
    latest = int(rpc.call("eth_blockNumber", []), 16)
    # Prefer a slightly older window so goldens are stable.
    end = latest - 2000
    start = end - 8000
    logs = rpc.call(
        "eth_getLogs",
        [
            {
                "fromBlock": hex(start),
                "toBlock": hex(end),
                "address": FACTORY_V4,
                "topics": [TOKEN_CREATED_TOPIC0],
            }
        ],
        block_range=[start, end],
        address=FACTORY_V4,
        topic0=TOKEN_CREATED_TOPIC0,
    )
    if len(logs) < 3:
        raise SystemExit(f"need>=3 launches got={len(logs)}")

    decoder = ClankerLaunchDecoder()
    success = []
    for lg in logs:
        res = decoder.decode_log(lg)
        if res.status == "OK" and res.record is not None:
            success.append(lg)
        if len(success) >= 3:
            break
    if len(success) < 3:
        raise SystemExit("could not decode 3 success goldens")

    # Non-Clanker: ERC20 Transfer from WETH in same window.
    transfers = rpc.call(
        "eth_getLogs",
        [
            {
                "fromBlock": hex(end - 5),
                "toBlock": hex(end),
                "address": WETH,
                "topics": [TRANSFER_TOPIC0],
            }
        ],
        block_range=[end - 5, end],
        address=WETH,
        topic0=TRANSFER_TOPIC0,
    )
    if not transfers:
        raise SystemExit("no transfer counterexample")
    non_clanker = transfers[0]

    # Malformed: clone a success log and truncate data.
    malformed = json.loads(json.dumps(success[0]))
    malformed["data"] = "0xdead"

    # Wrong version / unknown topic on factory address (not ERC20 Transfer).
    wrong_version = json.loads(json.dumps(success[1]))
    fake_topic0 = "0x" + ("11" * 32)
    wrong_version["topics"] = [fake_topic0, wrong_version["topics"][1], wrong_version["topics"][2]]

    cases = [
        ("success_01.json", success[0], "OK"),
        ("success_02.json", success[1], "OK"),
        ("success_03.json", success[2], "OK"),
        ("non_clanker_transfer.json", non_clanker, "NOT_CLANKER_LAUNCH"),
        ("malformed_short_data.json", malformed, "MALFORMED_LOG"),
        ("wrong_version_topic.json", wrong_version, "UNKNOWN_VERSION"),
    ]

    manifest = {
        "source_endpoint_class": "public_base_http",
        "source_endpoint": "https://mainnet.base.org",
        "window": {"from_block": start, "to_block": end, "latest_at_fetch": latest},
        "factory": FACTORY_V4,
        "topic0": TOKEN_CREATED_TOPIC0,
        "cases": [],
    }

    dec = ClankerLaunchDecoder()
    for name, raw, expected in cases:
        blob = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        digest = sha256_bytes(blob)
        path = OUT / name
        path.write_bytes(blob)
        got = dec.decode_log(raw, allow_duplicate=True).status
        manifest["cases"].append(
            {
                "file": name,
                "sha256": digest,
                "expected_status": expected,
                "observed_status": got,
                "transaction_hash": raw.get("transactionHash"),
                "block_number": int(raw["blockNumber"], 16)
                if isinstance(raw.get("blockNumber"), str)
                else raw.get("blockNumber"),
                "address": raw.get("address"),
            }
        )
        print(name, got, "expected", expected, "sha256", digest[:16])

    man_path = OUT / "MANIFEST.json"
    man_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print("wrote", man_path)
    print("rpc_calls", rpc.calls, "bytes", rpc.response_bytes_total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
