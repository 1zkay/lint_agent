# mcp_alint 项目文档

## 1. 项目概述

### 1.1 项目名称
**ALINT-PRO Verilog 智能分析系统**（mcp_alint）

### 1.2 项目定位
基于 LangChain、LangGraph、MCP（Model Context Protocol）与 Agentic RAG 的 Verilog/SystemVerilog 硬件代码智能审查系统。

### 1.3 核心目标
- 为硬件代码审查提供统一的自然语言交互入口
- 将 lint 结果、源码、结构化分析结果和标准知识检索纳入同一分析闭环
- 为复杂问题提供可引用、可验证、可复盘的分析依据
- 形成支持持久化、人机协同控制能力的工程化智能分析系统

### 1.4 技术栈
| 类别 | 技术 |
|------|------|
| 智能体框架 | LangChain Agents (`create_agent`)、LangGraph (`StateGraph`) |
| 协议层 | MCP (Model Context Protocol) via `FastMCP` + `langchain-mcp-adapters` |
| 前端交互 | Chainlit（对话式 UI） |
| 静态分析 | ALINT-PRO（Aldec 硬件 lint 工具） |
| 结构分析 | Yosys（通过 OSS CAD Suite） |
| 向量检索 | FAISS + OpenAI 兼容 Embeddings |
| 持久化 | PostgreSQL (`AsyncPostgresSaver`) + S3 兼容对象存储 (MinIO) |
| LLM 接入 | OpenRouter 兼容接口，支持多模型切换 |

---

## 2. 系统架构

### 2.1 三层架构

```
┌─────────────────────────────────────────────────────────────────┐
│                     表示层 (Presentation Layer)                  │
│  Chainlit 对话 UI │ 文件上传 │ 任务面板 │ 审批交互 │ 历史恢复      │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│                   业务逻辑层 (Business Logic Layer)               │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              主控智能体 (create_agent)                     │   │
│  │                                                          │   │
│  │  中间件栈（从外到内，共 8 层）：                            │   │
│  │  1. TodoListMiddleware      — Plan-and-Execute 规划       │   │
│  │  2. SkillsMiddleware        — 领域知识注入                │   │
│  │  3. SummarizationMiddleware — 长对话摘要压缩              │   │
│  │  4. ReflectionMiddleware    — Evaluator-Optimizer 反思    │   │
│  │  5. ModelRetryMiddleware    — LLM 调用自动重试            │   │
│  │  6. ToolRetryMiddleware     — 工具调用自动重试            │   │
│  │  7. ShellToolMiddleware     — 持久化 Shell 会话           │   │
│  │  8. HumanInTheLoopMiddleware— 高风险工具审批              │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│              数据访问与知识服务层 (Data & Knowledge Layer)        │
│                                                                  │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐  │
│  │ 静态分析    │ │ 结构理解    │ │ 标准知识    │ │ 持久化      │  │
│  │ ALINT-PRO  │ │ Yosys/AST  │ │ Agentic RAG│ │ Postgres   │  │
│  │ CSV 报告   │ │ AST/CFG/DDG│ │ IEEE PDF   │ │ + S3/MinIO │  │
│  └────────────┘ └────────────┘ └────────────┘ └────────────┘  │
│                                                                  │
│  MCP Server (FastMCP) 统一暴露：                                 │
│  @mcp.tool  │  @mcp.resource  │  @mcp.prompt                    │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 核心设计原则
1. **智能体与确定性工作流协同**：主控智能体负责任务协调，确定性工作流（StateGraph）负责分析步骤
2. **双知识源融合**：工程规则知识（lint 报告 + 知识库）与参考文档知识（IEEE 标准 PDF + Vivado 综合指南 PDF）分别处理、协同使用
3. **协议化解耦**：通过 MCP 协议向上暴露能力，上层智能体无需关心底层工具链实现细节

---

## 3. 智能体组成

### 3.1 主控智能体 (Main Agent)

**入口**: `chat_app.py` → `create_agent()`

**职责**:
- 理解用户意图（自然语言问答）
- 组织工具调用顺序
- 协调不同能力单元
- 生成最终回答

**装配的工具集**:
| 工具类别 | 工具来源 | 说明 |
|----------|----------|------|
| MCP 工具 | `mcp_lint.py` (FastMCP Server) | 静态分析、结构分析、资源读取 |
| 参考文档 RAG 工具 | `agentic_rag.py` | 多 PDF 参考文档检索问答 |
| 文件工具 | `FilesystemMiddleware` | `ls` / `read_file` / `write_file` / `edit_file` / `glob` / `grep` |
| 搜索工具 | `TavilySearch` | 联网检索（可选） |
| URL 抓取 | `RequestsGetTool` | 网页内容获取 |

### 3.2 中间件栈 (Middleware Stack)

采用**洋葱模型**执行原则：外层先进入 `before_model`，后退出 `after_model`。

| 层级 | 中间件 | 功能 | 配置开关 |
|------|--------|------|----------|
| 1 (最外层) | `TodoListMiddleware` | Plan-and-Execute 规划，实时 UI 进度同步 | `agent_enable_todo` |
| 2 | `SkillsMiddleware` | 领域知识注入（SKILL.md 按需加载） | `agent_enable_skills` |
| 3 | `SummarizationMiddleware` | 长对话自动摘要压缩 | `agent_enable_summarization` |
| 4 | `ReflectionMiddleware` | Evaluator-Optimizer 反思循环 | `agent_enable_reflection` |
| 5 | `ModelRetryMiddleware` | LLM 调用失败自动重试（指数退避） | `agent_enable_model_retry` |
| 6 | `ToolRetryMiddleware` | 工具调用失败自动重试（指数退避） | `agent_enable_tool_retry` |
| 7 | `ShellToolMiddleware` | 持久化 Shell 会话（Git Bash） | `agent_enable_shell` |
| 8 (最内层) | `HumanInTheLoopMiddleware` | 高风险工具审批门控 | `agent_tool_approval_enabled` |

### 3.3 确定性分析工作流 (LangGraph StateGraph)

**入口**: `agent/graph.py` → `build_workflow()`

**状态定义**: `agent/state.py` → `AlintWorkflowState` (TypedDict)

**节点图**:
```
START
  │
  ▼
[lint_node] ── error ──► END
  │
  ▼
[structure_node] ── error ──► END
  │
  ▼
[sources_node] ── error ──► END
  │
  ▼
[organize_node]
  │
  ▼
END
```

#### 节点详情

| 节点 | 文件 | 功能 | 输入 | 输出 |
|------|------|------|------|------|
| `lint_node` | `agent/nodes/lint_node.py` | 运行 ALINT-PRO 批处理，生成 CSV 违规报告 | `workspace_path`, `project_name` | `raw_csv_path`, `alint_output_dir` |
| `structure_node` | `agent/nodes/structure_node.py` | 用 Yosys 生成 AST + CFG/DDG/DFG | `workspace_path`, `project_name` | `ast_json_file`, `cfg_ddg_file`, `rtlil_file`, `v_files` |
| `sources_node` | `agent/nodes/sources_node.py` | 提取 Verilog 源代码，生成带行号文本 | `v_files`, `project_dir` | `source_code_text` |
| `organize_node` | `agent/nodes/organize_node.py` | 整理所有产物到 `_prepared/<session_id>/` | 所有中间产物 | `prepared_directory`, 各产物路径 |

**条件边**: 每个节点写入 `error` 字段时，条件边路由到 `END`，实现短路退出。

### 3.4 技能模块 (Skills)

**位置**: `skills/verilog-lint-triage/`

**技能名称**: `verilog-lint-triage`

**功能**: Verilog Lint 结果分类处理
1. 读取自建知识库
2. 按语义主题批量查询 IEEE 标准（而非逐行查询）
3. 对每条 lint 发现进行预分组（同源文件行 + 同违规描述）
4. 分类判定：`严重` / `一般` / `误报`
5. 补充遗漏缺陷发现
6. 独立 IEEE 标准代码诊断（不依赖 lint 报告）
7. 输出结构化 JSON 结果文件

---

## 4. 核心模块详解

### 4.1 MCP Server (`mcp_lint.py`)

基于 `FastMCP` 构建，提供三类能力暴露形式：

#### Resources (只读资源)
| URI | 说明 |
|-----|------|
| `alint://basic/sources` | 项目源代码（带行号） |
| `alint://basic/violations` | ALINT 违规报告 (CSV) |
| `alint://basic/ast` | AST 抽象语法树 (JSON) |
| `alint://basic/cfg_ddg_dfg` | 控制流/数据依赖/数据流 (JSON) |
| `alint://basic/kb` | Verilog 知识库 (JSONL) |

#### Tools (可执行工具)
- `run_basic_analysis` — 执行完整分析工作流
- `get_structured_report` — 生成结构化分析报告
- 其他分析相关工具

#### Prompts (提示模板)
- 由 `prompts/templates.py` 统一管理
- `get_basic_hardware_analysis_messages` — JSON 分析 Prompt
- `get_structured_report_messages` — Markdown 分析报告 Prompt

### 4.2 Agentic RAG (`agentic_rag.py`)

**实现方式**: 参照 LangGraph 官方 agentic RAG 教程

**流程**:
```
generate_query_or_respond → retrieve → grade → rewrite → generate
```

**技术组件**:
| 组件 | 技术 |
|------|------|
| 文档加载 | `PyPDFLoader`（逐页模式，保留页码元数据） |
| 文本分块 | `RecursiveCharacterTextSplitter` |
| 向量索引 | `FAISS` + `OpenAIEmbeddings` |
| 图构建 | `StateGraph` + `ToolNode` + `tools_condition` |
| 相关性评分 | `BaseModel` + `with_structured_output`（结构化输出约束） |
| 索引缓存 | 基于 PDF 元数据（大小、修改时间、模型）判断 freshness |

**对外接口**: `build_hardware_reference_agentic_rag_tool()` → LangChain Tool

### 4.3 结构分析 (`AST.py`)

基于 **Yosys**（通过 OSS CAD Suite）实现 Verilog/SystemVerilog 解析：

| 功能 | 说明 |
|------|------|
| AST 生成 | `parse_target()` → 简化 AST JSON |
| RTLIL 中间表示 | `run_yosys_for_rtlil_processes()` |
| CFG/DDG 构建 | `build_cfg_ddg_from_rtlil_processes()` |
| 门级网表导出 | `run_yosys_for_netlist()` |
| 包含目录推断 | `infer_incdirs()` |

### 4.4 配置管理 (`config.py`)

**唯一配置入口**，其他模块一律通过 `from config import config` 访问。

**配置优先级**: 类内默认值 → `.env` 文件 → 系统环境变量

**配置覆盖范围**:
- ALINT 基础路径
- LLM 接入（provider:model、temperature、多模型预设）
- Agent 中间件（Todo / Summarization / Reflection / Retry / HITL）
- LangGraph Checkpointer（PostgreSQL / Memory）
- Chainlit 认证与 Data Layer
- S3 兼容对象存储（附件上传）
- OpenRouter 兼容 HTTP 头
- 参考文档 RAG（多 PDF 路径、分块参数、Embedding 模型）

### 4.5 LLM 工厂 (`llm_factory.py`)

- 统一构建 LangChain chat model
- 支持 `provider:model` 格式（如 `openai:gpt-4o`）
- 自动检测 OpenRouter 端点并注入必需 HTTP 头
- 支持非 OpenAI 端点的 tiktoken 禁用和长度安全分片关闭

---

## 5. 工程运行机制

### 5.1 会话管理
| 机制 | 实现 |
|------|------|
| 会话初始化 | `on_chat_start` → `_initialize_chat_runtime()` |
| 会话恢复 | `on_chat_resume` → 同一初始化链路 |
| 线程 ID | Chainlit thread_id ↔ LangGraph thread_id |
| 模型切换 | `on_settings_update` → 重建运行时 |
| 会话关闭 | `on_chat_end` → 关闭 MCP stdio 子进程 |

### 5.2 持久化
| 数据类型 | 存储方式 |
|----------|----------|
| 对话历史 | PostgreSQL (`AsyncPostgresSaver`) |
| 分析产物 | 文件系统 (`_prepared/<session_id>/`) |
| 用户上传附件 | S3 兼容对象存储 (MinIO/LocalStack) |
| 技能反馈 | JSON 文件 (`feedback/`) |

### 5.3 流式输出
- 使用 `agent.astream(stream_mode=["messages", "updates"], version="v2")`
- `messages` 模式：LLM 逐 token 输出
- `updates` 模式：节点完成后的完整状态更新
- 通过 Chainlit `cl.Step` 和 `cl.Message` 实时展示执行过程

### 5.4 人机协同 (HITL)
- 高风险工具调用触发审批中断
- 通过 Chainlit `AskActionMessage` 收集用户决策
- 使用 `Command(resume=...)` 恢复执行
- 支持 approve/reject 决策

### 5.5 附件处理
- 文本文件：内联注入上下文（含文件头元信息）
- 图片：Base64 内联
- 二进制文件：提供绝对路径索引，由工具链继续处理
- MIME 类型解析：优先使用 Chainlit 提供值，否则按扩展名猜测

### 5.6 兼容性补丁
- **Windows asyncio 兼容**: 使用 `WindowsSelectorEventLoopPolicy`
- **LangGraph Send 序列化**: Monkey-patch `sanitize_untracked_values_in_send` 为递归版本（等效 langgraph#6794）
- **Chainlit 数据层**: 自定义 `AppChainlitDataLayer` 处理时间戳格式差异

---

## 6. 项目文件结构

```
mcp_alint/
├── chat_app.py                  # Chainlit 主应用（主控智能体 + 中间件 + 流式输出）
├── config.py                    # 全局配置（唯一入口）
├── mcp_lint.py                  # MCP Server（FastMCP：Resources + Tools + Prompts）
├── agentic_rag.py               # IEEE 标准 Agentic RAG 模块
├── llm_factory.py               # LLM 模型工厂
├── reflection_middleware.py     # 反思中间件（Evaluator-Optimizer）
├── utils.py                     # 共享工具函数
├── AST.py                       # Yosys 结构分析后端
├── agent/                       # LangGraph 工作流
│   ├── __init__.py
│   ├── state.py                 # 工作流状态定义 (TypedDict)
│   ├── graph.py                 # StateGraph 构建与编译
│   └── nodes/                   # 工作流节点
│       ├── lint_node.py         # ALINT-PRO 批处理
│       ├── structure_node.py    # Yosys AST/CFG/DDG 生成
│       ├── sources_node.py      # 源代码提取
│       └── organize_node.py     # 产物整理归档
├── prompts/
│   ├── __init__.py
│   └── templates.py             # MCP Prompt 模板定义
├── skills/
│   └── verilog-lint-triage/     # Lint 分类处理技能
│       └── SKILL.md
├── scripts/
│   ├── build_ieee_standard_rag_index.py  # 参考文档 RAG 索引构建脚本（兼容旧文件名）
│   ├── init_postgres_windows.ps1         # PostgreSQL 初始化
│   └── init_services_ubuntu.sh           # Ubuntu 服务初始化
├── start_chainlit_mcp.cmd       # Windows 启动脚本
├── .env                         # 环境配置
├── project_design_overview_cn.md # 项目设计概述（中文）
└── chainlit.md                  # Chainlit 欢迎页面
```

---

## 7. 启动与运行

### 7.1 启动方式
```bash
chainlit run chat_app.py -w
```
或使用 Windows 批处理脚本：
```cmd
start_chainlit_mcp.cmd
```

### 7.2 环境要求
- **操作系统**: Windows（ALINT-PRO 仅支持 Windows）
- **Python**: 3.12+
- **ALINT-PRO**: Aldec 硬件 lint 工具（需配置 `ALINT_EXE`）
- **OSS CAD Suite**: Yosys 结构分析（默认路径 `oss-cad-suite/`）
- **PostgreSQL**: 会话持久化（可选，回退到 InMemorySaver）
- **MinIO**: 附件对象存储（可选，自动启动本地实例）
- **推荐环境文件**: `environment.mcp-alint.yml`（已固定一组经过验证的 Conda + pip 版本）

### 7.3 关键环境变量
| 变量 | 说明 |
|------|------|
| `LLM_MODEL` | LLM 模型（支持 `provider:model` 格式） |
| `LLM_BASE_URL` | LLM API 端点 |
| `LLM_API_KEY` | LLM API 密钥 |
| `ALINT_EXE` | ALINT-PRO 可执行文件路径 |
| `ALINT_PRO_ROOT` | ALINT 工作区根目录 |
| `CHECKPOINTER_DB_URI` | PostgreSQL 连接串 |
| `CHAINLIT_ENABLE_PASSWORD_AUTH` | 启用认证 |
| `RAG_PDF_PATHS` | 参考 PDF 路径列表（单个 PDF 也使用同一格式） |

---

## 8. 技术创新点

1. **智能体与确定性工作流协同**: 主控智能体负责工具选择与上下文协调，确定性工作流以 StateGraph 为基础组织分析阶段
2. **标准知识检索增强**: 围绕 IEEE 标准文档构建 Agentic RAG 检索链路，支持问题改写、相关性评分与页码引用
3. **多源知识融合**: 工程规则知识与语言标准知识分别处理、协同使用
4. **工程运行机制完善**: 任务列表同步、工具失败自动重试、长对话摘要压缩、高风险操作审批、历史会话持久化

---

## 9. 未来工作方向

1. **多角色协同智能体**: 拆分为规则判责、标准核证、修复建议与结果审校等角色
2. **知识体系扩展**: 将语言标准、设计规范、项目历史缺陷和企业内部经验规则纳入统一框架
3. **自动化评测体系**: 建立问题识别准确率、条文引用准确率和修复建议质量的离线评估机制
4. **离线部署与本地模型适配**: 满足内网运行和资源可控性要求
