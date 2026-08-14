import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    return SimpleNamespace(
        source=source(chat_id),
        session_key=session_key,
        session_id=f"session-{session_key}",
    )


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


def test_routed_context_sets_tool_task_cwd_until_clear(tmp_path: Path) -> None:
    from tools import terminal_tool
    from tools.file_tools import _resolve_base_dir

    gateway = FakeGateway()
    install_gateway_patches(gateway, router_config(tmp_path))

    tokens = gateway._set_session_env(context("C_TARGET", "target"))
    try:
        assert terminal_tool.get_session_cwd("session-target") == str(tmp_path)
        assert _resolve_base_dir("session-target") == tmp_path
        assert terminal_tool._resolve_container_task_id("session-target") == "session-target"
        assert "env_type" in terminal_tool.resolve_task_overrides("session-target")
    finally:
        gateway._clear_session_env(tokens)

    assert terminal_tool.get_session_cwd("session-target") is None


def test_routed_context_restores_outer_tool_task_cwd(tmp_path: Path) -> None:
    from tools import terminal_tool

    outer = tmp_path / "outer"
    project = tmp_path / "project"
    outer.mkdir()
    project.mkdir()
    previous_overrides = {"cwd": str(outer), "docker_image": "example/image"}
    terminal_tool.register_task_env_overrides("session-target", previous_overrides)
    try:
        gateway = FakeGateway()
        install_gateway_patches(gateway, router_config(project))

        tokens = gateway._set_session_env(context("C_TARGET", "target"))
        assert terminal_tool.get_session_cwd("session-target") == str(project)
        assert terminal_tool.resolve_task_overrides("session-target") == {
            "cwd": str(project),
            "docker_image": "example/image",
        }

        gateway._clear_session_env(tokens)

        assert terminal_tool.resolve_task_overrides("session-target") == previous_overrides
        assert terminal_tool.get_session_cwd("session-target") == str(outer)
    finally:
        terminal_tool.clear_task_env_overrides("session-target")


def test_nested_routed_context_restores_outer_tool_binding(tmp_path: Path) -> None:
    from tools.terminal_tool import get_session_cwd

    gateway = FakeGateway()
    install_gateway_patches(gateway, router_config(tmp_path))

    outer_tokens = gateway._set_session_env(context("C_TARGET", "target"))
    inner_tokens = gateway._set_session_env(context("C_TARGET", "target"))

    gateway._clear_session_env(inner_tokens)
    assert get_session_cwd("session-target") == str(tmp_path)

    gateway._clear_session_env(outer_tokens)
    assert get_session_cwd("session-target") is None


def test_tool_cwd_binding_does_not_mutate_shared_environment(tmp_path: Path) -> None:
    from tools import terminal_tool

    outer = tmp_path / "outer"
    project = tmp_path / "project"
    outer.mkdir()
    project.mkdir()
    shared_env = SimpleNamespace(cwd=str(outer))
    with terminal_tool._env_lock:
        previous_env = terminal_tool._active_environments.get("default")
        terminal_tool._active_environments["default"] = shared_env

    gateway = FakeGateway()
    install_gateway_patches(gateway, router_config(project))
    tokens = None
    try:
        tokens = gateway._set_session_env(context("C_TARGET", "target"))
        assert shared_env.cwd == str(outer)
        gateway._clear_session_env(tokens)
        tokens = None
        assert shared_env.cwd == str(outer)
    finally:
        if tokens is not None:
            gateway._clear_session_env(tokens)
        terminal_tool.clear_task_env_overrides("session-target")
        with terminal_tool._env_lock:
            if previous_env is None:
                terminal_tool._active_environments.pop("default", None)
            else:
                terminal_tool._active_environments["default"] = previous_env


def test_tool_cwd_binding_restores_prior_isolated_environment(tmp_path: Path) -> None:
    from tools import terminal_tool
    from workspace_context.runtime import (
        _default_bind_tool_cwd,
        _default_reset_tool_cwd,
    )

    outer = tmp_path / "outer"
    project = tmp_path / "project"
    outer.mkdir()
    project.mkdir()
    task_id = "session-isolated"
    previous_overrides = {"env_type": "local", "cwd": str(outer)}
    environment = SimpleNamespace(cwd=str(outer))
    previous_raw_overrides = terminal_tool._task_env_overrides.get(task_id)
    terminal_tool._task_env_overrides[task_id] = dict(previous_overrides)
    terminal_tool.record_session_cwd(task_id, str(outer))
    with terminal_tool._env_lock:
        previous_env = terminal_tool._active_environments.get(task_id)
        terminal_tool._active_environments[task_id] = environment

    token = _default_bind_tool_cwd(task_id, str(project))
    try:
        assert environment.cwd == str(project)
    finally:
        _default_reset_tool_cwd(token)

    try:
        assert environment.cwd == str(outer)
        assert terminal_tool.resolve_task_overrides(task_id) == previous_overrides
        assert terminal_tool.get_session_cwd(task_id) == str(outer)
    finally:
        terminal_tool.clear_task_env_overrides(task_id)
        if previous_raw_overrides is not None:
            terminal_tool._task_env_overrides[task_id] = previous_raw_overrides
        with terminal_tool._env_lock:
            if previous_env is None:
                terminal_tool._active_environments.pop(task_id, None)
            else:
                terminal_tool._active_environments[task_id] = previous_env


def test_preexisting_raw_environment_is_restored_but_not_owned(tmp_path: Path) -> None:
    from tools import file_tools, terminal_tool
    from workspace_context.runtime import (
        _cleanup_plugin_isolation_for_unrouted_session,
        _default_bind_tool_cwd,
        _default_reset_tool_cwd,
    )

    outer = tmp_path / "outer-preexisting"
    project = tmp_path / "project-preexisting"
    outer.mkdir()
    project.mkdir()
    task_id = "session-preexisting-unowned"
    environment = SimpleNamespace(cwd=str(outer))
    file_ops = object()
    previous_overrides = terminal_tool._task_env_overrides.pop(task_id, None)
    terminal_tool.record_session_cwd(task_id, str(outer))
    with terminal_tool._env_lock:
        previous_env = terminal_tool._active_environments.get(task_id)
        terminal_tool._active_environments[task_id] = environment
    with file_tools._file_ops_lock:
        previous_file_ops = file_tools._file_ops_cache.get(task_id)
        file_tools._file_ops_cache[task_id] = file_ops

    try:
        token = _default_bind_tool_cwd(task_id, str(project))
        assert environment.cwd == str(project)
        _default_reset_tool_cwd(token)

        assert environment.cwd == str(outer)
        assert terminal_tool.get_session_cwd(task_id) == str(outer)
        _cleanup_plugin_isolation_for_unrouted_session(task_id)
        assert terminal_tool._active_environments.get(task_id) is environment
        assert file_tools._file_ops_cache.get(task_id) is file_ops
    finally:
        terminal_tool.clear_task_env_overrides(task_id)
        if previous_overrides is not None:
            terminal_tool._task_env_overrides[task_id] = previous_overrides
        with terminal_tool._env_lock:
            if previous_env is None:
                terminal_tool._active_environments.pop(task_id, None)
            else:
                terminal_tool._active_environments[task_id] = previous_env
        with file_tools._file_ops_lock:
            if previous_file_ops is None:
                file_tools._file_ops_cache.pop(task_id, None)
            else:
                file_tools._file_ops_cache[task_id] = previous_file_ops


@pytest.mark.parametrize("initial_cwd", [None, "missing"])
def test_preexisting_raw_environment_restores_empty_cwd_shape(
    tmp_path: Path,
    initial_cwd: str | None,
) -> None:
    from tools import terminal_tool
    from workspace_context.runtime import (
        _default_bind_tool_cwd,
        _default_reset_tool_cwd,
    )

    task_id = f"session-preexisting-{initial_cwd}"
    environment = (
        SimpleNamespace()
        if initial_cwd == "missing"
        else SimpleNamespace(cwd=initial_cwd)
    )
    previous_overrides = terminal_tool._task_env_overrides.pop(task_id, None)
    terminal_tool.clear_session_cwd(task_id)
    with terminal_tool._env_lock:
        previous_env = terminal_tool._active_environments.get(task_id)
        terminal_tool._active_environments[task_id] = environment

    try:
        token = _default_bind_tool_cwd(task_id, str(tmp_path))
        assert environment.cwd == str(tmp_path)
        _default_reset_tool_cwd(token)

        if initial_cwd == "missing":
            assert not hasattr(environment, "cwd")
        else:
            assert environment.cwd is None
        assert terminal_tool.get_session_cwd(task_id) is None
    finally:
        terminal_tool.clear_task_env_overrides(task_id)
        if previous_overrides is not None:
            terminal_tool._task_env_overrides[task_id] = previous_overrides
        with terminal_tool._env_lock:
            if previous_env is None:
                terminal_tool._active_environments.pop(task_id, None)
            else:
                terminal_tool._active_environments[task_id] = previous_env


def test_unrouted_reuse_cleans_plugin_isolated_environment_and_cache(
    tmp_path: Path,
) -> None:
    from tools import file_tools, terminal_tool

    gateway = FakeGateway()
    install_gateway_patches(gateway, router_config(tmp_path))
    routed_tokens = gateway._set_session_env(context("C_TARGET", "target"))
    gateway._clear_session_env(routed_tokens)

    task_id = "session-target"
    cleaned: list[bool] = []
    environment = SimpleNamespace(
        cwd=str(tmp_path),
        cleanup=lambda: cleaned.append(True),
    )
    with terminal_tool._env_lock:
        previous_env = terminal_tool._active_environments.get(task_id)
        previous_activity = terminal_tool._last_activity.get(task_id)
        terminal_tool._active_environments[task_id] = environment
        terminal_tool._last_activity[task_id] = 0.0
    with file_tools._file_ops_lock:
        previous_file_ops = file_tools._file_ops_cache.get(task_id)
        file_tools._file_ops_cache[task_id] = object()

    try:
        unrouted_tokens = gateway._set_session_env(context("C_OTHER", "target"))
        gateway._clear_session_env(unrouted_tokens)

        assert task_id not in terminal_tool._active_environments
        assert task_id not in file_tools._file_ops_cache
        assert cleaned == [True]
    finally:
        with terminal_tool._env_lock:
            if previous_env is not None:
                terminal_tool._active_environments[task_id] = previous_env
            if previous_activity is not None:
                terminal_tool._last_activity[task_id] = previous_activity
            else:
                terminal_tool._last_activity.pop(task_id, None)
        with file_tools._file_ops_lock:
            if previous_file_ops is not None:
                file_tools._file_ops_cache[task_id] = previous_file_ops
            else:
                file_tools._file_ops_cache.pop(task_id, None)


def test_routed_bind_waits_for_same_session_unrouted_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import concurrent.futures
    import threading

    from tools import terminal_tool
    from workspace_context.runtime import (
        _cleanup_plugin_isolation_for_unrouted_session,
        _default_bind_tool_cwd,
        _default_reset_tool_cwd,
    )

    task_id = "session-cleanup-race"
    initial_token = _default_bind_tool_cwd(task_id, str(tmp_path))
    _default_reset_tool_cwd(initial_token)

    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()

    def blocking_cleanup(cleanup_task_id: str) -> None:
        assert cleanup_task_id == task_id
        cleanup_started.set()
        assert allow_cleanup.wait(timeout=2)

    monkeypatch.setattr(terminal_tool, "cleanup_vm", blocking_cleanup)
    routed_token = None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            cleanup_future = executor.submit(
                _cleanup_plugin_isolation_for_unrouted_session,
                task_id,
            )
            assert cleanup_started.wait(timeout=1)
            bind_future = executor.submit(
                _default_bind_tool_cwd,
                task_id,
                str(tmp_path),
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                bind_future.result(timeout=0.05)

            allow_cleanup.set()
            cleanup_future.result(timeout=1)
            routed_token = bind_future.result(timeout=1)
    finally:
        allow_cleanup.set()
        if routed_token is not None:
            _default_reset_tool_cwd(routed_token)
        _cleanup_plugin_isolation_for_unrouted_session(task_id)
        terminal_tool.clear_task_env_overrides(task_id)


def test_out_of_order_same_session_cleanup_keeps_active_binding(
    tmp_path: Path,
) -> None:
    from tools import terminal_tool
    from workspace_context.runtime import (
        _default_bind_tool_cwd,
        _default_reset_tool_cwd,
    )

    outer_token = _default_bind_tool_cwd("session-target", str(tmp_path))
    inner_token = _default_bind_tool_cwd("session-target", str(tmp_path))
    try:
        _default_reset_tool_cwd(outer_token)
        assert terminal_tool.get_session_cwd("session-target") == str(tmp_path)

        _default_reset_tool_cwd(inner_token)
        inner_token = None
        assert terminal_tool.get_session_cwd("session-target") is None
        assert terminal_tool._task_env_overrides.get("session-target") is None
    finally:
        if inner_token is not None:
            _default_reset_tool_cwd(inner_token)
        terminal_tool.clear_task_env_overrides("session-target")


def test_tool_cwd_binding_rolls_back_partial_registry_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import terminal_tool
    from workspace_context.runtime import _default_bind_tool_cwd

    original_record = terminal_tool.record_session_cwd

    def record_then_fail(task_id: str, cwd: str) -> None:
        original_record(task_id, cwd)
        raise RuntimeError("record failed after mutation")

    monkeypatch.setattr(terminal_tool, "record_session_cwd", record_then_fail)
    try:
        with pytest.raises(RuntimeError, match="record failed after mutation"):
            _default_bind_tool_cwd("session-target", str(tmp_path))

        assert terminal_tool._task_env_overrides.get("session-target") is None
        assert terminal_tool.get_session_cwd("session-target") is None
    finally:
        terminal_tool.clear_task_env_overrides("session-target")


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


def test_routed_context_rolls_back_when_tool_cwd_binding_fails(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    reset_calls: list[str] = []

    def fail_bind(_task_id: str, _cwd: str):
        raise RuntimeError("tool cwd private API changed")

    install_gateway_patches(
        gateway,
        router_config(tmp_path),
        set_session_cwd=lambda _cwd: "cwd-token",
        reset_session_cwd=reset_calls.append,
        bind_tool_cwd=fail_bind,
        reset_tool_cwd=lambda _token: None,
    )

    try:
        gateway._set_session_env(context("C_TARGET", "target"))
    except RuntimeError as exc:
        assert str(exc) == "tool cwd private API changed"
    else:
        raise AssertionError("tool cwd binding failure should propagate")

    assert gateway.cleared_tokens == [["target"]]
    assert reset_calls == ["cwd-token"]
    assert current_workspace() is None
