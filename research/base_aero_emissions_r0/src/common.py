"""Shared constants + ClickHouse helpers."""

import csv
import io
import json
import subprocess

CH_CONTAINER = "eatlq-clickhouse-1"

# --- Aerodrome core (verified on-chain, see out/contracts.json) ---
VOTER = "0x16613524e02ad97eDfeF371bC883F2F5d6C480A5"
MINTER = "0xeB018363F0a9Af8f91F06FEe6613a751b2A33FE5"
VOTING_ESCROW = "0xeBf418Fe2512e7E6bd9b87a8F0f294aCDC67e6B4"
AERO = "0x940181a94A35A4569E4529A3CDfB74e38FD98631"
WETH = "0x4200000000000000000000000000000000000006"

# --- function selectors (cast sig, cross-checked with pure-python keccak) ---
SEL = {
    "gauges(address)": "0xb9a09fd5",
    "poolForGauge(address)": "0x06d6a1b2",
    "isAlive(address)": "0x1703e5f9",
    "weights(address)": "0xa7cac846",
    "totalWeight()": "0x96c82e57",
    "rewardsByEpoch(uint256)": "0x06dd86c8",
    "rewardRateByEpoch(uint256)": "0x94af5b63",
    "pool()": "0x16f0115b",
    "gaugeFactory()": "0x0d52333c",
    "rewardToken()": "0xf7c618c1",
    "nft()": "0x47ccca02",
    "periodFinish()": "0xebe2b12b",
    "stakedLiquidity()": "0x3ab04b20",
    "liquidity()": "0x1a686502",
    "tickSpacing()": "0xd0c93a7c",
    "token0()": "0x0dfe1681",
    "token1()": "0xd21220a7",
    "slot0()": "0x3850c7bd",
    "gauge()": "0xa6f19c84",
    "weekly()": "0x26cfc17b",
    "activePeriod()": "0x0a441f7b",
    "totalSupply()": "0x18160ddd",
    "balanceOf(address)": "0x70a08231",
    "decimals()": "0x313ce567",
    "symbol()": "0x95d89b41",
    "implementation()": "0x5c60da1b",
    "minter()": "0x07546172",
    "ve()": "0x1f850716",
    "factoryRegistry()": "0x3bf0c9fb",
    "aero()": "0x26837eda",
    "voter()": "0x46c96aac",
    "token()": "0xfc0c546a",
    "owner()": "0x8da5cb5b",
    "notifyAdmin()": "0x447595b4",
}

# --- event topic0 (keccak256 of canonical signature; computed two ways) ---
TOPIC0 = {
    "NotifyReward(address,uint256)":
        "0x095667752957714306e1a6ad83495404412df6fdb932fca6dc849a7ee910d4c1",
    "Deposit(address,uint256,uint128)":
        "0x1c8ab8c7f45390d58f58f1d655213a82cca5d12179761a87c16f098813b8f211",
    "Withdraw(address,uint256,uint128)":
        "0x8903a5b5d08a841e7f68438387f1da20c84dea756379ed37e633ff3854b99b84",
    "ClaimRewards(address,uint256)":
        "0x1f89f96333d3133000ee447473151fa9606543368f02271c9d95ae14f13bcc67",
    "ClaimFees(address,uint256,uint256)":
        "0xbc567d6cbad26368064baa0ab5a757be46aae4d70f707f9203d9d9b6c8ccbfa3",
    "Voted(address,address,uint256,uint256,uint256,uint256)":
        "0x452d440efc30dfa14a0ef803ccb55936af860ec6a6960ed27f129bef913f296a",
    # NOTE: the Voter's GaugeCreated takes 8 args (trailing `creator`), not 7.
    "GaugeCreated(address,address,address,address,address,address,address,address)":
        "0xef9f7d1ffff3b249c6b9bf2528499e935f7d96bb6d6ec4e7da504d1d3c6279e1",
    "GaugeKilled(address)":
        "0x04a5d3f5d80d22d9345acc80618f4a4e7e663cf9e1aed23b57d975acec002ba7",
    "GaugeRevived(address)":
        "0xed18e9faa3dccfd8aa45f69c4de40546b2ca9cccc4538a2323531656516db1aa",
    "DistributeReward(address,address,uint256)":
        "0x4fa9693cae526341d334e2862ca2413b2e503f1266255f9e0869fb36e6d89b17",
}

OUT = "/root/base_aero_emissions_r0/out"

# Panel: Base blocks covering 2025-08-18 .. 2026-08-17 (inclusive, UTC).
from datetime import datetime, timezone  # noqa: E402

PANEL_TS_FROM = int(datetime(2025, 8, 18, 0, 0, 0, tzinfo=timezone.utc).timestamp())
PANEL_TS_TO = int(datetime(2026, 8, 17, 23, 59, 59, tzinfo=timezone.utc).timestamp())


def ch(query, fmt="TabSeparatedWithNames", timeout=600):
    # Cap memory: the box is shared and an unbounded query already OOM-restarted the server.
    cmd = ["docker", "exec", "-i", CH_CONTAINER, "clickhouse-client",
           "--max_execution_time", "900",
           "--max_memory_usage", "3000000000",
           "--max_bytes_before_external_group_by", "1500000000",
           "--max_bytes_before_external_sort", "1500000000"]
    if fmt:
        cmd += ["--format", fmt]
    cmd += ["--query", query]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"clickhouse error: {r.stderr[:3000]}")
    return r.stdout


def ch_rows(query):
    txt = ch(query, fmt="TabSeparatedWithNames")
    lines = txt.rstrip("\n").split("\n")
    if not lines or not lines[0]:
        return []
    hdr = lines[0].split("\t")
    return [dict(zip(hdr, ln.split("\t"))) for ln in lines[1:] if ln]


def ch_insert_tsv(table, columns, rows):
    """rows: iterable of tuples matching columns."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n",
                   quoting=csv.QUOTE_NONE, escapechar="\\")
    n = 0
    for r in rows:
        w.writerow(r)
        n += 1
    cmd = ["docker", "exec", "-i", CH_CONTAINER, "clickhouse-client",
           "--query", f"INSERT INTO {table} ({','.join(columns)}) FORMAT TabSeparated"]
    r = subprocess.run(cmd, input=buf.getvalue(), capture_output=True, text=True, timeout=1200)
    if r.returncode != 0:
        raise RuntimeError(f"insert failed: {r.stderr[:3000]}")
    return n


def save_json(name, obj):
    with open(f"{OUT}/{name}", "w") as fh:
        json.dump(obj, fh, indent=2)


def load_json(name):
    with open(f"{OUT}/{name}") as fh:
        return json.load(fh)
