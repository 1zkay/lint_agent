"""反思中间件：实现 Evaluator-Optimizer 闭环。

通过 `awrap_model_call` 对草稿回答进行评估，不通过则注入反馈后重试。
草稿生成与评估调用使用 `TAG_NOSTREAM`，避免失败草稿流式泄漏到前端。
草稿通过时直接返回 `ModelResponse(result=[draft])`；
达到最大重试次数或发生 fail-open 场景时回退到 `handler(current_request)`。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.constants import TAG_NOSTREAM

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_core.language_models.chat_models import BaseChatModel

logger = logging.getLogger(__name__)

# ── 评估器 System Prompt ─────────────────────────────────────────────────────
# 评估器以完整对话历史 + 待评估草稿的形式接收上下文，
# 与 LangGraph Evaluator-Optimizer 教程中评估节点接收完整 state 的模式一致。
_EVALUATOR_SYSTEM_PROMPT = """\
You are a strict quality evaluator. You will receive the FULL conversation \
history (including the system prompt, user messages, assistant messages, \
tool calls, and tool outputs) followed by a draft response marked as \
[DRAFT TO EVALUATE].

Evaluate whether the draft response:
1. Directly addresses the user's latest question in context of the conversation
2. Is factually accurate and logically coherent
3. Provides sufficient detail without unnecessary verbosity
4. Does not contain hallucinated information

IMPORTANT:
- Tool outputs (ToolMessage) in the conversation are real data retrieved at \
runtime. Answers grounded in tool outputs are NOT hallucination.
- Consider the full conversation context when judging relevance — the draft \
may reference earlier turns.

Respond with EXACTLY one of:
- PASS
- FAIL: <concise feedback describing what to improve>
"""

# TAG_NOSTREAM config — 传递给 model.ainvoke() 以抑制流式输出
# StreamMessagesHandler.on_chat_model_start 检测到此标签后不注册 run_id，
# 从而阻止 token 被推送到 graph.astream(stream_mode="messages") 输出。
_NOSTREAM_CONFIG: dict[str, Any] = {"tags": [TAG_NOSTREAM]}


class ReflectionMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """对文本草稿做质量评估，并在需要时基于反馈重试。"""

    def __init__(
        self,
        *,
        model: BaseChatModel,
        max_reflections: int = 1,
    ) -> None:
        """初始化 ReflectionMiddleware.

        Args:
            model: 用于评估的 LLM（可与主模型相同，也可使用更轻量的模型）。
            max_reflections: 最大反思迭代次数（默认 1，即最多重试一次）。
                Must be >= 1.

        Raises:
            ValueError: 如果 max_reflections < 1。
        """
        super().__init__()
        if max_reflections < 1:
            msg = f"max_reflections must be >= 1, got {max_reflections}"
            raise ValueError(msg)
        self.model = model
        self.max_reflections = max_reflections

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_user_question(request: ModelRequest[Any]) -> str:
        """从消息列表中提取最近一条用户问题。"""
        for msg in reversed(request.messages):
            if isinstance(msg, HumanMessage) and msg.content:
                return msg.text.strip()
        return ""

    async def _generate_draft(
        self, request: ModelRequest[Any],
    ) -> AIMessage | None:
        """按请求参数对齐模型绑定，生成不流式输出的草稿。"""
        messages: list[Any] = []
        if request.system_message:
            messages.append(request.system_message)
        messages.extend(request.messages)

        model = request.model
        if request.tools:
            model = model.bind_tools(
                request.tools,
                tool_choice=request.tool_choice,
                **request.model_settings,
            )
        elif request.model_settings:
            model = model.bind(**request.model_settings)

        try:
            return await model.ainvoke(messages, config=_NOSTREAM_CONFIG)
        except Exception as exc:
            logger.warning(
                f"[ReflectionMiddleware] Draft generation failed (fail-open): {exc}"
            )
            return None

    async def _evaluate(
        self, draft: AIMessage,
        request: ModelRequest[Any],
    ) -> tuple[bool, str]:
        """基于完整对话上下文评估草稿质量，返回 `(是否通过, 反馈文本)`。

        评估器接收与生成器等量的上下文信息（完整对话历史 + 系统提示），
        与 LangGraph Evaluator-Optimizer 教程中评估节点接收完整 state 的模式一致。
        """
        # 构造评估消息：评估器 system prompt + 完整对话历史 + 待评估草稿 + 评估指令
        eval_messages: list[Any] = [SystemMessage(content=_EVALUATOR_SYSTEM_PROMPT)]

        # 注入原始 system prompt（让评估器了解 agent 的角色定义和行为约束）
        if request.system_message:
            eval_messages.append(request.system_message)

        # 注入完整对话历史（包含多轮问答、工具调用、工具输出等全部上下文）
        eval_messages.extend(request.messages)

        # 注入待评估的草稿，标记为评估对象
        eval_messages.append(AIMessage(
            content=f"[DRAFT TO EVALUATE]\n{draft.text}"
        ))

        # 评估指令
        eval_messages.append(HumanMessage(
            content="Please evaluate the draft above. Respond with PASS or FAIL: <feedback>."
        ))

        try:
            eval_response = await self.model.ainvoke(
                eval_messages, config=_NOSTREAM_CONFIG
            )
        except Exception as exc:
            logger.warning(
                f"[ReflectionMiddleware] Evaluator call failed (fail-open): {exc}"
            )
            return True, ""

        verdict = eval_response.text.strip()

        if verdict.upper().startswith("PASS"):
            return True, ""

        feedback = verdict
        if ":" in verdict:
            feedback = verdict.split(":", 1)[1].strip()
        return False, feedback

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """在模型调用外层执行反思循环，仅针对文本回答生效。"""
        user_question = self._extract_user_question(request)
        if not user_question:
            return await handler(request)

        current_request = request

        for iteration in range(self.max_reflections):
            draft = await self._generate_draft(current_request)
            if draft is None:
                break

            if draft.tool_calls:
                break

            draft_text = draft.text.strip()
            if not draft_text:
                break

            passed, feedback = await self._evaluate(
                draft, request=current_request,
            )
            if passed:
                logger.info(
                    "[ReflectionMiddleware] Draft passed evaluation "
                    f"(iteration {iteration + 1}/{self.max_reflections})"
                )
                return ModelResponse(result=[draft])

            logger.info(
                f"[ReflectionMiddleware] Draft failed evaluation "
                f"(iteration {iteration + 1}/{self.max_reflections}): {feedback}"
            )

            # ── 注入反馈 ────────────────────────────────────────────────
            reflection_feedback = HumanMessage(
                content=(
                    f"[Reflection feedback] Your previous answer was not satisfactory. "
                    f"Please improve based on the following feedback:\n{feedback}"
                )
            )
            new_messages = list(current_request.messages) + [draft, reflection_feedback]
            current_request = current_request.override(messages=new_messages)

        return await handler(current_request)
