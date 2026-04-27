# LangGraph Agent Server

本目录把 ALINT 智能体暴露为 LangGraph Agent Server，并提供 `lint-agent` 命令行入口。它是项目的 HTTP/CLI 运行方式，和根目录的 `chat_app.py` Chainlit Web UI 并列存在。

当前入口适合三类场景：

- 在 LangGraph Agent Server 上用标准 HTTP API 运行 `lint` graph。
- 在 PowerShell/cmd 中用 `lint-agent` 进行一次性提问或交互式持久对话。
- 在 ALINT-PRO Tcl console 中通过非阻塞包装调用同一个 Agent Server。

## 当前实现

当前实现采用 LangGraph Server 的标准 graph factory 形态：

- `langgraph.json` 声明 graph ID `lint`，入口为 `./langgraph_server/agent_runtime.py:lint_agent_graph`。
- `lint_agent_graph(runtime)` 是异步 context manager，返回由 LangChain `create_agent` 创建的 agent graph。
- LangGraph Server 负责 HTTP API、thread、run、store 的服务端管理；本项目在 graph factory 中读取 `runtime.store` 并注入给 agent。
- 主 agent 共享根目录的运行时模块：`agent_runtime/`、`memory/`、`rag/`、`llm/`、`compat/`。
- MCP 工具通过 stdio 子进程加载，启动方式是 `python -m mcp_server.server`。
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
memory/
  long_term.py                      # 用户 profile 和长期记忆工具
```

## 启动 Agent Server

PowerShell 或 cmd 中启动：

```powershell
D:\mcp\lint_agent\langgraph_server\start_langgraph_agent_server.cmd
```

等价命令：

```powershell
cd D:\mcp\lint_agent
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

`start_langgraph_agent_server.cmd` 当前会激活本机 Conda 环境 `mcp`。如果你的环境名不同，需要修改该脚本，或先手动激活环境后运行等价命令。

启动成功后，日志中应出现类似内容：

```text
API: http://127.0.0.1:2024
```

也可以用健康检查确认服务已就绪：

```powershell
Invoke-WebRequest http://127.0.0.1:2024/ok -UseBasicParsing
```

### Docker 启动

客户包的 `docker-compose.yml` 已包含 `langgraph-server` 服务。项目根目录执行：

```powershell
docker compose up -d --build
```

会默认启动 Chainlit Web UI 和 LangGraph Agent Server。Agent Server 在容器内监听 `0.0.0.0:2024`，映射到宿主机：

```text
http://127.0.0.1:2024
```

查看状态和日志：

```powershell
docker compose ps langgraph-server
docker compose logs -f langgraph-server
```

Docker 只启动 Agent Server，不启动 ALINT-PRO/EDA 软件。用户需要在自己的 EDA Tcl console 中手动 source `lint_agent_alint_console.tcl`，再用 `lint-agent` 连接上述服务。

## 命令行使用

Agent Server 启动后，在另一个 PowerShell 终端中调用：

```powershell
D:\mcp\lint_agent\langgraph_server\lint-agent.cmd "分析这个 ALINT 工程"
```

如果已经把 `lint-agent` 加入 `PATH`，也可以直接：

```powershell
lint-agent "分析这个 ALINT 工程"
```

### 一次性调用

带 prompt 参数时，CLI 执行一次请求后退出：

```powershell
lint-agent "你是谁？"
```

也可以从管道或文件读取输入：

```powershell
"分析当前 lint 报告" | lint-agent
lint-agent --prompt-file D:\tmp\prompt.txt
lint-agent --prompt-file D:\tmp\prompt.txt --delete-prompt-file
```

常用参数：

```powershell
lint-agent --url http://127.0.0.1:2024 "你的问题"
lint-agent --thread-id 11111111-1111-1111-1111-111111111111 "继续指定 thread"
lint-agent --user-id tom "带用户身份调用"
lint-agent --recursion-limit 80 "提高递归限制"
lint-agent --auto-approve "自动批准高风险工具调用"
lint-agent --auto-reject "自动拒绝高风险工具调用"
lint-agent --debug "显示 Python traceback"
```

注意：`--thread-id` 必须是 UUID。LangGraph Server 会校验 thread ID，不能使用 `my-thread` 这类普通字符串。

对应环境变量：

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `LANGGRAPH_URL` | `http://127.0.0.1:2024` | Agent Server 地址 |
| `LANGGRAPH_ASSISTANT` | `lint` | graph/assistant 名称 |
| `LANGGRAPH_THREAD_ID` | 空 | 指定要继续的 thread UUID |
| `LANGGRAPH_USER_ID` | 空 | 指定用户身份；为空时使用 `cli:<USERNAME>` |
| `LANGGRAPH_RECURSION_LIMIT` | `50` | 单次 run 的递归限制 |

### 交互式持久对话

裸启动 `lint-agent` 时，如果当前 stdin 是交互式终端，会进入 REPL：

```powershell
lint-agent
```

也可以显式指定交互模式：

```powershell
lint-agent --interactive
lint-agent -i
```

每次裸启动默认创建一个新的 UUID thread。也就是说，下一次直接运行 `lint-agent` 不会自动接上一次对话；如果需要继续旧对话，先用 `/threads` 查看，再用 `/resume <thread_id>` 切换。

交互模式启动后会显示当前 `thread_id` 和 `user_id`：

```text
lint-agent interactive mode
server: http://127.0.0.1:2024
assistant: lint
thread_id: <uuid>
user_id: cli:<Windows 用户名>
```

交互式命令：

| 命令 | 作用 |
| --- | --- |
| `/new` | 创建一个新的持久 thread |
| `/threads [limit]` | 查看当前用户最近的 threads |
| `/threads all [limit]` | 查看所有用户最近的 threads |
| `/resume <thread_id>` | 切换到已有 thread |
| `/thread` | 打印当前 thread_id |
| `/thread-info` | 查看当前 thread 的服务端 metadata |
| `/state` | 查看当前 thread 的 state 摘要 |
| `/history [limit]` | 查看当前 thread 的 checkpoint history |
| `/runs [limit]` | 查看当前 thread 的 runs |
| `/run <run_id>` | 查看某个 run 的 JSON |
| `/cancel <run_id>` | interrupt 一个 pending/running run |
| `/assistants [limit]` | 查看当前 graph 的 assistants |
| `/assistant [id]` | 查看 assistant metadata；不传 id 时自动解析默认 assistant |
| `/graph` | 查看 assistant graph JSON |
| `/schemas` | 查看 assistant schemas JSON，输出通常较长 |
| `/help` | 查看帮助 |
| `/exit` | 退出交互模式 |

示例：

```text
lint-agent> /threads 10
lint-agent> /resume 11111111-1111-1111-1111-111111111111
lint-agent> 继续刚才的问题
lint-agent> /state
lint-agent> /exit
```

## 用户、Thread 和数据保存位置

CLI 不再保存“上一次 thread”的本地状态文件。thread 的选择规则如下：

- 没有传 `--thread-id`：每次启动生成新的 UUID thread。
- 传了 `--thread-id`：继续这个 thread，前提是它是合法 UUID。
- 交互模式下 `/new`：生成新的 UUID thread。
- 交互模式下 `/resume <thread_id>`：切换到已有 thread。

`user_id` 的选择规则如下：

- 没有传 `--user-id`：默认是 `cli:<USERNAME>`，例如 `cli:Tom12`。
- 传了 `--user-id tom`：使用 `tom`。
- CLI 会把 `source=lint-agent-cli`、`assistant=lint`、`user_id`、`authenticated` 写入 LangGraph thread metadata，方便 `/threads` 按当前用户筛选。

当前本地 `langgraph dev` 启动方式下，LangGraph Agent Server 的运行数据由 LangGraph Server 管理，默认落在项目根目录：

```text
D:\mcp\lint_agent\.langgraph_api\
```

Docker 启动方式下，该目录挂载到客户包根目录：

```text
customer-data/langgraph_api/
```

其中包括本地 dev server 的 thread、run、checkpoint、store 等运行时数据。这个目录是运行产物，已在 `.gitignore` 中排除。

长期记忆使用 LangGraph store，命名空间定义在 `memory/long_term.py`：

```text
("users", user_id)                       # 用户 profile，key 为 profile
("users", user_id, "memories")           # 用户长期记忆条目，key 为 memory_id
```

对于 CLI，`user_id` 默认是 `cli:<USERNAME>`，所以当前用户的长期记忆会挂在这个用户命名空间下。Agent 不应把源码、lint 原始报告、大段工具输出、密码或 API key 写入长期记忆。

注意区分两条入口：

- `langgraph_server/`：使用 LangGraph Server 自己的 thread/run/store 管理，本地 dev 数据在 `.langgraph_api/`。
- `chat_app.py` Chainlit：通过 `agent_runtime/checkpointer.py` 和 `memory/long_term.py::build_memory_store()` 使用 `.env` 中的 PostgreSQL 配置，例如 `CHECKPOINTER_DB_URI`、`MEMORY_STORE_DB_URI`，同时 Chainlit 历史会话使用 `DATABASE_URL`。

## SDK 接口取舍

`lint_agent_cli.py` 使用 `langgraph_sdk.get_sync_client()` 连接 Agent Server。当前只暴露对本项目有实际价值的 SDK 能力：

| SDK 资源 | 已使用接口 | 用途 |
| --- | --- | --- |
| `runs` | `wait` | 提交用户消息并等待结果；支持 HITL resume |
| `runs` | `list`、`get`、`cancel` | 查看和 interrupt 当前 thread 的 run |
| `threads` | `create`、`update` | 预创建 thread 并写入 CLI metadata |
| `threads` | `search`、`get` | 查看 thread 列表和 metadata |
| `threads` | `get_state`、`get_history` | 查看当前 thread 状态和 checkpoint 历史 |
| `assistants` | `search`、`get` | 查找 graph 对应的 assistant UUID，并查看 metadata |
| `assistants` | `get_graph`、`get_schemas` | 调试 graph 结构和 schema |

暂未暴露的接口：

- `store.put_item/get_item/delete_item/search_items/list_namespaces`：长期记忆应由 agent 工具按命名空间管理，CLI 不直接绕过 agent 修改 store。
- `crons.*`：当前项目没有定时任务需求。
- `assistants.create/update/delete/set_latest/get_versions`：本地 graph 由 `langgraph.json` 管理，不在 CLI 中做 assistant 发布管理。
- `threads.delete/copy/prune/update_state`：这些接口有破坏性或会绕过正常 run/checkpoint 流程，当前 CLI 不提供。
- `runs.create/create_batch/join/join_stream/delete`：当前同步 CLI 以 `runs.wait` 为主，保证一次输入对应一个完整输出。

这个取舍保持 CLI 接近官方 thread/run/assistant 模型，同时避免引入不必要的管理面复杂度。

## 运行时结构

`langgraph_server/agent_runtime.py` 启动时会做以下事情：

1. 应用 LangGraph 兼容补丁：
   - `LINT_AGENT_PATCH_LANGGRAPH_SEND=true`
   - `LINT_AGENT_PATCH_LANGGRAPH_DEV_PERSISTENCE=true`
2. 读取 `.env` 和 `config.py`。
3. 根据默认 LLM preset 构建 chat model。
4. 通过 `agent_runtime.tools.load_agent_tools()` 加载：
   - FastMCP 工具，stdio 命令为 `python -m mcp_server.server`
   - 内置硬件参考 RAG 工具
   - Tavily 搜索工具，仅配置 `TAVILY_API_KEY` 时启用
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

LLM、MCP session 和工具会在 Agent Server 进程内缓存，避免每个请求重复初始化。首次加载可能较慢，后续调用应复用缓存。

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

## Agent Chat UI

官方 Agent Chat UI 可以直接连接本服务。

- Hosted UI: `https://agentchat.vercel.app`
- Deployment URL: `http://127.0.0.1:2024`
- Graph ID: `lint`
- LangSmith API Key: 本地使用可留空

当前实现兼容不传自定义 context 的 Agent Chat UI。长期记忆工具会优先使用显式 `AgentContext`，否则从 LangGraph runtime metadata 中推导 thread/user 信息。

## ALINT-PRO Console 调用

`D:\software\ALINT-PRO\runalintprocon.bat` 打开的是 ALINT-PRO Tcl console，不是 PowerShell/cmd，因此不能直接依赖 Windows `PATH`。

在 ALINT-PRO console 中先加载 Tcl 包装：

```tcl
source D:/Downloads/alint-pro-customer/lint_agent/langgraph_server/lint_agent_alint_console.tcl
```

如果安装目录不同，把路径替换为实际客户包路径。客户包里的 Tcl 包装直接通过 Tcl 标准 `http` 包调用 Docker 中的 LangGraph Server，不要求 EDA 工作站安装 Python、`langgraph-sdk` 或本项目 Python 依赖。

ALINT-PRO Tcl console 的特点和 PowerShell/cmd 不同：

- console 输入由 ALINT-PRO/Tcl 解释器接管，不适合作为外部交互式进程的 stdin。
- Tcl namespace 变量会在当前 ALINT-PRO console 进程内保留，适合保存当前 `thread_id`。
- 适合的模式是 Tcl 自己实现对话循环，每轮通过 HTTP 调用 LangGraph Server。

因此 Tcl 包装不运行 Python REPL，也不调用 `lint_agent_cli.py`，而是在 Tcl 层提供 `lint-agent>` 对话循环，并保存当前 `thread_id`。每一轮输入都会把同一个 UUID thread 通过 HTTP API 传给 LangGraph Server，从而继续同一条对话。

常用调用：

```tcl
lint-agent
lint-agent "分析当前工程"
lint-agent -new "开启一个新会话并提问"
lint-agent -thread 11111111-1111-1111-1111-111111111111 "在指定 thread 中提问"
```

裸 `lint-agent` 会进入和普通终端一致的对话模式：

```text
lint-agent interactive mode
lint-agent> 你的问题
lint-agent> /thread
lint-agent> /exit
```

带 prompt 的 `lint-agent "..."` 仍是非阻塞一次性调用：命令会立刻返回 ALINT-PRO 的 `>` 提示符，Tcl 后台 HTTP 请求调用 `http://127.0.0.1:2024`，完成后再把结果打印回同一个 console。

裸 `lint-agent` 的每一轮对话也使用 Tcl 异步 HTTP 请求执行，Tcl 侧通过 `vwait` 等待本轮完成。这样用户输入会先立即显示为 `user: ...`，智能体回答会显示为 `assistant: ...`，ALINT-PRO 的 Tcl 事件循环仍可处理后台完成回调；为了保持同一条 thread 的消息顺序，下一轮输入会等当前回答结束后再出现提示符。

会话管理命令：

| Tcl 命令 | 作用 |
| --- | --- |
| `lint-agent` | 进入 `lint-agent>` 对话模式 |
| `lint-agent-help` | 查看 Tcl 包装命令 |
| `lint-agent-new` | 切换到新的空 thread |
| `lint-agent-thread` | 查看当前 `thread_id` 和 `user_id` |
| `lint-agent-resume <thread_id>` | 切换到已有 thread |
| `lint-agent-threads ?all? ?limit?` | 通过 HTTP API 查看 threads |
| `lint-agent-thread-info` | 查看当前 thread metadata |
| `lint-agent-state` | 查看当前 thread state 摘要 |
| `lint-agent-history ?limit?` | 查看 checkpoint history |
| `lint-agent-runs ?limit?` | 查看当前 thread 的 runs |
| `lint-agent-assistant` | 查看 assistant metadata |
| `lint-agent-graph` | 查看 assistant graph JSON |
| `lint-agent-schemas` | 查看 assistant schemas JSON，输出通常较长 |
| `lint-agent-user ?user_id?` / `lint-agent-user -default` | 查看或设置传给 LangGraph Server 的 user_id |
| `lint-agent-url ?url?` | 查看或设置 Agent Server URL |
| `lint-agent-jobs` | 查看正在运行的后台任务 |

示例：

```tcl
lint-agent-thread
lint-agent "分析当前工程"
lint-agent-state
lint-agent-threads 10
lint-agent-resume 11111111-1111-1111-1111-111111111111
lint-agent "继续这个 thread"
```

裸 `lint-agent` 会按轮次串行等待回答，保证同一条 thread 的消息顺序。带 prompt 的 `lint-agent "..."` 是后台 HTTP 请求，适合一次性提问；不要对同一个 thread 同时发起多条长任务。

## 与 Chainlit 的关系

`langgraph_server/` 只负责 LangGraph Agent Server 方案，不替代 Chainlit 入口。

- Chainlit Web UI：根目录 `chat_app.py`
- LangGraph Agent Server：`langgraph_server/agent_runtime.py`
- 两边共享 `agent_runtime/`、`memory/`、`rag/`、`llm/`、`compat/` 等模块
- 两边生命周期不同：Chainlit 每个聊天会话有自己的 runtime owner；Agent Server 在进程内缓存 LLM/MCP/tools
- 两边持久化路径不同：Chainlit 主要依赖 `.env` 中的 PostgreSQL/Chainlit data layer；本地 Agent Server dev 模式依赖 `.langgraph_api/`

这种重复是有意保留的入口生命周期边界，不是业务逻辑重复。

## 兼容补丁

当前默认启用两个补丁：

```env
LINT_AGENT_PATCH_LANGGRAPH_SEND=true
LINT_AGENT_PATCH_LANGGRAPH_DEV_PERSISTENCE=true
```

用途：

- `LINT_AGENT_PATCH_LANGGRAPH_SEND`：递归清理 LangGraph `Send` 中嵌套的不可序列化运行时对象，避免 MCP/shell session 对象进入 checkpoint。
- `LINT_AGENT_PATCH_LANGGRAPH_DEV_PERSISTENCE`：仅用于 `langgraph dev` 本地 `.langgraph_api` 落盘时，清理不可 pickle 对象，避免 Windows + MCP stdio 下出现 `TextIOWrapper` pickle 异常。

如果未来升级 LangGraph 后官方已经修复，可以临时关闭验证：

```powershell
$env:LINT_AGENT_PATCH_LANGGRAPH_DEV_PERSISTENCE = "false"
$env:LINT_AGENT_PATCH_LANGGRAPH_SEND = "false"
D:\mcp\lint_agent\langgraph_server\start_langgraph_agent_server.cmd
```

## 测试与验证

语法检查：

```powershell
D:\software\Miniconda3\envs\mcp\python.exe -m py_compile D:\mcp\lint_agent\langgraph_server\lint_agent_cli.py
```

CLI 参数检查：

```powershell
D:\mcp\lint_agent\langgraph_server\lint-agent.cmd --help
```

真实 Agent Server 测试：

```powershell
D:\mcp\lint_agent\langgraph_server\start_langgraph_agent_server.cmd
```

另开一个 PowerShell：

```powershell
D:\mcp\lint_agent\langgraph_server\lint-agent.cmd --user-id test-user "请只回复 OK"
D:\mcp\lint_agent\langgraph_server\lint-agent.cmd --interactive --user-id test-user
```

交互模式中验证：

```text
/threads all 10
/thread-info
/state
/history 3
/runs 5
/assistants 5
/assistant
/graph
/schemas
/exit
```

## 常见问题

### Agent Server 不可达

如果 `lint-agent` 提示：

```text
Agent Server is not reachable at http://127.0.0.1:2024
```

先确认 `start_langgraph_agent_server.cmd` 已启动，并且日志中出现：

```text
API: http://127.0.0.1:2024
```

### thread_id 不合法

如果看到：

```text
thread_id must be a UUID. Use /threads to list valid thread IDs.
```

说明传入的 `--thread-id` 或 `/resume` 参数不是 UUID。先运行 `/threads` 查看已有 thread，再复制完整 UUID。

### 第一次调用很慢

如果看到：

```text
Slow graph load. Accessing graph 'lint' took ...
```

第一次调用通常正常，因为需要初始化 LLM、MCP stdio server、RAG、工具和 middleware。后续调用不应反复出现明显慢加载；如果每次都很慢，通常说明 Agent Server 进程被重启或缓存没有生效。

### MCP 工具加载失败

单独验证 MCP server：

```powershell
cd D:\mcp\lint_agent
python -m mcp_server.server
```

该命令会以前台 stdio server 方式启动，正常情况下会等待 MCP 客户端连接。

### 中文乱码

Windows 终端建议使用 UTF-8：

```powershell
chcp 65001
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

`lint-agent.cmd` 和 `start_langgraph_agent_server.cmd` 已设置 `PYTHONUTF8=1` 与 `PYTHONIOENCODING=utf-8`。
