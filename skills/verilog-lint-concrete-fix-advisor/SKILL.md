---
name: verilog-lint-concrete-fix-advisor
description: Use this skill when the user provides Verilog/SystemVerilog code plus a lint warning and wants a specific, code-aware repair recommendation rather than a generic lint-tool message, especially incomplete case coverage warnings such as W69 where the agent must identify missing case items or propose a concrete default branch.
license: MIT
metadata:
  author: zk
  version: "1.0"
---

# Verilog Lint Concrete Fix Advisor

## When to Use

- The user provides RTL code and a lint warning.
- The user wants a concrete fix, not a generic message such as "add missing cases or default".
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

Then the concrete diagnosis is:

```text
The selector appears to be 2 bits wide and covers 00, 01, and 11. The missing item is 2'b10.
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

### 4. Final response shape

For each warning, return:

- `问题定位`: exact construct and source line if known.
- `根本原因`: source-specific cause.
- `具体修复方案`: at least one concrete patch option.
- `需要设计者确认`: any behavior that cannot be inferred safely.
- `为什么不是泛泛建议`: explain what was derived from the actual code.

## Guardrails

- Do not invent intended logic for missing branches.
- Do not silently add `default` if an explicit missing enumerated value is better for readability or coverage.
- Do not treat `default` as always correct; in safety-critical decode logic, explicit enumeration can be preferable.
- Do not modify unrelated branches.
- Preserve source signal names and widths.

