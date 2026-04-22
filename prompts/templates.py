"""
MCP Prompt 模板定义

将原 mcp_lint.py 中硬编码的 prompt 字符串统一管理。
每个函数返回 FastMCP 标准 PromptMessage 列表。
"""

from fastmcp.prompts import Message
from mcp.types import PromptMessage, ResourceLink


# ─── 资源 URI 常量 ────────────────────────────────────────────────────────────
_RESOURCE_ITEMS = [
    ("sources", "alint://basic/sources"),
    ("violations", "alint://basic/violations"),
    ("ast", "alint://basic/ast"),
    ("cfg_ddg_dfg", "alint://basic/cfg_ddg_dfg"),
    ("kb", "alint://basic/kb"),
]

# ─── JSON 分析 Prompt ─────────────────────────────────────────────────────────
BASIC_HARDWARE_ANALYSIS_TEXT = """\
你是一位资深硬件设计与代码审查专家。请严格基于以下五个资源进行【综合硬件代码质量检查】，\
并输出**唯一且合法的JSON**（不要输出Markdown、不要输出多余解释文字）。

可用资源（必须全部使用并交叉验证）：
1) 项目源代码：alint://basic/sources（Verilog/SystemVerilog完整上下文）
2) ALINT违规报告：alint://basic/violations（CSV）
3) AST抽象语法树：alint://basic/ast（JSON）
4) CFG/DDG/DFG：alint://basic/cfg_ddg_dfg（JSON，控制流/数据依赖/数据流）
5) Verilog知识库：alint://basic/kb（JSONL，设计规范和最佳实践）

重要前提（必须在总览中体现）：
- ALINT违规报告可能存在**误报**与**漏报**，只能作为参考线索；你必须结合源码、AST、CFG/DDG/DFG以及知识库进行独立综合判断与补充发现。

====================
分析要求
====================
1) 先从AST理解模块层次/接口/时序与组合结构/FSM/关键数据通路，避免断章取义。
2) 结合知识库理解设计规范、最佳实践和常见陷阱。
3) 再结合ALINT报告逐条核验：确认/驳回/不确定，并补充lint未报但高风险的问题。
4) 对每个最终输出的缺陷，必须结合CFG/DFG/DDG做因果推断：说明在哪些控制路径触发、数据如何传播导致问题。
5) 引用知识库中的相关规则ID和描述来支持你的分析。

====================
严重性定义（仅三档）
====================
- "严重"：高概率导致功能错误/综合仿真不一致/CDC风险/时序或数据丢失/锁存器或不可综合等。
- "一般"：低到中等风险，主要影响可维护性/可读性/可验证性/潜在边界条件。
- "误报"：经综合检查后认为lint提示不成立或风险可忽略，但必须给出为何误报的依据。

====================
输出格式
====================
你必须输出且只输出一个JSON对象，满足以下结构：

{
  "总览": "...",
  "缺陷列表": [
    {
      "编号": "001",
      "严重性": "严重",
      "问题": "...",
      "来源": "lint",
      "规则ID": "...",
      "描述": "...",
      "知识库参考": "...",
      "缺陷分析": "..."
    }
  ]
}

字段约束：
- "严重性" 只能是："严重" | "一般" | "误报"
- "来源" 只能是："lint" | "专家补充" | "lint+专家"
- 若"来源"包含lint，则"规则ID"与"描述"必须为字符串；否则两者必须为null
- 每个缺陷的"缺陷分析"必须体现你使用了CFG/DFG/DDG中的至少一种证据

现在开始分析并输出JSON。"""

# ─── Markdown 分析报告 Prompt ─────────────────────────────────────────────────
STRUCTURED_REPORT_TEXT = (
    "你是一位资深硬件设计与代码审查专家。请严格基于以下五个资源进行**综合硬件代码质量检查**，"
    "并按照指定的Markdown格式输出结构化分析报告。\n\n"
    "## 可用资源（必须全部使用并交叉验证）\n\n"
    "1. **项目源代码**：alint://basic/sources\n"
    "2. **ALINT违规报告**：alint://basic/violations（CSV）\n"
    "3. **AST抽象语法树**：alint://basic/ast（JSON）\n"
    "4. **CFG/DDG/DFG**：alint://basic/cfg_ddg_dfg（JSON）\n"
    "5. **Verilog知识库**：alint://basic/kb（JSONL）\n\n"
    "## 重要前提\n\n"
    "- ALINT违规报告可能存在**误报**与**漏报**，只能作为参考线索\n"
    "- 必须结合源码、AST、CFG/DDG/DFG以及知识库进行独立综合判断\n\n"
    "## 输出格式（Markdown，必须包含以下5章）\n\n"
    "1. 静态分析结果汇总（表格 + 分布统计 + 源码片段）\n"
    "2. 语义理解分析（代码意图、实际实现、AST/DFG要点）\n"
    "3. 根源分析（直接原因 + 级联影响 + 错误类型分类）\n"
    "4. 修复建议（立即修复 + 优化方案 + 验证方法）\n"
    "5. 总结（问题本质 + 关键发现表格 + 修复前后对比）\n\n"
    "严重性定义：**严重** | **一般** | **误报**\n\n"
    "现在开始分析并生成结构化的Markdown报告。"
)


def _resource_messages() -> list[PromptMessage]:
    """构造标准 MCP ResourceLink PromptMessage 列表。"""
    messages: list[PromptMessage] = []
    for name, uri in _RESOURCE_ITEMS:
        messages.append(
            Message(
                ResourceLink(
                    type="resource_link",
                    name=name,
                    uri=uri,
                )
            )
        )
    return messages


def get_basic_hardware_analysis_messages() -> list[PromptMessage]:
    """返回 basic_hardware_analysis prompt 的标准消息列表。"""
    return [
        Message(BASIC_HARDWARE_ANALYSIS_TEXT),
        *_resource_messages(),
    ]


def get_structured_report_messages() -> list[PromptMessage]:
    """返回 structured_hardware_analysis_report prompt 的标准消息列表。"""
    return [
        Message(STRUCTURED_REPORT_TEXT),
        *_resource_messages(),
    ]
