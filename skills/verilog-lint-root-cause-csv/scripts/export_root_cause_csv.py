#!/usr/bin/env python3
"""Deterministically expand an adjudicated global map into the final leaf CSV."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from _contract import (
    GLOBAL_ROOT_MAP_COLUMNS,
    ROOT_CAUSE_COLUMNS,
)
from _work_units import local_item_id, read_manifest


def read_csv(path: Path, columns: list[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != columns:
            raise ValueError(f"{path}: expected header {columns}")
        return list(reader)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slices-dir", type=Path, required=True)
    parser.add_argument("--global-map", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    slices_dir = args.slices_dir.expanduser().resolve()
    mapping_rows = read_csv(
        args.global_map.expanduser().resolve(), GLOBAL_ROOT_MAP_COLUMNS
    )
    mapping = {row["local_item_id"]: row for row in mapping_rows}
    if len(mapping) != len(mapping_rows):
        raise ValueError("global map contains duplicate local_item_id values")

    output_rows: list[dict[str, str]] = []
    seen_items: set[str] = set()
    for unit_id, unit_dir in read_manifest(slices_dir).work_units:
        local_rows = read_csv(
            unit_dir / "local_root_cause.csv", ROOT_CAUSE_COLUMNS
        )
        for row in local_rows:
            item_id = local_item_id(
                unit_id,
                row["root_id"],
                row["leaf_violation_id"],
            )
            global_row = mapping.get(item_id)
            if global_row is None:
                raise ValueError(f"global map is missing local item {item_id}")
            seen_items.add(item_id)
            output_rows.append(
                {
                    "root_id": global_row["global_root_id"],
                    "root_note": global_row["root_note"],
                    "fix_suggestion": global_row["fix_suggestion"],
                    "root_file_path": row["root_file_path"],
                    "root_file_start": row["root_file_start"],
                    "root_file_end": row["root_file_end"],
                    "parent_root_id": global_row["parent_global_root_id"],
                    "leaf_violation_id": row["leaf_violation_id"],
                    "leaf_violation_note": row["leaf_violation_note"],
                }
            )
    unused_items = sorted(set(mapping) - seen_items)
    if unused_items:
        raise ValueError(f"global map contains unused local items: {unused_items[:20]}")

    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROOT_CAUSE_COLUMNS)
        writer.writeheader()
        writer.writerows(output_rows)
    print(f"ROOT_CAUSE_DRAFT={output}")
    print(f"LEAF_ROWS={len(output_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
