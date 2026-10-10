#!/usr/bin/env python3
"""Sprint054 — Real RPC net_return_5m label for Sprint053 canonical candidates.

Research-only. No private key, no live, no training, no production signals.

Uses QuoterV2 `quoteExactInputSingle` to compute the ACTUAL 5-minute WETH→TOKEN
entry quote and TOKEN→WETH exit quote at the historical blocks for each
Sprint053 canonical gate candidate.

Covers ALL canonical gate candidates (592 rows) across ALL fee tiers, giving:
  - fee_100bps vs fee_3000bps vs fee_500bps vs fee_10000bps comparison
  - Sep-28 pre-bundle vs April post-bundle comparison
  - proxy label (sqrtPriceX96) vs real label (QuoterV2 net_return)

RPC estimate:
  - pool.token1(): ~50 calls (one-off per pool, needed where token0=WETH)
  - QuoterV2 entry: 592 calls
  - QuoterV2 exit: 592 calls
  - Total: ~1,234 calls (budget: 250k)
"""

from __future__ import annotations

import bisect
import csv
import io
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.lib.v3_quoterv2_struct import (
    JsonRpcClient,
    WETH_BASE,
    QUOTER_V2_BASE,
    quote_exact_input_single_struct,
)

ROOT = Path(__file__).resolve().parents[1]
AUDIT_OUT = ROOT / "mvp/audits/sprint054"
DIAG_OUT  = ROOT / "mvp/diagnostics/sprint054"
AUDIT_OUT.mkdir(parents=True, exist_ok=True)
DIAG_OUT.mkdir(parents=True, exist_ok=True)

S053_CANDS = ROOT / "mvp/diagnostics/sprint053/oos_candidates_gated.json"
S053_META  = ROOT / "mvp/diagnostics/sprint053/pool_meta.json"

CH_CONTAINER   = "eatlq-clickhouse-1"
CHAIN_ID       = 8453
WINDOW_SEC     = 300
ENTRY_WETH_RAW = int(0.00005 * 10**18)  # 5e13 wei (micro notional, consistent with q4 bundle)
GAS_PROXY_PCT  = 0.002                   # 0.2% gas+slippage proxy (same as Sprint050 econ pre-val)
SEL_TOKEN1     = "0xd21220a7"            # pool.token1() selector
RPC_DELAY      = 0.04                    # 25 rps

# Sprint050 in-bundle benchmarks for comparison
S050_BENCH = {
    "fee_100bps":  {"rows": 1513, "hit": 0.495, "pf": 1.79},
    "fee_3000bps": {"rows": 3797, "hit": 0.585, "pf": 2.31},
    "fee_500bps":  {"rows": 348,  "hit": 0.515, "pf": 2.36},
    "fee_10000bps":{"rows": 6363, "hit": 0.221, "pf": 0.77},
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ch(sql: str, timeout: int = 180) -> list[list[str]]:
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client", "--format", "CSV", "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH: {r.stderr.strip()[:300]}")
    return [row for row in csv.reader(io.StringIO(r.stdout)) if row]


def _load_env_rpc() -> str:
    env_path = ROOT / "infra/compose/.env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            if k.strip() in ("LABEL_RPC_URL", "EVM_HTTP_URL"):
                v = v.strip().strip('"').strip("'")
                if v and "http" in v:
                    return v
    return os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")


# ---------------------------------------------------------------------------
# Block lookup from CH
# ---------------------------------------------------------------------------

def build_block_index(week_start: str, week_end: str, pools: set[str]) -> dict[str, list[tuple[int, int]]]:
    """Return dict: pool → sorted list of (block_time_ts, block_number)."""
    if not pools:
        return {}
    pl = ",".join(f"'{p}'" for p in pools)
    # extend end by 10m to capture exit blocks
    end_dt = (datetime.strptime(week_end, "%Y-%m-%d").replace(tzinfo=timezone.utc))
    end_ext = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    rows = _ch(f"""
SELECT pool_address, toUnixTimestamp(block_time) AS ts, block_number
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id = {CHAIN_ID}
  AND block_time >= toDateTime('{week_start}')
  AND block_time <  toDateTime('{end_ext}')
  AND block_time IS NOT NULL
  AND pool_address IN ({pl})
ORDER BY pool_address, ts
""", timeout=120)
    idx: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for r in rows:
        if len(r) >= 3:
            try:
                idx[r[0]].append((int(r[1]), int(r[2])))
            except (ValueError, OverflowError):
                pass
    return dict(idx)


def resolve_blocks(pool: str, wts: int, idx: dict[str, list[tuple[int, int]]]) -> tuple[int | None, int | None]:
    """Return (entry_block, exit_block) for a candidate (pool, wts)."""
    ticks = idx.get(pool, [])
    if not ticks:
        return None, None
    ts_list = [t[0] for t in ticks]
    blk_list = [t[1] for t in ticks]
    # entry_block: last block in [wts, wts+300)
    end_entry = wts + WINDOW_SEC
    # find last index with ts < end_entry
    i_entry = bisect.bisect_left(ts_list, end_entry) - 1
    if i_entry < 0:
        return None, None
    entry_block = blk_list[i_entry]
    # exit_block: first block in [wts+300, wts+600)
    end_exit = wts + 2 * WINDOW_SEC
    i_exit = bisect.bisect_left(ts_list, end_entry)
    if i_exit >= len(ts_list) or ts_list[i_exit] >= end_exit:
        return None, None
    exit_block = blk_list[i_exit]
    return entry_block, exit_block


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

def get_token1(rpc: JsonRpcClient, pool: str) -> str | None:
    """Call pool.token1() view. Returns lowercase address or None."""
    import requests
    try:
        resp = rpc.session.post(
            rpc.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                  "params": [{"to": pool, "data": SEL_TOKEN1}, "latest"]},
            timeout=15,
        )
        result = resp.json().get("result", "0x")
        if result and len(result) >= 42:
            return ("0x" + result[-40:]).lower()
    except Exception:
        pass
    return None


def run_quote(rpc: JsonRpcClient, token_in: str, token_out: str, fee_bps: int,
              amount_in: int, entry_block: int, exit_block: int) -> dict:
    """QuoterV2: entry (WETH→TOKEN at entry_block), exit (TOKEN→WETH at exit_block).
    Returns net_return_5m (WETH-normalized), entry/exit quote details."""
    # Entry quote: WETH → TOKEN at entry_block
    q_entry = quote_exact_input_single_struct(
        rpc, token_in, token_out, amount_in, fee_bps,
        block_identifier=entry_block,
    )
    if not q_entry.get("ok"):
        return {
            "entry_quote_ok": False,
            "exit_quote_ok": False,
            "net_return_5m": None,
            "entry_amount_out": None,
            "exit_amount_out_weth": None,
            "fail_reason": "entry_" + (q_entry.get("reason") or "fail"),
        }
    token_amount = int(q_entry["amount_out"])
    # Exit quote: TOKEN → WETH at exit_block (using same token amount from entry)
    q_exit = quote_exact_input_single_struct(
        rpc, token_out, token_in, token_amount, fee_bps,
        block_identifier=exit_block,
    )
    if not q_exit.get("ok"):
        return {
            "entry_quote_ok": True,
            "exit_quote_ok": False,
            "net_return_5m": None,
            "entry_amount_out": token_amount,
            "exit_amount_out_weth": None,
            "fail_reason": "exit_" + (q_exit.get("reason") or "fail"),
        }
    exit_weth = int(q_exit["amount_out"])
    net_ret = exit_weth / amount_in - 1.0
    return {
        "entry_quote_ok": True,
        "exit_quote_ok": True,
        "net_return_5m": round(net_ret, 8),
        "entry_amount_out": token_amount,
        "exit_amount_out_weth": exit_weth,
        "fail_reason": "",
    }


# ---------------------------------------------------------------------------
# Main labeling loop
# ---------------------------------------------------------------------------

def label_all_candidates(
    cands: list[dict],
    pool_meta: dict[str, dict],
    rpc: JsonRpcClient,
) -> list[dict]:
    # Step 1: collect all pools needing token1
    need_token1 = {
        c["pool_address"]
        for c in cands
        if pool_meta.get(c["pool_address"], {}).get("weth_is_token0") is True
        and pool_meta.get(c["pool_address"], {}).get("token1") is None
    }
    token1_map: dict[str, str | None] = {}
    print(f"  [rpc] fetching token1 for {len(need_token1)} pools ...", flush=True)
    for pool in sorted(need_token1):
        t1 = get_token1(rpc, pool)
        token1_map[pool] = t1
        time.sleep(RPC_DELAY)

    # Step 2: build block indices per week
    weeks = sorted({c["regime_week"] for c in cands})
    block_idx: dict[str, dict[str, list]] = {}  # week → pool → ticks
    for wk in weeks:
        wk_pools = {c["pool_address"] for c in cands if c["regime_week"] == wk}
        print(f"  [ch] block index for week {wk} ({len(wk_pools)} pools) ...", flush=True)
        # week_end: wk + 7 days to encompass exit window
        wk_start = wk
        from datetime import timedelta
        wk_end_dt = datetime.strptime(wk, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=7, minutes=10)
        wk_end = wk_end_dt.strftime("%Y-%m-%d")
        block_idx[wk] = build_block_index(wk_start, wk_end, wk_pools)

    # Step 3: label each candidate
    out = []
    total = len(cands)
    for i, c in enumerate(cands):
        if i % 50 == 0:
            print(f"  [quote] {i}/{total} (rpc_calls={rpc.calls}) ...", flush=True)
        pool = c["pool_address"]
        wts  = c["wts"]
        wk   = c["regime_week"]
        meta = pool_meta.get(pool, {})
        fee_bps = meta.get("fee_bps") or 100
        weth0   = meta.get("weth_is_token0")
        t0      = meta.get("token0", "").lower()
        t1_known= meta.get("token1", token1_map.get(pool))

        # Determine WETH and TOKEN
        if weth0 is True:
            weth_tok  = t0   # = WETH
            other_tok = t1_known
        elif weth0 is False:
            weth_tok  = WETH_BASE
            other_tok = t0
        else:
            # Unknown direction — skip (shouldn't happen for fee-enriched pools)
            out.append({**c, "entry_quote_ok": False, "exit_quote_ok": False,
                        "net_return_5m": None, "fail_reason": "unknown_weth_direction"})
            continue

        if not other_tok:
            out.append({**c, "entry_quote_ok": False, "exit_quote_ok": False,
                        "net_return_5m": None, "fail_reason": "token1_missing"})
            continue

        # Resolve blocks
        entry_block, exit_block = resolve_blocks(pool, wts, block_idx.get(wk, {}))
        if entry_block is None or exit_block is None:
            out.append({**c, "entry_quote_ok": False, "exit_quote_ok": False,
                        "net_return_5m": None, "fail_reason": "block_not_found"})
            continue

        # Quote
        try:
            q = run_quote(rpc, WETH_BASE, other_tok, fee_bps,
                          ENTRY_WETH_RAW, entry_block, exit_block)
        except Exception as exc:
            q = {"entry_quote_ok": False, "exit_quote_ok": False,
                 "net_return_5m": None, "fail_reason": str(exc)[:120]}
        time.sleep(RPC_DELAY)  # brief pause between candidates

        out.append({
            **c,
            "entry_block": entry_block,
            "exit_block": exit_block,
            "entry_weth_raw": ENTRY_WETH_RAW,
            "fee_bps_used": fee_bps,
            "token_other": other_tok,
            "weth_is_token0": weth0,
            "fee_label": _fee_label(fee_bps),
            "heat_bucket": _heat_bucket(c.get("activity_heat", 0)),
            **q,
        })

    return out


def _fee_label(f: int | None) -> str:
    if f is None: return "fee_unknown"
    if f == 100: return "fee_100bps"
    if f == 500: return "fee_500bps"
    if f == 3000: return "fee_3000bps"
    if f == 10000: return "fee_10000bps"
    return f"fee_{f}bps"


def _heat_bucket(h: int) -> str:
    if h < 20:  return "heat_lt_20"
    if h < 50:  return "heat_20_50"
    if h < 100: return "heat_50_100"
    if h < 250: return "heat_100_250"
    return "heat_ge_250"


# ---------------------------------------------------------------------------
# Statistics and matrix
# ---------------------------------------------------------------------------

GAS_PROXY = GAS_PROXY_PCT  # 0.002

def _stats(items: list[dict]) -> dict:
    labeled = [c for c in items if c.get("net_return_5m") is not None and c.get("exit_quote_ok")]
    n = len(labeled)
    n_entry_ok = sum(1 for c in items if c.get("entry_quote_ok"))
    n_exit_ok = sum(1 for c in items if c.get("exit_quote_ok"))
    n_zero_quote = sum(1 for c in items if not c.get("entry_quote_ok"))
    fail_reasons = Counter(c.get("fail_reason", "") for c in items if not c.get("exit_quote_ok"))
    if n == 0:
        return {"n_rows": len(items), "n_labeled": 0, "quote_rate": n_entry_ok / len(items) if items else 0,
                "fail_reasons": dict(fail_reasons), "n_zero_quote": n_zero_quote}
    rets = [c["net_return_5m"] for c in labeled]
    rets_ag = [r - GAS_PROXY for r in rets]
    pos = sum(1 for r in rets_ag if r > 0)
    gains = [r for r in rets_ag if r > 0]
    losses = [-r for r in rets_ag if r < 0]
    pf = (sum(gains) / sum(losses)) if losses else (None if not gains else 999.0)
    pools = Counter(c["pool_address"] for c in labeled)
    total_p = sum(pools.values())
    return {
        "n_rows": len(items),
        "n_labeled": n,
        "n_entry_ok": n_entry_ok,
        "n_exit_ok": n_exit_ok,
        "n_quote_fail": len(items) - n_exit_ok,
        "quote_rate": round(n_exit_ok / len(items), 4) if items else 0,
        "hit_rate": round(pos / n, 4),
        "mean_ret_rpc": round(statistics.mean(rets_ag), 5),
        "median_ret_rpc": round(statistics.median(rets_ag), 5),
        "profit_factor": round(pf, 3) if pf and pf < 999 else pf,
        "loss_tail_p10": round(sorted(rets_ag)[max(0, int(n * 0.1))], 5),
        "max_loss": round(min(rets_ag), 5),
        "pool_top1": round(pools.most_common(1)[0][1] / total_p, 3) if total_p else None,
        "pool_top5": round(sum(v for _, v in pools.most_common(5)) / total_p, 3) if total_p else None,
        "sample_class": _sclass(n),
        "fail_reasons": dict(fail_reasons),
    }


def _sclass(n: int) -> str:
    if n < 300: return "smoke"
    if n < 1000: return "very_weak"
    if n < 5000: return "scoped_dryrun"
    return "formal_research"


def build_matrix(labeled: list[dict]) -> list[dict]:
    rows = []

    def _grp(items, *keys):
        g: dict = defaultdict(list)
        for item in items:
            g[tuple(item.get(k) for k in keys)].append(item)
        return g

    # fee × heat (all weeks)
    for (fee_lab, heat), items in sorted(_grp(labeled, "fee_label", "heat_bucket").items()):
        s = _stats(items)
        rows.append({"scope": "FEE_HEAT", "regime_week": "ALL",
                     "fee_label": fee_lab or "?", "heat_bucket": heat or "?", **s})

    # fee only (all weeks)
    for (fee_lab,), items in sorted(_grp(labeled, "fee_label").items()):
        s = _stats(items)
        rows.append({"scope": "FEE_ALL", "regime_week": "ALL",
                     "fee_label": fee_lab or "?", "heat_bucket": "ALL", **s})

    # fee × week
    for (fee_lab, wk), items in sorted(_grp(labeled, "fee_label", "regime_week").items()):
        s = _stats(items)
        rows.append({"scope": "FEE_WEEK", "regime_week": wk or "?",
                     "fee_label": fee_lab or "?", "heat_bucket": "ALL", **s})

    return rows


# ---------------------------------------------------------------------------
# Proxy vs real comparison
# ---------------------------------------------------------------------------

def proxy_vs_real(labeled: list[dict]) -> dict:
    both = [c for c in labeled if c.get("label_sqrt") is not None and c.get("net_return_5m") is not None]
    if not both:
        return {"n": 0}
    agree = sum(1 for c in both
                if (c["label_sqrt"] == 1) == (c["net_return_5m"] - GAS_PROXY > 0))
    proxy_pos = sum(1 for c in both if c["label_sqrt"] == 1)
    real_pos  = sum(1 for c in both if c["net_return_5m"] - GAS_PROXY > 0)
    return {
        "n_both_labeled": len(both),
        "proxy_label_pos": proxy_pos,
        "real_label_pos": real_pos,
        "agreement_rate": round(agree / len(both), 4),
        "proxy_hit_rate": round(proxy_pos / len(both), 4),
        "real_hit_rate": round(real_pos / len(both), 4),
        "proxy_overestimates_pos": proxy_pos > real_pos,
    }


# ---------------------------------------------------------------------------
# Autopsy + Sprint055 recommendation
# ---------------------------------------------------------------------------

def sprint055_recommendation(matrix: list[dict], proxy_cmp: dict) -> str:
    f100 = next((r for r in matrix if r["scope"] == "FEE_ALL" and r["fee_label"] == "fee_100bps"), None)
    f100_heat = next((r for r in matrix if r["scope"] == "FEE_HEAT" and r["fee_label"] == "fee_100bps" and "heat_20" in r.get("heat_bucket", "")), None)

    if not f100 or not f100.get("n_labeled"):
        return "BLOCKED_NO_LABELED_ROWS"

    n = f100["n_labeled"]
    hit = f100.get("hit_rate", 0)
    pf = f100.get("profit_factor") or 0
    qr = f100.get("quote_rate", 0)

    if qr < 0.5:
        return f"BLOCKED_LOW_QUOTEABILITY_{qr:.1%}"
    if n < 100:
        return f"BLOCKED_BELOW_SMOKE_{n}_rows"
    if pf and pf >= 1.5 and (hit or 0) >= 0.5:
        return "PROCEED_SPRINT055_PAPER_SHADOW"
    if pf and pf >= 1.0 and (hit or 0) >= 0.45:
        return "CONDITIONAL_SPRINT055_MORE_OOS_WEEKS_FIRST"
    return f"HOLD_SPRINT055_SIGNAL_WEAK_pf={pf}_hit={hit}"


# ---------------------------------------------------------------------------
# Markdown writers
# ---------------------------------------------------------------------------

def write_readout(matrix: list[dict], proxy_cmp: dict, total_rpc: int, s055: str) -> None:
    fee_all = [r for r in matrix if r["scope"] == "FEE_ALL"]
    fee_heat = [r for r in matrix if r["scope"] == "FEE_HEAT"]

    out = []
    out.append("# Run-054 — Real RPC Label Validation\n")
    out.append(f"**Branch:** `sprint054-real-label-validation` · **Generated:** {_now()}")
    out.append(f"\n**RPC calls (total):** {total_rpc}  ·  **Notional:** {ENTRY_WETH_RAW/1e18:.5f} WETH per candidate  ·  **Gas proxy:** {GAS_PROXY_PCT*100:.1f}%")
    out.append(f"\n**Sprint055 recommendation:** `{s055}`\n")

    out.append("## Fee bucket summary (canonical gate, all OOS weeks, after gas proxy)\n")
    out.append("| fee_label | rows | labeled | quote_rate | hit_rate | PF | median_ret | loss_tail_p10 | pool_top1 | sample_class | Sprint050 benchmark |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |")
    for r in sorted(fee_all, key=lambda x: -x["n_rows"]):
        s050 = S050_BENCH.get(r["fee_label"], {})
        bench = f"hit={s050.get('hit','—')} pf={s050.get('pf','—')}" if s050 else "—"
        flag = " ✓" if r.get("profit_factor") and r["profit_factor"] >= 1.5 and r.get("hit_rate", 0) >= 0.5 else ""
        out.append(
            f"| **{r['fee_label']}** | {r['n_rows']} | {r['n_labeled']} | "
            f"{r.get('quote_rate','—')} | {r.get('hit_rate','—')} | {r.get('profit_factor','—')}{flag} | "
            f"{r.get('median_ret_rpc','—')} | {r.get('loss_tail_p10','—')} | "
            f"{r.get('pool_top1','—')} | {r['sample_class']} | {bench} |"
        )

    out.append("\n## fee_100bps by heat bucket (canonical gate, all OOS)\n")
    out.append("| heat_bucket | rows | labeled | hit_rate | PF | median_ret | pool_top1 | sample_class |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    f100_heat = [r for r in fee_heat if r["fee_label"] == "fee_100bps"]
    for r in sorted(f100_heat, key=lambda x: x["heat_bucket"]):
        out.append(
            f"| {r['heat_bucket']} | {r['n_rows']} | {r['n_labeled']} | "
            f"{r.get('hit_rate','—')} | {r.get('profit_factor','—')} | "
            f"{r.get('median_ret_rpc','—')} | {r.get('pool_top1','—')} | {r['sample_class']} |"
        )

    out.append("\n## fee_100bps by week (canonical gate)\n")
    out.append("| week | rows | labeled | hit_rate | PF | median_ret | pool_top1 | quote_rate |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    f100_week = [r for r in matrix if r["scope"] == "FEE_WEEK" and r["fee_label"] == "fee_100bps"]
    for r in sorted(f100_week, key=lambda x: x["regime_week"]):
        out.append(
            f"| {r['regime_week']} | {r['n_rows']} | {r['n_labeled']} | "
            f"{r.get('hit_rate','—')} | {r.get('profit_factor','—')} | "
            f"{r.get('median_ret_rpc','—')} | {r.get('pool_top1','—')} | {r.get('quote_rate','—')} |"
        )

    out.append("\n## Proxy label vs real label comparison\n")
    out.append("| Metric | Value |")
    out.append("| --- | --- |")
    for k, v in proxy_cmp.items():
        out.append(f"| {k} | {v} |")

    out.append("\n## Sprint055 paper shadow plan\n")
    if "PROCEED" in s055:
        out.append("**Fee_100bps signal CONFIRMED with real labels. Recommend Sprint055.**")
        out.append("")
        out.append("Sprint055 scope:")
        out.append("- Deploy `strategy_engine` in paper mode, monitoring for fee_100bps young-pool-onset triggers")
        out.append("- Gate: Sprint037G canonical (pool_age ≤ 24h, onset ≤ 60m, heat ≥ 10), fee_bps = 100")
        out.append("- Duration: 7 days live observation")
        out.append("- Record: pool_address, wts, activity_heat, buy_pressure_ratio, entry_quote, exit_quote at +5m")
        out.append("- Budget: entry + exit QuoterV2 per signal ≈ 2 RPC calls; expected ≤ 500 calls/day")
        out.append("- Hard limit: LIVE_ALLOWED=false, execute_buy=false, signals_stream=forbidden")
    elif "CONDITIONAL" in s055:
        out.append(f"**Conditional on more OOS weeks.** {s055}")
        out.append("Expand to 3+ more OOS weeks before Sprint055.")
    elif "HOLD" in s055:
        out.append(f"**Hold.** {s055}")
        out.append("Signal does not clear Sprint055 threshold. Investigate quoteability or gate.")
    else:
        out.append(f"**Blocked: {s055}**")

    out.append("\n## Decision flags\n")
    out.append("- TRAINING_ALLOWED = `false`")
    out.append("- LIVE_ALLOWED = `false`")
    out.append("- PRIVATE_KEY_USED = `false`")
    out.append("- PRODUCTION_SIGNALS_WRITTEN = `false`")
    out.append("- NO_ALPHA_CLAIM = `true`")

    txt = "\n".join(out)
    (AUDIT_OUT / "sprint054_real_label_readout.md").write_text(txt)
    (ROOT / "mvp/runs/run-054-real-label-validation-readout.md").write_text(txt)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"[sprint054] start {_now()}", flush=True)
    assert os.environ.get("LIVE_TRADING_MODE", "").lower() not in ("1", "true", "yes", "on")

    # Load Sprint053 data
    cands_all  = json.loads(S053_CANDS.read_text())
    pool_meta  = json.loads(S053_META.read_text())
    canonical  = [c for c in cands_all if c["gate"] == "CANONICAL_037G"]
    print(f"[sprint054] canonical candidates: {len(canonical)} rows", flush=True)

    # Add fee_label and heat_bucket to candidates from pool_meta
    for c in canonical:
        meta = pool_meta.get(c["pool_address"], {})
        c["fee_bps"] = meta.get("fee_bps")
        c["fee_label"] = _fee_label(meta.get("fee_bps"))
        c["heat_bucket"] = _heat_bucket(c.get("activity_heat", 0))

    # Setup RPC
    rpc_url = _load_env_rpc()
    if not rpc_url:
        print("[sprint054] ERROR: no RPC URL", flush=True)
        return 1
    rpc = JsonRpcClient(rpc_url, delay_s=0.0)  # we manage delay manually

    print(f"[sprint054] running QuoterV2 label for {len(canonical)} candidates ...", flush=True)
    labeled = label_all_candidates(canonical, pool_meta, rpc)
    print(f"[sprint054] labeled {sum(1 for c in labeled if c.get('exit_quote_ok'))} / {len(labeled)}", flush=True)
    print(f"[sprint054] total RPC calls: {rpc.calls}", flush=True)

    # Build matrix and comparison
    matrix = build_matrix(labeled)
    proxy_cmp = proxy_vs_real(labeled)
    s055 = sprint055_recommendation(matrix, proxy_cmp)

    # Persist
    json.dump(labeled, open(DIAG_OUT / "labeled_candidates.json", "w"), indent=2, default=str)
    # CSV (drop large raw fields)
    keys = sorted({k for c in labeled for k in c if k not in ("last_sqrt",)})
    with (DIAG_OUT / "labeled_candidates.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for c in labeled:
            w.writerow({k: c.get(k) for k in keys})
    json.dump(matrix, open(DIAG_OUT / "reproduction_matrix.json", "w"), indent=2, default=str)
    keys2 = sorted({k for r in matrix for k in r})
    with (DIAG_OUT / "reproduction_matrix.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys2)
        w.writeheader()
        for r in matrix:
            w.writerow(r)
    json.dump(proxy_cmp, open(DIAG_OUT / "proxy_vs_real.json", "w"), indent=2, default=str)

    write_readout(matrix, proxy_cmp, rpc.calls, s055)

    # Print key numbers
    print(f"\n[sprint054] === RESULTS ===", flush=True)
    for r in sorted([r for r in matrix if r["scope"] == "FEE_ALL"], key=lambda x: -x["n_rows"]):
        print(f"  {r['fee_label']}: rows={r['n_rows']} labeled={r['n_labeled']} hit={r.get('hit_rate')} pf={r.get('profit_factor')} sclass={r['sample_class']}", flush=True)
    print(f"\n  proxy_vs_real agreement: {proxy_cmp.get('agreement_rate')}", flush=True)
    print(f"\n  Sprint055 recommendation: {s055}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
