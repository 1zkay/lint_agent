#!/usr/bin/env python3
"""Validate the JSON produced by the verilog-lint-triage skill."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ALLOWED_OVERALL_RESULTS = {"严重缺陷", "一般缺陷", "全部误报"}
ALLOWED_LINT_CATEGORIES = {"严重", "一般", "误报"}
ALLOWED_MISSED_CATEGORIES = {"严重", "一般"}
ALLOWED_STANDARD_CATEGORIES = {"严重", "一般", "提示"}
DEFAULT_OUTPUT_PATTERN = re.compile(
    r"^(?:/workspace[/\\])?reports[/\\]verilog_lint_triage_result_\d{8}_\d{6}\.json$"
)
MISSED_DEFECT_ID_PATTERN = re.compile(r"^MISSED_\d{3}$")
STANDARD_FINDING_ID_PATTERN = re.compile(r"^STD_\d{3}$")


def _expect(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _require_keys(obj: dict, keys: list[str], prefix: str, errors: list[str]) -> None:
    for key in keys:
        _expect(key in obj, f"{prefix}: missing key '{key}'", errors)


def _validate_non_empty_string(
    value: object,
    field_path: str,
    errors: list[str],
) -> None:
    _expect(
        isinstance(value, str) and value.strip() != "",
        f"{field_path}: must be a non-empty string",
        errors,
    )


def _validate_positive_int(value: object, field_path: str, errors: list[str]) -> None:
    _expect(
        isinstance(value, int) and value >= 1,
        f"{field_path}: must be an integer >= 1",
        errors,
    )


def _validate_id(
    value: object,
    pattern: re.Pattern[str],
    field_path: str,
    description: str,
    errors: list[str],
) -> None:
    _validate_non_empty_string(value, field_path, errors)
    if isinstance(value, str):
        _expect(
            bool(pattern.fullmatch(value)),
            f"{field_path}: must use the {description} format",
            errors,
        )


def _validate_unique_strings(
    values: list[object],
    field_path: str,
    errors: list[str],
) -> None:
    strings = [value for value in values if isinstance(value, str)]
    if len(strings) != len(set(strings)):
        errors.append(f"{field_path}: must not contain duplicate values")


def _validate_kb_rules(value: object, prefix: str, errors: list[str]) -> None:
    _expect(isinstance(value, list), f"{prefix}: 'kb_rules' must be a list", errors)
    if not isinstance(value, list):
        return
    for idx, rule in enumerate(value):
        item_prefix = f"{prefix}.kb_rules[{idx}]"
        _expect(isinstance(rule, dict), f"{item_prefix}: must be an object", errors)
        if not isinstance(rule, dict):
            continue
        _require_keys(rule, ["id", "severity_default", "description"], item_prefix, errors)
        _validate_non_empty_string(rule.get("id"), f"{item_prefix}.id", errors)
        _validate_non_empty_string(
            rule.get("severity_default"),
            f"{item_prefix}.severity_default",
            errors,
        )
        _validate_non_empty_string(
            rule.get("description"),
            f"{item_prefix}.description",
            errors,
        )


def _validate_lint_item(item: object, idx: int, errors: list[str]) -> None:
    prefix = f"lint_items[{idx}]"
    _expect(isinstance(item, dict), f"{prefix}: must be an object", errors)
    if not isinstance(item, dict):
        return

    _require_keys(
        item,
        [
            "report_line_numbers",
            "raw_report_lines",
            "report_rule_id",
            "report_severity",
            "file",
            "code_line",
            "category",
            "issue",
            "kb_rules",
            "why",
            "evidence",
            "fix_hint",
        ],
        prefix,
        errors,
    )

    _validate_non_empty_string(item.get("file"), f"{prefix}.file", errors)
    _validate_positive_int(item.get("code_line"), f"{prefix}.code_line", errors)
    _validate_non_empty_string(item.get("issue"), f"{prefix}.issue", errors)
    _validate_non_empty_string(item.get("why"), f"{prefix}.why", errors)
    _validate_non_empty_string(item.get("evidence"), f"{prefix}.evidence", errors)
    _validate_non_empty_string(item.get("fix_hint"), f"{prefix}.fix_hint", errors)

    category = item.get("category")
    _expect(category in ALLOWED_LINT_CATEGORIES, f"{prefix}: invalid category '{category}'", errors)

    report_line_numbers = item.get("report_line_numbers")
    raw_report_lines = item.get("raw_report_lines")
    report_rule_id = item.get("report_rule_id")
    report_severity = item.get("report_severity")

    _expect(
        isinstance(report_line_numbers, list) and len(report_line_numbers) > 0,
        f"{prefix}: 'report_line_numbers' must be a non-empty list",
        errors,
    )
    _expect(
        isinstance(raw_report_lines, list) and len(raw_report_lines) > 0,
        f"{prefix}: 'raw_report_lines' must be a non-empty list",
        errors,
    )
    _expect(
        isinstance(report_rule_id, list) and len(report_rule_id) > 0,
        f"{prefix}: 'report_rule_id' must be a non-empty list",
        errors,
    )
    _expect(
        isinstance(report_severity, list) and len(report_severity) > 0,
        f"{prefix}: 'report_severity' must be a non-empty list",
        errors,
    )

    if (
        isinstance(report_line_numbers, list)
        and isinstance(raw_report_lines, list)
        and isinstance(report_rule_id, list)
        and isinstance(report_severity, list)
    ):
        expected_len = len(report_line_numbers)
        _expect(
            len(raw_report_lines) == expected_len
            and len(report_rule_id) == expected_len
            and len(report_severity) == expected_len,
            f"{prefix}: merged report arrays must have the same length",
            errors,
        )

        for line_idx, line_number in enumerate(report_line_numbers):
            _validate_positive_int(
                line_number,
                f"{prefix}.report_line_numbers[{line_idx}]",
                errors,
            )
        for line_idx, raw_line in enumerate(raw_report_lines):
            _validate_non_empty_string(
                raw_line,
                f"{prefix}.raw_report_lines[{line_idx}]",
                errors,
            )
        for line_idx, rule_id in enumerate(report_rule_id):
            _validate_non_empty_string(
                rule_id,
                f"{prefix}.report_rule_id[{line_idx}]",
                errors,
            )
        for line_idx, severity in enumerate(report_severity):
            _validate_non_empty_string(
                severity,
                f"{prefix}.report_severity[{line_idx}]",
                errors,
            )

    _validate_kb_rules(item.get("kb_rules"), prefix, errors)


def _validate_missed_defect(item: object, idx: int, errors: list[str]) -> None:
    prefix = f"missed_defects[{idx}]"
    _expect(isinstance(item, dict), f"{prefix}: must be an object", errors)
    if not isinstance(item, dict):
        return

    _require_keys(
        item,
        [
            "id",
            "file",
            "code_line",
            "category",
            "issue",
            "kb_rules",
            "why_missed_by_lint",
            "evidence",
            "fix_hint",
        ],
        prefix,
        errors,
    )

    _validate_id(
        item.get("id"),
        MISSED_DEFECT_ID_PATTERN,
        f"{prefix}.id",
        "MISSED_001",
        errors,
    )
    _validate_non_empty_string(item.get("file"), f"{prefix}.file", errors)
    _validate_positive_int(item.get("code_line"), f"{prefix}.code_line", errors)
    _validate_non_empty_string(item.get("issue"), f"{prefix}.issue", errors)
    _validate_non_empty_string(
        item.get("why_missed_by_lint"),
        f"{prefix}.why_missed_by_lint",
        errors,
    )
    _validate_non_empty_string(item.get("evidence"), f"{prefix}.evidence", errors)
    _validate_non_empty_string(item.get("fix_hint"), f"{prefix}.fix_hint", errors)

    category = item.get("category")
    _expect(
        category in ALLOWED_MISSED_CATEGORIES,
        f"{prefix}: invalid category '{category}'",
        errors,
    )

    _validate_kb_rules(item.get("kb_rules"), prefix, errors)


def _validate_standard_finding(
    item: object,
    diag_idx: int,
    finding_idx: int,
    errors: list[str],
) -> None:
    prefix = f"standard_file_diagnosis[{diag_idx}].findings[{finding_idx}]"
    _expect(isinstance(item, dict), f"{prefix}: must be an object", errors)
    if not isinstance(item, dict):
        return

    _require_keys(
        item,
        [
            "id",
            "code_line",
            "category",
            "issue",
            "standard_pages",
            "why",
            "evidence",
            "fix_hint",
        ],
        prefix,
        errors,
    )

    _validate_id(
        item.get("id"),
        STANDARD_FINDING_ID_PATTERN,
        f"{prefix}.id",
        "STD_001",
        errors,
    )
    _validate_positive_int(item.get("code_line"), f"{prefix}.code_line", errors)
    _validate_non_empty_string(item.get("issue"), f"{prefix}.issue", errors)
    _validate_non_empty_string(
        item.get("standard_pages"),
        f"{prefix}.standard_pages",
        errors,
    )
    _validate_non_empty_string(item.get("why"), f"{prefix}.why", errors)
    _validate_non_empty_string(item.get("evidence"), f"{prefix}.evidence", errors)
    _validate_non_empty_string(item.get("fix_hint"), f"{prefix}.fix_hint", errors)

    _expect(
        item.get("category") in ALLOWED_STANDARD_CATEGORIES,
        f"{prefix}: invalid category '{item.get('category')}'",
        errors,
    )


def _validate_standard_diagnosis(item: object, idx: int, errors: list[str]) -> None:
    prefix = f"standard_file_diagnosis[{idx}]"
    _expect(isinstance(item, dict), f"{prefix}: must be an object", errors)
    if not isinstance(item, dict):
        return

    _require_keys(item, ["file", "summary_text", "findings"], prefix, errors)
    _validate_non_empty_string(item.get("file"), f"{prefix}.file", errors)
    _validate_non_empty_string(item.get("summary_text"), f"{prefix}.summary_text", errors)

    findings = item.get("findings")
    _expect(isinstance(findings, list), f"{prefix}: 'findings' must be a list", errors)
    if isinstance(findings, list):
        finding_ids = []
        for finding_idx, finding in enumerate(findings):
            _validate_standard_finding(finding, idx, finding_idx, errors)
            if isinstance(finding, dict):
                finding_ids.append(finding.get("id"))
        _validate_unique_strings(finding_ids, f"{prefix}.findings[].id", errors)


def _derive_overall_result(data: dict) -> str:
    severities: list[str] = []

    for item in data.get("lint_items", []):
        category = item.get("category")
        if category in {"严重", "一般"}:
            severities.append(category)

    for item in data.get("missed_defects", []):
        category = item.get("category")
        if category in {"严重", "一般"}:
            severities.append(category)

    for diagnosis in data.get("standard_file_diagnosis", []):
        for finding in diagnosis.get("findings", []):
            category = finding.get("category")
            if category in {"严重", "一般"}:
                severities.append(category)

    if "严重" in severities:
        return "严重缺陷"
    if "一般" in severities:
        return "一般缺陷"
    return "全部误报"


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: validate_triage_json.py <json_path>", file=sys.stderr)
        return 2

    json_path = Path(sys.argv[1])
    if not json_path.is_file():
        print(f"File not found: {json_path}", file=sys.stderr)
        return 2

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON: {exc}", file=sys.stderr)
        return 1

    errors: list[str] = []
    _expect(isinstance(data, dict), "top-level JSON must be an object", errors)
    if not isinstance(data, dict):
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    _require_keys(
        data,
        ["overall_result", "summary", "lint_items", "missed_defects", "standard_file_diagnosis"],
        "root",
        errors,
    )

    overall_result = data.get("overall_result")
    _expect(
        overall_result in ALLOWED_OVERALL_RESULTS,
        f"root: invalid overall_result '{overall_result}'",
        errors,
    )

    summary = data.get("summary")
    source_files: list[object] = []
    if not isinstance(summary, dict):
        _expect(False, "root.summary must be an object", errors)
    else:
        _require_keys(
            summary,
            [
                "knowledge_base",
                "report_path",
                "source_files",
                "output_path",
                "summary_text",
            ],
            "summary",
            errors,
        )
        _validate_non_empty_string(summary.get("knowledge_base"), "summary.knowledge_base", errors)
        _validate_non_empty_string(summary.get("report_path"), "summary.report_path", errors)
        _validate_non_empty_string(summary.get("summary_text"), "summary.summary_text", errors)

        source_files_value = summary.get("source_files")
        _expect(
            isinstance(source_files_value, list) and len(source_files_value) > 0,
            "summary.source_files must be a non-empty list",
            errors,
        )
        if isinstance(source_files_value, list):
            source_files = source_files_value
            for idx, source_file in enumerate(source_files_value):
                _validate_non_empty_string(
                    source_file,
                    f"summary.source_files[{idx}]",
                    errors,
                )
            _validate_unique_strings(source_files_value, "summary.source_files", errors)

        output_path = summary.get("output_path")
        _validate_non_empty_string(output_path, "summary.output_path", errors)
        if isinstance(output_path, str) and (
            output_path.startswith("reports/")
            or output_path.startswith("reports\\")
            or output_path.startswith("/workspace/reports/")
            or output_path.startswith("/workspace/reports\\")
        ):
            _expect(
                bool(DEFAULT_OUTPUT_PATTERN.match(output_path)),
                "summary.output_path must use the default timestamped form reports/verilog_lint_triage_result_YYYYMMDD_HHMMSS.json or /workspace/reports/verilog_lint_triage_result_YYYYMMDD_HHMMSS.json",
                errors,
            )

    lint_items = data.get("lint_items")
    _expect(isinstance(lint_items, list), "root.lint_items must be a list", errors)
    if isinstance(lint_items, list):
        for idx, item in enumerate(lint_items):
            _validate_lint_item(item, idx, errors)

    missed_defects = data.get("missed_defects")
    _expect(isinstance(missed_defects, list), "root.missed_defects must be a list", errors)
    if isinstance(missed_defects, list):
        missed_ids = []
        for idx, item in enumerate(missed_defects):
            _validate_missed_defect(item, idx, errors)
            if isinstance(item, dict):
                missed_ids.append(item.get("id"))
        _validate_unique_strings(missed_ids, "missed_defects[].id", errors)

    standard_diagnosis = data.get("standard_file_diagnosis")
    _expect(
        isinstance(standard_diagnosis, list),
        "root.standard_file_diagnosis must be a list",
        errors,
    )
    if isinstance(standard_diagnosis, list):
        diagnosis_files: list[object] = []
        for idx, item in enumerate(standard_diagnosis):
            _validate_standard_diagnosis(item, idx, errors)
            if isinstance(item, dict):
                diagnosis_files.append(item.get("file"))

        _validate_unique_strings(diagnosis_files, "root.standard_file_diagnosis[].file", errors)
        if source_files:
            source_file_strings = [item for item in source_files if isinstance(item, str)]
            diagnosis_file_strings = [item for item in diagnosis_files if isinstance(item, str)]
            _expect(
                sorted(diagnosis_file_strings) == sorted(source_file_strings),
                "root.standard_file_diagnosis must contain exactly one entry for each source file",
                errors,
            )

    derived_overall = _derive_overall_result(data)
    _expect(
        overall_result == derived_overall,
        f"root: overall_result should be '{derived_overall}' based on lint_items, missed_defects, and standard_file_diagnosis",
        errors,
    )

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(f"Validation passed: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
