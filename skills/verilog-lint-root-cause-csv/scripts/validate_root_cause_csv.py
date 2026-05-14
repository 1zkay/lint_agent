#!/usr/bin/env python3
"""Validate the root-cause CSV schema and lint ViolationID references."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


REQUIRED_COLUMNS = [
    "ViolationID",
    "Severity",
    "cause_file_path",
    "cause_line_start_end",
    "effect_violation_id",
    "root_cause_analysis",
]
LINE_RANGE_PART_RE = re.compile(r"^\d+(?:-\d+)?$")
NON_STANDARD_ID_SEPARATOR_RE = re.compile(r"[,;；]")
WHITESPACE_RE = re.compile(r"\s")


def _load_lint_items(path: Path | None) -> tuple[list[str], set[str]]:
    if path is None:
        return [], set()
    items = json.loads(path.read_text(encoding="utf-8"))
    valid_ids = [str(item.get("ViolationID", "")).strip() for item in items if item.get("ViolationID")]
    source_files = {str(item.get("source_file", "")).strip() for item in items if item.get("source_file")}
    return valid_ids, source_files


def _split_id_list(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text or text == "-":
        return []
    return [part.strip() for part in text.split("、")]


def _validate_id_list_format(value: str, field: str, line: int, errors: list[str]) -> None:
    text = str(value or "").strip()
    if not text or text == "-":
        return
    if NON_STANDARD_ID_SEPARATOR_RE.search(text):
        errors.append(f"Line {line}: {field} must use `、` as the ID separator")
    parts = [part.strip() for part in text.split("、")]
    if any(not part for part in parts):
        errors.append(f"Line {line}: {field} contains an empty ID")
    if any(WHITESPACE_RE.search(part) for part in parts):
        errors.append(f"Line {line}: {field} contains whitespace inside an ID")


def _valid_cause_line_range(value: str) -> bool:
    text = str(value or "").strip()
    if text == "-":
        return True
    parts = [part.strip() for part in text.split("、")]
    if not parts or any(not part for part in parts):
        return False
    for part in parts:
        if not LINE_RANGE_PART_RE.match(part):
            return False
        if "-" in part:
            start, end = [int(item) for item in part.split("-", 1)]
            if start <= 0 or end <= 0 or start > end:
                return False
        elif int(part) <= 0:
            return False
    return True


def validate(output_csv: Path, lint_items: Path | None) -> list[str]:
    valid_id_list, source_files = _load_lint_items(lint_items)
    valid_ids = set(valid_id_list)
    covered_ids: Counter[str] = Counter()
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
            errors.append(f"Line {index}: row has extra CSV columns; use `、` inside fields, not comma")
            continue

        violation_id_value = str(row.get("ViolationID", "")).strip()
        severity = str(row.get("Severity", "")).strip()
        cause_file = str(row.get("cause_file_path", "")).strip()
        cause_range = str(row.get("cause_line_start_end", "")).strip()
        effect_id_value = str(row.get("effect_violation_id", "")).strip()
        _validate_id_list_format(violation_id_value, "ViolationID", index, errors)
        _validate_id_list_format(effect_id_value, "effect_violation_id", index, errors)
        violation_ids = _split_id_list(violation_id_value)
        effect_ids = _split_id_list(effect_id_value)
        analysis = str(row.get("root_cause_analysis", "")).strip()

        if not violation_ids:
            errors.append(f"Line {index}: ViolationID is empty")
        if len(set(violation_ids)) != len(violation_ids):
            errors.append(f"Line {index}: ViolationID contains duplicate IDs")
        if len(set(effect_ids)) != len(effect_ids):
            errors.append(f"Line {index}: effect_violation_id contains duplicate IDs")
        repeated_between_fields = sorted(set(violation_ids) & set(effect_ids))
        if repeated_between_fields:
            errors.append(
                f"Line {index}: IDs cannot appear in both ViolationID and effect_violation_id: "
                f"{'、'.join(repeated_between_fields)}"
            )
        for violation_id in violation_ids:
            if valid_ids and violation_id not in valid_ids:
                errors.append(f"Line {index}: unknown ViolationID {violation_id}")
            covered_ids[violation_id] += 1
            id_locations[violation_id].append(f"Line {index} ViolationID")

        if not severity:
            errors.append(f"Line {index}: Severity is empty")

        if not analysis:
            errors.append(f"Line {index}: root_cause_analysis is empty")

        if not cause_file:
            errors.append(f"Line {index}: cause_file_path is empty")
        elif cause_file not in {"-", "误报"}:
            if Path(cause_file).name != cause_file:
                errors.append(f"Line {index}: cause_file_path must be a filename, got {cause_file}")
            if source_files and cause_file not in source_files:
                errors.append(f"Line {index}: cause_file_path is not in source archive: {cause_file}")

        if not cause_range:
            errors.append(f"Line {index}: cause_line_start_end is empty")
        elif not _valid_cause_line_range(cause_range):
            errors.append(f"Line {index}: invalid cause_line_start_end {cause_range}")

        if cause_file == "误报":
            if cause_range != "-":
                errors.append(f"Line {index}: false positive must use cause_line_start_end=-")
            if effect_id_value != "-":
                errors.append(f"Line {index}: false positive must use effect_violation_id=-")
            if "误报" not in analysis and "false positive" not in analysis.lower():
                errors.append(f"Line {index}: false positive analysis should explicitly mention 误报")
        elif len(violation_ids) > 1:
            errors.append(f"Line {index}: real root-cause rows must use exactly one ViolationID")

        for effect_id in effect_ids:
            if valid_ids and effect_id not in valid_ids:
                errors.append(f"Line {index}: unknown effect_violation_id {effect_id}")
            covered_ids[effect_id] += 1
            id_locations[effect_id].append(f"Line {index} effect_violation_id")

    if valid_ids:
        missing_ids = [violation_id for violation_id in valid_id_list if covered_ids[violation_id] == 0]
        if missing_ids:
            preview = "、".join(missing_ids[:20])
            suffix = "" if len(missing_ids) <= 20 else f" ... and {len(missing_ids) - 20} more"
            errors.append(f"Missing lint ViolationID coverage: {preview}{suffix}")

        duplicate_ids = [violation_id for violation_id in valid_id_list if covered_ids[violation_id] > 1]
        for violation_id in duplicate_ids[:20]:
            errors.append(
                f"ViolationID {violation_id} appears multiple times: "
                f"{', '.join(id_locations[violation_id])}"
            )
        if len(duplicate_ids) > 20:
            errors.append(f"{len(duplicate_ids) - 20} more ViolationID values appear multiple times")

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
