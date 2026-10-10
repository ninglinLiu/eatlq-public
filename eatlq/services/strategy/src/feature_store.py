from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import msgpack
import numpy as np
import redis

_INT256_THRESHOLD = 1 << 255
_INT256_MODULUS = 1 << 256

TOPIC_V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
TOPIC_AERO_VAMM_SWAP = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"
TOPIC_V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
TOPIC_ERC20_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_V2_SWAP_TOPICS = frozenset({TOPIC_V2_SWAP, TOPIC_AERO_VAMM_SWAP})

# H18.14 pool-first schema patch (Base canonical quotes; same constants already
# referenced in services/executor/src/registry.py and STRATEGY_TOKEN_BLACKLIST).
# Order is the quote-resolution priority used by _resolve_quote_target().
_QUOTE_PRIORITY_BASE = (
    ("0x4200000000000000000000000000000000000006", "WETH"),
    ("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USDC"),
    ("0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca", "USDbC"),
    ("0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf", "cbBTC"),
)
_QUOTE_ADDR_TO_SYMBOL = {a: s for a, s in _QUOTE_PRIORITY_BASE}
_QUOTE_ADDRS = frozenset(_QUOTE_ADDR_TO_SYMBOL.keys())
_ROUTER_ADDRS = frozenset({
    "0x6ff5693b99212da76ad316178a184ab56d299b43",  # Universal Router
})
_FORBIDDEN_SENDERS = _QUOTE_ADDRS | _ROUTER_ADDRS

@dataclass
class FeatureVector:
    price_velocity_5m: float
    buy_pressure_ratio: float
    token_volatility_5m: float
    activity_heat: int
    unique_buyer_entropy_5m: float = 0.0
    buy_pressure_ratio_count: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "price_velocity_5m": self.price_velocity_5m,
            "buy_pressure_ratio": self.buy_pressure_ratio,
            "token_volatility_5m": self.token_volatility_5m,
            "activity_heat": self.activity_heat,
            "unique_buyer_entropy_5m": self.unique_buyer_entropy_5m,
            "buy_pressure_ratio_count": self.buy_pressure_ratio_count,
        }


class FeatureStore:
    def __init__(self, redis_url: str, window_seconds: int = 300) -> None:
        self.redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self.window_seconds = window_seconds

    @staticmethod
    def _window_bounds_ms(now_ms: int, window_seconds: int) -> tuple[int, int]:
        return now_ms - (window_seconds * 1000), now_ms

    @staticmethod
    def _normalize_msgpack_payload(payload_bytes: bytes) -> Dict[str, Any]:
        # Ingestor uses rmp_serde::to_vec_named so payloads decode to dict.
        # Invalid MsgPack: raises (caught by strategy main loop → entry still XACK'd).
        unpacked = msgpack.unpackb(payload_bytes, raw=False)
        if isinstance(unpacked, dict):
            return unpacked
        if isinstance(unpacked, list):
            # Legacy fallbacks kept for in-flight messages from the pre-named
            # encoding; once the Redis stream MAXLEN trims past those, these
            # branches are dead code.
            if len(unpacked) % 2 == 0 and all(isinstance(unpacked[i], str) for i in range(0, len(unpacked), 2)):
                return {unpacked[i]: unpacked[i + 1] for i in range(0, len(unpacked), 2)}
            if all(isinstance(item, (list, tuple)) and len(item) == 2 for item in unpacked):
                return {str(item[0]): item[1] for item in unpacked}
        raise ValueError("Unsupported msgpack payload format")

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _hex_to_uint256(hex64: str) -> int:
        return int(hex64, 16) if hex64 else 0

    @staticmethod
    def _hex_to_int256(hex64: str) -> int:
        val = int(hex64, 16) if hex64 else 0
        return val - _INT256_MODULUS if val >= _INT256_THRESHOLD else val

    @staticmethod
    def _strip_0x(data: str) -> str:
        return data[2:] if data.startswith("0x") or data.startswith("0X") else data

    @staticmethod
    def _decode_v2_swap(hex_data: str) -> Dict[str, Any]:
        """Decode V2/Aerodrome-vAMM Swap data: 4 x uint256."""
        if len(hex_data) < 256:
            return {"net_flow": 0.0, "abs_flow": 0.0, "is_buy": True}
        a0_in = FeatureStore._hex_to_uint256(hex_data[0:64])
        a1_in = FeatureStore._hex_to_uint256(hex_data[64:128])
        a0_out = FeatureStore._hex_to_uint256(hex_data[128:192])
        a1_out = FeatureStore._hex_to_uint256(hex_data[192:256])
        net_flow = float(a0_in - a0_out)
        abs_flow = float(max(a0_in, a1_in, a0_out, a1_out))
        is_buy = a0_out > a0_in
        return {"net_flow": net_flow, "abs_flow": abs_flow, "is_buy": is_buy}

    @staticmethod
    def _decode_v3_swap(hex_data: str) -> Dict[str, Any]:
        """Decode V3 Swap data: int256 amount0, int256 amount1, ..."""
        if len(hex_data) < 128:
            return {"net_flow": 0.0, "abs_flow": 0.0, "is_buy": True}
        amount0 = FeatureStore._hex_to_int256(hex_data[0:64])
        amount1 = FeatureStore._hex_to_int256(hex_data[64:128])
        net_flow = float(-amount0)
        abs_flow = float(max(abs(amount0), abs(amount1)))
        is_buy = amount0 < 0
        return {"net_flow": net_flow, "abs_flow": abs_flow, "is_buy": is_buy}

    @staticmethod
    def _topic_to_address(topic_hex: str) -> str:
        """Extract 20-byte address from 32-byte indexed topic (last 40 hex chars)."""
        cleaned = FeatureStore._strip_0x(str(topic_hex or "")).lower()
        if len(cleaned) < 40:
            return ""
        return "0x" + cleaned[-40:]

    @staticmethod
    def _resolve_quote_target(token0: str, token1: str) -> Dict[str, str]:
        """H18.14 pool-first quote/target resolution.

        Returns:
            quote_token_address, quote_symbol, target_token_address, target_resolution_status
            where status ∈ {"ok", "no_known_quote_leg", "pool_meta_missing"}.
        """
        t0 = (token0 or "").lower().strip()
        t1 = (token1 or "").lower().strip()
        if not t0 and not t1:
            return {
                "quote_token_address": "",
                "quote_symbol": "",
                "target_token_address": "",
                "target_resolution_status": "pool_meta_missing",
            }
        for q_addr, q_sym in _QUOTE_PRIORITY_BASE:
            if t0 == q_addr:
                return {
                    "quote_token_address": q_addr,
                    "quote_symbol": q_sym,
                    "target_token_address": t1,
                    "target_resolution_status": "ok" if t1 else "pool_meta_missing",
                }
            if t1 == q_addr:
                return {
                    "quote_token_address": q_addr,
                    "quote_symbol": q_sym,
                    "target_token_address": t0,
                    "target_resolution_status": "ok" if t0 else "pool_meta_missing",
                }
        return {
            "quote_token_address": "",
            "quote_symbol": "",
            "target_token_address": "",
            "target_resolution_status": "no_known_quote_leg",
        }

    @staticmethod
    def _normalize_side(
        topic0: str,
        raw_data: str,
        token0: str,
        token1: str,
        quote_token_address: str,
    ) -> str:
        """H18.14 quote-leg-relative side normalization.

        Returns one of: "buy_target" | "sell_target" | "ambiguous" | "unknown".
        Independent from the legacy is_buy = "token0 was bought" rule which is
        WETH-leg-blind and only happens to be correct on certain pool layouts.
        """
        t0 = (token0 or "").lower().strip()
        t1 = (token1 or "").lower().strip()
        q = (quote_token_address or "").lower().strip()
        if not q or not (t0 or t1):
            return "unknown"
        quote_is_t0 = (t0 == q)
        quote_is_t1 = (t1 == q)
        if not (quote_is_t0 or quote_is_t1):
            return "unknown"
        if topic0 in _V2_SWAP_TOPICS:
            if len(raw_data) < 256:
                return "ambiguous"
            a0i = FeatureStore._hex_to_uint256(raw_data[0:64])
            a1i = FeatureStore._hex_to_uint256(raw_data[64:128])
            a0o = FeatureStore._hex_to_uint256(raw_data[128:192])
            a1o = FeatureStore._hex_to_uint256(raw_data[192:256])
            quote_in   = a0i if quote_is_t0 else a1i
            quote_out  = a0o if quote_is_t0 else a1o
            target_in  = a1i if quote_is_t0 else a0i
            target_out = a1o if quote_is_t0 else a0o
            if quote_in > 0 and target_out > 0:
                return "buy_target"
            if target_in > 0 and quote_out > 0:
                return "sell_target"
            return "ambiguous"
        if topic0 == TOPIC_V3_SWAP:
            if len(raw_data) < 128:
                return "ambiguous"
            a0 = FeatureStore._hex_to_int256(raw_data[0:64])
            a1 = FeatureStore._hex_to_int256(raw_data[64:128])
            quote_signed  = a0 if quote_is_t0 else a1
            target_signed = a1 if quote_is_t0 else a0
            # V3 sign: positive = pool received that token, negative = pool paid out.
            if quote_signed > 0 and target_signed < 0:
                return "buy_target"
            if quote_signed < 0 and target_signed > 0:
                return "sell_target"
            return "ambiguous"
        return "unknown"

    @staticmethod
    def _topic_to_pool_type(topic0: str) -> str:
        """Best-effort pool_type from topic0 alone (Aero CL is also V3 Swap topic
        and only distinguishable via pool:discovered metadata)."""
        if topic0 == TOPIC_V2_SWAP:
            return "v2"
        if topic0 == TOPIC_AERO_VAMM_SWAP:
            return "aero_vamm"
        if topic0 == TOPIC_V3_SWAP:
            return "v3"
        return "unknown"

    def _lookup_pool_meta(self, pool_address: str) -> Dict[str, str]:
        """Bounded, cache-only pool metadata lookup. Reads pool:discovered:{addr}
        from Redis. No on-chain RPC is performed here — H18.14 keeps RPC bounded
        by relying on the existing pool-discovery service to populate the cache.
        Returns {} on miss.
        """
        if not pool_address:
            return {}
        try:
            meta = self.redis.hgetall(f"pool:discovered:{pool_address}")
            return meta or {}
        except Exception:
            return {}

    def _extract_tick_fields(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # H18.14: payload["address"] is the EVM log emitter. For Swap events that
        # is the POOL CONTRACT, NOT an ERC20 token. The legacy `token_address`
        # field is preserved as a frozen alias of `pool_address` for backward
        # compatibility — new code MUST use `pool_address` / `target_token_address`.
        pool_address = (
            payload.get("address")
            or payload.get("token_address")
            or payload.get("token")
            or payload.get("asset")
            or ""
        )
        pool_address = str(pool_address).lower().strip()

        symbol = payload.get("symbol") or payload.get("token_symbol") or "UNKNOWN"
        ts = int(FeatureStore._to_float(payload.get("timestamp_ms") or payload.get("received_at_ms"), 0))
        if ts <= 0:
            ts = int(time.time() * 1000)

        topic0 = str(payload.get("topic0") or "").lower().strip()
        raw_data = FeatureStore._strip_0x(str(payload.get("data") or ""))

        tx_from = str(payload.get("tx_from") or "").lower().strip()
        topic1_addr = FeatureStore._topic_to_address(payload.get("topic1"))
        if tx_from.startswith("0x") and len(tx_from) == 42 and tx_from not in _FORBIDDEN_SENDERS:
            sender_address = tx_from
        elif topic1_addr.startswith("0x") and topic1_addr not in _FORBIDDEN_SENDERS:
            sender_address = topic1_addr
        else:
            sender_address = ""
        recipient_address = FeatureStore._topic_to_address(payload.get("topic2"))

        if topic0 in _V2_SWAP_TOPICS:
            decoded = FeatureStore._decode_v2_swap(raw_data)
        elif topic0 == TOPIC_V3_SWAP:
            decoded = FeatureStore._decode_v3_swap(raw_data)
        else:
            decoded = {"net_flow": 0.0, "abs_flow": 0.0, "is_buy": True}

        # --- H18.14 pool-first enrichment (additive, degrades gracefully) ---
        pool_meta = self._lookup_pool_meta(pool_address)
        token0 = (pool_meta.get("token0_address") or "").lower().strip()
        token1 = (pool_meta.get("token1_address") or "").lower().strip()
        pool_type_meta = (pool_meta.get("pool_type") or "").lower().strip()
        pool_type = pool_type_meta or FeatureStore._topic_to_pool_type(topic0)
        factory = (pool_meta.get("factory_address") or "").lower().strip()
        if pool_type == "v3" and factory == "0x33128a8fc17869897dce68ed026d694621f6fdfd":
            dex = "uniswap_v3"
        elif pool_type == "v2" and factory == "0x8909dc15e40173ff4699343b6eb8132c65e18ec6":
            dex = "uniswap_v2"
        elif pool_type in ("aero_vamm", "aero_cl"):
            dex = "aerodrome"
        elif pool_type == "v3":
            dex = "uniswap_v3"
        elif pool_type == "v2":
            dex = "uniswap_v2"
        else:
            dex = "unknown"
        try:
            fee_bps_val = int(pool_meta.get("fee_bps", "")) if pool_meta.get("fee_bps") else None
        except (TypeError, ValueError):
            fee_bps_val = None

        qt = FeatureStore._resolve_quote_target(token0, token1)
        if not pool_meta:
            qt["target_resolution_status"] = "pool_meta_missing"
        normalized_side = FeatureStore._normalize_side(
            topic0, raw_data, token0, token1, qt["quote_token_address"]
        )

        return {
            # Legacy field kept as frozen alias of pool_address (DO NOT read in
            # new code; use pool_address / target_token_address instead).
            "token_address": pool_address,
            "symbol": str(symbol),
            "timestamp_ms": ts,
            "topic0": topic0,
            "block_number": int(FeatureStore._to_float(payload.get("block_number"), 0)),
            "tx_hash": str(payload.get("tx_hash") or "").lower().strip(),
            "tx_index": int(FeatureStore._to_float(payload.get("tx_index"), 0)),
            "log_index": int(FeatureStore._to_float(payload.get("log_index"), 0)),
            "sender_address": sender_address,
            "tx_from": tx_from if tx_from.startswith("0x") else "",
            "recipient_address": recipient_address,
            "net_flow": decoded["net_flow"],
            "abs_flow": decoded["abs_flow"],
            "is_buy": decoded["is_buy"],
            # --- H18.14 new pool-first fields (additive) ---
            "pool_address": pool_address,
            "pool_type": pool_type,
            "dex": dex,
            "fee_bps": fee_bps_val,
            "token0": token0,
            "token1": token1,
            "quote_token_address": qt["quote_token_address"],
            "quote_symbol": qt["quote_symbol"],
            "target_token_address": qt["target_token_address"],
            "target_resolution_status": qt["target_resolution_status"],
            "normalized_side": normalized_side,
            "is_buy_legacy": decoded["is_buy"],
        }

    def update_from_stream_entry(self, payload_msgpack: bytes,
                                  extra_fields: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Decode one ``market_ticks`` MsgPack row and refresh FeatureStore (hot path).

        Contract: ``payload_msgpack`` must be the blob from ``parse_stream_payload`` in
        ``main.py`` (field priority ``docs/market-ticks-stream-contract.md``). Does not
        change scoring thresholds — only feature extraction inputs.
        """
        normalized = self._normalize_msgpack_payload(payload_msgpack)
        if extra_fields:
            for k, v in extra_fields.items():
                if v and k not in normalized:
                    normalized[k] = v
        tick = self._extract_tick_fields(normalized)
        self.update(tick)
        return tick

    def update(self, tick: Dict[str, Any]) -> None:
        token = tick["token_address"]
        if not token:
            return

        now_ms = int(tick["timestamp_ms"])
        lower_ms, _ = self._window_bounds_ms(now_ms, self.window_seconds)
        suffix = f"{now_ms}:{uuid.uuid4().hex}"

        flow_key = f"feat:flow:{token}"
        toxic_key = f"feat:toxic:{token}"
        buys_key = f"feat:buys:{token}"
        sells_key = f"feat:sells:{token}"
        ticks_key = f"feat:ticks:{token}"
        meta_key = f"feat:meta:{token}"
        seen_key = f"feat:seen:{token}"
        wallet_first_buy_key = f"feat:wallet_first_buy:{token}"
        toxic_wallets_key = f"feat:toxic_wallets:{token}"

        tx_hash = str(tick.get("tx_hash") or "")
        log_index = int(tick.get("log_index", 0))
        if tx_hash:
            dedup_member = f"{tx_hash}:{log_index}"
            if self.redis.sadd(seen_key, dedup_member) == 0:
                return
            self.redis.expire(seen_key, self.window_seconds * 2)

        sender = str(tick.get("sender_address") or "").lower()
        is_buy = bool(tick.get("is_buy", False))
        is_toxic = False

        if sender:
            if is_buy:
                existing = self.redis.hget(wallet_first_buy_key, sender)
                if existing is None:
                    self.redis.hset(wallet_first_buy_key, sender, now_ms)
            else:
                first_buy = self.redis.hget(wallet_first_buy_key, sender)
                if first_buy is not None:
                    try:
                        delta_ms = now_ms - int(first_buy)
                    except (TypeError, ValueError):
                        delta_ms = 999999999
                    if 0 <= delta_ms <= 60000:
                        is_toxic = True
                        self.redis.zadd(toxic_wallets_key, {sender: now_ms})
                        self.redis.expire(toxic_wallets_key, self.window_seconds * 2)

            block_number = int(tick.get("block_number") or 0)
            tx_index = int(tick.get("tx_index") or 0)
            if block_number > 0:
                bw_key = f"feat:block_wallet:{token}:{block_number}"
                raw_state = self.redis.hget(bw_key, sender)
                if raw_state:
                    try:
                        state = json.loads(raw_state)
                    except json.JSONDecodeError:
                        state = {}
                else:
                    state = {}
                if is_buy:
                    prev_buy = state.get("buy_tx_index")
                    if prev_buy is None or tx_index < int(prev_buy):
                        state["buy_tx_index"] = tx_index
                else:
                    buy_tx_idx = state.get("buy_tx_index")
                    if buy_tx_idx is not None:
                        diff = tx_index - int(buy_tx_idx)
                        if 2 <= diff <= 4:
                            is_toxic = True
                            self.redis.zadd(toxic_wallets_key, {sender: now_ms})
                            self.redis.expire(toxic_wallets_key, self.window_seconds * 2)
                self.redis.hset(bw_key, sender, json.dumps(state, separators=(",", ":")))
                self.redis.expire(bw_key, self.window_seconds * 2)

        flow_payload = json.dumps(
            {
                "net_flow": tick["net_flow"],
                "abs_flow": tick["abs_flow"],
                "is_buy": is_buy,
                "sender_address": sender,
                "block_number": int(tick.get("block_number") or 0),
            },
            separators=(",", ":"),
        )

        if is_toxic:
            self.redis.zadd(toxic_key, {suffix: now_ms})
        else:
            self.redis.zadd(flow_key, {flow_payload: now_ms})
            self.redis.zadd(ticks_key, {suffix: now_ms})
            if is_buy:
                self.redis.zadd(buys_key, {suffix: now_ms})
            else:
                self.redis.zadd(sells_key, {suffix: now_ms})

        self.redis.hset(meta_key, mapping={"symbol": tick.get("symbol", "UNKNOWN")})
        self.redis.expire(meta_key, self.window_seconds * 2)

        if sender and not is_toxic:
            wallet_key = f"feat:wallet_buys:{token}" if tick["is_buy"] else f"feat:wallet_sells:{token}"
            self.redis.zadd(wallet_key, {sender: now_ms})
            self.redis.zremrangebyscore(wallet_key, "-inf", lower_ms - 1)

        for key in (flow_key, toxic_key, buys_key, sells_key, ticks_key):
            self.redis.zremrangebyscore(key, "-inf", lower_ms - 1)
        stale_wallets = []
        for key, value in self.redis.hgetall(wallet_first_buy_key).items():
            try:
                if int(value) < lower_ms:
                    stale_wallets.append(key)
            except (TypeError, ValueError):
                stale_wallets.append(key)
        if stale_wallets:
            self.redis.hdel(wallet_first_buy_key, *stale_wallets)

    def is_wallet_toxic(self, token_address: str, sender_address: str, now_ms: int | None = None) -> bool:
        sender = str(sender_address or "").lower().strip()
        if not sender:
            return False
        token = token_address.lower()
        now_ms = now_ms or int(time.time() * 1000)
        lower_ms, _ = self._window_bounds_ms(now_ms, self.window_seconds)
        key = f"feat:toxic_wallets:{token}"
        self.redis.zremrangebyscore(key, "-inf", lower_ms - 1)
        return self.redis.zscore(key, sender) is not None

    @staticmethod
    def compute_from_snapshot(
        flow_points: Iterable[Dict[str, float]],
        tick_count: int,
    ) -> FeatureVector:
        points = list(flow_points)
        if not points:
            return FeatureVector(0.0, 0.5, 0.0, 0, 0.0, 0.5)

        net_by_sender_block: Dict[tuple[int, str], float] = defaultdict(float)
        buy_volume = 0.0
        buy_count = 0
        sell_count = 0
        buyer_volumes: Dict[str, float] = defaultdict(float)

        for p in points:
            block = int(p.get("block_number", 0))
            sender = str(p.get("sender_address", "") or "")
            key = (block, sender)
            net_by_sender_block[key] += float(p.get("net_flow", 0.0))
            flow_abs = abs(float(p.get("abs_flow", 0.0)))
            if bool(p.get("is_buy", False)):
                buy_volume += flow_abs
                buy_count += 1
                if sender:
                    buyer_volumes[sender] += flow_abs
            else:
                sell_count += 1

        net_flow = float(sum(net_by_sender_block.values()))
        abs_flows = np.array([abs(float(p.get("abs_flow", 0.0))) for p in points], dtype=np.float64)
        total_flow = float(abs_flows.sum())
        if total_flow <= 0:
            velocity = 0.0
            pressure = 0.5
        else:
            velocity = max(-1.0, min(1.0, net_flow / abs(total_flow)))
            pressure = max(0.0, min(1.0, buy_volume / total_flow))

        mean_abs = float(abs_flows.mean()) if len(abs_flows) > 0 else 0.0
        if mean_abs <= 0:
            volatility = 0.0
        else:
            volatility = float(abs_flows.std(ddof=0) / mean_abs)
        volatility = min(2.0, max(0.0, volatility))

        entropy = 0.0
        total_buy_vol = float(sum(buyer_volumes.values()))
        if total_buy_vol > 0 and len(buyer_volumes) > 1:
            probs = np.array([v / total_buy_vol for v in buyer_volumes.values()], dtype=np.float64)
            entropy = float(-np.sum(probs * np.log(probs + 1e-12)))

        denom = buy_count + sell_count
        pressure_count = (buy_count / denom) if denom > 0 else 0.5

        return FeatureVector(
            price_velocity_5m=float(velocity),
            buy_pressure_ratio=float(pressure),
            token_volatility_5m=float(volatility),
            activity_heat=int(tick_count),
            unique_buyer_entropy_5m=float(entropy),
            buy_pressure_ratio_count=float(pressure_count),
        )

    def compute(self, token_address: str, now_ms: int | None = None) -> Dict[str, Any]:
        now_ms = now_ms or int(time.time() * 1000)
        token = token_address.lower()
        lower_ms, upper_ms = self._window_bounds_ms(now_ms, self.window_seconds)

        flow_key = f"feat:flow:{token}"
        buys_key = f"feat:buys:{token}"
        sells_key = f"feat:sells:{token}"
        ticks_key = f"feat:ticks:{token}"

        for key in (flow_key, buys_key, sells_key, ticks_key):
            self.redis.zremrangebyscore(key, "-inf", lower_ms - 1)

        raw_flows: List[str] = self.redis.zrangebyscore(flow_key, lower_ms, upper_ms)  # type: ignore[assignment]
        flow_points: List[Dict[str, float]] = []
        for raw in raw_flows:
            try:
                flow_points.append(json.loads(raw))
            except json.JSONDecodeError:
                continue

        tick_count = int(self.redis.zcount(ticks_key, lower_ms, upper_ms))

        features = self.compute_from_snapshot(flow_points, tick_count).to_dict()
        self._publish_feature_keys(token, features)
        return features

    def _publish_feature_keys(self, pool_address: str, features: Dict[str, Any]) -> None:
        """Write per-feature Redis keys for fusion / ML consumers (TTL 600s)."""
        pool = pool_address.lower()
        ttl = 600
        mapping = {
            f"feat:{pool}:price_velocity_5m": features.get("price_velocity_5m", 0.0),
            f"feat:{pool}:buy_pressure_ratio": features.get("buy_pressure_ratio", 0.5),
            f"feat:{pool}:token_volatility_5m": features.get("token_volatility_5m", 0.0),
            f"feat:{pool}:activity_heat": features.get("activity_heat", 0),
            f"feat:{pool}:unique_buyer_entropy_5m": features.get("unique_buyer_entropy_5m", 0.0),
            f"feat:{pool}:buy_pressure_ratio_count": features.get("buy_pressure_ratio_count", 0.5),
        }
        pipe = self.redis.pipeline()
        for key, val in mapping.items():
            pipe.set(key, str(val), ex=ttl)
        pipe.execute()

    @staticmethod
    def compute_token_volatility_5m(flow_points: Iterable[Dict[str, float]]) -> float:
        """5min window size CV, capped at 2.0 (whitepaper dim 4)."""
        abs_flows = [abs(float(p.get("abs_flow", 0.0))) for p in flow_points]
        if not abs_flows:
            return 0.0
        mean_abs = float(np.mean(abs_flows))
        if mean_abs <= 0:
            return 0.0
        vol = float(np.std(abs_flows, ddof=0) / mean_abs)
        return min(2.0, max(0.0, vol))
