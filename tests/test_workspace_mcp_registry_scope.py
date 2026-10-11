"""Regression coverage for workspace MCP registration under Hermes profile scope."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_routed_workspace_mcp_is_visible_in_real_core_registry_scope(tmp_path: Path) -> None:
    """A cached workspace MCP tool must survive the global-to-routed scope boundary."""
    project = tmp_path / "project"
    other_project = tmp_path / "other-project"
    hermes_home = tmp_path / "hermes-home"
    other_profile_home = tmp_path / "other-profile-home"
    project.mkdir()
    other_project.mkdir()
    hermes_home.mkdir()
    other_profile_home.mkdir()
    (project / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sentry": {
                        "type": "http",
                        "url": "https://example.invalid/mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (other_project / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "linear": {
                        "type": "http",
                        "url": "https://example.invalid/linear-mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    repo_root = Path(__file__).resolve().parents[1]
    core_root = Path(sys.executable).resolve().parent.parent.parent
    script = textwrap.dedent(
        """
        import asyncio
        import json
        from pathlib import Path
        from types import SimpleNamespace

        from hermes_constants import (
            hermes_home_key,
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from agent.secret_scope import reset_secret_scope, set_multiplex_active, set_secret_scope
        from model_tools import get_tool_definitions, handle_function_call
        from tools.mcp_schema_cache import config_fingerprint, write_cache_entry
        from tools.mcp_tool_discovery import (
            discover_mcp_tools,
            reconcile_mcp_servers_with_config,
            register_mcp_servers,
        )
        from tools.registry import registry
        from workspace_context.config import Workspace
        from workspace_context.mcp import install_mcp_patches, load_workspace_mcp_servers
        from workspace_context.runtime import _CURRENT_WORKSPACE

        project = Path(__import__("os").environ["PROJECT_DIR"])
        workspace = Workspace(
            name="mog-app",
            cwd=project,
            skill_dirs=(),
            mcp_file=project / ".mcp.json",
            auto_detect_mcp=False,
        )
        server_name, server_config = next(iter(load_workspace_mcp_servers(workspace).items()))
        other_project = Path(__import__("os").environ["OTHER_PROJECT_DIR"])
        other_workspace = Workspace(
            name="other-app",
            cwd=other_project,
            skill_dirs=(),
            mcp_file=other_project / ".mcp.json",
            auto_detect_mcp=False,
        )
        other_server_name, other_server_config = next(
            iter(load_workspace_mcp_servers(other_workspace).items())
        )
        write_cache_entry(
            server_name,
            config_fingerprint({"lazy": True, **server_config}),
            tools=[
                {
                    "name": "find_organizations",
                    "description": "Find Sentry organizations.",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ],
        )
        write_cache_entry(
            other_server_name,
            config_fingerprint({"lazy": True, **other_server_config}),
            tools=[
                {
                    "name": "list_issues",
                    "description": "List Linear issues.",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ],
        )

        # Simulate plugin discovery before the multiplex gateway has bound a routed profile.
        patched_tools_config = SimpleNamespace(
            _get_platform_tools=lambda _config, _platform, **_kwargs: {"terminal", "skills"}
        )
        mcp_manager = install_mcp_patches(
            workspaces={
                workspace.name: workspace,
                other_workspace.name: other_workspace,
            },
            tools_config=patched_tools_config,
            register_mcp_servers=register_mcp_servers,
        )
        discover_mcp_tools()
        tool_name = next(iter(registry.get_tool_names_for_toolset(f"mcp-{server_name}")))
        other_tool_name = next(
            iter(registry.get_tool_names_for_toolset(f"mcp-{other_server_name}"))
        )
        global_registered = registry.snapshot_registration(tool_name, scope=None) is not None

        # A real gateway later resolves the same agent in its routed profile scope.
        set_multiplex_active(True)
        routed_scope = hermes_home_key()
        secret_token = set_secret_scope(
            {}, profile_home=__import__("os").environ["HERMES_HOME"]
        )
        token = _CURRENT_WORKSPACE.set(workspace)
        try:
            enabled = sorted(
                patched_tools_config._get_platform_tools(
                    {"platform_toolsets": {"slack": ["hermes-slack"]}},
                    "slack",
                )
            )
            definitions = get_tool_definitions(
                enabled_toolsets=enabled,
                quiet_mode=False,
                skip_tool_search_assembly=True,
            )
            search_result = json.loads(
                handle_function_call(
                    "tool_search",
                    {"queries": ["Sentry organizations"]},
                    enabled_toolsets=enabled,
                )
            )
            describe_result = json.loads(
                handle_function_call(
                    "tool_describe",
                    {"names": [tool_name]},
                    enabled_toolsets=enabled,
                )
            )
            active_entry = registry.get_entry(tool_name)
            check_fn_available = bool(
                active_entry is not None
                and (active_entry.check_fn is None or active_entry.check_fn())
            )
        finally:
            _CURRENT_WORKSPACE.reset(token)

        routed_registered = registry.snapshot_registration(tool_name, scope=routed_scope) is not None
        routed_names = [item["function"]["name"] for item in definitions]

        # Unrouted turns keep the base platform toolsets and never gain either workspace MCP set.
        unrouted_enabled = sorted(
            patched_tools_config._get_platform_tools(
                {"platform_toolsets": {"slack": ["hermes-slack"]}},
                "slack",
            )
        )
        unrouted_defs = get_tool_definitions(
            enabled_toolsets=unrouted_enabled,
            quiet_mode=False,
            skip_tool_search_assembly=True,
        )
        unrouted_names = [item["function"]["name"] for item in unrouted_defs]

        # A different routed workspace gets only its own MCP toolset in the same profile.
        other_token = _CURRENT_WORKSPACE.set(other_workspace)
        try:
            other_enabled = sorted(
                patched_tools_config._get_platform_tools(
                    {"platform_toolsets": {"slack": ["hermes-slack"]}},
                    "slack",
                )
            )
            other_defs = get_tool_definitions(
                enabled_toolsets=other_enabled,
                quiet_mode=False,
                skip_tool_search_assembly=True,
            )
        finally:
            _CURRENT_WORKSPACE.reset(other_token)
        other_names = [item["function"]["name"] for item in other_defs]
        other_registered = registry.snapshot_registration(other_tool_name, scope=routed_scope) is not None

        async def observe(workspace_for_task, expected_name, forbidden_name):
            task_token = _CURRENT_WORKSPACE.set(workspace_for_task)
            try:
                await asyncio.sleep(0)
                task_enabled = sorted(
                    patched_tools_config._get_platform_tools(
                        {"platform_toolsets": {"slack": ["hermes-slack"]}},
                        "slack",
                    )
                )
                await asyncio.sleep(0)
                task_defs = get_tool_definitions(
                    enabled_toolsets=task_enabled,
                    quiet_mode=False,
                    skip_tool_search_assembly=True,
                )
                task_names = [item["function"]["name"] for item in task_defs]
                return expected_name in task_names and forbidden_name not in task_names
            finally:
                _CURRENT_WORKSPACE.reset(task_token)

        async def observe_parallel():
            return await asyncio.gather(
                observe(workspace, tool_name, other_tool_name),
                observe(other_workspace, other_tool_name, tool_name),
            )

        parallel_results = asyncio.run(observe_parallel())

        # A changed workspace MCP connection becomes a new generation. The real core reconcile
        # removes the old name and discovers the new cached entry without a core edit.
        project.joinpath(".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "sentry": {
                            "type": "http",
                            "url": "https://example.invalid/mcp-v2",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        changed_generation = mcp_manager.refresh_workspace(workspace)
        changed_server_name = next(iter(changed_generation.servers))
        changed_server_config = changed_generation.servers[changed_server_name]
        write_cache_entry(
            changed_server_name,
            config_fingerprint({"lazy": True, **changed_server_config}),
            tools=[
                {
                    "name": "find_organizations",
                    "description": "Find Sentry organizations after reload.",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ],
        )
        reload_secret_token = set_secret_scope(
            {}, profile_home=__import__("os").environ["HERMES_HOME"]
        )
        try:
            reload_result = reconcile_mcp_servers_with_config()
            reload_token = _CURRENT_WORKSPACE.set(workspace)
            try:
                reloaded_enabled = sorted(
                    patched_tools_config._get_platform_tools(
                        {"platform_toolsets": {"slack": ["hermes-slack"]}},
                        "slack",
                    )
                )
                reloaded_defs = get_tool_definitions(
                    enabled_toolsets=reloaded_enabled,
                    quiet_mode=False,
                    skip_tool_search_assembly=True,
                )
                expected_changed_tool = (
                    f"mcp__{changed_server_name.replace('-', '_')}__find_organizations"
                )
                reloaded_visible = any(
                    item["function"]["name"] == expected_changed_tool
                    for item in reloaded_defs
                )
            finally:
                _CURRENT_WORKSPACE.reset(reload_token)
        finally:
            reset_secret_scope(reload_secret_token)

        # A separate profile scope sees neither the routed overlay nor workspace MCP toolsets.
        other_profile = __import__("os").environ["OTHER_PROFILE_HOME"]
        profile_home_token = set_hermes_home_override(other_profile)
        profile_secret_token = set_secret_scope({}, profile_home=other_profile)
        try:
            other_profile_scope = hermes_home_key()
            profile_enabled = sorted(
                patched_tools_config._get_platform_tools(
                    {"platform_toolsets": {"slack": ["hermes-slack"]}},
                    "slack",
                )
            )
            profile_defs = get_tool_definitions(
                enabled_toolsets=profile_enabled,
                quiet_mode=False,
                skip_tool_search_assembly=True,
            )
            profile_names = [item["function"]["name"] for item in profile_defs]
            profile_registered = registry.snapshot_registration(
                tool_name, scope=other_profile_scope
            ) is not None
        finally:
            reset_secret_scope(profile_secret_token)
            reset_hermes_home_override(profile_home_token)
            reset_secret_scope(secret_token)
        print(json.dumps({
            "server_name": server_name,
            "tool_name": tool_name,
            "global_registered": global_registered,
            "routed_scope": routed_scope,
            "routed_registered": routed_registered,
            "check_fn_available": check_fn_available,
            "search_result": search_result,
            "describe_result": describe_result,
            "routed_visible": tool_name in routed_names,
            "unrouted_visible": tool_name in unrouted_names or other_tool_name in unrouted_names,
            "other_visible": other_tool_name in other_names,
            "other_has_primary": tool_name in other_names,
            "other_registered": other_registered,
            "profile_registered": profile_registered,
            "profile_visible": tool_name in profile_names or other_tool_name in profile_names,
            "parallel_results": parallel_results,
            "enabled_workspace_toolset": f"mcp-{server_name}" in enabled,
            "reload_result": reload_result,
            "changed_server_name": changed_server_name,
            "reloaded_visible": reloaded_visible,
        }))
        assert global_registered
        assert routed_registered, {
            "global_registered": global_registered,
            "routed_scope": routed_scope,
            "routed_names": routed_names,
        }
        assert tool_name in routed_names, {
            "tool_name": tool_name,
            "global_registered": global_registered,
            "routed_registered": routed_registered,
            "routed_names": routed_names,
        }
        assert any(
            tool_name in group.get("matches", [])
            for group in search_result.get("results", [])
        ), search_result
        assert tool_name in describe_result.get("tools", {}), describe_result
        assert tool_name not in unrouted_names
        assert other_tool_name not in unrouted_names
        assert other_tool_name in other_names
        assert tool_name not in other_names
        assert other_registered
        assert not profile_registered
        assert tool_name not in profile_names
        assert other_tool_name not in profile_names
        assert parallel_results == [True, True]
        assert server_name in reload_result["removed"]
        assert changed_server_name in reload_result["added"]
        assert reloaded_visible
        """
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["PROJECT_DIR"] = str(project)
    env["OTHER_PROJECT_DIR"] = str(other_project)
    env["OTHER_PROFILE_HOME"] = str(other_profile_home)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repo_root), str(core_root), env.get("PYTHONPATH")) if part
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + "\n" + completed.stdout
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["routed_visible"] is True


def test_single_profile_turn_registers_workspace_mcp_before_tool_definitions(tmp_path: Path) -> None:
    """The first real gateway turn must see a newly refreshed workspace MCP generation."""
    plugin_dir = tmp_path / "plugin"
    project = tmp_path / "project"
    hermes_home = tmp_path / "hermes-home"
    plugin_dir.mkdir()
    project.mkdir()
    hermes_home.mkdir()
    (project / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sentry": {
                        "type": "http",
                        "url": "https://example.invalid/mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "config.yaml").write_text(
        f"""
workspaces:
  app:
    cwd: {project}
    skill_dirs: []
    mcp_file: {project / '.mcp.json'}
    auto_detect_mcp: false
routes:
  - platform: slack
    chat_id: C_TARGET
    workspace: app
""",
        encoding="utf-8",
    )

    repo_root = Path(__file__).resolve().parents[1]
    core_root = Path(sys.executable).resolve().parent.parent.parent
    script = textwrap.dedent(
        """
        import json
        import os
        from pathlib import Path
        from types import SimpleNamespace

        from gateway.run import GatewayRunner
        from model_tools import get_tool_definitions
        from tools.mcp_schema_cache import config_fingerprint, write_cache_entry
        from workspace_context.config import load_config
        from workspace_context.mcp import _config_fingerprint, load_workspace_mcp_servers
        from workspace_context.plugin import register_plugin

        class Context:
            def __init__(self):
                self.hooks = []
                self.cli = []

            def register_hook(self, name, callback):
                self.hooks.append((name, callback))

            def register_cli_command(self, name, **kwargs):
                self.cli.append(name)

        class Gateway:
            def _set_session_env(self, context):
                return context.session_id

            def _clear_session_env(self, token):
                return None

        plugin_dir = Path(os.environ["PLUGIN_DIR"])
        config = load_config(plugin_dir / "config.yaml")
        workspace = config.workspaces["app"]

        def cache_current_server(tool_name, *, actual_name=None):
            logical_name, server_config = next(iter(load_workspace_mcp_servers(workspace).items()))
            cache_name = actual_name or logical_name
            write_cache_entry(
                cache_name,
                config_fingerprint({"lazy": True, **server_config}),
                tools=[
                    {
                        "name": tool_name,
                        "description": "Find Sentry organizations.",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ],
            )
            return logical_name, server_config, cache_name

        server_name, _, _ = cache_current_server("find_organizations")

        ctx = Context()
        register_plugin(ctx, plugin_dir=plugin_dir, install_skill_patches=lambda: None)
        hook = next(callback for name, callback in ctx.hooks if name == "pre_gateway_dispatch")
        source = SimpleNamespace(platform="slack", chat_id="C_TARGET", thread_id=None)
        gateway = Gateway()
        hook(event=SimpleNamespace(source=source), gateway=gateway)

        def resolve_turn(session_id):
            context = SimpleNamespace(source=source, session_id=session_id)
            tokens = gateway._set_session_env(context)
            try:
                runner = object.__new__(GatewayRunner)
                runner._delivery_adapter_for = lambda _source: None
                enabled, disabled = GatewayRunner._resolve_turn_toolsets(
                    runner,
                    {"platform_toolsets": {"slack": ["terminal"]}},
                    source,
                    "slack",
                )
                definitions = get_tool_definitions(
                    enabled_toolsets=enabled,
                    disabled_toolsets=disabled,
                    quiet_mode=True,
                    skip_tool_search_assembly=True,
                )
            finally:
                gateway._clear_session_env(tokens)
            return enabled, [item["function"]["name"] for item in definitions]

        first_enabled, first_names = resolve_turn("single-profile-turn-1")

        workspace.mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "sentry": {
                            "type": "http",
                            "url": "https://example.invalid/mcp-v2",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        changed_name, changed_config = next(iter(load_workspace_mcp_servers(workspace).items()))
        changed_generation_name = (
            f"{changed_name}-g{_config_fingerprint(changed_config)[:12]}"
        )
        cache_current_server("find_organizations_v2", actual_name=changed_generation_name)
        second_enabled, second_names = resolve_turn("single-profile-turn-2")

        expected_tool = f"mcp__{server_name.replace('-', '_')}__find_organizations"
        expected_changed_tool = (
            f"mcp__{changed_generation_name.replace('-', '_')}__find_organizations_v2"
        )
        print(json.dumps({
            "first_enabled_workspace_toolset": f"mcp-{server_name}" in first_enabled,
            "first_tool": expected_tool,
            "first_visible": expected_tool in first_names,
            "changed_enabled_workspace_toolset": f"mcp-{changed_generation_name}" in second_enabled,
            "changed_tool": expected_changed_tool,
            "changed_visible": expected_changed_tool in second_names,
        }))
        assert f"mcp-{server_name}" in first_enabled
        assert expected_tool in first_names
        assert f"mcp-{changed_generation_name}" in second_enabled
        assert expected_changed_tool in second_names
        """
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["PLUGIN_DIR"] = str(plugin_dir)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repo_root), str(core_root), env.get("PYTHONPATH")) if part
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + "\n" + completed.stdout
