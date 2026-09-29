"""Shared LangChain chat-model helpers."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from langchain.chat_models import init_chat_model


def is_openrouter_base_url(base_url: str | None) -> bool:
    """Return True when the configured base URL points to OpenRouter."""

    return bool(base_url and "openrouter.ai" in base_url.lower())


def is_siliconflow_base_url(base_url: str | None) -> bool:
    """Return True when the configured base URL points to SiliconFlow."""

    return bool(base_url and "siliconflow" in base_url.lower())


def build_responses_client_kwargs(base_url: str | None) -> dict[str, Any]:
    """Convert a full Responses endpoint to SDK client options, or return {}."""

    if not base_url:
        return {}
    url = urlsplit(base_url)
    path = url.path.rstrip("/")
    if not path.lower().endswith("/responses"):
        return {}

    # The SDK appends /responses itself; query parameters belong in default_query.
    kwargs: dict[str, Any] = {
        "base_url": url._replace(
            path=path[: -len("/responses")], query="", fragment=""
        ).geturl(),
    }
    if url.query:
        kwargs["default_query"] = dict(parse_qsl(url.query, keep_blank_values=True))
    return kwargs


def build_openrouter_default_headers(base_url: str, cfg: Any) -> dict[str, str] | None:
    """Return OpenRouter-required headers for OpenRouter-compatible endpoints."""

    if is_openrouter_base_url(base_url):
        return {
            "HTTP-Referer": cfg.openrouter_referer,
            "X-OpenRouter-Title": cfg.openrouter_title,
        }
    return None


def build_chat_model_from_config(
    cfg: Any,
    *,
    temperature: float | None = None,
    logger: logging.Logger | None = None,
    log_prefix: str = "[llm.factory]",
):
    """Build a LangChain chat model from the shared config object."""

    if not cfg.llm_model:
        return None

    kwargs: dict[str, Any] = {
        "temperature": cfg.llm_temperature if temperature is None else temperature,
        "max_tokens": cfg.llm_max_tokens,
        "timeout": cfg.llm_timeout,
    }
    if cfg.llm_api_key:
        kwargs["api_key"] = cfg.llm_api_key
    if cfg.llm_base_url:
        kwargs["base_url"] = cfg.llm_base_url

    provider = None
    model_name = cfg.llm_model
    if ":" in model_name:
        provider, model_name = model_name.split(":", 1)
        provider = provider.replace("-", "_").lower()
    if provider in (None, "openai", "azure_openai"):
        responses_kwargs = build_responses_client_kwargs(cfg.llm_base_url)
        if responses_kwargs:
            kwargs.update(responses_kwargs)
            kwargs["use_responses_api"] = True
            provider = provider or "openai"
            if logger is not None:
                logger.info("%s Responses endpoint detected, enabling Responses API", log_prefix)

    if is_siliconflow_base_url(cfg.llm_base_url):
        kwargs["streaming"] = False
        if logger is not None:
            logger.info("%s SiliconFlow detected, disabling model streaming", log_prefix)

    default_headers = build_openrouter_default_headers(cfg.llm_base_url, cfg)
    if default_headers:
        kwargs["default_headers"] = default_headers
        if logger is not None:
            logger.info(
                "%s OpenRouter detected, injecting required HTTP-Referer / X-OpenRouter-Title headers",
                log_prefix,
            )

    if provider is not None:
        return init_chat_model(model_name, model_provider=provider, **kwargs)
    return init_chat_model(model_name, **kwargs)
