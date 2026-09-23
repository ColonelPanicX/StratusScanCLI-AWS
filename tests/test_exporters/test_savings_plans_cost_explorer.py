#!/usr/bin/env python3
"""
Tests for the Savings Plans Cost Explorer utilization queries (Issue #285).

moto implements none of the Cost Explorer utilization operations, so these use
botocore's Stubber on a real ``ce`` client. Stubber validates every request
parameter and every canned response against the installed botocore service
model, so a misspelled parameter or response field fails here rather than in
the field.

Covers each stated outcome: data returned, AWS returned nothing,
DataUnavailableException, disabled (never queried), GovCloud (never queried),
and a failed lookup (AccessDenied), plus NextToken pagination and the shared
settings / month-window helpers in utils.
"""

import datetime
import sys
from pathlib import Path

import boto3
import botocore.exceptions
import pytest
from botocore.stub import Stubber

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import savings_plans_export as spx  # noqa: E402

import utils  # noqa: E402

TODAY = datetime.date(2026, 9, 23)
ARN_A = "arn:aws:savingsplans::123456789012:savingsplan/aaaa-1111"
ARN_B = "arn:aws:savingsplans::123456789012:savingsplan/bbbb-2222"


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv(utils.CE_UTILIZATION_ENV_VAR, raising=False)


@pytest.fixture
def ce_stub(monkeypatch):
    client = boto3.client("ce", region_name="us-east-1")
    stubber = Stubber(client)
    monkeypatch.setattr(spx.utils, "get_boto3_client", lambda *a, **kw: client)
    with stubber:
        yield stubber
    stubber.assert_no_pending_responses()


def _settings(enabled=True, months=2):
    return {"enabled": enabled, "lookback_months": months, "source": "test"}


def _util_block(pct="98.5"):
    return {
        "Utilization": {
            "TotalCommitment": "52696.03",
            "UsedCommitment": "51905.59",
            "UnusedCommitment": "790.44",
            "UtilizationPercentage": pct,
        },
        "Savings": {"NetSavings": "10000.00", "OnDemandCostEquivalent": "62696.03"},
        "AmortizedCommitment": {
            "AmortizedRecurringCommitment": "52696.03",
            "AmortizedUpfrontCommitment": "0",
            "TotalAmortizedCommitment": "52696.03",
        },
    }


def _no_client(*_a, **_kw):
    raise AssertionError("Cost Explorer client must not be created in this state")


# ---------------------------------------------------------------------------
# utils helpers
# ---------------------------------------------------------------------------


class TestMonthWindow:
    def test_twelve_complete_months_end_exclusive(self):
        periods = utils.cost_explorer_month_window(12, TODAY)
        assert len(periods) == 12
        assert periods[0] == ("2025-09-01", "2025-10-01")
        assert periods[-1] == ("2026-08-01", "2026-09-01")

    def test_year_wrap(self):
        assert utils.cost_explorer_month_window(1, datetime.date(2026, 1, 5)) == [
            ("2025-12-01", "2026-01-01")
        ]

    def test_clamped_to_twelve(self):
        assert len(utils.cost_explorer_month_window(40, TODAY)) == 12


class TestSettings:
    def _config(self, monkeypatch, cost_explorer):
        monkeypatch.setattr(
            utils, "get_config",
            lambda: ({}, {"advanced_settings": {"cost_explorer": cost_explorer}}),
        )

    def test_default_is_disabled(self, monkeypatch):
        monkeypatch.setattr(utils, "get_config", lambda: ({}, {}))
        s = utils.cost_explorer_utilization_settings()
        assert s["enabled"] is False
        assert s["lookback_months"] == 12

    def test_config_enables(self, monkeypatch):
        self._config(monkeypatch, {"utilization_enabled": True, "lookback_months": 6})
        s = utils.cost_explorer_utilization_settings()
        assert s["enabled"] is True and s["lookback_months"] == 6
        assert "config.json" in s["source"]

    def test_env_overrides_config_both_ways(self, monkeypatch):
        self._config(monkeypatch, {"utilization_enabled": True})
        monkeypatch.setenv(utils.CE_UTILIZATION_ENV_VAR, "0")
        assert utils.cost_explorer_utilization_settings()["enabled"] is False
        self._config(monkeypatch, {"utilization_enabled": False})
        monkeypatch.setenv(utils.CE_UTILIZATION_ENV_VAR, "1")
        s = utils.cost_explorer_utilization_settings()
        assert s["enabled"] is True and utils.CE_UTILIZATION_ENV_VAR in s["source"]

    def test_bad_months_fall_back(self, monkeypatch):
        self._config(monkeypatch, {"utilization_enabled": True, "lookback_months": 99})
        assert utils.cost_explorer_utilization_settings()["lookback_months"] == 12

    def test_non_bool_enabled_ignored(self, monkeypatch):
        self._config(monkeypatch, {"utilization_enabled": "yes"})
        assert utils.cost_explorer_utilization_settings()["enabled"] is False


# ---------------------------------------------------------------------------
# Never-queried states
# ---------------------------------------------------------------------------


class TestNotQueried:
    def test_disabled_makes_no_call_and_says_how_to_enable(self, monkeypatch):
        monkeypatch.setattr(spx.utils, "get_boto3_client", _no_client)
        result = spx.collect_sp_cost_explorer("aws", _settings(enabled=False), {}, TODAY)
        for key in ("monthly", "per_plan"):
            assert result[key]["status"] == utils.CE_STATUS_DISABLED
            assert utils.CE_UTILIZATION_ENV_VAR in result[key]["detail"]
            assert "advanced_settings.py" in result[key]["detail"]
        assert result["requests"] == 0
        assert result["failed_scopes"] == []

        sheets = spx.build_sp_cost_explorer_sheets(result)
        for name in (spx.SHEET_CE_MONTHLY, spx.SHEET_CE_PER_PLAN):
            df = sheets[name]
            assert list(df.columns) == ["Status", "Detail"]
            assert df.iloc[0]["Status"] == utils.CE_STATUS_DISABLED
        assert spx.SHEET_CE_STATUS in sheets

    def test_govcloud_skips_even_when_enabled(self, monkeypatch):
        monkeypatch.setattr(spx.utils, "get_boto3_client", _no_client)
        result = spx.collect_sp_cost_explorer("aws-us-gov", _settings(enabled=True), {}, TODAY)
        for key in ("monthly", "per_plan"):
            assert result[key]["status"] == utils.CE_STATUS_GOVCLOUD
        assert result["failed_scopes"] == []
        assert result["requests"] == 0


# ---------------------------------------------------------------------------
# Queried states
# ---------------------------------------------------------------------------


class TestQueried:
    def test_data_with_pagination_and_inventory_join(self, ce_stub):
        # Window for months=2 on TODAY: 2026-07 and 2026-08.
        ce_stub.add_response(
            "get_savings_plans_utilization",
            {
                "SavingsPlansUtilizationsByTime": [
                    dict(TimePeriod={"Start": "2026-07-01", "End": "2026-08-01"}, **_util_block("97.0")),
                    dict(TimePeriod={"Start": "2026-08-01", "End": "2026-09-01"}, **_util_block("98.5")),
                ],
                "Total": _util_block("97.75"),
            },
            {
                "TimePeriod": {"Start": "2026-07-01", "End": "2026-09-01"},
                "Granularity": "MONTHLY",
            },
        )
        july = {"TimePeriod": {"Start": "2026-07-01", "End": "2026-08-01"},
                "DataType": spx._DETAIL_DATA_TYPES}
        # July: two pages (NextToken followed).
        ce_stub.add_response(
            "get_savings_plans_utilization_details",
            {
                "SavingsPlansUtilizationDetails": [
                    dict(SavingsPlanArn=ARN_A, Attributes={"PaymentOption": "No Upfront"}, **_util_block("97.0")),
                ],
                "NextToken": "page-2",
                "TimePeriod": july["TimePeriod"],
            },
            july,
        )
        ce_stub.add_response(
            "get_savings_plans_utilization_details",
            {
                "SavingsPlansUtilizationDetails": [
                    dict(SavingsPlanArn=ARN_B, **_util_block("50.0")),
                ],
                "TimePeriod": july["TimePeriod"],
            },
            dict(july, NextToken="page-2"),
        )
        ce_stub.add_response(
            "get_savings_plans_utilization_details",
            {
                "SavingsPlansUtilizationDetails": [
                    dict(SavingsPlanArn=ARN_A, **_util_block("98.5")),
                ],
                "TimePeriod": {"Start": "2026-08-01", "End": "2026-09-01"},
            },
            {"TimePeriod": {"Start": "2026-08-01", "End": "2026-09-01"},
             "DataType": spx._DETAIL_DATA_TYPES},
        )

        inventory = {ARN_A: {"Savings Plan ID": "aaaa-1111", "Savings Plan Type": "Compute",
                             "Payment Option": "No Upfront", "Hourly Commitment": 70.828}}
        result = spx.collect_sp_cost_explorer("aws", _settings(months=2), inventory, TODAY)

        assert result["monthly"]["status"] == utils.CE_STATUS_DATA
        monthly = result["monthly"]["rows"]
        assert [r["Month"] for r in monthly] == ["2026-07", "2026-08", "TOTAL (period)"]
        assert monthly[1]["Utilization %"] == 98.5
        assert monthly[1]["Unused Commitment"] == 790.44

        assert result["per_plan"]["status"] == utils.CE_STATUS_DATA
        per_plan = result["per_plan"]["rows"]
        assert len(per_plan) == 3, "page 2 of July was not read"
        a_july = next(r for r in per_plan if r["Month"] == "2026-07" and r["Savings Plan ARN"] == ARN_A)
        assert a_july["Savings Plan ID"] == "aaaa-1111"
        assert a_july["Hourly Commitment"] == 70.828
        assert a_july["Attributes"] == "PaymentOption=No Upfront"
        b_july = next(r for r in per_plan if r["Savings Plan ARN"] == ARN_B)
        assert b_july["Savings Plan ID"] == "bbbb-2222"  # from ARN, not in inventory
        assert b_july["Savings Plan Type"] == "Not in active/queued inventory"

        assert result["requests"] == 4
        assert result["failed_scopes"] == []

        sheets = spx.build_sp_cost_explorer_sheets(result)
        assert "Utilization %" in sheets[spx.SHEET_CE_MONTHLY].columns
        assert len(sheets[spx.SHEET_CE_PER_PLAN]) == 3
        status = dict(zip(sheets[spx.SHEET_CE_STATUS]["Field"], sheets[spx.SHEET_CE_STATUS]["Value"]))
        assert status["Cost Explorer API requests issued"] == 4

    def test_no_data(self, ce_stub):
        ce_stub.add_response(
            "get_savings_plans_utilization",
            {"SavingsPlansUtilizationsByTime": [], "Total": {"Utilization": {}}},
        )
        for start, end in (("2026-07-01", "2026-08-01"), ("2026-08-01", "2026-09-01")):
            ce_stub.add_response(
                "get_savings_plans_utilization_details",
                {"SavingsPlansUtilizationDetails": [], "TimePeriod": {"Start": start, "End": end}},
            )
        result = spx.collect_sp_cost_explorer("aws", _settings(months=2), {}, TODAY)
        assert result["monthly"]["status"] == utils.CE_STATUS_NO_DATA
        assert result["per_plan"]["status"] == utils.CE_STATUS_NO_DATA
        assert result["failed_scopes"] == []
        sheets = spx.build_sp_cost_explorer_sheets(result)
        assert sheets[spx.SHEET_CE_MONTHLY].iloc[0]["Status"] == utils.CE_STATUS_NO_DATA

    def test_data_unavailable_is_its_own_state_not_a_failure(self, ce_stub):
        ce_stub.add_client_error(
            "get_savings_plans_utilization", "DataUnavailableException", "No data", 400
        )
        for _ in range(2):
            ce_stub.add_client_error(
                "get_savings_plans_utilization_details", "DataUnavailableException", "No data", 400
            )
        result = spx.collect_sp_cost_explorer("aws", _settings(months=2), {}, TODAY)
        assert result["monthly"]["status"] == utils.CE_STATUS_UNAVAILABLE
        assert result["per_plan"]["status"] == utils.CE_STATUS_UNAVAILABLE
        assert result["failed_scopes"] == []
        assert result["requests"] == 3

    def test_access_denied_fails_loud_and_stops_further_requests(self, ce_stub):
        ce_stub.add_client_error(
            "get_savings_plans_utilization", "AccessDeniedException", "not authorized", 400
        )
        # No details responses queued: a second request would raise
        # StubResponseError and fail the fixture's pending-response check.
        result = spx.collect_sp_cost_explorer("aws", _settings(months=12), {}, TODAY)
        assert result["monthly"]["status"] == utils.CE_STATUS_FAILED
        assert "AccessDeniedException" in result["monthly"]["detail"]
        assert result["per_plan"]["status"] == utils.CE_STATUS_FAILED
        assert "Not attempted" in result["per_plan"]["detail"]
        assert len(result["failed_scopes"]) == 2
        assert result["requests"] == 1

    def test_per_plan_failure_mid_window_discards_partial_rows(self, ce_stub):
        ce_stub.add_response(
            "get_savings_plans_utilization",
            {"SavingsPlansUtilizationsByTime": [], "Total": {"Utilization": {}}},
        )
        ce_stub.add_response(
            "get_savings_plans_utilization_details",
            {"SavingsPlansUtilizationDetails": [dict(SavingsPlanArn=ARN_A, **_util_block())],
             "TimePeriod": {"Start": "2026-07-01", "End": "2026-08-01"}},
        )
        ce_stub.add_client_error(
            "get_savings_plans_utilization_details", "InternalServerException", "boom", 500
        )
        result = spx.collect_sp_cost_explorer("aws", _settings(months=2), {}, TODAY)
        assert result["per_plan"]["status"] == utils.CE_STATUS_FAILED
        assert result["per_plan"]["rows"] == []
        assert result["requests"] == 3
        assert [s for s, _ in result["failed_scopes"]] == ["cost_explorer_sp_utilization_details"]


# ---------------------------------------------------------------------------
# Export wiring
# ---------------------------------------------------------------------------


def _plan_row():
    return spx._build_savings_plan_row({
        "savingsPlanId": "aaaa-1111", "savingsPlanArn": ARN_A, "savingsPlanType": "Compute",
        "paymentOption": "No Upfront", "state": "active", "commitment": "70.828",
    })


class TestExportWiring:
    @pytest.fixture(autouse=True)
    def _wiring(self, monkeypatch, tmp_path):
        monkeypatch.setattr(spx.utils, "get_output_dir", lambda: tmp_path)
        monkeypatch.setattr(spx.utils, "detect_partition", lambda *a, **kw: "aws")
        self.saved = {}

        def fake_save(frames, filename, *a, **kw):
            self.saved.update(frames)
            return str(tmp_path / "out.xlsx")

        monkeypatch.setattr(spx.utils, "save_multiple_dataframes_to_excel", fake_save)

    def test_disabled_run_writes_status_sheets_and_exits_clean(self, monkeypatch):
        monkeypatch.setenv(utils.CE_UTILIZATION_ENV_VAR, "0")
        monkeypatch.setattr(spx.utils, "get_boto3_client", _no_client)
        monkeypatch.setattr(spx, "collect_savings_plans",
                            lambda states: [_plan_row()] if states == ["active"] else [])
        spx.export_savings_plans_data("123456789012", "test-account")
        assert spx.SHEET_CE_MONTHLY in self.saved
        assert self.saved[spx.SHEET_CE_MONTHLY].iloc[0]["Status"] == utils.CE_STATUS_DISABLED

    def test_enabled_lookup_failure_exits_nonzero_with_marker(self, monkeypatch):
        monkeypatch.setenv(utils.CE_UTILIZATION_ENV_VAR, "1")
        client = boto3.client("ce", region_name="us-east-1")
        stubber = Stubber(client)
        stubber.add_client_error("get_savings_plans_utilization", "AccessDeniedException", "no", 400)
        monkeypatch.setattr(spx.utils, "get_boto3_client", lambda *a, **kw: client)
        monkeypatch.setattr(spx, "collect_savings_plans",
                            lambda states: [_plan_row()] if states == ["active"] else [])
        reported = {}
        monkeypatch.setattr(
            spx.utils, "report_collection_failures",
            lambda account, rtype, scopes: reported.update(scopes=scopes) or "marker.txt",
        )
        with stubber, pytest.raises(SystemExit) as exc:
            spx.export_savings_plans_data("123456789012", "test-account")
        assert exc.value.code == 1
        assert {s for s, _ in reported["scopes"]} == {
            "cost_explorer_sp_utilization", "cost_explorer_sp_utilization_details"
        }
        # The inventory still lands; the CE sheet states the failure.
        assert "Active Savings Plans" in self.saved
        assert self.saved[spx.SHEET_CE_MONTHLY].iloc[0]["Status"] == utils.CE_STATUS_FAILED


def test_classify_non_client_error_is_failure():
    status, detail, failure = utils.classify_cost_explorer_error(
        botocore.exceptions.EndpointConnectionError(endpoint_url="https://ce.us-east-1.amazonaws.com")
    )
    assert status == utils.CE_STATUS_FAILED and failure is True
    assert "EndpointConnectionError" in detail


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
