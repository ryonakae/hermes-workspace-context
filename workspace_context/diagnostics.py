"""Small, non-sensitive structured logging helpers for gateway diagnosis."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
from typing import Any


_BASE_LOGGER = "gateway.plugins.hermes_workspace_context"
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.:-]+")


def component_logger(component: str) -> logging.Logger:
    """Return a logger that is routed with Hermes gateway diagnostics."""
    return logging.getLogger(f"{_BASE_LOGGER}.{component}")


def runner_fields() -> dict[str, int | str]:
    """Return stable process-local identifiers without exposing request data."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return {
        "runner_pid": os.getpid(),
        "runner_thread_id": threading.get_ident(),
        "runner_task_ref": opaque_ref(id(task)) if task is not None else "none",
    }


def opaque_ref(value: Any) -> str:
    """Return a short correlation token without logging the source identifier."""
    if value is None or isinstance(value, bool):
        return "none"
    if not isinstance(value, (str, int)):
        return "none"
    text = str(value)
    if not text:
        return "none"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def safe_label(value: Any, *, missing: str = "none") -> str:
    """Render a bounded, newline-free label without using arbitrary repr()."""
    if value is None:
        return missing
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        return "unknown"
    text = _SAFE_LABEL.sub("_", value.replace("\r", "_").replace("\n", "_"))
    return text[:80] or missing


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit one compact INFO record using only explicitly safe scalar fields."""
    parts = ["workspace_context", f"event={safe_label(event, missing='unknown')}"]
    for key, value in fields.items():
        parts.append(f"{safe_label(key, missing='field')}={safe_label(value)}")
    try:
        logger.info("%s", " ".join(parts))
    except Exception:
        # Diagnostics must never change gateway routing or tool resolution.
        return
