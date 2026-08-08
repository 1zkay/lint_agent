#!/usr/bin/env python3
"""Map lint CSV entries onto an elaborated hierarchy or source modules."""

from __future__ import annotations

import argparse
import csv
import json
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from _contract import (
    FILELIST_RECOVERY_EXIT_CODE,
    HDL_IDENTIFIER_RE,
    HIERARCHY_ATTEMPTS,
    IN_HIERARCHY_STATUS,
    MAPPED_LINT_COLUMNS,
    MODULE_SCOPE_STATUS,
    STANDALONE_STATUS,
    format_violation_id,
    hierarchy_module_status,
    hierarchy_tree_status,
)
from _filelist import (
    SOURCE_SUFFIXES,
    FilelistInputs,
    choose_filelist,
    parse_filelist,
    render_filelist,
)

LINT_AGENT_ROOT = Path(__file__).resolve().parents[3]
if str(LINT_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINT_AGENT_ROOT))

from eda.yosys import YosysLocation, build_yosys_env, find_yosys


POSITION_RE = re.compile(
    r"(?P<path>.+\.(?:sv|v|svh|vh))\((?P<line>\d+)\)\s*$",
    re.IGNORECASE,
)
EMBEDDED_POSITION_RE = re.compile(
    r"(?:^|\t)(?P<path>[^\t\r\n]+\.(?:sv|v|svh|vh))"
    r"\((?P<line>\d+)\)\s*$",
    re.IGNORECASE,
)
REPORTED_HIERARCHY_RE = re.compile(r"\bhierarchy\s+'([^']+)'", re.IGNORECASE)
SRC_LINE_RE = re.compile(r":(?P<line>\d+)\.")
VERILOG_TOKEN_RE = re.compile(r"\\[^\s]+|[A-Za-z_][A-Za-z0-9_$]*")
MODULE_KEYWORDS = {"module", "macromodule"}
MODULE_LIFETIMES = {"automatic", "static"}
NORMALIZED_LINT_INPUT_COLUMNS = (
    "violation_id",
    "severity",
    "message_id",
    "description",
    "file_path",
    "line_number",
)
LEGACY_LINT_INPUT_COLUMNS = (
    "stage",
    "messageid",
    "severity",
    "contents",
    "lineno",
    "",
)
@dataclass(frozen=True)
class ModuleRange:
    module: str
    file: Path
    analysis_file: str
    start: int
    end: int


@dataclass(frozen=True)
class FileModuleRangeIndex:
    file: Path
    ranges: tuple[ModuleRange, ...]
    starts: tuple[int, ...]


type ModuleRangeIndex = dict[str, tuple[FileModuleRangeIndex, ...]]


@dataclass
class CellNode:
    path: tuple[str, ...]
    display_type: str
    unresolved: bool = False
    children: list["CellNode"] = field(default_factory=list)

    @property
    def display_path(self) -> str:
        return ".".join(self.path)


@dataclass
class MappedLint:
    vio_id: str
    message_id: str
    severity: str
    source_file: str
    source_line: int
    source_module: str
    hierarchy_paths: list[str]
    status: str
    contents: str


@dataclass(frozen=True)
class HierarchyRun:
    json_path: Path
    frontend: str
    completeness: str


class FilelistRecoveryError(ValueError):
    """Report an input-selection error that a corrected filelist may resolve."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Map lint CSV file(line) reports onto a Yosys hierarchy, with "
            "source-module fallback when hierarchy generation fails."
        )
    )
    parser.add_argument("--csv", type=Path, required=True, help="Lint report CSV path.")
    parser.add_argument("--source", type=Path, required=True, help="Staged project directory.")
    parser.add_argument("--top", required=True, help="Top module.")
    hierarchy_output = parser.add_mutually_exclusive_group()
    hierarchy_output.add_argument(
        "--require-hierarchy",
        action="store_true",
        help="Return an error instead of writing module-level fallback outputs.",
    )
    hierarchy_output.add_argument(
        "--module-only",
        metavar="REASON",
        help="Skip Yosys and write module-level outputs with this reason.",
    )
    parser.add_argument("--out-dir", type=Path, required=True, help="Output directory.")
    parser.add_argument("--yosys", type=Path, help="Explicit Yosys executable.")
    parser.add_argument(
        "--keep-work",
        action="store_true",
        help="Keep the temporary extraction/Yosys work directory for debugging.",
    )
    return parser.parse_args()


def copy_runtime_data_files(source_root: Path, work_root: Path) -> None:
    """Stage common $readmemh/$readmemb data files for Yosys' run directory."""
    suffixes = {".mem", ".hex", ".mif", ".dat", ".bin"}
    data_files = [p for p in source_root.rglob("*") if p.is_file() and p.suffix.lower() in suffixes]
    if not data_files:
        return

    build_dir = work_root / "build"
    src_dir = work_root / "src"
    build_src_dir = build_dir / "src"
    for directory in (build_dir, src_dir, build_src_dir):
        directory.mkdir(parents=True, exist_ok=True)

    for data_file in data_files:
        rel_path = data_file.relative_to(source_root)
        targets = [
            build_dir / data_file.name,
            src_dir / data_file.name,
            build_src_dir / data_file.name,
            build_dir / rel_path,
        ]
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                continue
            shutil.copy2(data_file, target)


def collect_verilog_inputs(
    source_root: Path,
) -> tuple[FilelistInputs, list[Path]]:
    analysis_root = (source_root / "rtl").resolve()
    filelist = choose_filelist(source_root)
    if filelist:
        inputs = parse_filelist(filelist, working_dir=source_root)
    else:
        inputs = FilelistInputs(
            files=sorted(
                p.resolve()
                for p in source_root.rglob("*")
                if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES
            )
        )
    files = inputs.source_files()

    if not files:
        raise FileNotFoundError(f"No Verilog source files found under {source_root}")

    for source_file in files:
        try:
            source_file.relative_to(analysis_root)
        except ValueError as exc:
            raise ValueError(
                f"filelist source is outside staged rtl/: {source_file}"
            ) from exc
    for directory in [*inputs.incdirs, *inputs.library_dirs]:
        try:
            directory.relative_to(analysis_root)
        except ValueError as exc:
            raise ValueError(
                f"filelist directory is outside staged rtl/: {directory}"
            ) from exc

    return inputs, files


def yosys_quote(value: str | Path) -> str:
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_yosys(
    yosys: YosysLocation,
    inputs: FilelistInputs,
    top: str,
    work_root: Path,
) -> HierarchyRun:
    hierarchy_path = work_root / "hierarchy.json"
    build_dir = work_root / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    files = [*inputs.library_files, *inputs.files]
    has_sv = (
        inputs.language_standard == "latest"
        or bool(
            inputs.language_standard
            and inputs.language_standard.startswith("1800-")
        )
        or any(
            path.suffix.lower() in {".sv", ".svh"}
            for path in inputs.source_files()
        )
    )

    defaults = ["verilog_defaults", "-add"]
    if has_sv:
        defaults.append("-sv")
    defaults.extend(f"-I{path.as_posix()}" for path in inputs.incdirs)
    defaults.extend(f"-D{define}" for define in inputs.defines)

    read_commands: list[list[str]] = []
    if len(defaults) > 2:
        read_commands.append(defaults)
    if files:
        read_commands.append(
            ["read_verilog", "-defer", *(yosys_quote(path) for path in files)]
        )

    def write_script(
        path: Path,
        commands: list[list[str]],
        hierarchy_command: str,
    ) -> None:
        script = "\n".join(
            [
                *(" ".join(command) for command in commands),
                hierarchy_command,
                "proc",
                f"write_json {yosys_quote(hierarchy_path)}",
                "",
            ]
        )
        path.write_text(script, encoding="utf-8")

    def execute(path: Path) -> subprocess.CompletedProcess[str]:
        command = [str(yosys.bin), "-q", "-s", str(path)]
        return subprocess.run(
            command,
            cwd=build_dir,
            env=build_yosys_env(yosys),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    slang_filelist_path = work_root / "hierarchy.slang.f"
    failures: list[str] = []
    for selected_frontend, completeness in HIERARCHY_ATTEMPTS:
        hierarchy_path.unlink(missing_ok=True)
        hierarchy_parts = [
            "hierarchy",
            *(["-check"] if completeness == "complete" else []),
            "-top",
            top,
        ]
        if selected_frontend == "read_verilog":
            for library_dir in inputs.library_dirs:
                hierarchy_parts.extend(["-libdir", library_dir.as_posix()])
            script_path = work_root / f"hierarchy.read_verilog.{completeness}.ys"
            commands = read_commands
        else:
            if not slang_filelist_path.exists():
                slang_filelist_path.write_text(
                    render_filelist(inputs, lambda path: str(path), top=top),
                    encoding="utf-8",
                )
            script_path = work_root / f"hierarchy.read_slang.{completeness}.ys"
            commands = [
                [
                    "read_slang",
                    "--keep-hierarchy",
                    "-F",
                    slang_filelist_path.as_posix(),
                ]
            ]
        write_script(script_path, commands, " ".join(hierarchy_parts))

        result = execute(script_path)
        for stream, contents in (
            ("stdout", result.stdout),
            ("stderr", result.stderr),
        ):
            (
                work_root
                / f"yosys.{selected_frontend}.{completeness}.{stream}.log"
            ).write_text(
                contents,
                encoding="utf-8",
                errors="replace",
            )
        if result.returncode == 0 and hierarchy_path.is_file():
            return HierarchyRun(
                json_path=hierarchy_path,
                frontend=selected_frontend,
                completeness=completeness,
            )
        failures.append(
            f"{selected_frontend} + {completeness} script: {script_path}\n"
            f"{selected_frontend} + {completeness} stderr tail:\n"
            f"{result.stderr[-2000:]}"
        )

    raise RuntimeError(
        "Yosys hierarchy generation failed. See attempt-specific logs:\n"
        + "\n".join(failures)
    )


def module_display_name(
    internal_name: str,
    module: dict,
    source_modules: set[str],
) -> str:
    attrs = module.get("attributes", {})
    hdlname = attrs.get("hdlname")
    if isinstance(hdlname, str) and hdlname:
        return hdlname.split()[-1]
    if internal_name in source_modules:
        return internal_name
    if internal_name.startswith("$paramod\\"):
        parts = internal_name.split("\\")
        if len(parts) >= 2:
            return parts[1]
    if internal_name.startswith("$paramod$"):
        parts = internal_name.split("\\")
        if parts:
            return parts[-1]
    slang_matches = [
        source_module
        for source_module in source_modules
        if internal_name.startswith(f"{source_module}$")
    ]
    if slang_matches:
        return max(slang_matches, key=len)
    return internal_name


def src_line(cell: dict) -> int:
    src = cell.get("attributes", {}).get("src", "")
    match = SRC_LINE_RE.search(src)
    return int(match.group("line")) if match else 10**9


def find_top_module(modules: dict[str, dict]) -> str:
    tops = [
        name
        for name, module in modules.items()
        if module.get("attributes", {}).get("top")
        not in (None, "00000000000000000000000000000000", "0")
    ]
    if len(tops) != 1:
        raise RuntimeError(f"Expected one top module after Yosys hierarchy, found: {tops}")
    return tops[0]


def build_hierarchy(
    json_path: Path,
    source_modules: set[str],
) -> tuple[CellNode, dict[str, list[str]], dict[str, int]]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    modules: dict[str, dict] = data["modules"]
    module_names = set(modules)
    display_by_internal = {
        internal: module_display_name(internal, module, source_modules)
        for internal, module in modules.items()
    }
    top_internal = find_top_module(modules)
    top_display = display_by_internal[top_internal]

    module_to_paths: dict[str, list[str]] = defaultdict(list)
    unresolved_modules: dict[str, int] = defaultdict(int)

    def make_node(internal: str, path: tuple[str, ...]) -> CellNode:
        display_type = display_by_internal[internal]
        node = CellNode(
            path=path,
            display_type=display_type,
        )
        path_text = node.display_path
        module_to_paths[display_type].append(path_text)

        cells = modules[internal].get("cells", {})
        child_items = sorted(
            (
                (src_line(cell), cell_name, cell)
                for cell_name, cell in cells.items()
                if cell.get("type") in module_names
                or (
                    str(cell.get("type", ""))
                    and not str(cell.get("type", "")).startswith("$")
                )
            ),
            key=lambda item: (item[0], item[1]),
        )
        for _, cell_name, cell in child_items:
            child_internal = cell["type"]
            if child_internal in module_names:
                child = make_node(child_internal, (*path, cell_name))
            else:
                unresolved_modules[child_internal] += 1
                child = CellNode(
                    path=(*path, cell_name),
                    display_type=child_internal,
                    unresolved=True,
                )
            node.children.append(child)
        return node

    root = make_node(top_internal, (top_display,))
    return root, dict(module_to_paths), dict(unresolved_modules)


def render_tree(root: CellNode) -> str:
    lines: list[str] = []

    def label(node: CellNode) -> str:
        suffix = " [unresolved]" if node.unresolved else ""
        return f"{node.path[-1]} : {node.display_type}{suffix}"

    lines.append(label(root))

    def rec(node: CellNode, prefix: str = "") -> None:
        for idx, child in enumerate(node.children):
            last = idx == len(node.children) - 1
            branch = "`-- " if last else "|-- "
            lines.append(prefix + branch + label(child))
            rec(child, prefix + ("    " if last else "|   "))

    rec(root)
    return "\n".join(lines)


def parse_module_ranges(
    files: Iterable[Path],
    rtl_root: Path,
) -> list[ModuleRange]:
    ranges: list[ModuleRange] = []
    for file_path in files:
        analysis_file = (
            file_path.resolve().relative_to(rtl_root.resolve()).as_posix()
        )
        in_block_comment = False
        in_string = False
        current: str | None = None
        start = 0
        module_start = 0
        expect_module_name = False
        lines = file_path.read_text(
            encoding="utf-8-sig",
            errors="replace",
        ).splitlines()
        for line_no, line in enumerate(lines, 1):
            code: list[str] = []
            index = 0
            while index < len(line):
                if in_block_comment:
                    end = line.find("*/", index)
                    if end < 0:
                        index = len(line)
                        continue
                    in_block_comment = False
                    code.append(" ")
                    index = end + 2
                    continue
                if in_string:
                    if line[index] == "\\":
                        index += 2
                    elif line[index] == '"':
                        in_string = False
                        index += 1
                    else:
                        index += 1
                    continue
                if line.startswith("//", index):
                    break
                if line.startswith("/*", index):
                    in_block_comment = True
                    code.append(" ")
                    index += 2
                    continue
                if line[index] == '"':
                    in_string = True
                    code.append(" ")
                    index += 1
                    continue
                if line[index] == "\\":
                    end = index + 1
                    while end < len(line) and not line[end].isspace():
                        end += 1
                    code.append(line[index:end])
                    index = end
                    continue
                code.append(line[index])
                index += 1
            for match in VERILOG_TOKEN_RE.finditer("".join(code)):
                token = match.group(0)
                if current is None:
                    if expect_module_name:
                        if token in MODULE_LIFETIMES:
                            continue
                        current = token
                        start = module_start
                        expect_module_name = False
                    elif token in MODULE_KEYWORDS:
                        module_start = line_no
                        expect_module_name = True
                elif token == "endmodule":
                    ranges.append(
                        ModuleRange(
                            module=current,
                            file=file_path,
                            analysis_file=analysis_file,
                            start=start,
                            end=line_no,
                        )
                    )
                    current = None
    return ranges


def normalize_lint_file(path_text: str) -> str:
    return posixpath.normpath(path_text.replace("\\", "/"))


def normalize_reported_hierarchy(value: str) -> str:
    value = value.strip().strip("/")
    if not value:
        return ""
    separator = ":" if ":" in value and "@" in value else "/"
    instances = [
        token.split("@", 1)[0].strip().strip("/")
        for token in value.replace("\\", "/").split(separator)
    ]
    return ".".join(instance for instance in instances if instance)


def build_module_range_index(ranges: list[ModuleRange]) -> ModuleRangeIndex:
    by_file: dict[Path, list[ModuleRange]] = defaultdict(list)
    for item in ranges:
        by_file[item.file.resolve()].append(item)

    by_basename: dict[str, list[FileModuleRangeIndex]] = defaultdict(list)
    for source_file, file_ranges in by_file.items():
        ordered = tuple(sorted(file_ranges, key=lambda item: (item.start, item.end)))
        by_basename[source_file.name].append(
            FileModuleRangeIndex(
                file=source_file,
                ranges=ordered,
                starts=tuple(item.start for item in ordered),
            )
        )

    return {
        basename: tuple(sorted(files, key=lambda item: item.file.as_posix()))
        for basename, files in by_basename.items()
    }


def contains_hierarchy_path(contents: str, path: str) -> bool:
    needle = path.replace(".", "/")
    start = 0
    while True:
        index = contents.find(needle, start)
        if index < 0:
            return False
        before = contents[index - 1] if index else ""
        end = index + len(needle)
        after = contents[end] if end < len(contents) else ""
        if (
            (not before or not (before.isalnum() or before in "_$/"))
            and (not after or after in "/[ .'\"),;:@")
        ):
            return True
        start = index + 1


def reported_hierarchy_paths(contents: str, candidates: list[str]) -> list[str]:
    resolved: list[str] = []
    for value in REPORTED_HIERARCHY_RE.findall(contents):
        reported = normalize_reported_hierarchy(value)
        matches = [
            path
            for path in candidates
            if path == reported or path.endswith(f".{reported}")
        ]
        if len(matches) == 1:
            resolved.extend(matches)

    if not resolved:
        resolved.extend(
            path for path in candidates if contains_hierarchy_path(contents, path)
        )
    return list(dict.fromkeys(resolved))


def locate_module_for_line(
    index: ModuleRangeIndex,
    lint_path: str,
    line: int,
) -> ModuleRange | None:
    norm = normalize_lint_file(lint_path)
    basename = Path(norm).name

    def path_match_score(
        file_index: FileModuleRangeIndex,
    ) -> tuple[int, int] | None:
        candidate = file_index.ranges[0].analysis_file
        if norm == candidate or norm == file_index.file.as_posix():
            return 2, len(norm)
        if norm.endswith(f"/{candidate}"):
            return 1, len(candidate)
        if candidate.endswith(f"/{norm}"):
            return 1, len(norm)
        return None

    basename_files = index.get(basename, ())
    scored_files = [
        (score, file_index)
        for file_index in basename_files
        if (score := path_match_score(file_index)) is not None
    ]
    if scored_files:
        best_score = max(score for score, _ in scored_files)
        candidate_files = tuple(
            file_index
            for score, file_index in scored_files
            if score == best_score
        )
    else:
        candidate_files = basename_files
    if not candidate_files:
        return None

    if len(candidate_files) > 1:
        candidates = ", ".join(item.file.as_posix() for item in candidate_files)
        raise FilelistRecoveryError(
            f"ambiguous lint source path {lint_path!r} at line {line}: {candidates}"
        )

    file_index = candidate_files[0]
    position = bisect_right(file_index.starts, line) - 1
    if position < 0:
        return None
    item = file_index.ranges[position]
    return item if line <= item.end else None


def parse_lint_csv(
    csv_path: Path,
    ranges: list[ModuleRange],
) -> list[MappedLint]:
    mapped: list[MappedLint] = []
    range_index = build_module_range_index(ranges)

    with csv_path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.DictReader(handle)
        normalized_fields = tuple(
            str(field or "").strip().lstrip("\ufeff").lower()
            for field in reader.fieldnames or []
        )
        if normalized_fields not in (
            NORMALIZED_LINT_INPUT_COLUMNS,
            LEGACY_LINT_INPUT_COLUMNS,
        ):
            raise ValueError(
                "Unsupported lint CSV header. Expected normalized violation_id schema "
                "or legacy Stage,MessageID,Severity,Contents,LineNo, schema."
            )
        for row_index, row in enumerate(reader, 1):
            if normalized_fields == NORMALIZED_LINT_INPUT_COLUMNS:
                source_path = str(row.get("file_path", "") or "").strip()
                try:
                    source_line = int(str(row.get("line_number", "") or "").strip())
                except ValueError as exc:
                    raise ValueError(
                        f"{csv_path}:{row_index + 1}: invalid line_number"
                    ) from exc
                message_id = str(row.get("message_id", "") or "")
                severity = str(row.get("severity", "") or "")
                contents = str(row.get("description", "") or "")
            else:
                position = str(row.get("LineNo", "") or "").strip()
                pos_match = POSITION_RE.fullmatch(position)
                contents = str(row.get("Contents", "") or "")
                if not pos_match and not position:
                    pos_match = EMBEDDED_POSITION_RE.search(contents)
                    if pos_match:
                        contents = contents[: pos_match.start()].rstrip()
                if not pos_match:
                    raise ValueError(
                        f"{csv_path}:{row_index + 1}: invalid LineNo: {position!r}"
                    )
                source_path = pos_match.group("path")
                source_line = int(pos_match.group("line"))
                message_id = str(row.get("MessageID", "") or "")
                severity = str(row.get("Severity", "") or "")

            if not source_path or source_line <= 0:
                raise ValueError(
                    f"{csv_path}:{row_index + 1}: source path and line must be present"
                )
            located = locate_module_for_line(range_index, source_path, source_line)
            if located is None:
                raise FilelistRecoveryError(
                    f"{csv_path}:{row_index + 1}: cannot map "
                    f"{source_path}({source_line}) to a source module"
                )
            source_file = located.analysis_file
            source_module = located.module

            mapped.append(
                MappedLint(
                    vio_id=format_violation_id(row_index),
                    message_id=message_id,
                    severity=severity,
                    source_file=source_file,
                    source_line=source_line,
                    source_module=source_module,
                    hierarchy_paths=[],
                    status=MODULE_SCOPE_STATUS,
                    contents=contents,
                )
            )
    return mapped


def apply_hierarchy_mapping(
    mapped: list[MappedLint],
    module_to_paths: dict[str, list[str]],
) -> None:
    for item in mapped:
        candidate_paths = module_to_paths.get(item.source_module, [])
        item.status = (
            IN_HIERARCHY_STATUS if candidate_paths else STANDALONE_STATUS
        )
        item.hierarchy_paths = reported_hierarchy_paths(
            item.contents,
            candidate_paths,
        )


def source_files_by_module(ranges: list[ModuleRange]) -> dict[str, list[str]]:
    source_by_module: dict[str, list[str]] = {}
    for item in ranges:
        source_file = item.analysis_file
        files = source_by_module.setdefault(item.module, [])
        if source_file not in files:
            files.append(source_file)
    return source_by_module


def build_source_metadata(ranges: list[ModuleRange]) -> dict:
    modules: dict[str, dict] = {}
    for module, source_files in source_files_by_module(ranges).items():
        metadata = {
            "source_file": source_files[0],
            "parameters": {},
            "ports": [],
            "child_instances": [],
        }
        if len(source_files) > 1:
            metadata["source_files"] = source_files
        modules[module] = metadata
    return {"schema_version": 1, "modules": modules}


def build_design_metadata(
    json_path: Path,
    ranges: list[ModuleRange],
) -> dict:
    design = json.loads(json_path.read_text(encoding="utf-8"))
    yosys_modules: dict[str, dict] = design["modules"]
    source_modules = {item.module for item in ranges}
    display_by_internal = {
        internal: module_display_name(internal, module, source_modules)
        for internal, module in yosys_modules.items()
    }
    modules = build_source_metadata(ranges)["modules"]
    enriched: set[str] = set()
    for internal, module in yosys_modules.items():
        display = display_by_internal[internal]
        if display not in modules or display in enriched:
            continue
        children = []
        for instance, cell in module.get("cells", {}).items():
            child_type = cell.get("type", "")
            child = display_by_internal.get(child_type, child_type)
            if child in modules:
                children.append({"instance": instance, "module": child})
        modules[display].update(
            {
                "parameters": module.get("parameter_default_values", {}),
                "ports": [
                    {
                        "name": name,
                        "direction": port["direction"],
                        "width": len(port.get("bits", [])),
                    }
                    for name, port in module.get("ports", {}).items()
                ],
                "child_instances": sorted(
                    children, key=lambda item: item["instance"]
                ),
            }
        )
        enriched.add(display)
    return {"schema_version": 1, "modules": modules}


def write_outputs(
    out_dir: Path,
    root: CellNode | None,
    mapped: list[MappedLint],
    design_metadata: dict,
    hierarchy_error: str | None,
    hierarchy_run: HierarchyRun | None,
    unresolved_modules: dict[str, int],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    tree_path = out_dir / "hierarchy_tree.txt"
    if root is None:
        tree_path.unlink(missing_ok=True)
    else:
        tree_path.write_text(render_tree(root) + "\n", encoding="utf-8")

    mapped_csv = out_dir / "lint_entries_mapped.csv"
    with mapped_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPED_LINT_COLUMNS)
        writer.writeheader()
        for item in mapped:
            writer.writerow(
                {
                    "vio_id": item.vio_id,
                    "status": item.status,
                    "hierarchy": ";".join(item.hierarchy_paths),
                    "source_module": item.source_module,
                    "source_file": item.source_file,
                    "source_line": item.source_line,
                    "message_id": item.message_id,
                    "severity": item.severity,
                    "contents": item.contents,
                }
            )

    metadata_path = out_dir / "design_metadata.json"
    metadata_path.write_text(
        json.dumps(design_metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    if root is None:
        status = hierarchy_module_status(
            (hierarchy_error or "hierarchy unavailable")[-4000:]
        )
    else:
        if hierarchy_run is None:
            raise ValueError("hierarchy run metadata is missing")
        status = hierarchy_tree_status(
            completeness=hierarchy_run.completeness,
            frontend=hierarchy_run.frontend,
            top=root.display_type,
            unresolved_modules=unresolved_modules,
        )
    status_path = out_dir / "hierarchy_status.json"
    status_path.write_text(
        json.dumps(status, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"HIERARCHY_STATUS={status_path}")
    if root is not None:
        print(f"HIERARCHY_TREE={tree_path}")
    print(f"MAPPED_LINT_CSV={mapped_csv}")
    print(f"DESIGN_METADATA={metadata_path}")


def main() -> int:
    args = parse_args()
    csv_path = args.csv.expanduser().resolve()
    source_root = args.source.expanduser().resolve()
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV path not found: {csv_path}")
    if not source_root.is_dir():
        raise FileNotFoundError(f"Staged source directory not found: {source_root}")

    out_dir = args.out_dir.expanduser().resolve()

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.keep_work:
            work_root = out_dir / "_work"
            if work_root.exists():
                shutil.rmtree(work_root)
            work_root.mkdir(parents=True, exist_ok=True)
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
            temp_dir = tempfile.TemporaryDirectory(prefix=".mapper_", dir=out_dir)
            work_root = Path(temp_dir.name)

        try:
            inputs, files = collect_verilog_inputs(source_root)
        except (FileNotFoundError, ValueError) as exc:
            raise FilelistRecoveryError(
                f"normalized filelist cannot select the design inputs: {exc}"
            ) from exc

        top = args.top
        if not HDL_IDENTIFIER_RE.fullmatch(top):
            raise ValueError(f"unsupported top module identifier: {top!r}")
        ranges = parse_module_ranges(files, source_root / "rtl")
        source_modules = {item.module for item in ranges}
        if top not in source_modules:
            raise FilelistRecoveryError(
                f"top module is absent from source inventory: {top}"
            )
        mapped = parse_lint_csv(csv_path, ranges)
        hierarchy_error: str | None = None
        hierarchy_run: HierarchyRun | None = None
        unresolved_modules: dict[str, int] = {}
        if args.module_only is not None:
            hierarchy_error = args.module_only
            root = None
            module_to_paths = None
            metadata = build_source_metadata(ranges)
        else:
            copy_runtime_data_files(source_root, work_root)
            try:
                yosys = find_yosys(
                    explicit_bin=str(args.yosys) if args.yosys else None,
                    start_points=[source_root],
                )
                hierarchy_run = run_yosys(
                    yosys,
                    inputs,
                    top,
                    work_root,
                )
                hierarchy_json = hierarchy_run.json_path
                root, module_to_paths, unresolved_modules = build_hierarchy(
                    hierarchy_json, source_modules
                )
            except (
                OSError,
                RuntimeError,
                ValueError,
                json.JSONDecodeError,
                KeyError,
            ) as exc:
                if args.require_hierarchy:
                    raise
                print(
                    "warning: hierarchy generation failed; using "
                    f"source-module fallback: {exc}",
                    file=sys.stderr,
                )
                hierarchy_error = str(exc)
                root = None
                module_to_paths = None
                metadata = build_source_metadata(ranges)
            else:
                metadata = build_design_metadata(hierarchy_json, ranges)
                apply_hierarchy_mapping(mapped, module_to_paths)

        write_outputs(
            out_dir,
            root,
            mapped,
            metadata,
            hierarchy_error,
            hierarchy_run,
            unresolved_modules,
        )

        if args.keep_work:
            print(f"  work_dir: {work_root}")
        return 0
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FilelistRecoveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(FILELIST_RECOVERY_EXIT_CODE)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
