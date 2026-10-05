from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from workspace_context import mcp
from workspace_context.plugin import register_plugin


class FakePluginContext:
    def __init__(self) -> None:
        self.hooks: list[tuple[str, object]] = []
        self.cli_commands: dict[str, dict[str, object]] = {}

    def register_hook(self, name: str, callback) -> None:
        self.hooks.append((name, callback))

    def register_cli_command(self, name: str, help: str, setup_fn, handler_fn=None, description: str = ""):
        self.cli_commands[name] = {
            "help": help,
            "setup_fn": setup_fn,
            "handler_fn": handler_fn,
            "description": description,
        }


def write_config(plugin_dir: Path, project: Path, server: str = "remote") -> None:
    (plugin_dir / "config.yaml").write_text(
        f"""
workspaces:
  app:
    cwd: {project}
    skill_dirs: []
    mcp_file: .mcp.json
    auto_detect_mcp: false
routes: []
""",
        encoding="utf-8",
    )
    (project / ".mcp.json").write_text(
        f"""
{{
  "mcpServers": {{
    "{server}": {{
      "type": "http",
      "url": "https://example.invalid/mcp",
      "auth": "oauth"
    }}
  }}
}}
""",
        encoding="utf-8",
    )


def registered_parser(ctx: FakePluginContext) -> argparse.ArgumentParser:
    command = ctx.cli_commands["workspace-context"]
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)
    return parser


def register_cli_plugin(plugin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> FakePluginContext:
    monkeypatch.setattr(sys, "argv", ["hermes", "workspace-context", "mcp", "reauth"])
    ctx = FakePluginContext()
    register_plugin(
        ctx,
        plugin_dir=plugin_dir,
        install_skill_patches=lambda: pytest.fail("skill patches must not load for plugin CLI discovery"),
        install_mcp_patches=lambda **_kwargs: pytest.fail("MCP servers must not load for plugin CLI discovery"),
    )
    return ctx


def test_workspace_context_cli_discovery_is_lazy_without_runtime_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = register_cli_plugin(tmp_path / "plugin", monkeypatch)

    assert set(ctx.cli_commands) == {"workspace-context"}
    assert ctx.hooks == []


def test_reauth_cli_uses_namespaced_server_and_selected_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir = tmp_path / "plugin"
    project = tmp_path / "project"
    plugin_dir.mkdir()
    project.mkdir()
    write_config(plugin_dir, project)
    ctx = register_cli_plugin(plugin_dir, monkeypatch)
    parser = registered_parser(ctx)
    args = parser.parse_args(["mcp", "reauth", "app", "remote", "--flow", "device"])
    calls: list[tuple[str, dict, str | None]] = []

    monkeypatch.setattr(
        mcp,
        "_core_reauth_oauth_server",
        lambda: lambda name, config, *, flow=None: calls.append((name, config, flow)) or True,
    )

    assert ctx.cli_commands["workspace-context"]["handler_fn"](args) == 0
    assert calls == [
        (
            "workspace-app-remote",
            {"url": "https://example.invalid/mcp", "auth": "oauth"},
            "device",
        )
    ]


def test_reauth_cli_rejects_unknown_server_and_non_oauth_without_auth_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plugin_dir = tmp_path / "plugin"
    project = tmp_path / "project"
    plugin_dir.mkdir()
    project.mkdir()
    write_config(plugin_dir, project, server="local")
    (project / ".mcp.json").write_text(
        '{"mcpServers": {"local": {"type": "http", "url": "https://example.invalid/mcp", "auth": "none"}}}',
        encoding="utf-8",
    )
    ctx = register_cli_plugin(plugin_dir, monkeypatch)
    parser = registered_parser(ctx)
    handler = ctx.cli_commands["workspace-context"]["handler_fn"]
    calls: list[tuple[str, dict, str | None]] = []
    monkeypatch.setattr(
        mcp,
        "_core_reauth_oauth_server",
        lambda: lambda name, config, *, flow=None: calls.append((name, config, flow)) or True,
    )

    unknown = parser.parse_args(["mcp", "reauth", "missing", "local"])
    non_oauth = parser.parse_args(["mcp", "reauth", "app", "local"])

    assert handler(unknown) == 2
    assert "unknown workspace" in capsys.readouterr().err
    assert handler(non_oauth) == 2
    captured = capsys.readouterr()
    assert "not configured for OAuth" in captured.err
    assert calls == []


def test_reauth_cli_returns_failure_status_when_oauth_flow_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir = tmp_path / "plugin"
    project = tmp_path / "project"
    plugin_dir.mkdir()
    project.mkdir()
    write_config(plugin_dir, project)
    ctx = register_cli_plugin(plugin_dir, monkeypatch)
    parser = registered_parser(ctx)
    args = parser.parse_args(["mcp", "reauth", "app", "remote"])
    monkeypatch.setattr(
        mcp,
        "_core_reauth_oauth_server",
        lambda: lambda _name, _config, *, flow=None: False,
    )

    assert ctx.cli_commands["workspace-context"]["handler_fn"](args) == 1
