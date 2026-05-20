#!/usr/bin/env python3
"""Validate the root-cause CSV schema and effect violation references."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


REQUIRED_COLUMNS = [
    "cause_file_path",
    "cause_file_start",
    "cause_file_end",
    "effect_violation_id",
]
INTEGER_RE = re.compile(r"^\d+$")
WHITESPACE_RE = re.compile(r"\s")


def _load_lint_items(path: Path | None) -> tuple[set[str], set[str]]:
    if path is None:
        return set(), set()
    items = json.loads(path.read_text(encoding="utf-8"))
    valid_ids = {str(item.get("violation_id", "")).strip() for item in items if item.get("violation_id")}
    source_files = {
        str(item.get("file_path", "")).strip()
        for item in items
        if str(item.get("file_path", "")).strip()
    }
    return valid_ids, source_files


def _positive_int(value: str) -> int | None:
    text = str(value or "").strip()
    if not INTEGER_RE.match(text):
        return None
    number = int(text)
    return number if number > 0 else None


def validate(output_csv: Path, lint_items: Path | None) -> list[str]:
    valid_ids, source_files = _load_lint_items(lint_items)
    seen_effect_ids: Counter[str] = Counter()
    id_locations: defaultdict[str, list[str]] = defaultdict(list)
    errors: list[str] = []

    if not output_csv.read_bytes().startswith(b"\xef\xbb\xbf"):
        errors.append("CSV must be encoded as UTF-8 with BOM (`utf-8-sig`)")

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

        cause_file = str(row.get("cause_file_path", "")).strip()
        start_value = str(row.get("cause_file_start", "")).strip()
        end_value = str(row.get("cause_file_end", "")).strip()
        effect_id = str(row.get("effect_violation_id", "")).strip()

        if not cause_file:
            errors.append(f"Line {index}: cause_file_path is empty")
        elif Path(cause_file).name != cause_file:
            errors.append(f"Line {index}: cause_file_path must be a filename, got {cause_file}")
        elif source_files and cause_file not in source_files:
            errors.append(f"Line {index}: cause_file_path is not in source archive: {cause_file}")

        start = _positive_int(start_value)
        end = _positive_int(end_value)
        if start is None:
            errors.append(f"Line {index}: cause_file_start must be a positive integer")
        if end is None:
            errors.append(f"Line {index}: cause_file_end must be a positive integer")
        if start is not None and end is not None and start > end:
            errors.append(f"Line {index}: cause_file_start cannot be greater than cause_file_end")

        if not effect_id:
            errors.append(f"Line {index}: effect_violation_id is empty")
        elif effect_id == "-":
            errors.append(f"Line {index}: effect_violation_id must be an input violation_id, not '-'")
        elif "," in effect_id or "、" in effect_id or ";" in effect_id or "；" in effect_id:
            errors.append(f"Line {index}: effect_violation_id must contain exactly one ID")
        elif WHITESPACE_RE.search(effect_id):
            errors.append(f"Line {index}: effect_violation_id contains whitespace")
        elif valid_ids and effect_id not in valid_ids:
            errors.append(f"Line {index}: unknown effect_violation_id {effect_id}")

        if effect_id:
            seen_effect_ids[effect_id] += 1
            id_locations[effect_id].append(f"Line {index}")

    duplicate_ids = [effect_id for effect_id, count in seen_effect_ids.items() if count > 1]
    for effect_id in duplicate_ids[:20]:
        errors.append(
            f"effect_violation_id {effect_id} appears multiple times: "
            f"{', '.join(id_locations[effect_id])}"
        )
    if len(duplicate_ids) > 20:
        errors.append(f"{len(duplicate_ids) - 20} more effect_violation_id values appear multiple times")

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
    print("OK: root-cause CSV is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
