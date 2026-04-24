"""Yosys structure-analysis MCP tools."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from workspace.project_utils import get_project_report_dir, resolve_project_verilog_inputs
from mcp_server.eda_backend import (
    AST_AVAILABLE,
    _prepare_temp_sources,
    build_cfg_ddg_from_rtlil_processes,
    node_to_dict,
    parse_target,
    run_yosys_for_rtlil_processes,
)
from mcp_server.pathing import resolve_workspace_path, to_workspace_virtual_path


def register_structure_tools(mcp) -> None:
    """Register direct AST/CFG/DDG/DFG analysis tools."""

    @mcp.tool()
    async def analyze_verilog_structure(
        workspace_path: str,
        project_name: str,
        analysis_type: str = "all",
        output_dir: Optional[str] = None,
        simplified_ast: bool = True,
        top: Optional[str] = None,
        oss_root: Optional[str] = None,
        dfg_effort: Optional[int] = 0,
    ) -> dict:
        """使用 Yosys 分析 Verilog/SystemVerilog 结构，生成 AST 和/或 CFG/DDG/DFG。"""
        if not AST_AVAILABLE:
            return {"error": "AST module not available. Ensure eda.ast is importable."}
        if analysis_type not in ("ast", "cfg", "all"):
            return {"error": "analysis_type must be 'ast', 'cfg', or 'all'"}

        inputs, err = resolve_project_verilog_inputs(workspace_path, project_name)
        if err:
            return {"error": err}

        v_files = inputs["v_files"]
        input_path = inputs["input_path"]
        project_dir = inputs["project_dir"]
        incdirs = inputs["incdirs"]
        base_name = inputs["base_name"]

        out_dir = resolve_workspace_path(output_dir) if output_dir else get_project_report_dir(project_name)
        out_dir.mkdir(parents=True, exist_ok=True)
        resolved_oss = Path(oss_root).resolve() if oss_root else None
        results: dict = {"status": "success", "project": project_name, "files_processed": len(v_files)}

        if analysis_type in ("ast", "all") and parse_target:
            ast_output = str(out_dir / f"{base_name}_AST{'2' if simplified_ast else '1'}.json")
            try:
                ast_tree, meta, _ = parse_target(
                    input_path, incdirs=incdirs, defines=[], recursive=True,
                    oss_root=resolved_oss, simplified=simplified_ast,
                )
                ast_dict = node_to_dict(ast_tree, include_coord=False)
                if isinstance(ast_dict, dict) and isinstance(ast_dict.get("source_files"), list):
                    ast_dict["source_files"] = [
                        to_workspace_virtual_path(source)
                        for source in ast_dict["source_files"]
                    ]
                with open(ast_output, "w", encoding="utf-8") as f:
                    json.dump(ast_dict, f, ensure_ascii=False, indent=2)
                results["ast_json_file"] = to_workspace_virtual_path(ast_output)
                results["ast_mode"] = "dump_ast2(简化)" if simplified_ast else "dump_ast1(原始)"
            except Exception as exc:
                results["ast_error"] = f"{type(exc).__name__}: {exc}"

        if analysis_type in ("cfg", "all") and _prepare_temp_sources:
            cfg_output = str(out_dir / f"{base_name}_cfg_ddg_dfg.json")
            rtlil_output = str(out_dir / f"{base_name}_process.rtlil")
            dfg_output = str(out_dir / f"{base_name}_dfg.dot")
            temp_root = None
            try:
                temp_files, temp_incdirs, temp_root = _prepare_temp_sources(
                    v_files, incdirs, base_root=project_dir
                )
                run_yosys_for_rtlil_processes(
                    temp_files, incdirs=temp_incdirs, defines=[],
                    rtlil_out=rtlil_output, oss_root=resolved_oss, top=top,
                )
                rtlil_text = Path(rtlil_output).read_text(encoding="utf-8", errors="replace")
                graphs = build_cfg_ddg_from_rtlil_processes(
                    rtlil_text, dfg_dot_out=dfg_output, oss_root=resolved_oss,
                    top=top, dfg_effort=dfg_effort,
                )
                graphs["source_files"] = [
                    to_workspace_virtual_path(source) for source in v_files
                ]
                with open(cfg_output, "w", encoding="utf-8") as f:
                    json.dump(graphs, f, ensure_ascii=False, indent=2)
                results["cfg_ddg_file"] = to_workspace_virtual_path(cfg_output)
                results["rtlil_file"] = to_workspace_virtual_path(rtlil_output)
                results["top_module"] = top or "auto"
            except Exception as exc:
                results["cfg_error"] = f"{type(exc).__name__}: {exc}"
            finally:
                if temp_root:
                    shutil.rmtree(temp_root, ignore_errors=True)

        results["message"] = "\n".join(
            f"✅ {key}: {value}" for key, value in results.items() if key not in ("status", "message")
        )
        return results
