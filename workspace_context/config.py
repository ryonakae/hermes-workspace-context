"""Local configuration loading for the workspace context plugin."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class ConfigError(ValueError):
    """Raised when local workspace routing configuration is invalid."""


@dataclass(frozen=True)
class Workspace:
    name: str
    cwd: Path
    skill_dirs: tuple[Path, ...]
    mcp_file: Path | None
    auto_detect_mcp: bool = False


@dataclass(frozen=True)
class Route:
    platform: str
    workspace: str
    chat_id: str | None = None
    thread_id: str | None = None


@dataclass(frozen=True)
class RouterConfig:
    workspaces: Mapping[str, Workspace]
    routes: tuple[Route, ...]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a mapping")
    return value


def _resolve_from(cwd: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def load_config(path: str | Path) -> RouterConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"config file does not exist: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    root = _mapping(raw, "config")

    workspaces: dict[str, Workspace] = {}
    for name, workspace_raw in _mapping(root.get("workspaces"), "workspaces").items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("workspace names must be non-empty strings")
        workspace_data = _mapping(workspace_raw, f"workspace {name!r}")
        cwd = _resolve_from(config_path.parent, workspace_data.get("cwd"), f"workspace {name!r} cwd")
        if not cwd.is_dir():
            raise ConfigError(f"workspace directory does not exist: {cwd}")

        skill_values = workspace_data.get("skill_dirs", [".agents/skills"])
        if not isinstance(skill_values, list) or not all(isinstance(item, str) for item in skill_values):
            raise ConfigError(f"workspace {name!r} skill_dirs must be a list of paths")
        skill_dirs = tuple(_resolve_from(cwd, item, f"workspace {name!r} skill dir") for item in skill_values)

        mcp_value = workspace_data.get("mcp_file")
        mcp_file = None if mcp_value is None else _resolve_from(cwd, mcp_value, f"workspace {name!r} mcp_file")
        auto_detect_mcp = workspace_data.get("auto_detect_mcp", mcp_value is None)
        if not isinstance(auto_detect_mcp, bool):
            raise ConfigError(f"workspace {name!r} auto_detect_mcp must be a boolean")
        if mcp_value is not None and auto_detect_mcp:
            raise ConfigError(
                f"workspace {name!r} cannot combine mcp_file with auto_detect_mcp: true"
            )
        workspaces[name] = Workspace(
            name=name,
            cwd=cwd,
            skill_dirs=skill_dirs,
            mcp_file=mcp_file,
            auto_detect_mcp=auto_detect_mcp,
        )

    routes_raw = root.get("routes", [])
    if not isinstance(routes_raw, list):
        raise ConfigError("routes must be a list")
    routes: list[Route] = []
    for index, route_raw in enumerate(routes_raw):
        route_data = _mapping(route_raw, f"route {index}")
        platform = route_data.get("platform")
        workspace_name = route_data.get("workspace")
        if not isinstance(platform, str) or not platform.strip():
            raise ConfigError(f"route {index} platform must be a non-empty string")
        if not isinstance(workspace_name, str) or workspace_name not in workspaces:
            raise ConfigError(f"route {index} references unknown workspace: {workspace_name!r}")
        chat_id = route_data.get("chat_id")
        thread_id = route_data.get("thread_id")
        if chat_id is not None and not isinstance(chat_id, str):
            raise ConfigError(f"route {index} chat_id must be a string")
        if thread_id is not None and not isinstance(thread_id, str):
            raise ConfigError(f"route {index} thread_id must be a string")
        chat_id = chat_id.strip() or None if isinstance(chat_id, str) else None
        thread_id = thread_id.strip() or None if isinstance(thread_id, str) else None
        if chat_id is None and thread_id is None:
            raise ConfigError(f"route {index} must define chat_id or thread_id")
        routes.append(
            Route(
                platform=platform.strip().lower(),
                workspace=workspace_name,
                chat_id=chat_id,
                thread_id=thread_id,
            )
        )

    return RouterConfig(workspaces=workspaces, routes=tuple(routes))
