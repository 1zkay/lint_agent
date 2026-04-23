"""
ALINT-PRO Chainlit 聊天应用（MCP Client + create_agent 版）

架构（基于 LangChain/LangGraph 官方 API）：
  LLM ←→ create_agent(langchain.agents) ←→ MCP Session(持久 stdio)

官方 API 对照：
  图构造 : create_agent(llm, tools, system_prompt=..., checkpointer=...)
          来源: https://docs.langchain.com/oss/python/langchain/agents
  流式   : agent.astream(stream_mode=["messages", "updates"], version="v2")
          来源: https://docs.langchain.com/oss/python/langchain/streaming
  Step   : step.input = ... / step.output = ... （官方字段）
          来源: https://docs.chainlit.io/api-reference/step-class
  审批   : HumanInTheLoopMiddleware + Command(resume=...)
          来源: https://docs.langchain.com/oss/python/langchain/human-in-the-loop
  MCP    : AsyncExitStack + client.session("name") + load_mcp_tools(session)
          来源: https://github.com/langchain-ai/langchain-mcp-adapters README
  状态写回: agent.aupdate_state(config={"configurable":{"thread_id":...}}, values={"messages":[...]})
          来源: https://docs.langchain.com/oss/python/langgraph/persistence

多轮历史：由 checkpointer（postgres/memory）按 thread_id 自动管理。
  每轮只传当前 HumanMessage；history（含 ToolMessage）由 checkpointer 追加累积。
  create_agent 内部正确处理多轮 ToolMessage，不会产生无限循环。

启动：
  chainlit run chat_app.py -w
"""
import atexit
import asyncio
import hmac
import logging
import os
import socket
import subprocess
import sys
import time
import uuid
from copy import copy
from contextlib import AsyncExitStack, aclosing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import urlparse

# Windows + psycopg async 兼容：需使用 SelectorEventLoopPolicy
if os.name == "nt":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent
os.environ.setdefault("CHAINLIT_APP_ROOT", str(PROJECT_ROOT))

# 将当前脚本所在目录加入模块搜索路径，确保同目录的自定义模块（如 reflection_middleware）可导入
sys.path.insert(0, str(PROJECT_ROOT))

import chainlit as cl
from chainlit.chat_context import chat_context
from chainlit.chat_settings import ChatSettings
from chainlit.input_widget import Select
from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    HostExecutionPolicy,
    ModelRetryMiddleware,
    ShellToolMiddleware,
    SummarizationMiddleware,
    TodoListMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.middleware.todo import WRITE_TODOS_SYSTEM_PROMPT
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.skills import SkillsMiddleware
from reflection_middleware import ReflectionMiddleware
from agentic_rag import build_hardware_reference_agentic_rag_tool
from llm_factory import build_chat_model_from_config
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langchain_community.tools import RequestsGetTool
from langchain_community.utilities.requests import TextRequestsWrapper
from langchain_tavily import TavilySearch
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import (
    Command,
    Interrupt,
    MessagesStreamPart,
    StreamPart,
    UpdatesStreamPart,
)
from chainlit.types import ThreadDict
from chainlit.data.chainlit_data_layer import ChainlitDataLayer

from config import config
from long_term_memory import (
    AgentContext,
    MEMORY_SYSTEM_PROMPT,
    build_memory_store,
    build_memory_tools,
)

logger = logging.getLogger(__name__)

_FILESYSTEM_TOOL_NAMES = ("ls", "read_file", "write_file", "edit_file", "glob", "grep")

_LOCAL_MINIO_PROCESS: subprocess.Popen | None = None


def _is_loopback_host(host: str | None) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def _can_connect(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _start_local_minio_if_needed() -> None:
    global _LOCAL_MINIO_PROCESS

    if os.name != "nt":
        return

    if not config.local_minio_auto_start:
        return

    if _LOCAL_MINIO_PROCESS and _LOCAL_MINIO_PROCESS.poll() is None:
        return

    parsed = urlparse((config.s3_endpoint_url or "").strip())
    host = parsed.hostname
    if not _is_loopback_host(host):
        return

    port = parsed.port or 9000
    if _can_connect(host or "127.0.0.1", port):
        return

    if not config.local_minio_exe:
        logger.warning("[chat_app] LOCAL_MINIO_EXE is empty; skip local MinIO auto-start")
        return
    if not config.local_minio_data_dir:
        logger.warning("[chat_app] LOCAL_MINIO_DATA_DIR is empty; skip local MinIO auto-start")
        return

    local_minio_exe = Path(config.local_minio_exe)
    local_minio_data_dir = Path(config.local_minio_data_dir)

    if not local_minio_exe.exists():
        logger.warning("[chat_app] Local MinIO executable not found: %s", local_minio_exe)
        return

    local_minio_data_dir.mkdir(parents=True, exist_ok=True)

    minio_env = os.environ.copy()
    if config.s3_access_key:
        minio_env["MINIO_ROOT_USER"] = config.s3_access_key
    if config.s3_secret_key:
        minio_env["MINIO_ROOT_PASSWORD"] = config.s3_secret_key

    try:
        _LOCAL_MINIO_PROCESS = subprocess.Popen(
            [
                str(local_minio_exe),
                "server",
                str(local_minio_data_dir),
                "--address",
                f":{port}",
                "--console-address",
                f":{config.local_minio_console_port}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=minio_env,
        )
    except Exception as e:
        logger.warning(f"[chat_app] Failed to start local MinIO: {e}")
        _LOCAL_MINIO_PROCESS = None
        return

    deadline = time.monotonic() + config.local_minio_start_timeout
    while time.monotonic() < deadline:
        if _can_connect(host or "127.0.0.1", port):
            logger.info("[chat_app] Local MinIO started")
            return
        time.sleep(0.5)

    logger.warning("[chat_app] Timed out waiting for local MinIO port to open")


def _stop_local_minio_if_owned() -> None:
    global _LOCAL_MINIO_PROCESS

    proc = _LOCAL_MINIO_PROCESS
    _LOCAL_MINIO_PROCESS = None
    if not proc or proc.poll() is not None:
        return

    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


atexit.register(_stop_local_minio_if_owned)


# ── 源头治理：递归清理 Send.arg 中不可序列化的中间件状态 ──────────────
# 背景：create_agent 的 model_to_tools 边会生成
#   Send("tools", ToolCallWithContext(state=state))
# 其中 state 是完整 agent 状态，含 ShellToolMiddleware 等中间件注入的不可
# 序列化对象（如 _SessionResources），导致 checkpointer 写入时触发
#   "Type is not msgpack serializable: Send" 错误。
#
# langgraph 已有 sanitize_untracked_values_in_send 但只过滤顶层 key，
# 无法处理 ToolCallWithContext.state 内的嵌套字段。
# 以下 monkey-patch 等效于官方待合并 PR：
#   - langgraph#6794  （递归 sanitize UntrackedValue）
#   - langchain#34500 （Send 创建时过滤不可序列化 state）
# 相关 issue: langchain#34490, langgraph#5891, langgraph#6789
# TODO: langgraph 合并 PR#6794 后可移除此补丁。
def _apply_recursive_send_sanitization() -> None:
    """Monkey-patch sanitize_untracked_values_in_send 为递归版本。"""
    try:
        from langgraph.pregel import _algo
        from langgraph.channels.untracked_value import UntrackedValue
        from langgraph.types import Send as _Send

        def _recursive_filter(obj, channels):
            if isinstance(obj, dict):
                return {
                    k: _recursive_filter(v, channels)
                    for k, v in obj.items()
                    if not isinstance(channels.get(k), UntrackedValue)
                }
            if isinstance(obj, list):
                return [_recursive_filter(item, channels) for item in obj]
            return obj

        def _patched_sanitize(packet, channels):
            if not isinstance(packet.arg, dict):
                return packet
            sanitized_arg = _recursive_filter(packet.arg, channels)
            return _Send(node=packet.node, arg=sanitized_arg)

        _algo.sanitize_untracked_values_in_send = _patched_sanitize

        # _loop 通过 from ... import 持有独立引用，也必须替换，否则运行时仍走旧函数
        from langgraph.pregel import _loop
        _loop.sanitize_untracked_values_in_send = _patched_sanitize

        logger.info("[chat_app] 已应用递归 Send 清理补丁（等效 langgraph#6794）")
    except Exception as e:
        logger.warning(f"[chat_app] 递归 Send 清理补丁应用失败: {e}")


_apply_recursive_send_sanitization()

# MCP Server 路径
_MCP_SERVER = str(Path(__file__).parent / "mcp_lint.py")

def _to_project_virtual_path(path: Path) -> str | None:
    """Convert a local path into the POSIX virtual path used by deepagents."""
    resolved = path.resolve()
    try:
        relative_path = resolved.relative_to(PROJECT_ROOT)
    except ValueError:
        return None
    return "/" if not relative_path.parts else "/" + relative_path.as_posix()


def _normalize_skill_sources(sources: list[str]) -> list[str]:
    normalized_sources: list[str] = []
    for source in sources:
        raw = source.strip()
        if not raw:
            continue

        if raw.startswith("/"):
            posix_path = PurePosixPath(raw.lstrip("/"))
            normalized_sources.append("/" if not posix_path.parts else f"/{posix_path.as_posix()}")
            continue

        virtual_source = _to_project_virtual_path((PROJECT_ROOT / raw).resolve())
        if virtual_source:
            normalized_sources.append(virtual_source.rstrip("/") or "/")

    seen: set[str] = set()
    deduped_sources: list[str] = []
    for source in normalized_sources:
        if source in seen:
            continue
        seen.add(source)
        deduped_sources.append(source)
    return deduped_sources


def _build_human_message_from_chainlit_message(message: cl.Message) -> HumanMessage:
    """Convert Chainlit uploads into a text message with workspace file references."""
    user_text = str(message.content or "").strip()
    message_id = str(getattr(message, "id", "") or "")
    elements = list(getattr(message, "elements", []) or [])

    if not elements:
        return HumanMessage(content=user_text, id=message_id or None)

    attachment_notes: list[str] = []
    for idx, elem in enumerate(elements, start=1):
        path_str = str(getattr(elem, "path", "") or "").strip()
        name = str(getattr(elem, "name", "") or "").strip() or f"upload_{idx}"
        mime = str(getattr(elem, "mime", "") or "").strip().lower() or "unknown"

        if not path_str:
            attachment_notes.append(f"- `{name}`: no local path (mime={mime})")
            continue

        p = Path(path_str)
        if not p.exists():
            attachment_notes.append(f"- `{name}`: file not found (path={path_str})")
            continue

        abs_path = str(p.resolve())
        tool_path = _to_project_virtual_path(p)
        if tool_path:
            attachment_notes.append(f"- `{name}`: use `{tool_path}` (mime={mime})")
        else:
            attachment_notes.append(
                f"- `{name}`: outside workspace ({abs_path}, mime={mime})"
            )

    text_parts: list[str] = []
    if user_text:
        text_parts.append(user_text)
    if attachment_notes:
        text_parts.append(
            "[attachment index]\n"
            + "\n".join(attachment_notes)
            + "\n\nUse `read_file` to inspect uploaded files. Use `ls`, `glob`, or `grep` when you need to discover paths or search within the workspace."
        )
    elif not text_parts:
        text_parts.append("[user sent attachments, but no accessible file paths were available]")

    return HumanMessage(content="\n\n".join(text_parts), id=message_id or None)

def _build_langchain_message_from_chainlit_history_message(message: cl.Message):
    message_type = str(getattr(message, "type", "") or "")
    message_id = str(getattr(message, "id", "") or "")
    content = str(getattr(message, "content", "") or "")

    if message_type == "user_message":
        return _build_human_message_from_chainlit_message(message)
    if message_type == "assistant_message":
        return AIMessage(content=content, id=message_id or None)
    if message_type == "system_message":
        return SystemMessage(content=content, id=message_id or None)
    return None


def _chainlit_history_before_message(current_message_id: str) -> list[cl.Message]:
    history = list(chat_context.get())
    if not current_message_id:
        return history
    for idx, item in enumerate(history):
        if str(getattr(item, "id", "") or "") == current_message_id:
            return history[:idx]
    return history


def _extract_seen_user_message_ids_from_thread(thread: ThreadDict) -> list[str]:
    seen_ids: list[str] = []
    for step in list(thread.get("steps", []) or []):
        if str(step.get("type", "") or "") != "user_message":
            continue
        step_id = str(step.get("id", "") or "").strip()
        if step_id:
            seen_ids.append(step_id)
    return seen_ids


async def _reset_agent_history_from_chainlit_context(
    agent: Any,
    thread_id: str,
    current_message: cl.Message,
) -> None:
    prior_messages = []
    for item in _chainlit_history_before_message(str(getattr(current_message, "id", "") or "")):
        lc_message = _build_langchain_message_from_chainlit_history_message(item)
        if lc_message is not None:
            prior_messages.append(lc_message)

    await agent.aupdate_state(
        config={"configurable": {"thread_id": thread_id}},
        values={
            "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *prior_messages],
            "todos": [],
        },
    )

    task_list = cl.user_session.get("task_list")
    if task_list:
        task_list.tasks.clear()
        task_list.status = "Ready"
        await task_list.send()

if config.chainlit_enable_password_auth:
    @cl.password_auth_callback
    async def _password_auth_callback(username: str, password: str):
        """
        官方标准所需：启用认证后，Chainlit 才能展示并恢复聊天历史。
        凭据统一由 config.py 管理。
        """
        if not config.chainlit_auth_username or not config.chainlit_auth_password:
            logger.warning(
                "[chat_app] CHAINLIT_ENABLE_PASSWORD_AUTH=true 但未配置认证用户名/密码。"
            )
            return None
        ok = hmac.compare_digest(username, config.chainlit_auth_username) and hmac.compare_digest(password, config.chainlit_auth_password)
        if not ok:
            return None
        return cl.User(identifier=config.chainlit_auth_username, metadata={"auth_provider": "password"})


class AppChainlitDataLayer(ChainlitDataLayer):
    """
    使用 Chainlit 官方 @cl.data_layer 扩展点，兼容本项目运行时的已知数据层差异。

    说明：
    - 这是官方支持的自定义数据层方式；
    - 兼容 update_thread 的 metadata 为空场景；
    - 兼容 Step 时间戳字符串不是 UTC ...Z 格式时，官方 create_step 的严格解析。
    """

    @staticmethod
    def _normalize_chainlit_timestamp(value):
        """将时间值统一规范为 Chainlit 数据层可解析的 UTC ...Z 格式。"""
        if value is None:
            return None

        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            return text

        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return text

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @classmethod
    def _normalize_step_timestamps(cls, step_dict: dict):
        """统一规范 StepDict 中的 createdAt/start/end 字段格式。"""
        normalized = dict(step_dict)
        for field in ("createdAt", "start", "end"):
            if field in normalized:
                normalized[field] = cls._normalize_chainlit_timestamp(normalized.get(field))
        return normalized

    async def create_step(self, step_dict):
        return await super().create_step(self._normalize_step_timestamps(step_dict))

    async def update_step(self, step_dict):
        return await super().update_step(self._normalize_step_timestamps(step_dict))

    async def update_thread(
        self,
        thread_id: str,
        name: str | None = None,
        user_id: str | None = None,
        metadata: dict | None = None,
        tags: list[str] | None = None,
    ):
        safe_metadata = {} if metadata is None else metadata
        return await super().update_thread(
            thread_id=thread_id,
            name=name,
            user_id=user_id,
            metadata=safe_metadata,
            tags=tags,
        )


def _build_chainlit_storage_client():
    """
    对齐 Chainlit 官方 Data Layer 的 S3 自动装配逻辑：
    - 通过 BUCKET_NAME + APP_AWS_* 启用 S3StorageClient
    - 本地对象存储可用 DEV_AWS_ENDPOINT 指向 LocalStack/MinIO 兼容端点
    """
    if not (config.s3_bucket_name and config.s3_region and config.s3_access_key and config.s3_secret_key):
        return None

    _start_local_minio_if_needed()

    try:
        from chainlit.data.storage_clients.s3 import S3StorageClient

        return S3StorageClient(
            bucket=config.s3_bucket_name,
            region_name=config.s3_region,
            aws_access_key_id=config.s3_access_key,
            aws_secret_access_key=config.s3_secret_key,
            endpoint_url=config.s3_endpoint_url,
        )
    except Exception as e:
        logger.warning(f"[chat_app] 初始化 Chainlit S3StorageClient 失败，将不上传附件: {e}")
        return None


if config.chainlit_database_url:
    @cl.data_layer
    def _app_data_layer():
        """Chainlit 官方数据层注册入口：配置 PostgreSQL + 可选 S3 存储。"""
        db_url = config.chainlit_database_url
        storage_client = _build_chainlit_storage_client()
        if storage_client:
            logger.info("[chat_app] Chainlit data layer storage client enabled (S3-compatible)")
        else:
            logger.info("[chat_app] Chainlit data layer storage client disabled")
        return AppChainlitDataLayer(database_url=db_url, storage_client=storage_client)

# ─────────────────────────────────────────────────────────────────────────────
# 系统提示词
# ─────────────────────────────────────────────────────────────────────────────

# TodoListMiddleware 增强 prompt：官方默认 + 即时更新 + 输出纪律
_WRITE_TODOS_ENHANCED_PROMPT = WRITE_TODOS_SYSTEM_PROMPT + """
## Critical: Real-Time Todo Updates
- You MUST call `write_todos` IMMEDIATELY after completing each individual task — do NOT batch multiple completions into one call.
- The correct per-task cycle is: mark task `in_progress` → do the work → mark task `completed` (and mark next task `in_progress`) → move on.
- Each `write_todos` call updates the UI in real time. If you skip intermediate calls, the user sees stale progress.

## Critical: Final Answer Placement — One Turn, One Response
When you are ready to deliver the final answer to the user, you MUST follow this exact pattern in a **single LLM turn**:
1. Write your complete, polished final answer as the text of your message.
2. In that **same turn**, call `write_todos` to mark all remaining tasks as `completed`.

**After that turn, output nothing further.** Do NOT add a follow-up turn with phrases like "Done!", "Task complete.", "Let me know if you need anything else.", or any other closing remarks. The turn that contains both the final answer and the final `write_todos` call is the last turn — stop there.

Why this matters: the system displays the text from the write_todos turn as the user-visible response. Any text produced in a subsequent "termination" turn will either overwrite or conflict with the real answer, causing the user to see incomplete or low-quality output.
"""

_SYSTEM_PROMPT = """
你是一位资深 Verilog/SystemVerilog 硬件设计专家。
""".strip() + "\n\n" + MEMORY_SYSTEM_PROMPT


# ─────────────────────────────────────────────────────────────────────────────
# LLM 工厂
# ─────────────────────────────────────────────────────────────────────────────
def _find_llm_preset_by_id(preset_id: str | None) -> dict[str, str] | None:
    preset_id = str(preset_id or "").strip()
    for preset in config.llm_model_presets:
        if preset.get("id") == preset_id:
            return preset
    return None


def _resolve_llm_preset_id(preset_id: str | None = None) -> str:
    preset_id = str(preset_id or "").strip()
    if preset_id and _find_llm_preset_by_id(preset_id):
        return preset_id
    return config.llm_model_preset_default


def _build_runtime_config_for_llm_preset(preset_id: str | None = None):
    runtime_cfg = copy(config)
    preset = _find_llm_preset_by_id(_resolve_llm_preset_id(preset_id))
    if preset:
        runtime_cfg.llm_model = preset["model"]
        runtime_cfg.llm_base_url = preset["base_url"]
        runtime_cfg.llm_api_key = preset["api_key"]
    return runtime_cfg


def _build_llm_for_runtime_config(runtime_cfg: Any):
    """根据 runtime config 构建 LangChain chat model；未配置返回 None。"""
    if not runtime_cfg.llm_model:
        return None
    try:
        llm = build_chat_model_from_config(
            runtime_cfg,
            logger=logger,
            log_prefix="[chat_app]",
        )
        logger.info(f"[chat_app] LLM loaded: {runtime_cfg.llm_model}")
        return llm
    except Exception as e:
        logger.error(f"[chat_app] LLM 初始化失败: {e}")
        return None


async def _send_model_chat_settings() -> None:
    presets = list(config.llm_model_presets or [])
    if len(presets) <= 1:
        return

    current_preset_id = _resolve_llm_preset_id(cl.user_session.get("llm_preset_id"))
    items = {preset["label"]: preset["id"] for preset in presets}
    await ChatSettings(
        [
            Select(
                id="llm_preset",
                label="模型",
                items=items,
                initial_value=current_preset_id,
                tooltip="仅切换主聊天模型；内置参考文档 RAG 继续使用后端默认配置。",
                description="修改后会重建当前会话运行时。",
            )
        ]
    ).send()


# ─────────────────────────────────────────────────────────────────────────────
# Checkpointer 工厂（开发/生产统一入口）
# ─────────────────────────────────────────────────────────────────────────────
async def _build_checkpointer(exit_stack: AsyncExitStack):
    """
    按配置创建 LangGraph checkpointer（PostgreSQL 标准实现）。

    优先级：
      1) postgres（若配置且依赖可用）
      2) InMemorySaver（回退）
    """
    backend = (config.checkpointer_backend or "postgres").lower()
    db_uri = (config.checkpointer_db_uri or "").strip()

    if backend == "memory":
        logger.info("[chat_app] Checkpointer: InMemorySaver")
        return InMemorySaver()

    if backend != "postgres":
        logger.warning(f"[chat_app] 不支持的 CHECKPOINTER_BACKEND={backend}，按 postgres 处理。")

    if not db_uri:
        logger.warning("[chat_app] 未配置 CHECKPOINTER_DB_URI（PostgreSQL 连接串），回退 InMemorySaver。")
        return InMemorySaver()

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # type: ignore
    except Exception as e:
        logger.warning(
            f"[chat_app] 未安装 AsyncPostgresSaver 依赖（需要 langgraph-checkpoint-postgres/psycopg），回退 InMemorySaver。错误: {e}"
        )
        return InMemorySaver()

    try:
        checkpointer = await exit_stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(db_uri)
        )
        if config.checkpointer_auto_setup:
            await checkpointer.setup()
        logger.info(f"[chat_app] Checkpointer: AsyncPostgresSaver ({db_uri})")
        return checkpointer
    except Exception as e:
        logger.error(f"[chat_app] 初始化 AsyncPostgresSaver 失败，回退 InMemorySaver: {e}", exc_info=True)
        return InMemorySaver()


def _build_tool_approval_middleware():
    """
    使用 LangChain 官方 HumanInTheLoopMiddleware 为高风险工具增加审批。
    仅拦截“写入/删除/移动/复制”类工具，读取类工具默认放行。
    """
    if not config.agent_tool_approval_enabled:
        return [], []

    # 直接使用配置中的工具名。
    # HumanInTheLoopMiddleware 对不在 interrupt_on 中的工具自动放行，
    # 因此中间件注入的工具（如 shell）也能被正确拦截。
    guarded_tools = list(config.agent_approval_tool_names)
    if not guarded_tools:
        return [], []

    interrupt_on = {
        name: {"allowed_decisions": ["approve", "reject"]}
        for name in guarded_tools
    }
    middleware = [
        HumanInTheLoopMiddleware(
            interrupt_on=interrupt_on,
            description_prefix="检测到高风险工具调用，请审批后继续执行。",
        )
    ]
    return middleware, guarded_tools


def _extract_hitl_request_from_interrupts(interrupts):
    """从 LangGraph v2 Interrupt 元组中提取 HITL 中断请求。"""
    if not interrupts:
        return None
    first = interrupts[0]
    if isinstance(first, Interrupt) and isinstance(first.value, dict):
        return first.value
    return None


def _extract_decision_from_action_result(action_result) -> str:
    """兼容 Chainlit AskActionMessage 返回结构，提取决策类型。"""
    payload = {}
    if isinstance(action_result, dict):
        payload = action_result.get("payload") or {}
    elif action_result is not None and hasattr(action_result, "payload"):
        payload = getattr(action_result, "payload") or {}
    decision = str(payload.get("decision") or "reject").strip().lower()
    if decision not in ("approve", "reject"):
        return "reject"
    return decision


def _build_decision_actions(allowed_decisions: list[str]) -> list:
    """根据 allowed_decisions 生成 AskActionMessage 按钮。"""
    action_map = {
        "approve": cl.Action(
            name="hitl_decision",
            payload={"decision": "approve"},
            label="✅ 批准",
            tooltip="批准并继续执行工具调用",
        ),
        "reject": cl.Action(
            name="hitl_decision",
            payload={"decision": "reject"},
            label="❌ 拒绝",
            tooltip="拒绝该工具调用",
        ),
    }
    actions = []
    for name in allowed_decisions:
        if name in action_map:
            actions.append(action_map[name])
    if not actions:
        actions.append(action_map["reject"])
    return actions


async def _ask_hitl_resume_payload(hitl_request: dict):
    """
    使用 Chainlit AskActionMessage 按 action_request 顺序收集决策，
    返回 {"decisions": [...]} 字典，供调用方包装为 Command(resume=...)。

    对齐官方 HITL 语义：
    - decisions 的数量与顺序必须与 action_requests 一致。
    """
    action_requests = (hitl_request or {}).get("action_requests") or []
    review_configs = (hitl_request or {}).get("review_configs") or []

    if not action_requests:
        return {"decisions": [{"type": "reject", "message": "审批请求无效，已拒绝。"}]}

    decisions = []
    for idx, req in enumerate(action_requests, start=1):
        name = req.get("name", "tool")
        current_args = req.get("args", {})
        description = req.get("description", "")

        cfg = review_configs[idx - 1] if idx - 1 < len(review_configs) else {}
        allowed_decisions = cfg.get("allowed_decisions") or ["approve", "reject"]
        if not isinstance(allowed_decisions, list):
            allowed_decisions = ["approve", "reject"]
        allowed_decisions = [str(x).strip().lower() for x in allowed_decisions]
        actions = _build_decision_actions(allowed_decisions)

        content_lines = [
            f"审批请求 {idx}/{len(action_requests)}",
            f"工具: `{name}`",
            f"参数: `{str(current_args)[:800]}`",
        ]
        if description:
            content_lines.append(f"说明: {description}")
        content_lines.append("请选择处理方式。")

        ask = await cl.AskActionMessage(
            content="\n".join(content_lines),
            actions=actions,
            timeout=config.agent_hitl_timeout,
            raise_on_timeout=False,
        ).send()
        decision = _extract_decision_from_action_result(ask)
        if decision not in allowed_decisions:
            decision = "reject"

        if decision == "approve":
            decisions.append({"type": "approve"})
            continue

        decisions.append({"type": "reject", "message": "用户拒绝执行该工具调用。"})

    # 官方约束：数量必须一致；若不一致，兜底全拒绝避免中断状态卡死
    if len(decisions) != len(action_requests):
        logger.error(
            "[chat_app] HITL decisions length mismatch: decisions=%s action_requests=%s",
            len(decisions),
            len(action_requests),
        )
        decisions = [
            {"type": "reject", "message": "审批决策数量异常，系统已拒绝该工具调用。"}
            for _ in action_requests
        ]
    return {"decisions": decisions}


# ─────────────────────────────────────────────────────────────────────────────
# Chainlit 生命周期
# ─────────────────────────────────────────────────────────────────────────────

def _get_chainlit_thread_id_fallback() -> str:
    """
    获取当前 Chainlit 线程 ID（官方会话线程标识）。
    若获取失败则回退到随机 UUID。
    """
    try:
        from chainlit.context import context
        tid = getattr(getattr(context, "session", None), "thread_id", None)
        if tid:
            return str(tid)
    except Exception:
        pass
    return str(uuid.uuid4())


def _resolve_agent_context(thread_id: str) -> AgentContext:
    """Build the per-run context required by long-term memory tools."""

    app_user = cl.user_session.get("user")
    identifier = str(getattr(app_user, "identifier", "") or "").strip()
    authenticated = bool(identifier)
    user_id = identifier or f"anonymous:{thread_id}"
    return AgentContext(
        user_id=user_id,
        thread_id=thread_id,
        authenticated=authenticated,
    )


def _clear_runtime_session_state() -> None:
    """Clear per-chat runtime objects stored in Chainlit user session."""

    cl.user_session.set("agent", None)
    cl.user_session.set("session", None)
    cl.user_session.set("llm", None)
    cl.user_session.set("memory_store", None)
    cl.user_session.set("runtime_task", None)
    cl.user_session.set("runtime_close_event", None)
    cl.user_session.set("runtime_id", None)


async def _stop_runtime_owner(*, wait: bool) -> None:
    """Ask the runtime owner task to close resources in its own task."""

    close_event = cl.user_session.get("runtime_close_event")
    runtime_task = cl.user_session.get("runtime_task")

    if close_event:
        close_event.set()

    if wait and runtime_task:
        try:
            await runtime_task
        except Exception as exc:
            logger.warning(f"[chat_app] Runtime owner close error: {exc}")


async def _run_chat_runtime_owner(
    *,
    runtime_id: str,
    llm: Any,
    runtime_cfg: Any,
    ready_future: asyncio.Future[dict[str, Any]],
    close_event: asyncio.Event,
) -> None:
    """Own the MCP/session resources so cleanup happens in the same task."""

    exit_stack = AsyncExitStack()
    try:
        client = MultiServerMCPClient(
            {
                "alint": {
                    "command": sys.executable,
                    "args":    [_MCP_SERVER],
                    "transport": "stdio",
                }
            }
        )
        session = await exit_stack.enter_async_context(client.session("alint"))
        mcp_tools = await load_mcp_tools(session)

        search_tools = [TavilySearch(max_results=5)] if os.getenv("TAVILY_API_KEY") else []
        fetch_url_tool = RequestsGetTool(
            requests_wrapper=TextRequestsWrapper(),
            allow_dangerous_requests=True,
            name="fetch_url",
            description="Fetch the content of a URL. Input should be a URL string (e.g. https://example.com). Returns the text content of the page.",
        )
        try:
            rag_tool = build_hardware_reference_agentic_rag_tool(config)
        except Exception as e:
            logger.warning(f"[chat_app] hardware-reference agentic RAG tool init failed: {e}")
            rag_tool = None
        rag_tools = [rag_tool] if rag_tool else []
        memory_tools = build_memory_tools()
        tools = [*mcp_tools, *rag_tools, *search_tools, fetch_url_tool, *memory_tools]
        tool_names = list(dict.fromkeys([*(t.name for t in tools), *_FILESYSTEM_TOOL_NAMES]))
        logger.info(f"[chat_app] MCP persistent session started, tools: {tool_names}")

        checkpointer = await _build_checkpointer(exit_stack)
        memory_store = await build_memory_store(config, exit_stack)
        hitl_middleware, approval_guarded_tools = _build_tool_approval_middleware()

        middleware_stack = []

        if config.agent_enable_todo:
            middleware_stack.append(
                TodoListMiddleware(
                    system_prompt=_WRITE_TODOS_ENHANCED_PROMPT,
                )
            )
            logger.info("[chat_app] TodoListMiddleware (Plan-and-Execute) enabled")

        normalized_skill_sources = _normalize_skill_sources(config.agent_skills_dirs)
        if config.agent_enable_skills and normalized_skill_sources:
            skills_backend = FilesystemBackend(
                root_dir=str(PROJECT_ROOT),
                virtual_mode=True,
            )
            skills_middleware = SkillsMiddleware(
                backend=skills_backend,
                sources=normalized_skill_sources,
            )
            middleware_stack.append(skills_middleware)
            logger.info(
                f"[chat_app] SkillsMiddleware enabled "
                f"(sources={normalized_skill_sources}, root_dir={PROJECT_ROOT})"
            )
        elif config.agent_enable_skills and config.agent_skills_dirs:
            logger.warning("[chat_app] SkillsMiddleware disabled: no skill sources resolved under %s", PROJECT_ROOT)

        middleware_stack.append(
            FilesystemMiddleware(
                backend=FilesystemBackend(
                    root_dir=str(PROJECT_ROOT),
                    virtual_mode=True,
                )
            )
        )
        logger.info("[chat_app] FilesystemMiddleware enabled (root_dir=%s, virtual_mode=True)", PROJECT_ROOT)

        if config.agent_enable_summarization:
            summarization_middleware = SummarizationMiddleware(
                model=llm,
                trigger=("tokens", config.agent_summarization_trigger_tokens),
                keep=("messages", config.agent_summarization_keep_messages),
            )
            middleware_stack.append(summarization_middleware)
            logger.info(
                f"[chat_app] SummarizationMiddleware enabled "
                f"(trigger={config.agent_summarization_trigger_tokens} tokens, "
                f"keep={config.agent_summarization_keep_messages} messages)"
            )

        if config.agent_enable_reflection:
            reflection_middleware = ReflectionMiddleware(
                model=llm,
                max_reflections=config.agent_reflection_max,
            )
            middleware_stack.append(reflection_middleware)
            logger.info(
                f"[chat_app] ReflectionMiddleware enabled "
                f"(max_reflections={config.agent_reflection_max})"
            )

        if config.agent_enable_model_retry:
            model_retry_middleware = ModelRetryMiddleware(
                max_retries=config.agent_model_retry_max,
            )
            middleware_stack.append(model_retry_middleware)
            logger.info(
                f"[chat_app] ModelRetryMiddleware enabled "
                f"(max_retries={config.agent_model_retry_max})"
            )

        if config.agent_enable_tool_retry:
            retry_middleware = ToolRetryMiddleware(
                max_retries=config.agent_tool_retry_max,
            )
            middleware_stack.append(retry_middleware)
            logger.info(f"[chat_app] ToolRetryMiddleware enabled (max_retries={config.agent_tool_retry_max})")

        if config.agent_enable_shell:
            if os.name == "nt":
                _git_bash = r"C:\Program Files\Git\bin\bash.exe"
                if os.path.isfile(_git_bash):
                    shell_command = (_git_bash,)
                else:
                    import shutil
                    _bash = shutil.which("bash")
                    if not _bash:
                        logger.warning(
                            "[chat_app] ShellToolMiddleware requires Git Bash on Windows. "
                            "Shell disabled."
                        )
                        config.agent_enable_shell = False
                    else:
                        shell_command = (_bash,)
            else:
                shell_command = ("/bin/bash",)
        if config.agent_enable_shell:
            _py_exe = sys.executable.replace("\\", "/")
            _shell_startup = (
                f'python()  {{ "{_py_exe}" "$@"; }}',
                f'python3() {{ "{_py_exe}" "$@"; }}',
                f'pip()     {{ "{_py_exe}" -m pip "$@"; }}',
            )

            shell_middleware = ShellToolMiddleware(
                workspace_root=config.shell_workspace_root or str(PROJECT_ROOT),
                shell_command=shell_command,
                startup_commands=_shell_startup,
                execution_policy=HostExecutionPolicy(
                    command_timeout=config.shell_command_timeout,
                    max_output_lines=config.shell_max_output_lines,
                ),
            )
            middleware_stack.append(shell_middleware)
            logger.info(
                f"[chat_app] ShellToolMiddleware enabled "
                f"(workspace={config.shell_workspace_root or PROJECT_ROOT}, "
                f"timeout={config.shell_command_timeout}s)"
            )

        middleware_stack.extend(hitl_middleware)

        agent = create_agent(
            llm,
            tools,
            system_prompt=_SYSTEM_PROMPT,
            middleware=middleware_stack,
            checkpointer=checkpointer,
            store=memory_store,
            context_schema=AgentContext,
        )
        if approval_guarded_tools:
            logger.info(f"[chat_app] Tool approval enabled for: {approval_guarded_tools}")

        cl.user_session.set("agent", agent)
        cl.user_session.set("session", session)
        cl.user_session.set("llm", llm)
        cl.user_session.set("memory_store", memory_store)

        if not ready_future.done():
            ready_future.set_result(
                {
                    "tool_names": tool_names,
                    "runtime_cfg": runtime_cfg,
                }
            )

        await close_event.wait()
    except Exception as exc:
        if not ready_future.done():
            ready_future.set_exception(exc)
        else:
            logger.warning(f"[chat_app] Runtime owner error: {exc}")
    finally:
        try:
            await exit_stack.aclose()
            logger.info("[chat_app] MCP session closed")
        except Exception as exc:
            logger.warning(f"[chat_app] MCP session close error: {exc}")
        finally:
            if cl.user_session.get("runtime_id") == runtime_id:
                _clear_runtime_session_state()


async def _initialize_chat_runtime(
    thread_id: str,
    *,
    send_intro: bool,
    llm_preset_id: str | None = None,
) -> None:
    """
    初始化 MCP Client（持久会话）+ create_agent，并绑定 LangGraph thread_id。
    新会话与恢复会话均走同一初始化链路，避免状态分叉。
    """
    await _stop_runtime_owner(wait=True)
    _clear_runtime_session_state()
    cl.user_session.set("thread_id", thread_id)
    agent_context = _resolve_agent_context(thread_id)
    cl.user_session.set("agent_context", agent_context)
    cl.user_session.set("user_id", agent_context.user_id)
    cl.user_session.set("authenticated", agent_context.authenticated)

    resolved_llm_preset_id = _resolve_llm_preset_id(llm_preset_id)
    runtime_cfg = _build_runtime_config_for_llm_preset(resolved_llm_preset_id)
    llm = _build_llm_for_runtime_config(runtime_cfg)
    cl.user_session.set("llm_preset_id", resolved_llm_preset_id)
    if not llm:
        await cl.Message(
            content=(
                "你好！我是 **ALINT-PRO Verilog 代码审查助手**。\n\n"
                "> ⚠️ 未配置 LLM（`LLM_MODEL` 环境变量为空），问答功能不可用。\n"
                "> 请在 `.env` 文件中填入 LLM 配置后重启。"
            )
        ).send()
        return

    runtime_id = str(uuid.uuid4())
    close_event = asyncio.Event()
    ready_future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    runtime_task = asyncio.create_task(
        _run_chat_runtime_owner(
            runtime_id=runtime_id,
            llm=llm,
            runtime_cfg=runtime_cfg,
            ready_future=ready_future,
            close_event=close_event,
        )
    )
    cl.user_session.set("runtime_id", runtime_id)
    cl.user_session.set("runtime_close_event", close_event)
    cl.user_session.set("runtime_task", runtime_task)

    try:
        setup_payload = await ready_future
    except Exception as e:
        await _stop_runtime_owner(wait=True)
        logger.error(f"[chat_app] MCP Client 初始化失败: {e}")
        await cl.Message(
            content=f"❌ MCP Server 连接失败：`{e}`\n请检查 `mcp_lint.py` 是否正常。"
        ).send()
        return

    tool_names = cast(list[str], setup_payload["tool_names"])

    task_list = cl.TaskList()
    cl.user_session.set("task_list", task_list)
    await task_list.send()

    if not send_intro:
        return

    tools_list = "\n".join(f"  - `{n}`" for n in tool_names)
    history_readiness = []
    if not config.chainlit_enable_password_auth:
        history_readiness.append("- 未启用 Chainlit 认证（`CHAINLIT_ENABLE_PASSWORD_AUTH`）")
    if not config.chainlit_database_url:
        history_readiness.append("- 未配置 Chainlit 数据层（`DATABASE_URL`）")
    history_hint = ""
    if history_readiness:
        history_hint = (
            "\n\n> ℹ️ 网页历史会话显示需要同时启用 Chainlit 认证与数据持久化：\n"
            + "\n".join(f"> {x}" for x in history_readiness)
        )
    await cl.Message(
        content=(
            f"**已连接 MCP 工具（{len(tool_names)} 个）：**\n{tools_list}\n\n"
            "文件工具根目录：`GLOBAL`\n\n"
            f"当前模型：`{runtime_cfg.llm_model}`"
            f"{history_hint}"
        )
    ).send()


@cl.on_chat_start
async def on_chat_start():
    """
    新会话入口：使用 Chainlit 当前线程 ID 作为 LangGraph thread_id。
    不自动发送欢迎消息，进入即对话。
    """
    thread_id = _get_chainlit_thread_id_fallback()
    await _initialize_chat_runtime(thread_id, send_intro=False)
    cl.user_session.set("seen_user_message_ids", [])
    await _send_model_chat_settings()


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    """
    历史线程恢复入口（官方要求：启用认证 + 数据持久化时触发）。
    使用被恢复线程的 id 作为 LangGraph thread_id，确保记忆连续。
    """
    thread_id = str(thread.get("id") or _get_chainlit_thread_id_fallback())
    await _initialize_chat_runtime(thread_id, send_intro=False)
    cl.user_session.set("seen_user_message_ids", _extract_seen_user_message_ids_from_thread(thread))
    await _send_model_chat_settings()


@cl.on_settings_update
async def on_settings_update(settings: dict[str, Any]):
    preset_id = _resolve_llm_preset_id((settings or {}).get("llm_preset"))
    current_preset_id = _resolve_llm_preset_id(cl.user_session.get("llm_preset_id"))
    if not preset_id or preset_id == current_preset_id:
        return

    thread_id = str(cl.user_session.get("thread_id") or _get_chainlit_thread_id_fallback())
    await _initialize_chat_runtime(thread_id, send_intro=False, llm_preset_id=preset_id)
    await _send_model_chat_settings()

    preset = _find_llm_preset_by_id(preset_id)
    label = (preset or {}).get("label") or preset_id
    model_name = (preset or {}).get("model") or ""
    await cl.Message(content=f"已切换模型：`{label}`\n`{model_name}`").send()


@cl.on_chat_end
async def on_chat_end():
    """Chainlit session 结束时正确关闭 MCP stdio 子进程。"""
    await _stop_runtime_owner(wait=True)


# ── TodoListMiddleware → Chainlit TaskList 桥接 ──────────────────────────
# 官方 API 参考：
#   TodoListMiddleware: Command(update={"todos": [Todo], ...})
#     Todo = TypedDict(content=str, status="pending"|"in_progress"|"completed")
#   Chainlit TaskList: add_task(Task) → send()
#     官方参考：https://docs.chainlit.io/api-reference/elements/tasklist

_TODO_STATUS_MAP = {
    "pending": cl.TaskStatus.READY,
    "in_progress": cl.TaskStatus.RUNNING,
    "completed": cl.TaskStatus.DONE,
}


async def _sync_todos_to_tasklist(todos: list[dict]) -> None:
    """将 TodoListMiddleware 的 todos 状态同步到 Chainlit TaskList 面板。"""
    task_list = cl.user_session.get("task_list")
    if not task_list:
        return

    # write_todos 每次全量替换，因此清空后重建
    task_list.tasks.clear()
    for todo in todos:
        task = cl.Task(
            title=todo.get("content", ""),
            status=_TODO_STATUS_MAP.get(todo.get("status", "pending"), cl.TaskStatus.READY),
        )
        await task_list.add_task(task)

    # 计算面板状态标签
    done = sum(1 for t in task_list.tasks if t.status == cl.TaskStatus.DONE)
    total = len(task_list.tasks)
    if total == 0:
        task_list.status = "Ready"
    elif done == total:
        task_list.status = "Done"
    else:
        task_list.status = f"Running... {done}/{total}"

    await task_list.send()


def _step_name(step_type: str, node_name: str) -> str:
    if step_type == "llm":
        return "🧠 LLM" if node_name == "model" else f"🧠 {node_name}"
    if step_type == "tool":
        return f"🔧 {node_name}"
    return f"⚙️ {node_name}"


def _message_preview(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content[:800]
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
        if texts:
            return "\n".join(texts)[:800]
    if content:
        return str(content)[:800]
    return ""


def _tool_call_summary(tool_calls: Any) -> str:
    if not isinstance(tool_calls, list):
        return ""
    names = [
        str(tc.get("name") or "tool")
        for tc in tool_calls
        if isinstance(tc, dict)
    ]
    if not names:
        return ""
    return f"Tool calls: {', '.join(names[:5])}"


def _update_preview(update: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in update.items()
        if key not in {"messages", "todos"}
    }
    if not payload:
        return ""
    return str(payload)[:800]


def _should_show_run_step(node_name: str) -> bool:
    return node_name not in {"model", "tools"} and not node_name.startswith("__") and ":" not in node_name


@cl.on_message
async def on_message(message: cl.Message):
    """
    接收用户消息，统一走 create_agent 主链路（官方短期记忆主入口）。

    流式处理采用官方推荐的 stream_mode=["messages", "updates"] API：
      messages 模式：LLM 输出 token（AIMessageChunk），含文本和工具调用块
      updates 模式：完整状态更新，含 model 节点和 tools 节点的输出消息

    官方参考：https://docs.langchain.com/oss/python/langchain/streaming

    cl.Step 用法遵循官方：
      step.input  = ...  设置工具参数展示（show_input=True 时可见）
      step.output = ...  设置工具结果展示
      官方参考：https://docs.chainlit.io/api-reference/step-class

    记忆语义：
    - 本函数经 agent.astream 执行，状态更新自动落入 checkpointer。
    """
    agent = cl.user_session.get("agent")
    if not agent:
        await cl.Message(
            content="⚠️ 未配置 LLM，无法回答问题。请配置 `.env` 后重启。"
        ).send()
        return

    thread_id: str = cl.user_session.get("thread_id")
    current_message_id = str(getattr(message, "id", "") or "")
    seen_user_message_ids = list(cl.user_session.get("seen_user_message_ids") or [])
    is_message_edit = bool(current_message_id) and current_message_id in seen_user_message_ids
    if current_message_id and not is_message_edit:
        seen_user_message_ids.append(current_message_id)
        cl.user_session.set("seen_user_message_ids", seen_user_message_ids)

    try:
        user_human_message = _build_human_message_from_chainlit_message(message)
    except Exception as e:
        logger.warning(f"[chat_app] 构造上传消息失败，回退纯文本输入: {e}")
        user_human_message = HumanMessage(content=str(message.content or ""), id=current_message_id or None)

    if is_message_edit:
        await _reset_agent_history_from_chainlit_context(agent, thread_id, message)

    # 每轮只传当前消息——历史由 checkpointer 按 thread_id 自动追加管理
    agent_context = cl.user_session.get("agent_context") or _resolve_agent_context(thread_id)
    run_config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": config.agent_recursion_limit,
    }

    # ── 流式处理（对照官方标准实现）────────────────────────────────────────
    # 官方标准参考：https://docs.langchain.com/oss/python/langchain/streaming
    #
    # 官方 v2 循环结构（精简版）：
    #   async with aclosing(agent.astream(..., stream_mode=["messages","updates"], version="v2")) as stream:
    #       async for part in stream:
    #           mode = part["type"]
    #           data = part["data"]
    #           if mode == "messages":
    #               token, metadata = data
    #           elif mode == "updates":
    #               for source, update in data.items():
    #                   ...
    #
    # messages 模式仅产出 LLM 的逐 token 输出（AIMessageChunk），ToolMessage 仅在 updates 模式中出现。
    # ─────────────────────────────────────────────────────────────────────────
    response_msg = cl.Message(content="")
    await response_msg.send()

    llm_step_by_node: dict[str, cl.Step] = {}
    llm_output_by_node: dict[str, str] = {}
    # {tc_id: cl.Step} — 统一追踪当前未完成的工具 Step。
    # messages 模式负责尽早创建 Step，updates 模式负责补齐 input 和关闭 output。
    tool_step_by_id: dict = {}
    _model_text_buffer: str = ""       # 当前轮缓冲，确认无真实工具调用后写入 response_msg
    _write_todos_text_fallback: str = ""  # write_todos 同轮文字，用于后续空终止轮兜底

    try:
        pending_input = {"messages": [user_human_message]}
        while True:
            hitl_request = None
            async with aclosing(
                agent.astream(
                    pending_input,
                    config=run_config,
                    context=agent_context,
                    stream_mode=["messages", "updates"],
                    version="v2",
                )
            ) as stream:
                async for part in stream:
                    stream_part = cast(StreamPart[Any, Any], part)
                    stream_mode = stream_part["type"]

                    # ── messages 模式：LLM 逐 token 输出 ─────────────────────────────
                    # v2 StreamPart: {"type":"messages","data":(token, metadata), ...}
                    if stream_mode == "messages":
                        messages_part = cast(MessagesStreamPart, stream_part)
                        token, metadata = messages_part["data"]
                        if not isinstance(token, AIMessageChunk):
                            continue

                        node_name = str((metadata or {}).get("langgraph_node") or "model")
                        llm_step = llm_step_by_node.get(node_name)
                        if llm_step is None:
                            llm_step = cl.Step(name=_step_name("llm", node_name), type="llm")
                            await llm_step.send()
                            llm_step_by_node[node_name] = llm_step
                            llm_output_by_node[node_name] = ""

                        if token.text:
                            await llm_step.stream_token(token.text)
                            llm_output_by_node[node_name] += token.text
                            if node_name == "model":
                                _model_text_buffer += token.text

                        # 工具调用流 — 跟踪并创建 cl.Step
                        # 官方输出格式：首 chunk 含 {id, name, ...}，后续参数 chunk 仅补 args。
                        for tc_chunk in (token.tool_call_chunks or []):
                            tc_id = tc_chunk.get("id")
                            tc_name = tc_chunk.get("name")

                            if tc_id and tc_name and tc_id not in tool_step_by_id:
                                step = cl.Step(name=_step_name("tool", tc_name), type="tool", show_input=True)
                                await step.send()
                                tool_step_by_id[tc_id] = step

                    # ── updates 模式：节点完成后的完整消息 ───────────────────────────
                    # v2 StreamPart: {"type":"updates","data":{node_name:update,...}, ...}
                    elif stream_mode == "updates":
                        updates_part = cast(UpdatesStreamPart, stream_part)
                        data = updates_part["data"]
                        hitl_request = _extract_hitl_request_from_interrupts(
                            data.get("__interrupt__") if isinstance(data, dict) else None
                        )
                        if hitl_request:
                            break

                        if not isinstance(data, dict):
                            continue

                        for source, update in data.items():
                            if not isinstance(update, dict):
                                continue

                            todos_update = update.get("todos")
                            if todos_update is not None:
                                await _sync_todos_to_tasklist(todos_update)

                            msgs = update.get("messages")
                            last_msg = msgs[-1] if msgs else None

                            llm_step = llm_step_by_node.pop(source, None)
                            if llm_step:
                                llm_output = llm_output_by_node.pop(source, "")
                                summary = llm_output
                                if last_msg is not None and not summary:
                                    summary = _message_preview(last_msg) or _tool_call_summary(
                                        getattr(last_msg, "tool_calls", None)
                                    )
                                if summary and not llm_output:
                                    llm_step.output = summary
                                await llm_step.update()
                                if source not in {"model", "tools"}:
                                    continue

                            if source == "model":
                                if not last_msg:
                                    continue

                                if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                                    # write_todos 是 UI 元工具：同轮文字暂存为兜底，供后续空终止轮使用。
                                    # 有其他真实工具调用时丢弃缓冲（中间思考不展示给用户）。
                                    if all(tc.get("name") == "write_todos" for tc in last_msg.tool_calls):
                                        _write_todos_text_fallback = _model_text_buffer
                                    _model_text_buffer = ""
                                    # 用完整 tool_calls 补全 Step.input（比流式 args chunks 准确）
                                    for tc in last_msg.tool_calls:
                                        tc_id = tc.get("id")
                                        if not tc_id:
                                            continue
                                        step = tool_step_by_id.get(tc_id)
                                        if step:
                                            step.input = str(tc.get("args", {}))[:600]
                                        else:
                                            # 若 messages 模式未收到 tool_call_chunks（非流式模型）
                                            # 在此创建 Step
                                            step = cl.Step(
                                                name=_step_name("tool", str(tc.get("name", "工具"))),
                                                type="tool",
                                                show_input=True,
                                            )
                                            await step.send()
                                            step.input = str(tc.get("args", {}))[:600]
                                            tool_step_by_id[tc_id] = step
                                else:
                                    # 本轮无工具调用（最终回答轮）：写入缓冲文字；
                                    # 若模型输出为空（write_todos 同轮已输出回复），用 fallback 兜底。
                                    final_text = _model_text_buffer or _write_todos_text_fallback
                                    if final_text:
                                        response_msg.content = final_text
                                        await response_msg.update()
                                    _model_text_buffer = ""
                                    _write_todos_text_fallback = ""

                            elif source == "tools":
                                # ToolMessage：关闭对应 Step（遍历全部，支持并行工具调用）
                                for tool_msg in (msgs or []):
                                    tc_id = getattr(tool_msg, "tool_call_id", None)
                                    step = tool_step_by_id.pop(tc_id, None)
                                    if step:
                                        step.output = str(tool_msg.content)[:800]
                                        await step.update()

                            elif _should_show_run_step(source):
                                step = cl.Step(name=_step_name("run", source), type="run")
                                step.output = _update_preview(update) or (
                                    _message_preview(last_msg) if last_msg is not None else f"Node `{source}` completed"
                                )
                                await step.send()
                                await step.update()

            if not hitl_request:
                break

            resume_payload = await _ask_hitl_resume_payload(hitl_request)
            pending_input = Command(resume=resume_payload)
            await cl.Message(content="🛂 已提交审批决策，继续执行...").send()
            response_msg = cl.Message(content="")
            await response_msg.send()

    except Exception as e:
        response_msg.content += f"\n\n[错误: {e}]"
        await response_msg.update()
        logger.error(f"[chat_app] Agent stream failed: {e}", exc_info=True)
    finally:
        for llm_step in llm_step_by_node.values():
            try:
                await llm_step.update()
            except Exception:
                pass

        # 关闭所有未完成的 Step（异常保护）
        for step in tool_step_by_id.values():
            if not step:
                continue
            try:
                await step.update()
            except Exception:
                pass

    await response_msg.update()


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from chainlit.cli import run_chainlit
    run_chainlit(__file__)
