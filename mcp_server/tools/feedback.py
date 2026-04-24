"""User feedback MCP tools."""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import config


def register_feedback_tools(mcp) -> None:
    """Register user feedback persistence tools."""

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
            return {
                "status": "success",
                "feedback_id": fid,
                "file_path": str(out),
                "message": f"✅ 反馈已保存: {out}",
            }
        except Exception as exc:
            return {"status": "error", "message": f"❌ 保存反馈失败: {exc}"}
