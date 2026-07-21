"""Shared internal contracts for hierarchy slices and root-cause CSV files."""

from __future__ import annotations

import csv
import re
import sys


def _allow_maximum_csv_field_size() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


_allow_maximum_csv_field_size()


MAPPED_LINT_COLUMNS = [
    "vio_id",
    "status",
    "hierarchy",
    "source_module",
    "source_file",
    "source_line",
    "message_id",
    "severity",
    "contents",
]

ROOT_CAUSE_COLUMNS = [
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

LEVEL_SCOPES = ("level1", "level2", "level3", "level4")
SLICE_SCOPES = (*LEVEL_SCOPES, "isolated")

ROOT_ID_RE = re.compile(r"^root_(\d{3,})$")
VIOLATION_ID_RE = re.compile(r"^vio_(\d{3,})$")
FALSE_POSITIVE_ROOT_ID = "误报"


def format_violation_id(number: int) -> str:
    return f"vio_{number:03d}"
