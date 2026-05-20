---
name: verilog-lint-root-cause-csv
description: Use this skill when the user provides a Verilog/SystemVerilog lint report in either normalized violation_id/severity/message_id/description/file_path/line_number format or legacy Stage/MessageID/Severity/Contents/LineNo format, plus source files or a source archive, and wants a root-cause CSV whose columns are cause_file_path, cause_file_start, cause_file_end, and effect_violation_id.
license: MIT
metadata:
  author: zk
  version: "1.4"
---

# Verilog Lint Root-Cause CSV

## When to Use

- The user provides one lint report and one or more Verilog/SystemVerilog files, directories, or source archives.
- The lint report must use exactly one of these two schemas.

Normalized schema:

```text
violation_id,severity,message_id,description,file_path,line_number
```

Legacy schema:

```text
Stage,MessageID,Severity,Contents,LineNo,
```

- The requested output is a CSV mapping lint violation IDs to the concrete source-code range that caused the violation.

## Output Schema

Write exactly these columns, in this order:

```text
cause_file_path,cause_file_start,cause_file_end,effect_violation_id
```

Rules:

- `cause_file_path`: source filename containing the concrete root cause, such as `temp.v`. Use only the filename, not an absolute path.
- `cause_file_start`: 1-based inclusive start line of the root-cause range.
- `cause_file_end`: 1-based inclusive end line of the root-cause range. For a single-line cause, make it equal to `cause_file_start`.
- `effect_violation_id`: the original `violation_id` value from the input lint report.
- Write one output row per reported effect violation. If several violations share the same root-cause range, repeat the same `cause_file_path`, `cause_file_start`, and `cause_file_end` and use one `effect_violation_id` per row.
- Do not combine multiple effect IDs in one cell. Do not add severity, message ID, prose analysis, false-positive text, or any extra columns.
- Write only actionable, source-localized mappings. Omit false positives and lint rows whose cause cannot be confidently localized to a concrete source range.
- Write the CSV as UTF-8 with BOM (`utf-8-sig`) for spreadsheet compatibility.

For example:

```text
cause_file_path,cause_file_start,cause_file_end,effect_violation_id
temp.v,1,1,4
temp.v,1,1,5
temp.v,10,10,6
```

## Workflow

### 1. Prepare deterministic inputs

Do not parse original reports manually in the analysis workflow. Always run the
helper below first, and treat its generated `normalized_lint_report.csv`,
`lint_items.csv`, `lint_items.json`, and `SOURCE_ROOT` as the authoritative
inputs for root-cause analysis. Unsupported report headers or malformed rows
must fail in the helper instead of being interpreted heuristically.
The helper also handles the known legacy ALINT row defect where `LineNo` is
tab-appended to `Contents` while the header remains `Stage,MessageID,Severity,Contents,LineNo,`.

Run the helper from the `lint_agent` project root:

```bash
python skills/verilog-lint-root-cause-csv/scripts/prepare_root_cause_inputs.py \
  --lint-report <lint_report.csv> \
  --source-archive <sources.tar.xz>
```

For source directories instead of archives, use:

```bash
python skills/verilog-lint-root-cause-csv/scripts/prepare_root_cause_inputs.py \
  --lint-report <lint_report.csv> \
  --source-dir <source_dir>
```

Read the printed `WORK_DIR`, `NORMALIZED_LINT_REPORT_CSV`, `LINT_ITEMS_CSV`, `LINT_ITEMS_JSON`, and `SOURCE_ROOT` paths. Do not guess them.

### 2. Analyze root causes

- Read `normalized_lint_report.csv` first. It has the same schema as the normalized input example: `violation_id,severity,message_id,description,file_path,line_number`.
- Then read `lint_items.csv` if you need helper metadata such as original report line number or original source path.
- Inspect the referenced source files around candidate cause lines.
- For each actionable violation, identify the smallest source range that explains the reported effect.
- If several lint rows are different effects of the same source construct, emit one row per `violation_id` with the same cause range.
- Omit false positives, pure policy noise, and rows whose cause cannot be confidently localized to a concrete source range.
- Prefer concise, code-evidenced ranges. For example, if a case item and its assignments are the root cause, the range should cover that case statement or the offending assignment block rather than the whole file.

### 3. Write the CSV

Unless the user gives an explicit output path, write:

```text
reports/verilog_lint_root_cause_<YYYYMMDD_HHMMSS>.csv
```

The timestamp must come from an executed command in the current environment.

### 4. Second-pass review

After writing the first CSV draft, perform a full second-pass review before
validation:

- Re-read every CSV row and the corresponding lint item from `lint_items.csv`.
- Re-open the relevant source code ranges for rows that are broad, style-only, tool-policy-only, or based on a lint message that may not be a functional defect.
- Remove any row that is a false positive or cannot be tied to a concrete source range.
- Keep the CSV schema unchanged: no comments, no analysis columns, and no grouped ID cells.

Do not finish after the first CSV write. The final CSV must include the results
of this second-pass review.

### 5. Validate before finishing

```bash
python skills/verilog-lint-root-cause-csv/scripts/validate_root_cause_csv.py \
  <output_csv> \
  --lint-items <LINT_ITEMS_JSON>
```

Fix validation errors and rerun until it passes.
