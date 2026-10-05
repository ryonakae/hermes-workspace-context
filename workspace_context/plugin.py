"""Hermes plugin registration for workspace routing."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

from .config import ConfigError, load_config
from .mcp import (
    McpConfigError,
    install_mcp_patches as _install_mcp_patches,
    reauth_workspace_mcp_server,
)
from .runtime import make_pre_gateway_dispatch
from .skills import install_skill_patches as _install_skill_patches


# Keep this list deliberately small and limited to top-level options that consume a
# following value. It lets plugin discovery recognize its own command without
# importing Hermes' private parser helpers or treating a chat prompt as a command.
_CLI_VALUE_OPTIONS = {
    "-p",
    "--profile",
    "-r",
    "--resume",
    "-c",
    "--continue",
    "-s",
    "--skills",
    "-m",
    "--model",
    "-t",
    "--toolsets",
    "--provider",
    "--source",
    "-q",
    "--query",
    "--max-turns",
}


def _first_cli_positional(argv: list[str] | None = None) -> str | None:
    args = list(sys.argv[1:] if argv is None else argv)
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if token.startswith("-"):
            if "=" not in token and token in _CLI_VALUE_OPTIONS:
                index += 2
            else:
                index += 1
            continue
        return token
    return None


def _is_workspace_context_cli_invocation() -> bool:
    return _first_cli_positional() == "workspace-context"


def _setup_workspace_context_cli(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="workspace_context_command", required=True)
    mcp_parser = commands.add_parser("mcp", help="Manage workspace MCP authentication")
    mcp_commands = mcp_parser.add_subparsers(dest="workspace_mcp_command", required=True)
    reauth_parser = mcp_commands.add_parser(
        "reauth",
        help="Re-authenticate one workspace OAuth MCP server",
        description=(
            "Run a fresh OAuth flow for a workspace MCP server without changing "
            "Hermes' global MCP configuration."
        ),
    )
    reauth_parser.add_argument("workspace", help="Configured workspace name")
    reauth_parser.add_argument("server", help="MCP server name from the workspace configuration")
    reauth_parser.add_argument(
        "--flow",
        choices=("browser", "device"),
        default=None,
        help="OAuth flow to use (default: the Hermes MCP OAuth default)",
    )


def _workspace_context_cli_error(message: str) -> int:
    print(f"workspace-context: {message}", file=sys.stderr)
    return 2


def _handle_workspace_context_cli(args: argparse.Namespace, *, plugin_dir: Path) -> int:
    if (
        getattr(args, "workspace_context_command", None) != "mcp"
        or getattr(args, "workspace_mcp_command", None) != "reauth"
    ):
        return _workspace_context_cli_error("unsupported command")

    config_path = plugin_dir / "config.yaml"
    if not config_path.is_file():
        return _workspace_context_cli_error("local config.yaml is missing")
    try:
        config = load_config(config_path)
    except ConfigError:
        return _workspace_context_cli_error("workspace configuration is invalid")
    except OSError:
        return _workspace_context_cli_error("workspace configuration could not be read")

    workspace = config.workspaces.get(args.workspace)
    if workspace is None:
        return _workspace_context_cli_error(f"unknown workspace: {args.workspace!r}")

    try:
        authenticated = reauth_workspace_mcp_server(
            workspace,
            args.server,
            flow=getattr(args, "flow", None),
        )
    except McpConfigError as exc:
        return _workspace_context_cli_error(str(exc))
    return 0 if authenticated else 1


def register_plugin(
    ctx: Any,
    *,
    plugin_dir: Path | None = None,
    install_skill_patches: Callable[[], None] = _install_skill_patches,
    install_mcp_patches: Callable[..., None] = _install_mcp_patches,
) -> None:
    root = Path(plugin_dir or Path(__file__).resolve().parent.parent)
    ctx.register_cli_command(
        name="workspace-context",
        help="Manage workspace context and MCP authentication",
        setup_fn=_setup_workspace_context_cli,
        handler_fn=lambda args: _handle_workspace_context_cli(args, plugin_dir=root),
        description=(
            "Workspace-local context commands. MCP reauthentication is resolved "
            "from the plugin workspace configuration at execution time."
        ),
    )

    # The main CLI discovers plugin commands before argparse runs. Do not load
    # workspace MCP entries during that discovery: a lazy registration without a
    # schema cache still performs the normal eager discovery pass and can connect
    # every configured server just to render help or execute this command.
    if _is_workspace_context_cli_invocation():
        return

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
