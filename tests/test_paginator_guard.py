#!/usr/bin/env python3
"""
Guard against get_paginator() calls on non-pageable AWS operations.

``client.get_paginator('op')`` only works for operations botocore ships a
paginator model for. Calling it on anything else raises
``OperationNotPageableError`` *before any AWS call is made* — and because
exporters wrap collection in broad handlers, that historically surfaced as a
silent "no resources found" rather than an error (Issue #223 / #214, ~40 call
sites across 18 exporters).

That class of bug is invisible in review and expensive to find in the field, so
it gets a test rather than a convention. Proposed as a follow-up on #223 and
added after it recurred in the Auto Scaling exporter
(``describe_instance_refreshes``, Issue #261).
"""

import collections
import re
from pathlib import Path

import boto3
import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"

# Matches the literal form: client.get_paginator('some_operation')
_GET_PAGINATOR = re.compile(r"get_paginator\(\s*['\"]([a-z0-9_]+)['\"]")


def _paginator_calls() -> dict:
    """Map {file path: {operation names}} for every literal get_paginator call."""
    calls = collections.defaultdict(set)
    for path in sorted(SCRIPTS_DIR.rglob("*.py")):
        for match in _GET_PAGINATOR.finditer(path.read_text(encoding="utf-8")):
            calls[path].add(match.group(1))
    return calls


@pytest.fixture(scope="module")
def pageable_operations() -> set:
    """
    Every operation name botocore can paginate, across all available services.

    Checked service-agnostically: resolving which service a given client
    variable belongs to would mean type-inferring the whole file, and an
    operation name that no service can paginate is already proof of a bug.
    """
    pageable = set()
    session = boto3.session.Session()
    for service in session.get_available_services():
        try:
            client = boto3.client(
                service,
                region_name="us-east-1",
                aws_access_key_id="testing",
                aws_secret_access_key="testing",
            )
        except Exception:  # noqa: BLE001 - service needs config we don't have
            continue
        for operation in client._PY_TO_OP_NAME:
            try:
                if client.can_paginate(operation):
                    pageable.add(operation)
            except Exception:  # noqa: BLE001 - malformed name in the model
                continue
    return pageable


def test_every_get_paginator_call_targets_a_pageable_operation(pageable_operations):
    """
    No exporter may call get_paginator() on an operation botocore cannot
    paginate. Use a manual NextToken/NextMarker loop for those instead.
    """
    calls = _paginator_calls()
    assert calls, "found no get_paginator calls — the scanner regex is broken"

    violations = [
        f"{path.relative_to(SCRIPTS_DIR.parent)}: get_paginator('{operation}')"
        for path, operations in calls.items()
        for operation in sorted(operations)
        if operation not in pageable_operations
    ]

    assert not violations, (
        "get_paginator() called on non-pageable operation(s) — this raises "
        "OperationNotPageableError before any AWS call and yields a silent "
        "empty result. Replace with a manual token loop:\n  "
        + "\n  ".join(violations)
    )
