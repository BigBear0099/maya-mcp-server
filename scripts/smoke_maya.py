"""Bootstrap an owned Maya GUI/profile and test the real stdio -> Qt -> Maya path.

Only new instances are accepted. No discovery probes or commands reach existing Maya.
Evidence is written before reporting PASS/FAIL; all owned processes are cleaned up.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import uuid
from datetime import timedelta
from pathlib import Path

import psutil
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def bootstrap(version, run_dir):
    executable = Path("C:/Program Files/Autodesk") / ("Maya" + version) / "bin/maya.exe"
    if not executable.is_file():
        raise RuntimeError("Maya is not installed: " + str(executable))
    token = uuid.uuid4().hex
    port = free_port()
    profile = run_dir / "profile"
    profile.mkdir()
    ready = run_dir / "ready.json"
    boot = run_dir / "bootstrap.py"
    boot.write_text(
        "import os,json,sys\nimport maya.cmds as c\n"
        + f"c.cmdFileOutput(open={str(run_dir / 'script_editor.log')!r})\n"
        + f"c.commandPort(name='127.0.0.1:{port}',sourceType='python',echoOutput=False,"
        "securityWarning=False)\n"
        + "data={'pid':os.getpid(),'token':os.environ['MAYA_MCP_SMOKE_TOKEN'],"
        "'maya':c.about(version=True),'python':sys.version,"
        "'profile':os.environ['MAYA_APP_DIR']}\n"
        + "with open(%r,'w') as f: json.dump(data,f)\n" % (ready.as_posix() + ".tmp")
        + "os.replace({!r},{!r})\n".format(ready.as_posix() + ".tmp", ready.as_posix()),
        encoding="utf-8",
    )
    mel = run_dir / "bootstrap.mel"
    expression = f"exec(compile(open({boot.as_posix()!r},'rb').read(),{boot.as_posix()!r},'exec'))"
    mel.write_text(f"python({json.dumps(expression)});\n", encoding="utf-8")
    env = dict(os.environ)
    # A private profile and empty user script/module paths keep company userSetup
    # and production plugins out of this regression instance.
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "MAYA_SCRIPT_PATH",
        "MAYA_MODULE_PATH",
        "MAYA_PLUG_IN_PATH",
        "MAYA_LOCATION",
    ):
        env.pop(key, None)
    env.update(
        MAYA_APP_DIR=str(profile),
        MAYA_MCP_SMOKE_TOKEN=token,
        MAYA_SKIP_USERSETUP_PY="1",
        MAYA_DISABLE_CIP="1",
        MAYA_DISABLE_CER="1",
    )
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    log = (run_dir / "maya.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(executable), "-noAutoloadPlugins", "-script", str(mel)],
        env=env,
        cwd=run_dir,
        stdout=log,
        stderr=subprocess.STDOUT,
        startupinfo=startup,
    )
    try:
        owner = psutil.Process(proc.pid)
        created = owner.create_time()
        write_json(
            run_dir / "owner.json",
            {
                "pid": proc.pid,
                "created": created,
                "token": token,
                "port": port,
                "executable": str(executable),
            },
        )
    except BaseException:
        proc.terminate()
        proc.wait(15)
        log.close()
        raise
    return proc, created, token, port, ready, log


async def run_version(
    version, timeout, server_python=None, server_wrapper=None, long_operation=False
):
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = ROOT / "artifacts" / ("maya" + version) / run_id
    run_dir.mkdir(parents=True)
    before = {
        p.pid: p.create_time() for p in psutil.process_iter() if p.name().lower() == "maya.exe"
    }
    result = {"version": version, "run_id": run_id, "checks": [], "pass": False}
    proc = None
    maya_log = None
    events = (run_dir / "rpc.jsonl").open("w", encoding="utf-8")
    try:
        proc, created, token, port, ready, maya_log = bootstrap(version, run_dir)
        deadline = time.monotonic() + timeout
        while not ready.exists():
            if proc.poll() is not None:
                raise RuntimeError(f"Owned Maya exited before bootstrap: {proc.returncode}")
            if time.monotonic() > deadline:
                raise TimeoutError("Owned Maya did not write its bootstrap ready file")
            await asyncio.sleep(1)
        manifest = json.loads(ready.read_text(encoding="utf-8"))
        assert manifest["pid"] == proc.pid and manifest["token"] == token
        assert str(manifest["maya"]).startswith(version)
        assert psutil.Process(proc.pid).create_time() == created and proc.pid not in before
        ports = psutil.Process(proc.pid).net_connections(kind="inet")
        assert any(c.status == "LISTEN" and c.laddr.port == port for c in ports)
        write_json(run_dir / "manifest.json", manifest)
        result["manifest"] = manifest
        env = dict(
            os.environ,
            MAYA_MCP_ALLOW_PORTS="0",
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
            PYTHONUNBUFFERED="1",
        )
        params = StdioServerParameters(
            command=server_python or sys.executable,
            args=([server_wrapper] if server_wrapper else ["-m", "maya_mcp_server"])
            + ["--scan-interval", "0.2" if long_operation else "3600", "-v"],
            env=env,
            cwd=str(ROOT),
        )
        with (run_dir / "server.log").open("w", encoding="utf-8") as server_log:
            async with stdio_client(params, errlog=server_log) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {"execute_code", "list_sessions", "add_session", "write_module"} <= {
                        item.name for item in tools.tools
                    }

                    async def call(name, arguments):
                        reply = await session.call_tool(
                            name, arguments, read_timeout_seconds=timedelta(seconds=120)
                        )
                        events.write(
                            json.dumps(
                                {
                                    "tool": name,
                                    "arguments": arguments,
                                    "reply": reply.model_dump(mode="json"),
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        events.flush()
                        return reply

                    added = await call("add_session", {"host": "127.0.0.1", "port": port})
                    if added.isError:
                        with socket.create_connection(("127.0.0.1", port), timeout=5) as diagnostic:
                            diagnostic.settimeout(5)
                            diagnostic.sendall(
                                b"str(__import__('sys').modules.get('maya_mcp').get_qt_server_port())\n"
                            )
                            result["qt_diagnostic"] = diagnostic.recv(16384).decode(
                                "utf-8", "replace"
                            )
                        result["owned_listeners"] = [
                            c.laddr.port
                            for c in psutil.Process(proc.pid).net_connections(kind="inet")
                            if c.status == "LISTEN"
                        ]
                    assert not added.isError, added
                    info = added.structuredContent
                    if not info:
                        info = json.loads(added.content[0].text)
                    assert info["pid"] == proc.pid
                    key = info["session_key"]
                    result["session"] = info
                    listed = await call("list_sessions", {})
                    assert not listed.isError
                    assert str(proc.pid) in json.dumps(listed.model_dump(mode="json"))
                    qt_port = int(key.rsplit(":", 1)[1])
                    assert qt_port != port
                    assert any(
                        c.status == "LISTEN" and c.laddr.port == qt_port
                        for c in psutil.Process(proc.pid).net_connections(kind="inet")
                    )

                    async def execute(code, mode="JSON", error=False, **kwargs):
                        reply = await call(
                            "execute_code",
                            dict(code=code, result_type=mode, session_key=key, **kwargs),
                        )
                        body = reply.structuredContent
                        assert isinstance(body, dict), reply
                        assert set(
                            (
                                "success",
                                "result",
                                "error",
                                "stdout",
                                "stderr",
                                "truncated",
                                "execution_state",
                            )
                        ) <= set(body)
                        assert bool(reply.isError) == error, body
                        assert body["success"] is not error, body
                        assert json.loads(reply.content[0].text) == body
                        return body

                    async def check(name, operation):
                        await operation()
                        result["checks"].append(name)
                        print(f"Maya {version} PASS {name}", flush=True)

                    async def identity():
                        body = await execute(
                            "import os,sys; import maya.cmds as cmds\n"
                            "try:\n    import PySide2\n    qt_binding='PySide2'\n"
                            "except ImportError:\n    import PySide6\n    qt_binding='PySide6'\n"
                            "{'pid':os.getpid(),'python':sys.version,'qt':qt_binding,"
                            "'main_thread':__import__('threading').current_thread() is "
                            "__import__('threading').main_thread()}"
                        )
                        assert body["result"]["pid"] == proc.pid
                        assert body["result"]["main_thread"] is True
                        assert body["result"]["qt"] == (
                            "PySide2" if version == "2022" else "PySide6"
                        )
                        result["runtime"] = body["result"]

                    await check("stdio/Qt identity", identity)

                    node = "mcp_smoke_" + token[:12]
                    text = '中文\\路径\n引号" emoji 🐻'

                    async def scene():
                        await execute(
                            f"node=cmds.createNode('transform',name={node!r}); "
                            "cmds.setAttr(node+'.translate',1,2,3); "
                            "cmds.addAttr(node,ln='text',dt='string'); "
                            f"cmds.setAttr(node+'.text',{text!r},type='string')",
                            "NONE",
                        )
                        body = await execute(
                            "{{'type':cmds.nodeType({!r}),'t':cmds.getAttr({!r}),"
                            "'text':cmds.getAttr({!r})}}".format(
                                node, node + ".translate", node + ".text"
                            )
                        )
                        assert body["result"] == {
                            "type": "transform",
                            "t": [[1.0, 2.0, 3.0]],
                            "text": text,
                        }

                    await check("scene and Unicode round trip", scene)

                    async def values():
                        for source, expected in (
                            ("None", None),
                            ("False", False),
                            ("0", 0),
                            ("''", ""),
                            ("[]", []),
                            ("{'a':[0,False]}", {"a": [0, False]}),
                        ):
                            body = await execute(source)
                            assert body["result"] == expected
                            assert type(body["result"]) is type(expected)
                            raw = await execute(source, "RAW")
                            assert raw["result"] == str(expected)
                            none = await execute(source, "NONE")
                            assert none["result"] is None

                    await check("NONE/RAW/JSON result contract", values)

                    async def once():
                        await execute("counter=[]", "NONE")
                        await execute("counter.append(1)", "NONE")
                        await execute("counter.append(2)", "RAW")
                        await execute("counter.append(3)", "JSON")
                        assert (await execute("counter"))["result"] == [1, 2, 3]

                    await check("side effects exactly once", once)

                    async def outputs():
                        body = await execute(
                            "import sys; print({!r}); sys.stderr.write({!r}); 'ok'".format(
                                text, "中文 stderr"
                            ),
                            "RAW",
                        )
                        assert body["stdout"] == text + "\n"
                        assert body["stderr"] == "中文 stderr"
                        assert (await execute("'next'", "RAW"))["stdout"] == ""
                        body = await execute(
                            "print('before'); raise ValueError('中文错误')", "NONE", error=True
                        )
                        assert body["error"]["type"].endswith("ValueError")
                        assert "中文错误" in body["error"]["traceback"]
                        assert body["stdout"] == "before\n"
                        assert body["execution_state"] == "failed"
                        syntax = await execute("if (", error=True)
                        assert syntax["error"]["type"].endswith("SyntaxError")
                        serial = await execute("{1,2,3}", error=True)
                        assert serial["error"]["type"].endswith("TypeError")
                        assert (await execute("2+2"))["result"] == 4

                    await check("stdout/stderr and exception recovery", outputs)

                    async def module():
                        reply = await call(
                            "write_module",
                            {
                                "name": "mcp_smoke_module",
                                "code": (
                                    "base=7\ndef values():\n    return [base+i for i in range(3)]\n"
                                ),
                                "session_key": key,
                            },
                        )
                        assert not reply.isError, reply
                        body = await execute("import mcp_smoke_module; mcp_smoke_module.values()")
                        assert body["result"] == [7, 8, 9]

                    await check("module and execution namespace", module)

                    async def limits():
                        body = await execute("print('中'*400000)", "NONE")
                        assert body["truncated"] is True
                        assert len(body["stdout"].encode("utf-8")) <= 1048576
                        assert "\ufffd" not in body["stdout"]
                        assert (await execute("3+3"))["result"] == 6

                    await check("bounded Unicode output", limits)

                    async def shared_clients():
                        async with stdio_client(params, errlog=server_log) as (read2, write2):
                            async with ClientSession(read2, write2) as second:
                                await second.initialize()
                                added2 = await second.call_tool(
                                    "add_session", {"host": "127.0.0.1", "port": port}
                                )
                                assert not added2.isError, added2
                                info2 = added2.structuredContent
                                assert info2["session_key"] == key
                                code = (
                                    "qt_core=__import__(qt_binding+'.QtCore',"
                                    "fromlist=['QCoreApplication']); print('first'); "
                                    "qt_core.QCoreApplication.processEvents(); 'first'"
                                )
                                first, reply2 = await asyncio.gather(
                                    execute(code, "RAW"),
                                    second.call_tool(
                                        "execute_code",
                                        {
                                            "code": "print('second'); 'second'",
                                            "result_type": "RAW",
                                            "session_key": key,
                                        },
                                    ),
                                )
                                assert not reply2.isError, reply2
                                assert first["stdout"] == "first\n"
                                assert reply2.structuredContent["stdout"] == "second\n"
                        assert (await execute("7+7"))["result"] == 14

                    await check("shared Qt service and atomic client output", shared_clients)

                    if long_operation:

                        async def long_request():
                            body = await execute("import time; time.sleep(65); 65", timeout=85)
                            assert body["result"] == 65
                            assert (await execute("8+8"))["result"] == 16

                        await check("65-second request survives heartbeat scans", long_request)

                    async def slow():
                        # A tiny explicit timeout fails after dispatch; allow the
                        # original mutation to finish, then independently query it.
                        body = await execute(
                            "import time; time.sleep(0.5); counter.append(4)",
                            "NONE",
                            error=True,
                            timeout=0.05,
                        )
                        assert body["execution_state"] == "unknown"
                        await asyncio.sleep(0.8)
                        assert (await execute("counter"))["result"] == [1, 2, 3, 4]

                    await check("timeout without replay or stale reply", slow)
        result["pass"] = True
    except BaseException:
        result["error"] = traceback.format_exc()
        print(result["error"], flush=True)
    finally:
        events.close()
        if proc is not None:
            try:
                owned = psutil.Process(proc.pid)
                if owned.create_time() == created:
                    owned.terminate()
                    try:
                        owned.wait(15)
                    except psutil.TimeoutExpired:
                        owned.kill()
                        owned.wait(10)
            except psutil.NoSuchProcess:
                pass
            except BaseException:
                result["cleanup_error"] = traceback.format_exc()
                result["pass"] = False
        if maya_log:
            maya_log.close()
        # This assertion never sends a command to an existing user Maya.
        try:
            result["existing_instances_preserved"] = all(
                psutil.pid_exists(pid) and psutil.Process(pid).create_time() == started
                for pid, started in before.items()
            )
        except psutil.Error:
            result["existing_instances_preserved"] = False
        result["pass"] = result["pass"] and result["existing_instances_preserved"]
        write_json(run_dir / "result.json", result)
    print(
        json.dumps(
            {"version": version, "pass": result["pass"], "evidence": str(run_dir)},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return result["pass"]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", action="store_true", required=True)
    parser.add_argument("--version", choices=["2022", "2026"], required=True)
    parser.add_argument("--startup-timeout", type=float, default=240)
    parser.add_argument("--server-python")
    parser.add_argument("--server-wrapper")
    parser.add_argument("--long-operation", action="store_true")
    args = parser.parse_args()
    return (
        0
        if await run_version(
            args.version,
            args.startup_timeout,
            args.server_python,
            args.server_wrapper,
            args.long_operation,
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
