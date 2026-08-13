"""Workspace-local MCP configuration and Hermes registry adapters."""

from __future__ import annotations

import json
import os
import re
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from .config import Workspace
from .runtime import current_workspace


_PATCH_MARKER = "_hermes_workspace_context_mcp_patch"
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


def install_mcp_patches(
    *,
    workspaces: Mapping[str, Workspace],
    tools_config: Any | None = None,
    register_mcp_servers: Callable[[dict[str, dict[str, Any]]], list[str]] | None = None,
) -> None:
    """Register namespaced project MCP servers and scope their toolsets by task."""
    if tools_config is None:
        from hermes_cli import tools_config as tools_config
    if register_mcp_servers is None:
        from tools.mcp_tool import register_mcp_servers

    if getattr(tools_config, _PATCH_MARKER, False):
        return
    original_get_platform_tools = getattr(tools_config, "_get_platform_tools", None)
    if not callable(original_get_platform_tools):
        raise McpConfigError("Hermes tools_config is missing _get_platform_tools private API")

    server_names_by_workspace: dict[str, tuple[str, ...]] = {}
    all_servers: dict[str, dict[str, Any]] = {}
    for workspace in workspaces.values():
        servers = load_workspace_mcp_servers(workspace)
        overlap = set(all_servers).intersection(servers)
        if overlap:
            raise McpConfigError(f"duplicate namespaced MCP server(s): {', '.join(sorted(overlap))}")
        all_servers.update(servers)
        server_names_by_workspace[workspace.name] = tuple(servers)

    register_mcp_servers(all_servers)

    def routed_get_platform_tools(config: dict, platform: str, **kwargs: Any):
        enabled = set(original_get_platform_tools(config, platform, **kwargs))
        workspace = current_workspace()
        if workspace is None:
            return enabled
        for server_name in server_names_by_workspace.get(workspace.name, ()):
            enabled.add(f"mcp-{server_name}")
        return enabled

    tools_config._get_platform_tools = routed_get_platform_tools
    setattr(tools_config, _PATCH_MARKER, True)
