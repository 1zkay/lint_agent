"""
LangGraph StateGraph：ALINT 基础分析工作流

节点图：
    START
      │
   [lint_node]  ── error ──► END
      │
   [structure_node]  ── error ──► END
      │
   [sources_node]  ── error ──► END
      │
   [organize_node]
      │
     END

每个节点写入 error 字段时，条件边路由到 END，
调用方通过检查 state["error"] 判断是否成功。
"""
import logging
from typing import Literal

from langgraph.graph import StateGraph, START, END

from .state import AlintWorkflowState
from .nodes.lint_node import run_lint_node
from .nodes.structure_node import run_structure_node
from .nodes.sources_node import run_sources_node
from .nodes.organize_node import run_organize_node

logger = logging.getLogger(__name__)


# ─── 条件边函数 ──────────────────────────────────────────────────────────────

def _route_after_lint(state: AlintWorkflowState) -> Literal["structure_node", "__end__"]:
    """Step1 失败则短路到 END"""
    if state.get("error"):
        logger.error(f"[graph] lint_node failed: {state['error']}")
        return END
    return "structure_node"


def _route_after_structure(state: AlintWorkflowState) -> Literal["sources_node", "__end__"]:
    """Step2 失败（源码也无法提取）则短路"""
    # structure 失败通常是非致命的（AST/CFG 可选），只有 v_files 缺失才停止
    if not state.get("v_files"):
        logger.error("[graph] structure_node failed to resolve v_files")
        return END
    return "sources_node"


def _route_after_sources(state: AlintWorkflowState) -> Literal["organize_node", "__end__"]:
    """Step3 失败则短路"""
    if state.get("error"):
        logger.error(f"[graph] sources_node failed: {state['error']}")
        return END
    return "organize_node"


# ─── 构建图 ──────────────────────────────────────────────────────────────────

def build_workflow() -> StateGraph:
    """
    构建并编译 ALINT 分析 StateGraph。

    Returns:
        compiled LangGraph CompiledStateGraph
    """
    builder = StateGraph(AlintWorkflowState)

    # 注册节点
    builder.add_node("lint_node", run_lint_node)
    builder.add_node("structure_node", run_structure_node)
    builder.add_node("sources_node", run_sources_node)
    builder.add_node("organize_node", run_organize_node)

    # 入口边
    builder.add_edge(START, "lint_node")

    # 条件边：失败时短路退出
    builder.add_conditional_edges(
        "lint_node",
        _route_after_lint,
        {"structure_node": "structure_node", END: END},
    )
    builder.add_conditional_edges(
        "structure_node",
        _route_after_structure,
        {"sources_node": "sources_node", END: END},
    )
    builder.add_conditional_edges(
        "sources_node",
        _route_after_sources,
        {"organize_node": "organize_node", END: END},
    )

    # organize → END
    builder.add_edge("organize_node", END)

    return builder.compile()


# 模块级单例（懒加载）
_workflow = None


def get_workflow():
    """获取编译好的工作流单例"""
    global _workflow
    if _workflow is None:
        _workflow = build_workflow()
        logger.info("[graph] ALINT workflow compiled successfully")
    return _workflow
