"""Node implementations for the lint root-cause workflow."""

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
    build_analysis_batch_prompt,
    build_classifier_prompt,
    build_global_merge_prompt,
)
from .state import AnalysisBatchState, MergeCandidateState, WorkflowState


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "verilog-lint-root-cause-csv"
SCRIPT_DIR = SKILL_DIR / "scripts"
PREPARE_SCRIPT = SCRIPT_DIR / "prepare_hierarchy_inputs.py"
BUILD_SLICES_SCRIPT = SCRIPT_DIR / "build_slices.py"
BUILD_CATALOG_SCRIPT = SCRIPT_DIR / "build_local_root_catalog.py"
VALIDATE_MAP_SCRIPT = SCRIPT_DIR / "validate_global_root_map.py"
EXPORT_SCRIPT = SCRIPT_DIR / "export_root_cause_csv.py"
SORT_SCRIPT = SCRIPT_DIR / "sort_root_cause_csv.py"
VALIDATE_REPORT_SCRIPT = SCRIPT_DIR / "validate_root_cause_csv.py"

sys.path.insert(0, str(SCRIPT_DIR))
try:
    from _work_units import read_manifest
finally:
    sys.path.pop(0)


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


def _hierarchy_available(path: Path) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid hierarchy status: {path}")
    mode = data.get("mode")
    expected_keys = (
        {"schema_version", "mode"}
        if mode == "hierarchy"
        else {"schema_version", "mode", "reason"}
    )
    if (
        data.get("schema_version") != 1
        or mode not in {"hierarchy", "module"}
        or set(data) != expected_keys
        or (mode == "module" and not str(data.get("reason", "")).strip())
    ):
        raise ValueError(f"invalid hierarchy status: {path}")
    return mode == "hierarchy"


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
        analysis_batch_agent: Any,
        merge_agent: Any,
        judge_agent: Any,
        ensemble_size: int,
    ) -> None:
        self.classifier_agent = classifier_agent
        self.analysis_batch_agent = analysis_batch_agent
        self.merge_agent = merge_agent
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
                "input preparation returned an incomplete output contract"
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
        hierarchy_status_path = _required_path(
            work_dir / "hierarchy_status.json", directory=False
        )
        hierarchy_available = _hierarchy_available(hierarchy_status_path)
        hierarchy_tree_path = work_dir / "hierarchy_tree.txt"
        if hierarchy_available:
            _required_path(hierarchy_tree_path, directory=False)
        elif hierarchy_tree_path.exists():
            raise RuntimeError(
                "module-only preparation unexpectedly produced a hierarchy tree"
            )
        _required_path(work_dir / "lint_entries_mapped.csv", directory=False)
        design_metadata_path = _required_path(
            work_dir / "design_metadata.json", directory=False
        )
        return {
            "run_dir": str(run_dir),
            "rtl_dir": str(rtl_dir),
            "filelist_path": str(filelist_path),
            "hierarchy_status_path": str(hierarchy_status_path),
            "design_metadata_path": str(design_metadata_path),
            "policy_path": str(work_dir / "slice_policy.json"),
            "slices_dir": "",
            "local_catalog_path": "",
            "adjudicated_map_path": "",
            "draft_csv": str(work_dir / "root_cause_draft.csv"),
            "report_path": str(report_path),
            "slice_error": "",
            "validation_error": "",
            "work_unit_results": [],
            "merge_candidate_results": [],
        }

    async def classify_and_slice(
        self,
        state: WorkflowState,
        config: RunnableConfig,
        runtime: Runtime[AgentContext],
    ) -> dict[str, Any]:
        hierarchy_status_path = Path(state["hierarchy_status_path"])
        hierarchy_available = _hierarchy_available(hierarchy_status_path)
        prompt = build_classifier_prompt(
            rtl_dir=state["rtl_dir"],
            filelist_path=state["filelist_path"],
            hierarchy_available=hierarchy_available,
            hierarchy_tree_path=str(
                hierarchy_status_path.parent / "hierarchy_tree.txt"
            ),
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

        build_arguments = [
            "--project-dir",
            state["run_dir"],
            "--work-dir",
            str(Path(state["design_metadata_path"]).parent),
            "--policy",
            state["policy_path"],
        ]
        if not hierarchy_available:
            build_arguments.append("--module-only")
        build_result = await _run_script(BUILD_SLICES_SCRIPT, *build_arguments)
        if build_result.returncode == 2:
            return self._slice_failure(_script_error(build_result))
        if build_result.returncode:
            raise RuntimeError(f"slice builder failed: {_script_error(build_result)}")

        slices_dir = _required_path(
            Path(state["run_dir"]) / "slices", directory=True
        )
        read_manifest(slices_dir)
        return {
            "slice_error": "",
            "slices_dir": str(slices_dir),
        }

    def _slice_failure(self, error: str) -> dict[str, Any]:
        return {
            "slice_error": error,
            "slices_dir": "",
        }

    async def analyze_batch(
        self,
        state: AnalysisBatchState,
        config: RunnableConfig,
        runtime: Runtime[AgentContext],
    ) -> dict[str, Any]:
        assignments = state["work_units"]
        if not assignments:
            raise RuntimeError("analysis batch is empty")

        members: list[tuple[str, Path, Path]] = []
        seen: set[str] = set()
        slices_dir: Path | None = None
        for assignment in assignments:
            unit_id = assignment["unit_id"]
            unit_dir = Path(assignment["work_unit_dir"]).resolve()
            current_slices_dir = unit_dir.parents[2]
            expected = (current_slices_dir / unit_id).resolve()
            if unit_id in seen or unit_dir != expected:
                raise RuntimeError(
                    f"analysis batch has an invalid work unit: {unit_id}"
                )
            if slices_dir is None:
                slices_dir = current_slices_dir
            elif current_slices_dir != slices_dir:
                raise RuntimeError("analysis batch spans multiple slices directories")
            seen.add(unit_id)
            members.append((unit_id, unit_dir, unit_dir / "local_root_cause.csv"))

        pending_members: list[tuple[str, Path, Path]] = []
        validation_errors: list[str] = []
        for unit_id, unit_dir, report_path in members:
            if not report_path.is_file():
                pending_members.append((unit_id, unit_dir, report_path))
                continue
            validation = await self._normalize_and_validate_local_report(
                report_path, unit_dir
            )
            if validation.returncode == 2:
                pending_members.append((unit_id, unit_dir, report_path))
                validation_errors.append(f"{unit_id}: {_script_error(validation)}")
            elif validation.returncode:
                raise RuntimeError(
                    f"local report validator failed: {_script_error(validation)}"
                )
        if not pending_members:
            return self._analysis_batch_success(members)

        prompt = build_analysis_batch_prompt(
            members=[
                (unit_id, str(unit_dir), str(report_path))
                for unit_id, unit_dir, report_path in pending_members
            ],
            previous_error="\n".join(validation_errors),
        )
        await self.analysis_batch_agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=config,
            context=_agent_context(runtime),
        )

        validation_errors = []
        for unit_id, unit_dir, report_path in members:
            if not report_path.is_file():
                validation_errors.append(f"{unit_id}: report is missing")
                continue
            validation = await self._normalize_and_validate_local_report(
                report_path, unit_dir
            )
            if validation.returncode == 2:
                validation_errors.append(f"{unit_id}: {_script_error(validation)}")
            elif validation.returncode:
                raise RuntimeError(
                    f"local report validator failed: {_script_error(validation)}"
                )
        if validation_errors:
            return {"validation_error": "\n".join(validation_errors)}
        return self._analysis_batch_success(members)

    async def build_local_root_catalog(
        self, state: WorkflowState
    ) -> dict[str, Any]:
        expected = {
            unit_id: unit_dir / "local_root_cause.csv"
            for unit_id, unit_dir in read_manifest(
                Path(state["slices_dir"])
            ).work_units
        }
        actual: dict[str, Path] = {}
        for result in state.get("work_unit_results", []):
            unit_id = result["unit_id"]
            report_path = Path(result["report_path"]).resolve()
            previous = actual.setdefault(unit_id, report_path)
            if previous != report_path:
                raise RuntimeError(f"work unit {unit_id} returned conflicting paths")
        if set(actual) != set(expected):
            raise RuntimeError(
                "work-unit result set is incomplete: "
                f"expected {len(expected)}, got {len(actual)}"
            )
        for unit_id, expected_path in expected.items():
            if actual[unit_id] != expected_path.resolve():
                raise RuntimeError(f"work unit {unit_id} returned an invalid path")

        catalog_path = Path(state["run_dir"]) / "work" / "local_root_catalog.csv"
        result = await _run_script(
            BUILD_CATALOG_SCRIPT,
            "--slices-dir",
            state["slices_dir"],
            "--output",
            str(catalog_path),
        )
        if result.returncode:
            raise RuntimeError(f"local catalog failed: {_script_error(result)}")
        _required_path(catalog_path, directory=False)
        return {"local_catalog_path": str(catalog_path)}

    async def analyze_merge_candidate(
        self,
        state: MergeCandidateState,
        config: RunnableConfig,
        runtime: Runtime[AgentContext],
    ) -> dict[str, Any]:
        candidate_id = state["candidate_id"]
        map_path = Path(state["map_path"]).resolve()
        run_dir = Path(state["slices_dir"]).resolve().parent
        expected = (
            run_dir
            / "work"
            / "ensemble"
            / f"global_map_{candidate_id:03d}.csv"
        ).resolve()
        if map_path != expected:
            raise RuntimeError(f"unexpected global map path: {map_path}")
        map_path.parent.mkdir(parents=True, exist_ok=True)
        previous_error = state.get("validation_error", "")

        if map_path.is_file() and not previous_error:
            validation = await self._validate_global_map(
                map_path, state["local_catalog_path"]
            )
            if validation.returncode == 0:
                return self._merge_candidate_success(candidate_id, map_path)
            if validation.returncode != 2:
                raise RuntimeError(
                    f"global map validator failed: {_script_error(validation)}"
                )
            previous_error = _script_error(validation)

        prompt = build_global_merge_prompt(
            slices_dir=state["slices_dir"],
            local_catalog_path=state["local_catalog_path"],
            map_path=str(map_path),
            previous_error=previous_error,
        )
        await self.merge_agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=config,
            context=_agent_context(runtime),
        )
        if not map_path.is_file():
            return {"validation_error": f"agent did not create {map_path}"}

        validation = await self._validate_global_map(
            map_path, state["local_catalog_path"]
        )
        if validation.returncode == 2:
            return {"validation_error": _script_error(validation)}
        if validation.returncode:
            raise RuntimeError(
                f"global map validator failed: {_script_error(validation)}"
            )
        return self._merge_candidate_success(candidate_id, map_path)

    async def adjudicate_root_causes(
        self,
        state: WorkflowState,
        config: RunnableConfig,
        runtime: Runtime[AgentContext],
    ) -> dict[str, Any]:
        map_path = (
            Path(state["run_dir"]) / "work" / "adjudicated_global_map.csv"
        ).resolve()
        previous_error = state.get("validation_error", "")
        if map_path.is_file() and not previous_error:
            validation = await self._validate_global_map(
                map_path, state["local_catalog_path"]
            )
            if validation.returncode == 0:
                return {
                    "validation_error": "",
                    "adjudicated_map_path": str(map_path),
                }
            if validation.returncode != 2:
                raise RuntimeError(
                    f"global map validator failed: {_script_error(validation)}"
                )
            previous_error = _script_error(validation)

        prompt = build_adjudication_prompt(
            slices_dir=state["slices_dir"],
            local_catalog_path=state["local_catalog_path"],
            candidate_maps=self._merge_candidate_paths(state),
            adjudicated_map_path=str(map_path),
            previous_error=previous_error,
        )
        await self.judge_agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=config,
            context=_agent_context(runtime),
        )
        if not map_path.is_file():
            return {
                "validation_error": f"agent did not create {map_path}",
                "adjudicated_map_path": "",
            }
        validation = await self._validate_global_map(
            map_path, state["local_catalog_path"]
        )
        if validation.returncode == 2:
            return {
                "validation_error": _script_error(validation),
                "adjudicated_map_path": "",
            }
        if validation.returncode:
            raise RuntimeError(
                f"global map validator failed: {_script_error(validation)}"
            )
        return {
            "validation_error": "",
            "adjudicated_map_path": str(map_path),
        }

    async def export_final_report(
        self, state: WorkflowState
    ) -> dict[str, Any]:
        report_path = Path(state["report_path"])
        if report_path.is_file():
            validation = await self._validate_global_report(
                report_path, state["slices_dir"]
            )
            if validation.returncode:
                raise RuntimeError(
                    f"existing published report is invalid: {_script_error(validation)}"
                )
            report_path.chmod(0o644)
            _cleanup_intermediate_run(state["run_dir"])
            return {"report_path": str(report_path)}

        map_validation = await self._validate_global_map(
            Path(state["adjudicated_map_path"]), state["local_catalog_path"]
        )
        if map_validation.returncode:
            raise RuntimeError(
                f"adjudicated map is invalid: {_script_error(map_validation)}"
            )

        draft_csv = Path(state["draft_csv"])
        export = await _run_script(
            EXPORT_SCRIPT,
            "--slices-dir",
            state["slices_dir"],
            "--global-map",
            state["adjudicated_map_path"],
            "--output",
            str(draft_csv),
        )
        if export.returncode:
            raise RuntimeError(f"final CSV export failed: {_script_error(export)}")
        validation = await self._normalize_and_validate_global_report(
            draft_csv, state["slices_dir"]
        )
        if validation.returncode:
            raise RuntimeError(
                f"final report validation failed: {_script_error(validation)}"
            )

        report_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(draft_csv, report_path)
        report_path.chmod(0o644)
        _cleanup_intermediate_run(state["run_dir"])
        return {"report_path": str(report_path)}

    def _merge_candidate_paths(self, state: WorkflowState) -> list[str]:
        paths: dict[int, str] = {}
        for result in state.get("merge_candidate_results", []):
            candidate_id = result["candidate_id"]
            map_path = str(
                _required_path(Path(result["map_path"]).resolve(), directory=False)
            )
            previous = paths.setdefault(candidate_id, map_path)
            if previous != map_path:
                raise RuntimeError(
                    f"global map {candidate_id} returned conflicting paths"
                )
        expected_ids = set(range(self.ensemble_size))
        if set(paths) != expected_ids:
            raise RuntimeError(
                "global map set is incomplete: "
                f"expected {sorted(expected_ids)}, got {sorted(paths)}"
            )
        return [paths[candidate_id] for candidate_id in sorted(expected_ids)]

    @staticmethod
    def _analysis_batch_success(
        members: list[tuple[str, Path, Path]],
    ) -> dict[str, Any]:
        return {
            "validation_error": "",
            "work_unit_results": [
                {"unit_id": unit_id, "report_path": str(report_path)}
                for unit_id, _, report_path in members
            ],
        }

    @staticmethod
    def _merge_candidate_success(
        candidate_id: int, map_path: Path
    ) -> dict[str, Any]:
        return {
            "validation_error": "",
            "merge_candidate_results": [
                {"candidate_id": candidate_id, "map_path": str(map_path)}
            ],
        }

    async def _validate_global_map(
        self, map_path: Path, catalog_path: str
    ) -> ScriptResult:
        return await _run_script(
            VALIDATE_MAP_SCRIPT,
            str(map_path),
            "--catalog",
            catalog_path,
        )

    async def _validate_global_report(
        self, report_path: Path, slices_dir: str
    ) -> ScriptResult:
        return await _run_script(
            VALIDATE_REPORT_SCRIPT,
            str(report_path),
            "--slices-dir",
            slices_dir,
        )

    async def _normalize_and_validate_local_report(
        self, report_path: Path, unit_dir: Path
    ) -> ScriptResult:
        sort_result = await _run_script(SORT_SCRIPT, str(report_path))
        if sort_result.returncode:
            return ScriptResult(
                returncode=2,
                stdout="",
                stderr=f"CSV normalization failed: {_script_error(sort_result)}",
            )
        return await _run_script(
            VALIDATE_REPORT_SCRIPT,
            str(report_path),
            "--work-unit-dir",
            str(unit_dir),
        )

    async def _normalize_and_validate_global_report(
        self, report_path: Path, slices_dir: str
    ) -> ScriptResult:
        sort_result = await _run_script(SORT_SCRIPT, str(report_path))
        if sort_result.returncode:
            return ScriptResult(
                returncode=2,
                stdout="",
                stderr=f"CSV normalization failed: {_script_error(sort_result)}",
            )
        return await self._validate_global_report(report_path, slices_dir)
