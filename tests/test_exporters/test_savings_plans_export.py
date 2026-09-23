#!/usr/bin/env python3
"""
Tests for savings_plans_export.py.

Covers:
- _build_savings_plan_row() per-item field extraction (KeyError guarding)
- collect_savings_plans() per-item guarding and account-scope failure propagation
- export_savings_plans_data()/main()'s failed-scope tracking, finalize, and
  exit-code behavior

Savings Plans is a global/account-scope service (not multi-region), so
collect_savings_plans() is the account-scope PRIMARY collector -- mirrors the
scripts/shield_export.py account-scope pattern (see scripts/lambda_export.py
for the finalize shape). moto's Savings Plans support is limited (no
describe_savings_plans state to seed and no configurable plan data), so these
tests drive the module through monkeypatched boto3 clients / module
functions rather than real moto-backed Savings Plans state.

See .collab/audit/07.16.2026-silent-collection-failure-blast-radius.md
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import botocore.exceptions
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import savings_plans_export  # noqa: E402
from savings_plans_export import collect_savings_plans  # noqa: E402

REGION = "us-east-1"


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture(autouse=True)
def pin_cost_explorer_off(monkeypatch):
    """Keep the paid Cost Explorer queries off and partition detection local
    (no STS call) -- they are covered in test_savings_plans_cost_explorer.py."""
    monkeypatch.setenv("STRATUSSCAN_CE_UTILIZATION", "0")
    monkeypatch.setattr(savings_plans_export.utils, "detect_partition", lambda *a, **kw: "aws")


@pytest.fixture(autouse=True)
def patch_output_dir(tmp_path, monkeypatch):
    """Redirect get_output_dir() to a temp directory for every test."""
    monkeypatch.setattr(savings_plans_export.utils, "get_output_dir", lambda: tmp_path)
    yield tmp_path


def _fake_sp_client(plans=None):
    """Build a MagicMock standing in for a boto3 savingsplans client."""
    client = MagicMock()
    client.describe_savings_plans.return_value = {"savingsPlans": plans or []}
    return client


class TestSilentCollectionFailureRegression:
    """
    Regression tests for the 07.15.2026 / 07.16.2026 silent-collection-failure
    audits, applied to Savings Plans: collect_savings_plans() -- the PRIMARY,
    global/account-scope collector -- used to swallow a real API error into
    an empty list, indistinguishable from an account with no savings plans
    purchased. main() also used to have no way to signal that failure
    downstream. These tests cover: (a) a malformed plan is skipped, not
    fatal; (b) collect_savings_plans() raises rather than swallowing a real
    API error; (c) that failure surfaces through main() as a non-zero exit
    plus a utils.report_collection_failures() call.
    """

    # -- (a) Per-item guard ------------------------------------------------

    def test_malformed_plan_is_skipped_not_fatal(self, monkeypatch):
        """One savings plan that fails to process must not discard the others."""
        good = {
            "savingsPlanId": "good-id",
            "savingsPlanArn": "arn:aws:savingsplans::123456789012:savingsplan/good-id",
            "savingsPlanType": "Compute",
            "state": "active",
            "commitment": "10.0",
        }
        bad = {
            "savingsPlanId": "bad-id",
            "savingsPlanArn": "arn:aws:savingsplans::123456789012:savingsplan/bad-id",
            "savingsPlanType": "Compute",
            "state": "active",
            "commitment": "5.0",
        }

        client = _fake_sp_client(plans=[good, bad])
        monkeypatch.setattr(savings_plans_export.utils, "get_boto3_client", lambda *a, **kw: client)

        original = savings_plans_export._build_savings_plan_row

        def raise_for_bad(plan):
            if plan.get("savingsPlanId") == "bad-id":
                raise KeyError("SomeUnexpectedField")
            return original(plan)

        monkeypatch.setattr(savings_plans_export, "_build_savings_plan_row", raise_for_bad)

        result = collect_savings_plans(["active"])

        ids = {row["Savings Plan ID"] for row in result}
        assert "good-id" in ids, "healthy savings plan was lost when a sibling failed"
        assert "bad-id" not in ids, "malformed savings plan should have been skipped"

    # -- (b) Account-scope failure propagation ------------------------------

    def test_collect_savings_plans_raises_on_api_error_not_swallowed(self, monkeypatch):
        """A real Savings Plans API error during collection must propagate,
        not collapse to an empty list."""

        client = MagicMock()
        client.describe_savings_plans.side_effect = botocore.exceptions.ClientError(
            {"Error": {"Code": "InternalServerException", "Message": "Something broke"}},
            "DescribeSavingsPlans",
        )
        monkeypatch.setattr(savings_plans_export.utils, "get_boto3_client", lambda *a, **kw: client)

        with pytest.raises(botocore.exceptions.ClientError):
            collect_savings_plans(["active"])

    # -- (c) main(): failure surfaced, non-zero exit ------------------------

    def test_main_savings_plans_failure_exits_nonzero_and_reports(self, monkeypatch):
        """A savings plans API failure in main() must exit non-zero and call
        utils.report_collection_failures -- never silently collapse into an
        empty export."""
        monkeypatch.setattr(savings_plans_export.utils, "ensure_dependencies", lambda *a, **kw: True)
        monkeypatch.setattr(savings_plans_export.utils, "setup_logging", lambda *a, **kw: None)
        monkeypatch.setattr(
            savings_plans_export.utils,
            "print_script_banner",
            lambda *a, **kw: ("123456789012", "test-account"),
        )

        def boom(states):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "InternalServerException", "Message": "Something broke"}},
                "DescribeSavingsPlans",
            )

        monkeypatch.setattr(savings_plans_export, "collect_savings_plans", boom)

        calls = {}

        def fake_report(account_name, resource_type, failed_scopes):
            calls["account_name"] = account_name
            calls["resource_type"] = resource_type
            calls["failed_scopes"] = failed_scopes
            return "fake-marker.txt"

        monkeypatch.setattr(savings_plans_export.utils, "report_collection_failures", fake_report)

        with pytest.raises(SystemExit) as exc_info:
            savings_plans_export.main()

        assert exc_info.value.code == 1
        assert calls.get("account_name") == "test-account"
        assert calls.get("resource_type") == "savings-plans"
        assert calls.get("failed_scopes")
        assert calls["failed_scopes"][0][0] == "savings_plans"

    def test_main_genuinely_empty_account_exits_cleanly_no_marker(self, monkeypatch):
        """A genuinely empty account (no plans, no errors) must not write a
        failure marker or exit non-zero."""
        monkeypatch.setattr(savings_plans_export.utils, "ensure_dependencies", lambda *a, **kw: True)
        monkeypatch.setattr(savings_plans_export.utils, "setup_logging", lambda *a, **kw: None)
        monkeypatch.setattr(
            savings_plans_export.utils,
            "print_script_banner",
            lambda *a, **kw: ("123456789012", "test-account"),
        )
        monkeypatch.setattr(savings_plans_export, "collect_savings_plans", lambda states: [])

        report_called = []
        monkeypatch.setattr(
            savings_plans_export.utils,
            "report_collection_failures",
            lambda *a, **kw: report_called.append((a, kw)),
        )

        # main() completes without raising SystemExit(non-zero); it simply
        # returns after logging the empty-account warning.
        savings_plans_export.main()

        assert report_called == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
