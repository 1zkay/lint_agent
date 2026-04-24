"""
Node 4: 整理所有产物到 _prepared/<session_id>/ 目录

对应原 generate_basic_analysis_workflow Step4
"""
import logging
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from agent.state import AlintWorkflowState
from config import config

logger = logging.getLogger(__name__)


async def run_organize_node(state: AlintWorkflowState) -> dict:
    """
    LangGraph 节点：将分析产物统一复制到 _prepared 子目录

    输入 state 字段:
        source_code_text, raw_csv_path, ast_json_file,
        cfg_ddg_file, v_files, project_name
    输出 state 字段:
        prepared_directory, prepared_sources, prepared_violations,
        prepared_ast, prepared_cfg_ddg_dfg, prepared_kb, session_id
    """
    source_code_text: str = state.get("source_code_text") or ""
    raw_csv_path: Optional[str] = state.get("raw_csv_path")
    ast_json_file: Optional[str] = state.get("ast_json_file")
    cfg_ddg_file: Optional[str] = state.get("cfg_ddg_file")
    v_files: List[str] = state.get("v_files") or []
    project_name: str = state["project_name"]

    # 创建 _prepared/<session_id> 目录
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "-" + secrets.token_hex(3)
    prepared_dir = Path(config.csv_output_dir) / "_prepared" / session_id
    prepared_dir.mkdir(parents=True, exist_ok=True)

    prepared_sources = prepared_dir / "1_project_sources.txt"
    prepared_violations = prepared_dir / "2_alint_violations.csv"
    prepared_ast = prepared_dir / "3_ast.json"
    prepared_cfg = prepared_dir / "4_cfg_ddg_dfg.json"
    prepared_kb = prepared_dir / "5_verilog_kb.jsonl"

    # 1. 写入源代码文本
    prepared_sources.write_text(source_code_text, encoding="utf-8")
    logger.info(f"[organize_node] Sources → {prepared_sources}")

    # 2. 复制违规 CSV
    if raw_csv_path and Path(raw_csv_path).exists():
        shutil.copy2(raw_csv_path, prepared_violations)
        logger.info(f"[organize_node] Violations → {prepared_violations}")

    # 3. 复制 AST JSON
    if ast_json_file and Path(ast_json_file).exists():
        shutil.copy2(ast_json_file, prepared_ast)
        logger.info(f"[organize_node] AST → {prepared_ast}")

    # 4. 复制 CFG/DDG JSON
    if cfg_ddg_file and Path(cfg_ddg_file).exists():
        shutil.copy2(cfg_ddg_file, prepared_cfg)
        logger.info(f"[organize_node] CFG/DDG → {prepared_cfg}")

    # 5. 复制知识库
    kb_src = Path(config.alint_pro_root) / "verilog_guidelines_kb_en_completed.jsonl"
    if kb_src.exists():
        shutil.copy2(kb_src, prepared_kb)
        logger.info(f"[organize_node] KB → {prepared_kb}")
    else:
        logger.warning(f"[organize_node] KB file not found: {kb_src}")

    return {
        "prepared_directory": str(prepared_dir),
        "prepared_sources": str(prepared_sources),
        "prepared_violations": str(prepared_violations),
        "prepared_ast": str(prepared_ast),
        "prepared_cfg_ddg_dfg": str(prepared_cfg),
        "prepared_kb": str(prepared_kb),
        "session_id": session_id,
    }
