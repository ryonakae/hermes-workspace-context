"""Workspace-local MCP configuration and Hermes registry adapters."""

from __future__ import annotations

import json
from copy import deepcopy
import hashlib
import os
import re
import tomllib
import threading
import warnings
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, cast

import yaml

from .config import RouterConfig, Workspace
from .diagnostics import component_logger, log_event, runner_fields
from .runtime import current_workspace


_PATCH_MARKER = "_hermes_workspace_context_mcp_patch"
_CONFIG_PATCH_MARKER = "_hermes_workspace_context_mcp_config_patch"
_LOGGER = component_logger("mcp")
_PROFILE_MANAGER_LOCK = threading.RLock()
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_COLON_PATTERN = re.compile(r"\$?\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_SAFE_NAME_PATTERN = re.compile(r"[^a-z0-9]+")
_HERMES_MCP_KEYS = {
    "type",
    "command",
    "args",
    "env",
    "url",
    "headers",
    "ssl_verify",
    "client_cert",
    "client_key",
    "timeout",
    "connect_timeout",
    "supports_parallel_tool_calls",
    "skip_preflight",
    "transport",
    "keepalive_interval",
    "idle_timeout_seconds",
    "max_lifetime_seconds",
    "lifecycle",
    "tools",
    "auth",
    "oauth",
    "sampling",
    "elicitation",
}
_UNSUPPORTED_CODEX_KEYS = {
    "cwd",
    "default_tools_approval_mode",
    "experimental_environment",
    "required",
    "tools",
}


class McpConfigError(ValueError):
    """Raised when a workspace MCP file cannot be converted safely."""


class WorkspaceMcpWarning(UserWarning):
    """Reports a skipped workspace MCP entry without exposing its values."""


def _warn_unsupported(source_kind: str, server_name: str, keys: set[str], source: str) -> None:
    key_names = ", ".join(sorted(keys))
    warnings.warn(
        f"skipping {source_kind} MCP server {server_name!r} from {source}: unsupported setting(s): {key_names}",
        WorkspaceMcpWarning,
        stacklevel=3,
    )


def _validate_string_mapping(value: Any, *, label: str, server_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise McpConfigError(f"MCP server {server_name!r} {label} must be an object")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise McpConfigError(f"MCP server {server_name!r} {label} names and values must be strings")


@dataclass(frozen=True)
class _McpEntry:
    name: str
    config: dict[str, Any]
    source: Path


@dataclass
class _WorkspaceMcpGeneration:
    workspace_name: str
    source_identity: tuple[str, str, str | None, bool]
    owner_id: int
    logical_configs: dict[str, dict[str, Any]]
    logical_names: dict[str, str]
    servers: dict[str, dict[str, Any]]
    generation_id: str
    active_turns: int = 0


@dataclass(frozen=True)
class WorkspaceMcpBinding:
    token: Token[_WorkspaceMcpGeneration | None]
    generation: _WorkspaceMcpGeneration


_CURRENT_MCP_GENERATION: ContextVar[_WorkspaceMcpGeneration | None] = ContextVar(
    "hermes_workspace_context_current_mcp_generation",
    default=None,
)


def _safe_mode_enabled() -> bool:
    try:
        from utils import env_var_enabled

        return bool(env_var_enabled("HERMES_SAFE_MODE"))
    except Exception:
        return str(os.environ.get("HERMES_SAFE_MODE", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }


def _config_fingerprint(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _workspace_source_identity(workspace: Workspace) -> tuple[str, str, str | None, bool]:
    """Identify the project MCP source, not only its logical workspace name."""
    cwd = str(workspace.cwd.expanduser().resolve())
    mcp_file = (
        str(workspace.mcp_file.expanduser().resolve())
        if workspace.mcp_file is not None
        else None
    )
    return workspace.name, cwd, mcp_file, workspace.auto_detect_mcp


def _profile_home_key() -> str:
    """Return the active Hermes profile key without consulting workspace routing."""
    try:
        from hermes_constants import hermes_home_key

        return str(hermes_home_key())
    except Exception:
        fallback = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        return str(Path(fallback).expanduser().resolve())


class WorkspaceMcpManager:
    """Track current and draining workspace MCP generations without touching Hermes core."""

    def __init__(
        self,
        *,
        config_provider: Any,
        workspaces: Mapping[str, Workspace] | None = None,
        security_filter: Callable[[dict[str, dict[str, Any]]], dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        self._config_provider = config_provider
        self._fallback_workspaces = dict(workspaces or {})
        self._security_filter = security_filter
        self._current: dict[str, _WorkspaceMcpGeneration] = {}
        self._draining: dict[str, list[_WorkspaceMcpGeneration]] = {}
        self._used_names: dict[tuple[str, str], set[str]] = {}
        self._last_good_entries: dict[
            tuple[str, str, str | None, bool], dict[str, dict[str, Any]]
        ] = {}
        self._blocked_names: set[str] = set()
        self._lock = threading.RLock()

    def update_source(self, *, config_provider: Any, workspaces: Mapping[str, Workspace] | None = None) -> None:
        with self._lock:
            if config_provider is not self._config_provider:
                self._current.clear()
                self._draining.clear()
                self._used_names.clear()
                self._last_good_entries.clear()
                self._blocked_names.clear()
            self._config_provider = config_provider
            if workspaces is not None:
                self._fallback_workspaces = dict(workspaces)

    def _router_config(self) -> RouterConfig:
        provider = self._config_provider
        if hasattr(provider, "load"):
            config = provider.load()
        elif callable(provider):
            config = provider()
        else:
            config = RouterConfig(workspaces=self._fallback_workspaces, routes=())
        if not isinstance(config, RouterConfig):
            raise McpConfigError("workspace config provider returned an invalid router config")
        return config

    def _read_entries(self, workspace: Workspace) -> dict[str, dict[str, Any]]:
        source_identity = _workspace_source_identity(workspace)
        try:
            entries = load_workspace_mcp_servers(workspace)
        except McpConfigError as exc:
            with self._lock:
                previous = self._last_good_entries.get(source_identity)
            if previous is not None:
                log_event(
                    _LOGGER,
                    "workspace_mcp_reload",
                    result="preserved_last_good",
                    workspace=workspace.name,
                    error_type=type(exc).__name__,
                    server_count=len(previous),
                    **runner_fields(),
                )
                return deepcopy(previous)
            log_event(
                _LOGGER,
                "workspace_mcp_reload",
                result="skipped_invalid",
                workspace=workspace.name,
                error_type=type(exc).__name__,
                server_count=0,
                **runner_fields(),
            )
            return {}
        if self._security_filter is not None:
            try:
                entries = self._security_filter(entries)
            except Exception as exc:
                log_event(
                    _LOGGER,
                    "workspace_mcp_reload",
                    result="skipped_security_filter_error",
                    workspace=workspace.name,
                    error_type=type(exc).__name__,
                    server_count=0,
                    **runner_fields(),
                )
                entries = {}
        safe_entries = deepcopy(entries)
        with self._lock:
            self._last_good_entries[source_identity] = safe_entries
        return deepcopy(safe_entries)

    def _allocate_name(self, workspace: Workspace, logical_name: str, config: Mapping[str, Any]) -> str:
        base = logical_name
        key = (workspace.name, logical_name)
        used = self._used_names.setdefault(key, set())
        if not used:
            candidate = base
        else:
            candidate = f"{base}-g{_config_fingerprint(config)[:12]}"
            suffix = 2
            while candidate in used:
                candidate = f"{base}-g{_config_fingerprint(config)[:12]}-{suffix}"
                suffix += 1
        used.add(candidate)
        return candidate

    def _prune_draining(self, workspace_name: str) -> None:
        self._draining[workspace_name] = [
            generation
            for generation in self._draining.get(workspace_name, ())
            if generation.active_turns > 0
        ]
        if not self._draining[workspace_name]:
            self._draining.pop(workspace_name, None)

    def refresh_workspace(self, workspace: Workspace) -> _WorkspaceMcpGeneration:
        entries = self._read_entries(workspace)
        source_identity = _workspace_source_identity(workspace)
        with self._lock:
            current = self._current.get(workspace.name)
            if current is not None and current.source_identity == source_identity:
                if current.logical_configs == entries:
                    self._prune_draining(workspace.name)
                    return current
                same_source = True
            else:
                same_source = False
            logical_names: dict[str, str] = {}
            servers: dict[str, dict[str, Any]] = {}
            for logical_name, config in entries.items():
                if (
                    same_source
                    and current.logical_configs.get(logical_name) == config
                    and logical_name in current.logical_names
                ):
                    actual_name = current.logical_names[logical_name]
                else:
                    actual_name = self._allocate_name(workspace, logical_name, config)
                logical_names[logical_name] = actual_name
                servers[actual_name] = deepcopy(config)
            generation = _WorkspaceMcpGeneration(
                workspace_name=workspace.name,
                source_identity=source_identity,
                owner_id=id(self),
                logical_configs=deepcopy(entries),
                logical_names=logical_names,
                servers=servers,
                generation_id=_config_fingerprint(
                    {
                        "workspace": workspace.name,
                        "source": source_identity,
                        "servers": servers,
                    }
                ),
            )
            if current is not None and current.active_turns > 0:
                self._draining.setdefault(workspace.name, []).append(current)
            self._current[workspace.name] = generation
            self._prune_draining(workspace.name)
            return generation

    def refresh_all(self, workspaces: Mapping[str, Workspace]) -> None:
        for workspace in workspaces.values():
            self.refresh_workspace(workspace)
        with self._lock:
            removed = set(self._current) - set(workspaces)
            for workspace_name in removed:
                current = self._current.pop(workspace_name)
                if current.active_turns > 0:
                    self._draining.setdefault(workspace_name, []).append(current)
                self._prune_draining(workspace_name)

    def all_servers(self, native_names: set[str]) -> dict[str, dict[str, Any]]:
        if _safe_mode_enabled():
            return {}
        config = self._router_config()
        self.refresh_all(config.workspaces)
        with self._lock:
            self._blocked_names = set(native_names)
            generations = list(self._current.values())
            for draining in self._draining.values():
                generations.extend(generation for generation in draining if generation.active_turns > 0)
            servers: dict[str, dict[str, Any]] = {}
            for generation in generations:
                for name, server_config in generation.servers.items():
                    if name not in self._blocked_names:
                        servers[name] = {"lazy": True, **deepcopy(server_config)}
            return servers

    def bind(self, workspace: Workspace) -> WorkspaceMcpBinding:
        generation = self.refresh_workspace(workspace)
        with self._lock:
            generation.active_turns += 1
        return WorkspaceMcpBinding(_CURRENT_MCP_GENERATION.set(generation), generation)

    def reset(self, binding: WorkspaceMcpBinding) -> None:
        _CURRENT_MCP_GENERATION.reset(binding.token)
        with self._lock:
            binding.generation.active_turns = max(0, binding.generation.active_turns - 1)
            self._prune_draining(binding.generation.workspace_name)

    def current_servers(self, workspace: Workspace) -> dict[str, dict[str, Any]]:
        if _safe_mode_enabled():
            return {}
        generation = _CURRENT_MCP_GENERATION.get()
        if (
            generation is None
            or generation.owner_id != id(self)
            or generation.source_identity != _workspace_source_identity(workspace)
        ):
            generation = self.refresh_workspace(workspace)
        with self._lock:
            return {
                name: deepcopy(config)
                for name, config in generation.servers.items()
                if name not in self._blocked_names
            }


def current_mcp_generation() -> _WorkspaceMcpGeneration | None:
    return _CURRENT_MCP_GENERATION.get()


def _slug(value: str) -> str:
    slug = _SAFE_NAME_PATTERN.sub("-", value.strip().lower()).strip("-")
    if not slug:
        raise McpConfigError(f"MCP name has no safe characters: {value!r}")
    return slug


def namespace_server_name(workspace_name: str, server_name: str) -> str:
    return f"workspace-{_slug(workspace_name)}-{_slug(server_name)}"


def _source_label(path: Path, workspace: Workspace) -> str:
    try:
        return str(path.relative_to(workspace.cwd))
    except ValueError:
        return path.name


def _expand_string(value: str) -> str:
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            missing.add(name)
            return match.group(0)
        return os.environ[name]

    normalized = _ENV_COLON_PATTERN.sub(lambda match: "${" + match.group(1) + "}", value)
    expanded = os.path.expanduser(_ENV_PATTERN.sub(replace, normalized))
    if missing:
        names = ", ".join(sorted(missing))
        raise McpConfigError(f"MCP configuration references unset environment variable(s): {names}")
    return expanded


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _expand_string(value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _expand(item) for key, item in value.items()}
    return value


def _convert_server(workspace: Workspace, server_name: str, raw: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise McpConfigError(f"MCP server {server_name!r} must be an object")
    _validate_string_mapping(raw.get("env"), label="env", server_name=server_name)
    _validate_string_mapping(raw.get("headers"), label="header", server_name=server_name)
    config = _expand(dict(raw))
    transport_type = str(config.pop("type", "") or "").strip().lower()

    has_command = isinstance(config.get("command"), str) and bool(config["command"].strip())
    has_url = isinstance(config.get("url"), str) and bool(config["url"].strip())
    if transport_type in {"stdio", "local"} or (not transport_type and has_command):
        if transport_type == "local" and isinstance(config.get("command"), list):
            command = config["command"]
            if not command or not all(isinstance(item, str) for item in command):
                raise McpConfigError(f"local MCP server {server_name!r} command must be a non-empty list")
            config["command"] = command[0]
            config["args"] = command[1:]
            has_command = True
        if not has_command:
            raise McpConfigError(f"stdio MCP server {server_name!r} requires command")
        if has_url:
            raise McpConfigError(f"stdio MCP server {server_name!r} cannot also define url")
        args = config.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise McpConfigError(f"stdio MCP server {server_name!r} args must be a list of strings")
        config["args"] = args
    elif transport_type in {"http", "streamable-http", "sse", "remote"} or (not transport_type and has_url):
        if not has_url:
            raise McpConfigError(f"{transport_type or 'HTTP'} MCP server {server_name!r} requires url")
        if has_command:
            raise McpConfigError(f"HTTP MCP server {server_name!r} cannot also define command")
        if transport_type == "sse":
            config["transport"] = "sse"
        headers = config.get("headers")
        if headers is not None and not isinstance(headers, dict):
            raise McpConfigError(f"HTTP MCP server {server_name!r} headers must be an object")
    else:
        raise McpConfigError(
            f"MCP server {server_name!r} must define type stdio/http/sse, command, or url"
        )

    return namespace_server_name(workspace.name, server_name), config


def _strip_jsonc_comments(text: str) -> str:
    result: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
        elif char == "/" and next_char == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
        elif char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                index += 1
            if index + 1 >= len(text):
                raise McpConfigError("unterminated JSONC block comment")
            index += 2
        else:
            result.append(char)
            index += 1
    return _strip_jsonc_trailing_commas("".join(result))


def _strip_jsonc_trailing_commas(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            result.append(char)
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                continue
        result.append(char)
    return "".join(result)


def _read_mapping(path: Path, kind: str, source: str) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        if kind == "json":
            raw = json.loads(text)
        elif kind == "jsonc":
            raw = json.loads(_strip_jsonc_comments(text))
        elif kind == "toml":
            raw = tomllib.loads(text)
        else:
            raw = yaml.safe_load(text) or {}
    except McpConfigError as exc:
        raise McpConfigError(
            f"cannot read workspace MCP file {source}: {type(exc).__name__}"
        ) from None
    except UnicodeDecodeError:
        raise McpConfigError(
            f"cannot read workspace MCP file {source}: UnicodeDecodeError"
        ) from None
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
        raise McpConfigError(
            f"cannot read workspace MCP file {source}: {type(exc).__name__}"
        ) from None
    if not isinstance(raw, Mapping):
        raise McpConfigError(f"workspace MCP file must contain an object: {source}")
    return raw


def _normalize_entry(source_kind: str, raw: Any, server_name: str, source: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise McpConfigError(f"MCP server {server_name!r} must be an object")
    config = dict(raw)
    for key, label in (
        ("env", "env"),
        ("environment", "env"),
        ("headers", "header"),
        ("http_headers", "header"),
        ("env_http_headers", "header"),
    ):
        if key in config:
            _validate_string_mapping(config[key], label=label, server_name=server_name)
    unsupported = _UNSUPPORTED_CODEX_KEYS.intersection(config) if source_kind == "codex" else set()
    if unsupported:
        _warn_unsupported(source_kind, server_name, {str(key) for key in unsupported}, source)
        return {}
    if config.get("enabled", True) is False:
        return {}
    config.pop("enabled", None)
    transport_aliases = {"streamable-http": "http", "remote": "http", "local": "stdio"}
    if isinstance(config.get("type"), str):
        config["type"] = transport_aliases.get(config["type"].lower(), config["type"].lower())
    if source_kind == "claude":
        timeout_ms = config.get("timeout")
        if timeout_ms is not None:
            if not isinstance(timeout_ms, (int, float)) or timeout_ms < 0:
                raise McpConfigError(f"Claude MCP server {server_name!r} timeout must be a non-negative number")
            config["timeout"] = timeout_ms / 1000
    elif source_kind == "opencode":
        command = config.get("command")
        if isinstance(command, list):
            if not command or not all(isinstance(item, str) for item in command):
                raise McpConfigError(f"OpenCode MCP server {server_name!r} command must be a non-empty list")
            config["command"] = command[0]
            config["args"] = command[1:]
        if "environment" in config:
            config["env"] = config.pop("environment")
        timeout_ms = config.pop("timeout", None)
        if timeout_ms is not None:
            if not isinstance(timeout_ms, (int, float)) or timeout_ms < 0:
                raise McpConfigError(f"OpenCode MCP server {server_name!r} timeout must be a non-negative number")
            config["timeout"] = timeout_ms / 1000
        oauth = config.get("oauth")
        if oauth is False:
            config["auth"] = "none"
            config.pop("oauth", None)
        elif oauth is True:
            config["auth"] = "oauth"
            config.pop("oauth", None)
        elif isinstance(oauth, Mapping):
            config["auth"] = "oauth"
            oauth_aliases: dict[str, str] = {
                "clientId": "client_id",
                "clientSecret": "client_secret",
                "scope": "scope",
            }
            config["oauth"] = {oauth_aliases.get(str(key), str(key)): value for key, value in oauth.items()}
    elif source_kind == "codex":
        aliases = {
            "startup_timeout_sec": "connect_timeout",
            "tool_timeout_sec": "timeout",
        }
        config = {aliases.get(key, key): value for key, value in config.items()}
        if config.get("auth") == "chatgpt":
            _warn_unsupported(source_kind, server_name, {"auth"}, source)
            return {}
        env_vars = config.pop("env_vars", [])
        if env_vars:
            if not isinstance(env_vars, list) or not all(isinstance(name, str) for name in env_vars):
                raise McpConfigError(f"Codex MCP server {server_name!r} env_vars must be a list of strings")
            env = dict(config.get("env", {}))
            env.update({name: "${" + name + "}" for name in env_vars})
            config["env"] = env
        headers = dict(config.pop("http_headers", config.get("headers", {})) or {})
        env_headers = config.pop("env_http_headers", {}) or {}
        if not isinstance(env_headers, Mapping):
            raise McpConfigError(f"Codex MCP server {server_name!r} env_http_headers must be an object")
        headers.update({str(key): "${" + str(value) + "}" for key, value in env_headers.items()})
        bearer_env = config.pop("bearer_token_env_var", None)
        if bearer_env is not None:
            if not isinstance(bearer_env, str) or not bearer_env.strip():
                raise McpConfigError(f"Codex MCP server {server_name!r} bearer_token_env_var must be a string")
            headers["Authorization"] = "Bearer ${" + bearer_env + "}"
        if headers:
            config["headers"] = headers
        include = config.pop("enabled_tools", None)
        exclude = config.pop("disabled_tools", None)
        if include is not None or exclude is not None:
            config["tools"] = {
                **({"include": include} if include is not None else {}),
                **({"exclude": exclude} if exclude is not None else {}),
            }
    if source_kind in {"claude", "codex", "opencode"} and isinstance(config.get("url"), str):
        headers = config.get("headers", {})
        has_authorization = isinstance(headers, Mapping) and any(
            str(key).lower() == "authorization" for key in headers
        )
        if "auth" not in config and not has_authorization:
            config["auth"] = "oauth"
    if source_kind != "hermes":
        unsupported = {str(key) for key in config if key not in _HERMES_MCP_KEYS}
        if unsupported:
            _warn_unsupported(source_kind, server_name, unsupported, source)
            return {}
    return config


def _entries_from(
    path: Path,
    source_kind: str,
    root_key: str,
    file_kind: str,
    source: str,
) -> list[_McpEntry]:
    raw = _read_mapping(path, file_kind, source)
    servers = raw.get(root_key, {})
    if not isinstance(servers, Mapping):
        raise McpConfigError(f"workspace MCP file must contain a {root_key} object: {source}")
    entries: list[_McpEntry] = []
    for name, server_raw in servers.items():
        if not isinstance(name, str) or not name.strip():
            raise McpConfigError("MCP server names must be non-empty strings")
        config = _normalize_entry(source_kind, server_raw, name, source)
        if config:
            entries.append(_McpEntry(name=name, config=config, source=path))
    return entries


def _detected_entries(workspace: Workspace) -> list[_McpEntry]:
    candidates = (
        (workspace.cwd / ".mcp.json", "claude", "mcpServers", "json"),
        (workspace.cwd / ".codex" / "config.toml", "codex", "mcp_servers", "toml"),
        (workspace.cwd / "opencode.json", "opencode", "mcp", "json"),
        (workspace.cwd / "opencode.jsonc", "opencode", "mcp", "jsonc"),
        (workspace.cwd / ".hermes" / "config.yaml", "hermes", "mcp_servers", "yaml"),
        (workspace.cwd / ".hermes" / "config.yml", "hermes", "mcp_servers", "yaml"),
    )
    entries: list[_McpEntry] = []
    for path, source_kind, root_key, file_kind in candidates:
        if path.is_file():
            entries.extend(
                _entries_from(
                    path,
                    source_kind,
                    root_key,
                    file_kind,
                    str(path.relative_to(workspace.cwd)),
                )
            )
    return entries


def _same_connection(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if "url" in left or "url" in right:
        return left.get("url") == right.get("url") and "command" not in left and "command" not in right
    return left.get("command") == right.get("command") and left.get("args", []) == right.get("args", [])


def _merge_mapping(target: dict[str, Any], incoming: Mapping[str, Any], prefix: str = "") -> str | None:
    for key, value in incoming.items():
        key_path = f"{prefix}.{key}" if prefix else str(key)
        if key not in target:
            target[key] = value
            continue
        existing = target[key]
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            nested = dict(existing)
            conflict = _merge_mapping(nested, value, key_path)
            if conflict is not None:
                return conflict
            target[key] = nested
        elif existing != value:
            return key_path
    return None


def _merge_entries(workspace: Workspace, entries: list[_McpEntry]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[_McpEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.name, []).append(entry)

    candidates: list[tuple[str, str, dict[str, Any], tuple[str, ...]]] = []
    for server_name, matches in grouped.items():
        if any(not _same_connection(matches[0].config, item.config) for item in matches[1:]):
            sources = ", ".join(_source_label(item.source, workspace) for item in matches)
            warnings.warn(
                f"skipping conflicting workspace MCP server {server_name!r} from {sources}",
                WorkspaceMcpWarning,
                stacklevel=2,
            )
            continue
        merged: dict[str, Any] = {}
        for match in matches:
            conflict = _merge_mapping(merged, match.config)
            if conflict is not None:
                sources = ", ".join(_source_label(item.source, workspace) for item in matches)
                warnings.warn(
                    f"skipping workspace MCP server {server_name!r} with incompatible {conflict!r} settings from {sources}",
                    WorkspaceMcpWarning,
                    stacklevel=2,
                )
                merged = {}
                break
            if not merged:
                break
        if not merged:
            continue
        headers = merged.get("headers", {})
        if isinstance(headers, Mapping) and any(str(key).lower() == "authorization" for key in headers):
            merged.pop("auth", None)
            merged.pop("oauth", None)
        namespaced, config = _convert_server(workspace, server_name, merged)
        sources = tuple(_source_label(item.source, workspace) for item in matches)
        candidates.append((server_name, namespaced, config, sources))

    by_namespace: dict[str, list[tuple[str, dict[str, Any], tuple[str, ...]]]] = {}
    for server_name, namespaced, config, sources in candidates:
        by_namespace.setdefault(namespaced, []).append((server_name, config, sources))

    servers: dict[str, dict[str, Any]] = {}
    for namespaced, matches in by_namespace.items():
        if len(matches) > 1:
            names = ", ".join(repr(name) for name, _, _ in matches)
            sources = ", ".join(sorted({source for _, _, item_sources in matches for source in item_sources}))
            warnings.warn(
                f"skipping workspace MCP namespace collision {namespaced!r} "
                f"from server names {names} in {sources}",
                WorkspaceMcpWarning,
                stacklevel=2,
            )
            continue
        servers[namespaced] = matches[0][1]
    return servers


def load_workspace_mcp_servers(
    workspace: Workspace,
    *,
    auto_detect: bool | None = None,
) -> dict[str, dict[str, Any]]:
    use_auto_detect = workspace.auto_detect_mcp if auto_detect is None else auto_detect
    if use_auto_detect:
        return _merge_entries(workspace, _detected_entries(workspace))
    path = workspace.mcp_file
    if path is None or not path.exists():
        return {}
    if not path.is_file():
        raise McpConfigError(f"workspace MCP path is not a file: {path}")
    try:
        source = str(path.relative_to(workspace.cwd))
    except ValueError:
        source = path.name
    return _merge_entries(workspace, _entries_from(path, "claude", "mcpServers", "json", source))


def _core_reauth_oauth_server() -> Callable[..., bool]:
    """Return Hermes' private OAuth reauthentication adapter."""
    try:
        from hermes_cli.mcp_config import _reauth_oauth_server
    except (ImportError, AttributeError):
        raise McpConfigError(
            "Hermes MCP OAuth reauthentication API is unavailable in this runtime"
        ) from None
    if not callable(_reauth_oauth_server):
        raise McpConfigError(
            "Hermes MCP OAuth reauthentication API is unavailable in this runtime"
        )
    return cast(Callable[..., bool], _reauth_oauth_server)


def reauth_workspace_mcp_server(
    workspace: Workspace,
    server_name: str,
    *,
    flow: str | None = None,
) -> bool:
    """Re-authenticate one namespaced OAuth MCP server from workspace config.

    The namespace is passed to Hermes so the OAuth token remains scoped to this
    workspace entry rather than colliding with a global server of the same name.
    """
    namespaced_name = namespace_server_name(workspace.name, server_name)
    servers = load_workspace_mcp_servers(workspace)
    server_config = servers.get(namespaced_name)
    if server_config is None:
        raise McpConfigError(
            f"unknown MCP server {server_name!r} in workspace {workspace.name!r}"
        )
    if not server_config.get("url"):
        raise McpConfigError(
            f"workspace MCP server {server_name!r} is not OAuth-capable (no URL)"
        )
    if server_config.get("auth") != "oauth":
        raise McpConfigError(
            f"workspace MCP server {server_name!r} is not configured for OAuth"
        )
    return bool(_core_reauth_oauth_server()(namespaced_name, server_config, flow=flow))


def _install_workspace_mcp_loader(
    *,
    workspaces: Mapping[str, Workspace],
    config_provider: Any,
) -> WorkspaceMcpManager:
    try:
        from tools import mcp_tool_config
    except ImportError:
        raise McpConfigError("Hermes MCP config loader is unavailable in this runtime") from None
    original_loader = getattr(mcp_tool_config, "_load_mcp_config", None)
    security_filter = getattr(mcp_tool_config, "_filter_suspicious_mcp_servers", None)
    if not callable(original_loader) or not callable(security_filter):
        raise McpConfigError("Hermes MCP config loader/security API is unavailable in this runtime")

    profile_key = _profile_home_key()
    with _PROFILE_MANAGER_LOCK:
        existing = getattr(mcp_tool_config, _CONFIG_PATCH_MARKER, None)
        if isinstance(existing, dict):
            saved_loader = existing.get("original_loader")
            if callable(saved_loader):
                original_loader = cast(Callable[[], dict[str, dict[str, Any]]], saved_loader)
            managers = existing.get("managers")
            if not isinstance(managers, dict):
                managers = {}
        else:
            managers = {}
        manager = managers.get(profile_key)
        if not isinstance(manager, WorkspaceMcpManager):
            manager = WorkspaceMcpManager(
                config_provider=config_provider,
                workspaces=workspaces,
                security_filter=security_filter,
            )
            managers[profile_key] = manager
        else:
            manager.update_source(config_provider=config_provider, workspaces=workspaces)

    def routed_load_mcp_config() -> dict[str, dict[str, Any]]:
        native = original_loader()
        if not isinstance(native, dict) or _safe_mode_enabled():
            return native if isinstance(native, dict) else {}
        with _PROFILE_MANAGER_LOCK:
            marker = getattr(mcp_tool_config, _CONFIG_PATCH_MARKER, None)
            managers = marker.get("managers") if isinstance(marker, dict) else None
            active_manager = (
                managers.get(_profile_home_key())
                if isinstance(managers, dict)
                else None
            )
        if not isinstance(active_manager, WorkspaceMcpManager):
            return native
        workspace_servers = active_manager.all_servers(set(native))
        merged = dict(native)
        merged.update({name: config for name, config in workspace_servers.items() if name not in merged})
        return merged

    with _PROFILE_MANAGER_LOCK:
        current_marker = getattr(mcp_tool_config, _CONFIG_PATCH_MARKER, None)
        if not isinstance(current_marker, dict):
            setattr(mcp_tool_config, "_load_mcp_config", routed_load_mcp_config)
            setattr(
                mcp_tool_config,
                _CONFIG_PATCH_MARKER,
                {"managers": managers, "original_loader": original_loader},
            )
        else:
            current_marker["managers"] = managers
    return manager


def install_mcp_patches(
    *,
    workspaces: Mapping[str, Workspace],
    config_provider: Any | None = None,
    tools_config: Any | None = None,
    register_mcp_servers: Callable[[dict[str, dict[str, Any]]], list[str]] | None = None,
) -> WorkspaceMcpManager:
    """Wrap Hermes' loader and expose only the routed workspace's current MCP generation."""
    if tools_config is None:
        from hermes_cli import tools_config as tools_config
    if register_mcp_servers is None:
        from tools.mcp_tool_discovery import register_mcp_servers
    register_servers = cast(Callable[[dict[str, dict[str, Any]]], list[str]], register_mcp_servers)
    if config_provider is None:
        config_provider = lambda: RouterConfig(workspaces=dict(workspaces), routes=())

    manager = _install_workspace_mcp_loader(
        workspaces=workspaces,
        config_provider=config_provider,
    )

    if getattr(tools_config, _PATCH_MARKER, False):
        return manager
    original_get_platform_tools = getattr(tools_config, "_get_platform_tools", None)
    if not callable(original_get_platform_tools):
        raise McpConfigError("Hermes tools_config is missing _get_platform_tools private API")

    def _active_profile_manager() -> WorkspaceMcpManager | None:
        try:
            from tools import mcp_tool_config
        except ImportError:
            return None
        with _PROFILE_MANAGER_LOCK:
            marker = getattr(mcp_tool_config, _CONFIG_PATCH_MARKER, None)
            if not isinstance(marker, dict):
                return None
            managers = marker.get("managers")
            if not isinstance(managers, dict):
                return None
            manager = managers.get(_profile_home_key())
            return manager if isinstance(manager, WorkspaceMcpManager) else None

    def _register_workspace_servers_in_active_profile(workspace: Workspace) -> dict[str, dict[str, Any]]:
        active_manager = _active_profile_manager()
        if active_manager is None:
            return {}
        scoped_servers = {
            name: {"lazy": True, **config}
            for name, config in active_manager.current_servers(workspace).items()
        }
        try:
            from tools.mcp_tool import _mcp_registry_scope
            from tools.registry import registry
            scoped = _mcp_registry_scope()
        except (ImportError, AttributeError):
            scoped = None
        if scoped is not None and scoped_servers:
            missing_servers = {
                name: config
                for name, config in scoped_servers.items()
                if not any(
                    registry.snapshot_registration(tool_name, scope=scoped) is not None
                    for tool_name in registry.get_tool_names_for_toolset(f"mcp-{name}")
                )
            }
            registered = register_servers(missing_servers) if missing_servers else []
            log_event(
                _LOGGER,
                "scoped_mcp_registration",
                workspace=workspace.name,
                candidate_count=len(missing_servers),
                registered_count=len(registered),
                **runner_fields(),
            )
        return scoped_servers

    def routed_get_platform_tools(config: dict, platform: str, **kwargs: Any):
        enabled = set(original_get_platform_tools(config, platform, **kwargs))
        base_count = len(enabled)
        workspace = current_workspace()
        added_count = 0
        candidate_count = 0
        workspace_name = "none"
        if workspace is not None:
            workspace_name = workspace.name
            scoped_servers = _register_workspace_servers_in_active_profile(workspace)
            candidate_count = len(scoped_servers)
            for server_name in scoped_servers:
                toolset_name = f"mcp-{server_name}"
                if toolset_name not in enabled:
                    enabled.add(toolset_name)
                    added_count += 1
        log_event(
            _LOGGER,
            "enabled_toolsets",
            platform=platform,
            workspace=workspace_name,
            base_toolset_count=base_count,
            workspace_mcp_candidate_count=candidate_count,
            workspace_mcp_added_count=added_count,
            resolved_toolset_count=len(enabled),
            **runner_fields(),
        )
        return enabled

    tools_config._get_platform_tools = routed_get_platform_tools
    setattr(tools_config, _PATCH_MARKER, True)
    return manager
