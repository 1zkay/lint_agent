# Verilog / SystemVerilog Lint 诊断

本智能体用于分析 Verilog / SystemVerilog 源码与 lint 报告，判断每条 lint 告警是否为真实缺陷，并补充漏报问题。同时，它还会在 JSON 末尾追加一个独立于 lint 报告的 IEEE标准代码诊断部分。

## 使用方法

1. 上传或提供以下文件路径：
   - 1 份 lint 报告
   - 1 个或多个 `.v` / `.vh` / `.sv` / `.svh` 源码文件
2. 直接说明你的目标，例如：
   - 分析这些 Verilog 文件和 lint 报告，判断每条告警是严重、一般还是误报，并补充漏报，输出 JSON 报告。
   - 用上传的源码和 lint 报告做逐条诊断，结果写成 JSON，并追加基于 IEEE标准 的独立代码诊断。
3. 如果不指定输出位置，生成的 JSON 报告默认写到 `reports/verilog_lint_triage_result_<YYYYMMDD_HHMMSS>.json`。
4. 生成默认文件名时，必须先执行实际命令读取当前本地时间，再拼接时间戳；不要凭记忆填写日期。
5. 写完 JSON 后，必须执行 `python skills/verilog-lint-triage/scripts/validate_triage_json.py <json_path>`，并确认校验通过。

## 输出内容

诊断结果为一个 JSON 文件，包含：

- `overall_result`：总体结论，取值为 `严重缺陷` / `一般缺陷` / `全部误报`
- `lint_items`：先按同一源码行和相同违规描述预分组，再对每个问题单元进行分析
- `missed_defects`：lint 未报出但代码中真实存在的问题
- `standard_file_diagnosis`：对源码文件本身做的 IEEE标准 独立诊断，不依赖 lint 报告

## 说明

- 智能体会自动使用内置知识库对照分析。
- 对涉及 Verilog / SystemVerilog 语言语义、`wire` / `reg`、连续赋值、过程块、阻塞/非阻塞赋值、`always_comb` / `always_ff` / `always_latch`、断言、接口或其他标准细节的问题，智能体会自动查询内置的 IEEE 标准 PDF 知识库，并给出页码依据。
- 对涉及 Xilinx / Vivado 综合行为、属性、约束、综合策略或工具限制的问题，智能体会自动查询内置的 `vivado-synthesis.pdf` 参考文档，并给出页码依据。
- 诊断文本内容为中文。
- 默认输出为 JSON，不输出 Markdown 报告。
- 默认结果目录是 `reports`，也就是当前项目下的 `D:\mcp\mcp_alint\reports`。

## 示例指令

```text
请读取我上传的 Verilog / SystemVerilog 文件和 lint 报告，先按同一源码行和相同违规描述对告警预分组，再按问题单元判断是真实缺陷、一般问题还是误报，并补充漏报；然后再对这些源码文件做一轮不依赖 lint 报告的 IEEE标准独立诊断。生成默认文件名时先执行时间命令获取当前本地时间，最后把 JSON 结果写到 reports/verilog_lint_triage_result_YYYYMMDD_HHMMSS.json，并执行 python skills/verilog-lint-triage/scripts/validate_triage_json.py <json_path> 直到通过。
```
