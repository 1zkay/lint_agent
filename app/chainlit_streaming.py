"""Chainlit streaming and task-list display helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import chainlit as cl

from agent_runtime.message_types import message_text

INTERNAL_TOOL_INPUT_KEYS = {
    "callbacks",
    "config",
    "handler",
    "run_manager",
    "runtime",
    "state",
    "store",
    "stream_writer",
    "tool_call_id",
}

TODO_STATUS_MAP = {
    "pending": cl.TaskStatus.READY,
    "in_progress": cl.TaskStatus.RUNNING,
    "completed": cl.TaskStatus.DONE,
}


async def sync_todos_to_tasklist(todos: list[dict]) -> None:
    """将主智能体 TodoListMiddleware 的 todos 同步到 Chainlit TaskList 面板。"""
    task_list = cl.user_session.get("task_list")
    if not task_list:
        return

    task_list.tasks.clear()
    for todo in todos:
        title = str(todo.get("content", "")).strip()
        if not title:
            continue
        task = cl.Task(
            title=title,
            status=TODO_STATUS_MAP.get(todo.get("status", "pending"), cl.TaskStatus.READY),
        )
        await task_list.add_task(task)

    done = sum(1 for task in task_list.tasks if task.status == cl.TaskStatus.DONE)
    total = len(task_list.tasks)
    if total == 0:
        task_list.status = "Ready"
    elif done == total:
        task_list.status = "Done"
    else:
        task_list.status = f"Running... {done}/{total}"

    await task_list.send()


def step_name(step_type: str, node_name: str) -> str:
    if step_type == "llm":
        return "🧠 LLM" if node_name == "model" else f"🧠 {node_name}"
    if step_type == "tool":
        return f"🔧 {node_name}"
    return f"⚙️ {node_name}"


def message_preview(message: Any) -> str:
    return message_text(message)[:10000]


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_int(*values: Any) -> int | None:
    for value in values:
        parsed = _int_or_none(value)
        if parsed is not None:
            return parsed
    return None


def token_usage_from_message(message: Any) -> dict[str, Any] | None:
    """Extract provider-reported token usage from a LangChain message."""
    usage = _dict_or_empty(getattr(message, "usage_metadata", None))
    response_metadata = _dict_or_empty(getattr(message, "response_metadata", None))
    token_usage = _dict_or_empty(response_metadata.get("token_usage"))
    if not usage and not token_usage:
        return None

    input_tokens = _first_int(usage.get("input_tokens"), token_usage.get("prompt_tokens"))
    output_tokens = _first_int(usage.get("output_tokens"), token_usage.get("completion_tokens"))
    total_tokens = _first_int(usage.get("total_tokens"), token_usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    input_details = _dict_or_empty(usage.get("input_token_details"))
    output_details = _dict_or_empty(usage.get("output_token_details"))
    prompt_details = _dict_or_empty(token_usage.get("prompt_tokens_details"))
    completion_details = _dict_or_empty(token_usage.get("completion_tokens_details"))

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": _first_int(
            input_details.get("cache_read"),
            prompt_details.get("cached_tokens"),
            token_usage.get("prompt_cache_hit_tokens"),
        ),
        "reasoning_tokens": _first_int(
            output_details.get("reasoning"),
            completion_details.get("reasoning_tokens"),
        ),
        "usage_metadata": usage,
        "token_usage": token_usage,
    }


def token_usage_total(usage: dict[str, Any] | None) -> int | None:
    if not usage:
        return None
    return _int_or_none(usage.get("total_tokens"))


def token_usage_summary(usage: dict[str, Any] | None) -> str:
    if not usage:
        return ""

    parts = []
    for label, key in [
        ("total", "total_tokens"),
        ("input", "input_tokens"),
        ("output", "output_tokens"),
        ("cache_read", "cache_read_tokens"),
        ("reasoning", "reasoning_tokens"),
    ]:
        value = _int_or_none(usage.get(key))
        if value is not None:
            parts.append(f"{label}={value}")
    return "Token usage: " + ", ".join(parts) if parts else ""


def token_usage_generation(message: Any, usage: dict[str, Any] | None):
    """Build Chainlit's standard generation object from message usage."""
    if not usage:
        return None

    response_metadata = _dict_or_empty(getattr(message, "response_metadata", None))
    metadata = {}
    if usage.get("usage_metadata"):
        metadata["usage_metadata"] = usage["usage_metadata"]
    if usage.get("token_usage"):
        metadata["token_usage"] = usage["token_usage"]

    return cl.ChatGeneration(
        provider=response_metadata.get("model_provider"),
        model=response_metadata.get("model_name"),
        token_count=_int_or_none(usage.get("total_tokens")),
        input_token_count=_int_or_none(usage.get("input_tokens")),
        output_token_count=_int_or_none(usage.get("output_tokens")),
        metadata=metadata,
        message_completion=cl.GenerationMessage(
            role="assistant",
            content=message_preview(message),
        ),
    )


def tool_call_summary(tool_calls: Any) -> str:
    if not isinstance(tool_calls, list):
        return ""
    names = [
        str(tool_call.get("name") or "tool")
        for tool_call in tool_calls
        if isinstance(tool_call, dict)
    ]
    if not names:
        return ""
    return f"Tool calls: {', '.join(names[:10000])}"


def _filter_internal_tool_input(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _filter_internal_tool_input(item)
            for key, item in value.items()
            if str(key) not in INTERNAL_TOOL_INPUT_KEYS
        }
    if isinstance(value, list | tuple):
        return [_filter_internal_tool_input(item) for item in value]
    return value


def tool_input_for_step(tool_input: Any) -> tuple[Any, bool | str]:
    """Return Chainlit Step.input content and show_input value for a tool call."""
    display_input = _filter_internal_tool_input({} if tool_input is None else tool_input)
    if not display_input:
        return "", False
    if isinstance(display_input, dict | list | tuple):
        return display_input, "json"
    return display_input, "text"


def update_preview(update: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in update.items()
        if key not in {"messages", "todos"}
    }
    if not payload:
        return ""
    return str(payload)[:10000]


def should_show_run_step(node_name: str) -> bool:
    return node_name not in {"model", "tools"} and not node_name.startswith("__") and ":" not in node_name
