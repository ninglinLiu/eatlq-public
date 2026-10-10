#!/usr/bin/env python3
"""Build an incremental current-state external risk cache for Keystone tokens.

Important methodology note: GoPlus/Honeypot.is return current scanner state, not
historical block-state. This cache is therefore a *current overlay* for live-risk
policy and conservative rebacktests, not a perfect causal historical replay.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/EATLQ/services/executor/src")
sys.path.insert(0, HERE)

from external_risk import (  # noqa: E402
    ExternalRiskPolicy,
    _assess_goplus,
    _assess_honeypot,
    _get_goplus,
    _get_honeypot,
)
from keystone_live import COLS, POOLS  # noqa: E402

CACHE = os.path.join(HERE, "_external_risk_current.json")


def _dedupe(xs):
    return list(dict.fromkeys(xs))


def load_tokens(scope: str) -> list[str]:
    df = pd.read_csv(POOLS, sep="\t", names=COLS)
    df["t0"] = pd.to_datetime(df["t0"])
    if scope == "holdout":
        cut = df.t0.quantile(0.6)
        df = df[df.t0 > cut]
    elif scope == "june":
        df = df[(df.t0 >= "2026-06-01") & (df.t0 < "2026-07-01")]
    elif scope != "all":
        raise ValueError(f"unknown scope: {scope}")
    return sorted(set(str(x).lower() for x in df.alt.dropna()))


def assess_raw(token: str, raw: Dict[str, Any], strict: bool) -> Dict[str, Any]:
    policy = ExternalRiskPolicy(
        enabled=True,
        fail_closed=True,
        providers=("goplus", "honeypot"),
        block_proxy=True,
        block_unverified=strict,
        block_mintable=strict,
        block_blacklist=True,
        block_anti_whale=strict,
    )
    critical: list[str] = []
    warnings: list[str] = []
    honeypot_cleared = False
    if raw.get("goplus") is not None:
        c, w, _m = _assess_goplus(raw["goplus"], token, policy)
        critical.extend(c)
        warnings.extend(w)
    else:
        warnings.append("goplus_missing")
    if raw.get("honeypot") is not None:
        c, w, _m, honeypot_cleared = _assess_honeypot(raw["honeypot"], policy)
        critical.extend(c)
        warnings.extend(w)
    else:
        critical.append("honeypot_missing")
    critical = _dedupe(critical)
    warnings = _dedupe(warnings)
    return {
        "ok": len(critical) == 0,
        "critical": critical,
        "warnings": warnings,
        "reason": ",".join(critical[:4]) if critical else "external_risk_ok",
    }


def fetch_token(token: str, timeout: float) -> Dict[str, Any]:
    raw: Dict[str, Any] = {}
    errors: list[str] = []
    try:
        raw["goplus"] = _get_goplus(token, 8453, timeout)
    except Exception as e:
        raw["goplus"] = None
        errors.append(f"goplus:{type(e).__name__}:{str(e)[:120]}")
    try:
        raw["honeypot"] = _get_honeypot(token, 8453, timeout)
    except Exception as e:
        raw["honeypot"] = None
        errors.append(f"honeypot:{type(e).__name__}:{str(e)[:120]}")
    return {
        "token": token,
        "checked_at": int(time.time()),
        "errors": errors,
        "raw": raw,
        "strict": assess_raw(token, raw, strict=True),
        "scanner_only": assess_raw(token, raw, strict=False),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["holdout", "june", "all"], default="holdout")
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument("--timeout", type=float, default=4.0)
    ap.add_argument("--sleep", type=float, default=0.15)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    tokens = load_tokens(args.scope)
    if args.limit:
        tokens = tokens[: args.limit]
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    todo = [t for t in tokens if args.force or t not in cache]
    print(f"external risk cache scope={args.scope} tokens={len(tokens)} cached={len(cache)} todo={len(todo)}")
    for i, token in enumerate(todo, 1):
        cache[token] = fetch_token(token, args.timeout)
        if i % 10 == 0 or i == len(todo):
            json.dump(cache, open(CACHE, "w"), indent=2, sort_keys=True)
            print(f"  {i}/{len(todo)} saved")
        time.sleep(args.sleep)
    json.dump(cache, open(CACHE, "w"), indent=2, sort_keys=True)
    strict_block = sum(1 for t in tokens if not cache.get(t, {}).get("strict", {}).get("ok", False))
    scan_block = sum(1 for t in tokens if not cache.get(t, {}).get("scanner_only", {}).get("ok", False))
    print(f"strict_block={strict_block}/{len(tokens)} scanner_only_block={scan_block}/{len(tokens)} cache={CACHE}")


if __name__ == "__main__":
    main()
