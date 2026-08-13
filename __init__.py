"""Hermes standalone plugin entry point."""

try:
    from .workspace_context.plugin import register
except ImportError:
    from workspace_context.plugin import register

__all__ = ["register"]
