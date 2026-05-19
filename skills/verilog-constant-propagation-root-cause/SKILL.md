---
name: verilog-constant-propagation-root-cause
description: Use this skill when the user provides Verilog/SystemVerilog source files or a source directory plus a top module, and wants a concise Chinese JSON diagnosis for hierarchical constant-propagation defects caused by parent-module constant pins or named wires that pollute child-module inputs, internal wires, and outputs across multiple levels, using the bundled trace_constant_propagation.py engine and then separating likely real defects from design-intended constants.
license: MIT
metadata:
  author: zk
  version: "1.5"
---

# Verilog Constant Propagation Root Cause

## When to Use

- The user provides one or more `.v`, `.vh`, `.sv`, or `.svh` source files, or a source directory.
- The user also provides a top module.
- The user wants to find constant-propagation defects across hierarchy, including cases that do not necessarily delete optimized cells.
- The user cares about the earliest named constant root and the downstream polluted signal set.
- The user wants help deciding whether the detected constant propagation is likely a real defect or a design-intended constant.
- The user wants the final diagnosis saved as a concise Chinese JSON report.

## Required Inputs

- One or more HDL source files, or one source directory.
- One top module name.
- Optional additional context about expected behavior or suspicious modules.

## Workflow

### 1. Run the Detector

Run directly from the `lint_agent` project root:

```powershell
python skills/verilog-constant-propagation-root-cause/scripts/run_constant_trace.py --top <top_module> <source_or_dir> [more_sources...]
```

- Do not guess the report path. Read the wrapper output and use the reported output directory and report path.
- The wrapper always writes results under `reports/constant_propagation_<YYYYMMDD_HHMMSS>/` relative to the project root.
- The detector writes:
  - `trace_constant_propagation_report.json`
  - `diagnosis_bundle.json`

### 2. Read the Detector Output

- First read `diagnosis_bundle.json`.
- Then read `trace_constant_propagation_report.json`.
- Focus on:
  - `摘要`
  - `最源头常量引脚和线网`
  - `按根源分组的污染常量集合`
  - `层次化常量输出`
  - `层次化常量输入`
  - `层次化常量非端口信号`
  - `冲突和注意事项`
- Treat `trace_constant_propagation_report.json` and source code as required evidence for the final diagnosis.
- The report is a propagation-scope report, not a removed-cell diff. It can flag semantic constant pollution even when no optimized cell deletion is reported.

### 3. Decide Whether a Root Is a Real Defect or a Design-Intended Constant

- Use the rubric in `references/triage-rubric.md`.
- Prefer roots that meet several of these conditions:
  - The root is introduced in a parent or mid-level module, not a known architectural tie-off.
  - The same root pollutes multiple child modules or multiple control/data branches.
  - The polluted signals are control-path signals such as `valid`, `ready`, `enable`, `flush`, `debug`, `exception`, `predict`, `mode`, or select lines.
  - Constant pollution reaches module outputs, protocol-visible signals, state-update enables, mux selects, or logic that should normally remain data-dependent.
  - The source code around the root does not contain a clear comment or configuration reason for tying it off.
- Be conservative about declaring a real defect when the root is clearly a configuration constant, architectural constant, or protocol-required tie-off.

### 4. Final Diagnosis Is Mandatory

- Do not stop after reading the JSON report.
- The final diagnosis must combine:
  - `trace_constant_propagation_report.json`
  - source files around the root and polluted modules
  - user-provided expectations, if available
- A result is not complete until the agent checks whether the constant root and polluted signals are semantically expected in the source.
- If a root only pollutes harmless data constants or clearly documented configuration tie-offs, classify it as `设计预期常量`.

### 5. Write the Final Chinese JSON Diagnosis

Write the final diagnosis to the report directory printed by the wrapper:

```text
<REPORT_DIR>/constant_propagation_diagnosis.json
```

The final assistant response should be brief and in Chinese: state the JSON report path and the number of likely real defects. Do not duplicate the full report in prose unless the user asks.

The JSON report must contain only core content:

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
      "污染信号": ["u_child.valid_i", "u_child.enable_i"],
      "证据": {
        "报告证据": "trace_constant_propagation_report.json 中该根源的污染集合包含控制路径信号和层次化常量输出",
        "源码证据": "源码中该控制信号被上层常量连接，未看到明确配置意图"
      },
      "诊断": "上层常量连接污染子模块控制路径，导致相关逻辑语义被固定，疑似非预期常量传播。",
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
- `证据` must cite only facts from `trace_constant_propagation_report.json` and source code.
- Do not include broad RTL optimization background or generic constant-propagation explanation.
- If no likely real defect is found, write `"发现项": []` and set `疑似真实缺陷数量` to `0`.

## Guardrails

- Do not treat every constant root as a defect.
- Do not rely only on signal names. Read the source around the root and the polluted modules.
- Do not claim a parent-module named root unless the detector already promoted it or the source clearly proves it.
- Do not require optimized-cell deletion as proof of a propagation defect; this skill intentionally diagnoses constant pollution even when optimization did not remove cells.
- Do not ignore `冲突和注意事项`; conflicts should be called out as requiring manual review.

## References

- For defect-vs-expected triage, read `references/triage-rubric.md`.

## Example Requests

- "Analyze this RTL directory with top `e203_core` and tell me whether there is a real constant-propagation defect."
- "Given these Verilog files and top `FABSCALAR`, locate the earliest parent constant root and the polluted hierarchy."
- "Run constant-propagation root-cause analysis on this project and separate true defects from expected tie-offs."
