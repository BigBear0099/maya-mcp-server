"""MCP helper"""

from __future__ import annotations

import ast
import json
import sys
import traceback
from types import CodeType
from typing import Any


# Try importing Qt from PySide2 (Maya 2022-2023) or PySide6 (Maya 2024+)
try:
    from PySide2.QtCore import QIODevice, QTimer
    from PySide2.QtNetwork import (
        QHostAddress,
        QTcpServer,
        QTcpSocket,
    )
except ImportError:
    try:
        from PySide6.QtCore import QIODevice, QTimer
        from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket
    except ImportError:
        # Qt not available - Qt server functions will fail gracefully
        QTcpServer = None
        QTcpSocket = None
        QHostAddress = None
        QTimer = None
        QIODevice = None

CAPTURE_VARIABLE = "_mcp_result"
MAX_OUTPUT_BYTES = 1_048_576
MAX_REQUEST_BYTES = 8_388_608
_execution_namespace: dict[str, Any] = {
    "__builtins__": __builtins__,
    "__name__": "__mcp_exec__",
    "__doc__": None,
}


def prepare_code_for_result_capture(
    code: str, capture_variable: str = CAPTURE_VARIABLE
) -> tuple[CodeType, bool]:
    """
    Transform Python code to capture the result of the final expression.

    If the final statement is a standalone expression at the module level
    (not nested in a loop, function, or class, and not an assignment),
    returns a compiled module which assigns its value to ``capture_variable``.
    Editing the AST keeps source locations and works on Maya 2022's Python
    3.7 without ``ast.unparse``. Syntax errors propagate to the execution
    envelope with the original line numbers.
    """
    tree = ast.parse(code, filename="<mcp>")
    last_stmt = tree.body[-1] if tree.body else None
    transformed = isinstance(last_stmt, ast.Expr)
    if isinstance(last_stmt, ast.Expr):
        assignment = ast.Assign(
            targets=[ast.Name(id=capture_variable, ctx=ast.Store())], value=last_stmt.value
        )
        tree.body[-1] = ast.copy_location(assignment, last_stmt)
        ast.fix_missing_locations(tree)
    return compile(tree, "<mcp>", "exec", dont_inherit=True), transformed


class StreamWriter:
    """Custom writer that captures output for MCP Resource streaming."""

    def __init__(self, stream_type: str, original: Any, max_bytes: int = MAX_OUTPUT_BYTES) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.stream_type = stream_type
        self._wrapped = original
        self._max_bytes = max_bytes
        self._buffer = bytearray()
        self._truncated = False

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("write() argument must be str")
        if text:
            encoded = text.encode("utf-8", errors="replace")
            remaining = self._max_bytes - len(self._buffer)
            self._buffer.extend(encoded[:remaining])
            self._truncated = self._truncated or len(encoded) > remaining
        if self._wrapped:
            self._wrapped.write(text)
        return len(text)

    def flush(self) -> None:
        if self._wrapped:
            self._wrapped.flush()

    def get_buffer(self) -> str:
        """Compatibility accessor; new callers use drain() for truncation metadata."""
        return self.drain()[0]

    def drain(self) -> tuple[str, bool]:
        # A byte cap can split the final UTF-8 character. Drop that incomplete
        # suffix rather than returning a replacement glyph or exceeding the cap.
        result = self._buffer.decode("utf-8", errors="ignore")
        truncated = self._truncated
        self._buffer.clear()
        self._truncated = False
        return result, truncated

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


_stdout_writer: StreamWriter | None = None
_stderr_writer: StreamWriter | None = None


def install_stream_capture() -> str:
    """Install stream capture for stdout/stderr."""
    global _stdout_writer
    global _stderr_writer

    if _stdout_writer is None:
        _stdout_writer = StreamWriter("stdout", sys.stdout)
        sys.stdout = _stdout_writer
    if _stderr_writer is None:
        _stderr_writer = StreamWriter("stderr", sys.stderr)
        sys.stderr = _stderr_writer
    return json.dumps({"success": True})


def uninstall_stream_capture() -> str:
    """Remove stream capture and restore original streams."""
    global _stdout_writer
    global _stderr_writer

    if _stdout_writer is not None:
        if sys.stdout is _stdout_writer:
            sys.stdout = _stdout_writer._wrapped
        _stdout_writer = None
    if _stderr_writer is not None:
        if sys.stderr is _stderr_writer:
            sys.stderr = _stderr_writer._wrapped
        _stderr_writer = None
    return json.dumps({"success": True})


def _drain_output() -> dict[str, Any]:
    stdout, out_truncated = _stdout_writer.drain() if _stdout_writer else ("", False)
    stderr, err_truncated = _stderr_writer.drain() if _stderr_writer else ("", False)
    return {"stdout": stdout, "stderr": stderr, "truncated": out_truncated or err_truncated}


def get_buffered_output() -> str:
    """Get buffered stdout/stderr and clear buffers, including truncation state."""
    return json.dumps(_drain_output())


def get_session_info() -> str:
    """Return session information as JSON."""
    import getpass
    import os

    import maya.cmds as cmds

    scene_path = cmds.file(query=True, sceneName=True) or ""
    scene_name = os.path.basename(scene_path) if scene_path else "untitled"
    return json.dumps(
        {
            "pid": os.getpid(),
            "user": getpass.getuser(),
            "maya_version": cmds.about(version=True),
            "scene_name": scene_name,
            "scene_path": scene_path,
        }
    )


def _exception_info(error: BaseException) -> dict[str, str]:
    try:
        message = str(error)
    except Exception:
        message = "<exception message could not be converted to text>"
    return {
        "type": f"{type(error).__module__}.{type(error).__name__}",
        "message": message,
        "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
    }


def _execute_response(code: str, result_type: str = "NONE") -> dict[str, Any]:
    """Execute exactly once; make every failure serializable on both transports."""
    result = None
    error = None
    context = _execution_namespace
    try:
        if result_type not in ("NONE", "RAW", "JSON"):
            raise ValueError("result_type must be NONE, RAW, or JSON")
        if result_type == "NONE":
            compiled = compile(code, "<mcp>", "exec", dont_inherit=True)
        else:
            compiled, was_modified = prepare_code_for_result_capture(code)
            if not was_modified:
                raise RuntimeError("A captured result requires a final expression at module scope.")
        exec(compiled, context)
        if result_type != "NONE":
            value = context.pop(CAPTURE_VARIABLE)
            # JSON stays double-encoded for the existing host-side decoder.
            # RAW supports arbitrary Maya objects through their string form.
            result = json.dumps(value, allow_nan=False) if result_type == "JSON" else str(value)

    except BaseException as e:
        # SystemExit/KeyboardInterrupt must complete the RPC as errors too;
        # leaving it unanswered would hide cancellation behind a timeout.
        result = None
        error = _exception_info(e)
    return {"result": result, "error": error}


def execute(code: str, result_type: str = "NONE") -> str:
    """Legacy result/error envelope; NONE always returns a null result."""
    return json.dumps(_execute_response(code, result_type))


def execute_and_get_buffered(code: str, result_type: str = "NONE") -> str:
    """Execute and collect its output in one main-thread RPC.

    Drain earlier interactive output before executing so prior activity cannot
    be mistaken for the current request's output. Qt dispatch serializes this
    entire operation, including when the payload calls processEvents().
    """
    install_stream_capture()
    _drain_output()
    response = _execute_response(code, result_type)
    response.update(_drain_output())
    return json.dumps(response)


def start_command_port(port: int) -> str:
    import maya.cmds as cmds

    cmds.commandPort(name=f":{port}", sourceType="python")
    return json.dumps({"success": True, "port": port})


class QtCommandServer:
    """Qt-based TCP command server for Maya MCP.

    Runs on Maya's main thread using Qt's event loop.
    Uses QTimer for non-blocking I/O processing.
    Supports multiple concurrent client connections.
    """

    def __init__(self, port: int = 0):
        if QTcpServer is None:
            raise RuntimeError("Qt not available - cannot create command server")

        self._server = QTcpServer()
        # client_id -> {socket, input_buffer, output_queue}
        self._clients: dict[int, dict[str, Any]] = {}
        self._process_timer = QTimer()
        self._port = port
        self._running = False
        self._processing = False
        self._next_client_id = 0

        # Connect signals
        self._server.newConnection.connect(self._on_new_connection)
        self._process_timer.timeout.connect(self._process_messages)

    def start(self) -> None:
        """Start listening on OS-assigned port."""
        if self._running:
            return
        # Listen on loopback with OS-assigned port (port 0)
        if not self._server.listen(QHostAddress.LocalHost, self._port):
            raise RuntimeError(f"Failed to start server: {self._server.errorString()}")

        # if port was 0, it's OS-assigned, so reassign new value
        self._port = self._server.serverPort()
        self._running = True

        # Start message processing timer (check every 50ms)
        self._process_timer.start(50)

    def stop(self) -> None:
        """Stop the server and close connections."""
        self._running = False
        # Disconnect callbacks before releasing objects: deleteLater() from a
        # prior teardown can otherwise fire into an obsolete module instance.
        for obj, signal_name, slot in (
            (self._process_timer, "timeout", self._process_messages),
            (self._server, "newConnection", self._on_new_connection),
        ):
            try:
                getattr(obj, signal_name).disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        try:
            self._process_timer.stop()
        except RuntimeError:
            pass
        for client_id in list(self._clients):
            self._remove_client(client_id)
        for obj in (self._process_timer, self._server):
            try:
                if obj is self._server:
                    obj.close()
                obj.deleteLater()
            except RuntimeError:
                pass

    @property
    def port(self) -> int:
        """Get the actual port the server is listening on."""
        return self._port

    def _on_new_connection(self) -> None:
        """Handle new client connection."""
        if not self._running:
            return
        socket = self._server.nextPendingConnection()
        if socket is None:
            return

        # Assign unique client ID
        client_id = self._next_client_id
        self._next_client_id += 1

        # Store client info
        self._clients[client_id] = {"socket": socket, "input_buffer": b"", "output_queue": []}

        # Connect signals with client_id
        socket.readyRead.connect(lambda cid=client_id: self._on_ready_read(cid))
        socket.disconnected.connect(lambda cid=client_id: self._on_disconnected(cid))

    def _on_ready_read(self, client_id: int) -> None:
        """Read data from client (called by Qt signal)."""
        # Just mark that data is available; actual processing in timer
        pass

    def _on_disconnected(self, client_id: int) -> None:
        """Handle client disconnect."""
        self._remove_client(client_id)

    def _remove_client(self, client_id: int) -> None:
        client_info = self._clients.pop(client_id, None)
        if client_info is None:
            return
        socket = client_info["socket"]
        for signal_name in ("readyRead", "disconnected"):
            try:
                getattr(socket, signal_name).disconnect()
            except (RuntimeError, TypeError):
                pass
        # Peer shutdown may have deleted the C++ socket before Python notices.
        for action in ("disconnectFromHost", "close", "deleteLater"):
            try:
                getattr(socket, action)()
            except RuntimeError:
                pass

    def _process_messages(self) -> None:
        """Process incoming/outgoing messages (called by timer)."""
        # A payload may pump the Qt event loop. Never let the timer execute a
        # second request inside its first request's stdout/error transaction.
        if not self._running or self._processing:
            return
        self._processing = True
        try:
            for client_id, client_info in list(self._clients.items()):
                if client_id not in self._clients:
                    continue
                socket = client_info["socket"]
                try:
                    if not socket.isValid() or socket.state() != QTcpSocket.ConnectedState:
                        self._remove_client(client_id)
                        continue
                    if socket.bytesAvailable() > 0:
                        # Keep undecoded bytes until the complete JSON line has
                        # arrived; TCP can split any multibyte UTF-8 character.
                        incoming = bytes(socket.readAll())
                        input_buffer = client_info["input_buffer"] + incoming
                        if len(input_buffer) > MAX_REQUEST_BYTES:
                            self._remove_client(client_id)
                            continue
                        while b"\n" in input_buffer:
                            line, input_buffer = input_buffer.split(b"\n", 1)
                            client_info["input_buffer"] = input_buffer
                            if line.strip():
                                self._handle_message(client_id, line.strip())
                            if client_id not in self._clients or not self._running:
                                break
                        client_info["input_buffer"] = input_buffer
                    if client_id in self._clients and self._running:
                        output_queue = client_info["output_queue"]
                        if output_queue:
                            message = output_queue.pop(0)
                            socket.write((message + "\n").encode("utf-8"))
                            socket.flush()
                except RuntimeError:
                    self._remove_client(client_id)
        finally:
            self._processing = False

    def _handle_message(self, client_id: int, line: bytes | str) -> None:
        """Handle a complete JSON message."""
        if client_id not in self._clients:
            return

        output_queue = self._clients[client_id]["output_queue"]
        request: Any = None

        try:
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Request must be a JSON object")
            response = self._dispatch_request(request)
            output_queue.append(json.dumps(response))
        except Exception as e:
            error_response = {
                "id": request.get("id") if isinstance(request, dict) else None,
                "result": None,
                "error": _exception_info(e),
            }
            output_queue.append(json.dumps(error_response))

    def _dispatch_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Dispatch request to appropriate handler."""
        method = request.get("method")
        params = request.get("params", {})
        req_id = request.get("id")

        try:
            # Dispatch to existing helper functions
            if method == "execute":
                result_str = execute(params.get("code", ""), params.get("result_type", "NONE"))
                result_obj = json.loads(result_str)
                return {
                    "id": req_id,
                    "result": result_obj.get("result"),
                    "error": result_obj.get("error"),
                }

            elif method == "execute_and_get_buffered":
                result_str = execute_and_get_buffered(
                    params.get("code", ""), params.get("result_type", "NONE")
                )
                response: dict[str, Any] = json.loads(result_str)
                response["id"] = req_id
                return response

            elif method == "get_session_info":
                result_str = get_session_info()
                return {"id": req_id, "result": json.loads(result_str), "error": None}

            elif method == "install_stream_capture":
                install_stream_capture()
                return {"id": req_id, "result": {"success": True}, "error": None}

            elif method == "uninstall_stream_capture":
                uninstall_stream_capture()
                return {"id": req_id, "result": {"success": True}, "error": None}

            elif method == "get_buffered_output":
                result_str = get_buffered_output()
                return {"id": req_id, "result": json.loads(result_str), "error": None}

            elif method == "create_module":
                # Import create_module from maya_bootstrap
                create_module_func = globals().get("create_module")
                if create_module_func is None:
                    return {
                        "id": req_id,
                        "result": None,
                        "error": {"message": "create_module function not available"},
                    }
                result_str = create_module_func(
                    params.get("name", ""), params.get("code", ""), params.get("overwrite", False)
                )
                result_obj = json.loads(result_str)
                if "error" in result_obj:
                    return {"id": req_id, "result": None, "error": {"message": result_obj["error"]}}
                return {"id": req_id, "result": result_obj, "error": None}

            elif method == "ping":
                return {"id": req_id, "result": "pong", "error": None}

            else:
                return {
                    "id": req_id,
                    "result": None,
                    "error": {"message": f"Unknown method: {method}"},
                }
        except Exception as e:
            return {
                "id": req_id,
                "result": None,
                "error": _exception_info(e),
            }


# Global Qt server instance
_qt_server: QtCommandServer | None = None


def start_qt_server(port: int) -> str:
    """Start the Qt command server and return the port number."""
    global _qt_server

    if _qt_server is not None and _qt_server._running:
        return json.dumps({"port": _qt_server.port, "already_running": True})

    new_server = None
    try:
        new_server = QtCommandServer(port)
        new_server.start()
        _qt_server = new_server
        return json.dumps({"port": _qt_server.port, "already_running": False})
    except Exception as e:
        if new_server is not None:
            new_server.stop()
        _qt_server = None
        return json.dumps({"error": _exception_info(e)})


def stop_qt_server() -> str:
    """Stop the Qt command server."""
    global _qt_server
    old_server = _qt_server
    _qt_server = None
    if old_server is not None:
        old_server.stop()
    return json.dumps({"success": True})


def teardown() -> str:
    """Release helper-owned Qt and stream objects before module replacement.

    User variables live in _execution_namespace and deliberately survive this
    cleanup. create_module can transfer that dictionary to the replacement.
    """
    try:
        stop_qt_server()
    finally:
        uninstall_stream_capture()
    return json.dumps({"success": True})


def _mcp_teardown() -> str:
    """Reload hook recognized by both bootstrap implementations."""
    return teardown()


def get_qt_server_port() -> str:
    """Get the port of the running Qt server."""
    if _qt_server is None:
        return json.dumps({"error": "Server not running"})
    return json.dumps({"port": _qt_server.port})
