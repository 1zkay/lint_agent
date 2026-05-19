#!/usr/bin/env python3
"""层次化 Verilog 常量传播根因分析工具。

核心目标：
1. 不再只分析顶层；支持父/子模块多层嵌套的层次化常量传播。
2. 定位“最源头常量引脚/线网”。
3. 给出每个根源常量对应的污染常量集合（跨层次）。
4. 对时序单元保守处理：默认不跨寄存器/触发器继续传播，避免把寄存器输出误报为常量。

说明：
- 该工具依赖 Yosys 导出 normal JSON 与 noopt RTLIL 两种语义视图。
- normal JSON 用于识别最终常量事实和跨层次传播；noopt RTLIL 用于保留直接常量根和模块内部组合依赖。
- normal 已确认的层次边界常量会作为 noopt 归因种子；noopt 只在 normal 同值校验通过后沿实例端口传递根因，不新增最终常量事实。
- 报告重点是“根源常量 -> 层次污染集合”，而不是仅仅对比顶层优化前后差异。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple, Union

Bit = Union[int, str]
CONST_BITS = {"0", "1"}
HDL_SOURCE_SUFFIXES = {".v", ".sv"}
YOSYS_SRC_RANGE_RE = re.compile(
    r"^(?P<file>.*):(?P<start_line>\d+)(?:\.(?P<start_col>\d+))?"
    r"-(?P<end_line>\d+)(?:\.(?P<end_col>\d+))?"
)

COMB_CELL_TYPES = {
    "$and",
    "$nand",
    "$logic_and",
    "$or",
    "$nor",
    "$logic_or",
    "$xor",
    "$xnor",
    "$not",
    "$logic_not",
    "$buf",
    "$mux",
    "$pmux",
    "$reduce_and",
    "$reduce_or",
    "$reduce_xor",
    "$reduce_xnor",
    "$eq",
    "$ne",
    "$logic_eq",
    "$logic_ne",
    # 常见 techmapped / primitive 形式
    "$_AND_",
    "$_NAND_",
    "$_OR_",
    "$_NOR_",
    "$_XOR_",
    "$_XNOR_",
    "$_NOT_",
    "$_BUF_",
    "and",
    "nand",
    "or",
    "nor",
    "xor",
    "xnor",
    "not",
    "buf",
}

SEQUENTIAL_TYPE_PATTERNS = (
    re.compile(r"\$(?:a|d|sd|ad|dl|sr)?ff", re.IGNORECASE),
    re.compile(r"\$mem", re.IGNORECASE),
    re.compile(r"\$latch", re.IGNORECASE),
)

RTLIL_MODULE_RE = re.compile(r"^\s*module\s+(?P<name>\\\S+|\S+)")
RTLIL_BLOCK_START_RE = re.compile(r"^\s*(?:cell|process|memory)\s+")
RTLIL_CONNECT_RE = re.compile(
    r"^\s*connect\s+(?P<dst>\\\S+|\S+)\s+(?P<src>\\\S+|\$\S+|\d+'[01xzXZ]+|[01xzXZ])\s*$"
)
RTLIL_CONST_RE = re.compile(r"^(?P<width>\d+)'(?:b)?(?P<bits>[01xzXZ]+)$")


@dataclass
class RootCause:
    root_id: str
    hierarchical_signal: str
    local_signal: str
    constant_value: str
    source_type: str
    location: str
    aliases: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass
class ConstEvidence:
    value: str
    root_ids: Set[str] = field(default_factory=set)


@dataclass
class SignalConstRecord:
    hierarchical_signal: str
    module: str
    signal_kind: str
    constant_value: str
    aliases: List[str] = field(default_factory=list)
    root_ids: List[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class ModuleIndex:
    module_name: str
    port_directions: Dict[str, str]
    name_to_bits: Dict[str, List[Bit]]
    name_to_bit: Dict[str, Bit]
    bit_to_names: Dict[Bit, List[str]]
    bit_roles: Dict[Bit, Set[str]]
    cells: Dict[str, Dict]


@dataclass
class InstanceContext:
    module_name: str
    path: Tuple[str, ...]
    children: Dict[str, "InstanceContext"] = field(default_factory=dict)

    @property
    def path_str(self) -> str:
        return ".".join(self.path)


class ConstantTracer:
    """层次化常量传播分析器。"""

    def __init__(
        self,
        design_inputs: Union[str, List[str]],
        top_module: str = "top_module",
        yosys_bin: Optional[str] = None,
    ):
        if isinstance(design_inputs, str):
            design_inputs = [design_inputs]
        self.design_inputs = [str(Path(item).resolve()) for item in design_inputs]
        self.top_module = top_module
        self.yosys_bin = self._find_yosys(yosys_bin)
        self.design_files = self._collect_design_files()
        self.design_catalog = self._build_design_catalog()
        self.selected_files = list(self.design_files)
        self.primary_input = (
            str(self.selected_files[0]) if self.selected_files else self.design_inputs[0]
        )

        self.netlist_data: Dict = {}
        self.modules_data: Dict[str, Dict] = {}
        self.module_indices: Dict[str, ModuleIndex] = {}
        self.prepared_source_map: Dict[str, str] = {}
        self.raw_rtlil_text: str = ""
        self.noopt_rtlil_text: str = ""
        self.direct_constant_connections: Dict[str, Dict[str, str]] = {}
        self.direct_constant_connection_sources: Dict[str, Dict[str, str]] = {}
        self.noopt_modules: Dict[str, Dict] = {}
        self.root_context: Optional[InstanceContext] = None
        self.all_contexts_preorder: List[InstanceContext] = []
        self.all_contexts_postorder: List[InstanceContext] = []

        self.root_causes: Dict[str, RootCause] = {}
        self.context_signal_direct_roots: Dict[str, Dict[str, Set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self.const_map: Dict[Tuple[str, Bit], ConstEvidence] = {}
        self.reason_map: Dict[Tuple[str, Bit], str] = {}
        self.noopt_const_map: Dict[Tuple[str, str], ConstEvidence] = {}
        self.noopt_reason_map: Dict[Tuple[str, str], str] = {}
        self.conflicts: List[str] = []
        self.yosys_stat: str = ""

    # ------------------------------------------------------------------
    # 基础准备
    # ------------------------------------------------------------------
    def _find_yosys(self, explicit: Optional[str]) -> str:
        if explicit:
            return explicit

        env_override = os.environ.get("YOSYS_BIN")
        if env_override:
            return env_override

        env_yosys = shutil.which("yosys")
        if env_yosys:
            return env_yosys

        verilog_path = Path(self.design_inputs[0]).resolve()
        candidates = []
        for parent in [verilog_path.parent, *verilog_path.parents]:
            candidates.append(parent / "oss-cad-suite" / "bin" / "yosys.exe")
            candidates.append(parent / "oss-cad-suite" / "bin" / "yosys")

        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

        raise FileNotFoundError(
            "未找到 yosys。请使用 --yosys 指定路径，或设置环境变量 YOSYS_BIN，"
            "也可以将 yosys 加入 PATH，或在工程附近放置 oss-cad-suite。"
        )

    def _collect_design_files(self) -> List[Path]:
        files: List[Path] = []
        seen: Set[Path] = set()

        for item in self.design_inputs:
            path = Path(item).resolve()
            if path.is_file():
                if path.suffix.lower() in HDL_SOURCE_SUFFIXES and path not in seen:
                    files.append(path)
                    seen.add(path)
                continue

            if path.is_dir():
                candidates = (
                    file_path
                    for suffix in sorted(HDL_SOURCE_SUFFIXES)
                    for file_path in path.rglob(f"*{suffix}")
                )
                for file_path in sorted(candidates):
                    resolved = file_path.resolve()
                    if resolved not in seen:
                        files.append(resolved)
                        seen.add(resolved)
                continue

            raise FileNotFoundError(f"Input path does not exist: {path}")

        if not files:
            raise FileNotFoundError("No Verilog/SystemVerilog source files were found.")
        return files

    def _build_design_catalog(self) -> Dict:
        file_texts: Dict[Path, str] = {}

        for path in self.design_files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            file_texts[path] = text

        return {"file_texts": file_texts}

    def _original_source_path(self, path_text: str) -> Path:
        normalized = Path(path_text).resolve().as_posix()
        for prepared_path, original_path in self.prepared_source_map.items():
            if normalized == prepared_path:
                return Path(original_path)
        return Path(path_text).resolve()

    def _source_slice_from_yosys_src(self, src: str) -> Tuple[Optional[Path], str]:
        if not src:
            return None, ""

        first_src = src.split("|", 1)[0].strip()
        match = YOSYS_SRC_RANGE_RE.match(first_src)
        if not match:
            return None, ""

        file_path = self._original_source_path(match.group("file"))
        file_text = self.design_catalog["file_texts"].get(file_path)
        if file_text is None:
            return file_path, ""

        start_line = int(match.group("start_line"))
        end_line = int(match.group("end_line"))
        start_col = int(match.group("start_col") or 1)
        end_col = int(match.group("end_col") or 0)
        lines = file_text.splitlines(keepends=True)
        if start_line < 1 or end_line < start_line or start_line > len(lines):
            return file_path, ""

        end_line = min(end_line, len(lines))
        selected = lines[start_line - 1 : end_line]
        if not selected:
            return file_path, ""

        selected[0] = selected[0][max(start_col - 1, 0) :]
        if end_col > 0 and len(selected) == 1:
            selected[0] = selected[0][: max(end_col - start_col + 1, 0)]
        elif end_col > 0:
            selected[-1] = selected[-1][:end_col]
        return file_path, "".join(selected)

    def _original_yosys_src(self, src: str) -> str:
        if not src:
            return ""

        converted_ranges: List[str] = []
        for src_part in src.split("|"):
            src_part = src_part.strip()
            match = YOSYS_SRC_RANGE_RE.match(src_part)
            if not match:
                converted_ranges.append(src_part)
                continue

            file_path = self._original_source_path(match.group("file"))
            start_line = match.group("start_line")
            start_col = match.group("start_col")
            end_line = match.group("end_line")
            end_col = match.group("end_col")
            start = f"{start_line}.{start_col}" if start_col else start_line
            end = f"{end_line}.{end_col}" if end_col else end_line
            converted_ranges.append(f"{file_path}:{start}-{end}")
        return "|".join(converted_ranges)

    @staticmethod
    def _yosys_path(path: Union[str, Path]) -> str:
        return Path(path).resolve().as_posix()

    @staticmethod
    def _rtlil_name_to_plain(name: str) -> str:
        name = name.strip()
        if name.startswith("\\"):
            return name[1:]
        return name

    @staticmethod
    def _split_rtlil_connect_operands(text: str) -> Tuple[str, str]:
        text = text.strip()
        if text.startswith("connect"):
            text = text[len("connect") :].strip()

        if text.startswith("{"):
            depth = 0
            for index, char in enumerate(text):
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        return text[: index + 1].strip(), text[index + 1 :].strip()
            return text, ""

        parts = text.split(None, 1)
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], parts[1].strip()

    @staticmethod
    def _rtlil_const_to_bit_values(value: str) -> Optional[List[str]]:
        match = RTLIL_CONST_RE.match(value)
        if not match:
            return None

        width = int(match.group("width"))
        bit_text = match.group("bits").lower()
        if any(bit not in CONST_BITS for bit in bit_text):
            return None

        if len(bit_text) < width:
            bit_text = bit_text.rjust(width, "0")
        elif len(bit_text) > width:
            bit_text = bit_text[-width:]

        return list(reversed(bit_text))

    @staticmethod
    def _const_to_str(bit: Bit) -> str:
        return f"1'b{bit}"

    @staticmethod
    def _source_type_label(source_type: str) -> str:
        labels = {
            "direct_assign": "直接赋值",
            "parameter": "参数常量",
            "reset_value": "复位赋值",
            "yosys_constant_connection": "Yosys常量连接",
            "literal_connection": "字面量端口连接",
            "port_connection": "端口常量连接",
            "inferred_constant": "推导得到的常量源",
        }
        return labels.get(source_type, source_type)

    def _run_yosys(self, script: str, timeout: int = 60) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        yosys_path = Path(self.yosys_bin).resolve()
        if yosys_path.parent.name.lower() == "bin":
            suite_root = yosys_path.parent.parent
            extra_paths = [str(yosys_path.parent), str(suite_root / "lib")]
            env["PATH"] = os.pathsep.join(extra_paths + [env.get("PATH", "")])
            env.setdefault("YOSYSHQ_ROOT", str(suite_root))

        result = subprocess.run(
            [self.yosys_bin, "-p", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result

    def _index_noopt_direct_constant_connections(self) -> None:
        direct_constants: Dict[str, Dict[str, str]] = defaultdict(dict)
        direct_sources: Dict[str, Dict[str, str]] = defaultdict(dict)

        for module_name, module_data in self.noopt_modules.items():
            for dst_tokens, src_tokens in module_data.get("connects", []):
                if not dst_tokens or not src_tokens:
                    continue

                src_values = [token.lower() for token in src_tokens]
                if any(value not in CONST_BITS for value in src_values):
                    continue

                if len(dst_tokens) == 1:
                    dst_name = dst_tokens[0]
                    if dst_name.lower() in CONST_BITS or dst_name.startswith("$"):
                        continue
                    direct_constants[module_name][dst_name] = self._format_const_value(src_values)
                    direct_sources[module_name][dst_name] = dst_name
                    continue

                if len(dst_tokens) != len(src_values):
                    continue

                for dst_name, bit_value in zip(dst_tokens, src_values):
                    if dst_name.lower() in CONST_BITS or dst_name.startswith("$"):
                        continue
                    direct_constants[module_name][dst_name] = self._format_const_value([bit_value])
                    direct_sources[module_name][dst_name] = dst_name

        self.direct_constant_connections = {
            module: dict(items) for module, items in direct_constants.items()
        }
        self.direct_constant_connection_sources = {
            module: dict(items) for module, items in direct_sources.items()
        }

    def _rtlil_parse_sigspec(self, text: str) -> List[str]:
        tokens = re.findall(
            r"\d+'[01xzXZ]+|\\\S+|\$\S+|[01xzXZ]|-?\d+",
            text,
        )
        parsed: List[str] = []
        for token in tokens:
            token = token.strip().strip("{}")
            if not token:
                continue

            bit_values = self._rtlil_const_to_bit_values(token)
            if bit_values is not None:
                parsed.extend(bit_values)
                continue

            lowered = token.lower()
            if lowered in CONST_BITS:
                parsed.append(lowered)
                continue

            parsed.append(self._rtlil_name_to_plain(token))
        return parsed

    def _parse_noopt_rtlil_modules(self, rtlil_text: str) -> Dict[str, Dict]:
        modules: Dict[str, Dict] = defaultdict(lambda: {"cells": [], "connects": []})
        current_module: Optional[str] = None
        current_cell: Optional[Dict] = None
        skip_block_depth = 0
        pending_src = ""

        cell_re = re.compile(r"^\s*cell\s+(?P<type>\\\S+|\S+)\s+(?P<name>\\\S+|\S+)")
        src_attr_re = re.compile(r'^\s*attribute\s+\\src\s+"(?P<src>.*)"\s*$')

        for line in rtlil_text.splitlines():
            module_match = RTLIL_MODULE_RE.match(line)
            if module_match:
                current_module = self._rtlil_name_to_plain(module_match.group("name"))
                current_cell = None
                skip_block_depth = 0
                pending_src = ""
                continue

            if current_module is None:
                continue

            stripped = line.strip()
            if not stripped:
                continue

            if current_cell is not None:
                if stripped == "end":
                    modules[current_module]["cells"].append(current_cell)
                    current_cell = None
                    continue
                if stripped.startswith("connect "):
                    lhs, rhs = ConstantTracer._split_rtlil_connect_operands(stripped)
                    port_names = ConstantTracer._rtlil_parse_sigspec(self, lhs)
                    if len(port_names) != 1:
                        continue
                    current_cell["connections"][port_names[0]] = (
                        ConstantTracer._rtlil_parse_sigspec(self, rhs)
                    )
                continue

            if skip_block_depth:
                if stripped == "end":
                    skip_block_depth -= 1
                elif RTLIL_BLOCK_START_RE.match(line):
                    skip_block_depth += 1
                continue

            if stripped == "end":
                current_module = None
                continue

            src_attr_match = src_attr_re.match(line)
            if src_attr_match:
                pending_src = src_attr_match.group("src")
                continue

            cell_match = cell_re.match(line)
            if cell_match:
                current_cell = {
                    "type": self._rtlil_name_to_plain(cell_match.group("type")),
                    "name": self._rtlil_name_to_plain(cell_match.group("name")),
                    "src": self._original_yosys_src(pending_src),
                    "connections": {},
                }
                pending_src = ""
                continue

            if re.match(r"^\s*(?:process|memory)\s+", line):
                skip_block_depth = 1
                continue

            if stripped.startswith("connect "):
                lhs, rhs = ConstantTracer._split_rtlil_connect_operands(stripped)
                modules[current_module]["connects"].append(
                    (
                        ConstantTracer._rtlil_parse_sigspec(self, lhs),
                        ConstantTracer._rtlil_parse_sigspec(self, rhs),
                    )
                )

        return {module: dict(data) for module, data in modules.items()}

    def _read_verilog_cmd(self, prepared_files: List[Path], noopt: bool = False) -> str:
        include_dirs = sorted({str(path.parent) for path in self.selected_files})
        include_flags = " ".join(f"-I {self._yosys_path(path)}" for path in include_dirs)
        file_args = " ".join(self._yosys_path(path) for path in prepared_files)
        sv_flag = "-sv " if any(path.suffix.lower() == ".sv" for path in prepared_files) else ""
        noopt_flag = "-noopt " if noopt else ""
        flags = f"{noopt_flag}{sv_flag}{include_flags} " if include_flags else f"{noopt_flag}{sv_flag}"
        return f"read_verilog -defer {flags}{file_args}; "

    def _prepare_design_sources(self, workdir: Path) -> List[Path]:
        prepared_files: List[Path] = []
        for index, path in enumerate(self.selected_files):
            out_path = workdir / f"{index:03d}_{path.name}"
            out_path.write_text(
                self.design_catalog["file_texts"][path],
                encoding="utf-8",
            )
            prepared_files.append(out_path)
        return prepared_files

    def _export_design_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix="trace_yosys_hier_") as tempdir:
            json_path = Path(tempdir) / "design.json"
            noopt_rtlil_path = Path(tempdir) / "design_noopt.il"
            source_dir = Path(tempdir) / "sources"
            source_dir.mkdir(parents=True, exist_ok=True)
            prepared_files = self._prepare_design_sources(source_dir)
            self.prepared_source_map = {
                self._yosys_path(prepared): str(original)
                for prepared, original in zip(prepared_files, self.selected_files)
            }
            read_cmd = self._read_verilog_cmd(prepared_files)

            script = (
                f"{read_cmd}"
                f"hierarchy -check -top {self.top_module}; "
                "proc; "
                f"write_json {self._yosys_path(json_path)}; "
                "stat"
            )
            result = self._run_yosys(script, timeout=120)
            self.yosys_stat = result.stdout
            self.netlist_data = json.loads(json_path.read_text(encoding="utf-8"))
            self.modules_data = self.netlist_data.get("modules", {})

            noopt_script = (
                f"{self._read_verilog_cmd(prepared_files, noopt=True)}"
                f"hierarchy -check -top {self.top_module}; "
                "proc -noopt; "
                f"write_rtlil {self._yosys_path(noopt_rtlil_path)}"
            )
            self._run_yosys(noopt_script, timeout=120)
            self.noopt_rtlil_text = noopt_rtlil_path.read_text(
                encoding="utf-8",
                errors="ignore",
            )
            self.noopt_modules = self._parse_noopt_rtlil_modules(self.noopt_rtlil_text)
            self._index_noopt_direct_constant_connections()

    # ------------------------------------------------------------------
    # 模块/实例索引
    # ------------------------------------------------------------------
    def _build_module_indices(self) -> None:
        self.module_indices = {}
        for module_name, module_data in self.modules_data.items():
            name_to_bits: Dict[str, List[Bit]] = {}
            name_to_bit: Dict[str, Bit] = {}
            bit_to_names: Dict[Bit, List[str]] = defaultdict(list)
            bit_roles: Dict[Bit, Set[str]] = defaultdict(set)
            port_directions: Dict[str, str] = {}

            ports = module_data.get("ports", {})
            for port_name, port_data in ports.items():
                direction = port_data.get("direction", "")
                port_directions[port_name] = direction
                bits = port_data.get("bits", [])
                if not bits:
                    continue
                name_to_bits[port_name] = list(bits)
                for bit in bits:
                    bit_roles[bit].add(direction or "port")
                if len(bits) == 1:
                    bit = bits[0]
                    name_to_bit[port_name] = bit
                    bit_to_names[bit].append(port_name)

            for net_name, net_data in module_data.get("netnames", {}).items():
                bits = net_data.get("bits", [])
                if not bits:
                    continue
                name_to_bits[net_name] = list(bits)
                for bit in bits:
                    bit_roles[bit].add("wire")
                if len(bits) == 1:
                    bit = bits[0]
                    name_to_bit[net_name] = bit
                    bit_to_names[bit].append(net_name)

            self.module_indices[module_name] = ModuleIndex(
                module_name=module_name,
                port_directions=port_directions,
                name_to_bits=name_to_bits,
                name_to_bit=name_to_bit,
                bit_to_names=dict(bit_to_names),
                bit_roles=dict(bit_roles),
                cells=module_data.get("cells", {}),
            )

    def _build_context_tree(self) -> None:
        self.all_contexts_preorder = []
        self.all_contexts_postorder = []

        if self.top_module not in self.module_indices:
            available = ", ".join(sorted(self.module_indices)[:20])
            if len(self.module_indices) > 20:
                available += " ..."
            raise ValueError(
                f"Top module {self.top_module} was not present in the Yosys hierarchy. "
                f"Available modules: {available}"
            )

        def visit(
            module_name: str,
            path: Tuple[str, ...],
            ancestors: FrozenSet[str],
        ) -> InstanceContext:
            ctx = InstanceContext(module_name=module_name, path=path)
            self.all_contexts_preorder.append(ctx)

            next_ancestors = ancestors | {module_name}
            module_index = self.module_indices[module_name]
            for cell_name, cell_data in module_index.cells.items():
                cell_type = cell_data.get("type", "")
                if cell_type not in self.module_indices:
                    continue
                if cell_type in next_ancestors:
                    # 保守跳过递归层次
                    continue
                child_ctx = visit(cell_type, path + (cell_name,), next_ancestors)
                ctx.children[cell_name] = child_ctx

            self.all_contexts_postorder.append(ctx)
            return ctx

        self.root_context = visit(self.top_module, (self.top_module,), frozenset())

    # ------------------------------------------------------------------
    # 信号/根因辅助
    # ------------------------------------------------------------------
    def _node_key(self, ctx: InstanceContext, bit: Bit) -> Tuple[str, Bit]:
        return (ctx.path_str, bit)

    def _signal_src(self, module_name: str, signal_name: str) -> str:
        module_data = self.modules_data.get(module_name, {})
        for section in ("netnames", "ports"):
            signal_data = module_data.get(section, {}).get(signal_name)
            if signal_data:
                return signal_data.get("attributes", {}).get("src", "")
        return ""

    def _source_note_for_signal(self, module_name: str, signal_name: str) -> Optional[str]:
        src = self._signal_src(module_name, signal_name)
        if not src:
            return None

        _, snippet = self._source_slice_from_yosys_src(src)
        snippet = " ".join(snippet.split())
        original_src = self._original_yosys_src(src)
        if snippet:
            return f"Yosys src: {original_src}; source snippet: {snippet}"
        return f"Yosys src: {original_src}"

    def _json_literal_constant_reason(
        self,
        ctx: InstanceContext,
        signal_name: str,
        bits: List[Bit],
        const_value: str,
    ) -> str:
        if not bits or any(bit not in CONST_BITS for bit in bits):
            return ""

        signal = self._hier_signal(ctx, signal_name)
        reason = f"Yosys JSON binds {signal} to literal constant {const_value}."
        source_note = self._source_note_for_signal(ctx.module_name, signal_name)
        if source_note:
            return f"{reason} {source_note}"
        return reason

    @staticmethod
    def _format_const_value(bit_values: List[str]) -> str:
        if not bit_values:
            return "1'bx"
        if len(bit_values) == 1:
            return f"1'b{bit_values[0]}"
        return f"{len(bit_values)}'b{''.join(reversed(bit_values))}"

    def _public_signal_names(self, module_index: ModuleIndex) -> List[str]:
        names = [name for name in module_index.name_to_bits if not name.startswith("$")]
        return sorted(set(names), key=lambda x: ("." in x, "[" in x, len(x), x))

    def _public_names(self, module_index: ModuleIndex, bit: Bit) -> List[str]:
        names = [name for name in module_index.bit_to_names.get(bit, []) if not name.startswith("$")]
        return sorted(set(names), key=lambda x: ("." in x, len(x), x))

    def _role_of_signal_group(self, module_index: ModuleIndex, signal_names: List[str]) -> str:
        directions = [module_index.port_directions.get(name) for name in signal_names]
        if "output" in directions:
            return "output"
        if "input" in directions:
            return "input"
        if "inout" in directions:
            return "inout"
        return "wire"

    def _preferred_signal_name(self, module_index: ModuleIndex, signal_names: List[str]) -> str:
        def rank(name: str) -> Tuple[int, bool, int, str]:
            direction = module_index.port_directions.get(name)
            role_rank = 1
            if direction == "output":
                role_rank = 0
            elif direction in {"input", "inout"}:
                role_rank = 2
            return (role_rank, "[" in name or "." in name, len(name), name)

        return sorted(set(signal_names), key=rank)[0]

    def _preferred_local_name(self, module_index: ModuleIndex, bit: Bit) -> str:
        public_names = self._public_names(module_index, bit)
        if public_names:
            output_names = [n for n in public_names if module_index.port_directions.get(n) == "output"]
            input_names = [n for n in public_names if module_index.port_directions.get(n) == "input"]
            wire_names = [n for n in public_names if n not in output_names and n not in input_names]
            ordered = output_names + wire_names + input_names
            if ordered:
                return ordered[0]
        all_names = sorted(set(module_index.bit_to_names.get(bit, [])))
        return all_names[0] if all_names else str(bit)

    def _display_local_signal(self, local_name: str) -> str:
        display = local_name
        for prepared_path, original_path in sorted(
            self.prepared_source_map.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            display = display.replace(prepared_path, original_path)
        return display

    def _hier_signal(self, ctx: InstanceContext, local_name: str) -> str:
        return f"{ctx.path_str}.{self._display_local_signal(local_name)}"

    def _preferred_hier_signal(self, ctx: InstanceContext, bit: Bit) -> str:
        module_index = self.module_indices[ctx.module_name]
        return self._hier_signal(ctx, self._preferred_local_name(module_index, bit))

    def _signal_aliases_hier(self, ctx: InstanceContext, bits: List[Bit]) -> List[str]:
        if any(bit in CONST_BITS for bit in bits):
            return []

        module_index = self.module_indices[ctx.module_name]
        target = tuple(bits)
        aliases = [
            self._hier_signal(ctx, name)
            for name in self._public_signal_names(module_index)
            if tuple(module_index.name_to_bits.get(name, [])) == target
        ]
        return sorted(set(aliases))

    def _register_root(
        self,
        ctx: InstanceContext,
        local_signal: str,
        value: str,
        source_type: str,
        note: Optional[str] = None,
        location: Optional[str] = None,
    ) -> str:
        hierarchical_signal = self._hier_signal(ctx, local_signal)
        root_id = hierarchical_signal
        if root_id not in self.root_causes:
            module_index = self.module_indices[ctx.module_name]
            bits = module_index.name_to_bits.get(local_signal, [])
            aliases = self._signal_aliases_hier(ctx, bits) if bits else []
            if not aliases:
                aliases = [hierarchical_signal]
            self.root_causes[root_id] = RootCause(
                root_id=root_id,
                hierarchical_signal=hierarchical_signal,
                local_signal=local_signal,
                constant_value=value,
                source_type=source_type,
                location=location or f"信号: {hierarchical_signal}",
                aliases=aliases,
                notes=[note] if note else [],
            )
        elif note and note not in self.root_causes[root_id].notes:
            self.root_causes[root_id].notes.append(note)
        return root_id

    def _register_literal_root(
        self,
        ctx: InstanceContext,
        site: str,
        value: str,
    ) -> str:
        root_id = f"{site}<{value}>"
        if root_id not in self.root_causes:
            self.root_causes[root_id] = RootCause(
                root_id=root_id,
                hierarchical_signal=site,
                local_signal=site.split(".")[-1],
                constant_value=value,
                source_type="literal_connection",
                location=f"连接点: {site}",
                notes=["该根源来自字面量常量连接，而不是具名线网/端口。"],
            )
        return root_id

    def _signal_groups(self, module_index: ModuleIndex) -> List[Tuple[List[Bit], List[str]]]:
        grouped: Dict[Tuple[Bit, ...], List[str]] = defaultdict(list)
        constant_groups: List[Tuple[List[Bit], List[str]]] = []
        for name in self._public_signal_names(module_index):
            bits = module_index.name_to_bits.get(name, [])
            if not bits:
                continue
            if any(bit in CONST_BITS for bit in bits):
                constant_groups.append((list(bits), [name]))
                continue
            grouped[tuple(bits)].append(name)
        return [(list(bits), names) for bits, names in grouped.items()] + constant_groups

    def _seed_direct_constant_roots(self) -> None:
        for ctx in self.all_contexts_preorder:
            module_index = self.module_indices[ctx.module_name]
            constant_connections = self.direct_constant_connections.get(ctx.module_name, {})
            constant_connection_sources = self.direct_constant_connection_sources.get(
                ctx.module_name, {}
            )
            for bits, signal_names in self._signal_groups(module_index):
                if not bits or any(bit not in CONST_BITS for bit in bits):
                    continue
                const_value = self._format_const_value([str(bit) for bit in bits])
                for name in sorted(signal_names):
                    if constant_connections.get(name) != const_value:
                        continue

                    root_name = constant_connection_sources.get(name, name)
                    root_signal_name = (
                        name
                        if root_name.startswith("$")
                        or root_name not in module_index.name_to_bits
                        else root_name
                    )
                    hierarchical_signal = self._hier_signal(ctx, name)
                    hierarchical_root = self._hier_signal(ctx, root_name)
                    if root_name == name:
                        bind_note = (
                            f"Yosys noopt RTLIL direct connect binds {hierarchical_signal} "
                            f"to {const_value}."
                        )
                        location = (
                            f"Yosys noopt RTLIL direct constant connection: "
                            f"{hierarchical_signal} = {const_value}"
                        )
                    else:
                        bind_note = (
                            f"Yosys noopt RTLIL connect chain binds {hierarchical_signal} "
                            f"to {const_value} via {hierarchical_root}."
                        )
                        location = (
                            f"Yosys noopt RTLIL constant connection: "
                            f"{hierarchical_signal} -> {hierarchical_root} = {const_value}"
                        )

                    notes = [
                        bind_note
                    ]
                    source_note = self._source_note_for_signal(ctx.module_name, root_name)
                    if not source_note and root_name != name:
                        source_note = self._source_note_for_signal(ctx.module_name, name)
                    if source_note:
                        notes.append(source_note)

                    root_id = self._register_root(
                        ctx=ctx,
                        local_signal=root_signal_name,
                        value=const_value,
                        source_type="yosys_constant_connection",
                        note=" | ".join(notes),
                        location=location,
                    )
                    self.context_signal_direct_roots[ctx.path_str][name].add(root_id)
                    self.context_signal_direct_roots[ctx.path_str][root_signal_name].add(root_id)

    def _local_signal_from_site(self, ctx: InstanceContext, site: str) -> str:
        prefix = f"{ctx.path_str}."
        local = site[len(prefix):] if site.startswith(prefix) else site
        match = re.match(r"^(.*)\[\d+\]$", local)
        return match.group(1) if match else local

    def _literal_roots_for_context(
        self,
        ctx: InstanceContext,
        const_bit: str,
        site: str,
        create_literal_root: bool,
    ) -> Set[str]:
        local_signal = self._local_signal_from_site(ctx, site)
        roots = set(self.context_signal_direct_roots[ctx.path_str].get(local_signal, set()))
        if roots:
            return roots
        if not create_literal_root:
            return set()
        return {self._register_literal_root(ctx, site, self._const_to_str(const_bit))}

    def _get_state(
        self,
        ctx: InstanceContext,
        bit: Bit,
        site: str,
        create_literal_root: bool = False,
    ) -> Optional[ConstEvidence]:
        if bit in CONST_BITS:
            return ConstEvidence(
                value=str(bit),
                root_ids=self._literal_roots_for_context(
                    ctx,
                    str(bit),
                    site,
                    create_literal_root=create_literal_root,
                ),
            )
        return self.const_map.get(self._node_key(ctx, bit))

    def _resolve_signal_constant(
        self,
        ctx: InstanceContext,
        bits: List[Bit],
        site_prefix: str,
    ) -> Optional[Tuple[str, Set[str]]]:
        if not bits:
            return None

        bit_values: List[str] = []
        root_ids: Set[str] = set()
        for index, bit in enumerate(bits):
            evidence = self._get_state(
                ctx,
                bit,
                f"{site_prefix}[{index}]",
                create_literal_root=False,
            )
            if evidence is None:
                return None
            bit_values.append(evidence.value)
            root_ids |= set(evidence.root_ids)

        return self._format_const_value(bit_values), root_ids

    def _assign_const(
        self,
        ctx: InstanceContext,
        bit: Bit,
        value: str,
        root_ids: Iterable[str],
        reason: str,
    ) -> bool:
        if bit in CONST_BITS:
            return False
        key = self._node_key(ctx, bit)
        root_ids = set(root_ids)
        if not root_ids:
            return False

        current = self.const_map.get(key)
        if current is None:
            self.const_map[key] = ConstEvidence(value=value, root_ids=set(root_ids))
            self.reason_map[key] = reason
            return True

        if current.value != value:
            conflict_msg = (
                f"冲突: {self._preferred_hier_signal(ctx, bit)} 同时被推导为 "
                f"1'b{current.value} 和 1'b{value}。"
            )
            if conflict_msg not in self.conflicts:
                self.conflicts.append(conflict_msg)
            return False

        new_roots = set(current.root_ids) | set(root_ids)
        if new_roots != current.root_ids:
            current.root_ids = new_roots
            if reason not in self.reason_map.get(key, ""):
                self.reason_map[key] = self.reason_map.get(key, "") + " | " + reason
            return True
        return False

    @staticmethod
    def _merge_roots(*evidences: Optional[ConstEvidence]) -> Set[str]:
        merged: Set[str] = set()
        for evidence in evidences:
            if evidence:
                merged |= set(evidence.root_ids)
        return merged

    @staticmethod
    def _all_known(evidences: List[Optional[ConstEvidence]]) -> bool:
        return all(e is not None for e in evidences)

    @staticmethod
    def _is_sequential_cell_type(cell_type: str) -> bool:
        return any(pattern.search(cell_type) for pattern in SEQUENTIAL_TYPE_PATTERNS)

    @staticmethod
    def _normalize_cell_type(cell_type: str) -> str:
        mapping = {
            "$and": "and",
            "$logic_and": "and",
            "$_AND_": "and",
            "and": "and",
            "$nand": "nand",
            "$_NAND_": "nand",
            "nand": "nand",
            "$or": "or",
            "$logic_or": "or",
            "$_OR_": "or",
            "or": "or",
            "$nor": "nor",
            "$_NOR_": "nor",
            "nor": "nor",
            "$xor": "xor",
            "$_XOR_": "xor",
            "xor": "xor",
            "$xnor": "xnor",
            "$_XNOR_": "xnor",
            "xnor": "xnor",
            "$not": "not",
            "$logic_not": "not",
            "$_NOT_": "not",
            "not": "not",
            "$buf": "buf",
            "$_BUF_": "buf",
            "buf": "buf",
            "$mux": "mux",
            "$pmux": "pmux",
            "$reduce_and": "reduce_and",
            "$reduce_or": "reduce_or",
            "$reduce_xor": "reduce_xor",
            "$reduce_xnor": "reduce_xnor",
            "$eq": "eq",
            "$ne": "ne",
            "$logic_eq": "eq",
            "$logic_ne": "ne",
        }
        return mapping.get(cell_type, cell_type)

    def _noopt_key(self, ctx: InstanceContext, signal_name: str) -> Tuple[str, str]:
        return (ctx.path_str, signal_name)

    def _noopt_get_state(self, ctx: InstanceContext, token: str) -> Optional[ConstEvidence]:
        lowered = token.lower()
        if lowered in CONST_BITS:
            return ConstEvidence(value=lowered, root_ids=set())
        return self.noopt_const_map.get(self._noopt_key(ctx, token))

    def _noopt_assign_const(
        self,
        ctx: InstanceContext,
        signal_name: str,
        value: str,
        root_ids: Iterable[str],
        reason: str,
    ) -> bool:
        if not signal_name or signal_name.lower() in CONST_BITS:
            return False
        root_ids = set(root_ids)
        if not root_ids:
            return False

        key = self._noopt_key(ctx, signal_name)
        current = self.noopt_const_map.get(key)
        if current is None:
            self.noopt_const_map[key] = ConstEvidence(value=value, root_ids=set(root_ids))
            self.noopt_reason_map[key] = reason
            return True

        if current.value != value:
            conflict_msg = (
                f"冲突: {self._hier_signal(ctx, signal_name)} 在 noopt RTLIL 图中同时被推导为 "
                f"1'b{current.value} 和 1'b{value}。"
            )
            if conflict_msg not in self.conflicts:
                self.conflicts.append(conflict_msg)
            return False

        new_roots = set(current.root_ids) | set(root_ids)
        if new_roots != current.root_ids:
            current.root_ids = new_roots
            if reason not in self.noopt_reason_map.get(key, ""):
                self.noopt_reason_map[key] = self.noopt_reason_map.get(key, "") + " | " + reason
            return True
        return False

    def _seed_noopt_direct_roots(self) -> None:
        for ctx in self.all_contexts_preorder:
            direct_roots = self.context_signal_direct_roots.get(ctx.path_str, {})
            for signal_name, root_ids in direct_roots.items():
                if not root_ids:
                    continue
                root = self.root_causes.get(sorted(root_ids)[0])
                if root is None:
                    continue
                bit_values = self._rtlil_const_to_bit_values(root.constant_value)
                if not bit_values or len(bit_values) != 1:
                    continue
                self._noopt_assign_const(
                    ctx,
                    signal_name,
                    bit_values[0],
                    root_ids,
                    f"{self._hier_signal(ctx, signal_name)} 是直接常量根源",
                )

    def _noopt_boundary_seed_names(self, ctx: InstanceContext) -> Set[str]:
        module_index = self.module_indices[ctx.module_name]
        seed_names: Set[str] = set(module_index.port_directions)

        for cell_data in module_index.cells.values():
            if cell_data.get("type", "") not in self.module_indices:
                continue
            for bits in cell_data.get("connections", {}).values():
                for bit in bits:
                    if bit in CONST_BITS:
                        continue
                    seed_names.update(self._public_names(module_index, bit))

        return {
            name
            for name in seed_names
            if name in module_index.name_to_bits and not name.startswith("$")
        }

    def _seed_noopt_from_normal_boundary_constants(self) -> None:
        """Use normal hierarchy-proven constants only as noopt attribution seeds.

        normal JSON remains the source of truth for final constant facts.  These
        seeds only bridge hierarchy-boundary constants into the noopt module-local
        graph, so noopt can continue source-level attribution inside the module.
        """
        for ctx in self.all_contexts_preorder:
            module_index = self.module_indices[ctx.module_name]
            for signal_name in sorted(self._noopt_boundary_seed_names(ctx)):
                bits = module_index.name_to_bits.get(signal_name, [])
                if len(bits) != 1:
                    continue

                resolved = self._resolve_signal_constant(
                    ctx,
                    bits,
                    self._hier_signal(ctx, signal_name),
                )
                if resolved is None:
                    continue

                const_value, root_ids = resolved
                if not root_ids:
                    continue

                bit_values = self._rtlil_const_to_bit_values(const_value)
                if not bit_values or len(bit_values) != 1:
                    continue

                self._noopt_assign_const(
                    ctx,
                    signal_name,
                    bit_values[0],
                    root_ids,
                    (
                        f"normal JSON 已确认层次边界信号 "
                        f"{self._hier_signal(ctx, signal_name)} = {const_value}；"
                        "作为 noopt RTLIL 模块内归因种子"
                    ),
                )

    def _normal_confirms_noopt_state(
        self,
        ctx: InstanceContext,
        bits: List[Bit],
        signal_name: str,
        state: ConstEvidence,
    ) -> bool:
        resolved = self._resolve_signal_constant(
            ctx,
            bits,
            self._hier_signal(ctx, signal_name),
        )
        if resolved is None:
            return False

        const_value, _ = resolved
        return const_value == self._format_const_value([state.value])

    def _propagate_noopt_parent_to_children(self, ctx: InstanceContext) -> bool:
        changed = False
        module_index = self.module_indices[ctx.module_name]
        module = self.noopt_modules.get(ctx.module_name, {})
        noopt_cells = {
            cell.get("name", ""): cell
            for cell in module.get("cells", [])
            if cell.get("name")
        }

        for cell_name, cell_data in module_index.cells.items():
            child_ctx = ctx.children.get(cell_name)
            if child_ctx is None:
                continue
            noopt_cell = noopt_cells.get(cell_name)
            if not noopt_cell:
                continue

            child_index = self.module_indices[child_ctx.module_name]
            for port_name, direction in child_index.port_directions.items():
                if direction not in {"input", "inout"}:
                    continue

                parent_tokens = noopt_cell.get("connections", {}).get(port_name, [])
                child_bits = child_index.name_to_bits.get(port_name, [])
                if len(parent_tokens) != 1 or len(child_bits) != 1:
                    continue

                parent_state = self._noopt_get_state(ctx, parent_tokens[0])
                if parent_state is None or not parent_state.root_ids:
                    continue
                if not self._normal_confirms_noopt_state(
                    child_ctx,
                    child_bits,
                    port_name,
                    parent_state,
                ):
                    continue

                changed |= self._noopt_assign_const(
                    child_ctx,
                    port_name,
                    parent_state.value,
                    parent_state.root_ids,
                    (
                        f"normal JSON 已确认跨层次输入 "
                        f"{self._hier_signal(child_ctx, port_name)} = "
                        f"{self._format_const_value([parent_state.value])}；"
                        f"noopt RTLIL 从父实例 {ctx.path_str}.{cell_name}.{port_name} "
                        "传入根因"
                    ),
                )

        return changed

    def _propagate_noopt_child_to_parent(self, ctx: InstanceContext) -> bool:
        changed = False
        module_index = self.module_indices[ctx.module_name]
        module = self.noopt_modules.get(ctx.module_name, {})
        noopt_cells = {
            cell.get("name", ""): cell
            for cell in module.get("cells", [])
            if cell.get("name")
        }

        for cell_name, cell_data in module_index.cells.items():
            child_ctx = ctx.children.get(cell_name)
            if child_ctx is None:
                continue
            noopt_cell = noopt_cells.get(cell_name)
            if not noopt_cell:
                continue

            child_index = self.module_indices[child_ctx.module_name]
            for port_name, direction in child_index.port_directions.items():
                if direction not in {"output", "inout"}:
                    continue

                child_bits = child_index.name_to_bits.get(port_name, [])
                parent_tokens = noopt_cell.get("connections", {}).get(port_name, [])
                if len(child_bits) != 1 or len(parent_tokens) != 1:
                    continue

                child_state = self._noopt_get_state(child_ctx, port_name)
                if child_state is None or not child_state.root_ids:
                    continue

                parent_token = parent_tokens[0]
                parent_bits = module_index.name_to_bits.get(parent_token, [])
                if len(parent_bits) != 1:
                    continue
                if not self._normal_confirms_noopt_state(
                    ctx,
                    parent_bits,
                    parent_token,
                    child_state,
                ):
                    continue

                changed |= self._noopt_assign_const(
                    ctx,
                    parent_token,
                    child_state.value,
                    child_state.root_ids,
                    (
                        f"normal JSON 已确认跨层次输出 "
                        f"{self._hier_signal(ctx, parent_token)} = "
                        f"{self._format_const_value([child_state.value])}；"
                        f"noopt RTLIL 从子实例 {child_ctx.path_str}.{port_name} "
                        "反传根因"
                    ),
                )

        return changed

    @staticmethod
    def _expand_noopt_states(
        states: List[Optional[ConstEvidence]],
        width: int,
    ) -> Optional[List[Optional[ConstEvidence]]]:
        if len(states) == width:
            return states
        if len(states) == 1 and width > 1:
            return states * width
        return None

    def _infer_noopt_cell(
        self,
        ctx: InstanceContext,
        cell: Dict,
    ) -> List[Tuple[str, str, Set[str], str]]:
        cell_type = cell.get("type", "")
        op = self._normalize_cell_type(cell_type)
        connections = cell.get("connections", {})
        out_tokens = connections.get("Y") or connections.get("Q") or []
        if not out_tokens:
            return []

        def port_states(port_name: str) -> List[Optional[ConstEvidence]]:
            return [
                self._noopt_get_state(ctx, token)
                for token in connections.get(port_name, [])
            ]

        def expand(port_name: str) -> Optional[List[Optional[ConstEvidence]]]:
            return self._expand_noopt_states(port_states(port_name), len(out_tokens))

        results: List[Tuple[str, str, Set[str], str]] = []
        reason = (
            f"noopt RTLIL {self._hier_signal(ctx, cell.get('name', '<cell>'))} "
            f"{cell_type}: constant propagation"
        )

        if op in {"and", "nand", "or", "nor", "xor", "xnor"}:
            a_states = expand("A")
            b_states = expand("B")
            if a_states is None or b_states is None:
                return []

            for index, out_token in enumerate(out_tokens):
                bit_inputs = [a_states[index], b_states[index]]
                zeros = [state for state in bit_inputs if state and state.value == "0"]
                ones = [state for state in bit_inputs if state and state.value == "1"]

                if op in {"and", "nand"}:
                    if zeros:
                        out_value = "0" if op == "and" else "1"
                        roots = self._merge_roots(*zeros)
                    elif self._all_known(bit_inputs):
                        out_value = "1" if op == "and" else "0"
                        roots = self._merge_roots(*bit_inputs)
                    else:
                        continue
                elif op in {"or", "nor"}:
                    if ones:
                        out_value = "1" if op == "or" else "0"
                        roots = self._merge_roots(*ones)
                    elif self._all_known(bit_inputs):
                        out_value = "0" if op == "or" else "1"
                        roots = self._merge_roots(*bit_inputs)
                    else:
                        continue
                else:
                    if not self._all_known(bit_inputs):
                        continue
                    parity = sum(1 for state in bit_inputs if state and state.value == "1") % 2
                    out_value = str(parity)
                    if op == "xnor":
                        out_value = "0" if out_value == "1" else "1"
                    roots = self._merge_roots(*bit_inputs)

                results.append((out_token, out_value, roots, reason))
            return results

        if op in {"not", "buf"}:
            a_states = expand("A")
            if a_states is None:
                return []
            for index, out_token in enumerate(out_tokens):
                src = a_states[index]
                if src is None:
                    continue
                out_value = src.value if op == "buf" else ("1" if src.value == "0" else "0")
                results.append((out_token, out_value, set(src.root_ids), reason))
            return results

        if op == "mux":
            a_states = expand("A")
            b_states = expand("B")
            s_states = port_states("S")
            if a_states is None or b_states is None or len(s_states) != 1:
                return []
            select = s_states[0]
            for index, out_token in enumerate(out_tokens):
                a_state = a_states[index]
                b_state = b_states[index]
                if a_state and b_state and a_state.value == b_state.value:
                    results.append(
                        (out_token, a_state.value, self._merge_roots(a_state, b_state), reason)
                    )
                    continue
                if select and select.value == "0" and a_state:
                    results.append(
                        (out_token, a_state.value, self._merge_roots(select, a_state), reason)
                    )
                    continue
                if select and select.value == "1" and b_state:
                    results.append(
                        (out_token, b_state.value, self._merge_roots(select, b_state), reason)
                    )
            return results

        return []

    def _propagate_noopt_connects(self, ctx: InstanceContext) -> bool:
        changed = False
        module = self.noopt_modules.get(ctx.module_name, {})
        for dst_tokens, src_tokens in module.get("connects", []):
            if not dst_tokens or not src_tokens:
                continue
            src_states = [self._noopt_get_state(ctx, token) for token in src_tokens]
            expanded_states = self._expand_noopt_states(src_states, len(dst_tokens))
            if expanded_states is None:
                continue
            for dst_token, state in zip(dst_tokens, expanded_states):
                if state is None:
                    continue
                changed |= self._noopt_assign_const(
                    ctx,
                    dst_token,
                    state.value,
                    state.root_ids,
                    f"noopt RTLIL connect propagates constant to {self._hier_signal(ctx, dst_token)}",
                )
        return changed

    def _propagate_noopt_local_comb(self, ctx: InstanceContext) -> bool:
        changed = False
        module = self.noopt_modules.get(ctx.module_name, {})
        for cell in module.get("cells", []):
            cell_type = cell.get("type", "")
            if cell_type in self.module_indices:
                continue
            if self._is_sequential_cell_type(cell_type):
                continue
            if cell_type not in COMB_CELL_TYPES and self._normalize_cell_type(cell_type) == cell_type:
                continue

            for out_token, out_value, roots, reason in self._infer_noopt_cell(ctx, cell):
                changed |= self._noopt_assign_const(
                    ctx,
                    out_token,
                    out_value,
                    roots,
                    reason,
                )
        return changed

    def _run_noopt_fixpoint(self) -> None:
        if not self.noopt_modules:
            return

        self._seed_noopt_direct_roots()
        self._seed_noopt_from_normal_boundary_constants()

        changed = True
        guard = 0
        while changed:
            changed = False
            guard += 1
            if guard > 10000:
                raise RuntimeError("noopt RTLIL 常量传播固定点迭代次数异常。")

            for ctx in self.all_contexts_preorder:
                changed |= self._propagate_noopt_parent_to_children(ctx)
                changed |= self._propagate_noopt_local_comb(ctx)
                changed |= self._propagate_noopt_connects(ctx)

            for ctx in self.all_contexts_postorder:
                changed |= self._propagate_noopt_child_to_parent(ctx)
                changed |= self._propagate_noopt_local_comb(ctx)
                changed |= self._propagate_noopt_connects(ctx)

    # ------------------------------------------------------------------
    # 固定点传播
    # ------------------------------------------------------------------
    def _infer_comb_cell_wide(
        self,
        ctx: InstanceContext,
        cell_name: str,
        cell_data: Dict,
    ) -> List[Tuple[Bit, str, Set[str], str]]:
        cell_type = cell_data.get("type", "")
        op = self._normalize_cell_type(cell_type)
        directions = cell_data.get("port_directions", {})
        connections = cell_data.get("connections", {})

        output_ports = [port for port, direction in directions.items() if direction == "output"]
        if len(output_ports) != 1:
            return []
        out_port = output_ports[0]
        out_bits = connections.get(out_port, [])
        if not out_bits:
            return []

        def port_states(port_name: str) -> List[Optional[ConstEvidence]]:
            bits = connections.get(port_name, [])
            return [
                self._get_state(ctx, bit, f"{ctx.path_str}.{cell_name}.{port_name}[{index}]")
                for index, bit in enumerate(bits)
            ]

        def expand_states(states: List[Optional[ConstEvidence]]) -> Optional[List[Optional[ConstEvidence]]]:
            if len(states) == len(out_bits):
                return states
            if len(states) == 1 and len(out_bits) > 1:
                return states * len(out_bits)
            return None

        results: List[Tuple[Bit, str, Set[str], str]] = []
        input_ports = [port for port, direction in directions.items() if direction == "input"]

        if op in {"and", "nand", "or", "nor", "xor", "xnor"}:
            expanded_inputs: List[List[Optional[ConstEvidence]]] = []
            for port_name in input_ports:
                states = expand_states(port_states(port_name))
                if states is None:
                    return []
                expanded_inputs.append(states)

            for index, out_bit in enumerate(out_bits):
                bit_inputs = [states[index] for states in expanded_inputs]
                zeros = [state for state in bit_inputs if state and state.value == "0"]
                ones = [state for state in bit_inputs if state and state.value == "1"]

                if op in {"and", "nand"}:
                    if zeros:
                        out_value = "0" if op == "and" else "1"
                        roots = self._merge_roots(*zeros)
                    elif bit_inputs and self._all_known(bit_inputs):
                        out_value = "1" if op == "and" else "0"
                        roots = self._merge_roots(*bit_inputs)
                    else:
                        continue
                elif op in {"or", "nor"}:
                    if ones:
                        out_value = "1" if op == "or" else "0"
                        roots = self._merge_roots(*ones)
                    elif bit_inputs and self._all_known(bit_inputs):
                        out_value = "0" if op == "or" else "1"
                        roots = self._merge_roots(*bit_inputs)
                    else:
                        continue
                else:
                    if not bit_inputs or not self._all_known(bit_inputs):
                        continue
                    parity = sum(1 for state in bit_inputs if state and state.value == "1") % 2
                    out_value = str(parity)
                    if op == "xnor":
                        out_value = "0" if out_value == "1" else "1"
                    roots = self._merge_roots(*bit_inputs)

                results.append(
                    (
                        out_bit,
                        out_value,
                        roots,
                        f"{ctx.path_str}.{cell_name} {cell_type}: bitwise constant propagation",
                    )
                )
            return results

        if op in {"not", "buf"}:
            if not input_ports:
                return []
            src_states = expand_states(port_states(input_ports[0]))
            if src_states is None:
                return []
            for index, out_bit in enumerate(out_bits):
                src = src_states[index]
                if src is None:
                    continue
                out_value = src.value if op == "buf" else ("1" if src.value == "0" else "0")
                results.append(
                    (
                        out_bit,
                        out_value,
                        set(src.root_ids),
                        f"{ctx.path_str}.{cell_name} {cell_type}: unary constant propagation",
                    )
                )
            return results

        if op == "mux":
            a_states = expand_states(port_states("A"))
            b_states = expand_states(port_states("B"))
            s_states = port_states("S")
            if a_states is None or b_states is None or len(s_states) != 1:
                return []
            select = s_states[0]
            for index, out_bit in enumerate(out_bits):
                a_state = a_states[index]
                b_state = b_states[index]
                if a_state and b_state and a_state.value == b_state.value:
                    results.append(
                        (
                            out_bit,
                            a_state.value,
                            self._merge_roots(a_state, b_state),
                            f"{ctx.path_str}.{cell_name} {cell_type}: equal data inputs",
                        )
                    )
                    continue
                if select and select.value == "0" and a_state:
                    results.append(
                        (
                            out_bit,
                            a_state.value,
                            self._merge_roots(select, a_state),
                            f"{ctx.path_str}.{cell_name} {cell_type}: selected A",
                        )
                    )
                    continue
                if select and select.value == "1" and b_state:
                    results.append(
                        (
                            out_bit,
                            b_state.value,
                            self._merge_roots(select, b_state),
                            f"{ctx.path_str}.{cell_name} {cell_type}: selected B",
                        )
                    )
            return results

        if op == "pmux":
            a_states = expand_states(port_states("A"))
            select_states = port_states("S")
            if a_states is None or not select_states:
                return []
            if all(state and state.value == "0" for state in select_states) and self._all_known(a_states):
                for index, out_bit in enumerate(out_bits):
                    a_state = a_states[index]
                    if a_state is None:
                        continue
                    results.append(
                        (
                            out_bit,
                            a_state.value,
                            self._merge_roots(*select_states, a_state),
                            f"{ctx.path_str}.{cell_name} {cell_type}: no select asserted",
                        )
                    )
                return results
            if len(select_states) == 1:
                b_states = expand_states(port_states("B"))
                if b_states is None:
                    return []
                select = select_states[0]
                for index, out_bit in enumerate(out_bits):
                    a_state = a_states[index]
                    b_state = b_states[index]
                    if a_state and b_state and a_state.value == b_state.value:
                        results.append(
                            (
                                out_bit,
                                a_state.value,
                                self._merge_roots(a_state, b_state),
                                f"{ctx.path_str}.{cell_name} {cell_type}: equal data inputs",
                            )
                        )
                        continue
                    if select and select.value == "0" and a_state:
                        results.append(
                            (
                                out_bit,
                                a_state.value,
                                self._merge_roots(select, a_state),
                                f"{ctx.path_str}.{cell_name} {cell_type}: selected A",
                            )
                        )
                        continue
                    if select and select.value == "1" and b_state:
                        results.append(
                            (
                                out_bit,
                                b_state.value,
                                self._merge_roots(select, b_state),
                                f"{ctx.path_str}.{cell_name} {cell_type}: selected B",
                            )
                        )
                return results
            return []

        if op in {"reduce_and", "reduce_or", "reduce_xor", "reduce_xnor"}:
            if len(out_bits) != 1:
                return []
            a_states = port_states("A")
            if not a_states:
                return []
            out_bit = out_bits[0]
            if op == "reduce_and":
                zeros = [state for state in a_states if state and state.value == "0"]
                if zeros:
                    return [(
                        out_bit,
                        "0",
                        self._merge_roots(*zeros),
                        f"{ctx.path_str}.{cell_name} {cell_type}: reduce_and control value",
                    )]
                if self._all_known(a_states):
                    return [(
                        out_bit,
                        "1",
                        self._merge_roots(*a_states),
                        f"{ctx.path_str}.{cell_name} {cell_type}: reduce_and all inputs known",
                    )]
                return []
            if op == "reduce_or":
                ones = [state for state in a_states if state and state.value == "1"]
                if ones:
                    return [(
                        out_bit,
                        "1",
                        self._merge_roots(*ones),
                        f"{ctx.path_str}.{cell_name} {cell_type}: reduce_or control value",
                    )]
                if self._all_known(a_states):
                    return [(
                        out_bit,
                        "0",
                        self._merge_roots(*a_states),
                        f"{ctx.path_str}.{cell_name} {cell_type}: reduce_or all inputs known",
                    )]
                return []
            if self._all_known(a_states):
                parity = sum(1 for state in a_states if state and state.value == "1") % 2
                out_value = str(parity)
                if op == "reduce_xnor":
                    out_value = "0" if out_value == "1" else "1"
                return [(
                    out_bit,
                    out_value,
                    self._merge_roots(*a_states),
                    f"{ctx.path_str}.{cell_name} {cell_type}: reduce xor/xnor all inputs known",
                )]
            return []

        if op in {"eq", "ne"}:
            if len(out_bits) != 1:
                return []
            a_states = port_states("A")
            b_states = port_states("B")
            if len(a_states) != len(b_states) or not a_states:
                return []
            if not self._all_known(a_states) or not self._all_known(b_states):
                return []
            a_val = "".join(state.value for state in a_states if state)
            b_val = "".join(state.value for state in b_states if state)
            out_value = "1" if a_val == b_val else "0"
            if op == "ne":
                out_value = "0" if out_value == "1" else "1"
            return [(
                out_bits[0],
                out_value,
                self._merge_roots(*a_states, *b_states),
                f"{ctx.path_str}.{cell_name} {cell_type}: comparison resolved",
            )]

        return []

    def _propagate_parent_to_children(self, ctx: InstanceContext) -> bool:
        changed = False
        module_index = self.module_indices[ctx.module_name]
        for cell_name, cell_data in module_index.cells.items():
            child_ctx = ctx.children.get(cell_name)
            if child_ctx is None:
                continue
            child_index = self.module_indices[child_ctx.module_name]
            connections = cell_data.get("connections", {})
            for port_name, direction in child_index.port_directions.items():
                if direction not in {"input", "inout"}:
                    continue
                parent_bits = connections.get(port_name, [])
                child_bits = child_index.name_to_bits.get(port_name, [])
                if not parent_bits or not child_bits or len(parent_bits) != len(child_bits):
                    continue
                for index, (parent_bit, child_bit) in enumerate(zip(parent_bits, child_bits)):
                    evidence = self._get_state(
                        ctx,
                        parent_bit,
                        f"{ctx.path_str}.{cell_name}.{port_name}[{index}]",
                        create_literal_root=True,
                    )
                    if evidence is None or child_bit in CONST_BITS:
                        continue
                    changed |= self._assign_const(
                        child_ctx,
                        child_bit,
                        evidence.value,
                        evidence.root_ids,
                        reason=(
                            f"{child_ctx.path_str}.{port_name}[{index}] 由父模块连接点 "
                            f"{ctx.path_str}.{cell_name}.{port_name}[{index}] 传入常量"
                        ),
                    )
        return changed

    def _propagate_child_to_parent(self, ctx: InstanceContext) -> bool:
        changed = False
        module_index = self.module_indices[ctx.module_name]
        for cell_name, cell_data in module_index.cells.items():
            child_ctx = ctx.children.get(cell_name)
            if child_ctx is None:
                continue
            child_index = self.module_indices[child_ctx.module_name]
            connections = cell_data.get("connections", {})
            for port_name, direction in child_index.port_directions.items():
                if direction not in {"output", "inout"}:
                    continue
                child_bits = child_index.name_to_bits.get(port_name, [])
                parent_bits = connections.get(port_name, [])
                if not child_bits or not parent_bits or len(child_bits) != len(parent_bits):
                    continue
                for index, (child_bit, parent_bit) in enumerate(zip(child_bits, parent_bits)):
                    evidence = self._get_state(
                        child_ctx,
                        child_bit,
                        f"{child_ctx.path_str}.{port_name}[{index}]",
                        create_literal_root=True,
                    )
                    if evidence is None or parent_bit in CONST_BITS:
                        continue
                    changed |= self._assign_const(
                        ctx,
                        parent_bit,
                        evidence.value,
                        evidence.root_ids,
                        reason=(
                            f"{ctx.path_str}.{cell_name}.{port_name}[{index}] "
                            "由子模块输出常量反映到父模块"
                        ),
                    )
        return changed

    def _propagate_local_comb(self, ctx: InstanceContext) -> bool:
        changed = False
        module_index = self.module_indices[ctx.module_name]
        for cell_name, cell_data in module_index.cells.items():
            cell_type = cell_data.get("type", "")
            if cell_type in self.module_indices:
                continue
            if self._is_sequential_cell_type(cell_type):
                continue
            if cell_type not in COMB_CELL_TYPES and self._normalize_cell_type(cell_type) == cell_type:
                continue

            for out_bit, out_value, roots, reason in self._infer_comb_cell_wide(
                ctx,
                cell_name,
                cell_data,
            ):
                changed |= self._assign_const(ctx, out_bit, out_value, roots, reason)
        return changed

    def _run_fixpoint(self) -> None:
        self._seed_direct_constant_roots()

        changed = True
        guard = 0
        while changed:
            changed = False
            guard += 1
            if guard > 10000:
                raise RuntimeError("常量传播固定点迭代次数异常，可能存在未预期的组合回路。")

            for ctx in self.all_contexts_preorder:
                changed |= self._propagate_parent_to_children(ctx)
                changed |= self._propagate_local_comb(ctx)

            for ctx in self.all_contexts_postorder:
                changed |= self._propagate_child_to_parent(ctx)
                changed |= self._propagate_local_comb(ctx)

        self._run_noopt_fixpoint()

    # ------------------------------------------------------------------
    # 结果收集
    # ------------------------------------------------------------------
    def _collect_signal_constants(self) -> List[SignalConstRecord]:
        records: Dict[str, SignalConstRecord] = {}

        for ctx in self.all_contexts_preorder:
            module_index = self.module_indices[ctx.module_name]
            for bits, signal_names in self._signal_groups(module_index):
                preferred_name = self._preferred_signal_name(module_index, signal_names)
                resolved = self._resolve_signal_constant(
                    ctx,
                    bits,
                    self._hier_signal(ctx, preferred_name),
                )
                if resolved is None:
                    continue
                const_value, root_ids = resolved
                hierarchical_signal = self._hier_signal(ctx, preferred_name)
                aliases = [self._hier_signal(ctx, name) for name in sorted(set(signal_names))]
                reason_parts = []

                noopt_evidence = self.noopt_const_map.get(
                    self._noopt_key(ctx, preferred_name)
                )
                noopt_expected_value = None
                if noopt_evidence is not None:
                    noopt_expected_value = self._format_const_value([noopt_evidence.value])
                if noopt_expected_value == const_value:
                    root_ids |= set(noopt_evidence.root_ids)
                    noopt_reason = self.noopt_reason_map.get(
                        self._noopt_key(ctx, preferred_name),
                        "",
                    )
                    if noopt_reason:
                        reason_parts.append(noopt_reason)

                for bit in bits:
                    if bit in CONST_BITS:
                        continue
                    reason = self.reason_map.get(self._node_key(ctx, bit), "")
                    if reason and reason not in reason_parts:
                        reason_parts.append(reason)
                for root_id in sorted(root_ids):
                    root = self.root_causes.get(root_id)
                    if not root:
                        continue
                    for note in root.notes:
                        if note and note not in reason_parts:
                            reason_parts.append(note)
                literal_reason = self._json_literal_constant_reason(
                    ctx,
                    preferred_name,
                    bits,
                    const_value,
                )
                if literal_reason and literal_reason not in reason_parts:
                    reason_parts.append(literal_reason)
                records[hierarchical_signal] = SignalConstRecord(
                    hierarchical_signal=hierarchical_signal,
                    module=ctx.path_str,
                    signal_kind=self._role_of_signal_group(module_index, signal_names),
                    constant_value=const_value,
                    aliases=aliases,
                    root_ids=sorted(root_ids),
                    reason=" | ".join(reason_parts),
                )

        return sorted(records.values(), key=lambda item: item.hierarchical_signal)

    def _build_root_clusters(self, signal_records: List[SignalConstRecord]) -> Dict[str, List[Dict[str, str]]]:
        clusters: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        for record in signal_records:
            for root_id in record.root_ids:
                root = self.root_causes.get(root_id)
                if root and record.hierarchical_signal == root.hierarchical_signal:
                    continue
                clusters[root_id].append(
                    {
                        "signal": record.hierarchical_signal,
                        "kind": record.signal_kind,
                        "value": record.constant_value,
                        "roots": root_id,
                        "reason": record.reason,
                    }
                )
        for root_id in clusters:
            clusters[root_id].sort(key=lambda x: x["signal"])
        return dict(clusters)

    def _collect_hierarchical_constant_outputs(self, signal_records: List[SignalConstRecord]) -> List[Dict[str, str]]:
        outputs = [
            {
                "signal": record.hierarchical_signal,
                "value": record.constant_value,
                "kind": record.signal_kind,
                "roots": ", ".join(record.root_ids),
                "reason": record.reason,
            }
            for record in signal_records
            if record.signal_kind == "output"
        ]
        outputs.sort(key=lambda item: item["signal"])
        return outputs

    def _collect_hierarchical_constant_inputs(self, signal_records: List[SignalConstRecord]) -> List[Dict[str, str]]:
        inputs = [
            {
                "signal": record.hierarchical_signal,
                "value": record.constant_value,
                "kind": record.signal_kind,
                "roots": ", ".join(record.root_ids),
                "reason": record.reason,
            }
            for record in signal_records
            if record.signal_kind == "input"
        ]
        inputs.sort(key=lambda item: item["signal"])
        return inputs

    def _collect_hierarchical_constant_wires(self, signal_records: List[SignalConstRecord]) -> List[Dict[str, str]]:
        wires = [
            {
                "signal": record.hierarchical_signal,
                "value": record.constant_value,
                "kind": record.signal_kind,
                "roots": ", ".join(record.root_ids),
                "reason": record.reason,
            }
            for record in signal_records
            if record.signal_kind in {"wire", "root"}
        ]
        wires.sort(key=lambda item: item["signal"])
        return wires

    def _cell_counter_by_module(self) -> Dict[str, Dict[str, int]]:
        stats: Dict[str, Dict[str, int]] = {}
        for module_name, index in self.module_indices.items():
            stats[module_name] = dict(Counter(cell.get("type", "") for cell in index.cells.values()))
        return stats

    def analyze_design(self) -> Dict:
        print("步骤 1: 导出 normal Yosys JSON 和 noopt RTLIL 视图...")
        self._export_design_json()

        print("步骤 2: 构建模块索引与实例树...")
        self._build_module_indices()
        self._build_context_tree()

        print("步骤 3: 进行跨层次常量传播固定点分析...")
        self._run_fixpoint()

        print("步骤 4: 汇总根因与污染集合...")
        signal_records = self._collect_signal_constants()
        root_clusters = self._build_root_clusters(signal_records)

        findings = {
            "hierarchical_constant_outputs": self._collect_hierarchical_constant_outputs(signal_records),
            "hierarchical_constant_inputs": self._collect_hierarchical_constant_inputs(signal_records),
            "hierarchical_constant_wires": self._collect_hierarchical_constant_wires(signal_records),
            "all_constant_signals": [asdict(item) for item in signal_records],
            "cell_stats_by_module": self._cell_counter_by_module(),
            "conflicts": list(self.conflicts),
        }

        roots = [asdict(root) for root in sorted(self.root_causes.values(), key=lambda x: x.hierarchical_signal)]
        summary = self.summarize_analysis(findings, root_clusters)

        return {
            "findings": findings,
            "root_causes": roots,
            "root_pollution_clusters": root_clusters,
            "analysis_summary": summary,
        }

    def summarize_analysis(self, findings: Dict, root_clusters: Dict[str, List[Dict[str, str]]]) -> Dict:
        summary = {
            "total_root_causes": len(self.root_causes),
            "total_constant_signals": len(findings["all_constant_signals"]),
            "total_constant_outputs": len(findings["hierarchical_constant_outputs"]),
            "total_constant_inputs": len(findings["hierarchical_constant_inputs"]),
            "total_constant_wires": len(findings["hierarchical_constant_wires"]),
            "total_root_clusters": len(root_clusters),
            "total_conflicts": len(findings["conflicts"]),
            "potential_issues": [],
        }

        if summary["total_root_causes"] > 0:
            summary["potential_issues"].append(
                f"发现 {summary['total_root_causes']} 个最源头常量引脚/线网。"
            )
        if summary["total_constant_outputs"] > 0:
            summary["potential_issues"].append(
                f"发现 {summary['total_constant_outputs']} 个层次化常量输出端口。"
            )
        if summary["total_constant_inputs"] > 0:
            summary["potential_issues"].append(
                f"发现 {summary['total_constant_inputs']} 个层次化常量输入端口。"
            )
        if summary["total_constant_wires"] > 0:
            summary["potential_issues"].append(
                f"发现 {summary['total_constant_wires']} 个层次化常量非端口信号。"
            )
        if summary["total_conflicts"] > 0:
            summary["potential_issues"].append(
                f"发现 {summary['total_conflicts']} 个冲突推导点，建议人工复核。"
            )
        return summary

    def build_json_report(self, analysis_results: Dict) -> Dict:
        findings = analysis_results["findings"]
        summary = analysis_results["analysis_summary"]
        def convert_root(root: Dict) -> Dict:
            return {
                "层次化信号": root["hierarchical_signal"],
                "常量值": root["constant_value"],
                "根源类型": self._source_type_label(root["source_type"]),
                "原始根源类型": root["source_type"],
                "位置": root["location"],
                "别名": root.get("aliases", []),
                "说明": root.get("notes", []),
            }

        def convert_signal(item: Dict) -> Dict:
            return {
                "信号": item["signal"],
                "值": item["value"],
                "类别": item["kind"],
                "根源": item.get("roots", "") or "组合逻辑推导常量（非最源头）",
                "原因": item.get("reason", ""),
            }

        def convert_cluster(cluster: Dict[str, List[Dict[str, str]]]) -> Dict[str, List[Dict]]:
            return {
                root_id: [convert_signal(item) for item in items]
                for root_id, items in cluster.items()
            }

        return {
            "报告类型": "层次化常量传播根因分析",
            "报告格式": "json",
            "生成时间": datetime.now().isoformat(timespec="seconds"),
            "设计输入": list(self.design_inputs),
            "主输入": self.primary_input,
            "顶层模块": self.top_module,
            "Yosys路径": self.yosys_bin,
            "分析结果": {
                "摘要": {
                    "最源头常量数量": summary["total_root_causes"],
                    "层次化常量信号总数": summary["total_constant_signals"],
                    "层次化常量输出数量": summary["total_constant_outputs"],
                    "层次化常量输入数量": summary["total_constant_inputs"],
                    "层次化常量非端口信号数量": summary["total_constant_wires"],
                    "污染簇数量": summary["total_root_clusters"],
                    "冲突点数量": summary["total_conflicts"],
                    "潜在问题": list(summary["potential_issues"]),
                },
                "最源头常量引脚和线网": [convert_root(root) for root in analysis_results["root_causes"]],
                "按根源分组的污染常量集合": convert_cluster(analysis_results["root_pollution_clusters"]),
                "层次化常量输出": [convert_signal(item) for item in findings["hierarchical_constant_outputs"]],
                "层次化常量输入": [convert_signal(item) for item in findings["hierarchical_constant_inputs"]],
                "层次化常量非端口信号": [convert_signal(item) for item in findings["hierarchical_constant_wires"]],
                "模块单元统计": findings["cell_stats_by_module"],
                "冲突和注意事项": findings["conflicts"],
                "分析边界": [
                    "该工具会跨模块、跨实例向下和向上追踪常量传播。",
                    "对触发器、锁存器、存储器等时序单元默认作为传播边界，不把其输出直接判定为常量。",
                    "最终常量事实依据 normal Yosys JSON 识别；直接常量根和模块内部污染传播集合依据 read_verilog -noopt + proc -noopt 导出的 RTLIL 组合图追踪。",
                    "normal JSON 已确认的层次边界常量会作为 noopt RTLIL 的归因种子；noopt 只在 normal 同值校验通过后沿实例端口传递根因，用于接上跨模块进入模块内部的源码级传播；noopt 不新增最终常量事实。",
                    "源码仅通过 Yosys src 属性作为定位上下文，不使用源码正则推断根因。",
                    "常量根因按信号连接点精确归属；若 noopt RTLIL 也无法保留依赖，报告不会把同值常量线网合并为候选根因。",
                ],
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="基于 Yosys 层次化 JSON 网表分析跨模块常量传播问题。"
    )
    parser.add_argument(
        "design_inputs",
        nargs="+",
        help="Verilog 设计文件或源文件目录，可同时传多个路径",
    )
    parser.add_argument("--top", default="top_module", help="顶层模块名")
    parser.add_argument(
        "--output",
        default="constant_hierarchical_analysis_report.json",
        help="输出 JSON 报告文件",
    )
    parser.add_argument(
        "--yosys",
        default=None,
        help="Yosys 可执行文件路径。不指定时会从 PATH 或附近的 oss-cad-suite 中自动查找。",
    )

    args = parser.parse_args()

    try:
        tracer = ConstantTracer(args.design_inputs, args.top, args.yosys)
        results = tracer.analyze_design()
        report = tracer.build_json_report(results)

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\n报告已保存到: {output_path}")

        has_issue = bool(results["root_causes"] or results["findings"]["hierarchical_constant_outputs"])
        if has_issue:
            print("\n检测到层次化常量传播问题。")
            return 1

        print("\n未检测到明显的层次化常量传播问题。")
        return 0

    except FileNotFoundError as exc:
        print(f"错误: {exc}")
        return 2
    except ValueError as exc:
        print(f"错误: {exc}")
        return 2
    except RuntimeError as exc:
        print(f"错误: 运行 Yosys 失败。\n{exc}")
        return 2
    except subprocess.TimeoutExpired:
        print("错误: Yosys 执行超时。")
        return 2


if __name__ == "__main__":
    sys.exit(main())
