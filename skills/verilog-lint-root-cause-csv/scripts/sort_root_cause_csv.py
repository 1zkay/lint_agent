#!/usr/bin/env python3
"""Normalize root IDs and sort a root-cause CSV."""

from __future__ import annotations

import argparse
import csv
import stat
import tempfile
from pathlib import Path

from _contract import (
    FALSE_POSITIVE_ROOT_ID,
    ROOT_CAUSE_COLUMNS,
    ROOT_ID_RE,
    VIOLATION_ID_RE,
    format_root_id,
)


def _root_sort_key(root_id: str) -> tuple[int, int, str]:
    text = str(root_id or "").strip()
    match = ROOT_ID_RE.match(text)
    if match:
        return (0, int(match.group(1)), text)
    if text == FALSE_POSITIVE_ROOT_ID:
        return (1, 0, text)
    return (2, 0, text)


def _leaf_sort_key(leaf_id: str) -> tuple[int, str]:
    text = str(leaf_id or "").strip()
    match = VIOLATION_ID_RE.match(text)
    if match:
        return (int(match.group(1)), text)
    return (10**12, text)


def sort_csv(input_csv: Path, output_csv: Path) -> None:
    with input_csv.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ROOT_CAUSE_COLUMNS:
            raise ValueError(
                f"CSV header must be exactly {ROOT_CAUSE_COLUMNS}, got {reader.fieldnames}"
            )
        rows = list(reader)

    normal_root_ids = sorted(
        {
            str(row.get("root_id", "")).strip()
            for row in rows
            if ROOT_ID_RE.match(str(row.get("root_id", "")).strip())
        },
        key=_root_sort_key,
    )
    root_id_mapping = {
        root_id: format_root_id(index)
        for index, root_id in enumerate(normal_root_ids, start=1)
    }
    for row in rows:
        root_id = str(row.get("root_id", "")).strip()
        parent_root_id = str(row.get("parent_root_id", "")).strip()
        if root_id in root_id_mapping:
            row["root_id"] = root_id_mapping[root_id]
        if parent_root_id in root_id_mapping:
            row["parent_root_id"] = root_id_mapping[parent_root_id]

    sorted_rows = sorted(
        enumerate(rows),
        key=lambda item: (
            _root_sort_key(item[1].get("root_id", "")),
            _leaf_sort_key(item[1].get("leaf_violation_id", "")),
            item[0],
        ),
    )

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ROOT_CAUSE_COLUMNS)
        writer.writeheader()
        for _, row in sorted_rows:
            writer.writerow(row)


def sort_csv_in_place(input_csv: Path) -> None:
    input_mode = stat.S_IMODE(input_csv.stat().st_mode)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=input_csv.parent,
        prefix=f".{input_csv.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        sort_csv(input_csv, tmp_path)
        tmp_path.chmod(input_mode)
        tmp_path.replace(input_csv)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_csv")
    parser.add_argument("--output")
    args = parser.parse_args()

    input_csv = Path(args.input_csv)
    if not input_csv.is_file():
        raise FileNotFoundError(input_csv)

    if args.output:
        sort_csv(input_csv, Path(args.output))
    else:
        sort_csv_in_place(input_csv)

    print(
        "OK: normalized and sorted root-cause CSV: "
        f"{Path(args.output) if args.output else input_csv}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
