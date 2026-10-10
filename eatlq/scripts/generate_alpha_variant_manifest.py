#!/usr/bin/env python3
"""Generate Sprint087 alpha variant manifest for comparative dry-run."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "mvp/audits/sprint087_alpha_variants/alpha_variant_manifest.json"
TOXIC_AUDIT = REPO / "mvp/audits/sprint083_fee500_paper_shadow/sop_reality_audit.json"

EXIT_HORIZONS = ("30m", "1h", "2h", "4h", "6h", "12h", "24h")
FILTER_GRID = {
    "baseline": {},
    "anti_toxic": {"anti_toxic": True},
    "conc_cap1": {"max_trades_per_pool": 1},
    "conc_cap3": {"max_trades_per_pool": 3},
    "anti_toxic_conc1": {"anti_toxic": True, "max_trades_per_pool": 1},
    "momentum": {"momentum_gate": True},
    "pv_pos": {"pv_min": 0.0},
}


def load_toxic_pools() -> list[str]:
    if not TOXIC_AUDIT.exists():
        return []
    data = json.loads(TOXIC_AUDIT.read_text())
    out = []
    for row in (data.get("rolling_toxic_7d") or {}).get("top_toxic_pools") or []:
        if float(row.get("toxic_rate") or 0) >= 0.15:
            out.append((row.get("pool") or "").lower())
    return [p for p in out if p]


def build_specs() -> list[dict]:
    toxic = load_toxic_pools()
    specs: list[dict] = []
    seen: set[str] = set()

    def add(spec_id: str, family: str, **kwargs) -> None:
        if spec_id in seen:
            return
        seen.add(spec_id)
        spec = {
            "strategy_id": spec_id,
            "family": family,
            "promotion_band": "DRYRUN_COMPARE_ONLY",
            "sop_promotion_eligible": False,
            "fee_bps_in": [100],
            "require_sender_gated": True,
            "signal_csv": "logs/sprint055_paper_shadow.csv",
            "age_min_sec": kwargs.pop("age_min_sec", 1800),
            "age_max_sec": kwargs.pop("age_max_sec", 86400),
            **kwargs,
        }
        if spec.get("anti_toxic"):
            spec["exclude_toxic_pools"] = toxic
            spec.pop("anti_toxic", None)
        specs.append(spec)

    # Core: exit × filter full cross (fee100 sender-gated)
    for exit_h, (filt_name, filt_kw) in product(EXIT_HORIZONS, FILTER_GRID.items()):
        sid = f"AV_X_{exit_h.upper()}_{filt_name.upper()}"
        add(
            sid,
            "exit_x_filter",
            exit_horizon=exit_h,
            hard_horizon_exit=exit_h in ("4h", "6h", "12h", "24h"),
            **filt_kw,
        )

    # Fee tier variants (2h baseline)
    for fee_name, fee_kw in (
        ("fee500", {"fee_bps_in": [500], "require_sender_gated": False}),
        ("fee3000", {"fee_bps_in": [3000], "require_sender_gated": False}),
        ("fee_any", {"fee_max_bps": 9999, "require_sender_gated": True}),
    ):
        add(f"AV_FEE_{fee_name.upper()}", "fee_tier", exit_horizon="2h", **fee_kw)

    # Sender / wallet
    for name, kw in (
        ("sender_open", {"require_sender_gated": False}),
        ("composite", {"require_sender_gated": True, "require_composite": True}),
    ):
        add(f"AV_SEND_{name.upper()}", "sender_wallet", exit_horizon="2h", **kw)

    # Old-pool engineering
    for fee_name, fee_kw in (("fee500", {"fee_bps_in": [500]}), ("fee_any", {"fee_max_bps": 9999})):
        add(
            f"AV_OLD_{fee_name.upper()}",
            "old_pool_engineering",
            exit_horizon="2h",
            age_min_sec=86400,
            age_max_sec=604800,
            require_sender_gated=False,
            **fee_kw,
        )

    # Fee500 exit mini-grid
    for h in ("30m", "2h", "4h", "6h"):
        add(
            f"AV_FEE500_EXIT_{h.upper()}",
            "fee500_exit",
            exit_horizon=h,
            hard_horizon_exit=h in ("4h", "6h"),
            fee_bps_in=[500],
            require_sender_gated=False,
        )

    return specs


def main() -> None:
    specs = build_specs()
    doc = {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "sprint": "087",
        "policy": (
            "Comparative dry-run only. exit×filter cross-product + fee/sender/old-pool engineering. "
            "No live trading, no model promotion."
        ),
        "variant_count": len(specs),
        "families": sorted({s["family"] for s in specs}),
        "strategies": specs,
        "sop_gates": {"live_allowed": False, "training_promotion_allowed": False},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=2))
    print(json.dumps({"ok": True, "path": str(OUT), "variants": len(specs)}, indent=2))


if __name__ == "__main__":
    main()
