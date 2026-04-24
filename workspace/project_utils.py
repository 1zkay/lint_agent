"""
共享工具函数

供 MCP 工具和工作流节点复用，避免重复代码。
"""
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import config

logger = logging.getLogger(__name__)

HDL_EXTS = (".v", ".vh", ".sv", ".svh", ".vhd", ".vhdl", ".adc")

# ── AST 后端可用性检查 ─────────────────────────────────────────────────────────
try:
    from eda.ast import infer_incdirs as _infer_incdirs
    _AST_AVAILABLE = True
except ImportError:
    _AST_AVAILABLE = False
    _infer_incdirs = None


def find_project_file(workspace_path: str, project_name: str) -> Optional[str]:
    """从 .alintws 文件解析对应的 .alintproj 路径"""
    ws_path = Path(workspace_path).resolve()
    try:
        tree = ET.parse(str(ws_path))
        root = tree.getroot()
        for pe in root.findall(".//structure/project"):
            proj_path = pe.get("path")
            if proj_path:
                proj_file = ws_path.parent / proj_path
                if proj_file.exists():
                    return str(proj_file)
    except Exception as e:
        logger.error(f"Error parsing workspace file: {e}")
    return None


def parse_alintproj_files(proj_file: Path) -> Dict[str, Path]:
    """解析 .alintproj，返回 {各种路径格式 → 实际 Path} 映射"""
    file_map: Dict[str, Path] = {}
    try:
        tree = ET.parse(str(proj_file))
        root = tree.getroot()
        project_dir = proj_file.parent
        project_name = proj_file.stem
        for fe in root.findall(".//structure/file"):
            fpath_str = fe.get("path", "").strip()
            if not fpath_str or not fpath_str.endswith(HDL_EXTS):
                continue
            actual = (project_dir / fpath_str).resolve()
            if actual.exists():
                file_map[f"../{project_dir.name}/{fpath_str}"] = actual
                file_map[f"{project_name}/{fpath_str}"] = actual
                file_map[fpath_str] = actual
                file_map[Path(fpath_str).name] = actual
    except Exception as e:
        logger.error(f"Failed to parse .alintproj: {e}")
    return file_map


def resolve_project_verilog_inputs(
    workspace_path: str, project_name: str
) -> Tuple[Optional[dict], Optional[str]]:
    """
    从工作区和项目文件解析 Verilog 源文件列表。

    Returns:
        (inputs_dict, error_str)
        inputs_dict 包含 proj_file, project_dir, v_files, input_path, incdirs, base_name
    """
    proj_file_path = find_project_file(workspace_path, project_name)
    if not proj_file_path:
        return None, f"Project file not found for: {project_name}"

    proj_file = Path(proj_file_path).resolve()
    project_dir = proj_file.parent

    file_map = parse_alintproj_files(proj_file)
    v_files = [str(p) for p in set(file_map.values()) if p.suffix.lower() in (".v", ".sv")]
    if not v_files:
        return None, f"No Verilog/SV files found in project {project_name}"

    input_path = v_files[0] if len(v_files) == 1 else str(project_dir)
    incdirs = _infer_incdirs(input_path, []) if _AST_AVAILABLE else []
    base_name = Path(v_files[0]).stem if len(v_files) == 1 else project_name

    return {
        "proj_file": proj_file,
        "project_dir": project_dir,
        "v_files": v_files,
        "input_path": input_path,
        "incdirs": incdirs,
        "base_name": base_name,
    }, None


def get_project_report_dir(project_name: str) -> Path:
    """创建带时间戳的项目报告目录"""
    base_dir = Path(config.csv_output_dir).resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    project_dir = base_dir / f"{project_name}_{timestamp}"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


def get_latest_prepared_dir() -> Path:
    """返回最新的 _prepared/<session_id> 目录"""
    base_dir = Path(config.csv_output_dir).resolve() / "_prepared"
    if not base_dir.exists():
        raise FileNotFoundError(
            "未找到 _prepared 目录。请先运行 generate_basic_analysis_workflow。"
        )
    dirs = [d for d in base_dir.iterdir() if d.is_dir()]
    if not dirs:
        raise FileNotFoundError(
            "未找到任何准备好的资源目录。请先运行 generate_basic_analysis_workflow。"
        )
    return max(dirs, key=lambda d: d.stat().st_mtime)
