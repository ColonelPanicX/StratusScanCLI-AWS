"""
Unit tests for cost estimation functions (utils.py).

All functions are pure-computation with hardcoded pricing tables;
no moto or AWS credentials required.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import (
    _estimate_excel_size,
    calculate_nat_gateway_monthly_cost,
    estimate_rds_monthly_cost,
    estimate_s3_monthly_cost,
    generate_cost_optimization_recommendations,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeDf:
    """Minimal DataFrame stand-in for _estimate_excel_size tests."""

    def __init__(self, rows: int, cols: int):
        self._rows = rows
        self._cols = cols
        self.columns = list(range(cols))

    def __len__(self):
        return self._rows


# ---------------------------------------------------------------------------
# _estimate_excel_size
# ---------------------------------------------------------------------------


class TestEstimateExcelSize:
    def test_empty_dataframe(self):
        df = _FakeDf(0, 0)
        assert _estimate_excel_size(df) == 0

    def test_single_cell(self):
        df = _FakeDf(1, 1)
        result = _estimate_excel_size(df)
        # 1 cell * 100 bytes + 20% overhead = 120
        assert result == 120

    def test_scales_with_rows_and_columns(self):
        df_small = _FakeDf(10, 5)
        df_large = _FakeDf(100, 5)
        assert _estimate_excel_size(df_large) > _estimate_excel_size(df_small)

    def test_returns_integer(self):
        df = _FakeDf(7, 3)
        result = _estimate_excel_size(df)
        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# estimate_rds_monthly_cost
# ---------------------------------------------------------------------------


class TestEstimateRdsMonthlyCost:
    def test_known_instance_class(self):
        result = estimate_rds_monthly_cost("db.t3.micro", "mysql", 20)
        assert result["total"] > 0
        assert result["instance_cost"] > 0
        assert result["storage_cost"] > 0

    def test_multi_az_doubles_instance_cost(self):
        single = estimate_rds_monthly_cost("db.t3.medium", "postgres", 100, multi_az=False)
        multi = estimate_rds_monthly_cost("db.t3.medium", "postgres", 100, multi_az=True)
        assert pytest.approx(multi["instance_cost"], rel=1e-3) == single["instance_cost"] * 2

    def test_unknown_instance_class_uses_fallback(self):
        result = estimate_rds_monthly_cost("db.x9.unknown", "mysql", 50)
        # Fallback hourly rate is 0.10 → monthly ~73.00
        assert result["instance_cost"] == pytest.approx(0.10 * 730, rel=1e-3)

    def test_gp3_cheaper_than_gp2_for_same_storage(self):
        gp2 = estimate_rds_monthly_cost("db.t3.micro", "mysql", 100, storage_type="gp2")
        gp3 = estimate_rds_monthly_cost("db.t3.micro", "mysql", 100, storage_type="gp3")
        assert gp3["storage_cost"] < gp2["storage_cost"]

    def test_result_has_required_keys(self):
        result = estimate_rds_monthly_cost("db.m5.large", "oracle", 500)
        for key in ("instance_cost", "storage_cost", "total", "multi_az_enabled", "note"):
            assert key in result


# ---------------------------------------------------------------------------
# estimate_s3_monthly_cost
# ---------------------------------------------------------------------------


class TestEstimateS3MonthlyCost:
    def test_standard_storage_basic(self):
        result = estimate_s3_monthly_cost(1000.0)
        # 1000 GB * $0.023 = $23.00
        assert result["storage_cost"] == pytest.approx(23.0, rel=1e-3)

    def test_glacier_cheaper_than_standard(self):
        std = estimate_s3_monthly_cost(1000.0, "STANDARD")
        glc = estimate_s3_monthly_cost(1000.0, "GLACIER")
        assert glc["total"] < std["total"]

    def test_request_cost_nonzero_when_provided(self):
        result = estimate_s3_monthly_cost(100.0, "STANDARD", requests_per_month=1_000_000)
        assert result["request_cost"] > 0

    def test_intelligent_tiering_has_monitoring_cost(self):
        result = estimate_s3_monthly_cost(10_000.0, "INTELLIGENT_TIERING")
        assert result["monitoring_cost"] > 0

    def test_result_has_required_keys(self):
        result = estimate_s3_monthly_cost(500.0)
        for key in ("storage_cost", "request_cost", "monitoring_cost", "total", "storage_class", "note"):
            assert key in result


# ---------------------------------------------------------------------------
# calculate_nat_gateway_monthly_cost
# ---------------------------------------------------------------------------


class TestCalculateNatGatewayMonthlyCost:
    def test_full_month_no_data(self):
        result = calculate_nat_gateway_monthly_cost(730, 0.0)
        # 730 hours * $0.045 = $32.85
        assert result["hourly_cost"] == pytest.approx(32.85, rel=1e-3)
        assert result["data_processing_cost"] == 0.0

    def test_data_processing_cost_scales(self):
        result = calculate_nat_gateway_monthly_cost(730, 1000.0)
        # 1000 GB * $0.045 = $45.00
        assert result["data_processing_cost"] == pytest.approx(45.0, rel=1e-3)

    def test_total_is_sum_of_components(self):
        result = calculate_nat_gateway_monthly_cost(200, 500.0)
        expected = result["hourly_cost"] + result["data_processing_cost"]
        assert result["total"] == pytest.approx(expected, rel=1e-3)

    def test_result_has_required_keys(self):
        result = calculate_nat_gateway_monthly_cost()
        for key in ("hourly_cost", "data_processing_cost", "total", "hours", "data_processed_gb", "warning"):
            assert key in result


# ---------------------------------------------------------------------------
# generate_cost_optimization_recommendations
# ---------------------------------------------------------------------------


class TestGenerateCostOptimizationRecommendations:
    def test_ec2_stopped_instance_recommendation(self):
        recs = generate_cost_optimization_recommendations(
            "ec2", {"state": "stopped", "instance_type": "t3.large", "days_stopped": 30}
        )
        assert any("stopped" in r.lower() for r in recs)

    def test_ec2_t2_upgrade_recommendation(self):
        recs = generate_cost_optimization_recommendations(
            "ec2", {"state": "running", "instance_type": "t2.medium", "days_stopped": 0}
        )
        assert any("t3" in r for r in recs)

    def test_rds_multiaz_nonprod_recommendation(self):
        recs = generate_cost_optimization_recommendations(
            "rds", {"multi_az": True, "environment": "dev", "backup_retention_period": 3}
        )
        assert any("multi-az" in r.lower() or "single-az" in r.lower() for r in recs)

    def test_s3_cold_data_recommendation(self):
        recs = generate_cost_optimization_recommendations(
            "s3", {"storage_class": "STANDARD", "size_gb": 100, "days_since_last_access": 120}
        )
        assert any("glacier" in r.lower() or "standard_ia" in r.lower() for r in recs)

    def test_unknown_resource_type_returns_default(self):
        recs = generate_cost_optimization_recommendations("unknown_service", {})
        assert len(recs) == 1
        assert "no specific" in recs[0].lower()
