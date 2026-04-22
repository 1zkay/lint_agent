# ALINT-PRO Verilog Lint EDA Diagnostic Agent

`mcp_alint` is a Verilog/SystemVerilog lint diagnosis agent built around
LangChain, LangGraph, MCP, Chainlit, ALINT-PRO, Yosys, and Agentic RAG.

The project turns commercial lint output, HDL source code, structural analysis
artifacts, self-built Verilog rule knowledge, and reference-document retrieval
into one interactive diagnosis workflow.

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
FastMCP Server: mcp_lint.py
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
| Chainlit app | `chat_app.py` | Main web chat UI and agent runtime |
| MCP server | `mcp_lint.py` | Exposes EDA tools, resources, and prompts |
| LangGraph workflow | `agent/graph.py` | Deterministic ALINT analysis pipeline |
| Yosys backend | `AST.py` | AST, RTLIL, CFG/DDG/DFG, and netlist generation |
| Agentic RAG | `agentic_rag.py` | Reference-document retrieval and answer generation |
| Long-term memory | `long_term_memory.py` | User profile and durable memory tools |
| LangGraph Agent Server | `langgraph_server/agent_runtime.py` | Alternative HTTP/CLI agent runtime |

## Requirements

Recommended environment:

- Windows, because ALINT-PRO is invoked through a Windows executable.
- Python 3.12.
- Conda or another virtual environment manager.
- ALINT-PRO installed and licensed.
- OSS CAD Suite / Yosys available, usually at `D:\mcp\oss-cad-suite` or configured through `OSS_CAD_SUITE_ROOT`.
- LLM API credentials for a LangChain-compatible chat model.

Optional components:

- PostgreSQL for LangGraph checkpointer, memory store, and Chainlit data layer.
- MinIO or another S3-compatible storage service for Chainlit file uploads.
- Tavily API key for web search.

## Installation

From the project directory:

```powershell
cd D:\mcp\mcp_alint
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

## Configuration

Create a `.env` file in the project root:

```text
D:\mcp\mcp_alint\.env
```

The `.env` file is ignored by Git and should not be committed.

Important variables:

```env
# LLM
LLM_MODEL=openai:gpt-4.1
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=your_api_key
LLM_TEMPERATURE=0.1
LLM_MAX_TOKENS=81920

# ALINT-PRO
ALINT_PRO_ROOT=D:\mcp
ALINT_EXE=D:\software\ALINT-PRO\bin\alintcon.exe
CSV_OUTPUT_DIR=D:\mcp\mcp_alint\reports

# RAG
RAG_ENABLED=true
RAG_PDF_PATHS=D:\mcp\1800-2017.pdf;D:\mcp\vivado-synthesis.pdf
RAG_EMBED_MODEL=openai/text-embedding-3-small
RAG_EMBED_BASE_URL=https://api.openai.com/v1
RAG_EMBED_API_KEY=your_embedding_api_key

# Persistence, optional
CHECKPOINTER_BACKEND=postgres
CHECKPOINTER_DB_URI=postgresql://user:password@localhost:5432/dbname
MEMORY_STORE_BACKEND=postgres
DATABASE_URL=postgresql://user:password@localhost:5432/dbname

# Chainlit S3-compatible upload storage, optional
BUCKET_NAME=chainlit
APP_AWS_REGION=us-east-1
APP_AWS_ACCESS_KEY=minioadmin
APP_AWS_SECRET_KEY=minioadmin
DEV_AWS_ENDPOINT=http://127.0.0.1:9000
```

If PostgreSQL is not configured, the Chainlit runtime can fall back to in-memory
checkpointing for chat state. Long-term memory with PostgreSQL requires a valid
memory store URI.

## Run the Chainlit App

Option 1: use the helper script:

```powershell
D:\mcp\mcp_alint\start_chainlit_mcp.cmd
```

Option 2: run manually:

```powershell
cd D:\mcp\mcp_alint
conda activate mcp-alint
chainlit run chat_app.py
```

The Chainlit app starts the MCP server automatically through stdio. You do not
need to start `mcp_lint.py` separately for normal chat usage.

## Run the LangGraph Agent Server

The project also includes an Agent Server entrypoint:

```powershell
D:\mcp\mcp_alint\langgraph_server\start_langgraph_agent_server.cmd
```

Equivalent command:

```powershell
cd D:\mcp\mcp_alint
langgraph dev --config langgraph_server\langgraph.json --no-browser --allow-blocking --host 127.0.0.1 --port 2024
```

Default graph ID:

```text
lint
```

CLI call after the server is running:

```powershell
D:\mcp\mcp_alint\langgraph_server\lint-agent.cmd "你是谁"
```

For details, see `langgraph_server/README.md`.

## MCP Tools and Resources

`mcp_lint.py` exposes the following main MCP tools:

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
  chat_app.py                         # Chainlit UI and main agent runtime
  mcp_lint.py                         # FastMCP server
  AST.py                              # Yosys-based structure analysis backend
  agentic_rag.py                      # Hardware reference Agentic RAG
  long_term_memory.py                 # Long-term memory tools and store setup
  reflection_middleware.py            # Evaluator-optimizer middleware
  llm_factory.py                      # Shared LLM construction helper
  config.py                           # Centralized environment configuration
  utils.py                            # Shared project and path utilities
  agent/
    graph.py                          # LangGraph workflow definition
    state.py                          # Workflow state
    nodes/                            # ALINT/Yosys/source/organize nodes
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

- `OSS_CAD_SUITE_ROOT` or the default `D:\mcp\oss-cad-suite` path.
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
