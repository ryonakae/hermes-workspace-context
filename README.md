# hermes-workspace-context

Apply a project workspace context to selected Hermes gateway conversations without creating separate profiles.

## Features

- **Session-local cwd:** Pins Hermes' logical working directory for matching gateway sessions without calling `os.chdir()`.
- **Workspace context:** Applies the workspace's cwd, context files, skills, and MCP tools to each matching conversation.
- **Project-first skills:** Adds configured project skill directories to the standard skills index. A project skill wins when its name matches a global skill.
- **Workspace MCP:** Auto-detects Claude Code, Codex, OpenCode, and Hermes project MCP configs, merges compatible entries, and exposes their namespaced tools only while that workspace route is active.
- **Default profile preserved:** Unmatched conversations keep their existing profile, memory, plugins, skills, cwd, and toolsets.

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [Configuration](#configuration)
- [Safety boundaries](#safety-boundaries)
- [Compatibility](#compatibility)
- [Development](#development)
- [License](#license)

## Requirements

- Hermes Agent with standalone plugin support.
- Python 3.11 or newer.
- A gateway platform supported by Hermes, such as Slack, Discord, or Telegram.
- A Hermes version that still provides the private APIs listed under [Compatibility](#compatibility).

## Install

Install and enable the plugin:

```bash
hermes plugins install ryonakae/hermes-workspace-context --enable
```

Hermes copies `config.yaml.example` to the ignored local file `config.yaml` during installation. Edit that file in the installed plugin directory:

```bash
PLUGIN_DIR="$HOME/.hermes/plugins/hermes-workspace-context"
${EDITOR:-vi} "$PLUGIN_DIR/config.yaml"
```

Configure one or more workspaces and routes:

```yaml
workspaces:
  web-app:
    cwd: /Users/me/dev/web-app
    skill_dirs:
      - .agents/skills

routes:
  - platform: slack
    chat_id: C0123456789
    workspace: web-app
```

Restart the Hermes gateway after changing plugin code or configuration:

```bash
hermes-gateway restart
```

## Usage

Send a message to a configured gateway channel. Hermes will handle that turn as if it had started from the workspace's `cwd`:

- terminal and file tools resolve against the workspace;
- workspace context files enter the normal Hermes context-file pipeline;
- project skills appear in the standard skill index and work with `skill_view`;
- workspace MCP tools appear through Hermes' normal direct-tool or deferred `tool_search` interface.

Messages in unmatched channels continue with the normal profile environment.

A route may target a whole channel or a single thread:

```yaml
routes:
  - platform: slack
    chat_id: C0123456789
    thread_id: "1786579873.276259"
    workspace: web-app
```

A thread-specific route takes precedence over a channel-only route.

## Configuration

### `workspaces`

Each workspace accepts:

| Key | Required | Meaning |
| --- | --- | --- |
| `cwd` | yes | Existing absolute path or `~`-relative project directory. |
| `skill_dirs` | no | Skill roots. Relative paths resolve from `cwd`. |
| `auto_detect_mcp` | no | Detect supported project MCP files. Defaults to `true` when `mcp_file` is omitted. |
| `mcp_file` | no | Read only this Claude-style `mcpServers` JSON file instead of auto-detection. Relative paths resolve from `cwd`; do not combine it with `auto_detect_mcp: true`. |

Auto-detection reads these files when present:

- `.mcp.json` (`mcpServers`) for Claude Code;
- `.codex/config.toml` (`mcp_servers`) for Codex;
- `opencode.json` or `opencode.jsonc` (`mcp`) for OpenCode;
- `.hermes/config.yaml` or `.hermes/config.yml` (`mcp_servers`) for Hermes.

Servers with the same name and connection target are merged, so one file can provide OAuth or timeout details that another omits. If the same name points to a different URL or command, only that server is skipped and a warning names the conflicting files without logging their values. Other servers remain available.

Missing skill directories and MCP files are allowed. A missing workspace `cwd` is a configuration error.

`${NAME}`, `${env:NAME}`, and `{env:NAME}` placeholders in MCP strings expand from the gateway process environment. Startup fails for that MCP configuration when a referenced variable is unset. Keep credentials out of this repository and out of `config.yaml`; place them in the environment or the agent's normal ignored credential store.

Remote OAuth defaults are preserved during conversion: Claude Code, Codex, and OpenCode remote entries become Hermes `auth: oauth` unless they explicitly disable OAuth or provide an `Authorization` header. OpenCode timeouts are converted from milliseconds to Hermes seconds. A Codex server using `auth = "chatgpt"` is skipped because Hermes cannot safely reproduce that credential source.

### `routes`

Each route accepts:

| Key | Required | Meaning |
| --- | --- | --- |
| `platform` | yes | Lowercase Hermes platform name. |
| `workspace` | yes | Key from `workspaces`. |
| `chat_id` | no | Platform conversation or channel ID. |
| `thread_id` | no | Platform thread or topic ID. |

A route must include `chat_id`, `thread_id`, or both.

## Safety boundaries

The plugin does not change the process cwd. It uses Hermes' session-scoped cwd `ContextVar`, then restores the previous token after the turn. The workspace marker is another task-local `ContextVar`, so concurrent routed and unrouted turns do not share state.

MCP servers live in Hermes' process-wide MCP registry because Hermes manages MCP connections globally. The plugin namespaces server names as `workspace-<workspace>-<server>` and adds their toolsets only to matching turns. Unmatched turns cannot discover those tools through their enabled toolset catalog.

Project skills are read-only through this overlay. `skill_manage` continues to target the normal Hermes skill store instead of modifying project files.

## Compatibility

This plugin intentionally uses Hermes private APIs because the public plugin API does not expose per-session cwd, skill roots, or toolsets. The compatibility layer depends on:

- gateway instance methods `_set_session_env()` and `_clear_session_env()`;
- `agent.runtime_cwd.set_session_cwd()` returning a `ContextVar` token;
- skill discovery helpers in `agent.skill_utils`, `agent.prompt_builder`, and `tools.skills_tool`;
- `hermes_cli.tools_config._get_platform_tools()`;
- `tools.mcp_tool.register_mcp_servers()`.

The plugin fails closed when required gateway or toolset APIs disappear. Run the test suite after upgrading Hermes.

## Development

Clone into any directory and run:

```bash
git clone https://github.com/ryonakae/hermes-workspace-context.git
cd hermes-workspace-context
PYTHONPATH=.:$HOME/.hermes/hermes-agent PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
python -m py_compile __init__.py workspace_context/*.py tests/*.py
git diff --check
```

The tests use fake Hermes modules for narrow contracts and the active Hermes checkout for the cwd token restoration regression test.

## License

[MIT](LICENSE)
