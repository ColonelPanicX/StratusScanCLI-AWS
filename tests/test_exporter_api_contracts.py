#!/usr/bin/env python3
"""
Static API-contract checks across every exporter in scripts/.

The import-only smoke test (test_smoke.py) never exercises an exporter's AWS
calls, so runtime-only mistakes hide — e.g. issue #208, where a client was
built with the non-existent boto3 service 'verifiedaccess' and the error was
swallowed into an empty export.

These tests parse each exporter's AST (no AWS credentials / moto needed) and
guard the runtime-fatal mistakes this codebase has actually shipped:

  1. get_boto3_client('<svc>') must name a real boto3 service       (issue #208)
  2. <client>.get_paginator('<op>') must name an operation that
     exists on that client's service                                (issues #208, #212)
  3. <client>.get_paginator('<op>') must not be used on a known
     non-paginatable operation — ratcheted against a baseline so the
     existing backlog can't grow                                    (issue #214)

Version coupling: validity is checked against the *installed* botocore. At or
above the declared floor (utils.BOTOCORE_MIN_VERSION, issue #213) the checks
are strict. Below it (e.g. an OS-packaged botocore on a dev box) a symbol is
tolerated only if MEASURED_MIN_BOTOCORE records that it first ships in a
release newer than the installed one -- anything else still fails, so a typo
like #208's 'verifiedaccess' is caught on every SDK.
"""

import ast
from pathlib import Path

import boto3
import botocore
import botocore.session
import pytest
from botocore import xform_name

import utils

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"

_session = botocore.session.get_session()
_VALID_SERVICES = set(_session.get_available_services())

# First botocore release containing each symbol that sits above older SDKs,
# measured by installing botocore releases and reading their service models
# (issue #213) -- not estimated. Keys: service name, or (service, op) for a
# paginator. Every value must be <= the declared floor (test_sdk_floor.py).
# Not an allowlist: at/above the floor these entries tolerate nothing.
MEASURED_MIN_BOTOCORE: dict = {
    "controlcatalog": "1.34.80",  # controltower_export.py
}

_INSTALLED = utils._version_tuple(botocore.__version__)


def _newer_than_installed(symbol) -> bool:
    """True if this symbol is known to first ship after the installed botocore."""
    first = MEASURED_MIN_BOTOCORE.get(symbol)
    return first is not None and utils._version_tuple(first) > _INSTALLED

# (service, op) pairs that name an operation which does not exist in any SDK
# version. Empty since #212 was fixed; keep it that way.
KNOWN_BROKEN_PAGINATORS: set[tuple[str, str]] = set()

# Baseline of get_paginator() calls on operations that EXIST but are NOT
# paginatable (issue #214). Burned down to zero; the ratchet test now blocks
# any such call. Keyed "<file>::<svc>.<op>".
KNOWN_NONPAGINATABLE: set[str] = set()

_ops_cache: dict = {}
_paginatable_cache: dict = {}


def _operations(service):
    """snake_case operation names for a service, or None if service unknown here."""
    if service not in _ops_cache:
        if service not in _VALID_SERVICES:
            _ops_cache[service] = None
        else:
            model = _session.get_service_model(service)
            _ops_cache[service] = {xform_name(o) for o in model.operation_names}
    return _ops_cache[service]


def _paginatable(service):
    """snake_case paginatable operation names for a service, or None if unknown."""
    if service not in _paginatable_cache:
        if service not in _VALID_SERVICES:
            _paginatable_cache[service] = None
        else:
            client = boto3.client(
                service,
                region_name="us-east-1",
                aws_access_key_id="testing",
                aws_secret_access_key="testing",
            )
            _paginatable_cache[service] = {
                op for op in _operations(service) if client.can_paginate(op)
            }
    return _paginatable_cache[service]


def _exporter_files():
    return sorted(p for p in SCRIPTS_DIR.glob("*.py") if p.name != "__init__.py")


def _client_service_literals(tree):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if name == "get_boto3_client" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.append((node.lineno, arg.value))
    return out


def _paginator_calls(tree):
    """Resolve <var>.get_paginator('<op>') to (lineno, service, op) using
    get_boto3_client('<svc>') assignments in the same scope."""
    results = set()
    scopes = [tree] + [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for scope in scopes:
        var_service = {}
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                f = node.value.func
                name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
                if name == "get_boto3_client" and node.value.args:
                    arg = node.value.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        for tgt in node.targets:
                            if isinstance(tgt, ast.Name):
                                var_service[tgt.id] = arg.value
        for node in ast.walk(scope):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get_paginator"
                and isinstance(node.func.value, ast.Name)
                and node.args
            ):
                arg = node.args[0]
                svc = var_service.get(node.func.value.id)
                if svc and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    results.add((node.lineno, svc, arg.value))
    return results


@pytest.mark.parametrize("path", _exporter_files(), ids=lambda p: p.name)
def test_get_boto3_client_uses_valid_service(path):
    """Every get_boto3_client('<svc>') must name a real boto3 service (issue #208)."""
    tree = ast.parse(path.read_text(), str(path))
    invalid = [
        (lineno, svc)
        for lineno, svc in _client_service_literals(tree)
        if svc not in _VALID_SERVICES and not _newer_than_installed(svc)
    ]
    assert not invalid, (
        f"{path.name} calls get_boto3_client with invalid boto3 service(s): "
        + ", ".join(f"'{s}' (line {ln})" for ln, s in invalid)
    )


@pytest.mark.parametrize("path", _exporter_files(), ids=lambda p: p.name)
def test_get_paginator_operation_exists(path):
    """Every get_paginator('<op>') must name an operation on that service (#208/#212)."""
    tree = ast.parse(path.read_text(), str(path))
    bad = []
    for lineno, svc, op in _paginator_calls(tree):
        if _newer_than_installed((svc, op)) or (svc, op) in KNOWN_BROKEN_PAGINATORS:
            continue
        ops = _operations(svc)
        if ops is None:
            continue  # service unknown to this botocore — covered elsewhere
        if op not in ops:
            bad.append((lineno, svc, op))
    assert not bad, (
        f"{path.name} calls get_paginator with non-existent operation(s): "
        + ", ".join(f"{svc}.{op} (line {ln})" for ln, svc, op in bad)
    )


def test_no_new_nonpaginatable_get_paginator():
    """Ratchet (issue #214): no NEW get_paginator() on a non-paginatable op.

    Existing offenders are baselined in KNOWN_NONPAGINATABLE; remove entries as
    they are fixed. A new offender anywhere fails this test.
    """
    found = set()
    for path in _exporter_files():
        tree = ast.parse(path.read_text(), str(path))
        for _lineno, svc, op in _paginator_calls(tree):
            ops = _operations(svc)
            paginatable = _paginatable(svc)
            if ops is None or paginatable is None:
                continue
            if op in ops and op not in paginatable:
                found.add(f"{path.name}::{svc}.{op}")

    new_offenders = sorted(found - KNOWN_NONPAGINATABLE)
    assert not new_offenders, (
        "New get_paginator() call(s) on non-paginatable operations (issue #214). "
        "Use a manual NextToken/Marker loop instead of get_paginator: "
        + ", ".join(new_offenders)
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
