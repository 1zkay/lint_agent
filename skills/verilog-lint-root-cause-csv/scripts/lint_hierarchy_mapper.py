#!/usr/bin/env python3
"""Map lint CSV entries and RTL metadata onto a Yosys-elaborated hierarchy."""

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
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from _contract import MAPPED_LINT_COLUMNS, format_violation_id
from _filelist import SOURCE_SUFFIXES, choose_filelist, parse_filelist, unique_paths

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
SRC_LINE_RE = re.compile(r":(?P<line>\d+)\.")
MODULE_RE = re.compile(r"\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b")
ENDMODULE_RE = re.compile(r"\s*endmodule\b")
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
SV_FRONTEND_FALLBACK_ERROR = "Invalid nesting of always blocks and/or initializations."


@dataclass(frozen=True)
class ModuleRange:
    module: str
    file: Path
    analysis_file: str
    start: int
    end: int


@dataclass
class CellNode:
    path: tuple[str, ...]
    display_type: str
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
    instance_paths: list[str]
    status: str
    contents: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a Yosys hierarchy tree from a staged Verilog project and map "
            "lint CSV file(line) reports onto instance paths."
        )
    )
    parser.add_argument("--csv", type=Path, required=True, help="Lint report CSV path.")
    parser.add_argument("--source", type=Path, required=True, help="Staged project directory.")
    parser.add_argument("--top", help="Top module. Defaults to Yosys hierarchy -auto-top.")
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
) -> tuple[list[Path], list[Path], list[str], str | None]:
    analysis_root = (source_root / "rtl").resolve()
    filelist = choose_filelist(source_root)
    if filelist:
        parsed = parse_filelist(filelist)
        files = parsed.files
        filelist_dirs = [
            path
            for path in parsed.filelist_dirs
            if path == analysis_root or analysis_root in path.parents
        ]
        incdirs = [*parsed.incdirs, *filelist_dirs]
        defines = parsed.defines
        filelist_top = parsed.top
    else:
        files = unique_paths(
            p
            for p in source_root.rglob("*")
            if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES
        )
        incdirs = []
        defines = []
        filelist_top = None

    if not files:
        raise FileNotFoundError(f"No Verilog source files found under {source_root}")

    for source_file in files:
        try:
            source_file.relative_to(analysis_root)
        except ValueError as exc:
            raise ValueError(
                f"filelist source is outside staged rtl/: {source_file}"
            ) from exc
    for incdir in incdirs:
        try:
            incdir.relative_to(analysis_root)
        except ValueError as exc:
            raise ValueError(
                f"filelist include directory is outside staged rtl/: {incdir}"
            ) from exc

    incdirs = unique_paths([*incdirs, *(path.parent for path in files)])
    return files, incdirs, defines, filelist_top


def yosys_quote(value: str | Path) -> str:
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_yosys(
    yosys: YosysLocation,
    files: list[Path],
    incdirs: list[Path],
    defines: list[str],
    top: str | None,
    work_root: Path,
) -> tuple[Path, Path]:
    all_modules_path = work_root / "all_modules.json"
    hierarchy_path = work_root / "hierarchy.json"
    script_path = work_root / "hierarchy.ys"
    build_dir = work_root / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    has_sv = any(p.suffix.lower() == ".sv" for p in files)

    cmd_parts = ["read_verilog"]
    if has_sv:
        cmd_parts.append("-sv")
    for incdir in incdirs:
        cmd_parts.extend(["-I", yosys_quote(incdir)])
    for define in defines:
        cmd_parts.append("-D" + define)
    cmd_parts.extend(yosys_quote(path) for path in files)

    hierarchy_cmd = f"hierarchy -check -top {top}" if top else "hierarchy -check -auto-top"

    def write_script(path: Path, read_command: list[str]) -> None:
        # hierarchy may derive new parameterized modules after the first proc pass.
        script = "\n".join(
            [
                " ".join(read_command),
                "proc",
                f"write_json {yosys_quote(all_modules_path)}",
                hierarchy_cmd,
                "proc",
                f"write_json {yosys_quote(hierarchy_path)}",
                "",
            ]
        )
        path.write_text(script, encoding="utf-8")

    def execute(path: Path, *, plugin: str | None = None) -> subprocess.CompletedProcess[str]:
        command = [str(yosys.bin)]
        if plugin:
            command.extend(["-m", plugin])
        command.extend(["-q", "-s", str(path)])
        return subprocess.run(
            command,
            cwd=build_dir,
            env=build_yosys_env(yosys),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    write_script(script_path, cmd_parts)
    result = execute(script_path)
    fallback_script_path: Path | None = None
    if (
        result.returncode != 0
        and has_sv
        and SV_FRONTEND_FALLBACK_ERROR in result.stderr
    ):
        (work_root / "yosys.read_verilog.stdout.log").write_text(
            result.stdout,
            encoding="utf-8",
            errors="replace",
        )
        (work_root / "yosys.read_verilog.stderr.log").write_text(
            result.stderr,
            encoding="utf-8",
            errors="replace",
        )
        slang_filelist_path = work_root / "hierarchy.slang.f"
        slang_filelist_parts: list[str] = []
        for incdir in incdirs:
            slang_filelist_parts.extend(["-I", yosys_quote(incdir)])
        for define in defines:
            slang_filelist_parts.extend(["-D", yosys_quote(define)])
        slang_filelist_parts.extend(yosys_quote(path) for path in files)
        slang_filelist_path.write_text(
            "\n".join(slang_filelist_parts) + "\n",
            encoding="utf-8",
        )
        fallback_script_path = work_root / "hierarchy.slang.ys"
        write_script(
            fallback_script_path,
            ["read_slang", "--keep-hierarchy", "-f", "../hierarchy.slang.f"],
        )
        result = execute(fallback_script_path, plugin="slang")

    (work_root / "yosys.stdout.log").write_text(result.stdout, encoding="utf-8", errors="replace")
    (work_root / "yosys.stderr.log").write_text(result.stderr, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        if fallback_script_path:
            raise RuntimeError(
                "Yosys failed after SystemVerilog frontend fallback. See logs:\n"
                f"  read_verilog script: {script_path}\n"
                f"  read_verilog stdout: {work_root / 'yosys.read_verilog.stdout.log'}\n"
                f"  read_verilog stderr: {work_root / 'yosys.read_verilog.stderr.log'}\n"
                f"  read_slang script: {fallback_script_path}\n"
                f"  read_slang stdout: {work_root / 'yosys.stdout.log'}\n"
                f"  read_slang stderr: {work_root / 'yosys.stderr.log'}\n"
                f"read_slang stderr tail:\n{result.stderr[-2000:]}"
            )
        raise RuntimeError(
            "Yosys failed. See logs:\n"
            f"  script: {script_path}\n"
            f"  stdout: {work_root / 'yosys.stdout.log'}\n"
            f"  stderr: {work_root / 'yosys.stderr.log'}\n"
            f"stderr tail:\n{result.stderr[-2000:]}"
        )
    return hierarchy_path, all_modules_path


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
) -> tuple[CellNode, dict[str, list[str]]]:
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

    def make_node(internal: str, path: tuple[str, ...]) -> CellNode:
        display_type = display_by_internal[internal]
        node = CellNode(
            path=path,
            display_type=display_type,
        )
        path_text = node.display_path
        module_to_paths[display_type].append(path_text)

        cells = modules[internal].get("cells", {})
        child_items = [
            (src_line(cell), cell_name, cell)
            for cell_name, cell in cells.items()
            if cell.get("type") in module_names
        ]
        for _, cell_name, cell in sorted(child_items, key=lambda item: (item[0], item[1])):
            child_internal = cell["type"]
            child = make_node(child_internal, (*path, cell_name))
            node.children.append(child)
        return node

    root = make_node(top_internal, (top_display,))
    return root, dict(module_to_paths)


def render_tree(root: CellNode, direct_counts: Counter[str] | None = None) -> str:
    direct_counts = direct_counts or Counter()
    subtree_counts: Counter[str] = Counter()

    def fill_counts(node: CellNode) -> int:
        total = direct_counts[node.display_path]
        for child in node.children:
            total += fill_counts(child)
        subtree_counts[node.display_path] = total
        return total

    fill_counts(root)

    lines: list[str] = []

    def label(node: CellNode) -> str:
        count_text = (
            f" [lint={direct_counts[node.display_path]}, "
            f"subtree={subtree_counts[node.display_path]}]"
        )
        return f"{node.path[-1]} : {node.display_type}{count_text}"

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
        current: str | None = None
        start = 0
        lines = file_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        for line_no, line in enumerate(lines, 1):
            if current is None:
                match = MODULE_RE.match(line)
                if match:
                    current = match.group(1)
                    start = line_no
            elif ENDMODULE_RE.match(line):
                analysis_file = (
                    file_path.resolve().relative_to(rtl_root.resolve()).as_posix()
                )
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


def locate_module_for_line(
    ranges: list[ModuleRange],
    lint_path: str,
    line: int,
) -> ModuleRange | None:
    norm = normalize_lint_file(lint_path)
    basename = Path(norm).name
    line_hits = [
        item
        for item in ranges
        if item.start <= line <= item.end
    ]

    def path_matches(candidate: str) -> bool:
        return (
            norm == candidate
            or norm.endswith(f"/{candidate}")
            or candidate.endswith(f"/{norm}")
        )

    exact_hits = [
        item
        for item in line_hits
        if path_matches(item.analysis_file)
        or norm == item.file.resolve().as_posix()
    ]
    hits = exact_hits or [item for item in line_hits if item.file.name == basename]
    if not hits:
        return None

    matching_files = {item.file.resolve() for item in hits}
    if len(matching_files) > 1:
        candidates = ", ".join(sorted(item.as_posix() for item in matching_files))
        raise ValueError(
            f"ambiguous lint source path {lint_path!r} at line {line}: {candidates}"
        )
    return min(hits, key=lambda item: item.end - item.start)


def parse_lint_csv(
    csv_path: Path,
    ranges: list[ModuleRange],
    module_to_paths: dict[str, list[str]],
) -> list[MappedLint]:
    mapped: list[MappedLint] = []

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
            located = locate_module_for_line(ranges, source_path, source_line)
            if located is None:
                raise ValueError(
                    f"{csv_path}:{row_index + 1}: cannot map "
                    f"{source_path}({source_line}) to a source module"
                )
            source_file = located.analysis_file
            source_module = located.module

            instance_paths = list(module_to_paths.get(source_module, []))
            status = "in_hierarchy_tree" if instance_paths else "standalone_module_not_in_tree"

            mapped.append(
                MappedLint(
                    vio_id=format_violation_id(row_index),
                    message_id=message_id,
                    severity=severity,
                    source_file=source_file,
                    source_line=source_line,
                    source_module=source_module,
                    instance_paths=instance_paths,
                    status=status,
                    contents=contents,
                )
            )
    return mapped


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
    source_by_module: dict[str, str] = {}
    for item in ranges:
        source_file = item.analysis_file
        previous = source_by_module.setdefault(item.module, source_file)
        if previous != source_file:
            raise ValueError(f"Module {item.module} is defined in multiple source files")

    modules: dict[str, dict] = {}
    for internal, module in yosys_modules.items():
        display = display_by_internal[internal]
        if display not in source_by_module or display in modules:
            continue
        children = []
        for instance, cell in module.get("cells", {}).items():
            child_type = cell.get("type", "")
            child = display_by_internal.get(child_type, child_type)
            if child in source_by_module:
                children.append({"instance": instance, "module": child})
        modules[display] = {
            "source_file": source_by_module[display],
            "parameters": module.get("parameter_default_values", {}),
            "ports": [
                {
                    "name": name,
                    "direction": port["direction"],
                    "width": len(port.get("bits", [])),
                }
                for name, port in module.get("ports", {}).items()
            ],
            "child_instances": sorted(children, key=lambda item: item["instance"]),
        }
    return {"schema_version": 1, "modules": modules}


def write_outputs(
    out_dir: Path,
    root: CellNode,
    mapped: list[MappedLint],
    design_metadata: dict,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    direct_counts: Counter[str] = Counter()
    for item in mapped:
        if item.status == "in_hierarchy_tree":
            for instance_path in item.instance_paths:
                direct_counts[instance_path] += 1

    tree_text = render_tree(root, direct_counts)
    tree_path = out_dir / "hierarchy_tree.txt"
    tree_path.write_text(tree_text + "\n", encoding="utf-8")

    mapped_csv = out_dir / "lint_entries_mapped.csv"
    with mapped_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPED_LINT_COLUMNS)
        writer.writeheader()
        for item in mapped:
            writer.writerow(
                {
                    "vio_id": item.vio_id,
                    "status": item.status,
                    "hierarchy": ";".join(item.instance_paths),
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

        files, incdirs, defines, filelist_top = collect_verilog_inputs(source_root)
        copy_runtime_data_files(source_root, work_root)
        yosys = find_yosys(
            explicit_bin=str(args.yosys) if args.yosys else None,
            start_points=[source_root],
        )
        hierarchy_json, all_modules_json = run_yosys(
            yosys,
            files,
            incdirs,
            defines,
            args.top or filelist_top,
            work_root,
        )
        ranges = parse_module_ranges(files, source_root / "rtl")
        source_modules = {item.module for item in ranges}
        root, module_to_paths = build_hierarchy(hierarchy_json, source_modules)
        mapped = parse_lint_csv(csv_path, ranges, module_to_paths)
        metadata = build_design_metadata(all_modules_json, ranges)
        write_outputs(out_dir, root, mapped, metadata)

        if args.keep_work:
            print(f"  work_dir: {work_root}")
        return 0
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
