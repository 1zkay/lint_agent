# Verilog Lint EDA Diagnostic Agent

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/1zkay/lint_agent)

`lint_agent` is a Verilog/SystemVerilog lint tool diagnosis agent built around
LangChain, LangGraph, MCP, Chainlit, Yosys, and Agentic RAG.

The project turns lint tool output, HDL source code, structural analysis
artifacts, self-built Verilog rule knowledge, and reference-document retrieval
into one interactive diagnosis workflow.

The core Chainlit agent can also run without the external lint tool, Yosys, or other EDA
tools. In that mode it works as a general-purpose LLM agent with chat, memory,
RAG, skills, and approval workflows; only EDA-specific analysis tools are
unavailable or return graceful failure messages.

## What This Project Does

- Provides a Chainlit chat UI for Verilog lint diagnosis.
- Uses LangChain `create_agent` as the main agent runtime.
- Uses MCP to expose the lint tool, Yosys, resources, and prompts as standard tools.
- Uses LangGraph `StateGraph` for deterministic EDA preprocessing workflows.
- Uses the lint tool to generate lint violation CSV reports.
- Uses Yosys / OSS CAD Suite to generate AST, RTLIL, CFG, DDG, DFG, and netlist artifacts.
- Uses Agentic RAG over IEEE hardware reference PDFs.
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
LangGraph lint tool workflow
  |-- lint_node       -> lint tool CSV report
  |-- structure_node  -> Yosys AST / RTLIL / CFG / DDG / DFG
  |-- sources_node    -> source code with line numbers
  |-- organize_node   -> reports/_prepared/<session_id>/
```

## Main Entry Points

| Entry                  | File                                  | Purpose                                                         |
| ---------------------- | ------------------------------------- | --------------------------------------------------------------- |
| Chainlit app           | `chat_app.py`                       | Chainlit compatibility entrypoint and message streaming handler |
| MCP server             | `mcp_server/server.py`              | FastMCP server assembly                                         |
| MCP implementation     | `mcp_server/`                       | Exposes EDA tools, resources, and prompts                       |
| LangGraph workflow     | workflow graph                        | Deterministic lint tool analysis pipeline                       |
| Yosys backend          | `eda/ast.py`                        | AST, RTLIL, CFG/DDG/DFG, and netlist generation                 |
| Agentic RAG            | `rag/hardware_reference.py`         | Reference-document retrieval and answer generation              |
| Long-term memory       | `memory/long_term.py`               | User profile and durable memory tools                           |
| LangGraph Agent Server | `langgraph_server/agent_runtime.py` | Alternative HTTP/CLI agent runtime                              |

## Requirements

Recommended environment:

- Python 3.12.
- Conda or another virtual environment manager.
- LLM API credentials for a LangChain-compatible chat model.

Required only for EDA diagnosis workflows:

- Windows, when the configured lint tool is invoked through a Windows executable.
- A supported lint tool installed and licensed.
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
conda env create -f environment.lint-agent.yml
conda activate lint-agent
```

The existing Windows helper scripts in this repository currently activate a
Conda environment named `mcp`. If your environment name is different, either
activate it manually before running commands or update the `.cmd` files.

For editable Python package usage:

```powershell
pip install -e .
```

Use `environment.lint-agent.yml` for the full Chainlit runtime because it also
includes UI-related dependencies.

## Prebuilt Docker Images

Prebuilt customer Docker images are available from Baidu Netdisk:

[Download from Baidu Netdisk](https://pan.baidu.com/s/1ZmkFPn5icKgY3T5h6hXFaA?pwd=mfa8)

These images can run the packaged Chainlit agent without requiring local Python
dependency installation. The lint tool and Yosys are still optional from the perspective
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

Persistence initialization in this project has four parts:

| Purpose                            | Configuration                                                                                                     | Initialization entry point                                                                                                                                                                | Tables                                                                                                                                                   |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| LangGraph checkpointer             | `CHECKPOINTER_BACKEND`, `CHECKPOINTER_DB_URI`, `CHECKPOINTER_AUTO_SETUP`                                    | `agent_runtime/checkpointer.py::build_checkpointer()` creates `AsyncPostgresSaver` and calls `checkpointer.setup()` at startup                                                      | `checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`                                                                  |
| Long-term memory store             | `MEMORY_STORE_BACKEND`, `MEMORY_STORE_DB_URI`, `MEMORY_STORE_AUTO_SETUP`, `MEMORY_ENABLE_SEMANTIC_SEARCH` | `memory/long_term.py::build_memory_store()` creates `AsyncPostgresStore` and calls `store.setup()` at startup                                                                       | `store_migrations`, `store`; when semantic search is enabled, it also creates `vector_migrations`, `store_vectors`, and the `vector` extension |
| Chainlit thread history data layer | `DATABASE_URL`, `CHAINLIT_ENABLE_PASSWORD_AUTH`, `CHAINLIT_AUTH_SECRET`                                     | `app/chainlit_data.py` registers `AppChainlitDataLayer` through `@cl.data_layer`; the Python runtime only connects to the database and does not run Prisma migrations automatically | `User`, `Thread`, `Step`, `Element`, `Feedback`, `StepType`, created by the Prisma migrations in `chainlit-datalayer`                      |
| Chainlit attachment object storage | `BUCKET_NAME`, `APP_AWS_*`, `DEV_AWS_ENDPOINT`, `LOCAL_MINIO_*`                                           | `app/chainlit_data.py::_build_chainlit_storage_client()` creates `S3StorageClient`; local MinIO can be started automatically from environment variables                               | File objects are stored in MinIO/S3; PostgreSQL stores only metadata such as `Element.objectKey`                                                       |

The two LangGraph table groups can be created automatically when Chainlit starts. Chainlit thread history tables must be created ahead of time by running the `chainlit-datalayer` Prisma migrations. MinIO only stores attachment objects and does not create database tables.

Windows local service initialization script:

```powershell
cd <project-root>
if (!(Test-Path .env)) { Copy-Item .env.example .env }

.\scripts\init_services_windows.ps1 `
  -SuperUser postgres `
  -SuperPassword "<postgres-admin-password>" `
  -AppUser postgres `
  -AppPassword "<app-password>"
```

The script performs these steps:

- Connects to the local PostgreSQL instance with `psql` and creates or updates the application user.
- Creates or fixes the owner of `langgraph_db` and `chainlit_db`.
- Prepares `pgcrypto` in `chainlit_db`.
- Builds and installs pgvector using the official Windows flow: sets `PGROOT`, runs `nmake /F Makefile.win` and `nmake /F Makefile.win install`, then creates the `vector` extension in `langgraph_db`.
- Finds or clones the sibling `chainlit-datalayer` repository, installs Node dependencies, and runs `npx prisma migrate deploy`.
- Downloads local MinIO into `.local/minio/bin` in this project; the data directory is `.local/minio/data`.
- Starts MinIO and creates the bucket named by `BUCKET_NAME`.
- Does not modify `.env`; script parameters and `.env` values should stay consistent with `.env.example`.

If `psql.exe` is not in `PATH`, pass it explicitly:

```powershell
.\scripts\init_services_windows.ps1 `
  -PsqlPath "<path-to-psql.exe>" `
  -SuperUser postgres `
  -SuperPassword "<postgres-admin-password>" `
  -AppUser postgres `
  -AppPassword "<app-password>"
```

If PostgreSQL does not have pgvector installed, follow the [official pgvector Windows installation instructions](https://github.com/pgvector/pgvector#windows) to prepare the Visual Studio C++ build tools. Run this script as an administrator from the Visual Studio x64 Native Tools environment, or ensure that `nmake` and `cl` are already in `PATH`. By default, the script clones the pgvector source into `.local/pgvector/<version>`, and the installation target is inferred from the PostgreSQL root directory associated with `psql.exe`. You can override this with `-PgRoot`, `-PgVectorSourceDir`, and `-PgVectorVersion`.

Equivalent command for running the Chainlit data layer migration manually:

```powershell
cd ..\chainlit-datalayer
npm ci
$env:DATABASE_URL = "<DATABASE_URL from .env>"
npx prisma migrate deploy
```

Check tables after initialization:

```powershell
psql "<CHECKPOINTER_DB_URI from .env>" -c "\dt"
psql "<DATABASE_URL from .env>" -c "\dt"
```

Notes:

- PostgreSQL must be installed and running as a Windows service first; `psql.exe` can be provided through `PATH` or a script parameter.
- Chainlit thread history and thread resume require both authentication and the data layer to be enabled.
- `MEMORY_ENABLE_SEMANTIC_SEARCH=true` requires the PostgreSQL `pgvector` extension and a working embedding model configuration.
- If `CHECKPOINTER_DB_URI` is empty or dependencies are unavailable, `chat_app.py` falls back to `InMemorySaver`, and chat state will not be persisted.
- If `DATABASE_URL` is empty, the Chainlit data layer is not registered, and web thread history and thread resume are unavailable.

## Run the Chainlit App

```powershell
cd <project-root>
conda activate lint-agent
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
.\langgraph_server\lint-agent.cmd "Who are you?"
```

For details, see `langgraph_server/README.md`.

## MCP Tools and Resources

`mcp_server/server.py` exposes the following main MCP tools:

| Tool                                 | Purpose                                                                             |
| ------------------------------------ | ----------------------------------------------------------------------------------- |
| `generate_basic_analysis_workflow` | Run the full lint tool + Yosys + source extraction + artifact organization workflow |
| lint analysis tool                   | Run the lint tool only and generate a CSV report                                    |
| `analyze_verilog_structure`        | Run Yosys structure analysis only                                                   |
| `export_verilog_netlist`           | Export synthesized Verilog and JSON netlists                                        |
| `convert_copilot_json_to_csv`      | Convert JSON diagnosis output to CSV                                                |
| `save_user_feedback`               | Save user feedback to a JSON file                                                   |

It also exposes these read-only resources after a workflow run:

| URI                               | Content                           |
| --------------------------------- | --------------------------------- |
| `lint-tool://basic/sources`     | Source code with line numbers     |
| `lint-tool://basic/violations`  | lint tool violation CSV           |
| `lint-tool://basic/ast`         | AST JSON                          |
| `lint-tool://basic/cfg_ddg_dfg` | CFG/DDG/DFG JSON                  |
| `lint-tool://basic/kb`          | Self-built Verilog knowledge base |

## Typical Diagnosis Flow

1. Start the Chainlit app.
2. Ask the agent to analyze a lint tool workspace and project.
3. The agent calls `generate_basic_analysis_workflow`.
4. The workflow writes standardized artifacts to:

```text
reports/_prepared/<session_id>/
```

5. The agent reads MCP resources and combines:

- lint tool violation report
- Verilog source code
- AST / CFG / DDG / DFG structure
- self-built Verilog rule knowledge
- IEEE reference evidence when needed

6. The agent returns diagnosis, classification, evidence, and fix suggestions.

## Project Structure

```text
lint_agent/
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
    lint_tool.py                      # lint tool batch runner
    ast.py                            # Yosys-based AST/CFG/DDG/netlist backend
  llm/
    factory.py                        # Shared LLM construction helper
  memory/
    long_term.py                      # Long-term memory tools and store setup
  rag/
    hardware_reference.py             # Hardware reference Agentic RAG
  lint_tool_workflow/
    graph.py                          # LangGraph workflow definition
    state.py                          # Workflow state
    nodes/                            # lint tool / Yosys / source / organize nodes
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
AGENT_EXTRA_SKILLS_DIRS=customer_skills
```

In Docker packages, `./customer-config/skills` is mounted to
`customer_skills`, so customers can add `skill-name/SKILL.md` directories
without rebuilding the image.

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

If lint tool analysis fails, check:

- The machine is Windows.
- `LINT_TOOL_EXE` points to the lint tool executable.
- The lint tool license is available.
- The lint tool workspace path and project name are correct.

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
