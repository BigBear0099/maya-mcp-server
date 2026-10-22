---
share: external
---
<!-- Privacy reviewed: no machine-specific absolute paths, private note links, process identifiers, credentials or personal contact details. -->

# Maintenance handoff

This repository maintains the Maya 2022/2026 compatibility and reliability work
completed on 2026-10-22. The approved 0.2.0 scope is complete. There is no confirmed
unfinished feature work in this handoff.

| Item | Confirmed baseline |
| --- | --- |
| Maintained repository | [BigBear0099/maya-mcp-server](https://github.com/BigBear0099/maya-mcp-server) |
| Upstream | [chadrik/maya-mcp-server](https://github.com/chadrik/maya-mcp-server) |
| Upstream starting commit | [5eaa537](https://github.com/chadrik/maya-mcp-server/commit/5eaa537) |
| Default development branch | `codex/maya-2022-2026-reliability` |
| Maintained version | `0.2.0` |
| Verified delivery commit | [706cc2b](https://github.com/BigBear0099/maya-mcp-server/commit/706cc2b) |
| License | MIT; upstream attribution remains in [LICENSE](../LICENSE) |

Installing the package name from PyPI selects the upstream distribution. Use a
verified commit or a wheel built from this fork when maintaining or deploying
this version. The current tool contract is described in [README](../README.md).

## Runtime boundaries

| Runtime | Verified interpreter and Qt binding |
| --- | --- |
| External MCP host | Python 3.10 or newer; dependencies installed outside Maya |
| Maya 2022 | Python 3.7.7 and PySide2 |
| Maya 2026 | Python 3.11.9 and PySide6 |

The injected helper uses the standard library and Maya's bundled Qt. It must
remain compatible with Python 3.7. Host tests and standalone helper checks do
not replace verification inside the two Maya GUI runtimes.

Qt is the supported production transport. Native commandPort remains a
compatibility path; its separate execution and output requests do not provide
the same output isolation across different clients.

## Behavior to preserve

| Area | Required behavior |
| --- | --- |
| Result capture | Transform the final expression through AST nodes and compile it directly. Do not depend on `ast.unparse` or source-offset slicing. |
| Return modes | `NONE` always returns null; `RAW` returns a string; `JSON` preserves JSON value types. Non-serializable values, including nested NaN/Infinity, return errors. |
| Tool envelope | Keep `success`, `result`, `error`, `stdout`, `stderr`, `truncated` and `execution_state` in both MCP text and `structuredContent`. Failures set MCP `isError=true`. |
| Remote errors | Preserve exception type, message and traceback, plus output produced before the failure. Syntax and serialization errors use the same contract. |
| Atomic output | Execute and collect output in one Qt RPC. Drain earlier activity before the call and prevent Qt timer reentry from interleaving another request. |
| Output bounds | Bound each stream to 1 MiB of UTF-8 bytes. Mark clipping with `truncated` and omit an incomplete final character. |
| Qt lifecycle | Handle fragmented UTF-8 input and already-deleted sockets. Stop timers, disconnect callbacks and restore captured streams during helper replacement. |
| Bootstrap transfer | Transfer compressed source through acknowledged ASCII chunks, verify SHA256, and keep each native bootstrap command within 1024 bytes. Do not rely on a shared filesystem. |
| Helper reuse | Reuse identical source hashes so attaching another client preserves the shared Qt service. Source changes clean up the old helper and retain the maintained helper's separate user execution namespace. |
| Legacy upgrade | If an upstream helper has no teardown hook, stop its Qt service and uninstall stream capture. Do not copy arbitrary legacy helper globals into the new helper. |
| Response deadline | Accept a positive finite per-call `timeout`. Timeout or cancellation detaches the uncertain connection; a later explicit request may reconnect without replaying the original code. |
| Session pruning | Do not prune a busy execution or an intentionally detached session awaiting reconnect merely because a heartbeat cannot complete. |

The `execution_state` values distinguish completed work, a remote execution
failure, work that did not start, and an unknown result after communication
failure. A timeout does not cancel work already running inside Maya. Verify the
resulting scene state before deciding whether another operation is needed.

Multiple clients can share the Qt service, but scene mutations still require
coordination. An asynchronous job API for long operations is an optional future
design direction; it is not a scheduled task or an unfinished release item.

## Validation record

The maintenance delivery was verified on 2026-10-22:

| Verification | Recorded result |
| --- | --- |
| Local unit suite | 200 tests passed |
| GitHub CI | All five jobs passed: Windows/Linux host tests on Python 3.10/3.14, plus the standalone Python 3.7 helper check |
| Maya 2022 through the consumer wrapper | 9/9 smoke groups passed |
| Maya 2026 through the consumer wrapper | 9/9 smoke groups passed |
| Extended Maya 2026 run | A 65-second execution passed with a 0.2-second scan interval; timeout recovery also passed |

The smoke groups cover the real stdio-to-Qt route, runtime identity, scene and
Unicode round trips, return modes, exactly-once side effects, output and error
recovery, virtual modules, output bounds, shared clients and late-reply recovery.
The CI workflow is [tests.yml](../.github/workflows/tests.yml).

Raw runs remain under ignored `artifacts/`. Keep the runtime manifest, RPC
transcript and final `result.json` together when assessing a run. Record the
tested source commit, installed package identity and selected host interpreter
with deployment evidence. Review raw logs for privacy before sharing them.

## Validate a maintenance change

Run these commands from the repository root in the host development environment:

```sh
python -m pytest -q
python -m ruff check --no-cache src tests
python -m mypy src --ignore-missing-imports
```

Run the dependency-free helper check with each supported Maya interpreter:

```sh
mayapy scripts/check_injected_runtime.py
```

Run the GUI smoke driver against newly bootstrapped instances:

```sh
python scripts/smoke_maya.py --bootstrap --version 2022
python scripts/smoke_maya.py --bootstrap --version 2026 --long-operation
```

The extended run includes a deliberate 65-second operation. The harness chooses
a fresh process, unique `MAYA_APP_DIR`, private bootstrap port and ownership
token, then cleans up only the instance it created. It disables discovery and
attaches explicitly to that instance. Never replace these checks with tests
against an existing user scene, and never send Python probes to MEL/plugin ports.

After installing a candidate runtime, repeat the smoke tests through the actual
consumer launcher. Substitute local executable and launcher locations at run
time; do not commit those locations:

```sh
python scripts/smoke_maya.py --bootstrap --version 2022 --server-python "<launcher-python>" --server-wrapper "<consumer-wrapper>"
python scripts/smoke_maya.py --bootstrap --version 2026 --server-python "<launcher-python>" --server-wrapper "<consumer-wrapper>"
```

## Repository and deployment ownership

This repository owns MCP source, tests, CI, validation tooling and development
evidence. [AGENTS.md](../AGENTS.md) contains project-specific agent instructions.
Do not embed the MCP source tree in a consuming project.

The consumer project owns its launcher and machine-local runtime manifest.
The launcher selects the tested independent runtime, inherits stdio, waits for
the server child to exit and preserves its exit status. Keep logs on stderr and
reserve stdout for MCP messages. Retain the previous runtime and manifest for
rollback, and make missing configured runtimes fail explicitly.

Private design history and reusable knowledge remain in the configured local
knowledge vault.
Resolve `PENG_VAULT_ROOT` in the current execution environment and read the source
`AGENTS.md` there when that context is needed. Do not copy vault governance,
private note paths or machine configuration into this public repository. A new
development chat receives machine-specific locations through its private handoff.
