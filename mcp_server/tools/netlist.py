"""Yosys netlist export MCP tools."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from workspace.project_utils import get_project_report_dir, resolve_project_verilog_inputs
from mcp_server.eda_backend import (
    AST_AVAILABLE,
    _prepare_temp_sources,
    run_yosys_for_netlist,
)
from mcp_server.pathing import resolve_workspace_path, to_workspace_virtual_path


def register_netlist_tools(mcp) -> None:
    """Register netlist export tools."""

    @mcp.tool()
    async def export_verilog_netlist(
        workspace_path: str,
        project_name: str,
        verilog_output: Optional[str] = None,
        json_output: Optional[str] = None,
        top: Optional[str] = None,
        oss_root: Optional[str] = None,
    ) -> dict:
        """使用 Yosys 导出门级网表 Verilog/JSON。"""
        if not AST_AVAILABLE or not run_yosys_for_netlist:
            return {"error": "AST module not available."}

        inputs, err = resolve_project_verilog_inputs(workspace_path, project_name)
        if err:
            return {"error": err}

        v_files = inputs["v_files"]
        project_dir = inputs["project_dir"]
        incdirs = inputs["incdirs"]
        base_name = inputs["base_name"]
        out_dir = get_project_report_dir(project_name)

        verilog_output = (
            str(resolve_workspace_path(verilog_output))
            if verilog_output
            else str(out_dir / f"{base_name}_synth.v")
        )
        json_output = (
            str(resolve_workspace_path(json_output))
            if json_output
            else str(out_dir / f"{base_name}_netlist.json")
        )
        resolved_oss = Path(oss_root).resolve() if oss_root else None

        temp_root = None
        try:
            temp_files, temp_incdirs, temp_root = _prepare_temp_sources(
                v_files, incdirs, base_root=project_dir
            )
            run_yosys_for_netlist(
                temp_files, incdirs=temp_incdirs, defines=[],
                verilog_out=verilog_output, json_out=json_output,
                oss_root=resolved_oss, top=top,
            )
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            if temp_root:
                shutil.rmtree(temp_root, ignore_errors=True)

        return {
            "status": "success",
            "project": project_name,
            "verilog_netlist": to_workspace_virtual_path(verilog_output),
            "json_netlist": to_workspace_virtual_path(json_output),
            "files_processed": len(v_files),
            "message": (
                "✅ 网表已生成:\n"
                f"Verilog: {to_workspace_virtual_path(verilog_output)}\n"
                f"JSON: {to_workspace_virtual_path(json_output)}"
            ),
        }
