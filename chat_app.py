"""
ALINT-PRO Chainlit 聊天应用（MCP Client + create_agent 版）

架构（基于 LangChain/LangGraph 官方 API）：
  LLM ←→ create_agent(langchain.agents) ←→ MCP Session(持久 stdio)

官方 API 对照：
  图构造 : create_agent(llm, tools, system_prompt=..., checkpointer=...)
          来源: https://docs.langchain.com/oss/python/langchain/agents
  流式   : agent.astream_events(..., version="v3")
          -> run.messages/run.tool_calls/run.updates
          来源: https://docs.langchain.com/oss/python/langchain/event-streaming
  Step   : step.input = ... / step.output = ... （官方字段）
          来源: https://docs.chainlit.io/api-reference/step-class
  审批   : HumanInTheLoopMiddleware + Command(resume=...)
          来源: https://docs.langchain.com/oss/python/langchain/human-in-the-loop
  MCP    : AsyncExitStack + client.session("name") + load_mcp_tools(session)
          来源: https://github.com/langchain-ai/langchain-mcp-adapters README
  状态写回: agent.aupdate_state(config={"configurable":{"thread_id":...}}, values={"messages":[...]})
          来源: https://docs.langchain.com/oss/python/langgraph/persistence

多轮历史：由 checkpointer（postgres/memory）按 thread_id 自动管理。
  每轮只传当前 HumanMessage；history（含 ToolMessage）由 checkpointer 追加累积。
  create_agent 内部正确处理多轮 ToolMessage，不会产生无限循环。

启动：
  chainlit run chat_app.py -w
"""
import asyncio
import hmac
import logging
import os
import sys
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
    reset_agent_history_from_chainlit_context as _reset_agent_history_from_chainlit_context,
)
from app.chainlit_streaming import (
    message_preview as _message_preview,
    should_show_run_step as _should_show_run_step,
    step_name as _step_name,
    sync_todos_to_tasklist as _sync_todos_to_tasklist,
    tool_call_summary as _tool_call_summary,
    update_preview as _update_preview,
)
from app.chainlit_runtime import (
    get_chainlit_thread_id_fallback as _get_chainlit_thread_id_fallback,
    initialize_chat_runtime as _initialize_chat_runtime,
    resolve_agent_context as _resolve_agent_context,
    stop_runtime_owner as _stop_runtime_owner,
)
from agent_runtime.message_types import (
    HumanMessage,
    message_text as _message_text,
    message_tool_calls as _message_tool_calls,
)
from compat.langgraph import apply_recursive_send_sanitization
from config import config

logger = logging.getLogger(__name__)

# ── 源头治理：递归清理 Send.arg 中不可序列化的中间件状态 ──────────────
# 兼容补丁集中在 compat.langgraph，便于后续官方修复后统一移除。
apply_recursive_send_sanitization(log_prefix="[chat_app]")

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
    接收用户消息，统一走 create_agent 主链路（官方短期记忆主入口）。

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

    if is_message_edit:
        await _reset_agent_history_from_chainlit_context(agent, thread_id, message)

    # 每轮只传当前消息——历史由 checkpointer 按 thread_id 自动追加管理
    agent_context = cl.user_session.get("agent_context") or _resolve_agent_context(thread_id)
    run_config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": config.agent_recursion_limit,
    }

    # ── 流式处理（v3 typed projections）────────────────────────────────────
    # 官方标准参考：https://docs.langchain.com/oss/python/langchain/event-streaming
    # run.messages 负责 token；create_agent 内置 ToolCallTransformer 提供 run.tool_calls。
    # UpdatesTransformer 显式开启 run.updates，用于同步 todos 和最终回答状态。
    # ─────────────────────────────────────────────────────────────────────────
    response_msg = cl.Message(content="")
    await response_msg.send()

    llm_step_by_node: dict[str, cl.Step] = {}
    llm_output_by_node: dict[str, str] = {}
    _model_text_buffer: str = ""       # 当前轮缓冲，确认无真实工具调用后写入 response_msg
    _write_todos_text_fallback: str = ""  # write_todos 同轮文字，用于后续空终止轮兜底

    async def _ensure_llm_step(node_name: str) -> cl.Step:
        llm_step = llm_step_by_node.get(node_name)
        if llm_step is not None:
            return llm_step
        llm_step = cl.Step(name=_step_name("llm", node_name), type="llm")
        await llm_step.send()
        llm_step_by_node[node_name] = llm_step
        llm_output_by_node[node_name] = ""
        return llm_step

    async def _consume_v3_message_stream(message_stream: Any) -> None:
        nonlocal _model_text_buffer

        node_name = str(getattr(message_stream, "node", None) or "model")
        llm_step = await _ensure_llm_step(node_name)

        async def _consume_text() -> None:
            nonlocal _model_text_buffer

            async for token_text in message_stream.text:
                token_text = str(token_text or "")
                if not token_text:
                    continue
                await llm_step.stream_token(token_text)
                llm_output_by_node[node_name] += token_text
                if node_name == "model":
                    _model_text_buffer += token_text

        await _consume_text()

    async def _consume_v3_messages(run: Any) -> None:
        async for message_stream in run.messages:
            await _consume_v3_message_stream(message_stream)

    async def _consume_v3_tool_call(tool_call_stream: Any) -> None:
        tool_name = str(getattr(tool_call_stream, "tool_name", "") or "tool")
        tool_input = getattr(tool_call_stream, "input", {}) or {}

        step = cl.Step(name=_step_name("tool", tool_name), type="tool", show_input=True)
        step.input = str(tool_input)[:600]
        step_sent = False

        try:
            await step.send()
            step_sent = True

            async for _delta in tool_call_stream:
                pass

            error = str(getattr(tool_call_stream, "error", "") or "")
            if error:
                step.output = f"Error: {error}"[:800]
            else:
                output = getattr(tool_call_stream, "output", "")
                output_text = _message_text(output) or (str(output) if output is not None else "")
                step.output = output_text[:800]
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

    async def _process_v3_update_data(data: Any) -> None:
        nonlocal _model_text_buffer, _write_todos_text_fallback

        if not isinstance(data, dict):
            return

        for source, update in data.items():
            if not isinstance(update, dict):
                continue

            todos_update = update.get("todos")
            if todos_update is not None:
                await _sync_todos_to_tasklist(todos_update)

            msgs = update.get("messages")
            last_msg = msgs[-1] if msgs else None

            llm_step = llm_step_by_node.pop(source, None)
            if llm_step:
                llm_output = llm_output_by_node.pop(source, "")
                summary = llm_output
                if last_msg is not None and not summary:
                    summary = _message_preview(last_msg) or _tool_call_summary(
                        _message_tool_calls(last_msg)
                    )
                if summary and not llm_output:
                    llm_step.output = summary
                await llm_step.update()
                if source not in {"model", "tools"}:
                    continue

            if source == "model":
                if not last_msg:
                    continue

                tool_calls = _message_tool_calls(last_msg)
                if tool_calls:
                    # write_todos 是 UI 元工具：同轮文字暂存为兜底，供后续空终止轮使用。
                    # 有其他真实工具调用时丢弃缓冲（中间思考不展示给用户）。
                    if all(str(tc.get("name") or "") == "write_todos" for tc in tool_calls):
                        _write_todos_text_fallback = _model_text_buffer
                    _model_text_buffer = ""
                else:
                    # 本轮无工具调用（最终回答轮）：写入缓冲文字；
                    # 若模型输出为空（write_todos 同轮已输出回复），用 fallback 兜底。
                    final_text = _model_text_buffer or _write_todos_text_fallback
                    if final_text:
                        response_msg.content = final_text
                        await response_msg.update()
                    _model_text_buffer = ""
                    _write_todos_text_fallback = ""

            elif _should_show_run_step(source):
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
                    _consume_v3_updates(run),
                )
                if await run.interrupted():
                    hitl_request = _extract_hitl_request_from_interrupts(await run.interrupts())

            if not hitl_request:
                break

            resume_payload = await _ask_hitl_resume_payload(hitl_request)
            pending_input = Command(resume=resume_payload)
            await cl.Message(content="🛂 已提交审批决策，继续执行...").send()
            response_msg = cl.Message(content="")
            await response_msg.send()

    except Exception as e:
        response_msg.content += f"\n\n[错误: {e}]"
        await response_msg.update()
        logger.error(f"[chat_app] Agent stream failed: {e}", exc_info=True)
    finally:
        for llm_step in llm_step_by_node.values():
            try:
                await llm_step.update()
            except Exception:
                pass

    await response_msg.update()


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from chainlit.cli import run_chainlit
    run_chainlit(__file__)
