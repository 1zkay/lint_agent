"""Normalize known Responses stream failures through public model middleware hooks."""

from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.exceptions import (
    ModelAPIError,
    ModelError,
    ModelInvalidRequestError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from langchain_openai import AzureChatOpenAI, ChatOpenAI


_RESPONSE_ERROR_TYPES: dict[str, type[ModelError]] = {
    "server_error": ModelAPIError,
    "rate_limit_exceeded": ModelRateLimitError,
    "invalid_prompt": ModelInvalidRequestError,
    "vector_store_timeout": ModelTimeoutError,
}


def _response_model_error(request: ModelRequest, exc: ValueError) -> ModelError | None:
    if not isinstance(request.model, (ChatOpenAI, AzureChatOpenAI)):
        return None
    if request.model.use_responses_api is False:
        return None

    # langchain-openai 1.6.6 raises plain ValueError for `error` and
    # `response.failed`, losing the structured error. Match only its known
    # formats/codes; configuration and other unclassified ValueErrors propagate.
    # https://github.com/langchain-ai/langchain/pull/40791
    text = str(exc)
    for code, error_type in _RESPONSE_ERROR_TYPES.items():
        if text.startswith((f"{code}: ", f"ResponseError(code={code!r}, message=")):
            return error_type(text)
    return None


class OpenAIResponsesErrorMiddleware(AgentMiddleware):
    """Place inside ModelRetryMiddleware so standard retryability is respected.

    Uses the documented wrap hooks; tool errors and graph control signals are
    outside this adapter. Existing provider ModelErrors propagate unchanged.
    https://docs.langchain.com/oss/python/langchain/middleware/custom
    """

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        try:
            return handler(request)
        except ValueError as exc:
            error = _response_model_error(request, exc)
            if error is None:
                raise
            raise error from exc

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        try:
            return await handler(request)
        except ValueError as exc:
            error = _response_model_error(request, exc)
            if error is None:
                raise
            raise error from exc
