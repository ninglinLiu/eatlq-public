"""Minimal Clanker v4 TokenCreated decoder (fail-closed)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from eth_abi import decode
from eth_hash.auto import keccak

CHAIN_ID_BASE = 8453
FACTORY_V4 = "0xE85A59c628F7d27878ACeB4bf3b35733630083a9"
TOKEN_CREATED_SIGNATURE = (
    "TokenCreated(address,address,address,string,string,string,string,string,"
    "int24,address,bytes32,address,address,address,uint256,address[])"
)
TOKEN_CREATED_TOPIC0 = "0x" + keccak(TOKEN_CREATED_SIGNATURE.encode()).hex()
TOKEN_CREATED_DATA_TYPES = [
    "address",
    "string",
    "string",
    "string",
    "string",
    "string",
    "int24",
    "address",
    "bytes32",
    "address",
    "address",
    "address",
    "uint256",
    "address[]",
]

# ERC-20 Transfer topic0 — must never be accepted as a launch.
ERC20_TRANSFER_TOPIC0 = "0x" + keccak(b"Transfer(address,address,uint256)").hex()


def _addr(value: str) -> str:
    return "0x" + value[-40:].lower()


def _norm_addr(value: str) -> str:
    return value.lower() if value.startswith("0x") else "0x" + value.lower()


@dataclass
class LaunchRecord:
    chain_id: int
    factory: str
    factory_version: str
    transaction_hash: str
    block_number: int
    log_index: int
    token_address: str
    token_admin: str
    msg_sender: str
    token_name: str
    token_symbol: str
    token_image: str
    token_metadata: str
    token_context: str
    starting_tick: int
    pool_hook: str
    pool_id: str
    paired_token: str
    locker: str
    mev_module: str
    extensions_supply: int
    extensions: list[str] = field(default_factory=list)

    def dedupe_key(self) -> str:
        return f"{self.transaction_hash.lower()}:{self.log_index}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LaunchDecodeResult:
    status: str
    record: Optional[LaunchRecord] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "record": None if self.record is None else self.record.to_dict(),
        }


class ClankerLaunchDecoder:
    """Dispatch by factory/version; unknown emitters/versions fail closed."""

    def __init__(self, chain_id: int = CHAIN_ID_BASE) -> None:
        self.chain_id = chain_id
        self._factories = {
            FACTORY_V4.lower(): {
                "version": "4",
                "topic0": TOKEN_CREATED_TOPIC0.lower(),
            }
        }
        self._seen: set[str] = set()

    def reset_dedupe(self) -> None:
        self._seen.clear()

    def decode_log(
        self,
        log: dict[str, Any],
        *,
        chain_id: Optional[int] = None,
        allow_duplicate: bool = False,
    ) -> LaunchDecodeResult:
        cid = self.chain_id if chain_id is None else chain_id
        if cid != CHAIN_ID_BASE:
            return LaunchDecodeResult("CHAIN_MISMATCH", error=f"unsupported chain_id={cid}")

        try:
            address = _norm_addr(log["address"])
            topics = [t.lower() for t in (log.get("topics") or [])]
            data_hex = log.get("data") or "0x"
            tx_hash = (log.get("transactionHash") or "").lower()
            block_number = int(log["blockNumber"], 16) if isinstance(log["blockNumber"], str) else int(log["blockNumber"])
            log_index = int(log["logIndex"], 16) if isinstance(log["logIndex"], str) else int(log["logIndex"])
        except Exception as exc:  # noqa: BLE001 — fail closed
            return LaunchDecodeResult("MALFORMED_LOG", error=f"header_parse:{exc}")

        if not topics:
            return LaunchDecodeResult("MALFORMED_LOG", error="missing_topics")

        topic0 = topics[0]
        if topic0 == ERC20_TRANSFER_TOPIC0.lower():
            return LaunchDecodeResult("NOT_CLANKER_LAUNCH", error="erc20_transfer")

        factory_meta = self._factories.get(address)
        if factory_meta is None:
            return LaunchDecodeResult("UNKNOWN_EMITTER", error=f"emitter={address}")

        if topic0 != factory_meta["topic0"]:
            # Same factory, unrecognized event/version layout.
            return LaunchDecodeResult("UNKNOWN_VERSION", error=f"topic0={topic0}")

        if len(topics) != 3:
            return LaunchDecodeResult("MALFORMED_LOG", error=f"expected_3_topics got={len(topics)}")

        if not data_hex.startswith("0x"):
            return LaunchDecodeResult("MALFORMED_LOG", error="data_not_hex")
        raw = bytes.fromhex(data_hex[2:])
        if len(raw) < 32:
            return LaunchDecodeResult("MALFORMED_LOG", error="data_too_short")

        try:
            decoded = decode(TOKEN_CREATED_DATA_TYPES, raw)
        except Exception as exc:  # noqa: BLE001
            return LaunchDecodeResult("MALFORMED_LOG", error=f"abi_decode:{exc}")

        token_address = _addr(topics[1])
        token_admin = _addr(topics[2])
        pool_id = "0x" + decoded[8].hex()
        extensions = [a.lower() for a in decoded[13]]

        record = LaunchRecord(
            chain_id=cid,
            factory=address,
            factory_version=factory_meta["version"],
            transaction_hash=tx_hash,
            block_number=block_number,
            log_index=log_index,
            token_address=token_address,
            token_admin=token_admin,
            msg_sender=decoded[0].lower(),
            token_image=decoded[1],
            token_name=decoded[2],
            token_symbol=decoded[3],
            token_metadata=decoded[4],
            token_context=decoded[5],
            starting_tick=int(decoded[6]),
            pool_hook=decoded[7].lower(),
            pool_id=pool_id,
            paired_token=decoded[9].lower(),
            locker=decoded[10].lower(),
            mev_module=decoded[11].lower(),
            extensions_supply=int(decoded[12]),
            extensions=extensions,
        )

        key = record.dedupe_key()
        if not allow_duplicate and key in self._seen:
            return LaunchDecodeResult("DUPLICATE", error=key, record=record)
        self._seen.add(key)
        return LaunchDecodeResult("OK", record=record)
