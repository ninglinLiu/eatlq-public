#!/usr/bin/env python3
"""Read-only validator: audit JSON files must not flip authorization booleans."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


class FlagViolation:
    __slots__ = ("file_rel", "json_path", "key", "value", "detail")

    def __init__(
        self, file_rel: str, json_path: str, key: str, value: object, detail: str
    ) -> None:
        self.file_rel = file_rel
        self.json_path = json_path
        self.key = key
        self.value = value
        self.detail = detail


def _is_authorization_flag_key(key: str) -> bool:
    return key.startswith("allow_") or key.endswith("_ALLOWED")


def _walk(
    node: object,
    path: str,
    file_rel: str,
    out: list[FlagViolation],
) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            child_path = f"{path}.{k}" if path != "$" else f"$.{k}"
            if _is_authorization_flag_key(k):
                if not isinstance(v, bool):
                    out.append(
                        FlagViolation(
                            file_rel,
                            child_path,
                            k,
                            v,
                            "authorization flags must use JSON booleans (false/true), strict false required",
                        )
                    )
                elif v is not False:
                    out.append(
                        FlagViolation(
                            file_rel,
                            child_path,
                            k,
                            v,
                            "value must be the boolean false",
                        )
                    )
            _walk(v, child_path, file_rel, out)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk(item, f"{path}[{i}]", file_rel, out)


def _audit_file(path: Path, root: Path, out: list[FlagViolation]) -> None:
    file_rel = str(path.relative_to(root))
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        out.append(
            FlagViolation(file_rel, "$", "", None, f"read error: {exc}")
        )
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        out.append(
            FlagViolation(file_rel, "$", "", None, f"invalid JSON: {exc}")
        )
        return
    _walk(payload, "$", file_rel, out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate authorization flags in mvp/audits/*.json",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root containing mvp/audits/",
    )
    parsed = parser.parse_args(argv)
    root = parsed.root.resolve()
    audits = root / "mvp" / "audits"

    violations: list[FlagViolation] = []

    if not audits.is_dir():
        print(f"error: audits directory missing: {audits}", file=sys.stderr)
        return 2

    json_paths = sorted(audits.glob("*.json"))

    if not json_paths:
        print("warning: no *.json files in mvp/audits/", file=sys.stderr)

    for jpath in json_paths:
        _audit_file(jpath, root, violations)

    if violations:
        for v in violations:
            msg = (
                f"{v.file_rel}: json_path={v.json_path}"
                + (f" key={v.key!r}" if v.key else "")
                + (f" value={v.value!r}" if v.detail != "invalid JSON" else "")
                + f" — {v.detail}"
            )
            print(msg, file=sys.stderr)
        return 1

    n = len(json_paths)
    print(
        f"OK: audited {n} JSON files; all allow_* and *_ALLOWED flags are false.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
