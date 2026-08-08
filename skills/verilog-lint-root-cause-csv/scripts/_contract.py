"""Shared internal contracts for hierarchy slices and root-cause CSV files."""

from __future__ import annotations

import csv
import re
import sys
from typing import Any


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
HDL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
FALSE_POSITIVE_ROOT_ID = "误报"
HIERARCHY_STATUS_SCHEMA_VERSION = 2
FILELIST_RECOVERY_EXIT_CODE = 2
HIERARCHY_ATTEMPTS = (
    ("read_verilog", "complete"),
    ("read_verilog", "partial"),
    ("read_slang", "complete"),
    ("read_slang", "partial"),
)


def hierarchy_module_status(reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("module hierarchy status requires a reason")
    return {
        "schema_version": HIERARCHY_STATUS_SCHEMA_VERSION,
        "mode": "module",
        "reason": reason,
    }


def hierarchy_tree_status(
    *,
    completeness: str,
    frontend: str,
    top: str,
    unresolved_modules: dict[str, int],
) -> dict[str, Any]:
    status = {
        "schema_version": HIERARCHY_STATUS_SCHEMA_VERSION,
        "mode": "hierarchy",
        "completeness": completeness,
        "frontend": frontend,
        "top": top,
        "unresolved_modules": [
            {"module": module, "instances": count}
            for module, count in sorted(unresolved_modules.items())
        ],
    }
    return validate_hierarchy_status(status)


def validate_hierarchy_status(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("hierarchy status must be an object")
    mode = data.get("mode")
    if (
        data.get("schema_version") != HIERARCHY_STATUS_SCHEMA_VERSION
        or mode not in {"hierarchy", "module"}
    ):
        raise ValueError("invalid hierarchy status version or mode")
    if mode == "module":
        if set(data) != {"schema_version", "mode", "reason"} or not str(
            data.get("reason", "")
        ).strip():
            raise ValueError("invalid module hierarchy status")
        return data

    unresolved = data.get("unresolved_modules")
    if (
        set(data)
        != {
            "schema_version",
            "mode",
            "completeness",
            "frontend",
            "top",
            "unresolved_modules",
        }
        or (data.get("frontend"), data.get("completeness"))
        not in HIERARCHY_ATTEMPTS
        or not str(data.get("top", "")).strip()
        or not isinstance(unresolved, list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"module", "instances"}
            or not str(item.get("module", "")).strip()
            or not isinstance(item.get("instances"), int)
            or item["instances"] < 1
            for item in unresolved
        )
        or (data.get("completeness") == "complete" and unresolved)
    ):
        raise ValueError("invalid hierarchy status")
    return data


def format_root_id(number: int) -> str:
    return f"root_{number:03d}"


def format_violation_id(number: int) -> str:
    return f"vio_{number:03d}"
