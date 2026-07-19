"""Node implementations for the three-stage lint root-cause workflow."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from config import config as app_config
from memory.long_term import AgentContext

from .prompts import (
    SLICE_POLICY_PARSER,
    build_adjudication_prompt,
    build_classifier_prompt,
    build_root_cause_prompt,
)
from .state import CandidateWorkflowState, WorkflowState


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "verilog-lint-root-cause-csv"
PREPARE_SCRIPT = SKILL_DIR / "scripts" / "prepare_hierarchy_inputs.py"
BUILD_SLICES_SCRIPT = SKILL_DIR / "scripts" / "build_slices.py"
SORT_SCRIPT = SKILL_DIR / "scripts" / "sort_root_cause_csv.py"
VALIDATE_SCRIPT = SKILL_DIR / "scripts" / "validate_root_cause_csv.py"


@dataclass(frozen=True)
class ScriptResult:
    returncode: int
    stdout: str
    stderr: str


async def _run_script(script: Path, *arguments: str) -> ScriptResult:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        *arguments,
        cwd=str(REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return ScriptResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def _script_error(result: ScriptResult) -> str:
    text = result.stderr.strip() or result.stdout.strip() or "script failed"
    return text[-4000:]


def _required_path(path: Path, *, directory: bool) -> Path:
    valid = path.is_dir() if directory else path.is_file()
    if not valid:
        kind = "directory" if directory else "file"
        raise FileNotFoundError(f"required {kind} not found: {path}")
    return path


def _stdout_value(result: ScriptResult, key: str) -> str:
    prefix = f"{key}="
    return next(
        (
            line.removeprefix(prefix).strip()
            for line in result.stdout.splitlines()
            if line.startswith(prefix)
        ),
        "",
    )


def _agent_context(runtime: Runtime[AgentContext]) -> AgentContext:
    return runtime.context or AgentContext()


def _cleanup_intermediate_run(run_dir: str) -> None:
    if app_config.lint_root_cause_keep_intermediates:
        return

    reports_dir = (REPO_ROOT / "reports").resolve()
    path = Path(run_dir).resolve()
    if path.parent != reports_dir:
        raise RuntimeError(
            f"refusing to remove intermediate directory outside reports/: {path}"
        )
    shutil.rmtree(path)


class WorkflowNodes:
    """Bind immutable agent graphs to stateless workflow node methods."""

    def __init__(
        self,
        *,
        classifier_agent: Any,
        candidate_agent: Any,
        judge_agent: Any,
        ensemble_size: int,
    ) -> None:
        self.classifier_agent = classifier_agent
        self.candidate_agent = candidate_agent
        self.judge_agent = judge_agent
        self.ensemble_size = ensemble_size

    async def prepare_inputs(self, state: WorkflowState) -> dict[str, Any]:
        source_path = Path(state["source_path"]).expanduser().resolve()
        lint_csv_path = _required_path(
            Path(state["lint_csv_path"]).expanduser().resolve(),
            directory=False,
        )
        if source_path.is_file():
            source_option = "--source-archive"
        elif source_path.is_dir():
            source_option = "--source-dir"
        else:
            raise FileNotFoundError(f"source input not found: {source_path}")

        result = await _run_script(
            PREPARE_SCRIPT,
            source_option,
            str(source_path),
            "--lint-report",
            str(lint_csv_path),
        )
        if result.returncode:
            raise RuntimeError(f"input preparation failed: {_script_error(result)}")

        project_name = _stdout_value(result, "PROJECT_NAME")
        run_dir_text = _stdout_value(result, "RUN_DIR")
        report_path_text = _stdout_value(result, "REPORT_PATH")
        if not project_name or not run_dir_text or not report_path_text:
            raise RuntimeError(
                "input preparation did not return PROJECT_NAME, RUN_DIR, and REPORT_PATH"
            )

        reports_dir = (REPO_ROOT / "reports").resolve()
        run_dir = _required_path(Path(run_dir_text).resolve(), directory=True)
        try:
            run_dir.relative_to(reports_dir)
        except ValueError as exc:
            raise RuntimeError(f"RUN_DIR is outside reports/: {run_dir}") from exc
        report_path = Path(report_path_text).resolve()
        if report_path.parent != reports_dir or not report_path.name.startswith(
            f"{project_name}_root_cause_"
        ):
            raise RuntimeError(f"unexpected REPORT_PATH: {report_path}")

        rtl_dir = _required_path(run_dir / "rtl", directory=True)
        filelist_path = _required_path(run_dir / "filelist.f", directory=False)
        work_dir = _required_path(run_dir / "work", directory=True)
        hierarchy_tree_path = _required_path(
            work_dir / "hierarchy_tree.txt", directory=False
        )
        _required_path(work_dir / "lint_entries_mapped.csv", directory=False)
        design_metadata_path = _required_path(
            work_dir / "design_metadata.json", directory=False
        )
        return {
            "run_dir": str(run_dir),
            "rtl_dir": str(rtl_dir),
            "filelist_path": str(filelist_path),
            "hierarchy_tree_path": str(hierarchy_tree_path),
            "design_metadata_path": str(design_metadata_path),
            "policy_path": str(work_dir / "slice_policy.json"),
            "slices_dir": "",
            "draft_csv": str(work_dir / "root_cause_draft.csv"),
            "report_path": str(report_path),
            "slice_error": "",
            "validation_error": "",
            "candidate_reports": [],
        }

    async def classify_and_slice(
        self,
        state: WorkflowState,
        config: RunnableConfig,
        runtime: Runtime[AgentContext],
    ) -> dict[str, Any]:
        prompt = build_classifier_prompt(
            rtl_dir=state["rtl_dir"],
            hierarchy_tree_path=state["hierarchy_tree_path"],
            design_metadata_path=state["design_metadata_path"],
            previous_error=state.get("slice_error", ""),
        )
        result = await self.classifier_agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=config,
            context=_agent_context(runtime),
        )

        try:
            policy = SLICE_POLICY_PARSER.invoke(result["messages"][-1])
        except (KeyError, IndexError, TypeError, OutputParserException) as exc:
            return self._slice_failure(f"invalid classifier JSON: {exc}")

        policy_path = Path(state["policy_path"])
        temporary_path = policy_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(policy.model_dump(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, policy_path)

        build_result = await _run_script(
            BUILD_SLICES_SCRIPT,
            "--project-dir",
            state["run_dir"],
            "--tree-dir",
            str(Path(state["hierarchy_tree_path"]).parent),
            "--policy",
            state["policy_path"],
        )
        if build_result.returncode == 2:
            return self._slice_failure(_script_error(build_result))
        if build_result.returncode:
            raise RuntimeError(f"slice builder failed: {_script_error(build_result)}")

        slices_dir = _required_path(
            Path(state["run_dir"]) / "slices", directory=True
        )
        _required_path(slices_dir / "coverage.json", directory=False)
        return {
            "slice_error": "",
            "slices_dir": str(slices_dir),
        }

    def _slice_failure(self, error: str) -> dict[str, Any]:
        return {
            "slice_error": error,
            "slices_dir": "",
        }

    async def analyze_candidate(
        self,
        state: CandidateWorkflowState,
        config: RunnableConfig,
        runtime: Runtime[AgentContext],
    ) -> dict[str, Any]:
        candidate_id = state["candidate_id"]
        draft_csv = Path(state["candidate_draft_csv"])
        draft_csv.parent.mkdir(parents=True, exist_ok=True)
        previous_error = state.get("validation_error", "")

        if draft_csv.is_file() and not previous_error:
            validation = await self._validate_report(draft_csv, state["slices_dir"])
            if validation.returncode == 0:
                return self._candidate_success(candidate_id, draft_csv)
            if validation.returncode != 2:
                raise RuntimeError(
                    f"candidate validator failed: {_script_error(validation)}"
                )
            previous_error = _script_error(validation)

        prompt = build_root_cause_prompt(
            rtl_dir=state["rtl_dir"],
            slices_dir=state["slices_dir"],
            filelist_path=state["filelist_path"],
            draft_csv=str(draft_csv),
            previous_error=previous_error,
        )
        await self.candidate_agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=config,
            context=_agent_context(runtime),
        )

        if not draft_csv.is_file():
            return self._analysis_failure(
                f"candidate {candidate_id} did not create its required draft: "
                f"{draft_csv}"
            )

        validation = await self._validate_report(draft_csv, state["slices_dir"])
        if validation.returncode == 2:
            return self._analysis_failure(_script_error(validation))
        if validation.returncode:
            raise RuntimeError(
                f"candidate validator failed: {_script_error(validation)}"
            )

        return self._candidate_success(candidate_id, draft_csv)

    async def adjudicate_root_causes(
        self,
        state: WorkflowState,
        config: RunnableConfig,
        runtime: Runtime[AgentContext],
    ) -> dict[str, Any]:
        report_path = Path(state["report_path"])
        draft_csv = Path(state["draft_csv"])

        if report_path.is_file():
            validation = await self._validate_report(report_path, state["slices_dir"])
            if validation.returncode == 0:
                report_path.chmod(0o644)
                _cleanup_intermediate_run(state["run_dir"])
                return {
                    "validation_error": "",
                    "report_path": str(report_path),
                }
            raise RuntimeError(
                f"existing published report is invalid: {_script_error(validation)}"
            )

        prompt = build_adjudication_prompt(
            rtl_dir=state["rtl_dir"],
            slices_dir=state["slices_dir"],
            filelist_path=state["filelist_path"],
            candidate_reports=self._candidate_report_paths(state),
            draft_csv=str(draft_csv),
            previous_error=state.get("validation_error", ""),
        )
        await self.judge_agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=config,
            context=_agent_context(runtime),
        )

        if not draft_csv.is_file():
            return self._analysis_failure(
                f"agent did not create the required draft: {draft_csv}"
            )

        validation = await self._validate_report(draft_csv, state["slices_dir"])
        if validation.returncode == 2:
            return self._analysis_failure(_script_error(validation))
        if validation.returncode:
            raise RuntimeError(f"report validator failed: {_script_error(validation)}")

        sort_result = await _run_script(SORT_SCRIPT, str(draft_csv))
        if sort_result.returncode:
            raise RuntimeError(f"report sorter failed: {_script_error(sort_result)}")

        sorted_validation = await self._validate_report(draft_csv, state["slices_dir"])
        if sorted_validation.returncode:
            raise RuntimeError(
                f"sorted report failed validation: {_script_error(sorted_validation)}"
            )

        report_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(draft_csv, report_path)
        report_path.chmod(0o644)
        _cleanup_intermediate_run(state["run_dir"])
        return {
            "validation_error": "",
            "report_path": str(report_path),
        }

    def _candidate_report_paths(self, state: WorkflowState) -> list[str]:
        reports_by_id: dict[int, str] = {}
        for report in state.get("candidate_reports", []):
            candidate_id = report["candidate_id"]
            report_path = str(
                _required_path(Path(report["report_path"]).resolve(), directory=False)
            )
            existing = reports_by_id.setdefault(candidate_id, report_path)
            if existing != report_path:
                raise RuntimeError(
                    f"candidate {candidate_id} returned conflicting report paths"
                )

        expected_ids = set(range(self.ensemble_size))
        if set(reports_by_id) != expected_ids:
            raise RuntimeError(
                "candidate report set is incomplete: "
                f"expected {sorted(expected_ids)}, got {sorted(reports_by_id)}"
            )
        return [reports_by_id[candidate_id] for candidate_id in sorted(expected_ids)]

    @staticmethod
    def _candidate_success(candidate_id: int, report_path: Path) -> dict[str, Any]:
        return {
            "validation_error": "",
            "candidate_reports": [
                {"candidate_id": candidate_id, "report_path": str(report_path)}
            ],
        }

    async def _validate_report(
        self, report_path: Path, slices_dir: str
    ) -> ScriptResult:
        return await _run_script(
            VALIDATE_SCRIPT,
            str(report_path),
            "--slices-dir",
            slices_dir,
        )

    def _analysis_failure(self, error: str) -> dict[str, Any]:
        return {
            "validation_error": error,
        }
