---
name: verilog-dead-code-root-cause
description: Use this skill when the user provides Verilog/SystemVerilog source files or a source directory plus a top module, and wants a concise Chinese JSON diagnosis for unreachable procedural branches, static dead-code conditions, or logic that disappears before/after Yosys proc/opt, especially cases where for-loop unfolding or constant loop variables make if/case branches impossible.
license: MIT
metadata:
  author: zk
  version: "1.1"
---

# Verilog Dead Code Root Cause

## When to Use

- The user asks whether a Verilog branch, assignment, signal, or block is dead code.
- The design has procedural `always` blocks, `for` loops, `if/else`, or `case` statements.
- The suspected issue is like `for (k = 0; k < 3; ...)` with conditions such as `if (k == 3)` or `if (k > 3)`.
- The user wants to know whether before/after RTLIL diff is enough to recover the dead-code root.
- The user wants the final diagnosis saved as a concise Chinese JSON report.

## Key Rule

Do not rely only on `raw_proc.il -> opt_proc.il` diff for procedural dead-code diagnosis.

Yosys may eliminate unreachable branches during Verilog elaboration or process lowering before the standard `proc` output. In those cases:

- `pre_proc.il` may still contain `process` and `switch 1'0` / `switch 1'1` with source locations.
- `raw_proc.il` may already have lost the unreachable branch.
- `opt_proc.il` may only show later cleanup, such as unused registers or internal logic being purged.

## Workflow

### 1. Generate evidence files

Run from the `lint_agent` project root:

```powershell
python skills/verilog-dead-code-root-cause/scripts/run_dead_code_trace.py --top <top_module> <source_or_dir> [more_sources...]
```

The script writes a new directory under `reports/dead_code_<YYYYMMDD_HHMMSS>/` relative to the project root containing:

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

### 5. Write the final Chinese JSON diagnosis

- Write the final diagnosis to the report directory printed by the wrapper:

```text
<REPORT_DIR>/dead_code_diagnosis.json
```

- The final assistant response should be brief and in Chinese: state the JSON report path and the number of dead-code findings. Do not duplicate the full report in prose unless the user asks.
- The JSON report must contain only core content:

```json
{
  "summary": {
    "top_module": "top",
    "input_paths": ["rtl"],
    "artifact_dir": "reports/dead_code_20260428_153000",
    "dead_code_count": 1,
    "output_path": "reports/dead_code_20260428_153000/dead_code_diagnosis.json"
  },
  "findings": [
    {
      "id": "DC_001",
      "file": "rtl/top.sv",
      "line": 58,
      "condition": "if (k == 3)",
      "category": "源码级死代码",
      "root_cause": "for 循环中 k 的取值范围为 0 到 2，因此 k == 3 永远不成立。",
      "evidence": {
        "source_evidence": "源码中的循环范围与条件互斥",
        "pre_proc_evidence": "pre_proc.il 中对应分支表现为 switch 1'0",
        "raw_proc_evidence": "raw_proc.il 中已不存在该分支对应的 mux 或赋值",
        "opt_proc_evidence": "opt_proc.il 中仅保留后续清理结果"
      },
      "diagnosis": "该分支为静态不可达分支，应删除或修正循环范围/条件。",
      "fix_hint": "如果 k==3 是有效场景，应调整循环边界；否则删除该不可达分支。"
    }
  ]
}
```

Field rules:

- Keep JSON keys stable in English, but write all diagnosis text in Chinese.
- `summary` must contain only top module, inputs, artifact directory, count, and output path.
- `findings` must contain one entry per confirmed dead-code condition.
- Use `category` values `源码级死代码` or `优化清理`.
- `root_cause`, `diagnosis`, and `fix_hint` must be concise and evidence-based.
- `evidence` must cite only facts from source code, `pre_proc.il`, `raw_proc.il`, `raw_proc_ifx_noopt.il`, and `opt_proc.il`.
- Do not include broad Yosys background or generic dead-code explanation.
- If no dead code is confirmed, write `"findings": []` and set `dead_code_count` to `0`.
