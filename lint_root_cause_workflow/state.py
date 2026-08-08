"""Typed state contracts for the lint root-cause workflow."""

import operator
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict
from typing_extensions import TypedDict


class WorkflowInput(TypedDict):
    source_path: str
    lint_csv_path: str
    top_module: str


class WorkflowOutput(TypedDict):
    report_path: str


class WorkUnitResult(TypedDict):
    unit_id: str
    report_path: str


class MergeCandidateResult(TypedDict):
    candidate_id: int
    map_path: str


class WorkflowState(WorkflowInput, total=False):
    run_dir: str
    rtl_dir: str
    filelist_path: str
    hierarchy_status_path: str
    design_metadata_path: str
    policy_path: str
    slices_dir: str
    local_catalog_path: str
    adjudicated_map_path: str
    draft_csv: str
    report_path: str
    hierarchy_resolution_complete: bool
    filelist_recovery_error: str
    filelist_recovery_history: list[str]
    slice_error: str
    validation_error: str
    work_unit_results: Annotated[list[WorkUnitResult], operator.add]
    merge_candidate_results: Annotated[list[MergeCandidateResult], operator.add]


class WorkUnitAssignment(TypedDict):
    unit_id: str
    work_unit_dir: str


class AnalysisBatchInput(TypedDict):
    work_units: list[WorkUnitAssignment]


class AnalysisBatchOutput(TypedDict):
    work_unit_results: list[WorkUnitResult]


class AnalysisBatchState(AnalysisBatchInput, total=False):
    validation_error: str
    work_unit_results: list[WorkUnitResult]


class MergeCandidateInput(TypedDict):
    candidate_id: int
    map_path: str
    slices_dir: str
    local_catalog_path: str


class MergeCandidateOutput(TypedDict):
    merge_candidate_results: list[MergeCandidateResult]


class MergeCandidateState(MergeCandidateInput, total=False):
    validation_error: str
    merge_candidate_results: list[MergeCandidateResult]


class SlicePolicy(BaseModel):
    """Structured semantic ownership returned by the classifier agent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    level1: list[str]
    level2: list[str]
    level3: list[str]
    level4: list[str]


class FilelistRecoveryPlan(BaseModel):
    """Constrained filelist recovery decision returned by the resolver agent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    action: Literal["retry", "module_fallback"]
    reason: str
