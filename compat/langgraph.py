"""LangGraph compatibility patches used by multiple entrypoints."""

from __future__ import annotations

import logging
import pickle
from typing import Any

logger = logging.getLogger(__name__)


def apply_dev_persistence_pickle_sanitization(*, log_prefix: str) -> None:
    """Prevent LangGraph dev persistence from crashing on runtime-only handles."""

    try:
        from langgraph.checkpoint.memory import PersistentDict

        if getattr(PersistentDict, "_lint_agent_safe_dump", False):
            return

        original_dump = PersistentDict.dump

        def _safe_for_pickle(obj: Any, seen: set[int]) -> Any:
            if obj is None or isinstance(obj, str | int | float | bool | bytes):
                return obj

            obj_id = id(obj)
            if obj_id in seen:
                return "<cycle>"

            if isinstance(obj, dict):
                seen.add(obj_id)
                cleaned_dict: dict[Any, Any] = {}
                for key, value in obj.items():
                    safe_key = _safe_for_pickle(key, seen)
                    try:
                        hash(safe_key)
                    except Exception:
                        safe_key = repr(safe_key)
                    cleaned_dict[safe_key] = _safe_for_pickle(value, seen)
                seen.discard(obj_id)
                return cleaned_dict

            if isinstance(obj, list):
                seen.add(obj_id)
                cleaned_list = [_safe_for_pickle(item, seen) for item in obj]
                seen.discard(obj_id)
                return cleaned_list

            if isinstance(obj, tuple):
                seen.add(obj_id)
                cleaned_tuple = tuple(_safe_for_pickle(item, seen) for item in obj)
                seen.discard(obj_id)
                return cleaned_tuple

            if isinstance(obj, set):
                seen.add(obj_id)
                cleaned_set = [_safe_for_pickle(item, seen) for item in obj]
                seen.discard(obj_id)
                return cleaned_set

            try:
                pickle.dumps(obj)
            except Exception:
                return f"<non-pickleable {type(obj).__module__}.{type(obj).__name__}>"
            return obj

        def _patched_dump(self: Any, fileobj: Any) -> None:
            if self.format == "pickle":
                pickle.dump(_safe_for_pickle(dict(self), set()), fileobj, 2)
                return
            original_dump(self, fileobj)

        PersistentDict.dump = _patched_dump
        PersistentDict._lint_agent_safe_dump = True
        logger.info("%s Applied LangGraph dev persistence pickle sanitization patch.", log_prefix)
    except Exception as exc:
        logger.warning("%s Dev persistence pickle sanitization patch failed: %s", log_prefix, exc)
