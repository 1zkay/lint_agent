from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from agent_runtime.configuration import (
    build_runtime_config_for_llm_preset,
)
from agent_runtime.middleware import create_lint_deep_agent
from agent_runtime.prompts import SYSTEM_PROMPT
from agent_runtime.root_cause import build_root_cause_workflow_tool
from agent_runtime.tools import load_agent_tools
from compat.langgraph import (
    apply_dev_persistence_pickle_sanitization,
)
from config import config
from llm.factory import build_chat_model_from_config
from memory.long_term import AgentContext

try:
    from langgraph_sdk.runtime import ServerRuntime
except Exception:  # pragma: no cover - imported only by LangGraph Agent Server.
    ServerRuntime = Any  # type: ignore


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PROJECT_ROOT.parent

def _bool_env(key: str, default: bool, *, legacy_key: str | None = None) -> bool:
    raw = os.getenv(key)
    if raw is None and legacy_key is not None:
        raw = os.getenv(legacy_key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


if _bool_env(
    "LINT_AGENT_PATCH_LANGGRAPH_DEV_PERSISTENCE",
    True,
    legacy_key="MCP_ALINT_PATCH_LANGGRAPH_DEV_PERSISTENCE",
):
    apply_dev_persistence_pickle_sanitization(log_prefix="[agent_runtime]")
def build_llm_for_runtime_config(
    runtime_cfg: Any, *, temperature: float | None = None
) -> Any:
    if not runtime_cfg.llm_model:
        raise RuntimeError("LLM_MODEL is empty. Configure .env before starting LangGraph Agent Server.")
    return build_chat_model_from_config(
        runtime_cfg,
        temperature=temperature,
        logger=logger,
        log_prefix="[agent_runtime]",
    )


def build_agent_context(
    *,
    thread_id: str | None = None,
    user_id: str | None = None,
    authenticated: bool = False,
) -> AgentContext:
    resolved_thread_id = str(thread_id or uuid.uuid4())
    resolved_user_id = str(user_id or f"anonymous:{resolved_thread_id}")
    return AgentContext(
        user_id=resolved_user_id,
        thread_id=resolved_thread_id,
        authenticated=authenticated,
    )


@asynccontextmanager
async def lint_agent_graph(runtime: ServerRuntime | None = None) -> AsyncIterator[Any]:
    """LangGraph Agent Server factory for the complete ALINT intelligent agent."""

    config.validate()
    runtime_cfg = build_runtime_config_for_llm_preset()
    llm = build_llm_for_runtime_config(runtime_cfg)
    candidate_llm = build_llm_for_runtime_config(
        runtime_cfg,
        temperature=runtime_cfg.lint_root_cause_candidate_temperature,
    )
    judge_llm = build_llm_for_runtime_config(
        runtime_cfg,
        temperature=runtime_cfg.lint_root_cause_judge_temperature,
    )
    store = getattr(runtime, "store", None) if runtime is not None else None
    is_execution = runtime is None or runtime.execution_runtime is not None

    async with AsyncExitStack() as exit_stack:
        if is_execution:
            loaded_tools = await load_agent_tools(
                exit_stack,
                log_prefix="[agent_runtime]",
            )
            base_tools = loaded_tools.tools
            tool_names = loaded_tools.tool_names
        else:
            base_tools = []

        root_cause_tool = build_root_cause_workflow_tool(
            llm,
            base_tools,
            candidate_llm=candidate_llm,
            judge_llm=judge_llm,
            analysis_batch_max_concurrency=(
                runtime_cfg.lint_root_cause_analysis_batch_max_concurrency
            ),
            ensemble_size=runtime_cfg.lint_root_cause_ensemble_size,
            root_dir=REPO_ROOT,
            log_prefix="[agent_runtime:lint_root_cause]",
        )
        tools = [*base_tools, root_cause_tool]

        agent, _, runtime_tool_names = create_lint_deep_agent(
            llm,
            tools,
            root_dir=REPO_ROOT,
            log_prefix="[agent_runtime]",
            system_prompt=SYSTEM_PROMPT,
            store=store,
            context_schema=AgentContext,
            tool_retry_tools=base_tools,
        )
        if is_execution:
            tool_names = list(
                dict.fromkeys(
                    [*tool_names, root_cause_tool.name, *runtime_tool_names]
                )
            )
            logger.info("[agent_runtime] Available agent tools: %s", tool_names)

        yield agent


async def ainvoke_once(
    message: str,
    *,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> Any:
    """Direct Python entry point for smoke tests and local scripts."""

    context = build_agent_context(thread_id=thread_id, user_id=user_id)
    async with lint_agent_graph() as graph:
        return await graph.ainvoke(
            {"messages": [{"role": "user", "content": message}]},
            context=context,
            config={"recursion_limit": config.agent_recursion_limit},
        )


def invoke_once(message: str, *, thread_id: str | None = None, user_id: str | None = None) -> Any:
    return asyncio.run(ainvoke_once(message, thread_id=thread_id, user_id=user_id))
