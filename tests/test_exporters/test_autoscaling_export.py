#!/usr/bin/env python3
"""
Moto-based tests for autoscaling_export.py.

Focus: the silent-collection-failure contract (Tier-2, see
.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md) and the
launch-source coverage gap (Issues #257 / #258, see
.collab/audit/08.31.2026-autoscaling-exporter-coverage-gap.md).
"""

import sys
from pathlib import Path

import boto3
import botocore
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import autoscaling_export  # noqa: E402
from autoscaling_export import _scan_asgs_region  # noqa: E402

REGION = "us-east-1"


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _create_asg(client, name):
    """Create a launch configuration + Auto Scaling Group named ``name``."""
    client.create_launch_configuration(
        LaunchConfigurationName=f"{name}-lc",
        ImageId="ami-12345678",
        InstanceType="t3.micro",
    )
    client.create_auto_scaling_group(
        AutoScalingGroupName=name,
        LaunchConfigurationName=f"{name}-lc",
        MinSize=0,
        MaxSize=1,
        DesiredCapacity=0,
        AvailabilityZones=[f"{REGION}a"],
    )


class TestScanAsgsRegion:
    """Happy-path collection."""

    @mock_aws
    def test_collects_asgs(self):
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "web-asg")

        rows = _scan_asgs_region(REGION)

        names = {row["ASG Name"] for row in rows}
        assert "web-asg" in names

    @mock_aws
    def test_empty_region_returns_empty_list(self):
        boto3.client("autoscaling", region_name=REGION)  # region exists, no ASGs

        rows = _scan_asgs_region(REGION)

        assert rows == []


class TestSilentCollectionFailureRegression:
    """
    Regression tests for the 07.15 / 07.16.2026 audits: exporters silently lost
    data because a collection error was swallowed to an empty list,
    indistinguishable from a genuinely empty region.
    """

    @mock_aws
    def test_malformed_item_is_skipped_not_fatal(self, monkeypatch):
        """One ASG that fails to process must not discard the whole region."""
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "good-asg")
        _create_asg(client, "bad-asg")

        original = autoscaling_export._build_asg_row

        def raise_for_bad(asg, region):
            if asg.get("AutoScalingGroupName") == "bad-asg":
                raise KeyError("SomeUnexpectedField")
            return original(asg, region)

        monkeypatch.setattr(autoscaling_export, "_build_asg_row", raise_for_bad)

        rows = _scan_asgs_region(REGION)

        names = {row["ASG Name"] for row in rows}
        assert "good-asg" in names, "healthy ASG was lost when a sibling failed"
        assert "bad-asg" not in names, "malformed ASG should have been skipped"

    @mock_aws
    def test_region_api_failure_raises_not_empty(self, monkeypatch):
        """
        A region-level API failure must propagate (so the caller can record a
        FAILED region) rather than being swallowed into an empty list.
        """

        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "DescribeAutoScalingGroups",
            )

        monkeypatch.setattr(autoscaling_export.utils, "get_boto3_client", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            _scan_asgs_region(REGION)

    @mock_aws
    def test_collect_autoscaling_groups_surfaces_failed_regions(self, monkeypatch):
        """
        The scope wrapper must return failed regions via collect_failures, not
        drop them — this is what lets export write a FAILED marker + exit 1.
        """

        def boom(region):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                "DescribeAutoScalingGroups",
            )

        monkeypatch.setattr(autoscaling_export, "_scan_launch_data_region", boom)

        asgs, failed_regions = autoscaling_export.collect_autoscaling_groups([REGION])

        assert asgs == []
        assert [r for r, _ in failed_regions] == [REGION]


# ---------------------------------------------------------------------------
# Launch source coverage (Issue #257)
#
# The exporter previously recorded only the *name* of a launch template or
# configuration and never fetched the object, so instance type, AMI, volumes,
# and IMDS posture were absent while the docstring claimed to cover them.
# ---------------------------------------------------------------------------


def _create_launch_template(ec2_client, name, **overrides):
    """
    Create a launch template with a realistic data block.

    The security group is created for real rather than hardcoded: newer moto
    releases validate that an ASG's launch template references an existing
    group, so a literal sg-xxxx id fails at create_auto_scaling_group time.
    """
    security_group_id = ec2_client.create_security_group(
        GroupName=f"{name}-sg", Description=f"test group for {name}"
    )["GroupId"]
    data = {
        "ImageId": "ami-0abcdef1234567890",
        "InstanceType": "m5.xlarge",
        "KeyName": "prod-key",
        "SecurityGroupIds": [security_group_id],
        "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1},
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {"VolumeSize": 50, "VolumeType": "gp3", "Encrypted": True},
            },
            {
                "DeviceName": "/dev/xvdb",
                "Ebs": {"VolumeSize": 100, "VolumeType": "gp3", "Encrypted": True},
            },
        ],
    }
    data.update(overrides)
    return ec2_client.create_launch_template(
        LaunchTemplateName=name, LaunchTemplateData=data
    )["LaunchTemplate"]


class TestLaunchConfigurationResolution:
    """Launch configurations are the majority path in long-lived estates."""

    @mock_aws
    def test_referenced_launch_configuration_is_resolved(self):
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "web-asg")

        result = autoscaling_export._scan_launch_data_region(REGION)
        configs = result["launch_configurations"]

        assert len(configs) == 1
        row = configs[0]
        assert row["Launch Configuration Name"] == "web-asg-lc"
        assert row["Instance Type"] == "t3.micro"
        assert row["AMI ID"] == "ami-12345678"
        assert row["Used By ASGs"] == "web-asg"

    @mock_aws
    def test_unreferenced_launch_configuration_is_excluded(self):
        """Referenced-only: an LC no ASG uses is out of scope for this export."""
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "web-asg")
        client.create_launch_configuration(
            LaunchConfigurationName="orphan-lc",
            ImageId="ami-99999999",
            InstanceType="c5.large",
        )

        result = autoscaling_export._scan_launch_data_region(REGION)

        names = {row["Launch Configuration Name"] for row in result["launch_configurations"]}
        assert names == {"web-asg-lc"}, "unreferenced LC leaked into the export"

    @mock_aws
    def test_shared_launch_configuration_lists_every_asg(self):
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "web-asg")
        client.create_auto_scaling_group(
            AutoScalingGroupName="api-asg",
            LaunchConfigurationName="web-asg-lc",
            MinSize=0,
            MaxSize=1,
            DesiredCapacity=0,
            AvailabilityZones=[f"{REGION}a"],
        )

        result = autoscaling_export._scan_launch_data_region(REGION)

        assert result["launch_configurations"][0]["Used By ASGs"] == "api-asg, web-asg"

    @mock_aws
    def test_unresolvable_launch_configurations_raise(self, monkeypatch):
        """
        Referenced configurations that all fail to resolve means a permission
        or API problem, not an empty account. It must fail loud rather than
        export an empty sheet that reads as complete.
        """
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "web-asg")

        refs = {"web-asg": autoscaling_export._launch_source_ref(
            {"LaunchConfigurationName": "web-asg-lc"}
        )}

        class EmptyPaginator:
            def paginate(self, *args, **kwargs):
                return iter([{"LaunchConfigurations": []}])

        class StubClient:
            def get_paginator(self, name):
                return EmptyPaginator()

        monkeypatch.setattr(
            autoscaling_export.utils, "get_boto3_client", lambda *a, **k: StubClient()
        )

        with pytest.raises(RuntimeError, match="launch configuration"):
            autoscaling_export._collect_launch_configurations(REGION, refs)


class TestLaunchTemplateResolution:
    @mock_aws
    def test_referenced_launch_template_is_resolved(self):
        ec2_client = boto3.client("ec2", region_name=REGION)
        asg_client = boto3.client("autoscaling", region_name=REGION)
        template = _create_launch_template(ec2_client, "web-lt")
        asg_client.create_auto_scaling_group(
            AutoScalingGroupName="web-asg",
            LaunchTemplate={
                "LaunchTemplateId": template["LaunchTemplateId"],
                "Version": "1",
            },
            MinSize=1,
            MaxSize=4,
            DesiredCapacity=2,
            AvailabilityZones=[f"{REGION}a"],
        )

        result = autoscaling_export._scan_launch_data_region(REGION)
        templates = result["launch_templates"]

        assert len(templates) == 1
        row = templates[0]
        assert row["Instance Type"] == "m5.xlarge"
        assert row["AMI ID"] == "ami-0abcdef1234567890"
        assert row["IMDSv2 Required"] == "required"
        assert row["Security Groups"].startswith("sg-")
        assert row["Root Volume Size (GiB)"] == 50
        assert row["Total EBS Size (GiB)"] == 150
        assert row["EBS Encrypted"] is True
        assert row["Used By ASGs"] == "web-asg"

    @mock_aws
    def test_default_version_alias_is_resolved_to_a_number(self):
        """
        An ASG that omits the version means $Default. The export must resolve
        that to the concrete version actually in use.
        """
        ec2_client = boto3.client("ec2", region_name=REGION)
        asg_client = boto3.client("autoscaling", region_name=REGION)
        template = _create_launch_template(ec2_client, "web-lt")
        asg_client.create_auto_scaling_group(
            AutoScalingGroupName="web-asg",
            LaunchTemplate={"LaunchTemplateId": template["LaunchTemplateId"]},
            MinSize=0,
            MaxSize=1,
            DesiredCapacity=0,
            AvailabilityZones=[f"{REGION}a"],
        )

        result = autoscaling_export._scan_launch_data_region(REGION)
        row = result["launch_templates"][0]

        assert row["Version In Use"] == "$Default"
        assert row["Resolved Version"] == "1"
        assert row["Instance Type"] == "m5.xlarge"

    @mock_aws
    def test_unreferenced_launch_template_is_excluded(self):
        ec2_client = boto3.client("ec2", region_name=REGION)
        asg_client = boto3.client("autoscaling", region_name=REGION)
        template = _create_launch_template(ec2_client, "web-lt")
        _create_launch_template(ec2_client, "orphan-lt")
        asg_client.create_auto_scaling_group(
            AutoScalingGroupName="web-asg",
            LaunchTemplate={"LaunchTemplateId": template["LaunchTemplateId"], "Version": "1"},
            MinSize=0,
            MaxSize=1,
            DesiredCapacity=0,
            AvailabilityZones=[f"{REGION}a"],
        )

        result = autoscaling_export._scan_launch_data_region(REGION)

        names = {row["Launch Template Name"] for row in result["launch_templates"]}
        assert names == {"web-lt"}, "unreferenced launch template leaked into the export"


class TestCapacityEnvelope:
    """
    Capacity is an envelope, not a scalar: per-instance spec x a capacity
    range. A single number is wrong the moment the group scales.
    """

    @mock_aws
    def test_envelope_spans_min_desired_and_max(self):
        ec2_client = boto3.client("ec2", region_name=REGION)
        asg_client = boto3.client("autoscaling", region_name=REGION)
        template = _create_launch_template(ec2_client, "web-lt")
        asg_client.create_auto_scaling_group(
            AutoScalingGroupName="web-asg",
            LaunchTemplate={"LaunchTemplateId": template["LaunchTemplateId"], "Version": "1"},
            MinSize=1,
            MaxSize=4,
            DesiredCapacity=2,
            AvailabilityZones=[f"{REGION}a"],
        )

        row = autoscaling_export._scan_launch_data_region(REGION)["asgs"][0]

        # m5.xlarge: 4 vCPU / 16 GiB, 150 GiB of EBS per instance
        assert row["Instance Type"] == "m5.xlarge"
        assert row["vCPU per Instance"] == 4
        assert row["vCPU (Min)"] == 4
        assert row["vCPU (Desired)"] == 8
        assert row["vCPU (Max)"] == 16
        assert row["Memory GiB (Desired)"] == 32
        assert row["Memory GiB (Max)"] == 64
        assert row["Storage GiB (Desired)"] == 300
        assert row["AMI ID"] == "ami-0abcdef1234567890"
        assert row["Root Volume Size (GiB)"] == 50

    @mock_aws
    def test_launch_configuration_asg_is_enriched_too(self):
        client = boto3.client("autoscaling", region_name=REGION)
        client.create_launch_configuration(
            LaunchConfigurationName="web-lc",
            ImageId="ami-12345678",
            InstanceType="c5.large",
        )
        client.create_auto_scaling_group(
            AutoScalingGroupName="web-asg",
            LaunchConfigurationName="web-lc",
            MinSize=2,
            MaxSize=6,
            DesiredCapacity=3,
            AvailabilityZones=[f"{REGION}a"],
        )

        row = autoscaling_export._scan_launch_data_region(REGION)["asgs"][0]

        # c5.large: 2 vCPU / 4 GiB
        assert row["Launch Source Type"] == "Launch Configuration"
        assert row["Instance Type"] == "c5.large"
        assert row["vCPU (Min)"] == 4
        assert row["vCPU (Desired)"] == 6
        assert row["vCPU (Max)"] == 12
        assert row["Memory GiB (Desired)"] == 12

    def test_unknown_instance_type_reports_na_not_zero(self):
        """An unresolved spec must read as 'N/A', never as a confident zero."""
        enrichment = autoscaling_export._launch_enrichment(
            {"instance_type": "made.up", "ami_id": "ami-1", "source": "Launch Template"},
            {},
            1, 2, 4,
        )

        assert enrichment["vCPU per Instance"] == "N/A"
        assert enrichment["vCPU (Desired)"] == "N/A"
        assert enrichment["Memory GiB (Max)"] == "N/A"

    def test_mixed_policy_without_single_type_reports_overrides(self):
        """
        A mixed-instances policy with no template-level type has no single
        per-instance spec; the columns must say so rather than pick one.
        """
        enrichment = autoscaling_export._launch_enrichment(
            {
                "instance_type": None,
                "overrides": ["m5.large", "m5a.large"],
                "ami_id": "ami-1",
                "source": "Launch Template",
            },
            {},
            1, 2, 4,
        )

        assert enrichment["Instance Type"] == "Mixed: m5.large, m5a.large"
        assert enrichment["vCPU (Desired)"] == "N/A"


class TestBlockDeviceSummary:
    def test_root_volume_preferred_over_first_mapping(self):
        summary = autoscaling_export._summarize_block_devices([
            {"DeviceName": "/dev/xvdb", "Ebs": {"VolumeSize": 500, "VolumeType": "st1"}},
            {"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 30, "VolumeType": "gp3"}},
        ])

        assert summary["root_size"] == 30
        assert summary["root_type"] == "gp3"
        assert summary["total_size"] == 530

    def test_absent_mappings_report_none_not_zero(self):
        """
        A template that omits block devices inherits the AMI's. That is
        unknown, not zero, and must not be reported as a size.
        """
        summary = autoscaling_export._summarize_block_devices([])

        assert summary["root_size"] is None
        assert summary["total_size"] is None

    def test_partial_encryption_is_not_reported_as_encrypted(self):
        summary = autoscaling_export._summarize_block_devices([
            {"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 30, "Encrypted": True}},
            {"DeviceName": "/dev/xvdb", "Ebs": {"VolumeSize": 30, "Encrypted": False}},
        ])

        assert summary["encrypted"] is False


class TestInstanceSpecResolution:
    def test_reference_data_supplies_true_vcpu_not_core_count(self):
        """
        vCPU must be the thread count. m5.xlarge is 4 vCPU / 2 physical cores;
        sourcing CoreCount would report 2 (see Issue #259).
        """
        specs = autoscaling_export._resolve_instance_specs(REGION, {"m5.xlarge"})

        assert specs["m5.xlarge"]["vcpu"] == 4
        assert specs["m5.xlarge"]["memory_gib"] == 16.0

    def test_empty_input_makes_no_api_call(self, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("no client should be created for an empty type set")

        monkeypatch.setattr(autoscaling_export.utils, "get_boto3_client", boom)

        assert autoscaling_export._resolve_instance_specs(REGION, set()) == {}


# ---------------------------------------------------------------------------
# Scheduled actions (Issue #258)
# ---------------------------------------------------------------------------


class TestScheduledActions:
    @mock_aws
    def test_collects_scheduled_actions(self):
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "web-asg")
        client.put_scheduled_update_group_action(
            AutoScalingGroupName="web-asg",
            ScheduledActionName="scale-up-weekday-mornings",
            Recurrence="0 13 * * MON-FRI",
            MinSize=2,
            MaxSize=10,
            DesiredCapacity=4,
        )

        rows = autoscaling_export._scan_scheduled_actions_region(REGION)

        assert len(rows) == 1
        row = rows[0]
        assert row["Action Name"] == "scale-up-weekday-mornings"
        assert row["ASG Name"] == "web-asg"
        assert row["Recurrence"] == "0 13 * * MON-FRI"
        assert row["Min Size"] == 2
        assert row["Max Size"] == 10
        assert row["Desired Capacity"] == 4

    @mock_aws
    def test_empty_region_returns_empty_list(self):
        boto3.client("autoscaling", region_name=REGION)

        assert autoscaling_export._scan_scheduled_actions_region(REGION) == []

    @mock_aws
    def test_region_api_failure_raises_not_empty(self, monkeypatch):
        """Same fail-loud contract as the primary ASG scope."""

        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                "DescribeScheduledActions",
            )

        monkeypatch.setattr(autoscaling_export.utils, "get_boto3_client", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            autoscaling_export._scan_scheduled_actions_region(REGION)


class TestMixedInstancesPolicy:
    """
    A mixed-instances policy carries its template on a different key and adds
    an override list plus a spot/on-demand split — all of which the exporter
    previously reduced to a display string.
    """

    @mock_aws
    def test_mixed_policy_template_and_distribution_are_captured(self):
        ec2_client = boto3.client("ec2", region_name=REGION)
        asg_client = boto3.client("autoscaling", region_name=REGION)
        template = _create_launch_template(ec2_client, "mix-lt", InstanceType="m5.large")
        asg_client.create_auto_scaling_group(
            AutoScalingGroupName="mix-asg",
            MixedInstancesPolicy={
                "LaunchTemplate": {
                    "LaunchTemplateSpecification": {
                        "LaunchTemplateId": template["LaunchTemplateId"],
                        "Version": "1",
                    },
                    "Overrides": [
                        {"InstanceType": "m5.large"},
                        {"InstanceType": "m5a.large"},
                    ],
                },
                "InstancesDistribution": {
                    "OnDemandBaseCapacity": 1,
                    "OnDemandPercentageAboveBaseCapacity": 50,
                    "SpotAllocationStrategy": "capacity-optimized",
                },
            },
            MinSize=1,
            MaxSize=4,
            DesiredCapacity=2,
            AvailabilityZones=[f"{REGION}a"],
        )

        result = autoscaling_export._scan_launch_data_region(REGION)

        row = result["launch_templates"][0]
        assert row["Instance Type Overrides"] == "m5.large, m5a.large"
        assert row["On-Demand Base Capacity"] == 1
        assert row["On-Demand % Above Base"] == 50
        assert row["Spot Allocation Strategy"] == "capacity-optimized"

        asg_row = result["asgs"][0]
        assert asg_row["Launch Source"] == "Mixed: mix-lt (1)"
        assert asg_row["Instance Type"] == "m5.large"
        assert asg_row["vCPU (Desired)"] == 4


# ---------------------------------------------------------------------------
# Scaling policy enrichment (Issue #260)
#
# The sheet handled target tracking well and degraded poorly on everything
# else: step boundaries were dropped, the CloudWatch alarms that actually
# trigger scaling were never joined, and predictive policies fell through to
# the simple-scaling branch and exported as "Adjustment: N/A, Type: N/A".
# ---------------------------------------------------------------------------


class TestStepAdjustmentFormatting:
    def test_bounded_and_unbounded_intervals(self):
        rendered = autoscaling_export._format_step_adjustments([
            {"MetricIntervalUpperBound": 0, "ScalingAdjustment": -1},
            {"MetricIntervalLowerBound": 0, "MetricIntervalUpperBound": 10,
             "ScalingAdjustment": 1},
            {"MetricIntervalLowerBound": 10, "ScalingAdjustment": 3},
        ])

        assert rendered == "< 0: -1; 0 to 10: +1; >= 10: +3"

    def test_no_steps_reads_na(self):
        assert autoscaling_export._format_step_adjustments([]) == "N/A"


class TestPolicyRowBranching:
    def test_step_policy_exports_its_boundaries(self):
        """The step boundaries are the policy's entire content."""
        row = autoscaling_export._build_policy_row(
            {
                "PolicyName": "step-up",
                "AutoScalingGroupName": "web-asg",
                "PolicyType": "StepScaling",
                "AdjustmentType": "ChangeInCapacity",
                "StepAdjustments": [
                    {"MetricIntervalLowerBound": 0, "MetricIntervalUpperBound": 10,
                     "ScalingAdjustment": 1},
                    {"MetricIntervalLowerBound": 10, "ScalingAdjustment": 2},
                ],
            },
            REGION,
            {},
        )

        assert row["Step Adjustments"] == "0 to 10: +1; >= 10: +2"
        assert "Steps:" in row["Policy Detail"]
        assert row["Policy Detail"] != "Adjustment: N/A, Type: ChangeInCapacity"

    def test_predictive_policy_is_not_reported_as_a_malformed_simple_policy(self):
        """
        Predictive scaling has neither TargetTrackingConfiguration nor
        ScalingAdjustment; it used to land in the else branch and export as
        "Adjustment: N/A, Type: N/A".
        """
        row = autoscaling_export._build_policy_row(
            {
                "PolicyName": "predictive",
                "AutoScalingGroupName": "web-asg",
                "PolicyType": "PredictiveScaling",
                "PredictiveScalingConfiguration": {
                    "MetricSpecifications": [{
                        "TargetValue": 50.0,
                        "PredefinedMetricPairSpecification": {
                            "PredefinedMetricType": "ASGCPUUtilization"
                        },
                    }],
                    "Mode": "ForecastAndScale",
                    "SchedulingBufferTime": 300,
                    "MaxCapacityBreachBehavior": "IncreaseMaxCapacity",
                    "MaxCapacityBuffer": 10,
                },
            },
            REGION,
            {},
        )

        assert row["Predictive Mode"] == "ForecastAndScale"
        assert row["Target Value"] == 50.0
        assert row["Predictive Scheduling Buffer (s)"] == 300
        assert row["Max Capacity Breach Behavior"] == "IncreaseMaxCapacity"
        assert row["Max Capacity Buffer (%)"] == 10
        assert "ASGCPUUtilization" in row["Policy Detail"]
        assert row["Policy Detail"] != "Adjustment: N/A, Type: N/A"

    def test_target_tracking_detail_is_preserved(self):
        row = autoscaling_export._build_policy_row(
            {
                "PolicyName": "tt",
                "AutoScalingGroupName": "web-asg",
                "PolicyType": "TargetTrackingScaling",
                "TargetTrackingConfiguration": {
                    "TargetValue": 70.0,
                    "PredefinedMetricSpecification": {
                        "PredefinedMetricType": "ASGAverageCPUUtilization"
                    },
                    "DisableScaleIn": True,
                },
            },
            REGION,
            {},
        )

        assert row["Policy Detail"] == "Target: 70.0, Metric: ASGAverageCPUUtilization"
        assert row["Target Value"] == 70.0
        assert row["Disable Scale In"] is True

    def test_simple_policy_still_reports_its_adjustment(self):
        row = autoscaling_export._build_policy_row(
            {
                "PolicyName": "simple",
                "AutoScalingGroupName": "web-asg",
                "PolicyType": "SimpleScaling",
                "AdjustmentType": "ChangeInCapacity",
                "ScalingAdjustment": 2,
            },
            REGION,
            {},
        )

        assert row["Policy Detail"] == "Adjustment: 2, Type: ChangeInCapacity"


class TestAlarmJoin:
    def test_alarm_metric_and_threshold_are_surfaced(self):
        summary = autoscaling_export._summarize_alarms(
            [{"AlarmName": "cpu-high"}],
            {
                "cpu-high": {
                    "AlarmName": "cpu-high",
                    "Namespace": "AWS/EC2",
                    "MetricName": "CPUUtilization",
                    "Statistic": "Average",
                    "ComparisonOperator": "GreaterThanThreshold",
                    "Threshold": 80.0,
                    "EvaluationPeriods": 2,
                    "Period": 300,
                }
            },
        )

        assert summary["Alarm Names"] == "cpu-high"
        assert summary["Alarm Metric"] == "AWS/EC2/CPUUtilization"
        assert summary["Alarm Statistic"] == "Average"
        assert summary["Alarm Condition"] == "GreaterThanThreshold 80.0 for 2 period(s)"
        assert summary["Alarm Period (s)"] == "300"

    def test_policy_without_alarms_reads_na(self):
        assert autoscaling_export._summarize_alarms([], {})["Alarm Names"] == "N/A"

    def test_unresolved_alarm_still_reports_its_name(self):
        """
        The alarm name is known from describe_policies even when the CloudWatch
        join fails; the name is more useful than dropping the row's alarm data.
        """
        summary = autoscaling_export._summarize_alarms([{"AlarmName": "cpu-high"}], {})

        assert summary["Alarm Names"] == "cpu-high"
        assert summary["Alarm Metric"] == "N/A"

    @mock_aws
    def test_fetch_policy_alarms_reads_cloudwatch(self):
        cw_client = boto3.client("cloudwatch", region_name=REGION)
        cw_client.put_metric_alarm(
            AlarmName="cpu-high",
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Statistic="Average",
            ComparisonOperator="GreaterThanThreshold",
            Threshold=80.0,
            EvaluationPeriods=2,
            Period=300,
        )

        alarms = autoscaling_export._fetch_policy_alarms(REGION, {"cpu-high"})

        assert alarms["cpu-high"]["MetricName"] == "CPUUtilization"

    def test_cloudwatch_failure_degrades_without_raising(self, monkeypatch):
        """
        The alarm join is enrichment. A CloudWatch permission gap must not fail
        a region whose policies were collected successfully.
        """

        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                "DescribeAlarms",
            )

        monkeypatch.setattr(autoscaling_export.utils, "get_boto3_client", boom)

        assert autoscaling_export._fetch_policy_alarms(REGION, {"cpu-high"}) == {}

    def test_no_alarm_names_makes_no_api_call(self, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("no CloudWatch client should be created")

        monkeypatch.setattr(autoscaling_export.utils, "get_boto3_client", boom)

        assert autoscaling_export._fetch_policy_alarms(REGION, set()) == {}


class TestScalingPolicyCollection:
    @mock_aws
    def test_step_policy_round_trips_through_the_region_scan(self):
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "web-asg")
        client.put_scaling_policy(
            AutoScalingGroupName="web-asg",
            PolicyName="step-up",
            PolicyType="StepScaling",
            AdjustmentType="ChangeInCapacity",
            MetricAggregationType="Average",
            StepAdjustments=[
                {"MetricIntervalLowerBound": 0, "MetricIntervalUpperBound": 10,
                 "ScalingAdjustment": 1},
                {"MetricIntervalLowerBound": 10, "ScalingAdjustment": 2},
            ],
        )

        rows = autoscaling_export._scan_scaling_policies_region(REGION)

        assert len(rows) == 1
        assert rows[0]["Policy Name"] == "step-up"
        assert rows[0]["Step Adjustments"] == "0.0 to 10.0: +1; >= 10.0: +2"

    @mock_aws
    def test_predictive_policy_round_trips_through_the_region_scan(self):
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "web-asg")
        client.put_scaling_policy(
            AutoScalingGroupName="web-asg",
            PolicyName="predictive",
            PolicyType="PredictiveScaling",
            PredictiveScalingConfiguration={
                "MetricSpecifications": [{
                    "TargetValue": 50.0,
                    "PredefinedMetricPairSpecification": {
                        "PredefinedMetricType": "ASGCPUUtilization"
                    },
                }],
                "Mode": "ForecastAndScale",
                "SchedulingBufferTime": 300,
            },
        )

        rows = autoscaling_export._scan_scaling_policies_region(REGION)

        assert rows[0]["Predictive Mode"] == "ForecastAndScale"
        assert "ASGCPUUtilization" in rows[0]["Policy Detail"]

    @mock_aws
    def test_region_api_failure_raises_not_empty(self, monkeypatch):
        """
        This collector previously swallowed per-region errors into an empty
        list — the Tier-3 PARTIAL pattern. It must now fail loud.
        """

        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                "DescribePolicies",
            )

        monkeypatch.setattr(autoscaling_export.utils, "get_boto3_client", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            autoscaling_export._scan_scaling_policies_region(REGION)

    @mock_aws
    def test_collect_scaling_policies_surfaces_failed_regions(self):
        def boom(region):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                "DescribePolicies",
            )

        original = autoscaling_export._scan_scaling_policies_region
        autoscaling_export._scan_scaling_policies_region = boom
        try:
            policies, failed_regions = autoscaling_export.collect_scaling_policies([REGION])
        finally:
            autoscaling_export._scan_scaling_policies_region = original

        assert policies == []
        assert [r for r, _ in failed_regions] == [REGION]

    @mock_aws
    def test_malformed_policy_is_skipped_not_fatal(self, monkeypatch):
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "web-asg")
        for name in ("good-policy", "bad-policy"):
            client.put_scaling_policy(
                AutoScalingGroupName="web-asg",
                PolicyName=name,
                PolicyType="SimpleScaling",
                AdjustmentType="ChangeInCapacity",
                ScalingAdjustment=1,
            )

        original = autoscaling_export._build_policy_row

        def raise_for_bad(policy, region, alarms):
            if policy.get("PolicyName") == "bad-policy":
                raise KeyError("SomeUnexpectedField")
            return original(policy, region, alarms)

        monkeypatch.setattr(autoscaling_export, "_build_policy_row", raise_for_bad)

        rows = autoscaling_export._scan_scaling_policies_region(REGION)

        names = {row["Policy Name"] for row in rows}
        assert "good-policy" in names, "healthy policy was lost when a sibling failed"
        assert "bad-policy" not in names


class TestMergeFailedRegions:
    def test_regions_failing_multiple_scopes_are_reported_once(self):
        merged = autoscaling_export._merge_failed_regions(
            [("us-east-1", "asg scope failed")],
            [("us-east-1", "policy scope failed"), ("us-west-2", "policy scope failed")],
        )

        assert dict(merged) == {
            "us-east-1": "asg scope failed",
            "us-west-2": "policy scope failed",
        }

    def test_empty_and_none_inputs_are_tolerated(self):
        assert autoscaling_export._merge_failed_regions([], None) == []


# ---------------------------------------------------------------------------
# Warm pools, scaling activity, instance refreshes (Issue #261)
# ---------------------------------------------------------------------------


class TestWarmPools:
    @mock_aws
    def test_warm_pool_is_collected(self):
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "web-asg")
        client.put_warm_pool(AutoScalingGroupName="web-asg", MinSize=2)

        rows = autoscaling_export._scan_warm_pools_region(REGION)

        assert len(rows) == 1
        assert rows[0]["ASG Name"] == "web-asg"
        assert rows[0]["Min Size"] == 2

    @mock_aws
    def test_group_without_a_warm_pool_yields_no_row(self):
        """No warm pool is a normal state, not a failure."""
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "web-asg")

        assert autoscaling_export._scan_warm_pools_region(REGION) == []

    @mock_aws
    def test_one_group_failing_does_not_discard_the_region(self, monkeypatch):
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "good-asg")
        _create_asg(client, "bad-asg")
        client.put_warm_pool(AutoScalingGroupName="good-asg", MinSize=1)

        real_get_client = autoscaling_export.utils.get_boto3_client

        class FailBadGroup:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def describe_warm_pool(self, **kwargs):
                if kwargs.get("AutoScalingGroupName") == "bad-asg":
                    raise botocore.exceptions.ClientError(
                        {"Error": {"Code": "ValidationError", "Message": "nope"}},
                        "DescribeWarmPool",
                    )
                return self._wrapped.describe_warm_pool(**kwargs)

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        monkeypatch.setattr(
            autoscaling_export.utils,
            "get_boto3_client",
            lambda *a, **k: FailBadGroup(real_get_client(*a, **k)),
        )

        rows = autoscaling_export._scan_warm_pools_region(REGION)

        assert {row["ASG Name"] for row in rows} == {"good-asg"}

    @mock_aws
    def test_region_api_failure_raises_not_empty(self, monkeypatch):
        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                "DescribeAutoScalingGroups",
            )

        monkeypatch.setattr(autoscaling_export.utils, "get_boto3_client", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            autoscaling_export._scan_warm_pools_region(REGION)


class TestScalingActivity:
    def test_activity_row_is_built_from_the_api_shape(self):
        """
        moto returns an empty activity list for every operation, so the
        collector cannot be exercised end to end under mocking — the row
        builder is unit tested instead.
        """
        row = autoscaling_export._build_activity_row(
            {
                "ActivityId": "activity-123",
                "AutoScalingGroupName": "web-asg",
                "StatusCode": "Successful",
                "StatusMessage": "instance launched",
                "Description": "Launching a new EC2 instance: i-abc",
                "Cause": "an instance was started in response to a difference "
                         "between desired and actual capacity",
                "Progress": 100,
                "Details": '{"Availability Zone":"us-east-1a"}',
            },
            REGION,
            "web-asg",
        )

        assert row["Activity ID"] == "activity-123"
        assert row["Status"] == "Successful"
        assert row["Progress (%)"] == 100
        assert row["Cause"].startswith("an instance was started")
        assert row["Start Time"] == "N/A"

    @mock_aws
    def test_group_with_no_recorded_activity_yields_no_rows(self):
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "web-asg")

        assert autoscaling_export._scan_scaling_activities_region(REGION) == []

    @mock_aws
    def test_empty_region_returns_empty_list(self):
        boto3.client("autoscaling", region_name=REGION)

        assert autoscaling_export._scan_scaling_activities_region(REGION) == []

    @mock_aws
    def test_region_api_failure_raises_not_empty(self, monkeypatch):
        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                "DescribeAutoScalingGroups",
            )

        monkeypatch.setattr(autoscaling_export.utils, "get_boto3_client", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            autoscaling_export._scan_scaling_activities_region(REGION)


class TestScalingActivityLimit:
    """
    Activity history is unbounded and is the only collector here with a real
    runtime cost, so the cap must actually be read from configuration.
    """

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.setattr(
            autoscaling_export.utils, "get_config", lambda: (None, {})
        )

        assert autoscaling_export._scaling_activity_limit() == 100

    def test_configured_value_is_used(self, monkeypatch):
        monkeypatch.setattr(
            autoscaling_export.utils,
            "get_config",
            lambda: (None, {"advanced_settings": {"performance": {"scaling_activity_limit": 25}}}),
        )

        assert autoscaling_export._scaling_activity_limit() == 25

    @pytest.mark.parametrize("bad_value", ["not-a-number", None, 0, -5])
    def test_malformed_or_nonpositive_value_falls_back(self, monkeypatch, bad_value):
        """A bad config value must not become an unbounded pull."""
        monkeypatch.setattr(
            autoscaling_export.utils,
            "get_config",
            lambda: (
                None,
                {"advanced_settings": {"performance": {"scaling_activity_limit": bad_value}}},
            ),
        )

        assert autoscaling_export._scaling_activity_limit() == 100

    @mock_aws
    def test_limit_is_passed_to_the_paginator(self, monkeypatch):
        client = boto3.client("autoscaling", region_name=REGION)
        _create_asg(client, "web-asg")
        monkeypatch.setattr(autoscaling_export, "_scaling_activity_limit", lambda: 7)

        seen = {}
        real_get_client = autoscaling_export.utils.get_boto3_client

        class CapturePagination:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def get_paginator(self, name):
                paginator = self._wrapped.get_paginator(name)
                if name != "describe_scaling_activities":
                    return paginator

                real_paginate = paginator.paginate

                def capturing(**kwargs):
                    seen.update(kwargs.get("PaginationConfig", {}))
                    return real_paginate(**kwargs)

                paginator.paginate = capturing
                return paginator

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        monkeypatch.setattr(
            autoscaling_export.utils,
            "get_boto3_client",
            lambda *a, **k: CapturePagination(real_get_client(*a, **k)),
        )

        autoscaling_export._scan_scaling_activities_region(REGION)

        assert seen.get("MaxItems") == 7


class TestInstanceRefreshes:
    """
    moto does not implement describe_instance_refreshes, so the collector
    cannot be exercised end to end under mocking — the row builder is unit
    tested instead, and the per-group loop means an empty account never
    reaches the call.
    """

    def test_refresh_row_is_built_from_the_api_shape(self):
        row = autoscaling_export._build_instance_refresh_row(
            {
                "InstanceRefreshId": "refresh-123",
                "AutoScalingGroupName": "web-asg",
                "Status": "InProgress",
                "StatusReason": "Replacing instances",
                "PercentageComplete": 40,
                "InstancesToUpdate": 3,
                "Preferences": {
                    "MinHealthyPercentage": 90,
                    "InstanceWarmup": 300,
                    "CheckpointPercentages": [20, 50, 100],
                    "SkipMatching": True,
                },
            },
            REGION,
            "web-asg",
        )

        assert row["Instance Refresh ID"] == "refresh-123"
        assert row["Status"] == "InProgress"
        assert row["Percentage Complete"] == 40
        assert row["Min Healthy Percentage"] == 90
        assert row["Checkpoint Percentages"] == "20, 50, 100"
        assert row["Skip Matching"] is True

    def test_absent_preferences_report_na(self):
        row = autoscaling_export._build_instance_refresh_row(
            {"InstanceRefreshId": "refresh-123", "Status": "Successful"},
            REGION,
            "web-asg",
        )

        assert row["Min Healthy Percentage"] == "N/A"
        assert row["Checkpoint Percentages"] == "N/A"

    @mock_aws
    def test_empty_region_never_calls_the_unmocked_api(self):
        """
        With no Auto Scaling Groups the per-group loop makes no call at all,
        which is what keeps the smoke test passing despite the moto gap.
        """
        boto3.client("autoscaling", region_name=REGION)

        assert autoscaling_export._scan_instance_refreshes_region(REGION) == []


class TestAlarmJoinPagination:
    """
    Regression tests for Issue #268.

    ``describe_alarms`` returns at most MaxRecords per response (default 50)
    and pages via NextToken. Reading only the first response silently dropped
    every alarm past that page — in a live account, a 100-name chunk resolved
    50 of 100, leaving 50 policies with an alarm name but no metric.

    moto does NOT emulate the MaxRecords page limit — it returns every matching
    alarm in a single response — so a moto-only test passes with the bug
    present. These tests emulate the real paging contract instead.
    """

    @staticmethod
    def _paging_client(real_client, page_size=50):
        """
        Wrap a client so describe_alarms pages the way AWS actually does.

        The real botocore Paginator is driven on top of the wrapper, so this
        exercises the same machinery production uses. Patching happens before
        get_paginator() so the paginator binds the emulating method.
        """
        all_alarms = real_client.describe_alarms()["MetricAlarms"]
        by_name = {a["AlarmName"]: a for a in all_alarms}

        def describe_alarms(**kwargs):
            requested = kwargs.get("AlarmNames") or sorted(by_name)
            matched = [by_name[n] for n in requested if n in by_name]
            start = int(kwargs.get("NextToken") or 0)
            window = matched[start:start + page_size]
            response = {"MetricAlarms": window, "CompositeAlarms": []}
            if start + page_size < len(matched):
                response["NextToken"] = str(start + page_size)
            return response

        real_client.describe_alarms = describe_alarms
        return real_client

    @mock_aws
    def test_alarms_beyond_the_first_page_are_resolved(self, monkeypatch):
        """139 alarms across a 50-per-page API must all resolve."""
        cw_client = boto3.client("cloudwatch", region_name=REGION)
        names = [f"alarm-{i:03d}" for i in range(139)]
        for name in names:
            cw_client.put_metric_alarm(
                AlarmName=name,
                Namespace="AWS/EC2",
                MetricName="CPUUtilization",
                Statistic="Average",
                ComparisonOperator="GreaterThanThreshold",
                Threshold=80.0,
                EvaluationPeriods=2,
                Period=300,
            )

        paging_client = self._paging_client(cw_client)
        monkeypatch.setattr(
            autoscaling_export.utils, "get_boto3_client", lambda *a, **k: paging_client
        )

        alarms = autoscaling_export._fetch_policy_alarms(REGION, set(names))

        assert len(alarms) == 139, (
            f"resolved {len(alarms)} of 139 — alarms past the first response "
            "page were dropped"
        )
        assert alarms["alarm-138"]["MetricName"] == "CPUUtilization"

    @mock_aws
    def test_more_names_than_one_chunk_still_resolve_completely(self, monkeypatch):
        """
        Name chunking (100) and response paging (50) are independent limits;
        both have to be handled for a large account to resolve fully.
        """
        cw_client = boto3.client("cloudwatch", region_name=REGION)
        names = [f"alarm-{i:03d}" for i in range(250)]
        for name in names:
            cw_client.put_metric_alarm(
                AlarmName=name,
                Namespace="AWS/EC2",
                MetricName="CPUUtilization",
                Statistic="Average",
                ComparisonOperator="GreaterThanThreshold",
                Threshold=80.0,
                EvaluationPeriods=2,
                Period=300,
            )

        paging_client = self._paging_client(cw_client)
        monkeypatch.setattr(
            autoscaling_export.utils, "get_boto3_client", lambda *a, **k: paging_client
        )

        alarms = autoscaling_export._fetch_policy_alarms(REGION, set(names))

        assert len(alarms) == 250
