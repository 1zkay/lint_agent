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

LOCAL_ROOT_CATALOG_COLUMNS = [
    "local_item_id",
    "unit_id",
    "local_root_id",
    "root_note",
    "fix_suggestion",
    "parent_local_item_id",
    "leaf_count",
]

GLOBAL_ROOT_MAP_COLUMNS = [
    "local_item_id",
    "global_root_id",
    "root_note",
    "fix_suggestion",
    "parent_global_root_id",
]

LEVEL_SCOPES = ("level1", "level2", "level3", "level4")
ISOLATED_SCOPE = "isolated"
WORK_UNIT_SCOPES = (*LEVEL_SCOPES, ISOLATED_SCOPE)
MODULE_WORK_UNIT_KIND = "module"
INSTANCE_WORK_UNIT_KIND = "instance"
WORK_UNIT_KINDS = (MODULE_WORK_UNIT_KIND, INSTANCE_WORK_UNIT_KIND)
WORK_UNIT_ID_DIGEST_LENGTH = 12
SLICE_SCHEMA_VERSION = 3

IN_HIERARCHY_STATUS = "in_hierarchy_tree"
STANDALONE_STATUS = "standalone_module_not_in_tree"
MODULE_SCOPE_STATUS = "module_scope"

ROOT_ID_RE = re.compile(r"^root_(\d{3,})$")
VIOLATION_ID_RE = re.compile(r"^vio_(\d{3,})$")
FALSE_POSITIVE_ROOT_ID = "误报"


def format_root_id(number: int) -> str:
    return f"root_{number:03d}"


def format_violation_id(number: int) -> str:
    return f"vio_{number:03d}"
