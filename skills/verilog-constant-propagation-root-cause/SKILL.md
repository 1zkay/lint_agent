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
  - `raw_proc.il`
  - `opt_proc.il`

### 2. Read the detector output in this order

- First read `diagnosis_bundle.json`.
- Then read `trace_removed_path_report.json`.
- Focus on:
  - Summary counts
  - Removed local cells and removed instances
  - Signals that become direct constants after optimization
  - Associated explicit roots
  - Source-level direct constant roots (`源码直接常量根源`) on each removed/constantized item
- Treat `raw_proc.il`, `opt_proc.il`, and source code as required evidence for the final diagnosis, not optional extras.
- Only open `raw_design.json` when the report is not enough to explain a root or a removed item.

### 3. Decide whether a root is a real defect or a design-intended constant

- Use the rubric in `references/triage-rubric.md`.
- Prefer roots that meet several of these conditions:
  - The root is introduced in a parent or mid-level module, not a known architectural tie-off.
  - The same root pollutes multiple child modules or multiple branches.
  - The polluted signals are control-path signals such as `valid`, `ready`, `enable`, `flush`, `debug`, `exception`, `predict`, `mode`, or select lines.
  - The root collapses logic in modules that should normally remain data-dependent.
  - The source code around the root does not contain a clear comment or configuration reason for tying it off.
- Be conservative about declaring a real defect when the root is clearly a configuration constant, architectural constant, or protocol-required tie-off.

### 4. Use before/after RTLIL only for evidence

- `raw_proc.il` is the pre-optimization structural view after `proc`.
- `opt_proc.il` is the post-optimization structural view after `proc` and `opt`.
- Use them to confirm:
  - which local cells existed before optimization,
  - which ones disappeared after optimization,
  - which output signals are rewired to direct constants after optimization,
  - and whether the removed or constantized logic matches the claimed source-level direct constant roots and polluted path.
- Do not manually diff the whole files line-by-line unless necessary. Start from the removed item path, constantized signal path, or the relevant source location from the report.

### 5. Final diagnosis is mandatory

- Do not stop after reading the JSON report.
- The final diagnosis must combine:
  - `trace_removed_path_report.json`, especially each item's `源码直接常量根源` field when present
  - `raw_proc.il`
  - `opt_proc.il`
  - source files around the root and the polluted modules
- A result is not complete until the agent checks whether the removed logic seen in RTLIL matches the root and the source-level intent.
- If the report contains no removed cells but does contain constantized signals, treat those signal entries as the structural optimization evidence.

### 6. Write the final Chinese JSON diagnosis

- Write the final diagnosis to the report directory printed by the wrapper:

```text
<REPORT_DIR>/constant_propagation_diagnosis.json
```

- The final assistant response should be brief and in Chinese: state the JSON report path and the number of likely real defects. Do not duplicate the full report in prose unless the user asks.
- The JSON report must contain only core content:

```json
{
  "summary": {
    "top_module": "top",
    "input_paths": ["rtl"],
    "artifact_dir": "reports/constant_propagation_20260428_153000",
    "real_defect_count": 1,
    "intended_constant_count": 2,
    "output_path": "reports/constant_propagation_20260428_153000/constant_propagation_diagnosis.json"
  },
  "findings": [
    {
      "id": "CP_001",
      "category": "疑似真实缺陷",
      "root_signal": "parent_cfg_force_zero",
      "root_module": "top",
      "affected_modules": ["u_child"],
      "polluted_signals": ["valid_i", "enable_i"],
      "removed_logic": ["u_child.$procmux$12"],
      "evidence": {
        "report_item": "trace_removed_path_report.json 中对应根源和删除项/常量化信号",
        "rtlil_evidence": "raw_proc.il 中存在相关逻辑，opt_proc.il 中被删除或输出被改接为常量",
        "source_evidence": "源码中该控制信号被上层常量连接，未看到明确配置意图"
      },
      "diagnosis": "上层常量连接污染子模块控制路径，导致下游逻辑被优化删除，疑似非预期常量传播。",
      "confirmation_needed": [
        "确认该控制信号是否本应固定为常量。"
      ]
    }
  ]
}
```

Field rules:

- Keep JSON keys stable in English, but write all diagnosis text in Chinese.
- `summary` must contain only top module, inputs, artifact directory, counts, and output path.
- `findings` must contain only likely real defects or clearly important intended constants.
- Use `category` values `疑似真实缺陷` or `设计预期常量`.
- `diagnosis` must be concise and evidence-based.
- `evidence` must cite only facts from `trace_removed_path_report.json`, `raw_proc.il`, `opt_proc.il`, and source code.
- Do not include broad RTL optimization background or generic constant-propagation explanation.
- If no likely real defect is found, write `"findings": []` and set `real_defect_count` to `0`.

## Guardrails

- Do not treat every constant root as a defect.
- Do not rely only on signal names. Read the source around the root and the polluted modules.
- Do not claim a parent-module named root unless the detector already promoted it or the source clearly proves it.
- Do not ignore removed-item or constantized-signal evidence; the detector is intentionally filtered to constants that already caused structural optimization.
- Do not spend time manually reading the full `raw_design.json` unless the report and source are insufficient.

## References

- For defect-vs-expected triage, read `references/triage-rubric.md`.

## Example Requests

- "Analyze this RTL directory with top `e203_core` and tell me whether there is a real constant-propagation defect."
- "Given these Verilog files and top `FABSCALAR`, locate the earliest parent constant root and the polluted hierarchy."
- "Run constant-propagation root-cause analysis on this project and separate true defects from expected tie-offs."
