"""
Node 3: 提取项目源代码，生成带行号文本

对应原 generate_basic_analysis_workflow Step3
"""
import logging
import re
from pathlib import Path
from typing import List

from ..state import AlintWorkflowState

logger = logging.getLogger(__name__)


async def run_sources_node(state: AlintWorkflowState) -> dict:
    """
    LangGraph 节点：将所有 Verilog 源文件拼接为带行号的文本

    输入 state 字段: v_files, project_dir
    输出 state 字段: source_code_text
    """
    v_files: List[str] = state.get("v_files") or []
    project_dir_str: str = state.get("project_dir") or ""

    if not v_files:
        return {"error": "No Verilog source files available (v_files is empty)"}

    project_dir = Path(project_dir_str) if project_dir_str else None
    parts = []

    for vf in v_files:
        file_path = Path(vf)
        if project_dir and file_path.is_relative_to(project_dir):
            rel_path = file_path.relative_to(project_dir)
        else:
            rel_path = file_path.name

        header = f"\n=== {rel_path} ===\n"
        chunk = header
        try:
            if file_path.exists():
                with open(file_path, "r", encoding="utf-8", errors="ignore") as sf:
                    lines = sf.read().splitlines()
                if lines:
                    chunk += "\n".join(
                        f"{i:>5}: {lines[i - 1]}" for i in range(1, len(lines) + 1)
                    ) + "\n"
                else:
                    chunk += "(empty file)\n"
            else:
                chunk += f"File not found: {file_path}\n"
        except Exception as e:
            chunk += f"Error reading file: {type(e).__name__}: {e}\n"
        parts.append(chunk)

    source_code_text = re.sub(r"\n{3,}", "\n\n", "".join(parts))
    logger.info(f"[sources_node] Extracted {len(v_files)} source files")
    return {"source_code_text": source_code_text}
