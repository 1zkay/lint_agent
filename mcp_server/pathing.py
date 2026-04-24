"""Path helpers for MCP tools."""

from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent


def resolve_workspace_path(raw_path: str | Path) -> Path:
    """Resolve `/...` file-tool paths and relative paths under the project root."""
    raw = str(raw_path or "").strip()
    if not raw:
        return APP_ROOT

    app_root = APP_ROOT.resolve()
    app_root_str = str(app_root)
    app_root_posix = app_root.as_posix()
    if raw == app_root_str or raw.startswith(app_root_str + os.sep):
        return Path(raw).resolve()
    if raw == app_root_posix or raw.startswith(app_root_posix + "/"):
        return Path(raw).resolve()

    if raw.startswith("/"):
        return (app_root / raw.lstrip("/")).resolve()

    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    return (app_root / candidate).resolve()


def to_workspace_virtual_path(path: str | Path | None) -> str | None:
    """Return a `/...` path that FilesystemMiddleware can read when possible."""
    if path is None:
        return None
    resolved = Path(path).resolve()
    try:
        relative_path = resolved.relative_to(APP_ROOT.resolve())
    except ValueError:
        return str(resolved)
    return "/" if not relative_path.parts else "/" + relative_path.as_posix()
