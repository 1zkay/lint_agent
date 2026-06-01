"""Shared Yosys discovery and runtime environment helpers."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Iterable, Iterator, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - fallback for bare script execution.
    def load_dotenv(path: Path, override: bool = False) -> None:
        if not path.exists():
            return
        for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and (override or key not in os.environ):
                os.environ[key] = value

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(ENV_FILE, override=False)

try:
    from config import config
except ImportError:  # pragma: no cover - keeps standalone scripts usable.
    config = None

YOSYS_BIN_CANDIDATES = ("yosys.exe", "yosys")
DEFAULT_OSS_ROOT = (PROJECT_ROOT / "oss-cad-suite").resolve()


@dataclass(frozen=True)
class YosysLocation:
    """Resolved Yosys executable and optional OSS CAD Suite root."""

    bin: Path
    root: Optional[Path] = None


def _env_path(name: str) -> str:
    value = os.getenv(name, "").strip()
    return value


def _configured_path(attr: str, *env_names: str) -> str:
    value = getattr(config, attr, "") if config is not None else ""
    value = str(value or "").strip()
    if value:
        return value
    for env_name in env_names:
        value = _env_path(env_name)
        if value:
            return value
    return ""


def _resolve_path(value: str) -> Path:
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _existing_file(path: Path) -> Optional[Path]:
    return path if path.exists() and path.is_file() else None


def _iter_root_candidates(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return

    for name in YOSYS_BIN_CANDIDATES:
        yield root / name
        yield root / "bin" / name
        yield root / ".local" / "bin" / name
        yield root / "oss-cad-suite" / "bin" / name


def _iter_recursive_candidates(root: Path) -> Iterator[Path]:
    if not root.exists() or not root.is_dir():
        return
    for name in YOSYS_BIN_CANDIDATES:
        try:
            yield from root.rglob(name)
        except OSError:
            continue


def _first_yosys_under(root: Path) -> Optional[Path]:
    seen: set[Path] = set()
    for candidate in chain(_iter_root_candidates(root), _iter_recursive_candidates(root)):
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        found = _existing_file(candidate)
        if found is not None:
            return found
    return None


def _looks_like_oss_cad_suite(root: Path, yosys_bin: Path) -> bool:
    try:
        if (root / "bin").resolve() != yosys_bin.parent.resolve():
            return False
    except OSError:
        return False
    markers = (
        root / "environment",
        root / "environment.bat",
        root / "environment.ps1",
        root / "environment.fish",
    )
    return root.name.lower() == "oss-cad-suite" or any(marker.exists() for marker in markers)


def _infer_oss_root(yosys_bin: Path, preferred_root: Optional[Path] = None) -> Optional[Path]:
    roots: list[Path] = []
    if preferred_root is not None and preferred_root.is_dir():
        roots.append(preferred_root)
    if yosys_bin.parent.name.lower() == "bin":
        roots.append(yosys_bin.parent.parent)

    seen: set[Path] = set()
    for root in roots:
        try:
            root = root.resolve()
        except OSError:
            continue
        if root in seen:
            continue
        seen.add(root)
        if _looks_like_oss_cad_suite(root, yosys_bin):
            return root
    return None


def _location_from_bin(path: Path, preferred_root: Optional[Path] = None) -> YosysLocation:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Configured Yosys executable not found: {resolved}")
    if not resolved.is_file():
        raise FileNotFoundError(f"Configured Yosys path is not a file: {resolved}")
    return YosysLocation(bin=resolved, root=_infer_oss_root(resolved, preferred_root))


def _location_from_root(root: Path) -> YosysLocation:
    resolved_root = root.expanduser().resolve()
    yosys_bin = _first_yosys_under(resolved_root)
    if yosys_bin is None:
        raise FileNotFoundError(f"Yosys executable not found under configured root: {resolved_root}")
    return YosysLocation(bin=yosys_bin, root=_infer_oss_root(yosys_bin, resolved_root))


def _iter_start_point_roots(start_points: Iterable[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for start in start_points:
        try:
            start = start.expanduser().resolve()
        except OSError:
            continue
        for parent in [start, *start.parents]:
            if parent in seen:
                continue
            seen.add(parent)
            yield parent / "oss-cad-suite"


def find_yosys(
    search_root: Optional[Path] = None,
    *,
    explicit_bin: Optional[str] = None,
    start_points: Optional[Iterable[Path]] = None,
) -> YosysLocation:
    """Resolve Yosys using explicit args, .env config, and conservative fallbacks."""
    if explicit_bin:
        return _location_from_bin(_resolve_path(explicit_bin))

    if search_root is not None:
        return _location_from_root(search_root)

    configured_bin = _configured_path("yosys_bin", "YOSYS_BIN")
    if configured_bin:
        return _location_from_bin(_resolve_path(configured_bin))

    configured_root = _configured_path("yosys_search_root", "YOSYS_SEARCH_ROOT")
    if configured_root:
        return _location_from_root(_resolve_path(configured_root))

    if DEFAULT_OSS_ROOT.exists():
        try:
            return _location_from_root(DEFAULT_OSS_ROOT)
        except FileNotFoundError:
            pass

    env_yosys = shutil.which("yosys")
    if env_yosys:
        return _location_from_bin(Path(env_yosys))

    if start_points:
        for root in _iter_start_point_roots(start_points):
            if not root.exists():
                continue
            try:
                return _location_from_root(root)
            except FileNotFoundError:
                continue

    raise FileNotFoundError(
        "Yosys executable not found. Set YOSYS_BIN to the executable path "
        "or YOSYS_SEARCH_ROOT to the directory that contains it."
    )


def build_yosys_env(location: YosysLocation) -> dict[str, str]:
    """Build process environment for either OSS CAD Suite or system-installed Yosys."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    paths = [str(location.bin.parent)]
    if location.root is not None:
        root_str = str(location.root)
        if not root_str.endswith(os.sep):
            root_str += os.sep
        paths.append(str(location.root / "lib"))
        env["YOSYSHQ_ROOT"] = root_str
        env.setdefault("SSL_CERT_FILE", str(location.root / "etc" / "cacert.pem"))
        env.setdefault("PYTHON_EXECUTABLE", str(location.root / "lib" / "python3.exe"))
        env.setdefault("QT_PLUGIN_PATH", str(location.root / "lib" / "qt5" / "plugins"))
        env.setdefault("QT_LOGGING_RULES", "*=false")
        env.setdefault("GTK_EXE_PREFIX", root_str)
        env.setdefault("GTK_DATA_PREFIX", root_str)
        env.setdefault(
            "GDK_PIXBUF_MODULEDIR",
            str(location.root / "lib" / "gdk-pixbuf-2.0" / "2.10.0" / "loaders"),
        )
        env.setdefault(
            "GDK_PIXBUF_MODULE_FILE",
            str(location.root / "lib" / "gdk-pixbuf-2.0" / "2.10.0" / "loaders.cache"),
        )

    if os.name != "nt":
        # OSS CAD Suite ships a shell wrapper with "#!/usr/bin/env bash".
        # Agent shell environments can have a very small PATH, so keep the
        # normal POSIX command locations available for env, bash, dirname, etc.
        paths.extend(
            path
            for path in ("/usr/local/bin", "/usr/bin", "/bin")
            if Path(path).exists()
        )

    current_path = env.get("PATH", "")
    if current_path:
        paths.extend(item for item in current_path.split(os.pathsep) if item)

    deduped_paths: list[str] = []
    seen_paths: set[str] = set()
    for path in paths:
        if path in seen_paths:
            continue
        seen_paths.add(path)
        deduped_paths.append(path)
    env["PATH"] = os.pathsep.join(deduped_paths)
    return env


def warn_missing_yosys(search_root: Optional[Path] = None) -> None:
    """Warn if Yosys cannot be resolved from the current configuration."""
    try:
        find_yosys(search_root)
    except FileNotFoundError as exc:
        print(f"[WARN] {exc}", file=sys.stderr)
