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
from langgraph_server.response_parsing import (
    ResponseContractError,
    field as _field,
    final_assistant_text,
    message_role as _message_role,
    message_text as _message_text,
    messages_from_state as _messages_from_state,
)

EXIT_COMMANDS = {"/exit", "/quit", "exit", "quit", "q"}
_ENSURED_THREAD_KEYS: set[tuple[str, str, str, str]] = set()


def _resolve_user_id(args: argparse.Namespace) -> str:
    return args.user_id or f"cli:{os.getenv('USERNAME') or os.getenv('USER') or 'anonymous'}"


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


def _set_thread_id(args: argparse.Namespace, thread_id: str) -> None:
    args.thread_id = thread_id


def _normalize_thread_id(thread_id: Any) -> str:
    text = str(thread_id or "").strip()
    if not text:
        raise ValueError("thread_id is empty")
    try:
        uuid.UUID(text)
    except ValueError as exc:
        raise ValueError("thread_id must be a UUID. Use /threads to list valid thread IDs.") from exc
    return text


def _resolve_thread_id(args: argparse.Namespace) -> str:
    if args.thread_id:
        return _normalize_thread_id(args.thread_id)
    return str(uuid.uuid4())


def _build_context(args: argparse.Namespace) -> dict[str, Any]:
    user_id = _resolve_user_id(args)
    return {
        "user_id": user_id,
        "thread_id": args.thread_id,
        "authenticated": bool(args.user_id),
    }


def _thread_metadata(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "lint-agent-cli",
        "assistant": args.assistant,
        "user_id": context["user_id"],
        "authenticated": context["authenticated"],
    }


def _thread_cache_key(args: argparse.Namespace, context: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(args.url).rstrip("/"),
        str(args.assistant),
        str(args.thread_id),
        str(context["user_id"]),
    )


def _ensure_thread(client: Any, args: argparse.Namespace, context: dict[str, Any]) -> None:
    cache_key = _thread_cache_key(args, context)
    if cache_key in _ENSURED_THREAD_KEYS:
        return
    _ENSURED_THREAD_KEYS.add(cache_key)

    metadata = _thread_metadata(args, context)
    try:
        # Do not call threads.update here. LangGraph local in-memory runtime may
        # deepcopy existing thread values and fail on runtime objects such as
        # TextIOWrapper. create(if_exists="do_nothing") sets metadata for new
        # CLI-created threads without patching existing ones on every turn.
        client.threads.create(
            thread_id=args.thread_id,
            metadata=metadata,
            graph_id=args.assistant,
            if_exists="do_nothing",
        )
    except Exception as exc:
        # Existing older servers or non-UUID thread IDs may reject explicit creation.
        # runs.wait(if_not_exists="create") remains the source of truth for execution.
        print(f"warning: failed to pre-create thread metadata: {exc}", file=sys.stderr)
        return


def _print_interactive_banner(args: argparse.Namespace, context: dict[str, Any]) -> None:
    print("lint-agent interactive mode")
    print(f"server: {args.url}")
    print(f"assistant: {args.assistant}")
    print(f"thread_id: {args.thread_id}")
    print(f"user_id: {context['user_id']}")
    print("commands: /new, /threads, /resume <thread_id>, /thread, /help, /exit")
    print()


def _print_interactive_help() -> None:
    print("commands:")
    print("  /new                 start a new persistent thread")
    print("  /threads [limit]     list recent threads for the current user")
    print("  /threads all [limit] list recent threads without user filtering")
    print("  /resume <thread_id>  switch to an existing persistent thread")
    print("  /thread              show the current thread_id")
    print("  /thread-info         show current thread metadata")
    print("  /state               show current thread state summary")
    print("  /history [limit]     show current thread checkpoint history")
    print("  /runs [limit]        list runs on current thread")
    print("  /run <run_id>        show one run as JSON")
    print("  /cancel <run_id>     cancel a pending/running run")
    print("  /assistants [limit]  list assistants for this graph")
    print("  /assistant [id]      show assistant metadata")
    print("  /graph               show assistant graph JSON")
    print("  /schemas             show assistant schemas JSON")
    print("  /help                show this help")
    print("  /exit                leave interactive mode")
    print()


def _thread_id_from_item(item: Any) -> str:
    return str(_field(item, "thread_id", "") or "").strip()


def _thread_metadata_from_item(item: Any) -> dict[str, Any]:
    metadata = _field(item, "metadata", {}) or {}
    return metadata if isinstance(metadata, dict) else {}


def _message_preview_from_thread(item: Any) -> str:
    extracted = _field(item, "extracted", {}) or {}
    if not isinstance(extracted, dict):
        return ""
    last_message = extracted.get("last_message")
    text = _message_text(last_message).strip()
    return text[:80] + ("..." if len(text) > 80 else "")


def _parse_threads_command(prompt: str) -> tuple[bool, int]:
    parts = prompt.split()
    include_all = len(parts) >= 2 and parts[1].lower() == "all"
    limit_text = parts[2] if include_all and len(parts) >= 3 else parts[1] if len(parts) >= 2 and not include_all else ""
    try:
        limit = int(limit_text) if limit_text else 10
    except ValueError:
        limit = 10
    return include_all, max(1, min(limit, 50))


def _parse_limit_command(prompt: str, *, default: int = 10, max_limit: int = 50) -> int:
    parts = prompt.split()
    limit_text = parts[1] if len(parts) >= 2 else ""
    try:
        limit = int(limit_text) if limit_text else default
    except ValueError:
        limit = default
    return max(1, min(limit, max_limit))


def _list_threads(client: Any, args: argparse.Namespace, context: dict[str, Any], *, include_all: bool, limit: int) -> None:
    metadata_filter = None if include_all else {"user_id": context["user_id"]}
    try:
        threads = client.threads.search(
            metadata=metadata_filter,
            limit=limit,
            sort_by="updated_at",
            sort_order="desc",
            select=["thread_id", "created_at", "updated_at", "metadata", "status"],
            extract={"last_message": "values.messages[-1]"},
        )
    except Exception as exc:
        print(f"failed to list threads: {exc}")
        return

    if not threads:
        print("no threads found")
        return

    print("recent threads:")
    for index, item in enumerate(threads, start=1):
        thread_id = _thread_id_from_item(item)
        metadata = _thread_metadata_from_item(item)
        marker = "*" if thread_id == args.thread_id else " "
        updated_at = str(_field(item, "updated_at", "") or "")
        status = str(_field(item, "status", "") or "")
        user_id = str(metadata.get("user_id") or "")
        preview = _message_preview_from_thread(item)
        print(f"{marker} {index:>2}. {thread_id}  {status}  {updated_at}  user={user_id}")
        if preview:
            print(f"      {preview}")
    print("use /resume <thread_id> to switch")


def _preview_text(text: str, *, limit: int = 100) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:limit] + ("..." if len(collapsed) > limit else "")


def _checkpoint_id_from_state(state: Any) -> str:
    checkpoint = _field(state, "checkpoint", {}) or {}
    if isinstance(checkpoint, dict):
        return str(checkpoint.get("checkpoint_id") or "")
    return ""


def _print_state_summary(state: Any, *, max_messages: int = 8) -> None:
    checkpoint_id = _checkpoint_id_from_state(state)
    metadata = _field(state, "metadata", {}) or {}
    next_nodes = _field(state, "next", []) or []
    messages = _messages_from_state(state)
    if checkpoint_id:
        print(f"checkpoint: {checkpoint_id}")
    if isinstance(metadata, dict) and metadata:
        print(f"step: {metadata.get('step', '')}  source: {metadata.get('source', '')}  run_id: {metadata.get('run_id', '')}")
    print(f"next: {next_nodes}")
    print(f"messages: {len(messages)}")
    for message in messages[-max_messages:]:
        role = _message_role(message)
        text = _preview_text(_message_text(message), limit=140)
        if text:
            print(f"  {role}: {text}")


def _show_thread_info(client: Any, args: argparse.Namespace) -> None:
    try:
        thread = client.threads.get(args.thread_id)
    except Exception as exc:
        print(f"failed to get thread: {exc}")
        return
    print(_json_text(thread))


def _show_state(client: Any, args: argparse.Namespace) -> None:
    try:
        state = client.threads.get_state(args.thread_id)
    except Exception as exc:
        print(f"failed to get thread state: {exc}")
        return
    _print_state_summary(state)


def _show_history(client: Any, args: argparse.Namespace, *, limit: int) -> None:
    try:
        history = client.threads.get_history(args.thread_id, limit=limit)
    except Exception as exc:
        print(f"failed to get thread history: {exc}")
        return
    if not history:
        print("no history found")
        return
    for index, state in enumerate(history, start=1):
        checkpoint_id = _checkpoint_id_from_state(state)
        metadata = _field(state, "metadata", {}) or {}
        messages = _messages_from_state(state)
        step = metadata.get("step", "") if isinstance(metadata, dict) else ""
        source = metadata.get("source", "") if isinstance(metadata, dict) else ""
        print(f"{index:>2}. checkpoint={checkpoint_id} step={step} source={source} messages={len(messages)}")
        if messages:
            last = messages[-1]
            text = _preview_text(_message_text(last), limit=120)
            if text:
                print(f"    {_message_role(last)}: {text}")


def _run_id_from_item(item: Any) -> str:
    return str(_field(item, "run_id", "") or "").strip()


def _list_runs(client: Any, args: argparse.Namespace, *, limit: int) -> None:
    try:
        runs = client.runs.list(args.thread_id, limit=limit)
    except Exception as exc:
        print(f"failed to list runs: {exc}")
        return
    if not runs:
        print("no runs found")
        return
    print("recent runs:")
    for index, run in enumerate(runs, start=1):
        run_id = _run_id_from_item(run)
        status = str(_field(run, "status", "") or "")
        created_at = str(_field(run, "created_at", "") or "")
        updated_at = str(_field(run, "updated_at", "") or "")
        assistant_id = str(_field(run, "assistant_id", "") or "")
        print(f"{index:>2}. {run_id}  {status}  created={created_at}  updated={updated_at}  assistant={assistant_id}")


def _show_run(client: Any, args: argparse.Namespace, run_id: str) -> None:
    if not run_id:
        print("usage: /run <run_id>")
        return
    try:
        run = client.runs.get(args.thread_id, run_id)
    except Exception as exc:
        print(f"failed to get run: {exc}")
        return
    print(_json_text(run))


def _cancel_run(client: Any, args: argparse.Namespace, run_id: str) -> None:
    if not run_id:
        print("usage: /cancel <run_id>")
        return
    try:
        client.runs.cancel(args.thread_id, run_id, wait=False, action="interrupt")
    except Exception as exc:
        print(f"failed to cancel run: {exc}")
        return
    print(f"cancel requested: {run_id}")


def _list_assistants(client: Any, args: argparse.Namespace, *, limit: int) -> None:
    try:
        assistants = client.assistants.search(
            graph_id=args.assistant,
            limit=limit,
            sort_by="updated_at",
            sort_order="desc",
        )
    except Exception as exc:
        print(f"failed to list assistants: {exc}")
        return
    if not assistants:
        print("no assistants found")
        return
    print("assistants:")
    for index, assistant in enumerate(assistants, start=1):
        assistant_id = str(_field(assistant, "assistant_id", "") or "")
        graph_id = str(_field(assistant, "graph_id", "") or "")
        name = str(_field(assistant, "name", "") or "")
        updated_at = str(_field(assistant, "updated_at", "") or "")
        print(f"{index:>2}. {assistant_id}  graph={graph_id}  name={name}  updated={updated_at}")


def _default_assistant_id(client: Any, args: argparse.Namespace) -> str:
    try:
        assistants = client.assistants.search(
            graph_id=args.assistant,
            limit=1,
            sort_by="updated_at",
            sort_order="desc",
        )
    except Exception:
        return args.assistant
    if assistants:
        assistant_id = str(_field(assistants[0], "assistant_id", "") or "").strip()
        if assistant_id:
            return assistant_id
    return args.assistant


def _show_assistant(client: Any, args: argparse.Namespace, assistant_id: str | None = None) -> None:
    resolved_assistant_id = assistant_id or _default_assistant_id(client, args)
    try:
        assistant = client.assistants.get(resolved_assistant_id)
    except Exception as exc:
        print(f"failed to get assistant: {exc}")
        return
    print(_json_text(assistant))


def _show_graph(client: Any, args: argparse.Namespace) -> None:
    assistant_id = _default_assistant_id(client, args)
    try:
        graph = client.assistants.get_graph(assistant_id, xray=1)
    except Exception as exc:
        print(f"failed to get assistant graph: {exc}")
        return
    print(_json_text(graph))


def _show_schemas(client: Any, args: argparse.Namespace) -> None:
    assistant_id = _default_assistant_id(client, args)
    try:
        schemas = client.assistants.get_schemas(assistant_id)
    except Exception as exc:
        print(f"failed to get assistant schemas: {exc}")
        return
    print(_json_text(schemas))


def _run_wait(client: Any, args: argparse.Namespace, prompt: str, context: dict[str, Any]) -> int:
    _ensure_thread(client, args, context)
    input_message_id = str(uuid.uuid4())
    input_payload = {
        "messages": [{"role": "user", "content": prompt, "id": input_message_id}]
    }
    state = client.runs.wait(
        args.thread_id,
        args.assistant,
        input=input_payload,
        metadata=_thread_metadata(args, context),
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

        try:
            text = final_assistant_text(state, after_message_id=input_message_id)
        except ResponseContractError as exc:
            print(
                f"lint-agent received no final assistant message for this turn: {exc}",
                file=sys.stderr,
            )
            print("raw state follows:", file=sys.stderr)
            print(_json_text(state), file=sys.stderr)
            return 1

        print(text)
        return 0


def _handle_slash_command(
    client: Any,
    args: argparse.Namespace,
    context: dict[str, Any],
    prompt: str,
) -> tuple[bool, bool, dict[str, Any]]:
    lowered = prompt.lower()
    if lowered in EXIT_COMMANDS:
        return True, True, context
    if lowered == "/help":
        _print_interactive_help()
        return True, False, context
    if lowered == "/thread":
        print(args.thread_id)
        return True, False, context
    if lowered == "/threads" or lowered.startswith("/threads "):
        include_all, limit = _parse_threads_command(prompt)
        _list_threads(client, args, context, include_all=include_all, limit=limit)
        return True, False, context
    if lowered == "/thread-info":
        _show_thread_info(client, args)
        return True, False, context
    if lowered == "/state":
        _show_state(client, args)
        return True, False, context
    if lowered == "/history" or lowered.startswith("/history "):
        _show_history(client, args, limit=_parse_limit_command(prompt))
        return True, False, context
    if lowered == "/runs" or lowered.startswith("/runs "):
        _list_runs(client, args, limit=_parse_limit_command(prompt))
        return True, False, context
    if lowered == "/run" or lowered.startswith("/run "):
        run_id = prompt.split(maxsplit=1)[1].strip() if " " in prompt else ""
        _show_run(client, args, run_id)
        return True, False, context
    if lowered == "/cancel" or lowered.startswith("/cancel "):
        run_id = prompt.split(maxsplit=1)[1].strip() if " " in prompt else ""
        _cancel_run(client, args, run_id)
        return True, False, context
    if lowered == "/assistants" or lowered.startswith("/assistants "):
        _list_assistants(client, args, limit=_parse_limit_command(prompt))
        return True, False, context
    if lowered == "/assistant" or lowered.startswith("/assistant "):
        assistant_id = prompt.split(maxsplit=1)[1].strip() if " " in prompt else None
        _show_assistant(client, args, assistant_id)
        return True, False, context
    if lowered == "/graph":
        _show_graph(client, args)
        return True, False, context
    if lowered == "/schemas":
        _show_schemas(client, args)
        return True, False, context
    if lowered == "/new":
        _set_thread_id(args, str(uuid.uuid4()))
        context = _build_context(args)
        print(f"started new thread: {args.thread_id}")
        return True, False, context
    if lowered == "/resume" or lowered.startswith("/resume "):
        thread_id = prompt.split(maxsplit=1)[1].strip() if " " in prompt else ""
        if not thread_id:
            print("usage: /resume <thread_id>")
            return True, False, context
        try:
            _set_thread_id(args, _normalize_thread_id(thread_id))
        except ValueError as exc:
            print(exc)
            return True, False, context
        context = _build_context(args)
        print(f"resumed thread: {args.thread_id}")
        return True, False, context
    return False, False, context


def _run_repl_command(client: Any, args: argparse.Namespace, command: str) -> int:
    command = _recover_surrogate_text(command).strip()
    if not command:
        print("--repl-command is empty", file=sys.stderr)
        return 2
    context = _build_context(args)
    handled, _should_exit, _context = _handle_slash_command(client, args, context, command)
    if not handled:
        print(f"unsupported --repl-command: {command}", file=sys.stderr)
        return 2
    return 0


def _run_interactive(client: Any, args: argparse.Namespace) -> int:
    context = _build_context(args)
    _print_interactive_banner(args, context)

    while True:
        try:
            prompt = input("lint-agent> ")
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            return 130

        prompt = _recover_surrogate_text(prompt).strip()
        if not prompt:
            continue

        handled, should_exit, context = _handle_slash_command(client, args, context, prompt)
        if should_exit:
            return 0
        if handled:
            continue

        status = _run_wait(client, args, prompt, context)
        print()
        if status != 0:
            return status


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
    parser.add_argument("--thread-id", default=os.getenv("LANGGRAPH_THREAD_ID"), help="Persistent LangGraph thread UUID.")
    parser.add_argument("--user-id", default=os.getenv("LANGGRAPH_USER_ID"))
    parser.add_argument("--recursion-limit", type=int, default=int(os.getenv("LANGGRAPH_RECURSION_LIMIT", "50")))
    parser.add_argument("--auto-approve", action="store_true", help="Automatically approve HITL tool requests.")
    parser.add_argument("--auto-reject", action="store_true", help="Automatically reject HITL tool requests.")
    parser.add_argument("--reject-message", default="用户拒绝执行该工具调用。")
    parser.add_argument("--prompt-file", help="Read prompt text from a UTF-8 file.")
    parser.add_argument("--delete-prompt-file", action="store_true", help="Delete --prompt-file after reading.")
    parser.add_argument("--interactive", "-i", action="store_true", help="Start a persistent interactive chat session.")
    parser.add_argument("--repl-command", help="Run one interactive slash command and exit.")
    parser.add_argument("--debug", action="store_true", help="Show Python tracebacks for client errors.")
    args = parser.parse_args(argv)
    if args.auto_approve and args.auto_reject:
        parser.error("--auto-approve and --auto-reject cannot be used together")
    if args.repl_command and (args.prompt or args.prompt_file):
        parser.error("--repl-command cannot be combined with prompt text or --prompt-file")

    should_interact = bool(
        args.interactive
        or (not args.repl_command and not args.prompt and not args.prompt_file and sys.stdin.isatty())
    )
    try:
        _set_thread_id(args, _resolve_thread_id(args))
    except ValueError as exc:
        parser.error(str(exc))

    if args.prompt_file:
        prompt = _read_prompt_file(args.prompt_file, delete=args.delete_prompt_file).strip()
    elif args.repl_command:
        prompt = ""
    elif should_interact:
        prompt = ""
    else:
        prompt = " ".join(args.prompt).strip() if args.prompt else sys.stdin.read().strip()
    prompt = _recover_surrogate_text(prompt).strip()
    if not should_interact and not args.repl_command and not prompt:
        parser.error("prompt is required")

    try:
        _assert_server_available(args.url)
        client = get_sync_client(url=args.url)
        if args.repl_command:
            return _run_repl_command(client, args, args.repl_command)
        if should_interact:
            return _run_interactive(client, args)
        context = _build_context(args)
        return _run_wait(client, args, prompt, context)
    except Exception as exc:
        if args.debug:
            raise
        print(f"lint-agent failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
