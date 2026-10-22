"""Exercise the MCP tool wire contract without a Maya installation."""

import json
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from maya_mcp_server import server
from maya_mcp_server.client import MayaExecutionError
from maya_mcp_server.types import CommandResponse


@pytest.mark.parametrize("value", [None, False, 0, "", [], {"中文": [1, False]}])
async def test_success_keeps_json_types(monkeypatch, value):
    maya = AsyncMock()
    maya.execute_code.return_value = CommandResponse(value, None, stdout="回显\n")
    manager = AsyncMock()
    manager.get_client.return_value = maya
    monkeypatch.setattr(server, "_session_manager", manager)
    async with Client(server.mcp) as client:
        reply = await client.call_tool("execute_code", {"code": "value", "result_type": "JSON"})
    payload = reply.structured_content
    assert payload["success"] is True
    assert payload["result"] == value
    assert type(payload["result"]) is type(value)
    assert payload["stdout"] == "回显\n"
    assert json.loads(reply.content[0].text) == payload
    assert reply.is_error is False


async def test_remote_error_keeps_traceback_and_output(monkeypatch):
    error = {"type": "builtins.ValueError", "message": "中文错误", "traceback": "trace marker"}
    maya = AsyncMock()
    maya.execute_code.return_value = CommandResponse(None, error, stdout="before failure")
    manager = AsyncMock()
    manager.get_client.return_value = maya
    monkeypatch.setattr(server, "_session_manager", manager)
    async with Client(server.mcp) as client:
        reply = await client.call_tool(
            "execute_code", {"code": "raise ValueError()"}, raise_on_error=False
        )
    assert reply.is_error is True
    assert reply.structured_content["error"] == error
    assert reply.structured_content["stdout"] == "before failure"
    assert reply.structured_content["execution_state"] == "failed"
    assert json.loads(reply.content[0].text) == reply.structured_content


async def test_timeout_is_unknown_and_never_replayed(monkeypatch):
    maya = AsyncMock()
    maya.execute_code.side_effect = MayaExecutionError("Timeout waiting for Qt server response")
    manager = AsyncMock()
    manager.get_client.return_value = maya
    monkeypatch.setattr(server, "_session_manager", manager)
    async with Client(server.mcp) as client:
        reply = await client.call_tool(
            "execute_code", {"code": "mutate()", "timeout": 0.1}, raise_on_error=False
        )
    assert reply.is_error is True
    assert reply.structured_content["execution_state"] == "unknown"
    maya.execute_code.assert_awaited_once()
