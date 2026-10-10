#!/usr/bin/env python3
"""All Base Uniswap V3 WETH dump-reclaim signals. CH only."""
from __future__ import annotations

import json
import math
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/root/base_b20_dump_reclaim_r0/src")
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
        raise RuntimeError((proc.stderr or proc.stdout)[-4000:])
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
    pre_sql = f"""
    WITH prim AS (
      SELECT
        lower(pool_address) AS pool,
        lower(if(lower(token0_address)='{WETH_L}', token1_address, token0_address)) AS token,
        if(lower(token0_address)='{WETH_L}', token1_symbol, token0_symbol) AS sym,
        if(lower(token0_address)='{WETH_L}', -1, 1) AS orient,
        fee_bps * 100 AS fee,
        created_block,
        row_number() OVER (
          PARTITION BY lower(if(lower(token0_address)='{WETH_L}', token1_address, token0_address))
          ORDER BY created_block ASC, pool_address ASC
        ) AS rn
      FROM eatlq.discovered_pools
      WHERE pool_type='v3' AND has_weth_leg=1 AND fee_bps IN {FEE_BPS_OK}
    ),
    p AS (SELECT * FROM prim WHERE rn=1),
    fs AS (
      SELECT
        lower(pool_address) AS pool,
        min(block_time) AS t0,
        min(block_number) AS b0,
        argMin(tick, (block_time, block_number)) AS tick0
      FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id={CHAIN_ID}
        AND lower(pool_address) IN (SELECT pool FROM p)
      GROUP BY pool
    ),
    pre AS (
      SELECT
        lower(s.pool_address) AS pool,
        count() AS ns_pre,
        argMax(s.tick, (s.block_time, s.block_number)) AS last_tick_pre
      FROM eatlq.uniswap_v3_swaps_decoded s
      INNER JOIN fs ON lower(s.pool_address)=fs.pool
      WHERE s.chain_id={CHAIN_ID}
        AND dateDiff('second', fs.t0, s.block_time) BETWEEN 0 AND {PRE_SEC}
      GROUP BY pool
      HAVING ns_pre >= {MIN_PRE}
    )
    SELECT
      p.pool AS pool, p.token AS token, p.sym AS sym, p.orient AS orient,
      p.fee AS fee, p.created_block AS created_block,
      toUnixTimestamp(fs.t0) AS t0_ts, fs.b0 AS t0_block, fs.tick0 AS tick0,
      pre.ns_pre AS ns_pre, pre.last_tick_pre AS last_tick_pre
    FROM p
    INNER JOIN fs ON p.pool=fs.pool
    INNER JOIN pre ON p.pool=pre.pool
    """
    print("querying min-pre universe...", flush=True)
    minpre = ch_rows(pre_sql)
    for r in minpre:
        r["early_ret"] = (int(r["last_tick_pre"]) - int(r["tick0"])) * LN1P * int(r["orient"])
        r["is_b20"] = str(r["token"]).startswith("0xb200")
    dumps = [r for r in minpre if r["early_ret"] <= DUMP_LOG]
    print(f"minpre={len(minpre)} dump={len(dumps)}", flush=True)
    if not dumps:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "universe": {"n_minpre": len(minpre), "n_dump": 0, "n_arm_a": 0},
            "arm_a": [],
            "minpre_dump": [],
        }
        (OUT / "signals.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print("no dumps")
        return 0

    in_list = ",".join(f"'{r['pool']}'" for r in dumps)
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
    print("pulling dump-pool paths...", flush=True)
    by: dict[str, list[dict]] = defaultdict(list)
    for row in ch_rows(sw_sql):
        by[row["pool"]].append(row)

    meta = {r["pool"]: r for r in dumps}
    arm_a = []
    for pool, m in meta.items():
        path = by.get(pool) or []
        if not path:
            continue
        t0_ts = int(m["t0_ts"])
        t0_tick = int(m["tick0"])
        orient = int(m["orient"])
        rec = rec_idx = None
        for i, s in enumerate(path):
            age = int(s["ts"]) - t0_ts
            if age <= PRE_SEC or age > SEARCH_SEC:
                continue
            if (int(s["tick"]) - t0_tick) * LN1P * orient >= RECOVER_LOG:
                rec, rec_idx = s, i
                break
        if rec is None:
            continue
        ex = walk_exit(path, rec_idx, orient, int(rec["block_number"]))
        arm_a.append({
            "pool": pool,
            "token": m["token"],
            "sym": m["sym"],
            "fee": int(m["fee"]),
            "orient": orient,
            "is_b20": m["is_b20"],
            "created_block": int(m["created_block"]),
            "t0_block": int(m["t0_block"]),
            "t0_ts": t0_ts,
            "ns_pre": int(m["ns_pre"]),
            "early_ret": m["early_ret"],
            "recover_ret": (int(rec["tick"]) - t0_tick) * LN1P * orient,
            "entry_block": int(rec["block_number"]),
            "entry_tick": int(rec["tick"]),
            **ex,
        })

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe": {
            "kind": "all_base_uniswap_v3_weth",
            "n_minpre": len(minpre),
            "n_dump": len(dumps),
            "n_arm_a": len(arm_a),
            "n_arm_a_b20": sum(1 for r in arm_a if r["is_b20"]),
        },
        "arm_a": arm_a,
        "dumps": [{"pool": r["pool"], "sym": r["sym"], "early_ret": r["early_ret"], "is_b20": r["is_b20"]} for r in dumps],
    }
    (OUT / "signals.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("universe", json.dumps(payload["universe"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
