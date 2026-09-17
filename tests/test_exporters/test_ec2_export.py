#!/usr/bin/env python3
"""
Moto-based tests for ec2_export.py.

Covers:
- get_instance_data()
"""

import sys
from pathlib import Path

import boto3
import botocore
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import ec2_export  # noqa: E402
from ec2_export import get_instance_data  # noqa: E402

REGION = "us-east-1"
AMI_ID = "ami-12345678"


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


class TestGetInstanceData:
    """Tests for get_instance_data()."""

    @mock_aws
    def test_launched_instance_appears_in_results(self):
        """A running EC2 instance is returned by the collector."""
        ec2 = boto3.client("ec2", region_name=REGION)
        response = ec2.run_instances(
            ImageId=AMI_ID, MinCount=1, MaxCount=1, InstanceType="t3.micro"
        )
        instance_id = response["Instances"][0]["InstanceId"]

        result = get_instance_data(REGION)

        assert isinstance(result, list)
        assert len(result) >= 1
        assert any(row["Instance ID"] == instance_id for row in result)

    @mock_aws
    def test_result_contains_expected_columns(self):
        """Each row contains the core expected column keys."""
        ec2 = boto3.client("ec2", region_name=REGION)
        ec2.run_instances(ImageId=AMI_ID, MinCount=1, MaxCount=1, InstanceType="t3.micro")

        result = get_instance_data(REGION)

        assert len(result) >= 1
        row = result[0]
        for col in ("Instance ID", "State", "Instance Type", "Region", "Private IPv4"):
            assert col in row, f"Missing column: {col}"

    @mock_aws
    def test_instance_type_is_preserved(self):
        """The instance type from launch is reflected in the results."""
        ec2 = boto3.client("ec2", region_name=REGION)
        ec2.run_instances(ImageId=AMI_ID, MinCount=1, MaxCount=1, InstanceType="t3.small")

        result = get_instance_data(REGION)

        assert len(result) >= 1
        assert result[0]["Instance Type"] == "t3.small"

    @mock_aws
    def test_region_field_matches_requested_region(self):
        """The Region field on every returned row matches the requested region."""
        ec2 = boto3.client("ec2", region_name=REGION)
        ec2.run_instances(ImageId=AMI_ID, MinCount=1, MaxCount=1, InstanceType="t3.micro")

        result = get_instance_data(REGION)

        assert all(row["Region"] == REGION for row in result)

    @mock_aws
    def test_empty_region_returns_empty_list(self):
        """Region with no EC2 instances returns an empty list."""
        result = get_instance_data(REGION)
        assert result == []


class TestSilentCollectionFailureRegression:
    """
    Regression tests for the 07.15.2026 audit: exporter data silently lost
    because a collection error was swallowed to an empty list, indistinguishable
    from a genuinely empty region. See
    .collab/audit/07.15.2026-rds-silent-collection-failure.md and the
    07.16.2026 blast-radius sweep.
    """

    @mock_aws
    def test_malformed_item_is_skipped_not_fatal(self, monkeypatch):
        """
        One instance that fails to process must not discard the whole region's
        results — the healthy instance is still collected.
        """
        ec2 = boto3.client("ec2", region_name=REGION)
        good = ec2.run_instances(
            ImageId=AMI_ID, MinCount=1, MaxCount=1, InstanceType="t3.micro"
        )["Instances"][0]["InstanceId"]
        bad = ec2.run_instances(
            ImageId=AMI_ID, MinCount=1, MaxCount=1, InstanceType="t3.micro"
        )["Instances"][0]["InstanceId"]

        original = ec2_export._build_instance_row

        def raise_for_bad(instance, *args, **kwargs):
            if instance.get("InstanceId") == bad:
                raise KeyError("SomeMissingField")
            return original(instance, *args, **kwargs)

        monkeypatch.setattr(ec2_export, "_build_instance_row", raise_for_bad)

        result = get_instance_data(REGION)

        ids = {row["Instance ID"] for row in result}
        assert good in ids, "healthy instance was lost when a sibling failed"
        assert bad not in ids, "malformed instance should have been skipped"

    @mock_aws
    def test_region_api_failure_raises_not_empty(self, monkeypatch):
        """
        A region-level API failure must propagate (so the caller can record a
        FAILED region) rather than being swallowed into an empty list.
        """

        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "DescribeInstances",
            )

        monkeypatch.setattr(ec2_export.utils, "get_boto3_client", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            get_instance_data(REGION)


# ---------------------------------------------------------------------------
# vCPU sourcing (Issue #259)
#
# The vCPU column read CpuOptions.CoreCount, which is *physical cores*. On any
# SMT-enabled family — most of them — that under-reports by the threads-per-core
# factor, typically 2x. Every EC2 export shipped to date carries the wrong value.
# ---------------------------------------------------------------------------


class TestVcpuSourcing:
    @mock_aws
    def test_vcpu_reports_threads_not_physical_cores(self):
        """
        m5.xlarge is 4 vCPU across 2 physical cores. Sourcing CoreCount would
        report 2; the column must report 4.
        """
        ec2 = boto3.client("ec2", region_name=REGION)
        ec2.run_instances(
            ImageId=AMI_ID, MinCount=1, MaxCount=1, InstanceType="m5.xlarge"
        )

        rows = get_instance_data(REGION)

        assert len(rows) == 1
        assert rows[0]["vCPU"] == 4, "vCPU must be DefaultVCpus, not DefaultCores"
        assert rows[0]["RAM (MiB)"] == 16 * 1024

    @mock_aws
    def test_reference_data_avoids_a_describe_instance_types_call(self, monkeypatch):
        """
        The static reference data covers ~1,200 types, so the common case must
        not spend an API call. Proven by making the fallback fail loudly: if it
        is reached, the export raises.
        """
        ec2 = boto3.client("ec2", region_name=REGION)
        ec2.run_instances(
            ImageId=AMI_ID, MinCount=1, MaxCount=1, InstanceType="m5.xlarge"
        )

        real_get_client = ec2_export.utils.get_boto3_client

        class NoDescribeTypes:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def __getattr__(self, name):
                if name == "describe_instance_types":
                    raise AssertionError(
                        "describe_instance_types called for a type the "
                        "reference data already covers"
                    )
                return getattr(self._wrapped, name)

        monkeypatch.setattr(
            ec2_export.utils,
            "get_boto3_client",
            lambda *a, **k: NoDescribeTypes(real_get_client(*a, **k)),
        )

        rows = get_instance_data(REGION)

        assert rows[0]["vCPU"] == 4

    @mock_aws
    def test_type_absent_from_reference_data_falls_back_to_the_api(self, monkeypatch):
        """A type the reference JSON does not carry still resolves."""
        monkeypatch.setattr(ec2_export.utils, "load_instance_type_specs", dict)

        ec2 = boto3.client("ec2", region_name=REGION)
        ec2.run_instances(
            ImageId=AMI_ID, MinCount=1, MaxCount=1, InstanceType="m5.xlarge"
        )

        rows = get_instance_data(REGION)

        assert rows[0]["vCPU"] == 4, "fallback must also use DefaultVCpus"
        assert rows[0]["RAM (MiB)"] == 16 * 1024

    @mock_aws
    def test_cores_and_threads_are_exported_alongside_vcpu(self, monkeypatch):
        """
        CoreCount and ThreadsPerCore are legitimate data in their own right —
        they are just not vCPU. vCPU = cores x threads per core.
        """
        monkeypatch.setattr(ec2_export, "get_os_info_from_ssm", lambda *a, **k: "N/A")
        monkeypatch.setattr(ec2_export, "get_os_info_from_ami", lambda *a, **k: "N/A")
        ec2_client = boto3.client("ec2", region_name=REGION)

        row = ec2_export._build_instance_row(
            {
                "InstanceId": "i-1234567890abcdef0",
                "InstanceType": "m5.xlarge",
                "State": {"Name": "running"},
                "CpuOptions": {"CoreCount": 2, "ThreadsPerCore": 2},
                "BlockDeviceMappings": [],
            },
            REGION,
            ec2_client,
            {},
            {},
            "test",
            {"m5.xlarge": {"memory_mib": 16384, "vcpu": 4}},
            {},
        )

        assert row["vCPU"] == 4
        assert row["CPU Cores"] == 2
        assert row["Threads per Core"] == 2

    @mock_aws
    def test_unresolvable_type_reports_na_not_a_wrong_number(self, monkeypatch):
        monkeypatch.setattr(ec2_export, "get_os_info_from_ssm", lambda *a, **k: "N/A")
        monkeypatch.setattr(ec2_export, "get_os_info_from_ami", lambda *a, **k: "N/A")
        ec2_client = boto3.client("ec2", region_name=REGION)

        row = ec2_export._build_instance_row(
            {
                "InstanceId": "i-1234567890abcdef0",
                "InstanceType": "made.up",
                "State": {"Name": "running"},
                "BlockDeviceMappings": [],
            },
            REGION,
            ec2_client,
            {},
            {},
            "test",
            {},
            {},
        )

        assert row["vCPU"] == "N/A"
        assert row["RAM (MiB)"] == "N/A"
