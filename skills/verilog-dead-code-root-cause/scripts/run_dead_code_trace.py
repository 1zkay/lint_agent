#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable


def configure_stdio_utf8() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def safe_print(text: str, *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    if not text:
        return
    stream.buffer.write((text + "\n").encode("utf-8", errors="replace"))
    stream.flush()


def resolve_paths() -> tuple[Path, Path]:
    script_path = Path(__file__).resolve()
    skill_dir = script_path.parent.parent
    mcp_alint_root = skill_dir.parent.parent
    return skill_dir, mcp_alint_root


def build_output_dir(report_root: Path) -> Path:
    prefix = "dead_code"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = report_root / f"{prefix}_{timestamp}"
    suffix = 1
    while output_dir.exists():
        output_dir = report_root / f"{prefix}_{timestamp}_{suffix:02d}"
        suffix += 1
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def candidate_yosys_paths(start_points: Iterable[Path]) -> Iterable[Path]:
    env_bin = os.environ.get("YOSYS_BIN")
    if env_bin:
        yield Path(env_bin)

    which = shutil.which("yosys")
    if which:
        yield Path(which)

    seen: set[Path] = set()
    for start in start_points:
        for parent in [start, *start.parents]:
            if parent in seen:
                continue
            seen.add(parent)
            yield parent / "oss-cad-suite" / "bin" / "yosys.exe"


def find_yosys(explicit: str | None, start_points: Iterable[Path]) -> Path:
    if explicit:
        path = Path(explicit)
        if path.exists():
            return path
        raise FileNotFoundError(f"Yosys not found: {path}")

    for path in candidate_yosys_paths(start_points):
        if path.exists():
            return path
    raise FileNotFoundError("Yosys executable not found. Set --yosys or YOSYS_BIN.")


def yosys_command(yosys_path: Path, script_path: Path) -> list[str]:
    env_bat = yosys_path.parent.parent / "environment.bat"
    if os.name == "nt" and env_bat.exists():
        return [
            "cmd",
            "/c",
            f"call {env_bat} && yosys -q -s {script_path}",
        ]
    return [str(yosys_path), "-q", "-s", str(script_path)]


def yosys_path(path: Path) -> str:
    return path.resolve().as_posix()


def run_yosys(yosys_bin: Path, script: str) -> None:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    with tempfile.NamedTemporaryFile("w", suffix=".ys", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        handle.write("\n")
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            yosys_command(yosys_bin, script_path),
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "Yosys failed.\n"
                f"Script file: {script_path}\n"
                f"Command script: {script}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass


def collect_design_files(inputs: list[str]) -> list[Path]:
    exts = {".v", ".vh", ".sv", ".svh"}
    files: list[Path] = []
    for item in inputs:
        path = Path(item).resolve()
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*") if p.suffix.lower() in exts))
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(f"Input not found: {path}")
    if not files:
        raise ValueError("No HDL source files found.")
    return files


def read_cmd(files: list[Path]) -> str:
    parts = ["read_verilog -sv"]
    parts.extend(yosys_path(file) for file in files)
    return " ".join(parts) + "; "


def count_patterns(path: Path) -> dict[str, int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    patterns = {
        "process": r"\bprocess\b",
        "switch_1b0": r"switch\s+1'0",
        "switch_1b1": r"switch\s+1'1",
        "switch": r"\bswitch\b",
        "cell": r"\bcell\b",
        "dff": r"\$dff\b",
        "mux": r"\$mux\b|\$pmux\b",
        "eq": r"\$eq\b",
        "gt": r"\$gt\b",
        "connect": r"\bconnect\b",
    }
    return {name: len(re.findall(pattern, text)) for name, pattern in patterns.items()}


def build_artifacts_json(
    *,
    output_dir: Path,
    top: str,
    inputs: list[str],
    files: list[Path],
    yosys_bin: Path,
) -> Path:
    artifacts = {
        "pre_proc_rtlil": output_dir / "pre_proc.il",
        "raw_proc_rtlil": output_dir / "raw_proc.il",
        "raw_proc_ifx_noopt_rtlil": output_dir / "raw_proc_ifx_noopt.il",
        "opt_proc_rtlil": output_dir / "opt_proc.il",
    }
    payload = {
        "报告类型": "Verilog 死代码诊断证据包",
        "报告格式": "json",
        "生成时间": datetime.now().isoformat(timespec="seconds"),
        "顶层模块": top,
        "设计输入": [str(Path(item).resolve()) for item in inputs],
        "展开后的源文件": [str(file) for file in files],
        "Yosys路径": str(yosys_bin),
        "产物": {name: str(path.resolve()) for name, path in artifacts.items()},
        "统计": {name: count_patterns(path) for name, path in artifacts.items()},
        "最终诊断必须读取": [
            "source_code",
            "pre_proc_rtlil",
            "raw_proc_rtlil",
            "opt_proc_rtlil",
        ],
        "诊断流程": [
            "先读源码，定位可疑 always/for/if/case 条件。",
            "再读 pre_proc.il，查找与源码行号对应的 process、switch 1'0 或 switch 1'1。",
            "再读 raw_proc.il，确认 proc 后哪些分支、比较器或 mux 已经消失。",
            "最后读 opt_proc.il，确认后续 opt 是否只是清理无可观察影响的寄存器或线网。",
        ],
        "判断原则": [
            "若死分支在 pre_proc.il 已表现为 switch 1'0，根因通常是源码级静态不可达条件。",
            "若 raw_proc.il 已经没有原始分支结构，不应只用 raw_proc 到 opt_proc 的差异反推源码根因。",
            "若 opt_proc.il 只删除未输出的寄存器或线网，这只能说明后续清理，不等于能定位死条件。",
        ],
    }
    out = output_dir / "dead_code_artifacts.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def main() -> int:
    configure_stdio_utf8()
    parser = argparse.ArgumentParser(
        description="Generate RTLIL evidence for Verilog procedural dead-code diagnosis."
    )
    parser.add_argument("inputs", nargs="+", help="HDL source files or source directories")
    parser.add_argument("--top", required=True, help="top module name")
    parser.add_argument("--yosys", default=None, help="optional Yosys executable path")
    args = parser.parse_args()

    skill_dir, mcp_alint_root = resolve_paths()
    report_root = mcp_alint_root / "reports"
    output_dir = build_output_dir(report_root)

    try:
        files = collect_design_files(args.inputs)
        yosys_bin = find_yosys(args.yosys, [Path.cwd(), skill_dir, mcp_alint_root])
        base = read_cmd(files) + f"hierarchy -check -top {args.top}; "

        pre_proc = output_dir / "pre_proc.il"
        raw_proc = output_dir / "raw_proc.il"
        raw_proc_ifx = output_dir / "raw_proc_ifx_noopt.il"
        opt_proc = output_dir / "opt_proc.il"

        run_yosys(yosys_bin, base + f"write_rtlil {yosys_path(pre_proc)}")
        run_yosys(yosys_bin, base + f"proc; write_rtlil {yosys_path(raw_proc)}")
        run_yosys(yosys_bin, base + f"proc -ifx -noopt; write_rtlil {yosys_path(raw_proc_ifx)}")
        run_yosys(yosys_bin, base + f"proc; opt -purge; write_rtlil {yosys_path(opt_proc)}")

        artifacts_path = build_artifacts_json(
            output_dir=output_dir,
            top=args.top,
            inputs=list(args.inputs),
            files=files,
            yosys_bin=yosys_bin,
        )

        safe_print(f"SKILL_DIR={skill_dir}")
        safe_print(f"REPORT_DIR={output_dir}")
        safe_print(f"ARTIFACTS_PATH={artifacts_path}")
        safe_print(f"PRE_PROC_IL={pre_proc}")
        safe_print(f"RAW_PROC_IL={raw_proc}")
        safe_print(f"RAW_PROC_IFX_NOOPT_IL={raw_proc_ifx}")
        safe_print(f"OPT_PROC_IL={opt_proc}")
        return 0
    except Exception as exc:
        safe_print(f"错误: {exc}", err=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
