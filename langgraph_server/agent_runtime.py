from __future__ import annotations

import asyncio
import atexit
import logging
import os
import pickle
import shutil
import sys
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from copy import copy
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.skills import SkillsMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import (
    HostExecutionPolicy,
    HumanInTheLoopMiddleware,
    ModelRetryMiddleware,
    ShellToolMiddleware,
    SummarizationMiddleware,
    TodoListMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.middleware.todo import WRITE_TODOS_SYSTEM_PROMPT
from langchain_community.tools import RequestsGetTool
from langchain_community.utilities.requests import TextRequestsWrapper
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_tavily import TavilySearch

from agentic_rag import build_hardware_reference_agentic_rag_tool
from config import config
from llm_factory import build_chat_model_from_config
from long_term_memory import AgentContext, MEMORY_SYSTEM_PROMPT, build_memory_tools
from reflection_middleware import ReflectionMiddleware

try:
    from langgraph_sdk.runtime import ServerRuntime
except Exception:  # pragma: no cover - imported only by LangGraph Agent Server.
    ServerRuntime = Any  # type: ignore


logger = logging.getLogger(__name__)

FILESYSTEM_TOOL_NAMES = ("ls", "read_file", "write_file", "edit_file", "glob", "grep")

PROJECT_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PROJECT_ROOT.parent
MCP_SERVER = str(REPO_ROOT / "mcp_lint.py")
_RUNTIME_COMPONENTS_LOCK: asyncio.Lock | None = None
_RUNTIME_COMPONENTS: dict[str, Any] | None = None

WRITE_TODOS_ENHANCED_PROMPT = WRITE_TODOS_SYSTEM_PROMPT + """
## Critical: Real-Time Todo Updates
- You MUST call `write_todos` IMMEDIATELY after completing each individual task — do NOT batch multiple completions into one call.
- The correct per-task cycle is: mark task `in_progress` → do the work → mark task `completed` (and mark next task `in_progress`) → move on.
- Each `write_todos` call updates the UI in real time. If you skip intermediate calls, the user sees stale progress.

## Critical: Final Answer Placement — One Turn, One Response
When you are ready to deliver the final answer to the user, you MUST follow this exact pattern in a **single LLM turn**:
1. Write your complete, polished final answer as the text of your message.
2. In that **same turn**, call `write_todos` to mark all remaining tasks as `completed`.

**After that turn, output nothing further.** Do NOT add a follow-up turn with phrases like "Done!", "Task complete.", "Let me know if you need anything else.", or any other closing remarks. The turn that contains both the final answer and the final `write_todos` call is the last turn — stop there.

Why this matters: the system displays the text from the write_todos turn as the user-visible response. Any text produced in a subsequent "termination" turn will either overwrite or conflict with the real answer, causing the user to see incomplete or low-quality output.
"""

SYSTEM_PROMPT = """
你是一位资深 Verilog/SystemVerilog 硬件设计专家。
""".strip() + "\n\n" + MEMORY_SYSTEM_PROMPT


def _normalize_skill_sources(sources: list[str]) -> list[str]:
    normalized_sources: list[str] = []
    for source in sources:
        raw = source.strip()
        if not raw:
            continue

        if raw.startswith("/"):
            posix_path = PurePosixPath(raw.lstrip("/"))
            normalized_sources.append("/" if not posix_path.parts else f"/{posix_path.as_posix()}")
            continue

        try:
            relative_path = (REPO_ROOT / raw).resolve().relative_to(REPO_ROOT)
        except ValueError:
            continue
        normalized_sources.append("/" if not relative_path.parts else "/" + relative_path.as_posix())

    seen: set[str] = set()
    deduped_sources: list[str] = []
    for source in normalized_sources:
        if source in seen:
            continue
        seen.add(source)
        deduped_sources.append(source)
    return deduped_sources


def _bool_env(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _apply_recursive_send_sanitization() -> None:
    """Patch LangGraph Send sanitization to remove nested untracked values.

    create_agent can route ToolCallWithContext(state=state) through Send. Some
    middleware stores runtime-only objects in that nested state, for example
    shell sessions, MCP sessions, locks, or process handles. LangGraph already
    filters top-level UntrackedValue channels, but affected versions do not
    filter nested state inside Send.arg, so persistence can fail with errors
    such as "cannot pickle '_thread.lock' object".
    """

    try:
        from langgraph.channels.untracked_value import UntrackedValue
        from langgraph.pregel import _algo, _loop
        from langgraph.types import Send as _Send

        filtered = object()

        def _filter_if_unpickleable(obj: Any) -> Any:
            try:
                pickle.dumps(obj)
            except Exception:
                return filtered
            return obj

        def _recursive_filter(obj: Any, channels: Any) -> Any:
            if isinstance(obj, dict):
                cleaned: dict[Any, Any] = {}
                for key, value in obj.items():
                    if isinstance(channels.get(key), UntrackedValue):
                        continue
                    filtered_value = _recursive_filter(value, channels)
                    if filtered_value is not filtered:
                        cleaned[key] = filtered_value
                return cleaned
            if isinstance(obj, list):
                return [
                    filtered_item
                    for item in obj
                    if (filtered_item := _recursive_filter(item, channels)) is not filtered
                ]
            if isinstance(obj, tuple):
                return tuple(
                    filtered_item
                    for item in obj
                    if (filtered_item := _recursive_filter(item, channels)) is not filtered
                )
            if isinstance(obj, set):
                return {
                    filtered_item
                    for item in obj
                    if (filtered_item := _recursive_filter(item, channels)) is not filtered
                }
            return _filter_if_unpickleable(obj)

        def _patched_sanitize(packet: Any, channels: Any) -> Any:
            if not isinstance(packet.arg, dict):
                return packet
            return _Send(node=packet.node, arg=_recursive_filter(packet.arg, channels))

        _algo.sanitize_untracked_values_in_send = _patched_sanitize
        _loop.sanitize_untracked_values_in_send = _patched_sanitize
        logger.info("[agent_runtime] Applied recursive LangGraph Send sanitization patch.")
    except Exception as exc:
        logger.warning("[agent_runtime] Recursive Send sanitization patch failed: %s", exc)


def _apply_dev_persistence_pickle_sanitization() -> None:
    """Prevent LangGraph dev persistence from crashing on runtime-only handles.

    The local `langgraph dev` runtime periodically pickles `.langgraph_api`
    stores. MCP stdio and shell middleware can keep live file handles in memory;
    those are valid at runtime but not serializable. This patch only sanitizes
    the copy written to disk and leaves the in-memory runtime objects unchanged.
    """

    try:
        from langgraph.checkpoint.memory import PersistentDict

        if getattr(PersistentDict, "_mcp_alint_safe_dump", False):
            return

        original_dump = PersistentDict.dump

        def _safe_for_pickle(obj: Any, seen: set[int]) -> Any:
            if obj is None or isinstance(obj, str | int | float | bool | bytes):
                return obj

            obj_id = id(obj)
            if obj_id in seen:
                return "<cycle>"

            if isinstance(obj, dict):
                seen.add(obj_id)
                cleaned: dict[Any, Any] = {}
                for key, value in obj.items():
                    safe_key = _safe_for_pickle(key, seen)
                    try:
                        hash(safe_key)
                    except Exception:
                        safe_key = repr(safe_key)
                    cleaned[safe_key] = _safe_for_pickle(value, seen)
                seen.discard(obj_id)
                return cleaned

            if isinstance(obj, list):
                seen.add(obj_id)
                cleaned = [_safe_for_pickle(item, seen) for item in obj]
                seen.discard(obj_id)
                return cleaned

            if isinstance(obj, tuple):
                seen.add(obj_id)
                cleaned = tuple(_safe_for_pickle(item, seen) for item in obj)
                seen.discard(obj_id)
                return cleaned

            if isinstance(obj, set):
                seen.add(obj_id)
                cleaned = [_safe_for_pickle(item, seen) for item in obj]
                seen.discard(obj_id)
                return cleaned

            try:
                pickle.dumps(obj)
            except Exception:
                return f"<non-pickleable {type(obj).__module__}.{type(obj).__name__}>"
            return obj

        def _patched_dump(self: Any, fileobj: Any) -> None:
            if self.format == "pickle":
                pickle.dump(_safe_for_pickle(dict(self), set()), fileobj, 2)
                return
            original_dump(self, fileobj)

        PersistentDict.dump = _patched_dump
        PersistentDict._mcp_alint_safe_dump = True
        logger.info("[agent_runtime] Applied LangGraph dev persistence pickle sanitization patch.")
    except Exception as exc:
        logger.warning("[agent_runtime] Dev persistence pickle sanitization patch failed: %s", exc)


if _bool_env("MCP_ALINT_PATCH_LANGGRAPH_DEV_PERSISTENCE", True):
    _apply_dev_persistence_pickle_sanitization()
if _bool_env("MCP_ALINT_PATCH_LANGGRAPH_SEND", True):
    _apply_recursive_send_sanitization()


def find_llm_preset_by_id(preset_id: str | None) -> dict[str, str] | None:
    preset_id = str(preset_id or "").strip()
    for preset in config.llm_model_presets:
        if preset.get("id") == preset_id:
            return preset
    return None


def resolve_llm_preset_id(preset_id: str | None = None) -> str:
    preset_id = str(preset_id or "").strip()
    if preset_id and find_llm_preset_by_id(preset_id):
        return preset_id
    return config.llm_model_preset_default


def build_runtime_config_for_llm_preset(preset_id: str | None = None) -> Any:
    runtime_cfg = copy(config)
    preset = find_llm_preset_by_id(resolve_llm_preset_id(preset_id))
    if preset:
        runtime_cfg.llm_model = preset["model"]
        runtime_cfg.llm_base_url = preset["base_url"]
        runtime_cfg.llm_api_key = preset["api_key"]
    return runtime_cfg


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


def _build_tool_approval_middleware() -> tuple[list[Any], list[str]]:
    if not config.agent_tool_approval_enabled:
        return [], []

    guarded_tools = list(config.agent_approval_tool_names)
    if not guarded_tools:
        return [], []

    interrupt_on = {
        name: {"allowed_decisions": ["approve", "reject"]}
        for name in guarded_tools
    }
    return [
        HumanInTheLoopMiddleware(
            interrupt_on=interrupt_on,
            description_prefix="检测到高风险工具调用，请审批后继续执行。",
        )
    ], guarded_tools


def _resolve_shell_command() -> tuple[str, ...] | None:
    if os.name == "nt":
        git_bash = r"C:\Program Files\Git\bin\bash.exe"
        if os.path.isfile(git_bash):
            return (git_bash,)
        bash = shutil.which("bash")
        return (bash,) if bash else None
    return ("/bin/bash",)


def _build_middleware(llm: Any) -> list[Any]:
    middleware_stack: list[Any] = []
    normalized_skill_sources = _normalize_skill_sources(config.agent_skills_dirs)

    if config.agent_enable_todo:
        middleware_stack.append(TodoListMiddleware(system_prompt=WRITE_TODOS_ENHANCED_PROMPT))

    if config.agent_enable_skills and normalized_skill_sources:
        middleware_stack.append(
            SkillsMiddleware(
                backend=FilesystemBackend(root_dir=str(REPO_ROOT), virtual_mode=True),
                sources=normalized_skill_sources,
            )
        )
    elif config.agent_enable_skills and config.agent_skills_dirs:
        logger.warning("[agent_runtime] SkillsMiddleware disabled: no skill sources resolved under %s", REPO_ROOT)

    middleware_stack.append(
        FilesystemMiddleware(
            backend=FilesystemBackend(
                root_dir=str(REPO_ROOT),
                virtual_mode=True,
            )
        )
    )

    if config.agent_enable_summarization:
        middleware_stack.append(
            SummarizationMiddleware(
                model=llm,
                trigger=("tokens", config.agent_summarization_trigger_tokens),
                keep=("messages", config.agent_summarization_keep_messages),
            )
        )

    if config.agent_enable_reflection:
        middleware_stack.append(
            ReflectionMiddleware(
                model=llm,
                max_reflections=config.agent_reflection_max,
            )
        )

    if config.agent_enable_model_retry:
        middleware_stack.append(ModelRetryMiddleware(max_retries=config.agent_model_retry_max))

    if config.agent_enable_tool_retry:
        middleware_stack.append(ToolRetryMiddleware(max_retries=config.agent_tool_retry_max))

    if config.agent_enable_shell:
        shell_command = _resolve_shell_command()
        if shell_command:
            py_exe = sys.executable.replace("\\", "/")
            middleware_stack.append(
                ShellToolMiddleware(
                    workspace_root=config.shell_workspace_root or str(REPO_ROOT),
                    shell_command=shell_command,
                    startup_commands=(
                        f'python()  {{ "{py_exe}" "$@"; }}',
                        f'python3() {{ "{py_exe}" "$@"; }}',
                        f'pip()     {{ "{py_exe}" -m pip "$@"; }}',
                    ),
                    execution_policy=HostExecutionPolicy(
                        command_timeout=config.shell_command_timeout,
                        max_output_lines=config.shell_max_output_lines,
                    ),
                )
            )
        else:
            logger.warning("[agent_runtime] ShellToolMiddleware disabled: bash not found.")

    hitl_middleware, guarded_tools = _build_tool_approval_middleware()
    middleware_stack.extend(hitl_middleware)
    if guarded_tools:
        logger.info("[agent_runtime] Tool approval enabled for: %s", guarded_tools)

    return middleware_stack


async def _load_tools(exit_stack: AsyncExitStack) -> list[Any]:
    client = MultiServerMCPClient(
        {
            "alint": {
                "command": sys.executable,
                "args": [MCP_SERVER],
                "transport": "stdio",
            }
        }
    )
    session = await exit_stack.enter_async_context(client.session("alint"))
    mcp_tools = await load_mcp_tools(session)

    search_tools = [TavilySearch(max_results=5)] if os.getenv("TAVILY_API_KEY") else []
    fetch_url_tool = RequestsGetTool(
        requests_wrapper=TextRequestsWrapper(),
        allow_dangerous_requests=True,
        name="fetch_url",
        description="Fetch the content of a URL. Input should be a URL string (e.g. https://example.com). Returns the text content of the page.",
    )

    try:
        rag_tool = build_hardware_reference_agentic_rag_tool(config)
    except Exception as exc:
        logger.warning("[agent_runtime] hardware-reference agentic RAG tool init failed: %s", exc)
        rag_tool = None

    tools = [
        *mcp_tools,
        *([rag_tool] if rag_tool else []),
        *search_tools,
        fetch_url_tool,
        *build_memory_tools(),
    ]
    visible_tool_names = list(dict.fromkeys([*(getattr(t, "name", str(t)) for t in tools), *FILESYSTEM_TOOL_NAMES]))
    logger.info("[agent_runtime] Loaded tools: %s", visible_tool_names)
    return tools


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
        tools = await _load_tools(exit_stack)
    except Exception:
        await exit_stack.aclose()
        raise

    return {
        "exit_stack": exit_stack,
        "llm": llm,
        "tools": tools,
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
        "middleware": _build_middleware(llm),
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
