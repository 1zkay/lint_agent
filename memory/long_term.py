"""Official long-term memory helpers for the chat agent."""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from langchain.tools import ToolRuntime, tool
from langchain_openai import OpenAIEmbeddings
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, Field

from llm.factory import build_openrouter_default_headers

logger = logging.getLogger(__name__)


MEMORY_SYSTEM_PROMPT = """
Long-term memory tools are available.
- At the start of every user turn, you must call `get_user_profile` exactly once before answering, planning, or deciding whether other memory tools are needed.
- If `get_user_profile` returns "No saved user profile." or "Long-term memory store is unavailable.", continue normally.
- Use `search_user_memories` when prior user preferences or durable facts beyond the structured profile may matter.
- Use `save_user_profile` for stable structured preferences and profile facts.
- Use `remember_user_fact` only for durable, reusable facts that could help in future conversations.
- Never store raw source code, lint reports, large tool outputs, passwords, API keys, or other secrets in long-term memory.
- Keep saved memories concise, normalized, and useful across future threads.
""".strip()


@dataclass(frozen=True)
class AgentContext:
    """Per-run immutable context passed to the agent.

    Official LangGraph frontends such as Agent Chat UI connect with only the
    deployment URL and graph ID. They do not require callers to always provide
    a custom context payload, so fields must remain optional and be recoverable
    from runtime metadata.
    """

    user_id: str | None = None
    thread_id: str | None = None
    authenticated: bool = False


class UserProfileUpdate(BaseModel):
    """Structured user profile fields for durable personalization."""

    name: str | None = Field(default=None, description="User's preferred name.")
    preferred_language: str | None = Field(
        default=None,
        description="Preferred reply language, for example Chinese or English.",
    )
    response_style: str | None = Field(
        default=None,
        description="Preferred response style, for example concise or detailed.",
    )
    preferred_output_format: str | None = Field(
        default=None,
        description="Preferred output format, for example markdown or json.",
    )
    default_workspace: str | None = Field(
        default=None,
        description="Commonly used workspace path if the user explicitly states one.",
    )
    organization: str | None = Field(default=None, description="User organization or team.")
    role: str | None = Field(default=None, description="User role or job function.")
    notes: str | None = Field(
        default=None,
        description="Short stable note that could improve future assistance.",
    )


class MemoryFactInput(BaseModel):
    """A single durable memory item."""

    text: str = Field(description="The durable fact to remember.")
    category: str = Field(
        default="general",
        description="Short category such as preference, workflow, or personal.",
    )
    importance: str = Field(
        default="normal",
        description="Relative importance such as low, normal, or high.",
    )
    source: str = Field(
        default="conversation",
        description="Where the memory came from.",
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _profile_namespace(user_id: str) -> tuple[str, ...]:
    return ("users", user_id)


def _memory_namespace(user_id: str) -> tuple[str, ...]:
    return ("users", user_id, "memories")


async def _aget_profile(store: Any, user_id: str) -> dict[str, Any]:
    item = await store.aget(_profile_namespace(user_id), "profile")
    return dict(item.value) if item else {}


def _format_json_payload(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _context_from_runtime(runtime: ToolRuntime[AgentContext]) -> AgentContext:
    """Return explicit context when present, otherwise derive it from runtime.

    This matches the official LangGraph runtime model: callers may omit custom
    context entirely, while execution identity and authenticated server user
    information remain available via runtime metadata.
    """

    context = getattr(runtime, "context", None)
    execution_info = getattr(runtime, "execution_info", None)
    server_info = getattr(runtime, "server_info", None)
    server_user = getattr(server_info, "user", None)

    explicit_context = context if isinstance(context, AgentContext) else AgentContext()
    thread_id = (
        _optional_text(explicit_context.thread_id)
        or _optional_text(getattr(execution_info, "thread_id", None))
        or _optional_text(getattr(execution_info, "run_id", None))
        or "threadless"
    )
    user_id = (
        _optional_text(explicit_context.user_id)
        or _optional_text(getattr(server_user, "identity", None))
        or f"anonymous:{thread_id}"
    )

    return AgentContext(
        user_id=user_id,
        thread_id=thread_id,
        authenticated=bool(explicit_context.authenticated or server_user is not None),
    )


@tool
async def get_user_profile(runtime: ToolRuntime[AgentContext]) -> str:
    """Read the current user's long-term profile."""

    if runtime.store is None:
        return "Long-term memory store is unavailable."

    context = _context_from_runtime(runtime)
    profile = await _aget_profile(runtime.store, context.user_id)
    if not profile:
        return "No saved user profile."
    return _format_json_payload(profile)


@tool
async def save_user_profile(
    user_profile: UserProfileUpdate,
    runtime: ToolRuntime[AgentContext],
) -> str:
    """Save or update stable profile information for the current user."""

    if runtime.store is None:
        return "Long-term memory store is unavailable."

    updates = user_profile.model_dump(exclude_none=True)
    if not updates:
        return "No profile fields were provided."

    context = _context_from_runtime(runtime)
    current = await _aget_profile(runtime.store, context.user_id)
    merged = {
        **current,
        **updates,
        "user_id": context.user_id,
        "updated_at": _utc_now_iso(),
    }
    if "created_at" not in merged:
        merged["created_at"] = merged["updated_at"]

    await runtime.store.aput(
        _profile_namespace(context.user_id),
        "profile",
        merged,
        index=False,
    )
    return "Saved user profile."


@tool
async def remember_user_fact(
    memory: MemoryFactInput,
    runtime: ToolRuntime[AgentContext],
) -> str:
    """Save one durable long-term memory fact for the current user."""

    if runtime.store is None:
        return "Long-term memory store is unavailable."

    text = memory.text.strip()
    if not text:
        return "Memory text is empty."

    memory_id = str(uuid.uuid4())
    context = _context_from_runtime(runtime)
    payload = {
        "text": text,
        "category": memory.category.strip() or "general",
        "importance": memory.importance.strip() or "normal",
        "source": memory.source.strip() or "conversation",
        "thread_id": context.thread_id,
        "created_at": _utc_now_iso(),
    }
    await runtime.store.aput(
        _memory_namespace(context.user_id),
        memory_id,
        payload,
        index=["text"],
    )
    return f"Saved memory: {memory_id}"


@tool
async def search_user_memories(
    query: str,
    runtime: ToolRuntime[AgentContext],
    limit: int = 5,
    category: str | None = None,
) -> str:
    """Search durable long-term memories for the current user."""

    if runtime.store is None:
        return "Long-term memory store is unavailable."

    normalized_query = str(query or "").strip()
    if not normalized_query:
        return "Search query is empty."

    search_limit = max(1, min(int(limit or 5), 10))
    filters = {"category": category} if category else None
    context = _context_from_runtime(runtime)
    items = await runtime.store.asearch(
        _memory_namespace(context.user_id),
        query=normalized_query,
        filter=filters,
        limit=search_limit,
    )
    if not items:
        return "No relevant long-term memories found."

    payload = [
        {
            "memory_id": item.key,
            "score": item.score,
            **item.value,
        }
        for item in items
    ]
    return _format_json_payload(payload)


@tool
async def forget_user_memory(
    memory_id: str,
    runtime: ToolRuntime[AgentContext],
) -> str:
    """Delete one saved long-term memory item by memory id."""

    if runtime.store is None:
        return "Long-term memory store is unavailable."

    memory_id = str(memory_id or "").strip()
    if not memory_id:
        return "Memory id is empty."

    context = _context_from_runtime(runtime)
    await runtime.store.adelete(_memory_namespace(context.user_id), memory_id)
    return f"Deleted memory: {memory_id}"


def build_memory_tools() -> list[Any]:
    """Return the official ToolRuntime-based long-term memory tools."""

    return [
        get_user_profile,
        search_user_memories,
        save_user_profile,
        remember_user_fact,
        forget_user_memory,
    ]


def _build_memory_embeddings(cfg: Any) -> OpenAIEmbeddings:
    kwargs: dict[str, Any] = {
        "model": cfg.memory_embed_model,
        "base_url": cfg.memory_embed_base_url,
        "api_key": cfg.memory_embed_api_key or None,
        "timeout": cfg.llm_timeout,
    }
    if int(getattr(cfg, "memory_embed_dims", 0) or 0) > 0:
        kwargs["dimensions"] = int(cfg.memory_embed_dims)
    default_headers = build_openrouter_default_headers(cfg.memory_embed_base_url, cfg)
    if default_headers:
        kwargs["default_headers"] = default_headers
    if cfg.memory_embed_base_url and "openai.com" not in cfg.memory_embed_base_url:
        kwargs["tiktoken_enabled"] = False
        kwargs["check_embedding_ctx_length"] = False
    return OpenAIEmbeddings(**kwargs)


async def _resolve_memory_index_config(cfg: Any) -> dict[str, Any] | None:
    if not cfg.memory_enable_semantic_search:
        return None

    embeddings = _build_memory_embeddings(cfg)
    dims = int(getattr(cfg, "memory_embed_dims", 0) or 0)
    if dims <= 0:
        probe = await embeddings.aembed_query("memory dimension probe")
        dims = len(probe)

    if dims <= 0:
        raise ValueError("Failed to infer embedding dimensions for long-term memory.")

    logger.info(
        "[memory] Semantic search enabled for long-term memory "
        "(model=%s, dims=%s)",
        cfg.memory_embed_model,
        dims,
    )
    return {
        "embed": embeddings,
        "dims": dims,
        "fields": ["$"],
    }


async def build_memory_store(
    cfg: Any,
    exit_stack: AsyncExitStack,
) -> Any:
    """Build the LangGraph long-term memory store."""

    backend = (cfg.memory_store_backend or "postgres").strip().lower()
    index_config = await _resolve_memory_index_config(cfg)

    if backend == "memory":
        logger.info("[memory] Store: InMemoryStore")
        return InMemoryStore(index=index_config)

    if backend != "postgres":
        logger.warning(
            "[memory] Unsupported MEMORY_STORE_BACKEND=%s, using postgres.",
            backend,
        )

    db_uri = (cfg.memory_store_db_uri or cfg.checkpointer_db_uri or "").strip()
    if not db_uri:
        raise ValueError("MEMORY_STORE_DB_URI is empty.")

    from langgraph.store.postgres.aio import AsyncPostgresStore  # type: ignore

    store = await exit_stack.enter_async_context(
        AsyncPostgresStore.from_conn_string(
            db_uri,
            index=index_config,
        )
    )
    if cfg.memory_store_auto_setup:
        await store.setup()
    logger.info("[memory] Store: AsyncPostgresStore (%s)", db_uri)
    return store
