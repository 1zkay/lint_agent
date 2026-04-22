"""
ALINT-PRO 全局配置（唯一入口）

所有可配置项统一在此管理；其他模块（chat_app / mcp_lint 等）一律通过
``from config import config`` 访问，禁止自行调用 os.getenv。

优先级（由低到高）：类内默认值 → .env 文件 → 系统环境变量
"""
import os
import re
import logging
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env 文件（位于本文件同目录；override=False 表示系统环境变量优先）
_env_file = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_file, override=False)

logger = logging.getLogger(__name__)


class Config:
    """
    ALINT-PRO 统一配置类。

    职责：读取 .env / 系统环境变量并提供带类型、带默认值的属性。
    覆盖范围：
      - ALINT 基础路径
      - LLM 接入（provider:model、temperature 等）
      - Agent 中间件（Todo / Summarization / Reflection / ModelRetry / ToolRetry / HITL）
      - LangGraph Checkpointer（PostgreSQL / Memory）
      - Chainlit 认证与 Data Layer
      - S3 兼容对象存储（附件上传）
      - OpenRouter 兼容 HTTP 头
    """

    @staticmethod
    def _bool_env(key: str, default: str = "false") -> bool:
        return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "y", "on")

    @staticmethod
    def _slug_env_value(value: str, default: str = "value") -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
        return slug or default

    @staticmethod
    def _resolve_env_path(value: str, *, base_dir: Path) -> Path:
        path = Path(value.strip()).expanduser()
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        else:
            path = path.resolve()
        return path

    @classmethod
    def _collect_rag_pdf_paths(
        cls,
        *,
        default_root: Path,
        base_dir: Path,
    ) -> list[str]:
        raw_paths_env = os.getenv("RAG_PDF_PATHS", "").strip()
        if raw_paths_env:
            raw_paths = [
                item.strip()
                for item in re.split(r"[;\r\n]+", raw_paths_env)
                if item.strip()
            ]
        else:
            raw_paths = [str((default_root / "1800-2017.pdf").resolve())]

        return [
            str(cls._resolve_env_path(item, base_dir=base_dir))
            for item in raw_paths
        ]

    @classmethod
    def _default_llm_preset_label(cls, model: str) -> str:
        model = model.strip()
        if ":" in model:
            _, model = model.split(":", 1)
        return model or "model"

    @classmethod
    def _collect_llm_model_presets(
        cls,
        default_model: str,
        default_base_url: str,
        default_api_key: str,
    ) -> list[dict[str, str]]:
        presets: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        def add_preset(label: str, model: str, base_url: str, api_key: str) -> None:
            model = model.strip()
            if not model:
                return

            base_url = base_url.strip()
            api_key = api_key.strip()
            label = label.strip() or model

            preset_id_base = cls._slug_env_value(label, "model")
            preset_id = preset_id_base
            suffix = 2
            while preset_id in seen_ids:
                preset_id = f"{preset_id_base}_{suffix}"
                suffix += 1

            seen_ids.add(preset_id)
            presets.append(
                {
                    "id": preset_id,
                    "label": label,
                    "model": model,
                    "base_url": base_url,
                    "api_key": api_key,
                }
            )

        index = 1
        while True:
            suffix = "" if index == 1 else f"_{index}"
            model = os.getenv(f"LLM_MODEL{suffix}", "").strip()
            if not model:
                if index == 1 and default_model:
                    model = default_model.strip()
                else:
                    break

            base_url = os.getenv(f"LLM_BASE_URL{suffix}", "").strip() or default_base_url
            api_key = os.getenv(f"LLM_API_KEY{suffix}", "").strip() or default_api_key
            label = os.getenv(f"LLM_LABEL{suffix}", "").strip() or cls._default_llm_preset_label(model)
            add_preset(label, model, base_url, api_key)
            index += 1

        return presets

    def __init__(self):
        default_root = Path(__file__).resolve().parent.parent
        app_dir = Path(__file__).resolve().parent

        # ── ALINT 基础路径 ───────────────────────────────────────────────────
        self.alint_pro_root = os.getenv("ALINT_PRO_ROOT", str(default_root))
        self.alint_exe = os.getenv("ALINT_EXE", r"D:\software\ALINT-PRO\bin\alintcon.exe")
        self.csv_output_dir = os.getenv("CSV_OUTPUT_DIR", str(app_dir / "reports"))

        # Agentic RAG (built-in reference PDFs)
        self.rag_enabled = self._bool_env("RAG_ENABLED", "true")
        self.rag_pdf_paths = self._collect_rag_pdf_paths(
            default_root=default_root,
            base_dir=app_dir,
        )
        self.rag_embed_model = os.getenv(
            "RAG_EMBED_MODEL", "openai/text-embedding-3-small"
        ).strip()
        default_rag_index_prefix = "rag_index_hardware_references"
        default_rag_index_dir = default_root / (
            f"{default_rag_index_prefix}_{self._slug_env_value(self.rag_embed_model, 'embedding_model')}"
        )
        self.rag_index_dir = str(
            self._resolve_env_path(
                os.getenv("RAG_INDEX_DIR", "").strip() or str(default_rag_index_dir.resolve()),
                base_dir=app_dir,
            )
        )
        self.rag_embed_base_url = (
            os.getenv("RAG_EMBED_BASE_URL", "").strip()
            or os.getenv("LLM_BASE_URL", "").strip()
            or "https://openrouter.ai/api/v1"
        )
        self.rag_embed_api_key = (
            os.getenv("RAG_EMBED_API_KEY", "").strip()
            or os.getenv("LLM_API_KEY", "").strip()
        )
        self.rag_chunk_size = int(os.getenv("RAG_CHUNK_SIZE", "1400"))
        self.rag_chunk_overlap = int(os.getenv("RAG_CHUNK_OVERLAP", "200"))
        self.rag_top_k = int(os.getenv("RAG_TOP_K", "6"))
        self.rag_min_relevance = float(os.getenv("RAG_MIN_RELEVANCE", "0.25"))
        self.rag_max_rewrites = int(os.getenv("RAG_MAX_REWRITES", "1"))
        self.rag_embed_batch_size = int(os.getenv("RAG_EMBED_BATCH_SIZE", "64"))

        # ── LLM 接入 ─────────────────────────────────────────────────────────
        self.llm_model = os.getenv("LLM_MODEL", "")          # provider:model 格式
        self.llm_api_key = os.getenv("LLM_API_KEY", "")
        self.llm_base_url = os.getenv("LLM_BASE_URL", "")
        self.llm_temperature = float(os.getenv("LLM_TEMPERATURE", "0.1"))
        self.llm_max_tokens = int(os.getenv("LLM_MAX_TOKENS", "81920"))
        self.llm_timeout = int(os.getenv("LLM_TIMEOUT", "120"))
        self.llm_model_presets: list[dict[str, str]] = self._collect_llm_model_presets(
            self.llm_model,
            self.llm_base_url,
            self.llm_api_key,
        )
        self.llm_model_preset_default = (
            self.llm_model_presets[0]["id"] if self.llm_model_presets else ""
        )

        # ── Agent 中间件 ──────────────────────────────────────────────────
        self.agent_enable_todo = self._bool_env("AGENT_ENABLE_TODO", "true")
        self.agent_enable_summarization = self._bool_env("AGENT_ENABLE_SUMMARIZATION", "true")
        self.agent_summarization_trigger_tokens = int(os.getenv("AGENT_SUMMARIZATION_TRIGGER_TOKENS", "60000"))
        self.agent_summarization_keep_messages = int(os.getenv("AGENT_SUMMARIZATION_KEEP_MESSAGES", "20"))
        self.agent_enable_reflection = self._bool_env("AGENT_ENABLE_REFLECTION", "false")
        self.agent_reflection_max = int(os.getenv("AGENT_REFLECTION_MAX", "1"))
        self.agent_enable_model_retry = self._bool_env("AGENT_ENABLE_MODEL_RETRY", "true")
        self.agent_model_retry_max = int(os.getenv("AGENT_MODEL_RETRY_MAX", "1"))
        self.agent_enable_tool_retry = self._bool_env("AGENT_ENABLE_TOOL_RETRY", "true")
        self.agent_tool_retry_max = int(os.getenv("AGENT_TOOL_RETRY_MAX", "2"))
        self.agent_enable_shell = self._bool_env("AGENT_ENABLE_SHELL", "false")
        self.shell_workspace_root = os.getenv("SHELL_WORKSPACE_ROOT", "").strip()
        self.shell_command_timeout = float(os.getenv("SHELL_COMMAND_TIMEOUT", "30"))
        self.shell_max_output_lines = int(os.getenv("SHELL_MAX_OUTPUT_LINES", "200"))
        self.agent_enable_skills = self._bool_env("AGENT_ENABLE_SKILLS", "false")
        self.agent_skills_dirs = [
            d.strip() for d in os.getenv("AGENT_SKILLS_DIRS", "").split(",") if d.strip()
        ]
        self.agent_tool_approval_enabled = self._bool_env("AGENT_TOOL_APPROVAL_ENABLED", "true")
        self.agent_approval_tool_names: tuple[str, ...] = (
            "write_file", "edit_file", "shell"
        )
        self.agent_hitl_timeout = int(os.getenv("AGENT_HITL_TIMEOUT", "600"))
        self.agent_recursion_limit = int(os.getenv("AGENT_RECURSION_LIMIT", "50"))

        # ── LangGraph Checkpointer ────────────────────────────────────────
        self.checkpointer_backend = os.getenv("CHECKPOINTER_BACKEND", "postgres").strip().lower()
        self.checkpointer_db_uri = os.getenv("CHECKPOINTER_DB_URI", "").strip()
        self.checkpointer_auto_setup = self._bool_env("CHECKPOINTER_AUTO_SETUP", "true")
        self.memory_store_backend = os.getenv("MEMORY_STORE_BACKEND", "postgres").strip().lower()
        self.memory_store_db_uri = (
            os.getenv("MEMORY_STORE_DB_URI", "").strip()
            or self.checkpointer_db_uri
        )
        self.memory_store_auto_setup = self._bool_env("MEMORY_STORE_AUTO_SETUP", "true")
        self.memory_enable_semantic_search = self._bool_env("MEMORY_ENABLE_SEMANTIC_SEARCH", "true")
        self.memory_embed_model = (
            os.getenv("MEMORY_EMBED_MODEL", "").strip()
            or self.rag_embed_model
        )
        self.memory_embed_base_url = (
            os.getenv("MEMORY_EMBED_BASE_URL", "").strip()
            or self.rag_embed_base_url
        )
        self.memory_embed_api_key = (
            os.getenv("MEMORY_EMBED_API_KEY", "").strip()
            or self.rag_embed_api_key
        )
        self.memory_embed_dims = int(os.getenv("MEMORY_EMBED_DIMS", "0"))

        # ── Chainlit 认证 ────────────────────────────────────────────────────
        self.chainlit_enable_password_auth = self._bool_env("CHAINLIT_ENABLE_PASSWORD_AUTH", "false")
        self.chainlit_auth_username = os.getenv("CHAINLIT_AUTH_USERNAME", "").strip()
        self.chainlit_auth_password = os.getenv("CHAINLIT_AUTH_PASSWORD", "").strip()
        self.chainlit_auth_secret = os.getenv("CHAINLIT_AUTH_SECRET", "").strip()

        # ── Chainlit Data Layer ──────────────────────────────────────────────
        self.chainlit_database_url = os.getenv("DATABASE_URL", "").strip()

        # ── S3 兼容对象存储 ──────────────────────────────────────────────────
        self.s3_bucket_name = os.getenv("BUCKET_NAME", "").strip()
        self.s3_region = os.getenv("APP_AWS_REGION", "").strip()
        self.s3_access_key = os.getenv("APP_AWS_ACCESS_KEY", "").strip()
        self.s3_secret_key = os.getenv("APP_AWS_SECRET_KEY", "").strip()
        self.s3_endpoint_url = os.getenv("DEV_AWS_ENDPOINT", "").strip() or None

        # ── OpenRouter ───────────────────────────────────────────────────────
        self.openrouter_referer = os.getenv("OPENROUTER_REFERER", "https://github.com/alint-pro").strip()
        self.openrouter_title = os.getenv("OPENROUTER_TITLE", "ALINT-PRO Analysis").strip()

    def validate(self):
        if not Path(self.alint_exe).exists():
            logger.warning(f"ALINT executable not found: {self.alint_exe}")
        Path(self.csv_output_dir).mkdir(parents=True, exist_ok=True)
        if not Path(self.alint_pro_root).exists():
            logger.warning(f"ALINT-PRO root directory not found: {self.alint_pro_root}")
        Path(self.rag_index_dir).mkdir(parents=True, exist_ok=True)
        if self.rag_enabled:
            for pdf_path in self.rag_pdf_paths:
                kb_path = Path(pdf_path)
                if not kb_path.exists():
                    logger.warning("RAG PDF knowledge base not found: %s", kb_path)
        logger.info("Configuration validation completed")


# 全局单例
config = Config()
