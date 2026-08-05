"""Native-tool adapter for the lint root-cause workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool

from agent_runtime.contracts import ROOT_CAUSE_WORKFLOW_TOOL_NAME
from agent_runtime.middleware import create_lint_deep_agent
from lint_root_cause_workflow import build_workflow
from lint_root_cause_workflow.prompts import (
    CLASSIFIER_SYSTEM_PROMPT,
    ROOT_CAUSE_ANALYSIS_BATCH_SYSTEM_PROMPT,
    ROOT_CAUSE_GLOBAL_MERGE_SYSTEM_PROMPT,
    ROOT_CAUSE_JUDGE_SYSTEM_PROMPT,
)
from memory.long_term import AgentContext


def build_root_cause_workflow(
    llm: Any,
    base_tools: list[Any],
    *,
    candidate_llm: Any,
    judge_llm: Any,
    analysis_batch_max_concurrency: int,
    ensemble_size: int,
    root_dir: str | Path,
    log_prefix: str,
    store: Any | None = None,
) -> Any:
    """Build the compiled workflow with agents limited to the base tool set."""

    classifier_agent, _, _ = create_lint_deep_agent(
        llm,
        base_tools,
        root_dir=root_dir,
        log_prefix=f"{log_prefix}:classifier",
        system_prompt=CLASSIFIER_SYSTEM_PROMPT,
        store=store,
        context_schema=AgentContext,
        name="lint_slice_classifier",
        tool_retry_tools=base_tools,
        model_retry_on_failure="error",
    )
    analysis_batch_agent, _, _ = create_lint_deep_agent(
        llm,
        base_tools,
        root_dir=root_dir,
        log_prefix=f"{log_prefix}:analysis_batch",
        system_prompt=ROOT_CAUSE_ANALYSIS_BATCH_SYSTEM_PROMPT,
        store=store,
        context_schema=AgentContext,
        name="lint_root_cause_analysis_batch",
        tool_retry_tools=base_tools,
        model_retry_on_failure="error",
    )
    merge_agent, _, _ = create_lint_deep_agent(
        candidate_llm,
        base_tools,
        root_dir=root_dir,
        log_prefix=f"{log_prefix}:global_merge",
        system_prompt=ROOT_CAUSE_GLOBAL_MERGE_SYSTEM_PROMPT,
        store=store,
        context_schema=AgentContext,
        name="lint_root_cause_global_merge",
        tool_retry_tools=base_tools,
        model_retry_on_failure="error",
    )
    judge_agent, _, _ = create_lint_deep_agent(
        judge_llm,
        base_tools,
        root_dir=root_dir,
        log_prefix=f"{log_prefix}:judge",
        system_prompt=ROOT_CAUSE_JUDGE_SYSTEM_PROMPT,
        store=store,
        context_schema=AgentContext,
        name="lint_root_cause_judge",
        tool_retry_tools=base_tools,
        model_retry_on_failure="error",
    )
    return build_workflow(
        classifier_agent=classifier_agent,
        analysis_batch_agent=analysis_batch_agent,
        merge_agent=merge_agent,
        judge_agent=judge_agent,
        analysis_batch_max_concurrency=analysis_batch_max_concurrency,
        ensemble_size=ensemble_size,
    )


def build_root_cause_workflow_tool(
    llm: Any,
    base_tools: list[Any],
    *,
    candidate_llm: Any,
    judge_llm: Any,
    analysis_batch_max_concurrency: int,
    ensemble_size: int,
    root_dir: str | Path,
    log_prefix: str,
) -> BaseTool:
    """Return a native tool that builds the workflow only when invoked."""

    @tool(
        ROOT_CAUSE_WORKFLOW_TOOL_NAME,
        description=(
            "Run the complete Verilog lint root-cause workflow when the user "
            "provides a source archive or source directory and its corresponding "
            "lint CSV. Use it for full report generation, not for general questions."
        ),
        response_format="content_and_artifact",
    )
    async def run_lint_root_cause_workflow(
        source_path: str,
        lint_csv_path: str,
        runtime: ToolRuntime[AgentContext],
    ) -> tuple[dict[str, str], dict[str, str]]:
        workflow = build_root_cause_workflow(
            llm,
            base_tools,
            candidate_llm=candidate_llm,
            judge_llm=judge_llm,
            analysis_batch_max_concurrency=analysis_batch_max_concurrency,
            ensemble_size=ensemble_size,
            root_dir=root_dir,
            log_prefix=log_prefix,
            store=runtime.store,
        )
        result = await workflow.ainvoke(
            {
                "source_path": source_path,
                "lint_csv_path": lint_csv_path,
            },
            config=runtime.config,
            context=runtime.context or AgentContext(),
        )
        report = {"report_path": str(result["report_path"])}
        return report, report

    return run_lint_root_cause_workflow
