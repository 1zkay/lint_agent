"""JSON/CSV conversion helpers for MCP tools."""

from __future__ import annotations

import json


def extract_json_from_text(text: str) -> dict:
    text = text.strip()
    for prefix in ("```json", "```"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            if text.endswith("```"):
                text = text[:-3]
            break
    return json.loads(text.strip())


def json_to_csv_content(json_data: dict) -> str:
    clusters_key = "clusters" if "clusters" in json_data else "违规簇列表"
    if clusters_key not in json_data:
        raise ValueError("缺少 'clusters' 或 '违规簇列表' 字段")
    clusters = json_data[clusters_key]
    cn_fields = ["簇编号", "文件路径", "规则编号", "违规数量", "行号列表", "严重程度", "评分", "置信度", "分析原因"]
    en_keys = ["cluster_id", "file_path", "rules", "violations", "lines", "severity", "score", "confidence", "reason"]
    mapping = dict(zip(cn_fields, en_keys))

    def _get(cluster, cn_key):
        en_key = mapping.get(cn_key, "")
        return cluster.get(cn_key, cluster.get(en_key, cluster.get("file" if en_key == "file_path" else "rule" if en_key == "rules" else en_key, "")))

    lines = [",".join(f'"{field}"' for field in cn_fields)]
    for cluster in clusters:
        raw_lines = _get(cluster, "行号列表")
        lines_str = ";".join(map(str, raw_lines)) if isinstance(raw_lines, list) else str(raw_lines or "")
        row = [
            str(_get(cluster, "簇编号")), str(_get(cluster, "文件路径")), str(_get(cluster, "规则编号")),
            str(_get(cluster, "违规数量") or 0), lines_str, str(_get(cluster, "严重程度")),
            str(_get(cluster, "评分") or 0.0), str(_get(cluster, "置信度") or 0.0), str(_get(cluster, "分析原因")),
        ]
        lines.append(",".join(f'"{value.replace(chr(34), chr(34)*2)}"' if any(char in value for char in ',"') else value for value in row))
    return "\n".join(lines)
