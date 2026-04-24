#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Parse Verilog/SystemVerilog (single file or directory) with OSS CAD Suite (Yosys) and output AST.

Usage examples:
  # Single file
  python -m eda.ast rtl/top.v --print
  python -m eda.ast rtl/top.v --json ast.json
  python -m eda.ast rtl/top.v --netlist-verilog synth.v --netlist-json netlist.json --top top

  # Directory (recursively parse all .v/.sv under it)
  python -m eda.ast ./rtl --print
  python -m eda.ast ./rtl -I ./include -D WIDTH=32 -D USE_FOO --json ast.json
  python -m eda.ast ./rtl --netlist-verilog synth.v --netlist-json netlist.json --top top
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_OSS_ROOT = Path(
    os.getenv("OSS_CAD_SUITE_ROOT", Path(__file__).resolve().parents[1] / "oss-cad-suite")
).resolve()
YOSYS_BIN_CANDIDATES = ("yosys.exe", "yosys")


def warn_missing_yosys(oss_root: Path) -> None:
    """Warn if the configured OSS CAD Suite/Yosys path looks missing."""
    yosys_bin = next((oss_root / "bin" / name for name in YOSYS_BIN_CANDIDATES if (oss_root / "bin" / name).exists()), None)
    if not oss_root.exists():
        print(f"[WARN] OSS CAD Suite root not found: {oss_root}", file=sys.stderr)
    elif yosys_bin is None:
        print(f"[WARN] Yosys binary not found under {oss_root / 'bin'}", file=sys.stderr)


def collect_verilog_files(dir_path: str, recursive: bool = True) -> List[str]:
    """Collect .v/.sv files under a directory. (Headers are usually included via `include`.)"""
    dir_path = os.path.abspath(dir_path)
    patterns = ["*.v", "*.sv"]
    files: List[str] = []
    for pat in patterns:
        pattern = os.path.join(dir_path, "**", pat) if recursive else os.path.join(dir_path, pat)
        files.extend(glob.glob(pattern, recursive=recursive))
    files = [os.path.abspath(f) for f in files if os.path.isfile(f)]
    files = sorted(set(files))
    return files


def infer_incdirs(target_path: str, user_incdirs: List[str]) -> List[str]:
    """
    Include dirs: by default add:
      - if target is file: its parent directory
      - if target is dir : the dir itself
    plus user-provided -I dirs.
    """
    target_path = os.path.abspath(target_path)
    incdirs: List[str] = []
    if os.path.isdir(target_path):
        incdirs.append(target_path)
    else:
        incdirs.append(os.path.dirname(target_path))

    for d in user_incdirs:
        if not d:
            continue
        incdirs.append(os.path.abspath(d))

    # de-duplicate preserving order
    seen = set()
    uniq = []
    for d in incdirs:
        if d not in seen:
            uniq.append(d)
            seen.add(d)
    return uniq


def build_yosys_env(oss_root: Path) -> Dict[str, str]:
    """Construct an environment similar to oss-cad-suite/environment.ps1."""
    env = os.environ.copy()
    root_str = str(oss_root)
    if not root_str.endswith(os.sep):
        root_str += os.sep
    env["YOSYSHQ_ROOT"] = root_str
    env["PATH"] = f"{oss_root / 'bin'};{oss_root / 'lib'};{env.get('PATH', '')}"
    env.setdefault("SSL_CERT_FILE", str(oss_root / "etc" / "cacert.pem"))
    env.setdefault("PYTHON_EXECUTABLE", str(oss_root / "lib" / "python3.exe"))
    env.setdefault("QT_PLUGIN_PATH", str(oss_root / "lib" / "qt5" / "plugins"))
    env.setdefault("QT_LOGGING_RULES", "*=false")
    env.setdefault("GTK_EXE_PREFIX", root_str)
    env.setdefault("GTK_DATA_PREFIX", root_str)
    env.setdefault(
        "GDK_PIXBUF_MODULEDIR",
        str(oss_root / "lib" / "gdk-pixbuf-2.0" / "2.10.0" / "loaders"),
    )
    env.setdefault(
        "GDK_PIXBUF_MODULE_FILE",
        str(oss_root / "lib" / "gdk-pixbuf-2.0" / "2.10.0" / "loaders.cache"),
    )
    return env


def _find_yosys_bin(oss_root: Path) -> Optional[Path]:
    for name in YOSYS_BIN_CANDIDATES:
        candidate = oss_root / "bin" / name
        if candidate.exists():
            return candidate
    return None


def _quote_for_yosys(path: str) -> str:
    """Normalize path to POSIX style and quote if it contains spaces."""
    posix_path = Path(path).resolve().as_posix()
    if " " in posix_path:
        return f"\"{posix_path}\""
    return posix_path


def extract_ast_text(yosys_output: str) -> str:
    """Extract the AST dump region from Yosys output."""
    lines = yosys_output.splitlines()
    collecting = False
    collected: List[str] = []
    for line in lines:
        if "Dumping AST" in line:
            collecting = True
            continue
        if collecting:
            if line.strip().startswith("--- END OF AST DUMP"):
                # 不要 break，继续收集后续模块的 AST
                collecting = False
                continue
            if not line.strip():
                continue
            collected.append(line.rstrip("\r\n"))
    return "\n".join(collected).strip()


def parse_yosys_ast_text(ast_text: str) -> Dict[str, Any]:
    """
    Convert Yosys AST dump text (indent-based) into a nested dict.
    Each node keeps the raw line under "text" and derives type from the first token.
    """
    if not ast_text:
        return {"type": "AST_ROOT", "children": []}

    root_children: List[Dict[str, Any]] = []
    stack: List[Tuple[int, Dict[str, Any]]] = []
    base_indent: Optional[int] = None

    for raw_line in ast_text.splitlines():
        if not raw_line.strip():
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if base_indent is None:
            base_indent = indent
        rel_indent = max(indent - base_indent, 0)
        text = raw_line.strip()
        node_type = text.split()[0] if text else "UNKNOWN"
        node = {"type": node_type, "text": text, "children": []}

        while stack and rel_indent <= stack[-1][0]:
            stack.pop()

        if stack:
            stack[-1][1]["children"].append(node)
        else:
            root_children.append(node)

        stack.append((rel_indent, node))

    return {"type": "AST_ROOT", "backend": "yosys", "children": root_children}


def run_yosys_for_ast(
    files: List[str],
    incdirs: List[str],
    defines: List[str],
    oss_root: Optional[Path] = None,
    simplified: bool = False,
    base_root: Optional[Path] = None,
) -> Tuple[str, str]:
    """Run Yosys read_verilog with -dump_ast to obtain AST text and the full log."""
    if not files:
        raise ValueError("No input files provided for AST generation.")

    root = oss_root.resolve() if oss_root else DEFAULT_OSS_ROOT
    yosys_bin = _find_yosys_bin(root)
    if yosys_bin is None:
        raise FileNotFoundError(f"Yosys binary not found under {root / 'bin'}")

    flags = ["-sv", "-no_dump_ptr"]
    flags.append("-dump_ast2" if simplified else "-dump_ast1")
    flags += [f"-I{_quote_for_yosys(d)}" for d in incdirs if d]
    flags += [f"-D{d}" for d in defines if d]
    flags += [_quote_for_yosys(f) for f in files]

    script = "read_verilog " + " ".join(flags)
    env = build_yosys_env(root)
    proc = subprocess.run(
        [str(yosys_bin), "-p", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Yosys failed with exit code {proc.returncode}.\n"
            f"Command: {yosys_bin} -p {script}\n"
            f"Output:\n{output}"
        )

    ast_text = extract_ast_text(output)
    if not ast_text:
        raise RuntimeError("Failed to extract AST dump from Yosys output.")
    return ast_text, output


def run_yosys_for_netlist(
    files: List[str],
    incdirs: List[str],
    defines: List[str],
    verilog_out: Optional[str],
    json_out: Optional[str],
    oss_root: Optional[Path] = None,
    top: Optional[str] = None,
) -> str:
    """Run Yosys synth and export netlists with write_verilog/write_json."""
    if not files:
        raise ValueError("No input files provided for netlist generation.")
    if not verilog_out and not json_out:
        raise ValueError("At least one netlist output (verilog/json) must be specified.")

    root = oss_root.resolve() if oss_root else DEFAULT_OSS_ROOT
    yosys_bin = _find_yosys_bin(root)
    if yosys_bin is None:
        raise FileNotFoundError(f"Yosys binary not found under {root / 'bin'}")

    if verilog_out:
        Path(verilog_out).resolve().parent.mkdir(parents=True, exist_ok=True)
    if json_out:
        Path(json_out).resolve().parent.mkdir(parents=True, exist_ok=True)

    flags = ["-sv", "-no_dump_ptr"]
    flags += [f"-I{_quote_for_yosys(d)}" for d in incdirs if d]
    flags += [f"-D{d}" for d in defines if d]
    flags += [_quote_for_yosys(f) for f in files]

    script_parts = ["read_verilog " + " ".join(flags)]
    synth_cmd = "synth"
    if top:
        synth_cmd += f" -top {top}"
    script_parts.append(synth_cmd)
    if verilog_out:
        script_parts.append(f"write_verilog {_quote_for_yosys(verilog_out)}")
    if json_out:
        script_parts.append(f"write_json {_quote_for_yosys(json_out)}")

    script = "; ".join(script_parts)
    env = build_yosys_env(root)
    proc = subprocess.run(
        [str(yosys_bin), "-p", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Yosys failed with exit code {proc.returncode}.\n"
            f"Command: {yosys_bin} -p {script}\n"
            f"Output:\n{output}"
        )

    return output



def run_yosys_for_rtlil_processes(
    files: List[str],
    incdirs: List[str],
    defines: List[str],
    rtlil_out: str,
    oss_root: Optional[Path] = None,
    top: Optional[str] = None,
) -> str:
    """Run Yosys and export RTLIL before proc pass (processes preserved for CFG/DDG analysis)."""
    if not files:
        raise ValueError("No input files provided for RTLIL generation.")
    if not rtlil_out:
        raise ValueError("Output path is required for RTLIL generation.")

    root = oss_root.resolve() if oss_root else DEFAULT_OSS_ROOT
    yosys_bin = _find_yosys_bin(root)
    if yosys_bin is None:
        raise FileNotFoundError(f"Yosys binary not found under {root / 'bin'}")

    Path(rtlil_out).resolve().parent.mkdir(parents=True, exist_ok=True)

    flags = ["-sv", "-no_dump_ptr"]
    flags += [f"-I{_quote_for_yosys(d)}" for d in incdirs if d]
    flags += [f"-D{d}" for d in defines if d]
    flags += [_quote_for_yosys(f) for f in files]

    script_parts = ["read_verilog " + " ".join(flags)]
    if top:
        script_parts.append(f"hierarchy -top {top}")
    # 不执行 proc，保留 RTLIL::Process 结构用于 CFG/DDG 分析
    script_parts.append(f"write_rtlil {_quote_for_yosys(rtlil_out)}")

    script = "; ".join(script_parts)
    env = build_yosys_env(root)
    proc = subprocess.run(
        [str(yosys_bin), "-p", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Yosys failed with exit code {proc.returncode}.\n"
            f"Command: {yosys_bin} -p {script}\n"
            f"Output:\n{output}"
        )

    return output


def run_yosys_for_dfg_from_rtlil_text(
    rtlil_text: str,
    dfg_dot_out: str,
    oss_root: Optional[Path] = None,
    top: Optional[str] = None,
    effort: Optional[int] = None,
) -> str:
    """Run Yosys viz pass on RTLIL text to generate a data flow graph (DFG) DOT file (post-proc)."""
    if not rtlil_text:
        raise ValueError("No RTLIL text provided for DFG generation.")
    if not dfg_dot_out:
        raise ValueError("Output path is required for DFG generation.")

    root = oss_root.resolve() if oss_root else DEFAULT_OSS_ROOT
    yosys_bin = _find_yosys_bin(root)
    if yosys_bin is None:
        raise FileNotFoundError(f"Yosys binary not found under {root / 'bin'}")

    out_path = Path(dfg_dot_out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prefix = out_path.with_suffix("")

    with tempfile.TemporaryDirectory(prefix="yosys_dfg_") as temp_dir:
        rtlil_path = Path(temp_dir) / "design.rtlil"
        rtlil_path.write_text(rtlil_text, encoding="utf-8")

        script_parts = [f"read_rtlil {_quote_for_yosys(str(rtlil_path))}"]
        if top:
            script_parts.append(f"hierarchy -top {top}")
        script_parts.append("proc")  # 执行 proc 转换 Process 为门级单元
        # 生成 .dot 文件
        viz_cmd = f"viz -format dot -prefix {_quote_for_yosys(str(prefix))}"
        if effort is not None:
            viz_cmd += f" -{int(effort)}"
        script_parts.append(viz_cmd)

        script = "; ".join(script_parts)
        env = build_yosys_env(root)
        proc = subprocess.run(
            [str(yosys_bin), "-p", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise RuntimeError(
                f"Yosys failed with exit code {proc.returncode}.\n"
                f"Command: {yosys_bin} -p {script}\n"
                f"Output:\n{output}"
            )

    # 返回 .dot 文件路径
    return str(prefix.with_suffix(".dot"))


_SIG_TOKEN_RE = re.compile(r"(\\[^\s\[\]\{\}\(\)]+|\$[^\s\[\]\{\}\(\)]+)")

_DOT_ID = r"(?:\"[^\"]*\"|[A-Za-z0-9_:$\.-]+)"
_DOT_NODE_RE = re.compile(rf"^\s*({_DOT_ID})\s*\[(.*)\]\s*;?\s*$")
_DOT_EDGE_RE = re.compile(rf"^\s*({_DOT_ID})\s*->\s*({_DOT_ID})\s*(?:\[(.*)\])?\s*;?\s*$")


def _strip_dot_id(token: str) -> str:
    token = token.strip()
    if (token.startswith("\"") and token.endswith("\"")) or (token.startswith("'") and token.endswith("'")):
        return token[1:-1]
    return token


def _parse_dot_attrs(attr_text: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    if not attr_text:
        return attrs
    buf: List[str] = []
    current: List[str] = []
    in_quote: Optional[str] = None
    escaped = False
    for ch in attr_text:
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if ch == "\\" and in_quote:
            current.append(ch)
            escaped = True
            continue
        if ch in ("\"", "'"):
            if in_quote == ch:
                in_quote = None
            elif in_quote is None:
                in_quote = ch
            current.append(ch)
            continue
        if ch == "," and in_quote is None:
            buf.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if current:
        buf.append("".join(current).strip())

    for item in buf:
        if not item:
            continue
        if "=" in item:
            key, val = item.split("=", 1)
            key = key.strip()
            val = val.strip().strip("\"").strip("'")
            attrs[key] = val
        else:
            attrs[item] = ""
    return attrs


def _parse_dfg_dot(dot_text: str) -> Dict[str, Any]:
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []

    for raw_line in dot_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith(("digraph", "graph", "subgraph")) or line in ("{", "}"):
            continue
        if line.startswith(("node ", "edge ", "graph ")):
            continue

        edge_match = _DOT_EDGE_RE.match(line)
        if edge_match:
            src = _strip_dot_id(edge_match.group(1))
            dst = _strip_dot_id(edge_match.group(2))
            attrs = _parse_dot_attrs(edge_match.group(3) or "")
            edge: Dict[str, Any] = {"src": src, "dst": dst}
            if attrs:
                edge["attrs"] = attrs
                if "label" in attrs:
                    edge["label"] = attrs["label"]
            edges.append(edge)
            continue

        node_match = _DOT_NODE_RE.match(line)
        if node_match:
            node_id = _strip_dot_id(node_match.group(1))
            attrs = _parse_dot_attrs(node_match.group(2) or "")
            if node_id not in nodes:
                nodes[node_id] = {"id": node_id}
            if attrs:
                nodes[node_id]["attrs"] = attrs
                if "label" in attrs:
                    nodes[node_id]["label"] = attrs["label"]

    return {"nodes": list(nodes.values()), "edges": edges}


def _extract_signal_tokens(text: str) -> List[str]:
    if not text:
        return []
    return sorted(set(m.group(0) for m in _SIG_TOKEN_RE.finditer(text)))


def _parse_rtlil_processes(rtlil_text: str) -> Dict[str, Any]:
    modules: Dict[str, Any] = {}
    current_module: Optional[Dict[str, Any]] = None
    stack: List[Dict[str, Any]] = []

    explicit_end_blocks = {"module", "process", "switch", "cell", "memory"}

    def find_nearest(block_types: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
        for item in reversed(stack):
            if item["type"] in block_types:
                return item
        return None

    def append_stmt(stmt: Dict[str, Any]) -> None:
        parent = find_nearest(("case", "sync", "process"))
        if parent is None:
            return
        if parent["type"] == "process":
            parent["body"].append(stmt)
        elif parent["type"] == "case":
            parent["body"].append(stmt)
        elif parent["type"] == "sync":
            parent["actions"].append(stmt)

    def close_explicit_block() -> None:
        nonlocal current_module
        while stack:
            popped = stack.pop()
            if popped["type"] == "module":
                current_module = None
            if popped["type"] in explicit_end_blocks:
                break

    for raw_line in rtlil_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("module "):
            name = line.split(None, 1)[1]
            current_module = {"type": "module", "name": name, "processes": []}
            modules[name] = current_module
            stack = [current_module]
            continue

        if line == "end":
            close_explicit_block()
            continue

        if not stack:
            continue

        if line.startswith("cell "):
            stack.append({"type": "cell"})
            continue

        if line.startswith("memory "):
            stack.append({"type": "memory"})
            continue

        if line.startswith("process "):
            if current_module is None:
                continue
            proc_name = line.split(None, 1)[1]
            proc = {"type": "process", "name": proc_name, "body": []}
            current_module["processes"].append(proc)
            stack.append(proc)
            continue

        if line.startswith("switch "):
            cond = line[len("switch "):].strip()
            node = {"type": "switch", "cond": cond, "cases": []}
            append_stmt(node)
            stack.append(node)
            continue

        if line.startswith("case"):
            while stack and stack[-1]["type"] == "case":
                stack.pop()
            rest = line[len("case"):].strip()
            case_val = rest if rest else None
            case_node = {"type": "case", "value": case_val, "body": []}
            switch_node = find_nearest(("switch",))
            if switch_node is not None:
                switch_node["cases"].append(case_node)
                stack.append(case_node)
            continue

        if line.startswith("sync "):
            while stack and stack[-1]["type"] == "sync":
                stack.pop()
            event = line[len("sync "):].strip()
            node = {"type": "sync", "event": event, "actions": []}
            append_stmt(node)
            stack.append(node)
            continue

        if line.startswith("assign "):
            parts = line.split(None, 2)
            lhs = parts[1] if len(parts) > 1 else ""
            rhs = parts[2] if len(parts) > 2 else ""
            node = {"type": "assign", "lhs": lhs, "rhs": rhs, "text": line}
            append_stmt(node)
            continue

        if line.startswith("update "):
            parts = line.split(None, 2)
            lhs = parts[1] if len(parts) > 1 else ""
            rhs = parts[2] if len(parts) > 2 else ""
            node = {"type": "update", "lhs": lhs, "rhs": rhs, "text": line}
            append_stmt(node)
            continue

    return modules


def _build_cfg_ddg_for_process(proc: Dict[str, Any]) -> Dict[str, Any]:
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    next_id = 0

    def new_node(node_type: str, text: str, defs: List[str], uses: List[str]) -> int:
        nonlocal next_id
        node_id = next_id
        next_id += 1
        nodes.append(
            {
                "id": node_id,
                "type": node_type,
                "text": text,
                "defs": defs,
                "uses": uses,
            }
        )
        return node_id

    def build_body(body: List[Dict[str, Any]]) -> Tuple[Optional[int], List[int]]:
        entry_id: Optional[int] = None
        prev_exits: List[int] = []
        for stmt in body:
            stmt_entry, stmt_exits = build_stmt(stmt)
            if stmt_entry is None:
                continue
            if entry_id is None:
                entry_id = stmt_entry
            for prev in prev_exits:
                edges.append({"src": prev, "dst": stmt_entry})
            prev_exits = stmt_exits
        return entry_id, prev_exits

    def build_stmt(stmt: Dict[str, Any]) -> Tuple[Optional[int], List[int]]:
        stype = stmt.get("type")
        if stype in ("assign", "update"):
            lhs = stmt.get("lhs", "")
            rhs = stmt.get("rhs", "")
            node_id = new_node(stype, stmt.get("text", ""), _extract_signal_tokens(lhs), _extract_signal_tokens(rhs))
            return node_id, [node_id]

        if stype == "switch":
            cond = stmt.get("cond", "")
            node_id = new_node("switch", f"switch {cond}", [], _extract_signal_tokens(cond))
            merge_id = new_node("switch_merge", "switch_merge", [], [])
            exit_ids: List[int] = [merge_id]
            cases = stmt.get("cases", [])
            if cases:
                has_default = False
                for case in cases:
                    label = case.get("value")
                    if label is None:
                        has_default = True
                    case_entry, case_exits = build_body(case.get("body", []))
                    if case_entry is not None:
                        edge = {"src": node_id, "dst": case_entry}
                        if label:
                            edge["label"] = label
                        else:
                            edge["label"] = "default"
                        edges.append(edge)
                        for case_exit in case_exits:
                            edges.append({"src": case_exit, "dst": merge_id})
                    else:
                        edges.append({"src": node_id, "dst": merge_id, "label": label or "default"})
                if not has_default:
                    edges.append({"src": node_id, "dst": merge_id, "label": "default"})
            else:
                edges.append({"src": node_id, "dst": merge_id})
            return node_id, exit_ids

        if stype == "sync":
            event = stmt.get("event", "")
            node_id = new_node("sync", f"sync {event}", [], _extract_signal_tokens(event))
            entry, exits = build_body(stmt.get("actions", []))
            if entry is not None:
                edges.append({"src": node_id, "dst": entry, "label": "sync"})
                return node_id, exits
            return node_id, [node_id]

        return None, []

    start_id = new_node("process_start", proc.get("name", ""), [], [])
    entry, exits = build_body(proc.get("body", []))
    if entry is not None:
        edges.append({"src": start_id, "dst": entry})
    else:
        exits = [start_id]

    preds: Dict[int, List[int]] = {node["id"]: [] for node in nodes}
    for edge in edges:
        preds.setdefault(edge["dst"], []).append(edge["src"])

    defs_map: Dict[int, List[str]] = {node["id"]: node.get("defs", []) for node in nodes}
    uses_map: Dict[int, List[str]] = {node["id"]: node.get("uses", []) for node in nodes}

    in_map: Dict[int, Dict[str, set]] = {node["id"]: {} for node in nodes}
    out_map: Dict[int, Dict[str, set]] = {node["id"]: {} for node in nodes}

    def merge_incoming(node_id: int) -> Dict[str, set]:
        merged: Dict[str, set] = {}
        for pred in preds.get(node_id, []):
            for sig, defs in out_map.get(pred, {}).items():
                merged.setdefault(sig, set()).update(defs)
        return merged

    changed = True
    max_iters = max(1, len(nodes) * 4)
    iters = 0
    while changed and iters < max_iters:
        changed = False
        iters += 1
        for node in nodes:
            node_id = node["id"]
            new_in = merge_incoming(node_id)
            new_out: Dict[str, set] = {sig: set(defs) for sig, defs in new_in.items()}
            for sig in defs_map.get(node_id, []):
                new_out[sig] = {node_id}

            if new_in != in_map[node_id] or new_out != out_map[node_id]:
                in_map[node_id] = new_in
                out_map[node_id] = new_out
                changed = True

    ddg_edges: List[Dict[str, Any]] = []
    for node in nodes:
        node_id = node["id"]
        reaching = in_map.get(node_id, {})
        for sig in uses_map.get(node_id, []):
            for def_id in sorted(reaching.get(sig, set())):
                ddg_edges.append({"src": def_id, "dst": node_id, "signal": sig})

    cfg = {"entry": start_id, "nodes": nodes, "edges": edges}
    ddg = {"nodes": nodes, "edges": ddg_edges}
    return {"cfg": cfg, "ddg": ddg}


def build_cfg_ddg_from_rtlil_processes(
    rtlil_text: str,
    dfg_dot_out: Optional[str] = None,
    oss_root: Optional[Path] = None,
    top: Optional[str] = None,
    dfg_effort: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Build CFG/DDG from RTLIL::Process blocks (pre-proc).
    This is a structural approximation based on process statement order.
    When dfg_dot_out is provided, generate DFG using Yosys viz pass and attach DFG JSON.
    """
    modules = _parse_rtlil_processes(rtlil_text)
    out: Dict[str, Any] = {"modules": {}}
    for mod_name, mod in modules.items():
        proc_out = []
        for proc in mod.get("processes", []):
            graphs = _build_cfg_ddg_for_process(proc)
            proc_out.append({"process": proc.get("name", ""), **graphs})
        out["modules"][mod_name] = proc_out
    if dfg_dot_out:
        dfg_dot = run_yosys_for_dfg_from_rtlil_text(
            rtlil_text,
            dfg_dot_out,
            oss_root=oss_root,
            top=top,
            effort=dfg_effort,
        )
        # 现在 run_yosys_for_dfg_from_rtlil_text 返回 .dot 文件路径
        dot_path = Path(dfg_dot)
        dot_text = dot_path.read_text(encoding="utf-8", errors="replace")
        out["dfg_dot"] = str(dot_path)
        out["dfg_json"] = _parse_dfg_dot(dot_text)
    return out


def _sanitize_yosys_incompatible_constructs(text: str) -> str:
    """
    Yosys doesn't recognize some simulation-only system tasks/functions used in demo code.
    Replace a small known set with syntactically valid placeholders so the parser can proceed.
    """
    def replace_call_expr(src: str, func: str, replacement: str) -> str:
        """
        Replace occurrences of func(<...balanced...>) with replacement.
        Best-effort: skips over strings and line/block comments.
        """
        needle = func + "("
        i = 0
        out: List[str] = []
        n = len(src)
        while i < n:
            j = src.find(needle, i)
            if j < 0:
                out.append(src[i:])
                break

            out.append(src[i:j])
            k = j + len(func)  # points at '('
            if k >= n or src[k] != "(":
                out.append(src[j:k])
                i = k
                continue

            depth = 0
            in_str: Optional[str] = None
            in_line_comment = False
            in_block_comment = False

            p = k
            while p < n:
                ch = src[p]
                nxt = src[p + 1] if p + 1 < n else ""

                if in_line_comment:
                    if ch == "\n":
                        in_line_comment = False
                    p += 1
                    continue

                if in_block_comment:
                    if ch == "*" and nxt == "/":
                        in_block_comment = False
                        p += 2
                        continue
                    p += 1
                    continue

                if in_str is not None:
                    if ch == "\\":
                        p += 2
                        continue
                    if ch == in_str:
                        in_str = None
                    p += 1
                    continue

                if ch == "/" and nxt == "/":
                    in_line_comment = True
                    p += 2
                    continue
                if ch == "/" and nxt == "*":
                    in_block_comment = True
                    p += 2
                    continue
                if ch in ("\"", "'"):
                    in_str = ch
                    p += 1
                    continue

                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        p += 1
                        break
                p += 1

            # If we didn't find a matching ')', keep original text
            if depth != 0:
                out.append(src[j:p])
                i = p
                continue

            out.append(replacement)
            i = p

        return "".join(out)

    def replace_task_stmt(src: str, task: str) -> str:
        """
        Replace occurrences of task(<...>); with a bare ';' so statement remains valid.
        """
        needle = task + "("
        i = 0
        out: List[str] = []
        n = len(src)
        while i < n:
            j = src.find(needle, i)
            if j < 0:
                out.append(src[i:])
                break
            out.append(src[i:j])

            # Find matching ')' for the call
            k = j + len(task)  # '('
            if k >= n or src[k] != "(":
                out.append(src[j:k])
                i = k
                continue

            depth = 0
            p = k
            while p < n:
                ch = src[p]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        p += 1
                        break
                p += 1

            if depth != 0:
                out.append(src[j:p])
                i = p
                continue

            # Consume optional whitespace then a trailing semicolon if present
            q = p
            while q < n and src[q] in (" ", "\t", "\r", "\n"):
                q += 1
            if q < n and src[q] == ";":
                q += 1
            out.append(";")
            i = q

        return "".join(out)

    # Function-like calls -> replace with constant 0
    for func_name in (
        "$readSignedByte",
        "$readUnsignedByte",
        "$readSignedHalf",
        "$readUnsignedHalf",
        "$readWord",
    ):
        text = replace_call_expr(text, func_name, "0")

    # Task-like calls -> replace with empty statement
    for task_name in ("$writeByte", "$writeHalf", "$writeWord"):
        text = replace_task_stmt(text, task_name)

    return text


def _prepare_temp_sources(
    files: List[str],
    incdirs: List[str],
    base_root: Path,
) -> Tuple[List[str], List[str], str]:
    """
    Create a temporary mirror of sources under base_root and rewrite a small set of
    simulation-only system calls that Yosys can't parse.
    Returns (temp_files, temp_incdirs, temp_root_dir).
    """
    temp_root = Path(tempfile.mkdtemp(prefix="yosys_ast_")).resolve()

    temp_files: List[str] = []
    for f in files:
        src = Path(f).resolve()
        try:
            rel = src.relative_to(base_root)
        except Exception:
            rel = Path(src.name)
        dst = (temp_root / rel).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)
        raw = src.read_text(encoding="utf-8", errors="replace")
        dst.write_text(_sanitize_yosys_incompatible_constructs(raw), encoding="utf-8")
        temp_files.append(str(dst))

    temp_incdirs: List[str] = []
    for d in incdirs:
        if not d:
            continue
        src_dir = Path(d).resolve()
        try:
            rel = src_dir.relative_to(base_root)
            # Prefer temp-mirrored include dir (for relative includes), but keep original as fallback
            temp_incdirs.append(str((temp_root / rel).resolve()))
            temp_incdirs.append(str(src_dir))
        except Exception:
            temp_incdirs.append(str(src_dir))

    return temp_files, temp_incdirs, str(temp_root)


def node_to_dict(node: Any, include_coord: bool = False) -> Optional[Dict[str, Any]]:
    """
    Convert an AST representation into a JSON-serializable dict.
    Supports both Yosys AST dicts and legacy Pyverilog nodes (best-effort).
    """
    if node is None:
        return None
    if isinstance(node, dict):
        # Already JSON-friendly (e.g., from Yosys)
        return node

    # Legacy Pyverilog fallback (kept for compatibility)
    d: Dict[str, Any] = {"type": node.__class__.__name__}
    node_dict = getattr(node, "__dict__", {})
    for k, v in node_dict.items():
        if k == "coord" and not include_coord:
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            d[k] = v
        if k == "coord" and include_coord:
            d[k] = str(v)

    children = []
    try:
        for c in node.children():
            children.append(node_to_dict(c, include_coord=include_coord))
    except Exception:
        pass
    d["children"] = children
    return d


def parse_target(
    path: str,
    incdirs: List[str],
    defines: List[str],
    recursive: bool = True,
    oss_root: Optional[Path] = None,
    simplified: bool = False,
) -> Tuple[Any, Dict[str, Any], List[str]]:
    """
    Parse either a single file or a directory (collecting all .v/.sv files) using Yosys.
    Returns: (ast_tree_dict, meta, file_list_used)
    """
    abs_path = os.path.abspath(path)

    if os.path.isdir(abs_path):
        files = collect_verilog_files(abs_path, recursive=recursive)
        if not files:
            raise FileNotFoundError(f"No .v/.sv files found under directory: {abs_path}")
    else:
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        files = [abs_path]

    # Mirror sources into a temp tree so relative includes keep working, and sanitize a small set
    # of simulation-only system calls that Yosys can't resolve (e.g. $readSignedByte()).
    base_root = Path(abs_path if os.path.isdir(abs_path) else os.path.dirname(abs_path)).resolve()
    temp_root_dir: Optional[str] = None
    try:
        temp_files, temp_incdirs, temp_root_dir = _prepare_temp_sources(files, incdirs, base_root=base_root)
        ast_text, yosys_log = run_yosys_for_ast(
            temp_files,
            incdirs=temp_incdirs,
            defines=defines,
            oss_root=oss_root,
            simplified=simplified,
            base_root=base_root,
        )
    finally:
        if temp_root_dir:
            try:
                import shutil
                shutil.rmtree(temp_root_dir, ignore_errors=True)
            except Exception:
                pass

    ast_tree = parse_yosys_ast_text(ast_text)
    ast_tree["source_files"] = files

    meta = {"ast_text": ast_text, "yosys_log": yosys_log}
    return ast_tree, meta, files


def main() -> None:
    # Force UTF-8 encoding on Windows console if possible
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="Get Verilog/SystemVerilog AST using oss-cad-suite Yosys (input can be a file or a directory)."
    )
    ap.add_argument(
        "path",
        help="A Verilog/SystemVerilog file or a directory containing Verilog files",
    )
    ap.add_argument(
        "-I",
        "--incdir",
        action="append",
        default=[],
        help="Include directory for `include` (can pass multiple times)",
    )
    ap.add_argument(
        "-D",
        "--define",
        action="append",
        default=[],
        help="Macro define like FOO or WIDTH=32 (can pass multiple times)",
    )
    ap.add_argument(
        "--no-recursive",
        action="store_true",
        help="When path is a directory, do not search recursively",
    )
    ap.add_argument(
        "--print",
        action="store_true",
        help="Print AST (Yosys dump) to stdout",
    )
    ap.add_argument(
        "--json",
        default=None,
        help="Write AST to JSON file (e.g., ast.json)",
    )
    ap.add_argument(
        "--with-coord",
        action="store_true",
        help="Legacy option (kept for compatibility, ignored for Yosys AST)",
    )
    ap.add_argument(
        "--list-files",
        action="store_true",
        help="Print the final file list used for parsing to stderr",
    )
    ap.add_argument(
        "--oss-root",
        default=None,
        help="Path to oss-cad-suite root (default: ../oss-cad-suite relative to this script)",
    )
    ap.add_argument(
        "--simplified",
        action="store_true",
        help="Use Yosys -dump_ast2 (after simplification) instead of -dump_ast1",
    )
    ap.add_argument(
        "--netlist-verilog",
        default=None,
        help="Write synthesized netlist Verilog with Yosys write_verilog",
    )
    ap.add_argument(
        "--netlist-json",
        default=None,
        help="Write synthesized netlist JSON with Yosys write_json",
    )
    ap.add_argument(
        "--top",
        default=None,
        help="Top module name for Yosys synth (optional but recommended)",
    )

    args = ap.parse_args()
    oss_root = Path(args.oss_root).resolve() if args.oss_root else DEFAULT_OSS_ROOT
    warn_missing_yosys(oss_root)

    incdirs = infer_incdirs(args.path, args.incdir)
    defines = args.define[:]  # already like ["FOO", "WIDTH=32"]

    try:
        ast, meta, used_files = parse_target(
            args.path,
            incdirs=incdirs,
            defines=defines,
            recursive=(not args.no_recursive),
            oss_root=oss_root,
            simplified=args.simplified,
        )
    except Exception as e:
        print(f"[ERROR] Parse failed: {e}", file=sys.stderr)
        sys.exit(2)

    if args.list_files:
        print("[INFO] Files parsed:", file=sys.stderr)
        for f in used_files:
            print(f"  {f}", file=sys.stderr)
        print("[INFO] Include dirs:", file=sys.stderr)
        for d in incdirs:
            print(f"  {d}", file=sys.stderr)
        if defines:
            print("[INFO] Defines:", file=sys.stderr)
            for d in defines:
                print(f"  {d}", file=sys.stderr)

    if args.print:
        print(meta.get("ast_text", ""))

    if args.json:
        out = node_to_dict(ast, include_coord=args.with_coord)
        with open(args.json, "w", encoding="utf-8") as w:
            json.dump(out, w, ensure_ascii=False, indent=2)
        print(f"[OK] Wrote JSON AST to: {args.json}")

    if args.netlist_verilog or args.netlist_json:
        base_root = Path(os.path.abspath(args.path if os.path.isdir(args.path) else os.path.dirname(args.path))).resolve()
        temp_root_dir: Optional[str] = None
        try:
            temp_files, temp_incdirs, temp_root_dir = _prepare_temp_sources(
                used_files, incdirs, base_root=base_root
            )
            yosys_log = run_yosys_for_netlist(
                temp_files,
                incdirs=temp_incdirs,
                defines=defines,
                verilog_out=args.netlist_verilog,
                json_out=args.netlist_json,
                oss_root=oss_root,
                top=args.top,
            )
        except Exception as e:
            print(f"[ERROR] Netlist export failed: {e}", file=sys.stderr)
            sys.exit(3)
        finally:
            if temp_root_dir:
                try:
                    import shutil
                    shutil.rmtree(temp_root_dir, ignore_errors=True)
                except Exception:
                    pass

        if args.netlist_verilog:
            print(f"[OK] Wrote synthesized Verilog netlist to: {args.netlist_verilog}")
        if args.netlist_json:
            print(f"[OK] Wrote synthesized JSON netlist to: {args.netlist_json}")


if __name__ == "__main__":
    main()
