#!/usr/bin/env python3
"""
Tests for controltower_export.py.

Covers:
- collect_landing_zone() as a dual-purpose PRIMARY collector / "is Control
  Tower set up" gate: graceful "not set up" handling vs. real API-error
  propagation
- collect_organizational_units() / collect_enabled_controls() per-item
  guarding and account-scope failure propagation
- main()'s failed-scope tracking, always-written Summary sheet, and
  exit-code behavior

AWS Control Tower is a global, account-scope service run from the
management account (no region scan) -- collect_landing_zone(),
collect_organizational_units(), and collect_enabled_controls() are the
PRIMARY account-scope collectors -- mirrors the scripts/shield_export.py /
scripts/organizations_export.py account-scope pattern (see
scripts/lambda_export.py for the finalize shape).

NOTE on moto: moto has NO Control Tower support at all (no
``moto.controltower`` module -- confirmed via
``import moto.controltower.models`` raising ``ModuleNotFoundError``), and
botocore below 1.34.80 does not recognize ``controlcatalog`` as a service
(``UnknownServiceError``; measured in Issue #213, below the declared floor). Because
of this, every test below drives the module through monkeypatched boto3
clients / module functions rather than real moto-backed Control Tower
state; ``tests/test_smoke.py::test_exporter_smoke[controltower_export]`` is
expected to fail/report a real (non-mock) collection failure for the same
reason -- see that test's run notes.

See .collab/audit/07.16.2026-silent-collection-failure-blast-radius.md
"""

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import botocore.exceptions
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import controltower_export  # noqa: E402
from controltower_export import (  # noqa: E402
    collect_enabled_controls,
    collect_landing_zone,
    collect_organizational_units,
)

REGION = "us-east-1"

# save_multiple_dataframes_to_excel() logs via the raw module-level `logger`
# (not the null-safe get_logger() used by utils.log_*()). controltower's
# main() -- unlike shield/organizations -- has no wrapping try/except around
# its export call, so leaving utils.logger as None (its default) when
# setup_logging() is mocked away would surface as an unrelated
# AttributeError instead of exercising the behavior under test.
_NULL_LOGGER = logging.getLogger("stratusscan-test-controltower")
_NULL_LOGGER.addHandler(logging.NullHandler())
_NULL_LOGGER.propagate = False


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture(autouse=True)
def patch_output_dir(tmp_path, monkeypatch):
    """Redirect get_output_dir() to a temp directory for every test."""
    monkeypatch.setattr(controltower_export.utils, "get_output_dir", lambda: tmp_path)
    yield tmp_path


def _client_error(code, operation, message="boom"):
    return botocore.exceptions.ClientError({"Error": {"Code": code, "Message": message}}, operation)


class TestCollectLandingZone:
    """
    collect_landing_zone() is the PRIMARY, account-scope collector that also
    doubles as the "is Control Tower set up" gate. An empty landing-zone
    list, or AccessDenied/ResourceNotFound on ListLandingZones, is the
    legitimate "not set up" state and returns ``{}`` gracefully -- never a
    raise. Any other error (real API failure) must propagate.
    """

    def test_no_landing_zones_returns_empty_dict_gracefully(self, monkeypatch):
        client = MagicMock()
        client.list_landing_zones.return_value = {"landingZones": []}
        monkeypatch.setattr(controltower_export.utils, "get_boto3_client", lambda *a, **kw: client)

        assert collect_landing_zone() == {}

    def test_access_denied_returns_empty_dict_gracefully(self, monkeypatch):
        client = MagicMock()
        client.list_landing_zones.side_effect = _client_error(
            "AccessDeniedException", "ListLandingZones"
        )
        monkeypatch.setattr(controltower_export.utils, "get_boto3_client", lambda *a, **kw: client)

        assert collect_landing_zone() == {}

    def test_resource_not_found_returns_empty_dict_gracefully(self, monkeypatch):
        client = MagicMock()
        client.list_landing_zones.side_effect = _client_error(
            "ResourceNotFoundException", "ListLandingZones"
        )
        monkeypatch.setattr(controltower_export.utils, "get_boto3_client", lambda *a, **kw: client)

        assert collect_landing_zone() == {}

    def test_landing_zone_details_collected_successfully(self, monkeypatch):
        client = MagicMock()
        client.list_landing_zones.return_value = {
            "landingZones": [{"arn": "arn:aws:controltower::123456789012:landingzone/LZ1"}]
        }
        client.get_landing_zone.return_value = {
            "landingZone": {
                "arn": "arn:aws:controltower::123456789012:landingzone/LZ1",
                "version": "3.3",
                "status": "ACTIVE",
                "driftStatus": {"status": "IN_SYNC"},
                "manifest": {"governedRegions": ["us-east-1", "us-west-2"]},
            }
        }
        monkeypatch.setattr(controltower_export.utils, "get_boto3_client", lambda *a, **kw: client)

        result = collect_landing_zone()

        assert result["Status"] == "ACTIVE"
        assert result["Number of Governed Regions"] == 2


class TestCollectOrganizationalUnits:
    """collect_organizational_units() is a PRIMARY, account-scope collector."""

    def test_org_not_in_use_returns_empty_list_gracefully(self, monkeypatch):
        client = MagicMock()
        client.list_roots.side_effect = _client_error(
            "AWSOrganizationsNotInUseException", "ListRoots"
        )
        monkeypatch.setattr(controltower_export.utils, "get_boto3_client", lambda *a, **kw: client)

        assert collect_organizational_units() == []

    def test_access_denied_returns_empty_list_gracefully(self, monkeypatch):
        client = MagicMock()
        client.list_roots.side_effect = _client_error("AccessDeniedException", "ListRoots")
        monkeypatch.setattr(controltower_export.utils, "get_boto3_client", lambda *a, **kw: client)

        assert collect_organizational_units() == []


class TestSilentCollectionFailureRegression:
    """
    Regression tests for the 07.15.2026 / 07.16.2026 silent-collection-
    failure audits, applied to Control Tower. Before this fix,
    ``collect_landing_zone()``, ``collect_organizational_units()``, and
    ``collect_enabled_controls()`` were all decorated with
    ``@utils.aws_error_handler(default_return=...)``, silently collapsing a
    real API error into an empty result indistinguishable from "Control
    Tower not set up" / "no OUs" / "no controls". ``main()`` also had no way
    to signal that failure downstream (it wrote no file at all when
    ``collect_landing_zone()`` came back empty, for ANY reason).

    Covers:
        (a) a malformed item (OU / control) is skipped, not fatal
        (b) a PRIMARY collector raises rather than swallowing a real API error
        (c) a collector failure surfaces through main() as a non-zero exit
            plus a utils.report_collection_failures() call
        (d) a genuine "Control Tower not set up" state is a graceful exit 0
            with no failure marker
    """

    # -- (a) Per-item guards -------------------------------------------------

    def test_malformed_ou_is_skipped_not_fatal(self, monkeypatch):
        """One OU entry that fails to process must not discard its sibling
        or abort the whole recursive branch."""
        client = MagicMock()
        client.list_roots.return_value = {
            "Roots": [
                {"Id": "r-root", "Arn": "arn:aws:organizations::123456789012:root/o-abc/r-root", "Name": "Root"}
            ]
        }

        good = {"Id": "ou-good", "Arn": "arn:aws:organizations::123456789012:ou/o-abc/ou-good", "Name": "good-ou"}
        bad = {"Id": "ou-bad", "Arn": "arn:aws:organizations::123456789012:ou/o-abc/ou-bad", "Name": "bad-ou"}

        paginator = MagicMock()

        def paginate(ParentId, **kwargs):  # noqa: N803 (matches boto3 kwarg casing)
            if ParentId == "r-root":
                return [{"OrganizationalUnits": [good, bad]}]
            return []

        paginator.paginate.side_effect = paginate
        client.get_paginator.return_value = paginator
        monkeypatch.setattr(controltower_export.utils, "get_boto3_client", lambda *a, **kw: client)

        original = controltower_export._build_ou_row

        def raise_for_bad(ou):
            if ou.get("Id") == "ou-bad":
                raise KeyError("SomeUnexpectedField")
            return original(ou)

        monkeypatch.setattr(controltower_export, "_build_ou_row", raise_for_bad)

        result = collect_organizational_units()

        ids = {row["OU ID"] for row in result}
        assert "ou-good" in ids, "healthy OU was lost when a sibling failed"
        assert "ou-bad" not in ids, "malformed OU should have been skipped"

    def test_malformed_control_is_skipped_not_fatal(self, monkeypatch):
        """One control entry that fails to process must not discard its
        sibling or abort the whole OU's control listing."""
        ct_client = MagicMock()
        catalog_client = MagicMock()

        good = {"controlIdentifier": "AWS-GR_GOOD", "arn": "arn:aws:controltower::123456789012:control/good"}
        bad = {"controlIdentifier": "AWS-GR_BAD", "arn": "arn:aws:controltower::123456789012:control/bad"}

        paginator = MagicMock()
        paginator.paginate.return_value = [{"enabledControls": [good, bad]}]
        ct_client.get_paginator.return_value = paginator
        ct_client.get_enabled_control.return_value = {"enabledControlDetails": {}}
        catalog_client.get_control.return_value = {"Name": "N", "Description": "D", "Behavior": "B"}

        def fake_get_boto3_client(service, *a, **kw):
            return ct_client if service == "controltower" else catalog_client

        monkeypatch.setattr(controltower_export.utils, "get_boto3_client", fake_get_boto3_client)

        original = controltower_export._build_control_row

        def raise_for_bad(control, ou_name, ou_arn, ctc, catc):
            if control.get("controlIdentifier") == "AWS-GR_BAD":
                raise KeyError("SomeUnexpectedField")
            return original(control, ou_name, ou_arn, ctc, catc)

        monkeypatch.setattr(controltower_export, "_build_control_row", raise_for_bad)

        ous = [{"OU ARN": "arn:aws:organizations::123456789012:ou/o-abc/ou-1", "OU Name": "ou-1", "Type": "Organizational Unit"}]

        result = collect_enabled_controls(ous)

        ids = {row["Control Identifier"] for row in result}
        assert "AWS-GR_GOOD" in ids, "healthy control was lost when a sibling failed"
        assert "AWS-GR_BAD" not in ids, "malformed control should have been skipped"

    def test_controls_survive_sdk_without_controlcatalog(self, monkeypatch):
        """Issue #213: below the SDK floor, creating the controlcatalog client
        raises UnknownServiceError. Controls must still be listed, with the
        catalog columns carrying a stated 'Unavailable' value, not crash."""
        ct_client = MagicMock()
        control = {"controlIdentifier": "arn:aws:controltower:us-east-1::control/AWS-GR_S3_X",
                   "arn": "arn:aws:controltower::123456789012:control/x"}
        paginator = MagicMock()
        paginator.paginate.return_value = [{"enabledControls": [control]}]
        ct_client.get_paginator.return_value = paginator
        ct_client.get_enabled_control.return_value = {"enabledControlDetails": {}}

        def fake_get_boto3_client(service, *a, **kw):
            if service == "controlcatalog":
                raise botocore.exceptions.UnknownServiceError(
                    service_name="controlcatalog", known_service_names="controltower"
                )
            return ct_client

        monkeypatch.setattr(controltower_export.utils, "get_boto3_client", fake_get_boto3_client)
        ous = [{"OU ARN": "arn:aws:organizations::123456789012:ou/o-abc/ou-1", "OU Name": "ou-1", "Type": "Organizational Unit"}]

        rows = collect_enabled_controls(ous)

        assert len(rows) == 1
        assert rows[0]["Control Identifier"] == control["controlIdentifier"]
        assert rows[0]["Description"] == controltower_export.CATALOG_UNAVAILABLE
        assert rows[0]["Behavior"] == controltower_export.CATALOG_UNAVAILABLE

    # -- (b) Account-scope failure propagation --------------------------------

    def test_collect_landing_zone_raises_on_real_api_error_not_swallowed(self, monkeypatch):
        """A real Control Tower API error must propagate, not collapse into
        'not set up'."""
        client = MagicMock()
        client.list_landing_zones.side_effect = _client_error(
            "InternalErrorException", "ListLandingZones"
        )
        monkeypatch.setattr(controltower_export.utils, "get_boto3_client", lambda *a, **kw: client)

        with pytest.raises(botocore.exceptions.ClientError):
            collect_landing_zone()

    def test_collect_landing_zone_raises_on_get_details_error_not_swallowed(self, monkeypatch):
        """A landing zone genuinely exists (list succeeded) but fetching its
        details fails -- a real failure, not 'not set up'."""
        client = MagicMock()
        client.list_landing_zones.return_value = {
            "landingZones": [{"arn": "arn:aws:controltower::123456789012:landingzone/LZ1"}]
        }
        client.get_landing_zone.side_effect = _client_error(
            "InternalErrorException", "GetLandingZone"
        )
        monkeypatch.setattr(controltower_export.utils, "get_boto3_client", lambda *a, **kw: client)

        with pytest.raises(botocore.exceptions.ClientError):
            collect_landing_zone()

    def test_collect_organizational_units_raises_on_real_api_error_not_swallowed(self, monkeypatch):
        client = MagicMock()
        client.list_roots.side_effect = _client_error("InternalErrorException", "ListRoots")
        monkeypatch.setattr(controltower_export.utils, "get_boto3_client", lambda *a, **kw: client)

        with pytest.raises(botocore.exceptions.ClientError):
            collect_organizational_units()

    def test_collect_enabled_controls_raises_on_client_creation_error_not_swallowed(self, monkeypatch):
        """A real error creating the account-scope clients (e.g. the
        botocore version in use not recognizing 'controlcatalog') must
        propagate, not collapse into an empty controls list."""

        def boom(service, *a, **kw):
            raise RuntimeError(f"Unknown service: {service}")

        monkeypatch.setattr(controltower_export.utils, "get_boto3_client", boom)

        ous = [{"OU ARN": "arn:aws:organizations::123456789012:ou/o-abc/ou-1", "OU Name": "ou-1", "Type": "Organizational Unit"}]

        with pytest.raises(RuntimeError):
            collect_enabled_controls(ous)

    # -- (c) main(): failure surfaced, non-zero exit ---------------------------

    def _patch_main_scaffolding(self, monkeypatch):
        """Patch everything main() needs to run except the collectors under test."""
        monkeypatch.setattr(controltower_export.utils, "ensure_dependencies", lambda *a, **kw: True)
        monkeypatch.setattr(controltower_export.utils, "setup_logging", lambda *a, **kw: None)
        monkeypatch.setattr(controltower_export.utils, "log_script_start", lambda *a, **kw: None)
        monkeypatch.setattr(controltower_export.utils, "logger", _NULL_LOGGER)
        monkeypatch.setattr(
            controltower_export.utils,
            "print_script_banner",
            lambda *a, **kw: ("123456789012", "test-account"),
        )
        monkeypatch.setattr(controltower_export.utils, "mask_account_id", lambda account_id: "1234****9012")
        # detect_partition() with no args makes a real boto3.Session().client('sts')
        # call outside utils.get_boto3_client -- avoid any real network dependency.
        monkeypatch.setattr(controltower_export.utils, "detect_partition", lambda *a, **kw: "aws")

    def test_main_landing_zone_failure_exits_nonzero_and_reports(self, monkeypatch):
        """A landing-zone API failure in main() must exit non-zero and call
        utils.report_collection_failures -- never silently collapse into
        'not set up'."""
        self._patch_main_scaffolding(monkeypatch)

        def boom():
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "InternalErrorException", "Message": "Something broke"}},
                "ListLandingZones",
            )

        monkeypatch.setattr(controltower_export, "collect_landing_zone", boom)
        monkeypatch.setattr(controltower_export, "collect_organizational_units", lambda: [])
        monkeypatch.setattr(controltower_export, "collect_enabled_controls", lambda ous: [])

        calls = {}

        def fake_report(account_name, resource_type, failed_scopes):
            calls["account_name"] = account_name
            calls["resource_type"] = resource_type
            calls["failed_scopes"] = failed_scopes
            return "fake-marker.txt"

        monkeypatch.setattr(controltower_export.utils, "report_collection_failures", fake_report)

        with pytest.raises(SystemExit) as exc_info:
            controltower_export.main()

        assert exc_info.value.code == 1
        assert calls.get("account_name") == "test-account"
        assert calls.get("resource_type") == "controltower"
        assert calls.get("failed_scopes")
        assert calls["failed_scopes"][0][0] == "landing-zone"

    def test_main_enabled_controls_failure_exits_nonzero_and_reports(self, monkeypatch):
        """An enabled-controls failure in main() (landing zone + OUs both
        succeeded) must still exit non-zero and report -- a partial export
        that looks complete must never mask a failed scope."""
        self._patch_main_scaffolding(monkeypatch)

        monkeypatch.setattr(
            controltower_export,
            "collect_landing_zone",
            lambda: {"ARN": "arn:...", "Status": "ACTIVE", "Number of Governed Regions": 1, "Governed Regions": "us-east-1"},
        )
        monkeypatch.setattr(controltower_export, "collect_organizational_units", lambda: [])

        def boom(ous):
            raise RuntimeError("Unknown service: controlcatalog")

        monkeypatch.setattr(controltower_export, "collect_enabled_controls", boom)

        calls = {}

        def fake_report(account_name, resource_type, failed_scopes):
            calls["failed_scopes"] = failed_scopes
            return "fake-marker.txt"

        monkeypatch.setattr(controltower_export.utils, "report_collection_failures", fake_report)

        with pytest.raises(SystemExit) as exc_info:
            controltower_export.main()

        assert exc_info.value.code == 1
        assert calls.get("failed_scopes")
        assert calls["failed_scopes"][0][0] == "enabled-controls"

    # -- (d) Control Tower not set up: graceful skip, no marker ---------------

    def test_not_set_up_exits_zero_no_marker(self, monkeypatch):
        """Control Tower not being set up in this account (no landing zone
        found, and no error) is a legitimate, expected state -- exit 0 (via
        a plain return), and utils.report_collection_failures is never
        called (no *-FAILED-*.txt marker is written)."""
        self._patch_main_scaffolding(monkeypatch)

        monkeypatch.setattr(controltower_export, "collect_landing_zone", lambda: {})

        # These must not even be reached, but stub them defensively.
        monkeypatch.setattr(
            controltower_export,
            "collect_organizational_units",
            lambda: pytest.fail("collect_organizational_units should not run when CT is not set up"),
        )
        monkeypatch.setattr(
            controltower_export,
            "collect_enabled_controls",
            lambda ous: pytest.fail("collect_enabled_controls should not run when CT is not set up"),
        )

        report_called = []
        monkeypatch.setattr(
            controltower_export.utils,
            "report_collection_failures",
            lambda *a, **kw: report_called.append((a, kw)),
        )

        result = controltower_export.main()

        assert result is None
        assert report_called == []

    def test_partial_success_with_no_failures_exits_cleanly_with_summary(self, monkeypatch, tmp_path):
        """A fully successful run (no failed scopes) writes a file (with a
        Summary sheet always present) and does not call
        report_collection_failures or exit non-zero."""
        self._patch_main_scaffolding(monkeypatch)

        monkeypatch.setattr(
            controltower_export,
            "collect_landing_zone",
            lambda: {
                "ARN": "arn:aws:controltower::123456789012:landingzone/LZ1",
                "Version": "3.3",
                "Status": "ACTIVE",
                "Drift Status": "IN_SYNC",
                "Governed Regions": "us-east-1",
                "Number of Governed Regions": 1,
            },
        )
        monkeypatch.setattr(controltower_export, "collect_organizational_units", lambda: [])
        monkeypatch.setattr(controltower_export, "collect_enabled_controls", lambda ous: [])

        report_called = []
        monkeypatch.setattr(
            controltower_export.utils,
            "report_collection_failures",
            lambda *a, **kw: report_called.append((a, kw)),
        )

        result = controltower_export.main()

        assert result is None
        assert report_called == []
        exported_files = list(tmp_path.glob("test-account-controltower-*"))
        assert exported_files, "expected an exported workbook with the always-written Summary sheet"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
