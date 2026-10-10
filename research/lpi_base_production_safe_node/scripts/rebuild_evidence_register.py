#!/usr/bin/env python3
"""Rebuild evidence inventory TSV + Markdown register from existing raw files.

Prefers per-directory run_metadata.json; falls back to explicit legacy maps.
Does not delete or regenerate historical raw evidence bytes.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "reports" / "raw"
INV = ROOT / "evidence" / "raw_evidence_inventory.tsv"
REG = ROOT / "evidence" / "RAW_EVIDENCE_REGISTER.md"

FIELDS = (
    "relative_path",
    "size_bytes",
    "sha256",
    "classification",
    "retention_policy",
    "actual_source_round",
)

# Legacy only when run_metadata.json is absent. No guessing beyond this map.
LEGACY_DIR_SOURCE = {
    "offline_tests_20260717T134024Z": (
        "CORRECTION_ROUND_OFFLINE_TEST",
        "seal_in_git_permanent",
        "correction_round_offline_test",
    ),
    "offline_tests_20260717T132840Z": (
        "CORRECTION_ROUND_OFFLINE_TEST",
        "seal_in_git_permanent",
        "correction_round_offline_test",
    ),
    "offline_tests_20260717T132747Z": (
        "CORRECTION_ROUND_OFFLINE_TEST",
        "seal_in_git_permanent",
        "correction_round_offline_test",
    ),
    "offline_tests_20260717T135359Z": (
        "FINAL_OFFLINE_CLOSEOUT_TEST",
        "seal_in_git_permanent",
        "final_offline_closeout_test",
    ),
}


@dataclass(frozen=True)
class Entry:
    relative_path: str
    size_bytes: int
    sha256: str
    classification: str
    retention_policy: str
    actual_source_round: str

    def as_row(self) -> str:
        return "\t".join(
            [
                self.relative_path,
                str(self.size_bytes),
                self.sha256,
                self.classification,
                self.retention_policy,
                self.actual_source_round,
            ]
        )


def _read_run_metadata_for(path: Path) -> dict | None:
    """Walk up from file to RAW root looking for run_metadata.json."""
    cur = path.parent
    raw_resolved = RAW.resolve()
    for _ in range(8):
        meta = cur / "run_metadata.json"
        if meta.is_file() and not meta.is_symlink():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            if isinstance(data, dict) and all(k in data for k in ("round_id", "artifact_class", "source_commit", "created_at")):
                return data
        if cur.resolve() == raw_resolved or cur == ROOT:
            break
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def classify(rel: str, path: Path) -> tuple[str, str, str]:
    meta = _read_run_metadata_for(path)
    if meta is not None:
        round_id = str(meta["round_id"])
        artifact = str(meta["artifact_class"])
        return artifact, "seal_in_git_permanent", round_id

    # Legacy path maps (conservative, explicit).
    for dirname, triple in LEGACY_DIR_SOURCE.items():
        if dirname in rel:
            return triple

    if "bootstrap_health_pre_20260717T1311Z" in rel or "bootstrap_health_post_20260717T1315Z" in rel:
        return "REFERENCED_BOOTSTRAP_HEALTH_SNAPSHOT", "seal_in_git_permanent", "bootstrap_health_probe"
    if "bootstrap_health_pre_20260717/" in rel or rel.endswith("bootstrap_health_summary.tsv"):
        return "PRIOR_BOOTSTRAP_HEALTH", "seal_in_git_archive", "bootstrap_health_probe"
    if "live_capacity_" in rel:
        return "PRIOR_LIVE_CAPACITY_OBSERVE", "seal_in_git_archive_do_not_re_run_script", "prior_live_capacity_observe"
    if "offline_test_" in rel or "offline_secret_scan" in rel or "venv_health_" in rel:
        return "PRIOR_OFFLINE_TEST", "seal_in_git_archive", "prior_offline_test"
    if "clanker_1000" in rel:
        return "CLANKER_OFFLINE_PREFLIGHT", "seal_in_git_permanent", "clanker_offline_preflight"
    if "final_secret_scan" in rel or "CORRECTION_SECRET" in rel:
        return "SECRET_SCAN_ARTIFACT", "seal_in_git_archive", "correction_round_offline_test"
    if "offline_test_combined" in rel or "offline_test_output" in rel or "offline_test_resource" in rel:
        return "PRIOR_OFFLINE_TEST", "seal_in_git_archive", "prior_offline_test"
    return "RAW_OTHER", "seal_in_git_archive", "unknown_prior"


def collect() -> list[Entry]:
    entries: list[Entry] = []
    for path in sorted(RAW.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name == "LATEST_OFFLINE_TEST_DIR.txt":
            continue
        rel = str(path.relative_to(ROOT))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        classification, retention, source = classify(rel, path)
        entries.append(
            Entry(
                relative_path=rel,
                size_bytes=path.stat().st_size,
                sha256=digest,
                classification=classification,
                retention_policy=retention,
                actual_source_round=source,
            )
        )
    return entries


def write_inventory(entries: list[Entry]) -> None:
    INV.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(FIELDS)] + [e.as_row() for e in entries]
    INV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_register(entries: list[Entry]) -> None:
    lines = [
        "# Raw Evidence Register",
        "",
        f"generated_at_utc = {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"entries = {len(entries)}",
        "inventory = evidence/raw_evidence_inventory.tsv",
        "generator = scripts/rebuild_evidence_register.py",
        "",
        "| relative_path | size_bytes | sha256 | classification | retention_policy | actual_source_round |",
        "|---|---:|---|---|---|---|",
    ]
    for e in entries:
        lines.append(
            f"| `{e.relative_path}` | {e.size_bytes} | `{e.sha256}` | "
            f"{e.classification} | {e.retention_policy} | {e.actual_source_round} |"
        )
    lines += [
        "",
        "## Policy",
        "",
        "- Prefer directory `run_metadata.json` (round_id / artifact_class / source_commit / created_at).",
        "- Legacy timestamp maps are explicit and conservative; unknown stays UNKNOWN/RAW_OTHER.",
        "- Historical raw bytes are not deleted or regenerated by this script.",
        "",
    ]
    REG.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    entries = collect()
    write_inventory(entries)
    write_register(entries)
    print(f"entries={len(entries)} inventory={INV} register={REG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
