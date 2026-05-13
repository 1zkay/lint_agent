#!/usr/bin/env python3
"""Prepare lint rows and source files for root-cause CSV analysis."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


HDL_SUFFIXES = {".v", ".vh", ".sv", ".svh"}
SOURCE_LOC_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?[^,\t\r\n]*?\.(?:svh|sv|vh|v))\((?P<line>\d+)\)",
    re.IGNORECASE,
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    with tarfile.open(archive) as tf:
        kwargs: dict[str, Any] = {}
        if sys.version_info >= (3, 12):
            kwargs["filter"] = "data"
        tf.extractall(destination, **kwargs)


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = (destination / info.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"Unsafe archive member path: {info.filename}")
        zf.extractall(destination)


def extract_sources(archive: Path | None, source_dir: Path | None, work_dir: Path) -> Path:
    source_root = work_dir / "sources"
    if source_root.exists():
        shutil.rmtree(source_root)
    source_root.mkdir(parents=True, exist_ok=True)

    if archive is not None:
        archive = archive.resolve()
        lower_name = archive.name.lower()
        if lower_name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
            _safe_extract_tar(archive, source_root)
        elif lower_name.endswith(".zip"):
            _safe_extract_zip(archive, source_root)
        elif lower_name.endswith((".7z", ".rar", ".cab")):
            subprocess.run(
                ["7z", "x", f"-o{source_root}", str(archive)],
                check=True,
                text=True,
            )
        else:
            shutil.unpack_archive(str(archive), str(source_root))
        return source_root

    if source_dir is None:
        raise ValueError("Either --source-archive or --source-dir is required")

    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise ValueError(f"Source directory not found: {source_dir}")
    for path in source_dir.rglob("*"):
        if path.is_file():
            rel = path.relative_to(source_dir)
            target = source_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return source_root


def _split_report_line(line: str) -> list[str]:
    try:
        return next(csv.reader([line]))
    except csv.Error:
        return line.rstrip("\n").split(",", 4)


def _source_location_from_text(text: str) -> tuple[str, int | None]:
    matches = list(SOURCE_LOC_RE.finditer(text or ""))
    if not matches:
        return "", None
    match = matches[-1]
    return match.group("path"), int(match.group("line"))


def parse_lint_report(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    rows: list[dict[str, Any]] = []
    counters: defaultdict[str, int] = defaultdict(int)

    for physical_line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        parts = _split_report_line(raw_line)
        if parts and parts[0].strip().lower() == "stage":
            continue
        if len(parts) < 4:
            continue
        malformed_columns = len(parts) != 6

        stage = parts[0].strip()
        message_id = parts[1].strip()
        severity = parts[2].strip()
        contents = parts[3].strip()
        line_no = parts[4].strip() if len(parts) > 4 else ""

        source_path, source_line = _source_location_from_text(line_no)
        recovered_location = False
        if not source_path:
            source_path, source_line = _source_location_from_text(contents)
            if source_path:
                recovered_location = True
                contents = SOURCE_LOC_RE.sub("", contents).strip(" \t,")
        if not source_path:
            source_path, source_line = _source_location_from_text(raw_line)
            recovered_location = bool(source_path)

        if not message_id:
            message_id = "UnknownMessage"
        counters[message_id] += 1
        violation_id = f"{message_id}_{counters[message_id]}"

        rows.append(
            {
                "row_number": len(rows) + 1,
                "report_line_number": physical_line_number,
                "ViolationID": violation_id,
                "Stage": stage,
                "MessageID": message_id,
                "Severity": severity,
                "Contents": contents,
                "source_path": source_path,
                "source_file": Path(source_path).name if source_path else "",
                "source_line": source_line,
                "malformed_columns": malformed_columns,
                "recovered_location": recovered_location,
                "raw_report_line": raw_line,
            }
        )

    return rows


def write_lint_items_csv(rows: list[dict[str, Any]], path: Path) -> None:
    fieldnames = [
        "row_number",
        "report_line_number",
        "ViolationID",
        "Stage",
        "MessageID",
        "Severity",
        "source_file",
        "source_line",
        "Contents",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def build_source_index(source_root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in HDL_SUFFIXES:
            continue
        items.append(
            {
                "file": path.name,
                "relative_path": path.relative_to(source_root).as_posix(),
                "path": str(path),
                "line_count": sum(1 for _ in path.open(encoding="utf-8", errors="ignore")),
            }
        )
    return items


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lint-report", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-archive")
    group.add_argument("--source-dir")
    parser.add_argument("--work-dir")
    args = parser.parse_args()

    project_root = _project_root()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = Path(args.work_dir) if args.work_dir else project_root / "reports" / f"root_cause_inputs_{timestamp}"
    if not work_dir.is_absolute():
        work_dir = project_root / work_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    lint_report = Path(args.lint_report).resolve()
    if not lint_report.is_file():
        raise FileNotFoundError(lint_report)

    source_root = extract_sources(
        Path(args.source_archive) if args.source_archive else None,
        Path(args.source_dir) if args.source_dir else None,
        work_dir,
    )
    rows = parse_lint_report(lint_report)
    source_index = build_source_index(source_root)

    lint_items_json = work_dir / "lint_items.json"
    lint_items_csv = work_dir / "lint_items.csv"
    source_index_json = work_dir / "source_index.json"

    lint_items_json.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_lint_items_csv(rows, lint_items_csv)
    source_index_json.write_text(
        json.dumps(source_index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"WORK_DIR={work_dir}")
    print(f"SOURCE_ROOT={source_root}")
    print(f"SOURCE_INDEX_JSON={source_index_json}")
    print(f"LINT_ITEMS_JSON={lint_items_json}")
    print(f"LINT_ITEMS_CSV={lint_items_csv}")
    print(f"LINT_ROW_COUNT={len(rows)}")
    print(f"MALFORMED_ROW_COUNT={sum(1 for row in rows if row['malformed_columns'])}")
    print(f"RECOVERED_LOCATION_COUNT={sum(1 for row in rows if row['recovered_location'])}")
    print(f"MISSING_LOCATION_COUNT={sum(1 for row in rows if not row['source_file'] or row['source_line'] is None)}")
    print(f"SOURCE_FILE_COUNT={len(source_index)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
