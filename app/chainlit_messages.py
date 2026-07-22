"""Chainlit/LangChain message conversion helpers."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import chainlit as cl
from chainlit.types import ThreadDict

from agent_runtime.message_types import HumanMessage
from workspace.path_resolver import to_project_relative_path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NAMED_UPLOADS_DIRNAME = "_uploaded_files"


def _safe_uploaded_filename(name: str, fallback: str) -> str:
    """Return a local filename derived from the browser-provided upload name."""
    cleaned = str(name or "").replace("\x00", "").replace("\\", "/").strip()
    filename = Path(cleaned).name
    if filename in {"", ".", ".."}:
        return fallback
    return filename


def _named_upload_copy_path(source_path: Path, original_name: str) -> Path:
    file_id = source_path.stem or source_path.name
    filename = _safe_uploaded_filename(original_name, fallback=source_path.name)
    return source_path.parent / NAMED_UPLOADS_DIRNAME / file_id / filename


def _ensure_named_upload_copy(source_path: Path, original_name: str) -> Path:
    """Copy a Chainlit cached upload to a path whose filename is the upload name."""
    named_path = _named_upload_copy_path(source_path, original_name)
    if source_path.resolve() == named_path.resolve():
        return source_path

    named_path.parent.mkdir(parents=True, exist_ok=True)
    if (
        not named_path.exists()
        or named_path.stat().st_size != source_path.stat().st_size
    ):
        shutil.copy2(source_path, named_path)
    return named_path


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

        try:
            readable_path = _ensure_named_upload_copy(p, name)
        except Exception:
            readable_path = p

        abs_path = str(readable_path.resolve())
        tool_path = to_project_relative_path(readable_path, PROJECT_ROOT)
        if tool_path:
            attachment_notes.append(f"- `{name}`: use project-relative path `{tool_path}` (mime={mime})")
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


def extract_seen_user_message_ids_from_thread(thread: ThreadDict) -> list[str]:
    seen_ids: list[str] = []
    for step in list(thread.get("steps", []) or []):
        if str(step.get("type", "") or "") != "user_message":
            continue
        step_id = str(step.get("id", "") or "").strip()
        if step_id:
            seen_ids.append(step_id)
    return seen_ids


async def find_checkpoint_before_message(
    agent: Any,
    thread_id: str,
    message_id: str,
) -> dict[str, Any]:
    """Return the active-branch checkpoint immediately before a user message."""
    target_id = str(message_id or "").strip()
    if not target_id:
        raise ValueError("message_id is required")

    snapshot = await agent.aget_state(
        {"configurable": {"thread_id": thread_id}}
    )
    target_seen = False
    while snapshot is not None:
        values = getattr(snapshot, "values", {})
        messages = values.get("messages", []) if isinstance(values, dict) else []
        contains_target = any(
            str(getattr(item, "id", "") or "") == target_id for item in messages
        )
        if target_seen and not contains_target:
            return dict(snapshot.config)
        target_seen = target_seen or contains_target

        parent_config = getattr(snapshot, "parent_config", None)
        if not parent_config:
            break
        snapshot = await agent.aget_state(parent_config)

    raise RuntimeError(f"checkpoint not found before message {target_id}")
