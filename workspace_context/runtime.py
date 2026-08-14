"""Task-local workspace routing adapters for Hermes gateway internals."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
import threading
from typing import Any, Callable

from .config import Route, RouterConfig, Workspace


_CURRENT_WORKSPACE: ContextVar[Workspace | None] = ContextVar(
    "hermes_workspace_context_current_workspace",
    default=None,
)
_PATCH_MARKER = "_hermes_workspace_context_patch"


class CompatibilityError(RuntimeError):
    """Raised when the active Hermes gateway lacks a required private API."""


@dataclass(frozen=True)
class RoutedSessionTokens:
    hermes_tokens: Any
    workspace_token: Token[Workspace | None]
    cwd_token: Any | None
    tool_cwd_token: Any | None
    routed: bool


@dataclass(frozen=True)
class ToolCwdToken:
    task_id: str
    binding_id: object
    cwd: str


_NO_ENVIRONMENT_CWD_UPDATE = object()
_MISSING_ENVIRONMENT_CWD = object()


@dataclass
class _ToolCwdState:
    previous_overrides: dict[str, Any] | None
    previous_cwd: str | None
    previous_environment: Any | None
    previous_environment_cwd: Any
    plugin_owned_isolation: bool
    bindings: list[ToolCwdToken]


_TOOL_CWD_LOCK = threading.RLock()
_TOOL_CWD_CONDITION = threading.Condition(_TOOL_CWD_LOCK)
_TOOL_CWD_STATES: dict[str, _ToolCwdState] = {}
_PLUGIN_ISOLATED_SESSION_IDS: set[str] = set()
_CLEANING_ISOLATED_SESSION_IDS: set[str] = set()
_ISOLATION_KEYS = frozenset(
    {"docker_image", "modal_image", "singularity_image", "daytona_image", "env_type"}
)


def current_workspace() -> Workspace | None:
    return _CURRENT_WORKSPACE.get()


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", "")
    value = getattr(platform, "value", platform)
    return str(value or "").lower()


def _matches(route: Route, source: Any) -> bool:
    if route.platform != _platform_name(source):
        return False
    if route.chat_id is not None and route.chat_id != str(getattr(source, "chat_id", "") or ""):
        return False
    if route.thread_id is not None and route.thread_id != str(getattr(source, "thread_id", "") or ""):
        return False
    return True


def workspace_for_source(config: RouterConfig, source: Any) -> Workspace | None:
    matches = [route for route in config.routes if _matches(route, source)]
    if not matches:
        return None
    matches.sort(key=lambda route: (route.thread_id is not None, route.chat_id is not None), reverse=True)
    return config.workspaces[matches[0].workspace]


def _default_set_session_cwd(cwd: str) -> Any:
    from agent.runtime_cwd import set_session_cwd

    return set_session_cwd(cwd)


def _default_reset_session_cwd(token: Any) -> None:
    token.var.reset(token)


def _restore_session_cwd_direct(terminal_tool: Any, task_id: str, cwd: str | None) -> None:
    with terminal_tool._session_cwd_lock:
        if cwd is None:
            terminal_tool._session_cwd.pop(task_id, None)
        else:
            terminal_tool._session_cwd[task_id] = cwd


def _set_environment_cwd_if_current(
    terminal_tool: Any,
    task_id: str,
    environment: Any,
    cwd: Any,
) -> None:
    with terminal_tool._env_lock:
        if terminal_tool._active_environments.get(task_id) is environment:
            if cwd is _MISSING_ENVIRONMENT_CWD:
                if hasattr(environment, "cwd"):
                    delattr(environment, "cwd")
            else:
                environment.cwd = cwd


def _replace_tool_task_state(
    terminal_tool: Any,
    task_id: str,
    overrides: dict[str, Any] | None,
    cwd: str | None,
    *,
    update_isolated_environment: bool,
    environment_cwd: Any = _NO_ENVIRONMENT_CWD_UPDATE,
    expected_environment: Any | None = None,
) -> None:
    previous_overrides = terminal_tool._task_env_overrides.get(task_id)
    previous_cwd = terminal_tool.get_session_cwd(task_id)
    with terminal_tool._env_lock:
        environment = terminal_tool._active_environments.get(task_id)
        previous_environment_cwd = (
            getattr(environment, "cwd", _MISSING_ENVIRONMENT_CWD)
            if environment is not None
            else _NO_ENVIRONMENT_CWD_UPDATE
        )
    try:
        if overrides is None:
            terminal_tool._task_env_overrides.pop(task_id, None)
        else:
            terminal_tool._task_env_overrides[task_id] = dict(overrides)
        if cwd is None:
            terminal_tool.clear_session_cwd(task_id)
        else:
            terminal_tool.record_session_cwd(task_id, cwd)
        if (
            update_isolated_environment
            and environment is not None
            and environment_cwd is not _NO_ENVIRONMENT_CWD_UPDATE
            and (expected_environment is None or environment is expected_environment)
        ):
            _set_environment_cwd_if_current(
                terminal_tool,
                task_id,
                environment,
                environment_cwd,
            )
    except BaseException:
        if previous_overrides is None:
            terminal_tool._task_env_overrides.pop(task_id, None)
        else:
            terminal_tool._task_env_overrides[task_id] = previous_overrides
        _restore_session_cwd_direct(terminal_tool, task_id, previous_cwd)
        if environment is not None:
            try:
                _set_environment_cwd_if_current(
                    terminal_tool,
                    task_id,
                    environment,
                    previous_environment_cwd,
                )
            except BaseException:
                pass
        raise


def _requests_isolation(overrides: dict[str, Any] | None) -> bool:
    return bool(overrides and _ISOLATION_KEYS.intersection(overrides))


def _routed_overrides(
    terminal_tool: Any,
    state: _ToolCwdState,
    cwd: str,
) -> dict[str, Any]:
    overrides = dict(state.previous_overrides or {})
    overrides["cwd"] = cwd
    if not _requests_isolation(overrides):
        overrides["env_type"] = terminal_tool._get_env_config()["env_type"]
    return overrides


def _default_bind_tool_cwd(task_id: str, cwd: str) -> ToolCwdToken:
    from tools import terminal_tool

    with _TOOL_CWD_CONDITION:
        while task_id in _CLEANING_ISOLATED_SESSION_IDS:
            _TOOL_CWD_CONDITION.wait()
        state = _TOOL_CWD_STATES.get(task_id)
        if state is None:
            previous_overrides = terminal_tool._task_env_overrides.get(task_id)
            with terminal_tool._env_lock:
                previous_environment = terminal_tool._active_environments.get(task_id)
                previous_environment_cwd = (
                    getattr(
                        previous_environment,
                        "cwd",
                        _MISSING_ENVIRONMENT_CWD,
                    )
                    if previous_environment is not None
                    else _NO_ENVIRONMENT_CWD_UPDATE
                )
            plugin_owned_isolation = task_id in _PLUGIN_ISOLATED_SESSION_IDS or (
                not _requests_isolation(previous_overrides)
                and previous_environment is None
            )
            state = _ToolCwdState(
                previous_overrides=(
                    dict(previous_overrides)
                    if previous_overrides is not None
                    else None
                ),
                previous_cwd=terminal_tool.get_session_cwd(task_id),
                previous_environment=previous_environment,
                previous_environment_cwd=previous_environment_cwd,
                plugin_owned_isolation=plugin_owned_isolation,
                bindings=[],
            )
        token = ToolCwdToken(task_id=task_id, binding_id=object(), cwd=cwd)
        _replace_tool_task_state(
            terminal_tool,
            task_id,
            _routed_overrides(terminal_tool, state, cwd),
            cwd,
            update_isolated_environment=True,
            environment_cwd=cwd,
        )
        state.bindings.append(token)
        _TOOL_CWD_STATES[task_id] = state
        if state.plugin_owned_isolation:
            _PLUGIN_ISOLATED_SESSION_IDS.add(task_id)
        return token


def _default_reset_tool_cwd(token: ToolCwdToken) -> None:
    from tools import terminal_tool

    with _TOOL_CWD_LOCK:
        state = _TOOL_CWD_STATES.get(token.task_id)
        if state is None:
            return
        try:
            index = next(
                index
                for index, binding in enumerate(state.bindings)
                if binding.binding_id is token.binding_id
            )
        except StopIteration:
            return

        state.bindings.pop(index)
        try:
            if state.bindings:
                active_cwd = state.bindings[-1].cwd
                _replace_tool_task_state(
                    terminal_tool,
                    token.task_id,
                    _routed_overrides(terminal_tool, state, active_cwd),
                    active_cwd,
                    update_isolated_environment=True,
                    environment_cwd=active_cwd,
                )
            else:
                _replace_tool_task_state(
                    terminal_tool,
                    token.task_id,
                    state.previous_overrides,
                    state.previous_cwd,
                    update_isolated_environment=(
                        not state.plugin_owned_isolation
                        and state.previous_environment is not None
                    ),
                    environment_cwd=state.previous_environment_cwd,
                    expected_environment=state.previous_environment,
                )
                _TOOL_CWD_STATES.pop(token.task_id, None)
        except BaseException:
            state.bindings.insert(index, token)
            raise


def _cleanup_plugin_isolation_for_unrouted_session(task_id: str) -> None:
    from tools import terminal_tool

    with _TOOL_CWD_CONDITION:
        if task_id not in _PLUGIN_ISOLATED_SESSION_IDS:
            return
        if task_id in _TOOL_CWD_STATES:
            return
        if task_id in _CLEANING_ISOLATED_SESSION_IDS:
            return
        if _requests_isolation(terminal_tool._task_env_overrides.get(task_id)):
            _PLUGIN_ISOLATED_SESSION_IDS.discard(task_id)
            return
        _CLEANING_ISOLATED_SESSION_IDS.add(task_id)
    try:
        terminal_tool.cleanup_vm(task_id)
    finally:
        with _TOOL_CWD_CONDITION:
            _PLUGIN_ISOLATED_SESSION_IDS.discard(task_id)
            _CLEANING_ISOLATED_SESSION_IDS.discard(task_id)
            _TOOL_CWD_CONDITION.notify_all()


def install_gateway_patches(
    gateway: Any,
    config: RouterConfig,
    *,
    set_session_cwd: Callable[[str], Any] | None = None,
    reset_session_cwd: Callable[[Any], None] | None = None,
    bind_tool_cwd: Callable[[str, str], Any] | None = None,
    reset_tool_cwd: Callable[[Any], None] | None = None,
) -> None:
    """Wrap one gateway instance's session scope without changing process cwd."""
    if getattr(gateway, _PATCH_MARKER, False):
        return

    original_set = getattr(gateway, "_set_session_env", None)
    original_clear = getattr(gateway, "_clear_session_env", None)
    if not callable(original_set) or not callable(original_clear):
        raise CompatibilityError(
            "Hermes gateway is missing _set_session_env/_clear_session_env private APIs"
        )

    bind_cwd = set_session_cwd or _default_set_session_cwd
    reset_cwd = reset_session_cwd or _default_reset_session_cwd
    bind_tools = bind_tool_cwd or _default_bind_tool_cwd
    reset_tools = reset_tool_cwd or _default_reset_tool_cwd

    def routed_set(context: Any) -> RoutedSessionTokens:
        workspace = workspace_for_source(config, context.source)
        workspace_token = _CURRENT_WORKSPACE.set(workspace)
        hermes_tokens = None
        cwd_token = None
        tool_cwd_token = None
        try:
            hermes_tokens = original_set(context)
            session_id = str(getattr(context, "session_id", "") or "")
            if workspace is not None:
                cwd_token = bind_cwd(str(workspace.cwd))
                if not session_id:
                    raise CompatibilityError(
                        "Hermes gateway session context is missing session_id"
                    )
                tool_cwd_token = bind_tools(session_id, str(workspace.cwd))
            elif session_id:
                _cleanup_plugin_isolation_for_unrouted_session(session_id)
            return RoutedSessionTokens(
                hermes_tokens=hermes_tokens,
                workspace_token=workspace_token,
                cwd_token=cwd_token,
                tool_cwd_token=tool_cwd_token,
                routed=workspace is not None,
            )
        except BaseException:
            try:
                if tool_cwd_token is not None:
                    reset_tools(tool_cwd_token)
            finally:
                try:
                    if cwd_token is not None:
                        reset_cwd(cwd_token)
                finally:
                    try:
                        if hermes_tokens is not None:
                            original_clear(hermes_tokens)
                    finally:
                        _CURRENT_WORKSPACE.reset(workspace_token)
            raise

    def routed_clear(tokens: Any) -> None:
        if not isinstance(tokens, RoutedSessionTokens):
            original_clear(tokens)
            return
        try:
            original_clear(tokens.hermes_tokens)
        finally:
            try:
                if tokens.tool_cwd_token is not None:
                    reset_tools(tokens.tool_cwd_token)
            finally:
                try:
                    if tokens.cwd_token is not None:
                        reset_cwd(tokens.cwd_token)
                finally:
                    _CURRENT_WORKSPACE.reset(tokens.workspace_token)

    gateway._set_session_env = routed_set
    gateway._clear_session_env = routed_clear
    setattr(gateway, _PATCH_MARKER, True)


def make_pre_gateway_dispatch(config: RouterConfig):
    """Create the public hook that installs private compatibility adapters once."""

    def pre_gateway_dispatch(*, event: Any, gateway: Any, **_kwargs: Any) -> None:
        install_gateway_patches(gateway, config)
        return None

    return pre_gateway_dispatch
