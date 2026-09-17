#!/usr/bin/env python3
"""
Moto-based tests for ami_export.py.

Focus: the silent-collection-failure contract (Tier-2). See
.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md
"""

import sys
from pathlib import Path

import boto3
import botocore
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import ami_export  # noqa: E402
from ami_export import collect_amis_in_region  # noqa: E402

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _create_ami(ec2_client, name):
    """Launch a minimal instance and register/create an AMI named ``name``."""
    reservation = ec2_client.run_instances(
        ImageId="ami-12345678",
        MinCount=1,
        MaxCount=1,
        InstanceType="t2.micro",
    )
    instance_id = reservation["Instances"][0]["InstanceId"]
    response = ec2_client.create_image(
        InstanceId=instance_id,
        Name=name,
        Description="test AMI",
    )
    return response["ImageId"]


class TestCollectAmisInRegion:
    """Happy-path collection."""

    @mock_aws
    def test_collects_amis(self):
        client = boto3.client("ec2", region_name=REGION)
        _create_ami(client, "web-ami")

        rows = collect_amis_in_region(REGION, ACCOUNT_ID)

        names = {row["AMI Name"] for row in rows}
        assert "web-ami" in names

    @mock_aws
    def test_empty_region_returns_empty_list(self):
        boto3.client("ec2", region_name=REGION)  # region exists, no AMIs

        rows = collect_amis_in_region(REGION, ACCOUNT_ID)

        assert rows == []


class TestSilentCollectionFailureRegression:
    """
    Regression tests for the 07.15 / 07.16.2026 audits: exporters silently lost
    data because a collection error was swallowed to an empty list,
    indistinguishable from a genuinely empty region.
    """

    @mock_aws
    def test_malformed_item_is_skipped_not_fatal(self, monkeypatch):
        """One AMI that fails to process must not discard the whole region."""
        client = boto3.client("ec2", region_name=REGION)
        _create_ami(client, "good-ami")
        _create_ami(client, "bad-ami")

        original = ami_export._build_ami_row

        def raise_for_bad(ami, region):
            if ami.get("Name") == "bad-ami":
                raise KeyError("SomeUnexpectedField")
            return original(ami, region)

        monkeypatch.setattr(ami_export, "_build_ami_row", raise_for_bad)

        rows = collect_amis_in_region(REGION, ACCOUNT_ID)

        names = {row["AMI Name"] for row in rows}
        assert "good-ami" in names, "healthy AMI was lost when a sibling failed"
        assert "bad-ami" not in names, "malformed AMI should have been skipped"

    @mock_aws
    def test_region_api_failure_raises_not_empty(self, monkeypatch):
        """
        A region-level API failure must propagate (so the caller can record a
        FAILED region) rather than being swallowed into an empty list.
        """

        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "DescribeImages",
            )

        monkeypatch.setattr(ami_export.utils, "get_boto3_client", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            collect_amis_in_region(REGION, ACCOUNT_ID)

    @mock_aws
    def test_collect_amis_surfaces_failed_regions(self, monkeypatch):
        """
        The scope wrapper must return failed regions via collect_failures, not
        drop them — this is what lets export write a FAILED marker + exit 1.
        """

        def boom(region, account_id):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                "DescribeImages",
            )

        monkeypatch.setattr(ami_export, "collect_amis_in_region", boom)

        amis, failed_regions = ami_export.collect_amis([REGION], ACCOUNT_ID)

        assert amis == []
        assert [r for r, _ in failed_regions] == [REGION]



# ---------------------------------------------------------------------------
# AMI ancestry (Issues #270, #272)
#
# Ancestry comes from contractual DescribeImages fields only. A snapshot
# description parse was tried in #270 and removed in #272: AWS writes
# "Created by CreateImage(i-xxx) for ami-yyy" where ami-yyy is the AMI *being
# created*, so the parsed value was the row's own ID — a tautology. The tests
# that covered it asserted the misreading as correct and were deleted rather
# than adapted.
# ---------------------------------------------------------------------------


class TestCopySourceSupportDetection:
    @mock_aws
    def test_detection_matches_the_installed_botocore_model(self):
        """
        SourceImageId postdates the pinned boto3 floor, so the answer differs
        by installed version. Assert against the model itself rather than
        hardcoding an expectation that breaks on one end of the range.
        """
        client = boto3.client("ec2", region_name=REGION)
        image_shape = (
            client.meta.service_model
            .operation_model("DescribeImages")
            .output_shape.members["Images"].member
        )
        expected = "SourceImageId" in image_shape.members

        assert ami_export._copy_source_supported(client) is expected


class TestAncestryColumns:
    def test_contractual_fields_are_reported_when_present(self):
        columns = ami_export._ancestry_columns(
            {
                "ImageId": "ami-0self1111111111111",
                "SourceInstanceId": "i-0abc123def456789a",
                "SourceImageId": "ami-0parent22222222222",
                "SourceImageRegion": "us-west-2",
            },
            copy_source_supported=True,
        )

        assert columns["Source Instance ID"] == "i-0abc123def456789a"
        assert columns["Source Image ID"] == "ami-0parent22222222222"
        assert columns["Source Image Region"] == "us-west-2"

    def test_reported_parent_is_not_the_amis_own_id(self):
        """
        Regression guard for Issue #272. The removed column reported the AMI's
        own ID in 2,320 of 2,322 live rows; a populated ancestry column that
        merely echoes the row's identity carries no information.
        """
        ami = {
            "ImageId": "ami-0self1111111111111",
            "SourceImageId": "ami-0parent22222222222",
        }

        columns = ami_export._ancestry_columns(ami, copy_source_supported=True)

        assert columns["Source Image ID"] != ami["ImageId"]
        assert "Parent AMI (inferred)" not in columns, (
            "the tautological inferred-parent column must not come back"
        )

    def test_old_botocore_is_distinguishable_from_no_source(self):
        """
        'AWS reported no source' and 'this client cannot report one' are
        different facts. The distinction matters more since #272 made
        SourceImageId the only ancestry source — on an old boto3 there is now
        no fallback at all.
        """
        supported = ami_export._ancestry_columns({}, copy_source_supported=True)
        unsupported = ami_export._ancestry_columns({}, copy_source_supported=False)

        assert supported["Source Image ID"] == "N/A"
        assert unsupported["Source Image ID"] == "Unavailable (boto3 too old)"
        assert unsupported["Source Image Region"] == "Unavailable (boto3 too old)"

    def test_no_resolvable_ancestor_reports_na(self):
        """
        An imported or third-party AMI has no ancestor. That is a correct
        result, not a failure.
        """
        columns = ami_export._ancestry_columns({}, copy_source_supported=True)

        assert columns["Source Instance ID"] == "N/A"
        assert columns["Source Image ID"] == "N/A"


class TestAncestryInRegionScan:
    @mock_aws
    def test_region_scan_populates_ancestry_columns(self):
        ec2_client = boto3.client("ec2", region_name=REGION)
        base = ec2_client.describe_images()["Images"][0]["ImageId"]
        instance_id = ec2_client.run_instances(
            ImageId=base, MinCount=1, MaxCount=1, InstanceType="t3.micro"
        )["Instances"][0]["InstanceId"]
        ec2_client.create_image(InstanceId=instance_id, Name="built-from-instance")

        rows = ami_export.collect_amis_in_region(REGION, "123456789012")

        row = next(r for r in rows if r["AMI Name"] == "built-from-instance")
        assert row["Source Instance ID"] == instance_id

    @mock_aws
    def test_ancestry_costs_no_extra_api_calls(self, monkeypatch):
        """
        #272 removed the describe_snapshots round trip along with the parse it
        fed. Ancestry now comes from fields already in the describe_images
        response, so the scan must not reach for snapshots at all.
        """
        ec2_client = boto3.client("ec2", region_name=REGION)
        base = ec2_client.describe_images()["Images"][0]["ImageId"]
        instance_id = ec2_client.run_instances(
            ImageId=base, MinCount=1, MaxCount=1, InstanceType="t3.micro"
        )["Instances"][0]["InstanceId"]
        ec2_client.create_image(InstanceId=instance_id, Name="built-from-instance")

        real_get_client = ami_export.utils.get_boto3_client

        class NoSnapshotCalls:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def get_paginator(self, name):
                if name == "describe_snapshots":
                    raise AssertionError("ancestry must not call describe_snapshots")
                return self._wrapped.get_paginator(name)

            def __getattr__(self, name):
                if name == "describe_snapshots":
                    raise AssertionError("ancestry must not call describe_snapshots")
                return getattr(self._wrapped, name)

        monkeypatch.setattr(
            ami_export.utils,
            "get_boto3_client",
            lambda *a, **k: NoSnapshotCalls(real_get_client(*a, **k)),
        )

        rows = ami_export.collect_amis_in_region(REGION, "123456789012")

        assert any(r["AMI Name"] == "built-from-instance" for r in rows)
