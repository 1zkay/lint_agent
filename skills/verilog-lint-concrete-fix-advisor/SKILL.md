---
name: verilog-lint-concrete-fix-advisor
description: Use this skill when the user provides Verilog/SystemVerilog code plus a lint warning and wants a specific, code-aware repair recommendation in a concise Chinese JSON report rather than a generic lint-tool message, especially incomplete case coverage warnings such as W69 where the agent must identify missing case items or propose a concrete default branch.
license: MIT
metadata:
  author: zk
  version: "1.1"
---

# Verilog Lint Concrete Fix Advisor

## When to Use

- The user provides RTL code and a lint warning.
- The user wants a concrete fix, not a generic message such as "add missing cases or default".
- The user wants the result saved as a compact Chinese JSON report.
- The issue involves incomplete `case`, missing `default`, incomplete assignment, unused variable caused by unreachable code, or another warning where source-specific context determines the right fix.
- The warning looks like W69 or says `case` branches are incomplete.

## Core Principle

The final advice must be tied to the exact RTL.

Do not answer with only generic text such as:

```text
Add case clauses or a default clause.
```

Instead, inspect the code and state the missing condition or the exact branch to add.

For example, if the RTL is:

```verilog
case(in3)
  2'b00 : out[0] = in1 || mem[0];
  2'b01 : out[1] = in1 && mem[1];
  2'b11 : out[2] = in2 && mem[3];
endcase
```

具体诊断应写成：

```text
case 选择信号为 2 bit，当前覆盖了 00、01、11，缺少的取值是 2'b10。
```

The concrete fix should include one or both of:

```verilog
2'b10 : out[...] = ...;  // fill in intended behavior
```

or:

```verilog
default: out[...] = ...; // fill in safe intended behavior
```

## Workflow

### 1. Locate the exact construct

- Read the warning line and surrounding RTL.
- Identify the affected construct: `case`, `if/else`, `always`, assignment, declaration, or instance.
- Do not diagnose from the lint text alone.

### 2. For incomplete case warnings, run the helper when useful

Use:

```powershell
python skills/verilog-lint-concrete-fix-advisor/scripts/analyze_case_coverage.py <source_file>
```

Optional line-targeted mode:

```powershell
python skills/verilog-lint-concrete-fix-advisor/scripts/analyze_case_coverage.py <source_file> --line <line_number>
```

The script emits JSON with:

- selector expression,
- inferred selector width,
- explicit case values,
- missing binary values,
- whether a default branch exists.

Use the script output as evidence, not as a substitute for reading the code.

### 3. Produce concrete repair options

For an incomplete `case`:

- If missing values are finite and clear, list each missing item explicitly.
- If the intended behavior is unknown, say so and use placeholders only where semantics are user-defined.
- If a safe default behavior is obvious from surrounding assignments, propose it.
- If no safe default is obvious, propose a `default` branch but explicitly state that the RHS must be filled with the intended behavior.

For other lint warnings:

- Identify the source-level root cause.
- Explain why the tool reports the symptom.
- Propose a localized RTL edit, not a broad coding guideline.

### 4. Write a compact Chinese JSON report

- Unless the user explicitly asks for another path, write the report to:

```text
reports/verilog_lint_fix_advisor_<YYYYMMDD_HHMMSS>.json
```

- Generate the timestamp from the running environment. Do not guess it.
- The final assistant response should be brief and in Chinese: state the JSON report path and the fix item count. Do not duplicate the full report in prose unless the user asks.
- The JSON report must contain only core content:

```json
{
  "summary": {
    "source_files": ["rtl/top.sv"],
    "lint_warning_count": 1,
    "fix_item_count": 1,
    "output_path": "reports/verilog_lint_fix_advisor_20260428_153000.json"
  },
  "fix_items": [
    {
      "id": "FIX_001",
      "file": "rtl/top.sv",
      "line": 42,
      "lint_rule": "W69",
      "construct": "case(sel)",
      "root_cause": "case 选择信号为 2 bit，当前覆盖了 2'b00、2'b01、2'b11，但缺少 2'b10，且没有 default 分支。",
      "evidence": {
        "selector": "sel",
        "inferred_width": 2,
        "explicit_values": ["2'b00", "2'b01", "2'b11"],
        "missing_values": ["2'b10"],
        "has_default": false,
        "code_snippet": "case (sel) ..."
      },
      "fix_options": [
        {
          "type": "add_case_item",
          "patch": "2'b10: out = <设计意图对应的取值>;",
          "requires_designer_confirmation": true
        },
        {
          "type": "add_default",
          "patch": "default: out = <安全默认值或设计意图对应的取值>;",
          "requires_designer_confirmation": true
        }
      ],
      "confirmation_needed": [
        "需要设计者确认 selector 取值为 2'b10 时的预期行为。"
      ]
    }
  ]
}
```

Field rules:

- `summary` must contain only source files, warning count, fix item count, and output path.
- `fix_items` must contain one entry per analyzed warning or grouped warning.
- `root_cause` must be source-specific, concise, and written in Chinese.
- `evidence` must contain only facts read from the RTL or helper output.
- `fix_options` must contain concrete local edits only.
- `patch` and `confirmation_needed` must be written in Chinese except for code, signal names, literals, and file paths.
- `confirmation_needed` must be empty when the correct behavior is fully determined from code context.
- Do not add broad background, coding style essays, or generic lint explanations.
- Do not include a field named `why_not_generic` or `为什么不是泛泛建议`; this is redundant once evidence and concrete fixes are present.

## Guardrails

- Do not invent intended logic for missing branches.
- Do not silently add `default` if an explicit missing enumerated value is better for readability or coverage.
- Do not treat `default` as always correct; in safety-critical decode logic, explicit enumeration can be preferable.
- Do not modify unrelated branches.
- Preserve source signal names and widths.

