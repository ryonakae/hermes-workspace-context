from pathlib import Path

import pytest

from workspace_context.config import ConfigError, load_config


def test_load_config_resolves_workspace_paths_and_routes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".agents" / "skills").mkdir(parents=True)
    (project / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
workspaces:
  app:
    cwd: {project}
    skill_dirs:
      - .agents/skills
    mcp_file: .mcp.json
routes:
  - platform: slack
    chat_id: C123EXAMPLE
    workspace: app
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    workspace = config.workspaces["app"]
    assert workspace.cwd == project.resolve()
    assert workspace.skill_dirs == ((project / ".agents" / "skills").resolve(),)
    assert workspace.mcp_file == (project / ".mcp.json").resolve()
    assert config.routes[0].platform == "slack"
    assert config.routes[0].chat_id == "C123EXAMPLE"
    assert config.routes[0].workspace == "app"


def test_load_config_rejects_missing_workspace_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
workspaces:
  app:
    cwd: /definitely/missing/hermes-workspace-context-test
routes: []
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="workspace directory does not exist"):
        load_config(config_path)


def test_load_config_auto_detects_mcp_by_default(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
workspaces:
  app:
    cwd: {project}
routes: []
""",
        encoding="utf-8",
    )

    workspace = load_config(config_path).workspaces["app"]

    assert workspace.mcp_file is None
    assert workspace.auto_detect_mcp is True


def test_load_config_rejects_mcp_file_with_auto_detection(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
workspaces:
  app:
    cwd: {project}
    mcp_file: .mcp.json
    auto_detect_mcp: true
routes: []
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="cannot combine mcp_file with auto_detect_mcp"):
        load_config(config_path)


def test_load_config_rejects_route_to_unknown_workspace(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
workspaces: {}
routes:
  - platform: slack
    chat_id: C123EXAMPLE
    workspace: missing
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unknown workspace"):
        load_config(config_path)


@pytest.mark.parametrize(
    "route_ids",
    [
        "",
        "    chat_id: ''\n    thread_id: ''\n",
    ],
)
def test_load_config_rejects_platform_wide_route(tmp_path: Path, route_ids: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
workspaces:
  app:
    cwd: {project}
routes:
  - platform: slack
{route_ids}    workspace: app
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="chat_id or thread_id"):
        load_config(config_path)
