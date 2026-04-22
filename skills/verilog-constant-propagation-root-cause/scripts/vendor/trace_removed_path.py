#!/usr/bin/env python3
"""Hierarchical raw_proc/opt_proc diff based constant propagation tracer.

This script uses a different strategy from trace_modified.py:
1. Export a hierarchical raw_proc RTLIL design and an optimized RTLIL design.
2. Compare raw_proc vs opt_proc hierarchy to find removed module instances
   and removed local combinational cells.
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
    EXPLICIT_SOURCE_TYPES,
    InstanceContext,
    RootCause,
    SignalConstRecord,
    VERILOG_PRIMITIVES,
)

SOURCE_INSTANCE_STMT_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s+"
    r"(?:#\s*\([^;]*?\)\s*)?"
    r"([A-Za-z_][A-Za-z0-9_$]*)\s*\((.*?)\)\s*;",
    re.MULTILINE | re.DOTALL,
)

SUPPORTED_SOURCE_PRIMITIVES = {
    "and",
    "buf",
    "nand",
    "nor",
    "not",
    "or",
    "xnor",
    "xor",
}

RTLIL_TO_SOURCE_PRIMITIVE = {
    "$_AND_": "and",
    "$_BUF_": "buf",
    "$_NAND_": "nand",
    "$_NOR_": "nor",
    "$_NOT_": "not",
    "$_OR_": "or",
    "$_XNOR_": "xnor",
    "$_XOR_": "xor",
    "$and": "and",
    "$buf": "buf",
    "$nand": "nand",
    "$nor": "nor",
    "$not": "not",
    "$or": "or",
    "$xnor": "xnor",
    "$xor": "xor",
}

DEFAULT_REPORT_ROOT = Path(r"D:\mcp\gate_error_reports")
SIMPLE_CONNECTION_EXPR_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_$]*)(\[\d+\])?\s*$")
SIMPLE_SIGNAL_REF_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_$]*)(?:\[(\d+)(?::(\d+))?\])?\s*$"
)
FUNCTION_CALL_RE = re.compile(r"^\s*([$A-Za-z_][A-Za-z0-9_$:]*)\s*\((.*)\)\s*$", re.DOTALL)
NAMED_PORT_ARG_RE = re.compile(r"^\.\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\(\s*(.*?)\s*\)$", re.DOTALL)
PORT_DECL_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_$]*)\s*$")
UNARY_EXPR_PREFIXES = ("~&", "~|", "~^", "^~", "!", "~", "&", "|", "^", "+", "-")
BINARY_EXPR_OPERATORS = (
    "||",
    "&&",
    "|",
    "^~",
    "~^",
    "^",
    "&",
    "===",
    "!==",
    "==",
    "!=",
    "<=",
    ">=",
    "<<<",
    ">>>",
    "<<",
    ">>",
    "<",
    ">",
    "+",
    "-",
    "*",
    "/",
    "%",
)


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
class SourcePrimitiveCell:
    module_name: str
    instance_name: str
    primitive_type: str
    output_signal: str
    input_signals: List[str] = field(default_factory=list)
    src: str = ""


@dataclass
class RtlilCellInfo:
    name: str
    cell_type: str
    src: str = ""
    connections: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class RtlilModuleInfo:
    name: str
    cells: Dict[str, RtlilCellInfo] = field(default_factory=dict)
    connects: List[Tuple[List[str], List[str]]] = field(default_factory=list)
    inputs: Set[str] = field(default_factory=set)
    outputs: Set[str] = field(default_factory=set)


class OptimizationDiffRemovedTracer(ConstantTracer):
    """Compare raw_proc/opt_proc hierarchy and backtrace removed items to roots."""

    def __init__(self, design_inputs, top_module: str = "top_module", yosys_bin: Optional[str] = None):
        super().__init__(design_inputs, top_module, yosys_bin)
        self.raw_netlist_data: Dict = {}
        self.before_rtlil_modules: Dict[str, RtlilModuleInfo] = {}
        self.opt_rtlil_modules: Dict[str, RtlilModuleInfo] = {}
        self.before_contexts_preorder: List[InstanceContext] = []
        self.opt_contexts_preorder: List[InstanceContext] = []

        self.raw_context_map: Dict[str, InstanceContext] = {}
        self.opt_context_map: Dict[str, InstanceContext] = {}
        self.raw_signal_records: List[SignalConstRecord] = []
        self.raw_signal_map: Dict[str, SignalConstRecord] = {}
        self.prepared_source_map: Dict[str, str] = {}
        self.raw_json_text: str = ""
        self.raw_proc_rtlil_text: str = ""
        self.opt_proc_rtlil_text: str = ""
        self.source_primitive_cells: Dict[str, List[SourcePrimitiveCell]] = (
            self._build_source_primitive_cells()
        )
        self.source_module_ports: Dict[str, List[str]] = self._build_source_module_ports()
        self.source_instance_connections: Dict[str, Dict[str, Dict[str, str]]] = (
            self._build_source_instance_connections()
        )
        self.literal_root_promotion_cache: Dict[str, str] = {}
        self.promoted_root_origins: Dict[str, Set[str]] = defaultdict(set)

        self.forward_graph: Dict[Tuple[str, Bit], Set[Tuple[str, Bit]]] = defaultdict(set)
        self.reverse_graph: Dict[Tuple[str, Bit], Set[Tuple[str, Bit]]] = defaultdict(set)

    def _reset_constant_state(self) -> None:
        self.root_causes = {}
        self.context_direct_roots = defaultdict(lambda: defaultdict(set))
        self.const_map = {}
        self.reason_map = {}
        self.conflicts = []

    def _export_raw_opt_jsons(self) -> None:
        with tempfile.TemporaryDirectory(prefix="trace_removed_path_") as tempdir:
            raw_json = Path(tempdir) / "raw_design.json"
            raw_proc_rtlil = Path(tempdir) / "raw_proc.il"
            opt_proc_rtlil = Path(tempdir) / "opt_proc.il"
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
                f"write_rtlil {self._yosys_path(opt_proc_rtlil)}; "
                "stat"
            )
            result = self._run_yosys(opt_script, timeout=120)
            self.yosys_stat = result.stdout

            self.raw_json_text = raw_json.read_text(encoding="utf-8")
            self.raw_netlist_data = json.loads(self.raw_json_text)
            self.raw_proc_rtlil_text = raw_proc_rtlil.read_text(encoding="utf-8", errors="ignore")
            self.opt_proc_rtlil_text = opt_proc_rtlil.read_text(encoding="utf-8", errors="ignore")
            self.before_rtlil_modules = self._parse_rtlil_modules(self.raw_proc_rtlil_text)
            self.opt_rtlil_modules = self._parse_rtlil_modules(self.opt_proc_rtlil_text)

        self.modules_data = self.raw_netlist_data.get("modules", {})

    @staticmethod
    def _context_map(contexts: Iterable[InstanceContext]) -> Dict[str, InstanceContext]:
        return {ctx.path_str: ctx for ctx in contexts}

    @staticmethod
    def _split_top_level_args(text: str) -> List[str]:
        args: List[str] = []
        current: List[str] = []
        depth_paren = 0
        depth_brace = 0
        depth_bracket = 0

        for char in text:
            if char == "," and depth_paren == 0 and depth_brace == 0 and depth_bracket == 0:
                token = "".join(current).strip()
                if token:
                    args.append(token)
                current = []
                continue

            current.append(char)
            if char == "(":
                depth_paren += 1
            elif char == ")":
                depth_paren = max(0, depth_paren - 1)
            elif char == "{":
                depth_brace += 1
            elif char == "}":
                depth_brace = max(0, depth_brace - 1)
            elif char == "[":
                depth_bracket += 1
            elif char == "]":
                depth_bracket = max(0, depth_bracket - 1)

        token = "".join(current).strip()
        if token:
            args.append(token)
        return args

    @staticmethod
    def _extract_port_name_from_decl(token: str) -> Optional[str]:
        token = token.strip()
        if not token:
            return None
        token = re.sub(r"\b(input|output|inout|wire|logic|reg|signed|unsigned|var|tri|supply0|supply1)\b", " ", token)
        token = re.sub(r"\[[^\]]+\]", " ", token)
        token = token.split("=")[0].strip()
        match = PORT_DECL_NAME_RE.search(token)
        if not match:
            return None
        return match.group(1)

    @staticmethod
    def _simple_signal_name(expr: str) -> Optional[str]:
        token = expr.strip()
        if not token or token.startswith("."):
            return None
        if token in {"1'b0", "1'b1", "1'h0", "1'h1", "1'd0", "1'd1"}:
            return None
        if any(ch in token for ch in "{}()"):
            return None
        if "[" in token:
            return None
        return token

    @staticmethod
    def _simple_connection_expr(expr: str) -> Optional[str]:
        token = expr.strip()
        match = SIMPLE_CONNECTION_EXPR_RE.fullmatch(token)
        if not match:
            return None
        return token

    @staticmethod
    def _unwrap_outer_parens(expr: str) -> str:
        token = expr.strip()
        while token.startswith("(") and token.endswith(")"):
            depth = 0
            balanced = True
            for index, char in enumerate(token):
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0 and index != len(token) - 1:
                        balanced = False
                        break
            if not balanced or depth != 0:
                break
            token = token[1:-1].strip()
        return token

    @staticmethod
    def _parse_signal_reference(expr: str) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
        match = SIMPLE_SIGNAL_REF_RE.fullmatch(expr.strip())
        if not match:
            return None
        base = match.group(1)
        index_hi = int(match.group(2)) if match.group(2) is not None else None
        index_lo = int(match.group(3)) if match.group(3) is not None else None
        return base, index_hi, index_lo

    @staticmethod
    def _top_level_operator_index(expr: str, operator: str) -> int:
        depth_paren = 0
        depth_brace = 0
        depth_bracket = 0
        for index, char in enumerate(expr):
            if char == "(":
                depth_paren += 1
            elif char == ")":
                depth_paren = max(0, depth_paren - 1)
            elif char == "{":
                depth_brace += 1
            elif char == "}":
                depth_brace = max(0, depth_brace - 1)
            elif char == "[":
                depth_bracket += 1
            elif char == "]":
                depth_bracket = max(0, depth_bracket - 1)
            elif (
                char == operator
                and depth_paren == 0
                and depth_brace == 0
                and depth_bracket == 0
                ):
                    return index
        return -1

    def _find_top_level_binary_operator(self, expr: str, operator: str) -> int:
        depth_paren = 0
        depth_brace = 0
        depth_bracket = 0
        op_len = len(operator)

        for index in range(len(expr) - op_len, -1, -1):
            char = expr[index]
            if char == ")":
                depth_paren += 1
                continue
            if char == "(":
                depth_paren = max(0, depth_paren - 1)
                continue
            if char == "}":
                depth_brace += 1
                continue
            if char == "{":
                depth_brace = max(0, depth_brace - 1)
                continue
            if char == "]":
                depth_bracket += 1
                continue
            if char == "[":
                depth_bracket = max(0, depth_bracket - 1)
                continue
            if depth_paren != 0 or depth_brace != 0 or depth_bracket != 0:
                continue
            if not expr.startswith(operator, index):
                continue

            if operator in {"+", "-"}:
                prev_index = index - 1
                while prev_index >= 0 and expr[prev_index].isspace():
                    prev_index -= 1
                if prev_index < 0 or expr[prev_index] in "([{?:,=<>!&|^~+-*/%":
                    continue

            return index
        return -1

    def _split_top_level_binary(self, expr: str) -> Optional[Tuple[str, str, str]]:
        expr = self._unwrap_outer_parens(expr)
        for operator in BINARY_EXPR_OPERATORS:
            index = self._find_top_level_binary_operator(expr, operator)
            if index < 0:
                continue
            lhs = expr[:index].strip()
            rhs = expr[index + len(operator) :].strip()
            if not lhs or not rhs:
                continue
            return lhs, operator, rhs
        return None

    def _split_top_level_ternary(self, expr: str) -> Optional[Tuple[str, str, str]]:
        expr = self._unwrap_outer_parens(expr)
        q_index = self._top_level_operator_index(expr, "?")
        if q_index < 0:
            return None

        depth_paren = 0
        depth_brace = 0
        depth_bracket = 0
        nested_q = 0
        colon_index = -1
        for index in range(q_index + 1, len(expr)):
            char = expr[index]
            if char == "(":
                depth_paren += 1
            elif char == ")":
                depth_paren = max(0, depth_paren - 1)
            elif char == "{":
                depth_brace += 1
            elif char == "}":
                depth_brace = max(0, depth_brace - 1)
            elif char == "[":
                depth_bracket += 1
            elif char == "]":
                depth_bracket = max(0, depth_bracket - 1)
            elif (
                depth_paren == 0
                and depth_brace == 0
                and depth_bracket == 0
            ):
                if char == "?":
                    nested_q += 1
                elif char == ":":
                    if nested_q == 0:
                        colon_index = index
                        break
                    nested_q -= 1

        if colon_index < 0:
            return None
        return (
            expr[:q_index].strip(),
            expr[q_index + 1 : colon_index].strip(),
            expr[colon_index + 1 :].strip(),
        )

    @staticmethod
    def _strip_bit_suffix(signal_name: str) -> str:
        return re.sub(r"\[\d+\]$", "", signal_name.strip())

    def _target_bit_from_local_signal(self, local_signal: str) -> Optional[int]:
        parsed = self._parse_signal_reference(local_signal)
        if parsed is None:
            return None
        _, index_hi, index_lo = parsed
        if index_hi is not None and index_lo is None:
            return index_hi
        return None

    def _signal_ref_bits(self, ctx: InstanceContext, signal_expr: str) -> Optional[List[Bit]]:
        parsed = self._parse_signal_reference(signal_expr)
        if parsed is None:
            return None
        base_signal, index_hi, index_lo = parsed
        module_index = self.module_indices.get(ctx.module_name)
        if module_index is None:
            return None
        bits = module_index.name_to_bits.get(base_signal, [])
        if not bits:
            return None
        if index_hi is None:
            return list(bits)
        if index_lo is None:
            if 0 <= index_hi < len(bits):
                return [bits[index_hi]]
            return None
        lo = min(index_hi, index_lo)
        hi = max(index_hi, index_lo)
        if hi >= len(bits):
            return None
        return list(bits[lo : hi + 1])

    def _expr_width(self, ctx: InstanceContext, expr: str) -> Optional[int]:
        expr = self._unwrap_outer_parens(expr)
        sig_bits = self._signal_ref_bits(ctx, expr)
        if sig_bits is not None:
            return len(sig_bits)

        literal_match = re.fullmatch(r"(\d+)'[bBdDhHoO][0-9a-fA-F_xXzZ]+", expr)
        if literal_match:
            return int(literal_match.group(1))
        if expr in {"1'b0", "1'b1", "1'h0", "1'h1", "1'd0", "1'd1", "0", "1"}:
            return 1

        ternary = self._split_top_level_ternary(expr)
        if ternary is not None:
            _, true_expr, false_expr = ternary
            true_width = self._expr_width(ctx, true_expr)
            false_width = self._expr_width(ctx, false_expr)
            if true_width is not None and true_width == false_width:
                return true_width
            return true_width or false_width

        if expr.startswith("{") and expr.endswith("}"):
            inner = expr[1:-1].strip()
            rep_match = re.fullmatch(r"(\d+)\s*\{(.*)\}", inner, re.DOTALL)
            if rep_match:
                repeat = int(rep_match.group(1))
                inner_width = self._expr_width(ctx, rep_match.group(2).strip())
                return repeat * inner_width if inner_width is not None else None
            parts = self._split_top_level_args(inner)
            total = 0
            for part in parts:
                width = self._expr_width(ctx, part)
                if width is None:
                    return None
                total += width
            return total

        return None

    def _expr_signal_candidates(
        self,
        ctx: InstanceContext,
        expr: str,
        target_bit: Optional[int],
    ) -> List[str]:
        expr = self._unwrap_outer_parens(expr)
        candidates: List[str] = []
        seen: Set[str] = set()

        def add(candidate: str) -> None:
            candidate = self._unwrap_outer_parens(candidate)
            if not candidate or candidate in seen:
                return
            seen.add(candidate)
            candidates.append(candidate)

        if self._parse_signal_reference(expr) is not None:
            add(expr)
            return candidates

        ternary = self._split_top_level_ternary(expr)
        if ternary is not None:
            cond_expr, true_expr, false_expr = ternary
            for candidate in self._expr_signal_candidates(ctx, cond_expr, None):
                add(candidate)
            for candidate in self._expr_signal_candidates(ctx, true_expr, target_bit):
                add(candidate)
            for candidate in self._expr_signal_candidates(ctx, false_expr, target_bit):
                add(candidate)
            return candidates

        if expr.startswith("{") and expr.endswith("}"):
            inner = expr[1:-1].strip()
            rep_match = re.fullmatch(r"(\d+)\s*\{(.*)\}", inner, re.DOTALL)
            if rep_match:
                inner_expr = rep_match.group(2).strip()
                inner_width = self._expr_width(ctx, inner_expr)
                mapped_target = target_bit
                if inner_width not in {None, 0} and target_bit is not None:
                    mapped_target = target_bit % inner_width
                for candidate in self._expr_signal_candidates(ctx, inner_expr, mapped_target):
                    add(candidate)
                return candidates

            parts = self._split_top_level_args(inner)
            if target_bit is not None:
                cursor = 0
                for part in reversed(parts):
                    width = self._expr_width(ctx, part)
                    if width is None:
                        continue
                    if cursor <= target_bit < cursor + width:
                        inner_target = target_bit - cursor
                        for candidate in self._expr_signal_candidates(ctx, part, inner_target):
                            add(candidate)
                        break
                    cursor += width
            for part in parts:
                for candidate in self._expr_signal_candidates(ctx, part, None):
                    add(candidate)
            return candidates

        func_match = FUNCTION_CALL_RE.fullmatch(expr)
        if func_match is not None:
            arg_blob = func_match.group(2).strip()
            for arg in self._split_top_level_args(arg_blob):
                for candidate in self._expr_signal_candidates(ctx, arg, target_bit):
                    add(candidate)
            return candidates

        for prefix in UNARY_EXPR_PREFIXES:
            if expr.startswith(prefix):
                inner_expr = expr[len(prefix) :].strip()
                if inner_expr:
                    for candidate in self._expr_signal_candidates(ctx, inner_expr, target_bit):
                        add(candidate)
                    return candidates

        binary = self._split_top_level_binary(expr)
        if binary is not None:
            lhs, _, rhs = binary
            for candidate in self._expr_signal_candidates(ctx, lhs, target_bit):
                add(candidate)
            for candidate in self._expr_signal_candidates(ctx, rhs, target_bit):
                add(candidate)
            return candidates

        for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_$]*(?:\[\d+(?::\d+)?\])?", expr):
            add(match.group(0))
        return candidates

    def _parse_named_port_map(self, port_blob: str) -> Dict[str, str]:
        named_ports: Dict[str, str] = {}
        for token in self._split_top_level_args(port_blob):
            match = NAMED_PORT_ARG_RE.match(token.strip())
            if not match:
                continue
            named_ports[match.group(1)] = match.group(2).strip()
        return named_ports

    def _build_source_primitive_cells(self) -> Dict[str, List[SourcePrimitiveCell]]:
        cells_by_module: Dict[str, List[SourcePrimitiveCell]] = defaultdict(list)
        modules_by_name = self.design_catalog.get("modules_by_name", {})
        file_texts = self.design_catalog.get("file_texts", {})

        for module_name, entries in modules_by_name.items():
            if not entries:
                continue
            module_info = entries[0]
            module_text = module_info.get("text", "")
            stripped_text = self._strip_comments(module_text)
            file_path = module_info.get("file", "")
            file_text = file_texts.get(Path(file_path), "")
            block_offset = file_text.find(module_text) if file_text else -1

            for match in SOURCE_INSTANCE_STMT_RE.finditer(stripped_text):
                callee = match.group(1)
                if callee not in VERILOG_PRIMITIVES or callee not in SUPPORTED_SOURCE_PRIMITIVES:
                    continue

                instance_name = match.group(2)
                port_args = self._split_top_level_args(match.group(3))
                if not port_args:
                    continue

                output_signal = self._simple_signal_name(port_args[0])
                if output_signal is None:
                    continue

                input_signals = [
                    signal_name
                    for signal_name in (
                        self._simple_signal_name(arg) for arg in port_args[1:]
                    )
                    if signal_name is not None
                ]

                src = file_path
                if file_text and block_offset >= 0:
                    instance_offset = file_text.find(instance_name, block_offset)
                    absolute_offset = instance_offset if instance_offset >= 0 else block_offset + match.start()
                    line_no = file_text[:absolute_offset].count("\n") + 1
                    src = f"{file_path}:{line_no}"

                cells_by_module[module_name].append(
                    SourcePrimitiveCell(
                        module_name=module_name,
                        instance_name=instance_name,
                        primitive_type=callee,
                        output_signal=output_signal,
                        input_signals=input_signals,
                        src=src,
                    )
                )

        return cells_by_module

    def _build_source_module_ports(self) -> Dict[str, List[str]]:
        module_ports: Dict[str, List[str]] = {}
        modules_by_name = self.design_catalog.get("modules_by_name", {})

        for module_name, entries in modules_by_name.items():
            if not entries:
                continue
            module_text = entries[0].get("text", "")
            stripped_text = self._strip_comments(module_text)
            header_match = re.search(
                rf"module\s+{re.escape(module_name)}\b(?:\s*#\s*\((.*?)\))?\s*\((.*?)\)\s*;",
                stripped_text,
                re.DOTALL,
            )
            if not header_match:
                continue

            port_blob = header_match.group(2).strip()
            if not port_blob:
                module_ports[module_name] = []
                continue

            port_names: List[str] = []
            for token in self._split_top_level_args(port_blob):
                port_name = self._extract_port_name_from_decl(token)
                if port_name:
                    port_names.append(port_name)
            module_ports[module_name] = port_names

        return module_ports

    def _build_source_instance_connections(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        connections_by_module: Dict[str, Dict[str, Dict[str, str]]] = defaultdict(dict)
        modules_by_name = self.design_catalog.get("modules_by_name", {})

        for module_name, entries in modules_by_name.items():
            if not entries:
                continue
            module_text = entries[0].get("text", "")
            stripped_text = self._strip_comments(module_text)
            for match in SOURCE_INSTANCE_STMT_RE.finditer(stripped_text):
                callee = match.group(1)
                instance_name = match.group(2)
                if callee in VERILOG_PRIMITIVES:
                    continue
                port_blob = match.group(3)
                named_ports = self._parse_named_port_map(port_blob)
                if named_ports:
                    connections_by_module[module_name][instance_name] = named_ports
                    continue

                positional_args = self._split_top_level_args(port_blob)
                port_order = self.source_module_ports.get(callee, [])
                if not positional_args or not port_order:
                    continue

                mapped_ports = {
                    port_name: expr.strip()
                    for port_name, expr in zip(port_order, positional_args)
                    if expr.strip()
                }
                if mapped_ports:
                    connections_by_module[module_name][instance_name] = mapped_ports

        return connections_by_module

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

    def _parse_rtlil_modules(self, rtlil_text: str) -> Dict[str, RtlilModuleInfo]:
        modules: Dict[str, RtlilModuleInfo] = {}
        current_module: Optional[RtlilModuleInfo] = None
        current_cell: Optional[RtlilCellInfo] = None
        pending_attrs: Dict[str, str] = {}
        block_stack: List[str] = []

        for raw_line in rtlil_text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            if stripped == "end":
                ended_block = block_stack.pop() if block_stack else ""
                if ended_block == "cell":
                    current_cell = None
                elif ended_block == "module":
                    current_module = None
                pending_attrs = {}
                continue

            if stripped.startswith("attribute "):
                top_block = block_stack[-1] if block_stack else ""
                if top_block not in {"", "module"}:
                    continue
                parts = stripped.split(None, 2)
                if len(parts) >= 3:
                    value = parts[2].strip().strip('"')
                    pending_attrs[self._rtlil_unescape_id(parts[1])] = (
                        self._restore_original_src(value) if self._rtlil_unescape_id(parts[1]) == "src" else value
                    )
                continue

            if stripped.startswith("module "):
                module_name = self._rtlil_unescape_id(stripped.split(None, 1)[1])
                current_module = RtlilModuleInfo(name=module_name)
                modules[module_name] = current_module
                block_stack.append("module")
                pending_attrs = {}
                continue

            if current_module is None:
                pending_attrs = {}
                continue

            top_block = block_stack[-1] if block_stack else ""

            if top_block == "module" and stripped.startswith("wire "):
                parts = stripped.split()
                wire_name = self._rtlil_unescape_id(parts[-1])
                if "input" in parts:
                    current_module.inputs.add(wire_name)
                if "output" in parts:
                    current_module.outputs.add(wire_name)
                pending_attrs = {}
                continue

            if top_block == "module" and stripped.startswith("cell "):
                _, cell_type, cell_name = stripped.split(None, 2)
                current_cell = RtlilCellInfo(
                    name=self._rtlil_unescape_id(cell_name),
                    cell_type=self._rtlil_unescape_id(cell_type),
                    src=pending_attrs.get("src", ""),
                )
                current_module.cells[current_cell.name] = current_cell
                block_stack.append("cell")
                pending_attrs = {}
                continue

            if top_block == "module" and stripped.startswith("process "):
                block_stack.append("process")
                pending_attrs = {}
                continue

            if top_block in {"module", "process", "switch"} and stripped.startswith("switch "):
                block_stack.append("switch")
                pending_attrs = {}
                continue

            if top_block in {"process", "switch"} and stripped.startswith("case "):
                pending_attrs = {}
                continue

            if top_block in {"module", "process", "switch"} and stripped.startswith("sync "):
                pending_attrs = {}
                continue

            if top_block == "module" and stripped.startswith("memory "):
                block_stack.append("memory")
                pending_attrs = {}
                continue

            if top_block == "module" and stripped.startswith("connect "):
                lhs, rhs = self._split_rtlil_connect_operands(stripped[len("connect "):])
                current_module.connects.append(
                    (self._rtlil_parse_sigspec(lhs), self._rtlil_parse_sigspec(rhs))
                )
                pending_attrs = {}
                continue

            if top_block == "cell" and stripped.startswith("connect "):
                port_name, sigspec = self._split_rtlil_connect_operands(stripped[len("connect "):])
                current_cell.connections[self._rtlil_unescape_id(port_name)] = self._rtlil_parse_sigspec(
                    sigspec
                )
                continue

            if top_block in {"process", "switch"} and (
                stripped.startswith("assign ") or stripped.startswith("update ") or stripped.startswith("case ")
            ):
                continue

            if top_block == "cell" and stripped.startswith("parameter "):
                continue

            pending_attrs = {}

        return modules

    def _build_rtlil_context_tree(
        self, rtlil_modules: Dict[str, RtlilModuleInfo]
    ) -> Tuple[Optional[InstanceContext], List[InstanceContext], Dict[str, InstanceContext]]:
        def build(module_name: str, path: Tuple[str, ...]) -> InstanceContext:
            ctx = InstanceContext(module_name=module_name, path=path)
            module_info = rtlil_modules.get(module_name)
            if module_info is None:
                return ctx
            for cell_name, cell in sorted(module_info.cells.items()):
                if cell.cell_type in rtlil_modules:
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

    def _rtlil_alias_graph(self, module_info: RtlilModuleInfo) -> Dict[str, Set[str]]:
        graph: Dict[str, Set[str]] = defaultdict(set)
        for lhs_tokens, rhs_tokens in module_info.connects:
            if len(lhs_tokens) != 1 or len(rhs_tokens) != 1:
                continue
            lhs = lhs_tokens[0]
            rhs = rhs_tokens[0]
            if lhs in {"0", "1"} or rhs in {"0", "1"}:
                continue
            if re.fullmatch(r"[0-9]+'[01xz]+", lhs) or re.fullmatch(r"[0-9]+'[01xz]+", rhs):
                continue
            graph[lhs].add(rhs)
            graph[rhs].add(lhs)
        return graph

    def _resolve_rtlil_output_signals(
        self, module_info: RtlilModuleInfo, cell: RtlilCellInfo
    ) -> List[str]:
        output_port_names = {"Y", "Q", "QB", "O", "S"}
        alias_graph = self._rtlil_alias_graph(module_info)
        resolved: List[str] = []
        seen: Set[str] = set()

        for port_name, tokens in cell.connections.items():
            if port_name not in output_port_names:
                continue
            for token in tokens:
                queue = [token]
                visited = {token}
                best_names: List[str] = []
                while queue:
                    current = queue.pop(0)
                    if not current.startswith("$"):
                        best_names.append(current)
                    for neighbor in sorted(alias_graph.get(current, set())):
                        if neighbor in visited:
                            continue
                        visited.add(neighbor)
                        queue.append(neighbor)
                chosen = best_names or [token]
                for name in chosen:
                    if name not in seen:
                        seen.add(name)
                        resolved.append(name)
        return resolved

    def _resolve_rtlil_input_signals(
        self, module_info: RtlilModuleInfo, cell: RtlilCellInfo
    ) -> List[str]:
        output_port_names = {"Y", "Q", "QB", "O", "S"}
        alias_graph = self._rtlil_alias_graph(module_info)
        resolved: List[str] = []
        seen: Set[str] = set()

        for port_name, tokens in cell.connections.items():
            if port_name in output_port_names:
                continue
            for token in tokens:
                if token in {"0", "1"} or re.fullmatch(r"[0-9]+'[01xz]+", token):
                    continue
                queue = [token]
                visited = {token}
                chosen: Optional[str] = None
                while queue:
                    current = queue.pop(0)
                    if not current.startswith("$"):
                        chosen = current
                        break
                    for neighbor in sorted(alias_graph.get(current, set())):
                        if neighbor in visited:
                            continue
                        visited.add(neighbor)
                        queue.append(neighbor)
                final_name = chosen or token
                if final_name not in seen:
                    seen.add(final_name)
                    resolved.append(final_name)
        return resolved

    def _cell_source(self, cell_data: Dict) -> str:
        return self._restore_original_src(cell_data.get("attributes", {}).get("src", ""))

    @staticmethod
    def _rtlil_cell_to_source_primitive(cell_type: str) -> str:
        return RTLIL_TO_SOURCE_PRIMITIVE.get(cell_type, cell_type)

    def _canonical_comb_type(self, cell_type: str) -> str:
        return self._normalize_cell_type(self._rtlil_cell_to_source_primitive(cell_type))

    def _normalize_src_key(self, src: str) -> str:
        src = self._restore_original_src(src)
        match = re.match(r"^(.*):(\d+)(?:\.\d+(?:-\d+\.\d+)?)?$", src)
        if match:
            return f"{match.group(1)}:{match.group(2)}"
        return src

    def _match_source_primitive(
        self,
        module_name: str,
        cell_type: str,
        src: str,
        output_signals: List[str],
    ) -> Optional[SourcePrimitiveCell]:
        canonical_type = self._canonical_comb_type(cell_type)
        src_key = self._normalize_src_key(src)
        output_set = set(output_signals)

        candidates = [
            primitive
            for primitive in self.source_primitive_cells.get(module_name, [])
            if primitive.primitive_type == canonical_type
            and self._normalize_src_key(primitive.src) == src_key
        ]
        if not candidates:
            return None

        if output_set:
            exact = [primitive for primitive in candidates if primitive.output_signal in output_set]
            if len(exact) == 1:
                return exact[0]
            if exact:
                candidates = exact

        return candidates[0]

    def _rtlil_local_cell_match_key(
        self, module_info: RtlilModuleInfo, cell: RtlilCellInfo
    ) -> Tuple[str, str, Tuple[str, ...], Tuple[str, ...]]:
        return (
            self._canonical_comb_type(cell.cell_type),
            self._normalize_src_key(cell.src),
            tuple(sorted(self._resolve_rtlil_output_signals(module_info, cell))),
            tuple(sorted(self._resolve_rtlil_input_signals(module_info, cell))),
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

            for primitive in self.source_primitive_cells.get(ctx.module_name, []):
                output_bits = module_index.name_to_bits.get(primitive.output_signal, [])
                if not output_bits:
                    continue
                for input_signal in primitive.input_signals:
                    input_bits = module_index.name_to_bits.get(input_signal, [])
                    for input_bit in input_bits:
                        for output_bit in output_bits:
                            self._add_edge(
                                self._node_key_for_ctx(ctx, input_bit),
                                self._node_key_for_ctx(ctx, output_bit),
                            )

    def _record_to_affected(self, record: SignalConstRecord) -> AffectedSignal:
        paths: List[PropagationPath] = []
        promoted_roots: List[str] = []
        seen_roots: Set[str] = set()
        ctx = self.raw_context_map.get(record.module)
        bits: List[Bit] = []
        if ctx is not None:
            module_index = self.module_indices[ctx.module_name]
            leaf = record.hierarchical_signal.split(".")[-1]
            bits = module_index.name_to_bits.get(leaf, [])

        for raw_root_id in record.root_ids:
            root_id = self._promote_root_id(raw_root_id)
            if root_id in seen_roots:
                continue
            seen_roots.add(root_id)
            promoted_roots.append(root_id)
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
            roots=promoted_roots,
            reason=record.reason,
            propagation_paths=paths,
        )

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
        reason_parts: List[str] = []
        for bit in bits:
            if bit in CONST_BITS:
                continue
            reason = self.reason_map.get(self._node_key(ctx, bit), "")
            if reason and reason not in reason_parts:
                reason_parts.append(reason)

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

    def _removed_local_cell_outputs(self, ctx: InstanceContext, cell_name: str, cell_data: Dict) -> List[AffectedSignal]:
        directions = cell_data.get("port_directions", {})
        connections = cell_data.get("connections", {})
        affected: List[AffectedSignal] = []
        seen_signals: Set[str] = set()

        for port_name, direction in directions.items():
            if direction != "output":
                continue
            bits = connections.get(port_name, [])
            if not bits:
                continue
            aliases = self._signal_aliases_hier(ctx, bits)
            fallback_signal = aliases[0] if aliases else f"{ctx.path_str}.{cell_name}.{port_name}"
            record = self._signal_record_for_bits(ctx, bits, fallback_signal, "wire")
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
        reason_parts: List[str] = []
        for bit in bits:
            if bit in CONST_BITS:
                continue
            reason = self.reason_map.get(self._node_key(ctx, bit), "")
            if reason and reason not in reason_parts:
                reason_parts.append(reason)

        return SignalConstRecord(
            hierarchical_signal=fallback_signal,
            module=ctx.path_str,
            signal_kind=self._signal_kind_for_local_name(ctx, local_name),
            constant_value=value,
            aliases=self._signal_aliases_hier(ctx, bits),
            root_ids=sorted(root_ids),
            reason=" | ".join(reason_parts),
        )

    def _estimate_local_source_path(
        self, ctx: InstanceContext, root_signal: str, target_signal: str
    ) -> List[str]:
        root_parts = root_signal.split(".")
        target_parts = target_signal.split(".")
        if len(root_parts) < 2 or len(target_parts) < 2:
            return []
        if ".".join(root_parts[:-1]) != ctx.path_str or ".".join(target_parts[:-1]) != ctx.path_str:
            return []

        root_local = root_parts[-1]
        target_local = target_parts[-1]
        if root_local == target_local:
            return [root_signal]

        graph: Dict[str, Set[str]] = defaultdict(set)
        for primitive in self.source_primitive_cells.get(ctx.module_name, []):
            for input_signal in primitive.input_signals:
                graph[input_signal].add(primitive.output_signal)

        queue: List[List[str]] = [[root_local]]
        visited: Set[str] = {root_local}
        while queue:
            path = queue.pop(0)
            current = path[-1]
            if current == target_local:
                return [f"{ctx.path_str}.{name}" for name in path]
            for neighbor in sorted(graph.get(current, set())):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                queue.append(path + [neighbor])
        return []

    def _removed_source_cell_outputs(
        self, ctx: InstanceContext, primitive: SourcePrimitiveCell
    ) -> List[AffectedSignal]:
        record = self._record_for_named_signal(ctx, primitive.output_signal)
        if record is None:
            return []
        return [self._record_to_affected(record)]

    def _removed_before_local_cell_outputs(
        self, ctx: InstanceContext, module_info: RtlilModuleInfo, cell: RtlilCellInfo
    ) -> List[AffectedSignal]:
        affected: List[AffectedSignal] = []
        seen: Set[str] = set()
        for local_name in self._resolve_rtlil_output_signals(module_info, cell):
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

    def _find_existing_named_root(self, hierarchical_signal: str, value: str) -> Optional[str]:
        root = self.root_causes.get(hierarchical_signal)
        if root and root.source_type != "literal_connection" and root.constant_value == value:
            return root.root_id

        for root_id, candidate in self.root_causes.items():
            if candidate.source_type == "literal_connection":
                continue
            if candidate.constant_value != value:
                continue
            if candidate.hierarchical_signal == hierarchical_signal:
                return root_id
            if hierarchical_signal in candidate.aliases:
                return root_id
        return None

    def _register_promoted_named_root(
        self,
        ctx: InstanceContext,
        local_signal: str,
        value: str,
        source_type: str,
        note: str,
    ) -> str:
        hierarchical_signal = f"{ctx.path_str}.{local_signal}"
        existing = self._find_existing_named_root(hierarchical_signal, value)
        if existing:
            root = self.root_causes.get(existing)
            if root is not None and note not in root.notes:
                root.notes.append(note)
            return existing

        module_index = self.module_indices.get(ctx.module_name)
        bits = module_index.name_to_bits.get(local_signal, []) if module_index is not None else []
        aliases = self._signal_aliases_hier(ctx, bits) if bits else [hierarchical_signal]
        self.root_causes[hierarchical_signal] = RootCause(
            root_id=hierarchical_signal,
            hierarchical_signal=hierarchical_signal,
            local_signal=local_signal,
            constant_value=value,
            source_type=source_type,
            location=f"信号: {hierarchical_signal}",
            aliases=aliases,
            notes=[note],
        )
        return hierarchical_signal

    def _promote_signal_candidate(
        self,
        ctx: InstanceContext,
        signal_expr: str,
        expected_value: str,
        visited: Set[Tuple[str, str, str]],
    ) -> Optional[str]:
        parsed = self._parse_signal_reference(signal_expr)
        if parsed is None:
            return None

        base_signal, _, _ = parsed
        selected_bits = self._signal_ref_bits(ctx, signal_expr)
        if selected_bits:
            selected_const = self._resolve_signal_constant(
                ctx,
                selected_bits,
                f"{ctx.path_str}.{signal_expr}",
            )
            if selected_const is not None and selected_const[0] != expected_value:
                return None

        base_bits = self._signal_ref_bits(ctx, base_signal)
        base_hier_signal = f"{ctx.path_str}.{base_signal}"
        base_const = (
            self._resolve_signal_constant(ctx, base_bits, base_hier_signal)
            if base_bits
            else None
        )
        if base_const is not None:
            base_source_type = self._determine_source_type(
                ctx.module_name,
                base_hier_signal,
                base_const[0],
            )
            if base_source_type in EXPLICIT_SOURCE_TYPES:
                return self._register_promoted_named_root(
                    ctx,
                    base_signal,
                    base_const[0],
                    base_source_type,
                    f"promoted from child literal connection {ctx.path_str}.{signal_expr}",
                )

        module_index = self.module_indices.get(ctx.module_name)
        if module_index is None:
            return None

        direction = module_index.port_directions.get(base_signal)
        if direction in {"input", "inout"}:
            return self._promote_literal_root_through_parents(
                ctx,
                signal_expr,
                expected_value,
                visited,
            )
        return None

    def _promote_literal_root_through_parents(
        self,
        ctx: InstanceContext,
        local_signal: str,
        value: str,
        visited: Set[Tuple[str, str, str]],
    ) -> Optional[str]:
        visit_key = (ctx.path_str, local_signal, value)
        if visit_key in visited:
            return None
        visited.add(visit_key)

        parsed_local = self._parse_signal_reference(local_signal)
        if parsed_local is not None:
            promoted_here = self._promote_signal_candidate(ctx, local_signal, value, visited)
            if promoted_here:
                return promoted_here

        if len(ctx.path) <= 1:
            return None

        parent_ctx = self.raw_context_map.get(".".join(ctx.path[:-1]))
        if parent_ctx is None:
            return None

        port_name = self._strip_bit_suffix(local_signal)
        instance_name = ctx.path[-1]
        port_map = self.source_instance_connections.get(parent_ctx.module_name, {}).get(instance_name, {})
        expr = port_map.get(port_name)
        if not expr:
            return None

        target_bit = self._target_bit_from_local_signal(local_signal)
        for candidate in self._expr_signal_candidates(parent_ctx, expr, target_bit):
            promoted = self._promote_signal_candidate(parent_ctx, candidate, value, visited)
            if promoted:
                return promoted
        return None

    def _promote_root_id(self, root_id: str) -> str:
        cached = self.literal_root_promotion_cache.get(root_id)
        if cached is not None:
            return cached

        root = self.root_causes.get(root_id)
        if root is None or root.source_type != "literal_connection":
            self.literal_root_promotion_cache[root_id] = root_id
            return root_id

        parts = root.hierarchical_signal.split(".")
        if len(parts) < 2:
            self.literal_root_promotion_cache[root_id] = root_id
            return root_id

        ctx = self.raw_context_map.get(".".join(parts[:-1]))
        promoted = None
        if ctx is not None:
            promoted = self._promote_literal_root_through_parents(
                ctx,
                parts[-1],
                root.constant_value,
                set(),
            )

        canonical_root = promoted or root_id
        self.literal_root_promotion_cache[root_id] = canonical_root
        if canonical_root != root_id:
            self.promoted_root_origins[canonical_root].add(root_id)
        return canonical_root

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
        if target_ctx is not None:
            local_path = self._estimate_local_source_path(
                target_ctx, root.hierarchical_signal, target_signal
            )
            if local_path:
                return self._compress_path(local_path)
        target_nodes = self._target_nodes_for_root(bits, target_ctx, root_id)
        root_nodes = self._root_nodes(root_id)

        if not target_nodes or not root_nodes:
            origin_literals = sorted(self.promoted_root_origins.get(root_id, set()))
            if origin_literals:
                origin_root = self.root_causes.get(origin_literals[0])
                if origin_root is not None:
                    labels = [root.hierarchical_signal]
                    if origin_root.hierarchical_signal not in labels:
                        labels.append(origin_root.hierarchical_signal)
                    if target_signal not in labels:
                        labels.append(target_signal)
                    return self._compress_path(labels)
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

    def _collect_removed_items(self) -> Dict[str, List[RemovedItem]]:
        removed_instances: List[RemovedItem] = []
        removed_cells: List[RemovedItem] = []
        seen_removed_cell_paths: Set[str] = set()

        for before_ctx in self.before_contexts_preorder:
            raw_ctx = self.raw_context_map.get(before_ctx.path_str)
            if raw_ctx is None:
                continue
            opt_ctx = self.opt_context_map.get(before_ctx.path_str)

            raw_index = self.module_indices[raw_ctx.module_name]
            before_module_info = self.before_rtlil_modules.get(before_ctx.module_name)
            if before_module_info is None:
                continue
            opt_module_info = self.opt_rtlil_modules.get(opt_ctx.module_name) if opt_ctx is not None else None

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
            covered_source_instances: Set[str] = set()
            opt_local_signature_counts = Counter(
                self._rtlil_local_cell_match_key(opt_module_info, cell)
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
                    self._rtlil_local_cell_match_key(before_module_info, item[1]),
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
                signature = self._rtlil_local_cell_match_key(before_module_info, cell)
                if opt_local_signature_counts[signature] > 0:
                    opt_local_signature_counts[signature] -= 1
                    continue
                source_match = self._match_source_primitive(
                    before_ctx.module_name,
                    cell_type,
                    cell.src,
                    self._resolve_rtlil_output_signals(before_module_info, cell),
                )
                display_name = source_match.instance_name if source_match is not None else cell_name
                if source_match is not None:
                    covered_source_instances.add(source_match.instance_name)
                affected = self._removed_before_local_cell_outputs(raw_ctx, before_module_info, cell)
                if not affected:
                    continue
                removed_cells.append(
                    RemovedItem(
                        kind="removed_cell",
                        path=f"{before_ctx.path_str}.{display_name}",
                        parent_path=before_ctx.path_str,
                        item_name=display_name,
                        item_type=source_match.primitive_type if source_match is not None else cell_type,
                        src=source_match.src if source_match is not None else cell.src,
                        affected_signals=affected,
                    )
                )
                seen_removed_cell_paths.add(f"{before_ctx.path_str}.{display_name}")

            opt_local_names = {
                name
                for name in (opt_module_info.cells if opt_module_info is not None else {})
                if name not in opt_children
            }
            before_local_names = set(before_local_cells)
            for primitive in self.source_primitive_cells.get(before_ctx.module_name, []):
                item_path = f"{before_ctx.path_str}.{primitive.instance_name}"
                if item_path in seen_removed_cell_paths:
                    continue
                if primitive.instance_name in before_local_names:
                    continue
                if primitive.instance_name in covered_source_instances:
                    continue
                if primitive.instance_name in opt_local_names:
                    continue
                affected = self._removed_source_cell_outputs(raw_ctx, primitive)
                if not affected:
                    continue
                removed_cells.append(
                    RemovedItem(
                        kind="removed_cell",
                        path=item_path,
                        parent_path=before_ctx.path_str,
                        item_name=primitive.instance_name,
                        item_type=primitive.primitive_type,
                        src=primitive.src,
                        affected_signals=affected,
                    )
                )
                seen_removed_cell_paths.add(item_path)

        return {
            "removed_instances": removed_instances,
            "removed_cells": removed_cells,
        }

    def analyze_design(self) -> Dict:
        print("步骤 1: 导出 raw JSON 以及 raw_proc/opt_proc RTLIL...")
        self._export_raw_opt_jsons()

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

        print("步骤 3: 构建 raw_proc/opt_proc RTLIL 层次并对比缺失实例和单元...")
        _, self.before_contexts_preorder, _ = self._build_rtlil_context_tree(self.before_rtlil_modules)
        _, self.opt_contexts_preorder, self.opt_context_map = self._build_rtlil_context_tree(self.opt_rtlil_modules)

        diff_findings = self._collect_removed_items()

        removed_instances = diff_findings["removed_instances"]
        removed_cells = diff_findings["removed_cells"]
        total_affected_signals = sum(len(item.affected_signals) for item in removed_instances + removed_cells)
        referenced_roots = sorted(
            {
                root_id
                for item in removed_instances + removed_cells
                for affected in item.affected_signals
                for root_id in affected.roots
            }
        )

        summary = {
            "removed_instance_count": len(removed_instances),
            "removed_cell_count": len(removed_cells),
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
        if summary["affected_signal_count"] > 0:
            summary["potential_issues"].append(
                f"发现 {summary['affected_signal_count']} 个受被删除项影响的常量信号。"
            )
        if summary["referenced_root_count"] > 0:
            summary["potential_issues"].append(
                f"已将被删除项关联到 {summary['referenced_root_count']} 个显式常量根源。"
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
            "referenced_roots": referenced_root_records,
            "raw_constant_signal_count": len(self.raw_signal_records),
            "raw_conflicts": list(self.conflicts),
            "extra_exports": {
                "raw_json": "raw_design.json",
                "raw_proc_il": "raw_proc.il",
                "opt_proc_il": "opt_proc.il",
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
            "报告类型": "常量传播删除项分析",
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
                    "受影响信号数量": summary["affected_signal_count"],
                    "关联到的显式根源数量": summary["referenced_root_count"],
                    "raw传播冲突数量": summary["conflict_count"],
                    "潜在问题": list(summary["potential_issues"]),
                },
                "被删除的模块实例": [convert_removed_item(item) for item in analysis_results["removed_instances"]],
                "被删除的局部组合单元": [convert_removed_item(item) for item in analysis_results["removed_cells"]],
                "关联到的显式常量根源": [convert_root(root) for root in analysis_results["referenced_roots"]],
                "raw常量信号数量": analysis_results["raw_constant_signal_count"],
                "raw传播冲突": analysis_results["raw_conflicts"],
                "附加导出文件": {
                    "raw_json": analysis_results["extra_exports"]["raw_json"],
                    "raw_proc_il": analysis_results["extra_exports"]["raw_proc_il"],
                    "opt_proc_il": analysis_results["extra_exports"]["opt_proc_il"],
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
            "对比 raw_proc/opt_proc 层次网表，定位被删除的实例或组合单元，"
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
        raw_proc_path = output_dir / "raw_proc.il"
        opt_proc_path = output_dir / "opt_proc.il"
        raw_json_path.write_text(tracer.raw_json_text, encoding="utf-8")
        raw_proc_path.write_text(tracer.raw_proc_rtlil_text, encoding="utf-8")
        opt_proc_path.write_text(tracer.opt_proc_rtlil_text, encoding="utf-8")
        summary = results["summary"]
        print(
            "\n摘要: "
            f"被删除模块实例={summary['removed_instance_count']}，"
            f"被删除局部组合单元={summary['removed_cell_count']}，"
            f"受影响信号={summary['affected_signal_count']}，"
            f"关联根源={summary['referenced_root_count']}，"
            f"冲突={summary['conflict_count']}"
        )
        print(f"\n输出目录: {output_dir}")
        print(f"\n报告已保存到: {output_path}")
        print(f"附加导出文件: {raw_json_path}")
        print(f"附加导出文件: {raw_proc_path}")
        print(f"附加导出文件: {opt_proc_path}")

        has_issue = bool(results["removed_instances"] or results["removed_cells"])
        if has_issue:
            print("\n检测到与被删除项相关的常量传播问题。")
            return 1

        print("\n未检测到与被删除项相关的常量传播问题。")
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
