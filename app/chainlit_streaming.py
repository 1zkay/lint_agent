"""Chainlit streaming and task-list display helpers."""

from __future__ import annotations

from typing import Any

import chainlit as cl

TODO_STATUS_MAP = {
    "pending": cl.TaskStatus.READY,
    "in_progress": cl.TaskStatus.RUNNING,
    "completed": cl.TaskStatus.DONE,
}


async def sync_todos_to_tasklist(todos: list[dict]) -> None:
    """将 TodoListMiddleware 的 todos 状态同步到 Chainlit TaskList 面板。"""
    task_list = cl.user_session.get("task_list")
    if not task_list:
        return

    task_list.tasks.clear()
    for todo in todos:
        task = cl.Task(
            title=todo.get("content", ""),
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
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content[:800]
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
        if texts:
            return "\n".join(texts)[:800]
    if content:
        return str(content)[:800]
    return ""


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
    return f"Tool calls: {', '.join(names[:5])}"


def update_preview(update: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in update.items()
        if key not in {"messages", "todos"}
    }
    if not payload:
        return ""
    return str(payload)[:800]


def should_show_run_step(node_name: str) -> bool:
    return node_name not in {"model", "tools"} and not node_name.startswith("__") and ":" not in node_name
