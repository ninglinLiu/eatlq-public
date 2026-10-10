"""
S005 — Deconcentrate + On-Chain Repair Sprint
==============================================
Sub-tasks (all three run in sequence):
  A. Re-run S004 actionable events with OKX 1m focus on ≥2026 events
  B. De-concentrated sensitivity: exclude TOP_TOKEN, report residual metrics
  C. Targeted on-chain repair: Base RPC → Swap events → 1m price series → backtest

No X API. No live trading. No executor. No private keys.
"""
from __future__ import annotations

import csv, json, os, re, sys, time, importlib.util
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from statistics import mean, median
from typing import Any
import urllib.request, urllib.error

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
S003_DIR  = REPO_ROOT / "mvp" / "audits" / "social_catalyst_s003"
S004_DIR  = REPO_ROOT / "mvp" / "audits" / "social_catalyst_s004"
S005_DIR  = REPO_ROOT / "mvp" / "audits" / "social_catalyst_s005"
S005_DIR.mkdir(parents=True, exist_ok=True)

S004_INTENT  = S004_DIR / "post_intent_labels.csv"
S004_CLEAN   = S004_DIR / "clean_actionable_events.csv"
S004_GRID    = S004_DIR / "clean_event_return_grid.csv"
S003_POSTS   = S003_DIR / "raw_kol_posts.csv"
S003_EVENTS  = S003_DIR / "resolved_social_events.csv"
S003_COV     = S003_DIR / "data_source_coverage.csv"
S003_GRID    = S003_DIR / "event_return_grid.csv"

OUT_A_GRID   = S005_DIR / "s005a_1m_backtest_grid.csv"
OUT_A_SUM    = S005_DIR / "s005a_1m_policy_summary.csv"
OUT_B        = S005_DIR / "s005b_deconcentrated_metrics.csv"
OUT_C_SERIES = S005_DIR / "s005c_onchain_price_series.csv"
OUT_C_GRID   = S005_DIR / "s005c_repaired_backtest_grid.csv"
OUT_C_SUM    = S005_DIR / "s005c_repaired_policy_summary.csv"
OUT_COMBINED = S005_DIR / "s005_combined_readout.md"
OUT_FLAGS    = S005_DIR / "decision_flags.json"

# ---------------------------------------------------------------------------
# Grid constants (same as S003/S004)
# ---------------------------------------------------------------------------
ENTRY_DELAYS   = [0, 1, 3, 5, 10, 15, 30]
EXIT_AFTERS    = [5, 15, 30, 60, 120]
FIXED_POLICIES = {
    "policy_fast": (1, 15), "policy_mid": (5, 30),
    "policy_slow": (15, 60), "policy_late": (30, 120),
}
HOURLY_SOURCES = ("okx_candles_1h", "gecko_terminal_1h", "s3_fallback_1h")

TOP_TOKEN_ADDR  = "0x884b59ac68b03d86d9a339ce7a4b6ea4d86b074f"   # LFI
BASE_RPC_URL    = os.environ.get("BASE_RPC_URL",
    "https://prettiest-blue-aura.base-mainnet.quiknode.pro/"
    "0123456789abcdef0123456789abcdef01234567/")
WETH_BASE       = "0x4200000000000000000000000000000000000006"
USDC_BASE       = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

# V3 Swap(address,address,int256,int256,uint160,uint128,int24)
TOPIC_V3_SWAP   = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
# V2 Sync(uint112,uint112)
TOPIC_V2_SYNC   = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"

RPC_SLEEP       = 0.08   # between RPC calls
LOG_CHUNK       = 500    # blocks per eth_getLogs call
BINARY_SEARCH_STEPS = 18

# ---------------------------------------------------------------------------
# Load S002
# ---------------------------------------------------------------------------
def _load_s002():
    p    = REPO_ROOT / "scripts" / "sprint_s002_base_social_market_data_scout.py"
    name = "eatlq_s002_market"
    spec = importlib.util.spec_from_file_location(name, str(p))
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)   # type: ignore
    return mod

S002 = _load_s002()
S002.CAP_OKX_HARD = 2500
S002.CAP_OKX_SOFT = 2400
S002.CAP_GECKO_SOFT = 300
S002.CAP_PER_TOKEN_OKX = 25

# ===========================================================================
# Base RPC helpers
# ===========================================================================
_rpc_id = 0
_block_ts_cache: dict[int, int] = {}

def _rpc(method: str, params: list) -> Any:
    global _rpc_id
    _rpc_id += 1
    payload = json.dumps({"jsonrpc": "2.0", "method": method,
                          "params": params, "id": _rpc_id}).encode()
    req = urllib.request.Request(
        BASE_RPC_URL, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "EATLQ/S005"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            doc = json.loads(resp.read())
            time.sleep(RPC_SLEEP)
            return doc.get("result")
    except Exception as e:
        time.sleep(0.5)
        return None


def _hex_to_int(h: str) -> int:
    if not h:
        return 0
    return int(h, 16)


def _to_hex_block(n: int) -> str:
    return hex(n)


def get_latest_block() -> int:
    r = _rpc("eth_blockNumber", [])
    return _hex_to_int(r) if r else 0


def get_block_timestamp(block_n: int) -> int:
    if block_n in _block_ts_cache:
        return _block_ts_cache[block_n]
    r = _rpc("eth_getBlockByNumber", [_to_hex_block(block_n), False])
    if r and "timestamp" in r:
        ts = _hex_to_int(r["timestamp"])
        _block_ts_cache[block_n] = ts
        return ts
    return 0


def block_for_timestamp(target_ts: int, lo: int, hi: int) -> int:
    """Binary-search for the block number whose timestamp is closest to target_ts."""
    for _ in range(BINARY_SEARCH_STEPS):
        if hi <= lo + 1:
            break
        mid = (lo + hi) // 2
        ts  = get_block_timestamp(mid)
        if ts == 0:
            break
        if ts < target_ts:
            lo = mid
        else:
            hi = mid
    return lo


def erc20_decimals(addr: str) -> int:
    """Call decimals() on an ERC-20 contract."""
    r = _rpc("eth_call", [{"to": addr, "data": "0x313ce567"}, "latest"])
    if r:
        try:
            return _hex_to_int(r)
        except Exception:
            pass
    return 18   # default


def get_token0(pool_addr: str) -> str:
    r = _rpc("eth_call", [{"to": pool_addr, "data": "0x0dfe1681"}, "latest"])
    if r and len(r) >= 42:
        return "0x" + r[-40:]
    return ""


def get_token1(pool_addr: str) -> str:
    r = _rpc("eth_call", [{"to": pool_addr, "data": "0xd21220a7"}, "latest"])
    if r and len(r) >= 42:
        return "0x" + r[-40:]
    return ""


def eth_get_logs(from_block: int, to_block: int,
                 address: str, topics: list) -> list:
    params = [{"fromBlock": _to_hex_block(from_block),
               "toBlock":   _to_hex_block(to_block),
               "address":   address,
               "topics":    topics}]
    r = _rpc("eth_getLogs", params)
    return r if isinstance(r, list) else []


# ===========================================================================
# Price reconstruction from Swap / Sync logs
# ===========================================================================

def _signed_int256(h: str) -> int:
    v = int(h, 16)
    if v >= 2**255:
        v -= 2**256
    return v


def decode_v3_price(log: dict, token_is_token0: bool,
                    dec0: int, dec1: int) -> float | None:
    """Extract price from a V3 Swap log. Returns price in terms of quote/our_token."""
    # data layout (no indexed fields in data for V3):
    # amount0 (int256), amount1 (int256), sqrtPriceX96 (uint160), liquidity (uint128), tick (int24)
    data = log.get("data", "")
    if not data or data == "0x":
        return None
    raw = data[2:]  # strip 0x
    if len(raw) < 5 * 64:
        return None
    try:
        sqrt_px96 = int(raw[128:192], 16)   # 3rd 32-byte word
        if sqrt_px96 == 0:
            return None
        # price = (sqrtPriceX96 / 2^96)^2 in units of token1_per_token0 (raw, no decimal adjust)
        # adjusted: price_token1_per_token0 = (sqrt^2 / 2^192) * 10^dec0 / 10^dec1
        price_raw = (sqrt_px96 / (2**96)) ** 2
        price_adj = price_raw * (10**dec0) / (10**dec1)
        if token_is_token0:
            # our token is token0, quote (WETH/USDC) is token1
            # price of our token in terms of quote = price_adj
            return float(price_adj)
        else:
            # our token is token1
            return float(1.0 / price_adj) if price_adj > 0 else None
    except Exception:
        return None


def decode_v2_price(log: dict, token_is_token0: bool,
                    dec0: int, dec1: int) -> float | None:
    """Extract price from a V2 Sync log."""
    data = log.get("data", "")
    if not data or data == "0x":
        return None
    raw = data[2:]
    if len(raw) < 128:
        return None
    try:
        r0 = int(raw[:64], 16)
        r1 = int(raw[64:128], 16)
        if r0 == 0 or r1 == 0:
            return None
        # price_of_token0_in_token1 = (r1/10^dec1) / (r0/10^dec0)
        price_adj = (r1 / 10**dec1) / (r0 / 10**dec0)
        if token_is_token0:
            return float(price_adj)
        else:
            return float(1.0 / price_adj) if price_adj > 0 else None
    except Exception:
        return None


def logs_to_1m_series(logs_with_ts: list[tuple[int, float]]) -> list[tuple[int, float, float]]:
    """
    Convert (timestamp_ms, price) list to 1-minute OHLCV-style (ts_ms, close, volume).
    ts_ms is the open of each minute bar.
    """
    if not logs_with_ts:
        return []
    by_min: dict[int, list[float]] = defaultdict(list)
    for ts, px in logs_with_ts:
        minute_key = (ts // 60_000) * 60_000
        by_min[minute_key].append(px)
    series = []
    for ms, prices in sorted(by_min.items()):
        series.append((ms, prices[-1], float(len(prices))))
    return series


# ===========================================================================
# Pool discovery via DexScreener
# ===========================================================================

def find_pool_dex(budget: Any, token_addr: str) -> tuple[str, str]:
    """Return (pool_address, pool_type). pool_type in 'v3','v2','unknown'."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}"
    doc, err = S002.http_json_public(budget, "dex", url)
    if err or not doc:
        return "", "unknown"
    pairs = doc.get("pairs") or []
    # Prefer Base chain, highest liquidity
    base_pairs = [p for p in pairs if (p.get("chainId") or "").lower() in ("base", "8453")]
    if not base_pairs:
        return "", "unknown"
    # Sort by liquidity
    base_pairs.sort(key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0), reverse=True)
    best = base_pairs[0]
    pool_addr = best.get("pairAddress", "")
    dex_id    = (best.get("dexId") or "").lower()
    ptype = "v3" if "v3" in dex_id else "v2"
    return pool_addr, ptype


# ===========================================================================
# On-chain 1m series builder
# ===========================================================================

def build_onchain_series(
    token_addr: str,
    pool_addr: str,
    t_min_ms: int,
    t_max_ms: int,
    budget: Any,
    rpc_budget: list,  # [calls_used]
) -> tuple[list[tuple[int, float, float]], str, str]:
    """
    Fetch Swap/Sync events from Base chain for [t_min_ms, t_max_ms] and build 1m series.
    Returns (series, source_label, error_msg).
    """
    t_min_s = t_min_ms // 1000
    t_max_s = t_max_ms // 1000

    # Find block range via binary search
    # Estimate starting bounds using ~2s Base block time to avoid searching wrong era
    latest = get_latest_block()
    rpc_budget[0] += 1
    if not latest:
        return [], "", "RPC_FAILED"

    latest_ts = get_block_timestamp(latest)
    rpc_budget[0] += 1
    if not latest_ts:
        return [], "", "RPC_FAILED"

    BASE_BLOCK_TIME = 2  # seconds per block on Base
    # Estimated delta in blocks from latest to t_min
    est_delta_min = max(0, int((latest_ts - t_min_s) / BASE_BLOCK_TIME))
    est_delta_max = max(0, int((latest_ts - t_max_s) / BASE_BLOCK_TIME))
    # Use generous ±20% margin
    search_lo = max(0, latest - int(est_delta_min * 1.20) - 1_000)
    search_hi = min(latest, latest - int(est_delta_max * 0.80) + 1_000)
    if search_hi <= search_lo:
        search_lo = max(0, latest - int(est_delta_min * 1.5))
        search_hi = latest

    b_lo = block_for_timestamp(t_min_s, search_lo, search_hi)
    rpc_budget[0] += BINARY_SEARCH_STEPS
    b_hi = block_for_timestamp(t_max_s, b_lo, min(b_lo + 40_000, search_hi))
    rpc_budget[0] += BINARY_SEARCH_STEPS

    if b_lo <= 0 or b_hi <= b_lo:
        return [], "", "BLOCK_RANGE_INVALID"

    # Identify token ordering in the pool
    t0 = get_token0(pool_addr)
    t1 = get_token1(pool_addr)
    rpc_budget[0] += 2
    if not t0 and not t1:
        return [], "", "POOL_TOKEN_QUERY_FAILED"

    token_is_t0 = t0.lower() == token_addr.lower()
    # Determine pool type by trying V3 swap signature first
    # We'll try both; if V3 Swap events found → v3; else try V2 Sync
    dec0 = erc20_decimals(t0) if t0 else 18
    dec1 = erc20_decimals(t1) if t1 else 18
    rpc_budget[0] += 2

    logs_ts: list[tuple[int, float]] = []
    source_label = ""

    def _logs_to_ts_prices(logs_list, decode_fn):
        """Convert list of logs to (ts_ms, price) using cached block timestamps."""
        result = []
        # Collect unique block numbers first
        blks_needed = set()
        log_prices = []
        for log in logs_list:
            px = decode_fn(log, token_is_t0, dec0, dec1)
            if px and px > 0:
                blk = _hex_to_int(log.get("blockNumber", "0"))
                blks_needed.add(blk)
                log_prices.append((blk, px))
        # Fetch timestamps for uncached blocks
        for blk in blks_needed:
            if blk not in _block_ts_cache:
                ts = get_block_timestamp(blk)
                rpc_budget[0] += 1
                if ts:
                    _block_ts_cache[blk] = ts
        # Build (ts_ms, price) pairs
        for blk, px in log_prices:
            ts = _block_ts_cache.get(blk, 0)
            if ts:
                result.append((ts * 1000, px))
        return result

    # Collect all logs first (V3)
    all_v3_logs = []
    for chunk_lo in range(b_lo, b_hi + 1, LOG_CHUNK):
        chunk_hi = min(chunk_lo + LOG_CHUNK - 1, b_hi)
        logs = eth_get_logs(chunk_lo, chunk_hi, pool_addr, [TOPIC_V3_SWAP])
        rpc_budget[0] += 1
        if logs:
            all_v3_logs.extend(logs)

    logs_ts = _logs_to_ts_prices(all_v3_logs, decode_v3_price)
    if logs_ts:
        source_label = "base_v3_swap"
    else:
        # Try V2 Sync events
        all_v2_logs = []
        for chunk_lo in range(b_lo, b_hi + 1, LOG_CHUNK):
            chunk_hi = min(chunk_lo + LOG_CHUNK - 1, b_hi)
            logs = eth_get_logs(chunk_lo, chunk_hi, pool_addr, [TOPIC_V2_SYNC])
            rpc_budget[0] += 1
            if logs:
                all_v2_logs.extend(logs)
        logs_ts = _logs_to_ts_prices(all_v2_logs, decode_v2_price)
        if logs_ts:
            source_label = "base_v2_sync"

    if not logs_ts:
        return [], "", "NO_EVENTS_FOUND"

    logs_ts.sort(key=lambda x: x[0])
    series = logs_to_1m_series(logs_ts)
    return series, source_label, ""


# ===========================================================================
# Shared backtest helpers (same logic as S004)
# ===========================================================================

def price_at_or_before(series, ts_ms: int):
    best = None
    for t, c, _ in series:
        if t <= ts_ms:
            best = c
        elif t > ts_ms:
            break
    return best


def slice_forward(series, start_ms: int, duration_ms: int):
    end_ms = start_ms + duration_ms
    return [(t, c, v) for t, c, v in series if start_ms <= t <= end_ms]


def series_covers_window(series, t_min: int, t_max: int) -> bool:
    if not series:
        return False
    first, last = series[0][0], series[-1][0]
    return first <= t_min + 2 * 3_600_000 and last >= t_max - 2 * 3_600_000


def grid_row(ev: dict, series: list, source: str, ed: int, xa: int) -> dict:
    ts_str = ev.get("post_time_utc", "")
    skip = ""
    t0 = 0
    try:
        tsi = ts_str.replace("Z", "+00:00") if ts_str.endswith("Z") else ts_str
        t0 = int(datetime.fromisoformat(tsi).astimezone(UTC).timestamp() * 1000)
    except ValueError:
        skip = "BAD_TIMESTAMP"
    if not skip and source in HOURLY_SOURCES and xa < 60:
        skip = "HOURLY_RESOLUTION_SHORT_WINDOW_SKIP"
    base = {
        "author": ev.get("author", ""), "post_id": ev.get("post_id", ""),
        "post_time_utc": ts_str, "token_symbol": ev.get("token_symbol", ""),
        "token_address": ev.get("token_address", ""),
        "intent_label": ev.get("intent_label", ""),
        "data_source_used": source,
        "data_resolution": "1m" if "1m" in source or source.endswith("_swap") or source.endswith("_sync") else "1h",
        "entry_delay_min": ed, "exit_after_min": xa,
        "entry_price": "", "exit_price": "", "return_pct": "",
        "max_drawdown_before_exit": "", "max_runup_before_exit": "",
        "skip_reason": skip,
    }
    if skip:
        return base
    te = t0 + ed * 60_000
    tx = te + xa * 60_000
    ep = price_at_or_before(series, te)
    xp = price_at_or_before(series, tx)
    if not ep or ep <= 0:
        base["skip_reason"] = "NO_ENTRY_PRICE"; return base
    if not xp or xp <= 0:
        base["skip_reason"] = "NO_EXIT_PRICE"; return base
    fwd = slice_forward(series, te, xa * 60_000)
    closes = [c for _, c, _ in fwd]
    base.update({
        "entry_price": round(ep, 10), "exit_price": round(xp, 10),
        "return_pct": round((xp - ep) / ep, 6),
        "max_drawdown_before_exit": round((min(closes) - ep) / ep if closes else 0.0, 6),
        "max_runup_before_exit":    round((max(closes) - ep) / ep if closes else 0.0, 6),
    })
    return base


def run_grid(events: list[dict], cache: dict[str, tuple]) -> list[dict]:
    rows = []
    for ev in events:
        addr = ev.get("token_address", "").lower().strip()
        if addr not in cache:
            rows.append({**{k: ev.get(k,"") for k in ["author","post_id","post_time_utc",
                "token_symbol","token_address","intent_label"]},
                "data_source_used":"","data_resolution":"","entry_delay_min":0,"exit_after_min":5,
                "entry_price":"","exit_price":"","return_pct":"","max_drawdown_before_exit":"",
                "max_runup_before_exit":"","skip_reason":"NO_PRICE_SERIES"})
            continue
        series, source = cache[addr]
        for ed, xa in product(ENTRY_DELAYS, EXIT_AFTERS):
            rows.append(grid_row(ev, series, source, ed, xa))
    return rows


GRID_COLS = ["author","post_id","post_time_utc","token_symbol","token_address",
             "intent_label","data_source_used","data_resolution",
             "entry_delay_min","exit_after_min","entry_price","exit_price","return_pct",
             "max_drawdown_before_exit","max_runup_before_exit","skip_reason"]


def _pf(rets: list) -> str:
    g = sum(x for x in rets if x > 0)
    l = abs(sum(x for x in rets if x < 0))
    return "" if not rets else ("inf" if l == 0 else str(round(g/l,4)))


def agg(rows: list[dict], label: str) -> dict:
    rets = [float(r["return_pct"]) for r in rows if str(r.get("return_pct","")).strip()]
    n = len(rets)
    if n == 0:
        return {"label":label,"n":0,"hit_rate":"","mean_return":"","median_return":"",
                "profit_factor":"","max_mdd":"","best":"","worst":"","data_resolution":""}
    src = Counter(r.get("data_resolution","") for r in rows if str(r.get("return_pct","")).strip())
    mdd = [float(r["max_drawdown_before_exit"]) for r in rows
           if str(r.get("max_drawdown_before_exit","")).strip()]
    return {
        "label": label, "n": n,
        "hit_rate":    round(sum(1 for x in rets if x>0)/n, 4),
        "mean_return": round(mean(rets), 6),
        "median_return": round(median(rets), 6),
        "profit_factor": _pf(rets),
        "max_mdd": round(mean(mdd),6) if mdd else "",
        "best": round(max(rets),6), "worst": round(min(rets),6),
        "data_resolution": ",".join(k for k,_ in src.most_common()),
    }


def dedupe_valid(rows: list[dict]) -> list[dict]:
    seen: dict = {}
    for r in rows:
        k = (str(r.get("post_id")), str(r.get("token_address","")).lower())
        seen.setdefault(k, r)
    return list(seen.values())


def summaries_for(rows: list[dict], prefix: str) -> list[dict]:
    valid = [r for r in rows if not str(r.get("skip_reason","")).strip()]
    out   = []
    out.append({**agg(valid, f"{prefix}_overall"), "entry_delay_min":"", "exit_after_min":"", "policy":""})
    for ed, xa in product(ENTRY_DELAYS, EXIT_AFTERS):
        cell = [r for r in valid if str(r.get("entry_delay_min"))==str(ed)
                and str(r.get("exit_after_min"))==str(xa)]
        if len(cell) >= 3:
            out.append({**agg(cell, f"{prefix}_grid_{ed}_{xa}"),
                        "entry_delay_min": ed, "exit_after_min": xa, "policy": ""})
    for pol,(ed,xa) in FIXED_POLICIES.items():
        cell = [r for r in valid if str(r.get("entry_delay_min"))==str(ed)
                and str(r.get("exit_after_min"))==str(xa)]
        out.append({**agg(cell, f"{prefix}_{pol}"),
                    "entry_delay_min": ed, "exit_after_min": xa, "policy": pol})
    return out


SUM_COLS = ["label","policy","entry_delay_min","exit_after_min","n","hit_rate",
            "mean_return","median_return","profit_factor","max_mdd","best","worst","data_resolution"]


def _csv(path: Path, cols: list, rows: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k,"") for k in cols})


# ===========================================================================
# OKX paginated candles (same semantics as S003/S004)
# ===========================================================================

def _okx_paged(client, addr, bar, used, t_min, t_max, pages=4):
    all_rows = []
    after_ms = t_max
    for _ in range(pages):
        if used.get(addr,0) >= S002.CAP_PER_TOKEN_OKX or not client.budget.okx_ok():
            break
        rows, err = S002.okx_candles(client, addr, bar=bar, limit="300",
                                      before_ms=t_min, after_ms=after_ms)
        used[addr] = used.get(addr,0) + 1
        if err or not rows:
            break
        all_rows.extend(rows)
        oldest = min(int(r[0]) for r in rows)
        if oldest <= t_min:
            break
        after_ms = oldest
    return S002.candles_to_series(all_rows)


def build_cache_okx_1m(events: list[dict], client, budget, used: dict) -> dict:
    by_addr: dict[str, list[int]] = {}
    for ev in events:
        addr = ev.get("token_address","").lower().strip()
        ts_str = ev.get("post_time_utc","")
        if not addr or not ts_str:
            continue
        try:
            tsi = ts_str.replace("Z","+00:00") if ts_str.endswith("Z") else ts_str
            t0 = int(datetime.fromisoformat(tsi).astimezone(UTC).timestamp()*1000)
        except ValueError:
            continue
        by_addr.setdefault(addr,[]).append(t0)

    PAD_B = 130*60*1000; PAD_A = 330*60*1000; H1 = 3_600_000
    cache: dict[str, tuple] = {}
    for addr, times in by_addr.items():
        t_min = min(times)-PAD_B; t_max = max(times)+PAD_A
        if not (client and budget.okx_ok()):
            continue
        # 1m first
        s = _okx_paged(client, addr, "1m", used, t_min, t_max, 6)
        if s and len(s)>=5 and S002.series_covers_window(s,t_min,t_max):
            cache[addr]=(s,"okx_candles_1m"); continue
        # 1H fallback
        s = _okx_paged(client, addr, "1H", used, t_min, t_max, 4)
        if s and len(s)>=3 and s[0][0]<=t_min+2*H1 and s[-1][0]>=t_max-2*H1:
            cache[addr]=(s,"okx_candles_1h")
    return cache


# ===========================================================================
# Sub-task A: OKX 1m for 2026+ events
# ===========================================================================

def run_subtask_a(actionable: list[dict], client, budget) -> dict:
    recent = [ev for ev in actionable if ev.get("post_time_utc","")>="2026-"]
    print(f"[S005-A] 2026+ actionable events: {len(recent)}")
    used: dict[str,int] = {}
    cache = build_cache_okx_1m(recent, client, budget, used)
    print(f"[S005-A] cache_entries={len(cache)}  OKX_calls={budget.okx}")
    src = Counter(v[1] for v in cache.values())
    print(f"[S005-A] sources: {dict(src)}")
    grid_rows = run_grid(recent, cache)
    _csv(OUT_A_GRID, GRID_COLS, grid_rows)
    sums = summaries_for(grid_rows, "s005a")
    _csv(OUT_A_SUM, SUM_COLS, sums)
    valid = [r for r in grid_rows if not str(r.get("skip_reason","")).strip()]
    uv = dedupe_valid(valid)
    vpe = len(uv)
    print(f"[S005-A] valid_price_events={vpe}  1m_count={src.get('okx_candles_1m',0)}")
    return {
        "subtask": "A",
        "recent_events": len(recent),
        "valid_price_events": vpe,
        "okx_1m_tokens": src.get("okx_candles_1m",0),
        "okx_1h_tokens": src.get("okx_candles_1h",0),
        "best_policy": _best_policy(sums),
        "cache": cache,
        "grid_rows": grid_rows,
    }


def _best_policy(sums: list[dict]) -> dict:
    pol = [s for s in sums if s.get("policy") in FIXED_POLICIES and int(s.get("n") or 0)>0]
    if not pol:
        return {}
    best = max(pol, key=lambda x: float(x.get("mean_return") or 0))
    return {k: best.get(k) for k in ["label","policy","entry_delay_min","exit_after_min","n","hit_rate","mean_return"]}


# ===========================================================================
# Sub-task B: Exclude top token
# ===========================================================================

def run_subtask_b(s004_grid_path: Path) -> dict:
    grid = list(csv.DictReader(open(s004_grid_path, newline="", encoding="utf-8")))
    valid_all  = [r for r in grid if not str(r.get("skip_reason","")).strip()]
    valid_excl = [r for r in valid_all if r.get("token_address","").lower() != TOP_TOKEN_ADDR.lower()]

    uv_all  = dedupe_valid(valid_all)
    uv_excl = dedupe_valid(valid_excl)
    print(f"[S005-B] all valid events: {len(uv_all)}  excl-top-token: {len(uv_excl)}")

    sums_all  = summaries_for(valid_all,  "s005b_all")
    sums_excl = summaries_for(valid_excl, "s005b_excl")
    rows = sums_all + sums_excl
    _csv(OUT_B, SUM_COLS, rows)

    def _pol(sums):
        pol = [s for s in sums if s.get("policy") in FIXED_POLICIES and int(s.get("n") or 0)>0]
        return max(pol, key=lambda x: float(x.get("mean_return") or 0), default={}) if pol else {}

    auth_ctr_excl = Counter()
    tok_ctr_excl  = Counter()
    for r in uv_excl:
        auth_ctr_excl[r.get("author","")]            += 1
        tok_ctr_excl[r.get("token_address","").lower()] += 1
    n = len(uv_excl)
    top_auth_share = max(auth_ctr_excl.values())/n if n else 0
    top_tok_share  = max(tok_ctr_excl.values())/n  if n else 0

    return {
        "subtask": "B",
        "valid_events_with_top_token":    len(uv_all),
        "valid_events_excl_top_token":    len(uv_excl),
        "excl_top_author_share":          round(top_auth_share,4),
        "excl_top_token_share":           round(top_tok_share,4),
        "best_policy_all":   _pol(sums_all),
        "best_policy_excl":  _pol(sums_excl),
        "CONCENTRATION_RISK_EXCL": top_auth_share>0.40 or top_tok_share>0.40,
        "SINGLE_AUTHOR_EXCL": top_auth_share>0.60,
    }


# ===========================================================================
# Sub-task C: On-chain repair
# ===========================================================================

def run_subtask_c(actionable: list[dict], s004_valid_pairs: set,
                  cov_map: dict, budget_dex: Any) -> dict:
    # Candidate events: no price data in S004
    act_pairs = {(r["post_id"], r["token_address"].lower()) for r in actionable}
    no_price  = act_pairs - s004_valid_pairs
    cand_evs  = [ev for ev in actionable if (ev["post_id"], ev["token_address"].lower()) in no_price]
    print(f"[S005-C] candidate events: {len(cand_evs)}")

    # Unique tokens to repair
    tok_map: dict[str, str] = {}     # addr → symbol
    for ev in cand_evs:
        tok_map[ev["token_address"].lower().strip()] = ev.get("token_symbol","")

    # Step 1: Find pool addresses
    pool_map: dict[str, tuple[str, str]] = {}   # addr → (pool_addr, pool_type)
    for addr, sym in tok_map.items():
        # Try coverage data first
        cov = cov_map.get(addr, {})
        pool_id = str(cov.get("gecko_terminal_pool_id") or "")
        if pool_id and len(pool_id) == 42:
            pool_map[addr] = (pool_id, "known")
            print(f"  {sym:12} pool_from_cov: {pool_id}")
            continue
        # Try DexScreener
        pool_addr, ptype = find_pool_dex(budget_dex, addr)
        if pool_addr:
            pool_map[addr] = (pool_addr, ptype)
            print(f"  {sym:12} pool_from_dex: {pool_addr[:20]} ({ptype})")
        else:
            pool_map[addr] = ("", "not_found")
            print(f"  {sym:12} pool_NOT_FOUND")

    # Step 2: Fetch on-chain price series
    rpc_budget   = [0]    # mutable counter
    onchain_cache: dict[str, tuple] = {}
    series_log: list[dict] = []

    PAD_B = 130*60*1000; PAD_A = 330*60*1000

    for addr, sym in tok_map.items():
        pool_addr, ptype = pool_map.get(addr, ("", "not_found"))
        if not pool_addr:
            print(f"  {sym:12} SKIP_NO_POOL")
            series_log.append({"token_address":addr,"token_symbol":sym,
                "pool_address":"","pool_type":"not_found","rpc_calls":0,
                "series_bars":0,"source":"","status":"NO_POOL"})
            continue

        # Time window for this token: all events ± 6h
        times = [int(datetime.fromisoformat(
                     ev["post_time_utc"].replace("Z","+00:00")
                     if ev["post_time_utc"].endswith("Z") else ev["post_time_utc"]
                 ).astimezone(UTC).timestamp()*1000)
                 for ev in cand_evs if ev["token_address"].lower()==addr]
        if not times:
            continue
        t_min = min(times) - PAD_B
        t_max = max(times) + PAD_A

        before_rpc = rpc_budget[0]
        series, src_lbl, err = build_onchain_series(
            addr, pool_addr, t_min, t_max, budget_dex, rpc_budget
        )
        calls_used = rpc_budget[0] - before_rpc
        print(f"  {sym:12} pool={pool_addr[:20]} bars={len(series)} src={src_lbl or err} rpc={calls_used}")
        series_log.append({
            "token_address": addr, "token_symbol": sym,
            "pool_address": pool_addr, "pool_type": ptype,
            "rpc_calls": calls_used, "series_bars": len(series),
            "source": src_lbl, "status": err or "OK",
        })
        if series:
            onchain_cache[addr] = (series, src_lbl)

    _csv(OUT_C_SERIES, ["token_address","token_symbol","pool_address","pool_type",
                         "rpc_calls","series_bars","source","status"], series_log)

    print(f"[S005-C] Total RPC calls: {rpc_budget[0]}")

    # Step 3: Run backtest on repaired events
    repaired_evs = [ev for ev in cand_evs if ev["token_address"].lower() in onchain_cache]
    print(f"[S005-C] repaired events with series: {len(repaired_evs)}")
    c_grid = run_grid(repaired_evs, onchain_cache)
    _csv(OUT_C_GRID, GRID_COLS, c_grid)
    c_sums = summaries_for(c_grid, "s005c")
    _csv(OUT_C_SUM, SUM_COLS, c_sums)

    c_valid = [r for r in c_grid if not str(r.get("skip_reason","")).strip()]
    c_uv    = dedupe_valid(c_valid)
    src_dist = Counter(r.get("data_resolution","") for r in c_valid)
    print(f"[S005-C] repaired valid events: {len(c_uv)}  1m={src_dist.get('1m',0)}  1h={src_dist.get('1h',0)}")

    return {
        "subtask": "C",
        "candidate_events": len(cand_evs),
        "unique_tokens": len(tok_map),
        "pools_found": sum(1 for p,t in pool_map.values() if p),
        "series_built": len(onchain_cache),
        "repaired_valid_events": len(c_uv),
        "onchain_1m_events": src_dist.get("1m",0),
        "onchain_1h_events": src_dist.get("1h",0),
        "total_rpc_calls": rpc_budget[0],
        "best_policy": _best_policy(c_sums),
        "grid_rows": c_grid,
        "series_log": series_log,
    }


# ===========================================================================
# Combined verdict
# ===========================================================================

def combined_verdict(a: dict, b: dict, c: dict) -> str:
    total_vpe = a["valid_price_events"] + c["repaired_valid_events"]
    if total_vpe < 12:
        return "INCONCLUSIVE_SAMPLE_TOO_SMALL"
    excl_ca = float(b.get("excl_top_author_share") or 0)
    if excl_ca > 0.60:
        return "FAIL_SINGLE_AUTHOR_CONCENTRATED"
    excl_ct = float(b.get("excl_top_token_share") or 0)
    if excl_ct > 0.40:
        return "WEAK_EVIDENCE_TOKEN_CONCENTRATED"
    excl_bp = b.get("best_policy_excl") or {}
    mr = float(excl_bp.get("mean_return") or 0)
    hr = float(excl_bp.get("hit_rate") or 0)
    if mr > 0.02 and hr > 0.40:
        return "PASS_DECONCENTRATED_SIGNAL"
    return "FAIL_NO_DECONCENTRATED_ALPHA"


# ===========================================================================
# Main
# ===========================================================================

def main() -> None:
    print("[S005] Loading S004 artifacts ...")
    actionable = list(csv.DictReader(open(S004_CLEAN, newline="", encoding="utf-8")))
    s4_grid    = list(csv.DictReader(open(S004_GRID,  newline="", encoding="utf-8")))
    s4_valid_pairs = {(r["post_id"], r["token_address"].lower())
                      for r in s4_grid if not str(r.get("skip_reason","")).strip()}
    cov_map = {r["token_address"].lower().strip(): r
               for r in csv.DictReader(open(S003_COV, newline="", encoding="utf-8"))}
    print(f"  actionable={len(actionable)}  s4_valid_pairs={len(s4_valid_pairs)}")

    # OKX client
    budget = S002.CallBudget()
    client = None
    if (os.environ.get("OKX_API_KEY") and os.environ.get("OKX_SECRET_KEY")
            and os.environ.get("OKX_API_PASSPHRASE")):
        client = S002.OKXClient(budget)
        print(f"  OKX key={os.environ['OKX_API_KEY'][:8]}...")
    else:
        print("  WARNING: No OKX creds — Sub-task A will have no live data")

    # Budget for DexScreener (used in both A and C)
    dex_budget = S002.CallBudget()

    # --- Sub-task A ---
    res_a = run_subtask_a(actionable, client, budget)

    # --- Sub-task B ---
    res_b = run_subtask_b(S004_GRID)

    # --- Sub-task C ---
    res_c = run_subtask_c(actionable, s4_valid_pairs, cov_map, dex_budget)

    # --- Combined ---
    verdict = combined_verdict(res_a, res_b, res_c)
    total_vpe = res_a["valid_price_events"] + res_c["repaired_valid_events"]

    # Merge A + C grids for joint metrics
    joint_grid = res_a["grid_rows"] + res_c["grid_rows"]
    joint_valid = [r for r in joint_grid if not str(r.get("skip_reason","")).strip()]
    joint_uv = dedupe_valid(joint_valid)
    jn = len(joint_uv)
    auth_ctr = Counter(r.get("author","") for r in joint_uv)
    tok_ctr  = Counter(r.get("token_address","").lower() for r in joint_uv)
    j_top_auth_share = max(auth_ctr.values())/jn if jn else 0
    j_top_tok_share  = max(tok_ctr.values())/jn  if jn else 0

    # Best grid cell in joint data
    j_sums = summaries_for(joint_grid, "s005_joint")
    best_j_grid = max([s for s in j_sums if str(s.get("label","")).startswith("s005_joint_grid_")
                       and int(s.get("n") or 0)>=3],
                      key=lambda x: float(x.get("mean_return") or 0), default={})
    best_j_pol  = max([s for s in j_sums if s.get("policy") in FIXED_POLICIES
                       and int(s.get("n") or 0)>0],
                      key=lambda x: float(x.get("mean_return") or 0), default={})

    flags: dict[str,Any] = {
        "sprint": "S005", "generated_at_utc": datetime.now(UTC).isoformat(),
        "S005_VERDICT": verdict,
        "TOTAL_VALID_PRICE_EVENTS": total_vpe,
        "subA": {
            "recent_events": res_a["recent_events"],
            "valid_price_events": res_a["valid_price_events"],
            "okx_1m_tokens": res_a["okx_1m_tokens"],
            "okx_1h_tokens": res_a["okx_1h_tokens"],
            "best_policy": res_a["best_policy"],
        },
        "subB": {
            "valid_events_all": res_b["valid_events_with_top_token"],
            "valid_events_excl_top": res_b["valid_events_excl_top_token"],
            "excl_top_author_share": res_b["excl_top_author_share"],
            "excl_top_token_share": res_b["excl_top_token_share"],
            "CONCENTRATION_RISK_EXCL": res_b["CONCENTRATION_RISK_EXCL"],
            "best_policy_all":  res_b["best_policy_all"],
            "best_policy_excl": res_b["best_policy_excl"],
        },
        "subC": {
            "candidate_events": res_c["candidate_events"],
            "pools_found": res_c["pools_found"],
            "series_built": res_c["series_built"],
            "repaired_valid_events": res_c["repaired_valid_events"],
            "onchain_1m_events": res_c["onchain_1m_events"],
            "total_rpc_calls": res_c["total_rpc_calls"],
            "best_policy": res_c["best_policy"],
        },
        "JOINT_VALID_EVENTS": jn,
        "JOINT_TOP_AUTHOR_SHARE": round(j_top_auth_share,4),
        "JOINT_TOP_TOKEN_SHARE":  round(j_top_tok_share,4),
        "JOINT_CONCENTRATION_RISK": j_top_auth_share>0.40 or j_top_tok_share>0.40,
        "JOINT_SINGLE_AUTHOR_RESEARCH_ONLY": j_top_auth_share>0.60,
        "JOINT_BEST_DIAGNOSTIC_GRID": {
            k: best_j_grid.get(k) for k in ["label","entry_delay_min","exit_after_min","n","hit_rate","mean_return"]
        },
        "JOINT_BEST_FIXED_POLICY": {
            k: best_j_pol.get(k) for k in ["label","policy","entry_delay_min","exit_after_min","n","hit_rate","mean_return"]
        },
        "budget": {
            "OKX_CALLS": budget.okx,
            "GECKO_CALLS": budget.gecko,
            "DEX_CALLS": dex_budget.dex,
            "BASE_RPC_CALLS": res_c["total_rpc_calls"],
        },
        "constraints": {
            "no_x_api": True, "no_executor": True, "no_live_trading": True,
            "no_private_keys": True, "no_redis": True, "no_swap_broadcast": True,
            "no_full_route4_backfill": True,
        },
    }

    with OUT_FLAGS.open("w", encoding="utf-8") as f:
        json.dump(flags, f, indent=2, default=str)

    # Readout
    def _g(d,k): return d.get(k,"") if d else ""
    lines = [
        "# S005 — Deconcentrate + On-Chain Repair",
        f"\n- UTC: `{flags['generated_at_utc']}`",
        f"- S005_VERDICT: **{verdict}**",
        f"- TOTAL_VALID_PRICE_EVENTS: **{total_vpe}**",
        "\n## Sub-task A: OKX 1m (2026+ events)\n",
        f"- recent_events: {res_a['recent_events']}",
        f"- valid_price_events: {res_a['valid_price_events']}",
        f"- okx_1m_tokens: {res_a['okx_1m_tokens']}",
        f"- okx_1h_tokens: {res_a['okx_1h_tokens']}",
        f"- best_policy: `{json.dumps(res_a['best_policy'])}`",
        "\n## Sub-task B: Exclude TOP_TOKEN (LFI)\n",
        f"- valid_events_all: {res_b['valid_events_with_top_token']}",
        f"- valid_events_excl: {res_b['valid_events_excl_top_token']}",
        f"- excl_top_author_share: {res_b['excl_top_author_share']:.1%}",
        f"- excl_top_token_share: {res_b['excl_top_token_share']:.1%}",
        f"- CONCENTRATION_RISK_EXCL: {res_b['CONCENTRATION_RISK_EXCL']}",
        f"- best_policy_all: `{json.dumps(res_b['best_policy_all'])}`",
        f"- best_policy_excl: `{json.dumps(res_b['best_policy_excl'])}`",
        "\n## Sub-task C: On-chain repair\n",
        f"- candidate_events: {res_c['candidate_events']}",
        f"- pools_found: {res_c['pools_found']}",
        f"- series_built: {res_c['series_built']}",
        f"- repaired_valid_events: {res_c['repaired_valid_events']}",
        f"- onchain_1m_events: {res_c['onchain_1m_events']}",
        f"- total_rpc_calls: {res_c['total_rpc_calls']}",
        "\n## Joint metrics\n",
        f"- JOINT_VALID_EVENTS: **{jn}**",
        f"- JOINT_TOP_AUTHOR_SHARE: {j_top_auth_share:.1%}",
        f"- JOINT_TOP_TOKEN_SHARE: {j_top_tok_share:.1%}",
        f"- JOINT_CONCENTRATION_RISK: {flags['JOINT_CONCENTRATION_RISK']}",
        f"- JOINT_BEST_DIAGNOSTIC_GRID: `{json.dumps(flags['JOINT_BEST_DIAGNOSTIC_GRID'])}`",
        f"- JOINT_BEST_FIXED_POLICY: `{json.dumps(flags['JOINT_BEST_FIXED_POLICY'])}`",
    ]
    with OUT_COMBINED.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n{'='*60}")
    print("S005 COMPLETE")
    print(f"  S005_VERDICT:          {verdict}")
    print(f"  A valid_price:         {res_a['valid_price_events']} (1m={res_a['okx_1m_tokens']})")
    print(f"  B excl author share:   {res_b['excl_top_author_share']:.1%}")
    print(f"  C repaired:            {res_c['repaired_valid_events']} (rpc={res_c['total_rpc_calls']})")
    print(f"  JOINT valid events:    {jn}")
    print(f"  OKX calls:             {budget.okx}")
    print(f"  Base RPC calls:        {res_c['total_rpc_calls']}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
