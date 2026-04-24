"""ALINT-PRO batch execution helpers."""

from __future__ import annotations

import asyncio
import os
import platform
import shlex
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from config import config
from workspace.project_utils import get_project_report_dir


@dataclass(frozen=True)
class AlintBatchResult:
    """Result of one ALINT-PRO batch run."""

    success: bool
    error: str | None = None
    workspace_path: Path | None = None
    output_dir: Path | None = None
    csv_path: Path | None = None
    command: list[str] = field(default_factory=list)
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""


def _find_workspace_file(workspace_path: str | Path, *, logger: Any = None) -> Path | None:
    workspace = Path(workspace_path).resolve()
    if workspace.exists():
        return workspace

    if logger is not None:
        logger.warning("Workspace not found at: %s, searching parent...", workspace)

    parent_dir = workspace.parent.parent if workspace.parent else None
    if not parent_dir or not parent_dir.exists():
        return None

    for root, _, files in os.walk(parent_dir):
        if workspace.name in files:
            candidate = Path(root) / workspace.name
            if candidate.exists():
                return candidate.resolve()
    return None


def _build_alint_do_lines(workspace: Path, project_name: str, out_csv: Path) -> list[str]:
    return [
        f"workspace.open {{{workspace}}}",
        f"project.clean -project {{{project_name}}}",
        f"project.run -project {{{project_name}}}",
        f"project.lint -project {{{project_name}}}",
        f"project.report.violations -project {{{project_name}}} -format csv -report {{{out_csv}}} -max_details 0",
        "exit",
    ]


async def run_alint_batch(
    workspace_path: str | Path,
    project_name: str,
    *,
    output_name: str | None = None,
    search_missing_workspace: bool = False,
    timeout_seconds: int = 900,
    logger: Any = None,
) -> AlintBatchResult:
    """Run ALINT-PRO in batch mode and export a CSV violation report."""
    if platform.system() != "Windows":
        return AlintBatchResult(success=False, error="ALINT-PRO only runs on Windows.")

    if search_missing_workspace:
        workspace = _find_workspace_file(workspace_path, logger=logger)
    else:
        workspace = Path(workspace_path).resolve()
        if not workspace.exists():
            workspace = None
    if workspace is None:
        return AlintBatchResult(
            success=False,
            error=f"Workspace not found: {workspace_path}",
        )

    alint_exe = Path(config.alint_exe).resolve()
    if not alint_exe.exists():
        return AlintBatchResult(
            success=False,
            workspace_path=workspace,
            error=f"ALINT executable not found: {alint_exe}",
        )

    out_dir = get_project_report_dir(project_name)
    if not output_name:
        output_name = f"alint_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    out_csv = (out_dir / output_name).resolve()

    tmp_do = out_dir / f"_alint_batch_{datetime.now().strftime('%H%M%S')}.do"
    tmp_do.write_text(
        "\n".join(_build_alint_do_lines(workspace, project_name, out_csv)),
        encoding="utf-8",
    )

    cmd = [str(alint_exe), "-batch", "-do", str(tmp_do)]
    if logger is not None:
        logger.info("Running ALINT: %s", shlex.join(cmd))

    env = os.environ.copy()
    license_file = alint_exe.parent.parent / "license.dat"
    if license_file.exists():
        env["ALDEC_LICENSE_FILE"] = str(license_file)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(alint_exe.parent),
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            with suppress(Exception):
                await proc.wait()
            return AlintBatchResult(
                success=False,
                workspace_path=workspace,
                output_dir=out_dir,
                csv_path=out_csv,
                command=cmd,
                error="ALINT analysis timed out (exceeded 15 minutes).",
            )
    finally:
        with suppress(Exception):
            tmp_do.unlink(missing_ok=True)

    rc = proc.returncode
    out = (stdout or b"").decode(errors="replace")
    err = (stderr or b"").decode(errors="replace")

    if rc == 0 and out_csv.exists():
        return AlintBatchResult(
            success=True,
            workspace_path=workspace,
            output_dir=out_dir,
            csv_path=out_csv,
            command=cmd,
            returncode=rc,
            stdout=out,
            stderr=err,
        )

    return AlintBatchResult(
        success=False,
        workspace_path=workspace,
        output_dir=out_dir,
        csv_path=out_csv,
        command=cmd,
        returncode=rc,
        stdout=out,
        stderr=err,
        error=f"ALINT analysis failed (exit code: {rc})\nStderr:\n{err}\nStdout:\n{out}",
    )
