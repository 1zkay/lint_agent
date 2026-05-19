#!/usr/bin/env python3
"""Hierarchical raw/opt Yosys diff based constant propagation tracer.

This script uses a different strategy from trace_modified.py:
1. Export hierarchical raw/opt Yosys JSON designs, noopt RTLIL for root
   provenance, and raw/opt RTLIL only for optimization driver diff evidence.
2. Compare raw vs opt JSON hierarchy to find removed module instances and
   removed local combinational cells.
3. Run the raw hierarchical constant-propagation analysis from
   trace_modified.py.
4. For each removed item, inspect the raw outputs that became constant and
   trace them back to explicit constant roots with an estimated path.
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
    Bit,
    COMB_CELL_TYPES,
    CONST_BITS,
    ConstantTracer,
    InstanceContext,
    SignalConstRecord,
)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "reports" / "gate_error_reports"


@dataclass
class PropagationPath:
    root_id: str
    root_signal: str
    root_value: str
    path: List[str] = field(default_factory=list)


@dataclass
class AffectedSignal:
    signal: str
    value: str
    kind: str
    aliases: List[str] = field(default_factory=list)
    roots: List[str] = field(default_factory=list)
    reason: str = ""
    propagation_paths: List[PropagationPath] = field(default_factory=list)


@dataclass
class RemovedItem:
    kind: str
    path: str
    parent_path: str
    item_name: str
    item_type: str
    src: str = ""
    affected_signals: List[AffectedSignal] = field(default_factory=list)


@dataclass
class ConstantizedSignalItem:
    kind: str
    path: str
    parent_path: str
    signal: str
    signal_kind: str
    before_driver: str = ""
    raw_folded_value: str = ""
    after_value: str = ""
    affected_signals: List[AffectedSignal] = field(default_factory=list)


@dataclass
class YosysCellInfo:
    name: str
    cell_type: str
    src: str = ""
    connections: Dict[str, List[Bit]] = field(default_factory=dict)
    port_directions: Dict[str, str] = field(default_factory=dict)


@dataclass
class YosysModuleInfo:
    name: str
    cells: Dict[str, YosysCellInfo] = field(default_factory=dict)
    connects: List[Tuple[List[str], List[str]]] = field(default_factory=list)
    inputs: Set[str] = field(default_factory=set)
    outputs: Set[str] = field(default_factory=set)
    name_to_bits: Dict[str, List[Bit]] = field(default_factory=dict)
    bit_to_names: Dict[Bit, List[str]] = field(default_factory=lambda: defaultdict(list))


class OptimizationDiffRemovedTracer(ConstantTracer):
    """Compare raw/opt Yosys structures and backtrace removed items to roots."""

    def __init__(self, design_inputs, top_module: str = "top_module", yosys_bin: Optional[str] = None):
        super().__init__(design_inputs, top_module, yosys_bin)
        self.raw_netlist_data: Dict = {}
        self.opt_netlist_data: Dict = {}
        self.before_modules: Dict[str, YosysModuleInfo] = {}
        self.opt_modules: Dict[str, YosysModuleInfo] = {}
        self.before_contexts_preorder: List[InstanceContext] = []
        self.opt_contexts_preorder: List[InstanceContext] = []

        self.raw_context_map: Dict[str, InstanceContext] = {}
        self.opt_context_map: Dict[str, InstanceContext] = {}
        self.raw_signal_records: List[SignalConstRecord] = []
        self.raw_signal_map: Dict[str, SignalConstRecord] = {}
        self.prepared_source_map: Dict[str, str] = {}
        self.raw_json_text: str = ""
        self.opt_json_text: str = ""
        self.raw_proc_rtlil_text: str = ""
        self.opt_proc_rtlil_text: str = ""
        self.noopt_proc_rtlil_text: str = ""

        self.forward_graph: Dict[Tuple[str, Bit], Set[Tuple[str, Bit]]] = defaultdict(set)
        self.reverse_graph: Dict[Tuple[str, Bit], Set[Tuple[str, Bit]]] = defaultdict(set)

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
            for port_name, port_data in module_data.get("ports", {}).items():
                direction = port_data.get("direction", "")
                bits = list(port_data.get("bits", []))
                module_info.name_to_bits[port_name] = bits
                if direction in {"input", "inout"}:
                    module_info.inputs.add(port_name)
                if direction in {"output", "inout"}:
                    module_info.outputs.add(port_name)
                for bit in bits:
                    module_info.bit_to_names[bit].append(port_name)

            for net_name, net_data in module_data.get("netnames", {}).items():
                bits = list(net_data.get("bits", []))
                if not bits:
                    continue
                module_info.name_to_bits[net_name] = bits
                for bit in bits:
                    module_info.bit_to_names[bit].append(net_name)

            for cell_name, cell_data in module_data.get("cells", {}).items():
                attributes = cell_data.get("attributes", {})
                module_info.cells[cell_name] = YosysCellInfo(
                    name=cell_name,
                    cell_type=cell_data.get("type", ""),
                    src=self._restore_original_src(attributes.get("src", "")),
                    connections={
                        port: list(bits)
                        for port, bits in cell_data.get("connections", {}).items()
                    },
                    port_directions=dict(cell_data.get("port_directions", {})),
                )
            modules[module_name] = module_info
        return modules

    def _build_yosys_context_tree(
        self, modules: Dict[str, YosysModuleInfo]
    ) -> Tuple[Optional[InstanceContext], List[InstanceContext], Dict[str, InstanceContext]]:
        def build(module_name: str, path: Tuple[str, ...]) -> InstanceContext:
            ctx = InstanceContext(module_name=module_name, path=path)
            module_info = modules.get(module_name)
            if module_info is None:
                return ctx
            for cell_name, cell in sorted(module_info.cells.items()):
                if cell.cell_type in modules:
                    ctx.children[cell_name] = build(cell.cell_type, path + (cell_name,))
            return ctx

        root_context = build(self.top_module, (self.top_module,))
        contexts_preorder: List[InstanceContext] = []

        def walk(ctx: InstanceContext) -> None:
            contexts_preorder.append(ctx)
            for child_name in sorted(ctx.children):
                walk(ctx.children[child_name])

        walk(root_context)
        return root_context, contexts_preorder, self._context_map(contexts_preorder)

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

    def _rtlil_direct_driver(
        self, module_info: YosysModuleInfo, local_name: str
    ) -> Optional[str]:
        tokens = self._rtlil_direct_driver_tokens(module_info, local_name)
        if tokens is None:
            return None
        return " ".join(tokens)

    def _rtlil_direct_constant(
        self, module_info: YosysModuleInfo, local_name: str
    ) -> Optional[str]:
        tokens = self._rtlil_direct_driver_tokens(module_info, local_name)
        if tokens is None:
            return None
        return self._rtlil_constant_value_from_tokens(tokens)

    def _rtlil_constant_through_driver(
        self, module_info: YosysModuleInfo, local_name: str
    ) -> Tuple[str, str]:
        driver_tokens = self._rtlil_direct_driver_tokens(module_info, local_name)
        if not driver_tokens or len(driver_tokens) != 1:
            return "", ""

        driver = driver_tokens[0]
        if driver in module_info.inputs or driver in module_info.outputs:
            return driver, ""

        folded_value = self._rtlil_direct_constant(module_info, driver) or ""
        return driver, folded_value

    @staticmethod
    def _is_json_const_bit(bit: Bit) -> bool:
        return isinstance(bit, str) and bit.lower() in {"0", "1", "x", "z"}

    def _preferred_json_names_for_bits(
        self, module_info: YosysModuleInfo, bits: List[Bit]
    ) -> List[str]:
        if not bits or any(self._is_json_const_bit(bit) for bit in bits):
            return []

        exact_names = [
            name
            for name, name_bits in module_info.name_to_bits.items()
            if list(name_bits) == list(bits)
        ]
        if not exact_names and len(bits) == 1:
            exact_names = list(module_info.bit_to_names.get(bits[0], []))

        public_names = [name for name in exact_names if not name.startswith("$")]
        selected = public_names or exact_names
        return sorted(set(selected), key=lambda name: (name.startswith("$"), "[" in name, len(name), name))

    def _resolve_yosys_output_signals(
        self, module_info: YosysModuleInfo, cell: YosysCellInfo
    ) -> List[str]:
        fallback_output_ports = {"Y", "Q", "QB", "O", "S"}
        resolved: List[str] = []
        seen: Set[str] = set()

        for port_name, bits in cell.connections.items():
            direction = cell.port_directions.get(port_name)
            if direction != "output" and (
                direction is not None or port_name not in fallback_output_ports
            ):
                continue
            for name in self._preferred_json_names_for_bits(module_info, list(bits)):
                if name not in seen:
                    seen.add(name)
                    resolved.append(name)
        return resolved

    def _resolve_yosys_input_signals(
        self, module_info: YosysModuleInfo, cell: YosysCellInfo
    ) -> List[str]:
        fallback_output_ports = {"Y", "Q", "QB", "O", "S"}
        resolved: List[str] = []
        seen: Set[str] = set()

        for port_name, bits in cell.connections.items():
            direction = cell.port_directions.get(port_name)
            if direction == "output" or (
                direction is None and port_name in fallback_output_ports
            ):
                continue
            for name in self._preferred_json_names_for_bits(module_info, list(bits)):
                if name not in seen:
                    seen.add(name)
                    resolved.append(name)
        return resolved

    def _cell_source(self, cell_data: Dict) -> str:
        return self._restore_original_src(cell_data.get("attributes", {}).get("src", ""))

    def _canonical_comb_type(self, cell_type: str) -> str:
        return self._normalize_cell_type(cell_type)

    def _normalize_src_key(self, src: str) -> str:
        src = self._restore_original_src(src)
        match = re.match(r"^(.*):(\d+)(?:\.\d+(?:-\d+\.\d+)?)?$", src)
        if match:
            return f"{match.group(1)}:{match.group(2)}"
        return src

    def _yosys_local_cell_match_key(
        self, module_info: YosysModuleInfo, cell: YosysCellInfo
    ) -> Tuple[str, str, Tuple[str, ...], Tuple[str, ...]]:
        return (
            self._canonical_comb_type(cell.cell_type),
            self._normalize_src_key(cell.src),
            tuple(sorted(self._resolve_yosys_output_signals(module_info, cell))),
            tuple(sorted(self._resolve_yosys_input_signals(module_info, cell))),
        )

    def _node_key_for_ctx(self, ctx: InstanceContext, bit: Bit) -> Tuple[str, Bit]:
        return (ctx.path_str, bit)

    def _get_context_by_path(self, ctx_path: str) -> Optional[InstanceContext]:
        return self.raw_context_map.get(ctx_path)

    def _node_label(self, node: Tuple[str, Bit]) -> str:
        ctx = self._get_context_by_path(node[0])
        if ctx is None:
            return f"{node[0]}.{node[1]}"
        return self._preferred_hier_signal(ctx, node[1])

    def _add_edge(self, src: Tuple[str, Bit], dst: Tuple[str, Bit]) -> None:
        if src[1] in CONST_BITS or dst[1] in CONST_BITS:
            return
        self.forward_graph[src].add(dst)
        self.reverse_graph[dst].add(src)

    def _build_flow_graph(self) -> None:
        self.forward_graph = defaultdict(set)
        self.reverse_graph = defaultdict(set)

        for ctx in self.all_contexts_preorder:
            module_index = self.module_indices[ctx.module_name]
            for cell_name, cell_data in module_index.cells.items():
                child_ctx = ctx.children.get(cell_name)
                if child_ctx is not None:
                    child_index = self.module_indices[child_ctx.module_name]
                    connections = cell_data.get("connections", {})
                    for port_name, direction in child_index.port_directions.items():
                        parent_bits = connections.get(port_name, [])
                        child_bits = child_index.name_to_bits.get(port_name, [])
                        if not parent_bits or not child_bits or len(parent_bits) != len(child_bits):
                            continue
                        if direction in {"input", "inout"}:
                            for parent_bit, child_bit in zip(parent_bits, child_bits):
                                self._add_edge(
                                    self._node_key_for_ctx(ctx, parent_bit),
                                    self._node_key_for_ctx(child_ctx, child_bit),
                                )
                        if direction in {"output", "inout"}:
                            for child_bit, parent_bit in zip(child_bits, parent_bits):
                                self._add_edge(
                                    self._node_key_for_ctx(child_ctx, child_bit),
                                    self._node_key_for_ctx(ctx, parent_bit),
                                )
                    continue

                cell_type = cell_data.get("type", "")
                if self._is_sequential_cell_type(cell_type):
                    continue
                normalized = self._normalize_cell_type(cell_type)
                if cell_type not in COMB_CELL_TYPES and normalized == cell_type:
                    continue

                directions = cell_data.get("port_directions", {})
                connections = cell_data.get("connections", {})
                input_bits: List[Bit] = []
                output_bits: List[Bit] = []
                for port_name, direction in directions.items():
                    bits = connections.get(port_name, [])
                    if direction == "input":
                        input_bits.extend(bits)
                    elif direction == "output":
                        output_bits.extend(bits)

                for input_bit in input_bits:
                    for output_bit in output_bits:
                        self._add_edge(
                            self._node_key_for_ctx(ctx, input_bit),
                            self._node_key_for_ctx(ctx, output_bit),
                        )

    def _record_to_affected(self, record: SignalConstRecord) -> AffectedSignal:
        paths: List[PropagationPath] = []
        root_ids: List[str] = []
        seen_roots: Set[str] = set()
        ctx = self.raw_context_map.get(record.module)
        bits: List[Bit] = []
        if ctx is not None:
            module_index = self.module_indices[ctx.module_name]
            leaf = record.hierarchical_signal.split(".")[-1]
            bits = module_index.name_to_bits.get(leaf, [])

        for raw_root_id in record.root_ids:
            root_id = raw_root_id
            if root_id in seen_roots:
                continue
            seen_roots.add(root_id)
            root_ids.append(root_id)
            root = self.root_causes.get(root_id)
            if root is None:
                continue
            estimated_path = self._estimate_path_to_root(bits, record.hierarchical_signal, root_id)
            paths.append(
                PropagationPath(
                    root_id=root_id,
                    root_signal=root.hierarchical_signal,
                    root_value=root.constant_value,
                    path=estimated_path,
                )
            )

        return AffectedSignal(
            signal=record.hierarchical_signal,
            value=record.constant_value,
            kind=record.signal_kind,
            aliases=record.aliases,
            roots=root_ids,
            reason=record.reason,
            propagation_paths=paths,
        )

    def _merge_signal_evidence(
        self,
        ctx: InstanceContext,
        local_name: str,
        bits: List[Bit],
        value: str,
        root_ids: Set[str],
    ) -> Tuple[Set[str], List[str]]:
        merged_roots = set(root_ids)
        reason_parts: List[str] = []

        noopt_evidence = self.noopt_const_map.get(self._noopt_key(ctx, local_name))
        if noopt_evidence is not None:
            noopt_value = self._format_const_value([noopt_evidence.value])
            if noopt_value == value:
                merged_roots |= set(noopt_evidence.root_ids)
                noopt_reason = self.noopt_reason_map.get(self._noopt_key(ctx, local_name), "")
                if noopt_reason:
                    reason_parts.append(noopt_reason)

        for bit in bits:
            if bit in CONST_BITS:
                continue
            reason = self.reason_map.get(self._node_key(ctx, bit), "")
            if reason and reason not in reason_parts:
                reason_parts.append(reason)

        literal_reason = self._json_literal_constant_reason(ctx, local_name, bits, value)
        if literal_reason and literal_reason not in reason_parts:
            reason_parts.append(literal_reason)

        return merged_roots, reason_parts

    def _signal_record_for_bits(
        self,
        ctx: InstanceContext,
        bits: List[Bit],
        fallback_signal: str,
        fallback_kind: str,
    ) -> Optional[SignalConstRecord]:
        aliases = self._signal_aliases_hier(ctx, bits)
        for alias in aliases:
            record = self.raw_signal_map.get(alias)
            if record is not None:
                return record

        resolved = self._resolve_signal_constant(ctx, bits, fallback_signal)
        if resolved is None:
            return None

        value, root_ids = resolved
        local_name = fallback_signal.split(".")[-1]
        root_ids, reason_parts = self._merge_signal_evidence(
            ctx,
            local_name,
            bits,
            value,
            root_ids,
        )

        return SignalConstRecord(
            hierarchical_signal=fallback_signal,
            module=ctx.path_str,
            signal_kind=fallback_kind,
            constant_value=value,
            aliases=aliases,
            root_ids=sorted(root_ids),
            reason=" | ".join(reason_parts),
        )

    def _removed_child_outputs(
        self,
        parent_ctx: InstanceContext,
        child_ctx: InstanceContext,
        child_name: str,
        cell_data: Dict,
    ) -> List[AffectedSignal]:
        child_index = self.module_indices[child_ctx.module_name]
        connections = cell_data.get("connections", {})
        affected: List[AffectedSignal] = []
        seen_signals: Set[str] = set()

        for port_name, direction in child_index.port_directions.items():
            if direction not in {"output", "inout"}:
                continue
            parent_bits = connections.get(port_name, [])
            child_bits = child_index.name_to_bits.get(port_name, [])
            if not parent_bits or not child_bits or len(parent_bits) != len(child_bits):
                continue

            aliases = self._signal_aliases_hier(parent_ctx, parent_bits)
            fallback_signal = aliases[0] if aliases else f"{parent_ctx.path_str}.{child_name}.{port_name}"
            record = self._signal_record_for_bits(parent_ctx, parent_bits, fallback_signal, "wire")
            if record is None:
                continue
            if record.hierarchical_signal in seen_signals:
                continue
            seen_signals.add(record.hierarchical_signal)
            affected.append(self._record_to_affected(record))

        return affected

    def _signal_kind_for_local_name(self, ctx: InstanceContext, local_name: str) -> str:
        module_index = self.module_indices[ctx.module_name]
        direction = module_index.port_directions.get(local_name)
        if direction in {"input", "output", "inout"}:
            return direction
        return "wire"

    def _record_for_named_signal(self, ctx: InstanceContext, local_name: str) -> Optional[SignalConstRecord]:
        module_index = self.module_indices[ctx.module_name]
        bits = module_index.name_to_bits.get(local_name, [])
        if not bits:
            return None

        fallback_signal = f"{ctx.path_str}.{local_name}"
        resolved = self._resolve_signal_constant(ctx, bits, fallback_signal)
        if resolved is None:
            return None

        value, root_ids = resolved
        root_ids, reason_parts = self._merge_signal_evidence(
            ctx,
            local_name,
            bits,
            value,
            root_ids,
        )

        return SignalConstRecord(
            hierarchical_signal=fallback_signal,
            module=ctx.path_str,
            signal_kind=self._signal_kind_for_local_name(ctx, local_name),
            constant_value=value,
            aliases=self._signal_aliases_hier(ctx, bits),
            root_ids=sorted(root_ids),
            reason=" | ".join(reason_parts),
        )

    def _removed_before_local_cell_outputs(
        self, ctx: InstanceContext, module_info: YosysModuleInfo, cell: YosysCellInfo
    ) -> List[AffectedSignal]:
        affected: List[AffectedSignal] = []
        seen: Set[str] = set()
        for local_name in self._resolve_yosys_output_signals(module_info, cell):
            if local_name in seen:
                continue
            seen.add(local_name)
            record = self._record_for_named_signal(ctx, local_name)
            if record is None:
                continue
            affected.append(self._record_to_affected(record))
        return affected

    def _root_nodes(self, root_id: str) -> Set[Tuple[str, Bit]]:
        root = self.root_causes.get(root_id)
        if root is None or root.source_type == "literal_connection":
            return set()

        parts = root.hierarchical_signal.split(".")
        if len(parts) < 2:
            return set()
        ctx_path = ".".join(parts[:-1])
        local_name = parts[-1]
        ctx = self.raw_context_map.get(ctx_path)
        if ctx is None:
            return set()

        module_index = self.module_indices[ctx.module_name]
        bits = module_index.name_to_bits.get(local_name, [])
        nodes: Set[Tuple[str, Bit]] = set()
        for bit in bits:
            if bit in CONST_BITS:
                continue
            evidence = self.const_map.get(self._node_key(ctx, bit))
            if evidence and root_id in evidence.root_ids:
                nodes.add(self._node_key_for_ctx(ctx, bit))
        return nodes

    def _target_nodes_for_root(
        self,
        bits: Iterable[Bit],
        target_ctx: Optional[InstanceContext],
        root_id: str,
    ) -> List[Tuple[str, Bit]]:
        if target_ctx is None:
            return []
        nodes: List[Tuple[str, Bit]] = []
        for bit in bits:
            if bit in CONST_BITS:
                continue
            evidence = self.const_map.get(self._node_key(target_ctx, bit))
            if evidence and root_id in evidence.root_ids:
                nodes.append(self._node_key_for_ctx(target_ctx, bit))
        return nodes

    @staticmethod
    def _compress_path(labels: List[str]) -> List[str]:
        compressed: List[str] = []
        for label in labels:
            if not compressed or compressed[-1] != label:
                compressed.append(label)
        return compressed

    def _estimate_path_to_root(self, bits: List[Bit], target_signal: str, root_id: str) -> List[str]:
        root = self.root_causes.get(root_id)
        if root is None:
            return [target_signal]

        parts = target_signal.split(".")
        target_ctx = self.raw_context_map.get(".".join(parts[:-1])) if len(parts) > 1 else None
        target_nodes = self._target_nodes_for_root(bits, target_ctx, root_id)
        root_nodes = self._root_nodes(root_id)

        if not target_nodes or not root_nodes:
            if root.hierarchical_signal == target_signal:
                return [target_signal]
            return [root.hierarchical_signal, target_signal]

        for target_node in target_nodes:
            queue: List[List[Tuple[str, Bit]]] = [[target_node]]
            visited: Set[Tuple[str, Bit]] = {target_node}

            while queue:
                path_nodes = queue.pop(0)
                current = path_nodes[-1]
                if current in root_nodes:
                    labels = [self._node_label(node) for node in reversed(path_nodes)]
                    if labels and labels[0] != root.hierarchical_signal:
                        labels.insert(0, root.hierarchical_signal)
                    if labels and labels[-1] != target_signal:
                        labels.append(target_signal)
                    return self._compress_path(labels)

                for pred in sorted(self.reverse_graph.get(current, set())):
                    if pred in visited:
                        continue
                    pred_ctx = self.raw_context_map.get(pred[0])
                    if pred_ctx is None:
                        continue
                    evidence = self.const_map.get(self._node_key(pred_ctx, pred[1]))
                    if evidence is None or root_id not in evidence.root_ids:
                        continue
                    visited.add(pred)
                    queue.append(path_nodes + [pred])

        if root.hierarchical_signal == target_signal:
            return [target_signal]
        return [root.hierarchical_signal, target_signal]

    def _collect_constantized_signals(self) -> List[ConstantizedSignalItem]:
        """Find named outputs that optimization rewired directly to constants."""
        constantized: List[ConstantizedSignalItem] = []
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

            before_driver, raw_folded_value = self._rtlil_constant_through_driver(
                before_module_info,
                local_name,
            )
            if not before_driver:
                before_driver = self._rtlil_direct_driver(before_module_info, local_name) or ""

            constantized.append(
                ConstantizedSignalItem(
                    kind="constantized_signal",
                    path=record.hierarchical_signal,
                    parent_path=ctx.path_str,
                    signal=record.hierarchical_signal,
                    signal_kind=record.signal_kind,
                    before_driver=before_driver,
                    raw_folded_value=raw_folded_value,
                    after_value=after_value,
                    affected_signals=[self._record_to_affected(record)],
                )
            )
            seen.add(record.hierarchical_signal)

        return constantized

    def _collect_removed_items(self) -> Dict[str, List[RemovedItem]]:
        removed_instances: List[RemovedItem] = []
        removed_cells: List[RemovedItem] = []

        for before_ctx in self.before_contexts_preorder:
            raw_ctx = self.raw_context_map.get(before_ctx.path_str)
            if raw_ctx is None:
                continue
            opt_ctx = self.opt_context_map.get(before_ctx.path_str)

            raw_index = self.module_indices[raw_ctx.module_name]
            before_module_info = self.before_modules.get(before_ctx.module_name)
            if before_module_info is None:
                continue
            opt_module_info = self.opt_modules.get(opt_ctx.module_name) if opt_ctx is not None else None

            before_children = set(before_ctx.children)
            opt_children = set(opt_ctx.children) if opt_ctx is not None else set()

            for child_name in sorted(before_children - opt_children):
                child_ctx = raw_ctx.children.get(child_name)
                if child_ctx is None:
                    continue
                cell_data = raw_index.cells.get(child_name, {})
                affected = self._removed_child_outputs(raw_ctx, child_ctx, child_name, cell_data)
                if not affected:
                    continue
                removed_instances.append(
                    RemovedItem(
                        kind="removed_instance",
                        path=f"{before_ctx.path_str}.{child_name}",
                        parent_path=before_ctx.path_str,
                        item_name=child_name,
                        item_type=cell_data.get("type", child_ctx.module_name),
                        src=self._cell_source(cell_data),
                        affected_signals=affected,
                    )
                )

            before_local_cells = {
                name: cell
                for name, cell in before_module_info.cells.items()
                if name not in before_children
            }
            opt_local_signature_counts = Counter(
                self._yosys_local_cell_match_key(opt_module_info, cell)
                for name, cell in (opt_module_info.cells.items() if opt_module_info is not None else [])
                if name not in opt_children
                and not self._is_sequential_cell_type(cell.cell_type)
                and not (
                    cell.cell_type not in COMB_CELL_TYPES
                    and self._normalize_cell_type(cell.cell_type) == cell.cell_type
                )
            )

            before_local_items = sorted(
                before_local_cells.items(),
                key=lambda item: (
                    self._yosys_local_cell_match_key(before_module_info, item[1]),
                    item[0],
                ),
            )

            for cell_name, cell in before_local_items:
                cell_type = cell.cell_type
                if self._is_sequential_cell_type(cell_type):
                    continue
                normalized = self._normalize_cell_type(cell_type)
                if cell_type not in COMB_CELL_TYPES and normalized == cell_type:
                    continue
                signature = self._yosys_local_cell_match_key(before_module_info, cell)
                if opt_local_signature_counts[signature] > 0:
                    opt_local_signature_counts[signature] -= 1
                    continue
                affected = self._removed_before_local_cell_outputs(raw_ctx, before_module_info, cell)
                if not affected:
                    continue
                removed_cells.append(
                    RemovedItem(
                        kind="removed_cell",
                        path=f"{before_ctx.path_str}.{cell_name}",
                        parent_path=before_ctx.path_str,
                        item_name=cell_name,
                        item_type=cell_type,
                        src=cell.src,
                        affected_signals=affected,
                    )
                )

        return {
            "removed_instances": removed_instances,
            "removed_cells": removed_cells,
        }

    def analyze_design(self) -> Dict:
        print("步骤 1: 导出 raw/opt JSON、noopt RTLIL，并附带 raw/opt RTLIL 差分证据...")
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
        self._build_flow_graph()

        print("步骤 3: 构建 raw/opt Yosys JSON 层次并对比缺失实例和单元...")
        _, self.before_contexts_preorder, _ = self._build_yosys_context_tree(self.before_modules)
        _, self.opt_contexts_preorder, self.opt_context_map = self._build_yosys_context_tree(self.opt_modules)

        diff_findings = self._collect_removed_items()

        removed_instances = diff_findings["removed_instances"]
        removed_cells = diff_findings["removed_cells"]
        constantized_signals = self._collect_constantized_signals()
        structural_items = removed_instances + removed_cells + constantized_signals
        total_affected_signals = sum(len(item.affected_signals) for item in structural_items)
        referenced_roots = sorted(
            {
                root_id
                for item in structural_items
                for affected in item.affected_signals
                for root_id in affected.roots
            }
        )

        summary = {
            "removed_instance_count": len(removed_instances),
            "removed_cell_count": len(removed_cells),
            "constantized_signal_count": len(constantized_signals),
            "affected_signal_count": total_affected_signals,
            "referenced_root_count": len(referenced_roots),
            "conflict_count": len(self.conflicts),
            "potential_issues": [],
        }
        if summary["removed_instance_count"] > 0:
            summary["potential_issues"].append(
                f"发现 {summary['removed_instance_count']} 个被删除的模块实例，其输出受常量传播影响。"
            )
        if summary["removed_cell_count"] > 0:
            summary["potential_issues"].append(
                f"发现 {summary['removed_cell_count']} 个被删除的局部组合单元，其输出受常量传播影响。"
            )
        if summary["constantized_signal_count"] > 0:
            summary["potential_issues"].append(
                f"发现 {summary['constantized_signal_count']} 个优化后被直接常量化的输出信号。"
            )
        if summary["affected_signal_count"] > 0:
            summary["potential_issues"].append(
                f"发现 {summary['affected_signal_count']} 个受结构优化影响的常量信号。"
            )
        if summary["referenced_root_count"] > 0:
            summary["potential_issues"].append(
                f"已将结构优化项关联到 {summary['referenced_root_count']} 个显式常量根源。"
            )
        if summary["conflict_count"] > 0:
            summary["potential_issues"].append(
                f"raw 常量传播阶段发现 {summary['conflict_count']} 个冲突推断。"
            )

        referenced_root_records = [
            asdict(self.root_causes[root_id])
            for root_id in referenced_roots
            if root_id in self.root_causes
        ]

        return {
            "summary": summary,
            "removed_instances": [asdict(item) for item in removed_instances],
            "removed_cells": [asdict(item) for item in removed_cells],
            "constantized_signals": [asdict(item) for item in constantized_signals],
            "referenced_roots": referenced_root_records,
            "raw_constant_signal_count": len(self.raw_signal_records),
            "raw_conflicts": list(self.conflicts),
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
        def convert_path(path_item: Dict) -> Dict:
            return {
                "根源ID": path_item["root_id"],
                "根源信号": path_item["root_signal"],
                "根源值": path_item["root_value"],
                "传播路径": path_item["path"],
            }

        def convert_affected(affected: Dict) -> Dict:
            return {
                "信号": affected["signal"],
                "值": affected["value"],
                "类别": affected["kind"],
                "别名": affected.get("aliases", []),
                "根源": affected.get("roots", []),
                "原因": affected.get("reason", ""),
                "传播路径": [convert_path(item) for item in affected.get("propagation_paths", [])],
            }

        def convert_removed_item(item: Dict) -> Dict:
            return {
                "类型": item["kind"],
                "路径": item["path"],
                "父路径": item["parent_path"],
                "名称": item["item_name"],
                "单元类型": item["item_type"],
                "源码位置": item.get("src", ""),
                "受影响信号": [convert_affected(sig) for sig in item.get("affected_signals", [])],
            }

        def convert_constantized_item(item: Dict) -> Dict:
            return {
                "类型": item["kind"],
                "路径": item["path"],
                "父路径": item["parent_path"],
                "信号": item["signal"],
                "信号类别": item["signal_kind"],
                "raw驱动": item.get("before_driver", ""),
                "raw已折叠值": item.get("raw_folded_value", ""),
                "优化后常量值": item.get("after_value", ""),
                "受影响信号": [convert_affected(sig) for sig in item.get("affected_signals", [])],
            }

        def convert_root(root: Dict) -> Dict:
            return {
                "层次化信号": root["hierarchical_signal"],
                "常量值": root["constant_value"],
                "根源类型": self._source_type_label(root["source_type"]),
                "原始根源类型": root["source_type"],
                "位置": root["location"],
                "别名": root.get("aliases", []),
                "备注": root.get("notes", []),
            }

        return {
            "报告类型": "常量传播结构优化分析",
            "报告格式": "json",
            "生成时间": datetime.now().isoformat(timespec="seconds"),
            "设计输入": list(self.design_inputs),
            "主输入": self.primary_input,
            "顶层模块": self.top_module,
            "Yosys路径": self.yosys_bin,
            "分析结果": {
                "摘要": {
                    "被删除的模块实例数量": summary["removed_instance_count"],
                    "被删除的局部组合单元数量": summary["removed_cell_count"],
                    "优化后被直接常量化的信号数量": summary["constantized_signal_count"],
                    "受影响信号数量": summary["affected_signal_count"],
                    "关联到的显式根源数量": summary["referenced_root_count"],
                    "raw传播冲突数量": summary["conflict_count"],
                    "潜在问题": list(summary["potential_issues"]),
                },
                "被删除的模块实例": [convert_removed_item(item) for item in analysis_results["removed_instances"]],
                "被删除的局部组合单元": [convert_removed_item(item) for item in analysis_results["removed_cells"]],
                "优化后被直接常量化的信号": [
                    convert_constantized_item(item)
                    for item in analysis_results["constantized_signals"]
                ],
                "关联到的显式常量根源": [convert_root(root) for root in analysis_results["referenced_roots"]],
                "raw常量信号数量": analysis_results["raw_constant_signal_count"],
                "raw传播冲突": analysis_results["raw_conflicts"],
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
            "对比 raw/opt Yosys JSON 层次网表，定位被删除的实例、组合单元或被直接常量化的输出信号，"
            "并将其受影响的常量信号回溯到显式根源。"
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
            f"被删除模块实例={summary['removed_instance_count']}，"
            f"被删除局部组合单元={summary['removed_cell_count']}，"
            f"直接常量化信号={summary['constantized_signal_count']}，"
            f"受影响信号={summary['affected_signal_count']}，"
            f"关联根源={summary['referenced_root_count']}，"
            f"冲突={summary['conflict_count']}"
        )
        print(f"\n输出目录: {output_dir}")
        print(f"\n报告已保存到: {output_path}")
        print(f"附加导出文件: {raw_json_path}")
        print(f"附加导出文件: {opt_json_path}")
        print(f"附加导出文件: {raw_proc_path}")
        print(f"附加导出文件: {opt_proc_path}")
        print(f"附加导出文件: {noopt_proc_path}")

        has_issue = bool(
            results["removed_instances"]
            or results["removed_cells"]
            or results["constantized_signals"]
        )
        if has_issue:
            print("\n检测到与结构优化相关的常量传播问题。")
            return 1

        print("\n未检测到与结构优化相关的常量传播问题。")
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
