"""Shared prompts for LangChain agent runtime entrypoints."""

from langchain.agents.middleware.todo import WRITE_TODOS_SYSTEM_PROMPT

from memory.long_term import MEMORY_SYSTEM_PROMPT


WRITE_TODOS_ENHANCED_PROMPT = WRITE_TODOS_SYSTEM_PROMPT + """
## Critical: Real-Time Todo Updates
- You MUST call `write_todos` IMMEDIATELY after completing each individual task — do NOT batch multiple completions into one call.
- The correct per-task cycle is: mark task `in_progress` → do the work → mark task `completed` (and mark next task `in_progress`) → move on.
- Each `write_todos` call updates the UI in real time. If you skip intermediate calls, the user sees stale progress.

## Critical: Final Answer Placement — One Turn, One Response
When you are ready to deliver the final answer to the user, you MUST follow this exact pattern in a **single LLM turn**:
1. Write your complete, polished final answer as the text of your message.
2. In that **same turn**, call `write_todos` to mark all remaining tasks as `completed`.

**After that turn, output nothing further.** Do NOT add a follow-up turn with phrases like "Done!", "Task complete.", "Let me know if you need anything else.", or any other closing remarks. The turn that contains both the final answer and the final `write_todos` call is the last turn — stop there.

Why this matters: the system displays the text from the write_todos turn as the user-visible response. Any text produced in a subsequent "termination" turn will either overwrite or conflict with the real answer, causing the user to see incomplete or low-quality output.
"""

SYSTEM_PROMPT = """
你是一位资深 Verilog/SystemVerilog 硬件设计专家。
""".strip() + "\n\n" + MEMORY_SYSTEM_PROMPT
