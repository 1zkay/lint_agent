"""Shared runtime configuration helpers for agent entrypoints."""

from __future__ import annotations

from copy import copy
from typing import Any

from config import config


def find_llm_preset_by_id(preset_id: str | None) -> dict[str, str] | None:
    preset_id = str(preset_id or "").strip()
    for preset in config.llm_model_presets:
        if preset.get("id") == preset_id:
            return preset
    return None


def resolve_llm_preset_id(preset_id: str | None = None) -> str:
    preset_id = str(preset_id or "").strip()
    if preset_id and find_llm_preset_by_id(preset_id):
        return preset_id
    return config.llm_model_preset_default


def build_runtime_config_for_llm_preset(preset_id: str | None = None) -> Any:
    runtime_cfg = copy(config)
    preset = find_llm_preset_by_id(resolve_llm_preset_id(preset_id))
    if preset:
        runtime_cfg.llm_model = preset["model"]
        runtime_cfg.llm_base_url = preset["base_url"]
        runtime_cfg.llm_api_key = preset["api_key"]
    return runtime_cfg
