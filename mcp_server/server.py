"""ALINT-PRO FastMCP server assembly."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastmcp import FastMCP

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config
from mcp_server.prompts import register_prompts
from mcp_server.resources import register_resources
from mcp_server.tools import register_tools

logger = logging.getLogger(__name__)

mcp = FastMCP("ALINT-PRO Analysis Server")
register_resources(mcp)
register_prompts(mcp)
register_tools(mcp)


def main() -> None:
    """Run the ALINT-PRO MCP server."""
    logging.basicConfig(level=logging.INFO)
    config.validate()
    logger.info("Starting ALINT-PRO Analysis Server (LangGraph edition)...")
    mcp.run()


if __name__ == "__main__":
    main()
