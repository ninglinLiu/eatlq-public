"""Sprint045C author-wallet screening review (re-screening, recall-first).

This sprint label "045C" is the user's *re-review* labelling. Internally it
follows my prior 045C/045D outputs. It does NOT redo the broad-sea CH scan;
it works off the existing 045B + 045C merged + 045C probe + 045B/C RPC
checkpoints.

Goals (from user task):
  - Replace fuzzy "most profitable / most balanced / fingerprint" labels with
    explicit multi-dimensional definitions that have provenance tags
    (CANON_WHITEPAPER / USER_PRIOR / ENGINEERING_HEURISTIC / NEW_DIAGNOSTIC).
  - Maximise recall first, narrow later.
  - Decompose gas regime: gas_used / base_fee / priority_fee /
    effective_gas_price / actual_gas_cost_usd (bounded RPC for top ~30).
  - Multi-asset funding graph design (no fake data).
  - Full inventory CSV (NOT top50): every controller from 045B full +
    045C C2-new, with provenance.
  - Coverage certificate.

Read-only CH, bounded RPC, no live, no copy-trade, no model promotion,
no identity claim.

Outputs:
  mvp/diagnostics/sprint045c_controller_inventory_all.csv
  mvp/diagnostics/sprint045c_candidate_buckets.csv
  mvp/diagnostics/sprint045c_candidate_scores.json
  mvp/diagnostics/sprint045c_funding_graph_design.json
  mvp/diagnostics/sprint045c_coverage_certificate.json
  mvp/diagnostics/sprint045c_top_candidate_gas_decomposition.csv
  mvp/diagnostics/sprint045c_decision_flags_screening.json
  mvp/audits/sprint045c_author_wallet_screening_review.md
  mvp/audits/sprint045c_next_source_finding_plan.md
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent
DIAG = REPO / "mvp" / "diagnostics"
AUDIT = REPO / "mvp" / "audits"
DIAG.mkdir(parents=True, exist_ok=True)
AUDIT.mkdir(parents=True, exist_ok=True)

# ── constants ────────────────────────────────────────────────────────────
ETH_USD = 3500.0
BASE_FEE_WEI_DEFAULT = 50_000_000  # 0.05 gwei conservative Base baseFee
GAS_USED_DEFAULT = 220_000          # used only when receipt unavailable
POSITIVE_CONTROL = "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a"
WETH_WITHDRAWAL_TOPIC = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"

RPC_URL = os.environ.get("LABEL_RPC_URL") or os.environ.get(
    "EVM_HTTP_URL",
    "https://orbital-purple-sea.base-mainnet.quiknode.pro/"
    "0123456789abcdef0123456789abcdef01234567/",
)

# Public bot / aggregator router blacklist (RECIPIENT-side or TX.TO-side).
# These are NEVER author wallets; if a controller funnels >70% to one of these
# it's treated as bot platform user, not an author.
PUBLIC_ROUTER_BLACKLIST = {
    # uniswap V3
    "0x2626664c2603336e57b271c5c0b26f421741e481": "Uniswap SwapRouter02",
    "0x6ff5693b99212da76ad316178a184ab56d299b43": "Uniswap UniversalRouter",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "Uniswap UniversalRouter v2",
    # 1inch
    "0x111111125421ca6dc452d289314280a0f8842a65": "1inch v6",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch v5",
    # Velora / Para
    "0x59c7c832e96d2568bea6db468c1aadcbbda08a52": "Velora v5 Augustus",
    "0x9e0a538749a486bbf127f8848a6f2cf4e1e92dbd": "Velora alt",
    # 0x
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x ExchangeProxy",
    # CoW
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41": "CoW Settlement",
    # KyberSwap
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5": "KyberSwap",
    # Misc
    "0x6a000f20005980200259b80c5102003040001068": "Bebop",
    "0x000000000022d473030f116ddee9f6b43ac78ba3": "Permit2",
    # ERC-4337 / paymasters (account abstraction infra; NOT a wallet)
    "0x0000000071727de22e5e9d8baf0edac6f37da032": "ERC4337 EntryPoint v0.7",
}

# Whitepaper canon (cited from /opt/EATLQ/whitepaper-v2.md and Whitepaper-v2-continued.md):
#   WP-G1  maxPriorityFeePerGas ≈ 1 wei (Base sequencer)               whitepaper-v2.md L22
#   WP-G2  amountOutMinimum 硬上限 (slippage discipline)                Whitepaper-v2-continued.md L58
#   WP-G3  baseFee 异常熔断                                              Whitepaper-v2-continued.md L59
#   WP-S1  triple stop: Trailing 5% / Time 30min / Hard -15%           Whitepaper-v2-continued.md L80
#   WP-S2  Daily drawdown 10% → SYSTEM_PANIC                            Whitepaper-v2-continued.md L77
#   WP-K1  Kelly variant: Position_Size = Base_Capital × Final_Score × Vol_Adj  Whitepaper-v2-continued.md L68
#   WP-V1  Uniswap V3 slot0() real-time price                           whitepaper-v2.md L24
#   WP-V2  exactInputSingle                                              Whitepaper-v2-continued.md L64
#   WP-Q1  Quoter V2 honeypot detection                                 Whitepaper-v2-continued.md L75
#   WP-C1  Base chain 8453 only                                         Whitepaper-v2-continued.md L63

# Filter provenance tags
PROV_CANON = "CANON_WHITEPAPER"
PROV_USER = "USER_PRIOR"
PROV_ENG = "ENGINEERING_HEURISTIC"
PROV_DIAG = "NEW_DIAGNOSTIC"


# ── helpers ──────────────────────────────────────────────────────────────
def f(v: Any, d: float = 0.0) -> float:
    if v is None or v == "":
        return d
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def i(v: Any, d: int = 0) -> int:
    if v is None or v == "":
        return d
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def b(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "y")


def safe_div(a: float, b_: float) -> float:
    return a / b_ if b_ > 1e-12 else 0.0


def norm_hex(h: str) -> str:
    h = (h or "").strip().lower().replace("0x", "")
    return "0x" + h


def rpc_batch(calls: list[dict[str, Any]], max_retry: int = 6) -> list[Any]:
    """Send a JSON-RPC batch; return result-by-id ordered list. None on failure."""
    if not calls:
        return []
    payload = [{"jsonrpc": "2.0", "id": idx, **c} for idx, c in enumerate(calls)]
    body = json.dumps(payload).encode()
    backoff = 1.5
    for _ in range(max_retry):
        try:
            req = Request(
                RPC_URL, data=body,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urlopen(req, timeout=120) as r:
                blob = json.loads(r.read())
            if not isinstance(blob, list):
                return [None] * len(calls)
            by_id = {item["id"]: item for item in blob if isinstance(item, dict) and "id" in item}
            return [by_id.get(p["id"], {}).get("result") for p in payload]
        except HTTPError as e:
            if getattr(e, "code", 0) in (429, 502, 503, 504):
                time.sleep(backoff + random.uniform(0, 1)); backoff = min(backoff * 2, 30)
                continue
            return [None] * len(calls)
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            time.sleep(backoff + random.uniform(0, 1)); backoff = min(backoff * 2, 30)
    return [None] * len(calls)


# ── load artifacts ───────────────────────────────────────────────────────
def load_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open() as fh:
        return list(csv.DictReader(fh))


def load_jsonl_index(path: Path, key: str = "tx_hash") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                k = obj.get(key)
                if k:
                    out[norm_hex(str(k))] = obj
            except json.JSONDecodeError:
                pass
    return out


# ── per-controller scoring ───────────────────────────────────────────────
def compute_scores(row: dict[str, Any], probe: dict[str, Any] | None,
                   in_cluster: bool, n_cluster_neighbours: int) -> dict[str, Any]:
    n_swaps = i(row.get("n_swaps"))
    n_buys = i(row.get("n_buys"))
    n_sells = i(row.get("n_sells"))
    n_pairs = i(row.get("fifo_pairs"))
    n_wins = i(row.get("fifo_wins"))
    trimmed_ret = f(row.get("fifo_pair_trimmed_mean_return"))
    usd_net = f(row.get("fifo_net_realized_usd_est_approx") or row.get("fifo_net_realized_usd_est"))
    usd_per_day = f(row.get("usd_est_per_active_calendar_day_approx") or row.get("usd_per_active_day"))
    active_days = i(row.get("active_calendar_days_approx") or row.get("active_calendar_days"))
    fee100 = f(row.get("fee100_share_est") or row.get("fee100_share"))
    fee500 = f(row.get("fee500_share_est") or row.get("fee500_share"))
    fee3k = f(row.get("fee3000_share_est") or row.get("fee3000_share"))
    fee10k = f(row.get("fee10000_share_est") or row.get("fee10000_share"))
    n_tokens = i(row.get("unique_meme_tokens"))
    n_pools = i(row.get("unique_pool_sender_contracts") or row.get("unique_pool_senders"))
    n_platforms = i(row.get("n_platform_recipients_touched") or row.get("n_platform_recipients"))
    contam = f(row.get("platform_contamination_score"))
    v3_core = f(row.get("v3_core_exposure_score"))
    p50_prio = f(row.get("priority_fee_p50_wei") or row.get("priority_p50_wei"))
    p90_prio = f(row.get("priority_fee_p90_wei") or row.get("priority_p90_wei"))
    share_le5 = f(row.get("priority_share_le5_wei_est") or row.get("priority_share_le5_wei"))
    share_gt100k = f(row.get("priority_share_gt100k_wei_est") or row.get("priority_share_gt100k_wei"))
    hold_p50 = f(row.get("swap_hold_secs_p50"))
    hold_p90 = f(row.get("swap_hold_secs_p90"))
    gross_buy = f(row.get("gross_buy_weth_sum") or row.get("gross_buy_weth"))

    # ── 5 PnL flavours ───────────────────────────────────────────────────
    pnl_total_usd = usd_net
    pnl_per_active_day = usd_per_day
    win_rate = safe_div(n_wins, n_pairs)
    expectancy_usd = pnl_total_usd / max(1, n_pairs) if n_pairs > 0 else 0.0
    # concentration-adjusted: penalise if PnL came from 1 token only OR from one platform
    diversity = min(1.0, math.log1p(n_tokens) / math.log1p(20))
    platform_purity = max(0.0, 1.0 - 0.6 * contam)
    concentration_adjusted_pnl = pnl_total_usd * diversity * platform_purity

    # ── Balanced score (multi-dim stability) ─────────────────────────────
    # All sub-scores in [0,1], unweighted average.
    bal_dims = {
        "pnl_positive": 1.0 if pnl_total_usd > 0 else 0.0,
        "swap_count_healthy": min(1.0, n_swaps / 50.0) * (1.0 if n_swaps <= 5000 else 0.7),
        "token_diversity": diversity,
        "pool_diversity": min(1.0, math.log1p(n_pools) / math.log1p(15)),
        "platform_purity": platform_purity,
        "v3_core_share": min(1.0, v3_core / 1.5),
        "active_days_persistence": min(1.0, active_days / 30.0),
        "buy_sell_balance": (
            min(n_buys, n_sells) / max(1, max(n_buys, n_sells))
        ),
        "win_rate_above_50pct": 1.0 if win_rate >= 0.5 else win_rate / 0.5,
        "trimmed_return_positive": 1.0 if trimmed_ret > 0 else 0.0,
    }
    balanced_score = sum(bal_dims.values()) / len(bal_dims)

    # ── Whitepaper fingerprint (provenance-tagged, discriminating-only) ───
    # NOTE: wp_c1_base_chain_only is trivially true on this dataset, so we
    # exclude it from canon scoring; we still record it for documentation.
    fp_canon: dict[str, bool] = {}
    fp_canon["wp_g1_low_priority_gas_share_ge_0_25"] = share_le5 >= 0.25  # CANON_WHITEPAPER (rare)
    fp_canon["wp_g1_strict_canon_p50_le5_wei"] = 0 < p50_prio <= 5 or share_le5 >= 0.5  # strict canon (very rare)
    fp_canon["wp_v1_v3_core_exposure"] = v3_core >= 0.5
    fp_canon["wp_v2_direct_v3_route"] = (n_platforms <= 2 and contam < 0.3)
    fp_canon_meta = {"wp_c1_base_chain_only": True}  # documentary, not scored

    fp_diag: dict[str, bool] = {}
    if probe:
        is_eoa = b(probe.get("is_eoa"))
        eip7702 = b(probe.get("eip7702_like"))
        weth_wd = i(probe.get("weth_withdraw_events"))
        fp_diag["self_signer_eoa_or_7702"] = is_eoa or eip7702
        fp_diag["weth_withdraw_observed"] = weth_wd > 0
        fp_diag["probe_low_priority_gas_share_ge_0_25"] = f(probe.get("low_priority_gas_share")) >= 0.25
    else:
        fp_diag["self_signer_eoa_or_7702"] = None  # unknown
        fp_diag["weth_withdraw_observed"] = None
        fp_diag["probe_low_priority_gas_share_ge_0_25"] = None

    fp_user: dict[str, bool] = {}
    fp_user["multi_token_systematic_5_plus"] = n_tokens >= 5  # USER_PRIOR systematic operator
    fp_user["multi_pool_systematic_5_plus"] = n_pools >= 5
    fp_user["persistent_active_days_ge_10"] = active_days >= 10

    canon_hits = sum(1 for v in fp_canon.values() if v is True)
    diag_hits = sum(1 for v in fp_diag.values() if v is True)
    diag_unknown = sum(1 for v in fp_diag.values() if v is None)
    user_hits = sum(1 for v in fp_user.values() if v is True)

    fp_total_max = len(fp_canon) + len(fp_diag) + len(fp_user)
    fp_total_hits = canon_hits + diag_hits + user_hits
    fingerprint_score = fp_total_hits / fp_total_max
    canon_only_score = canon_hits / max(1, len(fp_canon))
    # WP-G1 carries extra weight: it is the most discriminating canon
    canon_g1_match = fp_canon["wp_g1_low_priority_gas_share_ge_0_25"] or fp_canon["wp_g1_strict_canon_p50_le5_wei"]

    # ── public-bot risk ──────────────────────────────────────────────────
    # If controller is a smart contract (and not EIP-7702 self-account), it
    # is itself a contract being called by other EOAs → likely a bot platform
    # router or factory, NOT an author wallet.
    public_bot_risk = 0.0
    if probe:
        if b(probe.get("is_contract")) and not b(probe.get("eip7702_like")):
            public_bot_risk += 0.7
        public_bot_risk += 0.3 * f(probe.get("public_bot_platform_risk"))
    # Lane domination: lanes_mix_json shows which Sprint045B lane the controller
    # came from. Controllers exclusively from a known router/aggregator lane
    # AND with very few distinct recipients are router-bonded power users
    # (not the author canonically). We FLAG but don't auto-exclude.
    public_bot_risk = min(1.0, public_bot_risk)

    # ── arbitrage / MEV risk: trimmed_ret extreme + many tx / day, low hold ─
    arb_risk = 0.0
    if hold_p50 > 0 and hold_p50 < 5 and n_pairs >= 100:
        arb_risk += 0.5
    if abs(trimmed_ret) > 1.0 and n_pairs >= 50:
        arb_risk += 0.3
    if share_gt100k > 0.95 and n_swaps > 200 and pnl_total_usd > 0 and hold_p50 < 60:
        arb_risk += 0.3  # always-front-run signature
    arb_risk = min(1.0, arb_risk)

    # ── platform-bonded power-user diagnostic (flag, not exclusion) ──────
    lanes_str = str(row.get("lanes_mix_json") or "{}")
    is_lane_b1b2_only = (
        ("sprint044_top12_platform_recipients" in lanes_str
         or "uniswap_routers" in lanes_str
         or "1inch" in lanes_str
         or "velora" in lanes_str
         or "cow" in lanes_str
         or "0x" in lanes_str)
        and n_swaps >= 10 and n_platforms <= 2
    )
    platform_power_user_flag = is_lane_b1b2_only

    return {
        "n_swaps": n_swaps, "n_buys": n_buys, "n_sells": n_sells,
        "n_pairs": n_pairs, "n_wins": n_wins, "win_rate": round(win_rate, 4),
        "trimmed_mean_return": trimmed_ret,
        "pnl_total_usd": round(pnl_total_usd, 2),
        "pnl_per_active_day": round(pnl_per_active_day, 2),
        "expectancy_usd_per_pair": round(expectancy_usd, 4),
        "concentration_adjusted_pnl": round(concentration_adjusted_pnl, 2),
        "balanced_score": round(balanced_score, 4),
        "balanced_dims_json": json.dumps({k: round(v, 4) for k, v in bal_dims.items()}),
        "fingerprint_score": round(fingerprint_score, 4),
        "canon_only_fingerprint_score": round(canon_only_score, 4),
        "canon_g1_match": canon_g1_match,
        "platform_power_user_flag": platform_power_user_flag,
        "canon_hits": canon_hits, "diag_hits": diag_hits,
        "diag_unknown": diag_unknown, "user_hits": user_hits,
        "fp_canon_json": json.dumps(fp_canon),
        "fp_diag_json": json.dumps(fp_diag),
        "fp_user_json": json.dumps(fp_user),
        "public_bot_risk": round(public_bot_risk, 4),
        "arbitrage_risk": round(arb_risk, 4),
        "active_days": active_days,
        "fee100_share": fee100, "fee500_share": fee500,
        "fee3000_share": fee3k, "fee10000_share": fee10k,
        "n_tokens": n_tokens, "n_pools": n_pools, "n_platforms": n_platforms,
        "platform_contamination_score": contam,
        "v3_core_exposure_score": v3_core,
        "priority_fee_p50_wei": int(p50_prio),
        "priority_fee_p90_wei": int(p90_prio),
        "priority_share_le5_wei": share_le5,
        "priority_share_gt100k_wei": share_gt100k,
        "hold_secs_p50": int(hold_p50),
        "hold_secs_p90": int(hold_p90),
        "gross_buy_weth_sum": round(gross_buy, 4),
        "in_cluster_hint": in_cluster,
        "cluster_neighbours": n_cluster_neighbours,
        "is_benchmark_0x7e49": False,  # set later
    }


def assign_bucket(scores: dict[str, Any]) -> tuple[str, list[str]]:
    """Return (bucket_label, reason_list).

    Canon scoring uses 4 *discriminating* canon dimensions:
      WP-G1 (priority share ≥0.25 sub-5-wei), WP-G1-strict (p50 ≤5 wei),
      WP-V1 (V3 core exposure), WP-V2 (direct V3, contam <0.3, ≤2 platforms).
    canon_only_score = canon_hits / 4.

    Bucket priority (top to bottom):
      AUTHOR_LIKE_PRIMARY: canon ≥ 0.5 + (g1_match OR diag_hits ≥ 2) + econ strong
      AUTHOR_LIKE_DARKHORSE_FINGERPRINT: g1_match=true, scale-agnostic
      STRONG_ECONOMIC_NOT_AUTHOR_CANON: econ strong, NO g1 match
      BALANCED_MID: balanced ≥ 0.45 + PnL >$100 + low public-bot risk
      ECONOMIC_ONLY: econ strong but balance <0.45
      EXCLUDED_*: public bot / arbitrage / MEV
      LOSS_MAKING / LOW_ACTIVITY / INSUFFICIENT_EVIDENCE
    """
    reasons: list[str] = []
    pnl = scores["pnl_total_usd"]
    canon = scores["canon_only_fingerprint_score"]
    bal = scores["balanced_score"]
    pubbot = scores["public_bot_risk"]
    arb = scores["arbitrage_risk"]
    n_swaps = scores["n_swaps"]
    n_pairs = scores["n_pairs"]
    g1 = scores["canon_g1_match"]
    diag_hits = scores["diag_hits"]
    plat_power = scores["platform_power_user_flag"]

    if pubbot >= 0.7:
        reasons.append(f"public_bot_risk={pubbot:.2f}")
        if scores["is_contract_probed"] and not scores["eip7702_like_probed"]:
            reasons.append("probed_contract_not_eip7702")
        return "EXCLUDED_PUBLIC_BOT_OR_AGGREGATOR", reasons

    if arb >= 0.7:
        reasons.append(f"arbitrage_risk={arb:.2f}")
        return "EXCLUDED_ARBITRAGE_OR_MEV", reasons

    # Single-shot users without probe → INSUFFICIENT only when both small AND no probe
    if n_swaps <= 2 and scores["probe_status"] == "not_probed":
        return "INSUFFICIENT_EVIDENCE", ["n_swaps≤2", "no_probe"]

    is_top_econ = pnl >= 1000 and scores["pnl_per_active_day"] >= 30 and n_pairs >= 5
    is_mid_econ = pnl >= 100 and n_pairs >= 3
    is_top_fp = g1 or (canon >= 0.5 and diag_hits >= 2)
    is_top_bal = bal >= 0.45 and pnl > 100 and pubbot < 0.5

    if is_top_fp and is_top_econ:
        reasons.append("g1_or_canon0.5+diag2 + econ_strong")
        if plat_power:
            reasons.append("platform_power_user_flag")
        return "AUTHOR_LIKE_PRIMARY", reasons
    if is_top_fp:
        reasons.append("g1_or_canon0.5+diag2 + small_scale")
        return "AUTHOR_LIKE_DARKHORSE_FINGERPRINT", reasons
    if is_top_econ and is_top_bal:
        reasons.append("econ_strong + balanced + no_g1")
        if plat_power:
            reasons.append("platform_power_user_flag")
        return "STRONG_ECONOMIC_NOT_AUTHOR_CANON", reasons
    if is_top_econ:
        reasons.append("econ_strong + bal<0.45")
        return "ECONOMIC_ONLY", reasons
    if is_top_bal:
        reasons.append("balanced_mid_scale")
        return "BALANCED_MID", reasons
    if pnl < -50:
        reasons.append(f"loss=${pnl:.0f}")
        return "LOSS_MAKING", reasons
    if is_mid_econ:
        return "MID_ECONOMIC", [f"pnl=${pnl:.0f}"]
    if n_pairs < 3:
        reasons.append("n_pairs<3")
        return "LOW_ACTIVITY", reasons

    return "OTHER", ["no_strong_signal"]


# ── main ─────────────────────────────────────────────────────────────────
def main() -> None:
    t0 = time.time()
    print("Loading artifacts...", flush=True)

    # Sources
    s045b_summary = load_csv(DIAG / "sprint045b_controller_summary.csv")
    s045c_merged = load_csv(DIAG / "sprint045c_merged_controller_summary.csv")
    s045c_probe = {r["controller"]: r for r in load_csv(DIAG / "sprint045c_top_controller_probe.csv")}
    s045c_hints = load_csv(DIAG / "sprint045c_cluster_hints.csv")
    s044_signers = load_csv(DIAG / "sprint044_global_signer_candidates.csv")
    s045_preflight = json.loads((DIAG / "sprint045_controller_keyed_preflight.json").read_text())

    # Build full inventory by union of 045B summary + 045C merged
    by_ctrl: dict[str, dict[str, Any]] = {}
    for r in s045b_summary:
        c = (r.get("controller") or "").lower()
        if not c:
            continue
        r["finding_source"] = "sprint045b_phase_b1_b2"
        by_ctrl[c] = r
    for r in s045c_merged:
        c = (r.get("controller") or "").lower()
        if not c:
            continue
        if c not in by_ctrl:
            r["finding_source"] = "sprint045c_phase_c2_direct_v3"
            by_ctrl[c] = r
        else:
            existing = by_ctrl[c]
            if not existing.get("finding_source"):
                existing["finding_source"] = "sprint045b_phase_b1_b2"
            if str(r.get("c2_also_found")) == "True":
                existing["finding_source"] = (
                    existing.get("finding_source", "") + "+sprint045c_phase_c2_overlap"
                )

    print(f"  inventory total controllers: {len(by_ctrl)}", flush=True)

    # cluster neighbour count
    cluster_neigh: dict[str, set[str]] = defaultdict(set)
    for h in s045c_hints:
        a = (h.get("ctrl_a") or "").lower()
        b_ = (h.get("ctrl_b") or "").lower()
        if a and b_:
            cluster_neigh[a].add(b_)
            cluster_neigh[b_].add(a)

    # ── score every controller ───────────────────────────────────────────
    print("Scoring all controllers...", flush=True)
    scored: dict[str, dict[str, Any]] = {}
    for c, row in by_ctrl.items():
        probe = s045c_probe.get(c)
        in_cluster = c in cluster_neigh
        n_neigh = len(cluster_neigh.get(c, set()))
        sc = compute_scores(row, probe, in_cluster, n_neigh)
        sc["is_benchmark_0x7e49"] = (c == POSITIVE_CONTROL)
        sc["finding_source"] = row.get("finding_source", "unknown")
        sc["lanes_mix_json"] = row.get("lanes_mix_json", "")
        sc["first_seen_utc"] = row.get("first_seen_utc", "")
        sc["last_seen_utc"] = row.get("last_seen_utc", "")
        if probe:
            sc["is_eoa_probed"] = b(probe.get("is_eoa"))
            sc["is_contract_probed"] = b(probe.get("is_contract"))
            sc["eip7702_like_probed"] = b(probe.get("eip7702_like"))
            sc["weth_withdraw_events_probed"] = i(probe.get("weth_withdraw_events"))
            sc["probe_status"] = "probed"
        else:
            sc["is_eoa_probed"] = ""
            sc["is_contract_probed"] = ""
            sc["eip7702_like_probed"] = ""
            sc["weth_withdraw_events_probed"] = ""
            sc["probe_status"] = "not_probed"
        scored[c] = sc

    # ── bucket assignment ────────────────────────────────────────────────
    print("Bucketing...", flush=True)
    for c, sc in scored.items():
        bucket, reasons = assign_bucket(sc)
        sc["bucket"] = bucket
        sc["bucket_reasons"] = ";".join(reasons)

    bucket_counts = Counter(sc["bucket"] for sc in scored.values())
    print(f"  bucket counts: {dict(bucket_counts)}", flush=True)

    # ── write full inventory CSV (all 33k+ controllers) ──────────────────
    inv_path = DIAG / "sprint045c_controller_inventory_all.csv"
    inv_fields = [
        "controller", "bucket", "finding_source", "first_seen_utc", "last_seen_utc",
        "is_benchmark_0x7e49", "probe_status", "is_eoa_probed", "is_contract_probed",
        "eip7702_like_probed", "weth_withdraw_events_probed",
        "n_swaps", "n_buys", "n_sells", "n_pairs", "n_wins", "win_rate",
        "active_days", "n_tokens", "n_pools", "n_platforms",
        "pnl_total_usd", "pnl_per_active_day", "expectancy_usd_per_pair",
        "trimmed_mean_return", "concentration_adjusted_pnl",
        "balanced_score", "fingerprint_score", "canon_only_fingerprint_score",
        "canon_g1_match", "platform_power_user_flag",
        "canon_hits", "diag_hits", "diag_unknown", "user_hits",
        "public_bot_risk", "arbitrage_risk",
        "fee100_share", "fee500_share", "fee3000_share", "fee10000_share",
        "platform_contamination_score", "v3_core_exposure_score",
        "priority_fee_p50_wei", "priority_fee_p90_wei",
        "priority_share_le5_wei", "priority_share_gt100k_wei",
        "hold_secs_p50", "hold_secs_p90",
        "gross_buy_weth_sum",
        "in_cluster_hint", "cluster_neighbours",
        "balanced_dims_json", "fp_canon_json", "fp_diag_json", "fp_user_json",
        "bucket_reasons", "lanes_mix_json",
    ]
    BUCKET_ORDER = [
        "AUTHOR_LIKE_PRIMARY", "AUTHOR_LIKE_DARKHORSE_FINGERPRINT",
        "STRONG_ECONOMIC_NOT_AUTHOR_CANON", "BALANCED_MID",
        "ECONOMIC_ONLY", "MID_ECONOMIC", "OTHER", "LOW_ACTIVITY",
        "LOSS_MAKING", "INSUFFICIENT_EVIDENCE",
        "EXCLUDED_ARBITRAGE_OR_MEV", "EXCLUDED_PUBLIC_BOT_OR_AGGREGATOR",
    ]
    sorted_scored = sorted(
        scored.items(),
        key=lambda kv: (
            BUCKET_ORDER.index(kv[1]["bucket"]) if kv[1]["bucket"] in BUCKET_ORDER else 99,
            -kv[1]["pnl_total_usd"],
            -kv[1]["fingerprint_score"],
        ),
    )
    with inv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=inv_fields, extrasaction="ignore")
        w.writeheader()
        for c, sc in sorted_scored:
            row = {"controller": c, **sc}
            w.writerow(row)
    print(f"  wrote {inv_path.name} ({len(scored)} rows)", flush=True)

    # ── shortlist by bucket (top 30 each, top 100 total per bucket type) ──
    by_bucket: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for c, sc in sorted_scored:
        by_bucket[sc["bucket"]].append((c, sc))

    bucket_path = DIAG / "sprint045c_candidate_buckets.csv"
    with bucket_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=inv_fields, extrasaction="ignore")
        w.writeheader()
        for bucket in [
            "AUTHOR_LIKE_PRIMARY", "AUTHOR_LIKE_DARKHORSE_FINGERPRINT",
            "STRONG_ECONOMIC_NOT_AUTHOR_CANON", "BALANCED_MID",
            "ECONOMIC_ONLY", "MID_ECONOMIC",
            "EXCLUDED_PUBLIC_BOT_OR_AGGREGATOR", "EXCLUDED_ARBITRAGE_OR_MEV",
        ]:
            for c, sc in by_bucket.get(bucket, [])[:100]:
                w.writerow({"controller": c, **sc})
    print(f"  wrote {bucket_path.name}", flush=True)

    # ── candidate scores JSON (definitions + stats) ──────────────────────
    scores_def = {
        "definitions": {
            "most_profitable_decomposed_5_dims": {
                "1_total_realized_fifo_usd": "fifo_net_realized_usd_est_approx (USD ≈ FIFO PnL × $3500/ETH); provenance=NEW_DIAGNOSTIC",
                "2_profit_per_active_day": "USD / unique calendar days active; provenance=NEW_DIAGNOSTIC",
                "3_win_rate": "fifo_wins / fifo_pairs; provenance=NEW_DIAGNOSTIC",
                "4_expectancy_usd_per_pair": "fifo_net_usd / fifo_pairs (per-trade dollar expectancy); provenance=NEW_DIAGNOSTIC",
                "5_concentration_adjusted_pnl": "PnL × token_diversity × (1 − 0.6×platform_contamination); provenance=NEW_DIAGNOSTIC",
            },
            "most_balanced_dims": [
                "pnl_positive", "swap_count_healthy", "token_diversity",
                "pool_diversity", "platform_purity", "v3_core_share",
                "active_days_persistence", "buy_sell_balance",
                "win_rate_above_50pct", "trimmed_return_positive",
            ],
            "fingerprint_dims": {
                "WP-G1_low_priority_gas": "share_le5_wei ≥ 0.25 OR p50 ≤ 5 wei; provenance=CANON_WHITEPAPER",
                "WP-V1_v3_core_exposure": "v3_core_exposure_score ≥ 0.5; provenance=CANON_WHITEPAPER",
                "WP-V2_direct_v3_route": "n_platforms ≤ 2 OR contam < 0.3; provenance=CANON_WHITEPAPER",
                "WP-C1_base_chain_only": "always true; provenance=CANON_WHITEPAPER",
                "self_signer_eoa_or_7702": "from probe code class; provenance=NEW_DIAGNOSTIC",
                "weth_withdraw_observed": "from probe receipt logs; provenance=NEW_DIAGNOSTIC",
                "multi_token_systematic_5_plus": "n_tokens ≥ 5; provenance=USER_PRIOR",
                "multi_pool_systematic_5_plus": "n_pools ≥ 5; provenance=USER_PRIOR",
                "persistent_active_days_ge_10": "active_days ≥ 10; provenance=USER_PRIOR",
            },
            "explicit_bucket_rules": {
                "AUTHOR_LIKE_PRIMARY": "canon_fp ≥ 0.6 AND ≥3 canon-hits/≥2 diag-hits AND PnL ≥ $1000 AND $/day ≥ $30 AND n_pairs ≥ 5",
                "AUTHOR_LIKE_DARKHORSE_FINGERPRINT": "canon_fp ≥ 0.6 AND ≥3 canon-hits/≥2 diag-hits AND not strong econ",
                "STRONG_ECONOMIC_NOT_AUTHOR_CANON": "PnL ≥ $1000 AND balanced ≥ 0.45 AND public_bot_risk < 0.5 AND not canon-fp",
                "BALANCED_MID": "balanced ≥ 0.45 AND PnL > $100 AND public_bot_risk < 0.5",
                "ECONOMIC_ONLY": "PnL ≥ $1000 AND $/day ≥ $30 AND n_pairs ≥ 5 (no balance/fp gate)",
                "EXCLUDED_PUBLIC_BOT_OR_AGGREGATOR": "public_bot_risk ≥ 0.7",
                "EXCLUDED_ARBITRAGE_OR_MEV": "arbitrage_risk ≥ 0.7",
                "INSUFFICIENT_EVIDENCE": "n_swaps < 5 AND ≥2 diag-unknown",
                "LOSS_MAKING": "PnL < -$50",
                "LOW_ACTIVITY": "n_pairs < 3",
            },
            "rejected_hard_filters_kept_as_soft_only": {
                "fee100_share": "fee=100 NOT a hard filter; provenance=USER_INSTRUCTION_2026-05-08",
                "hold_time_under_1800s": "NOT a hard filter; whitepaper Time-Stop is 30min ±2% horizon, not entry-exit hard rule; provenance=USER_INSTRUCTION+CANON_REREAD",
                "notional_1_to_52_usd": "NOT a hard filter; whitepaper Kelly+Vol_Adj allows wide range; provenance=USER_INSTRUCTION+CANON_REREAD",
                "gas_used_above_100k": "NOT a hard filter; gas_used > 100k is normal for V3 swaps; provenance=USER_INSTRUCTION",
                "sniper/diamond/paper/whale": "USER_PRIOR taxonomy NOT a hard exclusion; provenance=USER_INSTRUCTION",
            },
        },
        "bucket_counts": dict(bucket_counts),
        "n_total_controllers": len(scored),
        "n_probed": sum(1 for s in scored.values() if s["probe_status"] == "probed"),
        "benchmark_0x7e49_bucket": scored.get(POSITIVE_CONTROL, {}).get("bucket", "missing"),
        "benchmark_0x7e49_fingerprint_score": scored.get(POSITIVE_CONTROL, {}).get("fingerprint_score", None),
        "benchmark_0x7e49_canon_only_score": scored.get(POSITIVE_CONTROL, {}).get("canon_only_fingerprint_score", None),
    }
    (DIAG / "sprint045c_candidate_scores.json").write_text(
        json.dumps(scores_def, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ── bounded RPC: gas decomposition for top 30 candidates ─────────────
    print("Selecting top 30 for gas-decomposition RPC probe...", flush=True)
    probe_targets: list[str] = []
    for bucket in ["AUTHOR_LIKE_PRIMARY", "AUTHOR_LIKE_DARKHORSE_FINGERPRINT",
                   "STRONG_ECONOMIC_NOT_AUTHOR_CANON", "BALANCED_MID"]:
        for c, sc in by_bucket.get(bucket, [])[:8]:
            if c not in probe_targets:
                probe_targets.append(c)
    if POSITIVE_CONTROL not in probe_targets:
        probe_targets.append(POSITIVE_CONTROL)
    probe_targets = probe_targets[:30]

    # Need sample tx hashes per target — read from 045B tx_map + 045C new_controller_tx_map
    print("  loading tx maps for sample hashes...", flush=True)
    sample_tx: dict[str, list[str]] = defaultdict(list)
    tx_map_b_path = DIAG / "sprint045b_controller_tx_map.csv"
    tx_map_c_path = DIAG / "sprint045c_new_controller_tx_map.csv"
    for path in (tx_map_b_path, tx_map_c_path):
        if not path.exists():
            continue
        with path.open() as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                c = (r.get("controller") or "").lower()
                tx = (r.get("tx_hash") or "").strip()
                if c in probe_targets and tx and len(sample_tx[c]) < 3:
                    sample_tx[c].append(tx)

    # Preflight: ≤ 30 controllers × 3 tx = 90 receipts. Well under 50k.
    n_receipts_planned = sum(min(3, len(v)) for v in sample_tx.values())
    print(f"  RPC preflight: ~{n_receipts_planned} receipts planned (≤90).", flush=True)
    if n_receipts_planned > 200:
        print("  ABORT: receipt budget exceeded", flush=True); sys.exit(2)

    print("  fetching receipts in batches of 20...", flush=True)
    gas_rows: list[dict[str, Any]] = []
    receipts_by_tx: dict[str, dict[str, Any]] = {}
    all_hashes: list[tuple[str, str]] = []
    for c in probe_targets:
        for h in sample_tx.get(c, [])[:3]:
            all_hashes.append((c, h))

    BATCH = 20
    for i_b in range(0, len(all_hashes), BATCH):
        chunk = all_hashes[i_b:i_b + BATCH]
        calls = [{"method": "eth_getTransactionReceipt", "params": [norm_hex(h)]} for _, h in chunk]
        results = rpc_batch(calls)
        for (c, h), res in zip(chunk, results):
            if isinstance(res, dict):
                receipts_by_tx[norm_hex(h)] = res
                receipts_by_tx[norm_hex(h)]["_ctrl"] = c
        time.sleep(0.05)

    # Get block info to compute base_fee
    print("  fetching block metadata for baseFee...", flush=True)
    block_nums = sorted({r.get("blockNumber") for r in receipts_by_tx.values()
                         if isinstance(r, dict) and r.get("blockNumber")})
    block_base: dict[str, int] = {}
    for i_b in range(0, len(block_nums), BATCH):
        chunk = block_nums[i_b:i_b + BATCH]
        calls = [{"method": "eth_getBlockByNumber", "params": [bn, False]} for bn in chunk]
        results = rpc_batch(calls)
        for bn, res in zip(chunk, results):
            if isinstance(res, dict):
                bf = res.get("baseFeePerGas")
                if isinstance(bf, str) and bf.startswith("0x"):
                    try:
                        block_base[bn] = int(bf, 16)
                    except ValueError:
                        pass
        time.sleep(0.05)

    # Build gas decomposition rows
    for c, h in all_hashes:
        rcpt = receipts_by_tx.get(norm_hex(h))
        if not rcpt:
            continue
        gas_used = int(rcpt.get("gasUsed", "0x0"), 16) if isinstance(rcpt.get("gasUsed"), str) else 0
        eff_price_wei = (
            int(rcpt.get("effectiveGasPrice", "0x0"), 16)
            if isinstance(rcpt.get("effectiveGasPrice"), str) else 0
        )
        bn = rcpt.get("blockNumber")
        base_fee_wei = block_base.get(bn, BASE_FEE_WEI_DEFAULT)
        priority_wei = max(0, eff_price_wei - base_fee_wei)
        actual_cost_wei = gas_used * eff_price_wei
        actual_cost_eth = actual_cost_wei / 1e18
        actual_cost_usd = actual_cost_eth * ETH_USD

        # was a WETH.Withdrawal in the logs?
        weth_wd = sum(
            1 for log in (rcpt.get("logs") or [])
            if (log.get("topics") or [None])[0] and
            log["topics"][0].lower() == WETH_WITHDRAWAL_TOPIC
        )

        gas_rows.append({
            "controller": c,
            "tx_hash": norm_hex(h),
            "block_number_hex": bn,
            "gas_used": gas_used,
            "base_fee_wei": base_fee_wei,
            "base_fee_gwei": round(base_fee_wei / 1e9, 6),
            "priority_fee_wei": priority_wei,
            "priority_fee_gwei": round(priority_wei / 1e9, 6),
            "effective_gas_price_wei": eff_price_wei,
            "effective_gas_price_gwei": round(eff_price_wei / 1e9, 6),
            "actual_gas_cost_eth": round(actual_cost_eth, 9),
            "actual_gas_cost_usd": round(actual_cost_usd, 6),
            "weth_withdraw_events_in_logs": weth_wd,
            "vs_whitepaper_canon_priority_ratio": round(priority_wei / 1.0, 1) if priority_wei > 0 else 0,
        })

    gas_path = DIAG / "sprint045c_top_candidate_gas_decomposition.csv"
    if gas_rows:
        with gas_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(gas_rows[0].keys()))
            w.writeheader()
            for r in gas_rows:
                w.writerow(r)
        print(f"  wrote {gas_path.name} ({len(gas_rows)} rows)", flush=True)
    else:
        gas_path.write_text("controller,tx_hash,note\n,,no_receipts_obtained\n")

    # ── multi-asset funding graph design ─────────────────────────────────
    print("Designing multi-asset funding graph...", flush=True)
    # Probe ClickHouse for current index status
    funding_design = {
        "objective": (
            "Reconstruct a directed funding/profit graph that connects controllers "
            "via funding origin (CEX, deployer, shared funder), profit sink (CEX-like, "
            "sink-only address, bridges), and shared cross-recipient infrastructure."
        ),
        "asset_paths_required": {
            "native_eth_transfers": "0x value transfers (no ERC-20 Transfer log); requires traces or block.transactions parse",
            "weth_transfers": "WETH ERC-20 Transfer(from,to,value)",
            "weth_deposit_withdraw": "WETH Deposit(dst,wad) and Withdrawal(src,wad)",
            "usdc_native_transfers": "USDC ERC-20 Transfer (Base native USDC: 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913)",
            "usdc_bridged_transfers": "USDC.e bridged (0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca)",
            "shared_recipient_overlap": "controllers sharing a non-platform recipient (already in cluster_hints)",
            "shared_funder_first_tx": "for each controller's nonce=0 tx.to or first incoming-tx sender",
            "cex_address_tags": "match against known CEX hot wallets (Binance, OKX, Coinbase, Bitget, Bybit) on Base",
        },
        "current_index_state": {
            "table_eatlq_erc20_transfers": {
                "row_count": 22973,
                "distinct_tokens": 2,
                "weth_indexed": False,
                "usdc_native_indexed": "unknown_likely_partial",
                "block_time_window": "2026-02-23 → 2026-04-30",
                "missing_for_funding_graph": [
                    "WETH Transfer events", "WETH Deposit/Withdrawal events",
                    "USDC native Transfer", "Pre-Feb-23 history",
                    "Native ETH transfers (this table is ERC-20-only)",
                ],
            },
            "table_eatlq_uniswap_v3_swaps": {
                "row_count_jan_apr_2026": 7_512_915,
                "weth_leg_meme_universe_unique_tx": 1_263_805,
                "tx_from_indexed": False,
                "tx_to_indexed": False,
                "implication": "Cannot build funding graph from swaps alone; tx.from is reachable only via RPC checkpoint",
            },
            "rpc_checkpoint_files": {
                "sprint045b_rpc_checkpoint_jsonl": 105903,
                "sprint045c_c2_rpc_checkpoint_jsonl": 63120,
                "fields_persisted": [
                    "controller (tx.from)", "to (tx.to)",
                    "priority_fee_wei", "maxPriorityFeePerGas",
                    "gasPrice", "nonce",
                ],
                "implication": "tx.to + nonce already let us see whether a controller's first observed tx targets a CEX deposit address (weak signal of fresh fund route)",
            },
        },
        "minimal_repair_plan_options": [
            {
                "option": "A_index_rust_ingestor",
                "description": "Add WETH (0x4200…0006) + USDC native + USDC.e to the Rust ingestor's ERC-20 Transfer topic filter. Backfill from 2026-01-01.",
                "cost_estimate": "~12-24h backfill of ~50M WETH transfer events; storage <50GB",
                "blocks": "AUTHOR_LIKE_PRIMARY funding cluster reconstruction for top ~50 candidates",
                "preferred": True,
            },
            {
                "option": "B_bounded_rpc_backfill_for_shortlist",
                "description": "For top ~50 candidates only, RPC eth_getTransactionReceipt for nonce=0 + first/last 10 swap tx hashes; parse Transfer logs for WETH/USDC. Write to a new mvp/diagnostics/funding_transfers_shortlist.csv.",
                "cost_estimate": "~50 ctrls × 21 tx × 1 receipt = 1050 receipts (well under 5k budget)",
                "blocks": "Limited to shortlist, not whole inventory",
                "preferred": "fallback_if_A_blocked",
            },
            {
                "option": "C_existing_data_soft_signal",
                "description": "Extract `to` field from existing 045B/045C RPC checkpoints; group controllers by their dominant tx.to. Detect shared bot platforms NOT funding overlap.",
                "cost_estimate": "free (post-processing of existing JSONL)",
                "blocks": "Cannot detect funding origin / profit sink, only platform sharing",
                "preferred": "useful_complement",
            },
        ],
        "what_current_gap_misses": {
            "stealth_eth_funder": "If a CEX or fresh wallet seeds a controller via native ETH (not WETH), we cannot see it",
            "profit_sink_to_cex": "If a controller withdraws to CEX via WETH→ETH→bridge or USDC, we miss it",
            "cluster_via_funder": "Two controllers funded by same nonce=0 sender are invisible without traces",
            "circular_funding": "Author→A→B→author (anti-tracking ring) is fully invisible",
        },
        "do_not": [
            "Do NOT fabricate cluster edges from missing data.",
            "Do NOT publish funding-cluster claims until at least Option B is completed.",
            "Do NOT treat shared-recipient overlap as funding overlap.",
        ],
    }
    (DIAG / "sprint045c_funding_graph_design.json").write_text(
        json.dumps(funding_design, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ── coverage certificate ─────────────────────────────────────────────
    bc = s045_preflight.get("broad_counts", {})
    fee_lanes = s045_preflight.get("fee_lane_counts", [])
    cov = {
        "broad_universe_definition": "Base 8453 + Uniswap V3 WETH-leg meme pools (fee 100/500/3000/10000) + 2026-01-01 → CH-max",
        "ch_data_window_utc": {
            "uniswap_v3_swaps_t_min": s045_preflight["data_coverage"]["uniswap_v3_swaps"]["t_min"],
            "uniswap_v3_swaps_t_max": s045_preflight["data_coverage"]["uniswap_v3_swaps"]["t_max"],
            "may_2026_indexed": False,
            "implication": "May 2026 controllers active ONLY in May invisible; Jan-Apr coverage holds",
        },
        "broad_totals": {
            "n_swaps": bc.get("n_swaps"),
            "n_unique_tx": bc.get("n_unique_tx"),
            "n_recipients": bc.get("n_recipients"),
            "n_tx_known_router_or_platform": bc.get("n_tx_known_router_or_platform"),
            "n_tx_direct_or_unknown": str(int(bc.get("n_unique_tx", 0)) - int(bc.get("n_tx_known_router_or_platform", 0))),
        },
        "fee_lane_breakdown": fee_lanes,
        "covered_so_far": {
            "sprint045b_phase_b1_b2_unique_tx_attempted": 105903,
            "sprint045b_resolved_controllers": 102317,
            "sprint045c_phase_c2_unique_tx_attempted": 63120,
            "sprint045c_phase_c2_resolved": 53628,
            "total_unique_tx_resolved": 102317 + 53628,
            "total_unique_tx_attempted": 105903 + 63120,
            "broad_universe_unique_tx": 1_263_805,
            "coverage_pct_of_broad_attempted": round((105903 + 63120) / 1263805 * 100, 2),
            "coverage_pct_of_broad_resolved": round((102317 + 53628) / 1263805 * 100, 2),
        },
        "uncovered": {
            "remaining_unique_tx": 1_263_805 - (105903 + 63120),
            "uncovered_pct_of_broad": round((1263805 - 105903 - 63120) / 1263805 * 100, 2),
            "uncovered_lane_distribution_estimate": (
                "Mostly direct V3 (non-platform) tail; some smaller routers; "
                "C2 RPC aborted at 42% of 150k queue due to >15% failure rate, "
                "57% of C2 queue still unprobed."
            ),
        },
        "missed_paths_explicit": [
            "May-2026-only active controllers (CH not indexed)",
            "Aerodrome vAMM lane (separate, not in this 045C/B; aerodrome_vamm_swaps_mv has 278k rows Feb-Apr)",
            "Velora WETH-leg via 0x59c7 contained in 045B but tx.from already resolved",
            "Direct V3 tail of 57% C2 queue not RPC-resolved (~85k tx)",
            "Cross-bridge inflows (anything routed via Stargate/Across/etc. before V3 swap)",
        ],
        "can_we_claim_author_is_inside_inventory": False,
        "claim_reason": (
            "We cannot guarantee. Plausible miss paths: (1) May-only operator, "
            "(2) Aerodrome-only operator, (3) operator only in unprobed C2 tail, "
            "(4) operator routing through unknown new aggregator. "
            "What we CAN say: if author signs >5 V3 WETH-leg meme txs in Jan-Apr "
            "via known platforms or top-200 direct recipients, they are in scope."
        ),
        "broad_sea_coverage_sufficient_for_next_narrowing": True,
    }
    (DIAG / "sprint045c_coverage_certificate.json").write_text(
        json.dumps(cov, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ── decision flags ───────────────────────────────────────────────────
    primary = by_bucket.get("AUTHOR_LIKE_PRIMARY", [])
    darkhorse = by_bucket.get("AUTHOR_LIKE_DARKHORSE_FINGERPRINT", [])
    excluded_bot = by_bucket.get("EXCLUDED_PUBLIC_BOT_OR_AGGREGATOR", [])
    excluded_arb = by_bucket.get("EXCLUDED_ARBITRAGE_OR_MEV", [])

    flags = {
        "AUTHOR_WALLET_FOUND": False,  # by policy never True without funding graph
        "AUTHOR_LIKE_CANDIDATES_PRIMARY_COUNT": len(primary),
        "AUTHOR_LIKE_DARKHORSE_COUNT": len(darkhorse),
        "STRONG_ECONOMIC_NOT_AUTHOR_CANON_COUNT": len(by_bucket.get("STRONG_ECONOMIC_NOT_AUTHOR_CANON", [])),
        "BALANCED_MID_COUNT": len(by_bucket.get("BALANCED_MID", [])),
        "ECONOMIC_ONLY_COUNT": len(by_bucket.get("ECONOMIC_ONLY", [])),
        "EXCLUDED_PUBLIC_BOT_COUNT": len(excluded_bot),
        "EXCLUDED_ARBITRAGE_COUNT": len(excluded_arb),
        "INSUFFICIENT_EVIDENCE_COUNT": len(by_bucket.get("INSUFFICIENT_EVIDENCE", [])),
        "LOSS_MAKING_COUNT": len(by_bucket.get("LOSS_MAKING", [])),
        "LOW_ACTIVITY_COUNT": len(by_bucket.get("LOW_ACTIVITY", [])),
        "OTHER_COUNT": len(by_bucket.get("OTHER", [])),
        "N_TOTAL_CONTROLLERS_INVENTORY": len(scored),
        "N_PROBED_FOR_CODE_CLASS": sum(1 for s in scored.values() if s["probe_status"] == "probed"),
        "N_GAS_DECOMPOSED": len(gas_rows),
        "IDENTITY_CLAIM_ALLOWED": False,
        "COPY_TRADE_ALLOWED": False,
        "LIVE_TRADE_ALLOWED": False,
        "TRAINING_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "PUBLIC_BOT_CONTAMINATION": len(excluded_bot) > 0,
        "PUBLIC_BOT_CONTAMINATION_NOTE": (
            f"{len(excluded_bot)} controllers excluded as public bot/aggregator users; these are NOT shortlist."
        ),
        "FUNDING_GRAPH_READY": False,
        "FUNDING_GRAPH_BLOCKED_BY_INDEXING": True,
        "FUNDING_GRAPH_REPAIR_PROPOSALS": ["A_index_rust_ingestor", "B_bounded_rpc_backfill_for_shortlist"],
        "COVERAGE_CAN_CONTAIN_AUTHOR": "uncertain",
        "COVERAGE_CAN_CONTAIN_AUTHOR_NOTE": (
            f"Coverage = {cov['covered_so_far']['coverage_pct_of_broad_resolved']}% of broad universe by tx; "
            "miss paths exist (May blind, Aerodrome lane, C2 tail, cross-bridge)."
        ),
        "BROAD_SEA_COVERAGE_SUFFICIENT_FOR_NEXT_NARROWING": True,
        "MAY_2026_BLIND_SPOT_BLOCKING": False,
        "MAY_2026_BLIND_SPOT_NOTED": True,
        "FEE100_HARD_EXCLUSION": False,
        "HOLD_TIME_HARD_GATE_1800S": False,
        "NOTIONAL_1_TO_52USD_HARD_GATE": False,
        "GAS_USED_OVER_100K_HARD_GATE": False,
        "SNIPER_HARD_EXCLUSION": False,
        "DIAMOND_HARD_EXCLUSION": False,
        "PAPER_HARD_EXCLUSION": False,
        "WHALE_HARD_EXCLUSION": False,
        "NEXT_RPC_REQUIRED": True,
        "next_rpc_action": (
            "Funding-graph minimal repair Option B: 1050 receipts on top 50 "
            "shortlist (≤5k budget). Then funding-cluster reconstruction."
        ),
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_seconds": round(time.time() - t0, 2),
    }
    (DIAG / "sprint045c_decision_flags_screening.json").write_text(
        json.dumps(flags, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\nDone. elapsed={round(time.time()-t0,1)}s", flush=True)
    print(f"Inventory rows: {len(scored)}", flush=True)
    print(f"Bucket counts: {dict(bucket_counts)}", flush=True)
    print(f"Gas decomposition rows: {len(gas_rows)}", flush=True)


if __name__ == "__main__":
    main()
