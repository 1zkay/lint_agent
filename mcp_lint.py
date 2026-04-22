"""
ALINT-PRO MCP Server

架构：
  FastMCP（MCP 协议层）
    └── LangGraph StateGraph（工作流编排）
          ├── lint_node       → ALINT-PRO 批处理
          ├── structure_node  → Yosys AST / CFG / DDG / DFG
          ├── sources_node    → 源代码提取
          └── organize_node   → _prepared/ 目录整理

  prompts/templates.py → 统一管理 prompt 文本
  utils.py          → 共享工具函数
  config.py         → 配置单例
"""
import asyncio
import json
import logging
import os
import platform
import re
import secrets
import shlex
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastmcp import FastMCP

# ── 本地模块 ──────────────────────────────────────────────────────────────────
from config import config
from utils import (
    find_project_file,
    get_latest_prepared_dir,
    get_project_report_dir,
    resolve_project_verilog_inputs,
)
from agent.graph import get_workflow
from prompts.templates import (
    get_basic_hardware_analysis_messages,
    get_structured_report_messages,
)

# ── AST 后端（可选） ───────────────────────────────────────────────────────────
try:
    from AST import (
        parse_target,
        node_to_dict,
        infer_incdirs,
        run_yosys_for_netlist,
        _prepare_temp_sources,
        run_yosys_for_rtlil_processes,
        build_cfg_ddg_from_rtlil_processes,
    )
    AST_AVAILABLE = True
except ImportError as e:
    AST_AVAILABLE = False
    parse_target = node_to_dict = infer_incdirs = run_yosys_for_netlist = None
    _prepare_temp_sources = run_yosys_for_rtlil_processes = build_cfg_ddg_from_rtlil_processes = None

logger = logging.getLogger(__name__)

# ── FastMCP Server ─────────────────────────────────────────────────────────────
mcp = FastMCP("ALINT-PRO Analysis Server")

APP_ROOT = Path(__file__).resolve().parent
HDL_EXTS = (".v", ".vh", ".sv", ".svh", ".vhd", ".vhdl", ".adc")


def _resolve_workspace_path(raw_path: str | Path) -> Path:
    """Resolve `/...` file-tool paths and relative paths under the project root."""
    raw = str(raw_path or "").strip()
    if not raw:
        return APP_ROOT

    app_root = APP_ROOT.resolve()
    app_root_str = str(app_root)
    app_root_posix = app_root.as_posix()
    if raw == app_root_str or raw.startswith(app_root_str + os.sep):
        return Path(raw).resolve()
    if raw == app_root_posix or raw.startswith(app_root_posix + "/"):
        return Path(raw).resolve()

    if raw.startswith("/"):
        return (app_root / raw.lstrip("/")).resolve()

    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    return (app_root / candidate).resolve()


def _to_workspace_virtual_path(path: str | Path | None) -> str | None:
    """Return a `/...` path that FilesystemMiddleware can read when possible."""
    if path is None:
        return None
    resolved = Path(path).resolve()
    try:
        relative_path = resolved.relative_to(APP_ROOT.resolve())
    except ValueError:
        return str(resolved)
    return "/" if not relative_path.parts else "/" + relative_path.as_posix()


# =============================================================================
# Resources（MCP 资源：供 LLM 读取最新 _prepared 目录内容）
# =============================================================================

@mcp.resource(
    uri="alint://basic/sources",
    description="项目源代码(完整上下文) - 基础分析",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def read_basic_sources() -> str:
    f = get_latest_prepared_dir() / "1_project_sources.txt"
    if not f.exists():
        raise FileNotFoundError(f"源代码文件不存在: {f}")
    return f.read_text(encoding="utf-8", errors="ignore")


@mcp.resource(
    uri="alint://basic/violations",
    description="ALINT违规报告(CSV格式) - 基础分析",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def read_basic_violations() -> str:
    f = get_latest_prepared_dir() / "2_alint_violations.csv"
    if not f.exists():
        raise FileNotFoundError(f"违规文件不存在: {f}")
    return f.read_text(encoding="utf-8", errors="ignore")


@mcp.resource(
    uri="alint://basic/ast",
    description="AST抽象语法树分析结果(JSON格式) - 基础分析",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def read_basic_ast() -> str:
    f = get_latest_prepared_dir() / "3_ast.json"
    if not f.exists():
        raise FileNotFoundError(f"AST文件不存在: {f}")
    return f.read_text(encoding="utf-8", errors="ignore")


@mcp.resource(
    uri="alint://basic/cfg_ddg_dfg",
    description="CFG/DDG/DFG控制流和数据流分析结果(JSON格式) - 基础分析",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def read_basic_cfg_ddg_dfg() -> str:
    f = get_latest_prepared_dir() / "4_cfg_ddg_dfg.json"
    if not f.exists():
        raise FileNotFoundError(f"CFG/DDG/DFG文件不存在: {f}")
    return f.read_text(encoding="utf-8", errors="ignore")


@mcp.resource(
    uri="alint://basic/kb",
    description="Verilog设计规范知识库(JSONL格式) - 基础分析",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def read_basic_kb() -> str:
    f = get_latest_prepared_dir() / "5_verilog_kb.jsonl"
    if not f.exists():
        raise FileNotFoundError(f"知识库文件不存在: {f}")
    return f.read_text(encoding="utf-8", errors="ignore")


# =============================================================================
# Prompts（从 prompts/templates.py 统一管理）
# =============================================================================

@mcp.prompt()
def basic_hardware_analysis():
    """
    基础硬件代码质量综合分析（JSON 输出）

    自动引用：sources / violations / ast / cfg_ddg_dfg / kb
    """
    return get_basic_hardware_analysis_messages()


@mcp.prompt()
def structured_hardware_analysis_report():
    """
    结构化硬件代码分析报告（Markdown 输出）

    自动引用：sources / violations / ast / cfg_ddg_dfg / kb
    """
    return get_structured_report_messages()


# =============================================================================
# Tool: generate_basic_analysis_workflow（LangGraph 编排）
# =============================================================================

@mcp.tool()
async def generate_basic_analysis_workflow(
    workspace_path: str, project_name: str
) -> dict:
    """
    执行简化的 ALINT 分析工作流（LangGraph 版）：
      1. 运行 ALINT-PRO 生成原始 lint 报告
      2. 用 Yosys 生成 AST / CFG / DDG / DFG
      3. 提取项目源代码（带行号）
      4. 整理所有产物到 reports/_prepared/<session_id>/

    输入:
        workspace_path: .alintws 工作区文件路径
        project_name:   项目名称

    输出:
        prepared_directory 及各文件路径，供后续 LLM 分析使用。
    """
    workflow = get_workflow()
    initial_state = {
        "workspace_path": workspace_path,
        "project_name": project_name,
        # 其余字段由各节点填充
        "raw_csv_path": None,
        "alint_output_dir": None,
        "ast_json_file": None,
        "cfg_ddg_file": None,
        "rtlil_file": None,
        "v_files": None,
        "project_dir": None,
        "source_code_text": None,
        "prepared_directory": None,
        "prepared_sources": None,
        "prepared_violations": None,
        "prepared_ast": None,
        "prepared_cfg_ddg_dfg": None,
        "prepared_kb": None,
        "session_id": None,
        "error": None,
    }

    try:
        final_state = await workflow.ainvoke(initial_state)
    except Exception as e:
        logger.exception("Workflow invocation failed")
        return {"status": "error", "message": str(e)}

    if final_state.get("error"):
        return {"status": "error", "message": final_state["error"]}

    prepared_dir = final_state.get("prepared_directory", "")
    v_files = final_state.get("v_files") or []
    session_id = final_state.get("session_id", "")
    prepared_dir_virtual = _to_workspace_virtual_path(prepared_dir) or ""
    prepared_files = {
        "sources": _to_workspace_virtual_path(final_state.get("prepared_sources")),
        "violations": _to_workspace_virtual_path(final_state.get("prepared_violations")),
        "ast": _to_workspace_virtual_path(final_state.get("prepared_ast")),
        "cfg_ddg_dfg": _to_workspace_virtual_path(final_state.get("prepared_cfg_ddg_dfg")),
        "kb": _to_workspace_virtual_path(final_state.get("prepared_kb")),
    }

    return {
        "status": "success",
        "message": (
            f"✅ 工作流执行成功！资源文件已准备完毕。\n\n"
            f"📁 准备目录: {prepared_dir_virtual}\n\n"
            f"包含文件:\n"
            f"  • 1_project_sources.txt（源代码，{len(v_files)} 个文件）\n"
            f"  • 2_alint_violations.csv（ALINT 违规报告）\n"
            f"  • 3_ast.json（AST 分析结果）\n"
            f"  • 4_cfg_ddg_dfg.json（CFG/DDG/DFG 分析结果）\n"
            f"  • 5_verilog_kb.jsonl（Verilog 设计规范知识库）\n\n"
            f"💡 这些文件可直接用于 LLM 分析。"
        ),
        "prepared_directory": prepared_dir_virtual,
        "files": prepared_files,
        "statistics": {
            "source_files": len(v_files),
            "project_name": project_name,
            "session_id": session_id,
        },
    }


# =============================================================================
# Tool: run_alint_analysis（独立运行 ALINT，不含后续步骤）
# =============================================================================

@mcp.tool()
async def run_alint_analysis(
    workspace_path: str, project_name: str, output_name: Optional[str] = None
) -> str:
    """
    以批处理模式运行 ALINT-PRO 并生成 CSV 报告（仅限 Windows）
    """
    if platform.system() != "Windows":
        return "Error: ALINT-PRO only runs on Windows."

    ws = Path(workspace_path).resolve()
    if not ws.exists():
        return f"Error: Workspace not found: {workspace_path}"

    alint_exe = Path(config.alint_exe).resolve()
    if not alint_exe.exists():
        return f"Error: ALINT executable not found: {alint_exe}"

    out_dir = get_project_report_dir(project_name)
    if not output_name:
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
        return "Error: ALINT analysis timed out (exceeded 15 minutes)."
    finally:
        tmp_do.unlink(missing_ok=True)

    rc = proc.returncode
    out = (stdout or b"").decode(errors="replace")
    err = (stderr or b"").decode(errors="replace")

    if rc == 0 and out_csv.exists():
        return (
            f"ALINT analysis completed successfully!\n"
            f"Report: {_to_workspace_virtual_path(out_csv)}\n"
            f"Stdout:\n{out}\nStderr:\n{err}"
        )
    return f"ALINT analysis failed (exit code: {rc})\nStderr:\n{err}\nStdout:\n{out}"


# =============================================================================
# Tool: analyze_verilog_structure（直接 Yosys 分析，不走完整工作流）
# =============================================================================

@mcp.tool()
async def analyze_verilog_structure(
    workspace_path: str,
    project_name: str,
    analysis_type: str = "all",
    output_dir: Optional[str] = None,
    simplified_ast: bool = True,
    top: Optional[str] = None,
    oss_root: Optional[str] = None,
    dfg_effort: Optional[int] = 0,
) -> dict:
    """
    使用 Yosys 分析 Verilog/SystemVerilog 结构，生成 AST 和/或 CFG/DDG/DFG。
    """
    if not AST_AVAILABLE:
        return {"error": "AST module not available. Ensure AST.py is present."}
    if analysis_type not in ("ast", "cfg", "all"):
        return {"error": "analysis_type must be 'ast', 'cfg', or 'all'"}

    inputs, err = resolve_project_verilog_inputs(workspace_path, project_name)
    if err:
        return {"error": err}

    v_files = inputs["v_files"]
    input_path = inputs["input_path"]
    project_dir = inputs["project_dir"]
    incdirs = inputs["incdirs"]
    base_name = inputs["base_name"]

    out_dir = _resolve_workspace_path(output_dir) if output_dir else get_project_report_dir(project_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    resolved_oss = Path(oss_root).resolve() if oss_root else None
    results: dict = {"status": "success", "project": project_name, "files_processed": len(v_files)}

    # AST
    if analysis_type in ("ast", "all") and parse_target:
        ast_output = str(out_dir / f"{base_name}_AST{'2' if simplified_ast else '1'}.json")
        try:
            ast_tree, meta, _ = parse_target(
                input_path, incdirs=incdirs, defines=[], recursive=True,
                oss_root=resolved_oss, simplified=simplified_ast,
            )
            ast_dict = node_to_dict(ast_tree, include_coord=False)
            if isinstance(ast_dict, dict) and isinstance(ast_dict.get("source_files"), list):
                ast_dict["source_files"] = [
                    _to_workspace_virtual_path(source)
                    for source in ast_dict["source_files"]
                ]
            with open(ast_output, "w", encoding="utf-8") as f:
                json.dump(ast_dict, f, ensure_ascii=False, indent=2)
            results["ast_json_file"] = _to_workspace_virtual_path(ast_output)
            results["ast_mode"] = "dump_ast2(简化)" if simplified_ast else "dump_ast1(原始)"
        except Exception as e:
            results["ast_error"] = f"{type(e).__name__}: {e}"

    # CFG/DDG/DFG
    if analysis_type in ("cfg", "all") and _prepare_temp_sources:
        cfg_output = str(out_dir / f"{base_name}_cfg_ddg_dfg.json")
        rtlil_output = str(out_dir / f"{base_name}_process.rtlil")
        dfg_output = str(out_dir / f"{base_name}_dfg.dot")
        temp_root = None
        try:
            temp_files, temp_incdirs, temp_root = _prepare_temp_sources(
                v_files, incdirs, base_root=project_dir
            )
            run_yosys_for_rtlil_processes(
                temp_files, incdirs=temp_incdirs, defines=[],
                rtlil_out=rtlil_output, oss_root=resolved_oss, top=top,
            )
            rtlil_text = Path(rtlil_output).read_text(encoding="utf-8", errors="replace")
            graphs = build_cfg_ddg_from_rtlil_processes(
                rtlil_text, dfg_dot_out=dfg_output, oss_root=resolved_oss,
                top=top, dfg_effort=dfg_effort,
            )
            graphs["source_files"] = [
                _to_workspace_virtual_path(source) for source in v_files
            ]
            with open(cfg_output, "w", encoding="utf-8") as f:
                json.dump(graphs, f, ensure_ascii=False, indent=2)
            results["cfg_ddg_file"] = _to_workspace_virtual_path(cfg_output)
            results["rtlil_file"] = _to_workspace_virtual_path(rtlil_output)
            results["top_module"] = top or "auto"
        except Exception as e:
            results["cfg_error"] = f"{type(e).__name__}: {e}"
        finally:
            if temp_root:
                shutil.rmtree(temp_root, ignore_errors=True)

    results["message"] = "\n".join(
        f"✅ {k}: {v}" for k, v in results.items() if k not in ("status", "message")
    )
    return results


# =============================================================================
# Tool: export_verilog_netlist
# =============================================================================

@mcp.tool()
async def export_verilog_netlist(
    workspace_path: str,
    project_name: str,
    verilog_output: Optional[str] = None,
    json_output: Optional[str] = None,
    top: Optional[str] = None,
    oss_root: Optional[str] = None,
) -> dict:
    """使用 Yosys 导出门级网表 Verilog/JSON。"""
    if not AST_AVAILABLE or not run_yosys_for_netlist:
        return {"error": "AST module not available."}

    inputs, err = resolve_project_verilog_inputs(workspace_path, project_name)
    if err:
        return {"error": err}

    v_files = inputs["v_files"]
    project_dir = inputs["project_dir"]
    incdirs = inputs["incdirs"]
    base_name = inputs["base_name"]
    out_dir = get_project_report_dir(project_name)

    verilog_output = (
        str(_resolve_workspace_path(verilog_output))
        if verilog_output
        else str(out_dir / f"{base_name}_synth.v")
    )
    json_output = (
        str(_resolve_workspace_path(json_output))
        if json_output
        else str(out_dir / f"{base_name}_netlist.json")
    )
    resolved_oss = Path(oss_root).resolve() if oss_root else None

    temp_root = None
    try:
        temp_files, temp_incdirs, temp_root = _prepare_temp_sources(
            v_files, incdirs, base_root=project_dir
        )
        run_yosys_for_netlist(
            temp_files, incdirs=temp_incdirs, defines=[],
            verilog_out=verilog_output, json_out=json_output,
            oss_root=resolved_oss, top=top,
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        if temp_root:
            shutil.rmtree(temp_root, ignore_errors=True)

    return {
        "status": "success",
        "project": project_name,
        "verilog_netlist": _to_workspace_virtual_path(verilog_output),
        "json_netlist": _to_workspace_virtual_path(json_output),
        "files_processed": len(v_files),
        "message": (
            "✅ 网表已生成:\n"
            f"Verilog: {_to_workspace_virtual_path(verilog_output)}\n"
            f"JSON: {_to_workspace_virtual_path(json_output)}"
        ),
    }


# =============================================================================
# Tool: convert_copilot_json_to_csv
# =============================================================================

def _extract_json_from_text(text: str) -> dict:
    text = text.strip()
    for prefix in ("```json", "```"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            if text.endswith("```"):
                text = text[:-3]
            break
    return json.loads(text.strip())


def _json_to_csv_content(json_data: dict) -> str:
    clusters_key = "clusters" if "clusters" in json_data else "违规簇列表"
    if clusters_key not in json_data:
        raise ValueError("缺少 'clusters' 或 '违规簇列表' 字段")
    clusters = json_data[clusters_key]
    cn_fields = ["簇编号", "文件路径", "规则编号", "违规数量", "行号列表", "严重程度", "评分", "置信度", "分析原因"]
    en_keys   = ["cluster_id", "file_path", "rules", "violations", "lines", "severity", "score", "confidence", "reason"]
    mapping   = dict(zip(cn_fields, en_keys))

    def _get(cluster, cn_key):
        en_key = mapping.get(cn_key, "")
        return cluster.get(cn_key, cluster.get(en_key, cluster.get("file" if en_key == "file_path" else "rule" if en_key == "rules" else en_key, "")))

    lines = [",".join(f'"{f}"' for f in cn_fields)]
    for c in clusters:
        raw_lines = _get(c, "行号列表")
        lines_str = ";".join(map(str, raw_lines)) if isinstance(raw_lines, list) else str(raw_lines or "")
        row = [
            str(_get(c, "簇编号")), str(_get(c, "文件路径")), str(_get(c, "规则编号")),
            str(_get(c, "违规数量") or 0), lines_str, str(_get(c, "严重程度")),
            str(_get(c, "评分") or 0.0), str(_get(c, "置信度") or 0.0), str(_get(c, "分析原因")),
        ]
        lines.append(",".join(f'"{v.replace(chr(34), chr(34)*2)}"' if any(c in v for c in ',"') else v for v in row))
    return "\n".join(lines)


@mcp.tool()
async def convert_copilot_json_to_csv(
    json_output: str,
    output_csv_path: Optional[str] = None,
) -> str:
    """将 Copilot 输出的 JSON 评估结果转换为 CSV 文件"""
    try:
        data = _extract_json_from_text(json_output)
        if "clusters" not in data and "违规簇列表" not in data:
            return "Error: 缺少 'clusters' 或 '违规簇列表' 字段"
        if output_csv_path is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = _resolve_workspace_path(config.csv_output_dir) / f"evaluation_{ts}.csv"
        else:
            output_path = _resolve_workspace_path(output_csv_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        csv_content = _json_to_csv_content(data)
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(csv_content)
        clusters = data.get("clusters", data.get("违规簇列表", []))
        return (
            f"✓ JSON 已转换为 CSV\n"
            f"总簇数: {len(clusters)}\n"
            f"输出: {_to_workspace_virtual_path(output_path)}\n"
            f"大小: {output_path.stat().st_size} bytes"
        )
    except ValueError as e:
        return f"Error: JSON 解析失败 - {e}"
    except Exception as e:
        logger.exception("convert_copilot_json_to_csv failed")
        return f"Error: {type(e).__name__}: {e}"


# =============================================================================
# Tool: save_user_feedback
# =============================================================================

@mcp.tool()
def save_user_feedback(
    feedback_content: str,
    project_name: Optional[str] = None,
    violation_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """保存用户对分析结果的反馈意见到 JSON 文件"""
    try:
        feedback_dir = Path(config.alint_pro_root) / "feedback"
        feedback_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fid = f"feedback_{ts}_{secrets.token_hex(4)}"
        data = {
            "feedback_id": fid,
            "timestamp": datetime.now().isoformat(),
            "feedback_content": feedback_content,
            "project_name": project_name,
            "violation_id": violation_id,
            "metadata": metadata or {},
        }
        out = feedback_dir / f"{fid}.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return {"status": "success", "feedback_id": fid, "file_path": str(out),
                "message": f"✅ 反馈已保存: {out}"}
    except Exception as e:
        return {"status": "error", "message": f"❌ 保存反馈失败: {e}"}


# =============================================================================
# Entrypoint
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    config.validate()
    logger.info("Starting ALINT-PRO Analysis Server (LangGraph edition)...")
    mcp.run()
