# AGENTS.md

Hermes standalone plugin that applies project workspace contexts to selected gateway sessions using task-local state and isolated MCP toolsets.

## Commands

Run commands from the repository root.

```bash
PYTHONPATH=.:$HOME/.hermes/hermes-agent PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
python -m py_compile __init__.py workspace_context/*.py tests/*.py
git diff --check
```

Run a focused test while developing:

```bash
PYTHONPATH=.:$HOME/.hermes/hermes-agent PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests/test_runtime_routing.py
```

## Structure

- `__init__.py`: standalone Hermes plugin entry point.
- `plugin.yaml`: plugin manifest and hook declaration.
- `workspace_context/config.py`: local YAML validation and path resolution.
- `workspace_context/runtime.py`: gateway route matching and cwd ContextVar lifecycle.
- `workspace_context/skills.py`: read-only project skill overlays.
- `workspace_context/mcp.py`: Claude/Codex/OpenCode/Hermes MCP discovery, conversion, merging, namespacing, registration, and toolset scoping.
- `workspace_context/plugin.py`: startup orchestration.
- `tests/`: unit and active-Hermes compatibility tests.
- `.hermes/plans/`: implementation plan and private API rationale.

## Development rules

- Add a failing test before changing routing, cwd restoration, skill precedence, or MCP visibility.
- Keep Hermes private API access inside `runtime.py`, `skills.py`, and `mcp.py`.
- Never call `os.chdir()`. Gateway turns may run concurrently.
- Store per-turn state in `ContextVar`; restore tokens in `finally` blocks.
- Namespace MCP server names before inserting them into Hermes' global registry.
- Skip only the conflicting server when project MCP files reuse a name for different connection targets. Never log URLs, headers, environment values, or OAuth credentials in conflict warnings.
- Do not add workspace MCP toolsets to unrouted turns.
- Keep project skill access read-only. Do not redirect `skill_manage` into project directories.
- Treat missing workspace `cwd`, unknown route targets, and unset MCP environment variables as configuration errors.
- Preserve Hermes' fail-soft MCP connection behavior after configuration validation.

## Verification

Before committing:

1. Run the focused test for the changed subsystem.
2. Run the complete test suite with the active Hermes checkout on `PYTHONPATH`.
3. Run `py_compile` and `git diff --check`.
4. For private API changes, perform an active-Hermes smoke test that proves routed and unrouted catalogs differ.
5. Confirm `config.yaml`, credentials, channel IDs, workspace paths, and `.mcp.json` contents are not staged.

## Runtime changes

A loaded gateway keeps plugin modules and MCP connections in memory. After changing code or `config.yaml`, ask the operator to run `hermes-gateway restart` or use `/restart-gateway`. Do not restart the gateway from an agent sandbox.

`README.md` documents installation, route syntax, security boundaries, and private API compatibility. Update it when public behavior changes.
