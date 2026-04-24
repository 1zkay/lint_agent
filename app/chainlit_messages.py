"""Chainlit/LangChain message conversion helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import chainlit as cl
from chainlit.chat_context import chat_context
from chainlit.types import ThreadDict
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def to_project_virtual_path(path: Path) -> str | None:
    """Convert a local path into the POSIX virtual path used by deepagents."""
    resolved = path.resolve()
    try:
        relative_path = resolved.relative_to(PROJECT_ROOT)
    except ValueError:
        return None
    return "/" if not relative_path.parts else "/" + relative_path.as_posix()


def build_human_message_from_chainlit_message(message: cl.Message) -> HumanMessage:
    """Convert Chainlit uploads into a text message with workspace file references."""
    user_text = str(message.content or "").strip()
    message_id = str(getattr(message, "id", "") or "")
    elements = list(getattr(message, "elements", []) or [])

    if not elements:
        return HumanMessage(content=user_text, id=message_id or None)

    attachment_notes: list[str] = []
    for idx, elem in enumerate(elements, start=1):
        path_str = str(getattr(elem, "path", "") or "").strip()
        name = str(getattr(elem, "name", "") or "").strip() or f"upload_{idx}"
        mime = str(getattr(elem, "mime", "") or "").strip().lower() or "unknown"

        if not path_str:
            attachment_notes.append(f"- `{name}`: no local path (mime={mime})")
            continue

        p = Path(path_str)
        if not p.exists():
            attachment_notes.append(f"- `{name}`: file not found (path={path_str})")
            continue

        abs_path = str(p.resolve())
        tool_path = to_project_virtual_path(p)
        if tool_path:
            attachment_notes.append(f"- `{name}`: use `{tool_path}` (mime={mime})")
        else:
            attachment_notes.append(
                f"- `{name}`: outside workspace ({abs_path}, mime={mime})"
            )

    text_parts: list[str] = []
    if user_text:
        text_parts.append(user_text)
    if attachment_notes:
        text_parts.append(
            "[attachment index]\n"
            + "\n".join(attachment_notes)
            + "\n\nUse `read_file` to inspect uploaded files. Use `ls`, `glob`, or `grep` when you need to discover paths or search within the workspace."
        )
    elif not text_parts:
        text_parts.append("[user sent attachments, but no accessible file paths were available]")

    return HumanMessage(content="\n\n".join(text_parts), id=message_id or None)


def build_langchain_message_from_chainlit_history_message(message: cl.Message):
    message_type = str(getattr(message, "type", "") or "")
    message_id = str(getattr(message, "id", "") or "")
    content = str(getattr(message, "content", "") or "")

    if message_type == "user_message":
        return build_human_message_from_chainlit_message(message)
    if message_type == "assistant_message":
        return AIMessage(content=content, id=message_id or None)
    if message_type == "system_message":
        return SystemMessage(content=content, id=message_id or None)
    return None


def _chainlit_history_before_message(current_message_id: str) -> list[cl.Message]:
    history = list(chat_context.get())
    if not current_message_id:
        return history
    for idx, item in enumerate(history):
        if str(getattr(item, "id", "") or "") == current_message_id:
            return history[:idx]
    return history


def extract_seen_user_message_ids_from_thread(thread: ThreadDict) -> list[str]:
    seen_ids: list[str] = []
    for step in list(thread.get("steps", []) or []):
        if str(step.get("type", "") or "") != "user_message":
            continue
        step_id = str(step.get("id", "") or "").strip()
        if step_id:
            seen_ids.append(step_id)
    return seen_ids


async def reset_agent_history_from_chainlit_context(
    agent: Any,
    thread_id: str,
    current_message: cl.Message,
) -> None:
    prior_messages = []
    for item in _chainlit_history_before_message(str(getattr(current_message, "id", "") or "")):
        lc_message = build_langchain_message_from_chainlit_history_message(item)
        if lc_message is not None:
            prior_messages.append(lc_message)

    await agent.aupdate_state(
        config={"configurable": {"thread_id": thread_id}},
        values={
            "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *prior_messages],
            "todos": [],
        },
    )

    task_list = cl.user_session.get("task_list")
    if task_list:
        task_list.tasks.clear()
        task_list.status = "Ready"
        await task_list.send()
