from __future__ import annotations

import asyncio
import atexit
import logging
import os
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from langchain.agents import create_agent

from agent_runtime.configuration import (
    build_runtime_config_for_llm_preset,
)
from agent_runtime.middleware import build_agent_middleware
from agent_runtime.prompts import SYSTEM_PROMPT
from agent_runtime.tools import load_agent_tools
from compat.langgraph import (
    apply_dev_persistence_pickle_sanitization,
    apply_recursive_send_sanitization,
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
_RUNTIME_COMPONENTS_LOCK: asyncio.Lock | None = None
_RUNTIME_COMPONENTS: dict[str, Any] | None = None

def _bool_env(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


if _bool_env("MCP_ALINT_PATCH_LANGGRAPH_DEV_PERSISTENCE", True):
    apply_dev_persistence_pickle_sanitization(log_prefix="[agent_runtime]")
if _bool_env("MCP_ALINT_PATCH_LANGGRAPH_SEND", True):
    apply_recursive_send_sanitization(
        log_prefix="[agent_runtime]",
        drop_unpickleable=True,
    )


def build_llm_for_runtime_config(runtime_cfg: Any) -> Any:
    if not runtime_cfg.llm_model:
        raise RuntimeError("LLM_MODEL is empty. Configure .env before starting LangGraph Agent Server.")
    return build_chat_model_from_config(
        runtime_cfg,
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


def _get_runtime_components_lock() -> asyncio.Lock:
    global _RUNTIME_COMPONENTS_LOCK

    if _RUNTIME_COMPONENTS_LOCK is None:
        _RUNTIME_COMPONENTS_LOCK = asyncio.Lock()
    return _RUNTIME_COMPONENTS_LOCK


async def _build_cached_runtime_components() -> dict[str, Any]:
    config.validate()
    runtime_cfg = build_runtime_config_for_llm_preset()
    llm = build_llm_for_runtime_config(runtime_cfg)
    exit_stack = AsyncExitStack()
    try:
        loaded_tools = await load_agent_tools(
            exit_stack,
            log_prefix="[agent_runtime]",
        )
    except Exception:
        await exit_stack.aclose()
        raise

    return {
        "exit_stack": exit_stack,
        "llm": llm,
        "tools": loaded_tools.tools,
    }


async def _get_cached_runtime_components() -> dict[str, Any]:
    global _RUNTIME_COMPONENTS

    if _RUNTIME_COMPONENTS is not None:
        return _RUNTIME_COMPONENTS

    async with _get_runtime_components_lock():
        if _RUNTIME_COMPONENTS is None:
            logger.info("[agent_runtime] Initializing cached runtime components.")
            _RUNTIME_COMPONENTS = await _build_cached_runtime_components()
        return _RUNTIME_COMPONENTS


async def _close_cached_runtime_components() -> None:
    global _RUNTIME_COMPONENTS

    components = _RUNTIME_COMPONENTS
    _RUNTIME_COMPONENTS = None
    if components is not None:
        await components["exit_stack"].aclose()


def _close_cached_runtime_components_at_exit() -> None:
    try:
        asyncio.run(_close_cached_runtime_components())
    except Exception:
        logger.exception("[agent_runtime] Failed to close cached runtime components.")


atexit.register(_close_cached_runtime_components_at_exit)


@asynccontextmanager
async def lint_agent_graph(runtime: ServerRuntime | None = None) -> AsyncIterator[Any]:
    """LangGraph Agent Server factory for the complete ALINT intelligent agent."""

    components = await _get_cached_runtime_components()
    llm = components["llm"]
    tools = components["tools"]
    store = getattr(runtime, "store", None) if runtime is not None else None

    agent_kwargs: dict[str, Any] = {
        "middleware": build_agent_middleware(
            llm,
            root_dir=REPO_ROOT,
            log_prefix="[agent_runtime]",
        )[0],
        "context_schema": AgentContext,
        "system_prompt": SYSTEM_PROMPT,
    }
    if store is not None:
        agent_kwargs["store"] = store

    yield create_agent(
        llm,
        tools,
        **agent_kwargs,
    )


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
