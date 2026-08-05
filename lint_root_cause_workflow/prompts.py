"""Stage-specific prompts for the lint root-cause workflow."""

from __future__ import annotations

from langchain_core.output_parsers import PydanticOutputParser

from .state import SlicePolicy


SLICE_POLICY_PARSER = PydanticOutputParser(pydantic_object=SlicePolicy)


CLASSIFIER_SYSTEM_PROMPT = """
You are a senior Verilog/SystemVerilog design-structure analyst. Perform only
semantic module classification for the prepared design paths supplied by the
workflow. Use all available filesystem and analysis tools when useful, but do
not preprocess inputs, build slices, analyze lint root causes, or modify files.
""".strip()


ROOT_CAUSE_ANALYSIS_BATCH_SYSTEM_PROMPT = """
You are a senior Verilog/SystemVerilog lint root-cause analyst. Analyze only the
assigned batch of physical work units and follow the bundled
verilog-lint-root-cause-csv skill. Treat every work unit independently, use only
its copied source evidence, and write each exact local report target.
""".strip()


ROOT_CAUSE_GLOBAL_MERGE_SYSTEM_PROMPT = """
You are a senior Verilog/SystemVerilog lint root-cause analyst. Consolidate
every item in the supplied local-root catalog into one global root mapping by
following the bundled verilog-lint-root-cause-csv skill. Use the work-unit
evidence when useful, do not inspect another mapping output, and write only the
exact mapping target.
""".strip()


ROOT_CAUSE_JUDGE_SYSTEM_PROMPT = """
Follow the bundled verilog-lint-root-cause-csv skill. Compare the supplied
global mapping proposals item by item and write one complete mapping. Review
every local item and every proposed global root definition. When proposals
differ or evidence is ambiguous, reopen the corresponding work-unit lint rows
and RTL before deciding. Do not decide by majority agreement.
""".strip()


def build_classifier_prompt(
    *,
    rtl_dir: str,
    filelist_path: str,
    hierarchy_available: bool,
    hierarchy_tree_path: str,
    design_metadata_path: str,
    previous_error: str,
) -> str:
    retry_context = (
        f"\nThe previous policy failed deterministic validation:\n{previous_error}\n"
        if previous_error
        else ""
    )
    if hierarchy_available:
        structure_input = f"""
- hierarchy tree: {hierarchy_tree_path}

Classify every module present in the active hierarchy. Do not classify modules
absent from it. Structural depth is evidence, not the semantic decision rule.
The level4 module must be the elaborated top module.
""".strip()
    else:
        structure_input = """
Hierarchy elaboration is unavailable. Read the filelist, module inventory, and
RTL sources directly; classify every inventoried source module and infer exactly
one design top for level4. Do not invent modules or instance paths.
""".strip()
    return f"""
Classify every selected module exactly once into these four semantic levels:

- level1: reusable primitive or common support units, such as synchronizers,
  reset cells, gates, arbiters, encoders, or generic storage cells;
- level2: independently understandable modules implementing one clear function;
- level3: non-top composite, coordinating, or subsystem modules;
- level4: exactly one design top module.

Resolve overlaps deterministically: the design top is always level4; among
non-top modules, assign reusable primitive or common support units to level1,
then assign modules that coordinate multiple child functions or form a
subsystem boundary to level3, and assign all remaining single-function modules
to level2. Base the decision on RTL responsibility and composition, not module
name, hierarchy depth, or instance count alone.

Read:
- RTL directory: {rtl_dir}
- filelist: {filelist_path}
- design metadata: {design_metadata_path}

{structure_input}

Return only one JSON object without commentary or Markdown. Follow this format
exactly:

{SLICE_POLICY_PARSER.get_format_instructions()}
{retry_context}
""".strip()


def build_analysis_batch_prompt(
    *,
    members: list[tuple[str, str, str]],
    previous_error: str,
) -> str:
    revision = (
        f"""
The assigned reports failed deterministic validation. Revise only these exact
targets. Validator output:
{previous_error}
""".strip()
        if previous_error
        else "Create every member report at its exact local report path."
    )
    assignments = "\n".join(
        f"- unit {unit_id}\n  directory: {unit_dir}\n  report: {report_path}"
        for unit_id, unit_dir, report_path in members
    )
    return f"""
Assigned work units and report targets:
{assignments}

{revision}
""".strip()


def build_global_merge_prompt(
    *,
    slices_dir: str,
    local_catalog_path: str,
    map_path: str,
    previous_error: str,
) -> str:
    revision = (
        f"""
The mapping failed deterministic validation. Reopen and revise the same file.
Validator output:
{previous_error}
""".strip()
        if previous_error
        else "Create the complete global mapping at the exact output path above."
    )
    return f"""
- slices directory: {slices_dir}
- local-root catalog: {local_catalog_path}
- mapping output path: {map_path}

Map every local_item_id exactly once. Use exactly these columns:
local_item_id,global_root_id,root_note,fix_suggestion,parent_global_root_id

{revision}
""".strip()


def build_adjudication_prompt(
    *,
    slices_dir: str,
    local_catalog_path: str,
    candidate_maps: list[str],
    adjudicated_map_path: str,
    previous_error: str,
) -> str:
    revision = (
        f"""
The mapping failed deterministic validation. Reopen and revise the same file.
Validator output:
{previous_error}
""".strip()
        if previous_error
        else "Create the complete mapping at the exact output path above."
    )
    candidates = "\n".join(f"  - {path}" for path in candidate_maps)
    return f"""
- slices directory: {slices_dir}
- local-root catalog: {local_catalog_path}
- global mapping proposals:
{candidates}
- mapping output path: {adjudicated_map_path}

Review every local_item_id row, synthesize disagreements from evidence, and
use exactly these columns:
local_item_id,global_root_id,root_note,fix_suggestion,parent_global_root_id

{revision}
""".strip()
