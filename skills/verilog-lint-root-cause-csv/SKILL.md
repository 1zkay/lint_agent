---
name: verilog-lint-root-cause-csv
description: Use this skill when the user provides a Verilog/SystemVerilog lint report plus source files or a source archive and wants a root-cause CSV whose columns are ViolationID, Severity, cause_file_path, cause_line_start_end, effect_violation_id, and root_cause_analysis, with lint MessageID values converted to stable per-row ViolationID values such as BlkingUsedInSeqAlways_1, and with a second-pass false-positive review reflected in the CSV.
license: MIT
metadata:
  author: zk
  version: "1.0"
---

# Verilog Lint Root-Cause CSV

## When to Use

- The user provides one lint report and one or more Verilog/SystemVerilog files, directories, or source archives.
- The requested output is a CSV root-cause/effect mapping, not a prose answer.
- The lint report contains a `MessageID` column and each lint row needs a stable `ViolationID` such as `BlkingUsedInSeqAlways_1`, `BlkingUsedInSeqAlways_2`, etc.

## Output Schema

Write exactly these columns, in this order:

```text
ViolationID,Severity,cause_file_path,cause_line_start_end,effect_violation_id,root_cause_analysis
```

Rules:

- `ViolationID`: lint violation ID(s) covered by this row. Build IDs by appending a 1-based counter per `MessageID` in lint-report order, for example `BlkingUsedInSeqAlways_1`. For a real root-cause row, use exactly one representative/root `ViolationID`. For a false-positive row, list one or more false-positive `ViolationID` values separated by `、` when they share the same false-positive reason.
- `Severity`: use the representative/root severity, or the highest severity among the affected violations if the root row represents several warnings.
- `cause_file_path`: use only the source filename, such as `johnson8.v`, not the original absolute report path. Use `误报` for false positives.
- `cause_line_start_end`: use `N-M` for one continuous source range, `N` for a single line, `N、M、K` for discrete source lines, `N-M、K、P-Q` for mixed ranges and discrete lines, or `-` when not applicable.
- `effect_violation_id`: for real root-cause rows, list downstream affected violation IDs separated by `、`. Do not repeat the root `ViolationID` here. Use `-` when there is no downstream effect or the row is a false positive.
- `root_cause_analysis`: concise Chinese analysis explaining the real root cause or why the lint item is a false positive. This field must not be empty.
- Write the CSV as UTF-8 with BOM (`utf-8-sig`) for spreadsheet compatibility.

The output is root-cause oriented. It does not need one row per lint row. A single real root-cause row may list multiple affected lint `ViolationID` values in `effect_violation_id`, and a single false-positive row may list multiple same-reason false-positive IDs in `ViolationID`.

Every generated lint `ViolationID` from `lint_items.csv` must appear exactly once in the final CSV: either in `ViolationID` or in `effect_violation_id`. False-positive IDs must appear only in `ViolationID` on false-positive rows.

## Workflow

### 1. Prepare deterministic inputs

Lint reports may be malformed or tool-specific. Do not parse original reports
manually in the analysis workflow. Always run the helper below first, and treat
its generated `lint_items.csv`, `lint_items.json`, and `SOURCE_ROOT` as the
authoritative inputs for root-cause analysis. The helper owns report-format
normalization, source-location recovery, and malformed-row statistics.

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

Read the printed `WORK_DIR`, `LINT_ITEMS_CSV`, `LINT_ITEMS_JSON`, and `SOURCE_ROOT` paths. Do not guess them.

### 2. Analyze root causes

- Read `lint_items.csv` first. It contains every lint row with its generated `ViolationID`, source filename, source line, message, severity, and text.
- If the helper prints `MALFORMED_ROW_COUNT` or `RECOVERED_LOCATION_COUNT`, treat that as expected input-cleanup metadata, not as a fatal error. Mention malformed input only if it materially limits root-cause confidence.
- Inspect the referenced source files around the candidate cause lines.
- Group lint rows into root-cause rows when one code construct is the root cause for several downstream warnings.
- If one or more findings are false positives for the same reason, write a false-positive row with those IDs in `ViolationID` separated by `、`, `cause_file_path=误报`, `cause_line_start_end=-`, `effect_violation_id=-`, and explain the false-positive reason in `root_cause_analysis`.
- If a grouped row mixes false-positive IDs and real affected IDs, split it before writing the final CSV: keep or create one false-positive row for the false-positive IDs, and keep or create separate real root-cause rows for the real affected IDs. Do not hide real affected IDs by clearing `effect_violation_id` on a false-positive row.
- Prefer concise, code-evidenced ranges. For example, if a sequence of blocking assignments in one sequential `always` block causes several `BlkingUsedInSeqAlways_*` items, the cause range should cover that `always` block or the offending assignment block.

### 3. Write the first CSV draft

Unless the user gives an explicit output path, write:

```text
reports/verilog_lint_root_cause_<YYYYMMDD_HHMMSS>.csv
```

The timestamp must come from an executed command in the current environment.

### 4. Second-pass false-positive review

After writing the first CSV draft, perform a full second-pass review before
validation:

- Re-read every CSV row and the corresponding lint items from `lint_items.csv`.
- Re-open the relevant source code ranges for rows that are suspicious, broad, style-only, tool-policy-only, or based on a lint message that may not be a functional defect.
- Decide whether every ID covered by each row is a real issue or a false positive in the concrete code.
- If an entire row is false positive, modify that same CSV row to:
  - `ViolationID=<all same-reason false-positive IDs separated by 、>`
  - `cause_file_path=误报`
  - `cause_line_start_end=-`
  - `effect_violation_id=-`
  - `root_cause_analysis=<Chinese reason explaining why the lint warning is a false positive>`
- If a row contains both false-positive IDs and real IDs, split the row instead of marking the whole row as false positive. Every real ID that was previously listed in `ViolationID` or `effect_violation_id` must still appear in the final CSV either as a real row's single root `ViolationID` or in another real row's `effect_violation_id`.
- If a row remains a real issue, ensure `root_cause_analysis` explains the concrete root cause and affected violations.

Do not finish after the first CSV write. The final CSV must include the results
of this second-pass false-positive review.

### 5. Validate before finishing

```bash
python skills/verilog-lint-root-cause-csv/scripts/validate_root_cause_csv.py \
  <output_csv> \
  --lint-items <LINT_ITEMS_JSON>
```

Fix validation errors and rerun until it passes.
