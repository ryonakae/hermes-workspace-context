"""Task-local workspace routing adapters for Hermes gateway internals."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
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
    previous_overrides: dict[str, Any] | None
    previous_cwd: str | None


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


def _default_bind_tool_cwd(task_id: str, cwd: str) -> ToolCwdToken:
    from tools import terminal_tool

    previous_overrides = terminal_tool._task_env_overrides.get(task_id)
    previous_cwd = terminal_tool.get_session_cwd(task_id)
    routed_overrides = dict(previous_overrides or {})
    routed_overrides["cwd"] = cwd
    terminal_tool.register_task_env_overrides(task_id, routed_overrides)
    return ToolCwdToken(
        task_id=task_id,
        previous_overrides=(
            dict(previous_overrides) if previous_overrides is not None else None
        ),
        previous_cwd=previous_cwd,
    )


def _default_reset_tool_cwd(token: ToolCwdToken) -> None:
    from tools import terminal_tool

    terminal_tool.clear_task_env_overrides(token.task_id)
    if token.previous_overrides is not None:
        terminal_tool.register_task_env_overrides(
            token.task_id,
            token.previous_overrides,
        )
    if token.previous_cwd is not None:
        terminal_tool.record_session_cwd(token.task_id, token.previous_cwd)


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
            if workspace is not None:
                cwd_token = bind_cwd(str(workspace.cwd))
                session_id = str(getattr(context, "session_id", "") or "")
                if not session_id:
                    raise CompatibilityError(
                        "Hermes gateway session context is missing session_id"
                    )
                tool_cwd_token = bind_tools(session_id, str(workspace.cwd))
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
