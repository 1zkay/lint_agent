"""Shared LangChain chat-model helpers."""
from __future__ import annotations

import logging
from typing import Any

from langchain.chat_models import init_chat_model


def is_openrouter_base_url(base_url: str | None) -> bool:
    """Return True when the configured base URL points to OpenRouter."""

    return bool(base_url and "openrouter.ai" in base_url.lower())


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
    logger: logging.Logger | None = None,
    log_prefix: str = "[llm_factory]",
):
    """Build a LangChain chat model from the shared config object."""

    if not cfg.llm_model:
        return None

    kwargs: dict[str, Any] = {
        "temperature": cfg.llm_temperature,
        "max_tokens": cfg.llm_max_tokens,
        "timeout": cfg.llm_timeout,
    }
    if cfg.llm_api_key:
        kwargs["api_key"] = cfg.llm_api_key
    if cfg.llm_base_url:
        kwargs["base_url"] = cfg.llm_base_url

    default_headers = build_openrouter_default_headers(cfg.llm_base_url, cfg)
    if default_headers:
        kwargs["default_headers"] = default_headers
        if logger is not None:
            logger.info(
                "%s OpenRouter detected, injecting required HTTP-Referer / X-OpenRouter-Title headers",
                log_prefix,
            )

    if ":" in cfg.llm_model:
        provider, model_name = cfg.llm_model.split(":", 1)
        return init_chat_model(model_name, model_provider=provider, **kwargs)
    return init_chat_model(cfg.llm_model, **kwargs)
