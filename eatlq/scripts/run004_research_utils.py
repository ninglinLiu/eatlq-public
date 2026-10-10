from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from statistics import mean, median
from typing import Any


def sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_rows(path: Path, *, limit: int | None = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(dict(row))
            if limit is not None and len(rows) >= limit:
                break
    return rows


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n")


def write_csv_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        fieldnames = keys
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(val) or math.isinf(val):
        return None
    return val


def fnum(value: float | None, digits: int = 8) -> str:
    if value is None:
        return ""
    if math.isnan(value) or math.isinf(value):
        return ""
    return str(round(value, digits))


def rounded(value: float | None, digits: int = 8) -> float | None:
    if value is None:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return round(value, digits)


def gini(values: list[float]) -> float | None:
    vals = sorted(v for v in values if v is not None and v >= 0)
    if not vals:
        return None
    total = sum(vals)
    if total <= 0:
        return 0.0
    n = len(vals)
    weighted = sum((idx + 1) * val for idx, val in enumerate(vals))
    return round((2 * weighted) / (n * total) - (n + 1) / n, 8)


def shannon_entropy_from_counts(counts: Counter[Any]) -> float | None:
    total = sum(counts.values())
    if total <= 0:
        return None
    entropy = 0.0
    for count in counts.values():
        if count <= 0:
            continue
        p = count / total
        entropy -= p * math.log(p)
    return round(entropy, 8)


def _percentile(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    idx = (len(vals) - 1) * q
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return vals[int(idx)]
    return vals[lo] * (hi - idx) + vals[hi] * (idx - lo)


def percentile(values: list[Any], q: float) -> float | None:
    vals = sorted(v for v in (optional_float(value) for value in values) if v is not None)
    if not vals:
        return None
    return _percentile(vals, q)


def numeric_summary(values: list[Any]) -> dict[str, Any]:
    vals = [optional_float(v) for v in values]
    nums = [v for v in vals if v is not None]
    missing_count = len(values) - len(nums)
    nonzero = [v for v in nums if abs(v) > 1e-12]
    if not nums:
        return {
            "count": 0,
            "missing_count": missing_count,
            "missing_rate": round(missing_count / len(values), 6) if values else None,
            "nonzero_rate": 0.0,
            "mean": None,
            "std": None,
            "min": None,
            "p1": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
            "constant": False,
            "all_zero": False,
            "extreme_value_flag": False,
        }
    vals_sorted = sorted(nums)
    mu = mean(nums)
    var = sum((v - mu) ** 2 for v in nums) / len(nums)
    max_abs = max(abs(v) for v in nums)
    return {
        "count": len(nums),
        "missing_count": missing_count,
        "missing_rate": round(missing_count / len(values), 6) if values else None,
        "nonzero_rate": round(len(nonzero) / len(nums), 6) if nums else 0.0,
        "mean": round(mu, 8),
        "std": round(math.sqrt(var), 8),
        "min": round(vals_sorted[0], 8),
        "p1": round(_percentile(vals_sorted, 0.01), 8),
        "p50": round(median(vals_sorted), 8),
        "p95": round(_percentile(vals_sorted, 0.95), 8),
        "p99": round(_percentile(vals_sorted, 0.99), 8),
        "max": round(vals_sorted[-1], 8),
        "constant": len(set(round(v, 12) for v in nums)) == 1,
        "all_zero": all(abs(v) <= 1e-12 for v in nums),
        "extreme_value_flag": max_abs > 1e18,
    }


def nonempty_coverage(rows: list[dict[str, Any]], columns: list[str]) -> dict[str, Any]:
    total = len(rows)
    covered = sum(1 for row in rows if any(str(row.get(col, "")) != "" for col in columns))
    return {
        "rows": total,
        "covered_rows": covered,
        "coverage": round(covered / total, 6) if total else 0,
        "columns": columns,
    }
