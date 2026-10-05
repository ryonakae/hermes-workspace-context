import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from workspace_context.plugin import register_plugin


class FakePluginContext:
    def __init__(self) -> None:
        self.hooks: list[tuple[str, object]] = []
        self.cli_commands: list[tuple[str, object, object]] = []

    def register_hook(self, name: str, callback) -> None:
        self.hooks.append((name, callback))

    def register_cli_command(self, name: str, help: str, setup_fn, handler_fn=None, description: str = "") -> None:
        self.cli_commands.append((name, setup_fn, handler_fn))


def write_config(plugin_dir: Path, project: Path) -> None:
    (plugin_dir / "config.yaml").write_text(
        f"""
workspaces:
  app:
    cwd: {project}
    skill_dirs: []
    mcp_file: null
routes:
  - platform: slack
    chat_id: C_TARGET
    workspace: app
""",
        encoding="utf-8",
    )


def test_register_plugin_installs_adapters_and_hook(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    project = tmp_path / "project"
    plugin_dir.mkdir()
    project.mkdir()
    write_config(plugin_dir, project)
    ctx = FakePluginContext()
    calls: list[str] = []

    register_plugin(
        ctx,
        plugin_dir=plugin_dir,
        install_skill_patches=lambda: calls.append("skills"),
        install_mcp_patches=lambda **kwargs: calls.append(
            f"mcp:{','.join(sorted(kwargs['workspaces']))}"
        ),
    )

    assert calls == ["skills", "mcp:app"]
    assert len(ctx.hooks) == 1
    assert ctx.hooks[0][0] == "pre_gateway_dispatch"
    assert callable(ctx.hooks[0][1])


def test_register_plugin_fails_closed_without_local_config(tmp_path: Path) -> None:
    ctx = FakePluginContext()

    with pytest.raises(RuntimeError, match="copy config.yaml.example to config.yaml"):
        register_plugin(ctx, plugin_dir=tmp_path)

    assert ctx.hooks == []


def test_register_plugin_registers_nothing_when_private_adapter_fails(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    project = tmp_path / "project"
    plugin_dir.mkdir()
    project.mkdir()
    write_config(plugin_dir, project)
    ctx = FakePluginContext()

    def broken_skill_adapter() -> None:
        raise RuntimeError("private API missing")

    with pytest.raises(RuntimeError, match="private API missing"):
        register_plugin(
            ctx,
            plugin_dir=plugin_dir,
            install_skill_patches=broken_skill_adapter,
            install_mcp_patches=lambda **_kwargs: None,
        )

    assert ctx.hooks == []


def test_registration_works_with_active_hermes_core_without_mcp_network(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    project = tmp_path / "project"
    hermes_home = tmp_path / "hermes-home"
    plugin_dir.mkdir()
    project.mkdir()
    (project / ".agents" / "skills" / "project-skill").mkdir(parents=True)
    (project / ".agents" / "skills" / "project-skill" / "SKILL.md").write_text(
        "---\nname: project-skill\ndescription: project-only\n---\n\nproject marker\n",
        encoding="utf-8",
    )
    (plugin_dir / "config.yaml").write_text(
        f"""
workspaces:
  app:
    cwd: {project}
    skill_dirs: [.agents/skills]
    mcp_file: null
    auto_detect_mcp: false
routes:
  - platform: slack
    chat_id: C_TARGET
    workspace: app
""",
        encoding="utf-8",
    )

    repo_root = Path(__file__).resolve().parents[1]
    python_path = Path(sys.executable).resolve()
    core_root = python_path.parent.parent.parent
    script = """
import json
from pathlib import Path

from agent import prompt_builder
from workspace_context.config import load_config
from workspace_context.plugin import register_plugin
from workspace_context.runtime import _CURRENT_WORKSPACE


class Context:
    def __init__(self):
        self.hooks = []
        self.cli = []

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_cli_command(self, name, **kwargs):
        self.cli.append(name)


ctx = Context()
plugin_dir = Path(__import__("os").environ["PLUGIN_DIR"])
register_plugin(ctx, plugin_dir=plugin_dir)
config = load_config(plugin_dir / "config.yaml")
token = _CURRENT_WORKSPACE.set(config.workspaces["app"])
try:
    project_prompt = prompt_builder.build_skills_system_prompt()
    snapshot_path = str(prompt_builder._skills_prompt_snapshot_path())
finally:
    _CURRENT_WORKSPACE.reset(token)
print(json.dumps({
    "hooks": [name for name, _ in ctx.hooks],
    "cli": ctx.cli,
    "project_skill_visible": "project-skill" in project_prompt,
    "snapshot_path": snapshot_path,
}))
"""
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

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["hooks"] == ["pre_gateway_dispatch"]
    assert result["cli"] == ["workspace-context"]
    assert result["project_skill_visible"] is True
    assert result["snapshot_path"] != str(hermes_home / ".skills_prompt_snapshot.json")
    assert result["snapshot_path"].startswith(str(hermes_home / "cache" / "scratch"))
