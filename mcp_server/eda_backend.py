"""Optional EDA backend imports used by MCP tools."""

from __future__ import annotations

try:
    from eda.ast import (
        _prepare_temp_sources,
        build_cfg_ddg_from_rtlil_processes,
        infer_incdirs,
        node_to_dict,
        parse_target,
        run_yosys_for_netlist,
        run_yosys_for_rtlil_processes,
    )

    AST_AVAILABLE = True
except ImportError:
    AST_AVAILABLE = False
    parse_target = None
    node_to_dict = None
    infer_incdirs = None
    run_yosys_for_netlist = None
    _prepare_temp_sources = None
    run_yosys_for_rtlil_processes = None
    build_cfg_ddg_from_rtlil_processes = None

