---
name: verilog-lint-root-cause-csv
description: Use this skill when the user provides a Verilog/SystemVerilog lint report in either normalized violation_id/severity/message_id/description/file_path/line_number format or legacy Stage/MessageID/Severity/Contents/LineNo format, plus source files or a source archive, and wants a root-cause CSV whose columns describe root causes, fix suggestions, source ranges, parent root IDs, and leaf violation IDs.
license: MIT
metadata:
  author: zk
  version: "1.7"
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

## Terminology and Grouping Policy

- `rule`: a lint check rule, identified by `message_id`.
- `violation` or `message`: one concrete lint report emitted by a rule. It carries the concrete file, line number, object, and diagnostic text.
- `category`: a common potential error behavior. In business terms, a category may correspond to one rule, several rules, or no direct rule.
- `group`: a set of violations grouped by one chosen feature.
- `group by root cause`: group violations whose root cause is the same source-code location or source-code range. This is the target grouping method for this skill.
- `group by fixing pattern`: group violations that can be fixed or waived with a similar method. This is useful after designer confirmation for repair automation.

Use `group by root cause` as the target grouping method for this skill.

- A valid root-cause group should let the designer fix one concrete source location or range and clear all violations in that group.
- Acceptance criterion: applying the group's `fix_suggestion` to that one source location or range should clear all violations in this group while leaving unrelated groups independent.
- If two violations need similar fixes but come from different source locations or different independent source constructs, they must use different `root_id` values.
- If one source construct or source mistake triggers multiple rules, categories, or diagnostic messages, those leaf violations should share one `root_id`.
- `fix_suggestion` describes how to fix the identified root cause. Use the concrete source root cause as the grouping key.

## Output Schema

Write exactly these columns, in this order:

```text
root_id,root_note,fix_suggestion,root_file_path,root_file_start,root_file_end,parent_root_id,leaf_violation_id,leaf_violation_note
```

Rules:

- `root_id`: stable root-cause ID such as `root_001`. Reuse the same `root_id` for all leaf violations caused by the same source issue. For a confirmed false positive, write the literal value `误报`.
- `root_note`: concise Chinese explanation of the concrete root cause.
- `fix_suggestion`: concrete Chinese fix for the root cause. For a confirmed false positive, write `/`.
- `root_file_path`: source filename containing the concrete root cause, such as `temp.v`. Use only the filename, not an absolute path.
- `root_file_start`: 1-based inclusive start line of the root-cause range.
- `root_file_end`: 1-based inclusive end line of the root-cause range. For a single-line cause, make it equal to `root_file_start`.
- `parent_root_id`: `/` for a top-level root cause, another `root_id` when this row's root is derived from that parent root, or `/` for a confirmed false positive.
- `leaf_violation_id`: one normalized input `violation_id`, such as `vio_001`.
- `leaf_violation_note`: write the corresponding `message_id:description` from `normalized_lint_report.csv`, such as `LatchIsInferred:Latch is inferred for signal 'o1'`. Copy both `message_id` and `description` exactly, joined by one literal colon. Keep it as the original diagnostic text rather than a summary, translation, or fix note.
- If a copied `description` contains commas, quotes, or newlines, preserve the whole `message_id:description` value as one `leaf_violation_note` cell using standard CSV quoting and escaping.
- Write one output row per input lint violation. If several violations share the same root cause, repeat the same root fields and use one `leaf_violation_id` per row.
- Write exactly one `leaf_violation_id` per row and exactly the 9 specified columns.
- Keep every repeated `root_id` internally consistent: the same `root_note`, `fix_suggestion`, source range, and `parent_root_id` must be used on each row for that root.
- `root_id=误报` is a special marker, not a shared root-cause group. Multiple false-positive rows may all use `误报` with different `root_note` values.
- Keep the CSV header and structural IDs in English exactly as specified. Write analysis-authored natural-language values such as `root_note` and `fix_suggestion` in Chinese. `leaf_violation_note` is source data copied from the normalized lint report and may retain the lint tool's original language. Keep code identifiers, signal names, module names, file paths, rule/message IDs, violation IDs, and Verilog literals unchanged.
- Write the CSV as UTF-8. A BOM is allowed but not required.

For example:

```text
root_id,root_note,fix_suggestion,root_file_path,root_file_start,root_file_end,parent_root_id,leaf_violation_id,leaf_violation_note
root_001,mem数组被读取但没有任何写入或初始化,为mem添加明确的写入逻辑或初始化,temp.v,6,6,/,vio_008,VarReadBeforeSet:The variable 'mem' is read before it is set
root_002,case分支没有在所有路径上为每个输出赋值,在case前设置默认值或在每个分支中完整赋值,temp.v,10,14,/,vio_013,LatchIsInferred:Latch is inferred for signal 'o1'
root_003,由root_002推断锁存器后派生出的gated clock告警,先修复root_002；该派生告警应随锁存器消除而消失,temp.v,10,14,root_002,vio_021,LatchGatedClock:The latch inferred for 'o1' is used as a gated clock
误报,该unloaded net是同一时序更新内部使用的临时信号，不构成功能问题,/,temp.v,18,18,/,vio_022,DrivenNetUnloaded:The driven net 'tmp' is unloaded in the design
```

## Workflow

### 1. Prepare deterministic inputs — coordinator

Run the helper below first instead of manually parsing original reports in the
analysis workflow, and treat its generated `normalized_lint_report.csv`,
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

Use the printed `WORK_DIR`, `NORMALIZED_LINT_REPORT_CSV`, `LINT_ITEMS_CSV`, `LINT_ITEMS_JSON`, and `SOURCE_ROOT` paths.

### 2. Analyze root causes — coordinator and lint subagents

- Read `normalized_lint_report.csv` first. It has the same schema as the normalized input example: `violation_id,severity,message_id,description,file_path,line_number`, with `violation_id` values normalized to `vio_<number>`.
- Then read `lint_items.csv` if you need helper metadata such as original report line number or original source path.
- Inspect the referenced source files around candidate cause lines.
- For each violation, identify the smallest source range that explains the reported effect.
- If several lint rows are different effects of the same source construct, assign them the same `root_id` and repeat the same root fields.
- Group rows by shared concrete source root cause. Similar fixes at independent source locations are separate root-cause groups.
- Before finalizing each repeated `root_id`, check the one-fix acceptance criterion: one source edit at the recorded root range should clear that group's leaf violations, while unrelated groups remain independent.
- Use `parent_root_id` only for a real derived relationship. Use `/` for independent top-level roots.
- If a lint row is a confirmed false positive, still emit one row for that leaf: set `root_id` to `误报`, put the false-positive reason in `root_note` rather than `/`, set `fix_suggestion` and `parent_root_id` to `/`, fill `leaf_violation_id` with the normalized `violation_id`, and fill `leaf_violation_note` as `message_id:description`.
- If a lint row is policy-only but not a false positive, keep a normal `root_<number>` ID and explain the policy rationale and fix or waiver suggestion in the normal fields.
- Prefer concise, code-evidenced ranges. For example, if a case item and its assignments are the root cause, the range should cover that case statement or the offending assignment block rather than the whole file.

### 3. Multi-agent delegation

This skill uses the DeepAgents `task` tool as a multi-agent workflow. The
coordinator delegates bounded analysis slices to the project lint subagents
below, then reconciles their candidate findings into the final root-cause CSV.

Step routing:

| Step | Executor |
| --- | --- |
| Prepare deterministic inputs | coordinator |
| Read normalized lint rows and plan slices | coordinator |
| Local module/block/range root-cause analysis | `task(subagent_type="module-local-analyzer")` |
| Cross-module clock/reset/control/data-flow analysis | `task(subagent_type="cross-module-flow-analyzer")` |
| Naming/style/policy/waiver-oriented analysis | `task(subagent_type="style-policy-analyzer")` |
| Reconcile candidate roots and assign final IDs | coordinator |
| Write first CSV draft | coordinator |
| Review CSV draft contract and coverage | `task(subagent_type="csv-reviewer")` |
| Apply review fixes, sort, and validate | coordinator |

The coordinator keeps these responsibilities:

- Run input preparation and treat the generated `normalized_lint_report.csv`,
  `lint_items.csv`, `lint_items.json`, and `SOURCE_ROOT` as authoritative.
- Partition the work by source module, source range, rule family, or suspected
  cross-module flow. Keep related `vio_*` rows in the same slice instead of
  splitting only by raw row count.
- Decide final grouping after all candidate reports are returned.
- Assign final `root_###` IDs, resolve `parent_root_id`, write the CSV, sort it,
  run validation, and fix validation errors.

Use these exact `subagent_type` names for delegated analysis:

- `module-local-analyzer`: use for violations whose likely cause is inside one
  RTL module, one `always`/`assign`/`case` block, one declaration range, or one
  bounded source slice. Provide the relevant `vio_*` rows, `message_id`,
  `description`, file path, line numbers, and source excerpt. Ask it to group
  only rows explainable by one local source range.
- `cross-module-flow-analyzer`: use for reported lines that are downstream of
  the real cause, especially clock/reset/enable/control/data propagation,
  derived or gated clocks, instance wiring, port maps, or hierarchical signal
  roles. Provide the involved lint rows plus the connected module declarations,
  instantiations, port connections, assignments, and relevant upstream/downstream
  slices.
- `style-policy-analyzer`: use for naming, style, convention, policy-only, or
  waiver-oriented rows such as port/signal/instance/parameter naming. Provide
  the exact declaration or port/instance list evidence. It returns either a
  normal root cause or a confirmed false positive, with justification from the
  supplied RTL evidence.
- `csv-reviewer`: use after the coordinator has a first CSV draft. Provide the
  draft CSV plus `lint_items.json` or equivalent normalized lint rows. Ask it to
  report only schema, coverage, duplicate/missing leaf, repeated-root
  consistency, parent-reference, false-positive, and `leaf_violation_note`
  mismatch issues.

Subagent task contract:

- A subagent returns candidate findings for the slice it receives.
- A subagent uses temporary `candidate_root_id` values such as `candidate_001`.
- A subagent report includes candidate source ranges, covered `vio_*` leaf
  IDs, root-cause notes, fix suggestions, real parent-candidate relationships,
  and concise evidence.
- The coordinator uses the four specialized lint subagents above for all
  delegated root-cause analysis in this skill.
- After subagents return, the coordinator must reconcile overlaps, missing
  leaves, duplicated candidates, parent relationships, and false-positive
  evidence before writing the final 9-column CSV.

### 4. Write the CSV — coordinator

Unless the user gives an explicit output path, write:

```text
reports/verilog_lint_root_cause_<YYYYMMDD_HHMMSS>.csv
```

The timestamp must come from an executed command in the current environment.

### 5. Second-pass review — csv-reviewer and coordinator

After writing the first CSV draft, perform a full second-pass review before
validation:

- Re-read every CSV row and the corresponding lint item from `lint_items.csv`.
- Re-open the relevant source code ranges for rows that are broad, style-only, tool-policy-only, or based on a lint message that may not be a functional defect.
- Ensure every input `violation_id` appears exactly once as `leaf_violation_id`.
- Ensure every `leaf_violation_note` exactly matches `message_id:description` from the corresponding normalized lint row.
- Keep repeated normal `root_<number>` values consistent and make derived roots point to an existing parent root. Treat `root_id=误报` rows independently.
- Keep the CSV schema unchanged with exactly the 9 specified columns and one `leaf_violation_id` per row.
- Ensure `root_note` and `fix_suggestion` use Chinese natural-language text except for code identifiers, signal names, module names, file paths, rule/message IDs, violation IDs, and Verilog literals.

Continue after the first CSV write. The final CSV must include the results of
this second-pass review.

### 6. Sort before validation — coordinator

After the second-pass review, sort the CSV by `root_id`, then by the numeric
`vio_<number>` prefix in `leaf_violation_id`:

```bash
python skills/verilog-lint-root-cause-csv/scripts/sort_root_cause_csv.py \
  <output_csv>
```

The sorter keeps the 9-column schema unchanged and preserves one row per input
lint violation. Use `--output <sorted_csv>` only when the user asks to keep the
unsorted draft.

### 7. Validate before finishing — coordinator

```bash
python skills/verilog-lint-root-cause-csv/scripts/validate_root_cause_csv.py \
  <output_csv> \
  --lint-items <LINT_ITEMS_JSON>
```

Fix validation errors and rerun until it passes.
