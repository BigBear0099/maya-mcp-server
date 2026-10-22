"""Wire-level regressions for timeouts, framing, output, and session liveness."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import types
from unittest.mock import AsyncMock

import pytest

from maya_mcp_server.client import MayaClient, MayaConnectionError, MayaExecutionError, MayaQtClient
from maya_mcp_server.session_manager import SessionManager
from maya_mcp_server.types import CommandResponse, PortType, ResultType


@pytest.fixture
async def tcp_server():
    servers = []
    tasks = set()

    async def start(handler):
        async def tracked(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                await handler(reader, writer)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
                tasks.discard(task)

        server = await asyncio.start_server(tracked, "127.0.0.1", 0)
        servers.append(server)
        return server.sockets[0].getsockname()[1]

    yield start

    for server in servers:
        server.close()
    for task in list(tasks):
        task.cancel()
    await asyncio.gather(*list(tasks), return_exceptions=True)
    for server in servers:
        await server.wait_closed()


def atomic_reply(request, result=None, **extra):
    return (
        json.dumps(
            {
                "id": request["id"],
                "result": result,
                "error": None,
                "stdout": "",
                "stderr": "",
                "truncated": False,
                **extra,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


@pytest.mark.asyncio
async def test_atomic_output_and_error_are_returned_in_one_rpc():
    client = MayaQtClient()
    error = {"type": "builtins.ValueError", "message": "boom", "traceback": "Traceback..."}
    client._send_receive = AsyncMock(
        return_value=CommandResponse(
            result=None,
            error=error,
            stdout="before failure\n",
            stderr="warning\n",
            truncated=True,
        )
    )
    result = await client.execute_code("fail()", timeout=120.0)
    assert result.error == error
    assert result.stdout == "before failure\n"
    assert result.stderr == "warning\n"
    assert result.truncated is True
    assert client.get_accumulated_output().stdout == result.stdout
    client._send_receive.assert_awaited_once_with(
        "execute_and_get_buffered",
        {"code": "fail()", "result_type": "NONE"},
        raise_on_error=False,
        timeout=120.0,
    )
    assert client.is_busy is False


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
async def test_invalid_timeout_never_sends_a_command(timeout):
    client = MayaQtClient()
    client._send_receive = AsyncMock()
    with pytest.raises(ValueError, match="finite positive"):
        await client.execute_code("increment()", timeout=timeout)
    client._send_receive.assert_not_awaited()
    assert client.is_busy is False


@pytest.mark.asyncio
async def test_split_utf8_response_keeps_output_and_result(tcp_server):
    async def handler(reader, writer):
        request = json.loads(await reader.readline())
        response = atomic_reply(request, result='["中文"]', stdout="中文输出\n")
        marker = response.index("中".encode()) + 1
        writer.write(response[:marker])
        await writer.drain()
        await asyncio.sleep(0)
        writer.write(response[marker:])
        await writer.drain()

    client = MayaQtClient(port=await tcp_server(handler))
    await client.connect()
    result = await client.execute_code("query()", ResultType.JSON)
    assert result.result == ["中文"]
    assert result.stdout == "中文输出\n"
    await client.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("client_class", [MayaClient, MayaQtClient])
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_cancel_detaches_sent_stream(tcp_server, client_class, cancel):
    received = asyncio.Event()
    requests = []

    async def handler(reader, writer):
        requests.append(await reader.read(65536))
        received.set()
        await asyncio.Event().wait()

    client = client_class(port=await tcp_server(handler))
    await client.connect()
    operation = asyncio.create_task(
        client._send_receive(
            "increment()",
            timeout=1.0 if cancel else 0.02,
        )
    )
    await asyncio.wait_for(received.wait(), timeout=1.0)
    if cancel:
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        with pytest.raises(MayaExecutionError, match="code may still be running"):
            await operation
    assert client._writer is None
    assert client._reader is None
    with pytest.raises(MayaConnectionError):
        await client._send_receive("increment()")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_late_reply_cannot_corrupt_new_explicit_request(tcp_server):
    requests = []

    async def handler(reader, writer):
        request = json.loads(await reader.readline())
        requests.append(request["params"]["code"])
        if request["params"]["code"] == "increment()":
            await asyncio.sleep(0.1)
            writer.write(atomic_reply(request, result='"late"'))
        else:
            writer.write(atomic_reply(request, result='"fresh"'))
        await writer.drain()

    client = MayaQtClient(port=await tcp_server(handler))
    await client.connect()
    with pytest.raises(MayaExecutionError, match="Timeout"):
        await client.execute_code("increment()", timeout=0.02)
    await client.connect()
    result = await client.execute_code("query()", ResultType.JSON, timeout=1.0)
    assert result.result == "fresh"
    assert requests == ["increment()", "query()"]
    await client.disconnect()


@pytest.mark.asyncio
async def test_native_timeout_is_for_complete_response_not_each_chunk(tcp_server):
    async def handler(reader, writer):
        await reader.read(65536)
        for chunk in (b'"', b"a", b"b", b"c", b'"\n\x00'):
            writer.write(chunk)
            await writer.drain()
            await asyncio.sleep(0.02)

    client = MayaClient(port=await tcp_server(handler))
    await client.connect()
    with pytest.raises(MayaExecutionError, match="Timeout"):
        await client._send_receive("query()", timeout=0.03)
    assert client.is_connected is False


@pytest.mark.asyncio
@pytest.mark.parametrize("client_class", [MayaClient, MayaQtClient])
async def test_oversized_frame_disconnects_with_explicit_error(
    tcp_server, monkeypatch, client_class
):
    monkeypatch.setattr("maya_mcp_server.client.MAX_RESPONSE_BYTES", 128)

    async def handler(reader, writer):
        await reader.read(65536)
        writer.write(b"x" * 256 + (b"\n" if client_class is MayaQtClient else b"\n\x00"))
        await writer.drain()

    client = client_class(port=await tcp_server(handler))
    await client.connect()
    with pytest.raises(MayaExecutionError, match="limit"):
        await client._send_receive("query()")
    assert client.is_connected is False


@pytest.mark.asyncio
async def test_unknown_atomic_method_is_not_replayed(tcp_server):
    requests = []

    async def handler(reader, writer):
        request = json.loads(await reader.readline())
        requests.append(request)
        writer.write(
            json.dumps(
                {
                    "id": request["id"],
                    "result": None,
                    "error": {"message": "Unknown method: execute_and_get_buffered"},
                }
            ).encode("utf-8")
            + b"\n"
        )
        await writer.drain()

    client = MayaQtClient(port=await tcp_server(handler))
    await client.connect()
    result = await client.execute_code("increment()")
    assert "Unknown method" in result.error["message"]
    assert len(requests) == 1
    await client.disconnect()


@pytest.mark.asyncio
async def test_prune_does_not_ping_or_disconnect_active_execution(tcp_server):
    received = asyncio.Event()
    release = asyncio.Event()

    async def handler(reader, writer):
        request = json.loads(await reader.readline())
        received.set()
        await release.wait()
        writer.write(atomic_reply(request))
        await writer.drain()

    client = MayaQtClient(port=await tcp_server(handler))
    await client.connect()
    client.ping = AsyncMock(return_value=False)
    manager = SessionManager()
    manager._sessions[client.key] = client
    task = asyncio.create_task(client.execute_code("long_work()", timeout=1.0))
    await received.wait()
    await asyncio.wait_for(manager._prune_dead_sessions(), timeout=0.1)
    assert client.key in manager._sessions
    client.ping.assert_not_awaited()
    release.set()
    await task
    await client.disconnect()


@pytest.mark.asyncio
async def test_prune_rechecks_busy_after_ping_race():
    client = MayaQtClient()
    manager = SessionManager()
    manager._sessions[client.key] = client

    async def raced_ping():
        client._active_executions += 1
        return False

    client.ping = raced_ping
    await manager._prune_dead_sessions()
    assert client.key in manager._sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("client_class", [MayaClient, MayaQtClient])
async def test_ping_skips_locked_transport_without_waiting(client_class):
    client = client_class()
    client._send_receive = AsyncMock()
    async with client._lock:
        assert await asyncio.wait_for(client.ping(), timeout=0.1) is True
    client._send_receive.assert_not_awaited()


def test_accumulated_utf8_output_is_bounded_and_valid():
    client = MayaQtClient()
    client.append_output(stdout="中文🙂" * 100000)
    output = client.get_accumulated_output().stdout
    assert len(output.encode("utf-8")) <= 256 * 1024
    assert "truncated" in output
    assert "\ufffd" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("client_class", [MayaClient, MayaQtClient])
async def test_concurrent_connects_share_one_stream(tcp_server, client_class):
    accepted = asyncio.Event()
    connections = []

    async def handler(reader, writer):
        connections.append(writer)
        accepted.set()
        await reader.read()

    client = client_class(port=await tcp_server(handler))
    await asyncio.gather(client.connect(), client.connect())
    await accepted.wait()
    assert len(connections) == 1
    assert client.is_connected
    await client.disconnect()


@pytest.mark.asyncio
async def test_native_newline_and_empty_frames_preserve_qt_port_reply(tcp_server):
    commands = []

    async def handler(reader, writer):
        writer.write(b"\n\x00")
        await writer.drain()
        commands.append(await reader.readline())
        reply = json.dumps({"port": 54321, "already_running": False}).encode()
        writer.write(b"\n\x00" + reply + b"\n\x00")
        await writer.drain()

    client = MayaClient(port=await tcp_server(handler))
    await client.connect()
    response = await client._send_receive("start_qt_server(54321)", timeout=1.0)
    assert response.result == {"port": 54321, "already_running": False}
    assert commands == [b"start_qt_server(54321)\n"]
    await client.disconnect()


@pytest.mark.asyncio
async def test_native_keeps_nonempty_nul_frames_separate(tcp_server):
    async def handler(reader, writer):
        await reader.readline()
        writer.write(b"1\n\x002\n\x00")
        await writer.drain()
        await reader.readline()

    client = MayaClient(port=await tcp_server(handler))
    await client.connect()
    assert (await client._send_receive("first()", timeout=1.0)).result == 1
    assert (await client._send_receive("second()", timeout=1.0)).result == 2
    await client.disconnect()


@pytest.mark.asyncio
async def test_native_unwraps_extra_quoted_json_object(tcp_server):
    async def handler(reader, writer):
        await reader.readline()
        encoded = json.dumps(json.dumps({"port": 54321, "already_running": True}))
        writer.write(encoded.encode() + b"\n\x00")
        await writer.drain()

    client = MayaClient(port=await tcp_server(handler))
    await client.connect()
    response = await client._send_receive("start_qt_server(54321)", timeout=1.0)
    assert response.result == {"port": 54321, "already_running": True}
    await client.disconnect()


@pytest.mark.asyncio
async def test_bootstrap_refreshes_legacy_creator_then_reuses_same_helper(monkeypatch):
    legacy = types.ModuleType("maya_mcp")
    cleanup = []

    def old_creator(*args, **kwargs):
        raise AssertionError("The legacy module creator must never be called")

    legacy.create_module = old_creator
    legacy.stop_qt_server = lambda: cleanup.append("legacy_qt")
    legacy.uninstall_stream_capture = lambda: cleanup.append("legacy_stream")
    monkeypatch.setitem(sys.modules, "maya_mcp", legacy)
    maya_globals = {}
    commands = []
    client = MayaClient()
    client._detect_port_type = AsyncMock(return_value=PortType.PYTHON)

    async def send(method, params=None, raise_on_error=True, timeout=None):
        expression = method.format(**params) if params else method
        commands.append(expression)
        value = eval(expression, maya_globals)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        return CommandResponse(result=value, error=None)

    client._send_receive = send
    await client._bootstrap()
    installed = sys.modules["maya_mcp"]
    assert hasattr(installed, "_mcp_source_sha")
    assert cleanup == ["legacy_qt", "legacy_stream"]
    installed._mcp_teardown = lambda: cleanup.append("new_qt_closed")
    installed._execution_namespace["kept"] = 42
    await client._bootstrap()
    assert sys.modules["maya_mcp"] is installed
    assert installed._execution_namespace["kept"] == 42
    assert cleanup == ["legacy_qt", "legacy_stream"]
    assert all("__import__('maya_mcp').create_module" not in command for command in commands)
    assert any("__MCP_BOOTSTRAP_READY__" in command for command in commands)
    assert all(command.isascii() and len(command.encode()) + 1 <= 1024 for command in commands)
    assert not any(name.startswith("__maya_mcp_transfer_") for name in sys.modules)


@pytest.mark.asyncio
async def test_qt_bootstrap_uses_acknowledged_port(monkeypatch):
    client = MayaClient(timeout=60.0)
    client._bootstrap = AsyncMock()
    client._send_receive = AsyncMock(
        return_value=CommandResponse(
            result={"port": 54321, "already_running": True},
            error=None,
        )
    )
    monkeypatch.setattr(MayaQtClient, "connect", AsyncMock())
    qt_client = await client.bootstrap(client_type="qt")
    assert isinstance(qt_client, MayaQtClient)
    assert qt_client.port == 54321
    assert qt_client.timeout == 60.0


@pytest.mark.asyncio
async def test_qt_bootstrap_does_not_guess_port_after_bad_ack(monkeypatch):
    client = MayaClient()
    client._bootstrap = AsyncMock()
    client._send_receive = AsyncMock(return_value=CommandResponse(result="", error=None))
    connect = AsyncMock()
    monkeypatch.setattr(MayaQtClient, "connect", connect)
    with pytest.raises(MayaConnectionError, match="Invalid Qt server start response"):
        await client.bootstrap(client_type="qt")
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_chunk_ack_stops_without_replay_and_cleans_transfer():
    client = MayaClient()
    client._detect_port_type = AsyncMock(return_value=PortType.PYTHON)
    commands = []
    maya_globals = {}
    before = {name for name in sys.modules if name.startswith("__maya_mcp_transfer_")}

    async def send(command, **kwargs):
        commands.append(command)
        value = eval(command, maya_globals)
        if ".append(" in command:
            value = 0  # A duplicated, missing, or unacknowledged block is fatal.
        return CommandResponse(result=value, error=None)

    client._send_receive = send
    with pytest.raises(MayaConnectionError, match="transfer bootstrap chunk 1"):
        await client._bootstrap()
    assert sum(".append(" in command for command in commands) == 1
    assert "__MCP_TRANSFER_CLEARED__" in commands[-1]
    assert {name for name in sys.modules if name.startswith("__maya_mcp_transfer_")} == before


@pytest.mark.asyncio
async def test_helper_installation_error_is_not_reported_as_bootstrapped():
    client = MayaClient()
    client._detect_port_type = AsyncMock(return_value=PortType.PYTHON)
    commands = []
    maya_globals = {}
    before = {name for name in sys.modules if name.startswith("__maya_mcp_transfer_")}

    async def send(command, **kwargs):
        commands.append(command)
        if command.startswith("create_module("):
            return CommandResponse(
                result="unexpected character after line continuation character", error=None
            )
        value = eval(command, maya_globals)
        return CommandResponse(result=value, error=None)

    client._send_receive = send
    with pytest.raises(MayaConnectionError, match="install Maya helper.*unexpected character"):
        await client._bootstrap()
    assert sum(command.startswith("create_module(") for command in commands) == 1
    assert "__MCP_TRANSFER_CLEARED__" in commands[-1]
    assert {name for name in sys.modules if name.startswith("__maya_mcp_transfer_")} == before


@pytest.mark.asyncio
async def test_transfer_checksum_failure_prevents_source_execution():
    client = MayaClient()
    client._detect_port_type = AsyncMock(return_value=PortType.PYTHON)
    commands = []
    maya_globals = {}

    async def send(command, **kwargs):
        commands.append(command)
        value = eval(command, maya_globals)
        if command.startswith("'SHA256:'"):
            value = "SHA256:wrong"
        return CommandResponse(result=value, error=None)

    client._send_receive = send
    with pytest.raises(MayaConnectionError, match="verify bootstrap checksum"):
        await client._bootstrap()
    assert not any(command.startswith("exec(") for command in commands)
    assert "__MCP_TRANSFER_CLEARED__" in commands[-1]


@pytest.mark.asyncio
async def test_interleaved_bootstraps_use_separate_transfers_and_reuse_helper(monkeypatch):
    monkeypatch.setitem(sys.modules, "maya_mcp", types.ModuleType("maya_mcp"))
    before = {name for name in sys.modules if name.startswith("__maya_mcp_transfer_")}
    maya_globals = {}
    commands = []
    installations = []

    async def send(command, **kwargs):
        commands.append(command)
        value = eval(command, maya_globals)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        if command.startswith("create_module("):
            installations.append(value)
        await asyncio.sleep(0)
        return CommandResponse(result=value, error=None)

    clients = [MayaClient(), MayaClient()]
    for client in clients:
        client._detect_port_type = AsyncMock(return_value=PortType.PYTHON)
        client._send_receive = send
    await asyncio.gather(*(client._bootstrap() for client in clients))
    names = set(re.findall(r"__maya_mcp_transfer_[0-9a-f]+", "\n".join(commands)))
    assert len(names) == 2
    assert len(installations) == 2
    assert all(result["success"] is True for result in installations)
    assert installations[1]["reused"] is True
    assert all(command.isascii() and len(command.encode()) + 1 <= 1024 for command in commands)
    assert {name for name in sys.modules if name.startswith("__maya_mcp_transfer_")} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_fast_prune_retains_detached_session_and_listing_reconnects(tcp_server, cancel):
    received = asyncio.Event()
    never_reply = asyncio.Event()
    requests = []

    async def handler(reader, writer):
        while line := await reader.readline():
            request = json.loads(line)
            requests.append(request)
            method = request["method"]
            if method == "execute_and_get_buffered":
                if request["params"]["code"] == "slow_increment()":
                    received.set()
                    await never_reply.wait()
                reply = atomic_reply(request, result="42")
            elif method == "ping":
                reply = (
                    json.dumps({"id": request["id"], "result": "pong", "error": None}).encode()
                    + b"\n"
                )
            elif method == "get_session_info":
                reply = (
                    json.dumps(
                        {
                            "id": request["id"],
                            "error": None,
                            "result": {"pid": 123, "user": "tester", "maya_version": "2026"},
                        }
                    ).encode()
                    + b"\n"
                )
            else:
                reply = (
                    json.dumps(
                        {"id": request["id"], "result": {"success": True}, "error": None}
                    ).encode()
                    + b"\n"
                )
            writer.write(reply)
            await writer.drain()

    client = MayaQtClient(port=await tcp_server(handler))
    await client.connect()
    manager = SessionManager(scan_interval=0.001)
    manager._sessions[client.key] = client
    manager._config_to_session["127.0.0.1:7002"] = client.key
    manager._stream_capture_installed.add(client.key)

    async def scan():
        while True:
            await manager._prune_dead_sessions()
            await asyncio.sleep(0.001)

    scanner = asyncio.create_task(scan())
    try:
        operation = asyncio.create_task(
            client.execute_code(
                "slow_increment()",
                timeout=1.0 if cancel else 0.02,
            )
        )
        await asyncio.wait_for(received.wait(), timeout=1.0)
        if cancel:
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            with pytest.raises(MayaExecutionError, match="Timeout"):
                await operation
        await asyncio.sleep(0.03)  # Run many prune passes after execution is no longer busy.
        assert client.awaiting_reconnect is True
        assert client.is_connected is False
        assert manager._sessions[client.key] is client
        assert manager._config_to_session["127.0.0.1:7002"] == client.key

        sessions = await manager.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].pid == 123
        assert client.awaiting_reconnect is False
        assert (await client.execute_code("fresh_query()", ResultType.JSON)).result == 42
        assert (
            sum(request.get("params", {}).get("code") == "slow_increment()" for request in requests)
            == 1
        )
    finally:
        scanner.cancel()
        await asyncio.gather(scanner, return_exceptions=True)
        await client.disconnect()
        assert client.awaiting_reconnect is False


@pytest.mark.asyncio
async def test_actual_peer_eof_is_pruned_instead_of_marked_recoverable(tcp_server):
    async def handler(reader, writer):
        await reader.readline()
        # EOF is independently known; unlike a timeout it should not retain the session.

    client = MayaQtClient(port=await tcp_server(handler))
    await client.connect()
    manager = SessionManager()
    manager._sessions[client.key] = client
    with pytest.raises(MayaConnectionError, match="Connection closed"):
        await client._send_receive("ping", timeout=1.0)
    assert client.awaiting_reconnect is False
    await manager._prune_dead_sessions()
    assert client.key not in manager._sessions


@pytest.mark.asyncio
async def test_prune_rechecks_recovery_state_after_failed_ping():
    client = MayaQtClient()
    manager = SessionManager()
    manager._sessions[client.key] = client

    async def detach_during_ping():
        client._discard_connection(recoverable=True)
        return False

    client.ping = detach_during_ping
    await manager._prune_dead_sessions()
    assert client.key in manager._sessions
