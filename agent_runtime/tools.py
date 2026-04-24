"""Shared tool loading for agent runtimes."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_community.tools import RequestsGetTool
from langchain_community.utilities.requests import TextRequestsWrapper
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_tavily import TavilySearch

from rag.hardware_reference import build_hardware_reference_agentic_rag_tool
from config import config
from memory.long_term import build_memory_tools

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FILESYSTEM_TOOL_NAMES = ("ls", "read_file", "write_file", "edit_file", "glob", "grep")


@dataclass
class LoadedAgentTools:
    tools: list[Any]
    tool_names: list[str]


async def load_agent_tools(
    exit_stack: AsyncExitStack,
    *,
    log_prefix: str,
) -> LoadedAgentTools:
    """Load MCP, RAG, web, fetch-url, memory, and middleware-visible tools."""
    client = MultiServerMCPClient(
        {
            "alint": {
                "command": sys.executable,
                "args": ["-m", "mcp_server.server"],
                "cwd": str(PROJECT_ROOT),
                "transport": "stdio",
            }
        }
    )
    session = await exit_stack.enter_async_context(client.session("alint"))
    mcp_tools = await load_mcp_tools(session)

    search_tools = [TavilySearch(max_results=5)] if os.getenv("TAVILY_API_KEY") else []
    fetch_url_tool = RequestsGetTool(
        requests_wrapper=TextRequestsWrapper(),
        allow_dangerous_requests=True,
        name="fetch_url",
        description="Fetch the content of a URL. Input should be a URL string (e.g. https://example.com). Returns the text content of the page.",
    )

    try:
        rag_tool = build_hardware_reference_agentic_rag_tool(config)
    except Exception as exc:
        logger.warning("%s hardware-reference agentic RAG tool init failed: %s", log_prefix, exc)
        rag_tool = None

    tools = [
        *mcp_tools,
        *([rag_tool] if rag_tool else []),
        *search_tools,
        fetch_url_tool,
        *build_memory_tools(),
    ]
    tool_names = list(dict.fromkeys([*(getattr(tool, "name", str(tool)) for tool in tools), *FILESYSTEM_TOOL_NAMES]))
    logger.info("%s Loaded tools: %s", log_prefix, tool_names)
    return LoadedAgentTools(
        tools=tools,
        tool_names=tool_names,
    )
