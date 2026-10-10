"""
Pool registry for Base chain (chain_id=8453).

Maps pool_address -> PoolInfo with router, tokens, fee, and pool type.
Hardcoded from 006_registry.sql to avoid runtime DB dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

WETH_BASE = "0x4200000000000000000000000000000000000006"

UNISWAP_V2_ROUTER = "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"
AERODROME_ROUTER = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
UNISWAP_V3_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"

ROUTER_FOR_POOL_TYPE = {
    "v2": UNISWAP_V2_ROUTER,
    "aero_vamm": AERODROME_ROUTER,
    "v3": UNISWAP_V3_ROUTER,
    "aero_cl": UNISWAP_V3_ROUTER,  # Aerodrome CL uses same interface as V3
}

STABLECOINS = frozenset({
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
})


@dataclass(frozen=True)
class PoolInfo:
    pool_address: str
    pool_type: str  # v2 | aero_vamm | v3 | aero_cl
    router: str
    token0: str
    token0_symbol: str
    token0_decimals: int
    token1: str
    token1_symbol: str
    token1_decimals: int
    fee_bps: int

    @property
    def weth_is_token0(self) -> bool:
        return self.token0.lower() == WETH_BASE.lower()

    @property
    def weth_is_token1(self) -> bool:
        return self.token1.lower() == WETH_BASE.lower()

    @property
    def non_weth_token(self) -> str:
        if self.weth_is_token0:
            return self.token1
        return self.token0

    @property
    def non_weth_symbol(self) -> str:
        if self.weth_is_token0:
            return self.token1_symbol
        return self.token0_symbol

    @property
    def non_weth_decimals(self) -> int:
        if self.weth_is_token0:
            return self.token1_decimals
        return self.token0_decimals

    @property
    def has_weth(self) -> bool:
        return self.weth_is_token0 or self.weth_is_token1

    @property
    def non_weth_is_stablecoin(self) -> bool:
        return self.non_weth_token.lower() in STABLECOINS

    @property
    def signal_direction_inverted(self) -> bool:
        """True when model predicts token0 buying pressure but non_weth_token is NOT token0.
        For WETH/USDC: model predicts WETH(token0) up, but executor would buy USDC(token1).
        For AERO/WETH: model predicts AERO(token0) up, executor buys AERO(token0). OK.
        """
        if not self.has_weth:
            return False
        return self.weth_is_token0 and self.non_weth_is_stablecoin


_TOKEN_DECIMALS: Dict[str, int] = {
    "0x4200000000000000000000000000000000000006": 18,  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,   # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 6,   # USDbC
    "0x2ae3f1ec7f1f5012cfeab0185bfcb5aa7ac6e90f": 18,  # cbETH
    "0x940181a94a35a4569e4529a3cdfb74e38fd98631": 18,  # AERO
    "0x4ed4e862860be2ec1f4896764335d5942baadf07": 18,  # DEGEN
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": 18,  # DAI
    "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452": 18,  # wstETH
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": 8,   # cbBTC
}


def _decimals(addr: str) -> int:
    return _TOKEN_DECIMALS.get(addr.lower(), 18)


def _pool(pool_addr: str, pool_type: str, t0: str, t0s: str, t1: str, t1s: str, fee: int) -> PoolInfo:
    return PoolInfo(
        pool_address=pool_addr.lower(),
        pool_type=pool_type,
        router=ROUTER_FOR_POOL_TYPE[pool_type],
        token0=t0.lower(),
        token0_symbol=t0s,
        token0_decimals=_decimals(t0),
        token1=t1.lower(),
        token1_symbol=t1s,
        token1_decimals=_decimals(t1),
        fee_bps=fee,
    )


# ── Aerodrome vAMM pairs (V2 layout) ──
_AERO_VAMM = [
    _pool("0x6cdcb1c4a4d1c3c6d054b27ac5b77e89eafb971d", "aero_vamm",
          "0x940181a94a35a4569e4529a3cdfb74e38fd98631", "AERO",
          "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USDC", 30),
    _pool("0x7f670f78b17dec44d5ef68a48740b6f8849cc2e6", "aero_vamm",
          "0x940181a94a35a4569e4529a3cdfb74e38fd98631", "AERO",
          "0x4200000000000000000000000000000000000006", "WETH", 30),
]

# ── Uniswap V3 pools on Base ──
_V3 = [
    _pool("0xd0b53d9277642d899df5c87a3966a349a798f224", "v3",
          "0x4200000000000000000000000000000000000006", "WETH",
          "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USDC", 5),
    _pool("0x4c36388be6f416a29c8d8eee81c771ce6be14b18", "v3",
          "0x4200000000000000000000000000000000000006", "WETH",
          "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca", "USDbC", 5),
    _pool("0x10648ba41b8565907cfa1496765fa4d95390aa0d", "v3",
          "0x2ae3f1ec7f1f5012cfeab0185bfcb5aa7ac6e90f", "cbETH",
          "0x4200000000000000000000000000000000000006", "WETH", 5),
    _pool("0xb4cb800910b228ed3d0834cf79d697127bbb00e5", "v3",
          "0x4200000000000000000000000000000000000006", "WETH",
          "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USDC", 30),
    _pool("0xfcd08d68b2313cd5e1d9e0e3f3183ad7cfdbf66f", "v3",
          "0x4200000000000000000000000000000000000006", "WETH",
          "0x50c5725949a6f0c72e6c4a641f24049a917db0cb", "DAI", 5),
]

# ── Aerodrome CL pools (V3 interface) ──
_AERO_CL = [
    _pool("0x82321f3beb69f503380d6b233857d5c43562e2d0", "aero_cl",
          "0x940181a94a35a4569e4529a3cdfb74e38fd98631", "AERO",
          "0x4200000000000000000000000000000000000006", "WETH", 100),
    _pool("0xdfe5f275020def30993f042174fc2d335678b626", "aero_cl",
          "0x940181a94a35a4569e4529a3cdfb74e38fd98631", "AERO",
          "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf", "cbBTC", 100),
    _pool("0xbe00ff35af70e8415d0eb605a286d8a45466a4c1", "aero_cl",
          "0x940181a94a35a4569e4529a3cdfb74e38fd98631", "AERO",
          "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USDC", 100),
]

POOL_REGISTRY: Dict[str, PoolInfo] = {}
for _p in _AERO_VAMM + _V3 + _AERO_CL:
    POOL_REGISTRY[_p.pool_address] = _p


_redis_client: Optional["redis.Redis"] = None


def set_redis(r: "redis.Redis") -> None:
    global _redis_client
    _redis_client = r


def get_redis() -> Optional["redis.Redis"]:
    return _redis_client


def bootstrap_sprint078_pools(r: "redis.Redis") -> int:
    """Ensure sender-gated sprint078 pool metadata is in Redis pool:discovered:*.

    Safe to call on every executor start — seeder is idempotent.
    Returns the number of pools seeded/updated.

    Requires sprint082_registry_seeder.py to be importable from PYTHONPATH or
    the scripts/ directory relative to this service.
    """
    import importlib.util
    import os

    # Look for seeder in adjacent scripts/ directory
    candidates = [
        os.path.join(os.path.dirname(__file__), "../../../scripts/sprint082_registry_seeder.py"),
        "/opt/EATLQ/scripts/sprint082_registry_seeder.py",
    ]
    seeder_path = None
    for p in candidates:
        if os.path.exists(p):
            seeder_path = os.path.abspath(p)
            break

    if seeder_path is None:
        import logging
        logging.getLogger("registry").warning(
            "sprint082_registry_seeder.py not found; skipping sender-gated pool bootstrap"
        )
        return 0

    # Inject the redis URL via env so seeder can connect
    try:
        import logging
        _log = logging.getLogger("registry")
        spec = importlib.util.spec_from_file_location("sprint082_registry_seeder", seeder_path)
        mod = importlib.util.module_from_spec(spec)
        # Temporarily inject redis URL from the live client
        original_url = os.environ.get("REDIS_URL", "")
        try:
            conn_kwargs = r.connection_pool.connection_kwargs
            host = conn_kwargs.get("host", "localhost")
            port = conn_kwargs.get("port", 6379)
            db = conn_kwargs.get("db", 0)
            os.environ["REDIS_URL"] = f"redis://{host}:{port}/{db}"
        except Exception:
            pass
        spec.loader.exec_module(mod)
        mod.main()
        if original_url:
            os.environ["REDIS_URL"] = original_url
        _log.info("bootstrap_sprint078_pools: seeder completed")
        return 49
    except Exception as e:
        import logging
        logging.getLogger("registry").warning("bootstrap_sprint078_pools failed: %s", e)
        return 0


def _lookup_discovered(pool_address: str) -> Optional[PoolInfo]:
    """Try to build PoolInfo from Redis pool:discovered:* hash."""
    if _redis_client is None:
        return None
    key = f"pool:discovered:{pool_address}"
    try:
        raw = _redis_client.hgetall(key)
        if not raw:
            return None
        data: Dict[str, str] = {}
        for k, v in raw.items():
            dk = k.decode() if isinstance(k, bytes) else str(k)
            dv = v.decode() if isinstance(v, bytes) else str(v)
            data[dk] = dv

        if data.get("has_weth_leg") != "1":
            return None

        pt = data.get("pool_type", "v3")
        router = ROUTER_FOR_POOL_TYPE.get(pt)
        if router is None:
            return None

        return PoolInfo(
            pool_address=pool_address,
            pool_type=pt,
            router=router,
            token0=data.get("token0_address", "").lower(),
            token0_symbol=data.get("token0_symbol", "?"),
            token0_decimals=int(data.get("token0_decimals", "18")),
            token1=data.get("token1_address", "").lower(),
            token1_symbol=data.get("token1_symbol", "?"),
            token1_decimals=int(data.get("token1_decimals", "18")),
            fee_bps=int(data.get("fee_bps", "30")),
        )
    except Exception:
        return None


def lookup(pool_address: str) -> Optional[PoolInfo]:
    addr = pool_address.lower().strip()
    static = POOL_REGISTRY.get(addr)
    if static is not None:
        return static
    return _lookup_discovered(addr)
