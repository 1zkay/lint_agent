"""DeepAgents subagent definitions for the ALINT runtime."""

from __future__ import annotations

from typing import Any

from deepagents.middleware.filesystem import FilesystemPermission
from deepagents.middleware.subagents import SubAgent

REFERENCE_TOOL_NAMES = {"query_reference_docs"}

SUBAGENT_BOUNDARY_PROMPT = """## Delegation Boundary

You may use `ls`, `read_file`, `glob`, and `grep` to inspect project files.
Do not create, edit, move, or delete files. Return analysis only; the coordinator
is responsible for final CSV writing, root_id assignment, and validation.
"""

SUBAGENT_OUTPUT_PROMPT = """Return a concise Chinese report with:
- candidate_root_id: a temporary local identifier such as candidate_001
- root_range: filename:start-end
- leaf_violation_ids: the exact vio_* IDs covered
- root_note: concrete root cause
- fix_suggestion: concrete fix or waiver suggestion
- parent_candidate: another candidate ID only for a real derived relationship, otherwise /
- evidence: short source/rule evidence

Do not assign final root_### IDs and do not write files.
"""


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", "") or "")


def _select_tools(tools: list[Any] | None, allowed_names: set[str]) -> list[Any]:
    return [
        tool
        for tool in tools or []
        if _tool_name(tool) in allowed_names
    ]


def _read_only_filesystem_permissions() -> list[FilesystemPermission]:
    return [
        FilesystemPermission(
            operations=["write"],
            paths=["/**"],
            mode="deny",
        )
    ]


def _with_common_subagent_options(
    spec: SubAgent,
    *,
    normalized_skill_sources: list[str],
    enable_skills: bool,
) -> SubAgent:
    spec["permissions"] = _read_only_filesystem_permissions()
    if enable_skills and normalized_skill_sources:
        spec["skills"] = normalized_skill_sources
    return spec


def build_lint_subagents(
    llm: Any,
    *,
    tools: list[Any] | None,
    normalized_skill_sources: list[str],
    enable_skills: bool,
) -> list[SubAgent]:
    """Return broad, reusable DeepAgents subagent specs for lint analysis.

    The specs intentionally leave middleware construction to `create_deep_agent`,
    which applies the official DeepAgents stack to each child agent.
    """

    reference_tools = _select_tools(tools, REFERENCE_TOOL_NAMES)

    def with_common(spec: SubAgent) -> SubAgent:
        return _with_common_subagent_options(
            spec,
            normalized_skill_sources=normalized_skill_sources,
            enable_skills=enable_skills,
        )

    return [
        with_common({
            "name": "module-local-analyzer",
            "description": (
                "Analyze root causes that are local to one RTL module or one "
                "bounded always/assign/case source range."
            ),
            "system_prompt": (
                "You are an RTL lint root-cause analyst for local module evidence. "
                "Inspect only the provided lint rows and source slices, then group "
                "violations by the smallest source range where one edit would clear "
                "the group. Do not group unrelated locations just because fixes look similar.\n\n"
                + SUBAGENT_BOUNDARY_PROMPT
                + "\n\n"
                + SUBAGENT_OUTPUT_PROMPT
            ),
            "model": llm,
            "tools": reference_tools,
        }),
        with_common({
            "name": "cross-module-flow-analyzer",
            "description": (
                "Analyze cross-module clock, reset, enable, control, and data-flow "
                "relationships where the reported lint line may not be the root location."
            ),
            "system_prompt": (
                "You are an RTL cross-module flow analyst. Focus on instance wiring, "
                "clock/reset/control propagation, derived clocks, gated clocks, and "
                "hierarchical signal roles. Prefer the source location where changing "
                "one connection or control strategy would clear the linked violations.\n\n"
                + SUBAGENT_BOUNDARY_PROMPT
                + "\n\n"
                + SUBAGENT_OUTPUT_PROMPT
            ),
            "model": llm,
            "tools": reference_tools,
        }),
        with_common({
            "name": "style-policy-analyzer",
            "description": (
                "Analyze naming, style, and policy-only lint violations such as port, "
                "signal, instance, and parameter naming."
            ),
            "system_prompt": (
                "You are an RTL style-policy analyst. Group policy violations by the "
                "single declaration, port list, instance list, or naming block that "
                "would be edited together. Mark a row as false positive only when the "
                "provided RTL evidence proves the warning condition does not hold.\n\n"
                + SUBAGENT_BOUNDARY_PROMPT
                + "\n\n"
                + SUBAGENT_OUTPUT_PROMPT
            ),
            "model": llm,
            "tools": [],
        }),
        with_common({
            "name": "csv-reviewer",
            "description": (
                "Review root-cause CSV drafts for schema, leaf coverage, grouping "
                "consistency, parent references, and exact leaf_violation_note values."
            ),
            "system_prompt": (
                "You are a root-cause CSV contract reviewer. Do not redo full RTL "
                "analysis unless a row lacks evidence. Check that every input vio_* "
                "appears exactly once, repeated root fields are consistent, parent "
                "roots exist, false positives follow the contract, and leaf_violation_note "
                "matches message_id:description exactly. Return only issues and concise fixes."
            ),
            "model": llm,
            "tools": [],
        }),
    ]
