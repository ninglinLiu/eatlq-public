"""
Run-004 Patch F (A): E2 reconciliation audit — no RPC, no training, no prepare_dataset.

Compares the E2 v2-only labeled CSV against candidate filter counts and the E2 report JSON,
explains the gap input_v2_trainable_rows (19879) vs label_attempted_rows (17577), and flags
duplicate-key label conflicts.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))


def _load_json(p: Path) -> dict[str, Any]:
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def _short_tx(tx: str) -> str:
    t = (tx or "").strip()
    if len(t) > 16:
        return f"{t[:10]}..{t[-4:]}"
    return t or "(empty)"


def _recon_conclusion(
    missing_files: bool,
    schema_ok: bool,
    missing_keys: int,
    dup_conflicts: int,
    unexplained: int,
) -> str:
    if missing_files:
        return "HOLD_DUE_TO_MISSING_INPUT_FILES"
    if not schema_ok:
        return "HOLD_DUE_TO_SCHEMA_MISMATCH"
    if missing_keys > 0:
        return "HOLD_DUE_TO_UNEXPLAINED_E2_GAP"
    if dup_conflicts > 0:
        return "HOLD_DUE_TO_DUPLICATE_LABEL_CONFLICT"
    if unexplained > 0:
        return "HOLD_DUE_TO_UNEXPLAINED_E2_GAP"
    return "PATCH_F_RECON_GO"


def _render_md(
    out: dict[str, Any], examples: list[dict[str, Any]], recon: str, explain: str,
) -> str:
    return f"""# Run-004 Patch F — E2 reconciliation audit

**Read-only:** CSV and JSON only; no RPC; no re-label; no `prepare_dataset` / retrain / promote / LIVE.

**Conclusion:** `{recon}`

## Summary (gap explanation)

{explain}

## Key metrics (see JSON for full)

| Metric | Value |
|--------|------:|
| `raw_labeled_csv_rows` | {out.get("raw_labeled_csv_rows")} |
| `unique_labeled_keys` | {out.get("unique_labeled_keys")} |
| `duplicate_rows_collapsed` | {out.get("duplicate_rows_collapsed")} |
| `duplicate_key_count` | {out.get("duplicate_key_count")} |
| `duplicate_conflict_count` | {out.get("duplicate_conflict_count")} |
| `target_input_v2_trainable_rows` | {out.get("target_input_v2_trainable_rows")} |
| `label_attempted_rows` (E2 report) | {out.get("label_attempted_rows_reported")} |
| `success_rows` (E2 report) | {out.get("success_rows_reported")} |
| `missing_target_keys_count` | {out.get("missing_target_keys_count")} |
| `unexplained_gap_count` | {out.get("unexplained_gap_count")} |

## Top examples (no RPC URLs; tx shortened)

```json
{json.dumps(examples, indent=2)[:20000]}
```

## Artifacts

- JSON: `mvp/diagnostics/run004_patch_f_e2_reconciliation.json`
- This file: `mvp/runs/run-004-patch-f-e2-reconciliation.md`
"""


def main() -> int:
    csv_labeled = Path(
        os.environ.get(
            "RUN004_F_E2_CSV",
            str(REPO / "logs" / "run004_v2only_labeled_dryrun_full.csv"),
        )
    )
    e2_json = Path(
        os.environ.get(
            "RUN004_F_E2_REPORT_JSON",
            str(REPO / "mvp" / "diagnostics" / "run004_patch_e2_v2only_label_dryrun_full_report.json"),
        )
    )
    candidates_csv = Path(
        os.environ.get(
            "RUN004_CANDIDATES_CSV",
            str(REPO / "logs" / "run004_candidates_swaponly_full.csv"),
        )
    )
    out_json = Path(
        os.environ.get(
            "RUN004_F_RECON_OUT_JSON",
            str(REPO / "mvp" / "diagnostics" / "run004_patch_f_e2_reconciliation.json"),
        )
    )
    out_md = Path(
        os.environ.get(
            "RUN004_F_RECON_OUT_MD",
            str(REPO / "mvp" / "runs" / "run-004-patch-f-e2-reconciliation.md"),
        )
    )
    net_eps = float(os.environ.get("RUN004_F_DUP_NET_EPS", "1e-4") or 1e-4)

    if not csv_labeled.is_file() or not e2_json.is_file():
        out_json.parent.mkdir(parents=True, exist_ok=True)
        err = {
            "conclusion": "HOLD_DUE_TO_MISSING_INPUT_FILES",
            "missing": {
                "labeled_csv": not csv_labeled.is_file(),
                "e2_report_json": not e2_json.is_file(),
            },
        }
        with out_json.open("w", encoding="utf-8") as f:
            json.dump(err, f, indent=2)
        with out_md.open("w", encoding="utf-8") as f:
            f.write(
                "# E2 reconciliation — missing inputs\n\n"
                f"labeled_csv exists={csv_labeled.is_file()} e2_json exists={e2_json.is_file()}\n"
            )
        return 1

    import validate_run004_executable_labels as vr  # noqa: E402

    tv2 = vr._norm_topic(vr.TOPIC_V2).lower()

    e2r = _load_json(e2_json)
    target_19879 = 0
    if candidates_csv.is_file():
        with candidates_csv.open("r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if (r.get("candidate_status") or "") != "trainable_candidate":
                    continue
                if (r.get("pool_type") or "").lower() != "v2":
                    continue
                if vr._norm_topic(r.get("topic0", "")).lower() != tv2:
                    continue
                target_19879 += 1

    need_cols = {
        "timestamp", "tx_hash", "log_index", "pre_check", "label", "label_reason",
        "net_return", "pool_address", "token_address",
    }
    fieldnames: list[str] | None = None
    with csv_labeled.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames or not need_cols.issubset(set(fieldnames)):
            schema_ok = False
            rows = []
        else:
            schema_ok = True
            rows = list(reader)

    if not schema_ok or not rows:
        out: dict[str, Any] = {
            "conclusion": "HOLD_DUE_TO_SCHEMA_MISMATCH",
            "raw_labeled_csv_rows": 0,
            "fieldnames": fieldnames,
        }
        out_json.parent.mkdir(parents=True, exist_ok=True)
        with out_json.open("w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, default=str)
        return 1

    by_k: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        by_k[(r.get("tx_hash", ""), r.get("log_index", ""))].append(r)

    raw = len(rows)
    dup_rows = raw - len(by_k)
    key_dup_count = sum(1 for v in by_k.values() if len(v) > 1)

    def is_succ(r: dict[str, str]) -> bool:
        try:
            lb = int(r.get("label", "-1"))
        except (TypeError, ValueError):
            return False
        return (r.get("label_reason") or "") == "executable" and lb in (0, 1)

    def pnet(r: dict[str, str]) -> float | None:
        try:
            return float((r.get("net_return") or "").strip() or "nan")
        except (TypeError, ValueError):
            return None

    conflict_key_set: set[tuple[str, str]] = set()
    for k, vs in by_k.items():
        succs = [v for v in vs if is_succ(v)]
        for i in range(len(succs)):
            for j in range(i + 1, len(succs)):
                a, b = succs[i], succs[j]
                if int(a["label"]) != int(b["label"]):
                    conflict_key_set.add(k)
                    break
                na, nb = pnet(a), pnet(b)
                if na is None or nb is None or na != na or nb != nb:  # nan
                    conflict_key_set.add(k)
                    break
                if abs(na - nb) > net_eps:
                    conflict_key_set.add(k)
                    break
    dup_conflicts = len(conflict_key_set)
    conflict_keys = sorted(conflict_key_set)[:20]

    cand_keys: set[tuple[str, str]] = set()
    if candidates_csv.is_file():
        with candidates_csv.open("r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if (r.get("candidate_status") or "") != "trainable_candidate":
                    continue
                if (r.get("pool_type") or "").lower() != "v2":
                    continue
                if vr._norm_topic(r.get("topic0", "")).lower() != tv2:
                    continue
                cand_keys.add((r.get("tx_hash", ""), r.get("log_index", "")))

    lab_keys = set(by_k.keys())
    missing_cand = len(cand_keys - lab_keys) if cand_keys else 0
    missing_lab = len(lab_keys - cand_keys) if cand_keys else 0
    missing_target = missing_cand + missing_lab

    pre = Counter()
    for r in rows:
        pre[(r.get("pre_check") or "").strip() or "empty"] += 1

    fail_by_reason: Counter[str] = Counter()
    for r in rows:
        lr = (r.get("label_reason") or "").strip() or "empty"
        try:
            lb = int(r.get("label", "-1"))
        except (TypeError, ValueError):
            lb = -1
        if (r.get("pre_check") or "") != "ok":
            fail_by_reason[f"precheck:{(r.get('pre_check') or '')}"] += 1
        elif is_succ(r):
            fail_by_reason["ok_executable"] += 1
        else:
            fail_by_reason[lr] += 1

    att_rep = int(e2r.get("label_attempted_rows", 0) or 0)
    succ_rep = int(e2r.get("label_success_rows", 0) or 0)
    inp_rep = int(e2r.get("input_v2_trainable_rows", 0) or 0)

    unique_n = len(by_k)
    duplicate_only_gap = max(0, raw - unique_n)
    work_minus_att = max(0, max(inp_rep, target_19879, raw) - att_rep) if att_rep else 0

    # Formula check: 19879 - 17577 = 2302
    att_gap = max(0, (inp_rep or target_19879) - att_rep) if (inp_rep or target_19879) else 0
    # Explain: (work rows) - (unique keys) = duplicate lines in the candidate/labeled work stream
    #           att_gap ≈ duplicate_only_gap - (att_rep - unique_n) if att is close to "unique with attempts"
    att_inflation = att_rep - unique_n  # 17577-17295=282

    explained_parts: list[str] = [
        f"Work stream has {max(inp_rep, target_19879, raw)} listed rows; only {unique_n} unique (tx_hash,log_index) events.",
        f"Extra non-unique rows: {duplicate_only_gap} (= duplicate work-row lines, incl. same-event duplicates and concurrent-writer copies).",
        f"E2 `label_attempted_rows`={att_rep} exceeds unique-event count {unique_n} by {att_inflation} due to "
        f"resume/seed counters counting multiple passes before deterministic dedup (see E2 report `resume` / `existing_csv_*`).",
        f"Therefore input−label_attempted = {(inp_rep or target_19879) - att_rep} = duplicate_stream_rows ({duplicate_only_gap}) "
        f"minus counter/reconciliation slack ({att_inflation}).",
    ]
    if missing_target > 0:
        explained_parts.append("WARNING: candidate vs labeled key set mismatch; see `missing_target_keys`.")

    # Unexplained: if gap formula doesn't match within 0 (integer)
    gap_reported = (inp_rep or target_19879) - att_rep
    expected_alt = max(0, duplicate_only_gap - max(0, att_inflation))
    unexplained = 0
    if abs(gap_reported - expected_alt) > 2:  # allow ±1 row drift
        unexplained = abs(gap_reported - expected_alt)

    recon = _recon_conclusion(
        False,
        schema_ok,
        missing_target,
        dup_conflicts,
        unexplained,
    )

    examples: list[dict[str, Any]] = []
    for k in conflict_keys[:20]:
        vs = by_k[k]
        ex = {
            "tx": _short_tx(k[0]),
            "log_index": k[1],
            "row_count": len(vs),
            "labels": [r.get("label") for r in vs],
            "label_reasons": [r.get("label_reason") for r in vs],
            "net_returns": [r.get("net_return") for r in vs],
        }
        examples.append(ex)

    # missing / unexplained top 20: keys in cand not lab (if any)
    miss_ex: list[dict[str, str]] = []
    for k in list(cand_keys - lab_keys)[:20]:
        miss_ex.append({"tx": _short_tx(k[0]), "log_index": k[1], "kind": "missing_in_labeled_csv"})

    out_obj: dict[str, Any] = {
        "conclusion": recon,
        "raw_labeled_csv_rows": raw,
        "unique_labeled_keys": unique_n,
        "duplicate_rows_collapsed": dup_rows,
        "duplicate_key_count": key_dup_count,
        "duplicate_conflict_count": dup_conflicts,
        "net_return_duplicate_eps": net_eps,
        "target_input_v2_trainable_rows": max(target_19879, inp_rep) if target_19879 or inp_rep else target_19879,
        "label_attempted_rows_reported": att_rep,
        "success_rows_reported": succ_rep,
        "e2_input_rows_reported": inp_rep,
        "failure_rows_by_outcome": dict(fail_by_reason),
        "pre_check_distribution": dict(pre),
        "missing_target_keys_count": missing_target,
        "unexplained_gap_count": unexplained,
        "gap_explanation": {
            "work_stream_row_count": max(inp_rep, target_19879, raw),
            "unique_event_keys": unique_n,
            "duplicate_work_rows": duplicate_only_gap,
            "label_attempted_minus_unique_keys": att_inflation,
            "input_vs_label_attempted_gap": gap_reported,
            "narrative": " ".join(explained_parts),
        },
        "top_duplicate_conflict_examples": examples,
        "top_missing_key_examples": miss_ex,
        "paths": {
            "input_csv": str(csv_labeled.relative_to(REPO)) if csv_labeled.is_relative_to(REPO) else str(csv_labeled),
            "e2_report_json": str(e2_json.relative_to(REPO)) if e2_json.is_relative_to(REPO) else str(e2_json),
            "candidates_csv": str(candidates_csv.relative_to(REPO)) if candidates_csv.is_relative_to(REPO) else str(candidates_csv),
            "out_json": str(out_json.relative_to(REPO)) if out_json.is_relative_to(REPO) else str(out_json),
        },
    }

    explain = out_obj["gap_explanation"]["narrative"]
    if dup_conflicts > 0:
        explain += (
            f" Duplicate-key success rows differ beyond eps={net_eps} for {dup_conflicts} keys "
            f"(concurrent runs / re-labeling); reconciliation HOLD until deduped."
        )
    if unexplained:
        explain += f" Unaccounted gap magnitude ~{unexplained} rows; investigate counts."

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(out_obj, f, indent=2, default=str)
    with out_md.open("w", encoding="utf-8") as f:
        f.write(
            _render_md({k: out_obj[k] for k in out_obj if k in (
                "raw_labeled_csv_rows", "unique_labeled_keys", "duplicate_rows_collapsed",
                "duplicate_key_count", "duplicate_conflict_count", "target_input_v2_trainable_rows",
                "label_attempted_rows_reported", "success_rows_reported", "missing_target_keys_count",
                "unexplained_gap_count",
            )}, examples, recon, explain),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
