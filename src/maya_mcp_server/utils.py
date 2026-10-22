import logging
import os
import socket
from collections.abc import Iterator

import psutil

from maya_mcp_server.types import MayaListeningPort


logger = logging.getLogger(__name__)


def _configured_ports(name: str, default: str) -> set[int]:
    raw = os.environ.get(name, default)
    return {int(item.strip()) for item in raw.replace(";", ",").split(",") if item.strip()}


def get_maya_process() -> Iterator[psutil.Process]:
    """
    Find all Maya processes.

    Yields:
        Maya Process objects
    """
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            name = proc.info["name"]
            # Maya process names vary by platform
            if name in ["Maya", "maya", "maya.exe", "Maya.exe"]:
                yield proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def get_maya_listening_ports() -> Iterator[MayaListeningPort]:
    """
    Get all ports that Maya processes are listening on.

    Yields:
        MayaListeningPort dictionaries with port, address, and process_id
    """
    found_any_maya = False
    # Only explicit Python bootstrap ports may receive code. In particular never
    # probe the company MEL/plugin ports, even if they are in the allow list.
    allowed = _configured_ports("MAYA_MCP_ALLOW_PORTS", "7002,7003,7004,7005,7006")
    skipped = {4434, 4648, 4658} | _configured_ports("MAYA_MCP_SKIP_PORTS", "")

    for maya_proc in get_maya_process():
        found_any_maya = True
        logger.debug(f"Found Maya process: PID {maya_proc.pid}")

        try:
            # Get all network connections for Maya process
            connections = maya_proc.net_connections(kind="inet")

            for conn in connections:
                # Only get listening TCP connections on IPv4 (command ports are always IPv4)
                if (
                    conn.status == "LISTEN"
                    and conn.type == socket.SOCK_STREAM
                    and conn.family == socket.AF_INET
                    and conn.laddr.port in allowed
                    and conn.laddr.port not in skipped
                ):
                    yield MayaListeningPort(
                        port=conn.laddr.port,
                        address=conn.laddr.ip,
                        process_id=maya_proc.pid,
                    )

        except psutil.AccessDenied:
            logger.warning(
                f"Access denied getting Maya connections for PID {maya_proc.pid} - "
                "try running as administrator/sudo"
            )
            continue

    if not found_any_maya:
        logger.debug("Maya is not running")
