# Output Schema

Use this JSON structure for the final file.

```json
{
  "overall_result": "严重缺陷",
  "summary": {
    "knowledge_base": "自建知识库",
    "report_path": "reports/alint.csv",
    "source_files": [
      "rtl/top.sv"
    ],
    "output_path": "reports/verilog_lint_triage_result_20260321_153000.json",
    "summary_text": "本次诊断识别出1条严重真实缺陷、1条一般真实缺陷、1条误报，以及1条漏报缺陷。"
  },
  "lint_items": [
    {
      "report_line_numbers": [
        2,
        3
      ],
      "raw_report_lines": [
        "rtl/top.sv,42,NBSEQ,warning,Blocking assignment in sequential logic",
        "rtl/top.sv,42,SEQCHK,warning,Blocking assignment in sequential logic"
      ],
      "report_rule_id": [
        "NBSEQ",
        "SEQCHK"
      ],
      "report_severity": [
        "warning",
        "warning"
      ],
      "file": "rtl/top.sv",
      "code_line": 42,
      "category": "严重",
      "issue": "时序 always 块中使用了阻塞赋值。",
      "kb_rules": [
        {
          "id": "R-9-4",
          "severity_default": "Critical",
          "description": "Sequential circuits must use non-blocking assignments (<=); blocking assignments (=) are prohibited."
        }
      ],
      "why": "该 always 块由时钟边沿触发，阻塞赋值会改变时序逻辑语义，因此这不是风格问题，而是真实缺陷。",
      "evidence": "always @(posedge clk) begin q = d; end",
      "fix_hint": "将该时序块中的 '=' 改为 '<='。"
    }
  ],
  "missed_defects": [
    {
      "id": "MISSED_001",
      "file": "rtl/top.sv",
      "code_line": 55,
      "category": "一般",
      "issue": "组合逻辑 always 块遗漏了一个敏感信号。",
      "kb_rules": [
        {
          "id": "R-12-9",
          "severity_default": "Critical",
          "description": "Sensitive signals in the sensitivity list must be complete, with no omissions or redundancies."
        }
      ],
      "why_missed_by_lint": "lint 报告没有覆盖这个 always 块，但源码中确实读取了未出现在敏感列表中的信号。",
      "evidence": "always @(a or b) y = a & b & c;",
      "fix_hint": "补齐敏感列表，或改写为 always @* / always_comb。"
    }
  ],
  "standard_file_diagnosis": [
    {
      "file": "rtl/top.sv",
      "summary_text": "基于适用的 IEEE 标准和/或 Vivado 综合文档对源码做独立诊断，识别出2条严重问题和1条提示项，其中同时包含 IEEE 与 Vivado 依据。",
      "findings": [
        {
          "id": "STD_001",
          "code_line": 42,
          "category": "严重",
          "issue": "时序过程块中使用阻塞赋值，不符合 IEEE 1800-2017 对时序建模的推荐语义。",
          "standard_pages": "IEEE 1800-2017 p.316-317",
          "why": "该语句位于时钟边沿触发的 always 块中，阻塞赋值可能引入仿真与综合语义偏差，属于需要立即修复的问题。",
          "evidence": "always @(posedge clk) begin q = d; end",
          "fix_hint": "将阻塞赋值改为非阻塞赋值，并复核相关寄存器更新顺序。"
        },
        {
          "id": "STD_002",
          "code_line": 55,
          "category": "提示",
          "issue": "建议将组合逻辑过程块改写为 always_comb，以便在语言语义和综合意图上更清晰。",
          "standard_pages": "IEEE 1800-2017 p.324-326",
          "why": "源码当前使用传统 always 形式，虽然可以工作，但 always_comb 更能约束敏感列表完整性并明确组合逻辑意图。",
          "evidence": "always @(a or b) y = a & b & c;",
          "fix_hint": "将该组合过程块改写为 always_comb，并在修改后重新检查组合逻辑完整性。"
        },
        {
          "id": "STD_003",
          "code_line": 78,
          "category": "严重",
          "issue": "组合逻辑生成的 gated_clk 被当作时钟使用，不符合 Vivado 综合对时钟资源使用的建议。",
          "standard_pages": "Vivado Synthesis Guide p.55",
          "why": "在 FPGA 设计中，时钟应由专用全局时钟资源驱动。将 clk 与 en 的组合逻辑结果直接作为时钟，容易引入毛刺并带来时序收敛风险。",
          "evidence": "assign gated_clk = clk & en;\\nalways @(posedge gated_clk) begin q <= d; end",
          "fix_hint": "保持 clk 为全局时钟，将 en 改为寄存器使能条件，避免使用组合逻辑生成的 gated clock。"
        }
      ]
    }
  ]
}
```

## Field Rules

- `overall_result` must be exactly one of:
  - `严重缺陷`
  - `一般缺陷`
  - `全部误报`
- Keep JSON field names in English for schema stability, but all diagnostic prose values must be in Chinese.
- Derive `overall_result` as follows:
  - If any entry in `lint_items`, `missed_defects`, or `standard_file_diagnosis[].findings` is `严重`, use `严重缺陷`.
  - Else if any entry in `lint_items`, `missed_defects`, or `standard_file_diagnosis[].findings` is `一般`, use `一般缺陷`.
  - Else use `全部误报`.
- `lint_items` are pre-grouped issue entries, not a direct 1:1 mapping of report rows.
- Pre-grouping rule for `lint_items`:
  - First group lint rows that point to the same source code line and describe the same defect, then analyze that grouped issue as one entry.
  - If the source code line is the same but the defect description is different, keep separate grouped entries and analyze them separately.
- `report_line_numbers` must be a non-empty list of 1-based physical line numbers from the original report file.
- `raw_report_lines` must preserve the original lint rows included in the merged entry.
- `report_rule_id` must be a non-empty list of all lint rule IDs merged into the entry.
- `report_severity` must be a non-empty list of the corresponding lint severities.
- `report_line_numbers`, `raw_report_lines`, `report_rule_id`, and `report_severity` must have the same length and preserve the same ordering.
- `summary.knowledge_base` should be a user-facing label such as `自建知识库`, not a filesystem path or underlying filename.
- `summary.output_path` should default to `reports/verilog_lint_triage_result_<YYYYMMDD_HHMMSS>.json` unless the user explicitly provided another output path.
- `category` in `lint_items` must be exactly one of:
  - `严重`
  - `一般`
  - `误报`
- `category` in `missed_defects` must be exactly one of:
  - `严重`
  - `一般`
- `standard_file_diagnosis` must be a list with one entry per source file.
- Each `standard_file_diagnosis` entry must contain:
  - `file`
  - `summary_text`
  - `findings`
- Each `standard_file_diagnosis[].findings[]` item must contain:
  - `id`
  - `code_line`
  - `category`
  - `issue`
  - `standard_pages`
  - `why`
  - `evidence`
  - `fix_hint`
- `missed_defects[].id` should use a `MISSED_001`-style identifier.
- `standard_file_diagnosis[].findings[].id` should use a `STD_001`-style identifier. Do not use an `I` prefix.
- `standard_pages` stores page citations from the applicable built-in reference source. Keep the field name stable even when the citation comes from the Vivado synthesis document rather than IEEE.
- For language-semantics findings, cite IEEE page numbers in `standard_pages`. For synthesis/tool-behavior findings, cite the built-in Vivado synthesis document page numbers. If both materially support the diagnosis, include both explicitly.
- `category` in `standard_file_diagnosis[].findings` must be exactly one of:
  - `严重`
  - `一般`
  - `提示`
- `standard_file_diagnosis` is code-only diagnosis. Do not use lint rows as evidence in that section. Use source code plus the applicable IEEE and/or Vivado built-in reference evidence only.
- The following text fields must be written in Chinese:
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
- `kb_rules` may be empty only when the code or report is too incomplete to map reliably. If empty, explain why in `why`.

## Minimal Review Checklist

- Every data-bearing lint line appears in exactly one merged `lint_items` entry.
- Every real defect points to code evidence.
- Every real defect cites one or more knowledge-base rules where possible.
- Missed defects are not duplicates of existing lint items.
- Every source file has one `standard_file_diagnosis` entry.
- Every standards/tool-reference diagnosis finding includes explicit page references to the supporting IEEE and/or Vivado source.
- The file is valid JSON and not wrapped in Markdown.
