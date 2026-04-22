#!/usr/bin/env python3
"""层次化 Verilog 常量传播根因分析工具。

核心目标：
1. 不再只分析顶层；支持父/子模块多层嵌套的层次化常量传播。
2. 定位“最源头常量引脚/线网”。
3. 给出每个根源常量对应的污染常量集合（跨层次）。
4. 对时序单元保守处理：默认不跨寄存器/触发器继续传播，避免把寄存器输出误报为常量。

说明：
- 该工具依赖 Yosys 导出层次化 JSON 网表。
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
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple, Union

Bit = Union[int, str]
CONST_BITS = {"0", "1"}

MODULE_BLOCK_RE = re.compile(
    r"(?ms)^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b.*?^\s*endmodule\b"
)
COMMENT_RE = re.compile(r"//.*?$|/\*.*?\*/", re.MULTILINE | re.DOTALL)
INSTANCE_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s+"
    r"(?:#\s*\([^;]*?\)\s*)?"
    r"([A-Za-z_][A-Za-z0-9_$]*)\s*\(",
    re.MULTILINE,
)

VERILOG_PRIMITIVES = {
    "and",
    "buf",
    "bufif0",
    "bufif1",
    "cmos",
    "nand",
    "nmos",
    "nor",
    "not",
    "notif0",
    "notif1",
    "or",
    "pmos",
    "pullup",
    "pulldown",
    "rcmos",
    "rnmos",
    "rpmos",
    "rtran",
    "rtranif0",
    "rtranif1",
    "tran",
    "tranif0",
    "tranif1",
    "xnor",
    "xor",
}
NON_INSTANCE_TOKENS = VERILOG_PRIMITIVES | {
    "always",
    "always_comb",
    "always_ff",
    "always_latch",
    "assign",
    "begin",
    "case",
    "casex",
    "casez",
    "else",
    "end",
    "endcase",
    "endfunction",
    "endgenerate",
    "endmodule",
    "endtask",
    "for",
    "function",
    "generate",
    "genvar",
    "if",
    "initial",
    "input",
    "inout",
    "integer",
    "localparam",
    "logic",
    "module",
    "output",
    "parameter",
    "real",
    "reg",
    "supply0",
    "supply1",
    "task",
    "while",
    "wire",
}

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

EXPLICIT_SOURCE_TYPES = {
    "direct_assign",
    "parameter",
    "reset_value",
    "port_connection",
}

SEQUENTIAL_TYPE_PATTERNS = (
    re.compile(r"\$(?:a|d|sd|ad|dl|sr)?ff", re.IGNORECASE),
    re.compile(r"\$mem", re.IGNORECASE),
    re.compile(r"\$latch", re.IGNORECASE),
)


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
        self.selected_modules = self._select_reachable_modules()
        self.selected_files = self._select_analysis_files()
        self.source_text = self._build_source_text()
        self.primary_input = (
            str(self.selected_files[0]) if self.selected_files else self.design_inputs[0]
        )

        self.netlist_data: Dict = {}
        self.modules_data: Dict[str, Dict] = {}
        self.module_indices: Dict[str, ModuleIndex] = {}
        self.root_context: Optional[InstanceContext] = None
        self.all_contexts_preorder: List[InstanceContext] = []
        self.all_contexts_postorder: List[InstanceContext] = []

        self.root_causes: Dict[str, RootCause] = {}
        self.context_direct_roots: Dict[str, Dict[str, Set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self.const_map: Dict[Tuple[str, Bit], ConstEvidence] = {}
        self.reason_map: Dict[Tuple[str, Bit], str] = {}
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
                if path.suffix.lower() == ".v" and path not in seen:
                    files.append(path)
                    seen.add(path)
                continue

            if path.is_dir():
                for file_path in sorted(path.rglob("*.v")):
                    resolved = file_path.resolve()
                    if resolved not in seen:
                        files.append(resolved)
                        seen.add(resolved)
                continue

            raise FileNotFoundError(f"Input path does not exist: {path}")

        if not files:
            raise FileNotFoundError("No Verilog source files were found.")
        return files

    @staticmethod
    def _strip_comments(text: str) -> str:
        def replace(match: re.Match) -> str:
            return re.sub(r"[^\n]", " ", match.group(0))

        return COMMENT_RE.sub(replace, text)

    def _build_design_catalog(self) -> Dict:
        modules_by_name: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        modules_in_file: Dict[Path, List[str]] = defaultdict(list)
        instances_by_module: Dict[str, Set[str]] = defaultdict(set)
        file_texts: Dict[Path, str] = {}

        for path in self.design_files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            file_texts[path] = text
            stripped_text = self._strip_comments(text)
            for match in MODULE_BLOCK_RE.finditer(stripped_text):
                module_name = match.group(1)
                block_text = text[match.start() : match.end()]
                block_text_no_comments = stripped_text[match.start() : match.end()]
                modules_by_name[module_name].append({"file": str(path), "text": block_text})
                modules_in_file[path].append(module_name)
                for callee, instance_name in INSTANCE_RE.findall(block_text_no_comments):
                    if callee in NON_INSTANCE_TOKENS or instance_name in NON_INSTANCE_TOKENS:
                        continue
                    instances_by_module[module_name].add(callee)

        return {
            "file_texts": file_texts,
            "modules_by_name": modules_by_name,
            "modules_in_file": modules_in_file,
            "instances_by_module": instances_by_module,
        }

    def _select_reachable_modules(self) -> Set[str]:
        modules_by_name = self.design_catalog["modules_by_name"]
        if self.top_module not in modules_by_name:
            candidates = sorted(modules_by_name)
            candidate_text = ", ".join(candidates[:20])
            if len(candidates) > 20:
                candidate_text += " ..."
            raise ValueError(
                f"Top module {self.top_module} was not found. Candidates: {candidate_text}"
            )

        reachable: Set[str] = set()
        stack = [self.top_module]
        while stack:
            module_name = stack.pop()
            if module_name in reachable:
                continue
            reachable.add(module_name)
            for callee in self.design_catalog["instances_by_module"].get(module_name, set()):
                if callee in VERILOG_PRIMITIVES:
                    continue
                if callee in modules_by_name:
                    stack.append(callee)
        return reachable

    def _select_analysis_files(self) -> List[Path]:
        header_files: List[Path] = []
        selected_module_files: List[Path] = []

        for path in self.design_files:
            module_names = self.design_catalog["modules_in_file"].get(path, [])
            if not module_names:
                header_files.append(path)
                continue
            if any(module_name in self.selected_modules for module_name in module_names):
                selected_module_files.append(path)

        selected = header_files + selected_module_files
        if not selected:
            raise ValueError(f"No source files were selected for top module {self.top_module}.")
        return selected

    def _build_source_text(self) -> str:
        return "\n".join(self.design_catalog["file_texts"][path] for path in self.selected_files)

    @staticmethod
    def _yosys_path(path: Union[str, Path]) -> str:
        return Path(path).resolve().as_posix()

    @staticmethod
    def _const_to_str(bit: Bit) -> str:
        return f"1'b{bit}"

    @staticmethod
    def _source_type_label(source_type: str) -> str:
        labels = {
            "direct_assign": "直接赋值",
            "parameter": "参数常量",
            "reset_value": "复位赋值",
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

    def _read_verilog_cmd(self, prepared_files: List[Path]) -> str:
        include_dirs = sorted({str(path.parent) for path in self.selected_files})
        include_flags = " ".join(f"-I {self._yosys_path(path)}" for path in include_dirs)
        file_args = " ".join(self._yosys_path(path) for path in prepared_files)
        flags = f"{include_flags} " if include_flags else ""
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
            source_dir = Path(tempdir) / "sources"
            source_dir.mkdir(parents=True, exist_ok=True)
            prepared_files = self._prepare_design_sources(source_dir)
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

    # ------------------------------------------------------------------
    # 模块/实例索引
    # ------------------------------------------------------------------
    def _build_module_indices(self) -> None:
        self.module_indices = {}
        for module_name, module_data in self.modules_data.items():
            if module_name not in self.selected_modules:
                continue

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
        stack_guard: List[str] = []

        def visit(module_name: str, path: Tuple[str, ...]) -> InstanceContext:
            ctx = InstanceContext(module_name=module_name, path=path)
            self.all_contexts_preorder.append(ctx)
            stack_guard.append(module_name)

            module_index = self.module_indices[module_name]
            for cell_name, cell_data in module_index.cells.items():
                cell_type = cell_data.get("type", "")
                if cell_type not in self.module_indices:
                    continue
                if cell_type in stack_guard:
                    # 保守跳过递归层次
                    continue
                child_ctx = visit(cell_type, path + (cell_name,))
                ctx.children[cell_name] = child_ctx

            stack_guard.pop()
            self.all_contexts_postorder.append(ctx)
            return ctx

        self.root_context = visit(self.top_module, (self.top_module,))

    # ------------------------------------------------------------------
    # 信号/根因辅助
    # ------------------------------------------------------------------
    def _node_key(self, ctx: InstanceContext, bit: Bit) -> Tuple[str, Bit]:
        return (ctx.path_str, bit)

    def _module_source_text(self, module_name: str) -> str:
        definitions = self.design_catalog["modules_by_name"].get(module_name, [])
        if definitions:
            return definitions[0]["text"]
        return self.source_text

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

    def _role_of_bit(self, module_index: ModuleIndex, bit: Bit) -> str:
        roles = module_index.bit_roles.get(bit, set())
        if "output" in roles:
            return "output"
        if "input" in roles:
            return "input"
        if "inout" in roles:
            return "inout"
        return "wire"

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

    def _hier_signal(self, ctx: InstanceContext, local_name: str) -> str:
        return f"{ctx.path_str}.{local_name}"

    def _preferred_hier_signal(self, ctx: InstanceContext, bit: Bit) -> str:
        module_index = self.module_indices[ctx.module_name]
        return self._hier_signal(ctx, self._preferred_local_name(module_index, bit))

    def _aliases_hier(self, ctx: InstanceContext, bit: Bit) -> List[str]:
        module_index = self.module_indices[ctx.module_name]
        aliases = self._public_names(module_index, bit)
        return [self._hier_signal(ctx, name) for name in aliases]

    def _signal_aliases_hier(self, ctx: InstanceContext, bits: List[Bit]) -> List[str]:
        module_index = self.module_indices[ctx.module_name]
        target = tuple(bits)
        aliases = [
            self._hier_signal(ctx, name)
            for name in self._public_signal_names(module_index)
            if tuple(module_index.name_to_bits.get(name, [])) == target
        ]
        return sorted(set(aliases))

    def _determine_source_type(self, module_name: str, signal: str, value: str) -> str:
        module_text = self._module_source_text(module_name)
        leaf = signal.split(".")[-1]
        decl_pattern = (
            rf"(?:wire|logic|reg)\s+"
            rf"(?:\[[^\]]+\]\s+)?"
            rf"{re.escape(leaf)}\s*=\s*"
            r"\d+'\s*[bBdDhHoO][0-9a-fA-F_xXzZ]+\s*;"
        )
        if re.search(decl_pattern, module_text):
            return "direct_assign"

        assign_pattern = (
            rf"assign\s+{re.escape(leaf)}\s*=\s*"
            r"\d+'\s*[bBdDhHoO][0-9a-fA-F_xXzZ]+\s*;"
        )
        if re.search(assign_pattern, module_text):
            return "direct_assign"

        param_pattern = (
            r"(parameter|localparam)\s+\w+\s*=\s*"
            r"\d+'\s*[bBdDhHoO][0-9a-fA-F_xXzZ]+"
        )
        if re.search(param_pattern, module_text):
            return "parameter"

        reset_pattern = (
            rf"if\s*\(\s*!?\s*(rst|reset|rst_n)\w*\s*\).*"
            rf"{re.escape(leaf)}\s*<=\s*"
            r"\d+'\s*[bBdDhHoO][0-9a-fA-F_xXzZ]+"
        )
        if re.search(reset_pattern, module_text, re.IGNORECASE | re.DOTALL):
            return "reset_value"

        port_conn_pattern = rf"\.{re.escape(leaf)}\s*\(\s*{re.escape(value)}\s*\)"
        if re.search(port_conn_pattern, module_text):
            return "port_connection"

        return "inferred_constant"

    def _register_root(
        self,
        ctx: InstanceContext,
        local_signal: str,
        value: str,
        source_type: str,
        note: Optional[str] = None,
    ) -> str:
        hierarchical_signal = self._hier_signal(ctx, local_signal)
        root_id = hierarchical_signal
        if root_id not in self.root_causes:
            module_index = self.module_indices[ctx.module_name]
            bits = module_index.name_to_bits.get(local_signal, [])
            aliases = self._signal_aliases_hier(ctx, bits) if bits else [hierarchical_signal]
            self.root_causes[root_id] = RootCause(
                root_id=root_id,
                hierarchical_signal=hierarchical_signal,
                local_signal=local_signal,
                constant_value=value,
                source_type=source_type,
                location=f"信号: {hierarchical_signal}",
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
        for name in self._public_signal_names(module_index):
            bits = module_index.name_to_bits.get(name, [])
            if bits:
                grouped[tuple(bits)].append(name)
        return [(list(bits), names) for bits, names in grouped.items()]

    def _site_has_literal_connection(
        self,
        ctx: InstanceContext,
        site: str,
        const_bit: str,
    ) -> bool:
        module_text = self._module_source_text(ctx.module_name)
        leaf = site.split(".")[-1]
        leaf = re.sub(r"\[\d+\]$", "", leaf)
        pattern = rf"\.{re.escape(leaf)}\s*\(\s*1'b{const_bit}\s*\)"
        return re.search(pattern, module_text) is not None

    def _seed_direct_constant_roots(self) -> None:
        for ctx in self.all_contexts_preorder:
            module_index = self.module_indices[ctx.module_name]
            for bits, signal_names in self._signal_groups(module_index):
                if not bits or any(bit not in CONST_BITS for bit in bits):
                    continue
                const_value = self._format_const_value([str(bit) for bit in bits])
                explicit_names: List[Tuple[str, str]] = []
                for name in signal_names:
                    source_type = self._determine_source_type(
                        ctx.module_name,
                        self._hier_signal(ctx, name),
                        const_value,
                    )
                    if source_type in EXPLICIT_SOURCE_TYPES:
                        explicit_names.append((name, source_type))
                if not explicit_names:
                    continue
                preferred = self._preferred_signal_name(
                    module_index,
                    [name for name, _ in explicit_names],
                )
                source_type = next(src for name, src in explicit_names if name == preferred)
                root_id = self._register_root(
                    ctx=ctx,
                    local_signal=preferred,
                    value=const_value,
                    source_type=source_type,
                    note="direct constant source",
                )
                for bit in bits:
                    self.context_direct_roots[ctx.path_str][str(bit)].add(root_id)

    def _literal_roots_for_context(self, ctx: InstanceContext, const_bit: str, site: str) -> Set[str]:
        roots = set(self.context_direct_roots[ctx.path_str].get(const_bit, set()))
        if roots:
            return roots
        if self._site_has_literal_connection(ctx, site, const_bit):
            return {self._register_literal_root(ctx, site, self._const_to_str(const_bit))}
        fallback_roots: Set[str] = set()
        for root_ids in self.context_direct_roots[ctx.path_str].values():
            fallback_roots |= set(root_ids)
        if fallback_roots:
            return fallback_roots
        return {self._register_literal_root(ctx, site, self._const_to_str(const_bit))}

    def _get_state(self, ctx: InstanceContext, bit: Bit, site: str) -> Optional[ConstEvidence]:
        if bit in CONST_BITS:
            return ConstEvidence(
                value=str(bit),
                root_ids=self._literal_roots_for_context(ctx, str(bit), site),
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
            evidence = self._get_state(ctx, bit, f"{site_prefix}[{index}]")
            if evidence is None:
                return None
            bit_values.append(evidence.value)
            root_ids |= set(evidence.root_ids)

        if not root_ids:
            return None
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

    def _infer_comb_cell(self, ctx: InstanceContext, cell_name: str, cell_data: Dict) -> List[Tuple[Bit, str, Set[str], str]]:
        cell_type = cell_data.get("type", "")
        op = self._normalize_cell_type(cell_type)
        directions = cell_data.get("port_directions", {})
        connections = cell_data.get("connections", {})

        output_ports = [p for p, d in directions.items() if d == "output"]
        if len(output_ports) != 1:
            return []
        out_port = output_ports[0]
        out_bits = connections.get(out_port, [])
        if len(out_bits) != 1:
            return []
        out_bit = out_bits[0]

        input_ports = [p for p, d in directions.items() if d == "input"]

        def input_state(port_name: str) -> Optional[ConstEvidence]:
            bits = connections.get(port_name, [])
            if len(bits) != 1:
                return None
            site = f"{ctx.path_str}.{cell_name}.{port_name}"
            return self._get_state(ctx, bits[0], site)

        def input_states(port_names: List[str]) -> List[Optional[ConstEvidence]]:
            return [input_state(p) for p in port_names]

        # -------- AND / NAND --------
        if op in {"and", "nand"}:
            states = input_states(input_ports)
            zeros = [s for s in states if s and s.value == "0"]
            ones = [s for s in states if s and s.value == "1"]
            if zeros:
                out_value = "0" if op == "and" else "1"
                roots = self._merge_roots(*zeros)
                return [(out_bit, out_value, roots, f"{ctx.path_str}.{cell_name} {cell_type}: 控制值传播")]
            if len(ones) == len(states) and states:
                out_value = "1" if op == "and" else "0"
                roots = self._merge_roots(*states)
                return [(out_bit, out_value, roots, f"{ctx.path_str}.{cell_name} {cell_type}: 全部输入已知")]
            return []

        # -------- OR / NOR --------
        if op in {"or", "nor"}:
            states = input_states(input_ports)
            ones = [s for s in states if s and s.value == "1"]
            zeros = [s for s in states if s and s.value == "0"]
            if ones:
                out_value = "1" if op == "or" else "0"
                roots = self._merge_roots(*ones)
                return [(out_bit, out_value, roots, f"{ctx.path_str}.{cell_name} {cell_type}: 控制值传播")]
            if len(zeros) == len(states) and states:
                out_value = "0" if op == "or" else "1"
                roots = self._merge_roots(*states)
                return [(out_bit, out_value, roots, f"{ctx.path_str}.{cell_name} {cell_type}: 全部输入已知")]
            return []

        # -------- NOT / BUF --------
        if op in {"not", "buf"}:
            if not input_ports:
                return []
            src = input_state(input_ports[0])
            if src is None:
                return []
            out_value = src.value if op == "buf" else ("1" if src.value == "0" else "0")
            return [(out_bit, out_value, set(src.root_ids), f"{ctx.path_str}.{cell_name} {cell_type}: 单输入传播")]

        # -------- XOR / XNOR --------
        if op in {"xor", "xnor"}:
            states = input_states(input_ports)
            if not states or not self._all_known(states):
                return []
            parity = sum(1 for s in states if s and s.value == "1") % 2
            out_value = str(parity)
            if op == "xnor":
                out_value = "0" if out_value == "1" else "1"
            return [(out_bit, out_value, self._merge_roots(*states), f"{ctx.path_str}.{cell_name} {cell_type}: 奇偶传播")]

        # -------- MUX / PMUX --------
        if op in {"mux", "pmux"}:
            a = input_state("A")
            b = input_state("B")
            s = input_state("S")
            if a and b and a.value == b.value:
                return [(
                    out_bit,
                    a.value,
                    self._merge_roots(a, b),
                    f"{ctx.path_str}.{cell_name} {cell_type}: 两路数据相同",
                )]
            if s and s.value == "0" and a:
                return [(
                    out_bit,
                    a.value,
                    self._merge_roots(s, a),
                    f"{ctx.path_str}.{cell_name} {cell_type}: 选择 A",
                )]
            if s and s.value == "1" and b:
                return [(
                    out_bit,
                    b.value,
                    self._merge_roots(s, b),
                    f"{ctx.path_str}.{cell_name} {cell_type}: 选择 B",
                )]
            return []

        # -------- reduce_* --------
        if op in {"reduce_and", "reduce_or", "reduce_xor", "reduce_xnor"}:
            a_bits = connections.get("A", [])
            states = [self._get_state(ctx, bit, f"{ctx.path_str}.{cell_name}.A") for bit in a_bits]
            if not states:
                return []
            if op == "reduce_and":
                zeros = [s for s in states if s and s.value == "0"]
                if zeros:
                    return [(
                        out_bit,
                        "0",
                        self._merge_roots(*zeros),
                        f"{ctx.path_str}.{cell_name} {cell_type}: reduce_and 控制值",
                    )]
                if self._all_known(states):
                    return [(
                        out_bit,
                        "1",
                        self._merge_roots(*states),
                        f"{ctx.path_str}.{cell_name} {cell_type}: reduce_and 全已知",
                    )]
                return []
            if op == "reduce_or":
                ones = [s for s in states if s and s.value == "1"]
                if ones:
                    return [(
                        out_bit,
                        "1",
                        self._merge_roots(*ones),
                        f"{ctx.path_str}.{cell_name} {cell_type}: reduce_or 控制值",
                    )]
                if self._all_known(states):
                    return [(
                        out_bit,
                        "0",
                        self._merge_roots(*states),
                        f"{ctx.path_str}.{cell_name} {cell_type}: reduce_or 全已知",
                    )]
                return []
            if self._all_known(states):
                parity = sum(1 for s in states if s and s.value == "1") % 2
                out_value = str(parity)
                if op == "reduce_xnor":
                    out_value = "0" if out_value == "1" else "1"
                return [(
                    out_bit,
                    out_value,
                    self._merge_roots(*states),
                    f"{ctx.path_str}.{cell_name} {cell_type}: reduce_xor/xnor 全已知",
                )]
            return []

        # -------- EQ / NE --------
        if op in {"eq", "ne"}:
            a_bits = connections.get("A", [])
            b_bits = connections.get("B", [])
            if len(a_bits) != len(b_bits) or not a_bits:
                return []
            a_states = [self._get_state(ctx, bit, f"{ctx.path_str}.{cell_name}.A") for bit in a_bits]
            b_states = [self._get_state(ctx, bit, f"{ctx.path_str}.{cell_name}.B") for bit in b_bits]
            if not self._all_known(a_states) or not self._all_known(b_states):
                return []
            a_val = "".join(s.value for s in a_states if s)
            b_val = "".join(s.value for s in b_states if s)
            equal = a_val == b_val
            out_value = "1" if equal else "0"
            if op == "ne":
                out_value = "0" if out_value == "1" else "1"
            return [(
                out_bit,
                out_value,
                self._merge_roots(*a_states, *b_states),
                f"{ctx.path_str}.{cell_name} {cell_type}: 比较结果可确定",
            )]

        return []

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
                for bit in bits:
                    if bit in CONST_BITS:
                        continue
                    reason = self.reason_map.get(self._node_key(ctx, bit), "")
                    if reason and reason not in reason_parts:
                        reason_parts.append(reason)
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
                "roots": ", ".join(record.root_ids),
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
                "roots": ", ".join(record.root_ids),
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
                "roots": ", ".join(record.root_ids),
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
        print("步骤 1: 导出层次化 Yosys JSON 网表...")
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
                f"发现 {summary['total_constant_wires']} 个层次化常量线网/根源信号。"
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
                "根源": item.get("roots", ""),
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
                    "层次化常量线网和根源数量": summary["total_constant_wires"],
                    "污染簇数量": summary["total_root_clusters"],
                    "冲突点数量": summary["total_conflicts"],
                "潜在问题": list(summary["potential_issues"]),
                },
                "最源头常量引脚和线网": [convert_root(root) for root in analysis_results["root_causes"]],
                "按根源分组的污染常量集合": convert_cluster(analysis_results["root_pollution_clusters"]),
                "层次化常量输出": [convert_signal(item) for item in findings["hierarchical_constant_outputs"]],
                "层次化常量输入": [convert_signal(item) for item in findings["hierarchical_constant_inputs"]],
                "层次化常量线网和根源信号": [convert_signal(item) for item in findings["hierarchical_constant_wires"]],
                "模块单元统计": findings["cell_stats_by_module"],
                "冲突和注意事项": findings["conflicts"],
                "分析边界": [
                    "该工具会跨模块、跨实例向下和向上追踪常量传播。",
                    "对触发器、锁存器、存储器等时序单元默认作为传播边界，不把其输出直接判定为常量。",
                    "若 Yosys 在前端归一化时把多个同值根源折叠为同一个字面量常量位，报告会保守地把相关根源一并列为候选。",
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
