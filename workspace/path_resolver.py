"""Shared project-relative path helpers."""

from __future__ import annotations

from pathlib import Path

PROJECT_RELATIVE_ROOTS = frozenset(
    {
        ".files",
        ".langgraph_api",
        "feedback",
        "reports",
        "skills",
    }
)


def to_project_relative_path(path: str | Path, project_root: str | Path) -> str | None:
    """Return a POSIX project-relative path when path is under project_root."""
    resolved = Path(path).resolve()
    root_path = Path(project_root).resolve()
    try:
        relative_path = resolved.relative_to(root_path)
    except ValueError:
        return None
    return "." if not relative_path.parts else relative_path.as_posix()


def is_path_under_project_root(path: str | Path, project_root: str | Path) -> bool:
    """Return true when a native absolute path points inside project_root."""
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return False

    root_text = Path(project_root).resolve().as_posix().rstrip("/")
    return text == root_text or text.startswith(f"{root_text}/")


def resolve_legacy_slash_project_path(path: str | Path, project_root: str | Path) -> Path | None:
    """Resolve old `/.files/...` style paths to project-relative locations.

    A bare `/` is intentionally not treated as the project root. Use `.` for
    the project root; `/` remains a native POSIX absolute path.
    """
    text = str(path or "").strip().replace("\\", "/")
    if not text.startswith("/") or text == "/" or text.startswith("//"):
        return None

    relative_text = text[1:]
    first_part = relative_text.split("/", 1)[0]
    if first_part not in PROJECT_RELATIVE_ROOTS:
        return None

    return (Path(project_root).resolve() / relative_text).resolve()
