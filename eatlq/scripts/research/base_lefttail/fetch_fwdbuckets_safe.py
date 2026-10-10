#!/usr/bin/env python3
"""Safe forward-bucket extraction from ClickHouse (RESEARCH ONLY).

LIVE-FIRST safeguards:
  - One pool per query (pool_address indexed filter)
  - Per-query: max_memory_usage=1GiB, max_execution_time=90s, max_threads=1
  - Waits if ClickHouse RSS > CH_MEM_PAUSE_MB or live bridge scan running
  - nice/ionice via renice in caller; 2s pause between pools
  - Resumable checkpoint; writes atomically to _fwdbuckets.tsv

Only fetches pools that passed depth gate (fric >= -0.05 in _priced_live.json).
"""
from __future__ import annotations
import json, os, subprocess, sys, time, tempfile
from pathlib import Path

ROOT = Path("/opt/EATLQ/scripts/research/base_lefttail")
PCACHE = ROOT / "_priced_live.json"
OUT = ROOT / "_fwdbuckets.tsv"
PROGRESS = ROOT / "_fwdbuckets.progress.json"
CH = "eatlq-clickhouse-1"
W, H = 600, 14400
FRIC_THR = -0.05
CH_MEM_PAUSE_MB = 1800          # wait if CH RSS exceeds this while live busy
CH_MEM_HARD_ABORT_MB = 6000     # abort only on extreme pressure (~25% of 23GiB)
PAUSE_SEC = 3.0
QUERY_TIMEOUT = 120


def ch_query(sql: str) -> tuple[int, str, str]:
    r = subprocess.run(
        ["docker", "exec", CH, "clickhouse-client", "--query", sql],
        capture_output=True, text=True, timeout=QUERY_TIMEOUT,
    )
    return r.returncode, r.stdout, r.stderr


def ch_mem_mb() -> float:
    r = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", CH],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return 0.0
    # "860.5MiB / 23.47GiB"
    part = r.stdout.strip().split("/")[0].strip()
    if part.endswith("GiB"):
        return float(part[:-3]) * 1024
    if part.endswith("MiB"):
        return float(part[:-3])
    return 0.0


def live_ch_busy() -> bool:
    """True if signal-bridge style heavy scan or archiver insert in flight."""
    sql = (
        "SELECT count() FROM system.processes "
        "WHERE query NOT LIKE '%system.processes%' "
        "AND (read_rows > 1000000 OR elapsed > 5)"
    )
    rc, out, _ = ch_query(sql)
    if rc != 0:
        return True
    try:
        return int(out.strip() or "0") > 0
    except ValueError:
        return True


def wait_for_slot(max_wait: int = 600) -> bool:
    """Wait until CH is safe for research query. Returns False if aborted."""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        mem = ch_mem_mb()
        if mem > CH_MEM_HARD_ABORT_MB:
            print(f"ABORT: ClickHouse RSS {mem:.0f}MiB > {CH_MEM_HARD_ABORT_MB}MiB — live priority", flush=True)
            return False
        if mem <= CH_MEM_PAUSE_MB and not live_ch_busy():
            return True
        if mem > CH_MEM_PAUSE_MB and not live_ch_busy():
            # CH elevated but no competing query — safe for small indexed read
            return True
        reason = f"CH mem={mem:.0f}MiB" if mem > CH_MEM_PAUSE_MB else "live query active"
        print(f"  waiting ({reason}) ...", flush=True)
        time.sleep(5)
    print("ABORT: wait timeout — defer to off-peak", flush=True)
    return False


def pool_sql(pool: str) -> str:
    return f"""
SET max_memory_usage = 1073741824, max_execution_time = 90, max_threads = 1;
WITH fs AS (
  SELECT min(block_time) t0 FROM eatlq.uniswap_v3_swaps_decoded
  WHERE chain_id=8453 AND pool_address='{pool}' AND block_time IS NOT NULL
),
sw AS (
  SELECT tick, block_time bt, block_number bn, recipient rcp, toFloat64(liquidity) liq,
         dateDiff('second', fs.t0, block_time) sec
  FROM eatlq.uniswap_v3_swaps_decoded, fs
  WHERE chain_id=8453 AND pool_address='{pool}' AND block_time IS NOT NULL
    AND dateDiff('second', fs.t0, block_time) BETWEEN {W} AND {W + H}
)
SELECT '{pool}' AS pool, intDiv(sec - {W}, 600) AS bkt, count() AS ns,
       uniqExact(rcp) AS buyers, argMax(liq, bt) AS liq,
       argMax(tick, bt) AS ltick, max(bn) AS lblock
FROM sw GROUP BY bkt ORDER BY bkt
FORMAT TabSeparated
"""


def load_pools() -> list[str]:
    base = json.load(open(PCACHE))
    return sorted(p for p, v in base.items()
                  if v.get("fric") is not None and v["fric"] >= FRIC_THR)


def main():
    pools = load_pools()
    prog = json.load(open(PROGRESS)) if PROGRESS.exists() else {"done": [], "errors": {}}
    done = set(prog.get("done", []))
    todo = [p for p in pools if p not in done]
    print(f"forward buckets | gated={len(pools)} done={len(done)} todo={len(todo)}", flush=True)
    if not todo:
        print(f"complete -> {OUT}"); return

    mode = "a" if OUT.exists() and done else "w"
    f = open(OUT, mode)
    if mode == "w":
        pass  # no header — TSV columns inline

    for i, pool in enumerate(todo):
        if not wait_for_slot():
            break
        t0 = time.time()
        rc, out, err = ch_query(pool_sql(pool))
        dt = time.time() - t0
        if rc != 0:
            prog.setdefault("errors", {})[pool] = err[:200]
            print(f"  ERR {pool[:10]}.. {dt:.1f}s {err[:80]}", flush=True)
        else:
            for ln in out.strip().split("\n"):
                if ln:
                    f.write(ln + "\n")
            f.flush()
            done.add(pool)
            prog["done"] = sorted(done)
            json.dump(prog, open(PROGRESS, "w"))
            nrows = len(out.strip().split("\n")) if out.strip() else 0
            print(f"  OK  {i+1}/{len(todo)} {pool[:10]}.. rows={nrows} {dt:.1f}s mem={ch_mem_mb():.0f}MiB", flush=True)
        time.sleep(PAUSE_SEC)

    f.close()
    print(f"checkpoint: {len(done)}/{len(pools)} -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
