"""
Node 1: 运行 ALINT-PRO 分析，生成原始 CSV 报告
"""
import logging

from ..state import AlintWorkflowState
from eda.alint import run_alint_batch

logger = logging.getLogger(__name__)


async def run_lint_node(state: AlintWorkflowState) -> dict:
    """
    LangGraph 节点：运行 ALINT-PRO 并生成 CSV 报告

    输入 state 字段: workspace_path, project_name
    输出 state 字段: raw_csv_path, alint_output_dir  （或 error）
    """
    workspace_path: str = state["workspace_path"]
    project_name: str = state["project_name"]

    result = await run_alint_batch(
        workspace_path,
        project_name,
        search_missing_workspace=True,
        logger=logger,
    )
    if result.success and result.csv_path is not None and result.output_dir is not None:
        logger.info("[lint_node] Report generated: %s", result.csv_path)
        return {
            "raw_csv_path": str(result.csv_path),
            "alint_output_dir": str(result.output_dir),
        }
    return {"error": result.error or "ALINT analysis failed."}
