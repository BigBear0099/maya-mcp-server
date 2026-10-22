---
share: external
---
<!-- Privacy reviewed: examples use generic installation paths and contain no credentials. -->

# maya-mcp-server

## Maintained compatibility branch

This fork of [chadrik/maya-mcp-server](https://github.com/chadrik/maya-mcp-server)
maintains the `codex/maya-2022-2026-reliability` branch for **Maya 2022 / Python 3.7 /
PySide2** and **Maya 2026 / Python 3.11 / PySide6**. The external MCP server requires
Python 3.10+; its dependencies are never installed into Maya's interpreter.

The maintained version is **0.2.0**. Installing `maya-mcp-server` from PyPI still
selects the upstream package. Install this fork from an explicitly verified commit
or build a wheel from the tested checkout:

```sh
python -m pip install "maya-mcp-server @ git+https://github.com/BigBear0099/maya-mcp-server.git@<verified-commit>"
```

The first maintenance release repairs Python 3.7 result capture using compiled AST,
propagates remote exceptions, and returns this envelope in both MCP text and
`structuredContent`:

```json
{"success":true,"result":null,"error":null,"stdout":"","stderr":"","truncated":false,"execution_state":"completed"}
```

`NONE` executes without capturing a result and always returns `null`. `RAW` returns
the final expression's string representation. `JSON` preserves JSON value types.
Remote failures set MCP `isError=true` and preserve exception type, message,
traceback, and captured output. A communication timeout reports
`execution_state="unknown"`: the Maya operation may have completed, so the server
does **not** automatically replay it. `execute_code` accepts a positive finite
`timeout` in seconds. This is a response deadline, not cancellation of Maya work.

Qt execution and output capture use one atomic request. Each output stream is
bounded to 1 MiB of UTF-8 data and sets `truncated` when clipped. Busy requests are
protected from same-client heartbeat pruning, and timed-out connections are
discarded so late replies cannot be consumed by later requests. Identical injected
helpers are reused across clients. Same-scene mutations still require coordination.

Automatic discovery only probes `7002,7003,7004,7005,7006` by default. Override with
`MAYA_MCP_ALLOW_PORTS`; `0` disables discovery for explicit `add_session` use. MEL
and plugin ports `4434,4648,4658` remain excluded. Only pass known Python command
ports to `add_session`. Qt is the supported production transport; native commandPort
is retained as a compatibility path with less reliable cross-client output capture.

For the verified baseline, maintenance boundaries and future validation workflow,
read [Maintenance handoff](Docs/MAINTENANCE.md).

Validation commands:

```sh
python -m pytest -q
mayapy scripts/check_injected_runtime.py
python scripts/smoke_maya.py --bootstrap --version 2022
python scripts/smoke_maya.py --bootstrap --version 2026
```

The GUI smoke driver uses a new process, unique `MAYA_APP_DIR`, an owned bootstrap
port and the actual stdio/Qt transport. It checks scene changes, Unicode, all result
modes, errors, shared-client output, and timeout recovery. Raw evidence stays in
ignored `artifacts/` directories. CI covers host tests and Python 3.7 helper code;
GUI tests require licensed local Maya installations.

Patch references: upstream [exception PR #8](https://github.com/chadrik/maya-mcp-server/pull/8),
[timeout PR #6](https://github.com/chadrik/maya-mcp-server/pull/6), and PheobeAI's
[atomic output](https://github.com/PheobeAI/maya-mcp-server/commit/e7d2f2de6a9bd96b6585c41e2bafab7ecb2b00ad)
and [Qt cleanup](https://github.com/PheobeAI/maya-mcp-server/commit/55daa3df28606f473409502d07c78593a37a5b7e)
changes informed the maintenance implementation. Their permanent probe blacklist
and four-second heartbeat pruning were not adopted. Upstream MIT attribution is
preserved in `LICENSE`.

## Upstream overview

MCP server for interacting with Autodesk Maya sessions.

## Features

- **Multi-session support**: Manage multiple Maya sessions from a single MCP server. The server scans for new Maya sessions that have been started and shutdown by the user.
- **Full Python expressiveness**: Execute arbitrary Python code. Agents can create virtual python modules to expose functions for execution, including by the user.
- **Streaming output**: Capture stdout/stderr from Maya sessions via MCP resources. Agents can monitor output from their code or user activity.
- **Simple Maya-side setup**: No modules to install in Maya: leverages Maya's command port to bootstrap itself.
- **Easy installation**: Install and run via `uvx maya-mcp-server`

![screen_recording_4x.gif](screen_recording_4x.gif)

## Installation

```bash
# Using uvx (recommended)
uvx maya-mcp-server

# Or install with pip
pip install maya-mcp-server
```

## Usage

### Claude Code Configuration

Add to your Claude Code MCP configuration, run:

```commandline
claude mcp add --transport stdio maya -- uvx maya-mcp-server
```

The default scope is "local", which adds it to your `~/.claude.json` keyed to a particular project directory.  Setting `--scope=user` adds to `~/.claude.json` across all projects, and `--scope=project` to add the configuration into a `.mcp.json` in the current project directory, so that it can be commited to your repo.

For local development use: 
```commandline
claude mcp add --transport stdio maya -- uv run --directory /path/to/maya-mcp-server/ maya-mcp-server
```

### Maya Setup

The server automatically discovers Maya sessions via command ports. To enable a Python command port in Maya:

```python
import maya.cmds as cmds
cmds.commandPort(name=":7002", sourceType="python")
```

Or add to your `userSetup.py` for automatic startup.

## Tools

Tools accept an optional `session_key` parameter for targeting specific sessions.
If only one Maya session exists, it will be auto-selected.
Session keys are returned by `list_sessions` and `add_session`.

| Tool | Description |
|------|-------------|
| `list_sessions` | List all active Maya sessions. Returns session info including `session_key` for use with other tools/resources. |
| `add_session` | Manually add a Maya session at a specific host:port. Use when auto-discovery doesn't find your session. |
| `write_module` | Create a virtual Python module in Maya. Useful for defining reusable functions. |
| `execute_code` | Execute Python code in a session. Supports result capture modes: `NONE`, `JSON`, `RAW`. |

## Resources

| Resource | Description |
|----------|-------------|
| `maya://sessions/{session_key}/info` | Session information (pid, user, maya_version, scene_name, scene_path) |
| `maya://sessions/{session_key}/output` | Captured stdout/stderr output from the session |

## Similar tools

### [MayaMCP](https://github.com/PatrickPalmer/MayaMCP/)

This looks to be the first publicly available MCP server for Maya and I was inspired by a few aspects of this tool, especially the goal of zero Maya-side setup.

Disadvantages:
* The MCP server is bound to a single Maya session running on the default port.
* It is limited to a bespoke set of tools.  This could be seen as a security advantage, but it cripples the ability of an agent to do just about anything.
* No support for reading stdout or stderr, so the agent is blind to what's happening in the Maya session.
* Less robust approach to capturing command output (e.g. does not check if code is indented within a `for` loop or function)
* Can't run via `uvx`, or `pip install` from pypi.

### [ChatGPT4Maya](https://github.com/thejoltjoker/ChatGPTforMaya)

This is the original LLM integration for Maya, which embeds ChatGPT directly in a PySide window and enables the LLM to respond to user commands and queries by executing code in the session.

Disadvantages:
* Not an MCP server, so it cannot take full advantage of agentic workflows.
* Only works with ChatGPT.

### [Jupyter MCP Server](https://jupyter-mcp-server.datalayer.tech/)

This provided an interesting reference for how to create an MCP server in python that works with multiple remote sessions (in this case, notebooks) to execute arbitrary code.

## Development

```bash
# Install dev dependencies
uv sync --dev

# Run tests
uv run pytest

# Run the server
uv run maya-mcp-server
```

## License

MIT

## TODO

- [ ] Provide an option to `execute` to run in global or private context.
- [ ] Yield output as it's printed?
- [ ] Add tools to simplify interaction with UI: shelves, hotkeys, menus
- [ ] Plugins to extend session info, e.g. with custom pipeline info
- [ ] Investigate RPC for extensibility, implementation of custom tools
- [ ] Use a dispatch function for command port mode, to further harmonize. Create a shared type safe collection of tools that hold name and arguments. 
- [ ] Return stdout and stderr lines interleaved (and prefixed with `STDOUT:` `STDERR:`) so that the agent can determine order?
- [ ] Cleanup command ports when complete.  This won't be necessary if we default to the Qt command server. 
