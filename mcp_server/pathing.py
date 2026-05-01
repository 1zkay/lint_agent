"""Path helpers for MCP tools."""

from __future__ import annotations

from pathlib import Path

from workspace.host_paths import (
    is_configured_container_host_path,
    translate_posix_host_path_for_container,
    translate_windows_host_path_for_container,
)
from workspace.path_resolver import (
    is_path_under_project_root,
    resolve_legacy_slash_project_path,
    to_project_relative_path,
)

APP_ROOT = Path(__file__).resolve().parent.parent


def resolve_workspace_path(raw_path: str | Path) -> Path:
    """Resolve file-tool paths under the project root or configured host mounts."""
    raw = str(raw_path or "").strip()
    if not raw:
        return APP_ROOT

    translated_raw = translate_windows_host_path_for_container(raw)
    if translated_raw != raw:
        return Path(translated_raw).resolve()

    app_root = APP_ROOT.resolve()
    if is_path_under_project_root(raw, app_root):
        return Path(raw).resolve()

    if is_configured_container_host_path(raw):
        return Path(raw).resolve()

    if raw.startswith("/"):
        project_path = resolve_legacy_slash_project_path(raw, app_root)
        if project_path is not None:
            return project_path

        translated_raw = translate_posix_host_path_for_container(raw)
        if translated_raw != raw:
            return Path(translated_raw).resolve()

        return Path(raw).resolve()

    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    return (app_root / candidate).resolve()


def to_workspace_virtual_path(path: str | Path | None) -> str | None:
    """Return a project-relative path that FilesystemMiddleware can read."""
    if path is None:
        return None
    project_relative_path = to_project_relative_path(path, APP_ROOT)
    return project_relative_path if project_relative_path is not None else str(Path(path).resolve())
