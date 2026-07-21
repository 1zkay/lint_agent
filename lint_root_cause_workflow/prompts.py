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
Never use `cat`; return the requested JSON classification directly, without
commentary.
""".strip()


ROOT_CAUSE_CANDIDATE_SYSTEM_PROMPT = """
You are a senior Verilog/SystemVerilog lint root-cause analyst. Follow the
bundled verilog-lint-root-cause-csv skill with the prepared paths and exact
output target supplied by the workflow. Use all available tools when useful.
""".strip()


ROOT_CAUSE_JUDGE_SYSTEM_PROMPT = """
Follow the bundled verilog-lint-root-cause-csv skill. Synthesize the candidate
reports supplied by the workflow into the final root-cause CSV. Review every
proposed root and every leaf row within it. For each leaf row, reopen its slice
lint entry and corresponding RTL source before deciding its root assignment,
root cause, repair, parent relationship, or false-positive status. Do not accept
a conclusion only because the candidate reports agree. Write only the final
root-cause conclusions to the exact output target supplied by the workflow.
""".strip()


def build_classifier_prompt(
    *,
    rtl_dir: str,
    hierarchy_tree_path: str,
    design_metadata_path: str,
    previous_error: str,
) -> str:
    retry_context = (
        f"\nThe previous policy failed deterministic validation:\n{previous_error}\n"
        if previous_error
        else ""
    )
    return f"""
Classify every active module exactly once into these four semantic levels:

- level1: reusable primitive or common support units, such as synchronizers,
  reset cells, gates, arbiters, encoders, or generic storage cells;
- level2: independently understandable modules implementing one clear function;
- level3: non-top composite, coordinating, or subsystem modules;
- level4: exactly the elaborated top module.

Read:
- RTL directory: {rtl_dir}
- hierarchy tree: {hierarchy_tree_path}
- design metadata: {design_metadata_path}

Do not classify modules absent from the active hierarchy. Structural depth is
evidence, not the semantic decision rule. Return only one JSON object without
commentary or Markdown. Follow this format exactly:

{SLICE_POLICY_PARSER.get_format_instructions()}
{retry_context}
""".strip()


def build_root_cause_prompt(
    *,
    rtl_dir: str,
    slices_dir: str,
    filelist_path: str,
    draft_csv: str,
    previous_error: str,
) -> str:
    revision = (
        f"""
The existing draft failed deterministic validation. Reopen and revise the same
draft file; do not create another report. Validator output:
{previous_error}
""".strip()
        if previous_error
        else "Create the first draft at the exact output path below."
    )
    return f"""
    Prepared inputs:
    - RTL directory: {rtl_dir}
    - slices directory: {slices_dir}
    - filelist: {filelist_path}

    Draft output path: {draft_csv}

    {revision}
    """.strip()


def build_adjudication_prompt(
    *,
    rtl_dir: str,
    slices_dir: str,
    filelist_path: str,
    candidate_reports: list[str],
    draft_csv: str,
    previous_error: str,
) -> str:
    revision = (
        f"""
The adjudicated draft failed deterministic validation. Reopen and revise the
same draft file; do not create another report. Validator output:
{previous_error}
""".strip()
        if previous_error
        else "Create the adjudicated draft at the exact output path below."
    )
    candidate_list = "\n".join(f"- {path}" for path in candidate_reports)
    return f"""
    Prepared design evidence:
    - RTL directory: {rtl_dir}
    - slices directory: {slices_dir}
    - filelist: {filelist_path}

    Independently generated, deterministically validated candidate reports:
    {candidate_list}

    Adjudicated draft output path: {draft_csv}

    {revision}
    """.strip()
