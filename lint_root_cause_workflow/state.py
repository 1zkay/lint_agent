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


class CandidateReport(TypedDict):
    candidate_id: int
    report_path: str


class WorkflowState(WorkflowInput, total=False):
    run_dir: str
    rtl_dir: str
    filelist_path: str
    hierarchy_status_path: str
    design_metadata_path: str
    policy_path: str
    slices_dir: str
    draft_csv: str
    report_path: str
    slice_error: str
    validation_error: str
    candidate_reports: Annotated[list[CandidateReport], operator.add]


class CandidateWorkflowInput(TypedDict):
    candidate_id: int
    candidate_draft_csv: str
    rtl_dir: str
    filelist_path: str
    slices_dir: str


class CandidateWorkflowOutput(TypedDict):
    candidate_reports: list[CandidateReport]


class CandidateWorkflowState(CandidateWorkflowInput, total=False):
    validation_error: str
    candidate_reports: list[CandidateReport]


class SlicePolicy(BaseModel):
    """Structured semantic ownership returned by the classifier agent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    level1: list[str]
    level2: list[str]
    level3: list[str]
    level4: list[str]
