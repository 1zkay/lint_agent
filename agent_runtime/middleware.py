"""Shared middleware builders for agent runtimes."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware import filesystem as deepagents_filesystem
from langchain.agents.middleware import (
    HostExecutionPolicy,
    ModelRetryMiddleware,
    ShellToolMiddleware,
    ToolRetryMiddleware,
)

from config import config
from agent_runtime.reflection import ReflectionMiddleware
from agent_runtime.subagents import build_lint_subagents
from workspace.host_paths import (
    is_configured_container_host_path,
    translate_posix_host_path_for_container,
    translate_windows_host_path_for_container,
)
from workspace.path_resolver import (
    is_path_under_project_root,
    resolve_legacy_slash_project_path,
)

logger = logging.getLogger(__name__)
_FILESYSTEM_ROOT_PATH: Path | None = None
_DISABLED_GENERAL_PURPOSE_PROFILE_KEYS: set[str] = set()


FILESYSTEM_PATH_SYSTEM_PROMPT = """## Project Filesystem Path Conventions

- Read files before editing; understand existing content before making changes.
- Mimic existing style, naming conventions, and patterns.

Use native paths exactly as provided by the user. On Windows, paths such as `D:\\project\\file.sv`, `C:\\Users\\name\\file.txt`, and UNC paths are valid. POSIX-style absolute paths and project-relative paths are also accepted.
Use project-relative paths for files inside the agent repository root: `.files/...` for uploaded files, `skills/...` for bundled skills, and `reports/...` for generated reports. Use `.` for the project root. Legacy tool paths with a leading slash, such as `/.files/...` or `/skills/...`, mean the same project-relative path with the leading slash removed; a bare `/` remains a native POSIX absolute path.
When reading files referenced by a skill, resolve relative paths from that skill directory. For example, if a skill at `skills/example/SKILL.md` references `scripts/run.py`, read `skills/example/scripts/run.py`.
When this agent runs in the customer Docker package with a platform override file, host paths are automatically mapped through the configured bind mount. For example, Windows `D:\\project\\file.sv` can resolve through `/host/d/project/file.sv`, and Linux `/home/user/project/top.sv` can resolve through `/host/root/home/user/project/top.sv`.
Use pagination with offset/limit when reading large files.
"""


def _validate_unrestricted_filesystem_path(path: str, *, allowed_prefixes: Any = None) -> str:
    """Accept native absolute paths for trusted local agent deployments."""
    text = str(path or "").strip()
    if not text:
        return "."
    if text.startswith("~"):
        text = os.path.expanduser(text)
    windows_path = PureWindowsPath(text)
    if text.startswith("\\\\") or (windows_path.drive and windows_path.is_absolute()):
        translated = translate_windows_host_path_for_container(text)
        if translated != text:
            return translated
        return str(windows_path)
    if text.startswith("/"):
        root_path = _FILESYSTEM_ROOT_PATH
        if root_path is not None and is_path_under_project_root(text, root_path):
            return text.replace("\\", "/")
        if is_configured_container_host_path(text):
            return text
        if root_path is not None:
            project_path = resolve_legacy_slash_project_path(text, root_path)
            if project_path is not None:
                return str(project_path)
        return translate_posix_host_path_for_container(text)
    return text.replace("\\", "/")


def enable_unrestricted_deepagents_paths(root_path: str | Path) -> None:
    """Let DeepAgents file tools pass native OS paths through to the backend."""
    global _FILESYSTEM_ROOT_PATH

    _FILESYSTEM_ROOT_PATH = Path(root_path).resolve()
    deepagents_filesystem.validate_path = _validate_unrestricted_filesystem_path


def build_tool_approval_interrupts() -> tuple[dict[str, Any] | None, list[str]]:
    """Build DeepAgents `interrupt_on` config for high-risk tool approval."""
    if not config.agent_tool_approval_enabled:
        return None, []

    guarded_tools = list(config.agent_approval_tool_names)
    if not guarded_tools:
        return None, []

    interrupt_on = {
        name: {"allowed_decisions": ["approve", "reject"]}
        for name in guarded_tools
    }
    return interrupt_on, guarded_tools


def normalize_skill_sources(sources: list[str], root_dir: str | Path) -> list[str]:
    """Normalize skill directories for the `create_deep_agent` filesystem backend."""
    root_path = Path(root_dir).resolve()
    normalized_sources: list[str] = []
    for source in sources:
        raw = source.strip()
        if not raw:
            continue

        expanded = Path(os.path.expanduser(raw))
        candidate = expanded if expanded.is_absolute() else root_path / expanded

        resolved_candidate = candidate.resolve()
        try:
            relative_path = resolved_candidate.relative_to(root_path)
            if not resolved_candidate.is_dir():
                continue
            normalized_sources.append("." if not relative_path.parts else relative_path.as_posix())
            continue
        except ValueError:
            if expanded.is_absolute():
                legacy_candidate = (root_path / raw.lstrip("/")).resolve()
                if legacy_candidate.is_dir():
                    legacy_relative = legacy_candidate.relative_to(root_path)
                    normalized_sources.append(
                        "." if not legacy_relative.parts else legacy_relative.as_posix()
                    )
                    continue
                if resolved_candidate.is_dir():
                    normalized_sources.append(str(expanded))
            continue

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


def _build_deep_agent_system_prompt(system_prompt: str) -> str:
    parts = [
        system_prompt,
        FILESYSTEM_PATH_SYSTEM_PROMPT,
    ]
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _build_deep_agent_backend(root_path: Path) -> CompositeBackend:
    """Route DeepAgents artifacts under the project while preserving native paths."""
    return CompositeBackend(
        default=FilesystemBackend(root_dir=str(root_path), virtual_mode=False),
        routes={
            "/conversation_history/": FilesystemBackend(
                root_dir=str(root_path / "conversation_history"),
                virtual_mode=True,
            ),
            "/large_tool_results/": FilesystemBackend(
                root_dir=str(root_path / "large_tool_results"),
                virtual_mode=True,
            ),
        },
        artifacts_root="/",
    )


def _harness_profile_key_for_llm(llm: Any) -> str | None:
    """Return the DeepAgents provider-level harness profile key for the active model."""
    try:
        ls_params = llm._get_ls_params()
    except (AttributeError, TypeError, NotImplementedError):
        ls_params = None
    if isinstance(ls_params, Mapping):
        provider_value = ls_params.get("ls_provider")
        if isinstance(provider_value, str) and provider_value.strip():
            return provider_value.strip()
    return None


def disable_default_general_purpose_subagent(llm: Any, *, log_prefix: str) -> bool:
    """Disable DeepAgents' auto-added `general-purpose` subagent via official profiles."""
    profile_key = _harness_profile_key_for_llm(llm)
    if profile_key is None:
        logger.warning(
            "%s could not disable DeepAgents default general-purpose subagent: "
            "model provider/profile key is unavailable.",
            log_prefix,
        )
        return False

    profile = HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    )
    if profile_key not in _DISABLED_GENERAL_PURPOSE_PROFILE_KEYS:
        register_harness_profile(profile_key, profile)
        _DISABLED_GENERAL_PURPOSE_PROFILE_KEYS.add(profile_key)

    return True


def _build_project_middleware(
    llm: Any,
    *,
    root_dir: str | Path,
    log_prefix: str,
    tool_retry_tools: list[Any] | None = None,
    disable_shell_if_unavailable: bool = False,
    model_retry_on_failure: Literal["continue", "error"] = "continue",
) -> tuple[list[Any], list[str]]:
    """Build project-specific middleware added after the DeepAgents base stack."""
    root_path = Path(root_dir).resolve()
    middleware_stack: list[Any] = []
    runtime_tool_names: list[str] = []

    if config.agent_enable_reflection:
        middleware_stack.append(
            ReflectionMiddleware(
                model=llm,
                max_reflections=config.agent_reflection_max,
            )
        )
        logger.info("%s ReflectionMiddleware enabled (max_reflections=%s)", log_prefix, config.agent_reflection_max)

    if config.agent_enable_model_retry:
        middleware_stack.append(
            ModelRetryMiddleware(
                max_retries=config.agent_model_retry_max,
                on_failure=model_retry_on_failure,
            )
        )
        logger.info(
            "%s ModelRetryMiddleware enabled (max_retries=%s, on_failure=%s)",
            log_prefix,
            config.agent_model_retry_max,
            model_retry_on_failure,
        )

    if config.agent_enable_tool_retry:
        middleware_stack.append(
            ToolRetryMiddleware(
                max_retries=config.agent_tool_retry_max,
                tools=tool_retry_tools,
            )
        )
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
            runtime_tool_names.append("shell")
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

    return middleware_stack, runtime_tool_names


def create_lint_deep_agent(
    llm: Any,
    tools: list[Any],
    *,
    root_dir: str | Path,
    log_prefix: str,
    system_prompt: str,
    checkpointer: Any | None = None,
    store: Any | None = None,
    context_schema: type[Any] | None = None,
    name: str | None = None,
    tool_retry_tools: list[Any] | None = None,
    disable_shell_if_unavailable: bool = False,
    model_retry_on_failure: Literal["continue", "error"] = "continue",
) -> tuple[Any, list[str], list[str]]:
    """Create the ALINT agent through the official DeepAgents entrypoint."""
    root_path = Path(root_dir).resolve()
    enable_unrestricted_deepagents_paths(root_path)

    normalized_skill_sources = normalize_skill_sources(config.agent_skills_dirs, root_path)
    skill_sources = normalized_skill_sources if config.agent_enable_skills and normalized_skill_sources else None
    if skill_sources:
        logger.info(
            "%s create_deep_agent skills enabled (sources=%s, root_dir=%s)",
            log_prefix,
            skill_sources,
            root_path,
        )
    elif config.agent_enable_skills and config.agent_skills_dirs:
        logger.warning("%s create_deep_agent skills disabled: no skill sources resolved under %s", log_prefix, root_path)

    subagents = None
    default_general_purpose_disabled = False
    if config.agent_enable_subagents:
        subagents = build_lint_subagents(
            llm,
            tools=tools,
            normalized_skill_sources=normalized_skill_sources,
            enable_skills=bool(skill_sources),
        )
        logger.info(
            "%s create_deep_agent lint subagents enabled (subagents=%s, general-purpose=auto)",
            log_prefix,
            [subagent["name"] for subagent in subagents],
        )
    else:
        default_general_purpose_disabled = disable_default_general_purpose_subagent(
            llm,
            log_prefix=log_prefix,
        )
        general_purpose_state = "disabled" if default_general_purpose_disabled else "auto"
        logger.info(
            "%s create_deep_agent subagents disabled (general-purpose=%s)",
            log_prefix,
            general_purpose_state,
        )

    interrupt_on, guarded_tools = build_tool_approval_interrupts()
    if guarded_tools:
        logger.info("%s create_deep_agent tool approval enabled for: %s", log_prefix, guarded_tools)

    middleware_stack, runtime_tool_names = _build_project_middleware(
        llm,
        root_dir=root_path,
        log_prefix=log_prefix,
        tool_retry_tools=tool_retry_tools,
        disable_shell_if_unavailable=disable_shell_if_unavailable,
        model_retry_on_failure=model_retry_on_failure,
    )

    agent = create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=_build_deep_agent_system_prompt(system_prompt),
        middleware=middleware_stack,
        subagents=subagents,
        skills=skill_sources,
        backend=_build_deep_agent_backend(root_path),
        interrupt_on=interrupt_on,
        checkpointer=checkpointer,
        store=store,
        context_schema=context_schema,
        name=name,
    )

    logger.info(
        "%s create_deep_agent enabled (root_dir=%s, backend=filesystem/native paths + routed artifacts)",
        log_prefix,
        root_path,
    )
    task_tool_available = config.agent_enable_subagents or not default_general_purpose_disabled
    runtime_tools = ["task", *runtime_tool_names] if task_tool_available else runtime_tool_names
    return agent, guarded_tools, list(dict.fromkeys(runtime_tools))
