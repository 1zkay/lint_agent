#!/usr/bin/env python3
"""Inspect one lint work unit without loading its entire CSV into context."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from _work_units import read_lint_rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("work_unit_dir", type=Path)
    parser.add_argument("--offset", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--vio-id")
    args = parser.parse_args()
    offset = 0 if args.offset is None else args.offset
    limit = 50 if args.limit is None else args.limit
    if offset < 0 or limit < 1:
        parser.error("--offset must be nonnegative and --limit must be positive")

    unit_dir = args.work_unit_dir.expanduser().resolve()
    rows = read_lint_rows(unit_dir / "lint.csv")
    context = json.loads((unit_dir / "context.json").read_text(encoding="utf-8"))
    if args.vio_id:
        selected = [row for row in rows if row["vio_id"] == args.vio_id]
        if not selected:
            raise ValueError(f"unknown vio_id in work unit: {args.vio_id}")
        result = {"rows": selected}
    elif args.offset is not None or args.limit is not None:
        result = {
            "offset": offset,
            "limit": limit,
            "total": len(rows),
            "rows": rows[offset : offset + limit],
        }
    else:
        result = {
            "lint_count": len(rows),
            "primary_source_files": context["primary_source_files"],
            "dependency_source_files": context["dependency_source_files"],
            "hierarchy_paths": context["hierarchy_paths"],
            "message_ids": dict(
                sorted(Counter(row["message_id"] for row in rows).items())
            ),
            "source_files": dict(
                sorted(Counter(row["source_file"] for row in rows).items())
            ),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
