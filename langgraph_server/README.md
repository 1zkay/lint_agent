# LangGraph Agent Server

本目录把 ALINT 智能体暴露为 LangGraph Agent Server，并提供 `lint-agent` 命令行入口。它是项目的 HTTP/CLI 运行方式，和根目录的 `chat_app.py` Chainlit Web UI 并列存在。

## 当前实现

当前实现符合 LangGraph Server 的标准 graph factory 形态：

- `langgraph.json` 声明 graph ID `lint`，入口为 `./langgraph_server/agent_runtime.py:lint_agent_graph`。
- `lint_agent_graph(runtime)` 是异步 context manager，返回由 LangChain `create_agent` 创建的 agent graph。
- LangGraph Server 负责 HTTP API、thread、run、store 管理；本项目在 graph factory 中读取 `runtime.store` 并注入给 agent。
- 主 agent 共享根目录的运行时模块：`agent_runtime/`、`memory/`、`rag/`、`llm/`、`compat/`。
- MCP 工具通过 stdio 子进程加载，启动方式是 `python -m mcp_server.server`，不再依赖旧的 `mcp_lint.py` 包装文件。
- ALINT-PRO 批处理执行逻辑统一在 `eda/alint.py`，MCP 工具和固定工作流节点都调用同一个 runner。

## 文件说明

```text
langgraph_server/
  agent_runtime.py                  # LangGraph Server graph factory 和缓存运行时
  langgraph.json                    # LangGraph Server 配置
  lint_agent_cli.py                 # CLI 客户端，调用 Agent Server
  lint-agent.cmd                    # Windows 命令入口
  lint_agent_alint_console.tcl      # ALINT-PRO Tcl console 非阻塞包装
  start_langgraph_agent_server.cmd  # 本地启动脚本
```

相关共享模块：

```text
agent_runtime/
  configuration.py                  # LLM preset 解析
  middleware.py                     # todo、filesystem、skills、HITL、retry、reflection 等 middleware
  prompts.py                        # agent system prompt
  tools.py                          # MCP/RAG/web/memory 工具加载
compat/
  langgraph.py                      # LangGraph 兼容补丁
eda/
  alint.py                          # ALINT-PRO batch runner
  ast.py                            # Yosys AST/RTLIL/CFG/DDG/DFG/netlist backend
mcp_server/
  server.py                         # FastMCP server，供 stdio 子进程启动
```

## 启动 Agent Server

PowerShell 或 cmd 中启动：

```powershell
D:\mcp\mcp_alint\langgraph_server\start_langgraph_agent_server.cmd
```

等价命令：

```powershell
cd D:\mcp\mcp_alint
langgraph dev --config langgraph_server\langgraph.json --no-browser --allow-blocking --host 127.0.0.1 --port 2024
```

默认服务地址：

```text
http://127.0.0.1:2024
```

Graph ID：

```text
lint
```

`start_langgraph_agent_server.cmd` 当前会激活本机 Conda 环境 `mcp`。如果你的环境名不同，需要修改该脚本或先手动激活环境后运行等价命令。

## 运行时结构

`langgraph_server/agent_runtime.py` 启动时会做以下事情：

1. 应用 LangGraph 兼容补丁：
   - `MCP_ALINT_PATCH_LANGGRAPH_SEND=true`
   - `MCP_ALINT_PATCH_LANGGRAPH_DEV_PERSISTENCE=true`
2. 读取 `.env` 和 `config.py`。
3. 根据默认 LLM preset 构建 chat model。
4. 通过 `agent_runtime.tools.load_agent_tools()` 加载：
   - FastMCP 工具，stdio 命令为 `python -m mcp_server.server`
   - 内置硬件参考 RAG 工具
   - Tavily 搜索工具（仅配置 `TAVILY_API_KEY` 时）
   - `fetch_url`
   - 长期记忆工具
5. 构建 middleware 栈：
   - Todo
   - DeepAgents filesystem
   - Skills
   - Summarization
   - Reflection
   - ModelRetry
   - ToolRetry
   - Shell
   - HumanInTheLoop
6. 使用 `create_agent(...)` 创建最终 agent。

LLM、MCP session 和工具会在进程内缓存，避免每个请求重复初始化。首次加载可能较慢，后续调用应复用缓存。

## Agent Chat UI

官方 Agent Chat UI 可以直接连接本服务。

- Hosted UI: `https://agentchat.vercel.app`
- Deployment URL: `http://127.0.0.1:2024`
- Graph ID: `lint`
- LangSmith API Key: 本地使用可留空

当前实现兼容不传自定义 context 的 Agent Chat UI。长期记忆工具会优先使用显式 `AgentContext`，否则从 LangGraph runtime metadata 中推导 thread/user 信息。

## PowerShell 调用

Agent Server 启动后，在另一个 PowerShell 终端中调用：

```powershell
D:\mcp\mcp_alint\langgraph_server\lint-agent.cmd "分析这个 ALINT 工程"
```

如果已经把 `lint-agent` 加入 `PATH`，也可以直接：

```powershell
lint-agent "分析这个 ALINT 工程"
```

常用参数：

```powershell
lint-agent --url http://127.0.0.1:2024 "你的问题"
lint-agent --thread-id my-thread "继续同一个线程"
lint-agent --user-id tom "带用户身份调用"
lint-agent --recursion-limit 80 "提高递归限制"
lint-agent --auto-approve "自动批准高风险工具调用"
lint-agent --auto-reject "自动拒绝高风险工具调用"
```

`lint_agent_cli.py` 使用 `langgraph_sdk.get_sync_client()` 和 `runs.wait` 调用 Agent Server。如果遇到 HITL interrupt，会在交互式终端中提示批准、拒绝或编辑工具参数。

## ALINT-PRO Console 调用

`D:\software\ALINT-PRO\runalintprocon.bat` 打开的是 ALINT-PRO Tcl console，不是 PowerShell/cmd，因此不能直接依赖 Windows `PATH`。

在 ALINT-PRO console 中先加载 Tcl 包装：

```tcl
source D:/mcp/mcp_alint/langgraph_server/lint_agent_alint_console.tcl
```

然后调用：

```tcl
lint-agent "分析当前工程"
lint-agent -auto-approve "允许需要审批的工具调用"
lint-agent -auto-reject "拒绝需要审批的工具调用"
```

Tcl 包装是非阻塞的：命令会立刻返回 ALINT-PRO 的 `>` 提示符，后台 Python CLI 调用 `http://127.0.0.1:2024`，完成后再把结果打印回同一个 console。

## 工具审批

如果 `.env` 中启用：

```env
AGENT_TOOL_APPROVAL_ENABLED=true
```

则高风险工具会经过 LangChain `HumanInTheLoopMiddleware`。当前审批覆盖的工具由 `agent_runtime/middleware.py` 统一构建，包含文件写入、删除、移动、复制和 shell 等操作。

交互式 CLI 会提示人工决策；非交互场景可以使用：

```powershell
lint-agent --auto-approve "..."
lint-agent --auto-reject "..."
```

不建议默认使用 `--auto-approve`，因为它会放行写文件、删文件或执行 shell 等高风险操作。

## 与 Chainlit 的关系

`langgraph_server/` 只负责 LangGraph Agent Server 方案，不替代 Chainlit 入口。

- Chainlit Web UI：根目录 `chat_app.py`
- LangGraph Agent Server：`langgraph_server/agent_runtime.py`
- 两边共享 `agent_runtime/`、`memory/`、`rag/`、`llm/`、`compat/` 等模块
- 两边生命周期不同：Chainlit 每个聊天会话有自己的 runtime owner；Agent Server 在进程内缓存 LLM/MCP/tools

这种重复是有意保留的入口生命周期边界，不是业务逻辑重复。

## 兼容补丁

当前默认启用两个补丁：

```env
MCP_ALINT_PATCH_LANGGRAPH_SEND=true
MCP_ALINT_PATCH_LANGGRAPH_DEV_PERSISTENCE=true
```

用途：

- `MCP_ALINT_PATCH_LANGGRAPH_SEND`：递归清理 LangGraph `Send` 中嵌套的不可序列化运行时对象，避免 MCP/shell session 对象进入 checkpoint。
- `MCP_ALINT_PATCH_LANGGRAPH_DEV_PERSISTENCE`：仅用于 `langgraph dev` 本地 `.langgraph_api` 落盘时，清理不可 pickle 对象，避免 Windows + MCP stdio 下出现 `TextIOWrapper` pickle 异常。

如果未来升级 LangGraph 后官方已经修复，可以临时关闭验证：

```powershell
$env:MCP_ALINT_PATCH_LANGGRAPH_DEV_PERSISTENCE = "false"
$env:MCP_ALINT_PATCH_LANGGRAPH_SEND = "false"
D:\mcp\mcp_alint\langgraph_server\start_langgraph_agent_server.cmd
```

## 常见问题

如果 `lint-agent` 提示：

```text
Agent Server is not reachable at http://127.0.0.1:2024
```

先确认 `start_langgraph_agent_server.cmd` 已启动，并且日志中出现：

```text
API: http://127.0.0.1:2024
```

如果看到：

```text
Slow graph load. Accessing graph 'lint' took ...
```

第一次调用可能正常，因为需要初始化 LLM、MCP stdio server、RAG、工具和 middleware。后续调用不应反复出现明显慢加载；如果每次都很慢，通常说明 Agent Server 进程被重启或缓存没有生效。

如果 MCP 工具加载失败，单独验证：

```powershell
cd D:\mcp\mcp_alint
python -m mcp_server.server
```

该命令会以前台 stdio server 方式启动，正常情况下会等待 MCP 客户端连接。
