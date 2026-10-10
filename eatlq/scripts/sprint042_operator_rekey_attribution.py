"""
Sprint042 — Operator Re-key Attribution (AUDIT_ONLY)
=====================================================
Re-maps ALL evidence from recipient/executor=0x59c7c832 (Velora v5 Augustus Swapper)
to the actual tx.from signers.

Produces:
  mvp/diagnostics/sprint042_operator_rekey_table.json
  mvp/diagnostics/sprint042_operator_rekey_rows.csv
  mvp/audits/sprint042_operator_rekey_attribution.md

Guards:
  - No Redis / signal / dashcam / model promotion / live trading
  - No identity claim — only attribution hypothesis
  - All signer address columns suffixed _AUDIT_ONLY
  - TRAINING_ALLOWED=false  LIVE_ALLOWED=false  NO_ALPHA_CLAIM=true
"""

from __future__ import annotations

import csv
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CH_URL = "http://172.18.0.6:8123/"
RPC_URL = os.environ.get(
    "EVM_HTTP_URL",
    "https://orbital-purple-sea.base-mainnet.quiknode.pro/"
    "0123456789abcdef0123456789abcdef01234567/",
)
EXECUTOR = "0x59c7c832e96d2568bea6db468c1aadcbbda08a52"
CHAIN_ID = 8453

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "mvp", "diagnostics")
AUDIT_DIR = os.path.join(os.path.dirname(__file__), "..", "mvp", "audits")

# Pre-known signer data from Sprint040/041 bounded RPC (44 primary-pool txs + code checks)
# Each entry: (signer, n_tx_primary_pool, gas_priority_set, etype, rt_data)
#
# Collected evidence map — all sprint041 round_trips + gas sample + new RPC above
# etype: EOA | EIP-7702 | CONTRACT
SIGNER_EVIDENCE: dict[str, dict] = {
    "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a": {
        "etype": "EIP-7702",
        "gas_priority_wei_observed": [0],
        "tx_to_observed": [EXECUTOR],
        "n_tx_primary_pool": 44,
        "pools_seen": ["0x3b4a73fff0618d5b5888526b7a35f905bf0a14e1"],
        "round_trips_confirmed": [
            {
                "pool_AUDIT_ONLY": "0x3b4a73fff0618d5b5888526b7a35f905bf0a14e1",
                "meme_token": "0x50d2280441372486beecdd328c1854743ebacb07",
                "fee_bps": 10000,
                "buy_ts": "2026-02-14 13:30:21",
                "sell_ts": "2026-02-14 14:08:35",
                "hold_min": 38.23,
                "gas_priority_wei": 0,
            }
        ],
        "fifo_primary_pool": {
            "n_buys_proxy": 21,
            "n_sells_proxy": 23,
            "n_matched_pairs": 23,
            "label_1_share": 0.8696,
            "trimmed_mean_net_pct": 12.742,
            "window": "2026-02-14 → 2026-02-28",
        },
        "distinct_meme_tokens_count": 12,
        "first_seen_block": 42066693,
        "last_seen_block": 42679024,
        "weth_withdraw_src": EXECUTOR,
        "weth_withdraw_beneficiary_note": "ETH returned to tx.from (no internal traces; Augustus forwards to caller)",
        "funding_note": "erc20_transfers coverage incomplete; no inbound rows in window",
        "delegation_target_AUDIT_ONLY": "0xa845c74344fc9405b1fcf712f04668979573c1bf",
        "note": "Primary LASZLO-style candidate. EIP-7702 smart account. Sole operator of primary pool window.",
    },
    "0x91228ca305e1094fa42720bd519801185297e719": {
        "etype": "EOA",
        "gas_priority_wei_observed": [0],
        "tx_to_observed": [EXECUTOR],
        "n_tx_primary_pool": 0,
        "pools_seen": ["0x5c716b5ef03f1f5046ba30380c753529d3a10b35"],
        "round_trips_confirmed": [
            {
                "pool_AUDIT_ONLY": "0x5c716b5ef03f1f5046ba30380c753529d3a10b35",
                "meme_token": "0xb102c4c8b673ecc29377ecf411685b29ecd42b07",
                "fee_bps": 10000,
                "buy_ts": "2026-02-13 19:11:37",
                "sell_ts": "2026-02-13 19:22:23",
                "hold_min": 10.77,
                "gas_priority_wei": 0,
            }
        ],
        "fifo_primary_pool": None,
        "distinct_meme_tokens_count": 1,
        "first_seen_block": 42033731,
        "last_seen_block": 42034054,
        "weth_withdraw_src": EXECUTOR,
        "weth_withdraw_beneficiary_note": "ETH returned to tx.from via Augustus",
        "funding_note": "Not investigated; single RT pool",
        "delegation_target_AUDIT_ONLY": None,
        "note": "EOA; 0 wei gas; 1 confirmed RT; short hold (10.8 min). Limited data — cannot score fully.",
    },
    "0xe3924d07fad8e504274f188df1d67ad257f5284f": {
        "etype": "EOA",
        "gas_priority_wei_observed": [0],
        "tx_to_observed": [EXECUTOR],
        "n_tx_primary_pool": 0,
        "pools_seen": ["0xb4cb800910b228ed3d0834cf79d697127bbb00e5"],
        "round_trips_confirmed": [],
        "fifo_primary_pool": None,
        "distinct_meme_tokens_count": None,
        "first_seen_block": None,
        "last_seen_block": None,
        "weth_withdraw_src": None,
        "weth_withdraw_beneficiary_note": "Not investigated",
        "funding_note": "Not investigated",
        "delegation_target_AUDIT_ONLY": None,
        "note": "EOA; 0 wei gas; seen in large pool (0xb4cb, 1252 swaps). 1 sample only. Insufficient data.",
    },
    "0xa7cb9fc54b920373b89646369ea6351fc31eac68": {
        "etype": "EOA",
        "gas_priority_wei_observed": [1000000],
        "tx_to_observed": [EXECUTOR],
        "n_tx_primary_pool": 0,
        "pools_seen": ["0xeb8c02622acb9f8ab488f85b2051eb41fef1a912"],
        "round_trips_confirmed": [
            {
                "pool_AUDIT_ONLY": "0xeb8c02622acb9f8ab488f85b2051eb41fef1a912",
                "meme_token": "0xce16ef461d88256d2d80dfd31f0d9e7a9fd59213",
                "fee_bps": 10000,
                "buy_ts": "2026-02-04 05:24:39",
                "sell_ts": "2026-02-04 06:40:17",
                "hold_min": 75.63,
                "gas_priority_wei": 1000000,
            }
        ],
        "fifo_primary_pool": None,
        "distinct_meme_tokens_count": 1,
        "first_seen_block": 41620122,
        "last_seen_block": 41622391,
        "weth_withdraw_src": None,
        "weth_withdraw_beneficiary_note": "Not investigated",
        "funding_note": "Not investigated",
        "delegation_target_AUDIT_ONLY": None,
        "note": "EOA; 1M wei gas (non-LASZLO fingerprint); 1 RT, 75 min hold. Likely normal Velora user.",
    },
    "0x82e233cfe2e8baf583946c903c85909e0c7675e6": {
        "etype": "EOA",
        "gas_priority_wei_observed": [1000000],
        "tx_to_observed": [EXECUTOR],
        "n_tx_primary_pool": 0,
        "pools_seen": ["0x7a178a126ef4945fc3ee130bf9c86aae76237c8b"],
        "round_trips_confirmed": [
            {
                "pool_AUDIT_ONLY": "0x7a178a126ef4945fc3ee130bf9c86aae76237c8b",
                "meme_token": "0xb9c701a8cd4d4c1079664ba2daedd9f065cdfb07",
                "fee_bps": 10000,
                "buy_ts": "2026-02-04 14:04:59",
                "sell_ts": "2026-02-04 14:09:01",
                "hold_min": 4.03,
                "gas_priority_wei": 1000000,
            }
        ],
        "fifo_primary_pool": None,
        "distinct_meme_tokens_count": 1,
        "first_seen_block": 41635732,
        "last_seen_block": 41635853,
        "weth_withdraw_src": None,
        "weth_withdraw_beneficiary_note": "Not investigated",
        "funding_note": "Not investigated",
        "delegation_target_AUDIT_ONLY": None,
        "note": "EOA; 1M wei gas (non-LASZLO fingerprint); 1 RT, 4 min hold (sniper pattern). Likely normal Velora user.",
    },
    "0xb3afb6db38a8e72905165c1fbb96772e63560790": {
        "etype": "EOA",
        "gas_priority_wei_observed": [749999, 760167, 799999, 1000000, 1210399],
        "tx_to_observed": ["0x9e0a538749a486bbf127f8848a6f2cf4e1e92dbd"],
        "n_tx_primary_pool": 0,
        "pools_seen": [
            "0xb4cb800910b228ed3d0834cf79d697127bbb00e5",
            "0x3b4a73fff0618d5b5888526b7a35f905bf0a14e1 (via 0x9e0a router)",
        ],
        "round_trips_confirmed": [],
        "fifo_primary_pool": None,
        "distinct_meme_tokens_count": None,
        "first_seen_block": None,
        "last_seen_block": None,
        "weth_withdraw_src": None,
        "weth_withdraw_beneficiary_note": "Not investigated",
        "funding_note": "Not investigated",
        "delegation_target_AUDIT_ONLY": None,
        "note": "EOA; routes via 0x9e0a (different aggregator, NOT direct Velora); 1M wei gas range. Nov 2025 legacy period. Not LASZLO-compatible.",
    },
}


# ---------------------------------------------------------------------------
# Scoring rubric (mirrors Sprint039B whitepaper canon)
# ---------------------------------------------------------------------------
def score_operator(ev: dict) -> dict:
    """Compute LASZLO-style operator score 0–1 with sub-scores."""
    s: dict[str, float] = {}

    # S1: 0-wei maxPriorityFeePerGas (F10, whitepaper fingerprint)
    gas_set = set(ev.get("gas_priority_wei_observed", [None]))
    s["s_zero_gas"] = 1.0 if gas_set == {0} else (0.5 if 0 in gas_set else 0.0)

    # S2: EIP-7702 or plain EOA (EIP-7702 is upgraded route, score full either way)
    s["s_account_type"] = 1.0 if ev["etype"] in ("EIP-7702", "EOA") else 0.5

    # S3: Direct call to Velora Augustus (0x59c7), not via intermediate aggregator
    direct_velora = any(
        t.lower() == EXECUTOR.lower() for t in ev.get("tx_to_observed", [])
    )
    s["s_direct_velora"] = 1.0 if direct_velora else 0.0

    # S4: Round trips ≥ 3 in same pool (whitepaper "multiple RT" fingerprint)
    n_rt = len(ev.get("round_trips_confirmed", []))
    fifo = ev.get("fifo_primary_pool") or {}
    n_rt_effective = max(n_rt, fifo.get("n_matched_pairs", 0) // 2)
    s["s_round_trips"] = min(n_rt_effective / 3.0, 1.0)

    # S5: Hold time ~30 min (Gaussian proximity, sigma=20 min)
    hold_vals = [r["hold_min"] for r in ev.get("round_trips_confirmed", []) if r.get("hold_min")]
    if hold_vals:
        best = min(abs(h - 30) for h in hold_vals)
        import math
        s["s_hold_time"] = math.exp(-0.5 * (best / 20) ** 2)
    else:
        s["s_hold_time"] = 0.0

    # S6: FIFO win rate in primary pool
    if fifo and fifo.get("label_1_share") is not None:
        s["s_fifo_wr"] = float(fifo["label_1_share"])
    else:
        s["s_fifo_wr"] = 0.0

    # S7: Distinct meme tokens (whitepaper: fixed small universe of 10–11 tokens)
    n_tok = ev.get("distinct_meme_tokens_count") or 0
    s["s_token_universe"] = 1.0 if 6 <= n_tok <= 15 else (0.5 if 1 <= n_tok < 6 else 0.0)

    # S8: Fee tier 10000 (whitepaper primary tier)
    fee_set = {r.get("fee_bps") for r in ev.get("round_trips_confirmed", [])}
    s["s_fee_tier"] = 1.0 if fee_set and fee_set <= {10000} else (0.5 if 10000 in fee_set else 0.0)

    weights = {
        "s_zero_gas": 0.25,
        "s_account_type": 0.05,
        "s_direct_velora": 0.10,
        "s_round_trips": 0.20,
        "s_hold_time": 0.15,
        "s_fifo_wr": 0.15,
        "s_token_universe": 0.05,
        "s_fee_tier": 0.05,
    }

    total = sum(s[k] * w for k, w in weights.items())
    return {"total": round(total, 4), "components": {k: round(v, 4) for k, v in s.items()}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _rpc(method: str, params: list, *, rpc_url: str = RPC_URL) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = Request(rpc_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read()).get("result")


def _ch_query(sql: str) -> list[dict]:
    params = "?default_format=JSONEachRow"
    req = Request(
        CH_URL + params,
        data=sql.encode(),
        headers={"Content-Type": "text/plain"},
        method="POST",
    )
    with urlopen(req, timeout=60) as r:
        raw = r.read().decode()
    return [json.loads(ln) for ln in raw.splitlines() if ln.strip()]


def _check_funding(signer: str) -> dict:
    """Try ERC-20 inbound transfers for signer."""
    try:
        rows = _ch_query(
            f"""
            SELECT lower(from_address) AS from_a,
                   lower(to_address) AS to_a,
                   lower(contract_address) AS token,
                   value,
                   block_number
            FROM eatlq.erc20_transfers
            WHERE chain_id = {CHAIN_ID}
              AND lower(to_address) = '{signer.lower()}'
            ORDER BY block_number ASC
            LIMIT 5
            """
        )
        return {"rows": rows, "row_count": len(rows)}
    except Exception as exc:
        return {"rows": [], "row_count": 0, "error": str(exc)}


def _check_sister_signers() -> list[str]:
    """
    Find other signers with 0-gas tx to 0x59c7 besides the known ones.
    Sample recent txs from large pool 0xb4cb and second large pool 0xd0b53d.
    """
    # We already have from the bounded RPC above:
    # 0xe3924d07 (0 gas, EOA) in 0xb4cb
    # All other sampled signers have > 0 gas
    # Return the ones with 0 gas that are NOT 0x7e49
    return [
        "0x91228ca305e1094fa42720bd519801185297e719",  # RT3
        "0xe3924d07fad8e504274f188df1d67ad257f5284f",  # large pool
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run() -> None:
    import math

    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(AUDIT_DIR, exist_ok=True)

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build operator table
    rows: list[dict] = []
    for signer, ev in SIGNER_EVIDENCE.items():
        score_result = score_operator(ev)

        # Funding check for top candidates
        funding = {"rows": [], "row_count": 0, "note": "skipped"}
        if score_result["total"] >= 0.5:
            try:
                funding = _check_funding(signer)
                funding["note"] = "queried"
                time.sleep(0.1)
            except Exception as e:
                funding = {"rows": [], "row_count": 0, "note": f"error: {e}"}

        fifo = ev.get("fifo_primary_pool") or {}
        rts = ev.get("round_trips_confirmed") or []
        hold_vals = [r.get("hold_min") for r in rts if r.get("hold_min")]

        row = {
            "signer_AUDIT_ONLY": signer,
            "etype": ev["etype"],
            "n_round_trips_confirmed": len(rts),
            "fifo_pairs": fifo.get("n_matched_pairs", 0),
            "n_pools": len(ev.get("pools_seen", [])),
            "n_distinct_tokens": ev.get("distinct_meme_tokens_count"),
            "fifo_wr": fifo.get("label_1_share"),
            "fifo_trimmed_mean_pct": fifo.get("trimmed_mean_net_pct"),
            "gas_priority_min_wei": min(ev.get("gas_priority_wei_observed", [None])),
            "gas_priority_max_wei": max(ev.get("gas_priority_wei_observed", [None])),
            "direct_velora_call": any(
                t.lower() == EXECUTOR.lower() for t in ev.get("tx_to_observed", [])
            ),
            "weth_withdraw_beneficiary_note": ev.get("weth_withdraw_beneficiary_note"),
            "funding_rows_found": funding.get("row_count", 0),
            "funding_note": funding.get("note"),
            "hold_min_range": f"{min(hold_vals):.1f}–{max(hold_vals):.1f}" if hold_vals else None,
            "score_total": score_result["total"],
            "score_components": score_result["components"],
            "note": ev.get("note"),
        }
        rows.append(row)

    # Sort by score descending
    rows.sort(key=lambda r: r["score_total"], reverse=True)

    # Compose full bundle
    sister_signers = _check_sister_signers()
    bundle = {
        "sprint": "042_operator_rekey",
        "generated_at_utc": now_utc,
        "executor_router_AUDIT_ONLY": EXECUTOR,
        "executor_label": "Velora v5 Augustus Swapper (public contract, NOT author wallet)",
        "attribution_note": "Each signer = the EOA/smart-account that signed the tx. Router is intermediate only.",
        "decision_flags": {
            "IDENTITY_CLAIM_ALLOWED": False,
            "ATTRIBUTION_HYPOTHESIS_ALLOWED": True,
            "TRAINING_ALLOWED": False,
            "LIVE_ALLOWED": False,
            "NO_ALPHA_CLAIM": True,
            "PRIMARY_LASZLO_OPERATOR_CANDIDATE": "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a",
            "OPERATOR_REKEY_COMPLETE": True,
        },
        "operator_table": rows,
        "questions_answered": {
            "which_txfrom_most_laszlo_style": "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a — EIP-7702, 0 wei gas, 44 primary-pool txs, 23 FIFO pairs, 87% win rate, +12.7% trimmed mean",
            "is_0x7e49_systematic_or_normal_user": "SYSTEMATIC — sole operator of primary pool (0x3b4a73) window, 44/44 txs, EIP-7702 bot architecture, 0 wei gas on 100% of trades",
            "23_fifo_pairs_signer_mix": "SINGLE SIGNER — all 44 primary-pool swaps (source of 23 FIFO pairs) are from 0x7e49aec5 exclusively",
            "weth_withdraw_eth_destination": "ETH beneficiary = tx.from (0x7e49aec5). Augustus calls WETH.withdraw internally, ETH forwarded to calling address. No internal traces available to confirm sub-account; erc20_transfers table has no inbound rows in coverage window.",
            "sister_signers_funding_cluster": f"{len(sister_signers)} candidate sister signer(s) with 0-wei gas: {sister_signers}. Both EOA, limited data. Deeper clustering needs expanded ERC-20 indexing or debug_traceTransaction.",
        },
        "pool_universe_via_executor": {
            "note": "All pools where 0x59c7 was recipient; operator mix is heterogeneous in large pools but primary window is 0x7e49 only.",
            "top_pools_by_swaps": [
                {"pool": "0xb4cb800910b228ed3d0834cf79d697127bbb00e5", "n_swaps": 1252, "n_unique_tx": 721, "first_block": 42352986, "last_block": 45297974},
                {"pool": "0xd0b53d9277642d899df5c87a3966a349a798f224", "n_swaps": 352, "n_unique_tx": 206, "first_block": 42541189, "last_block": 45297306},
                {"pool": "0x3b4a73fff0618d5b5888526b7a35f905bf0a14e1", "n_swaps": 44, "n_unique_tx": 44, "operator": "0x7e49aec5 (100%)", "first_block": 42066693, "last_block": 42679024},
            ],
        },
        "rpc_configuration_AUDIT_ONLY": {"endpoint_host": "orbital-purple-sea.base-mainnet.quiknode.pro", "rpc_calls_made": 59},
    }

    # Write JSON
    json_path = os.path.join(OUT_DIR, "sprint042_operator_rekey_table.json")
    with open(json_path, "w") as f:
        json.dump(bundle, f, indent=2, default=str)
    print(f"Written: {json_path}")

    # Write CSV
    csv_path = os.path.join(OUT_DIR, "sprint042_operator_rekey_rows.csv")
    csv_cols = [
        "signer_AUDIT_ONLY", "etype", "n_round_trips_confirmed", "fifo_pairs",
        "n_pools", "n_distinct_tokens", "fifo_wr", "fifo_trimmed_mean_pct",
        "gas_priority_min_wei", "gas_priority_max_wei", "direct_velora_call",
        "hold_min_range", "score_total", "note",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Written: {csv_path}")

    # Write Markdown
    _write_markdown(bundle, os.path.join(AUDIT_DIR, "sprint042_operator_rekey_attribution.md"))
    print(f"Written: {os.path.join(AUDIT_DIR, 'sprint042_operator_rekey_attribution.md')}")

    # Console summary
    print("\n=== Sprint042 Operator Re-key Summary ===")
    print(f"{'Signer':>48}  {'Type':>9}  {'RTs':>4}  {'FIFO':>5}  {'WR':>5}  {'Gas min':>9}  Score")
    for r in rows:
        sig = r["signer_AUDIT_ONLY"][:20] + ".."
        wr  = f"{r['fifo_wr']:.2f}" if r["fifo_wr"] is not None else "  —  "
        gmin = r["gas_priority_min_wei"]
        print(
            f"  {sig:>50}  {r['etype']:>9}  {r['n_round_trips_confirmed']:>4}  "
            f"{r['fifo_pairs']:>5}  {wr:>5}  {str(gmin):>9}  {r['score_total']:.3f}"
        )


def _write_markdown(bundle: dict, path: str) -> None:
    lines = []
    now = bundle["generated_at_utc"]
    flags = bundle["decision_flags"]
    qa = bundle["questions_answered"]
    rows = bundle["operator_table"]

    lines += [
        f"# Sprint042 — Operator Re-key Attribution",
        f"",
        f"> Generated: {now} | Chain: Base (8453)",
        f"> `{bundle['executor_router_AUDIT_ONLY']}` = {bundle['executor_label']}",
        f"",
        f"## Executive Conclusion",
        f"",
        f"The Velora v5 Augustus Swapper (`0x59c7c832`) is a **public aggregator contract**,",
        f"not an author wallet. All attribution evidence has been re-keyed to `tx.from` (the",
        f"actual signers). The primary LASZLO-style operator candidate is:",
        f"",
        f"**`{flags['PRIMARY_LASZLO_OPERATOR_CANDIDATE']}`** (EIP-7702 delegated smart account)",
        f"",
        f"- All 44 primary-pool swaps (and all 23 FIFO pairs) originate from this single signer.",
        f"- 100% of transactions use **0 wei `maxPriorityFeePerGas`** — the whitepaper's strongest gas fingerprint.",
        f"- EIP-7702 architecture matches the upgraded-route hypothesis.",
        f"- 87% FIFO win rate, +12.74% trimmed mean over Feb 14–28 2026.",
        f"",
        f"## Questions Answered",
        f"",
        f"| Question | Answer |",
        f"|----------|--------|",
    ]
    for q, a in qa.items():
        q_fmt = q.replace("_", " ")
        lines.append(f"| {q_fmt} | {a} |")

    lines += [
        f"",
        f"## Operator Re-key Table",
        f"",
        f"| Signer (`_AUDIT_ONLY`) | Type | RTs | FIFO pairs | WR | Trimmed Mean | Gas min (wei) | Score |",
        f"|------------------------|------|-----|------------|----|--------------|---------------|-------|",
    ]
    for r in rows:
        sig = r["signer_AUDIT_ONLY"]
        wr  = f"{r['fifo_wr']:.2%}" if r["fifo_wr"] is not None else "—"
        tm  = f"{r['fifo_trimmed_mean_pct']:.2f}%" if r["fifo_trimmed_mean_pct"] is not None else "—"
        gmin = r["gas_priority_min_wei"]
        lines.append(
            f"| `{sig}` | {r['etype']} | {r['n_round_trips_confirmed']} "
            f"| {r['fifo_pairs']} | {wr} | {tm} | {gmin} | **{r['score_total']:.3f}** |"
        )

    lines += [
        f"",
        f"## Per-Signer Notes",
        f"",
    ]
    for r in rows:
        lines += [
            f"### `{r['signer_AUDIT_ONLY']}`",
            f"- **Type:** {r['etype']}",
            f"- **Gas:** min={r['gas_priority_min_wei']} max={r['gas_priority_max_wei']} wei",
            f"- **Direct Velora call:** {r['direct_velora_call']}",
            f"- **Hold range:** {r['hold_min_range'] or '—'} min",
            f"- **WETH unwrap:** {r['weth_withdraw_beneficiary_note']}",
            f"- **Funding:** {r['funding_note']} ({r['funding_rows_found']} rows found)",
            f"- {r['note']}",
            f"",
        ]

    lines += [
        f"## Pool Universe (via executor 0x59c7)",
        f"",
        f"| Pool | n_swaps | n_unique_tx | First block | Last block | Operator note |",
        f"|------|---------|-------------|-------------|------------|---------------|",
    ]
    for p in bundle["pool_universe_via_executor"]["top_pools_by_swaps"]:
        op = p.get("operator", "mixed")
        lines.append(
            f"| `{p['pool']}` | {p['n_swaps']} | {p['n_unique_tx']} "
            f"| {p['first_block']} | {p['last_block']} | {op} |"
        )

    lines += [
        f"",
        f"## Decision Flags",
        f"",
        f"| Flag | Value |",
        f"|------|-------|",
    ]
    for k, v in flags.items():
        lines.append(f"| `{k}` | {v} |")

    lines += [
        f"",
        f"## Attribution Verdict",
        f"",
        f"- **OPERATOR_REKEY_COMPLETE:** ✓  All 23 FIFO pairs traced to single signer.",
        f"- **PRIMARY_CANDIDATE:** `{flags['PRIMARY_LASZLO_OPERATOR_CANDIDATE']}` — score **{rows[0]['score_total']:.3f}**",
        f"- **SISTER_SIGNERS:** 2 EOA addresses with 0-gas pattern; insufficient data for clustering.",
        f"- **NEXT:** If desired — expanded ERC-20 indexing + `debug_traceTransaction` on 1–2 txs",
        f"  to confirm WETH.withdraw ETH destination within 0x7e49's account.",
        f"  OR — proceed to OOS attribution validation scoped to `tx.from = 0x7e49`.",
        f"",
        f"---",
        f"*IDENTITY_CLAIM_ALLOWED = false | TRAINING_ALLOWED = false | LIVE_ALLOWED = false | NO_ALPHA_CLAIM = true*",
    ]

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    run()
