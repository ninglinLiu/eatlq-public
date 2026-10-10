#!/usr/bin/env python3
"""Static checker: alpha-tier artifacts SHOULD include minimal SOP metadata.

Required (case-insensitive keys; Markdown = YAML subset in leading frontmatter):

  sop_applied: true
  alpha_id: ...
  family: ...
  next_gate: ...
  decision_flags:
    training_allowed: false|true
    live_allowed: false|true

JSON: same keys at the top-level object.

This script does NOT authorize training/live; it only validates documentation shape.

Usage:
  python3 scripts/check_alpha_sop_compliance.py [--print-template] [--self-test] [PATH ...]
Exit codes: 0 all pass, 1 usage/help, 2 any failure
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

FRONT_MATTER_BLOCK = re.compile(r"\A---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|\Z)")


def _strip_inline_comment(line: str) -> str:
    if "#" in line:
        return line.split("#", 1)[0].rstrip()
    return line.rstrip()


def _parse_truthy(val: str) -> bool | None:
    v = val.strip().lower()
    if v in ("true", "yes", "1", "on"):
        return True
    if v in ("false", "no", "0", "off"):
        return False
    return None


def parse_simple_frontmatter(text: str) -> dict[str, Any] | None:
    m = FRONT_MATTER_BLOCK.match(text)
    if not m:
        return None
    raw = m.group(1).splitlines()
    root: dict[str, Any] = {}
    ctx: dict[str, Any] = root
    indent_stack: list[tuple[int, dict[str, Any]]] = []

    for line in raw:
        line = _strip_inline_comment(line)
        if not line.strip():
            continue
        stripped = line.lstrip(" \t")
        indent = len(line) - len(stripped)
        while indent_stack and indent <= indent_stack[-1][0]:
            indent_stack.pop()
            ctx = indent_stack[-1][1] if indent_stack else root

        kv = stripped.split(":", 1)
        if len(kv) != 2:
            continue
        key, rest = kv[0].strip().lower(), kv[1].strip()

        if not rest:
            # nested block start e.g. decision_flags:
            new: dict[str, Any] = {}
            ctx[key] = new
            indent_stack.append((indent, ctx))
            ctx = new
            continue

        tl = rest.lower()
        if tl in ("true", "false", "yes", "no", "1", "0", "on", "off"):
            ctx[key] = _parse_truthy(rest)
        elif re.fullmatch(r"-?\d+", rest.strip()):
            ctx[key] = int(rest.strip())
        else:
            ctx[key] = rest.strip().strip("\"'")

    return root


def _normalize_flags(obj: dict[str, Any]) -> dict[str, Any]:
    """Lower-case top-level keys; normalize decision_flags sub-keys."""
    out = {str(k).lower(): v for k, v in obj.items()}
    df = out.get("decision_flags")
    if isinstance(df, dict):
        out["decision_flags"] = {str(k).lower(): v for k, v in df.items()}
    return out


def _flags_ok(obj: dict[str, Any]) -> tuple[bool, str]:
    obj = _normalize_flags(obj)
    sop = obj.get("sop_applied")
    if sop is not True:
        return False, "sop_applied must be boolean true"

    alpha_id = obj.get("alpha_id")
    if not alpha_id or not str(alpha_id).strip():
        return False, "alpha_id missing or empty"

    family = obj.get("family")
    if not family or not str(family).strip():
        return False, "family missing or empty"

    nxt = obj.get("next_gate")
    if not nxt or not str(nxt).strip():
        return False, "next_gate missing or empty"

    df = obj.get("decision_flags")
    if not isinstance(df, dict):
        return False, "decision_flags must be a mapping/object"

    for k in ("training_allowed", "live_allowed"):
        if k not in df:
            return False, f"decision_flags missing {k}"
        if not isinstance(df[k], bool):
            return False, f"decision_flags.{k} must be boolean"

    return True, "ok"


def check_markdown(path: Path) -> tuple[bool, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = parse_simple_frontmatter(text)
    if fm is None:
        return False, "no YAML frontmatter block starting with --- … ---"
    ok, msg = _flags_ok(fm)
    return ok, msg


def check_json(path: Path) -> tuple[bool, str]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict):
        return False, "JSON root must be object"
    ok, msg = _flags_ok(data)
    return ok, msg


def check_file(path: Path) -> tuple[bool, str]:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return check_markdown(path)
    if suffix == ".json":
        return check_json(path)
    return False, f"unsupported suffix {suffix!r} (use .md or .json)"


GOOD_FRONTMATTER = """---
sop_applied: true
alpha_id: QA-GOOD
family: F5
next_gate: SELF_TEST_PASS
decision_flags:
  training_allowed: false
  live_allowed: false
---

# Fixture

Body.
"""

BAD_FRONTMATTER = """---
sop_applied: true
family: F5
next_gate: MISSING_ALPHA_ID
decision_flags:
  training_allowed: false
---

broken
"""


def self_test() -> int:
    fails = 0
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "good.md"
        bad = Path(td) / "bad.md"
        good.write_text(GOOD_FRONTMATTER, encoding="utf-8")
        bad.write_text(BAD_FRONTMATTER, encoding="utf-8")
        ok, msg = check_file(good)
        if not ok:
            print(f"SELF-TEST FAIL: expected good.md PASS, got: {msg}", file=sys.stderr)
            fails += 1
        else:
            print("SELF-TEST: good.md PASS")
        ok_b, msg_b = check_file(bad)
        if ok_b:
            print("SELF-TEST FAIL: expected bad.md FAIL", file=sys.stderr)
            fails += 1
        else:
            print(f"SELF-TEST: bad.md FAIL (expected): {msg_b}")

        gj = Path(td) / "good.json"
        gj.write_text(
            json.dumps(
                {
                    "SOP_APPLIED": True,
                    "alpha_id": "QA-JSON",
                    "family": "F2",
                    "next_gate": "OK",
                    "decision_flags": {"training_allowed": False, "live_allowed": False},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        ok_j, msg_j = check_file(gj)
        if not ok_j:
            print(f"SELF-TEST FAIL: good.json expected PASS: {msg_j}", file=sys.stderr)
            fails += 1
        else:
            print("SELF-TEST: good.json PASS")

    return 2 if fails else 0


def print_template() -> None:
    print(
        "---\n"
        "sop_applied: true\n"
        "alpha_id: YOUR_ALPHA_ID\n"
        "family: F1\n"
        "next_gate: ACTIVE_SHADOW\n"
        "decision_flags:\n"
        "  training_allowed: false\n"
        "  live_allowed: false\n"
        "---\n",
        end="",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", type=Path, help="Markdown (.md) or JSON files")
    ap.add_argument("--self-test", action="store_true", help="built-in PASS/FAIL fixtures")
    ap.add_argument(
        "--print-template",
        action="store_true",
        help="print minimal frontmatter to stdout and exit",
    )
    args = ap.parse_args()
    if args.print_template:
        print_template()
        return 0
    if args.self_test:
        return self_test()

    if not args.paths:
        ap.print_help()
        return 1

    failures = 0
    for p in args.paths:
        path = (ROOT / p) if not p.is_absolute() else p
        if not path.is_file():
            print(f"FAIL {path}: not a file", file=sys.stderr)
            failures += 1
            continue
        ok, msg = check_file(path)
        tag = "PASS" if ok else "FAIL"
        print(f"{tag} {path}: {msg}")
        if not ok:
            failures += 1

    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
