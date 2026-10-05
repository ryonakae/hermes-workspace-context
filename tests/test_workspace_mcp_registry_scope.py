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
        from tools.mcp_tool_discovery import register_mcp_servers
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
            config_fingerprint(server_config),
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
            config_fingerprint(other_server_config),
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
        install_mcp_patches(
            workspaces={
                workspace.name: workspace,
                other_workspace.name: other_workspace,
            },
            tools_config=patched_tools_config,
            register_mcp_servers=register_mcp_servers,
        )
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
