"""Three-stage LangGraph assembly for Verilog lint root-cause analysis."""

from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from memory.long_term import AgentContext

from .nodes import WorkflowNodes
from .state import WorkflowInput, WorkflowOutput, WorkflowState


def _route_after_slicing(
    state: WorkflowState,
) -> Literal["classify_and_slice", "analyze_root_causes"]:
    return "analyze_root_causes" if state.get("slices_dir") else "classify_and_slice"


def _route_after_analysis(
    state: WorkflowState,
) -> Literal["analyze_root_causes", "__end__"]:
    if state.get("report_path") and not state.get("validation_error"):
        return END
    return "analyze_root_causes"


def build_workflow(*, classifier_agent: Any, root_cause_agent: Any) -> Any:
    """Build the fixed three-stage workflow with path-only parent state."""

    nodes = WorkflowNodes(
        classifier_agent=classifier_agent,
        root_cause_agent=root_cause_agent,
    )
    builder = StateGraph(
        WorkflowState,
        context_schema=AgentContext,
        input_schema=WorkflowInput,
        output_schema=WorkflowOutput,
    )
    builder.add_node("prepare_inputs", nodes.prepare_inputs)
    builder.add_node("classify_and_slice", nodes.classify_and_slice)
    builder.add_node("analyze_root_causes", nodes.analyze_root_causes)

    builder.add_edge(START, "prepare_inputs")
    builder.add_edge("prepare_inputs", "classify_and_slice")
    builder.add_conditional_edges("classify_and_slice", _route_after_slicing)
    builder.add_conditional_edges("analyze_root_causes", _route_after_analysis)
    return builder.compile()
