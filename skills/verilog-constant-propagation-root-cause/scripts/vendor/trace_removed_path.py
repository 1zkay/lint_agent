#!/usr/bin/env python3
"""Source-vs-optimized constant-propagation tracer.

The user-facing question is deliberately narrow: which source-level
combinational logic disappeared after Yosys optimization, and is that
disappearance explained by explicit constant-propagation roots?

The script therefore keeps one main evidence path:
1. Export normal raw/opt JSON, opt RTLIL, and noopt RTLIL.
2. Run the hierarchical constant-propagation analysis inherited from
   trace_modified.py on the raw JSON view.
3. Find source outputs that are rewired directly to constants after opt.
4. Walk the noopt RTLIL source cone for those outputs and report the source
   combinational cells that no longer exist in the optimized module.
"""

import argparse
import json
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from trace_modified import (
    COMB_CELL_TYPES,
    CONST_BITS,
    ConstantTracer,
    InstanceContext,
    SignalConstRecord,
)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "reports" / "gate_error_reports"


@dataclass
class OptimizedConstantOutput:
    signal: str
    parent_path: str
    local_signal: str
    raw_constant_value: str = ""
    after_value: str = ""
    constant_roots: List[str] = field(default_factory=list)


@dataclass
class MissingSourceLogicItem:
    signal: str
    after_value: str
    constant_roots: List[str] = field(default_factory=list)
    caused_by_constant_propagation: bool = False
    missing_cells: List[Dict[str, str]] = field(default_factory=list)
    evidence: str = ""


@dataclass
class YosysCellInfo:
    name: str
    cell_type: str
    src: str = ""


@dataclass
class YosysModuleInfo:
    name: str
    cells: Dict[str, YosysCellInfo] = field(default_factory=dict)
    connects: List[Tuple[List[str], List[str]]] = field(default_factory=list)


class OptimizationDiffRemovedTracer(ConstantTracer):
    """Compare source-like noopt logic with optimized constants."""

    def __init__(self, design_inputs, top_module: str = "top_module", yosys_bin: Optional[str] = None):
        super().__init__(design_inputs, top_module, yosys_bin)
        self.raw_netlist_data: Dict = {}
        self.opt_netlist_data: Dict = {}
        self.before_modules: Dict[str, YosysModuleInfo] = {}
        self.opt_modules: Dict[str, YosysModuleInfo] = {}

        self.raw_context_map: Dict[str, InstanceContext] = {}
        self.raw_signal_records: List[SignalConstRecord] = []
        self.raw_signal_map: Dict[str, SignalConstRecord] = {}
        self.prepared_source_map: Dict[str, str] = {}
        self.raw_json_text: str = ""
        self.opt_json_text: str = ""
        self.raw_proc_rtlil_text: str = ""
        self.opt_proc_rtlil_text: str = ""
        self.noopt_proc_rtlil_text: str = ""

    def _reset_constant_state(self) -> None:
        self.root_causes = {}
        self.context_signal_direct_roots = defaultdict(lambda: defaultdict(set))
        self.const_map = {}
        self.reason_map = {}
        self.noopt_const_map = {}
        self.noopt_reason_map = {}
        self.conflicts = []

    def _export_yosys_designs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="trace_removed_path_") as tempdir:
            raw_json = Path(tempdir) / "raw_design.json"
            opt_json = Path(tempdir) / "opt_design.json"
            raw_proc_rtlil = Path(tempdir) / "raw_proc.il"
            opt_proc_rtlil = Path(tempdir) / "opt_proc.il"
            noopt_proc_rtlil = Path(tempdir) / "noopt_proc.il"
            source_dir = Path(tempdir) / "sources"
            source_dir.mkdir(parents=True, exist_ok=True)
            prepared_files = self._prepare_design_sources(source_dir)
            self.prepared_source_map = {
                self._yosys_path(prepared): str(original)
                for prepared, original in zip(prepared_files, self.selected_files)
            }
            read_cmd = self._read_verilog_cmd(prepared_files)

            raw_script = (
                f"{read_cmd}"
                f"hierarchy -check -top {self.top_module}; "
                "proc; "
                f"write_json {self._yosys_path(raw_json)}; "
                f"write_rtlil {self._yosys_path(raw_proc_rtlil)}"
            )
            self._run_yosys(raw_script, timeout=120)

            opt_script = (
                f"{read_cmd}"
                f"hierarchy -check -top {self.top_module}; "
                "proc; "
                "opt -purge; "
                f"write_json {self._yosys_path(opt_json)}; "
                f"write_rtlil {self._yosys_path(opt_proc_rtlil)}; "
                "stat"
            )
            result = self._run_yosys(opt_script, timeout=120)
            self.yosys_stat = result.stdout

            noopt_script = (
                f"{self._read_verilog_cmd(prepared_files, noopt=True)}"
                f"hierarchy -check -top {self.top_module}; "
                "proc -noopt; "
                f"write_rtlil {self._yosys_path(noopt_proc_rtlil)}"
            )
            self._run_yosys(noopt_script, timeout=120)

            self.raw_json_text = raw_json.read_text(encoding="utf-8")
            self.opt_json_text = opt_json.read_text(encoding="utf-8")
            self.raw_netlist_data = json.loads(self.raw_json_text)
            self.opt_netlist_data = json.loads(self.opt_json_text)
            self.raw_proc_rtlil_text = raw_proc_rtlil.read_text(encoding="utf-8", errors="ignore")
            self.raw_rtlil_text = self.raw_proc_rtlil_text
            self.opt_proc_rtlil_text = opt_proc_rtlil.read_text(encoding="utf-8", errors="ignore")
            self.noopt_proc_rtlil_text = noopt_proc_rtlil.read_text(
                encoding="utf-8",
                errors="ignore",
            )
            self.noopt_rtlil_text = self.noopt_proc_rtlil_text
            self.noopt_modules = self._parse_noopt_rtlil_modules(self.noopt_rtlil_text)
            self._index_noopt_direct_constant_connections()
            self.before_modules = self._build_yosys_modules(
                self.raw_netlist_data,
                self.raw_proc_rtlil_text,
            )
            self.opt_modules = self._build_yosys_modules(
                self.opt_netlist_data,
                self.opt_proc_rtlil_text,
            )

        self.modules_data = self.raw_netlist_data.get("modules", {})

    @staticmethod
    def _context_map(contexts: Iterable[InstanceContext]) -> Dict[str, InstanceContext]:
        return {ctx.path_str: ctx for ctx in contexts}

    @staticmethod
    def _rtlil_unescape_id(token: str) -> str:
        token = token.strip()
        if token.startswith("\\"):
            return token[1:]
        return token

    def _rtlil_parse_sigspec(self, text: str) -> List[str]:
        tokens = re.findall(r"\\\S+|\$\S+|[0-9]+'[01xz]+|[01xz]", text)
        return [self._rtlil_unescape_id(token) for token in tokens]

    @staticmethod
    def _split_rtlil_connect_operands(text: str) -> Tuple[str, str]:
        text = text.strip()
        depth_brace = 0
        started = False

        for idx, char in enumerate(text):
            if char == "{":
                depth_brace += 1
                started = True
                continue
            if char == "}":
                depth_brace = max(0, depth_brace - 1)
                started = True
                continue
            if char.isspace() and depth_brace == 0 and started:
                lhs = text[:idx].strip()
                rhs = text[idx:].strip()
                if lhs and rhs:
                    return lhs, rhs
            if not char.isspace():
                started = True

        raise ValueError(f"Unable to split RTLIL connect operands: {text}")

    def _restore_original_src(self, src: str) -> str:
        normalized = src
        for prepared_path, original_path in self.prepared_source_map.items():
            if normalized.startswith(prepared_path):
                return original_path + normalized[len(prepared_path):]
        return src

    def _parse_rtlil_module_connects(
        self, rtlil_text: str
    ) -> Dict[str, List[Tuple[List[str], List[str]]]]:
        connects: Dict[str, List[Tuple[List[str], List[str]]]] = defaultdict(list)
        current_module: Optional[str] = None
        block_stack: List[str] = []

        for raw_line in rtlil_text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            if stripped == "end":
                ended_block = block_stack.pop() if block_stack else ""
                if ended_block == "module":
                    current_module = None
                continue

            if stripped.startswith("module "):
                current_module = self._rtlil_unescape_id(stripped.split(None, 1)[1])
                block_stack.append("module")
                continue

            if current_module is None:
                continue

            top_block = block_stack[-1] if block_stack else ""
            if top_block == "module" and stripped.startswith("connect "):
                lhs, rhs = self._split_rtlil_connect_operands(stripped[len("connect "):])
                connects[current_module].append(
                    (self._rtlil_parse_sigspec(lhs), self._rtlil_parse_sigspec(rhs))
                )
                continue

            if top_block == "module" and stripped.startswith("process "):
                block_stack.append("process")
                continue

            if top_block in {"module", "process", "switch"} and stripped.startswith("switch "):
                block_stack.append("switch")
                continue

            if top_block == "module" and stripped.startswith("memory "):
                block_stack.append("memory")
                continue

            if top_block == "module" and stripped.startswith("cell "):
                block_stack.append("cell")
                continue

        return {module: list(items) for module, items in connects.items()}

    def _build_yosys_modules(
        self, netlist_data: Dict, rtlil_text: str
    ) -> Dict[str, YosysModuleInfo]:
        rtlil_connects = self._parse_rtlil_module_connects(rtlil_text)
        modules: Dict[str, YosysModuleInfo] = {}

        for module_name, module_data in netlist_data.get("modules", {}).items():
            module_info = YosysModuleInfo(
                name=module_name,
                connects=rtlil_connects.get(module_name, []),
            )
            for cell_name, cell_data in module_data.get("cells", {}).items():
                attributes = cell_data.get("attributes", {})
                module_info.cells[cell_name] = YosysCellInfo(
                    name=cell_name,
                    cell_type=cell_data.get("type", ""),
                    src=self._restore_original_src(attributes.get("src", "")),
                )
            modules[module_name] = module_info
        return modules

    @staticmethod
    def _rtlil_constant_token_bits(token: str) -> Optional[str]:
        token = token.strip()
        if token in {"0", "1"}:
            return token
        match = re.fullmatch(r"(\d+)'([01xz]+)", token)
        if not match:
            return None
        return match.group(2)

    def _rtlil_constant_value_from_tokens(self, tokens: List[str]) -> Optional[str]:
        if not tokens:
            return None

        bit_chunks: List[str] = []
        for token in tokens:
            bits = self._rtlil_constant_token_bits(token)
            if bits is None:
                return None
            bit_chunks.append(bits)

        bit_string = "".join(bit_chunks)
        if not bit_string or any(bit not in {"0", "1"} for bit in bit_string):
            return None
        return f"{len(bit_string)}'b{bit_string}"

    def _rtlil_direct_driver_tokens(
        self, module_info: YosysModuleInfo, local_name: str
    ) -> Optional[List[str]]:
        for lhs_tokens, rhs_tokens in module_info.connects:
            if lhs_tokens == [local_name]:
                return list(rhs_tokens)
        return None

    def _rtlil_direct_constant(
        self, module_info: YosysModuleInfo, local_name: str
    ) -> Optional[str]:
        tokens = self._rtlil_direct_driver_tokens(module_info, local_name)
        if tokens is None:
            return None
        return self._rtlil_constant_value_from_tokens(tokens)

    def _normalize_src_key(self, src: str) -> str:
        src = self._restore_original_src(src)
        match = re.match(r"^(.*):(\d+)(?:\.\d+(?:-\d+\.\d+)?)?$", src)
        if match:
            return f"{match.group(1)}:{match.group(2)}"
        return src

    def _is_combinational_cell_type(self, cell_type: str) -> bool:
        if self._is_sequential_cell_type(cell_type):
            return False
        normalized = self._normalize_cell_type(cell_type)
        return cell_type in COMB_CELL_TYPES or normalized != cell_type

    def _source_cell_signature(self, cell_type: str, src: str) -> Tuple[str, str]:
        return (
            self._normalize_cell_type(cell_type),
            self._normalize_src_key(src),
        )

    def _collect_optimized_constant_outputs(self) -> List[OptimizedConstantOutput]:
        """Find source outputs that optimization rewired directly to constants."""
        outputs: List[OptimizedConstantOutput] = []
        seen: Set[str] = set()

        for record in self.raw_signal_records:
            if record.signal_kind not in {"output", "inout"}:
                continue

            if record.hierarchical_signal in seen:
                continue
            ctx = self.raw_context_map.get(record.module)
            if ctx is None:
                continue

            local_name = record.hierarchical_signal.split(".")[-1]
            before_module_info = self.before_modules.get(ctx.module_name)
            opt_module_info = self.opt_modules.get(ctx.module_name)
            if before_module_info is None or opt_module_info is None:
                continue

            after_value = self._rtlil_direct_constant(opt_module_info, local_name)
            if after_value is None:
                continue
            before_value = self._rtlil_direct_constant(before_module_info, local_name)
            if before_value == after_value:
                continue

            outputs.append(
                OptimizedConstantOutput(
                    signal=record.hierarchical_signal,
                    parent_path=ctx.path_str,
                    local_signal=local_name,
                    raw_constant_value=record.constant_value,
                    after_value=after_value,
                    constant_roots=sorted(record.root_ids),
                )
            )
            seen.add(record.hierarchical_signal)

        return outputs

    @staticmethod
    def _noopt_output_tokens(cell: Dict) -> List[str]:
        connections = cell.get("connections", {})
        for port_name in ("Y", "Q", "O"):
            tokens = connections.get(port_name)
            if tokens:
                return list(tokens)
        return []

    def _noopt_driver_indexes(self, module_name: str) -> Tuple[Dict[str, List[str]], Dict[str, Dict]]:
        module = self.noopt_modules.get(module_name, {})
        connect_drivers: Dict[str, List[str]] = {}
        cell_output_drivers: Dict[str, Dict] = {}

        for dst_tokens, src_tokens in module.get("connects", []):
            if not dst_tokens or not src_tokens:
                continue
            if len(src_tokens) == 1:
                for dst_token in dst_tokens:
                    connect_drivers[dst_token] = list(src_tokens)
                continue
            if len(dst_tokens) != len(src_tokens):
                continue
            for dst_token, src_token in zip(dst_tokens, src_tokens):
                connect_drivers[dst_token] = [src_token]

        for cell in module.get("cells", []):
            for token in self._noopt_output_tokens(cell):
                cell_output_drivers[token] = cell

        return connect_drivers, cell_output_drivers

    def _noopt_source_cone_cells(
        self,
        ctx: InstanceContext,
        local_signal: str,
    ) -> List[Dict]:
        connect_drivers, cell_output_drivers = self._noopt_driver_indexes(ctx.module_name)
        queue: List[str] = [local_signal]
        visited_tokens: Set[str] = set()
        cells: Dict[str, Dict] = {}

        while queue:
            token = queue.pop(0)
            if not token or token.lower() in CONST_BITS or token in visited_tokens:
                continue
            visited_tokens.add(token)

            for src_token in connect_drivers.get(token, []):
                if src_token.lower() not in CONST_BITS:
                    queue.append(src_token)

            cell = cell_output_drivers.get(token)
            if not cell:
                continue
            if not self._is_combinational_cell_type(cell.get("type", "")):
                continue
            cells[cell.get("name", "")] = cell

            output_ports = {"Y", "Q", "O"}
            for port_name, input_tokens in cell.get("connections", {}).items():
                if port_name in output_ports:
                    continue
                for input_token in input_tokens:
                    if input_token.lower() not in CONST_BITS:
                        queue.append(input_token)

        return sorted(
            cells.values(),
            key=lambda cell: (
                self._normalize_src_key(cell.get("src", "")),
                cell.get("name", ""),
            ),
        )

    def _source_snippet_for_src(self, src: str) -> str:
        _, snippet = self._source_slice_from_yosys_src(src)
        return " ".join(snippet.split())

    def _collect_missing_source_logic(
        self,
        optimized_outputs: List[OptimizedConstantOutput],
    ) -> List[MissingSourceLogicItem]:
        items: List[MissingSourceLogicItem] = []

        for item in optimized_outputs:
            ctx = self.raw_context_map.get(item.parent_path)
            opt_module_info = self.opt_modules.get(ctx.module_name) if ctx is not None else None
            if ctx is None or opt_module_info is None:
                continue

            root_ids = sorted(item.constant_roots)
            opt_cell_signature_counts = Counter(
                self._source_cell_signature(cell.cell_type, cell.src)
                for cell in opt_module_info.cells.values()
                if self._is_combinational_cell_type(cell.cell_type)
            )
            source_cells = self._noopt_source_cone_cells(
                ctx,
                item.local_signal,
            )

            missing_cells: List[Dict[str, str]] = []
            for cell in source_cells:
                cell_name = cell.get("name", "")
                if not cell_name:
                    continue
                signature = self._source_cell_signature(cell.get("type", ""), cell.get("src", ""))
                if cell_name in opt_module_info.cells:
                    if opt_cell_signature_counts[signature] > 0:
                        opt_cell_signature_counts[signature] -= 1
                    continue
                if opt_cell_signature_counts[signature] > 0:
                    opt_cell_signature_counts[signature] -= 1
                    continue
                src = cell.get("src", "")
                missing_cells.append(
                    {
                        "单元": cell_name,
                        "类型": cell.get("type", ""),
                        "源码位置": src,
                        "源码片段": self._source_snippet_for_src(src),
                    }
                )

            if not missing_cells:
                continue

            caused_by_constant = bool(root_ids) and item.raw_constant_value == item.after_value
            evidence = (
                f"{item.signal} 优化后直接连接为 {item.after_value}；"
                f"noopt 源码展开结构中有 {len(missing_cells)} 个相关组合单元在 opt 结构中不存在。"
            )
            if caused_by_constant:
                evidence += " 这些消失单元的输出常量可追溯到显式常量根源。"
            else:
                evidence += " 未能把该消失现象追溯到显式常量传播根源。"

            items.append(
                MissingSourceLogicItem(
                    signal=item.signal,
                    after_value=item.after_value,
                    constant_roots=root_ids,
                    caused_by_constant_propagation=caused_by_constant,
                    missing_cells=missing_cells,
                    evidence=evidence,
                )
            )

        return items

    def analyze_design(self) -> Dict:
        print("步骤 1: 导出 raw/opt JSON、noopt RTLIL，并附带 raw/opt RTLIL 审计文件...")
        self._export_yosys_designs()

        print("步骤 2: 基于 raw JSON 建立层次索引并执行常量传播...")
        self._reset_constant_state()
        self._build_module_indices()
        self._build_context_tree()
        self._run_fixpoint()
        self.raw_signal_records = self._collect_signal_constants()
        self.raw_signal_map = {
            record.hierarchical_signal: record for record in self.raw_signal_records
        }
        self.raw_context_map = self._context_map(self.all_contexts_preorder)

        print("步骤 3: 对比 noopt 源码组合锥和 opt 后直连常量输出...")
        optimized_outputs = self._collect_optimized_constant_outputs()
        missing_source_logic = self._collect_missing_source_logic(optimized_outputs)
        referenced_roots = sorted(
            {
                root_id
                for item in missing_source_logic
                for root_id in item.constant_roots
            }
        )
        constant_propagation_missing_count = sum(
            1 for item in missing_source_logic if item.caused_by_constant_propagation
        )

        summary = {
            "missing_source_logic_count": len(missing_source_logic),
            "constant_propagation_missing_logic_count": constant_propagation_missing_count,
            "referenced_root_count": len(referenced_roots),
            "conflict_count": len(self.conflicts),
            "potential_issues": [],
        }
        if summary["missing_source_logic_count"] > 0:
            summary["potential_issues"].append(
                f"发现 {summary['missing_source_logic_count']} 组源码逻辑在优化后消失。"
            )
        if summary["constant_propagation_missing_logic_count"] > 0:
            summary["potential_issues"].append(
                f"其中 {summary['constant_propagation_missing_logic_count']} 组可追溯到常量传播根源。"
            )
        if summary["referenced_root_count"] > 0:
            summary["potential_issues"].append(
                f"涉及 {summary['referenced_root_count']} 个显式常量根源。"
            )
        if summary["conflict_count"] > 0:
            summary["potential_issues"].append(
                f"常量传播阶段发现 {summary['conflict_count']} 个冲突推断。"
            )

        return {
            "summary": summary,
            "missing_source_logic": [asdict(item) for item in missing_source_logic],
            "extra_exports": {
                "raw_json": "raw_design.json",
                "opt_json": "opt_design.json",
                "raw_proc_il": "raw_proc.il",
                "opt_proc_il": "opt_proc.il",
                "noopt_proc_il": "noopt_proc.il",
            },
        }

    def build_json_report(self, analysis_results: Dict) -> Dict:
        summary = analysis_results["summary"]

        def convert_missing_source_logic(item: Dict) -> Dict:
            return {
                "信号": item["signal"],
                "优化后常量值": item["after_value"],
                "是否因为常量传播消失": item["caused_by_constant_propagation"],
                "常量根源": item.get("constant_roots", []),
                "源码相比优化后少掉的组合单元": item.get("missing_cells", []),
                "证据": item.get("evidence", ""),
            }

        return {
            "报告类型": "源码与优化后结构差异常量传播分析",
            "报告格式": "json",
            "生成时间": datetime.now().isoformat(timespec="seconds"),
            "设计输入": list(self.design_inputs),
            "主输入": self.primary_input,
            "顶层模块": self.top_module,
            "Yosys路径": self.yosys_bin,
            "分析结果": {
                "摘要": {
                    "源码相比优化后少掉的逻辑组数量": summary["missing_source_logic_count"],
                    "因为常量传播少掉的逻辑组数量": summary[
                        "constant_propagation_missing_logic_count"
                    ],
                    "涉及常量根源数量": summary["referenced_root_count"],
                    "冲突数量": summary["conflict_count"],
                    "潜在问题": list(summary["potential_issues"]),
                },
                "源码相比优化后少掉的逻辑": [
                    convert_missing_source_logic(item)
                    for item in analysis_results["missing_source_logic"]
                ],
                "附加导出文件": {
                    "raw_json": analysis_results["extra_exports"]["raw_json"],
                    "opt_json": analysis_results["extra_exports"]["opt_json"],
                    "raw_proc_il": analysis_results["extra_exports"]["raw_proc_il"],
                    "opt_proc_il": analysis_results["extra_exports"]["opt_proc_il"],
                    "noopt_proc_il": analysis_results["extra_exports"]["noopt_proc_il"],
                },
            },
        }


def main() -> int:
    def sanitize_name(name: str) -> str:
        cleaned = re.sub(r'[<>:"/\\\\|?*\\s]+', "_", name).strip("._")
        return cleaned or "top_module"

    def build_run_output_dir(report_root: Path, top_name: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"{sanitize_name(top_name)}_{timestamp}"
        candidate = report_root / base_name
        suffix = 1
        while candidate.exists():
            candidate = report_root / f"{base_name}_{suffix:02d}"
            suffix += 1
        return candidate

    parser = argparse.ArgumentParser(
        description=(
            "对比 noopt 源码组合锥与 opt 优化后结构，报告源码相比优化后少掉的组合逻辑，"
            "并判断这些逻辑是否因显式常量传播根源而消失。"
        )
    )
    parser.add_argument(
        "design_inputs",
        nargs="+",
        help="Verilog 源文件或源码目录。",
    )
    parser.add_argument("--top", default="top_module", help="顶层模块名。")
    parser.add_argument(
        "--output",
        default=None,
        help="报告输出文件路径。若不指定，则自动保存到报告根目录下按顶层名和时间创建的子目录中。",
    )
    parser.add_argument(
        "--report-root",
        default=str(DEFAULT_REPORT_ROOT),
        help="自动保存模式下的报告根目录。",
    )
    parser.add_argument(
        "--yosys",
        default=None,
        help="Yosys 可执行文件路径。可省略；脚本会优先从 --yosys、YOSYS_BIN、PATH 或附近的 oss-cad-suite 自动查找。",
    )

    args = parser.parse_args()

    try:
        tracer = OptimizationDiffRemovedTracer(args.design_inputs, args.top, args.yosys)
        results = tracer.analyze_design()
        json_report = tracer.build_json_report(results)

        if args.output:
            output_path = Path(args.output)
            output_dir = output_path.parent
        else:
            report_root = Path(args.report_root)
            output_dir = build_run_output_dir(report_root, args.top)
            output_path = output_dir / "trace_removed_path_report.json"

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(json_report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        raw_json_path = output_dir / "raw_design.json"
        opt_json_path = output_dir / "opt_design.json"
        raw_proc_path = output_dir / "raw_proc.il"
        opt_proc_path = output_dir / "opt_proc.il"
        noopt_proc_path = output_dir / "noopt_proc.il"
        raw_json_path.write_text(tracer.raw_json_text, encoding="utf-8")
        opt_json_path.write_text(tracer.opt_json_text, encoding="utf-8")
        raw_proc_path.write_text(tracer.raw_proc_rtlil_text, encoding="utf-8")
        opt_proc_path.write_text(tracer.opt_proc_rtlil_text, encoding="utf-8")
        noopt_proc_path.write_text(tracer.noopt_proc_rtlil_text, encoding="utf-8")
        summary = results["summary"]
        print(
            "\n摘要: "
            f"少掉逻辑组={summary['missing_source_logic_count']}，"
            f"常量传播导致={summary['constant_propagation_missing_logic_count']}，"
            f"涉及根源={summary['referenced_root_count']}，"
            f"冲突={summary['conflict_count']}"
        )
        print(f"\n输出目录: {output_dir}")
        print(f"\n报告已保存到: {output_path}")
        print(f"附加导出文件: {raw_json_path}")
        print(f"附加导出文件: {opt_json_path}")
        print(f"附加导出文件: {raw_proc_path}")
        print(f"附加导出文件: {opt_proc_path}")
        print(f"附加导出文件: {noopt_proc_path}")

        has_issue = summary["constant_propagation_missing_logic_count"] > 0
        if has_issue:
            print("\n检测到源码逻辑因常量传播在优化后消失。")
            return 1

        if summary["missing_source_logic_count"] > 0:
            print("\n检测到源码逻辑在优化后消失，但未追溯到显式常量传播根源。")
        else:
            print("\n未检测到源码逻辑因常量传播在优化后消失。")
        return 0

    except FileNotFoundError as exc:
        print(f"错误: {exc}")
        return 2
    except ValueError as exc:
        print(f"错误: {exc}")
        return 2
    except RuntimeError as exc:
        print(f"错误: Yosys 执行失败。\n{exc}")
        return 2
    except KeyboardInterrupt:
        print("已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
