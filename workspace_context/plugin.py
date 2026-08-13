"""Hermes plugin registration for workspace routing."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import load_config
from .mcp import install_mcp_patches as _install_mcp_patches
from .runtime import make_pre_gateway_dispatch
from .skills import install_skill_patches as _install_skill_patches


def register_plugin(
    ctx: Any,
    *,
    plugin_dir: Path | None = None,
    install_skill_patches: Callable[[], None] = _install_skill_patches,
    install_mcp_patches: Callable[..., None] = _install_mcp_patches,
) -> None:
    root = Path(plugin_dir or Path(__file__).resolve().parent.parent)
    config_path = root / "config.yaml"
    if not config_path.is_file():
        raise RuntimeError(
            f"hermes-workspace-context local config is missing: {config_path}; "
            "copy config.yaml.example to config.yaml and edit it"
        )

    config = load_config(config_path)
    install_skill_patches()
    install_mcp_patches(workspaces=config.workspaces)
    ctx.register_hook("pre_gateway_dispatch", make_pre_gateway_dispatch(config))


def register(ctx: Any) -> None:
    register_plugin(ctx)
