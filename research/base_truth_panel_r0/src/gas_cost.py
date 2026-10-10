#!/usr/bin/env python3
"""What a swap and an LP position actually cost in gas on Base, measured not assumed.

Every round so far sized trades at a nominal $5,000, where gas is 0.04 bps and can
be ignored. At $50 it cannot: a $0.05 transaction fee is 10 bps, which is the same
order as the fee tier itself, and the fee tier was the only cost left once slippage
went away. So gas moves from a rounding error to a co-equal cost term, and guessing
it would decide the answer.

Measured from real receipts of real user transactions. On Base a receipt carries
both the L2 execution fee and the L1 data fee, and the L1 component has historically
been the larger and more volatile of the two, so nothing but the receipt total will
do. Sampled across all twelve months because L1 data fees moved by orders of
magnitude over this period.

Three populations:
  swap  -- transactions containing exactly ONE swap log, which is what a simple
           single-hop trade costs. Multi-hop routes would overstate it.
  mint  -- opening a concentrated liquidity position
  burn  -- closing one

Output: out/gas_cost.json, plus the round-trip cost in bps at several trade sizes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict

RPC_URL = open("/tmp/rpc_url.txt").read().strip()
PER_MONTH = 22
BATCH = 20
SIZES_USD = [20, 50, 100, 250, 500, 5000]
_stats = {"calls": 0}


def ch(q: str) -> str:
    p = subprocess.run(["docker", "exec", "-i", "eatlq-clickhouse-1",
                        "clickhouse-client", "--max_execution_time=3600",
                        "--max_memory_usage=12000000000", "--query", q],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout


def rpc_batch(reqs: list) -> list:
    body = json.dumps(reqs).encode()
    req = urllib.request.Request(RPC_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=120).read())
            _stats["calls"] += len(reqs)
            return r if isinstance(r, list) else [r]
        except Exception:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    return []


def _spread(rows: list[tuple[str, str]], per_month: int) -> list[tuple[str, str]]:
    by = defaultdict(list)
    for h, d in rows:
        by[d[:7]].append((h, d))
    out = []
    for m in sorted(by):
        out.extend(sorted(by[m], key=lambda x: x[0])[:per_month])
    return out


def sample_swaps() -> list[tuple[str, str]]:
    """Transactions carrying exactly ONE swap log: what a single-hop trade costs.

    The hash-modulo filter is applied to tx_hash, so every log of a sampled
    transaction is retained and the count()=1 test is meaningful. Grouping all
    424M rows by tx_hash would not be.
    """
    raw = ch("""
    SELECT tx_hash, toString(toDate(min(block_time))) AS d, count() AS n
    FROM census.cl_swaps
    WHERE cityHash64(tx_hash) % 8000 = 0
    GROUP BY tx_hash HAVING n = 1
    """)
    rows = [(x.split("\t")[0], x.split("\t")[1])
            for x in raw.strip().split("\n") if x]
    return _spread(rows, PER_MONTH)


def sample_lp(table: str) -> list[tuple[str, str]]:
    raw = ch(f"""
    SELECT tx_hash, toString(min(block_date)) AS d
    FROM {table} WHERE block_number >= 34300000
    GROUP BY tx_hash
    """)
    rows = [(x.split("\t")[0], x.split("\t")[1])
            for x in raw.strip().split("\n") if x]
    return _spread(rows, PER_MONTH // 2)


def fetch(samples: list[tuple[str, str]]) -> list[dict]:
    out = []
    for i in range(0, len(samples), BATCH):
        grp = samples[i:i + BATCH]
        reqs = [{"jsonrpc": "2.0", "id": j, "method": "eth_getTransactionReceipt",
                 "params": [h]} for j, (h, _) in enumerate(grp)]
        res = rpc_batch(reqs)
        byid = {r.get("id"): r for r in res if isinstance(r, dict)}
        for j, (h, d) in enumerate(grp):
            r = byid.get(j, {}).get("result")
            if not r:
                continue
            try:
                gas_used = int(r["gasUsed"], 16)
                eff = int(r.get("effectiveGasPrice", "0x0"), 16)
                l1 = int(r.get("l1Fee") or "0x0", 16)
                l2_wei = gas_used * eff
                out.append(dict(tx=h, d=d, gas_used=gas_used,
                                l2_eth=l2_wei / 1e18, l1_eth=l1 / 1e18,
                                total_eth=(l2_wei + l1) / 1e18))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def eth_prices() -> dict:
    raw = ch("""SELECT toString(toDate(bar)), quantile(0.5)(eth_usd)
                FROM edge.eth_usd_1h GROUP BY 1""")
    return {a: float(b) for a, b in
            (ln.split("\t") for ln in raw.strip().split("\n") if ln)}


def stats(v):
    s = sorted(v)
    if not s:
        return {}
    q = lambda p: s[min(int(p * len(s)), len(s) - 1)]  # noqa: E731
    return dict(n=len(s), p10=q(0.10), p50=q(0.50), p90=q(0.90), mean=sum(s) / len(s))


def main() -> None:
    px = eth_prices()
    res = {}
    for label, samples in (("swap", sample_swaps()),
                           ("mint", sample_lp("eatlq.uniswap_v3_mints")),
                           ("burn", sample_lp("eatlq.uniswap_v3_burns"))):
        sys.stderr.write(f"[{label}] sampled {len(samples)} txs, fetching ...\n")
        rows = fetch(samples)
        for r in rows:
            r["usd"] = r["total_eth"] * px.get(r["d"], 2300.0)
        res[label] = rows
        sys.stderr.write(f"[{label}] receipts {len(rows)}\n")

    print(f"RPC calls used: {_stats['calls']}  "
          f"(~{_stats['calls'] * 15:,} credits at 15/call)\n")
    print("=" * 96)
    print("what one transaction actually cost, in USD (L2 execution + L1 data fee)")
    print("=" * 96)
    print(f"{'op':<8}{'n':>5}{'gas_used_p50':>14}{'L1_share':>10}"
          f"{'usd_p10':>10}{'usd_p50':>10}{'usd_p90':>10}{'usd_mean':>10}")
    for label, rows in res.items():
        if not rows:
            continue
        s = stats([r["usd"] for r in rows])
        g = stats([r["gas_used"] for r in rows])
        l1share = sum(r["l1_eth"] for r in rows) / max(
            sum(r["total_eth"] for r in rows), 1e-30)
        print(f"{label:<8}{s['n']:>5}{g['p50']:>14,.0f}{100*l1share:>9.1f}%"
              f"{s['p10']:>10.4f}{s['p50']:>10.4f}{s['p90']:>10.4f}"
              f"{s['mean']:>10.4f}")

    print("\nby month, swap only (L1 data fees moved a lot over this year):")
    bym = defaultdict(list)
    for r in res.get("swap", []):
        bym[r["d"][:7]].append(r["usd"])
    print(f"{'month':<10}{'n':>4}{'usd_p50':>10}{'usd_p90':>10}")
    for m in sorted(bym):
        s = stats(bym[m])
        print(f"{m:<10}{s['n']:>4}{s['p50']:>10.4f}{s['p90']:>10.4f}")

    sw = stats([r["usd"] for r in res.get("swap", [])])
    mi = stats([r["usd"] for r in res.get("mint", [])])
    bu = stats([r["usd"] for r in res.get("burn", [])])
    print("\n" + "=" * 96)
    print("what that means as a cost in bps -- gas is FIXED, so it explodes "
          "as the trade shrinks")
    print("=" * 96)
    # The mint/burn tables stop at block 46.2M (about 2026-05), so their USD
    # figures sit in a different L1-fee regime than the swap sample. gas_used is
    # protocol-determined and stable, so LP cost is expressed as a multiple of a
    # swap's gas and priced with the swap sample's own regime.
    g_sw = stats([r["gas_used"] for r in res.get("swap", [])])
    g_mi = stats([r["gas_used"] for r in res.get("mint", [])])
    g_bu = stats([r["gas_used"] for r in res.get("burn", [])])
    lp_mult = ((g_mi.get("p50", 0) + g_bu.get("p50", 0)) / g_sw["p50"]
               if g_sw.get("p50") else float("nan"))
    print(f"  a swap uses {g_sw.get('p50', 0):,.0f} gas; mint+burn uses "
          f"{g_mi.get('p50', 0) + g_bu.get('p50', 0):,.0f} = {lp_mult:.2f}x a swap")
    print(f"{'trade_usd':>10}{'swap_roundtrip_bps':>21}{'p90_bps':>10}"
          f"{'LP_mint+burn_bps':>19}{'p90_bps':>10}")
    for s_usd in SIZES_USD:
        rt = 2 * sw["p50"] / s_usd * 1e4
        rt90 = 2 * sw["p90"] / s_usd * 1e4
        lp = lp_mult * sw["p50"] / s_usd * 1e4
        lp90 = lp_mult * sw["p90"] / s_usd * 1e4
        print(f"{s_usd:>10}{rt:>21.1f}{rt90:>10.1f}{lp:>19.1f}{lp90:>10.1f}")

    json.dump({k: v for k, v in res.items()}, open("out/gas_cost.json", "w"))
    json.dump(dict(swap=sw, mint=mi, burn=bu),
              open("out/gas_summary.json", "w"), indent=1)


if __name__ == "__main__":
    main()
