"""
LangGraph 工作流共享状态定义

所有节点通过此 TypedDict 传递数据，实现解耦。
"""
from typing import Optional, List
from typing_extensions import TypedDict


class AlintWorkflowState(TypedDict):
    """generate_basic_analysis_workflow 的完整状态"""

    # ── 输入 ──────────────────────────────────────
    workspace_path: str
    project_name: str

    # ── 中间产物路径 ──────────────────────────────
    raw_csv_path: Optional[str]          # Step1: ALINT 原始报告
    alint_output_dir: Optional[str]      # Step1: 子目录

    ast_json_file: Optional[str]         # Step2: AST JSON
    cfg_ddg_file: Optional[str]          # Step2: CFG/DDG JSON
    rtlil_file: Optional[str]            # Step2: RTLIL 中间文件

    v_files: Optional[List[str]]         # Step3: 源文件列表
    project_dir: Optional[str]           # Step3: 项目目录
    source_code_text: Optional[str]      # Step3: 带行号源代码文本

    # ── 最终输出路径 ──────────────────────────────
    prepared_directory: Optional[str]
    prepared_sources: Optional[str]
    prepared_violations: Optional[str]
    prepared_ast: Optional[str]
    prepared_cfg_ddg_dfg: Optional[str]
    prepared_kb: Optional[str]
    session_id: Optional[str]


    llm_model_used: Optional[str]        # 实际调用的模型名

    # ── 错误信息（任意节点写入即中断） ───────────────
    error: Optional[str]
