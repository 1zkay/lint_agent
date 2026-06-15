#!/usr/bin/env python3
"""Sort a root-cause CSV by root_id and then by leaf violation number."""

from __future__ import annotations

import argparse
import csv
import re
import tempfile
from pathlib import Path


ROOT_ID_RE = re.compile(r"^root_(\d+)$")
LEAF_ID_RE = re.compile(r"^vio_(\d+)$")
FALSE_POSITIVE_ROOT_ID = "误报"


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
    match = LEAF_ID_RE.match(text)
    if match:
        return (int(match.group(1)), text)
    return (10**12, text)


def sort_csv(input_csv: Path, output_csv: Path) -> None:
    with input_csv.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV header is empty")
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

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
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for _, row in sorted_rows:
            writer.writerow(row)


def sort_csv_in_place(input_csv: Path) -> None:
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

    print(f"OK: sorted root-cause CSV: {Path(args.output) if args.output else input_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
