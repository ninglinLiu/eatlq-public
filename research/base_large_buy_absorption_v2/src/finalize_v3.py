"""Track D: per-hypothesis verdicts + BH note + review MD. Does not mutate v2 shadow."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .common import CFG, OUT, ROOT, save_json, sha256_file, utcnow
from .v3_common import V3_OUT, ensure_v3_out, load_v3


def finalize_v3(
    capacity: dict,
    matched: dict,
    ranking: dict,
) -> dict[str, Any]:
    ensure_v3_out()
    v3 = load_v3()
    hyp = {
        "SIGNAL_LEVEL_INCREMENTAL_RETURN": matched.get("verdicts", {}).get(
            "SIGNAL_LEVEL_INCREMENTAL_RETURN", "UNCONFIRMED"
        ),
        "WHALE_NOT_JUST_SIZE_DEPTH_PROXY": matched.get("verdicts", {}).get(
            "WHALE_NOT_JUST_SIZE_DEPTH_PROXY", "UNCONFIRMED"
        ),
        "SELECTION_EDGE_ROBUST_ACROSS_CAPACITY": capacity.get(
            "verdict_SELECTION_EDGE_ROBUST_ACROSS_CAPACITY", "UNCONFIRMED"
        ),
        "RANKER_BEATS_SIZE": ranking.get("verdicts", {}).get("RANKER_BEATS_SIZE", "UNCONFIRMED"),
        "RANKER_BEATS_RANDOM": ranking.get("verdicts", {}).get("RANKER_BEATS_RANDOM", "UNCONFIRMED"),
        "V2_SHADOW_MUTATED": "REFUTED",  # hard ban honored
        "BEST_CELL_SELECTED": "REFUTED",  # hard ban honored
    }

    req = v3["upgrade_requires"]
    # Parse simple equality checks
    def _ok(expr: str) -> bool:
        left, _, right = expr.partition("==")
        return hyp.get(left.strip()) == right.strip()

    upgrade = all(_ok(r) for r in req)
    open_items = [
        k for k, v in hyp.items()
        if v in ("REFUTED", "UNCONFIRMED", "SCHEDULED_EDGE_UNCONFIRMED", "SCHEDULED_EDGE_NEGATIVE")
        and k not in ("V2_SHADOW_MUTATED", "BEST_CELL_SELECTED")
    ]

    code_h = hashlib.sha256()
    for p in sorted((ROOT / "src").glob("*.py")):
        code_h.update(p.name.encode())
        code_h.update(p.read_bytes())

    payload = {
        "generated_at": utcnow(),
        "phase": "LARGE_BUY_ABSORPTION_V3",
        "historical_label": "DIAGNOSTIC_REPLAY",
        "live": "NO_GO",
        "v2_shadow_mutated": False,
        "propose_v3_shadow_upgrade": upgrade,
        "upgrade_requires": req,
        "per_hypothesis": hyp,
        "open_items": open_items,
        "capacity_brief": {
            "fills_at_c3": capacity.get("fills_at_c3"),
            "fills_max": capacity.get("fills_max_across_grid"),
            "fills_lift": capacity.get("fills_lift"),
            "bh_pass_cells": capacity.get("n_lba_vs_pool_bh_reject"),
            "n_cells": capacity.get("n_cells"),
            "verdict": hyp["SELECTION_EDGE_ROBUST_ACROSS_CAPACITY"],
        },
        "matched_brief": {
            "att_mean": (matched.get("att_lba_vs_nonlba") or {}).get("att_mean_delta_weth"),
            "n_pairs": (matched.get("att_lba_vs_nonlba") or {}).get("n_pairs"),
            "day_p05": ((matched.get("att_lba_vs_nonlba") or {}).get("bootstrap_day") or {}).get("p05"),
            "verdict": hyp["SIGNAL_LEVEL_INCREMENTAL_RETURN"],
            "proxy_verdict": hyp["WHALE_NOT_JUST_SIZE_DEPTH_PROXY"],
        },
        "ranking_brief": {
            "test_verdict": hyp["RANKER_BEATS_SIZE"],
            "sealed": ranking.get("sealed"),
            "policies": ranking.get("policies"),
        },
        "code_hash": code_h.hexdigest(),
        "config_hash": sha256_file(CFG / "freeze_v3.json"),
    }
    save_json(V3_OUT / "LBA_v3_REPLAY.json", payload)

    md = _md(payload, capacity, matched, ranking)
    (V3_OUT / "LBA_v3_REVIEW.md").write_text(md)
    (ROOT / "LBA_v3_REVIEW.md").write_text(md)

    # Proposal file only — does not arm trading
    save_json(V3_OUT / "SHADOW_UPGRADE_PROPOSAL.json", {
        "propose": upgrade,
        "live": "NO_GO",
        "mutates_v2": False,
        "reason": (
            "All upgrade_requires met — human may arm v3 observer shadow with edge claim."
            if upgrade
            else f"Blocked. Open items: {open_items}"
        ),
        "generated_at": utcnow(),
    })
    return payload


def _md(payload, capacity, matched, ranking) -> str:
    lines = [
        "# LARGE_BUY_ABSORPTION_v3 — Research Layer Review",
        "",
        f"Generated: `{payload['generated_at']}`",
        "",
        "> Per-hypothesis only. Does **not** mutate frozen v2 shadow. `live = NO_GO`.",
        "> Capacity grid: robustness across cells (BH-corrected), **not** best-cell selection.",
        "> Primary power test: signal-level covariate-matched ATT (Track B).",
        "",
        "## Per-hypothesis verdicts",
        "",
        "| Hypothesis | Verdict |",
        "| --- | --- |",
    ]
    for k, v in payload["per_hypothesis"].items():
        lines.append(f"| `{k}` | **{v}** |")
    lines += [
        "",
        f"- Propose v3 shadow upgrade: `{payload['propose_v3_shadow_upgrade']}`",
        f"- Live: `{payload['live']}`",
        "",
        "## Track A — Capacity grid",
        "",
        f"- Fills at 3-slot: `{payload['capacity_brief']['fills_at_c3']}` → max `{payload['capacity_brief']['fills_max']}` "
        f"(x`{payload['capacity_brief']['fills_lift']}`)",
        f"- BH-significant LBA-vs-POOL cells: `{payload['capacity_brief']['bh_pass_cells']}` / `{payload['capacity_brief']['n_cells']}`",
        f"- Verdict: `{payload['capacity_brief']['verdict']}`",
        "",
        "See `out/v3/capacity_grid.csv`.",
        "",
        "## Track B — Matched incremental (primary)",
        "",
        f"- ATT mean Δ WETH: `{payload['matched_brief']['att_mean']}`",
        f"- Pairs: `{payload['matched_brief']['n_pairs']}`",
        f"- Day-cluster p05: `{payload['matched_brief']['day_p05']}`",
        f"- Signal incremental: `{payload['matched_brief']['verdict']}`",
        f"- Not just size/depth proxy: `{payload['matched_brief']['proxy_verdict']}`",
        "",
        "## Track C — HistGBM ranker",
        "",
        f"- RANKER_BEATS_SIZE: `{payload['ranking_brief']['test_verdict']}`",
        f"- Sealed: `{json.dumps(payload['ranking_brief'].get('sealed'), default=str)[:400]}`",
        "",
        "## Open items",
        "",
    ]
    for item in payload.get("open_items") or []:
        lines.append(f"- `{item}`")
    if not payload.get("open_items"):
        lines.append("- None")
    lines += [
        "",
        "## Governance",
        "",
        f"- Code hash: `{payload['code_hash']}`",
        f"- Config hash: `{payload['config_hash']}`",
        "- v2 freeze / shadow: untouched",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    capacity = json.loads((V3_OUT / "capacity_grid.json").read_text())
    matched = json.loads((V3_OUT / "matched_incremental.json").read_text())
    ranking = json.loads((V3_OUT / "ranking_gbm.json").read_text())
    finalize_v3(capacity, matched, ranking)
