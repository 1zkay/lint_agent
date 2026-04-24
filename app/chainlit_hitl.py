"""Chainlit human-in-the-loop approval UI helpers."""

from __future__ import annotations

import logging

import chainlit as cl
from langgraph.types import Interrupt

from config import config

logger = logging.getLogger(__name__)


def extract_hitl_request_from_interrupts(interrupts):
    """从 LangGraph v2 Interrupt 元组中提取 HITL 中断请求。"""
    if not interrupts:
        return None
    first = interrupts[0]
    if isinstance(first, Interrupt) and isinstance(first.value, dict):
        return first.value
    return None


def _extract_decision_from_action_result(action_result) -> str:
    """兼容 Chainlit AskActionMessage 返回结构，提取决策类型。"""
    payload = {}
    if isinstance(action_result, dict):
        payload = action_result.get("payload") or {}
    elif action_result is not None and hasattr(action_result, "payload"):
        payload = getattr(action_result, "payload") or {}
    decision = str(payload.get("decision") or "reject").strip().lower()
    if decision not in ("approve", "reject"):
        return "reject"
    return decision


def _build_decision_actions(allowed_decisions: list[str]) -> list:
    """根据 allowed_decisions 生成 AskActionMessage 按钮。"""
    action_map = {
        "approve": cl.Action(
            name="hitl_decision",
            payload={"decision": "approve"},
            label="✅ 批准",
            tooltip="批准并继续执行工具调用",
        ),
        "reject": cl.Action(
            name="hitl_decision",
            payload={"decision": "reject"},
            label="❌ 拒绝",
            tooltip="拒绝该工具调用",
        ),
    }
    actions = []
    for name in allowed_decisions:
        if name in action_map:
            actions.append(action_map[name])
    if not actions:
        actions.append(action_map["reject"])
    return actions


async def ask_hitl_resume_payload(hitl_request: dict):
    """
    使用 Chainlit AskActionMessage 按 action_request 顺序收集决策，
    返回 {"decisions": [...]} 字典，供调用方包装为 Command(resume=...)。
    """
    action_requests = (hitl_request or {}).get("action_requests") or []
    review_configs = (hitl_request or {}).get("review_configs") or []

    if not action_requests:
        return {"decisions": [{"type": "reject", "message": "审批请求无效，已拒绝。"}]}

    decisions = []
    for idx, req in enumerate(action_requests, start=1):
        name = req.get("name", "tool")
        current_args = req.get("args", {})
        description = req.get("description", "")

        cfg = review_configs[idx - 1] if idx - 1 < len(review_configs) else {}
        allowed_decisions = cfg.get("allowed_decisions") or ["approve", "reject"]
        if not isinstance(allowed_decisions, list):
            allowed_decisions = ["approve", "reject"]
        allowed_decisions = [str(x).strip().lower() for x in allowed_decisions]
        actions = _build_decision_actions(allowed_decisions)

        content_lines = [
            f"审批请求 {idx}/{len(action_requests)}",
            f"工具: `{name}`",
            f"参数: `{str(current_args)[:800]}`",
        ]
        if description:
            content_lines.append(f"说明: {description}")
        content_lines.append("请选择处理方式。")

        ask = await cl.AskActionMessage(
            content="\n".join(content_lines),
            actions=actions,
            timeout=config.agent_hitl_timeout,
            raise_on_timeout=False,
        ).send()
        decision = _extract_decision_from_action_result(ask)
        if decision not in allowed_decisions:
            decision = "reject"

        if decision == "approve":
            decisions.append({"type": "approve"})
            continue

        decisions.append({"type": "reject", "message": "用户拒绝执行该工具调用。"})

    if len(decisions) != len(action_requests):
        logger.error(
            "[chat_app] HITL decisions length mismatch: decisions=%s action_requests=%s",
            len(decisions),
            len(action_requests),
        )
        decisions = [
            {"type": "reject", "message": "审批决策数量异常，系统已拒绝该工具调用。"}
            for _ in action_requests
        ]
    return {"decisions": decisions}

