"""Shared Verilog filelist discovery and parsing helpers."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


FILELIST_SUFFIXES = {".f", ".flist"}
SOURCE_SUFFIXES = {".v", ".sv"}
HEADER_SUFFIXES = {".vh", ".svh"}
FILELIST_SOURCE_SUFFIXES = {*SOURCE_SUFFIXES, *HEADER_SUFFIXES, ".h"}
DEFAULT_LIBRARY_EXTENSIONS = {".v", ".sv"}


@dataclass
class FilelistInputs:
    """Ordered inputs collected from one filelist tree."""

    files: list[Path] = field(default_factory=list)
    library_files: list[Path] = field(default_factory=list)
    incdirs: list[Path] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    library_dirs: list[Path] = field(default_factory=list)
    library_extensions: list[str] = field(default_factory=list)
    language_standard: str | None = None
    single_unit: bool = False
    top: str | None = None

    def extend(self, other: "FilelistInputs") -> None:
        self.files.extend(other.files)
        self.library_files.extend(other.library_files)
        self.incdirs.extend(other.incdirs)
        self.defines.extend(other.defines)
        self.library_dirs.extend(other.library_dirs)
        self.library_extensions.extend(other.library_extensions)
        if other.language_standard:
            self.language_standard = other.language_standard
        self.single_unit = self.single_unit or other.single_unit
        if other.top:
            self.set_top(other.top)

    def set_top(self, top: str) -> None:
        if not top:
            raise ValueError("filelist top module is empty")
        if self.top and self.top != top:
            raise ValueError(f"conflicting filelist top modules: {self.top}, {top}")
        self.top = top

    def source_files(self) -> list[Path]:
        """Return primary and library sources for source-level inspection."""

        extensions = {
            *DEFAULT_LIBRARY_EXTENSIONS,
            *self.library_extensions,
        }
        result = [*self.files, *self.library_files]
        for library_dir in self.library_dirs:
            result.extend(
                path
                for path in sorted(library_dir.iterdir())
                if path.is_file() and path.suffix.lower() in extensions
            )
        return unique_paths(result)


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


def _expand_environment(value: str) -> str:
    value = re.sub(
        r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        value,
    )
    return os.path.expandvars(value)


def _strip_comments(text: str) -> str:
    result: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        if escaped:
            result.append(character)
            escaped = False
            index += 1
            continue
        if character == "\\":
            result.append(character)
            escaped = True
            index += 1
            continue
        if quote:
            result.append(character)
            if character == quote:
                quote = None
            index += 1
            continue
        if character in {'"', "'"}:
            quote = character
            result.append(character)
            index += 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated block comment in filelist")
            result.extend(
                "\n"
                for character in text[index : end + 2]
                if character == "\n"
            )
            index = end + 2
            continue
        if character == "#" or (
            text.startswith("//", index)
            and (index == 0 or text[index - 1].isspace())
        ):
            end = text.find("\n", index)
            if end < 0:
                break
            result.append("\n")
            index = end + 1
            continue
        result.append(character)
        index += 1
    return "".join(result)


def _tokens(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return [
        _expand_environment(token)
        for token in shlex.split(_strip_comments(text), comments=False, posix=True)
    ]


def _resolve_path(base: Path, token: str) -> Path:
    expanded = os.path.expanduser(token)
    path = Path(expanded)
    return (path if path.is_absolute() else base / path).resolve()


def _split_plus_values(value: str) -> list[str]:
    return [item for item in value.split("+") if item]


def _normalize_library_extension(value: str) -> str:
    return value if value.startswith(".") else f".{value}"


def _parse_filelist(
    path: Path,
    active: set[Path],
    *,
    working_dir: Path,
    path_base: Path,
) -> FilelistInputs:
    filelist = path.resolve()
    if filelist in active:
        raise ValueError(f"cyclic filelist include: {filelist}")
    if not filelist.is_file():
        raise FileNotFoundError(f"filelist not found: {filelist}")
    active.add(filelist)

    result = FilelistInputs()
    try:
        tokens = _tokens(filelist)
        index = 0

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
                    _resolve_path(path_base, value)
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
                result.incdirs.append(_resolve_path(path_base, next_value(token)))
            elif token.startswith("-I") and len(token) > 2:
                result.incdirs.append(_resolve_path(path_base, token[2:]))
            elif token in {"-D", "+define"}:
                result.defines.append(next_value(token))
            elif token.startswith("-D") and len(token) > 2:
                result.defines.append(token[2:])
            elif token == "-F":
                nested = _resolve_path(path_base, next_value(token))
                result.extend(
                    _parse_filelist(
                        nested,
                        active,
                        working_dir=working_dir,
                        path_base=nested.parent,
                    )
                )
            elif token == "-f":
                nested = _resolve_path(working_dir, next_value(token))
                result.extend(
                    _parse_filelist(
                        nested,
                        active,
                        working_dir=working_dir,
                        path_base=working_dir,
                    )
                )
            elif token in {"-s", "-top", "--top", "--top-module"}:
                result.set_top(next_value(token))
            elif token.startswith(("-top=", "--top=", "--top-module=")):
                result.set_top(token.partition("=")[2])
            elif token == "--std":
                result.language_standard = next_value(token)
            elif token.startswith("--std="):
                result.language_standard = token.partition("=")[2]
            elif token == "--single-unit":
                result.single_unit = True
            elif token == "-v":
                result.library_files.append(
                    _resolve_path(path_base, next_value(token))
                )
            elif token == "-y":
                result.library_dirs.append(
                    _resolve_path(path_base, next_value(token))
                )
            elif token.startswith("-") or token.startswith("+"):
                raise ValueError(f"{filelist}: unsupported filelist option: {token}")
            elif Path(token).suffix.lower() in FILELIST_SOURCE_SUFFIXES:
                result.files.append(_resolve_path(path_base, token))
            else:
                raise ValueError(f"{filelist}: unsupported filelist entry: {token}")
        return result
    finally:
        active.remove(filelist)


def parse_filelist(
    filelist: Path,
    *,
    working_dir: Path | None = None,
) -> FilelistInputs:
    """Parse a command file, treating the root file with ``-F`` semantics."""

    filelist = filelist.resolve()
    working_dir = (working_dir or filelist.parent).resolve()
    result = _parse_filelist(
        filelist,
        set(),
        working_dir=working_dir,
        path_base=filelist.parent,
    )

    result.files = unique_paths(result.files)
    result.library_files = unique_paths(result.library_files)
    result.incdirs = unique_paths(result.incdirs)
    result.defines = unique_strings(result.defines)
    result.library_dirs = unique_paths(result.library_dirs)
    result.library_extensions = unique_strings(result.library_extensions)
    for source_file in [*result.files, *result.library_files]:
        if not source_file.is_file():
            raise FileNotFoundError(f"filelist source not found: {source_file}")
    for incdir in result.incdirs:
        if not incdir.is_dir():
            raise FileNotFoundError(
                f"filelist include directory not found: {incdir}"
            )
    for library_dir in result.library_dirs:
        if not library_dir.is_dir():
            raise FileNotFoundError(f"library directory not found: {library_dir}")
    return result


def render_filelist(
    inputs: FilelistInputs,
    path_text: Callable[[Path], str],
    *,
    top: str,
) -> str:
    """Render the supported command-file contract without flattening libraries."""

    lines = [
        *(f"-I {quote_filelist_path(path_text(path))}" for path in inputs.incdirs),
        *(f"-D {quote_filelist_path(define)}" for define in inputs.defines),
    ]
    if inputs.language_standard:
        lines.append(f"--std {inputs.language_standard}")
    if inputs.single_unit:
        lines.append("--single-unit")
    lines.append(f"--top {quote_filelist_path(top)}")
    if inputs.library_extensions:
        lines.append("+libext+" + "+".join(inputs.library_extensions))
    lines.extend(
        f"-y {quote_filelist_path(path_text(path))}" for path in inputs.library_dirs
    )
    lines.extend(
        f"-v {quote_filelist_path(path_text(path))}" for path in inputs.library_files
    )
    lines.extend(quote_filelist_path(path_text(path)) for path in inputs.files)
    return "\n".join(lines) + "\n"
