"""F — deployer / bytecode-cluster blacklist (cheapest pre-trade gate).

Why this exists
---------------
The dominant live threat is delayed LP-pull rugs (B/C reports: 99% of rugs).
A single attacker re-launches the *same* token contract from new pools/EOAs and
pulls liquidity again (the two PARADAENGI tokens that drained us on 2026-06-23
were structurally identical). No static pre-entry gate can prove a *fresh*
deployer is safe, but once ANY pool from a cluster is judged honeypot/rug we can
cheaply block every repeat from the same source — stopping the second, third…
shot of a connected attack.

Two cluster keys, both resolvable on-chain at pre-trade time:

1. ``bytecode`` — an OPCODE-SKELETON hash of the non-WETH token runtime code.
   We strip the trailing Solidity metadata (CBOR) and mask every PUSH operand,
   so two contracts compiled from the same source with different embedded
   constants (owner address, name, immutables) hash IDENTICALLY. This is the
   reliable clone signal: the two PARADAENGI tokens have *different* full
   bytecode but the *same* skeleton hash.
2. ``deployer`` — an EOA known to have shipped a rug. Populated from seeds and
   from abandons when the deployer is known; checked when resolvable.

Fail-open: a Redis hiccup must not block all trading (other gates still run);
a missed block is cheaper than a frozen executor. Every decision is logged.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from eth_utils import keccak
from web3 import Web3

log = logging.getLogger("cluster_blacklist")

BYTECODE_SET = "executor:blacklist:bytecode"   # set of skeleton hashes
DEPLOYER_SET = "executor:blacklist:deployer"   # set of lowercased EOA addrs
SYMBOL_SET = "executor:blacklist:symbol"       # uppercased non-WETH ticker (repeat-rug names)
META_PREFIX = "executor:blacklist:meta:"       # hash per key: reason/ts/sample
DEPLOYER_CACHE_PREFIX = "executor:deployer_cache:"
_DEPLOYER_CACHE_TTL = 86400 * 90
_EXPLORER_URL = os.environ.get("BLOCKSCOUT_URL", "https://base.blockscout.com/api")
_ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
_BASE_CHAIN_ID = 8453
_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_SAFE_RUG_DEPLOYERS_FIXTURE = _FIXTURES / "safe_rug_deployers.json"


def bytecode_skeleton(code: bytes) -> bytes:
    """Opcode skeleton: drop trailing CBOR metadata, then emit opcodes with all
    PUSH operands removed. Identical source ⇒ identical skeleton regardless of
    embedded constants/immutables."""
    b = bytes(code or b"")
    if len(b) > 2:
        mlen = int.from_bytes(b[-2:], "big")
        if 0 < mlen + 2 < len(b):
            b = b[: -(mlen + 2)]
    out = bytearray()
    i, n = 0, len(b)
    while i < n:
        op = b[i]
        out.append(op)
        if 0x60 <= op <= 0x7F:  # PUSH1..PUSH32 — skip the operand bytes
            i += 1 + (op - 0x5F)
        else:
            i += 1
    return bytes(out)


def skeleton_hash_from_code(code: bytes) -> Optional[str]:
    if not code:
        return None
    return "0x" + keccak(bytecode_skeleton(code)).hex()


def _get_code(w3, addr: str) -> bytes:
    try:
        return bytes(w3.eth.get_code(Web3.to_checksum_address(addr)))
    except Exception as e:  # pragma: no cover - network
        log.warning("get_code failed addr=%s: %s", addr, e)
        return b""


def token_skeleton_hash(w3, token_addr: str) -> Optional[str]:
    return skeleton_hash_from_code(_get_code(w3, token_addr))


def _fetch_creator_from_explorer(token: str) -> Optional[str]:
    """Best-effort contract creator via Blockscout (or Basescan V2 if key + paid plan)."""
    token = token.lower()
    api_key = os.environ.get("BASESCAN_API_KEY") or os.environ.get("ETHERSCAN_API_KEY")
    urls = []
    if api_key:
        q = urllib.parse.urlencode({
            "chainid": _BASE_CHAIN_ID,
            "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": token,
            "apikey": api_key,
        })
        urls.append(f"{_ETHERSCAN_V2}?{q}")
    q = urllib.parse.urlencode({
        "module": "contract",
        "action": "getcontractcreation",
        "contractaddresses": token,
    })
    urls.append(f"{_EXPLORER_URL}?{q}")
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "EATLQ-executor/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                j = json.loads(resp.read())
            if j.get("status") == "1" and isinstance(j.get("result"), list) and j["result"]:
                creator = (j["result"][0].get("contractCreator") or "").lower()
                if creator:
                    return creator
        except Exception as e:
            log.debug("creator lookup failed token=%s url=%s: %s", token[:10], url[:40], e)
    return None


def normalize_symbol(symbol: Optional[str]) -> Optional[str]:
    """Uppercase ticker for set lookup. Memecoin names are case-insensitive."""
    if not symbol or symbol in ("?", "WETH", "ETH"):
        return None
    s = str(symbol).strip().upper()
    return s or None


def is_symbol_blacklisted(r, symbol: Optional[str]) -> Tuple[bool, str]:
    if r is None:
        return False, "no_redis"
    sym = normalize_symbol(symbol)
    if not sym:
        return False, "ok"
    try:
        if r.sismember(SYMBOL_SET, sym):
            return True, f"symbol_blacklist:{sym}"
    except Exception as e:
        log.warning("symbol blacklist check failed (fail-open) sym=%s: %s", sym, e)
        return False, "check_error"
    return False, "ok"


def resolve_token_deployer(w3, token_addr: str, r=None) -> Optional[str]:
    """Resolve token deployer EOA; Redis cache + explorer. Fail-open."""
    token = (token_addr or "").lower()
    if not token:
        return None
    cache_key = DEPLOYER_CACHE_PREFIX + token
    if r is not None:
        try:
            cached = r.get(cache_key)
            if cached:
                return cached.decode() if isinstance(cached, bytes) else str(cached)
        except Exception:
            pass
    creator = _fetch_creator_from_explorer(token)
    if creator and r is not None:
        try:
            r.setex(cache_key, _DEPLOYER_CACHE_TTL, creator)
        except Exception:
            pass
    return creator


def is_blacklisted(r, w3, pool_info, deployer: Optional[str] = None) -> Tuple[bool, str]:
    """Return (blocked, reason). Symbol name, bytecode skeleton, deployer — in
    that cheap-to-expensive order. Fail-open on Redis errors."""
    if r is None:
        return False, "no_redis"
    try:
        blocked, reason = is_symbol_blacklisted(r, getattr(pool_info, "non_weth_symbol", None))
        if blocked:
            return True, reason
        token = pool_info.non_weth_token
        dep = deployer or resolve_token_deployer(w3, token, r)
        if dep:
            d = dep.lower()
            if r.sismember(DEPLOYER_SET, d):
                return True, f"deployer_blacklist:{d[:10]}"
        skel = token_skeleton_hash(w3, token)
        if skel and r.sismember(BYTECODE_SET, skel):
            return True, f"bytecode_cluster:{skel[:14]}"
    except Exception as e:
        log.warning("blacklist check failed (fail-open) pool=%s: %s",
                    getattr(pool_info, "pool_address", "?"), e)
        return False, "check_error"
    return False, "ok"


def record(r, w3, pool_info, reason: str, deployer: Optional[str] = None) -> dict:
    """Add a cluster to the blacklist. Called on any honeypot/rug/ABANDON.
    Returns the keys recorded (for logging/tests). Fail-open."""
    recorded = {"bytecode": None, "deployer": None, "symbol": None}
    if r is None:
        return recorded
    ts = int(time.time())
    try:
        token = pool_info.non_weth_token
        sym = normalize_symbol(getattr(pool_info, "non_weth_symbol", None))
        if sym:
            r.sadd(SYMBOL_SET, sym)
            r.hset(META_PREFIX + "sym:" + sym, mapping={
                "reason": reason, "ts": ts,
                "sample_pool": getattr(pool_info, "pool_address", ""),
                "sample_token": token,
            })
            recorded["symbol"] = sym
        skel = token_skeleton_hash(w3, token)
        if skel:
            r.sadd(BYTECODE_SET, skel)
            r.hset(META_PREFIX + skel, mapping={
                "reason": reason, "ts": ts,
                "sample_pool": getattr(pool_info, "pool_address", ""),
                "sample_token": token,
            })
            recorded["bytecode"] = skel
        if deployer:
            d = deployer.lower()
            r.sadd(DEPLOYER_SET, d)
            r.hset(META_PREFIX + d, mapping={"reason": reason, "ts": ts,
                                             "sample_pool": getattr(pool_info, "pool_address", "")})
            recorded["deployer"] = d
        log.error("CLUSTER BLACKLIST add pool=%s reason=%s symbol=%s bytecode=%s deployer=%s",
                  getattr(pool_info, "pool_address", "?"), reason,
                  recorded["symbol"], recorded["bytecode"], recorded["deployer"])
    except Exception as e:
        log.warning("blacklist record failed (fail-open) pool=%s: %s",
                    getattr(pool_info, "pool_address", "?"), e)
    return recorded


def add_skeleton_hash(r, skel_hash: str, reason: str, sample: str = "") -> None:
    """Seed a known skeleton hash directly (no chain call needed)."""
    if r is None or not skel_hash:
        return
    r.sadd(BYTECODE_SET, skel_hash)
    r.hset(META_PREFIX + skel_hash, mapping={"reason": reason, "ts": int(time.time()), "sample_pool": sample})


def add_deployer(r, deployer: str, reason: str) -> None:
    if r is None or not deployer:
        return
    r.sadd(DEPLOYER_SET, deployer.lower())
    r.hset(META_PREFIX + deployer.lower(), mapping={"reason": reason, "ts": int(time.time())})


def add_symbol(r, symbol: str, reason: str, sample_pool: str = "") -> None:
    sym = normalize_symbol(symbol)
    if r is None or not sym:
        return
    r.sadd(SYMBOL_SET, sym)
    r.hset(META_PREFIX + "sym:" + sym, mapping={
        "reason": reason, "ts": int(time.time()), "sample_pool": sample_pool,
    })


# Seed cluster from the 2026-06-23 PARADAENGI incident (same skeleton, 2 EOAs).
# Skeleton hash captured on-chain; both tokens share it despite different full code.
PARADAENGI_SKELETON = "0xa7df50c84afb0036133e169f94babbda439bc8ef4af178deff5a00cf470c3f25"
PARADAENGI_TOKENS = (
    "0x6c8a7e7f4574dca6f0e5ca8e6772d27cc53ead38",
    "0x56c6ee3498ca94b7cd2fc363e465b75828f12738",
)
PARADAENGI_DEPLOYERS = (
    "0x715c98cedbd0c242e257986535bbf6e1fba764be",
    "0xc8512b5324402be9ab986d011c9adef8992f076f",
    # 2026-06-29 third wave (same skeleton, new EOA)
    "0x1c80fcf62dab78142c2918f7d9819d1aa40661f9",
)
PARADAENGI_SYMBOL = "PARADAENGI"

# 33 opcode-skeleton hashes validated rug-only vs 378 clean winners (0 FP in labeled set).
# Covers 100/262 historical rugs (38.2%). See cluster_blacklist_fp_validation_20260624/.
SAFE_RUG_SKELETON_SEEDS = (
    "0x00b6ae8bc5129d7dbced61330780d4ec049794c9a41a9023f04d1c0adacc2d79",
    "0x2376a814b5ad21fd1188c0998a698028088fca3d9a146a7406fffe978f8685fe",
    "0x2be340ff91dab2b6edc87318d3508846bf392770a1a5611e368c1dc08258376f",
    "0x2e7f2922ec035b7d7c77a16a4b05729cab80c7a978ceba3d4038563021bc7db8",
    "0x3f18793891784fdbab9641139a3709585bf80a6ad93b4fb9a24f08cb30e5e685",
    "0x419db8d9ea3c34c28cd49dfe4825f8c82f9777a7717209df7caa83e88f4bc09d",
    "0x518929dfb605ef91b07c48147ecd350e3d294be35cadf60194270d5c7724fb6e",
    "0x574970a9db8779f7b233ea776b8f6627995c66d74d95ef9321bff099c099670c",
    "0x62ead82d8fa1d044af009c646de4db286fc66bfa6e327b2218b2d6385df0aaff",
    "0x675678725e9097a26b5af1ac3d45e33d8b4c62b777bfa573678cc8b74ef8e0ad",
    "0x842f4791e746a8c99a06d0cbc875c979c9919f32883cb7f74be74c4bd5b3ba73",
    "0x87f3b0ee13831662fad5196b14e057eea76e3d8fd2ef6e4ec2e49f7e8c1cdb60",
    "0x8a90ff4f7d335f7d6569726ba9044b49a4e86654c757f5f1856fd260020c039d",
    "0x8b98958f532adb343e99ef917b5f2296112b4559e31ccc456815d23ebf49be3f",
    "0x9023f569753e510c8ae5d16f41831f231efbfb87316936b278cf2aae7ff81ec4",
    "0x915d77dd67d8fb0a56b35c546f09270b5857782a15ca2805c468b35ce62eaed2",
    "0x97c6fa384ca4316382f82b12c6e818d65094b5c26e41e3434ea21a831fa9953a",
    "0xa4d8f61ecc13010502a33450f2487af970b5c445378ee5a46327ea191ea89e1c",
    "0xb2804216be637074d8cd028f722ddec808d9c058523036133d88aee28d5edafa",
    "0xb2904b2c9a64da26e41672c9356fc28543f093bd8245b93a7181b3a955a17b17",
    "0xb777f98e7e03777cc6d5471ab5c54bac7ef610c95a0d6034be81f6196cae1d1d",
    "0xd5b2062748804745001bc96b0a6f2785e93644238ccc3a3f10744f55c489e638",
    "0xd7cace3d9738e756d3c7062eb5052f670b3a3c3cede7cd8d0fc4065ac03bcec3",
    "0xe223a72ef696528a8339ea11f1130d6c4013b9db83b45840cf9d1c737ee5a9d0",
    "0xe787f8c217407db5629af8cdcefcb2c70b31e8238284f1f0f2f3c65915a93650",
    "0xecd3e37930e8ae3d911fddf2457b74b70f6b2173e9fe7be92a3dcdc4e3c3e637",
    "0xf0a43de8e3a18993c63a3dc2fae86fbf78f2ee19eddaa04de245d5e8d15968ab",
    "0xf120fa3e883983c7b265d702a745405a210639c5dd933dd478a4a95547306bbd",
    "0xf1bd84e9601494b5a49cdeb4e673d563364f88162e971cabfb30e50e13f28b52",
    "0xf27ad2309d36a92d27d40c237b1a0b690e244ee37743d50c91279db524a4a035",
    "0xf576bad71edcb9e920be75ca0668f517c70d84eb35693055d1d875ec5d08affc",
    "0xf725c23838e9d38e0e9377d691d1b5258643d04e7ff1e51a2ec5deb22e31f3c5",
    "0xffadf50a987e81a0a84b03098b0890876fdd066b4cf1e90e420c6ff4d48b7ffc",
)


def seed_paradaengi(r, w3=None, skeleton_hash: Optional[str] = None) -> dict:
    """Seed PARADAENGI repeat-rug cluster: symbol + skeleton + all known deployers."""
    seeded = {"bytecode": None, "deployers": [], "symbol": None}
    if r is None:
        return seeded
    add_symbol(r, PARADAENGI_SYMBOL, "paradaengi_seed_20260623_symbol")
    seeded["symbol"] = PARADAENGI_SYMBOL
    # Re-seed skeleton: three live hits (6/23×2 + 6/29) proved repeat > FP cost.
    add_skeleton_hash(r, PARADAENGI_SKELETON, "paradaengi_seed_20260629_repeat_confirmed")
    seeded["bytecode"] = PARADAENGI_SKELETON
    for d in PARADAENGI_DEPLOYERS:
        add_deployer(r, d, "paradaengi_seed_deployer")
        seeded["deployers"].append(d.lower())
    log.error(
        "seeded PARADAENGI cluster symbol=%s skeleton=%s deployers=%s",
        PARADAENGI_SYMBOL, PARADAENGI_SKELETON[:14], list(PARADAENGI_DEPLOYERS),
    )
    return seeded


def seed_safe_rug_skeletons(r) -> int:
    """Seed 33 FP-validated rug skeletons (38.2% historical rug coverage, 0 clean overlap)."""
    if r is None:
        return 0
    reason = "rug_skeleton_seed_20260624_fp_validated"
    for skel in SAFE_RUG_SKELETON_SEEDS:
        add_skeleton_hash(r, skel, reason)
    n = len(SAFE_RUG_SKELETON_SEEDS)
    log.error("seeded %d safe rug skeletons (38.2%% historical rug coverage, 0 FP in labeled set)", n)
    return n


def seed_safe_rug_deployers(r) -> int:
    """Seed FP-validated rug deployer EOAs from fixture (644-set zero clean overlap)."""
    if r is None:
        return 0
    if not _SAFE_RUG_DEPLOYERS_FIXTURE.exists():
        log.warning("safe rug deployers fixture missing: %s", _SAFE_RUG_DEPLOYERS_FIXTURE)
        return 0
    data = json.loads(_SAFE_RUG_DEPLOYERS_FIXTURE.read_text())
    reason = data.get("meta", {}).get("reason", "rug_deployer_seed_20260624_fp_validated")
    deployers = data.get("deployers", [])
    for d in deployers:
        add_deployer(r, d, reason)
    n = len(deployers)
    cov = data.get("meta", {}).get("rug_token_coverage", {})
    log.error("seeded %d safe rug deployers (%s/%s rug tokens, 0 FP in labeled set)",
              n, cov.get("rug_tokens_with_safe_deployer", "?"), cov.get("rug_tokens_total", "?"))
    return n


# ── Experiment toggle (2026-06-25): surgically REMOVE only the historical LP-pull
# rug seed (247 deployers + 33 skeletons) while KEEPING the 2 PARADAENGI honeypot
# deployer seeds and any runtime-learned (abandon-recorded) entries untouched.
# Fully reversible: re-run the seed_* functions to restore. Used to test "selling
# before an LP-pull has alpha" — see go-live aggressive-variant 2026-06-25.
def unseed_safe_rug_skeletons(r) -> int:
    """Remove the 33 historical rug skeleton seeds. Leaves all other bytecode
    cluster entries (runtime abandons) intact. Reversible via seed_safe_rug_skeletons."""
    if r is None:
        return 0
    removed = 0
    for skel in SAFE_RUG_SKELETON_SEEDS:
        try:
            if r.srem(BYTECODE_SET, skel):
                removed += 1
                r.delete(META_PREFIX + skel)
        except Exception as e:
            log.warning("unseed skeleton failed %s: %s", skel[:14], e)
    log.error("UNSEEDED %d/%d historical rug skeletons (experiment: F LP-pull list off)",
              removed, len(SAFE_RUG_SKELETON_SEEDS))
    return removed


def unseed_safe_rug_deployers(r) -> int:
    """Remove the fixture rug deployer EOAs. Explicitly PRESERVES the 2 PARADAENGI
    honeypot deployer seeds and any runtime-learned entries. Reversible."""
    if r is None:
        return 0
    if not _SAFE_RUG_DEPLOYERS_FIXTURE.exists():
        return 0
    data = json.loads(_SAFE_RUG_DEPLOYERS_FIXTURE.read_text())
    deployers = data.get("deployers", [])
    keep = {d.lower() for d in PARADAENGI_DEPLOYERS}  # never remove honeypot seeds
    removed = 0
    for d in deployers:
        dl = (d or "").lower()
        if dl in keep:
            continue
        try:
            if r.srem(DEPLOYER_SET, dl):
                removed += 1
                r.delete(META_PREFIX + dl)
        except Exception as e:
            log.warning("unseed deployer failed %s: %s", dl[:10], e)
    log.error("UNSEEDED %d historical rug deployers (experiment: F LP-pull list off; PARADAENGI honeypot seeds KEPT)",
              removed)
    return removed
