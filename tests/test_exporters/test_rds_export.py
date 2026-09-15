#!/usr/bin/env python3
"""
Moto-based tests for rds_export.py.

Covers:
- get_rds_instances()
"""

import sys
from pathlib import Path

import boto3
import botocore
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import rds_export  # noqa: E402
from rds_export import get_rds_instances  # noqa: E402

REGION = "us-east-1"


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


class TestGetRdsInstances:
    """Tests for get_rds_instances()."""

    @mock_aws
    def test_created_instance_appears_in_results(self):
        """A newly created RDS instance is returned by the collector."""
        rds = boto3.client("rds", region_name=REGION)
        rds.create_db_instance(
            DBInstanceIdentifier="test-db-instance",
            DBInstanceClass="db.t3.micro",
            Engine="mysql",
            MasterUsername="admin",
            MasterUserPassword="password123",
            AllocatedStorage=20,
        )

        result = get_rds_instances(REGION)

        assert isinstance(result, list)
        assert len(result) >= 1
        assert any(row["DB Identifier"] == "test-db-instance" for row in result)

    @mock_aws
    def test_result_contains_expected_columns(self):
        """Each row contains the expected column keys."""
        rds = boto3.client("rds", region_name=REGION)
        rds.create_db_instance(
            DBInstanceIdentifier="col-check-db",
            DBInstanceClass="db.t3.micro",
            Engine="postgres",
            MasterUsername="admin",
            MasterUserPassword="password123",
            AllocatedStorage=20,
        )

        result = get_rds_instances(REGION)

        assert len(result) >= 1
        row = result[0]
        for col in ("DB Identifier", "Engine", "Region", "Size"):
            assert col in row, f"Missing column: {col}"

    @mock_aws
    def test_engine_is_preserved(self):
        """The engine name from the created instance appears in results."""
        rds = boto3.client("rds", region_name=REGION)
        rds.create_db_instance(
            DBInstanceIdentifier="engine-check-db",
            DBInstanceClass="db.t3.micro",
            Engine="postgres",
            MasterUsername="admin",
            MasterUserPassword="password123",
            AllocatedStorage=20,
        )

        result = get_rds_instances(REGION)

        row = next(r for r in result if r["DB Identifier"] == "engine-check-db")
        assert "postgres" in row["Engine"].lower()

    @mock_aws
    def test_empty_region_returns_empty_list(self):
        """Region with no RDS instances returns an empty list."""
        result = get_rds_instances(REGION)
        assert result == []


def _create_postgres(rds, name, **extra):
    rds.create_db_instance(
        DBInstanceIdentifier=name,
        DBInstanceClass="db.t3.micro",
        Engine="postgres",
        MasterUsername="admin",
        MasterUserPassword="password123",
        AllocatedStorage=20,
        **extra,
    )


class TestDeploymentColumns:
    """Status / Multi-AZ / Availability Zone(s) columns, end to end through moto."""

    @mock_aws
    def test_columns_present_and_placed_after_region(self):
        rds = boto3.client("rds", region_name=REGION)
        _create_postgres(rds, "placement-db")

        keys = list(get_rds_instances(REGION)[0].keys())
        i = keys.index("Region")
        assert keys[i + 1 : i + 4] == ["Status", "Multi-AZ", "Availability Zone(s)"]

    @mock_aws
    def test_standalone_single_az(self):
        rds = boto3.client("rds", region_name=REGION)
        _create_postgres(rds, "single-az-db")
        api = rds.describe_db_instances(DBInstanceIdentifier="single-az-db")["DBInstances"][0]

        row = next(r for r in get_rds_instances(REGION) if r["DB Identifier"] == "single-az-db")

        assert row["Status"] == api["DBInstanceStatus"]
        assert row["Multi-AZ"] == "No"
        assert row["Availability Zone(s)"] == api["AvailabilityZone"]

    @mock_aws
    def test_standalone_multi_az_flag(self):
        rds = boto3.client("rds", region_name=REGION)
        _create_postgres(rds, "multi-az-db", MultiAZ=True)

        row = next(r for r in get_rds_instances(REGION) if r["DB Identifier"] == "multi-az-db")

        assert row["Multi-AZ"] == "Yes"
        # moto 5.1.21 does not return SecondaryAvailabilityZone; the standby must
        # read N/A, not a guessed AZ. The populated case is in TestGetDeploymentFields.
        assert row["Availability Zone(s)"].endswith("(primary), N/A (standby)")

    @mock_aws
    def test_aurora_member_uses_cluster_fields(self):
        rds = boto3.client("rds", region_name=REGION)
        rds.create_db_cluster(
            DBClusterIdentifier="aurora-c",
            Engine="aurora-postgresql",
            MasterUsername="admin",
            MasterUserPassword="password123",
            AvailabilityZones=["us-east-1a", "us-east-1b", "us-east-1c"],
        )
        rds.create_db_instance(
            DBInstanceIdentifier="aurora-c-1",
            DBInstanceClass="db.r5.large",
            Engine="aurora-postgresql",
            DBClusterIdentifier="aurora-c",
        )
        cluster = rds.describe_db_clusters(DBClusterIdentifier="aurora-c")["DBClusters"][0]
        api = rds.describe_db_instances(DBInstanceIdentifier="aurora-c-1")["DBInstances"][0]

        row = next(r for r in get_rds_instances(REGION) if r["DB Identifier"] == "aurora-c-1")

        assert row["Multi-AZ"] == ("Yes" if cluster["MultiAZ"] else "No")
        # Only the member's own AZ; the cluster's AvailabilityZones list is not exported.
        assert row["Availability Zone(s)"] == api["AvailabilityZone"]


class TestGetDeploymentFields:
    """Unit tests on crafted API dicts for response shapes moto cannot produce."""

    def test_standalone_multi_az_with_standby_az(self):
        instance = {
            "MultiAZ": True,
            "AvailabilityZone": "us-east-1a",
            "SecondaryAvailabilityZone": "us-east-1b",
        }
        assert rds_export.get_deployment_fields(instance, "N/A", None) == (
            "Yes",
            "us-east-1a (primary), us-east-1b (standby)",
        )

    def test_standalone_missing_fields_are_na(self):
        # e.g. RDS Custom, where AWS documents instance-level MultiAZ as not applicable.
        assert rds_export.get_deployment_fields({}, "N/A", None) == ("N/A", "N/A")

    def test_row_with_missing_fields_is_na(self):
        # No SGs, no subnet group, no cluster: _build_instance_data never touches the client.
        row = rds_export._build_instance_data(
            {"DBInstanceIdentifier": "bare"}, REGION, None, {}, {}, "note"
        )
        assert row["Status"] == "N/A"
        assert row["Multi-AZ"] == "N/A"
        assert row["Availability Zone(s)"] == "N/A"

    def test_cluster_member_ignores_instance_multi_az(self):
        instance = {"MultiAZ": False, "AvailabilityZone": "us-east-1b"}
        cluster = {"MultiAZ": True, "AvailabilityZones": ["us-east-1a", "us-east-1c"]}
        assert rds_export.get_deployment_fields(instance, "c", cluster) == ("Yes", "us-east-1b")

    def test_cluster_lookup_failed_multi_az_na_az_from_instance(self):
        instance = {"MultiAZ": False, "AvailabilityZone": "us-east-1b"}
        assert rds_export.get_deployment_fields(instance, "c", None) == ("N/A", "us-east-1b")

    def test_cluster_missing_fields_are_na(self):
        assert rds_export.get_deployment_fields({}, "c", {}) == ("N/A", "N/A")


class TestSilentCollectionFailureRegression:
    """
    Regression tests for the 07.15.2026 audit: RDS data silently lost because a
    collection error was swallowed to an empty list, indistinguishable from a
    genuinely empty region. See .collab/audit/07.15.2026-rds-silent-collection-failure.md
    """

    @mock_aws
    def test_malformed_instance_is_skipped_not_fatal(self, monkeypatch):
        """
        One instance that fails to process must not discard the whole region's
        results — the healthy instances are still collected.
        """
        rds = boto3.client("rds", region_name=REGION)
        for name in ("good-db", "bad-db"):
            rds.create_db_instance(
                DBInstanceIdentifier=name,
                DBInstanceClass="db.t3.micro",
                Engine="postgres",
                MasterUsername="admin",
                MasterUserPassword="password123",
                AllocatedStorage=20,
            )

        original = rds_export._build_instance_data

        def raise_for_bad(instance, *args, **kwargs):
            if instance.get("DBInstanceIdentifier") == "bad-db":
                raise KeyError("SomeEngineSpecificField")
            return original(instance, *args, **kwargs)

        monkeypatch.setattr(rds_export, "_build_instance_data", raise_for_bad)

        result = get_rds_instances(REGION)

        ids = {row["DB Identifier"] for row in result}
        assert "good-db" in ids, "healthy instance was lost when a sibling failed"
        assert "bad-db" not in ids, "malformed instance should have been skipped"

    @mock_aws
    def test_region_api_failure_raises_not_empty(self, monkeypatch):
        """
        A region-level API failure must propagate (so the caller can record a
        FAILED region) rather than being swallowed into an empty list.
        """

        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "DescribeDBInstances",
            )

        monkeypatch.setattr(rds_export.utils, "get_boto3_client", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            get_rds_instances(REGION)
