"""LangGraph checkpointer factory helpers."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack

from langgraph.checkpoint.memory import InMemorySaver

from config import config

logger = logging.getLogger(__name__)


async def build_checkpointer(exit_stack: AsyncExitStack, *, log_prefix: str = "[agent_runtime]"):
    """
    Create the configured LangGraph checkpointer.

    Priority:
      1. PostgreSQL when configured and available.
      2. InMemorySaver fallback.
    """
    backend = (config.checkpointer_backend or "postgres").lower()
    db_uri = (config.checkpointer_db_uri or "").strip()

    if backend == "memory":
        logger.info("%s Checkpointer: InMemorySaver", log_prefix)
        return InMemorySaver()

    if backend != "postgres":
        logger.warning("%s 不支持的 CHECKPOINTER_BACKEND=%s，按 postgres 处理。", log_prefix, backend)

    if not db_uri:
        logger.warning("%s 未配置 CHECKPOINTER_DB_URI（PostgreSQL 连接串），回退 InMemorySaver。", log_prefix)
        return InMemorySaver()

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # type: ignore
    except Exception as exc:
        logger.warning(
            "%s 未安装 AsyncPostgresSaver 依赖（需要 langgraph-checkpoint-postgres/psycopg），回退 InMemorySaver。错误: %s",
            log_prefix,
            exc,
        )
        return InMemorySaver()

    try:
        checkpointer = await exit_stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(db_uri)
        )
        if config.checkpointer_auto_setup:
            await checkpointer.setup()
        logger.info("%s Checkpointer: AsyncPostgresSaver (%s)", log_prefix, db_uri)
        return checkpointer
    except Exception as exc:
        logger.error("%s 初始化 AsyncPostgresSaver 失败，回退 InMemorySaver: %s", log_prefix, exc, exc_info=True)
        return InMemorySaver()

