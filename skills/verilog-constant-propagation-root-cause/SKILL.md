---
name: verilog-constant-propagation-root-cause
description: Use this skill when the user provides Verilog/SystemVerilog source files or a source directory plus a top module, and wants a concise Chinese JSON diagnosis for hierarchical constant-propagation defects caused by parent-module constant pins or named wires that pollute child-module inputs, internal wires, and outputs across multiple levels, using the bundled trace_removed_path.py engine and then separating likely real defects from design-intended constants.
license: MIT
metadata:
  author: zk
  version: "1.4"
---

# Verilog Constant Propagation Root Cause

## When to Use

- The user provides one or more `.v`, `.vh`, `.sv`, or `.svh` source files, or a source directory.
- The user also provides a top module.
- The user wants to find constant-propagation defects across hierarchy, not just top-level constant outputs.
- The user cares about the earliest parent-module named root and the downstream polluted signal set.
- The user wants help deciding whether the detected roots are likely real defects or design-intended constants.
- The user wants the final diagnosis saved as a concise Chinese JSON report.

## Required Inputs

- One or more HDL source files, or one source directory.
- One top module name.
- Optional additional context about expected behavior or suspicious modules.

## Workflow

### 1. Run the detector

- Run directly from the `lint_agent` project root:

```powershell
python skills/verilog-constant-propagation-root-cause/scripts/run_constant_trace.py --top <top_module> <source_or_dir> [more_sources...]
```

- Do not guess the report path. Read the wrapper output and use the reported output directory and report path.
- The wrapper always writes results under `reports/constant_propagation_<YYYYMMDD_HHMMSS>/` relative to the project root.
- The detector writes:
  - `trace_removed_path_report.json`
  - `diagnosis_bundle.json`
  - `raw_design.json`
  - `opt_design.json`
  - `noopt_proc.il`
  - `raw_proc.il`
  - `opt_proc.il`

### 2. Read the detector output in this order

- First read `diagnosis_bundle.json`.
- Then read `trace_removed_path_report.json`.
- Focus on:
  - Summary counts
  - `源码相比优化后少掉的逻辑`
  - Whether each missing logic group is marked as caused by constant propagation
  - Signals that become direct constants after optimization
  - Associated explicit roots
  - Source snippets and source locations on each missing logic item
- Treat `trace_removed_path_report.json`, `raw_design.json`, `noopt_proc.il`, and source code as required evidence for the final diagnosis.
- Use `raw_proc.il` and `opt_proc.il` only when the simplified missing-logic evidence is not enough to explain the optimization result.

### 3. Decide whether a root is a real defect or a design-intended constant

- Use the rubric in `references/triage-rubric.md`.
- Prefer roots that meet several of these conditions:
  - The root is introduced in a parent or mid-level module, not a known architectural tie-off.
  - The same root pollutes multiple child modules or multiple branches.
  - The polluted signals are control-path signals such as `valid`, `ready`, `enable`, `flush`, `debug`, `exception`, `predict`, `mode`, or select lines.
  - The root collapses logic in modules that should normally remain data-dependent.
  - The source code around the root does not contain a clear comment or configuration reason for tying it off.
- Be conservative about declaring a real defect when the root is clearly a configuration constant, architectural constant, or protocol-required tie-off.

### 4. Use noopt vs opt as the user-facing source/optimization comparison

- `noopt_proc.il` is the source-like structural view that preserves combinational logic from the source.
- `opt_proc.il` is the post-optimization structural view after `proc` and `opt`.
- Prefer the report field `源码相比优化后少掉的逻辑` to answer:
  - which source-level combinational cells disappeared after optimization,
  - which output became constant,
  - and whether the disappearance is caused by constant propagation roots.
- Do not manually diff the whole RTLIL files unless necessary. Start from the missing logic group, signal path, or source location from the report.

### 5. Final diagnosis is mandatory

- Do not stop after reading the JSON report.
- The final diagnosis must combine:
  - `trace_removed_path_report.json`, especially `源码相比优化后少掉的逻辑`
  - `raw_design.json`
  - `noopt_proc.il`
  - source files around the root and the polluted modules
- A result is not complete until the agent checks whether the logic missing after optimization matches the root and the source-level intent.
- If the report contains missing logic groups, treat those groups as the structural optimization evidence.

### 6. Write the final Chinese JSON diagnosis

- Write the final diagnosis to the report directory printed by the wrapper:

```text
<REPORT_DIR>/constant_propagation_diagnosis.json
```

- The final assistant response should be brief and in Chinese: state the JSON report path and the number of likely real defects. Do not duplicate the full report in prose unless the user asks.
- The JSON report must contain only core content:

```json
{
  "摘要": {
    "顶层模块": "top",
    "输入路径": ["rtl"],
    "产物目录": "reports/constant_propagation_20260428_153000",
    "疑似真实缺陷数量": 1,
    "设计预期常量数量": 2,
    "输出路径": "reports/constant_propagation_20260428_153000/constant_propagation_diagnosis.json"
  },
  "发现项": [
    {
      "编号": "CP_001",
      "类别": "疑似真实缺陷",
      "根源信号": "parent_cfg_force_zero",
      "根源模块": "top",
      "受影响模块": ["u_child"],
      "污染信号": ["valid_i", "enable_i"],
      "少掉的逻辑": ["u_child.$procmux$12"],
      "证据": {
        "报告证据": "trace_removed_path_report.json 中的“源码相比优化后少掉的逻辑”",
        "RTLIL证据": "noopt_proc.il 中存在相关源码组合逻辑，opt_proc.il 中对应输出被改接为常量",
        "源码证据": "源码中该控制信号被上层常量连接，未看到明确配置意图"
      },
      "诊断": "上层常量连接污染子模块控制路径，导致下游逻辑被优化删除，疑似非预期常量传播。",
      "需要确认": [
        "确认该控制信号是否本应固定为常量。"
      ]
    }
  ]
}
```

Field rules:

- JSON keys and diagnosis text must both be Chinese.
- `摘要` must contain only top module, inputs, artifact directory, counts, and output path.
- `发现项` must contain only likely real defects or clearly important intended constants.
- Use `类别` values `疑似真实缺陷` or `设计预期常量`.
- `诊断` must be concise and evidence-based.
- `证据` must cite only facts from `trace_removed_path_report.json`, `raw_design.json`, `noopt_proc.il`, optional `raw_proc.il`/`opt_proc.il`, and source code.
- Do not include broad RTL optimization background or generic constant-propagation explanation.
- If no likely real defect is found, write `"发现项": []` and set `疑似真实缺陷数量` to `0`.

## Guardrails

- Do not treat every constant root as a defect.
- Do not rely only on signal names. Read the source around the root and the polluted modules.
- Do not claim a parent-module named root unless the detector already promoted it or the source clearly proves it.
- Do not ignore `源码相比优化后少掉的逻辑`; the detector is intentionally filtered to source logic that already disappeared after optimization.
- Do not spend time manually reading the full `raw_design.json` unless the report and source are insufficient.

## References

- For defect-vs-expected triage, read `references/triage-rubric.md`.

## Example Requests

- "Analyze this RTL directory with top `e203_core` and tell me whether there is a real constant-propagation defect."
- "Given these Verilog files and top `FABSCALAR`, locate the earliest parent constant root and the polluted hierarchy."
- "Run constant-propagation root-cause analysis on this project and separate true defects from expected tie-offs."
