---
name: verilog-lint-root-cause-csv
description: Use this skill when the user provides a Verilog/SystemVerilog lint report in either normalized violation_id/severity/message_id/description/file_path/line_number format or legacy Stage/MessageID/Severity/Contents/LineNo format, plus source files or a source archive, and wants a root-cause CSV whose columns describe root causes, fix suggestions, source ranges, parent root IDs, and leaf violation IDs.
license: MIT
metadata:
  author: zk
  version: "1.5"
---

# Verilog Lint Root-Cause CSV

## When to Use

- The user provides one lint report and one or more Verilog/SystemVerilog files, directories, or source archives.
- The lint report must use exactly one of these two schemas.

Normalized schema:

```text
violation_id,severity,message_id,description,file_path,line_number
```

After preparation, `violation_id` values must be normalized in input row order
as `vio_001`, `vio_002`, `vio_003`, and so on. This is the canonical input
format used by downstream analysis, regardless of whether the original report
used numeric IDs, different IDs, or legacy rows without IDs.

Legacy schema:

```text
Stage,MessageID,Severity,Contents,LineNo,
```

- The requested output is a root-cause CSV that groups leaf lint violations by root cause and records fix guidance, source ranges, and parent-root relationships.

## Output Schema

Write exactly these columns, in this order:

```text
root_id,root_note,fix_suggestion,root_file_path,root_file_start,root_file_end,parent_root_id,leaf_violation_id,leaf_violation_note
```

Rules:

- `root_id`: stable root-cause ID such as `root_001`. Reuse the same `root_id` for all leaf violations caused by the same source issue. For a confirmed false positive, write the literal value `误报`.
- `root_note`: concise explanation of the concrete root cause.
- `fix_suggestion`: concrete fix for the root cause. For a confirmed false positive, write `/`.
- `root_file_path`: source filename containing the concrete root cause, such as `temp.v`. Use only the filename, not an absolute path.
- `root_file_start`: 1-based inclusive start line of the root-cause range.
- `root_file_end`: 1-based inclusive end line of the root-cause range. For a single-line cause, make it equal to `root_file_start`.
- `parent_root_id`: `/` for a top-level root cause, another `root_id` when this row's root is derived from that parent root, or `/` for a confirmed false positive.
- `leaf_violation_id`: one normalized input `violation_id`, such as `vio_001`.
- `leaf_violation_note`: concise note explaining how to fix or interpret this leaf violation. For a confirmed false positive, write `/`.
- Write one output row per input lint violation. If several violations share the same root cause, repeat the same root fields and use one `leaf_violation_id` per row.
- Do not combine multiple leaf IDs in one cell. Do not add severity, message ID, prose analysis columns, grouped-ID columns, or any extra columns.
- Keep every repeated `root_id` internally consistent: the same `root_note`, `fix_suggestion`, source range, and `parent_root_id` must be used on each row for that root.
- `root_id=误报` is a special marker, not a shared root-cause group. Multiple false-positive rows may all use `误报` with different `root_note` values.
- Write the CSV as UTF-8. A BOM is allowed but not required.

For example:

```text
root_id,root_note,fix_suggestion,root_file_path,root_file_start,root_file_end,parent_root_id,leaf_violation_id,leaf_violation_note
root_001,mem is read but never written or initialized,Add explicit writes or initialization for mem,temp.v,6,6,/,vio_008,Provide a defined value for mem before it is read
root_002,case paths do not assign every output,Assign defaults before the case or assign every output in every branch,temp.v,10,14,/,vio_013,Ensure o1 is assigned on all case paths
root_003,latch-derived gated clock warning,Fix root_002 first; the derived warning should disappear,temp.v,10,14,root_002,vio_021,Remove the o1 latch rather than changing clock logic directly
误报,The reported unloaded net is an internal temporary whose value is consumed in the same sequential update and does not indicate a functional issue,/,temp.v,18,18,/,vio_022,/
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
The helper rewrites the first column of the normalized lint report to
`vio_001`, `vio_002`, `vio_003`, ... in row order.

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

- Read `normalized_lint_report.csv` first. It has the same schema as the normalized input example: `violation_id,severity,message_id,description,file_path,line_number`, with `violation_id` values normalized to `vio_<number>`.
- Then read `lint_items.csv` if you need helper metadata such as original report line number or original source path.
- Inspect the referenced source files around candidate cause lines.
- For each violation, identify the smallest source range that explains the reported effect.
- If several lint rows are different effects of the same source construct, assign them the same `root_id` and repeat the same root fields.
- Use `parent_root_id` only for a real derived relationship. Use `/` for independent top-level roots.
- If a lint row is a confirmed false positive, still emit one row for that `leaf_violation_id`: set `root_id` to `误报`, put the false-positive reason in `root_note` rather than `/`, set `fix_suggestion`, `parent_root_id`, and `leaf_violation_note` to `/`, and fill `root_file_path`, `root_file_start`, `root_file_end`, and `leaf_violation_id` normally.
- If a lint row is policy-only but not a false positive, keep a normal `root_<number>` ID and explain the policy rationale and fix or waiver suggestion in the normal fields.
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
- Ensure every input `violation_id` appears exactly once as a `leaf_violation_id`.
- Keep repeated normal `root_<number>` values consistent and make derived roots point to an existing parent root. Do not apply normal root consistency to `root_id=误报`.
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
