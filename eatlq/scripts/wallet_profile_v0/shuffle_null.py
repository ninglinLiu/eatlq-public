#!/usr/bin/env python3
"""Null test: replace smart_money set with random non-labeled senders (H3 v2)."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

import sprint091_sender_gated_paper as s91  # noqa: E402
from ch_helper import CHAIN_ID, ch_query  # noqa: E402

log = logging.getLogger("shuffle_null")

LABELS_V1 = REPO / "mvp/audits/wallet_profile_v0/labels/all_labels.csv"
LABELS_V2 = REPO / "mvp/audits/wallet_profile_v0/labels/smart_money_strict.csv"
PIT_DIR = REPO / "mvp/audits/wallet_profile_v0/labels/pit"
OUT_DIR = REPO / "mvp/audits/wallet_profile_v0/sender_gated"


def labels_path(version: str) -> Path:
    return LABELS_V2 if version == "v2" else LABELS_V1


def load_all_labeled(version: str) -> set[str]:
    path = labels_path(version)
    if not path.exists():
        path = LABELS_V1
    return {
        r["sender_address"].lower()
        for r in csv.DictReader(path.open(newline="", encoding="utf-8"))
        if r.get("sender_address")
    }


def load_smart_money(version: str) -> set[str]:
    if version == "v2":
        for path in (LABELS_V2, REPO / "mvp/audits/wallet_profile_v0/labels/all_labels_strict.csv"):
            if path.exists():
                rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
                if rows and "strict_tokens" in (rows[0].keys() if rows else {}):
                    return {r["sender_address"].lower() for r in rows if r.get("sender_address")}
                return {
                    r["sender_address"].lower()
                    for r in rows
                    if str(r.get("is_smart_money", "")).lower() in ("true", "1")
                }
    s91._LABELS_PATH = labels_path(version)
    return s91.load_smart_money()


def load_sender_pool(days: int, limit: int = 800, fallback: list[str] | None = None) -> list[str]:
    sql = f"""
    SELECT lower(sender) AS s
    FROM eatlq.uniswap_v3_swaps_decoded
    WHERE chain_id={CHAIN_ID}
      AND block_time >= now() - INTERVAL {days + 1} DAY
      AND amount0 < 0
    GROUP BY s
    HAVING count() >= 3
    LIMIT {limit}
    FORMAT JSONEachRow
    """
    try:
        return [r["s"] for r in ch_query(sql, timeout=120)]
    except Exception as e:
        log.warning("sender pool CH query failed: %s", str(e)[:120])
        return fallback or []


def random_pool_senders(n: int, pool: list[str], exclude: set[str]) -> set[str]:
    eligible = [s for s in pool if s not in exclude]
    random.shuffle(eligible)
    return set(eligible[:n])


def collect_candidates(
    days: int,
    horizon_sec: int = 1800,
    *,
    single_tick: bool = False,
    pit_mode: bool = False,
    labels_version: str = "v1",
    pit_dir: Path | None = None,
) -> list[dict[str, Any]]:
    prev_tick = s91._USE_SINGLE_TICK
    prev_pit = s91._USE_PIT_LABELS
    prev_pit_dir = s91._PIT_LABELS_DIR
    prev_labels = s91._LABELS_PATH

    s91._USE_SINGLE_TICK = single_tick
    if pit_mode:
        s91._USE_PIT_LABELS = True
        s91._PIT_LABELS_DIR = pit_dir or PIT_DIR
    if labels_version == "v2":
        s91._LABELS_PATH = LABELS_V2 if LABELS_V2.exists() else LABELS_V1

    onsets = s91.discover_historical_onsets(days)
    candidates: list[dict[str, Any]] = []
    for o in onsets:
        wts = int(o["onset_wts"])
        heat = int(o.get("onset_heat") or 0)
        if heat < s91.ONSET_THRESH:
            continue
        if int(o.get("onset_lag", 999999)) > s91.ONSET_MAX:
            continue
        senders = s91.onset_senders(o["pool"], wts)
        ret = s91.forward_tick_return(o["pool"], wts, horizon_sec=horizon_sec)
        if ret is None or not senders:
            continue
        gate = s91.load_smart_money_at(wts) if pit_mode else load_smart_money(labels_version)
        sm_hits = [s for s in senders if s in gate]
        if len(set(sm_hits)) < s91._MIN_DISTINCT_SENDERS:
            continue
        if not sm_hits:
            continue
        candidates.append({
            "pool": o["pool"],
            "wts": wts,
            "heat": heat,
            "senders": senders,
            "ret": float(ret),
            "gated": True,
        })

    s91._USE_SINGLE_TICK = prev_tick
    s91._USE_PIT_LABELS = prev_pit
    s91._PIT_LABELS_DIR = prev_pit_dir
    s91._LABELS_PATH = prev_labels
    log.info(
        "cached gated candidates: %d (from %d onsets) single_tick=%s pit=%s v=%s",
        len(candidates), len(onsets), single_tick, pit_mode, labels_version,
    )
    return candidates


def pf_from_candidates(candidates: list[dict]) -> tuple[Optional[float], int]:
    rets = [c["ret"] for c in candidates if c.get("gated")]
    if not rets:
        return None, 0
    return s91._pf(rets), len(rets)


def pf_from_gate(candidates: list[dict], gate: set[str]) -> tuple[Optional[float], int]:
    rets: list[float] = []
    for c in candidates:
        hits = [s for s in c["senders"] if s in gate]
        if len(set(hits)) >= s91._MIN_DISTINCT_SENDERS:
            rets.append(c["ret"])
    if not rets:
        return None, 0
    return s91._pf(rets), len(rets)


def run_null_test(
    *,
    trials: int,
    backtest_days: int,
    single_tick: bool,
    labels_version: str,
    pit_mode: bool,
    pit_dir: Path | None,
) -> dict[str, Any]:
    labeled = load_all_labeled(labels_version)
    real_sm = load_smart_money(labels_version)

    base_candidates = collect_candidates(
        backtest_days,
        single_tick=single_tick,
        pit_mode=False,
        labels_version=labels_version,
    )
    if pit_mode:
        candidates = collect_candidates(
            backtest_days,
            single_tick=single_tick,
            pit_mode=True,
            labels_version=labels_version,
            pit_dir=pit_dir,
        )
    else:
        candidates = base_candidates

    real_pf, real_n = pf_from_gate(candidates if not pit_mode else candidates, real_sm)
    if pit_mode:
        real_pf, real_n = pf_from_candidates(candidates)

    sender_pool = load_sender_pool(
        backtest_days,
        fallback=sorted({s for c in base_candidates for s in c["senders"]}),
    )
    if not sender_pool:
        sender_pool = sorted({s for c in base_candidates for s in c["senders"]})

    results: list[dict] = []
    for trial in range(trials):
        fake = random_pool_senders(max(len(real_sm), 10), sender_pool, labeled)
        pf, n = pf_from_gate(base_candidates, fake)
        results.append({"trial": trial, "pf": pf, "n": n})

    pfs = sorted(r["pf"] for r in results if r["pf"] is not None)
    finite_pfs = [p for p in pfs if p != float("inf")]
    p95_idx = min(len(finite_pfs) - 1, int(len(finite_pfs) * 0.95)) if finite_pfs else 0
    real_finite = real_pf if real_pf not in (float("inf"), None) else None

    dual_pass = (
        real_finite is not None
        and real_finite > 1.5
        and real_n >= 15
        and finite_pfs
        and real_finite > finite_pfs[p95_idx]
    )

    return {
        "metric": "single_tick" if single_tick else "robust_pm60s",
        "labels_version": labels_version,
        "pit_mode": pit_mode,
        "trials_requested": trials,
        "trials_completed": len(pfs),
        "backtest_days": backtest_days,
        "real_smart_money_size": len(real_sm),
        "real_pf_computed": real_pf,
        "real_n_computed": real_n,
        "null_p95": finite_pfs[p95_idx] if finite_pfs else None,
        "verdict_real_above_p95": (
            real_finite is not None and finite_pfs and real_finite > finite_pfs[p95_idx]
        ),
        "dual_metric_pass": dual_pass,
        "verdict": (
            "gate_adds_information" if dual_pass else "gate_likely_spurious" if finite_pfs else "inconclusive"
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "RESEARCH_ONLY": True,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Shuffle null test for sender-gated gate")
    p.add_argument("--trials", type=int, default=50)
    p.add_argument("--backtest-days", type=int, default=14)
    p.add_argument("--single-tick", action="store_true")
    p.add_argument("--labels-version", choices=("v1", "v2"), default="v1")
    p.add_argument("--pit-mode", action="store_true")
    p.add_argument("--pit-labels-dir", type=str, default=str(PIT_DIR))
    p.add_argument("--run-all-configs", action="store_true", help="Run 4 H3 v2 configs")
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pit_dir = Path(args.pit_labels_dir)

    if args.run_all_configs:
        configs = [
            ("v1", False, True, "null_test_v2_v1_single_tick.json"),
            ("v1", False, False, "null_test_v2_v1_robust.json"),
            ("v2", True, True, "null_test_v2_strict_single_tick_pit.json"),
            ("v2", True, False, "null_test_v2_strict_robust_pit.json"),
        ]
        all_reports = {}
        for version, pit, single, fname in configs:
            s91._MIN_DISTINCT_SENDERS = 2 if version == "v2" else 1
            report = run_null_test(
                trials=args.trials,
                backtest_days=args.backtest_days,
                single_tick=single,
                labels_version=version,
                pit_mode=pit,
                pit_dir=pit_dir,
            )
            (OUT_DIR / fname).write_text(json.dumps(report, indent=2))
            all_reports[fname] = report
            log.info("%s verdict=%s pf=%s n=%s", fname, report["verdict"], report["real_pf_computed"], report["real_n_computed"])
        print(json.dumps(all_reports, indent=2))
        return

    report = run_null_test(
        trials=args.trials,
        backtest_days=args.backtest_days,
        single_tick=args.single_tick,
        labels_version=args.labels_version,
        pit_mode=args.pit_mode,
        pit_dir=pit_dir,
    )
    suffix = f"_{args.labels_version}"
    if args.pit_mode:
        suffix += "_pit"
    if args.single_tick:
        suffix += "_single_tick"
    out_path = OUT_DIR / f"null_test{suffix}.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
