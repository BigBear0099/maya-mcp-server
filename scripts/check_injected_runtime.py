"""Dependency-free injected helper checks, runnable in Maya's Python 3.7+."""

import json
import runpy
import sys
from pathlib import Path


root = Path(__file__).resolve().parents[1]
helper = runpy.run_path(str(root / "src/maya_mcp_server/maya_mcp_helper.py"))
execute = helper["execute"]
for mode, expected in [("NONE", None), ("RAW", "2"), ("JSON", "2")]:
    answer = json.loads(execute("1+1", mode))
    assert answer == {"result": expected, "error": None}, answer
answer = json.loads(execute("counter=[]", "NONE"))
assert not answer["error"]
answer = json.loads(execute("counter.append('中文')", "JSON"))
assert not answer["error"]
assert json.loads(json.loads(execute("counter", "JSON"))["result"]) == ["中文"]
answer = json.loads(execute("raise ValueError('failure')", "NONE"))
assert answer["error"]["type"].endswith("ValueError"), answer
assert "Traceback" in answer["error"]["traceback"]
print(json.dumps({"pass": True, "python": sys.version, "checks": 6}))
