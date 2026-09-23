#!/usr/bin/env python3
"""
Guard against printf-style calls to the utils.log_*() helpers (Issue #205).

``utils.log_error(message, error_obj=None)`` and its siblings take a finished
message, not a format string plus arguments. A call such as
``utils.log_error("account %s failed: %s", acct_id, exc)`` raises TypeError at
the moment an error is being reported, and the two-argument form silently
passes the account ID as the exception object.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SOURCES = sorted([*ROOT.glob("*.py"), *(ROOT / "scripts").rglob("*.py")])

# helper name -> maximum positional arguments it accepts
HELPERS = {
    "log_error": 2,
    "log_warning": 1,
    "log_info": 1,
    "log_success": 1,
    "log_debug": 1,
}


def _bad_calls(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "utils"):
            continue
        name = node.func.attr
        if name not in HELPERS:
            continue
        first = node.args[0] if node.args else None
        is_format_literal = (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and "%s" in first.value
        )
        if len(node.args) > HELPERS[name] or is_format_literal:
            yield node.lineno, name


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_printf_style_log_helper_calls(path):
    tree = ast.parse(path.read_text(), str(path))
    bad = list(_bad_calls(tree))
    assert not bad, (
        f"{path.name}: printf-style utils.log_*() call(s) at "
        + ", ".join(f"line {ln} ({name})" for ln, name in bad)
    )
