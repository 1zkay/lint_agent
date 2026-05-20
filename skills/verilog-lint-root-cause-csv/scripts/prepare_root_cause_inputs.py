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
from datetime import datetime
from pathlib import Path
from typing import Any


HDL_SUFFIXES = {".v", ".vh", ".sv", ".svh"}
NORMALIZED_REPORT_HEADER = [
    "violation_id",
    "severity",
    "message_id",
    "description",
    "file_path",
    "line_number",
]
LEGACY_REPORT_HEADER = [
    "stage",
    "messageid",
    "severity",
    "contents",
    "lineno",
    "",
]
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
    except csv.Error as exc:
        raise ValueError(f"Cannot parse legacy lint report row: {exc}") from exc


def _source_location_from_text(text: str) -> tuple[str, int | None]:
    matches = list(SOURCE_LOC_RE.finditer(text or ""))
    if not matches:
        return "", None
    match = matches[-1]
    return match.group("path"), int(match.group("line"))


def _remove_source_location(text: str) -> str:
    matches = list(SOURCE_LOC_RE.finditer(text or ""))
    if not matches:
        return text.strip(" \t,")
    match = matches[-1]
    return (text[: match.start()] + text[match.end() :]).strip(" \t,")


def _normalise_header(value: str | None) -> str:
    return str(value or "").strip().lstrip("\ufeff").lower()


def _parse_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        return None
    return number if number > 0 else None


def _read_report_header(lines: list[str]) -> list[str]:
    if not lines:
        raise ValueError("Lint report is empty")
    try:
        header = next(csv.reader([lines[0]]))
    except csv.Error as exc:
        raise ValueError(f"Cannot parse lint report header: {exc}") from exc
    return [_normalise_header(item) for item in header]


def _normalise_source_path(value: str) -> tuple[str, str]:
    source_path = str(value or "").strip()
    return source_path, Path(source_path).name if source_path else ""


def _build_row(
    *,
    row_number: int,
    report_line_number: int,
    violation_id: str,
    severity: str,
    message_id: str,
    description: str,
    source_path: str,
    source_line: int | None,
    raw_report_line: str,
) -> dict[str, Any]:
    source_path, source_file = _normalise_source_path(source_path)
    return {
        "row_number": row_number,
        "report_line_number": report_line_number,
        "violation_id": violation_id,
        "severity": severity,
        "message_id": message_id,
        "description": description,
        "file_path": source_file,
        "line_number": source_line,
        "source_path": source_path,
        "raw_report_line": raw_report_line,
    }


def _parse_normalized_lint_report(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return rows
        if [_normalise_header(field) for field in reader.fieldnames] != NORMALIZED_REPORT_HEADER:
            raise ValueError(
                "Normalized lint report header must be exactly "
                f"{','.join(NORMALIZED_REPORT_HEADER)}"
            )
        field_by_name = {_normalise_header(field): field for field in reader.fieldnames}

        for csv_line_number, row in enumerate(reader, start=2):
            if not row or not any(str(value or "").strip() for value in row.values() if value is not None):
                continue
            if None in row:
                raise ValueError(f"Line {csv_line_number}: row has extra CSV columns")
            if any(row.get(field_by_name[column]) is None for column in NORMALIZED_REPORT_HEADER):
                raise ValueError(f"Line {csv_line_number}: row has missing CSV columns")

            violation_id = str(row.get(field_by_name["violation_id"], "")).strip()
            severity = str(row.get(field_by_name["severity"], "")).strip()
            message_id = str(row.get(field_by_name["message_id"], "")).strip()
            description = str(row.get(field_by_name["description"], "")).strip()
            file_path = str(row.get(field_by_name["file_path"], "")).strip()
            line_number = _parse_int(row.get(field_by_name["line_number"]))
            source_path = file_path
            source_line = line_number
            if not violation_id:
                raise ValueError(f"Line {csv_line_number}: violation_id is empty")
            if not severity:
                raise ValueError(f"Line {csv_line_number}: severity is empty")
            if not message_id:
                raise ValueError(f"Line {csv_line_number}: message_id is empty")
            if not description:
                raise ValueError(f"Line {csv_line_number}: description is empty")
            if not source_path:
                raise ValueError(f"Line {csv_line_number}: file_path is empty")
            if source_line is None:
                raise ValueError(f"Line {csv_line_number}: line_number must be a positive integer")

            rows.append(
                _build_row(
                    row_number=len(rows) + 1,
                    report_line_number=csv_line_number,
                    violation_id=violation_id,
                    severity=severity,
                    message_id=message_id,
                    description=description,
                    source_path=source_path,
                    source_line=source_line,
                    raw_report_line="",
                )
            )

    return rows


def _parse_legacy_lint_report(lines: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for physical_line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        parts = _split_report_line(raw_line)
        if parts and parts[0].strip().lower() == "stage":
            continue
        legacy_line_no_in_contents = False
        if len(parts) == len(LEGACY_REPORT_HEADER) - 1 and not parts[4].strip():
            legacy_line_no_in_contents = bool(_source_location_from_text(parts[3])[0])
        if len(parts) != len(LEGACY_REPORT_HEADER) and not legacy_line_no_in_contents:
            raise ValueError(f"Line {physical_line_number}: legacy lint report row must have 6 columns")
        if len(parts) == len(LEGACY_REPORT_HEADER) and parts[5].strip():
            raise ValueError(f"Line {physical_line_number}: legacy lint report trailing column must be empty")

        message_id = parts[1].strip()
        severity = parts[2].strip()
        contents = parts[3].strip()
        line_no = parts[4].strip() if len(parts) == len(LEGACY_REPORT_HEADER) else ""
        if not message_id:
            raise ValueError(f"Line {physical_line_number}: MessageID is empty")
        if not severity:
            raise ValueError(f"Line {physical_line_number}: Severity is empty")
        if not contents:
            raise ValueError(f"Line {physical_line_number}: Contents is empty")
        if not line_no and not legacy_line_no_in_contents:
            raise ValueError(f"Line {physical_line_number}: LineNo is empty")

        if legacy_line_no_in_contents:
            source_path, source_line = _source_location_from_text(contents)
            contents = _remove_source_location(contents)
        else:
            source_path, source_line = _source_location_from_text(line_no)
        if not source_path or source_line is None:
            raise ValueError(f"Line {physical_line_number}: LineNo must contain a source location")
        if source_line <= 0:
            raise ValueError(f"Line {physical_line_number}: LineNo must be a positive integer")
        row_number = len(rows) + 1

        rows.append(
            _build_row(
                row_number=row_number,
                report_line_number=physical_line_number,
                violation_id=str(row_number),
                severity=severity,
                message_id=message_id,
                description=contents,
                source_path=source_path,
                source_line=source_line,
                raw_report_line=raw_line,
            )
        )

    return rows


def parse_lint_report(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    header = _read_report_header(lines)
    if header == NORMALIZED_REPORT_HEADER:
        return _parse_normalized_lint_report(path)
    if header == LEGACY_REPORT_HEADER:
        return _parse_legacy_lint_report(lines)
    raise ValueError(
        "Unsupported lint report header. Expected exactly "
        f"`{','.join(NORMALIZED_REPORT_HEADER)}` or `Stage,MessageID,Severity,Contents,LineNo,`."
    )


def write_lint_items_csv(rows: list[dict[str, Any]], path: Path) -> None:
    fieldnames = [
        "row_number",
        "report_line_number",
        "violation_id",
        "severity",
        "message_id",
        "description",
        "file_path",
        "line_number",
        "source_path",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def write_normalized_lint_report_csv(rows: list[dict[str, Any]], path: Path) -> None:
    fieldnames = [
        "violation_id",
        "severity",
        "message_id",
        "description",
        "file_path",
        "line_number",
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

    try:
        lint_report = Path(args.lint_report).resolve()
        if not lint_report.is_file():
            raise FileNotFoundError(lint_report)

        source_root = extract_sources(
            Path(args.source_archive) if args.source_archive else None,
            Path(args.source_dir) if args.source_dir else None,
            work_dir,
        )
        rows = parse_lint_report(lint_report)
        if not rows:
            raise ValueError("Lint report contains no data rows")
        source_index = build_source_index(source_root)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    lint_items_json = work_dir / "lint_items.json"
    lint_items_csv = work_dir / "lint_items.csv"
    normalized_lint_report_csv = work_dir / "normalized_lint_report.csv"
    source_index_json = work_dir / "source_index.json"

    lint_items_json.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_lint_items_csv(rows, lint_items_csv)
    write_normalized_lint_report_csv(rows, normalized_lint_report_csv)
    source_index_json.write_text(
        json.dumps(source_index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"WORK_DIR={work_dir}")
    print(f"SOURCE_ROOT={source_root}")
    print(f"SOURCE_INDEX_JSON={source_index_json}")
    print(f"NORMALIZED_LINT_REPORT_CSV={normalized_lint_report_csv}")
    print(f"LINT_ITEMS_JSON={lint_items_json}")
    print(f"LINT_ITEMS_CSV={lint_items_csv}")
    print(f"LINT_ROW_COUNT={len(rows)}")
    print(f"SOURCE_FILE_COUNT={len(source_index)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
