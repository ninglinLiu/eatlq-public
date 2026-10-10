"""Stage 2: exact fee revenue per position, from swap-level data.

For every swap inside a position's holding window whose tick sits inside the
position's range:

    fee_weth = |WETH leg| * fee_rate * L_mine / (L_active + L_mine)

L_active is the historical active liquidity at that swap (before my position
existed), so L_active + L_mine is the post-entry denominator. Omitting L_mine
from the denominator would systematically overstate revenue.

Non-overlapping windows let us avoid a fan-out join: a swap's window is a pure
function of its date, so we derive the entry date on the swap side and join on
an exact (pool, entry_date) key instead of a range predicate.
"""
import json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
BASE = None          # first entry date, filled from positions
T_GRID = [1, 7, 30]


def ch(query, fmt="TabSeparatedWithNames", stdin=None):
    cmd = ["docker", "exec"]
    if stdin is not None:
        cmd.append("-i")
    cmd += ["eatlq-clickhouse-1", "clickhouse-client",
            "--max_execution_time=7200", "--max_memory_usage=40000000000",
            "--format", fmt, "--query", query]
    p = subprocess.run(cmd, capture_output=True, text=True, input=stdin)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:6000])
        raise SystemExit("clickhouse failed")
    return p.stdout


def main():
    pos = json.load(open(os.path.join(OUT, "positions.json")))
    global BASE
    BASE = min(p["entry"] for p in pos)
    sys.stderr.write("positions: %d   base entry date: %s\n" % (len(pos), BASE))

    # ---- load positions into ClickHouse ----
    ch("DROP TABLE IF EXISTS census.lp_pos", fmt="TSV")
    ch("""CREATE TABLE census.lp_pos (
            pos_id UInt32, pool String, entry Date, T UInt16, c Float64,
            tick_lower Int32, tick_upper Int32, L Float64, weth_is_token1 UInt8,
            fee UInt32
          ) ENGINE = MergeTree ORDER BY (T, pool, entry)""", fmt="TSV")

    tsv = []
    for i, p in enumerate(pos):
        tsv.append("\t".join(map(str, [
            i, p["pool"], p["entry"], p["T"], p["c"], p["tick_lower"],
            p["tick_upper"], p["L"], p["weth_is_token1"], p["fee"]])))
    ch("INSERT INTO census.lp_pos FORMAT TabSeparated", fmt="TSV",
       stdin="\n".join(tsv) + "\n")
    n = ch("SELECT count() FROM census.lp_pos", fmt="TSV").strip()
    sys.stderr.write("loaded into census.lp_pos: %s rows\n" % n)

    # ---- per-T fee aggregation ----
    results = {}
    for T in T_GRID:
        sys.stderr.write("aggregating fees for T=%d ...\n" % T)
        q = """
        WITH sw AS (
            SELECT s.pool AS pool, s.tick AS tick,
                   toFloat64(s.liquidity) AS Lact,
                   abs(toFloat64(if(pl.token0 = '0x4200000000000000000000000000000000000006',
                                    s.amount0, s.amount1))) AS weth_amt,
                   toDate('{base}') + {T} * intDiv(toDate(s.block_time) - toDate('{base}'), {T}) AS entry_d
            FROM census.cl_swaps s
            INNER JOIN census.pools pl ON pl.pool = s.pool
            WHERE s.pool IN (SELECT DISTINCT pool FROM census.lp_pos WHERE T = {T})
              AND s.block_time >= toDate('{base}')
              AND s.sqrt_price_x96 > 0
        )
        SELECT p.pos_id AS pos_id,
               sum(if(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper,
                      sw.weth_amt * (p.fee / 1e6) * p.L / (sw.Lact + p.L), 0)) AS fees_weth_raw,
               countIf(sw.tick >= p.tick_lower AND sw.tick < p.tick_upper) AS n_in_range,
               count() AS n_window,
               sum(sw.weth_amt) AS window_weth_volume_raw
        FROM sw
        INNER JOIN census.lp_pos p ON p.pool = sw.pool AND p.entry = sw.entry_d
        WHERE p.T = {T}
        GROUP BY p.pos_id
        """.format(base=BASE, T=T)
        out = ch(q)
        cnt = 0
        for ln in out.strip().split("\n")[1:]:
            if not ln:
                continue
            pid, fees, nir, nw, vol = ln.split("\t")
            results[int(pid)] = dict(fees_weth=float(fees) / 1e18,
                                     n_in_range=int(nir), n_window=int(nw),
                                     window_weth_volume=float(vol) / 1e18)
            cnt += 1
        sys.stderr.write("  T=%d: %d positions with fee data\n" % (T, cnt))

    missing = [i for i in range(len(pos)) if i not in results]
    sys.stderr.write("positions with no swaps in window: %d\n" % len(missing))
    for i in missing:
        results[i] = dict(fees_weth=0.0, n_in_range=0, n_window=0,
                          window_weth_volume=0.0)

    with open(os.path.join(OUT, "fees.json"), "w") as f:
        json.dump({str(k): v for k, v in results.items()}, f)
    sys.stderr.write("wrote out/fees.json\n")

    # sanity readout, not a result: are fee shares plausible?
    inr = [r["n_in_range"] / r["n_window"] for r in results.values() if r["n_window"] > 0]
    inr.sort()
    if inr:
        sys.stderr.write("fraction of window swaps in range: p10=%.3f p50=%.3f p90=%.3f\n"
                         % (inr[int(len(inr) * .1)], inr[len(inr) // 2], inr[int(len(inr) * .9)]))


if __name__ == "__main__":
    main()
