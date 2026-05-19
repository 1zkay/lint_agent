#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def configure_stdio_utf8() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def safe_print(text: str, *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    if not text:
        return
    payload = (text + "\n").encode("utf-8", errors="replace")
    stream.buffer.write(payload)
    stream.flush()


def resolve_paths() -> tuple[Path, Path, Path]:
    script_path = Path(__file__).resolve()
    scripts_dir = script_path.parent
    skill_dir = scripts_dir.parent
    project_root = skill_dir.parent.parent
    tool = scripts_dir / "vendor" / "trace_constant_propagation.py"
    if not tool.exists():
        raise SystemExit(f"missing detector script: {tool}")
    return skill_dir, project_root, tool


def build_output_dir(report_root: Path) -> Path:
    prefix = "constant_propagation"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = report_root / f"{prefix}_{timestamp}"
    suffix = 1
    while output_dir.exists():
        output_dir = report_root / f"{prefix}_{timestamp}_{suffix:02d}"
        suffix += 1
    return output_dir


def build_diagnosis_bundle(
    *,
    output_dir: Path,
    output_path: Path,
    top_module: str,
    inputs: list[str],
) -> Path:
    bundle_path = output_dir / "diagnosis_bundle.json"
    bundle = {
        "bundle_type": "constant_propagation_final_diagnosis_inputs",
        "bundle_format": "json",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "top_module": top_module,
        "design_inputs": [str(Path(item).resolve()) for item in inputs],
        "artifacts": {
            "report_json": str(output_path.resolve()),
        },
        "final_diagnosis_required_inputs": [
            "json_report",
            "source_code",
        ],
        "final_diagnosis_optional_audit_inputs": [],
        "final_diagnosis_workflow": [
            "先读取 JSON 报告，重点查看“最源头常量引脚和线网”“按根源分组的污染常量集合”“层次化常量输出”。",
            "回到源代码，阅读根源、污染信号和相关模块附近的源码语义。",
            "结合命名、注释、配置意图和控制/数据路径语义，判断这是设计预期常量还是疑似真实缺陷。",
        ],
    }
    bundle_path.write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return bundle_path


def main() -> int:
    configure_stdio_utf8()
    parser = argparse.ArgumentParser(
        description="run hierarchical constant-propagation trace and write results under the project reports directory"
    )
    parser.add_argument("inputs", nargs="+", help="source files or source directories")
    parser.add_argument("--top", required=True, help="top module name")
    parser.add_argument(
        "--yosys",
        default=None,
        help="optional Yosys executable path forwarded to the detector",
    )
    args = parser.parse_args()

    skill_dir, project_root, tool = resolve_paths()
    report_root = project_root / "reports"
    output_dir = build_output_dir(report_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "trace_constant_propagation_report.json"

    cmd = [
        sys.executable,
        str(tool),
        *args.inputs,
        "--top",
        args.top,
        "--output",
        str(output_path),
    ]
    if args.yosys:
        cmd.extend(["--yosys", args.yosys])
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        cmd,
        cwd=str(project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        env=env,
    )

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if stdout:
        safe_print(stdout.rstrip())
    if stderr:
        safe_print(stderr.rstrip(), err=True)

    bundle_path: Path | None = None
    if output_path.exists():
        bundle_path = build_diagnosis_bundle(
            output_dir=output_dir,
            output_path=output_path,
            top_module=args.top,
            inputs=list(args.inputs),
        )

    safe_print(f"SKILL_DIR={skill_dir}")
    safe_print(f"REPORT_DIR={output_dir}")
    safe_print(f"REPORT_PATH={output_path}")
    if bundle_path is not None:
        safe_print(f"BUNDLE_PATH={bundle_path}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
