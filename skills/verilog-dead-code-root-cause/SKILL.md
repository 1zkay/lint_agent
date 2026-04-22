---
name: verilog-dead-code-root-cause
description: Use this skill when the user provides Verilog/SystemVerilog source files or a source directory plus a top module, and wants to diagnose unreachable procedural branches, static dead-code conditions, or logic that disappears before/after Yosys proc/opt, especially cases where for-loop unfolding or constant loop variables make if/case branches impossible.
license: MIT
metadata:
  author: zk
  version: "1.0"
---

# Verilog Dead Code Root Cause

## When to Use

- The user asks whether a Verilog branch, assignment, signal, or block is dead code.
- The design has procedural `always` blocks, `for` loops, `if/else`, or `case` statements.
- The suspected issue is like `for (k = 0; k < 3; ...)` with conditions such as `if (k == 3)` or `if (k > 3)`.
- The user wants to know whether before/after RTLIL diff is enough to recover the dead-code root.

## Key Rule

Do not rely only on `raw_proc.il -> opt_proc.il` diff for procedural dead-code diagnosis.

Yosys may eliminate unreachable branches during Verilog elaboration or process lowering before the standard `proc` output. In those cases:

- `pre_proc.il` may still contain `process` and `switch 1'0` / `switch 1'1` with source locations.
- `raw_proc.il` may already have lost the unreachable branch.
- `opt_proc.il` may only show later cleanup, such as unused registers or internal logic being purged.

## Workflow

### 1. Generate evidence files

Run from any working directory:

```powershell
python <mcp_alint>/skills/verilog-dead-code-root-cause/scripts/run_dead_code_trace.py --top <top_module> <source_or_dir> [more_sources...]
```

The script writes a new directory under `<mcp_alint>/reports/dead_code_<YYYYMMDD_HHMMSS>/` containing:

- `dead_code_artifacts.json`
- `pre_proc.il`
- `raw_proc.il`
- `raw_proc_ifx_noopt.il`
- `opt_proc.il`

Read `ARTIFACTS_PATH=...` from the script output and open `dead_code_artifacts.json` first.

### 2. Diagnose in this order

1. Read the source around the suspicious `always`, `for`, `if`, or `case`.
2. Read `pre_proc.il` and search for the source line, `process`, `switch 1'0`, and `switch 1'1`.
3. Read `raw_proc.il` to see what remains after `proc`.
4. Read `opt_proc.il` only to confirm additional cleanup after optimization.
5. Use `raw_proc_ifx_noopt.il` as a sanity check when you suspect default `proc` passes are hiding information.

### 3. What Counts as a Strong Dead-Code Finding

Report a likely real dead-code condition when several of these hold:

- Source has a statically impossible condition, such as loop variable ranges that cannot satisfy the branch.
- `pre_proc.il` shows a constant switch condition, especially `switch 1'0` for an unreachable branch.
- The source location on the `switch` points back to the suspicious `if` or `case`.
- Signals assigned only inside the unreachable branch become unused or disappear later.
- `raw_proc.il` no longer contains a corresponding mux/comparator/assignment for the branch.

### 4. What Not to Claim

- Do not claim `raw_proc -> opt_proc` alone proves the original dead condition. It may only prove later cleanup.
- Do not treat every removed register as dead code; it may be removed because the module has no observable outputs.
- Do not report a branch as dead unless source-level ranges or `pre_proc.il` constant switches support it.

### 5. Final Response Shape

For each finding, report:

- The source condition and source location.
- Why the condition is statically unreachable.
- Evidence from `pre_proc.il`, such as constant `switch 1'0`.
- What remains in `raw_proc.il`.
- What `opt_proc.il` removes later, if relevant.
- Whether the root is source-level dead code or only post-`proc` cleanup.

