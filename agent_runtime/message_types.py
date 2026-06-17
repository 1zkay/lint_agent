"""Shared LangChain message helpers for agent runtimes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeAlias

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

AgentMessage: TypeAlias = AnyMessage
AgentMessageObject: TypeAlias = BaseMessage
AgentStateMessages: TypeAlias = list[AgentMessage]


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
                continue
            if isinstance(item.get("content"), str):
                parts.append(item["content"])
        if parts:
            return "".join(parts)
    return str(content) if content else ""


def message_text(message: Any) -> str:
    """Return displayable text from LangChain v1 messages and message-like values."""
    if isinstance(message, BaseMessage):
        try:
            text_blocks = [
                str(block["text"])
                for block in message.content_blocks
                if isinstance(block, Mapping)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ]
            if text_blocks:
                return "".join(text_blocks)
        except Exception:
            pass
        return _content_to_text(message.content)

    if isinstance(message, Mapping):
        return _content_to_text(message.get("content", ""))

    return _content_to_text(getattr(message, "content", message))


def message_tool_calls(message: Any) -> list[dict[str, Any]]:
    calls = getattr(message, "tool_calls", None)
    if not isinstance(calls, list):
        return []
    return [dict(call) for call in calls if isinstance(call, Mapping)]


__all__ = [
    "AIMessage",
    "AgentMessage",
    "AgentMessageObject",
    "AgentStateMessages",
    "BaseMessage",
    "HumanMessage",
    "RemoveMessage",
    "SystemMessage",
    "ToolMessage",
    "message_text",
    "message_tool_calls",
]
