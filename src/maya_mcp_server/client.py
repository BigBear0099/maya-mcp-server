"""Maya client for communicating with Maya command ports."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import random
import uuid
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from typing_extensions import Self

from maya_mcp_server.bootstrap import (
    get_bootstrap_code,
    get_helper_module_code,
)
from maya_mcp_server.types import (
    COMMUNICATION_PORT_MAX,
    COMMUNICATION_PORT_MIN,
    CommandResponse,
    OutputBuffer,
    PortType,
    ResultType,
    SessionInfo,
)


if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class MayaConnectionError(Exception):
    """Error connecting to Maya."""

    pass


class MayaExecutionError(Exception):
    """Error executing code in Maya."""

    pass


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ACCUMULATED_BYTES = 256 * 1024


@dataclass
class BaseMayaClient(ABC):
    """Abstract base class for Maya clients."""

    GET_SESSION_INFO: ClassVar[str]
    EXECUTE_TEMPLATE: ClassVar[str]
    CREATE_MODULE_TEMPLATE: ClassVar[str]
    INSTALL_STREAM_CAPTURE: ClassVar[str]
    UNINSTALL_STREAM_CAPTURE: ClassVar[str]
    GET_BUFFERED_OUTPUT: ClassVar[str]
    ATOMIC_EXECUTION: ClassVar[bool] = False

    host: str = "127.0.0.1"
    port: int = 7001
    timeout: float = 30.0
    buffer_size: int = 65536

    def __post_init__(self) -> None:
        """
        Initialize a Maya client.

        Args:
            host: Maya host address
            port: Maya command port number
            timeout: Default timeout for operations in seconds
            buffer_size: Socket buffer size
        """
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()
        self._active_executions = 0
        self._awaiting_reconnect = False
        self._last_output_truncated = False
        self._native_buffer = bytearray()
        # Output buffers for stdout/stderr capture
        self._stdout_buffer: str = ""
        self._stderr_buffer: str = ""

    @property
    def key(self) -> str:
        """Unique key for the session."""
        return f"{self.host}:{self.port}"

    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._writer is not None and not self._writer.is_closing()

    @property
    def is_busy(self) -> bool:
        """Include queued executions and other requests that own the wire."""
        return self._active_executions > 0 or self._lock.locked() or self._connection_lock.locked()

    @property
    def awaiting_reconnect(self) -> bool:
        """An ambiguous timeout/cancel detached this stream intentionally."""
        return self._awaiting_reconnect

    def _effective_timeout(self, timeout: float | None) -> float:
        value = self.timeout if timeout is None else timeout
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timeout must be a finite positive number of seconds")
        return value

    def _discard_connection(self, *, recoverable: bool = False) -> asyncio.StreamWriter | None:
        """Detach a poisoned stream before another request can use it."""
        writer = self._writer
        self._writer = None
        self._reader = None
        self._native_buffer.clear()
        self._awaiting_reconnect = recoverable
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        return writer

    async def connect(self) -> None:
        """
        Establish connection to Maya command port.

        Raises:
            MayaConnectionError: If connection fails
        """
        async with self._connection_lock:
            if self.is_connected:
                self._awaiting_reconnect = False
                return
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=self.timeout,
                )
                self._awaiting_reconnect = False
                logger.info(f"Connected to Maya at {self.host}:{self.port}")
            except asyncio.TimeoutError as e:
                raise MayaConnectionError(
                    f"Timeout connecting to Maya at {self.host}:{self.port}"
                ) from e
            except OSError as e:
                self._awaiting_reconnect = False
                raise MayaConnectionError(
                    f"Failed to connect to Maya at {self.host}:{self.port}: {e}"
                ) from e

    async def disconnect(self) -> None:
        """Close connection to Maya."""
        # FIXME: shut down the maya command port running in Maya
        writer = self._discard_connection()
        if writer is not None:
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass

    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        """Async context manager exit."""
        await self.disconnect()

    def append_output(self, stdout: str = "", stderr: str = "") -> None:
        """
        Append output to the client's buffers.

        This is called after execute_code to accumulate output.

        Args:
            stdout: Stdout content to append
            stderr: Stderr content to append
        """
        self._stdout_buffer = self._append_capped(self._stdout_buffer, stdout)
        self._stderr_buffer = self._append_capped(self._stderr_buffer, stderr)

    @staticmethod
    def _append_capped(existing: str, new: str) -> str:
        combined = (existing + new).encode("utf-8")
        if len(combined) <= MAX_ACCUMULATED_BYTES:
            return combined.decode("utf-8")
        marker = "[earlier accumulated output truncated]\n"
        keep = MAX_ACCUMULATED_BYTES - len(marker.encode("utf-8"))
        return marker + combined[-keep:].decode("utf-8", errors="ignore")

    def get_accumulated_output(self, clear: bool = True) -> OutputBuffer:
        """
        Get accumulated stdout/stderr output.

        Args:
            clear: If True, clear the buffers after reading

        Returns:
            OutputBuffer with stdout and stderr fields
        """
        output = OutputBuffer(stdout=self._stdout_buffer, stderr=self._stderr_buffer)

        if clear:
            self._stdout_buffer = ""
            self._stderr_buffer = ""

        return output

    def clear_output(self) -> None:
        """Clear the accumulated output buffers."""
        self._stdout_buffer = ""
        self._stderr_buffer = ""

    async def install_stream_capture(self) -> None:
        """
        Install stream capture for stdout/stderr in Maya.

        This redirects stdout/stderr through custom writers that buffer
        output for retrieval via get_buffered_output().
        """
        await self._send_receive(self.INSTALL_STREAM_CAPTURE)
        logger.debug(f"Stream capture installed for {self.host}:{self.port}")

    async def uninstall_stream_capture(self) -> None:
        """
        Remove stream capture and restore original stdout/stderr in Maya.
        """
        await self._send_receive(self.UNINSTALL_STREAM_CAPTURE)
        logger.debug(f"Stream capture uninstalled for {self.host}:{self.port}")

    async def get_buffered_output(self, timeout: float | None = None) -> OutputBuffer:
        """
        Get buffered stdout/stderr content and clear the buffers.

        Returns:
            OutputBuffer with stdout and stderr fields containing captured output.
        """
        if timeout is None:
            response = await self._send_receive(self.GET_BUFFERED_OUTPUT)
        else:
            response = await self._send_receive(self.GET_BUFFERED_OUTPUT, timeout=timeout)
        result = response.result if response.result is not None else {}
        self._last_output_truncated = bool(result.get("truncated", False))
        return OutputBuffer(
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
        )

    async def session_info(self) -> SessionInfo:
        """
        Get information about the connected Maya session.

        Returns:
            SessionInfo with pid, user, maya version, current scene, etc.
        """
        response = await self._send_receive(self.GET_SESSION_INFO)
        info = response.result if response.result is not None else {}

        return SessionInfo(
            session_key=self.key,
            host=self.host,
            port=self.port,
            pid=info.get("pid", 0),
            user=info.get("user", ""),
            maya_version=info.get("maya_version", ""),
            scene_name=info.get("scene_name", ""),
            scene_path=info.get("scene_path", ""),
        )

    async def execute_code(
        self,
        code: str,
        result_type: ResultType = ResultType.NONE,
        timeout: float | None = None,
    ) -> CommandResponse:
        """
        Execute Python code in Maya and return the result.

        Args:
            code: Python code to execute
            result_type: How to handle the result
                - NONE: Execute statements, don't capture result
                - JSON: Evaluate expression, JSON encode result
                - RAW: Evaluate expression, return string representation

        Returns:
            CommandResponse with result and error info.
            Includes stdout/stderr for this execution. A timeout does not cancel
            code already running inside Maya and must never trigger replay.
        """
        effective_timeout = self._effective_timeout(timeout)
        self._active_executions += 1
        try:
            # Native execution and output retrieval must not interleave on this
            # client. Qt collects both inside a single Maya-side dispatch.
            async with self._execution_lock:
                deadline = asyncio.get_running_loop().time() + effective_timeout
                response = await self._send_receive(
                    self.EXECUTE_TEMPLATE,
                    {"code": code, "result_type": result_type.value},
                    raise_on_error=False,
                    timeout=timeout,
                )
                if not self.ATOMIC_EXECUTION:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        self._discard_connection(recoverable=True)
                        raise MayaExecutionError(
                            "Timeout collecting Maya output; code may have run"
                        )
                    output = await self.get_buffered_output(timeout=remaining)
                    response.stdout = output.stdout
                    response.stderr = output.stderr
                    response.truncated = response.truncated or self._last_output_truncated

                if result_type == ResultType.JSON and isinstance(response.result, str):
                    try:
                        response.result = json.loads(response.result)
                    except json.JSONDecodeError:
                        pass
                self.append_output(response.stdout, response.stderr)
                return response
        finally:
            self._active_executions -= 1

    # Abstract methods that must be implemented by subclasses

    @abstractmethod
    async def _send_receive(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        raise_on_error: bool = True,
        timeout: float | None = None,
    ) -> CommandResponse:
        """
        Send command to Maya and receive response.

        Args:
            method: Command string or method name (may contain {param} placeholders)
            params: Parameters to format into the command or pass to the method
            raise_on_error: Raise on remote errors when True; otherwise return the full response.

        Returns:
            CommandResponse with result and error attributes

        Raises:
            MayaConnectionError: If not connected
            MayaExecutionError: If communication fails or raise_on_error is True and command errors
        """
        ...

    @abstractmethod
    async def write_module(self, name: str, code: str, overwrite: bool = False) -> str:
        """Create a virtual Python module in the Maya session."""
        ...

    @abstractmethod
    async def ping(self) -> bool:
        """Check if the Maya session is still responsive."""
        ...


class MayaClient(BaseMayaClient):
    """Async client for communicating with a Maya command port."""

    # Access the helper via __import__ (works in single expressions)
    _MCP_HELPER = "__import__('maya_mcp')"

    # Command templates that use __import__ to access the persistent _mcp module
    GET_SESSION_INFO = f"{_MCP_HELPER}.get_session_info()"
    EXECUTE_TEMPLATE = f"{_MCP_HELPER}.execute({{code!r}}, {{result_type!r}})"
    CREATE_MODULE_TEMPLATE = f"{_MCP_HELPER}.create_module({{name!r}}, {{code!r}}, {{overwrite!r}})"
    INSTALL_STREAM_CAPTURE = f"{_MCP_HELPER}.install_stream_capture()"
    UNINSTALL_STREAM_CAPTURE = f"{_MCP_HELPER}.uninstall_stream_capture()"
    GET_BUFFERED_OUTPUT = f"{_MCP_HELPER}.get_buffered_output()"
    START_COMMAND_PORT = f"{_MCP_HELPER}.start_command_port({{port!r}})"

    # Qt server command templates
    START_QT_SERVER = f"{_MCP_HELPER}.start_qt_server({{port!r}})"
    STOP_QT_SERVER = f"{_MCP_HELPER}.stop_qt_server()"
    GET_QT_SERVER_PORT = f"{_MCP_HELPER}.get_qt_server_port()"

    # Check if bootstrap has been done
    CHECK_BOOTSTRAP = "'maya_mcp' in __import__('sys').modules"

    def __post_init__(self) -> None:
        super().__post_init__()
        self._port_type: PortType = PortType.UNKNOWN

    async def _send_receive(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        raise_on_error: bool = True,
        timeout: float | None = None,
    ) -> CommandResponse:
        """
        Send command to Maya commandPort and receive response.

        Args:
            method: Command template string (may contain {param} placeholders)
            params: Parameters to format into the template
            raise_on_error: Raise on remote errors when True; otherwise return the full response.

        Returns:
            CommandResponse with result and error attributes

        Raises:
            MayaConnectionError: If not connected
            MayaExecutionError: If communication fails or raise_on_error is True and command errors
        """
        effective_timeout = self._effective_timeout(timeout)

        # Format the command template with params
        if params:
            command = method.format(**params)
        else:
            command = method

        async with self._lock:
            if not self._writer or not self._reader:
                raise MayaConnectionError("Not connected to Maya")
            reader, writer = self._reader, self._writer

            async def exchange() -> bytes:
                # commandPort is line oriented; terminate every request.
                writer.write(command.encode("utf-8").rstrip(b"\n") + b"\n")
                await writer.drain()
                while True:
                    separator = self._native_buffer.find(b"\x00")
                    if separator >= 0:
                        frame = bytes(self._native_buffer[:separator])
                        del self._native_buffer[: separator + 1]
                        if len(frame) > MAX_RESPONSE_BYTES:
                            raise MayaExecutionError("Maya response exceeds 16 MiB limit")
                        # Ignore an initial/extra empty acknowledgement, keeping
                        # every complete nonempty frame separate.
                        if frame.strip():
                            return frame
                        continue
                    if len(self._native_buffer) > MAX_RESPONSE_BYTES:
                        raise MayaExecutionError("Maya response exceeds 16 MiB limit")
                    chunk = await reader.read(self.buffer_size)
                    if not chunk:
                        raise MayaConnectionError(
                            "Connection closed before Maya response terminator"
                        )
                    self._native_buffer.extend(chunk)

            try:
                # One timeout budget covers drain and every response chunk.
                response_bytes = await asyncio.wait_for(exchange(), timeout=effective_timeout)
                response_str = response_bytes.decode("utf-8").rstrip("\x00").rstrip("\n")
            except asyncio.CancelledError:
                self._discard_connection(recoverable=True)
                raise
            except asyncio.TimeoutError as e:
                self._discard_connection(recoverable=True)
                raise MayaExecutionError(
                    "Timeout waiting for Maya response; code may still be running"
                ) from e
            except Exception as e:
                self._discard_connection()
                if isinstance(e, (MayaConnectionError, MayaExecutionError)):
                    raise
                raise MayaExecutionError(f"Error communicating with Maya: {e}") from e

            # The complete frame is consumed before surfacing execution errors;
            # those errors do not poison an otherwise healthy connection.
            try:
                response = json.loads(response_str)
            except json.JSONDecodeError:
                return CommandResponse(result=response_str, error=None)

            # Some commandPort versions quote helper JSON one additional time.
            # Only unwrap a nested object, preserving ordinary RAW strings.
            if isinstance(response, str):
                try:
                    nested = json.loads(response)
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(nested, dict):
                        response = nested

            if isinstance(response, dict) and ("result" in response or "error" in response):
                if raise_on_error and response.get("error"):
                    error = response["error"]
                    message = (
                        error.get("message", "Unknown error") if isinstance(error, dict) else error
                    )
                    raise MayaExecutionError(str(message))
                return CommandResponse(
                    result=response.get("result"),
                    error=response.get("error"),
                    stdout=response.get("stdout", ""),
                    stderr=response.get("stderr", ""),
                    truncated=bool(response.get("truncated", False)),
                )
            return CommandResponse(result=response, error=None)

    async def _detect_port_type(self) -> PortType:
        """
        Detect whether the command port speaks MEL or Python.

        Returns:
            PortType indicating the port's language
        """
        if self._port_type != PortType.UNKNOWN:
            return self._port_type

        try:
            # Try a simple Python expression
            response = await self._send_receive("1+1")

            # If we get "2" back, it's Python
            result_str = str(response.result if response.result is not None else "").strip()
            if result_str == "2":
                self._port_type = PortType.PYTHON
            else:
                # Likely MEL - would return an error or different format
                self._port_type = PortType.MEL

        except Exception:
            self._port_type = PortType.UNKNOWN

        logger.info(f"Detected port type: {self._port_type.value}")
        return self._port_type

    async def _bootstrap(self, overwrite: bool = False) -> None:
        """
        Bootstrap the Maya session with helper functions.

        This creates the _mcp module in Maya's sys.modules,
        providing utilities for code execution with capture.
        """
        port_type = await self._detect_port_type()

        if port_type != PortType.PYTHON:
            # TODO: support bootstrapping from a MEL command port
            raise MayaConnectionError(
                f"Cannot bootstrap non-Python port (detected: {port_type.value})"
            )

        # Native commandPort treats a short TCP recv as a complete command.
        # Transfer compressed ASCII in small acknowledged requests, avoiding a
        # multi-kilobyte repr(source) and any shared filesystem assumptions.
        transfer_name = "__maya_mcp_transfer_" + uuid.uuid4().hex
        transfer = f"__import__('sys').modules[{transfer_name!r}]"
        stage = "initialize source transfer"
        installed = False

        async def send(command: str) -> CommandResponse:
            if not command.isascii() or len(command.encode("ascii")) + 1 > 1024:
                raise MayaConnectionError("Bootstrap transfer command exceeds 1024 ASCII bytes")
            return await self._send_receive(command)

        def require(response: CommandResponse, expected: Any) -> None:
            if response.error or response.result != expected:
                raise MayaConnectionError(
                    f"Unexpected acknowledgement: result={response.result!r}, "
                    f"error={response.error!r}"
                )

        sources = {"bootstrap": get_bootstrap_code(), "helper": get_helper_module_code()}
        decoded = {}
        try:
            initialize = (
                f"(__import__('sys').modules.__setitem__({transfer_name!r}, "
                f"__import__('types').ModuleType({transfer_name!r})), "
                f"{transfer}.__dict__.update(bootstrap=[], helper=[]), '__MCP_TRANSFER_READY__')[2]"
            )
            require(await send(initialize), "__MCP_TRANSFER_READY__")

            for label, source in sources.items():
                encoded = base64.b64encode(zlib.compress(source.encode("utf-8"))).decode("ascii")
                chunks = [encoded[index : index + 512] for index in range(0, len(encoded), 512)]
                target = f"{transfer}.{label}"
                for count, chunk in enumerate(chunks, start=1):
                    stage = f"transfer {label} chunk {count}/{len(chunks)}"
                    command = f"({target}.append({chunk!r}), len({target}))[1]"
                    require(await send(command), count)

                decoded[label] = (
                    f"__import__('zlib').decompress(__import__('base64').b64decode(''.join({target})))"
                )
                stage = f"verify {label} checksum"
                checksum_command = (
                    f"'SHA256:' + __import__('hashlib').sha256({decoded[label]}).hexdigest()"
                )
                digest = "SHA256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()
                require(await send(checksum_command), digest)

            # Refresh the global creator even when a legacy helper is present;
            # its embedded creator predates hash reuse and teardown.
            stage = "inject bootstrap creator"
            bootstrap_command = (
                f"exec({decoded['bootstrap']}.decode('utf-8'), globals()) "
                "or '__MCP_BOOTSTRAP_READY__'"
            )
            require(await send(bootstrap_command), "__MCP_BOOTSTRAP_READY__")
            stage = "install Maya helper"
            result = await send(
                f"create_module('maya_mcp', {decoded['helper']}.decode('utf-8'), True)"
            )
            if not isinstance(result.result, dict) or result.result.get("success") is not True:
                raise MayaConnectionError(f"Helper installation did not succeed: {result!r}")
            if result.result.get("warning"):
                logger.warning("Maya helper upgrade warning: %s", result.result["warning"])
            installed = True
            logger.info(
                "Maya session bootstrapped (helper reused=%s)", result.result.get("reused", False)
            )
        except Exception as exc:
            raise MayaConnectionError(f"Maya bootstrap failed during {stage}: {exc}") from exc
        finally:
            cleanup = (
                f"(__import__('sys').modules.pop({transfer_name!r}, None), "
                "'__MCP_TRANSFER_CLEARED__')[1]"
            )
            try:
                require(await send(cleanup), "__MCP_TRANSFER_CLEARED__")
            except Exception as exc:
                if installed:
                    raise MayaConnectionError(
                        f"Maya bootstrap transfer cleanup failed: {exc}"
                    ) from exc
                logger.warning("Could not clear Maya source transfer %s: %s", transfer_name, exc)

    async def bootstrap(self, client_type: Literal["native", "qt"] = "native") -> BaseMayaClient:
        """
        Boostrap remote session and return a new client
        """
        await self._bootstrap()

        # Create a dedicated communication port for this client
        new_port = random.randint(COMMUNICATION_PORT_MIN, COMMUNICATION_PORT_MAX)
        new_client: BaseMayaClient

        if client_type == "native":
            logger.info(f"Creating dedicated commandPort on port {new_port}")
            result = await self._send_receive(self.START_COMMAND_PORT, {"port": new_port})
            logger.info(f"Dedicated commandPort created: {result}")

            # Wait a moment for the port to start listening
            await asyncio.sleep(0.5)

            logger.info(f"Connecting to dedicated port {new_port}...")
            new_client = MayaClient(
                host=self.host, port=new_port, timeout=self.timeout, buffer_size=self.buffer_size
            )
        elif client_type == "qt":
            # Unlike the commandPort, the qt server can handle multiple clients on the same port.
            # Therefore, the port returned by _send_receive might be different than requested.
            logger.info(f"Starting Qt server on port {new_port}")
            result = await self._send_receive(self.START_QT_SERVER, {"port": new_port})
            logger.info("Qt server start response: %r (requested port %s)", result, new_port)
            # Never guess the requested port if startup did not acknowledge it.
            if not isinstance(result.result, dict) or "port" not in result.result:
                raise MayaConnectionError(f"Invalid Qt server start response: {result!r}")
            new_port = result.result["port"]
            if not isinstance(new_port, int) or not 1 <= new_port <= 65535:
                raise MayaConnectionError(f"Invalid Qt server port: {new_port!r}")
            logger.info(f"Qt server started on port {new_port}")

            # Wait a moment for the port to start listening
            await asyncio.sleep(0.5)

            logger.info(f"Connecting to dedicated port {new_port}...")
            new_client = MayaQtClient(
                host=self.host, port=new_port, timeout=self.timeout, buffer_size=self.buffer_size
            )
        else:
            raise TypeError(client_type)
        # Connect to the new dedicated port
        await new_client.connect()
        logger.info(f"Connected to dedicated port {new_port}")
        return new_client

    async def bootstrap_new(self) -> MayaQtClient:
        """
        Bootstrap the Maya session with helper functions.

        This creates the _mcp module in Maya's sys.modules,
        providing utilities for code execution with capture.
        Also starts the Qt command server and switches to it.
        """
        # Skip port detection - assume Python port and send commands directly
        # Maya command ports can be finicky with responses, so we'll just send
        # all bootstrap code and then connect to the Qt server it creates

        logger.info("Bootstrapping Maya session (sending code without waiting for responses)...")

        if not self._writer:
            raise MayaConnectionError("Not connected to Maya")

        # Phase 1: Execute bootstrap code to create create_module function
        bootstrap_code = get_bootstrap_code()
        bootstrap_cmd = f"exec({bootstrap_code!r}, globals())"
        self._writer.write(bootstrap_cmd.encode("utf-8") + b"\n")
        await self._writer.drain()
        await asyncio.sleep(0.1)  # Give Maya time to execute

        # Phase 2: Create the helper module
        helper_code = get_helper_module_code()
        cmd = self.CREATE_MODULE_TEMPLATE.format(name="maya_mcp", code=helper_code, overwrite=False)
        cmd = cmd.split(".", 1)[-1]  # Remove module prefix
        self._writer.write(cmd.encode("utf-8") + b"\n")
        await self._writer.drain()
        await asyncio.sleep(0.1)

        # Phase 3: Start Qt server and detect the new port
        # Get current ports before starting server
        from maya_mcp_server.utils import get_maya_listening_ports

        ports_before = {p["port"] for p in get_maya_listening_ports()}
        logger.debug(f"Ports before Qt server: {ports_before}")

        # Start the Qt server
        start_cmd = f"{self.START_QT_SERVER}"
        self._writer.write(start_cmd.encode("utf-8") + b"\n")
        await self._writer.drain()

        # Wait for new port to appear
        logger.info("Waiting for Qt server to start...")
        try:
            qt_port = None
            for attempt in range(30):  # Try for 3 seconds (30 * 0.1s)
                await asyncio.sleep(0.1)

                # Check for new ports
                ports_after = {p["port"] for p in get_maya_listening_ports()}
                new_ports = ports_after - ports_before

                if new_ports:
                    # Found a new port - this should be our Qt server
                    qt_port = new_ports.pop()
                    logger.info(f"Detected Qt server on port {qt_port}")
                    break

            if not qt_port:
                raise MayaConnectionError(
                    "Failed to detect Qt server port - no new ports opened by Maya"
                )

            # Connect to Qt server with retry logic
            qt_client = MayaQtClient(host=self.host, port=qt_port, timeout=self.timeout)
            await qt_client.connect()

            # Disconnect from commandPort (free it for others)
            if self._writer:
                old_port = self.port
                self._writer.close()
                try:
                    await self._writer.wait_closed()
                except Exception:
                    pass
                logger.info(
                    f"Disconnected from commandPort {old_port}, "
                    f"now using Qt server on port {qt_port}"
                )

        except Exception as e:
            raise MayaConnectionError(f"Qt server bootstrap failed: {e}") from e

        logger.info("Maya session bootstrapped with Qt server")
        return qt_client

    async def write_module(
        self,
        name: str,
        code: str,
        overwrite: bool = False,
    ) -> str:
        """
        Create a virtual Python module in the Maya session.

        Args:
            name: Module name (can be dotted path like 'mypackage.mymodule')
            code: Python source code for the module
            overwrite: If True, replace existing module; else raise error

        Returns:
            Success message

        Raises:
            MayaExecutionError: If module creation fails
        """
        response = await self._send_receive(
            self.CREATE_MODULE_TEMPLATE, {"name": name, "code": code, "overwrite": overwrite}
        )

        result = response.result if response.result is not None else {}
        if isinstance(result, dict):
            return str(result.get("message", f"Module '{name}' created"))
        return f"Module '{name}' created"

    async def ping(self) -> bool:
        """
        Check if the Maya connection is alive.

        Returns:
            True if Maya responds, False otherwise
        """
        if self.is_busy:
            return True
        try:
            response = await self._send_receive("1+1")
            return str(response.result if response.result is not None else "").strip() == "2"
        except Exception:
            return False


class MayaQtClient(BaseMayaClient):
    """Async client for communicating with our custom Maya Qt Command Server."""

    GET_SESSION_INFO = "get_session_info"
    EXECUTE_TEMPLATE = "execute_and_get_buffered"
    ATOMIC_EXECUTION = True
    CREATE_MODULE_TEMPLATE = "create_module"
    INSTALL_STREAM_CAPTURE = "install_stream_capture"
    UNINSTALL_STREAM_CAPTURE = "uninstall_stream_capture"
    GET_BUFFERED_OUTPUT = "get_buffered_output"

    def __post_init__(self) -> None:
        super().__post_init__()
        self._request_counter: int = 0

    async def connect(self) -> None:
        """Connect to the Qt server with retry logic."""
        async with self._connection_lock:
            if self.is_connected:
                self._awaiting_reconnect = False
                return
            max_retries = 3
            retry_delay = 0.5
            for attempt in range(max_retries):
                try:
                    self._reader, self._writer = await asyncio.wait_for(
                        asyncio.open_connection(self.host, self.port, limit=MAX_RESPONSE_BYTES),
                        timeout=self.timeout,
                    )
                    self._awaiting_reconnect = False
                    logger.info(f"Connected to Qt server at {self.host}:{self.port}")
                    return
                except Exception as e:
                    if attempt < max_retries - 1:
                        logger.warning(
                            "Failed to connect to Qt server (attempt %s/%s): %s",
                            attempt + 1,
                            max_retries,
                            e,
                        )
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        if isinstance(e, OSError):
                            self._awaiting_reconnect = False
                        raise MayaConnectionError(
                            f"Failed to connect to Qt server after {max_retries} attempts: {e}"
                        ) from e

    async def write_module(
        self,
        name: str,
        code: str,
        overwrite: bool = False,
    ) -> str:
        """
        Create a virtual Python module in the Maya session.

        Args:
            name: Module name (can be dotted path like 'mypackage.mymodule')
            code: Python source code for the module
            overwrite: If True, replace existing module; else raise error

        Returns:
            Success message

        Raises:
            MayaExecutionError: If module creation fails
        """
        response = await self._send_receive(
            self.CREATE_MODULE_TEMPLATE, {"name": name, "code": code, "overwrite": overwrite}
        )
        result = response.result if response.result is not None else {}
        if isinstance(result, dict):
            return str(result.get("message", f"Module '{name}' created"))
        return f"Module '{name}' created"

    async def _send_receive(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        raise_on_error: bool = True,
        timeout: float | None = None,
    ) -> CommandResponse:
        """Send JSON request to Qt server and receive response.

        Args:
            method: Method name to call
            params: Parameters for the method
            raise_on_error: Raise on remote errors when True; otherwise return the full response.

        Returns:
            CommandResponse with result and error attributes
        """
        effective_timeout = self._effective_timeout(timeout)

        async with self._lock:
            if not self._writer or not self._reader:
                raise MayaConnectionError("Not connected to Qt server")
            reader, writer = self._reader, self._writer
            # Generate request ID
            self._request_counter += 1
            request_id = f"req-{self._request_counter}"

            # Build request
            request = {"id": request_id, "method": method, "params": params or {}}

            async def exchange() -> bytes:
                request_line = json.dumps(request) + "\n"
                writer.write(request_line.encode("utf-8"))
                await writer.drain()
                return await reader.readline()

            try:
                response_line = await asyncio.wait_for(exchange(), timeout=effective_timeout)
                if not response_line or not response_line.endswith(b"\n"):
                    raise MayaConnectionError("Connection closed by Qt server")
                if len(response_line) > MAX_RESPONSE_BYTES:
                    raise MayaExecutionError("Qt response exceeds 16 MiB limit")

                response = json.loads(response_line.decode("utf-8"))
                if not isinstance(response, dict):
                    raise MayaExecutionError("Qt server response must be a JSON object")
                if method == self.EXECUTE_TEMPLATE and not response.get("error"):
                    if not all(
                        key in response for key in ("result", "stdout", "stderr", "truncated")
                    ):
                        raise MayaExecutionError("Invalid atomic execution response from Maya")
                    if not isinstance(response["stdout"], str) or not isinstance(
                        response["stderr"], str
                    ):
                        raise MayaExecutionError("Maya execution output must be text")

                # Validate response ID
                if response.get("id") != request_id:
                    raise MayaExecutionError(
                        f"Response ID mismatch: expected {request_id}, got {response.get('id')}"
                    )

            except asyncio.CancelledError:
                self._discard_connection(recoverable=True)
                raise
            except asyncio.TimeoutError as e:
                self._discard_connection(recoverable=True)
                raise MayaExecutionError(
                    "Timeout waiting for Qt server response; code may still be running"
                ) from e
            except json.JSONDecodeError as e:
                self._discard_connection()
                raise MayaExecutionError(f"Invalid JSON response from Qt server: {e}") from e
            except (ValueError, asyncio.LimitOverrunError) as e:
                self._discard_connection()
                raise MayaExecutionError("Qt response exceeds 16 MiB limit") from e
            except Exception as e:
                self._discard_connection()
                if not isinstance(e, (MayaConnectionError, MayaExecutionError)):
                    raise MayaExecutionError(f"Error communicating with Qt server: {e}") from e
                raise

            if raise_on_error and response.get("error"):
                error = response["error"]
                message = (
                    error.get("message", "Unknown error") if isinstance(error, dict) else error
                )
                raise MayaExecutionError(str(message))
            return CommandResponse(
                result=response.get("result"),
                error=response.get("error"),
                stdout=response.get("stdout", ""),
                stderr=response.get("stderr", ""),
                truncated=bool(response.get("truncated", False)),
            )

    async def ping(self) -> bool:
        """
        Check if the Maya connection is alive.

        Returns:
            True if Maya responds, False otherwise
        """
        if self.is_busy:
            return True
        try:
            response = await self._send_receive("ping")
            return bool(response.result == "pong")
        except Exception:
            return False


if __name__ == "__main__":
    cmd = """
import maya.cmds
maya.cmds.ls(cameras=True)
"""

    port = 7001

    # Native
    import socket

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(("127.0.0.1", port))

    client.send(cmd.encode("utf-8"))

    result = data = client.recv(1024)
    while len(data) == 1024:
        data = client.recv(1024)
        result += data
    client.close()

    output: str | None
    if result:
        output = result.decode("utf-8")
    else:
        output = None
    if output:
        print(output.strip())

    # Ours

    async def run() -> None:
        client = MayaClient(port=port)
        await client.connect()
        result = await client._send_receive(cmd)
        print(result)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass

    async def run2() -> None:
        client = MayaClient(port=port)
        await client.connect()
        await client._bootstrap(overwrite=True)
        result = await client.execute_code(cmd)
        print(result)

    try:
        asyncio.run(run2())
    except KeyboardInterrupt:
        pass
