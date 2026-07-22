"""
ALINT-PRO Chainlit 聊天应用（MCP Client + create_deep_agent 版）

架构（基于 LangChain/LangGraph 官方 API）：
  LLM ←→ create_deep_agent(deepagents) ←→ MCP Session(持久 stdio)

官方 API 对照：
  图构造 : create_deep_agent(model=llm, tools=..., system_prompt=..., checkpointer=...)
          来源: https://github.com/langchain-ai/deepagents
  流式   : agent.astream_events(..., version="v3")
          -> run.messages/run.tool_calls/run.subagents/run.updates
          来源: https://docs.langchain.com/oss/python/langchain/event-streaming
  Step   : step.input = ... / step.output = ... （官方字段）
          来源: https://docs.chainlit.io/api-reference/step-class
  审批   : create_deep_agent(interrupt_on=...) + Command(resume=...)
          来源: https://docs.langchain.com/oss/python/deepagents/human-in-the-loop
  MCP    : AsyncExitStack + client.session("name") + load_mcp_tools(session)
          来源: https://github.com/langchain-ai/langchain-mcp-adapters README
  历史编辑: 从目标消息之前的 checkpoint 分叉执行
          来源: https://docs.langchain.com/oss/python/langgraph/use-time-travel

多轮历史：由 checkpointer（postgres/memory）按 thread_id 自动管理。
  每轮只传当前 HumanMessage；history（含 ToolMessage）由 checkpointer 追加累积。
  create_deep_agent 内部正确处理多轮 ToolMessage，不会产生无限循环。

启动：
  chainlit run chat_app.py -w
"""
import asyncio
import hmac
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Windows + psycopg async 兼容：需使用 SelectorEventLoopPolicy
if os.name == "nt":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent
os.environ.setdefault("CHAINLIT_APP_ROOT", str(PROJECT_ROOT))

# 将当前脚本所在目录加入模块搜索路径，确保本地包可导入
sys.path.insert(0, str(PROJECT_ROOT))

import chainlit as cl
from chainlit.chat_settings import ChatSettings
from chainlit.input_widget import Select
from agent_runtime.configuration import (
    find_llm_preset_by_id as _find_llm_preset_by_id,
    resolve_llm_preset_id as _resolve_llm_preset_id,
)
from langgraph.stream import UpdatesTransformer
from langgraph.types import Command
from chainlit.types import ThreadDict

from app.chainlit_data import register_chainlit_data_layer
from app.chainlit_hitl import (
    ask_hitl_resume_payload as _ask_hitl_resume_payload,
    extract_hitl_request_from_interrupts as _extract_hitl_request_from_interrupts,
)
from app.chainlit_messages import (
    build_human_message_from_chainlit_message as _build_human_message_from_chainlit_message,
    extract_seen_user_message_ids_from_thread as _extract_seen_user_message_ids_from_thread,
    find_checkpoint_before_message as _find_checkpoint_before_message,
)
from app.chainlit_streaming import (
    message_preview as _message_preview,
    should_show_run_step as _should_show_run_step,
    step_name as _step_name,
    sync_todos_to_tasklist as _sync_todos_to_tasklist,
    tool_call_summary as _tool_call_summary,
    tool_input_for_step as _tool_input_for_step,
    token_usage_from_message as _token_usage_from_message,
    token_usage_generation as _token_usage_generation,
    token_usage_summary as _token_usage_summary,
    token_usage_total as _token_usage_total,
    update_preview as _update_preview,
)
from app.chainlit_runtime import (
    get_chainlit_thread_id_fallback as _get_chainlit_thread_id_fallback,
    initialize_chat_runtime as _initialize_chat_runtime,
    resolve_agent_context as _resolve_agent_context,
    stop_runtime_owner as _stop_runtime_owner,
)
from agent_runtime.contracts import ROOT_CAUSE_WORKFLOW_TOOL_NAME
from agent_runtime.message_types import (
    AIMessage,
    HumanMessage,
    message_text as _message_text,
    message_tool_calls as _message_tool_calls,
)
from config import config

logger = logging.getLogger(__name__)
SUBAGENT_DISPATCH_TOOL_NAMES = {"task"}


def _root_cause_report_path(tool_name: str, output: Any) -> Path | None:
    if tool_name != ROOT_CAUSE_WORKFLOW_TOOL_NAME:
        return None

    artifact = getattr(output, "artifact", None)
    if not isinstance(artifact, Mapping):
        return None

    report_path = str(artifact.get("report_path") or "").strip()
    if not report_path:
        return None

    candidate = Path(report_path).resolve()
    reports_root = (PROJECT_ROOT / "reports").resolve()
    try:
        candidate.relative_to(reports_root)
    except ValueError:
        return None
    if candidate.suffix.lower() != ".csv" or not candidate.is_file():
        return None
    return candidate


async def _send_root_cause_report(report_path: Path) -> None:
    await cl.Message(
        content="根因分析报告已生成。",
        elements=[
            cl.File(
                name=report_path.name,
                path=str(report_path),
                display="inline",
                mime="text/csv",
            )
        ],
    ).send()


if config.chainlit_enable_password_auth:
    @cl.password_auth_callback
    async def _password_auth_callback(username: str, password: str):
        """
        官方标准所需：启用认证后，Chainlit 才能展示并恢复聊天历史。
        凭据统一由 config.py 管理。
        """
        if not config.chainlit_auth_username or not config.chainlit_auth_password:
            logger.warning(
                "[chat_app] CHAINLIT_ENABLE_PASSWORD_AUTH=true 但未配置认证用户名/密码。"
            )
            return None
        ok = hmac.compare_digest(username, config.chainlit_auth_username) and hmac.compare_digest(password, config.chainlit_auth_password)
        if not ok:
            return None
        return cl.User(identifier=config.chainlit_auth_username, metadata={"auth_provider": "password"})


register_chainlit_data_layer(cl)


async def _send_model_chat_settings() -> None:
    presets = list(config.llm_model_presets or [])
    if len(presets) <= 1:
        await ChatSettings([]).send()
        return

    current_preset_id = _resolve_llm_preset_id(cl.user_session.get("llm_preset_id"))
    items = {preset["label"]: preset["id"] for preset in presets}
    await ChatSettings(
        [
            Select(
                id="llm_preset",
                label="模型",
                items=items,
                initial_value=current_preset_id,
                tooltip="仅切换主聊天模型；内置参考文档 RAG 继续使用后端默认配置。",
                description="修改后会重建当前会话运行时。",
            )
        ]
    ).send()


@cl.on_chat_start
async def on_chat_start():
    """
    新会话入口：使用 Chainlit 当前线程 ID 作为 LangGraph thread_id。
    不自动发送欢迎消息，进入即对话。
    """
    thread_id = _get_chainlit_thread_id_fallback()
    await _initialize_chat_runtime(thread_id, send_intro=False)
    cl.user_session.set("seen_user_message_ids", [])
    await _send_model_chat_settings()


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    """
    历史线程恢复入口（官方要求：启用认证 + 数据持久化时触发）。
    使用被恢复线程的 id 作为 LangGraph thread_id，确保记忆连续。
    """
    thread_id = str(thread.get("id") or _get_chainlit_thread_id_fallback())
    await _initialize_chat_runtime(thread_id, send_intro=False)
    cl.user_session.set("seen_user_message_ids", _extract_seen_user_message_ids_from_thread(thread))
    await _send_model_chat_settings()


@cl.on_settings_update
async def on_settings_update(settings: dict[str, Any]):
    preset_id = _resolve_llm_preset_id((settings or {}).get("llm_preset"))
    current_preset_id = _resolve_llm_preset_id(cl.user_session.get("llm_preset_id"))
    if not preset_id or preset_id == current_preset_id:
        return

    thread_id = str(cl.user_session.get("thread_id") or _get_chainlit_thread_id_fallback())
    await _initialize_chat_runtime(thread_id, send_intro=False, llm_preset_id=preset_id)
    await _send_model_chat_settings()

    preset = _find_llm_preset_by_id(preset_id)
    label = (preset or {}).get("label") or preset_id
    model_name = (preset or {}).get("model") or ""
    await cl.Message(content=f"已切换模型：`{label}`\n`{model_name}`").send()


@cl.on_chat_end
async def on_chat_end():
    """Chainlit session 结束时正确关闭 MCP stdio 子进程。"""
    await _stop_runtime_owner(wait=True)


@cl.on_message
async def on_message(message: cl.Message):
    """
    接收用户消息，统一走 create_deep_agent 主链路（官方短期记忆主入口）。

    流式处理采用官方推荐的 v3 event streaming API：
      run.messages：LLM 输出 token
      run.tool_calls：工具调用生命周期
      run.updates ：完整状态更新，含 model 节点和 tools 节点的输出消息

    官方参考：https://docs.langchain.com/oss/python/langchain/event-streaming

    cl.Step 用法遵循官方：
      step.input  = ...  设置工具参数展示（show_input=True 时可见）
      step.output = ...  设置工具结果展示
      官方参考：https://docs.chainlit.io/api-reference/step-class

    记忆语义：
    - 本函数经 agent.astream_events 执行，状态更新自动落入 checkpointer。
    """
    agent = cl.user_session.get("agent")
    if not agent:
        await cl.Message(
            content="⚠️ 未配置 LLM，无法回答问题。请配置 `.env` 后重启。"
        ).send()
        return

    thread_id: str = cl.user_session.get("thread_id")
    current_message_id = str(getattr(message, "id", "") or "")
    seen_user_message_ids = list(cl.user_session.get("seen_user_message_ids") or [])
    is_message_edit = bool(current_message_id) and current_message_id in seen_user_message_ids
    if current_message_id and not is_message_edit:
        seen_user_message_ids.append(current_message_id)
        cl.user_session.set("seen_user_message_ids", seen_user_message_ids)

    try:
        user_human_message = _build_human_message_from_chainlit_message(message)
    except Exception as e:
        logger.warning(f"[chat_app] 构造上传消息失败，回退纯文本输入: {e}")
        user_human_message = HumanMessage(content=str(message.content or ""), id=current_message_id or None)

    # 每轮只传当前消息——历史由 checkpointer 按 thread_id 自动追加管理
    agent_context = cl.user_session.get("agent_context") or _resolve_agent_context(thread_id)
    latest_thread_config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": config.agent_recursion_limit,
    }
    run_config = latest_thread_config

    # ── 流式处理（v3 typed projections）────────────────────────────────────
    # 官方标准参考：https://docs.langchain.com/oss/python/langchain/event-streaming
    # run.messages 负责 token；create_deep_agent 内置 ToolCallTransformer 提供 run.tool_calls。
    # UpdatesTransformer 显式开启 run.updates，用于同步 todos 和非模型节点状态。
    # ─────────────────────────────────────────────────────────────────────────
    task_list = cl.user_session.get("task_list")
    if task_list:
        task_list.tasks.clear()
        task_list.status = "Ready"
        await task_list.send()

    _reported_usage_message_keys: set[str] = set()

    def _usage_message_key(message: Any) -> str:
        message_id = str(getattr(message, "id", "") or "").strip()
        if message_id:
            return message_id
        return f"{type(message).__name__}:{id(message)}"

    def _append_usage_summary(text: str, usage_summary: str) -> str:
        if not usage_summary or usage_summary in text:
            return text
        return f"{text}\n\n{usage_summary}" if text else usage_summary

    async def _record_message_token_usage(message: Any) -> tuple[Any | None, str]:
        usage = _token_usage_from_message(message)
        if not usage:
            return None, ""

        key = _usage_message_key(message)
        if key not in _reported_usage_message_keys:
            _reported_usage_message_keys.add(key)
            total_tokens = _token_usage_total(usage)
            if total_tokens is not None:
                try:
                    maybe_awaitable = cl.context.emitter.update_token_count(total_tokens)
                    if hasattr(maybe_awaitable, "__await__"):
                        await maybe_awaitable
                except Exception as exc:
                    logger.debug("[chat_app] Chainlit token usage update failed: %s", exc)

        return _token_usage_generation(message, usage), _token_usage_summary(usage)

    async def _consume_v3_message_stream(message_stream: Any) -> None:
        node_name = str(getattr(message_stream, "node", None) or "model")
        output_buffer = ""

        async with cl.Step(name=_step_name("llm", node_name), type="llm") as llm_step:
            async for token_text in message_stream.text:
                token_text = str(token_text or "")
                if not token_text:
                    continue
                output_buffer += token_text
                await llm_step.stream_token(token_text)

            output_message = await message_stream.output
            usage_generation, usage_summary = await _record_message_token_usage(
                output_message
            )
            if usage_generation is not None:
                llm_step.generation = usage_generation

            summary = (
                output_buffer
                or _message_preview(output_message)
                or _tool_call_summary(_message_tool_calls(output_message))
            )
            if summary and not output_buffer:
                llm_step.output = summary
            if usage_summary:
                llm_step.output = _append_usage_summary(
                    llm_step.output or summary, usage_summary
                )

    async def _consume_v3_messages(run: Any) -> None:
        async for message_stream in run.messages:
            await _consume_v3_message_stream(message_stream)

    async def _consume_v3_tool_output_deltas(tool_call_stream: Any, step: cl.Step) -> None:
        deltas = getattr(tool_call_stream, "output_deltas", None)
        if deltas is not None:
            async for delta in deltas:
                delta_text = _message_text(delta) or (str(delta) if delta is not None else "")
                if delta_text:
                    await step.stream_token(delta_text)
            return

        async for delta in tool_call_stream:
            delta_text = _message_text(delta) or (str(delta) if delta is not None else "")
            if delta_text:
                await step.stream_token(delta_text)

    async def _drain_v3_tool_output_deltas(tool_call_stream: Any) -> None:
        deltas = getattr(tool_call_stream, "output_deltas", None)
        if deltas is not None:
            async for _delta in deltas:
                pass
            return

        async for _delta in tool_call_stream:
            pass

    async def _consume_v3_tool_call(
        tool_call_stream: Any,
        *,
        parent_id: str | None = None,
        default_open: bool = False,
        output_limit: int = 10000,
    ) -> None:
        tool_name = str(getattr(tool_call_stream, "tool_name", "") or "tool")
        tool_input = getattr(tool_call_stream, "input", {}) or {}
        if tool_name == "write_todos" and not config.chainlit_show_todo_list:
            await _drain_v3_tool_output_deltas(tool_call_stream)
            return
        if tool_name in SUBAGENT_DISPATCH_TOOL_NAMES:
            await _drain_v3_tool_output_deltas(tool_call_stream)
            error = str(getattr(tool_call_stream, "error", "") or "")
            if error:
                subagent_type = ""
                if isinstance(tool_input, dict):
                    subagent_type = str(tool_input.get("subagent_type") or "").strip()

                task_step_name = "🤖 task"
                if subagent_type:
                    task_step_name = f"🤖 {subagent_type}"

                step = cl.Step(
                    name=task_step_name,
                    type="run",
                    parent_id=parent_id,
                    default_open=default_open,
                    auto_collapse=True,
                )
                step.is_error = True
                step.output = error[:output_limit]
                await step.send()
                await step.update()
            return

        step_input, show_input = _tool_input_for_step(tool_input)

        step = cl.Step(
            name=_step_name("tool", tool_name),
            type="tool",
            parent_id=parent_id,
            show_input=show_input,
            default_open=default_open,
            auto_collapse=parent_id is not None,
        )
        step.input = step_input
        step_sent = False

        try:
            await step.send()
            step_sent = True

            await _consume_v3_tool_output_deltas(tool_call_stream, step)

            error = str(getattr(tool_call_stream, "error", "") or "")
            if error:
                step.is_error = True
                step.output = error[:output_limit]
            else:
                output = getattr(tool_call_stream, "output", "")
                output_text = _message_text(output) or (str(output) if output is not None else "")
                if output_text:
                    step.output = output_text[:output_limit]
                report_path = _root_cause_report_path(tool_name, output)
                if report_path is not None:
                    await _send_root_cause_report(report_path)
        finally:
            if step_sent:
                try:
                    await step.update()
                except Exception:
                    pass

    async def _consume_v3_tool_calls(run: Any) -> None:
        tool_tasks: list[asyncio.Task[None]] = []
        async for tool_call_stream in run.tool_calls:
            tool_tasks.append(asyncio.create_task(_consume_v3_tool_call(tool_call_stream)))
        if tool_tasks:
            await asyncio.gather(*tool_tasks)

    async def _consume_v3_subagent(subagent_stream: Any) -> None:
        subagent_name = str(
            getattr(subagent_stream, "name", None)
            or "subagent"
        )
        step = cl.Step(name=f"🤖 {subagent_name}", type="run", default_open=True)
        step_sent = False
        output_buffer = ""

        async def _consume_subagent_messages() -> None:
            nonlocal output_buffer

            async for message_stream in subagent_stream.messages:
                async for token_text in message_stream.text:
                    token_text = str(token_text or "")
                    if not token_text:
                        continue
                    output_buffer += token_text
                    await step.stream_token(token_text)
                try:
                    output_message = await message_stream.output
                except Exception:
                    continue
                usage_generation, usage_summary = await _record_message_token_usage(output_message)
                if usage_generation is not None:
                    step.generation = usage_generation
                if usage_summary:
                    output_buffer = _append_usage_summary(output_buffer, usage_summary)

        async def _consume_subagent_tool_calls() -> None:
            tool_calls = getattr(subagent_stream, "tool_calls", None)
            if tool_calls is None:
                return
            tool_tasks: list[asyncio.Task[None]] = []
            async for tool_call_stream in tool_calls:
                tool_tasks.append(
                    asyncio.create_task(
                        _consume_v3_tool_call(
                            tool_call_stream,
                            parent_id=step.id,
                            default_open=False,
                            output_limit=10000,
                        )
                    )
                )
            if tool_tasks:
                await asyncio.gather(*tool_tasks)

        try:
            await step.send()
            step_sent = True

            await asyncio.gather(
                _consume_subagent_messages(),
                _consume_subagent_tool_calls(),
            )

            error = str(getattr(subagent_stream, "error", "") or "")
            if error:
                step.is_error = True
                step.output = error[:10000]
            elif output_buffer:
                step.output = output_buffer[:10000]
            else:
                try:
                    output = await subagent_stream.output()
                except Exception as exc:
                    step.is_error = True
                    step.output = str(exc)[:10000]
                else:
                    messages = output.get("messages") if isinstance(output, dict) else None
                    last_msg = messages[-1] if messages else None
                    step.output = (
                        _message_preview(last_msg)
                        if last_msg is not None
                        else f"Status: {getattr(subagent_stream, 'status', 'completed')}"
                    )[:10000]
        finally:
            if step_sent:
                try:
                    await step.update()
                except Exception:
                    pass

    async def _consume_v3_subagents(run: Any) -> None:
        subagent_streams = getattr(run, "subagents", None)
        if subagent_streams is None:
            return
        subagent_tasks: list[asyncio.Task[None]] = []
        async for subagent_stream in subagent_streams:
            subagent_tasks.append(asyncio.create_task(_consume_v3_subagent(subagent_stream)))
        if subagent_tasks:
            await asyncio.gather(*subagent_tasks)

    async def _process_v3_update_data(data: Any) -> None:
        if not isinstance(data, dict):
            return

        for source, update in data.items():
            if not isinstance(update, dict):
                continue

            todos_update = update.get("todos")
            if todos_update is not None:
                await _sync_todos_to_tasklist(todos_update)

            if _should_show_run_step(source):
                msgs = update.get("messages")
                last_msg = msgs[-1] if msgs else None
                step = cl.Step(name=_step_name("run", source), type="run")
                step.output = _update_preview(update) or (
                    _message_preview(last_msg) if last_msg is not None else f"Node `{source}` completed"
                )
                await step.send()
                await step.update()

    async def _consume_v3_updates(run: Any) -> None:
        async for data in run.updates:
            await _process_v3_update_data(data)

    try:
        if is_message_edit:
            checkpoint_config = await _find_checkpoint_before_message(
                agent, thread_id, current_message_id
            )
            run_config = {
                **checkpoint_config,
                "recursion_limit": config.agent_recursion_limit,
            }

        pending_input = {"messages": [user_human_message]}
        while True:
            hitl_request = None
            async with await agent.astream_events(
                pending_input,
                config=run_config,
                context=agent_context,
                version="v3",
                transformers=[UpdatesTransformer],
            ) as run:
                await asyncio.gather(
                    _consume_v3_messages(run),
                    _consume_v3_tool_calls(run),
                    _consume_v3_subagents(run),
                    _consume_v3_updates(run),
                )
                if await run.interrupted():
                    hitl_request = _extract_hitl_request_from_interrupts(await run.interrupts())
                else:
                    final_state = await run.output()
                    messages = (
                        final_state.get("messages")
                        if isinstance(final_state, Mapping)
                        else None
                    )
                    final_message = messages[-1] if messages else None
                    if (
                        isinstance(final_message, AIMessage)
                        and not _message_tool_calls(final_message)
                    ):
                        final_text = _message_text(final_message)
                        if final_text:
                            await cl.Message(content=final_text).send()

            if hitl_request:
                run_config = latest_thread_config
            if not hitl_request:
                break

            resume_payload = await _ask_hitl_resume_payload(hitl_request)
            pending_input = Command(resume=resume_payload)
            await cl.Message(content="🛂 已提交审批决策，继续执行...").send()

    except Exception as e:
        await cl.Message(content=f"[错误: {e}]").send()
        logger.error(f"[chat_app] Agent stream failed: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from chainlit.cli import run_chainlit
    run_chainlit(__file__)
