"""Chainlit runtime lifecycle for the LangChain agent."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, cast

import chainlit as cl
from langchain.agents import create_agent

from agent_runtime.checkpointer import build_checkpointer
from agent_runtime.configuration import build_runtime_config_for_llm_preset, resolve_llm_preset_id
from agent_runtime.middleware import build_agent_middleware
from agent_runtime.prompts import SYSTEM_PROMPT
from agent_runtime.tools import load_agent_tools
from config import config
from llm.factory import build_chat_model_from_config
from memory.long_term import AgentContext, build_memory_store

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_llm_for_runtime_config(runtime_cfg: Any):
    """根据 runtime config 构建 LangChain chat model；未配置返回 None。"""
    if not runtime_cfg.llm_model:
        return None
    try:
        llm = build_chat_model_from_config(
            runtime_cfg,
            logger=logger,
            log_prefix="[chat_app]",
        )
        logger.info("[chat_app] LLM loaded: %s", runtime_cfg.llm_model)
        return llm
    except Exception as exc:
        logger.error("[chat_app] LLM 初始化失败: %s", exc)
        return None


def get_chainlit_thread_id_fallback() -> str:
    """获取当前 Chainlit 线程 ID；失败时回退到随机 UUID。"""
    try:
        from chainlit.context import context

        thread_id = getattr(getattr(context, "session", None), "thread_id", None)
        if thread_id:
            return str(thread_id)
    except Exception:
        pass
    return str(uuid.uuid4())


def resolve_agent_context(thread_id: str) -> AgentContext:
    """Build the per-run context required by long-term memory tools."""
    app_user = cl.user_session.get("user")
    identifier = str(getattr(app_user, "identifier", "") or "").strip()
    authenticated = bool(identifier)
    user_id = identifier or f"anonymous:{thread_id}"
    return AgentContext(
        user_id=user_id,
        thread_id=thread_id,
        authenticated=authenticated,
    )


def clear_runtime_session_state() -> None:
    """Clear per-chat runtime objects stored in Chainlit user session."""
    cl.user_session.set("agent", None)
    cl.user_session.set("llm", None)
    cl.user_session.set("memory_store", None)
    cl.user_session.set("runtime_task", None)
    cl.user_session.set("runtime_close_event", None)
    cl.user_session.set("runtime_id", None)


async def stop_runtime_owner(*, wait: bool) -> None:
    """Ask the runtime owner task to close resources in its own task."""
    close_event = cl.user_session.get("runtime_close_event")
    runtime_task = cl.user_session.get("runtime_task")

    if close_event:
        close_event.set()

    if wait and runtime_task:
        try:
            await runtime_task
        except Exception as exc:
            logger.warning("[chat_app] Runtime owner close error: %s", exc)


async def _run_chat_runtime_owner(
    *,
    runtime_id: str,
    llm: Any,
    runtime_cfg: Any,
    ready_future: asyncio.Future[dict[str, Any]],
    close_event: asyncio.Event,
) -> None:
    """Own the MCP/session resources so cleanup happens in the same task."""
    exit_stack = AsyncExitStack()
    try:
        loaded_tools = await load_agent_tools(
            exit_stack,
            log_prefix="[chat_app]",
        )
        tools = loaded_tools.tools
        tool_names = loaded_tools.tool_names

        checkpointer = await build_checkpointer(exit_stack, log_prefix="[chat_app]")
        memory_store = await build_memory_store(config, exit_stack)
        middleware_stack, approval_guarded_tools = build_agent_middleware(
            llm,
            root_dir=PROJECT_ROOT,
            log_prefix="[chat_app]",
            disable_shell_if_unavailable=True,
        )

        agent = create_agent(
            llm,
            tools,
            system_prompt=SYSTEM_PROMPT,
            middleware=middleware_stack,
            checkpointer=checkpointer,
            store=memory_store,
            context_schema=AgentContext,
        )
        if approval_guarded_tools:
            logger.info("[chat_app] Tool approval enabled for: %s", approval_guarded_tools)

        cl.user_session.set("agent", agent)
        cl.user_session.set("llm", llm)
        cl.user_session.set("memory_store", memory_store)

        if not ready_future.done():
            ready_future.set_result(
                {
                    "tool_names": tool_names,
                    "runtime_cfg": runtime_cfg,
                }
            )

        await close_event.wait()
    except Exception as exc:
        if not ready_future.done():
            ready_future.set_exception(exc)
        else:
            logger.warning("[chat_app] Runtime owner error: %s", exc)
    finally:
        try:
            await exit_stack.aclose()
            logger.info("[chat_app] MCP session closed")
        except Exception as exc:
            logger.warning("[chat_app] MCP session close error: %s", exc)
        finally:
            if cl.user_session.get("runtime_id") == runtime_id:
                clear_runtime_session_state()


async def initialize_chat_runtime(
    thread_id: str,
    *,
    send_intro: bool,
    llm_preset_id: str | None = None,
) -> None:
    """Initialize MCP, tools, middleware, agent graph, and Chainlit session state."""
    await stop_runtime_owner(wait=True)
    clear_runtime_session_state()
    cl.user_session.set("thread_id", thread_id)
    agent_context = resolve_agent_context(thread_id)
    cl.user_session.set("agent_context", agent_context)
    cl.user_session.set("user_id", agent_context.user_id)
    cl.user_session.set("authenticated", agent_context.authenticated)

    resolved_llm_preset_id = resolve_llm_preset_id(llm_preset_id)
    runtime_cfg = build_runtime_config_for_llm_preset(resolved_llm_preset_id)
    llm = build_llm_for_runtime_config(runtime_cfg)
    cl.user_session.set("llm_preset_id", resolved_llm_preset_id)
    if not llm:
        await cl.Message(
            content=(
                "你好！我是 **ALINT-PRO Verilog 代码审查助手**。\n\n"
                "> ⚠️ 未配置 LLM（`LLM_MODEL` 环境变量为空），问答功能不可用。\n"
                "> 请在 `.env` 文件中填入 LLM 配置后重启。"
            )
        ).send()
        return

    runtime_id = str(uuid.uuid4())
    close_event = asyncio.Event()
    ready_future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    runtime_task = asyncio.create_task(
        _run_chat_runtime_owner(
            runtime_id=runtime_id,
            llm=llm,
            runtime_cfg=runtime_cfg,
            ready_future=ready_future,
            close_event=close_event,
        )
    )
    cl.user_session.set("runtime_id", runtime_id)
    cl.user_session.set("runtime_close_event", close_event)
    cl.user_session.set("runtime_task", runtime_task)

    try:
        setup_payload = await ready_future
    except Exception as exc:
        await stop_runtime_owner(wait=True)
        logger.error("[chat_app] MCP Client 初始化失败: %s", exc)
        await cl.Message(
            content=f"❌ MCP Server 连接失败：`{exc}`\n请检查 `python -m mcp_server.server` 是否正常。"
        ).send()
        return

    tool_names = cast(list[str], setup_payload["tool_names"])

    task_list = cl.TaskList()
    cl.user_session.set("task_list", task_list)
    await task_list.send()

    if not send_intro:
        return

    tools_list = "\n".join(f"  - `{name}`" for name in tool_names)
    history_readiness = []
    if not config.chainlit_enable_password_auth:
        history_readiness.append("- 未启用 Chainlit 认证（`CHAINLIT_ENABLE_PASSWORD_AUTH`）")
    if not config.chainlit_database_url:
        history_readiness.append("- 未配置 Chainlit 数据层（`DATABASE_URL`）")
    history_hint = ""
    if history_readiness:
        history_hint = (
            "\n\n> ℹ️ 网页历史会话显示需要同时启用 Chainlit 认证与数据持久化：\n"
            + "\n".join(f"> {item}" for item in history_readiness)
        )
    await cl.Message(
        content=(
            f"**已连接 MCP 工具（{len(tool_names)} 个）：**\n{tools_list}\n\n"
            "文件工具根目录：`GLOBAL`\n\n"
            f"当前模型：`{runtime_cfg.llm_model}`"
            f"{history_hint}"
        )
    ).send()
