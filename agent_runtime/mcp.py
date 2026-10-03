"""MCP connections shared by the Chainlit and LangGraph runtimes."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.server_info import load_mcp_server_info
from langchain_mcp_adapters.tools import load_mcp_tools

from config import config

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def load_agent_mcp_tools(
    exit_stack: AsyncExitStack, *, log_prefix: str,
) -> tuple[list[Any], str]:
    """Return official adapter tools and the servers' original instructions."""
    connections: dict[str, Any] = {
        "alint": {
            "command": sys.executable,
            "args": ["-m", "mcp_server.server"],
            "cwd": str(PROJECT_ROOT),
            "transport": "stdio",
        },
    }
    if config.vivado_mcp_command:
        connections["vivado-mcp"] = {
            "command": config.vivado_mcp_command,
            "args": ["--stdio-bridge"],
            "env": {"VIVADO_PATH": config.vivado_path} if config.vivado_path else {},
            "transport": "stdio",
        }
    if config.amd_doc_search_enabled:
        connections["amd-doc-search"] = {
            "url": config.amd_doc_search_url,
            "transport": "streamable_http",
            "timeout": 30,
            "sse_read_timeout": 60,
        }

    client = MultiServerMCPClient(connections)
    tools: list[Any] = []
    instructions: list[str] = []
    for server in connections:
        try:
            async with AsyncExitStack() as server_stack:
                if server == "amd-doc-search":
                    # Official stateless mode: each search owns its HTTP session.
                    # An idle/disconnected remote session cannot cancel the runtime.
                    server_tools = await asyncio.wait_for(
                        client.get_tools(server_name=server), timeout=30
                    )
                    try:
                        server_info = await asyncio.wait_for(
                            client.get_server_info(server_name=server), timeout=30
                        )
                        info = server_info[server]
                    except Exception as exc:
                        # Optional instructions must not discard discovered tools.
                        info = None
                        logger.warning(
                            "%s MCP server %s instructions unavailable; keeping tools: %s",
                            log_prefix, server, exc,
                        )
                else:
                    session = await server_stack.enter_async_context(
                        client.session(server, auto_initialize=False)
                    )
                    info = await asyncio.wait_for(load_mcp_server_info(session), timeout=30)
                    server_tools = await asyncio.wait_for(
                        load_mcp_tools(
                            session,
                            server_name=server,
                        ),
                        timeout=30,
                    )
                # Keep sessions open and close them in reverse order in the owner task.
                exit_stack.push_async_exit(server_stack.pop_all())
                tools.extend(server_tools)
                if info is not None and info.instructions:
                    instructions.append(info.instructions)
                logger.info("%s MCP server %s: loaded %d tools", log_prefix, server, len(server_tools))
        except Exception as exc:
            if server == "alint":
                raise
            logger.warning("%s Optional MCP server %s unavailable: %s", log_prefix, server, exc)
    return tools, "\n\n".join(instructions)
