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
    LEVEL_SCOPES,
    MAPPED_LINT_COLUMNS,
    VIOLATION_ID_RE,
)


class PolicyValidationError(ValueError):
    """The agent-authored slice policy is invalid and may be revised."""


def parse_tree(path: Path) -> tuple[str, set[str]]:
    modules: set[str] = set()
    top_module = ""
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if " : " not in line or " [lint=" not in line:
            continue
        branch_position = max(line.rfind("|-- "), line.rfind("`-- "))
        label = line[branch_position + 4 :] if branch_position >= 0 else line
        instance, remainder = label.split(" : ", 1)
        module = remainder.split(" [lint=", 1)[0].strip()
        if not instance.strip() or not module:
            raise ValueError(f"{path}:{line_number}: malformed hierarchy line")
        modules.add(module)
        if branch_position < 0:
            if top_module:
                raise ValueError(f"{path}: multiple hierarchy roots")
            top_module = module
    if not top_module:
        raise ValueError(f"{path}: no hierarchy root")
    return top_module, modules


def read_policy(path: Path, active_modules: set[str], top_module: str) -> dict[str, list[str]]:
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
    if levels["level4"] != [top_module]:
        raise PolicyValidationError(f"level4 must contain only top module {top_module}")
    return levels


def read_lint(path: Path) -> list[dict[str, str]]:
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
    return rows


def read_design_metadata(path: Path, project_dir: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("modules"), dict):
        raise ValueError(f"{path}: invalid design metadata")
    modules = data["modules"]
    for module_name, module in modules.items():
        source_file = str(module.get("source_file", ""))
        source_path = (project_dir / "rtl" / source_file).resolve()
        try:
            source_path.relative_to(project_dir / "rtl")
        except ValueError as exc:
            raise ValueError(f"{path}: source path escapes rtl/: {source_file}") from exc
        if not source_path.is_file():
            raise FileNotFoundError(f"{module_name}: source file not found: {source_file}")
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
    parser.add_argument("--tree-dir", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_dir = args.project_dir.expanduser().resolve()
    tree_dir = args.tree_dir.expanduser().resolve()
    tree_path = tree_dir / "hierarchy_tree.txt"
    lint_path = tree_dir / "lint_entries_mapped.csv"
    metadata_path = tree_dir / "design_metadata.json"
    if not (project_dir / "rtl").is_dir() or not (project_dir / "filelist.f").is_file():
        raise ValueError("project-dir must contain rtl/ and filelist.f")
    for path in (tree_path, lint_path, metadata_path, args.policy):
        if not path.is_file():
            raise FileNotFoundError(path)

    top_module, active_modules = parse_tree(tree_path)
    levels = read_policy(args.policy, active_modules, top_module)
    rows = read_lint(lint_path)
    design_modules = read_design_metadata(metadata_path, project_dir)

    active_rows = [row for row in rows if row["status"] == "in_hierarchy_tree"]
    isolated_rows = [
        row for row in rows if row["status"] == "standalone_module_not_in_tree"
    ]
    if len(active_rows) + len(isolated_rows) != len(rows):
        raise ValueError("lint CSV contains an unknown status")
    if any(row["source_module"] not in active_modules for row in active_rows):
        raise ValueError("an in-hierarchy lint row references a module outside the hierarchy tree")

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
