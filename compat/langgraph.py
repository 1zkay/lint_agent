"""LangGraph compatibility patches used by multiple entrypoints."""

from __future__ import annotations

import logging
import pickle
from typing import Any

logger = logging.getLogger(__name__)


def apply_recursive_send_sanitization(
    *,
    log_prefix: str,
    drop_unpickleable: bool = False,
) -> None:
    """Patch LangGraph Send sanitization to remove nested runtime-only state."""

    try:
        from langgraph.channels.untracked_value import UntrackedValue
        from langgraph.pregel import _algo, _loop
        from langgraph.types import Send as _Send

        filtered = object()

        def _filter_leaf(obj: Any) -> Any:
            if not drop_unpickleable:
                return obj
            try:
                pickle.dumps(obj)
            except Exception:
                return filtered
            return obj

        def _recursive_filter(obj: Any, channels: Any) -> Any:
            if isinstance(obj, dict):
                cleaned: dict[Any, Any] = {}
                for key, value in obj.items():
                    if isinstance(channels.get(key), UntrackedValue):
                        continue
                    filtered_value = _recursive_filter(value, channels)
                    if filtered_value is not filtered:
                        cleaned[key] = filtered_value
                return cleaned
            if isinstance(obj, list):
                return [
                    filtered_item
                    for item in obj
                    if (filtered_item := _recursive_filter(item, channels)) is not filtered
                ]
            if isinstance(obj, tuple):
                return tuple(
                    filtered_item
                    for item in obj
                    if (filtered_item := _recursive_filter(item, channels)) is not filtered
                )
            if isinstance(obj, set):
                return {
                    filtered_item
                    for item in obj
                    if (filtered_item := _recursive_filter(item, channels)) is not filtered
                }
            return _filter_leaf(obj)

        def _patched_sanitize(packet: Any, channels: Any) -> Any:
            if not isinstance(packet.arg, dict):
                return packet
            return _Send(node=packet.node, arg=_recursive_filter(packet.arg, channels))

        _algo.sanitize_untracked_values_in_send = _patched_sanitize
        _loop.sanitize_untracked_values_in_send = _patched_sanitize
        logger.info("%s Applied recursive LangGraph Send sanitization patch.", log_prefix)
    except Exception as exc:
        logger.warning("%s Recursive Send sanitization patch failed: %s", log_prefix, exc)


def apply_dev_persistence_pickle_sanitization(*, log_prefix: str) -> None:
    """Prevent LangGraph dev persistence from crashing on runtime-only handles."""

    try:
        from langgraph.checkpoint.memory import PersistentDict

        if getattr(PersistentDict, "_mcp_alint_safe_dump", False):
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
                cleaned: dict[Any, Any] = {}
                for key, value in obj.items():
                    safe_key = _safe_for_pickle(key, seen)
                    try:
                        hash(safe_key)
                    except Exception:
                        safe_key = repr(safe_key)
                    cleaned[safe_key] = _safe_for_pickle(value, seen)
                seen.discard(obj_id)
                return cleaned

            if isinstance(obj, list):
                seen.add(obj_id)
                cleaned = [_safe_for_pickle(item, seen) for item in obj]
                seen.discard(obj_id)
                return cleaned

            if isinstance(obj, tuple):
                seen.add(obj_id)
                cleaned = tuple(_safe_for_pickle(item, seen) for item in obj)
                seen.discard(obj_id)
                return cleaned

            if isinstance(obj, set):
                seen.add(obj_id)
                cleaned = [_safe_for_pickle(item, seen) for item in obj]
                seen.discard(obj_id)
                return cleaned

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
        PersistentDict._mcp_alint_safe_dump = True
        logger.info("%s Applied LangGraph dev persistence pickle sanitization patch.", log_prefix)
    except Exception as exc:
        logger.warning("%s Dev persistence pickle sanitization patch failed: %s", log_prefix, exc)
