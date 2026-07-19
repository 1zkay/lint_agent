"""Three-stage LangGraph assembly for Verilog lint root-cause analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from memory.long_term import AgentContext

from .nodes import WorkflowNodes
from .state import (
    CandidateWorkflowInput,
    CandidateWorkflowOutput,
    CandidateWorkflowState,
    WorkflowInput,
    WorkflowOutput,
    WorkflowState,
)


def _route_after_candidate(
    state: CandidateWorkflowState,
) -> Literal["analyze_candidate", "__end__"]:
    return END if state.get("candidate_reports") else "analyze_candidate"


def _route_after_adjudication(
    state: WorkflowState,
) -> Literal["adjudicate_root_causes", "__end__"]:
    if state.get("report_path") and not state.get("validation_error"):
        return END
    return "adjudicate_root_causes"


def _build_candidate_workflow(nodes: WorkflowNodes) -> Any:
    builder = StateGraph(
        CandidateWorkflowState,
        context_schema=AgentContext,
        input_schema=CandidateWorkflowInput,
        output_schema=CandidateWorkflowOutput,
    )
    builder.add_node("analyze_candidate", nodes.analyze_candidate)
    builder.add_edge(START, "analyze_candidate")
    builder.add_conditional_edges("analyze_candidate", _route_after_candidate)
    return builder.compile()


def build_workflow(
    *,
    classifier_agent: Any,
    candidate_agent: Any,
    judge_agent: Any,
    ensemble_size: int,
) -> Any:
    """Build the fixed three-stage workflow with path-only parent state."""

    if ensemble_size < 1:
        raise ValueError("ensemble_size must be at least 1")
    nodes = WorkflowNodes(
        classifier_agent=classifier_agent,
        candidate_agent=candidate_agent,
        judge_agent=judge_agent,
        ensemble_size=ensemble_size,
    )
    candidate_workflow = _build_candidate_workflow(nodes)

    async def analyze_candidate(
        state: CandidateWorkflowInput,
        config: RunnableConfig,
        runtime: Runtime[AgentContext],
    ) -> CandidateWorkflowOutput:
        result = await candidate_workflow.ainvoke(
            state,
            config=config,
            context=runtime.context or AgentContext(),
        )
        return {"candidate_reports": result["candidate_reports"]}

    def route_after_slicing(
        state: WorkflowState,
    ) -> Literal["classify_and_slice"] | list[Send]:
        if not state.get("slices_dir"):
            return "classify_and_slice"

        ensemble_dir = Path(state["run_dir"]) / "work" / "ensemble"
        return [
            Send(
                "analyze_candidate",
                {
                    "candidate_id": candidate_id,
                    "candidate_draft_csv": str(
                        ensemble_dir / f"candidate_{candidate_id:03d}.csv"
                    ),
                    "rtl_dir": state["rtl_dir"],
                    "filelist_path": state["filelist_path"],
                    "slices_dir": state["slices_dir"],
                },
            )
            for candidate_id in range(ensemble_size)
        ]

    builder = StateGraph(
        WorkflowState,
        context_schema=AgentContext,
        input_schema=WorkflowInput,
        output_schema=WorkflowOutput,
    )
    builder.add_node("prepare_inputs", nodes.prepare_inputs)
    builder.add_node("classify_and_slice", nodes.classify_and_slice)
    builder.add_node("analyze_candidate", analyze_candidate)
    builder.add_node("adjudicate_root_causes", nodes.adjudicate_root_causes)

    builder.add_edge(START, "prepare_inputs")
    builder.add_edge("prepare_inputs", "classify_and_slice")
    builder.add_conditional_edges(
        "classify_and_slice",
        route_after_slicing,
        ["classify_and_slice", "analyze_candidate"],
    )
    builder.add_edge("analyze_candidate", "adjudicate_root_causes")
    builder.add_conditional_edges(
        "adjudicate_root_causes", _route_after_adjudication
    )
    return builder.compile()
