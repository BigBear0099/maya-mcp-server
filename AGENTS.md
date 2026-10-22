---
share: external
---
<!-- Privacy reviewed: no personal paths, credentials or private knowledge links. -->

# Maya MCP maintenance branch

This fork maintains chadrik/maya-mcp-server for Maya 2022 (Python 3.7 / PySide2)
and Maya 2026 (Python 3.11 / PySide6). The external MCP host uses Python 3.10+.

- Resolve the local knowledge vault through `PENG_VAULT_ROOT` and read its source
  `AGENTS.md`; do not copy vault governance into this repository.
- Maya-injected code must use only the Maya standard library and bundled Qt, and
  must compile and run under Python 3.7. Host-side dependencies stay outside Maya.
- Use only freshly bootstrapped test instances with a unique `MAYA_APP_DIR`.
  Tests must never scan, execute in, close, or clear an existing user instance.
- No automatic replay after timeout, cancellation, or uncertain replies. Preserve
  explicit Python bootstrap port filtering and exclude MEL/plugin ports.
- `NONE`, `RAW`, and `JSON` use a stable tool envelope; failures must preserve
  traceback/output and set MCP `isError`. Test the real stdio transport too.
- Development evidence stays in `artifacts/`. Design and implementation history
  stays in the configured local knowledge vault; do not publish private records.
- Run the unit suite and isolated Maya 2022/2026 smoke test before deployment.
  Keep a recoverable deployment manifest and the previous installed package.
