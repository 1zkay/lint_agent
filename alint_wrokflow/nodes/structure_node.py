"""
Node 2: 用 Yosys/AST.py 生成 AST、CFG/DDG/DFG

对应原 _analyze_verilog_structure_impl()
"""
import json
import logging
import shutil
from pathlib import Path
from typing import Optional

from agent.state import AlintWorkflowState
from utils import resolve_project_verilog_inputs

logger = logging.getLogger(__name__)

# ── Yosys 后端：延迟导入 ────────────────────────────────────────
try:
    from AST import (
        parse_target,
        node_to_dict,
        _prepare_temp_sources,
        run_yosys_for_rtlil_processes,
        build_cfg_ddg_from_rtlil_processes,
    )
    AST_AVAILABLE = True
except ImportError as e:
    AST_AVAILABLE = False
    logger.warning(f"AST module not available: {e}")


async def run_structure_node(state: AlintWorkflowState) -> dict:
    """
    LangGraph 节点：生成 AST + CFG/DDG/DFG

    输入 state 字段: workspace_path, project_name, alint_output_dir
    输出 state 字段: ast_json_file, cfg_ddg_file, rtlil_file, v_files, project_dir
    （或 error）
    """
    if not AST_AVAILABLE:
        return {"error": "AST module not available."}

    workspace_path: str = state["workspace_path"]
    project_name: str = state["project_name"]
    output_dir: str = state.get("alint_output_dir", "")

    inputs, err = resolve_project_verilog_inputs(workspace_path, project_name)
    if err:
        return {"error": err}

    v_files = inputs["v_files"]
    input_path = inputs["input_path"]
    project_dir = inputs["project_dir"]
    incdirs = inputs["incdirs"]
    base_name = inputs["base_name"]

    out_dir = Path(output_dir).resolve() if output_dir else Path(state.get("alint_output_dir", "."))
    out_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {}

    # ── AST ─────────────────────────────────────────────────────
    ast_output = str(out_dir / f"{base_name}_AST2.json")
    try:
        ast_tree, meta, _ = parse_target(
            input_path,
            incdirs=incdirs,
            defines=[],
            recursive=True,
            simplified=True,
        )
        ast_dict = node_to_dict(ast_tree, include_coord=False)
        with open(ast_output, "w", encoding="utf-8") as f:
            json.dump(ast_dict, f, ensure_ascii=False, indent=2)
        result["ast_json_file"] = ast_output
        logger.info(f"[structure_node] AST → {ast_output}")
    except Exception as e:
        logger.exception("[structure_node] AST failed")
        result["ast_json_file"] = None
        logger.warning(f"[structure_node] AST error (non-fatal): {e}")

    # ── CFG / DDG / DFG ─────────────────────────────────────────
    cfg_output = str(out_dir / f"{base_name}_cfg_ddg_dfg.json")
    rtlil_output = str(out_dir / f"{base_name}_process.rtlil")
    dfg_output = str(out_dir / f"{base_name}_dfg.dot")
    temp_root_dir = None
    try:
        temp_files, temp_incdirs, temp_root_dir = _prepare_temp_sources(
            v_files, incdirs, base_root=project_dir
        )
        run_yosys_for_rtlil_processes(
            temp_files,
            incdirs=temp_incdirs,
            defines=[],
            rtlil_out=rtlil_output,
        )
        rtlil_text = Path(rtlil_output).read_text(encoding="utf-8", errors="replace")
        graphs = build_cfg_ddg_from_rtlil_processes(
            rtlil_text,
            dfg_dot_out=dfg_output,
            dfg_effort=0,
        )
        graphs["source_files"] = v_files
        with open(cfg_output, "w", encoding="utf-8") as f:
            json.dump(graphs, f, ensure_ascii=False, indent=2)
        result["cfg_ddg_file"] = cfg_output
        result["rtlil_file"] = rtlil_output
        logger.info(f"[structure_node] CFG/DDG → {cfg_output}")
    except Exception as e:
        logger.exception("[structure_node] CFG failed")
        result["cfg_ddg_file"] = None
        result["rtlil_file"] = None
        logger.warning(f"[structure_node] CFG error (non-fatal): {e}")
    finally:
        if temp_root_dir:
            shutil.rmtree(temp_root_dir, ignore_errors=True)

    result["v_files"] = v_files
    result["project_dir"] = str(project_dir)
    return result
