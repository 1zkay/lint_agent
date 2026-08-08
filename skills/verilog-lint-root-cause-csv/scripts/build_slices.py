#!/usr/bin/env python3
"""Build exclusive, physically isolated lint-analysis work units."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from _contract import (
    HDL_IDENTIFIER_RE,
    IN_HIERARCHY_STATUS,
    INSTANCE_WORK_UNIT_KIND,
    ISOLATED_SCOPE,
    LEVEL_SCOPES,
    MAPPED_LINT_COLUMNS,
    MODULE_WORK_UNIT_KIND,
    MODULE_SCOPE_STATUS,
    SLICE_SCHEMA_VERSION,
    STANDALONE_STATUS,
    VIOLATION_ID_RE,
    WORK_UNIT_ID_DIGEST_LENGTH,
    WORK_UNIT_SCOPES,
)
from _filelist import (
    SOURCE_SUFFIXES,
    parse_filelist,
    quote_filelist_path,
)

LINT_AGENT_ROOT = Path(__file__).resolve().parents[3]
if str(LINT_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINT_AGENT_ROOT))

from eda.yosys import YosysLocation, build_yosys_env, find_yosys

REPORTED_HIERARCHY_RE = re.compile(r"\bhierarchy\s+'([^']+)'", re.IGNORECASE)
PREPROCESSED_FILE_PUSH_RE = re.compile(r'^`file_push "(.+)"$')
PREPROCESSED_FILE_NOTFOUND_RE = re.compile(r"^`file_notfound (.+)$")
PACKAGE_DEFINITION_RE = re.compile(
    r"\bpackage\s+(?!body\b)([A-Za-z_][A-Za-z0-9_$]*)\b"
)
PACKAGE_REFERENCE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_$]*)::")


class PolicyValidationError(ValueError):
    """The agent-authored slice policy is invalid and may be revised."""


@dataclass(frozen=True)
class TreeInfo:
    top_module: str
    root_instance: str
    modules: set[str]
    path_to_module: dict[str, str]


@dataclass
class WorkGroup:
    rows: list[dict[str, str]]
    primary_files: tuple[str, ...]
    hierarchy_paths: set[str]


@dataclass(frozen=True)
class WorkUnitSummary:
    unit_id: str
    scope: str
    families: frozenset[str]


def parse_tree(path: Path) -> TreeInfo:
    modules: set[str] = set()
    path_to_module: dict[str, str] = {}
    seen_paths: set[str] = set()
    instance_stack: list[str] = []
    top_module = ""
    root_instance = ""
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if " : " not in line:
            continue
        branch_position = max(line.rfind("|-- "), line.rfind("`-- "))
        label = line[branch_position + 4 :] if branch_position >= 0 else line
        instance, module = (value.strip() for value in label.split(" : ", 1))
        if not instance or not module:
            raise ValueError(f"{path}:{line_number}: malformed hierarchy line")
        unresolved = module.endswith(" [unresolved]")
        if unresolved:
            module = module.removesuffix(" [unresolved]")

        depth = branch_position // 4 + 1 if branch_position >= 0 else 0
        if depth > len(instance_stack):
            raise ValueError(f"{path}:{line_number}: invalid hierarchy depth")
        del instance_stack[depth:]
        instance_stack.append(instance)
        hierarchy_path = ".".join(instance_stack)
        if hierarchy_path in seen_paths:
            raise ValueError(
                f"{path}:{line_number}: duplicate hierarchy path {hierarchy_path}"
            )
        seen_paths.add(hierarchy_path)
        if unresolved:
            continue
        path_to_module[hierarchy_path] = module
        modules.add(module)
        if depth == 0:
            if top_module:
                raise ValueError(f"{path}: multiple hierarchy roots")
            top_module = module
            root_instance = instance
    if not top_module:
        raise ValueError(f"{path}: no hierarchy root")
    return TreeInfo(
        top_module=top_module,
        root_instance=root_instance,
        modules=modules,
        path_to_module=path_to_module,
    )


def read_policy(
    path: Path,
    active_modules: set[str],
    top_module: str,
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
    if levels["level4"] != [top_module]:
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
    if any(not VIOLATION_ID_RE.match(value) for value in ids) or len(ids) != len(
        set(ids)
    ):
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
        if status == IN_HIERARCHY_STATUS and source_module not in active_modules:
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


def read_design_metadata(path: Path, rtl_dir: Path) -> dict[str, dict]:
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
            source_path = (rtl_dir / value).resolve()
            try:
                source_path.relative_to(rtl_dir)
            except ValueError as exc:
                raise ValueError(
                    f"{path}: source path escapes rtl/: {value}"
                ) from exc
            if not source_path.is_file():
                raise FileNotFoundError(
                    f"{module_name}: source file not found: {value}"
                )
    return modules


def module_source_files(module: dict) -> list[str]:
    return list(module.get("source_files", [module["source_file"]]))


def path_ancestors(path: str, path_to_module: dict[str, str]) -> list[str]:
    parts = path.split(".")
    result = []
    for length in range(1, len(parts) + 1):
        value = ".".join(parts[:length])
        if value not in path_to_module:
            break
        result.append(value)
    return result


def normalize_annotated_hierarchy(value: str) -> str:
    instances = [
        token.split("@", 1)[0].strip().strip("/")
        for token in value.replace("\\", "/").split(":")
    ]
    return ".".join(instance for instance in instances if instance)


def longest_tree_prefix(value: str, path_to_module: dict[str, str]) -> str:
    parts = [
        token.split("[", 1)[0]
        for token in value.replace("\\", "/").strip("/").split("/")
        if token
    ]
    for length in range(len(parts), 0, -1):
        candidate = ".".join(parts[:length])
        if candidate in path_to_module:
            return candidate
    return ""


def referenced_tree_paths(contents: str, tree: TreeInfo) -> set[str]:
    result: set[str] = set()
    for value in REPORTED_HIERARCHY_RE.findall(contents):
        path = normalize_annotated_hierarchy(value)
        if path in tree.path_to_module:
            result.add(path)

    slash_path_re = re.compile(
        rf"\b{re.escape(tree.root_instance)}"
        r"(?:/[A-Za-z_$][A-Za-z0-9_$]*(?:\[[^\]\s/]+\])?)+"
    )
    for value in slash_path_re.findall(contents):
        path = longest_tree_prefix(value, tree.path_to_module)
        if path:
            result.add(path)
    return result


class DependencyResolver:
    """Resolve active include and SystemVerilog package dependencies."""

    def __init__(
        self,
        rtl_dir: Path,
        filelist_path: Path,
        yosys: YosysLocation,
        work_dir: Path,
    ) -> None:
        self.rtl_dir = rtl_dir.resolve()
        self.inputs = parse_filelist(filelist_path)
        self.yosys = yosys
        self.work_dir = work_dir.resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.source_order = [
            self._relative(path)
            for path in self.inputs.source_files()
            if self._is_inside(path)
        ]
        self.include_dirs = [
            path.resolve()
            for path in self.inputs.incdirs
            if self._is_inside(path)
        ]
        self._text_cache: dict[str, str] = {}
        self._include_cache: dict[str, set[str]] = {}
        self._package_files = self._index_packages()

    def _is_inside(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.rtl_dir)
            return True
        except ValueError:
            return False

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.rtl_dir).as_posix()

    def _text(self, relative: str) -> str:
        if relative not in self._text_cache:
            self._text_cache[relative] = (self.rtl_dir / relative).read_text(
                encoding="utf-8", errors="replace"
            )
        return self._text_cache[relative]

    def _index_packages(self) -> dict[str, set[str]]:
        result: dict[str, set[str]] = defaultdict(set)
        for relative in self.source_order:
            for package in PACKAGE_DEFINITION_RE.findall(self._text(relative)):
                result[package].add(relative)
        return result

    def _resolve_preprocessed_path(
        self,
        value: str,
        parent: Path | None,
    ) -> Path:
        path = Path(json.loads(f'"{value}"'))
        if path.is_absolute():
            candidates = [path]
        else:
            candidates = [
                *((parent.parent / path,) if parent is not None else ()),
                *(directory / path for directory in self.include_dirs),
                self.rtl_dir / path,
            ]
        for candidate in candidates:
            resolved = candidate.resolve()
            if self._is_inside(resolved) and resolved.is_file():
                return resolved
        raise FileNotFoundError(f"Yosys reported an unavailable source: {value}")

    def _active_includes(self, relative: str) -> set[str]:
        if relative in self._include_cache:
            return self._include_cache[relative]

        source = (self.rtl_dir / relative).resolve()
        digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
        script_path = self.work_dir / f"{digest}.ys"
        log_path = self.work_dir / f"{digest}.log"
        command = ["read_verilog", "-sv", "-ppdump"]
        for directory in self.include_dirs:
            command.append(f"-I{directory.as_posix()}")
        command.extend(f"-D{define}" for define in self.inputs.defines)
        command.append(quote_filelist_path(str(source)))
        script_path.write_text(" ".join(command) + "\n", encoding="utf-8")

        result = subprocess.run(
            [
                str(self.yosys.bin),
                "-Q",
                "-T",
                "-q",
                "-l",
                str(log_path),
                "-s",
                str(script_path),
            ],
            cwd=self.rtl_dir,
            env=build_yosys_env(self.yosys),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        # Post-preprocessing parser errors do not invalidate the ppdump markers.
        if not log_path.is_file():
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f"Yosys preprocessing failed for {relative}: "
                f"{detail[-2000:] or 'no preprocessor output'}"
            )

        dependencies: set[str] = set()
        stack: list[Path] = []
        missing_includes: list[str] = []
        observed_source = False
        completed_dump = False
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        for line in log_text.splitlines():
            match = PREPROCESSED_FILE_PUSH_RE.match(line)
            if match:
                path = self._resolve_preprocessed_path(
                    match.group(1),
                    stack[-1] if stack else None,
                )
                stack.append(path)
                observed_source = observed_source or path == source
                if path != source:
                    dependencies.add(self._relative(path))
            elif line == "`file_pop" and stack:
                stack.pop()
            else:
                missing = PREPROCESSED_FILE_NOTFOUND_RE.match(line)
                if missing:
                    missing_includes.append(missing.group(1))
                completed_dump = completed_dump or line == "-- END OF DUMP --"

        if missing_includes:
            raise FileNotFoundError(
                f"{relative}: active include file not found: "
                f"{', '.join(missing_includes)}"
            )
        if not observed_source or not completed_dump or stack:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or log_text[-2000:].strip()
            )
            raise RuntimeError(
                f"Yosys preprocessing failed for {relative}: "
                f"{detail[-2000:] or 'no preprocessor output'}"
            )
        self._include_cache[relative] = dependencies
        return dependencies

    def closure(self, primary_files: tuple[str, ...]) -> set[str]:
        selected: set[str] = set()
        pending = deque(primary_files)
        processed: set[str] = set()
        while pending:
            source = pending.popleft()
            if source in processed:
                continue
            processed.add(source)
            compilation_files = {source, *self._active_includes(source)}
            selected.update(compilation_files)
            for relative in compilation_files:
                for package in PACKAGE_REFERENCE_RE.findall(self._text(relative)):
                    for package_file in self._package_files.get(package, set()):
                        if package_file not in processed:
                            pending.append(package_file)
        return selected

    def ordered_sources(self, selected: set[str]) -> list[str]:
        ordered = [
            relative
            for relative in self.source_order
            if relative in selected and Path(relative).suffix.lower() in SOURCE_SUFFIXES
        ]
        known = set(ordered)
        ordered.extend(
            sorted(
                relative
                for relative in selected - known
                if Path(relative).suffix.lower() in SOURCE_SUFFIXES
            )
        )
        return ordered


def group_rows(
    rows: list[dict[str, str]],
    *,
    tree: TreeInfo | None,
    design_modules: dict[str, dict],
) -> dict[tuple[str, ...], WorkGroup]:
    groups: dict[tuple[str, ...], WorkGroup] = {}
    for row in rows:
        hierarchy_values = [
            value for value in row["hierarchy"].split(";") if value
        ]
        if not hierarchy_values:
            primary_files = (row["source_file"],)
            related_paths: set[str] = set()
        else:
            if tree is None:
                raise ValueError("instance-scoped lint requires a hierarchy tree")
            related_paths = set(hierarchy_values)
            related_paths.update(referenced_tree_paths(row["contents"], tree))
            expanded_paths = {
                ancestor
                for path in related_paths
                for ancestor in path_ancestors(path, tree.path_to_module)
            }
            related_paths = expanded_paths
            files = {row["source_file"]}
            for path in related_paths:
                module = tree.path_to_module[path]
                files.update(module_source_files(design_modules[module]))
            primary_files = tuple(sorted(files))

        group = groups.setdefault(
            primary_files,
            WorkGroup(rows=[], primary_files=primary_files, hierarchy_paths=set()),
        )
        group.rows.append(row)
        group.hierarchy_paths.update(related_paths)
    return groups


def normalized_module_family(module: str) -> str:
    family = re.sub(r"(?:_\d+)+$", "", module)
    return re.sub(r"\d+(?=_)", "N", family)


def summarize_work_unit(
    unit_id: str,
    scope: str,
    group: WorkGroup,
) -> WorkUnitSummary:
    return WorkUnitSummary(
        unit_id=unit_id,
        scope=scope,
        families=frozenset(
            normalized_module_family(row["source_module"])
            for row in group.rows
        ),
    )


def build_family_batches(units: list[WorkUnitSummary]) -> list[list[str]]:
    parents = list(range(len(units)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    owners: dict[str, int] = {}
    for index, unit in enumerate(units):
        for family in unit.families:
            previous = owners.setdefault(family, index)
            union(index, previous)

    groups: dict[int, list[str]] = defaultdict(list)
    for index, unit in enumerate(units):
        groups[find(index)].append(unit.unit_id)
    normalized = [sorted(batch) for batch in groups.values()]
    return sorted(normalized, key=lambda batch: tuple(batch))


def build_analysis_batches(units: list[WorkUnitSummary]) -> list[list[str]]:
    result: list[list[str]] = []
    grouped: dict[str, list[WorkUnitSummary]] = defaultdict(list)
    for unit in units:
        grouped[unit.scope].append(unit)

    for scope in WORK_UNIT_SCOPES:
        selected = grouped.get(scope, [])
        if not selected:
            continue
        if scope in LEVEL_SCOPES:
            result.extend(build_family_batches(selected))
        else:
            result.extend(
                [unit.unit_id]
                for unit in sorted(selected, key=lambda item: item.unit_id)
            )
    return result


def render_pruned_tree(tree: TreeInfo, selected_paths: set[str]) -> str:
    children: dict[str, list[str]] = defaultdict(list)
    for path in tree.path_to_module:
        if path not in selected_paths or "." not in path:
            continue
        parent = path.rsplit(".", 1)[0]
        if parent in selected_paths:
            children[parent].append(path)

    root = tree.root_instance
    if root not in selected_paths:
        raise ValueError("pruned hierarchy does not contain the tree root")
    lines = [f"{root} : {tree.path_to_module[root]}"]

    def append_children(parent: str, prefix: str) -> None:
        values = children.get(parent, [])
        for index, path in enumerate(values):
            is_last = index == len(values) - 1
            branch = "`-- " if is_last else "|-- "
            instance = path.rsplit(".", 1)[-1]
            lines.append(
                f"{prefix}{branch}{instance} : {tree.path_to_module[path]}"
            )
            append_children(path, prefix + ("    " if is_last else "|   "))

    append_children(root, "")
    return "\n".join(lines) + "\n"


def write_filelist(
    path: Path,
    resolver: DependencyResolver,
    selected: set[str],
) -> None:
    source_files = resolver.ordered_sources(selected)
    include_dirs = sorted(
        {
            str(Path(relative).parent)
            for relative in selected
            if str(Path(relative).parent) != "."
        }
    )
    lines = [
        f"-I {quote_filelist_path('rtl')}",
        *(
            f"-I {quote_filelist_path(f'rtl/{directory}')}"
            for directory in include_dirs
        ),
        *(
            f"-D {quote_filelist_path(define)}"
            for define in resolver.inputs.defines
        ),
        *(
            quote_filelist_path(f"rtl/{relative}")
            for relative in source_files
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_work_unit(
    output_root: Path,
    scope: str,
    kind: str,
    group: WorkGroup,
    *,
    rtl_dir: Path,
    resolver: DependencyResolver,
    design_modules: dict[str, dict],
    tree: TreeInfo | None,
) -> str:
    key = "\0".join((scope, kind, *group.primary_files))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[
        :WORK_UNIT_ID_DIGEST_LENGTH
    ]
    relative_dir = Path(scope) / kind / f"{kind}_{digest}"
    unit_dir = output_root / relative_dir
    unit_dir.mkdir(parents=True)
    (unit_dir / "work").mkdir()

    selected = resolver.closure(group.primary_files)
    for relative in sorted(selected):
        source = rtl_dir / relative
        target = unit_dir / "rtl" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    primary_set = set(group.primary_files)
    context_modules = {
        name: metadata
        for name, metadata in sorted(design_modules.items())
        if primary_set.intersection(module_source_files(metadata))
    }
    context = {
        "schema_version": SLICE_SCHEMA_VERSION,
        "modules": context_modules,
        "primary_source_files": list(group.primary_files),
        "dependency_source_files": sorted(selected - primary_set),
        "hierarchy_paths": sorted(group.hierarchy_paths),
    }
    (unit_dir / "context.json").write_text(
        json.dumps(context, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with (unit_dir / "lint.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPED_LINT_COLUMNS)
        writer.writeheader()
        writer.writerows(group.rows)
    write_filelist(unit_dir / "filelist.f", resolver, selected)

    if kind == INSTANCE_WORK_UNIT_KIND:
        if tree is None:
            raise ValueError("instance work unit requires hierarchy tree")
        (unit_dir / "hierarchy_tree.txt").write_text(
            render_pruned_tree(tree, group.hierarchy_paths),
            encoding="utf-8",
        )
    return relative_dir.as_posix()


def replace_directory(source: Path, target: Path) -> None:
    if not target.exists():
        source.rename(target)
        return
    backup = Path(tempfile.mkdtemp(prefix=".slices_previous_", dir=target.parent))
    backup.rmdir()
    target.rename(backup)
    try:
        source.rename(target)
    except Exception:
        backup.rename(target)
        raise
    shutil.rmtree(backup)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate exclusive physical work units from a module policy."
    )
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--top", required=True)
    parser.add_argument("--module-only", action="store_true")
    parser.add_argument("--yosys", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_dir = args.project_dir.expanduser().resolve()
    rtl_dir = project_dir / "rtl"
    filelist_path = project_dir / "filelist.f"
    work_dir = args.work_dir.expanduser().resolve()
    tree_path = work_dir / "hierarchy_tree.txt"
    lint_path = work_dir / "lint_entries_mapped.csv"
    metadata_path = work_dir / "design_metadata.json"
    if not rtl_dir.is_dir() or not filelist_path.is_file():
        raise ValueError("project-dir must contain rtl/ and filelist.f")
    required_paths = [lint_path, metadata_path, args.policy]
    if not args.module_only:
        required_paths.append(tree_path)
    for path in required_paths:
        if not path.is_file():
            raise FileNotFoundError(path)

    design_modules = read_design_metadata(metadata_path, rtl_dir)
    if not HDL_IDENTIFIER_RE.fullmatch(args.top):
        raise ValueError(f"unsupported top module identifier: {args.top!r}")
    if args.top not in design_modules:
        raise ValueError(f"top module is absent from design metadata: {args.top}")
    tree = None if args.module_only else parse_tree(tree_path)
    if tree is not None and tree.top_module != args.top:
        raise ValueError(
            f"hierarchy top {tree.top_module} does not match requested top {args.top}"
        )
    active_modules = set(design_modules) if tree is None else tree.modules
    path_to_module = {} if tree is None else tree.path_to_module
    levels = read_policy(args.policy, active_modules, args.top)
    rows = read_lint(
        lint_path,
        active_modules,
        path_to_module,
        hierarchy_available=tree is not None,
    )
    level_by_module = {
        module: scope for scope, modules in levels.items() for module in modules
    }
    output = project_dir / "slices"
    temp_output = Path(tempfile.mkdtemp(prefix=".slices_", dir=project_dir))
    dependency_work = Path(
        tempfile.mkdtemp(prefix=".yosys_dependencies_", dir=work_dir)
    )
    try:
        yosys = find_yosys(
            explicit_bin=str(args.yosys) if args.yosys else None,
            start_points=[project_dir],
        )
        resolver = DependencyResolver(
            rtl_dir,
            filelist_path,
            yosys,
            dependency_work,
        )
        scoped_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in rows:
            scope = (
                ISOLATED_SCOPE
                if row["status"] == STANDALONE_STATUS
                else level_by_module[row["source_module"]]
            )
            scoped_rows[scope].append(row)

        work_units: list[WorkUnitSummary] = []
        owned_ids: list[str] = []
        for scope in WORK_UNIT_SCOPES:
            module_rows = [
                row for row in scoped_rows.get(scope, []) if not row["hierarchy"]
            ]
            instance_rows = [
                row for row in scoped_rows.get(scope, []) if row["hierarchy"]
            ]
            for kind, selected_rows in (
                (MODULE_WORK_UNIT_KIND, module_rows),
                (INSTANCE_WORK_UNIT_KIND, instance_rows),
            ):
                for _, group in sorted(
                    group_rows(
                        selected_rows,
                        tree=tree,
                        design_modules=design_modules,
                    ).items()
                ):
                    unit_id = write_work_unit(
                        temp_output,
                        scope,
                        kind,
                        group,
                        rtl_dir=rtl_dir,
                        resolver=resolver,
                        design_modules=design_modules,
                        tree=tree,
                    )
                    work_units.append(
                        summarize_work_unit(
                            unit_id,
                            scope,
                            group,
                        )
                    )
                    owned_ids.extend(row["vio_id"] for row in group.rows)

        target_ids = {row["vio_id"] for row in rows}
        owned_id_set = set(owned_ids)
        duplicate_ids = sorted(
            value for value, count in Counter(owned_ids).items() if count > 1
        )
        missing_ids = sorted(target_ids - owned_id_set)
        if missing_ids or duplicate_ids:
            raise ValueError(
                "lint ownership check failed: "
                f"missing={missing_ids}, duplicates={duplicate_ids}"
            )
        manifest = {
            "schema_version": SLICE_SCHEMA_VERSION,
            "hierarchy_available": tree is not None,
            "analysis_batches": build_analysis_batches(work_units),
        }
        (temp_output / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        replace_directory(temp_output, output)
    finally:
        if temp_output.exists():
            shutil.rmtree(temp_output)
        if dependency_work.exists():
            shutil.rmtree(dependency_work)

    print(f"SLICES_DIR={output}")
    print(f"WORK_UNITS={len(work_units)}")
    print(f"ANALYSIS_BATCHES={len(manifest['analysis_batches'])}")
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
