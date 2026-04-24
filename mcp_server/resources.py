"""MCP resources for prepared ALINT analysis artifacts."""

from __future__ import annotations

from workspace.project_utils import get_latest_prepared_dir


def register_resources(mcp) -> None:
    """Register read-only prepared artifact resources."""

    @mcp.resource(
        uri="alint://basic/sources",
        description="项目源代码(完整上下文) - 基础分析",
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    def read_basic_sources() -> str:
        path = get_latest_prepared_dir() / "1_project_sources.txt"
        if not path.exists():
            raise FileNotFoundError(f"源代码文件不存在: {path}")
        return path.read_text(encoding="utf-8", errors="ignore")

    @mcp.resource(
        uri="alint://basic/violations",
        description="ALINT违规报告(CSV格式) - 基础分析",
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    def read_basic_violations() -> str:
        path = get_latest_prepared_dir() / "2_alint_violations.csv"
        if not path.exists():
            raise FileNotFoundError(f"违规文件不存在: {path}")
        return path.read_text(encoding="utf-8", errors="ignore")

    @mcp.resource(
        uri="alint://basic/ast",
        description="AST抽象语法树分析结果(JSON格式) - 基础分析",
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    def read_basic_ast() -> str:
        path = get_latest_prepared_dir() / "3_ast.json"
        if not path.exists():
            raise FileNotFoundError(f"AST文件不存在: {path}")
        return path.read_text(encoding="utf-8", errors="ignore")

    @mcp.resource(
        uri="alint://basic/cfg_ddg_dfg",
        description="CFG/DDG/DFG控制流和数据流分析结果(JSON格式) - 基础分析",
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    def read_basic_cfg_ddg_dfg() -> str:
        path = get_latest_prepared_dir() / "4_cfg_ddg_dfg.json"
        if not path.exists():
            raise FileNotFoundError(f"CFG/DDG/DFG文件不存在: {path}")
        return path.read_text(encoding="utf-8", errors="ignore")

    @mcp.resource(
        uri="alint://basic/kb",
        description="Verilog设计规范知识库(JSONL格式) - 基础分析",
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    def read_basic_kb() -> str:
        path = get_latest_prepared_dir() / "5_verilog_kb.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"知识库文件不存在: {path}")
        return path.read_text(encoding="utf-8", errors="ignore")
