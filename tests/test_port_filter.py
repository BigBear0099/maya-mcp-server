"""Unknown Maya listener ports must never receive a probe."""

import socket
from types import SimpleNamespace

from maya_mcp_server import utils


def test_only_configured_python_ports_are_exposed(monkeypatch):
    connections = [
        SimpleNamespace(
            status="LISTEN",
            type=socket.SOCK_STREAM,
            family=socket.AF_INET,
            laddr=SimpleNamespace(port=port, ip="127.0.0.1"),
        )
        for port in [4434, 4648, 4658, 7002, 7003, 9898]
    ]
    process = SimpleNamespace(pid=123, net_connections=lambda **_: connections)
    monkeypatch.setattr(utils, "get_maya_process", lambda: iter([process]))
    monkeypatch.setenv("MAYA_MCP_ALLOW_PORTS", "4434,4648,4658,7003")
    assert [p["port"] for p in utils.get_maya_listening_ports()] == [7003]
    monkeypatch.setenv("MAYA_MCP_ALLOW_PORTS", "0")
    assert list(utils.get_maya_listening_ports()) == []
