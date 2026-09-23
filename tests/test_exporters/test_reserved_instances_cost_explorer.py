#!/usr/bin/env python3
"""
Tests for the Reserved Instances Cost Explorer utilization / coverage queries
(Issue #285).

moto implements neither GetReservationUtilization nor GetReservationCoverage,
so these use botocore's Stubber on a real ``ce`` client: every request
parameter and canned response is validated against the installed botocore
model. Covers NextPageToken pagination, per-service outcomes (data / no
data / DataUnavailable / AccessDenied), disabled and GovCloud (never queried),
and the _run_export wiring (failure -> non-zero exit + marker).
"""

import datetime
import sys
from pathlib import Path

import boto3
import pandas as pd
import pytest
from botocore.stub import Stubber

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import reserved_instances_export as rix  # noqa: E402

import utils  # noqa: E402

TODAY = datetime.date(2026, 9, 23)
# months=1 on TODAY -> August 2026 only.
PERIOD = {"Start": "2026-08-01", "End": "2026-09-01"}
SERVICES = [value for _, value in rix.RI_CE_SERVICES]


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv(utils.CE_UTILIZATION_ENV_VAR, raising=False)


@pytest.fixture
def ce_client(monkeypatch):
    client = boto3.client("ce", region_name="us-east-1")
    monkeypatch.setattr(rix.utils, "get_boto3_client", lambda *a, **kw: client)
    return client


def _settings(enabled=True, months=1):
    return {"enabled": enabled, "lookback_months": months, "source": "test"}


def _util_params(service, token=None):
    params = {
        "TimePeriod": PERIOD,
        "Granularity": "MONTHLY",
        "Filter": {"Dimensions": {"Key": "SERVICE", "Values": [service]}},
    }
    if token:
        params["NextPageToken"] = token
    return params


def _cov_params(service, token=None):
    params = _util_params(service, token)
    params["Metrics"] = ["Hour", "Unit", "Cost"]
    return params


def _util_entry(pct="100"):
    return {
        "TimePeriod": PERIOD,
        "Total": {
            "UtilizationPercentage": pct,
            "PurchasedHours": "744",
            "TotalActualHours": "744",
            "UnusedHours": "0",
            "NetRISavings": "123.45",
            "TotalAmortizedFee": "50.00",
        },
    }


def _cov_entry(pct="40"):
    return {
        "TimePeriod": PERIOD,
        "Total": {
            "CoverageHours": {
                "CoverageHoursPercentage": pct,
                "OnDemandHours": "40",
                "ReservedHours": "40",
                "TotalRunningHours": "80",
            },
            "CoverageCost": {"OnDemandCost": "12.34"},
        },
    }


def _no_client(*_a, **_kw):
    raise AssertionError("Cost Explorer client must not be created in this state")


class TestNotQueried:
    def test_disabled(self, monkeypatch):
        monkeypatch.setattr(rix.utils, "get_boto3_client", _no_client)
        result = rix.collect_ri_cost_explorer("aws", _settings(enabled=False), TODAY)
        for key in ("utilization", "coverage"):
            assert {o["status"] for o in result[key]} == {utils.CE_STATUS_DISABLED}
            assert len(result[key]) == len(rix.RI_CE_SERVICES)
        assert result["requests"] == 0 and result["failed_scopes"] == []
        sheets = rix.build_ri_cost_explorer_sheets(result)
        util = sheets[rix.SHEET_CE_UTILIZATION]
        assert set(util["Status"]) == {utils.CE_STATUS_DISABLED}
        assert utils.CE_UTILIZATION_ENV_VAR in util.iloc[0]["Detail"]

    def test_govcloud(self, monkeypatch):
        monkeypatch.setattr(rix.utils, "get_boto3_client", _no_client)
        result = rix.collect_ri_cost_explorer("aws-us-gov", _settings(enabled=True), TODAY)
        for key in ("utilization", "coverage"):
            assert {o["status"] for o in result[key]} == {utils.CE_STATUS_GOVCLOUD}
        assert result["failed_scopes"] == []


class TestQueried:
    def test_pagination_data_and_no_data_per_service(self, ce_client):
        with Stubber(ce_client) as stub:
            # Utilization: EC2 split across two pages; others empty (one has
            # a month with an empty Total, which is "nothing", not a row).
            stub.add_response(
                "get_reservation_utilization",
                {"UtilizationsByTime": [_util_entry("97.5")], "NextPageToken": "p2"},
                _util_params(SERVICES[0]),
            )
            stub.add_response(
                "get_reservation_utilization",
                {"UtilizationsByTime": [_util_entry("99")]},
                _util_params(SERVICES[0], "p2"),
            )
            stub.add_response(
                "get_reservation_utilization",
                {"UtilizationsByTime": [{"TimePeriod": PERIOD, "Total": {}}]},
                _util_params(SERVICES[1]),
            )
            for svc in SERVICES[2:]:
                stub.add_response("get_reservation_utilization", {"UtilizationsByTime": []},
                                  _util_params(svc))
            # Coverage: EC2 data, RDS DataUnavailable, rest empty.
            stub.add_response("get_reservation_coverage", {"CoveragesByTime": [_cov_entry()]},
                              _cov_params(SERVICES[0]))
            stub.add_client_error("get_reservation_coverage", "DataUnavailableException",
                                  "no data", 400, expected_params=_cov_params(SERVICES[1]))
            for svc in SERVICES[2:]:
                stub.add_response("get_reservation_coverage", {"CoveragesByTime": []},
                                  _cov_params(svc))

            result = rix.collect_ri_cost_explorer("aws", _settings(), TODAY)
            stub.assert_no_pending_responses()

        util = {o["service"]: o for o in result["utilization"]}
        assert util["EC2"]["status"] == utils.CE_STATUS_DATA
        assert [r["Utilization %"] for r in util["EC2"]["rows"]] == [97.5, 99.0], "page 2 lost"
        assert util["RDS"]["status"] == utils.CE_STATUS_NO_DATA
        assert util["Redshift"]["status"] == utils.CE_STATUS_NO_DATA

        cov = {o["service"]: o for o in result["coverage"]}
        assert cov["EC2"]["status"] == utils.CE_STATUS_DATA
        row = cov["EC2"]["rows"][0]
        assert row["Coverage Hours %"] == 40.0 and row["On-Demand Cost"] == 12.34
        assert cov["RDS"]["status"] == utils.CE_STATUS_UNAVAILABLE

        assert result["failed_scopes"] == []
        assert result["requests"] == 9

        sheets = rix.build_ri_cost_explorer_sheets(result)
        util_df = sheets[rix.SHEET_CE_UTILIZATION]
        # 2 EC2 data rows + one stated row for each of the 3 other services.
        assert len(util_df) == 5
        assert set(util_df[util_df["Service"] != "EC2"]["Status"]) == {utils.CE_STATUS_NO_DATA}
        status_df = sheets[rix.SHEET_CE_STATUS]
        assert "Not queried" in set(status_df["Field"])

    def test_access_denied_stops_that_operation_but_tries_the_other(self, ce_client):
        with Stubber(ce_client) as stub:
            stub.add_client_error("get_reservation_utilization", "AccessDeniedException",
                                  "not authorized", 400)
            for svc in SERVICES:
                stub.add_response("get_reservation_coverage", {"CoveragesByTime": []},
                                  _cov_params(svc))
            result = rix.collect_ri_cost_explorer("aws", _settings(), TODAY)
            stub.assert_no_pending_responses()

        util = result["utilization"]
        assert all(o["status"] == utils.CE_STATUS_FAILED for o in util)
        assert "AccessDeniedException" in util[0]["detail"]
        assert all("Not attempted after denial" in o["detail"] for o in util[1:])
        assert {o["status"] for o in result["coverage"]} == {utils.CE_STATUS_NO_DATA}
        assert len(result["failed_scopes"]) == 1
        assert result["requests"] == 1 + len(SERVICES)


class TestRunExportWiring:
    @pytest.fixture(autouse=True)
    def _wiring(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rix, "pd", pd, raising=False)  # bound in main() in production
        monkeypatch.setattr(rix.utils, "get_output_dir", lambda: tmp_path)
        for name in ("ec2", "rds", "elasticache", "opensearch", "redshift", "memorydb"):
            monkeypatch.setattr(rix, f"collect_{name}_reserved_instances", lambda regions: ([], []))
        self.saved = {}

        def fake_save(frames, filename, *a, **kw):
            self.saved.update(frames)
            return str(tmp_path / "out.xlsx")

        monkeypatch.setattr(rix.utils, "save_multiple_dataframes_to_excel", fake_save)
        self.reported = []
        monkeypatch.setattr(
            rix.utils, "report_collection_failures",
            lambda account, rtype, scopes: self.reported.append(scopes) or "marker.txt",
        )

    def test_disabled_writes_stated_sheets_and_exits_clean(self, monkeypatch):
        monkeypatch.setenv(utils.CE_UTILIZATION_ENV_VAR, "0")
        monkeypatch.setattr(rix.utils, "get_boto3_client", _no_client)
        rix._run_export("123456789012", "test-account", ["us-east-1"])
        assert set(self.saved[rix.SHEET_CE_UTILIZATION]["Status"]) == {utils.CE_STATUS_DISABLED}
        assert self.reported == []

    def test_govcloud_region_skips_without_failure(self, monkeypatch):
        monkeypatch.setenv(utils.CE_UTILIZATION_ENV_VAR, "1")
        monkeypatch.setattr(rix.utils, "get_boto3_client", _no_client)
        rix._run_export("123456789012", "test-account", ["us-gov-west-1"])
        assert set(self.saved[rix.SHEET_CE_COVERAGE]["Status"]) == {utils.CE_STATUS_GOVCLOUD}
        assert self.reported == []

    def test_enabled_failure_exits_nonzero(self, monkeypatch, ce_client):
        monkeypatch.setenv(utils.CE_UTILIZATION_ENV_VAR, "1")
        with Stubber(ce_client) as stub:
            stub.add_client_error("get_reservation_utilization", "AccessDeniedException", "no", 400)
            stub.add_client_error("get_reservation_coverage", "AccessDeniedException", "no", 400)
            with pytest.raises(SystemExit) as exc:
                rix._run_export("123456789012", "test-account", ["us-east-1"])
        assert exc.value.code == 1
        assert len(self.reported[0]) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
