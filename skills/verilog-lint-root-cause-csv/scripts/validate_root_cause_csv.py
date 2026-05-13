#!/usr/bin/env python3
"""Validate the root-cause CSV schema and lint ViolationID references."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path


REQUIRED_COLUMNS = [
    "ViolationID",
    "Severity",
    "cause_file_path",
    "cause_line_start_end",
    "effect_violation_id",
]
LINE_RANGE_RE = re.compile(r"^\d+(?:-\d+)?$")
EFFECT_SPLIT_RE = re.compile(r"[、,;；\s]+")


def _load_lint_items(path: Path | None) -> tuple[set[str], set[str]]:
    if path is None:
        return set(), set()
    items = json.loads(path.read_text(encoding="utf-8"))
    valid_ids = {str(item.get("ViolationID", "")).strip() for item in items}
    source_files = {str(item.get("source_file", "")).strip() for item in items if item.get("source_file")}
    return valid_ids, source_files


def _split_effect_ids(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text or text == "-":
        return []
    return [item for item in EFFECT_SPLIT_RE.split(text) if item]


def validate(output_csv: Path, lint_items: Path | None) -> list[str]:
    valid_ids, source_files = _load_lint_items(lint_items)
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
        violation_id = str(row.get("ViolationID", "")).strip()
        severity = str(row.get("Severity", "")).strip()
        cause_file = str(row.get("cause_file_path", "")).strip()
        cause_range = str(row.get("cause_line_start_end", "")).strip()
        effect_ids = _split_effect_ids(str(row.get("effect_violation_id", "")))

        if not violation_id:
            errors.append(f"Line {index}: ViolationID is empty")
        elif valid_ids and violation_id not in valid_ids:
            errors.append(f"Line {index}: unknown ViolationID {violation_id}")

        if not severity:
            errors.append(f"Line {index}: Severity is empty")

        if not cause_file:
            errors.append(f"Line {index}: cause_file_path is empty")
        elif cause_file not in {"-", "误报"}:
            if Path(cause_file).name != cause_file:
                errors.append(f"Line {index}: cause_file_path must be a filename, got {cause_file}")
            if source_files and cause_file not in source_files:
                errors.append(f"Line {index}: cause_file_path is not in source archive: {cause_file}")

        if not cause_range:
            errors.append(f"Line {index}: cause_line_start_end is empty")
        elif cause_range != "-" and not LINE_RANGE_RE.match(cause_range):
            errors.append(f"Line {index}: invalid cause_line_start_end {cause_range}")

        for effect_id in effect_ids:
            if valid_ids and effect_id not in valid_ids:
                errors.append(f"Line {index}: unknown effect_violation_id {effect_id}")

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
