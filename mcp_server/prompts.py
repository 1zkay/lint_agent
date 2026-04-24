"""MCP prompt registrations."""

from __future__ import annotations

from prompts.templates import (
    get_basic_hardware_analysis_messages,
    get_structured_report_messages,
)


def register_prompts(mcp) -> None:
    """Register ALINT analysis prompts."""

    @mcp.prompt()
    def basic_hardware_analysis():
        """
        基础硬件代码质量综合分析（JSON 输出）

        自动引用：sources / violations / ast / cfg_ddg_dfg / kb
        """
        return get_basic_hardware_analysis_messages()

    @mcp.prompt()
    def structured_hardware_analysis_report():
        """
        结构化硬件代码分析报告（Markdown 输出）

        自动引用：sources / violations / ast / cfg_ddg_dfg / kb
        """
        return get_structured_report_messages()

