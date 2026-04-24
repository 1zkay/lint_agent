# LangGraph Agent Server

本目录用于把 ALINT 智能体作为一个完整的 LangGraph Agent Server 暴露出来，并提供 `lint-agent` 命令行入口。

## 当前结论

当前实现的主路径是官方标准做法：

- `langgraph.json` 声明 `dependencies`、`graphs`、`env` 和 `python_version`。
- `agent_runtime.py:lint_agent_graph` 是 LangGraph Server graph factory。
- graph factory 接收 `ServerRuntime`，并使用 `runtime.store` 注入 LangGraph Server 管理的 store。
- 智能体由 LangChain `create_agent` 创建，LangGraph Server 负责通过 HTTP API 暴露。
- CLI 使用 `langgraph_sdk` 的 `runs.wait` 调用 Agent Server。

当前实现也包含两个兼容性补丁，它们不是业务逻辑，也不是长期理想形态：

- `MCP_ALINT_PATCH_LANGGRAPH_SEND=true`：过滤 LangGraph `Send` 中嵌套的不可序列化运行时对象，避免 MCP/shell 句柄进入 checkpoint。
- `MCP_ALINT_PATCH_LANGGRAPH_DEV_PERSISTENCE=true`：只在 `langgraph dev` 本地 `.langgraph_api` 落盘时清洗不可 pickle 对象，避免 Windows + MCP stdio 下出现 `TextIOWrapper` pickle 异常。

这两个补丁默认开启，是为了保证当前 Windows 本机 ALINT-PRO + MCP stdio 能稳定运行。升级 LangGraph 后如果官方已修复，可在启动前设置为 `false` 关闭。

## 文件说明

```text
langgraph_server/
  agent_runtime.py                  # 构建并导出 LangGraph 智能体图
  langgraph.json                    # LangGraph Agent Server 配置
  lint_agent_cli.py                 # CLI 客户端，调用 Agent Server
  lint-agent.cmd                    # Windows 命令入口
  lint_agent_alint_console.tcl      # ALINT-PRO Tcl console 非阻塞包装
  start_langgraph_agent_server.cmd  # 启动 Agent Server
```

## 启动 Agent Server

在 PowerShell 或 cmd 中启动：

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

智能体 graph ID：

```text
lint
```

## Agent Chat UI 访问

官方 Agent Chat UI 可以直接作为本服务的网页前端使用。

- Hosted UI：`https://agentchat.vercel.app`
- 官方文档：`https://docs.langchain.com/oss/python/langchain/ui`

使用步骤：

1. 先启动 Agent Server：

```powershell
D:\mcp\mcp_alint\langgraph_server\start_langgraph_agent_server.cmd
```

2. 打开 `https://agentchat.vercel.app`

3. 在设置中填写：

```text
Deployment URL: http://127.0.0.1:2024
Graph ID: lint
LangSmith API Key: 留空即可（本地 Agent Server 不需要）
```

4. 点击 `Continue` 后即可开始聊天。

说明：

- Agent Chat UI 的标准接入只需要 `Deployment URL` 和 `Graph ID`，不需要手工提供额外的 `context`。
- 当前实现已兼容官方 Agent Chat UI；如果刚升级代码，需先重启 Agent Server 再访问。
- `127.0.0.1:2024` 表示浏览器与 Agent Server 必须在同一台机器上。
- 如果浏览器环境无法直接访问本机 `127.0.0.1`，可按官方方式本地启动 UI：

```powershell
npx create-agent-chat-app --project-name my-chat-ui
cd my-chat-ui
pnpm install
pnpm dev
```

然后在本地 UI 中填写同样的：

```text
Deployment URL: http://127.0.0.1:2024
Graph ID: lint
```

## PowerShell 调用

服务启动后，在另一个 PowerShell 终端中调用：

```powershell
lint-agent "你是谁"
```

如果当前终端还不能识别 `lint-agent`，使用完整路径：

```powershell
D:\mcp\mcp_alint\langgraph_server\lint-agent.cmd "你是谁"
```

或者重新加载 PowerShell profile：

```powershell
. $PROFILE
Get-Command lint-agent
```

## ALINT-PRO Console 调用

`D:\software\ALINT-PRO\runalintprocon.bat` 打开的是 ALINT-PRO Tcl console，不是 PowerShell/cmd，因此不能直接依赖 Windows `Path`。

在 ALINT-PRO console 中先加载项目侧 Tcl 包装：

```tcl
source D:/mcp/mcp_alint/langgraph_server/lint_agent_alint_console.tcl
```

然后调用：

```tcl
lint-agent "你是谁"
```

该 Tcl 包装是非阻塞的：

- `lint-agent` 会立即返回 ALINT-PRO 的 `>` 提示符。
- Python CLI 在后台调用 `http://127.0.0.1:2024`。
- 结果写入临时输出文件，再由 Tcl `after` 轮询并 `puts` 回同一个 ALINT console。
- 如果 Agent Server 没启动，会在 ALINT console 中输出清晰错误，而不是静默失败。

不修改 ALINT-PRO 安装目录的前提下，每次新打开 `runalintprocon.bat` 都需要重新执行一次 `source .../lint_agent_alint_console.tcl`。

## 工具审批

`lint-agent` 使用 LangGraph Server 的 `runs.wait` 接口。遇到 `write_file`、`move_file`、`file_delete`、`copy_file`、`shell` 等高风险工具时，如果 `.env` 中开启了 `AGENT_TOOL_APPROVAL_ENABLED=true`，LangChain `HumanInTheLoopMiddleware` 会返回 `__interrupt__`。

交互式 PowerShell 中会提示审批：

```powershell
lint-agent "新建一个简单 py 脚本"
```

非交互场景可以显式自动批准或拒绝：

```powershell
lint-agent --auto-approve "新建一个简单 py 脚本"
lint-agent --auto-reject "删除这个文件"
```

ALINT-PRO console 中也支持：

```tcl
lint-agent -auto-approve "请调用 shell 工具执行 pwd"
lint-agent -auto-reject "删除这个文件"
```

不建议默认使用 `--auto-approve`，因为它会放行写文件、删文件或执行 shell 命令等高风险操作。

## 与 Chainlit 的关系

本目录只负责 LangGraph Agent Server 方案，不替代原来的 Chainlit 入口。

- `chat_app.py` 仍按原方式启动 Chainlit。
- `langgraph_server/agent_runtime.py` 只服务 LangGraph Agent Server。
- 两边能力应保持对齐；共享 prompt 和兼容补丁放在 `agent_runtime/`、`compat/`，入口生命周期仍然分离。

## 冗余与边界

当前保留的重复主要是 `chat_app.py` 和 `agent_runtime.py` 各自管理一套入口生命周期。这是有意保留的兼容边界，避免 Chainlit 端行为被 Agent Server 优化影响。

当前已移除的冗余：

- 旧的 `@lint.cmd` 入口已删除，PowerShell 不能把裸 `@lint` 当命令名使用。
- CLI 中旧的 Windows `CONOUT$` 直写逻辑已移除，ALINT-PRO console 统一使用 Tcl 文件轮询输出。

## 常见问题

如果 `lint-agent` 提示服务不可达：

```text
Agent Server is not reachable at http://127.0.0.1:2024
```

先确认 `start_langgraph_agent_server.cmd` 已经启动，并且日志中出现：

```text
API: http://127.0.0.1:2024
```

如果看到 `Slow graph load`：

```text
Slow graph load. Accessing graph 'lint' took ...
```

第一次调用时可能出现一次，因为需要初始化 LLM、MCP session、RAG 和工具缓存。后续调用不应反复出现。如果每次都出现，说明服务进程被重启或缓存没有生效。

如果看到 `.langgraph_api` pickle 相关异常，保持默认兼容补丁开启即可。若升级 LangGraph 后要测试是否还需要补丁，可临时关闭：

```powershell
$env:MCP_ALINT_PATCH_LANGGRAPH_DEV_PERSISTENCE = "false"
$env:MCP_ALINT_PATCH_LANGGRAPH_SEND = "false"
D:\mcp\mcp_alint\langgraph_server\start_langgraph_agent_server.cmd
```
