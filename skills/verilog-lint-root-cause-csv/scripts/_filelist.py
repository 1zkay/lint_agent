"""Shared Verilog filelist discovery and parsing helpers."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


FILELIST_SUFFIXES = {".f", ".flist"}
SOURCE_SUFFIXES = {".v", ".sv"}
HEADER_SUFFIXES = {".vh", ".svh"}
DEFAULT_LIBRARY_EXTENSIONS = {".v", ".sv"}


@dataclass
class FilelistInputs:
    """Ordered inputs collected from one filelist tree."""

    files: list[Path] = field(default_factory=list)
    incdirs: list[Path] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    filelist_dirs: list[Path] = field(default_factory=list)
    library_dirs: list[Path] = field(default_factory=list)
    library_extensions: list[str] = field(default_factory=list)
    top: str | None = None

    def extend(self, other: "FilelistInputs") -> None:
        self.files.extend(other.files)
        self.incdirs.extend(other.incdirs)
        self.defines.extend(other.defines)
        self.filelist_dirs.extend(other.filelist_dirs)
        self.library_dirs.extend(other.library_dirs)
        self.library_extensions.extend(other.library_extensions)
        if other.top:
            self.set_top(other.top)

    def set_top(self, top: str) -> None:
        if not top:
            raise ValueError("filelist top module is empty")
        if self.top and self.top != top:
            raise ValueError(f"conflicting filelist top modules: {self.top}, {top}")
        self.top = top


def unique_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def unique_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def choose_filelist(source_root: Path) -> Path | None:
    """Choose one conventional project filelist or reject an ambiguous choice."""

    name_priority = {"filelist.f": 0, "files.f": 1, "rtl.f": 2}
    candidates = [
        path
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix.lower() in FILELIST_SUFFIXES
    ]
    if not candidates:
        return None

    def rank(path: Path) -> tuple[int, int]:
        return (
            len(path.relative_to(source_root).parts),
            name_priority.get(path.name.lower(), 3),
        )

    best_rank = min(rank(path) for path in candidates)
    best = sorted(
        (path for path in candidates if rank(path) == best_rank),
        key=lambda path: path.relative_to(source_root).as_posix(),
    )
    if len(best) > 1:
        paths = ", ".join(path.relative_to(source_root).as_posix() for path in best)
        raise ValueError(f"ambiguous project filelist; candidates: {paths}")
    return best[0]


def quote_filelist_path(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _strip_comment(line: str) -> str:
    in_quote = False
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == '"':
            in_quote = not in_quote
            continue
        if not in_quote and character == "#":
            return line[:index]
        if not in_quote and line[index : index + 2] == "//":
            return line[:index]
    return line


def _logical_lines(path: Path) -> list[str]:
    logical_lines: list[str] = []
    pending = ""
    for physical_line in path.read_text(
        encoding="utf-8-sig",
        errors="replace",
    ).splitlines():
        stripped = physical_line.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        logical_lines.append(pending + physical_line)
        pending = ""
    if pending:
        logical_lines.append(pending)
    return logical_lines


def _tokens(path: Path) -> list[str]:
    tokens: list[str] = []
    for line in _logical_lines(path):
        text = _strip_comment(line).strip()
        if text:
            tokens.extend(shlex.split(text, comments=False, posix=True))
    return tokens


def _resolve_path(base: Path, token: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(token))
    path = Path(expanded)
    return (path if path.is_absolute() else base / path).resolve()


def _split_plus_values(value: str) -> list[str]:
    return [item for item in value.split("+") if item]


def _normalize_library_extension(value: str) -> str:
    return value if value.startswith(".") else f".{value}"


def _parse_filelist(path: Path, seen: set[Path]) -> FilelistInputs:
    filelist = path.resolve()
    if filelist in seen:
        return FilelistInputs()
    if not filelist.is_file():
        raise FileNotFoundError(f"filelist not found: {filelist}")
    seen.add(filelist)

    result = FilelistInputs()
    base = filelist.parent
    result.filelist_dirs.append(base)
    tokens = _tokens(filelist)
    index = 0
    options_with_ignored_value = {
        "-l",
        "-L",
        "-o",
        "-P",
        "-timescale",
        "-work",
    }

    def next_value(option: str) -> str:
        nonlocal index
        if index >= len(tokens):
            raise ValueError(f"{filelist}: {option} requires a value")
        value = tokens[index]
        index += 1
        return value

    while index < len(tokens):
        token = tokens[index]
        index += 1

        if token.startswith("+incdir+"):
            result.incdirs.extend(
                _resolve_path(base, value)
                for value in _split_plus_values(token[len("+incdir+") :])
            )
        elif token.startswith("+define+"):
            result.defines.extend(
                _split_plus_values(token[len("+define+") :])
            )
        elif token.startswith("+libext+"):
            result.library_extensions.extend(
                _normalize_library_extension(value)
                for value in _split_plus_values(token[len("+libext+") :])
            )
        elif token in {"-I", "-incdir"}:
            result.incdirs.append(_resolve_path(base, next_value(token)))
        elif token.startswith("-I") and len(token) > 2:
            result.incdirs.append(_resolve_path(base, token[2:]))
        elif token in {"-D", "+define"}:
            result.defines.append(next_value(token))
        elif token.startswith("-D") and len(token) > 2:
            result.defines.append(token[2:])
        elif token in {"-f", "-F"}:
            nested = _resolve_path(base, next_value(token))
            result.extend(_parse_filelist(nested, seen))
        elif token in {"-s", "-top", "--top", "--top-module"}:
            result.set_top(next_value(token))
        elif token.startswith(("-top=", "--top=", "--top-module=")):
            result.set_top(token.partition("=")[2])
        elif token == "-v":
            result.files.append(_resolve_path(base, next_value(token)))
        elif token == "-y":
            result.library_dirs.append(_resolve_path(base, next_value(token)))
        elif token in options_with_ignored_value:
            next_value(token)
        elif token.startswith("-") or token.startswith("+"):
            continue
        elif Path(token).suffix.lower() in SOURCE_SUFFIXES:
            result.files.append(_resolve_path(base, token))

    return result


def parse_filelist(filelist: Path) -> FilelistInputs:
    """Parse common Verilog filelist constructs while preserving source order."""

    result = _parse_filelist(filelist, set())
    extensions = {
        *DEFAULT_LIBRARY_EXTENSIONS,
        *result.library_extensions,
    }
    for library_dir in unique_paths(result.library_dirs):
        if not library_dir.is_dir():
            raise FileNotFoundError(f"library directory not found: {library_dir}")
        result.files.extend(
            path
            for path in sorted(library_dir.iterdir())
            if path.is_file() and path.suffix.lower() in extensions
        )

    result.files = unique_paths(result.files)
    result.incdirs = unique_paths(result.incdirs)
    result.defines = unique_strings(result.defines)
    result.filelist_dirs = unique_paths(result.filelist_dirs)
    result.library_dirs = unique_paths(result.library_dirs)
    result.library_extensions = unique_strings(result.library_extensions)
    for source_file in result.files:
        if not source_file.is_file():
            raise FileNotFoundError(f"filelist source not found: {source_file}")
    for incdir in result.incdirs:
        if not incdir.is_dir():
            raise FileNotFoundError(
                f"filelist include directory not found: {incdir}"
            )
    return result
