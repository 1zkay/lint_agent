#!/usr/bin/env python3
"""Build a compact catalog from validated work-unit root-cause reports."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

from _contract import (
    FALSE_POSITIVE_ROOT_ID,
    LOCAL_ROOT_CATALOG_COLUMNS,
    ROOT_CAUSE_COLUMNS,
)
from _work_units import local_item_id, read_manifest


def read_report(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ROOT_CAUSE_COLUMNS:
            raise ValueError(f"{path}: unexpected root-cause CSV header")
        return list(reader)


def build_catalog(slices_dir: Path) -> list[dict[str, str | int]]:
    catalog: list[dict[str, str | int]] = []
    seen_leaf_ids: set[str] = set()
    for unit_id, unit_dir in read_manifest(slices_dir).work_units:
        report_path = unit_dir / "local_root_cause.csv"
        if not report_path.is_file():
            raise FileNotFoundError(f"local report not found: {report_path}")
        rows = read_report(report_path)
        by_root: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in rows:
            leaf_id = row["leaf_violation_id"]
            if leaf_id in seen_leaf_ids:
                raise ValueError(f"duplicate leaf across local reports: {leaf_id}")
            seen_leaf_ids.add(leaf_id)
            by_root[row["root_id"]].append(row)

        normal_items = {
            root_id: local_item_id(unit_id, root_id, "")
            for root_id in by_root
            if root_id != FALSE_POSITIVE_ROOT_ID
        }
        for root_id, root_rows in sorted(by_root.items()):
            if root_id == FALSE_POSITIVE_ROOT_ID:
                item_rows = ([row] for row in root_rows)
            else:
                item_rows = (root_rows,)
            for selected_rows in item_rows:
                first = selected_rows[0]
                parent = first["parent_root_id"]
                parent_item = "/" if parent == "/" else normal_items[parent]
                catalog.append(
                    {
                        "local_item_id": local_item_id(
                            unit_id,
                            root_id,
                            first["leaf_violation_id"],
                        ),
                        "unit_id": unit_id,
                        "local_root_id": root_id,
                        "root_note": first["root_note"],
                        "fix_suggestion": first["fix_suggestion"],
                        "parent_local_item_id": parent_item,
                        "leaf_count": len(selected_rows),
                    }
                )
    ids = [str(row["local_item_id"]) for row in catalog]
    duplicates = [value for value, count in Counter(ids).items() if count > 1]
    if duplicates:
        raise ValueError(f"duplicate local_item_id values: {duplicates}")
    return catalog


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slices-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = build_catalog(args.slices_dir.expanduser().resolve())
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOCAL_ROOT_CATALOG_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"LOCAL_ROOT_CATALOG={output}")
    print(f"LOCAL_ROOT_ITEMS={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
