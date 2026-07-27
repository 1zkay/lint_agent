#!/usr/bin/env python3
"""Validate a global mapping over every local root item."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from _contract import (
    FALSE_POSITIVE_ROOT_ID,
    GLOBAL_ROOT_MAP_COLUMNS,
    LOCAL_ROOT_CATALOG_COLUMNS,
    ROOT_ID_RE,
    format_root_id,
)

CJK_RE = re.compile(r"[\u3400-\u9fff]")


def read_csv(path: Path, columns: list[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        if reader.fieldnames != columns:
            raise ValueError(f"{path}: expected header {columns}")
        rows = list(reader)
    if any(None in row for row in rows):
        raise ValueError(f"{path}: row has extra CSV columns")
    return rows


def parent_cycle_errors(parents: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for start in parents:
        visited: list[str] = []
        current = start
        while current != "/" and current in parents:
            if current in visited:
                cycle = visited[visited.index(current) :] + [current]
                errors.append(f"parent_global_root_id cycle: {' -> '.join(cycle)}")
                break
            visited.append(current)
            current = parents[current]
    return sorted(set(errors))


def validate(map_path: Path, catalog_path: Path) -> list[str]:
    catalog = read_csv(catalog_path, LOCAL_ROOT_CATALOG_COLUMNS)
    try:
        rows = read_csv(map_path, GLOBAL_ROOT_MAP_COLUMNS)
    except (csv.Error, ValueError) as exc:
        return [str(exc)]
    catalog_items = [row["local_item_id"] for row in catalog]
    if len(catalog_items) != len(set(catalog_items)):
        raise ValueError(f"{catalog_path}: duplicate local_item_id values")
    expected_items = set(catalog_items)
    seen = Counter(row["local_item_id"] for row in rows)
    errors: list[str] = []
    for item_id in sorted(expected_items - set(seen)):
        errors.append(f"missing local_item_id {item_id}")
    for item_id, count in sorted(seen.items()):
        if item_id not in expected_items:
            errors.append(f"unknown local_item_id {item_id}")
        elif count != 1:
            errors.append(f"local_item_id {item_id} appears {count} times")

    definitions: dict[str, tuple[str, str, str]] = {}
    parent_locations: defaultdict[str, list[int]] = defaultdict(list)
    for line, row in enumerate(rows, 2):
        item_id = row["local_item_id"]
        root_id = row["global_root_id"].strip()
        root_note = row["root_note"].strip()
        fix = row["fix_suggestion"].strip()
        parent = row["parent_global_root_id"].strip()
        is_false_positive = root_id == FALSE_POSITIVE_ROOT_ID
        if item_id != item_id.strip():
            errors.append(f"Line {line}: local_item_id contains outer whitespace")
        if root_id != row["global_root_id"]:
            errors.append(f"Line {line}: global_root_id contains outer whitespace")
        if parent != row["parent_global_root_id"]:
            errors.append(
                f"Line {line}: parent_global_root_id contains outer whitespace"
            )
        if not is_false_positive and not ROOT_ID_RE.match(root_id):
            errors.append(f"Line {line}: invalid global_root_id {root_id!r}")
        if not root_note or not CJK_RE.search(root_note):
            errors.append(f"Line {line}: root_note must contain Chinese text")
        if is_false_positive:
            if fix != "/" or parent != "/":
                errors.append(
                    f"Line {line}: false positive must use / for fix and parent"
                )
            continue
        if not fix or not CJK_RE.search(fix):
            errors.append(f"Line {line}: fix_suggestion must contain Chinese text")
        if parent != "/" and not ROOT_ID_RE.match(parent):
            errors.append(f"Line {line}: invalid parent_global_root_id {parent!r}")
        if parent == root_id:
            errors.append(f"Line {line}: root cannot be its own parent")
        if parent != "/":
            parent_locations[parent].append(line)
        definition = (root_note, fix, parent)
        previous = definitions.setdefault(root_id, definition)
        if previous != definition:
            errors.append(f"Line {line}: inconsistent definition for {root_id}")

    normal_ids = set(definitions)
    expected_ids = {
        format_root_id(number) for number in range(1, len(normal_ids) + 1)
    }
    if normal_ids != expected_ids:
        errors.append("normal global_root_id values must be contiguous from root_001")
    for parent, lines in parent_locations.items():
        if parent not in definitions:
            errors.append(
                f"unknown parent_global_root_id {parent}: lines {lines[:20]}"
            )
    errors.extend(
        parent_cycle_errors(
            {root_id: definition[2] for root_id, definition in definitions.items()}
        )
    )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("map_csv", type=Path)
    parser.add_argument("--catalog", type=Path, required=True)
    args = parser.parse_args()
    try:
        errors = validate(
            args.map_csv.expanduser().resolve(),
            args.catalog.expanduser().resolve(),
        )
    except Exception as exc:
        print(f"ERROR: validator failed: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print("OK: global root map is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
