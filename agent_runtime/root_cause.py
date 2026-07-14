"""Native-tool adapter for the lint root-cause workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool

from agent_runtime.middleware import create_lint_deep_agent
from lint_root_cause_workflow import build_workflow
from lint_root_cause_workflow.prompts import (
    CLASSIFIER_SYSTEM_PROMPT,
    ROOT_CAUSE_SYSTEM_PROMPT,
)
from memory.long_term import AgentContext


ROOT_CAUSE_WORKFLOW_TOOL_NAME = "run_lint_root_cause_workflow"


def build_root_cause_workflow(
    llm: Any,
    base_tools: list[Any],
    *,
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
    root_cause_agent, _, _ = create_lint_deep_agent(
        llm,
        base_tools,
        root_dir=root_dir,
        log_prefix=f"{log_prefix}:analyzer",
        system_prompt=ROOT_CAUSE_SYSTEM_PROMPT,
        store=store,
        context_schema=AgentContext,
        name="lint_root_cause_analyzer",
        tool_retry_tools=base_tools,
        model_retry_on_failure="error",
    )
    return build_workflow(
        classifier_agent=classifier_agent,
        root_cause_agent=root_cause_agent,
    )


def build_root_cause_workflow_tool(
    llm: Any,
    base_tools: list[Any],
    *,
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
    )
    async def run_lint_root_cause_workflow(
        source_path: str,
        lint_csv_path: str,
        runtime: ToolRuntime[AgentContext],
    ) -> dict[str, str]:
        workflow = build_root_cause_workflow(
            llm,
            base_tools,
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
        return {"report_path": str(result["report_path"])}

    return run_lint_root_cause_workflow
