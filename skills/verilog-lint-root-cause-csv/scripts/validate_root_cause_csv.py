#!/usr/bin/env python3
"""Validate the root-cause CSV schema and leaf violation references."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath, PureWindowsPath

from _contract import (
    FALSE_POSITIVE_ROOT_ID,
    IN_HIERARCHY_STATUS,
    INSTANCE_WORK_UNIT_KIND,
    ISOLATED_SCOPE,
    MAPPED_LINT_COLUMNS,
    MODULE_WORK_UNIT_KIND,
    MODULE_SCOPE_STATUS,
    ROOT_CAUSE_COLUMNS,
    ROOT_ID_RE,
    STANDALONE_STATUS,
    VIOLATION_ID_RE,
    format_root_id,
)
from _filelist import HEADER_SUFFIXES, SOURCE_SUFFIXES
from _work_units import parse_work_unit_id, read_manifest

INTEGER_RE = re.compile(r"^\d+$")
WHITESPACE_RE = re.compile(r"\s")
CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _source_line_counts(rtl_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in rtl_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in {
            *SOURCE_SUFFIXES,
            *HEADER_SUFFIXES,
        }:
            with path.open(encoding="utf-8", errors="replace") as handle:
                counts[path.relative_to(rtl_dir).as_posix()] = sum(1 for _ in handle)
    if not counts:
        raise ValueError(f"RTL source directory is empty or missing: {rtl_dir}")
    return counts


def _read_unit_lint(
    unit_id: str,
    unit_dir: Path,
    hierarchy_available: bool,
) -> dict[str, tuple[str, str]]:
    scope, kind = parse_work_unit_id(unit_id)
    tree_path = unit_dir / "hierarchy_tree.txt"
    if kind == INSTANCE_WORK_UNIT_KIND and not tree_path.is_file():
        raise ValueError(f"instance work unit has no hierarchy tree: {unit_dir}")
    if kind == MODULE_WORK_UNIT_KIND and tree_path.exists():
        raise ValueError(f"module work unit unexpectedly has a hierarchy tree: {unit_dir}")

    result: dict[str, tuple[str, str]] = {}
    lint_csv = unit_dir / "lint.csv"
    with lint_csv.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != MAPPED_LINT_COLUMNS:
            raise ValueError(f"{lint_csv}: unexpected lint CSV header")
        for csv_line, row in enumerate(reader, start=2):
            status = str(row.get("status", ""))
            hierarchy = str(row.get("hierarchy", "")).strip()
            if hierarchy_available:
                expected_status = (
                    STANDALONE_STATUS
                    if scope == ISOLATED_SCOPE
                    else IN_HIERARCHY_STATUS
                )
                if status != expected_status:
                    raise ValueError(
                        f"{lint_csv}:{csv_line}: invalid hierarchy status"
                    )
            elif (
                scope == ISOLATED_SCOPE
                or kind != MODULE_WORK_UNIT_KIND
                or status != MODULE_SCOPE_STATUS
            ):
                raise ValueError(
                    f"{lint_csv}:{csv_line}: invalid module-only mapping"
                )
            if (kind == MODULE_WORK_UNIT_KIND and hierarchy) or (
                kind == INSTANCE_WORK_UNIT_KIND and not hierarchy
            ):
                raise ValueError(
                    f"{lint_csv}:{csv_line}: hierarchy field conflicts with work-unit kind"
                )
            violation_id = str(row.get("vio_id", "")).strip()
            if not VIOLATION_ID_RE.match(violation_id):
                raise ValueError(f"{lint_csv}:{csv_line}: invalid vio_id")
            if violation_id in result:
                raise ValueError(f"{lint_csv}: duplicate vio_id {violation_id}")
            result[violation_id] = (
                str(row.get("message_id", "")),
                str(row.get("contents", "")),
            )
    if not result:
        raise ValueError(f"{lint_csv}: work unit contains no lint rows")
    return result


def _load_slice_lint(
    slices_dir: Path,
) -> tuple[dict[str, tuple[str, str]], dict[str, int]]:
    manifest = read_manifest(slices_dir)
    lint_rows_by_id: dict[str, tuple[str, str]] = {}
    for unit_id, unit_dir in manifest.work_units:
        for violation_id, lint_entry in _read_unit_lint(
            unit_id, unit_dir, manifest.hierarchy_available
        ).items():
            if violation_id in lint_rows_by_id:
                raise ValueError(f"duplicate vio_id across slices: {violation_id}")
            lint_rows_by_id[violation_id] = lint_entry
    return lint_rows_by_id, _source_line_counts(slices_dir.parent / "rtl")


def _load_work_unit_lint(
    unit_dir: Path,
) -> tuple[dict[str, tuple[str, str]], dict[str, int]]:
    slices_dir = unit_dir.parents[2]
    manifest = read_manifest(slices_dir)
    unit_id = unit_dir.relative_to(slices_dir).as_posix()
    return (
        _read_unit_lint(unit_id, unit_dir, manifest.hierarchy_available),
        _source_line_counts(unit_dir / "rtl"),
    )


def _positive_int(value: str) -> int | None:
    text = str(value or "").strip()
    if not INTEGER_RE.match(text):
        return None
    number = int(text)
    return number if number > 0 else None


def _is_normalized_relative_path(value: str) -> bool:
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    return (
        bool(value)
        and value == posix_path.as_posix()
        and "\\" not in value
        and not posix_path.is_absolute()
        and not windows_path.is_absolute()
        and not windows_path.drive
        and ".." not in posix_path.parts
        and "." not in posix_path.parts
    )


def _parent_cycle_errors(
    root_definitions: dict[str, tuple[str, str, str]],
) -> list[str]:
    parents = {
        root_id: definition[-1]
        for root_id, definition in root_definitions.items()
        if definition[-1] != "/"
    }
    states: dict[str, int] = {}
    stack: list[str] = []
    stack_positions: dict[str, int] = {}
    errors: list[str] = []

    def visit(root_id: str) -> None:
        state = states.get(root_id, 0)
        if state == 2:
            return
        if state == 1:
            cycle = stack[stack_positions[root_id] :] + [root_id]
            errors.append(f"parent_root_id cycle: {' -> '.join(cycle)}")
            return

        states[root_id] = 1
        stack_positions[root_id] = len(stack)
        stack.append(root_id)
        parent = parents.get(root_id)
        if parent in root_definitions:
            visit(parent)
        stack.pop()
        stack_positions.pop(root_id)
        states[root_id] = 2

    for root_id in root_definitions:
        visit(root_id)
    return errors


def validate(
    output_csv: Path,
    *,
    slices_dir: Path | None = None,
    work_unit_dir: Path | None = None,
) -> list[str]:
    if (slices_dir is None) == (work_unit_dir is None):
        raise ValueError("select exactly one evidence scope")
    if slices_dir is not None:
        lint_rows_by_id, source_line_counts = _load_slice_lint(slices_dir)
    else:
        lint_rows_by_id, source_line_counts = _load_work_unit_lint(work_unit_dir)
    valid_ids = set(lint_rows_by_id)
    seen_leaf_ids: Counter[str] = Counter()
    id_locations: defaultdict[str, list[str]] = defaultdict(list)
    root_definitions: dict[str, tuple[str, str, str]] = {}
    parent_references: defaultdict[str, list[str]] = defaultdict(list)
    errors: list[str] = []

    with output_csv.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ROOT_CAUSE_COLUMNS:
            errors.append(f"Header must be exactly {ROOT_CAUSE_COLUMNS}, got {reader.fieldnames}")
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
        leaf_note = str(row.get("leaf_violation_note", ""))
        leaf_base_id = ""

        is_false_positive = root_id == FALSE_POSITIVE_ROOT_ID

        if not root_id:
            errors.append(f"Line {index}: root_id is empty")
        elif not is_false_positive and not ROOT_ID_RE.match(root_id):
            errors.append(
                f"Line {index}: root_id must match root_<three-or-more digits> "
                f"or be {FALSE_POSITIVE_ROOT_ID}, got {root_id}"
            )

        if not root_note:
            errors.append(f"Line {index}: root_note is empty")
        elif not CJK_RE.search(root_note):
            errors.append(f"Line {index}: root_note must contain Chinese text")
        if not fix_suggestion:
            errors.append(f"Line {index}: fix_suggestion is empty")
        elif not is_false_positive and not CJK_RE.search(fix_suggestion):
            errors.append(f"Line {index}: fix_suggestion must contain Chinese text")

        if not root_file:
            errors.append(f"Line {index}: root_file_path is empty")
        elif not _is_normalized_relative_path(root_file):
            errors.append(
                f"Line {index}: root_file_path must be a normalized path relative to rtl/, "
                f"got {root_file}"
            )
        elif root_file not in source_line_counts:
            errors.append(f"Line {index}: root_file_path is not in rtl/: {root_file}")

        start = _positive_int(start_value)
        end = _positive_int(end_value)
        if start is None:
            errors.append(f"Line {index}: root_file_start must be a positive integer")
        if end is None:
            errors.append(f"Line {index}: root_file_end must be a positive integer")
        if start is not None and end is not None and start > end:
            errors.append(f"Line {index}: root_file_start cannot be greater than root_file_end")
        if root_file in source_line_counts:
            max_line_count = source_line_counts[root_file]
            if start is not None and start > max_line_count:
                errors.append(
                    f"Line {index}: root_file_start exceeds {root_file} line count {max_line_count}"
                )
            if end is not None and end > max_line_count:
                errors.append(
                    f"Line {index}: root_file_end exceeds {root_file} line count {max_line_count}"
                )

        if not parent_root_id:
            errors.append(f"Line {index}: parent_root_id is empty; use / for a top-level root")
        elif parent_root_id != "/":
            if not ROOT_ID_RE.match(parent_root_id):
                errors.append(
                    f"Line {index}: parent_root_id must be / or "
                    f"root_<three-or-more digits>, got {parent_root_id}"
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
        elif not VIOLATION_ID_RE.match(leaf_id):
            errors.append(
                f"Line {index}: leaf_violation_id must match "
                f"vio_<three-or-more digits>, got {leaf_id}"
            )
        else:
            leaf_base_id = leaf_id
            if valid_ids and leaf_base_id not in valid_ids:
                errors.append(f"Line {index}: unknown leaf_violation_id {leaf_base_id}")
            elif lint_rows_by_id:
                expected_message_id, expected_contents = lint_rows_by_id[leaf_base_id]
                expected_leaf_note = f"{expected_message_id}:{expected_contents}"
                if leaf_note != expected_leaf_note:
                    errors.append(
                        f"Line {index}: leaf_violation_note must exactly match "
                        "message_id:contents from slice lint"
                    )
        if not leaf_note.strip():
            errors.append(f"Line {index}: leaf_violation_note is empty")

        if is_false_positive:
            if root_note == "/":
                errors.append(
                    f"Line {index}: false-positive root_note must state "
                    "the false-positive reason"
                )
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
                parent_root_id,
            )
            previous_definition = root_definitions.setdefault(root_id, root_definition)
            if previous_definition != root_definition:
                errors.append(
                    f"Line {index}: root_id {root_id} has inconsistent "
                    "root_note, fix_suggestion, or parent_root_id"
                )

    duplicate_ids = [leaf_id for leaf_id, count in seen_leaf_ids.items() if count > 1]
    for leaf_id in duplicate_ids[:20]:
        errors.append(
            f"leaf_violation_id {leaf_id} appears multiple times: "
            f"{', '.join(id_locations[leaf_id])}"
        )
    if len(duplicate_ids) > 20:
        errors.append(
            f"{len(duplicate_ids) - 20} more leaf_violation_id values "
            "appear multiple times"
        )

    if valid_ids:
        missing_ids = sorted(valid_ids - set(seen_leaf_ids))
        for leaf_id in missing_ids[:20]:
            errors.append(f"missing leaf_violation_id {leaf_id}")
        if len(missing_ids) > 20:
            errors.append(f"{len(missing_ids) - 20} more input violation IDs are missing")

    known_root_ids = set(root_definitions)
    normal_root_ids = {
        root_id for root_id in known_root_ids if ROOT_ID_RE.match(root_id)
    }
    expected_root_ids = {
        format_root_id(index) for index in range(1, len(normal_root_ids) + 1)
    }
    if normal_root_ids != expected_root_ids:
        actual = sorted(
            normal_root_ids,
            key=lambda root_id: int(ROOT_ID_RE.match(root_id).group(1)),
        )
        errors.append(
            "normal root_id values must be contiguous from root_001; got "
            f"{', '.join(actual[:20])}"
            f"{' ...' if len(actual) > 20 else ''}"
        )
    for parent_root_id, locations in sorted(parent_references.items()):
        if parent_root_id not in known_root_ids:
            errors.append(f"unknown parent_root_id {parent_root_id}: {', '.join(locations[:20])}")
    errors.extend(_parent_cycle_errors(root_definitions))

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_csv")
    evidence = parser.add_mutually_exclusive_group(required=True)
    evidence.add_argument("--slices-dir", type=Path)
    evidence.add_argument("--work-unit-dir", type=Path)
    args = parser.parse_args()

    try:
        errors = validate(
            Path(args.output_csv),
            slices_dir=args.slices_dir,
            work_unit_dir=args.work_unit_dir,
        )
    except Exception as exc:
        print(f"ERROR: validator failed: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print("OK: root-cause leaf CSV is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
