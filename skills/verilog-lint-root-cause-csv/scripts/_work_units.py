"""Shared helpers for versioned lint work-unit artifacts."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from _contract import (
    FALSE_POSITIVE_ROOT_ID,
    ISOLATED_SCOPE,
    MAPPED_LINT_COLUMNS,
    MODULE_WORK_UNIT_KIND,
    SLICE_SCHEMA_VERSION,
    WORK_UNIT_ID_DIGEST_LENGTH,
    WORK_UNIT_KINDS,
    WORK_UNIT_SCOPES,
)


@dataclass(frozen=True)
class WorkUnitManifest:
    hierarchy_available: bool
    work_units: tuple[tuple[str, Path], ...]


def parse_work_unit_id(value: str) -> tuple[str, str]:
    parts = PurePosixPath(value).parts
    if len(parts) != 3:
        raise ValueError(f"invalid work-unit path: {value}")
    scope, kind, name = parts
    if (
        scope not in WORK_UNIT_SCOPES
        or kind not in WORK_UNIT_KINDS
        or not re.fullmatch(
            rf"{re.escape(kind)}_[0-9a-f]{{{WORK_UNIT_ID_DIGEST_LENGTH}}}",
            name,
        )
        or (scope == ISOLATED_SCOPE and kind != MODULE_WORK_UNIT_KIND)
    ):
        raise ValueError(f"invalid work-unit path: {value}")
    return scope, kind


def read_manifest(slices_dir: Path) -> WorkUnitManifest:
    root = slices_dir.resolve()
    manifest_path = root / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or set(data) != {
            "schema_version",
            "hierarchy_available",
            "work_units",
        }
        or data.get("schema_version") != SLICE_SCHEMA_VERSION
        or not isinstance(data.get("hierarchy_available"), bool)
        or not isinstance(data.get("work_units"), list)
        or not data["work_units"]
    ):
        raise ValueError(f"{manifest_path}: invalid work-unit manifest")

    result: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for value in data["work_units"]:
        if not isinstance(value, str) or not value or value in seen:
            raise ValueError(f"{manifest_path}: invalid or duplicate work-unit path")
        parse_work_unit_id(value)
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"{manifest_path}: unsafe work-unit path {value!r}")
        unit_dir = (root / relative).resolve()
        try:
            unit_dir.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"{manifest_path}: work-unit path escapes slices/: {value}"
            ) from exc
        if not unit_dir.is_dir():
            raise FileNotFoundError(f"work-unit directory not found: {unit_dir}")
        for name in ("rtl", "work", "lint.csv", "filelist.f", "context.json"):
            path = unit_dir / name
            if not (
                path.is_dir()
                if name in {"rtl", "work"}
                else path.is_file()
            ):
                raise FileNotFoundError(f"work-unit artifact not found: {path}")
        seen.add(value)
        result.append((value, unit_dir))
    return WorkUnitManifest(
        hierarchy_available=data["hierarchy_available"],
        work_units=tuple(result),
    )


def read_lint_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != MAPPED_LINT_COLUMNS:
            raise ValueError(f"{path}: unexpected lint CSV header")
        rows = list(reader)
    if not rows:
        raise ValueError(f"{path}: work unit contains no lint rows")
    return rows


def local_item_id(unit_id: str, root_id: str, leaf_id: str) -> str:
    suffix = leaf_id if root_id == FALSE_POSITIVE_ROOT_ID else root_id
    return f"{unit_id}::{suffix}"
