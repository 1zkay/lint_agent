"""LangGraph workflow MCP tool."""

from __future__ import annotations

import logging

from alint_workflow.graph import get_workflow
from mcp_server.pathing import to_workspace_virtual_path

logger = logging.getLogger(__name__)


def register_workflow_tools(mcp) -> None:
    """Register workflow orchestration tools."""

    @mcp.tool()
    async def generate_basic_analysis_workflow(
        workspace_path: str, project_name: str
    ) -> dict:
        """
        执行简化的 ALINT 分析工作流（LangGraph 版）：
          1. 运行 ALINT-PRO 生成原始 lint 报告
          2. 用 Yosys 生成 AST / CFG / DDG / DFG
          3. 提取项目源代码（带行号）
          4. 整理所有产物到 reports/_prepared/<session_id>/

        输入:
            workspace_path: .alintws 工作区文件路径
            project_name:   项目名称

        输出:
            prepared_directory 及各文件路径，供后续 LLM 分析使用。
        """
        workflow = get_workflow()
        initial_state = {
            "workspace_path": workspace_path,
            "project_name": project_name,
            "raw_csv_path": None,
            "alint_output_dir": None,
            "ast_json_file": None,
            "cfg_ddg_file": None,
            "rtlil_file": None,
            "v_files": None,
            "project_dir": None,
            "source_code_text": None,
            "prepared_directory": None,
            "prepared_sources": None,
            "prepared_violations": None,
            "prepared_ast": None,
            "prepared_cfg_ddg_dfg": None,
            "prepared_kb": None,
            "session_id": None,
            "error": None,
        }

        try:
            final_state = await workflow.ainvoke(initial_state)
        except Exception as exc:
            logger.exception("Workflow invocation failed")
            return {"status": "error", "message": str(exc)}

        if final_state.get("error"):
            return {"status": "error", "message": final_state["error"]}

        prepared_dir = final_state.get("prepared_directory", "")
        v_files = final_state.get("v_files") or []
        session_id = final_state.get("session_id", "")
        prepared_dir_virtual = to_workspace_virtual_path(prepared_dir) or ""
        prepared_files = {
            "sources": to_workspace_virtual_path(final_state.get("prepared_sources")),
            "violations": to_workspace_virtual_path(final_state.get("prepared_violations")),
            "ast": to_workspace_virtual_path(final_state.get("prepared_ast")),
            "cfg_ddg_dfg": to_workspace_virtual_path(final_state.get("prepared_cfg_ddg_dfg")),
            "kb": to_workspace_virtual_path(final_state.get("prepared_kb")),
        }

        return {
            "status": "success",
            "message": (
                f"✅ 工作流执行成功！资源文件已准备完毕。\n\n"
                f"📁 准备目录: {prepared_dir_virtual}\n\n"
                f"包含文件:\n"
                f"  • 1_project_sources.txt（源代码，{len(v_files)} 个文件）\n"
                f"  • 2_alint_violations.csv（ALINT 违规报告）\n"
                f"  • 3_ast.json（AST 分析结果）\n"
                f"  • 4_cfg_ddg_dfg.json（CFG/DDG/DFG 分析结果）\n"
                f"  • 5_verilog_kb.jsonl（Verilog 设计规范知识库）\n\n"
                f"💡 这些文件可直接用于 LLM 分析。"
            ),
            "prepared_directory": prepared_dir_virtual,
            "files": prepared_files,
            "statistics": {
                "source_files": len(v_files),
                "project_name": project_name,
                "session_id": session_id,
            },
        }

