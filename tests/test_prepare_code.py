"""Behavioral tests for AST result capture, including Maya 2022 compatibility."""

import ast
from pathlib import Path
from types import CodeType

import pytest

from maya_mcp_server import maya_mcp_helper as helper


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("1 + 1", 2),
        ("value = 5\nvalue", 5),
        ("x = 1\ny = 2\nx + y", 3),
        ("[1, 2, 3]", [1, 2, 3]),
        ("{'a': 1, 'b': 2}", {"a": 1, "b": 2}),
        ("'hello world'", "hello world"),
        ("x = 1\n(1 +\n 2 +\n 3)", 6),
        ("sum(\n [1,\n 2,\n 3]\n)", 6),
        ("1 if True else 2", 1),
        ("[x * 2 for x in range(3)]", [0, 2, 4]),
        ("x = 1; 2", 2),
        ("def foo():\n    return 7\nfoo()", 7),
        ("class Foo:\n    x = 8\nFoo.x", 8),
        ("(1 + 2) * 3 / 4 - 5 % 2", 1.25),
        ("name = 'Maya'\nf'Hello {name}'", "Hello Maya"),
        ("1 < 2 < 3", True),
        ("True and False or True", True),
        ("not True", False),
        ("values = [1, 2, 3]\nvalues[0]", 1),
        ("values = [1, 2, 3]\nvalues[1:3]", [2, 3]),
        ("中文 = '路径'; # Unicode before final expression\n中文 + '/模型'", "路径/模型"),
        ("1 + 1   \n\n# trailing comment", 2),
    ],
)
def test_capture_executes_the_original_expression(source, expected):
    compiled, transformed = helper.prepare_code_for_result_capture(source)
    assert isinstance(compiled, CodeType)
    assert transformed
    namespace = {}
    exec(compiled, namespace)
    assert namespace[helper.CAPTURE_VARIABLE] == expected


@pytest.mark.parametrize(
    "source",
    [
        "",
        "   \n\n",
        "# just a comment",
        "x = 1",
        "x: int = 1",
        "x, y = 1, 2",
        "def foo():\n    return 1",
        "async def foo():\n    return 1",
        "class Foo:\n    pass",
        "for i in range(2):\n    pass",
        "while False:\n    pass",
        "if True:\n    pass",
        "try:\n    pass\nexcept Exception:\n    pass",
        "import os",
        "from os import path",
        "assert True",
        "pass",
        "global x",
    ],
)
def test_statements_and_empty_code_do_not_capture(source):
    compiled, transformed = helper.prepare_code_for_result_capture(source)
    assert isinstance(compiled, CodeType)
    assert not transformed
    namespace = {}
    exec(compiled, namespace)
    assert helper.CAPTURE_VARIABLE not in namespace


@pytest.mark.parametrize(
    "source", ["def foo(:", "break", "continue", "return 1", "nonlocal x", "await f()", "yield 2"]
)
def test_invalid_module_code_retains_python_syntax_errors(source):
    with pytest.raises(SyntaxError):
        helper.prepare_code_for_result_capture(source)


def test_capture_works_without_ast_unparse(monkeypatch):
    monkeypatch.delattr(ast, "unparse", raising=False)
    compiled, transformed = helper.prepare_code_for_result_capture("中文 = 4; 中文 + 2")
    namespace = {}
    exec(compiled, namespace)
    assert transformed
    assert namespace[helper.CAPTURE_VARIABLE] == 6


def test_final_call_runs_once_and_can_return_a_non_json_object():
    source = "calls.append(1)\n(lambda value: value + 1)"
    compiled, _ = helper.prepare_code_for_result_capture(source, capture_variable="answer")
    namespace = {"calls": []}
    exec(compiled, namespace)
    assert namespace["calls"] == [1]
    assert namespace["answer"](2) == 3


def test_traceback_preserves_source_line_numbers():
    compiled, _ = helper.prepare_code_for_result_capture("x = 1\ny = 0\nx / y")
    with pytest.raises(ZeroDivisionError) as caught:
        exec(compiled, {})
    traceback = caught.value.__traceback__
    while traceback.tb_next:
        traceback = traceback.tb_next
    assert traceback.tb_frame.f_code.co_filename == "<mcp>"
    assert traceback.tb_lineno == 3


def test_user_future_imports_remain_at_the_start():
    compiled, _ = helper.prepare_code_for_result_capture(
        "from __future__ import annotations\ndef fn(value: Missing):\n    return 2\nfn(0)"
    )
    namespace = {}
    exec(compiled, namespace)
    assert namespace[helper.CAPTURE_VARIABLE] == 2
    assert namespace["fn"].__annotations__["value"] == "Missing"


def test_injected_sources_use_python_37_grammar():
    # Supplements the separate real Python 3.7/mayapy run.
    for filename in ("maya_mcp_helper.py", "maya_bootstrap.py"):
        source = Path(helper.__file__).with_name(filename).read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 7))
