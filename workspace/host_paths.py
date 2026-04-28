"""Host path helpers for native Windows and Docker customer deployments."""

from __future__ import annotations

import os
from pathlib import PurePosixPath, PureWindowsPath


def configured_host_drive_mount_root() -> str:
    """Return the POSIX mount root used for host drive mappings."""
    return os.getenv("ALINT_HOST_DRIVE_MOUNT_ROOT", "").strip()


def configured_posix_host_source_root() -> str:
    """Return the POSIX host root that is bind-mounted into the container."""
    return os.getenv("ALINT_HOST_POSIX_SOURCE_ROOT", "").strip()


def configured_posix_host_mount_root() -> str:
    """Return the container mount root for POSIX host paths."""
    return os.getenv("ALINT_HOST_POSIX_MOUNT_ROOT", "").strip()


def translate_windows_host_path_for_container(path: str) -> str:
    """Map Windows drive paths to Docker bind-mount paths when configured."""
    text = str(path or "").strip()
    if not text or os.name == "nt":
        return text

    mount_root = configured_host_drive_mount_root()
    if not mount_root:
        return text

    windows_path = PureWindowsPath(text)
    drive = windows_path.drive.rstrip(":")
    if len(drive) != 1 or not windows_path.is_absolute():
        return text

    return PurePosixPath(mount_root, drive.lower(), *windows_path.parts[1:]).as_posix()


def translate_posix_host_path_for_container(path: str) -> str:
    """Map POSIX host absolute paths to Docker bind-mount paths when configured."""
    text = str(path or "").strip()
    if not text or os.name == "nt" or not text.startswith("/"):
        return text

    source_root = configured_posix_host_source_root()
    mount_root = configured_posix_host_mount_root()
    if not source_root or not mount_root:
        return text
    for container_root in (configured_host_drive_mount_root(), mount_root):
        normalized_root = PurePosixPath(container_root).as_posix().rstrip("/") if container_root else ""
        if normalized_root and (text == normalized_root or text.startswith(f"{normalized_root}/")):
            return text

    source_path = PurePosixPath(source_root)
    input_path = PurePosixPath(text)
    try:
        relative_path = input_path.relative_to(source_path)
    except ValueError:
        return text
    return PurePosixPath(mount_root, relative_path).as_posix()


def is_configured_container_host_path(path: str) -> bool:
    """Return true when a POSIX path is under a configured host mount."""
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return False

    mount_roots = [
        configured_host_drive_mount_root(),
        configured_posix_host_mount_root(),
    ]
    for mount_root in mount_roots:
        if not mount_root:
            continue
        normalized_root = PurePosixPath(mount_root).as_posix().rstrip("/")
        if not normalized_root or normalized_root == ".":
            continue
        if text == normalized_root or text.startswith(f"{normalized_root}/"):
            return True
    return False
