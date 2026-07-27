"""LangGraph assembly for work-unit lint root-cause analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from memory.long_term import AgentContext

from .nodes import WorkflowNodes, read_manifest
from .state import (
    MergeCandidateInput,
    MergeCandidateOutput,
    MergeCandidateState,
    WorkflowInput,
    WorkflowOutput,
    WorkflowState,
    WorkUnitInput,
    WorkUnitOutput,
    WorkUnitState,
)


def _route_after_work_unit(
    state: WorkUnitState,
) -> Literal["analyze_work_unit", "__end__"]:
    return END if state.get("work_unit_results") else "analyze_work_unit"


def _route_after_merge_candidate(
    state: MergeCandidateState,
) -> Literal["analyze_merge_candidate", "__end__"]:
    return (
        END
        if state.get("merge_candidate_results")
        else "analyze_merge_candidate"
    )


def _route_after_adjudication(
    state: WorkflowState,
) -> Literal["adjudicate_root_causes", "export_final_report"]:
    if state.get("adjudicated_map_path") and not state.get("validation_error"):
        return "export_final_report"
    return "adjudicate_root_causes"


def _build_work_unit_workflow(nodes: WorkflowNodes) -> Any:
    builder = StateGraph(
        WorkUnitState,
        context_schema=AgentContext,
        input_schema=WorkUnitInput,
        output_schema=WorkUnitOutput,
    )
    builder.add_node("analyze_work_unit", nodes.analyze_work_unit)
    builder.add_edge(START, "analyze_work_unit")
    builder.add_conditional_edges("analyze_work_unit", _route_after_work_unit)
    return builder.compile()


def _build_merge_candidate_workflow(nodes: WorkflowNodes) -> Any:
    builder = StateGraph(
        MergeCandidateState,
        context_schema=AgentContext,
        input_schema=MergeCandidateInput,
        output_schema=MergeCandidateOutput,
    )
    builder.add_node("analyze_merge_candidate", nodes.analyze_merge_candidate)
    builder.add_edge(START, "analyze_merge_candidate")
    builder.add_conditional_edges(
        "analyze_merge_candidate", _route_after_merge_candidate
    )
    return builder.compile()


def build_workflow(
    *,
    classifier_agent: Any,
    work_unit_agent: Any,
    merge_agent: Any,
    judge_agent: Any,
    work_unit_max_concurrency: int,
    ensemble_size: int,
) -> Any:
    """Build the fixed workflow with path-only shared state."""

    if work_unit_max_concurrency < 1:
        raise ValueError("work_unit_max_concurrency must be at least 1")
    if ensemble_size < 1:
        raise ValueError("ensemble_size must be at least 1")
    nodes = WorkflowNodes(
        classifier_agent=classifier_agent,
        work_unit_agent=work_unit_agent,
        merge_agent=merge_agent,
        judge_agent=judge_agent,
        ensemble_size=ensemble_size,
    )
    work_unit_workflow = _build_work_unit_workflow(nodes)
    merge_candidate_workflow = _build_merge_candidate_workflow(nodes)

    async def analyze_work_units(
        state: WorkflowState,
        config: RunnableConfig,
        runtime: Runtime[AgentContext],
    ) -> WorkUnitOutput:
        inputs: list[WorkUnitInput] = [
            {
                "unit_id": unit_id,
                "work_unit_dir": str(unit_dir),
            }
            for unit_id, unit_dir in read_manifest(
                Path(state["slices_dir"])
            ).work_units
        ]
        batch_config = dict(config)
        batch_config["max_concurrency"] = work_unit_max_concurrency
        outputs: list[WorkUnitOutput] = await work_unit_workflow.abatch(
            inputs,
            config=batch_config,
            context=runtime.context or AgentContext(),
        )
        return {
            "work_unit_results": [
                result
                for output in outputs
                for result in output["work_unit_results"]
            ]
        }

    async def analyze_merge_candidate(
        state: MergeCandidateInput,
        config: RunnableConfig,
        runtime: Runtime[AgentContext],
    ) -> MergeCandidateOutput:
        result = await merge_candidate_workflow.ainvoke(
            state,
            config=config,
            context=runtime.context or AgentContext(),
        )
        return {
            "merge_candidate_results": result["merge_candidate_results"]
        }

    def route_after_slicing(
        state: WorkflowState,
    ) -> Literal["classify_and_slice", "analyze_work_units"]:
        if not state.get("slices_dir"):
            return "classify_and_slice"
        return "analyze_work_units"

    def route_after_catalog(state: WorkflowState) -> list[Send]:
        ensemble_dir = Path(state["run_dir"]) / "work" / "ensemble"
        return [
            Send(
                "analyze_merge_candidate",
                {
                    "candidate_id": candidate_id,
                    "map_path": str(
                        ensemble_dir / f"global_map_{candidate_id:03d}.csv"
                    ),
                    "slices_dir": state["slices_dir"],
                    "local_catalog_path": state["local_catalog_path"],
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
    builder.add_node("analyze_work_units", analyze_work_units)
    builder.add_node("build_local_root_catalog", nodes.build_local_root_catalog)
    builder.add_node("analyze_merge_candidate", analyze_merge_candidate)
    builder.add_node("adjudicate_root_causes", nodes.adjudicate_root_causes)
    builder.add_node("export_final_report", nodes.export_final_report)

    builder.add_edge(START, "prepare_inputs")
    builder.add_edge("prepare_inputs", "classify_and_slice")
    builder.add_conditional_edges(
        "classify_and_slice",
        route_after_slicing,
        ["classify_and_slice", "analyze_work_units"],
    )
    builder.add_edge("analyze_work_units", "build_local_root_catalog")
    builder.add_conditional_edges(
        "build_local_root_catalog",
        route_after_catalog,
        ["analyze_merge_candidate"],
    )
    builder.add_edge("analyze_merge_candidate", "adjudicate_root_causes")
    builder.add_conditional_edges(
        "adjudicate_root_causes", _route_after_adjudication
    )
    builder.add_edge("export_final_report", END)
    return builder.compile()
