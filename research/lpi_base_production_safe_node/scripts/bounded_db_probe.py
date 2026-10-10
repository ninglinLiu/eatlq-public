#!/usr/bin/env python3
"""Bounded database probe planner — dry-run / explain-only. Never executes business queries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from launchpad_intelligence.config import load_access_defaults
from launchpad_intelligence.errors import AccessDeniedError, PreflightError
from launchpad_intelligence.query.preflight import BoundedQueryPlan, build_preflight


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate-table", required=True, help="Logical candidate table name only")
    p.add_argument("--columns", required=True, help="Comma-separated explicit columns")
    p.add_argument("--partition-key", default="block_number")
    p.add_argument("--start-block", type=int, required=True)
    p.add_argument("--end-block", type=int, required=True)
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--max-threads", type=int, default=1)
    p.add_argument("--statement-timeout-sec", type=int, default=10)
    p.add_argument("--max-bytes-to-read", type=int, default=8_388_608)
    p.add_argument("--checkpoint", default=None)
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--explain-only", action="store_true", default=True)
    p.add_argument("--execute", action="store_true", help="Rejected unless access defaults open")
    p.add_argument("--out", type=Path, default=None)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    access = load_access_defaults()
    columns = tuple(c.strip() for c in args.columns.split(",") if c.strip())
    plan = BoundedQueryPlan(
        candidate_table=args.candidate_table,
        explicit_columns=columns,
        partition_key=args.partition_key,
        start_block=args.start_block,
        end_block=args.end_block,
        limit=args.limit,
        max_threads=args.max_threads,
        statement_timeout_sec=args.statement_timeout_sec,
        max_bytes_to_read=args.max_bytes_to_read,
        readonly=True,
        dry_run=True,
        explain_only=True,
        checkpoint_path=args.checkpoint,
    )
    try:
        report = build_preflight(plan)
    except PreflightError as exc:
        print(json.dumps({"status": "PREFLIGHT_REJECTED", "error": str(exc)}, indent=2))
        return 2

    if args.execute:
        try:
            access.require_database_access()
            access.require_business_table_query()
        except AccessDeniedError as exc:
            payload = report.to_dict()
            payload["execute_attempt"] = "DENIED"
            payload["error"] = str(exc)
            text = json.dumps(payload, indent=2)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(text + "\n", encoding="utf-8")
            print(text)
            return 3

    text = json.dumps(report.to_dict(), indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
