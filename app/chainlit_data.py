"""Chainlit data layer and local object-storage helpers."""

from __future__ import annotations

import atexit
import logging
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.config import Config as BotoCoreConfig
from chainlit.data.chainlit_data_layer import ChainlitDataLayer
from chainlit.data.storage_clients.s3 import S3StorageClient, storage_expiry_time

from config import config

logger = logging.getLogger(__name__)

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
    except Exception as exc:
        logger.warning("[chat_app] Failed to start local MinIO: %s", exc)
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


class AppChainlitDataLayer(ChainlitDataLayer):
    """
    使用 Chainlit 官方 @cl.data_layer 扩展点，兼容本项目运行时的已知数据层差异。

    说明：
    - 这是官方支持的自定义数据层方式；
    - 兼容 update_thread 的 metadata 为空场景；
    - 兼容 Step 时间戳字符串不是 UTC ...Z 格式时，官方 create_step 的严格解析。
    """

    @staticmethod
    def _normalize_chainlit_timestamp(value: Any) -> str | None:
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
    def _normalize_step_timestamps(cls, step_dict: dict) -> dict:
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


class AppS3StorageClient(S3StorageClient):
    """Chainlit S3 client with separate internal and browser-facing endpoints."""

    def __init__(self, bucket: str, public_endpoint_url: str | None = None, **kwargs: Any):
        self._public_client = None
        self._public_endpoint_url = (public_endpoint_url or "").strip()
        super().__init__(bucket=bucket, **kwargs)

        if self._public_endpoint_url:
            public_kwargs = dict(kwargs)
            public_kwargs["endpoint_url"] = self._public_endpoint_url
            try:
                self._public_client = boto3.client("s3", **public_kwargs)
            except Exception as exc:
                logger.warning(
                    "[chat_app] Failed to initialize public S3 client; using internal endpoint for read URLs: %s",
                    exc,
                )

    def sync_get_read_url(self, object_key: str) -> str:
        client = self._public_client or getattr(self, "client", None)
        if client is None:
            return object_key

        try:
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": object_key},
                ExpiresIn=storage_expiry_time,
            )
        except Exception as exc:
            logger.warning("[chat_app] S3StorageClient get_read_url error: %s", exc)
            return object_key

    def sync_upload_file(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = super().sync_upload_file(*args, **kwargs)
        object_key = str(result.get("object_key") or "").strip() if isinstance(result, dict) else ""
        if not object_key:
            return result

        result = dict(result)
        result["url"] = self.sync_get_read_url(object_key)
        return result

    async def close(self) -> None:
        seen_ids: set[int] = set()
        for client in (self._public_client, getattr(self, "client", None)):
            if client is None or id(client) in seen_ids:
                continue
            seen_ids.add(id(client))
            close = getattr(client, "close", None)
            if not callable(close):
                continue
            result = close()
            if hasattr(result, "__await__"):
                await result


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
        client_kwargs: dict[str, Any] = {
            "region_name": config.s3_region,
            "aws_access_key_id": config.s3_access_key,
            "aws_secret_access_key": config.s3_secret_key,
            "endpoint_url": config.s3_endpoint_url,
        }
        if config.s3_endpoint_url:
            client_kwargs["config"] = BotoCoreConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            )

        return AppS3StorageClient(
            bucket=config.s3_bucket_name,
            public_endpoint_url=config.s3_public_endpoint_url,
            **client_kwargs,
        )
    except Exception as exc:
        logger.warning("[chat_app] 初始化 Chainlit S3StorageClient 失败，将不上传附件: %s", exc)
        return None


def register_chainlit_data_layer(cl: Any) -> None:
    """Register the Chainlit data layer when database persistence is configured."""
    if not config.chainlit_database_url:
        return

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
