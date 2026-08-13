from pathlib import Path
import pytest

from workspace_context.plugin import register_plugin


class FakePluginContext:
    def __init__(self) -> None:
        self.hooks: list[tuple[str, object]] = []

    def register_hook(self, name: str, callback) -> None:
        self.hooks.append((name, callback))


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
