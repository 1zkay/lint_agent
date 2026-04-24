"""JSON conversion MCP tools."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from config import config
from mcp_server.json_conversion import extract_json_from_text, json_to_csv_content
from mcp_server.pathing import resolve_workspace_path, to_workspace_virtual_path

logger = logging.getLogger(__name__)


def register_conversion_tools(mcp) -> None:
    """Register JSON/CSV conversion tools."""

    @mcp.tool()
    async def convert_copilot_json_to_csv(
        json_output: str,
        output_csv_path: Optional[str] = None,
    ) -> str:
        """将 Copilot 输出的 JSON 评估结果转换为 CSV 文件"""
        try:
            data = extract_json_from_text(json_output)
            if "clusters" not in data and "违规簇列表" not in data:
                return "Error: 缺少 'clusters' 或 '违规簇列表' 字段"
            if output_csv_path is None:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_path = resolve_workspace_path(config.csv_output_dir) / f"evaluation_{ts}.csv"
            else:
                output_path = resolve_workspace_path(output_csv_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            csv_content = json_to_csv_content(data)
            with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
                f.write(csv_content)
            clusters = data.get("clusters", data.get("违规簇列表", []))
            return (
                f"✓ JSON 已转换为 CSV\n"
                f"总簇数: {len(clusters)}\n"
                f"输出: {to_workspace_virtual_path(output_path)}\n"
                f"大小: {output_path.stat().st_size} bytes"
            )
        except ValueError as exc:
            return f"Error: JSON 解析失败 - {exc}"
        except Exception as exc:
            logger.exception("convert_copilot_json_to_csv failed")
            return f"Error: {type(exc).__name__}: {exc}"

