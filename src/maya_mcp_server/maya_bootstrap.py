"""Bootstrap code that runs in Maya to provide MCP server functionality.

This file is read and injected into Maya sessions to create a persistent
_mcp module with helper functions. It must be self-contained and use only
standard library imports that are available in Maya's Python environment.

The code is executed via: exec(open(__file__).read(), globals())
"""


def create_module(name: str, code: str, overwrite: bool = False) -> str:
    """Create a virtual module from source code."""
    import hashlib
    import json
    import sys
    import types

    parts = name.split(".")
    for i in range(len(parts) - 1):
        parent_name = ".".join(parts[: i + 1])
        if parent_name not in sys.modules:
            parent_mod = types.ModuleType(parent_name)
            setattr(parent_mod, "__path__", [])  # Package marker
            sys.modules[parent_name] = parent_mod

    if name in sys.modules and not overwrite:
        return json.dumps({"error": f"Module '{name}' already exists. Use overwrite=True."})
    source_sha = hashlib.sha256(code.encode("utf-8")).hexdigest()
    old_module = sys.modules.get(name)
    if overwrite and old_module is not None:
        if getattr(old_module, "_mcp_source_sha", None) == source_sha:
            # Multiple MCP clients attach to the same helper. Replacing an
            # identical helper would close the first client's Qt connection.
            return json.dumps(
                {"success": True, "message": f"Module '{name}' reused", "reused": True}
            )
    module = types.ModuleType(name)
    module.__file__ = f"<mcp:{name}>"
    compiled = compile(code, module.__file__, "exec")
    exec(compiled, module.__dict__)
    setattr(module, "_mcp_source_sha", source_sha)
    teardown_warning = None
    if overwrite and old_module is not None:
        # Only carry user execution state. Moving the old module's globals
        # would retain its obsolete helper functions, sockets and timers.
        namespace = getattr(old_module, "_execution_namespace", None)
        if isinstance(namespace, dict) and "_execution_namespace" in module.__dict__:
            setattr(module, "_execution_namespace", namespace)
        cleanup = getattr(old_module, "_mcp_teardown", None)
        cleanups = [cleanup] if callable(cleanup) else []
        if not cleanups and name == "maya_mcp":
            # Upgrading the original upstream helper predates the teardown
            # hook. Stop its listener/timer and unwrap its stream capture too.
            # Restrict this fallback to our helper; arbitrary virtual modules
            # may use these names for unrelated user functions.
            cleanups = [
                getattr(old_module, "stop_qt_server", None),
                getattr(old_module, "uninstall_stream_capture", None),
            ]
        warnings = []
        for cleanup in cleanups:
            if not callable(cleanup):
                continue
            try:
                cleanup()
            except Exception as exc:
                warnings.append(f"{type(exc).__name__}: {exc}")
        if warnings:
            teardown_warning = "; ".join(warnings)
    sys.modules[name] = module
    if len(parts) > 1:
        parent = sys.modules[".".join(parts[:-1])]
        setattr(parent, parts[-1], module)
    response = {"success": True, "message": f"Module '{name}' created"}
    if teardown_warning is not None:
        response["warning"] = teardown_warning
    return json.dumps(response)
