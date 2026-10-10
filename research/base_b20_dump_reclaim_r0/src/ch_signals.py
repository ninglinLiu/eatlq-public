#!/usr/bin/env python3
"""Build PIT B20 dump-reclaim signals from ClickHouse. No RPC."""
from __future__ import annotations

import json
import math
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from params import (  # noqa: E402
    CHAIN_ID,
    DUMP_LOG,
    FEE_BPS_OK,
    FWD_PULL_SEC,
    HARD_STOP,
    HOLD_BLOCKS,
    LN1P,
    MIN_PRE,
    PRE_SEC,
    RECOVER_LOG,
    SEARCH_SEC,
    TRAIL,
    WETH,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
WETH_L = WETH.lower()


def ch(sql: str) -> str:
    proc = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query", sql],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout)[-2000:])
    return proc.stdout


def ch_rows(sql: str) -> list[dict]:
    raw = ch(sql + "\nFORMAT JSONEachRow\n")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def walk_exit(swaps: list[dict], entry_idx: int, orient: int, entry_block: int) -> dict:
    entry_tick = int(swaps[entry_idx]["tick"])
    entry_lp = entry_tick * LN1P * orient
    peak = entry_lp
    last = swaps[entry_idx]
    cap = entry_block + HOLD_BLOCKS
    for s in swaps[entry_idx + 1 :]:
        blk = int(s["block_number"])
        if blk > cap:
            break
        last = s
        lp = int(s["tick"]) * LN1P * orient
        peak = max(peak, lp)
        if (lp - entry_lp) <= HARD_STOP:
            return {"exit_block": blk, "exit_why": "stop", "exit_tick": int(s["tick"])}
        if (lp - peak) <= math.log(1.0 - TRAIL):
            return {"exit_block": blk, "exit_why": "trail", "exit_tick": int(s["tick"])}
    return {
        "exit_block": int(last["block_number"]),
        "exit_why": "hold_end" if int(last["block_number"]) != entry_block else "no_fwd",
        "exit_tick": int(last["tick"]),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    uni_sql = f"""
    WITH b20 AS (
      SELECT
        lower(pool_address) AS pool,
        lower(if(lower(token0_address)='{WETH_L}', token1_address, token0_address)) AS token,
        if(lower(token0_address)='{WETH_L}', token1_symbol, token0_symbol) AS sym,
        if(lower(token0_address)='{WETH_L}', -1, 1) AS orient,
        fee_bps * 100 AS fee,
        created_block
      FROM eatlq.discovered_pools
      WHERE pool_type='v3' AND has_weth_leg=1
        AND (startsWith(lower(token0_address),'0xb200') OR startsWith(lower(token1_address),'0xb200'))
        AND fee_bps IN {FEE_BPS_OK}
    ),
    ranked AS (
      SELECT *, row_number() OVER (PARTITION BY token ORDER BY created_block ASC, pool ASC) AS rn
      FROM b20
    )
    SELECT pool, token, sym, orient, fee, created_block
    FROM ranked WHERE rn=1
    """
    prim = ch_rows(uni_sql)
    pools = [r["pool"] for r in prim]
    meta = {r["pool"]: r for r in prim}
    if not pools:
        raise SystemExit("no primary B20 V3 WETH pools")

    in_list = ",".join(f"'{p}'" for p in pools)
    t0_sql = f"""
    SELECT
      lower(pool_address) AS pool,
      min(block_time) AS t0,
      min(block_number) AS b0,
      count() AS nswap
    FROM eatlq.uniswap_v3_swaps_decoded
    WHERE chain_id={CHAIN_ID} AND lower(pool_address) IN ({in_list})
    GROUP BY pool
    """
    t0s = {r["pool"]: r for r in ch_rows(t0_sql)}

    sw_sql = f"""
    WITH t0 AS (
      SELECT lower(pool_address) AS pool, min(block_time) AS t0
      FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id={CHAIN_ID} AND lower(pool_address) IN ({in_list})
      GROUP BY pool
    )
    SELECT
      lower(s.pool_address) AS pool,
      s.block_number AS block_number,
      toUnixTimestamp(s.block_time) AS ts,
      s.tick AS tick
    FROM eatlq.uniswap_v3_swaps_decoded s
    INNER JOIN t0 ON lower(s.pool_address)=t0.pool
    WHERE s.chain_id={CHAIN_ID}
      AND dateDiff('second', t0.t0, s.block_time) BETWEEN 0 AND {int(FWD_PULL_SEC)}
    ORDER BY pool, block_number, ts
    """
    by: dict[str, list[dict]] = defaultdict(list)
    for row in ch_rows(sw_sql):
        by[row["pool"]].append(row)

    arm_a = []
    arm_b = []
    minpre_rows = []
    n_minpre = 0
    n_dump = 0
    for pool, m in meta.items():
        path = by.get(pool) or []
        if len(path) < MIN_PRE:
            continue
        t0_ts = int(path[0]["ts"])
        t0_tick = int(path[0]["tick"])
        orient = int(m["orient"])
        pre = [s for s in path if int(s["ts"]) - t0_ts <= PRE_SEC]
        if len(pre) < MIN_PRE:
            continue
        n_minpre += 1
        last_pre = pre[-1]
        early = (int(last_pre["tick"]) - t0_tick) * LN1P * orient
        minpre_rows.append({
            "pool": pool,
            "token": m["token"],
            "sym": m["sym"],
            "early_ret": early,
            "ns_pre": len(pre),
            "t0_block": int(path[0]["block_number"]),
        })
        # Arm B: first swap at/after +600s
        post = [s for s in path if int(s["ts"]) - t0_ts >= PRE_SEC]
        if not post:
            continue
        b_entry = post[0]
        b_idx = next(i for i, s in enumerate(path) if s is b_entry or (
            int(s["block_number"]) == int(b_entry["block_number"]) and int(s["ts"]) == int(b_entry["ts"])
        ))
        b_ex = walk_exit(path, b_idx, orient, int(b_entry["block_number"]))
        arm_b.append({
            "pool": pool,
            "token": m["token"],
            "sym": m["sym"],
            "fee": int(m["fee"]),
            "orient": orient,
            "t0_block": int(path[0]["block_number"]),
            "t0_ts": t0_ts,
            "ns_pre": len(pre),
            "early_ret": early,
            "entry_block": int(b_entry["block_number"]),
            "entry_tick": int(b_entry["tick"]),
            **b_ex,
        })
        if early > DUMP_LOG:
            continue
        n_dump += 1
        rec = None
        rec_idx = None
        for i, s in enumerate(path):
            age = int(s["ts"]) - t0_ts
            if age <= PRE_SEC or age > SEARCH_SEC:
                continue
            ret = (int(s["tick"]) - t0_tick) * LN1P * orient
            if ret >= RECOVER_LOG:
                rec, rec_idx = s, i
                break
        if rec is None:
            continue
        a_ex = walk_exit(path, rec_idx, orient, int(rec["block_number"]))
        arm_a.append({
            "pool": pool,
            "token": m["token"],
            "sym": m["sym"],
            "fee": int(m["fee"]),
            "orient": orient,
            "created_block": int(m["created_block"]),
            "t0_block": int(path[0]["block_number"]),
            "t0_ts": t0_ts,
            "ns_pre": len(pre),
            "early_ret": early,
            "recover_ret": (int(rec["tick"]) - t0_tick) * LN1P * orient,
            "entry_block": int(rec["block_number"]),
            "entry_tick": int(rec["tick"]),
            **a_ex,
        })

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe": {
            "n_primary": len(prim),
            "n_with_swaps": len(t0s),
            "n_minpre": n_minpre,
            "n_dump": n_dump,
            "n_arm_a": len(arm_a),
            "n_arm_b": len(arm_b),
        },
        "arm_a": arm_a,
        "arm_b": arm_b,
        "minpre": minpre_rows,
    }
    (OUT / "signals.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("universe", json.dumps(payload["universe"]))
    print(f"wrote {OUT / 'signals.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
