"""Standalone ALINT-PRO MCP tools."""

from __future__ import annotations

from typing import Optional

from eda.alint import run_alint_batch
from mcp_server.pathing import to_workspace_virtual_path


def register_lint_tools(mcp) -> None:
    """Register ALINT execution tools."""

    @mcp.tool()
    async def run_alint_analysis(
        workspace_path: str, project_name: str, output_name: Optional[str] = None
    ) -> str:
        """以批处理模式运行 ALINT-PRO 并生成 CSV 报告（仅限 Windows）"""
        result = await run_alint_batch(
            workspace_path,
            project_name,
            output_name=output_name,
        )
        if result.success and result.csv_path is not None:
            return (
                f"ALINT analysis completed successfully!\n"
                f"Report: {to_workspace_virtual_path(result.csv_path)}\n"
                f"Stdout:\n{result.stdout}\nStderr:\n{result.stderr}"
            )

        if result.returncode is None:
            return f"Error: {result.error}"
        return result.error or "ALINT analysis failed."
