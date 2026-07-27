"""Typed state contracts for the lint root-cause workflow."""

import operator
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict
from typing_extensions import TypedDict


class WorkflowInput(TypedDict):
    source_path: str
    lint_csv_path: str


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
    slice_error: str
    validation_error: str
    work_unit_results: Annotated[list[WorkUnitResult], operator.add]
    merge_candidate_results: Annotated[list[MergeCandidateResult], operator.add]


class WorkUnitInput(TypedDict):
    unit_id: str
    work_unit_dir: str


class WorkUnitOutput(TypedDict):
    work_unit_results: list[WorkUnitResult]


class WorkUnitState(WorkUnitInput, total=False):
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
