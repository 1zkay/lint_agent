"""Shared tool loading for agent runtimes."""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientSession, ClientTimeout, TCPConnector
from langchain_community.tools import RequestsGetTool
from langchain_community.utilities.requests import TextRequestsWrapper
from langchain_tavily import TavilySearch

from agent_runtime.mcp import load_agent_mcp_tools
from rag.hardware_reference import build_hardware_reference_agentic_rag_tool
from config import config
from memory.long_term import build_memory_tools

logger = logging.getLogger(__name__)


@dataclass
class LoadedAgentTools:
    tools: list[Any]
    tool_names: list[str]
    tool_retry_tools: list[Any]
    mcp_instructions: str = ""


async def load_agent_tools(
    exit_stack: AsyncExitStack,
    *,
    log_prefix: str,
) -> LoadedAgentTools:
    """Load MCP, RAG, web, fetch-url, memory, and middleware-visible tools."""
    mcp_tools, mcp_instructions = await load_agent_mcp_tools(exit_stack, log_prefix=log_prefix)

    search_tools = [TavilySearch(max_results=5)] if os.getenv("TAVILY_API_KEY") else []
    http_session = await exit_stack.enter_async_context(ClientSession(
        timeout=ClientTimeout(total=30),
        connector=TCPConnector(limit_per_host=4),
        trust_env=True,
        raise_for_status=True,
    ))
    fetch_url_tool = RequestsGetTool(
        requests_wrapper=TextRequestsWrapper(aiosession=http_session),
        allow_dangerous_requests=True,
        name="fetch_url",
        description="Fetch the content of a URL. Input should be a URL string (e.g. https://example.com). Returns the text content of the page.",
    )

    try:
        rag_tool = build_hardware_reference_agentic_rag_tool(config)
    except Exception as exc:
        logger.warning("%s hardware-reference agentic RAG tool init failed: %s", log_prefix, exc)
        rag_tool = None

    native_tools = [
        *([rag_tool] if rag_tool else []),
        *search_tools,
        fetch_url_tool,
        *build_memory_tools(),
    ]
    tools = [*mcp_tools, *native_tools]
    # Configured MCP servers must explicitly declare retry-safe behavior.
    retryable_mcp_tools = [
        tool for tool in mcp_tools
        if any((tool.metadata or {}).get(hint) is True
               for hint in ("readOnlyHint", "idempotentHint"))
    ]
    tool_names = list(dict.fromkeys(getattr(tool, "name", str(tool)) for tool in tools))
    return LoadedAgentTools(
        tools=tools,
        tool_names=tool_names,
        tool_retry_tools=[*retryable_mcp_tools, *native_tools],
        mcp_instructions=mcp_instructions,
    )
