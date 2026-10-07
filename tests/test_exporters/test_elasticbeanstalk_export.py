#!/usr/bin/env python3
"""
Moto-based tests for elasticbeanstalk_export.py.

Focus: the silent-collection-failure contract (Tier-3 PARTIAL). See
.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md
"""

import sys
from pathlib import Path

import boto3
import botocore
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import elasticbeanstalk_export  # noqa: E402
from elasticbeanstalk_export import collect_applications_from_region  # noqa: E402

REGION = "us-east-1"


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _create_application(client, name):
    """Create an Elastic Beanstalk application named ``name``."""
    client.create_application(ApplicationName=name, Description=f"{name} description")


class TestCollectApplicationsFromRegion:
    """Happy-path collection."""

    @mock_aws
    def test_collects_applications(self):
        client = boto3.client("elasticbeanstalk", region_name=REGION)
        _create_application(client, "web-app")

        rows = collect_applications_from_region(REGION)

        names = {row["Application Name"] for row in rows}
        assert "web-app" in names

    @mock_aws
    def test_empty_region_returns_empty_list(self):
        boto3.client("elasticbeanstalk", region_name=REGION)  # region exists, no apps

        rows = collect_applications_from_region(REGION)

        assert rows == []


class TestSilentCollectionFailureRegression:
    """
    Regression tests for the 07.15 / 07.16.2026 audits: exporters silently lost
    data because a collection error was swallowed to an empty list,
    indistinguishable from a genuinely empty region.
    """

    @mock_aws
    def test_malformed_item_is_skipped_not_fatal(self, monkeypatch):
        """One application that fails to process must not discard the whole region."""
        client = boto3.client("elasticbeanstalk", region_name=REGION)
        _create_application(client, "good-app")
        _create_application(client, "bad-app")

        original = elasticbeanstalk_export._build_application_row

        def raise_for_bad(app, region):
            if app.get("ApplicationName") == "bad-app":
                raise KeyError("SomeUnexpectedField")
            return original(app, region)

        monkeypatch.setattr(elasticbeanstalk_export, "_build_application_row", raise_for_bad)

        rows = collect_applications_from_region(REGION)

        names = {row["Application Name"] for row in rows}
        assert "good-app" in names, "healthy application was lost when a sibling failed"
        assert "bad-app" not in names, "malformed application should have been skipped"

    @mock_aws
    def test_region_api_failure_raises_not_empty(self, monkeypatch):
        """
        A region-level API failure must propagate (so the caller can record a
        FAILED region) rather than being swallowed into an empty list.
        """

        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "DescribeApplications",
            )

        monkeypatch.setattr(elasticbeanstalk_export.utils, "get_boto3_client", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            collect_applications_from_region(REGION)

    @mock_aws
    def test_collect_applications_surfaces_failed_regions(self, monkeypatch):
        """
        The scope wrapper must return failed regions via collect_failures, not
        drop them — this is what lets export write a FAILED marker + exit 1
        while still writing the always-on Summary sheet workbook.
        """

        def boom(region):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                "DescribeApplications",
            )

        monkeypatch.setattr(elasticbeanstalk_export, "collect_applications_from_region", boom)

        applications, failed_regions = elasticbeanstalk_export.collect_applications([REGION])

        assert applications == []
        assert [r for r, _ in failed_regions] == [REGION]


# ---------------------------------------------------------------------------
# Platform branch / version capture
#
# moto limit (memory: moto-testing-limits): moto does not implement
# ListPlatformBranches, so lifecycle lookups are exercised with a stub client.
# Real-account behaviour of that call is NOT proven by these tests.
# ---------------------------------------------------------------------------

import datetime  # noqa: E402

AL2023_BRANCH = "Python 3.11 running on 64bit Amazon Linux 2023"
AL2023_ARN = f"arn:aws:elasticbeanstalk:{REGION}::platform/{AL2023_BRANCH}/4.0.1"


class TestParsePlatformArn:
    def test_branch_with_spaces_and_version(self):
        cols = elasticbeanstalk_export.parse_platform_arn(AL2023_ARN, "N/A")
        assert cols["Platform Branch"] == AL2023_BRANCH
        assert cols["Platform Version"] == "4.0.1"
        assert cols["Platform"] == AL2023_BRANCH
        assert cols["Platform ARN"] == AL2023_ARN

    def test_custom_platform_arn_with_account(self):
        arn = "arn:aws:elasticbeanstalk:us-east-1:123456789012:platform/MyPlatform/1.2.3"
        cols = elasticbeanstalk_export.parse_platform_arn(arn, "N/A")
        assert cols["Platform Branch"] == "MyPlatform"
        assert cols["Platform Version"] == "1.2.3"

    def test_missing_arn_with_solution_stack_only(self):
        stack = "64bit Amazon Linux 2018.03 v2.12.1 running Python 3.6"
        cols = elasticbeanstalk_export.parse_platform_arn("N/A", stack)
        assert cols["Platform"] == stack
        assert cols["Platform Branch"] == "N/A"
        assert cols["Platform Version"] == "N/A"
        assert cols["Platform ARN"] == "N/A"
        assert cols["Solution Stack Name"] == stack

    def test_missing_everything_is_na(self):
        cols = elasticbeanstalk_export.parse_platform_arn("N/A", "N/A")
        assert set(cols.values()) == {"N/A"}

    def test_unparseable_arn_not_guessed(self):
        cols = elasticbeanstalk_export.parse_platform_arn("arn:aws:elasticbeanstalk:::platform/NoVersion", "N/A")
        assert cols["Platform Branch"] == "N/A"
        assert cols["Platform Version"] == "N/A"


class _FakeEB:
    """Stub EB client: one environment, scripted ListPlatformBranches."""

    def __init__(self, env, branch_pages=None, branch_error=None):
        self._env = env
        self._pages = list(branch_pages or [])
        self._error = branch_error
        self.branch_calls = 0

    def get_paginator(self, name):
        assert name == "describe_environments"
        env = self._env

        class _P:
            def paginate(self):
                return [{"Environments": [env]}]

        return _P()

    def list_platform_branches(self, **kwargs):
        self.branch_calls += 1
        if self._error:
            raise self._error
        return self._pages.pop(0)


def _env(arn=None, stack=None):
    env = {
        "EnvironmentName": "e1", "EnvironmentId": "e-1", "ApplicationName": "a",
        "Status": "Ready", "Health": "Green", "HealthStatus": "Ok",
        "Tier": {"Name": "WebServer", "Type": "Standard"},
        "DateCreated": datetime.datetime(2026, 1, 1), "DateUpdated": datetime.datetime(2026, 1, 2),
    }
    if arn:
        env["PlatformArn"] = arn
    if stack:
        env["SolutionStackName"] = stack
    return env


@pytest.fixture(autouse=True)
def clear_branch_cache():
    elasticbeanstalk_export._BRANCH_STATE_CACHE.clear()
    yield
    elasticbeanstalk_export._BRANCH_STATE_CACHE.clear()


def _collect(monkeypatch, fake):
    monkeypatch.setattr(elasticbeanstalk_export.utils, "get_boto3_client", lambda *a, **k: fake)
    return elasticbeanstalk_export.collect_environments_from_region(REGION)


class TestEnvironmentPlatformColumns:
    def test_lifecycle_state_paginated_and_columns_appended(self, monkeypatch):
        pages = [
            {"PlatformBranchSummaryList": [{"BranchName": "Other", "LifecycleState": "beta"}], "NextToken": "t"},
            {"PlatformBranchSummaryList": [{"BranchName": AL2023_BRANCH, "LifecycleState": "supported"}]},
        ]
        fake = _FakeEB(_env(arn=AL2023_ARN), branch_pages=pages)
        rows = _collect(monkeypatch, fake)
        row = rows[0]
        assert row["Platform"] == AL2023_BRANCH
        assert row["Platform Branch State"] == "Supported"
        assert list(row)[-5:] == [
            "Platform Branch", "Platform Version", "Platform ARN",
            "Solution Stack Name", "Platform Branch State",
        ]
        assert list(row).index("Platform") == 7  # existing position unchanged
        assert fake.branch_calls == 2

    def test_lookup_failure_is_explicit_state(self, monkeypatch):
        err = botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListPlatformBranches"
        )
        rows = _collect(monkeypatch, _FakeEB(_env(arn=AL2023_ARN), branch_error=err))
        assert rows[0]["Platform Branch State"] == "Lookup Failed (AccessDenied)"
        assert rows[0]["Platform Branch"] == AL2023_BRANCH  # parse unaffected

    def test_branch_not_found_state(self, monkeypatch):
        pages = [{"PlatformBranchSummaryList": [{"BranchName": "Other", "LifecycleState": "supported"}]}]
        rows = _collect(monkeypatch, _FakeEB(_env(arn=AL2023_ARN), branch_pages=pages))
        assert rows[0]["Platform Branch State"] == "Not Found"

    def test_solution_stack_only_makes_no_lookup(self, monkeypatch):
        fake = _FakeEB(_env(stack="64bit Amazon Linux 2018.03 v2.12.1 running Python 3.6"))
        rows = _collect(monkeypatch, fake)
        assert rows[0]["Platform"].startswith("64bit Amazon Linux")
        assert rows[0]["Platform Branch"] == "N/A"
        assert rows[0]["Platform Branch State"] == "N/A"
        assert fake.branch_calls == 0

    def test_lookup_cached_per_region(self, monkeypatch):
        pages = [{"PlatformBranchSummaryList": [{"BranchName": AL2023_BRANCH, "LifecycleState": "deprecated"}]}]
        fake = _FakeEB(_env(arn=AL2023_ARN), branch_pages=pages)
        monkeypatch.setattr(elasticbeanstalk_export.utils, "get_boto3_client", lambda *a, **k: fake)
        elasticbeanstalk_export.platform_columns(REGION, AL2023_ARN, "N/A")
        cols = elasticbeanstalk_export.platform_columns(REGION, AL2023_ARN, "N/A")
        assert cols["Platform Branch State"] == "Deprecated"
        assert fake.branch_calls == 1


class TestSummaryTopPlatforms:
    def test_groups_by_branch_not_version(self):
        envs = [
            {"Status": "Ready", "Health Status": "Ok", "Tier Name": "WebServer", "Region": REGION,
             "Platform": AL2023_BRANCH, "Platform Branch": AL2023_BRANCH},
            {"Status": "Ready", "Health Status": "Ok", "Tier Name": "WebServer", "Region": REGION,
             "Platform": AL2023_BRANCH, "Platform Branch": AL2023_BRANCH},
            {"Status": "Ready", "Health Status": "Ok", "Tier Name": "WebServer", "Region": REGION,
             "Platform": "legacy stack", "Platform Branch": "N/A"},
        ]
        summary = elasticbeanstalk_export.generate_summary([], envs, [], [])
        top = next(r for r in summary if r["Metric"] == "Top 5 Platforms")
        assert top["Count"] == 2
        assert f"{AL2023_BRANCH}: 2" in top["Details"]
        assert "legacy stack: 1" in top["Details"]
