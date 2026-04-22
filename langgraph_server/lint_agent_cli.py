from __future__ import annotations

import argparse
import json
import locale
import os
from pathlib import Path
import sys
from urllib.request import urlopen
import uuid
from typing import Any

from langgraph_sdk import get_sync_client


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _message_text(message: Any) -> str:
    content = _field(message, "content", "")
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


def _last_ai_text(state: Any) -> str:
    messages = _field(state, "messages", [])
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        msg_type = str(_field(message, "type", "") or _field(message, "role", "")).lower()
        if msg_type in {"ai", "assistant"}:
            text = _message_text(message).strip()
            if text:
                return text
    return ""


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _strip_surrogates(text: str) -> str:
    return text.encode("utf-8", errors="replace").decode("utf-8")


def _recover_surrogate_text(text: str) -> str:
    """Repair text read with surrogateescape from non-UTF-8 Windows consoles."""

    if not _has_surrogate(text):
        return text

    try:
        raw = text.encode("utf-8", errors="surrogateescape")
    except UnicodeEncodeError:
        return _strip_surrogates(text)

    encodings = [
        "utf-8",
        "gb18030",
        "cp936",
        locale.getpreferredencoding(False),
    ]
    seen: set[str] = set()
    for encoding in encodings:
        if not encoding:
            continue
        normalized = encoding.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            repaired = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if not _has_surrogate(repaired):
            return repaired

    return raw.decode("utf-8", errors="replace")


def _read_prompt_file(path: str, *, delete: bool = False) -> str:
    prompt_path = Path(path)
    try:
        return prompt_path.read_text(encoding="utf-8")
    finally:
        if delete:
            try:
                prompt_path.unlink()
            except FileNotFoundError:
                pass


def _extract_interrupts(state: Any) -> list[Any]:
    interrupts = _field(state, "__interrupt__", [])
    return interrupts if isinstance(interrupts, list) else []


def _first_hitl_request(state: Any) -> dict[str, Any] | None:
    for interrupt in _extract_interrupts(state):
        value = _field(interrupt, "value", {})
        if not isinstance(value, dict):
            continue
        action_requests = value.get("action_requests")
        review_configs = value.get("review_configs")
        if isinstance(action_requests, list) and isinstance(review_configs, list):
            return value
    return None


def _review_config_for_action(hitl_request: dict[str, Any], index: int, action: dict[str, Any]) -> dict[str, Any]:
    review_configs = hitl_request.get("review_configs") or []
    if index < len(review_configs) and isinstance(review_configs[index], dict):
        return review_configs[index]

    action_name = action.get("name")
    for config in review_configs:
        if isinstance(config, dict) and config.get("action_name") == action_name:
            return config
    return {"allowed_decisions": ["approve", "reject"]}


def _decision_prompt(allowed: list[str]) -> str:
    choices = []
    if "approve" in allowed:
        choices.append("a=approve")
    if "reject" in allowed:
        choices.append("r=reject")
    if "edit" in allowed:
        choices.append("e=edit")
    return "/".join(choices)


def _read_decision(action: dict[str, Any], review_config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    allowed = [
        str(item).strip().lower()
        for item in (review_config.get("allowed_decisions") or ["approve", "reject"])
    ]
    if args.auto_approve:
        if "approve" not in allowed:
            raise RuntimeError(f"Tool {action.get('name')} does not allow approve.")
        return {"type": "approve"}
    if args.auto_reject:
        if "reject" not in allowed:
            raise RuntimeError(f"Tool {action.get('name')} does not allow reject.")
        return {"type": "reject", "message": args.reject_message}

    print()
    print("需要审批的工具调用:")
    print(f"工具: {action.get('name')}")
    print("参数:")
    print(_json_text(action.get("args") or {}))
    description = str(action.get("description") or "").strip()
    if description:
        print("说明:")
        print(description)

    if not sys.stdin.isatty():
        raise RuntimeError(
            "当前 stdin 不是交互终端，无法人工审批。请在交互终端运行，或使用 --auto-approve / --auto-reject。"
        )

    prompt = f"请选择 ({_decision_prompt(allowed)}): "
    while True:
        choice = input(prompt).strip().lower()
        if choice in {"a", "approve", "y", "yes"} and "approve" in allowed:
            return {"type": "approve"}
        if choice in {"r", "reject", "n", "no"} and "reject" in allowed:
            message = input("拒绝原因（可空）: ").strip() or args.reject_message
            return {"type": "reject", "message": message}
        if choice in {"e", "edit"} and "edit" in allowed:
            print("请输入编辑后的 args JSON。原始参数如下:")
            print(_json_text(action.get("args") or {}))
            edited_text = input("edited args JSON: ").strip()
            try:
                edited_args = json.loads(edited_text)
            except json.JSONDecodeError as exc:
                print(f"JSON 解析失败: {exc}")
                continue
            if not isinstance(edited_args, dict):
                print("edited args JSON 必须是对象。")
                continue
            return {
                "type": "edit",
                "edited_action": {
                    "name": action.get("name"),
                    "args": edited_args,
                },
            }
        print("无效选择，或该工具不允许该决策类型。")


def _build_hitl_decisions(hitl_request: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    action_requests = hitl_request.get("action_requests") or []
    decisions: list[dict[str, Any]] = []
    for index, action in enumerate(action_requests):
        if not isinstance(action, dict):
            raise RuntimeError(f"Invalid HITL action request at index {index}: {action!r}")
        review_config = _review_config_for_action(hitl_request, index, action)
        decisions.append(_read_decision(action, review_config, args))
    return decisions


def _run_wait(client: Any, args: argparse.Namespace, prompt: str, context: dict[str, Any]) -> int:
    input_payload = {"messages": [{"role": "user", "content": prompt}]}
    state = client.runs.wait(
        args.thread_id,
        args.assistant,
        input=input_payload,
        config={"recursion_limit": args.recursion_limit},
        context=context,
        if_not_exists="create",
    )

    while True:
        hitl_request = _first_hitl_request(state)
        if hitl_request is not None:
            decisions = _build_hitl_decisions(hitl_request, args)
            state = client.runs.wait(
                args.thread_id,
                args.assistant,
                command={"resume": {"decisions": decisions}},
                config={"recursion_limit": args.recursion_limit},
                context=context,
            )
            continue

        text = _last_ai_text(state)
        if text:
            print(text)
            return 0

        print(_json_text(state))
    return 1


def _assert_server_available(url: str) -> None:
    health_url = f"{url.rstrip('/')}/ok"
    try:
        with urlopen(health_url, timeout=3) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                raise RuntimeError(f"{health_url} returned HTTP {status}")
    except Exception as exc:
        raise RuntimeError(
            f"Agent Server is not reachable at {url}. "
            "Start start_langgraph_agent_server.cmd first."
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Call the ALINT LangGraph Agent Server.")
    parser.add_argument("prompt", nargs="*", help="Prompt text. If omitted, stdin is used.")
    parser.add_argument("--url", default=os.getenv("LANGGRAPH_URL", "http://127.0.0.1:2024"))
    parser.add_argument("--assistant", default=os.getenv("LANGGRAPH_ASSISTANT", "lint"))
    parser.add_argument("--thread-id", default=os.getenv("LANGGRAPH_THREAD_ID") or str(uuid.uuid4()))
    parser.add_argument("--user-id", default=os.getenv("LANGGRAPH_USER_ID"))
    parser.add_argument("--recursion-limit", type=int, default=int(os.getenv("LANGGRAPH_RECURSION_LIMIT", "50")))
    parser.add_argument("--auto-approve", action="store_true", help="Automatically approve HITL tool requests.")
    parser.add_argument("--auto-reject", action="store_true", help="Automatically reject HITL tool requests.")
    parser.add_argument("--reject-message", default="用户拒绝执行该工具调用。")
    parser.add_argument("--prompt-file", help="Read prompt text from a UTF-8 file.")
    parser.add_argument("--delete-prompt-file", action="store_true", help="Delete --prompt-file after reading.")
    parser.add_argument("--debug", action="store_true", help="Show Python tracebacks for client errors.")
    args = parser.parse_args(argv)
    if args.auto_approve and args.auto_reject:
        parser.error("--auto-approve and --auto-reject cannot be used together")

    if args.prompt_file:
        prompt = _read_prompt_file(args.prompt_file, delete=args.delete_prompt_file).strip()
    else:
        prompt = " ".join(args.prompt).strip() if args.prompt else sys.stdin.read().strip()
    prompt = _recover_surrogate_text(prompt).strip()
    if not prompt:
        parser.error("prompt is required")

    user_id = args.user_id or f"cli:{os.getenv('USERNAME') or os.getenv('USER') or 'anonymous'}"
    context = {
        "user_id": user_id,
        "thread_id": args.thread_id,
        "authenticated": bool(args.user_id),
    }

    try:
        _assert_server_available(args.url)
        client = get_sync_client(url=args.url)
        return _run_wait(client, args, prompt, context)
    except Exception as exc:
        if args.debug:
            raise
        print(f"lint-agent failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
