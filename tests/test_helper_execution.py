"""Offline execution, output, reload and Qt lifecycle regression tests."""
# Qt test doubles intentionally expose the names used by PySide2 and PySide6.
# ruff: noqa: N802

import io
import json
import sys
import types

import pytest

from maya_mcp_server import maya_bootstrap
from maya_mcp_server import maya_mcp_helper as helper


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(helper, "_execution_namespace", {"__name__": "__mcp_exec__"})
    monkeypatch.setattr(helper, "_stdout_writer", None)
    monkeypatch.setattr(helper, "_stderr_writer", None)
    monkeypatch.setattr(helper, "_qt_server", None)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    yield helper
    helper.uninstall_stream_capture()


def test_none_runs_tail_calls_once_and_never_returns_the_value(isolated):
    response = json.loads(isolated.execute("calls = []\ncalls.append('once') or 42"))
    assert response == {"result": None, "error": None}
    assert isolated._execution_namespace["calls"] == ["once"]


def test_json_and_raw_have_explicit_wire_semantics(isolated):
    result = json.loads(isolated.execute("{'中文': [1, 2]}", "JSON"))
    assert json.loads(result["result"]) == {"中文": [1, 2]}
    assert result["error"] is None
    raw = json.loads(isolated.execute("{1, 2}", "RAW"))
    assert raw == {"result": "{1, 2}", "error": None}


@pytest.mark.parametrize(
    ("source", "mode", "error_type"),
    [
        ("missing_variable", "NONE", "NameError"),
        ("1 / 0", "JSON", "ZeroDivisionError"),
        ("def broken(:", "NONE", "SyntaxError"),
        ("{1, 2}", "JSON", "TypeError"),
        ("float('nan')", "JSON", "ValueError"),
        ("float('inf')", "JSON", "ValueError"),
        ("float('-inf')", "JSON", "ValueError"),
        ("{'nested': [float('nan')]}", "JSON", "ValueError"),
        ("x = 1", "JSON", "RuntimeError"),
        ("pass", "unknown", "ValueError"),
        ("raise SystemExit(2)", "NONE", "SystemExit"),
        ("raise KeyboardInterrupt()", "NONE", "KeyboardInterrupt"),
    ],
)
def test_all_execution_failures_have_serializable_tracebacks(isolated, source, mode, error_type):
    response = json.loads(isolated.execute(source, mode))
    assert response["result"] is None
    assert response["error"]["type"] == "builtins." + error_type
    assert isinstance(response["error"]["message"], str)
    assert "Traceback" in response["error"]["traceback"]


def test_user_variables_and_imported_virtual_modules_persist(isolated, monkeypatch):
    name = "_mcp_execution_test_module"
    monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    maya_bootstrap.create_module(name, "def answer():\n    return 42", overwrite=True)
    assert json.loads(isolated.execute("import " + name + "\nvalue = 3"))["error"] is None
    response = json.loads(isolated.execute(name + ".answer() + value", "JSON"))
    assert json.loads(response["result"]) == 45
    assert "prepare_code_for_result_capture" not in isolated._execution_namespace


def test_exception_with_broken_str_still_completes_with_an_error(isolated):
    response = json.loads(
        isolated.execute(
            "class Broken(Exception):\n"
            "    def __str__(self):\n        raise ValueError('bad message')\n"
            "raise Broken()"
        )
    )
    assert response["result"] is None
    assert response["error"]["type"] == "__mcp_exec__.Broken"
    assert "could not be converted" in response["error"]["message"]
    assert "Broken" in response["error"]["traceback"]


def test_atomic_output_is_for_this_call_including_errors(isolated):
    isolated.install_stream_capture()
    print("OLD INTERACTIVE OUTPUT")
    response = json.loads(
        isolated.execute_and_get_buffered(
            "import sys\nprint('中文输出')\nprint('warning', file=sys.stderr)\n1 / 0", "JSON"
        )
    )
    assert response["stdout"] == "中文输出\n"
    assert response["stderr"] == "warning\n"
    assert response["error"]["type"] == "builtins.ZeroDivisionError"
    assert response["result"] is None
    assert response["truncated"] is False
    assert json.loads(isolated.get_buffered_output()) == {
        "stdout": "",
        "stderr": "",
        "truncated": False,
    }


def test_utf8_output_cap_is_in_bytes_and_drain_resets_truncation():
    original = io.StringIO()
    writer = helper.StreamWriter("stdout", original, max_bytes=5)
    assert writer.write("中文") == 2
    assert writer.drain() == ("中", True)
    assert original.getvalue() == "中文"
    assert writer.write("abc") == 3
    assert writer.drain() == ("abc", False)


def test_atomic_call_reports_truncation_without_leaking_previous_flag(isolated):
    isolated.install_stream_capture()
    isolated._stdout_writer._max_bytes = 4
    isolated._stderr_writer._max_bytes = 4
    print("previous output too large")
    response = json.loads(isolated.execute_and_get_buffered("print('ok')"))
    assert response["stdout"] == "ok\n"
    assert response["truncated"] is False
    response = json.loads(isolated.execute_and_get_buffered("print('中文中文')"))
    assert response["stdout"] == "中"
    assert response["truncated"] is True


def test_install_and_teardown_are_idempotent_and_restore_original_streams(isolated):
    original_out, original_err = sys.stdout, sys.stderr
    isolated.install_stream_capture()
    first_writer = sys.stdout
    isolated.install_stream_capture()
    assert sys.stdout is first_writer
    isolated._execution_namespace["retained"] = 1
    isolated.teardown()
    isolated.teardown()
    assert sys.stdout is original_out
    assert sys.stderr is original_err
    assert isolated._execution_namespace["retained"] == 1


def test_uninstall_does_not_clobber_a_new_stream_owner(isolated, monkeypatch):
    isolated.install_stream_capture()
    replacement = io.StringIO()
    monkeypatch.setattr(sys, "stdout", replacement)
    isolated.uninstall_stream_capture()
    assert sys.stdout is replacement


def test_module_reload_cleans_old_resources_and_transfers_only_user_namespace(monkeypatch):
    name = "_mcp_reload_test_module"
    old = types.ModuleType(name)
    old._execution_namespace = {"user_value": 42}
    old.obsolete_helper = object()
    events = []
    old._mcp_teardown = lambda: events.append("cleanup")
    monkeypatch.setitem(sys.modules, name, old)
    result = json.loads(
        maya_bootstrap.create_module(
            name, "_execution_namespace = {}\nnew_helper = True", overwrite=True
        )
    )
    replacement = sys.modules[name]
    assert result["success"]
    assert events == ["cleanup"]
    assert replacement._execution_namespace is old._execution_namespace
    assert not hasattr(replacement, "obsolete_helper")
    assert replacement.new_helper


def test_invalid_replacement_keeps_old_module_and_does_not_teardown(monkeypatch):
    name = "_mcp_reload_test_module"
    old = types.ModuleType(name)
    events = []
    old._mcp_teardown = lambda: events.append("cleanup")
    monkeypatch.setitem(sys.modules, name, old)
    with pytest.raises(SyntaxError):
        maya_bootstrap.create_module(name, "def broken(:", overwrite=True)
    assert sys.modules[name] is old
    assert events == []


def test_same_source_reuses_module_without_disconnect_or_reexecuting(monkeypatch):
    name = "_mcp_reload_test_module"
    monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    source = "_execution_namespace = {}\nload_count = 1"
    maya_bootstrap.create_module(name, source, overwrite=True)
    original = sys.modules[name]
    events = []
    original._mcp_teardown = lambda: events.append("cleanup")
    original.load_count = 2
    original._execution_namespace["user_value"] = 42
    result = json.loads(maya_bootstrap.create_module(name, source, overwrite=True))
    assert result["reused"] is True
    assert sys.modules[name] is original
    assert original.load_count == 2
    assert original._execution_namespace == {"user_value": 42}
    assert "_mcp_source_sha" not in original._execution_namespace
    assert events == []


def test_changed_source_tears_down_same_version_module_once(monkeypatch):
    name = "_mcp_reload_test_module"
    monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    maya_bootstrap.create_module(name, "_execution_namespace = {}\nrevision = 1", overwrite=True)
    original = sys.modules[name]
    events = []
    original._mcp_teardown = lambda: events.append("cleanup")
    result = json.loads(
        maya_bootstrap.create_module(
            name, "_execution_namespace = {}\nrevision = 2", overwrite=True
        )
    )
    assert result["success"]
    assert events == ["cleanup"]
    assert sys.modules[name].revision == 2
    assert sys.modules[name]._execution_namespace is original._execution_namespace


def test_legacy_helper_upgrade_stops_qt_and_unwraps_streams_without_copying_globals(monkeypatch):
    old = types.ModuleType("maya_mcp")
    events = []
    old.stop_qt_server = lambda: events.append("stop")
    old.uninstall_stream_capture = lambda: events.append("uninstall")
    old._qt_server = object()
    old._stdout_writer = object()
    old.user_value = 42
    monkeypatch.setitem(sys.modules, "maya_mcp", old)
    result = json.loads(
        maya_bootstrap.create_module(
            "maya_mcp", "_execution_namespace = {}\nrevision = 1", overwrite=True
        )
    )
    assert result["success"]
    assert events == ["stop", "uninstall"]
    assert sys.modules["maya_mcp"]._execution_namespace == {}
    assert not hasattr(sys.modules["maya_mcp"], "_qt_server")
    assert not hasattr(sys.modules["maya_mcp"], "user_value")


def test_legacy_stream_cleanup_still_runs_if_socket_cleanup_fails(monkeypatch):
    old = types.ModuleType("maya_mcp")
    events = []

    def fail_stop():
        events.append("stop")
        raise RuntimeError("socket already deleted")

    old.stop_qt_server = fail_stop
    old.uninstall_stream_capture = lambda: events.append("uninstall")
    monkeypatch.setitem(sys.modules, "maya_mcp", old)
    result = json.loads(maya_bootstrap.create_module("maya_mcp", "revision = 1", overwrite=True))
    assert result["success"]
    assert "socket already deleted" in result["warning"]
    assert events == ["stop", "uninstall"]


def test_virtual_user_module_does_not_get_legacy_helper_cleanup(monkeypatch):
    name = "_mcp_reload_test_module"
    old = types.ModuleType(name)
    events = []
    old.stop_qt_server = lambda: events.append("unrelated user function")
    monkeypatch.setitem(sys.modules, name, old)
    maya_bootstrap.create_module(name, "revision = 1", overwrite=True)
    assert events == []


class FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot=None):
        if slot is None:
            self.slots.clear()
            return
        if slot not in self.slots:
            raise RuntimeError("signal already disconnected")
        self.slots.remove(slot)


class FakeSocket:
    ConnectedState = 3

    def __init__(self, data=b"", deleted=False):
        self.data = data
        self.deleted = deleted
        self.sent = []
        self.closed = []
        self.readyRead = FakeSignal()
        self.disconnected = FakeSignal()

    def isValid(self):
        if self.deleted:
            raise RuntimeError("Internal C++ object already deleted")
        return True

    def state(self):
        return self.ConnectedState

    def bytesAvailable(self):
        return len(self.data)

    def readAll(self):
        data, self.data = self.data, b""
        return data

    def write(self, data):
        self.sent.append(data)

    def flush(self):
        pass

    def disconnectFromHost(self):
        self.closed.append("disconnect")

    def close(self):
        self.closed.append("close")

    def deleteLater(self):
        self.closed.append("delete")


class FakeTimer:
    def __init__(self):
        self.timeout = FakeSignal()
        self.starts = []
        self.stopped = False

    def start(self, milliseconds):
        self.starts.append(milliseconds)

    def stop(self):
        self.stopped = True

    def deleteLater(self):
        pass


class FakeTcpServer:
    def __init__(self):
        self.newConnection = FakeSignal()
        self.closed = False

    def listen(self, address, port):
        return True

    def serverPort(self):
        return 51000

    def close(self):
        self.closed = True

    def deleteLater(self):
        pass


@pytest.fixture
def qt_server(monkeypatch, isolated):
    monkeypatch.setattr(helper, "QTcpServer", FakeTcpServer)
    monkeypatch.setattr(helper, "QTcpSocket", FakeSocket)
    monkeypatch.setattr(helper, "QTimer", FakeTimer)
    monkeypatch.setattr(helper, "QHostAddress", types.SimpleNamespace(LocalHost=0))
    server = helper.QtCommandServer()
    server.start()
    yield server
    server.stop()


def add_client(server, socket, client_id=1):
    server._clients[client_id] = {"socket": socket, "input_buffer": b"", "output_queue": []}


def test_tcp_split_inside_utf8_character_is_decoded_after_full_line(qt_server):
    wire = (
        json.dumps(
            {
                "id": "unicode",
                "method": "execute_and_get_buffered",
                "params": {"code": "print('中文')", "result_type": "NONE"},
            },
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    split = wire.index("中".encode()) + 1
    socket = FakeSocket(wire[:split])
    add_client(qt_server, socket)
    qt_server._process_messages()
    assert not socket.sent
    socket.data = wire[split:]
    qt_server._process_messages()
    response = json.loads(socket.sent[0])
    assert response["id"] == "unicode"
    assert response["stdout"] == "中文\n"
    assert response["result"] is None
    assert response["error"] is None


def test_process_events_reentry_cannot_interleave_requests(qt_server, monkeypatch):
    events = []

    def dispatch(request):
        events.append((request["id"], "start"))
        if request["id"] == 1:
            qt_server._process_messages()
        events.append((request["id"], "end"))
        return {"id": request["id"], "result": "done", "error": None}

    monkeypatch.setattr(qt_server, "_dispatch_request", dispatch)
    for client_id in (1, 2):
        socket = FakeSocket((json.dumps({"id": client_id}) + "\n").encode())
        add_client(qt_server, socket, client_id)
    qt_server._process_messages()
    assert events == [(1, "start"), (1, "end"), (2, "start"), (2, "end")]
    assert not qt_server._processing


def test_deleted_cpp_socket_does_not_abort_other_clients(qt_server):
    deleted = FakeSocket(deleted=True)
    good = FakeSocket(b'{"id":2,"method":"ping"}\n')
    add_client(qt_server, deleted, 1)
    add_client(qt_server, good, 2)
    qt_server._process_messages()
    assert 1 not in qt_server._clients
    assert json.loads(good.sent[0])["result"] == "pong"
    assert not qt_server._processing


def test_processing_guard_resets_after_unexpected_socket_error(qt_server, monkeypatch):
    socket = FakeSocket()
    add_client(qt_server, socket)

    def fail():
        raise ValueError("unexpected callback error")

    monkeypatch.setattr(socket, "bytesAvailable", fail)
    with pytest.raises(ValueError):
        qt_server._process_messages()
    assert not qt_server._processing


def test_invalid_utf8_returns_error_without_poisoning_next_request(qt_server):
    socket = FakeSocket(b'\xff\n{"id":2,"method":"ping"}\n')
    add_client(qt_server, socket)
    qt_server._process_messages()
    qt_server._process_messages()
    assert json.loads(socket.sent[0])["error"]["type"] == "builtins.UnicodeDecodeError"
    assert json.loads(socket.sent[1])["result"] == "pong"


def test_start_does_not_duplicate_timer_and_stop_releases_signals_and_sockets(qt_server):
    socket = FakeSocket()
    add_client(qt_server, socket)
    qt_server.start()
    assert qt_server._process_timer.starts == [50]
    qt_server.stop()
    qt_server.stop()
    assert qt_server._process_timer.stopped
    assert qt_server._process_timer.timeout.slots == []
    assert qt_server._server.newConnection.slots == []
    assert socket.closed == ["disconnect", "close", "delete"]
    assert qt_server._server.closed
