import json
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

from workspace_context.config import Workspace
from workspace_context.mcp import (
    McpConfigError,
    WorkspaceMcpWarning,
    install_mcp_patches,
    load_workspace_mcp_servers,
    namespace_server_name,
)
from workspace_context.runtime import _CURRENT_WORKSPACE


def workspace(project: Path, name: str = "My App") -> Workspace:
    return Workspace(
        name=name,
        cwd=project,
        skill_dirs=(),
        mcp_file=project / ".mcp.json",
    )


def test_load_workspace_mcp_servers_converts_and_namespaces_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_TOKEN", "secret-token")
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local docs": {
                        "type": "stdio",
                        "command": "npx",
                        "args": ["-y", "docs-mcp"],
                        "env": {"PROJECT_ROOT": "${HOME}/dev/example"},
                    },
                    "remote": {
                        "type": "http",
                        "url": "https://example.invalid/mcp?project=${PROJECT_ID}",
                        "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROJECT_ID", "abc")

    servers = load_workspace_mcp_servers(workspace(tmp_path))

    assert set(servers) == {"workspace-my-app-local-docs", "workspace-my-app-remote"}
    assert servers["workspace-my-app-local-docs"] == {
        "command": "npx",
        "args": ["-y", "docs-mcp"],
        "env": {"PROJECT_ROOT": f"{Path.home()}/dev/example"},
    }
    assert servers["workspace-my-app-remote"] == {
        "url": "https://example.invalid/mcp?project=abc",
        "headers": {"Authorization": "Bearer secret-token"},
    }


def test_load_workspace_mcp_servers_rejects_missing_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISSING_MCP_TOKEN", raising=False)
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://example.invalid/mcp",
                        "headers": {"Authorization": "Bearer ${MISSING_MCP_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(McpConfigError, match="MISSING_MCP_TOKEN"):
        load_workspace_mcp_servers(workspace(tmp_path))


def test_namespace_server_name_is_stable_and_safe() -> None:
    assert namespace_server_name("My App", "Sentry MCP") == "workspace-my-app-sentry-mcp"


def test_mcp_toolsets_are_added_only_inside_matching_workspace(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {"type": "stdio", "command": "docs", "args": []},
                    "remote": {"type": "http", "url": "https://example.invalid/mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    tools_config = SimpleNamespace(
        _get_platform_tools=lambda _config, _platform, **_kwargs: {"terminal", "skills"}
    )
    registered: list[dict] = []
    install_mcp_patches(
        workspaces={"My App": workspace(tmp_path)},
        tools_config=tools_config,
        register_mcp_servers=lambda servers: registered.append(servers) or [],
    )

    token = _CURRENT_WORKSPACE.set(workspace(tmp_path))
    try:
        routed = tools_config._get_platform_tools({}, "slack")
    finally:
        _CURRENT_WORKSPACE.reset(token)
    unrouted = tools_config._get_platform_tools({}, "slack")

    assert routed == {
        "terminal",
        "skills",
        "mcp-workspace-my-app-docs",
        "mcp-workspace-my-app-remote",
    }
    assert unrouted == {"terminal", "skills"}
    assert len(registered) == 1
    assert set(registered[0]) == {"workspace-my-app-docs", "workspace-my-app-remote"}


def test_mcp_patch_is_idempotent(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    tools_config = SimpleNamespace(_get_platform_tools=lambda *_args, **_kwargs: {"skills"})
    register_calls: list[dict] = []

    install_mcp_patches(
        workspaces={"My App": workspace(tmp_path)},
        tools_config=tools_config,
        register_mcp_servers=lambda servers: register_calls.append(servers) or [],
    )
    first = tools_config._get_platform_tools
    install_mcp_patches(
        workspaces={"My App": workspace(tmp_path)},
        tools_config=tools_config,
        register_mcp_servers=lambda servers: register_calls.append(servers) or [],
    )

    assert tools_config._get_platform_tools == first
    assert register_calls == [{}]


def test_auto_detects_and_merges_claude_and_codex_configs(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {"type": "http", "url": "https://example.invalid/mcp", "auth": "oauth"},
                    "claude-only": {"command": "claude-only", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        """
[mcp_servers.docs]
url = "https://example.invalid/mcp"
startup_timeout_sec = 12

[mcp_servers.codex-only]
command = "codex-only"
args = ["--stdio"]
""",
        encoding="utf-8",
    )

    servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert servers["workspace-app-docs"] == {
        "url": "https://example.invalid/mcp",
        "auth": "oauth",
        "connect_timeout": 12,
    }
    assert servers["workspace-app-claude-only"] == {"command": "claude-only", "args": []}
    assert servers["workspace-app-codex-only"] == {"command": "codex-only", "args": ["--stdio"]}


def test_auto_detects_opencode_jsonc_and_hermes_config(tmp_path: Path) -> None:
    (tmp_path / "opencode.jsonc").write_text(
        """
{
  // OpenCode project MCPs
  "mcp": {
    "remote": {
      "type": "remote",
      "url": "https://example.invalid/mcp",
      "oauth": false,
      "enabled": true
    },
    "local": {
      "type": "local",
      "command": ["npx", "-y", "local-mcp"],
      "environment": {"URL": "https://example.invalid//path"}
    }
  }
}
""",
        encoding="utf-8",
    )
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "config.yaml").write_text(
        """
mcp_servers:
  hermes-only:
    command: hermes-only
    args: []
""",
        encoding="utf-8",
    )

    servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert servers["workspace-app-remote"] == {
        "url": "https://example.invalid/mcp",
        "auth": "none",
    }
    assert servers["workspace-app-local"] == {
        "command": "npx",
        "args": ["-y", "local-mcp"],
        "env": {"URL": "https://example.invalid//path"},
    }
    assert servers["workspace-app-hermes-only"] == {"command": "hermes-only", "args": []}


def test_conflicting_server_is_skipped_without_blocking_other_servers(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "conflict": {"type": "http", "url": "https://one.invalid/mcp"},
                    "safe": {"command": "safe", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        """
[mcp_servers.conflict]
url = "https://two.invalid/mcp?token=must-not-leak"

[mcp_servers.codex-safe]
command = "codex-safe"
""",
        encoding="utf-8",
    )

    with pytest.warns(WorkspaceMcpWarning) as caught:
        servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert set(servers) == {"workspace-app-safe", "workspace-app-codex-safe"}
    warning = str(caught[0].message)
    assert "conflict" in warning
    assert ".mcp.json" in warning
    assert ".codex/config.toml" in warning
    assert "must-not-leak" not in warning


def test_codex_auth_and_tool_policy_are_converted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_BEARER", "secret-token")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        """
[mcp_servers.remote]
url = "https://example.invalid/mcp"
bearer_token_env_var = "MCP_BEARER"
startup_timeout_sec = 4
tool_timeout_sec = 9
enabled_tools = ["read"]
disabled_tools = ["write"]
""",
        encoding="utf-8",
    )

    servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert servers["workspace-app-remote"] == {
        "url": "https://example.invalid/mcp",
        "headers": {"Authorization": "Bearer secret-token"},
        "connect_timeout": 4,
        "timeout": 9,
        "tools": {"include": ["read"], "exclude": ["write"]},
    }


def test_opencode_env_reference_is_converted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TOKEN", "secret-token")
    (tmp_path / "opencode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "remote": {
                        "type": "remote",
                        "url": "https://example.invalid/mcp",
                        "headers": {"Authorization": "Bearer {env:MCP_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert servers["workspace-app-remote"]["headers"] == {"Authorization": "Bearer secret-token"}


@pytest.mark.parametrize("source", ["claude", "codex", "opencode"])
def test_remote_oauth_default_is_preserved(source: str, tmp_path: Path) -> None:
    if source == "claude":
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"remote": {"type": "http", "url": "https://example.invalid/mcp"}}}),
            encoding="utf-8",
        )
    elif source == "codex":
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "config.toml").write_text(
            '[mcp_servers.remote]\nurl = "https://example.invalid/mcp"\n',
            encoding="utf-8",
        )
    else:
        (tmp_path / "opencode.json").write_text(
            json.dumps({"mcp": {"remote": {"type": "remote", "url": "https://example.invalid/mcp"}}}),
            encoding="utf-8",
        )

    servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert servers["workspace-app-remote"]["auth"] == "oauth"


def test_opencode_timeout_milliseconds_are_converted_to_seconds(tmp_path: Path) -> None:
    (tmp_path / "opencode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "remote": {
                        "type": "remote",
                        "url": "https://example.invalid/mcp",
                        "timeout": 5500,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert servers["workspace-app-remote"]["timeout"] == 5.5


def test_unsupported_codex_auth_skips_only_that_server(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        """
[mcp_servers.unsupported]
url = "https://example.invalid/mcp"
auth = "chatgpt"

[mcp_servers.safe]
command = "safe"
""",
        encoding="utf-8",
    )

    with pytest.warns(WorkspaceMcpWarning) as caught:
        servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert set(servers) == {"workspace-app-safe"}
    warning = str(caught[0].message)
    assert "unsupported" in warning
    assert "auth" in warning
    assert ".codex/config.toml" in warning
    assert "chatgpt" not in warning


def test_names_that_slug_to_same_value_skip_only_colliding_servers(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs api": {"command": "first"},
                    "docs-api": {"command": "second"},
                    "safe": {"command": "safe"},
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.warns(WorkspaceMcpWarning) as caught:
        servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert set(servers) == {"workspace-app-safe"}
    warning = str(caught[0].message)
    assert "namespace collision" in warning
    assert "docs api" in warning
    assert "docs-api" in warning
    assert ".mcp.json" in warning
    assert "first" not in warning
    assert "second" not in warning


def test_explicit_mcp_file_outside_workspace_uses_basename_in_collision_warning(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external.json"
    external.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs api": {"command": "first"},
                    "docs-api": {"command": "second"},
                }
            }
        ),
        encoding="utf-8",
    )
    routed_workspace = Workspace(
        name="app",
        cwd=project,
        skill_dirs=(),
        mcp_file=external,
        auto_detect_mcp=False,
    )

    with pytest.warns(WorkspaceMcpWarning) as caught:
        servers = load_workspace_mcp_servers(routed_workspace)

    assert servers == {}
    warning = str(caught[0].message)
    assert "external.json" in warning
    assert str(tmp_path) not in warning


def test_unterminated_jsonc_block_comment_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "opencode.jsonc").write_text(
        '{"mcp": {}} /* unterminated',
        encoding="utf-8",
    )

    with pytest.raises(McpConfigError, match="cannot read workspace MCP file"):
        load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)


def test_parse_error_does_not_echo_secret_source_text(tmp_path: Path) -> None:
    secret = "do-not-echo-this-secret"
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "config.yaml").write_text(
        f"mcp_servers:\n  private: [\"{secret}\"\n",
        encoding="utf-8",
    )

    with pytest.raises(McpConfigError) as caught:
        load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert secret not in str(caught.value)
    assert ".hermes/config.yaml" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    rendered_traceback = "".join(
        traceback.format_exception(caught.type, caught.value, caught.tb)
    )
    assert secret not in rendered_traceback


def test_compatible_nested_mappings_are_merged(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {
                        "type": "http",
                        "url": "https://example.invalid/mcp",
                        "headers": {"X-Region": "local"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        """
[mcp_servers.docs]
url = "https://example.invalid/mcp"
http_headers = { X-Client = "codex" }
""",
        encoding="utf-8",
    )

    servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert servers["workspace-app-docs"]["headers"] == {
        "X-Region": "local",
        "X-Client": "codex",
    }


def test_incompatible_nested_value_skips_only_that_server(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {
                        "type": "http",
                        "url": "https://example.invalid/mcp",
                        "headers": {"X-Region": "one"},
                    },
                    "safe": {"command": "safe"},
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        """
[mcp_servers.docs]
url = "https://example.invalid/mcp"
http_headers = { X-Region = "two" }
""",
        encoding="utf-8",
    )

    with pytest.warns(WorkspaceMcpWarning) as caught:
        servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert set(servers) == {"workspace-app-safe"}
    assert "headers.X-Region" in str(caught[0].message)
    assert "one" not in str(caught[0].message)
    assert "two" not in str(caught[0].message)


def test_unsupported_source_setting_skips_only_that_server(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        """
[mcp_servers.approval]
url = "https://example.invalid/mcp"
default_tools_approval_mode = "prompt"

[mcp_servers.safe]
command = "safe"
""",
        encoding="utf-8",
    )

    with pytest.warns(WorkspaceMcpWarning) as caught:
        servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert set(servers) == {"workspace-app-safe"}
    warning = str(caught[0].message)
    assert "approval" in warning
    assert "default_tools_approval_mode" in warning
    assert ".codex/config.toml" in warning
    assert "prompt" not in warning


def test_claude_headers_helper_is_not_silently_ignored(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "dynamic": {
                        "type": "http",
                        "url": "https://example.invalid/mcp",
                        "headersHelper": "secret-producing-command",
                    },
                    "safe": {"command": "safe"},
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.warns(WorkspaceMcpWarning) as caught:
        servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert set(servers) == {"workspace-app-safe"}
    warning = str(caught[0].message)
    assert "headersHelper" in warning
    assert ".mcp.json" in warning
    assert "secret-producing-command" not in warning


@pytest.mark.parametrize(
    ("server", "error"),
    [
        ({"command": "server", "env": ["not", "an", "object"]}, "env must be an object"),
        (
            {"type": "http", "url": "https://example.invalid/mcp", "headers": {"X-Test": 123}},
            "header names and values must be strings",
        ),
    ],
)
def test_malformed_env_and_headers_are_rejected_before_registration(
    tmp_path: Path, server: dict[str, object], error: str
) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"invalid": server}}),
        encoding="utf-8",
    )

    with pytest.raises(McpConfigError, match=error):
        load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)


def test_opencode_oauth_true_is_normalized_for_hermes(tmp_path: Path) -> None:
    (tmp_path / "opencode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "docs": {
                        "type": "remote",
                        "url": "https://example.invalid/mcp",
                        "oauth": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert servers["workspace-app-docs"] == {
        "url": "https://example.invalid/mcp",
        "auth": "oauth",
    }


def test_opencode_json_and_jsonc_are_both_merged_when_compatible(tmp_path: Path) -> None:
    (tmp_path / "opencode.json").write_text(
        json.dumps({"mcp": {"docs": {"type": "remote", "url": "https://example.invalid/mcp"}}}),
        encoding="utf-8",
    )
    (tmp_path / "opencode.jsonc").write_text(
        '{"mcp": {"docs": {"type": "remote", "url": "https://example.invalid/mcp", "timeout": 3000}}}',
        encoding="utf-8",
    )

    servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert servers["workspace-app-docs"] == {
        "url": "https://example.invalid/mcp",
        "auth": "oauth",
        "timeout": 3,
    }


def test_claude_timeout_milliseconds_are_converted_to_seconds(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {
                        "type": "http",
                        "url": "https://example.invalid/mcp",
                        "timeout": 6000,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert servers["workspace-app-docs"]["timeout"] == 6


def test_authorization_header_takes_precedence_over_merged_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"docs": {"type": "http", "url": "https://example.invalid/mcp"}}}),
        encoding="utf-8",
    )
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        """
[mcp_servers.docs]
url = "https://example.invalid/mcp"
bearer_token_env_var = "DOCS_TOKEN"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCS_TOKEN", "secret-token")

    servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert servers["workspace-app-docs"] == {
        "url": "https://example.invalid/mcp",
        "headers": {"Authorization": "Bearer secret-token"},
    }


def test_jsonc_trailing_comma_removal_does_not_modify_strings(tmp_path: Path) -> None:
    (tmp_path / "opencode.jsonc").write_text(
        """
{
  "mcp": {
    "local": {
      "type": "local",
      "command": ["server", "value,}"],
    },
  },
}
""",
        encoding="utf-8",
    )

    servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert servers["workspace-app-local"]["args"] == ["value,}"]


def test_jsonc_allows_comment_after_trailing_comma(tmp_path: Path) -> None:
    (tmp_path / "opencode.jsonc").write_text(
        """
{
  "mcp": {
    "local": {
      "type": "local",
      "command": ["server"], // keep this explanation
    },
  },
}
""",
        encoding="utf-8",
    )

    servers = load_workspace_mcp_servers(workspace(tmp_path, "app"), auto_detect=True)

    assert servers["workspace-app-local"]["command"] == "server"
