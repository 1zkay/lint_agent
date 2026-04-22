#!/usr/bin/env python3
"""Prebuild the configured hardware-reference PDF vector indexes used by agentic RAG."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(APP_DIR))

from agentic_rag import get_hardware_reference_agentic_rag_service
from config import config


async def main() -> int:
    if not config.rag_enabled:
        print("RAG is disabled by config.")
        return 1

    available_pdfs = [
        Path(str(path)).resolve()
        for path in getattr(config, "rag_pdf_paths", [])
        if Path(str(path)).resolve().exists()
    ]
    if not available_pdfs:
        print("No configured PDF knowledge base was found.")
        return 1

    service = get_hardware_reference_agentic_rag_service(config)
    await service.ensure_vectorstores()
    print(json.dumps(service.index_summary(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
