import asyncio
from pathlib import Path
from types import SimpleNamespace

from workspace_context.config import Route, RouterConfig, Workspace
from workspace_context.runtime import (
    current_workspace,
    install_gateway_patches,
    make_pre_gateway_dispatch,
)


class FakeGateway:
    def __init__(self) -> None:
        self.cleared_tokens: list[object] = []

    def _set_session_env(self, context):
        return [context.session_key]

    def _clear_session_env(self, tokens):
        self.cleared_tokens.append(tokens)


def source(chat_id: str, thread_id: str | None = None):
    return SimpleNamespace(
        platform=SimpleNamespace(value="slack"),
        chat_id=chat_id,
        thread_id=thread_id,
    )


def context(chat_id: str, session_key: str):
    return SimpleNamespace(source=source(chat_id), session_key=session_key)


def router_config(project: Path) -> RouterConfig:
    workspace = Workspace(
        name="app",
        cwd=project,
        skill_dirs=(project / ".agents" / "skills",),
        mcp_file=project / ".mcp.json",
    )
    return RouterConfig(
        workspaces={"app": workspace},
        routes=(Route(platform="slack", chat_id="C_TARGET", workspace="app"),),
    )


def test_pre_gateway_dispatch_installs_idempotent_patches(tmp_path: Path) -> None:
    gateway = FakeGateway()
    hook = make_pre_gateway_dispatch(router_config(tmp_path))

    hook(event=SimpleNamespace(source=source("C_TARGET")), gateway=gateway)
    first_set = gateway._set_session_env
    hook(event=SimpleNamespace(source=source("C_TARGET")), gateway=gateway)

    assert gateway._set_session_env == first_set


def test_routed_context_sets_workspace_and_cwd_until_clear(tmp_path: Path) -> None:
    gateway = FakeGateway()
    cwd_values: list[str] = []
    reset_calls: list[str] = []

    def bind_cwd(cwd: str) -> str:
        cwd_values.append(cwd)
        return "cwd-token"

    install_gateway_patches(
        gateway,
        router_config(tmp_path),
        set_session_cwd=bind_cwd,
        reset_session_cwd=reset_calls.append,
    )

    tokens = gateway._set_session_env(context("C_TARGET", "target"))

    assert current_workspace() is not None
    assert current_workspace().name == "app"
    assert cwd_values == [str(tmp_path)]

    gateway._clear_session_env(tokens)

    assert current_workspace() is None
    assert reset_calls == ["cwd-token"]


def test_unrouted_context_keeps_original_cwd_behavior(tmp_path: Path) -> None:
    gateway = FakeGateway()
    cwd_values: list[str] = []
    install_gateway_patches(
        gateway,
        router_config(tmp_path),
        set_session_cwd=cwd_values.append,
        reset_session_cwd=lambda _token: None,
    )

    tokens = gateway._set_session_env(context("C_OTHER", "other"))

    assert current_workspace() is None
    assert cwd_values == []
    gateway._clear_session_env(tokens)


def test_workspace_context_is_isolated_between_concurrent_tasks(tmp_path: Path) -> None:
    gateway = FakeGateway()
    install_gateway_patches(
        gateway,
        router_config(tmp_path),
        set_session_cwd=lambda cwd: cwd,
        reset_session_cwd=lambda _token: None,
    )

    async def observe(chat_id: str) -> str | None:
        tokens = gateway._set_session_env(context(chat_id, chat_id))
        await asyncio.sleep(0)
        workspace = current_workspace()
        gateway._clear_session_env(tokens)
        return workspace.name if workspace else None

    async def run():
        return await asyncio.gather(observe("C_TARGET"), observe("C_OTHER"))

    assert asyncio.run(run()) == ["app", None]


def test_routed_context_restores_outer_hermes_cwd(tmp_path: Path) -> None:
    from agent import runtime_cwd

    outer = tmp_path / "outer"
    project = tmp_path / "project"
    outer.mkdir()
    project.mkdir()
    outer_token = runtime_cwd.set_session_cwd(str(outer))
    try:
        gateway = FakeGateway()
        install_gateway_patches(gateway, router_config(project))

        tokens = gateway._set_session_env(context("C_TARGET", "target"))
        assert runtime_cwd.resolve_agent_cwd() == project

        gateway._clear_session_env(tokens)
        assert runtime_cwd.resolve_agent_cwd() == outer
    finally:
        outer_token.var.reset(outer_token)


def test_routed_context_rolls_back_hermes_session_when_cwd_binding_fails(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()

    def fail_bind(_cwd: str):
        raise RuntimeError("cwd private API changed")

    install_gateway_patches(
        gateway,
        router_config(tmp_path),
        set_session_cwd=fail_bind,
        reset_session_cwd=lambda _token: None,
    )

    try:
        gateway._set_session_env(context("C_TARGET", "target"))
    except RuntimeError as exc:
        assert str(exc) == "cwd private API changed"
    else:
        raise AssertionError("cwd binding failure should propagate")

    assert gateway.cleared_tokens == [["target"]]
    assert current_workspace() is None
