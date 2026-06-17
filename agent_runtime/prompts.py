"""Shared prompts for LangChain agent runtime entrypoints."""

from memory.long_term import MEMORY_SYSTEM_PROMPT


SYSTEM_PROMPT = """
你是一位资深 Verilog/SystemVerilog 硬件设计专家。
""".strip() + "\n\n" + MEMORY_SYSTEM_PROMPT
