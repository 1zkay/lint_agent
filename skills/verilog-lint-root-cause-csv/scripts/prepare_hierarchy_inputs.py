#!/usr/bin/env python3
"""Stage a Verilog source archive or directory and run the hierarchy mapper."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from _filelist import (
    FilelistInputs,
    HEADER_SUFFIXES,
    SOURCE_SUFFIXES,
    choose_filelist,
    parse_filelist,
    quote_filelist_path,
    unique_paths,
)


LINT_AGENT_ROOT = Path(__file__).resolve().parents[3]
REPORTS_ROOT = LINT_AGENT_ROOT / "reports"
MAPPER = Path(__file__).with_name("lint_hierarchy_mapper.py")
ARCHIVE_SUFFIXES = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".zip",
    ".7z",
    ".rar",
    ".cab",
)
WRAPPER_IGNORE_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__MACOSX",
    "__pycache__",
}


def safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return name or "project"


def _validate_archive_member(name: str, target: Path) -> None:
    target_root = target.resolve()
    normalized = name.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(name)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or ".." in posix_path.parts
    ):
        raise ValueError(f"unsafe archive path: {name}")
    destination = (target / normalized).resolve()
    if destination != target_root and not str(destination).startswith(
        str(target_root) + os.sep
    ):
        raise ValueError(f"unsafe archive path: {name}")


def _extract_with_7z(archive: Path, target: Path) -> None:
    executable = shutil.which("7z") or shutil.which("7zz")
    if executable is None:
        raise RuntimeError(f"7z is required to extract {archive.suffix} archives")

    listing = subprocess.run(
        [executable, "l", "-slt", str(archive)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if listing.returncode:
        raise RuntimeError(f"failed to inspect archive: {listing.stderr[-2000:]}")

    in_entries = False
    entry_count = 0
    for line in listing.stdout.splitlines():
        if line.startswith("----------"):
            in_entries = True
        elif in_entries and line.startswith("Path = "):
            _validate_archive_member(line.removeprefix("Path = "), target)
            entry_count += 1
    if not entry_count:
        raise ValueError(f"archive contains no entries: {archive}")

    result = subprocess.run(
        [executable, "x", "-y", f"-o{target}", str(archive)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"failed to extract archive: {result.stderr[-2000:]}")


def _validate_extracted_tree(target: Path) -> None:
    target_root = target.resolve()
    for path in target.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"archive symlinks are not supported: {path}")
        resolved = path.resolve()
        if resolved != target_root and not str(resolved).startswith(
            str(target_root) + os.sep
        ):
            raise ValueError(f"unsafe extracted path: {path}")


def safe_extract(archive: Path, target: Path) -> None:
    lower_name = archive.name.lower()
    if lower_name.endswith(
        (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
    ):
        with tarfile.open(archive) as handle:
            for member in handle.getmembers():
                _validate_archive_member(member.name, target)
            handle.extractall(target, filter="data")
    elif lower_name.endswith(".zip"):
        with zipfile.ZipFile(archive) as handle:
            for name in handle.namelist():
                _validate_archive_member(name, target)
            handle.extractall(target)
    elif lower_name.endswith((".7z", ".rar", ".cab")):
        _extract_with_7z(archive, target)
    else:
        raise ValueError(
            f"unsupported source archive: {archive}; supported suffixes: {ARCHIVE_SUFFIXES}"
        )
    _validate_extracted_tree(target)


def _unwrap_source_root(source: Path) -> Path:
    """Remove one directory-only archive wrapper without assuming an RTL layout."""

    entries = [
        entry for entry in source.iterdir() if entry.name not in WRAPPER_IGNORE_NAMES
    ]
    if any(entry.is_file() for entry in entries):
        return source
    child_directories = [entry for entry in entries if entry.is_dir()]
    return child_directories[0] if len(child_directories) == 1 else source


def _copy_source_tree(
    source: Path,
    target: Path,
    *,
    ignored_top_level: set[str] | None = None,
) -> None:
    source_root = source.resolve()
    ignored_top_level = ignored_top_level or set()

    def ignore(_directory: str, names: list[str]) -> set[str]:
        ignored = set(names) & WRAPPER_IGNORE_NAMES
        if Path(_directory).resolve() == source_root:
            ignored.update(set(names) & ignored_top_level)
        if REPORTS_ROOT.name in names:
            candidate = (Path(_directory) / REPORTS_ROOT.name).resolve()
            if candidate == REPORTS_ROOT.resolve():
                ignored.add(REPORTS_ROOT.name)
        return ignored

    shutil.copytree(source, target, dirs_exist_ok=True, ignore=ignore)


def _write_generated_filelist(rtl_dir: Path, filelist_path: Path) -> None:
    source_files = sorted(
        path
        for path in rtl_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES
    )
    if not source_files:
        raise FileNotFoundError(f"no Verilog source files found under {rtl_dir}")

    header_files = (
        path
        for path in rtl_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in HEADER_SUFFIXES
    )
    include_dirs = sorted({path.parent for path in [*source_files, *header_files]})
    lines = [
        f"-I {quote_filelist_path(path.relative_to(filelist_path.parent).as_posix())}"
        for path in include_dirs
    ]
    lines.extend(
        quote_filelist_path(path.relative_to(filelist_path.parent).as_posix())
        for path in source_files
    )
    filelist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_staged_filelist(
    inputs: FilelistInputs,
    source_root: Path,
    rtl_dir: Path,
    filelist_path: Path,
) -> None:
    source_root = source_root.resolve()

    def staged_path(path: Path, kind: str) -> Path:
        try:
            relative = path.resolve().relative_to(source_root)
        except ValueError as exc:
            raise ValueError(f"filelist {kind} is outside source tree: {path}") from exc
        staged = rtl_dir / relative
        if not staged.exists():
            raise FileNotFoundError(f"staged filelist {kind} not found: {staged}")
        return staged

    include_dirs = [
        staged_path(path, "include directory")
        for path in unique_paths([*inputs.incdirs, *inputs.filelist_dirs])
    ]
    source_files = [staged_path(path, "source") for path in inputs.files]
    if not source_files:
        raise FileNotFoundError(f"no Verilog sources found in {source_root}")
    lines = [
        f"-I {quote_filelist_path(path.relative_to(filelist_path.parent).as_posix())}"
        for path in include_dirs
    ]
    lines.extend(f"-D {quote_filelist_path(define)}" for define in inputs.defines)
    if inputs.top:
        lines.append(f"-top {quote_filelist_path(inputs.top)}")
    lines.extend(
        quote_filelist_path(path.relative_to(filelist_path.parent).as_posix())
        for path in source_files
    )
    filelist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def stage_source(source: Path, run_dir: Path, *, unwrap_wrapper: bool = False) -> None:
    source_root = _unwrap_source_root(source) if unwrap_wrapper else source
    source_filelist = choose_filelist(source_root)
    filelist_inputs = parse_filelist(source_filelist) if source_filelist else None
    rtl_dir = run_dir / "rtl"
    generated_outputs = (
        {"slices", "work"}
        if (source_root / "rtl").is_dir()
        and (source_root / "filelist.f").is_file()
        else set()
    )
    _copy_source_tree(
        source_root,
        rtl_dir,
        ignored_top_level=generated_outputs,
    )
    filelist_path = run_dir / "filelist.f"
    if filelist_inputs is not None:
        _write_staged_filelist(
            filelist_inputs,
            source_root,
            rtl_dir,
            filelist_path,
        )
    else:
        _write_generated_filelist(rtl_dir, filelist_path)


def create_run_directory(project_name: str) -> tuple[Path, str]:
    REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
    while True:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = REPORTS_ROOT / f"{project_name}_{stamp}"
        try:
            run_dir.mkdir()
        except FileExistsError:
            time.sleep(0.05)
            continue
        return run_dir, stamp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage a source archive or directory and map its lint CSV onto the hierarchy."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-archive", type=Path)
    source.add_argument("--source-dir", type=Path)
    parser.add_argument("--lint-report", type=Path, required=True)
    parser.add_argument("--top")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_input = (args.source_archive or args.source_dir).expanduser().resolve()
    lint_report = args.lint_report.expanduser().resolve()
    if args.source_archive and not source_input.is_file():
        raise FileNotFoundError(source_input)
    if args.source_dir and not source_input.is_dir():
        raise FileNotFoundError(source_input)
    for path in (lint_report, MAPPER):
        if not path.is_file():
            raise FileNotFoundError(path)

    project_name = safe_name(lint_report.stem)
    run_dir, stamp = create_run_directory(project_name)
    report_path = REPORTS_ROOT / f"{project_name}_root_cause_{stamp}.csv"
    extracted = run_dir / ".source"
    work_dir = run_dir / "work"
    try:
        if args.source_archive:
            extracted.mkdir()
            safe_extract(source_input, extracted)
            stage_source(extracted, run_dir, unwrap_wrapper=True)
        else:
            stage_source(source_input, run_dir)

        command = [
            sys.executable,
            str(MAPPER),
            "--csv",
            str(lint_report),
            "--source",
            str(run_dir),
            "--out-dir",
            str(work_dir),
            "--keep-work",
        ]
        if args.top:
            command.extend(["--top", args.top])
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"hierarchy mapper failed:\n{result.stderr[-2000:]}")
        shutil.rmtree(work_dir / "_work", ignore_errors=True)
        if extracted.exists():
            shutil.rmtree(extracted)
    except Exception as exc:
        shutil.rmtree(extracted, ignore_errors=True)
        raise RuntimeError(f"{exc}\nfailed run retained: {run_dir}") from exc

    print(f"PROJECT_NAME={project_name}")
    print(f"RUN_DIR={run_dir}")
    print(f"REPORT_PATH={report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
