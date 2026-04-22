---
name: verilog-lint-triage
description: Use this skill when the user provides Verilog/SystemVerilog source files and a lint report, and wants the lint rows first pre-grouped by same source line and same violation description, then triaged into severe defect, general defect, or false positive, plus missed-defect discovery mapped to rules in the built-in custom knowledge base under skills/verilog-lint-triage/references, while batching IEEE standard lookups by semantic topic instead of per lint row and using the built-in Vivado synthesis document when the issue depends on synthesis behavior or tool guidance, plus a separate standards-based code-only diagnosis section grounded in IEEE and/or Vivado built-in references, with the final result written to a timestamped JSON file.
license: MIT
compatibility: Assumes the agent shell runs with the mcp_alint directory as its working directory and can access the input HDL files, the lint report, and the self-built knowledge base directory at skills/verilog-lint-triage/references.
metadata:
  author: zk
  version: "1.3"
---

# Verilog Lint Triage

## When to Use

- The user uploads or references `.v`, `.vh`, `.sv`, or `.svh` files together with a lint report.
- The user wants you to decide whether each lint finding is a real defect or a false positive.
- The user wants missed defects added, and each real defect mapped to one or more rules in the self-built knowledge base.
- The user wants a machine-readable JSON result file, not only prose.
- The user also wants a standards/tool-reference diagnosis section that is based on the source code itself rather than the lint report.

## Required Inputs

- One lint report file.
- One or more Verilog/SystemVerilog source files.
- Optional output path. If the user does not specify one, write `reports/verilog_lint_triage_result_<YYYYMMDD_HHMMSS>.json`.
- When using the default output path, you must first execute a command inside the running environment to read the current local time, then build the timestamped filename from that command output. Do not guess the date.

## Workflow

### 1. Normalize the inputs

- Read the self-built knowledge base file in `skills/verilog-lint-triage/references/` before making any defect judgment.
- Read the knowledge base as `utf-8-sig`. The file currently starts with a BOM.
- Before calling `query_reference_docs`, first group candidate IEEE-dependent or Vivado-dependent questions by semantic topic. Typical IEEE topics include blocking vs nonblocking assignment semantics, combinational process completeness and `always_comb`, event control and sensitivity semantics, net/variable declaration rules, typing rules, reset semantics, interface/modport rules, and assertion semantics. Typical Vivado topics include synthesis inference behavior, `RAM_STYLE`, `FSM_ENCODING`, resource mapping, attributes, constraints interactions, and tool-specific coding guidance.
- Do not call `query_reference_docs` once per lint row. Prefer a small number of topic-level queries that can support multiple lint findings at once.
- Only call `query_reference_docs` for issues that truly depend on Verilog or SystemVerilog language semantics, scheduling rules, procedural block behavior, `wire` / `reg` / variable rules, assignment semantics, typing rules, assertion semantics, interface/modport rules, Vivado synthesis behavior, synthesis attributes, constraints-sensitive inference, or another point that should be checked against the built-in IEEE standard or Vivado synthesis document.
- Identify the lint report format first. It may be CSV, TSV, plain text, or a tool-specific table dump.
- Preserve the original report lines. In merged `lint_items`, record their physical 1-based line numbers in `report_line_numbers`.
- Skip blank lines and obvious header rows. If they matter, mention the handling briefly in `summary.summary_text`.
- Before substantive defect analysis, pre-group data-bearing lint rows that point to the same source file line and carry the same or equivalent violation description. This pre-grouping step happens before final judgment, not after it.
- Use the reported source file, reported source code line, and normalized violation description as the primary grouping key. Keep rows separate when the reported source line differs or the defect description is materially different.
- If the report lacks a reliable source file, source line, or defect description for a row, keep that row as its own analysis unit rather than forcing it into a group.

### 2. Triage every lint finding line-by-line

- Process every data-bearing lint line in the report, but perform the main judgment on the pre-grouped analysis units built in step 1.
- For each pre-grouped unit, inspect the referenced source file and the surrounding code once before judging the grouped issue.
- If the report points to a wrong file or wrong line, still keep the original lint line in the grouped result and explain the mismatch.
- Map each grouped issue to the most relevant rule or rules from the self-built knowledge base.
- For ambiguous findings, or any finding tied to IEEE language behavior, legacy Verilog semantics, or SystemVerilog semantics, reuse the batched topic-level `query_reference_docs` results before finalizing the judgment and fold the returned page citations into `why` when they materially support the diagnosis.
- For findings tied to Vivado synthesis behavior, synthesis attributes, resource inference, or other tool-specific implementation behavior, reuse the batched topic-level `query_reference_docs` results from the built-in Vivado synthesis document before finalizing the judgment and fold the returned page citations into `why` when they materially support the diagnosis.
- A single IEEE query result may support multiple `lint_items` and missed defects if they share the same semantic topic. Reuse citations instead of re-querying.
- Assign exactly one category:
  - `严重`: Clear real defect with likely functional risk, synthesis/simulation mismatch, timing/data-loss risk, latch risk, CDC/reset/FSM failure risk, or another materially dangerous hardware bug.
  - `一般`: Real defect or meaningful coding issue, but the risk is lower or more localized.
  - `误报`: The lint finding does not hold after reading the code and the knowledge base, or the risk is negligible in the concrete implementation.
- Every judgment must include code-based evidence. Do not mark a finding as `误报` only because the lint text looks generic.
- Build final `lint_items` directly from those pre-grouped issue units:
  - If the same source code line has multiple lint findings with the same defect description, analyze them together and emit one JSON entry.
  - In that grouped entry, keep all contributing report line numbers, raw report lines, rule IDs, and report severities together.
  - If the same source code line has lint findings with different defect descriptions, keep them as separate grouped entries and analyze them separately.
- The final `lint_items` are issue-oriented grouped results, not a 1:1 copy of the original lint rows and not a post-analysis merge artifact.

### 3. Search for missed defects

- After processing the lint lines, review the relevant HDL code for clearly evidenced defects that the report missed.
- Only add a missed defect if you can point to concrete code and at least one supporting rule from the knowledge base.
- If the missed defect hinges on Verilog or SystemVerilog standard semantics, first try to reuse an existing topic-level IEEE query result. If it hinges on Vivado synthesis behavior or tool-specific inference behavior, first try to reuse an existing Vivado-oriented query result. Only issue a new `query_reference_docs` call when the missed defect introduces a new semantic or synthesis topic that has not been covered yet.
- Missed defects may only use `严重` or `一般`. Do not create missed-defect entries tagged `误报`.

### 4. Add a standards-based code-only diagnosis

- After `missed_defects`, add a new top-level section named `standard_file_diagnosis`.
- This section is independent from the lint report. For this section, inspect each source file directly and use the source code plus the applicable built-in reference evidence. IEEE language-standard evidence and the built-in Vivado synthesis document are peer sources here. Do not rely on lint rows when deciding whether to add a diagnosis finding.
- Create one entry per source file listed in `summary.source_files`, even if a file ends up with zero findings.
- For each source file, organize diagnoses by semantic topic or synthesis/tool topic and reuse prior topic-level query results whenever possible. Only issue new `query_reference_docs` calls for topics not yet covered in this run.
- Use IEEE when the judgment depends on Verilog/SystemVerilog language semantics. Use the built-in Vivado synthesis document when the judgment depends on synthesis behavior, inference rules, attributes, or other tool-specific implementation behavior. Do not force either source onto a topic that belongs to the other.
- Do not query IEEE or Vivado for naming, style, lint-tool policy, general CDC methodology, general constraint methodology, DO-254 process, or other topics outside the scope of those built-in references unless the judgment truly depends on language semantics or Vivado synthesis behavior.
- Record only findings that are supported by concrete code evidence and citations from the applicable built-in reference source.
- When a finding is IEEE-based, present it as an IEEE-supported diagnosis. When a finding is Vivado-based, present it as a Vivado-supported diagnosis. If both are materially needed, cite both explicitly without treating one as subordinate to the other.
- In `standard_file_diagnosis[].findings`, use `category` values `严重`, `一般`, or `提示`.
- If a file has no independent standards/tool-reference diagnosis findings, keep `findings` as an empty array and explain that in `summary_text`.

### 5. Write the final JSON file

- Follow the schema in `skills/verilog-lint-triage/references/output-schema.md`.
- Use Chinese values for the classification fields exactly as specified there.
- Keep the original lint report ordering in `lint_items`.
- Unless the user explicitly asks for another location, write the JSON file to `reports/verilog_lint_triage_result_<YYYYMMDD_HHMMSS>.json`.
- Generate the timestamp once per run using local time in `YYYYMMDD_HHMMSS` format, and reuse the exact same path in both the written file and `summary.output_path`.
- The timestamp must come from an executed command in the current environment, for example:

```bash
date +"%Y%m%d_%H%M%S"
```

or:

```bash
python - <<'PY'
from datetime import datetime
print(datetime.now().strftime("%Y%m%d_%H%M%S"))
PY
```

- Write the result to disk. You must then validate it with:

```bash
python skills/verilog-lint-triage/scripts/validate_triage_json.py <json_path>
```

- This validation step is mandatory. If validation fails, fix the JSON and rerun the validator until it passes. Do not finish the task without a successful validator run.

## Output Rules

- The JSON file must contain:
  - An overall diagnosis result for the whole run.
  - One `lint_items` entry for each merged real issue or merged false-positive issue after applying the merge rule above.
  - A `missed_defects` array, which may be empty.
  - A `standard_file_diagnosis` array with one entry per source file.
- Keep the JSON keys stable as defined in `skills/verilog-lint-triage/references/output-schema.md`, but write all diagnostic content in Chinese.
- When referring to the knowledge base in prompts, summaries, or output fields, call it `自建知识库`. Do not expose the underlying knowledge-base filename.
- The following fields must use Chinese natural-language text:
  - `summary.summary_text`
  - `lint_items[].issue`
  - `lint_items[].why`
  - `lint_items[].fix_hint`
  - `missed_defects[].issue`
  - `missed_defects[].why_missed_by_lint`
  - `missed_defects[].fix_hint`
  - `standard_file_diagnosis[].summary_text`
  - `standard_file_diagnosis[].findings[].issue`
  - `standard_file_diagnosis[].findings[].why`
  - `standard_file_diagnosis[].findings[].fix_hint`
- Keep raw code snippets and file paths unchanged from the source material.
- In merged `lint_items`, keep the original lint metadata in arrays:
  - `report_line_numbers`
  - `report_rule_id`
  - `report_severity`
  - `raw_report_lines`
- `summary.knowledge_base` should use a user-facing label such as `自建知识库`, not a filesystem path.
- `summary.output_path` should point to `reports/verilog_lint_triage_result_<YYYYMMDD_HHMMSS>.json` unless the user explicitly provided another output location.
- Each real defect must reference the violated knowledge-base rule IDs and descriptions.
- When `query_reference_docs` returns useful evidence, mention the IEEE standard and/or Vivado synthesis document page numbers directly in `why`, `why_missed_by_lint`, or `standard_pages`, according to the source actually used.
- Prefer a small number of topic-level IEEE and Vivado queries for a typical small triage run. Exceed that only when the code genuinely spans multiple unrelated semantic or synthesis/tool topics.
- If multiple knowledge-base rules apply, include all strong matches and order the most direct rule first.
- If the lint tool rule name is missing, preserve the raw report text and explain how you inferred the relevant knowledge-base rule.
- Each `standard_file_diagnosis` entry must contain `file`, `summary_text`, and `findings`.
- Each `standard_file_diagnosis[].findings[]` item must contain `id`, `code_line`, `category`, `issue`, `standard_pages`, `why`, `evidence`, and `fix_hint`.
- `standard_file_diagnosis` is code-only diagnosis: do not cite lint rows or use the lint report as evidence in that section. Use source code plus the applicable IEEE and/or Vivado built-in reference evidence only.

## Guardrails

- Do not trust the lint report blindly.
- Do not invent missed defects without code evidence.
- Do not drop a lint line because it looks duplicated or low value; if it should be merged, preserve it inside the merged arrays.
- Do not output Markdown when the user asked for the JSON file.
- Do not reveal the self-built knowledge-base filename in user-visible output.
- If the source code or report is incomplete, still produce the JSON file and record the limitation in the relevant `why` field and the top-level summary.

## Example Requests

- "Analyze `rtl/top.sv` and `reports/alint.csv`, decide which lint findings are real defects, find missed defects, and write the JSON result."
- "Read my uploaded Verilog/SystemVerilog files plus the lint report, use the self-built knowledge base, generate a triage JSON with `严重` / `一般` / `误报`, and then append a standards-based code-only diagnosis section for each source file using IEEE and/or Vivado built-in references as appropriate."

## Example Output

- Read `skills/verilog-lint-triage/references/example-output.json` for a minimal example.
- Read `skills/verilog-lint-triage/references/output-schema.md` for field definitions and overall-result rules.
