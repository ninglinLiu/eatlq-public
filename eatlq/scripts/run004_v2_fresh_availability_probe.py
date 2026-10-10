"""Run-004 V2 Fresh Availability Probe
======================================
Goal:
  Verify whether sufficient fresh, clean, labelable V2 samples exist on Base
  chain to build a second independent OOT window (blocks > LAST_KNOWN_BLOCK).

Scope:
  - pool_type == v2 ONLY.
  - Availability / candidate / labelability diagnostics ONLY.
  - NO training, NO H19, NO shadow, NO live, NO production model writes.
  - NO V3 / aero_cl / aero_vamm / unknown pool types.

Must answer (7 questions):
  Q1. How many fresh V2 candidates?
  Q2. How many are executable / labelable?
  Q3. How many are clean rows?
  Q4. unique_pools / top1_pool_share / top5_pool_share?
  Q5. quote_success_rate?
  Q6. Is the supply sufficient for a second independent OOT window?
  Q7. If insufficient, what is the primary cause?

Outputs:
  mvp/diagnostics/run004_v2_fresh_availability_probe.json
  mvp/runs/run-004-v2-fresh-availability-probe.md
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── safety assertions ────────────────────────────────────────────────────────
assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1", "true", "yes", "on",
), "LIVE_TRADING_MODE must not be active"

# ── constants ────────────────────────────────────────────────────────────────
# Last block in H18 validation window (H18.8 confirmed).
LAST_KNOWN_BLOCK = 45_408_934

# V2 Swap(address,uint256,uint256,uint256,uint256,address) event topic
V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"

# getReserves() selector on V2 pair: returns (uint112,uint112,uint32)
GET_RESERVES_SEL = "0x0902f1ac"

# WETH on Base
WETH_BASE = "0x4200000000000000000000000000000000000006"

# Probe tuning
LOG_CHUNK_BLOCKS = 4000          # max block range per eth_getLogs call
MAX_POOLS_PER_LOG_CALL = 100     # max addresses per eth_getLogs
PILOT_QUOTE_CAP = 200            # max events to test labelability on
TOTAL_CANDIDATE_CAP = 5000       # soft ceiling: stop collecting logs after this
MIN_OOT_WINDOW_ROWS = 1_000      # threshold: a "second OOT window" needs at least 1k clean rows

DIAG_DIR = ROOT / "mvp/diagnostics"
RUNS_DIR = ROOT / "mvp/runs"
OUT_JSON = DIAG_DIR / "run004_v2_fresh_availability_probe.json"
OUT_MD = RUNS_DIR / "run-004-v2-fresh-availability-probe.md"

# ── known bad pools (from H1610s config) ────────────────────────────────────
KNOWN_BAD_POOLS = {
    "0x3e118916cd67cc9bfeb0fc44ae3dde8bc33c2d4a",
}
ALPHA_ISLAND_POOLS = {
    "0x54219bce730d2727d91d8dcdf1465c7d299dee45",
    "0xc2476883d28d40b6c057f242fce2e8bda674faa0",
}
HARD_EXCLUDE_ABS_RETURN = 0.20

GENERATED_AT = datetime.now(timezone.utc).isoformat()

# ── helpers ──────────────────────────────────────────────────────────────────
def _load_env_file() -> None:
    env_path = ROOT / "infra/compose/.env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _rpc_url() -> str:
    _load_env_file()
    url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")
    if not url:
        raise RuntimeError("RPC URL required in LABEL_RPC_URL or EVM_HTTP_URL")
    return url


class _RPC:
    def __init__(self, url: str) -> None:
        self.url = url
        self._id = 0
        self.calls = 0
        self.errors = 0

    def call(self, method: str, params: list[Any], timeout: int = 30) -> Any:
        import urllib.request as _ur
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": self._id})
        req = _ur.Request(self.url, data=body.encode(), headers={"Content-Type": "application/json"})
        self.calls += 1
        try:
            with _ur.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            if "error" in data:
                self.errors += 1
                return None
            return data.get("result")
        except Exception:
            self.errors += 1
            return None

    def block_number(self) -> int | None:
        r = self.call("eth_blockNumber", [])
        return int(r, 16) if r else None

    def get_reserves(self, pool: str, block_hex: str) -> tuple[int, int] | None:
        """Call getReserves() on a V2 pair at a specific block."""
        res = self.call("eth_call", [
            {"to": pool, "data": GET_RESERVES_SEL},
            block_hex,
        ], timeout=15)
        if not res or res == "0x" or len(res) < 2 + 192:
            return None
        raw = res[2:]
        r0 = int(raw[:64], 16)
        r1 = int(raw[64:128], 16)
        if r0 == 0 and r1 == 0:
            return None
        return r0, r1

    def get_logs(self, from_block: int, to_block: int, addresses: list[str],
                 topics: list[str]) -> list[dict] | None:
        params = [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": addresses,
            "topics": [topics],
        }]
        return self.call("eth_getLogs", params, timeout=60)


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


# ── load known V2 pools from existing H1611 dataset ─────────────────────────
def load_known_v2_pools() -> list[dict[str, Any]]:
    """Return list of pool metadata dicts from the H1611 dataset."""
    path = DIAG_DIR / "run004_h1611_all_v2_decoded_backed_10k_dataset.csv"
    if not path.exists():
        print(f"[WARN] H1611 dataset not found: {path}")
        return []
    pool_meta: dict[str, dict[str, Any]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pool = _lower(row.get("pool_address", ""))
            if not pool or pool in pool_meta:
                continue
            pool_meta[pool] = {
                "pool_address": pool,
                "token0_address": _lower(row.get("token0_address") or row.get("token0", "")),
                "token1_address": _lower(row.get("token1_address") or row.get("token1", "")),
                "fee_bps": int(row.get("fee_bps") or 30),
                "weth_leg": _lower(row.get("weth_leg", "")),
                "pool_type": "v2",
            }
    print(f"[INFO] Loaded {len(pool_meta)} unique V2 pools from H1611 dataset.")
    return list(pool_meta.values())


# ── fetch fresh V2 Swap events ───────────────────────────────────────────────
def fetch_fresh_v2_events(
    rpc: _RPC,
    pools: list[dict[str, Any]],
    from_block: int,
    to_block: int,
) -> list[dict[str, Any]]:
    """Collect V2 Swap events from all known pools in [from_block, to_block]."""
    pool_addrs = [p["pool_address"] for p in pools]
    pool_lookup = {p["pool_address"]: p for p in pools}
    events: list[dict[str, Any]] = []

    # Chunk by block range × pool batch
    block_step = LOG_CHUNK_BLOCKS
    pool_step = MAX_POOLS_PER_LOG_CALL

    block_from = from_block
    while block_from <= to_block:
        block_to = min(block_from + block_step - 1, to_block)
        print(f"  [getLogs] blocks {block_from:,}–{block_to:,} …", end="", flush=True)
        chunk_count = 0
        for i in range(0, len(pool_addrs), pool_step):
            addr_batch = pool_addrs[i : i + pool_step]
            logs = rpc.get_logs(block_from, block_to, addr_batch, [V2_SWAP_TOPIC])
            if logs:
                for log in logs:
                    pool_addr = _lower(log.get("address", ""))
                    meta = pool_lookup.get(pool_addr, {})
                    events.append({
                        "pool_address": pool_addr,
                        "tx_hash": log.get("transactionHash", ""),
                        "log_index": int(log.get("logIndex", "0x0"), 16),
                        "block_number": int(log.get("blockNumber", "0x0"), 16),
                        "token0_address": meta.get("token0_address", ""),
                        "token1_address": meta.get("token1_address", ""),
                        "weth_leg": meta.get("weth_leg", ""),
                        "fee_bps": meta.get("fee_bps", 30),
                    })
                chunk_count += len(logs)
        print(f" +{chunk_count} (total {len(events)})")
        if len(events) >= TOTAL_CANDIDATE_CAP:
            print(f"  [INFO] Candidate cap {TOTAL_CANDIDATE_CAP} reached.")
            break
        block_from += block_step
        time.sleep(0.05)  # gentle rate limit

    return events


# ── dedup by (pool, tx_hash, log_index) ─────────────────────────────────────
def dedup_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for e in events:
        key = (e["pool_address"], e["tx_hash"], e["log_index"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


# ── quote-based labelability check ──────────────────────────────────────────
def check_labelability(
    rpc: _RPC,
    events: list[dict[str, Any]],
    pilot_cap: int,
) -> dict[str, Any]:
    """
    For a pilot sample, call getReserves() at source_block to check
    whether the pool had valid liquidity at that moment (labelable).
    Also check at source_block + 900 (~30 min exit) for round-trip feasibility.
    """
    sample = events[:pilot_cap]
    entry_ok = 0
    roundtrip_ok = 0
    reserve_errors = 0
    quote_results: list[dict[str, Any]] = []

    for ev in sample:
        block_hex = hex(ev["block_number"])
        exit_hex = hex(ev["block_number"] + 900)  # ~30 min on Base (~2s/block)
        pool = ev["pool_address"]

        r_entry = rpc.get_reserves(pool, block_hex)
        entry_valid = r_entry is not None
        if entry_valid:
            entry_ok += 1
        else:
            reserve_errors += 1

        r_exit = rpc.get_reserves(pool, exit_hex)
        roundtrip_valid = r_exit is not None
        if entry_valid and roundtrip_valid:
            roundtrip_ok += 1

        quote_results.append({
            "pool": pool,
            "block": ev["block_number"],
            "entry_quote_ok": entry_valid,
            "roundtrip_ok": entry_valid and roundtrip_valid,
        })
        time.sleep(0.04)

    n = len(sample)
    return {
        "pilot_n": n,
        "entry_quote_success": entry_ok,
        "roundtrip_success": roundtrip_ok,
        "reserve_errors": reserve_errors,
        "entry_quote_success_rate": entry_ok / n if n else 0.0,
        "roundtrip_success_rate": roundtrip_ok / n if n else 0.0,
        "quote_results_sample": quote_results[:10],
    }


# ── risk filter (mirrors H16.11 labeling logic) ──────────────────────────────
def apply_risk_filter(events: list[dict[str, Any]]) -> dict[str, Any]:
    clean: list[dict[str, Any]] = []
    risk_bad_pool: list[dict[str, Any]] = []

    for ev in events:
        pool = ev["pool_address"]
        if pool in KNOWN_BAD_POOLS:
            risk_bad_pool.append(ev)
        elif pool in ALPHA_ISLAND_POOLS:
            risk_bad_pool.append(ev)
        else:
            clean.append(ev)

    return {
        "total": len(events),
        "clean": clean,
        "risk_bad_pool": risk_bad_pool,
        "clean_count": len(clean),
        "risk_count": len(risk_bad_pool),
    }


# ── concentration stats ───────────────────────────────────────────────────────
def concentration_stats(events: list[dict[str, Any]], label: str = "") -> dict[str, Any]:
    if not events:
        return {
            "total": 0, "unique_pools": 0,
            "top1_pool_share": 0.0, "top5_pool_share": 0.0,
            "top5_pools": [],
        }
    n = len(events)
    pools = Counter(e["pool_address"] for e in events)
    top5 = pools.most_common(5)
    return {
        "total": n,
        "unique_pools": len(pools),
        "top1_pool_share": round(top5[0][1] / n, 8),
        "top5_pool_share": round(sum(v for _, v in top5) / n, 8),
        "top5_pools": [{"pool": p, "count": c, "share": round(c/n, 6)} for p, c in top5],
    }


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    print("=" * 70)
    print("V2 Fresh Availability Probe")
    print(f"Generated at: {GENERATED_AT}")
    print(f"LAST_KNOWN_BLOCK: {LAST_KNOWN_BLOCK:,}")
    print("=" * 70)

    rpc_url = _rpc_url()
    rpc = _RPC(rpc_url)

    # ── 1. current chain state ───────────────────────────────────────────────
    print("\n[1] Getting current block number …")
    current_block = rpc.block_number()
    if not current_block:
        raise RuntimeError("Failed to get current block number. Check RPC URL.")
    fresh_blocks = current_block - LAST_KNOWN_BLOCK
    print(f"    current_block   = {current_block:,}")
    print(f"    last_known_block= {LAST_KNOWN_BLOCK:,}")
    print(f"    fresh_blocks    = {fresh_blocks:,} (~{fresh_blocks * 2 / 3600:.1f}h at 2s/block)")

    # ── 2. load known V2 pools ───────────────────────────────────────────────
    print("\n[2] Loading known V2 pools from H1611 dataset …")
    known_pools = load_known_v2_pools()
    if not known_pools:
        raise RuntimeError("No V2 pools found. H1611 CSV may be missing.")
    print(f"    Known V2 pools: {len(known_pools)}")

    # ── 3. fetch fresh V2 events ─────────────────────────────────────────────
    print(f"\n[3] Fetching V2 Swap events in blocks [{LAST_KNOWN_BLOCK+1:,}, {current_block:,}] …")
    from_block = LAST_KNOWN_BLOCK + 1
    to_block = current_block
    raw_events = fetch_fresh_v2_events(rpc, known_pools, from_block, to_block)

    # Dedup
    events = dedup_events(raw_events)
    print(f"    Raw events: {len(raw_events)}  After dedup: {len(events)}")

    # ── 4. risk filter ───────────────────────────────────────────────────────
    print("\n[4] Applying risk filter …")
    risk_result = apply_risk_filter(events)
    clean_events = risk_result["clean"]
    print(f"    Total:           {risk_result['total']}")
    print(f"    Clean:           {risk_result['clean_count']}")
    print(f"    Risk/bad pool:   {risk_result['risk_count']}")

    # ── 5. concentration stats ───────────────────────────────────────────────
    print("\n[5] Concentration stats …")
    all_conc = concentration_stats(events, "all")
    clean_conc = concentration_stats(clean_events, "clean")
    print(f"    All   — unique_pools={all_conc['unique_pools']}, "
          f"top1={all_conc['top1_pool_share']:.4f}, top5={all_conc['top5_pool_share']:.4f}")
    print(f"    Clean — unique_pools={clean_conc['unique_pools']}, "
          f"top1={clean_conc['top1_pool_share']:.4f}, top5={clean_conc['top5_pool_share']:.4f}")

    # ── 6. labelability pilot ────────────────────────────────────────────────
    print(f"\n[6] Labelability pilot (archive getReserves, up to {PILOT_QUOTE_CAP} events) …")
    if clean_events:
        label_result = check_labelability(rpc, clean_events, min(PILOT_QUOTE_CAP, len(clean_events)))
    else:
        label_result = {
            "pilot_n": 0,
            "entry_quote_success": 0,
            "roundtrip_success": 0,
            "reserve_errors": 0,
            "entry_quote_success_rate": 0.0,
            "roundtrip_success_rate": 0.0,
            "quote_results_sample": [],
        }
    print(f"    Pilot N:                 {label_result['pilot_n']}")
    print(f"    Entry quote success:     {label_result['entry_quote_success']} "
          f"({label_result['entry_quote_success_rate']:.3f})")
    print(f"    Round-trip success:      {label_result['roundtrip_success']} "
          f"({label_result['roundtrip_success_rate']:.3f})")

    # ── 7. OOT viability assessment ──────────────────────────────────────────
    print("\n[7] OOT viability assessment …")
    total_candidates = len(events)
    clean_count = len(clean_events)

    # Projected labelable clean rows at observed success rate:
    # pilot applies to a subset; extrapolate to clean_count
    entry_rate = label_result["entry_quote_success_rate"]
    projected_labelable = int(clean_count * entry_rate)
    roundtrip_rate = label_result["roundtrip_success_rate"]
    projected_roundtrip = int(clean_count * roundtrip_rate)

    oot_feasible = projected_labelable >= MIN_OOT_WINDOW_ROWS

    # Root cause analysis
    if total_candidates == 0:
        primary_cause = "no_fresh_v2_events_found_in_known_pool_set"
    elif clean_count == 0:
        primary_cause = "all_events_risk_filtered"
    elif entry_rate < 0.5:
        primary_cause = "rpc_or_archive_labelability_failure"
    elif projected_labelable < MIN_OOT_WINDOW_ROWS:
        if total_candidates < MIN_OOT_WINDOW_ROWS:
            primary_cause = "v2_market_sample_volume_insufficient_in_known_pool_set"
        else:
            primary_cause = "risk_filter_too_strict_or_clean_yield_too_low"
    else:
        primary_cause = "none"

    print(f"    total_candidates:        {total_candidates:,}")
    print(f"    clean_count:             {clean_count:,}")
    print(f"    projected_labelable:     {projected_labelable:,}")
    print(f"    projected_roundtrip:     {projected_roundtrip:,}")
    print(f"    min_oot_window_target:   {MIN_OOT_WINDOW_ROWS:,}")
    print(f"    oot_feasible:            {oot_feasible}")
    print(f"    primary_cause:           {primary_cause}")

    # ── 8. gate decisions ────────────────────────────────────────────────────
    gate = {
        "allow_full_v2_labeling_run": oot_feasible,
        "allow_return_aware_model_dryrun": False,
        "allow_h19_shadow_scoring": False,
        "allow_live": False,
        "allow_promote": False,
        "allow_training": False,
        "v3_rows_included": False,
        "aero_rows_included": False,
        "mixed_dataset": False,
        "production_model_write": False,
        "note": (
            "allow_full_v2_labeling_run is true only when projected labelable >= "
            f"{MIN_OOT_WINDOW_ROWS}. All other gates remain false."
        ),
    }

    # ── 9. assemble final summary ────────────────────────────────────────────
    summary: dict[str, Any] = {
        "generated_at": GENERATED_AT,
        "patch": "run-004-v2-fresh-availability-probe",
        "sanity_only_not_deploy_evidence": True,
        "constraint_assertions": {
            "no_training": True,
            "no_h19": True,
            "no_shadow": True,
            "no_live": True,
            "no_production_model_write": True,
            "pool_type_filter": "v2_only",
            "no_v3_aero_mixed": True,
        },
        "chain_state": {
            "current_block": current_block,
            "last_known_block": LAST_KNOWN_BLOCK,
            "fresh_blocks": fresh_blocks,
            "fresh_blocks_hours": round(fresh_blocks * 2 / 3600, 2),
            "probe_block_range": f"{LAST_KNOWN_BLOCK + 1:,}–{current_block:,}",
        },
        "known_pool_set": {
            "source_dataset": "run004_h1611_all_v2_decoded_backed_10k_dataset.csv",
            "known_v2_pools": len(known_pools),
            "note": (
                "Pool set is bounded by existing H1611 dataset (747 pools). "
                "New V2 pools launched after H1611 data collection are NOT included. "
                "Total fresh candidates may be higher if new pools are included."
            ),
        },
        "q1_fresh_candidates": {
            "raw_events": len(raw_events),
            "after_dedup": total_candidates,
            "block_range": f"{LAST_KNOWN_BLOCK + 1:,}–{current_block:,}",
            "unique_pools_seen": all_conc["unique_pools"],
        },
        "q2_executable_labelable": {
            "pilot_n": label_result["pilot_n"],
            "entry_quote_success_rate": entry_rate,
            "roundtrip_success_rate": roundtrip_rate,
            "projected_labelable_from_clean": projected_labelable,
            "projected_roundtrip_from_clean": projected_roundtrip,
        },
        "q3_clean_rows": {
            "clean_count": clean_count,
            "risk_count": risk_result["risk_count"],
            "clean_rate": round(clean_count / total_candidates, 6) if total_candidates else 0.0,
        },
        "q4_pool_concentration": {
            "all_events": all_conc,
            "clean_events": clean_conc,
        },
        "q5_quote_success_rate": {
            "entry_rate": entry_rate,
            "roundtrip_rate": roundtrip_rate,
            "pilot_n": label_result["pilot_n"],
            "reserve_errors": label_result["reserve_errors"],
        },
        "q6_oot_window_feasibility": {
            "min_oot_window_rows": MIN_OOT_WINDOW_ROWS,
            "projected_labelable": projected_labelable,
            "feasible": oot_feasible,
            "gap_to_target": max(0, MIN_OOT_WINDOW_ROWS - projected_labelable),
        },
        "q7_primary_cause": {
            "cause": primary_cause,
            "detail": _cause_detail(primary_cause, total_candidates, clean_count,
                                    entry_rate, projected_labelable, len(known_pools)),
        },
        "rpc_stats": {
            "total_calls": rpc.calls,
            "total_errors": rpc.errors,
            "error_rate": round(rpc.errors / rpc.calls, 4) if rpc.calls else 0.0,
        },
        "gate": gate,
    }

    # ── 10. write outputs ────────────────────────────────────────────────────
    print("\n[10] Writing diagnostic outputs …")
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"    JSON: {OUT_JSON}")

    _write_md(summary)
    print(f"    MD:   {OUT_MD}")

    print("\n" + "=" * 70)
    print("V2 FRESH AVAILABILITY PROBE — COMPLETE")
    print(f"  Q1 fresh candidates:      {total_candidates:,}")
    print(f"  Q3 clean rows:            {clean_count:,}")
    print(f"  Q4 unique_pools:          {clean_conc['unique_pools']}")
    print(f"     top1_pool_share:       {clean_conc['top1_pool_share']:.4f}")
    print(f"     top5_pool_share:       {clean_conc['top5_pool_share']:.4f}")
    print(f"  Q5 entry_quote_rate:      {entry_rate:.3f}")
    print(f"  Q6 oot_feasible:          {oot_feasible}")
    print(f"  Q7 primary_cause:         {primary_cause}")
    print("=" * 70)


def _cause_detail(
    cause: str,
    total_candidates: int,
    clean_count: int,
    entry_rate: float,
    projected_labelable: int,
    known_pool_count: int,
) -> str:
    if cause == "none":
        return "No blocking cause. Sufficient fresh clean labelable V2 events found."
    if cause == "no_fresh_v2_events_found_in_known_pool_set":
        return (
            f"Zero V2 Swap events found in blocks > {LAST_KNOWN_BLOCK:,} across "
            f"{known_pool_count} known pools. Either the known pool set is exhausted "
            "(no swaps on these pools post-H1611) or the pools have gone inactive."
        )
    if cause == "all_events_risk_filtered":
        return (
            f"Found {total_candidates} raw events but all filtered as risk/bad-pool. "
            "Unlikely with 747 pools; check KNOWN_BAD_POOLS list."
        )
    if cause == "rpc_or_archive_labelability_failure":
        return (
            f"Entry quote success rate {entry_rate:.2%} < 50%. Archive RPC is returning "
            "errors for historical getReserves(). Check that the RPC endpoint is archive-capable."
        )
    if cause == "v2_market_sample_volume_insufficient_in_known_pool_set":
        return (
            f"Only {total_candidates:,} fresh V2 events in known pool set, projected "
            f"labelable = {projected_labelable:,} < {MIN_OOT_WINDOW_ROWS:,} target. "
            "Root cause: V2 trading volume on tracked pools is insufficient over the "
            "fresh block window. Options: (a) expand block window further back (overlap "
            "risk), (b) discover new V2 pools via factory scan, (c) lower OOT window target."
        )
    if cause == "risk_filter_too_strict_or_clean_yield_too_low":
        return (
            f"{total_candidates:,} total events but only {clean_count:,} clean, projected "
            f"labelable {projected_labelable:,} < {MIN_OOT_WINDOW_ROWS:,}. Risk filter or "
            "clean yield is too low. Review risk_severity thresholds."
        )
    return cause


def _write_md(s: dict[str, Any]) -> None:
    chain = s["chain_state"]
    kp = s["known_pool_set"]
    q1 = s["q1_fresh_candidates"]
    q2 = s["q2_executable_labelable"]
    q3 = s["q3_clean_rows"]
    q4 = s["q4_pool_concentration"]
    q5 = s["q5_quote_success_rate"]
    q6 = s["q6_oot_window_feasibility"]
    q7 = s["q7_primary_cause"]
    gate = s["gate"]

    clean_conc = q4["clean_events"]
    all_conc = q4["all_events"]

    md = f"""# Run-004: V2 Fresh Availability Probe

**Date:** {s['generated_at'][:10]}  
**Status:** COMPLETE — diagnostics only  
**Scope:** pool_type == v2 only · no training · no H19 · no shadow · no live · no production writes

---

## 1. Chain State

| Field | Value |
|-------|-------|
| current_block | {chain['current_block']:,} |
| last_known_block (H18 val) | {chain['last_known_block']:,} |
| fresh_blocks | {chain['fresh_blocks']:,} |
| fresh window (approx) | ~{chain['fresh_blocks_hours']:.1f}h at 2s/block |
| probe block range | {chain['probe_block_range']} |

**Known pool set:** {kp['known_v2_pools']} V2 pools from `run004_h1611_all_v2_decoded_backed_10k_dataset.csv`.  
> Note: pool set is bounded by H1611 (747 pools). Newly launched V2 pools after H1611 collection are NOT included.

---

## 2. Required Answers

### Q1. Fresh V2 Candidate Count

| Metric | Value |
|--------|-------|
| raw_events (getLogs) | {q1['raw_events']:,} |
| after_dedup | {q1['after_dedup']:,} |
| unique_pools_seen | {q1['unique_pools_seen']} |
| block_range | {q1['block_range']} |

### Q2. Executable / Labelable Count

| Metric | Value |
|--------|-------|
| pilot_n (archive getReserves sample) | {q2['pilot_n']} |
| entry_quote_success_rate | {q2['entry_quote_success_rate']:.3f} |
| roundtrip_success_rate | {q2['roundtrip_success_rate']:.3f} |
| projected_labelable (from clean) | {q2['projected_labelable_from_clean']:,} |
| projected_roundtrip_labelable | {q2['projected_roundtrip_from_clean']:,} |

### Q3. Clean Row Count

| Metric | Value |
|--------|-------|
| clean_count | {q3['clean_count']:,} |
| risk / bad_pool count | {q3['risk_count']:,} |
| clean_rate | {q3['clean_rate']:.4f} |

### Q4. Pool / Token Concentration

**All events:**

| Metric | Value |
|--------|-------|
| unique_pools | {all_conc['unique_pools']} |
| top1_pool_share | {all_conc['top1_pool_share']:.4f} |
| top5_pool_share | {all_conc['top5_pool_share']:.4f} |

**Clean events:**

| Metric | Value |
|--------|-------|
| unique_pools | {clean_conc['unique_pools']} |
| top1_pool_share | {clean_conc['top1_pool_share']:.4f} |
| top5_pool_share | {clean_conc['top5_pool_share']:.4f} |

Top-5 clean pools:
"""
    for p in clean_conc.get("top5_pools", []):
        md += f"- `{p['pool']}`: {p['count']} events ({p['share']:.4f})\n"

    md += f"""
### Q5. Quote Success Rate

| Metric | Value |
|--------|-------|
| entry_quote_success_rate | {q5['entry_rate']:.3f} |
| roundtrip_success_rate | {q5['roundtrip_rate']:.3f} |
| pilot_n | {q5['pilot_n']} |
| reserve_errors | {q5['reserve_errors']} |

Labelability check: archive `getReserves()` call at `source_block` (entry) and `source_block + 900` (exit ~30 min).

### Q6. Sufficient for Second Independent OOT Window?

| Metric | Value |
|--------|-------|
| projected_labelable | {q6['projected_labelable']:,} |
| min_oot_window_target | {q6['min_oot_window_rows']:,} |
| feasible | **{q6['feasible']}** |
| gap_to_target | {q6['gap_to_target']:,} |

### Q7. Primary Cause (if insufficient)

**Cause:** `{q7['cause']}`

{q7['detail']}

---

## 3. Gate Decisions

| Gate | Decision |
|------|----------|
| allow_full_v2_labeling_run | **{gate['allow_full_v2_labeling_run']}** |
| allow_return_aware_model_dryrun | {gate['allow_return_aware_model_dryrun']} |
| allow_h19_shadow_scoring | {gate['allow_h19_shadow_scoring']} |
| allow_live | {gate['allow_live']} |
| allow_promote | {gate['allow_promote']} |
| allow_training | {gate['allow_training']} |
| v3_rows_included | {gate['v3_rows_included']} |
| production_model_write | {gate['production_model_write']} |

---

## 4. Artifacts

- `mvp/diagnostics/run004_v2_fresh_availability_probe.json`
- `mvp/runs/run-004-v2-fresh-availability-probe.md` (this file)

## 5. Constraints Verification

- No training ✅  
- No H19 shadow ✅  
- No live / promote ✅  
- No production model writes ✅  
- pool_type == v2 only ✅  
- No V3 / aero / mixed ✅  
- Dune API not used ✅  
"""
    OUT_MD.write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
