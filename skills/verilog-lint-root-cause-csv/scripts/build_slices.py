#!/usr/bin/env python3
"""Build exclusive analysis slices from an agent-authored module policy."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

from _contract import (
    IN_HIERARCHY_STATUS,
    LEVEL_SCOPES,
    MAPPED_LINT_COLUMNS,
    MODULE_SCOPE_STATUS,
    STANDALONE_STATUS,
    VIOLATION_ID_RE,
)


class PolicyValidationError(ValueError):
    """The agent-authored slice policy is invalid and may be revised."""


def parse_tree(path: Path) -> tuple[str, set[str], dict[str, str]]:
    modules: set[str] = set()
    path_to_module: dict[str, str] = {}
    instance_stack: list[str] = []
    top_module = ""
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if " : " not in line:
            continue
        branch_position = max(line.rfind("|-- "), line.rfind("`-- "))
        label = line[branch_position + 4 :] if branch_position >= 0 else line
        instance, module = (value.strip() for value in label.split(" : ", 1))
        if not instance or not module:
            raise ValueError(f"{path}:{line_number}: malformed hierarchy line")

        depth = branch_position // 4 + 1 if branch_position >= 0 else 0
        if depth > len(instance_stack):
            raise ValueError(f"{path}:{line_number}: invalid hierarchy depth")
        del instance_stack[depth:]
        instance_stack.append(instance)
        hierarchy_path = ".".join(instance_stack)
        if hierarchy_path in path_to_module:
            raise ValueError(
                f"{path}:{line_number}: duplicate hierarchy path {hierarchy_path}"
            )
        path_to_module[hierarchy_path] = module
        modules.add(module)
        if depth == 0:
            if top_module:
                raise ValueError(f"{path}: multiple hierarchy roots")
            top_module = module
    if not top_module:
        raise ValueError(f"{path}: no hierarchy root")
    return top_module, modules, path_to_module


def read_policy(
    path: Path,
    active_modules: set[str],
    top_module: str | None,
) -> dict[str, list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PolicyValidationError(f"invalid slice policy JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyValidationError("slice policy must be a JSON object")
    if data.get("schema_version") != 1:
        raise PolicyValidationError("slice policy schema_version must be 1")

    levels: dict[str, list[str]] = {}
    for scope in LEVEL_SCOPES:
        modules = data.get(scope)
        if not isinstance(modules, list) or any(
            not isinstance(module, str) or not module for module in modules
        ):
            raise PolicyValidationError(
                f"slice policy {scope} must be a list of module names"
            )
        levels[scope] = modules

    assignments = [module for scope in LEVEL_SCOPES for module in levels[scope]]
    counts = Counter(assignments)
    duplicates = sorted(module for module, count in counts.items() if count > 1)
    missing = sorted(active_modules - set(assignments))
    unknown = sorted(set(assignments) - active_modules)
    if duplicates or missing or unknown:
        raise PolicyValidationError(
            "invalid module ownership: "
            f"duplicates={duplicates}, missing={missing}, unknown={unknown}"
        )
    if len(levels["level4"]) != 1:
        raise PolicyValidationError("level4 must contain exactly one top module")
    if top_module is not None and levels["level4"] != [top_module]:
        raise PolicyValidationError(f"level4 must contain only top module {top_module}")
    return levels


def read_lint(
    path: Path,
    active_modules: set[str],
    path_to_module: dict[str, str],
    *,
    hierarchy_available: bool,
) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != MAPPED_LINT_COLUMNS:
            raise ValueError(f"{path}: expected lint header {MAPPED_LINT_COLUMNS}")
        rows = list(reader)

    ids = [row["vio_id"].strip() for row in rows]
    if any(not VIOLATION_ID_RE.match(value) for value in ids) or len(ids) != len(set(ids)):
        raise ValueError(
            f"{path}: vio_id values must match vio_<three-or-more digits> and be unique"
        )
    if any(not row["source_module"].strip() for row in rows):
        raise ValueError(f"{path}: every lint row must map to a source_module")

    valid_statuses = (
        {IN_HIERARCHY_STATUS, STANDALONE_STATUS}
        if hierarchy_available
        else {MODULE_SCOPE_STATUS}
    )
    for row_number, row in enumerate(rows, 2):
        status = row["status"]
        source_module = row["source_module"].strip()
        hierarchy = row["hierarchy"].strip()
        hierarchy_paths = (
            [value.strip() for value in hierarchy.split(";")] if hierarchy else []
        )
        if status not in valid_statuses:
            raise ValueError(f"{path}:{row_number}: unknown status {status!r}")
        if any(not value for value in hierarchy_paths):
            raise ValueError(f"{path}:{row_number}: invalid hierarchy path list")
        if len(hierarchy_paths) != len(set(hierarchy_paths)):
            raise ValueError(f"{path}:{row_number}: duplicate hierarchy path")
        if not hierarchy_available:
            if hierarchy_paths or source_module not in active_modules:
                raise ValueError(
                    f"{path}:{row_number}: invalid module-scope lint mapping"
                )
            continue
        for hierarchy_path in hierarchy_paths:
            tree_module = path_to_module.get(hierarchy_path)
            if tree_module is None:
                raise ValueError(
                    f"{path}:{row_number}: hierarchy path is absent from the tree: "
                    f"{hierarchy_path}"
                )
            if tree_module != source_module:
                raise ValueError(
                    f"{path}:{row_number}: hierarchy path {hierarchy_path} maps to "
                    f"{tree_module}, not {source_module}"
                )
        if (
            status == IN_HIERARCHY_STATUS
            and source_module not in active_modules
        ):
            raise ValueError(
                f"{path}:{row_number}: in-hierarchy module is absent from the tree"
            )
        if status == STANDALONE_STATUS and (
            source_module in active_modules or hierarchy_paths
        ):
            raise ValueError(
                f"{path}:{row_number}: standalone row conflicts with the hierarchy tree"
            )
    return rows


def read_design_metadata(path: Path, project_dir: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("modules"), dict):
        raise ValueError(f"{path}: invalid design metadata")
    modules = data["modules"]
    for module_name, module in modules.items():
        source_file = str(module.get("source_file", ""))
        source_files = module.get("source_files", [source_file])
        if (
            not isinstance(source_files, list)
            or not source_files
            or any(not isinstance(value, str) or not value for value in source_files)
            or source_file not in source_files
        ):
            raise ValueError(f"{path}: invalid source files for {module_name}")
        for value in source_files:
            source_path = (project_dir / "rtl" / value).resolve()
            try:
                source_path.relative_to(project_dir / "rtl")
            except ValueError as exc:
                raise ValueError(
                    f"{path}: source path escapes rtl/: {value}"
                ) from exc
            if not source_path.is_file():
                raise FileNotFoundError(
                    f"{module_name}: source file not found: {value}"
                )
    return modules


def write_scope(
    output_root: Path,
    scope: str,
    modules: list[str],
    rows: list[dict[str, str]],
    design_modules: dict[str, dict],
) -> None:
    scope_dir = output_root / scope
    scope_dir.mkdir()
    missing_metadata = sorted(set(modules) - set(design_modules))
    if missing_metadata:
        raise ValueError(f"{scope}: modules missing from design metadata: {missing_metadata}")

    context = {
        "schema_version": 1,
        "modules": {module: design_modules[module] for module in modules},
    }
    (scope_dir / "context.json").write_text(
        json.dumps(context, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with (scope_dir / "lint.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPED_LINT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate four exclusive slice levels from a semantic module policy."
    )
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--module-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_dir = args.project_dir.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    tree_path = work_dir / "hierarchy_tree.txt"
    lint_path = work_dir / "lint_entries_mapped.csv"
    metadata_path = work_dir / "design_metadata.json"
    if not (project_dir / "rtl").is_dir() or not (project_dir / "filelist.f").is_file():
        raise ValueError("project-dir must contain rtl/ and filelist.f")
    required_paths = [lint_path, metadata_path, args.policy]
    if not args.module_only:
        required_paths.append(tree_path)
    for path in required_paths:
        if not path.is_file():
            raise FileNotFoundError(path)

    design_modules = read_design_metadata(metadata_path, project_dir)
    if args.module_only:
        top_module = None
        active_modules = set(design_modules)
        path_to_module = {}
    else:
        top_module, active_modules, path_to_module = parse_tree(tree_path)
    levels = read_policy(args.policy, active_modules, top_module)
    rows = read_lint(
        lint_path,
        active_modules,
        path_to_module,
        hierarchy_available=not args.module_only,
    )

    active_status = MODULE_SCOPE_STATUS if args.module_only else IN_HIERARCHY_STATUS
    active_rows = [row for row in rows if row["status"] == active_status]
    isolated_rows = [
        row for row in rows if row["status"] == STANDALONE_STATUS
    ]
    isolated_modules = sorted({row["source_module"] for row in isolated_rows})
    output = project_dir / "slices"
    temp_output = Path(tempfile.mkdtemp(prefix=".slices_", dir=project_dir))
    try:
        owned_ids: list[str] = []
        for scope in LEVEL_SCOPES:
            modules = levels[scope]
            module_set = set(modules)
            scope_rows = [row for row in active_rows if row["source_module"] in module_set]
            owned_ids.extend(row["vio_id"] for row in scope_rows)
            write_scope(temp_output, scope, modules, scope_rows, design_modules)

        isolated_ids = [row["vio_id"] for row in isolated_rows]
        write_scope(temp_output, "isolated", isolated_modules, isolated_rows, design_modules)

        target_ids = {row["vio_id"] for row in rows}
        all_owned_ids = [*owned_ids, *isolated_ids]
        owned_id_set = set(all_owned_ids)
        duplicate_ids = sorted(
            value for value, count in Counter(all_owned_ids).items() if count > 1
        )
        coverage = {
            "schema_version": 1,
            "hierarchy_available": not args.module_only,
            "active_modules": {
                "target_count": len(active_modules),
                "owned_count": sum(len(modules) for modules in levels.values()),
            },
            "lint_entries": {
                "target_count": len(target_ids),
                "owned_count": len(owned_id_set),
                "missing_violation_ids": sorted(target_ids - owned_id_set),
                "duplicate_violation_ids": duplicate_ids,
            },
        }
        if coverage["lint_entries"]["missing_violation_ids"] or duplicate_ids:
            raise ValueError(f"lint ownership check failed: {coverage}")

        (temp_output / "coverage.json").write_text(
            json.dumps(coverage, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if not args.module_only:
            shutil.copy2(tree_path, temp_output / "hierarchy_tree.txt")
        if output.exists():
            shutil.rmtree(output)
        temp_output.rename(output)
    finally:
        if temp_output.exists():
            shutil.rmtree(temp_output)

    print(f"SLICES_DIR={output}")
    print(f"ACTIVE_MODULES={len(active_modules)}")
    print(f"LINT_ENTRIES={len(rows)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PolicyValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
