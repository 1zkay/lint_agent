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
    AnalysisBatchInput,
    AnalysisBatchOutput,
    AnalysisBatchState,
    MergeCandidateInput,
    MergeCandidateOutput,
    MergeCandidateState,
    WorkflowInput,
    WorkflowOutput,
    WorkflowState,
)


def _route_after_analysis_batch(
    state: AnalysisBatchState,
) -> Literal["analyze_batch", "__end__"]:
    return END if state.get("work_unit_results") else "analyze_batch"


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


def _build_analysis_batch_workflow(nodes: WorkflowNodes) -> Any:
    builder = StateGraph(
        AnalysisBatchState,
        context_schema=AgentContext,
        input_schema=AnalysisBatchInput,
        output_schema=AnalysisBatchOutput,
    )
    builder.add_node("analyze_batch", nodes.analyze_batch)
    builder.add_edge(START, "analyze_batch")
    builder.add_conditional_edges("analyze_batch", _route_after_analysis_batch)
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
    analysis_batch_agent: Any,
    merge_agent: Any,
    judge_agent: Any,
    analysis_batch_max_concurrency: int,
    ensemble_size: int,
) -> Any:
    """Build the fixed workflow with path-only shared state."""

    if analysis_batch_max_concurrency < 1:
        raise ValueError("analysis_batch_max_concurrency must be at least 1")
    if ensemble_size < 1:
        raise ValueError("ensemble_size must be at least 1")
    nodes = WorkflowNodes(
        classifier_agent=classifier_agent,
        analysis_batch_agent=analysis_batch_agent,
        merge_agent=merge_agent,
        judge_agent=judge_agent,
        ensemble_size=ensemble_size,
    )
    analysis_batch_workflow = _build_analysis_batch_workflow(nodes)
    merge_candidate_workflow = _build_merge_candidate_workflow(nodes)

    async def analyze_batches(
        state: WorkflowState,
        config: RunnableConfig,
        runtime: Runtime[AgentContext],
    ) -> AnalysisBatchOutput:
        inputs: list[AnalysisBatchInput] = [
            {
                "work_units": [
                    {"unit_id": unit_id, "work_unit_dir": str(unit_dir)}
                    for unit_id, unit_dir in batch
                ],
            }
            for batch in read_manifest(
                Path(state["slices_dir"])
            ).analysis_batches
        ]
        batch_config = dict(config)
        batch_config["max_concurrency"] = analysis_batch_max_concurrency
        outputs: list[AnalysisBatchOutput] = await analysis_batch_workflow.abatch(
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
    ) -> Literal["classify_and_slice", "analyze_batches"]:
        if not state.get("slices_dir"):
            return "classify_and_slice"
        return "analyze_batches"

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
    builder.add_node("analyze_batches", analyze_batches)
    builder.add_node("build_local_root_catalog", nodes.build_local_root_catalog)
    builder.add_node("analyze_merge_candidate", analyze_merge_candidate)
    builder.add_node("adjudicate_root_causes", nodes.adjudicate_root_causes)
    builder.add_node("export_final_report", nodes.export_final_report)

    builder.add_edge(START, "prepare_inputs")
    builder.add_edge("prepare_inputs", "classify_and_slice")
    builder.add_conditional_edges(
        "classify_and_slice",
        route_after_slicing,
        ["classify_and_slice", "analyze_batches"],
    )
    builder.add_edge("analyze_batches", "build_local_root_catalog")
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
