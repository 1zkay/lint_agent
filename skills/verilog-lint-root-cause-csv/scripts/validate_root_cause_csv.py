#!/usr/bin/env python3
"""Validate the root-cause CSV schema and leaf violation references."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


REQUIRED_COLUMNS = [
    "root_id",
    "root_note",
    "fix_suggestion",
    "root_file_path",
    "root_file_start",
    "root_file_end",
    "parent_root_id",
    "leaf_violation_id",
    "leaf_violation_note",
]
INTEGER_RE = re.compile(r"^\d+$")
WHITESPACE_RE = re.compile(r"\s")
ROOT_ID_RE = re.compile(r"^root_\d{3,}$")
LEAF_ID_RE = re.compile(r"^vio_\d{3,}$")
FALSE_POSITIVE_ROOT_ID = "误报"


def _load_lint_items(path: Path | None) -> tuple[dict[str, tuple[str, str]], set[str]]:
    if path is None:
        return {}, set()
    items = json.loads(path.read_text(encoding="utf-8"))
    lint_items = {
        str(item.get("violation_id", "")).strip(): (
            str(item.get("message_id", "")).strip(),
            str(item.get("description", "")).strip(),
        )
        for item in items
        if str(item.get("violation_id", "")).strip()
    }
    source_files = {
        str(item.get("file_path", "")).strip()
        for item in items
        if str(item.get("file_path", "")).strip()
    }
    return lint_items, source_files


def _positive_int(value: str) -> int | None:
    text = str(value or "").strip()
    if not INTEGER_RE.match(text):
        return None
    number = int(text)
    return number if number > 0 else None


def validate(output_csv: Path, lint_items: Path | None) -> list[str]:
    lint_items_by_id, source_files = _load_lint_items(lint_items)
    valid_ids = set(lint_items_by_id)
    seen_leaf_ids: Counter[str] = Counter()
    id_locations: defaultdict[str, list[str]] = defaultdict(list)
    root_definitions: dict[str, tuple[str, str, str, str, str, str]] = {}
    parent_references: defaultdict[str, list[str]] = defaultdict(list)
    errors: list[str] = []

    with output_csv.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != REQUIRED_COLUMNS:
            errors.append(f"Header must be exactly {REQUIRED_COLUMNS}, got {reader.fieldnames}")
            return errors
        rows = list(reader)

    if not rows:
        errors.append("CSV must contain at least one data row")

    for index, row in enumerate(rows, start=2):
        if None in row:
            errors.append(f"Line {index}: row has extra CSV columns")
            continue

        root_id = str(row.get("root_id", "")).strip()
        root_note = str(row.get("root_note", "")).strip()
        fix_suggestion = str(row.get("fix_suggestion", "")).strip()
        root_file = str(row.get("root_file_path", "")).strip()
        start_value = str(row.get("root_file_start", "")).strip()
        end_value = str(row.get("root_file_end", "")).strip()
        parent_root_id = str(row.get("parent_root_id", "")).strip()
        leaf_id = str(row.get("leaf_violation_id", "")).strip()
        leaf_note = str(row.get("leaf_violation_note", "")).strip()
        leaf_base_id = ""

        is_false_positive = root_id == FALSE_POSITIVE_ROOT_ID

        if not root_id:
            errors.append(f"Line {index}: root_id is empty")
        elif not is_false_positive and not ROOT_ID_RE.match(root_id):
            errors.append(
                f"Line {index}: root_id must match root_<three-or-more digits> or be {FALSE_POSITIVE_ROOT_ID}, got {root_id}"
            )

        if not root_note:
            errors.append(f"Line {index}: root_note is empty")
        if not fix_suggestion:
            errors.append(f"Line {index}: fix_suggestion is empty")

        if not root_file:
            errors.append(f"Line {index}: root_file_path is empty")
        elif Path(root_file).name != root_file:
            errors.append(f"Line {index}: root_file_path must be a filename, got {root_file}")
        elif source_files and root_file not in source_files:
            errors.append(f"Line {index}: root_file_path is not in lint item source files: {root_file}")

        start = _positive_int(start_value)
        end = _positive_int(end_value)
        if start is None:
            errors.append(f"Line {index}: root_file_start must be a positive integer")
        if end is None:
            errors.append(f"Line {index}: root_file_end must be a positive integer")
        if start is not None and end is not None and start > end:
            errors.append(f"Line {index}: root_file_start cannot be greater than root_file_end")

        if not parent_root_id:
            errors.append(f"Line {index}: parent_root_id is empty; use / for a top-level root")
        elif parent_root_id != "/":
            if not ROOT_ID_RE.match(parent_root_id):
                errors.append(
                    f"Line {index}: parent_root_id must be / or root_<three-or-more digits>, got {parent_root_id}"
                )
            if parent_root_id == root_id:
                errors.append(f"Line {index}: parent_root_id cannot equal root_id")
            parent_references[parent_root_id].append(f"Line {index}")

        if not leaf_id:
            errors.append(f"Line {index}: leaf_violation_id is empty")
        elif leaf_id == "-":
            errors.append(f"Line {index}: leaf_violation_id must be an input violation_id, not '-'")
        elif "," in leaf_id or "、" in leaf_id or ";" in leaf_id or "；" in leaf_id:
            errors.append(f"Line {index}: leaf_violation_id must contain exactly one ID")
        elif WHITESPACE_RE.search(leaf_id):
            errors.append(f"Line {index}: leaf_violation_id contains whitespace")
        elif not LEAF_ID_RE.match(leaf_id):
            errors.append(
                f"Line {index}: leaf_violation_id must match vio_<three-or-more digits>, got {leaf_id}"
            )
        else:
            leaf_base_id = leaf_id
            if valid_ids and leaf_base_id not in valid_ids:
                errors.append(f"Line {index}: unknown leaf_violation_id {leaf_base_id}")
            elif lint_items_by_id:
                expected_message_id, expected_description = lint_items_by_id[leaf_base_id]
                expected_leaf_note = f"{expected_message_id}:{expected_description}"
                if leaf_note != expected_leaf_note:
                    errors.append(
                        f"Line {index}: leaf_violation_note must exactly match message_id:description from normalized lint"
                    )
        if not leaf_note:
            errors.append(f"Line {index}: leaf_violation_note is empty")

        if is_false_positive:
            if root_note == "/":
                errors.append(f"Line {index}: false-positive root_note must state the false-positive reason")
            if fix_suggestion != "/":
                errors.append(f"Line {index}: false-positive fix_suggestion must be /")
            if parent_root_id != "/":
                errors.append(f"Line {index}: false-positive parent_root_id must be /")

        if leaf_base_id:
            seen_leaf_ids[leaf_base_id] += 1
            id_locations[leaf_base_id].append(f"Line {index}")

        if root_id and not is_false_positive:
            root_definition = (
                root_note,
                fix_suggestion,
                root_file,
                start_value,
                end_value,
                parent_root_id,
            )
            previous_definition = root_definitions.setdefault(root_id, root_definition)
            if previous_definition != root_definition:
                errors.append(f"Line {index}: root_id {root_id} has inconsistent root fields")

    duplicate_ids = [leaf_id for leaf_id, count in seen_leaf_ids.items() if count > 1]
    for leaf_id in duplicate_ids[:20]:
        errors.append(
            f"leaf_violation_id {leaf_id} appears multiple times: "
            f"{', '.join(id_locations[leaf_id])}"
        )
    if len(duplicate_ids) > 20:
        errors.append(f"{len(duplicate_ids) - 20} more leaf_violation_id values appear multiple times")

    if valid_ids:
        missing_ids = sorted(valid_ids - set(seen_leaf_ids))
        for leaf_id in missing_ids[:20]:
            errors.append(f"missing leaf_violation_id {leaf_id}")
        if len(missing_ids) > 20:
            errors.append(f"{len(missing_ids) - 20} more input violation IDs are missing")

    known_root_ids = set(root_definitions)
    for parent_root_id, locations in sorted(parent_references.items()):
        if parent_root_id not in known_root_ids:
            errors.append(f"unknown parent_root_id {parent_root_id}: {', '.join(locations[:20])}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_csv")
    parser.add_argument("--lint-items")
    args = parser.parse_args()

    errors = validate(
        Path(args.output_csv),
        Path(args.lint_items) if args.lint_items else None,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("OK: root-cause leaf CSV is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
