# ALINT-PRO Verilog Lint EDA Diagnostic Agent

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/1zkay/lint_agent)

`mcp_alint` is a Verilog/SystemVerilog lint diagnosis agent built around
LangChain, LangGraph, MCP, Chainlit, ALINT-PRO, Yosys, and Agentic RAG.

The project turns commercial lint output, HDL source code, structural analysis
artifacts, self-built Verilog rule knowledge, and reference-document retrieval
into one interactive diagnosis workflow.

The core Chainlit agent can also run without ALINT-PRO, Yosys, or other EDA
tools. In that mode it works as a general-purpose LLM agent with chat, memory,
RAG, skills, and approval workflows; only EDA-specific analysis tools are
unavailable or return graceful failure messages.

## What This Project Does

- Provides a Chainlit chat UI for Verilog lint diagnosis.
- Uses LangChain `create_agent` as the main agent runtime.
- Uses MCP to expose ALINT-PRO, Yosys, resources, and prompts as standard tools.
- Uses LangGraph `StateGraph` for deterministic EDA preprocessing workflows.
- Uses ALINT-PRO to generate lint violation CSV reports.
- Uses Yosys / OSS CAD Suite to generate AST, RTLIL, CFG, DDG, DFG, and netlist artifacts.
- Uses Agentic RAG over hardware reference PDFs such as IEEE and Vivado documents.
- Supports long-term memory, task planning, human-in-the-loop tool approval, and skill-based diagnosis workflows.

## Architecture

```text
Chainlit Web UI
  |
  v
LangChain create_agent
  |-- Middleware: todo, filesystem, summarization, retry, HITL, skills, reflection
  |-- Memory tools
  |-- Agentic RAG tool
  |
  v
MCP Client over stdio
  |
  v
FastMCP Server: python -m mcp_server.server
  |-- MCP tools
  |-- MCP resources
  |-- MCP prompts
  |
  v
LangGraph ALINT workflow
  |-- lint_node       -> ALINT-PRO CSV report
  |-- structure_node  -> Yosys AST / RTLIL / CFG / DDG / DFG
  |-- sources_node    -> source code with line numbers
  |-- organize_node   -> reports/_prepared/<session_id>/
```

## Main Entry Points

| Entry | File | Purpose |
| --- | --- | --- |
| Chainlit app | `chat_app.py` | Chainlit compatibility entrypoint and message streaming handler |
| MCP server | `mcp_server/server.py` | FastMCP server assembly |
| MCP implementation | `mcp_server/` | Exposes EDA tools, resources, and prompts |
| LangGraph workflow | `alint_workflow/graph.py` | Deterministic ALINT analysis pipeline |
| Yosys backend | `eda/ast.py` | AST, RTLIL, CFG/DDG/DFG, and netlist generation |
| Agentic RAG | `rag/hardware_reference.py` | Reference-document retrieval and answer generation |
| Long-term memory | `memory/long_term.py` | User profile and durable memory tools |
| LangGraph Agent Server | `langgraph_server/agent_runtime.py` | Alternative HTTP/CLI agent runtime |

## Requirements

Recommended environment:

- Python 3.12.
- Conda or another virtual environment manager.
- LLM API credentials for a LangChain-compatible chat model.

Required only for EDA diagnosis workflows:

- Windows, because ALINT-PRO is invoked through a Windows executable.
- ALINT-PRO installed and licensed.
- OSS CAD Suite / Yosys available and configured through `OSS_CAD_SUITE_ROOT`.

Optional components:

- PostgreSQL for LangGraph checkpointer, memory store, and Chainlit data layer.
- MinIO or another S3-compatible storage service for Chainlit file uploads.
- Node.js/npm for running the Chainlit Data Layer Prisma migration.
- Visual Studio C++ build tools for building pgvector on Windows when it is not already installed in PostgreSQL.
- Tavily API key for web search.

## Installation

From the project directory:

```powershell
cd <project-root>
```

Create the Conda environment from the provided file:

```powershell
conda env create -f environment.mcp-alint.yml
conda activate mcp-alint
```

The existing Windows helper scripts in this repository currently activate a
Conda environment named `mcp`. If your environment name is different, either
activate it manually before running commands or update the `.cmd` files.

For editable Python package usage:

```powershell
pip install -e .
```

Use `environment.mcp-alint.yml` for the full Chainlit runtime because it also
includes UI-related dependencies.

## Prebuilt Docker Images

Prebuilt customer Docker images are available from Baidu Netdisk:

```text
Link: https://pan.baidu.com/s/1t2fzdzzrG1Y0__Bs1svInQ?pwd=uuam
Extraction code: uuam
```

These images can run the packaged Chainlit agent without requiring local Python
dependency installation. ALINT-PRO/Yosys are still optional from the perspective
of general chat usage; EDA-specific analysis requires the corresponding tools
and project inputs to be available.

## Configuration

Create a `.env` file from the template:

```powershell
Copy-Item .env.example .env
```

Use `.env.example` as the source of truth for variable names and inline comments.
The `.env` file is ignored by Git and should not be committed.

## Windows Service Initialization

本项目的持久化初始化分为四类：

| 用途 | 配置项 | 初始化入口 | 表结构 |
| --- | --- | --- | --- |
| LangGraph checkpointer | `CHECKPOINTER_BACKEND`, `CHECKPOINTER_DB_URI`, `CHECKPOINTER_AUTO_SETUP` | `agent_runtime/checkpointer.py::build_checkpointer()` 创建 `AsyncPostgresSaver`，启动时调用 `checkpointer.setup()` | `checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` |
| Long-term memory store | `MEMORY_STORE_BACKEND`, `MEMORY_STORE_DB_URI`, `MEMORY_STORE_AUTO_SETUP`, `MEMORY_ENABLE_SEMANTIC_SEARCH` | `memory/long_term.py::build_memory_store()` 创建 `AsyncPostgresStore`，启动时调用 `store.setup()` | `store_migrations`, `store`; 启用语义检索时还会创建 `vector_migrations`, `store_vectors` 和 `vector` 扩展 |
| Chainlit 历史会话数据层 | `DATABASE_URL`, `CHAINLIT_ENABLE_PASSWORD_AUTH`, `CHAINLIT_AUTH_SECRET` | `app/chainlit_data.py` 通过 `@cl.data_layer` 注册 `AppChainlitDataLayer`；Python 运行时只连接数据库，不自动执行 Prisma 迁移 | `User`, `Thread`, `Step`, `Element`, `Feedback`, `StepType`，由 `chainlit-datalayer` 的 Prisma migration 创建 |
| Chainlit 附件对象存储 | `BUCKET_NAME`, `APP_AWS_*`, `DEV_AWS_ENDPOINT`, `LOCAL_MINIO_*` | `app/chainlit_data.py::_build_chainlit_storage_client()` 创建 `S3StorageClient`；本地 MinIO 可按环境变量自动启动 | 文件对象存储在 MinIO/S3，PostgreSQL 中只保存 `Element.objectKey` 等元数据 |

LangGraph 的两类表可以在 Chainlit 启动时自动创建；Chainlit 历史会话表必须提前执行 `chainlit-datalayer` 的 Prisma migration；MinIO 只负责附件对象存储，不创建数据库表。

Windows 本地服务初始化脚本：

```powershell
cd <project-root>
if (!(Test-Path .env)) { Copy-Item .env.example .env }

.\scripts\init_services_windows.ps1 `
  -SuperUser postgres `
  -SuperPassword "<postgres-admin-password>" `
  -AppUser postgres `
  -AppPassword "<app-password>"
```

脚本会执行以下操作：

- 使用 `psql` 连接本机 PostgreSQL，创建或更新应用用户。
- 创建或修正 `langgraph_db` 和 `chainlit_db` 的 owner。
- 在 `chainlit_db` 中准备 `pgcrypto`。
- 按 pgvector 官方 Windows 流程构建安装 pgvector：设置 `PGROOT`，执行 `nmake /F Makefile.win` 和 `nmake /F Makefile.win install`，再在 `langgraph_db` 中创建 `vector` 扩展。
- 找到或克隆同级 `chainlit-datalayer`，安装 Node 依赖并运行 `npx prisma migrate deploy`。
- 下载本地 MinIO 到项目内 `.local/minio/bin`，数据目录为 `.local/minio/data`。
- 启动 MinIO，创建 `BUCKET_NAME` 对应 bucket。
- 不修改 `.env`；脚本参数和 `.env` 需要按 `.env.example` 保持一致。

如果 `psql.exe` 不在 `PATH`，通过参数显式指定：

```powershell
.\scripts\init_services_windows.ps1 `
  -PsqlPath "<path-to-psql.exe>" `
  -SuperUser postgres `
  -SuperPassword "<postgres-admin-password>" `
  -AppUser postgres `
  -AppPassword "<应用账号密码>"
```

如果 PostgreSQL 尚未安装 pgvector，请按 [pgvector 官方 Windows 安装说明](https://github.com/pgvector/pgvector#windows) 准备 Visual Studio C++ 构建工具，并以管理员身份在 Visual Studio x64 Native Tools 环境中运行该脚本，或确保 `nmake`/`cl` 已在 `PATH` 中。脚本默认把 pgvector 源码克隆到 `.local/pgvector/<version>`，安装目标由 `psql.exe` 推导出的 PostgreSQL 根目录决定；也可用 `-PgRoot`、`-PgVectorSourceDir` 和 `-PgVectorVersion` 覆盖。

手动执行 Chainlit 数据层迁移的等价命令：

```powershell
cd ..\chainlit-datalayer
npm ci
$env:DATABASE_URL = "<DATABASE_URL from .env>"
npx prisma migrate deploy
```

初始化后检查表：

```powershell
psql "<CHECKPOINTER_DB_URI from .env>" -c "\dt"
psql "<DATABASE_URL from .env>" -c "\dt"
```

注意事项：

- PostgreSQL 需要先在 Windows 上安装并启动服务，`psql.exe` 可通过 `PATH` 或脚本参数提供。
- Chainlit 历史会话列表和恢复入口需要同时启用认证与数据层。
- `MEMORY_ENABLE_SEMANTIC_SEARCH=true` 时需要 PostgreSQL 安装 `pgvector` 扩展，并且嵌入模型配置可用。
- 如果 `CHECKPOINTER_DB_URI` 为空或依赖不可用，`chat_app.py` 会回退到 `InMemorySaver`，聊天状态不会持久化。
- 如果 `DATABASE_URL` 为空，Chainlit 数据层不会注册，网页历史会话和线程恢复不可用。

## Run the Chainlit App

```powershell
cd <project-root>
conda activate mcp-alint
chainlit run chat_app.py
```

The Chainlit app starts the MCP server automatically through stdio. You do not
need to start `python -m mcp_server.server` separately for normal chat usage.

## Run the LangGraph Agent Server

The project also includes an Agent Server entrypoint:

```powershell
.\langgraph_server\start_langgraph_agent_server.cmd
```

Equivalent command:

```powershell
cd <project-root>
langgraph dev --config langgraph_server\langgraph.json --no-browser --allow-blocking --host 127.0.0.1 --port 2024
```

Default graph ID:

```text
lint
```

CLI call after the server is running:

```powershell
.\langgraph_server\lint-agent.cmd "你是谁"
```

For details, see `langgraph_server/README.md`.

## MCP Tools and Resources

`mcp_server/server.py` exposes the following main MCP tools:

| Tool | Purpose |
| --- | --- |
| `generate_basic_analysis_workflow` | Run the full ALINT + Yosys + source extraction + artifact organization workflow |
| `run_alint_analysis` | Run ALINT-PRO only and generate a CSV report |
| `analyze_verilog_structure` | Run Yosys structure analysis only |
| `export_verilog_netlist` | Export synthesized Verilog and JSON netlists |
| `convert_copilot_json_to_csv` | Convert JSON diagnosis output to CSV |
| `save_user_feedback` | Save user feedback to a JSON file |

It also exposes these read-only resources after a workflow run:

| URI | Content |
| --- | --- |
| `alint://basic/sources` | Source code with line numbers |
| `alint://basic/violations` | ALINT violation CSV |
| `alint://basic/ast` | AST JSON |
| `alint://basic/cfg_ddg_dfg` | CFG/DDG/DFG JSON |
| `alint://basic/kb` | Self-built Verilog knowledge base |

## Typical Diagnosis Flow

1. Start the Chainlit app.
2. Ask the agent to analyze an ALINT workspace and project.
3. The agent calls `generate_basic_analysis_workflow`.
4. The workflow writes standardized artifacts to:

```text
reports/_prepared/<session_id>/
```

5. The agent reads MCP resources and combines:

- ALINT violation report
- Verilog source code
- AST / CFG / DDG / DFG structure
- self-built Verilog rule knowledge
- IEEE / Vivado reference evidence when needed

6. The agent returns diagnosis, classification, evidence, and fix suggestions.

## Project Structure

```text
mcp_alint/
  chat_app.py                         # Chainlit compatibility entrypoint
  app/
    chainlit_data.py                  # Chainlit data layer and object storage setup
    chainlit_hitl.py                  # Chainlit HITL approval UI helpers
    chainlit_messages.py              # Chainlit/LangChain message conversion
    chainlit_runtime.py               # Chainlit agent runtime lifecycle
    chainlit_streaming.py             # Chainlit streaming/task display helpers
  mcp_server/
    eda_backend.py                    # Optional EDA backend imports
    json_conversion.py                # JSON/CSV conversion helpers
    pathing.py                        # Workspace path normalization helpers
    prompts.py                        # MCP prompt registration
    resources.py                      # MCP resource registration
    server.py                         # FastMCP server implementation
    tools/                            # MCP tool registration modules
  config.py                           # Centralized environment configuration
  workspace/
    project_utils.py                  # Shared project and path utilities
  eda/
    alint.py                          # ALINT-PRO batch runner
    ast.py                            # Yosys-based AST/CFG/DDG/netlist backend
  llm/
    factory.py                        # Shared LLM construction helper
  memory/
    long_term.py                      # Long-term memory tools and store setup
  rag/
    hardware_reference.py             # Hardware reference Agentic RAG
  alint_workflow/
    graph.py                          # LangGraph workflow definition
    state.py                          # Workflow state
    nodes/                            # ALINT/Yosys/source/organize nodes
  agent_runtime/
    checkpointer.py                   # LangGraph checkpointer factory
    configuration.py                  # LLM preset and runtime config helpers
    middleware.py                     # Shared middleware builders
    prompts.py                        # Shared agent prompts
    reflection.py                     # Evaluator-optimizer middleware
    tools.py                          # Shared MCP/RAG/web/memory tool loading
  compat/
    langgraph.py                      # Third-party compatibility patches
  prompts/
    templates.py                      # MCP prompt templates
  langgraph_server/                   # LangGraph Agent Server entrypoint and CLI
  skills/                             # Domain skills for lint triage and root-cause diagnosis
  scripts/                            # Utility scripts
  reports/                            # Generated reports, ignored by Git
```

## Skills

The repository contains several domain skills under `skills/`, including:

- `verilog-lint-triage`: classify lint findings into severe, general, or false positive, and write validated JSON.
- `verilog-lint-concrete-fix-advisor`: produce code-aware fix suggestions, especially for incomplete case coverage.
- `verilog-constant-propagation-root-cause`: trace hierarchy-level constant propagation roots.
- `verilog-dead-code-root-cause`: diagnose unreachable procedural branches and dead code evidence.

Enable skill loading with:

```env
AGENT_ENABLE_SKILLS=true
AGENT_SKILLS_DIRS=skills
```

## Generated Files and Git Policy

The following are runtime artifacts and should normally stay out of Git:

- `.env`
- `.chainlit/`
- `.files/`
- `.langgraph_api/`
- `.lint_agent_jobs/`
- `reports/`
- `__pycache__/`
- RAG vector indexes such as `rag_index*/`

Keep source code, prompts, skills, configuration templates, and documentation in
Git. Keep secrets, generated reports, uploaded files, and vector indexes out of
Git.

## Troubleshooting

If the app says the LLM is not configured, check:

```env
LLM_MODEL
LLM_BASE_URL
LLM_API_KEY
```

If ALINT analysis fails, check:

- The machine is Windows.
- `ALINT_EXE` points to `alintcon.exe`.
- The ALINT license is available.
- The `.alintws` workspace path and project name are correct.

If Yosys analysis fails, check:

- `OSS_CAD_SUITE_ROOT`.
- The Verilog/SystemVerilog files can be parsed by Yosys.
- Include directories and macro definitions are available.

If RAG is disabled, check:

- `RAG_ENABLED=true`
- PDF paths exist.
- Embedding API credentials are configured.

## More Documentation

- `PROJECT_DOCUMENTATION.md`: detailed design documentation.
- `project_design_overview_cn.md`: Chinese project design overview.
- `langgraph_server/README.md`: LangGraph Agent Server and CLI usage.
