"""MCP tool registration package."""

from .conversion import register_conversion_tools
from .feedback import register_feedback_tools
from .lint import register_lint_tools
from .netlist import register_netlist_tools
from .structure import register_structure_tools
from .workflow import register_workflow_tools


def register_tools(mcp) -> None:
    """Register all ALINT MCP tools."""
    register_workflow_tools(mcp)
    register_lint_tools(mcp)
    register_structure_tools(mcp)
    register_netlist_tools(mcp)
    register_conversion_tools(mcp)
    register_feedback_tools(mcp)
