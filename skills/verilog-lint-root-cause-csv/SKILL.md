---
name: verilog-lint-root-cause-csv
description: Use this skill when the user provides a Verilog/SystemVerilog lint report plus source files or a source archive and wants a root-cause CSV whose columns are ViolationID, Severity, cause_file_path, cause_line_start_end, and effect_violation_id, with lint MessageID values converted to stable per-row ViolationID values such as BlkingUsedInSeqAlways_1.
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
ViolationID,Severity,cause_file_path,cause_line_start_end,effect_violation_id
```

Rules:

- `ViolationID`: representative lint violation ID for the root-cause row. Build IDs by appending a 1-based counter per `MessageID` in lint-report order, for example `BlkingUsedInSeqAlways_1`.
- `Severity`: use the representative/root severity, or the highest severity among the affected violations if the root row represents several warnings.
- `cause_file_path`: use only the source filename, such as `johnson8.v`, not the original absolute report path. Use `误报` for false positives.
- `cause_line_start_end`: use `N-N` for a source range, `N` for a single line, or `-` when not applicable.
- `effect_violation_id`: list affected violation IDs separated by `、`. Use `-` when there is no downstream effect or the row is a false positive.
- Write the CSV as UTF-8 with BOM (`utf-8-sig`) for spreadsheet compatibility.

The output is root-cause oriented. It does not need one row per lint row. A single root-cause row may list multiple affected lint `ViolationID` values.

## Workflow

### 1. Prepare deterministic inputs

Lint reports may be malformed CSV. Common defects include source locations
accidentally appended to the `Contents` field with a tab, rows with fewer columns
than the header, embedded commas in quoted text, and blank trailing lines. Do not
parse such reports manually with a plain `csv.DictReader`. Always run the helper
below first; it parses physical report lines, recovers source locations from
`LineNo`, `Contents`, or the raw line, and reports malformed-row statistics.

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
- Keep false positives as root-cause rows only when the user expects false-positive reporting; set `cause_file_path=误报`, `cause_line_start_end=-`, and `effect_violation_id=-`.
- Prefer concise, code-evidenced ranges. For example, if a sequence of blocking assignments in one sequential `always` block causes several `BlkingUsedInSeqAlways_*` items, the cause range should cover that `always` block or the offending assignment block.

### 3. Write and validate

Unless the user gives an explicit output path, write:

```text
reports/verilog_lint_root_cause_<YYYYMMDD_HHMMSS>.csv
```

The timestamp must come from an executed command in the current environment.

Validate before finishing:

```bash
python skills/verilog-lint-root-cause-csv/scripts/validate_root_cause_csv.py \
  <output_csv> \
  --lint-items <LINT_ITEMS_JSON>
```

Fix validation errors and rerun until it passes.

## LangGraph vs Skill Guidance

Use this skill for this feature. A LangGraph graph is only justified if the product needs a fixed multi-node service pipeline with persisted intermediate states, retries, UI checkpoints, or separate deterministic analyzers. For interactive uploaded reports where the agent must inspect code and decide root-cause/effect grouping, a skill plus deterministic prep/validation scripts is simpler and avoids duplicating the existing chat-agent runtime.
