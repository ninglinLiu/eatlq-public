#!/usr/bin/env python3
"""B4 v2 arm A: deterministic, zero-RPC recomputation from frozen artifacts."""
from __future__ import annotations

import glob
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LEFT = ROOT / "scripts/research/base_lefttail"
OUT = ROOT / "mvp/diagnostics/base_b4_arm_a_20260714.json"

LABELS = ("instant_hp", "delayed_rug", "clean", "dead")


def wilson95(k: int, n: int) -> list[float | None]:
    if n == 0:
        return [None, None]
    z = 1.96
    p = k / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [center - half, center + half]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path):
    with path.open() as fh:
        return json.load(fh)


def load_shards() -> list[dict]:
    rows: dict[str, dict] = {}
    for name in sorted(glob.glob(str(LEFT / "_uni_shards/_uni_s*.json"))):
        for row in load_json(Path(name))["pools"]:
            rows[row["pool"].lower()] = row
    return list(rows.values())


def qv2_healthy(row: dict) -> bool:
    return row.get("qv2_why") == "ok"


def qv2_reasonable(row: dict) -> bool:
    """Frozen run_set survivability ruler, not the stricter strategy friction gate."""
    return qv2_healthy(row) and row.get("qv2_fric") is not None and row["qv2_fric"] > -0.5


def entry_ground_truth_tradeable(label: str) -> bool:
    return label in {"clean", "delayed_rug"}


def replay_table(path: Path) -> dict:
    data = load_json(path)
    rows = data["trades"]
    valid = [r for r in rows if "real_tradeable_at_exit" in r]
    q_ok = [r for r in valid if r.get("qv2_net") is not None and r["qv2_net"] > -0.5]
    missed = [r for r in q_ok if not r["real_tradeable_at_exit"]]
    return {
        "artifact": path.name,
        "n_rows": len(rows),
        "n_valid_pair": len(valid),
        "n_qv2_healthy": len(q_ok),
        "n_disagreement": len(missed),
        "disagreement_rate_all_pairs": len(missed) / len(valid) if valid else None,
        "all_pairs_wilson95": wilson95(len(missed), len(valid)),
        "disagreement_rate_among_qv2_healthy": len(missed) / len(q_ok) if q_ok else None,
        "wilson95": wilson95(len(missed), len(q_ok)),
        "missed_pools": sorted(r["pool"].lower() for r in missed),
    }


def replay_union(paths: list[Path]) -> dict:
    by_key = {}
    for path in paths:
        for row in load_json(path)["trades"]:
            by_key[(row["pool"].lower(), row.get("exb"))] = row
    rows = list(by_key.values())
    valid = [r for r in rows if "real_tradeable_at_exit" in r]
    q_ok = [r for r in valid if r.get("qv2_net") is not None and r["qv2_net"] > -0.5]
    missed = [r for r in q_ok if not r["real_tradeable_at_exit"]]
    return {"dedup_key": "pool+exb", "n_unique": len(rows), "n_valid_pair": len(valid),
            "n_qv2_healthy": len(q_ok), "n_disagreement": len(missed),
            "disagreement_rate_all_pairs": len(missed) / len(valid),
            "all_pairs_wilson95": wilson95(len(missed), len(valid)),
            "disagreement_rate_among_qv2_healthy": len(missed) / len(q_ok),
            "wilson95": wilson95(len(missed), len(q_ok)),
            "missed_pools": sorted(r["pool"].lower() for r in missed)}


def build() -> dict:
    universe_path = LEFT / "_universe_blocks.json"
    labeled_path = LEFT / "_labeled_universe.json"
    set131_path = LEFT / "_set131_forkreplay.json"
    set217_path = LEFT / "_set217_forkreplay.json"
    universe_data = load_json(universe_path)
    labeled = load_json(labeled_path)
    universe = {r["pool"].lower(): r for r in universe_data["rows"]}
    shards = load_shards()
    by_label = Counter(r["label"] for r in shards)

    entry = []
    for label in LABELS:
        rows = [r for r in shards if r["label"] == label]
        q_status = [qv2_healthy(universe[r["pool"].lower()]) for r in rows]
        gt_status = [entry_ground_truth_tradeable(r["label"]) for r in rows]
        status_mismatch = sum(q != gt for q, gt in zip(q_status, gt_status))
        prereg_false_positive = sum(
            qv2_reasonable(universe[r["pool"].lower()]) and not entry_ground_truth_tradeable(r["label"])
            for r in rows
        )
        reasonable_n = sum(qv2_reasonable(universe[r["pool"].lower()]) for r in rows)
        entry.append({
            "label": label,
            "n_labeled": len(rows),
            "n_qv2_call_ok": sum(q_status),
            "n_entry_replay_tradeable": sum(gt_status),
            "n_binary_status_mismatch": status_mismatch,
            "binary_status_mismatch_rate": status_mismatch / len(rows) if rows else None,
            "binary_status_mismatch_wilson95": wilson95(status_mismatch, len(rows)),
            "n_qv2_reasonable_by_frozen_gt_minus_50pct": reasonable_n,
            "n_preregistered_false_positive": prereg_false_positive,
            "false_positive_rate_among_qv2_reasonable": prereg_false_positive / reasonable_n if reasonable_n else None,
            "false_positive_wilson95": wilson95(prereg_false_positive, reasonable_n),
            "interpretation": "UNTESTED_NO_SUBSET" if not rows else "MEASURED",
        })

    flips = Counter()
    timing = Counter()
    flip_cross = Counter()
    priced = load_json(LEFT / "_priced_live.json")
    for r in shards:
        if r["label"] != "delayed_rug":
            continue
        flips[str(r["flip_offset"])] += 1
        exb = (priced.get(r["pool"]) or {}).get("exb")
        if not exb:
            bucket = "unknown_no_exit_block"
        elif r["flip_block"] <= exb:
            bucket = "before_or_at_exit_real_loss"
        else:
            bucket = "after_exit"
        timing[bucket] += 1
        flip_cross[(str(r["flip_offset"]), bucket)] += 1

    surv = [r for r in shards if universe[r["pool"].lower()]["in_traded_131_217"]]
    hidden = [r for r in shards if not universe[r["pool"].lower()]["in_traded_131_217"]]
    is_rug = lambda r: r["label"] in {"instant_hp", "delayed_rug"}
    enterable = lambda r: r["label"] != "dead"
    def stratum(name, rows):
        er = [r for r in rows if enterable(r)]
        rugs = [r for r in er if is_rug(r)]
        return {"stratum": name, "n_all": len(rows), "n_enterable": len(er),
                "n_rug": len(rugs), "rug_rate_all": len(rugs) / len(rows) if rows else None,
                "rug_rate_all_wilson95": wilson95(len(rugs), len(rows)),
                "rug_rate_enterable": len(rugs) / len(er) if er else None,
                "rug_rate_enterable_wilson95": wilson95(len(rugs), len(er))}

    observed_timing = timing["before_or_at_exit_real_loss"] + timing["after_exit"]
    artifacts = [universe_path, labeled_path, set131_path, set217_path,
                 LEFT / "_priced_live.json"] + [Path(p) for p in sorted(glob.glob(str(LEFT / "_uni_shards/_uni_s*.json")))]
    return {
        "schema_version": 1,
        "arm": "A_zero_new_rpc",
        "method": {
            "entry_disagreement": "qv2_why==ok, qv2_fric>-0.5 AND fork label instant_hp at entry",
            "binary_status_note": "qv2_why ok vs fork entry executable is separately reported; not a price agreement claim",
            "qv2_reasonable_note": "-0.5 is the frozen run_set survivability ruler, not the stricter strategy gate",
            "missing_policy": "n=0 is UNTESTED, never zero disagreement",
            "set_overlap_policy": "set217 primary; set131 independent replication; never concatenate",
        },
        "source_artifacts": [{"path": str(p.relative_to(ROOT)), "sha256": sha256(p)} for p in artifacts],
        "invariants": {"n_shard_rows": len(shards), "labels": dict(by_label),
                       "n_universe_rows": len(universe), "set131_set217_overlap_pools": len(
                           {r["pool"].lower() for r in load_json(set131_path)["trades"]} &
                           {r["pool"].lower() for r in load_json(set217_path)["trades"]})},
        "entry_consistency": entry,
        "exit_point_replay": {"primary_set217": replay_table(set217_path),
                              "replication_set131": replay_table(set131_path),
                              "deduplicated_union_descriptive": replay_union([set131_path, set217_path])},
        "time_sensitivity": {
            "flip_offset_blocks": dict(sorted(flips.items(), key=lambda x: int(x[0]))),
            "flip_timing_vs_our_exit": dict(timing),
            "flip_offset_x_exit_timing": [
                {"flip_offset": int(off), "timing": bucket, "n": n}
                for (off, bucket), n in sorted(flip_cross.items(), key=lambda x: (int(x[0][0]), x[0][1]))
            ],
            "n_delayed_rug": sum(flips.values()),
            "observed_exit_timing_n": observed_timing,
            "loss_share_among_known_exit_timing": timing["before_or_at_exit_real_loss"] / observed_timing,
            "loss_share_all_delayed_rug_lower_bound": timing["before_or_at_exit_real_loss"] / sum(flips.values()),
        },
        "survivorship": [stratum("traded_survivor", surv), stratum("hidden_by_quoterv2_friction", hidden),
                         stratum("full_candidate", shards)],
        "preregistered_status": {
            "H0a": "INCONCLUSIVE_INSTANT_HP_N_ZERO",
            "H0b": "SUPPORTED_DESCRIPTIVELY_BY_DELAYED_RUG_TIMING",
            "go_decision": "NOT_MADE",
            "arm_b_needed": True,
            "arm_b_reason": "p_fatal-gated universe has zero instant_hp; unbiased prevalence remains untested",
        },
    }


def main() -> None:
    result = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
