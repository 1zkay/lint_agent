"""Parse LangGraph message-state responses using explicit turn boundaries."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from agent_runtime.contracts import ROOT_CAUSE_WORKFLOW_TOOL_NAME


class ResponseContractError(ValueError):
    """Raised when a run response does not satisfy the expected message contract."""


def field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def message_text(message: Any) -> str:
    content = field(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content) if content else ""


def messages_from_state(state: Any) -> list[Any]:
    messages = field(state, "messages", None)
    if isinstance(messages, list) and messages:
        return messages

    values = field(state, "values", {}) or {}
    nested_messages = field(values, "messages", None)
    if isinstance(nested_messages, list):
        return nested_messages
    return messages if isinstance(messages, list) else []


def message_role(message: Any) -> str:
    return str(field(message, "type", "") or field(message, "role", "") or "message")


def messages_after(state: Any, message_id: str) -> list[Any]:
    messages = messages_from_state(state)
    matching_indexes = [
        index
        for index, message in enumerate(messages)
        if str(field(message, "id", "") or "") == message_id
    ]
    if len(matching_indexes) != 1:
        raise ResponseContractError(
            f"expected one input message with id {message_id}, found {len(matching_indexes)}"
        )
    return messages[matching_indexes[0] + 1 :]


def _has_tool_calls(message: Any) -> bool:
    if field(message, "tool_calls", None) or field(
        message, "invalid_tool_calls", None
    ):
        return True
    additional_kwargs = field(message, "additional_kwargs", {}) or {}
    return isinstance(additional_kwargs, dict) and bool(
        additional_kwargs.get("tool_calls")
        or additional_kwargs.get("invalid_tool_calls")
    )


def final_assistant_text(state: Any, *, after_message_id: str) -> str:
    turn_messages = messages_after(state, after_message_id)
    if not turn_messages:
        raise ResponseContractError("the current turn contains no response messages")

    final_message = turn_messages[-1]
    if message_role(final_message).lower() not in {"ai", "assistant"}:
        raise ResponseContractError("the current turn does not end with an assistant message")
    if _has_tool_calls(final_message):
        raise ResponseContractError("the final assistant message still contains tool calls")

    text = message_text(final_message).strip()
    if not text:
        raise ResponseContractError("the final assistant message has no text content")
    return text


def report_path_from_tool_artifact(
    state: Any,
    *,
    after_message_id: str,
    tool_name: str,
) -> str:
    paths: list[str] = []
    for message in messages_after(state, after_message_id):
        if message_role(message).lower() != "tool":
            continue
        if str(field(message, "name", "") or "") != tool_name:
            continue
        status = str(field(message, "status", "") or "").lower()
        if status and status != "success":
            continue
        artifact = field(message, "artifact", None)
        if not isinstance(artifact, dict):
            raise ResponseContractError(
                f"{tool_name} returned no valid artifact.report_path"
            )
        report_path = artifact.get("report_path")
        if not isinstance(report_path, str) or not report_path.strip():
            raise ResponseContractError(
                f"{tool_name} returned no valid artifact.report_path"
            )
        paths.append(report_path.strip())

    if len(paths) != 1:
        raise ResponseContractError(
            f"expected one report_path from {tool_name} artifact, found {len(paths)}"
        )
    return paths[0]


def _interrupts_from_state(state: Any) -> list[Any]:
    interrupts = field(state, "__interrupt__", [])
    return interrupts if isinstance(interrupts, list) else []


def parse_batch_response(
    response_file: Path,
    *,
    after_message_id: str,
    tool_name: str,
) -> str:
    with response_file.open(encoding="utf-8") as stream:
        state = json.load(stream)
    if _interrupts_from_state(state):
        raise ResponseContractError("the run is waiting for tool approval")

    final_assistant_text(state, after_message_id=after_message_id)
    return report_path_from_tool_artifact(
        state,
        after_message_id=after_message_id,
        tool_name=tool_name,
    )


def latest_run_id(run_list: Any) -> str:
    if not isinstance(run_list, list) or not run_list:
        raise ResponseContractError("the run list is empty or invalid")
    run_id = str(field(run_list[0], "run_id", "") or "")
    try:
        return str(uuid.UUID(run_id))
    except ValueError as exc:
        raise ResponseContractError("the latest run has no valid run_id") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    batch_parser = subparsers.add_parser("batch-response")
    batch_parser.add_argument("response_file", type=Path)
    batch_parser.add_argument("--after-message-id", required=True)
    subparsers.add_parser("latest-run-id")
    args = parser.parse_args(argv)

    try:
        if args.command == "batch-response":
            result = parse_batch_response(
                args.response_file,
                after_message_id=args.after_message_id,
                tool_name=ROOT_CAUSE_WORKFLOW_TOOL_NAME,
            )
        else:
            result = latest_run_id(json.load(sys.stdin))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ResponseContractError,
    ) as exc:
        print(f"invalid Agent Server response: {exc}", file=sys.stderr)
        return 1

    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
