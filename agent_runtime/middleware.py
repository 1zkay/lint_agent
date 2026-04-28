"""Shared middleware builders for agent runtimes."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware import filesystem as deepagents_filesystem
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.skills import SkillsMiddleware
from langchain.agents.middleware import (
    HostExecutionPolicy,
    HumanInTheLoopMiddleware,
    ModelRetryMiddleware,
    ShellToolMiddleware,
    SummarizationMiddleware,
    TodoListMiddleware,
    ToolRetryMiddleware,
)

from config import config
from agent_runtime.reflection import ReflectionMiddleware

from .prompts import WRITE_TODOS_ENHANCED_PROMPT

logger = logging.getLogger(__name__)


UNRESTRICTED_FILESYSTEM_SYSTEM_PROMPT = """## Following Conventions

- Read files before editing; understand existing content before making changes.
- Mimic existing style, naming conventions, and patterns.

## Filesystem Tools `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`

You have unrestricted local filesystem access through these tools.
Use native paths exactly as provided by the user. On Windows, paths such as `D:\\project\\file.sv`, `C:\\Users\\name\\file.txt`, and UNC paths are valid. POSIX-style absolute paths and relative paths are also accepted; relative paths resolve from the agent repository root.
When this agent runs in Docker, the container must bind-mount host paths for them to exist inside the runtime.

- ls: list files in a directory
- read_file: read a file from the filesystem
- write_file: write to a new file in the filesystem
- edit_file: edit a file in the filesystem
- glob: find files matching a pattern, optionally under a native path
- grep: search for text within files

Use pagination with offset/limit when reading large files.
"""


def _validate_unrestricted_filesystem_path(path: str, *, allowed_prefixes: Any = None) -> str:
    """Accept native absolute paths for trusted local agent deployments."""
    text = str(path or "").strip()
    if not text:
        return "."
    if text.startswith("~"):
        text = os.path.expanduser(text)
    if re.match(r"^[a-zA-Z]:[\\/]", text) or text.startswith("\\\\"):
        return str(PureWindowsPath(text))
    if PurePath(text).is_absolute() or text.startswith("/"):
        return text
    return text.replace("\\", "/")


def enable_unrestricted_deepagents_paths() -> None:
    """Let DeepAgents file tools pass native OS paths through to the backend."""
    deepagents_filesystem.validate_path = _validate_unrestricted_filesystem_path


def build_tool_approval_middleware() -> tuple[list[Any], list[str]]:
    """Build LangChain human-in-the-loop middleware for configured tools."""
    if not config.agent_tool_approval_enabled:
        return [], []

    guarded_tools = list(config.agent_approval_tool_names)
    if not guarded_tools:
        return [], []

    interrupt_on = {
        name: {"allowed_decisions": ["approve", "reject"]}
        for name in guarded_tools
    }
    return [
        HumanInTheLoopMiddleware(
            interrupt_on=interrupt_on,
            description_prefix="检测到高风险工具调用，请审批后继续执行。",
        )
    ], guarded_tools


def normalize_skill_sources(sources: list[str], root_dir: str | Path) -> list[str]:
    """Normalize configured skill directories to DeepAgents virtual paths."""
    root_path = Path(root_dir).resolve()
    normalized_sources: list[str] = []
    for source in sources:
        raw = source.strip()
        if not raw:
            continue

        if raw.startswith("/"):
            posix_path = PurePosixPath(raw.lstrip("/"))
            normalized_sources.append("/" if not posix_path.parts else f"/{posix_path.as_posix()}")
            continue

        try:
            relative_path = (root_path / raw).resolve().relative_to(root_path)
        except ValueError:
            continue
        normalized_sources.append("/" if not relative_path.parts else "/" + relative_path.as_posix())

    seen: set[str] = set()
    deduped_sources: list[str] = []
    for source in normalized_sources:
        if source in seen:
            continue
        seen.add(source)
        deduped_sources.append(source)
    return deduped_sources


def resolve_shell_command() -> tuple[str, ...] | None:
    """Resolve the shell command used by ShellToolMiddleware."""
    if os.name == "nt":
        git_bash = r"C:\Program Files\Git\bin\bash.exe"
        if os.path.isfile(git_bash):
            return (git_bash,)
        bash = shutil.which("bash")
        return (bash,) if bash else None
    return ("/bin/bash",)


def build_agent_middleware(
    llm: Any,
    *,
    root_dir: str | Path,
    log_prefix: str,
    disable_shell_if_unavailable: bool = False,
) -> tuple[list[Any], list[str]]:
    """Build the shared LangChain/DeepAgents middleware stack."""
    root_path = Path(root_dir).resolve()
    middleware_stack: list[Any] = []
    enable_unrestricted_deepagents_paths()

    if config.agent_enable_todo:
        middleware_stack.append(TodoListMiddleware(system_prompt=WRITE_TODOS_ENHANCED_PROMPT))
        logger.info("%s TodoListMiddleware (Plan-and-Execute) enabled", log_prefix)

    normalized_skill_sources = normalize_skill_sources(config.agent_skills_dirs, root_path)
    if config.agent_enable_skills and normalized_skill_sources:
        middleware_stack.append(
            SkillsMiddleware(
                backend=FilesystemBackend(root_dir=str(root_path), virtual_mode=True),
                sources=normalized_skill_sources,
            )
        )
        logger.info(
            "%s SkillsMiddleware enabled (sources=%s, root_dir=%s)",
            log_prefix,
            normalized_skill_sources,
            root_path,
        )
    elif config.agent_enable_skills and config.agent_skills_dirs:
        logger.warning("%s SkillsMiddleware disabled: no skill sources resolved under %s", log_prefix, root_path)

    middleware_stack.append(
        FilesystemMiddleware(
            backend=FilesystemBackend(
                root_dir=str(root_path),
                virtual_mode=False,
            ),
            system_prompt=UNRESTRICTED_FILESYSTEM_SYSTEM_PROMPT,
        )
    )
    logger.info("%s FilesystemMiddleware enabled (root_dir=%s, virtual_mode=False, unrestricted paths)", log_prefix, root_path)

    if config.agent_enable_summarization:
        middleware_stack.append(
            SummarizationMiddleware(
                model=llm,
                trigger=("tokens", config.agent_summarization_trigger_tokens),
                keep=("messages", config.agent_summarization_keep_messages),
            )
        )
        logger.info(
            "%s SummarizationMiddleware enabled (trigger=%s tokens, keep=%s messages)",
            log_prefix,
            config.agent_summarization_trigger_tokens,
            config.agent_summarization_keep_messages,
        )

    if config.agent_enable_reflection:
        middleware_stack.append(
            ReflectionMiddleware(
                model=llm,
                max_reflections=config.agent_reflection_max,
            )
        )
        logger.info("%s ReflectionMiddleware enabled (max_reflections=%s)", log_prefix, config.agent_reflection_max)

    if config.agent_enable_model_retry:
        middleware_stack.append(ModelRetryMiddleware(max_retries=config.agent_model_retry_max))
        logger.info("%s ModelRetryMiddleware enabled (max_retries=%s)", log_prefix, config.agent_model_retry_max)

    if config.agent_enable_tool_retry:
        middleware_stack.append(ToolRetryMiddleware(max_retries=config.agent_tool_retry_max))
        logger.info("%s ToolRetryMiddleware enabled (max_retries=%s)", log_prefix, config.agent_tool_retry_max)

    if config.agent_enable_shell:
        shell_command = resolve_shell_command()
        if shell_command:
            py_exe = sys.executable.replace("\\", "/")
            middleware_stack.append(
                ShellToolMiddleware(
                    workspace_root=config.shell_workspace_root or str(root_path),
                    shell_command=shell_command,
                    startup_commands=(
                        f'python()  {{ "{py_exe}" "$@"; }}',
                        f'python3() {{ "{py_exe}" "$@"; }}',
                        f'pip()     {{ "{py_exe}" -m pip "$@"; }}',
                    ),
                    execution_policy=HostExecutionPolicy(
                        command_timeout=config.shell_command_timeout,
                        max_output_lines=config.shell_max_output_lines,
                    ),
                )
            )
            logger.info(
                "%s ShellToolMiddleware enabled (workspace=%s, timeout=%ss)",
                log_prefix,
                config.shell_workspace_root or root_path,
                config.shell_command_timeout,
            )
        else:
            logger.warning("%s ShellToolMiddleware disabled: bash not found.", log_prefix)
            if disable_shell_if_unavailable:
                config.agent_enable_shell = False

    hitl_middleware, guarded_tools = build_tool_approval_middleware()
    middleware_stack.extend(hitl_middleware)
    if guarded_tools:
        logger.info("%s Tool approval enabled for: %s", log_prefix, guarded_tools)

    return middleware_stack, guarded_tools
