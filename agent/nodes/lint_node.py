"""
Node 1: 运行 ALINT-PRO 分析，生成原始 CSV 报告

对应原 _run_alint_analysis_impl()
"""
import asyncio
import logging
import os
import platform
import shlex
from datetime import datetime
from pathlib import Path
from typing import Optional

from agent.state import AlintWorkflowState
from config import config
from utils import get_project_report_dir as _get_project_report_dir

logger = logging.getLogger(__name__)


async def run_lint_node(state: AlintWorkflowState) -> dict:
    """
    LangGraph 节点：运行 ALINT-PRO 并生成 CSV 报告

    输入 state 字段: workspace_path, project_name
    输出 state 字段: raw_csv_path, alint_output_dir  （或 error）
    """
    workspace_path: str = state["workspace_path"]
    project_name: str = state["project_name"]

    if platform.system() != "Windows":
        return {"error": "ALINT-PRO only runs on Windows."}

    ws = Path(workspace_path).resolve()

    # 尝试搜索工作区文件
    if not ws.exists():
        logger.warning(f"Workspace not found at: {ws}, searching parent...")
        parent_dir = ws.parent.parent if ws.parent else None
        found_ws: Optional[Path] = None
        if parent_dir and parent_dir.exists():
            for root, _, files in os.walk(parent_dir):
                if ws.name in files:
                    candidate = Path(root) / ws.name
                    if candidate.exists():
                        found_ws = candidate
                        break
        if found_ws:
            ws = found_ws
        else:
            return {"error": f"Workspace not found: {workspace_path}"}

    alint_exe = Path(config.alint_exe).resolve()
    if not alint_exe.exists():
        return {"error": f"ALINT executable not found: {alint_exe}"}

    out_dir = _get_project_report_dir(project_name)
    output_name = f"alint_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    out_csv = (out_dir / output_name).resolve()

    do_lines = [
        f"workspace.open {{{ws}}}",
        f"project.clean -project {{{project_name}}}",
        f"project.run -project {{{project_name}}}",
        f"project.lint -project {{{project_name}}}",
        f"project.report.violations -project {{{project_name}}} -format csv -report {{{out_csv}}} -max_details 0",
        "exit",
    ]
    tmp_do = out_dir / f"_alint_batch_{datetime.now().strftime('%H%M%S')}.do"
    tmp_do.write_text("\n".join(do_lines), encoding="utf-8")

    cmd = [str(alint_exe), "-batch", "-do", str(tmp_do)]
    logger.info(f"[lint_node] Running ALINT: {shlex.join(cmd)}")

    env = os.environ.copy()
    license_file = alint_exe.parent.parent / "license.dat"
    if license_file.exists():
        env["ALDEC_LICENSE_FILE"] = str(license_file)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(alint_exe.parent),
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "ALINT analysis timed out (exceeded 15 minutes)."}
    finally:
        try:
            tmp_do.unlink(missing_ok=True)
        except Exception:
            pass

    rc = proc.returncode
    out = (stdout or b"").decode(errors="replace")
    err = (stderr or b"").decode(errors="replace")

    if rc == 0 and out_csv.exists():
        logger.info(f"[lint_node] Report generated: {out_csv}")
        return {
            "raw_csv_path": str(out_csv),
            "alint_output_dir": str(out_dir),
        }
    else:
        return {
            "error": (
                f"ALINT analysis failed (exit code: {rc})\n"
                f"Stderr:\n{err}\nStdout:\n{out}"
            )
        }
