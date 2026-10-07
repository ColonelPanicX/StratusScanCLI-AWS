#!/usr/bin/env python3
"""
Moto-based tests for iam_export.py.

Covers:
- collect_iam_user_information()
"""

import sys
from pathlib import Path

import boto3
import botocore.exceptions
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import iam_export  # noqa: E402
from iam_export import (  # noqa: E402
    collect_iam_role_information,
    collect_iam_user_information,
    collect_inline_policies,
    collect_managed_policies,
)

REGION = "us-east-1"


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


class TestCollectIamUserInformation:
    """Tests for collect_iam_user_information()."""

    @mock_aws
    def test_created_user_appears_in_results(self):
        """A newly created IAM user is returned by the collector."""
        iam = boto3.client("iam", region_name=REGION)
        iam.create_user(UserName="test-user")

        result = collect_iam_user_information()

        assert isinstance(result, list)
        assert len(result) >= 1
        assert any(row["User Name"] == "test-user" for row in result)

    @mock_aws
    def test_result_contains_expected_columns(self):
        """Each row contains the expected column keys."""
        iam = boto3.client("iam", region_name=REGION)
        iam.create_user(UserName="col-check-user")

        result = collect_iam_user_information()

        assert len(result) >= 1
        row = result[0]
        for col in ("User Name", "MFA", "Console Access", "Creation Date"):
            assert col in row, f"Missing column: {col}"

    @mock_aws
    def test_user_with_access_key_reflects_key_data(self):
        """Access key metadata is captured for users that have keys."""
        iam = boto3.client("iam", region_name=REGION)
        iam.create_user(UserName="key-user")
        iam.create_access_key(UserName="key-user")

        result = collect_iam_user_information()

        assert any(row["User Name"] == "key-user" for row in result)

    @mock_aws
    def test_empty_account_returns_empty_list(self):
        """Account with no IAM users returns an empty list."""
        result = collect_iam_user_information()
        assert result == []


class TestSilentCollectionFailureRegression:
    """
    Regression tests for the 07.15.2026 / 07.16.2026 silent-collection-failure
    audits: IAM's account-scope collectors used to swallow a collection error
    into an empty list, indistinguishable from a genuinely empty account. IAM
    is global/account-scope (not multi-region), so each top-level collector
    (users, roles, managed policies, inline policies) is verified separately:
    a malformed item must be skipped without sinking the whole collection,
    and an account-scope API failure must raise rather than return [].
    See .collab/audit/07.16.2026-silent-collection-failure-blast-radius.md
    """

    # -- Users ---------------------------------------------------------

    @mock_aws
    def test_malformed_user_is_skipped_not_fatal(self, monkeypatch):
        """One user that fails to process must not discard the others."""
        iam = boto3.client("iam", region_name=REGION)
        iam.create_user(UserName="good-user")
        iam.create_user(UserName="bad-user")

        original = iam_export._build_user_row

        def raise_for_bad(iam_client, user):
            if user.get("UserName") == "bad-user":
                raise KeyError("SomeUnexpectedField")
            return original(iam_client, user)

        monkeypatch.setattr(iam_export, "_build_user_row", raise_for_bad)

        result = collect_iam_user_information()

        names = {row["User Name"] for row in result}
        assert "good-user" in names, "healthy user was lost when a sibling failed"
        assert "bad-user" not in names, "malformed user should have been skipped"

    @mock_aws
    def test_users_account_scope_failure_raises_not_empty(self, monkeypatch):
        """An account-scope API failure must propagate, not collapse to []."""

        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "ListUsers",
            )

        monkeypatch.setattr(iam_export.utils, "get_boto3_client", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            collect_iam_user_information()

    # -- Roles -----------------------------------------------------------

    @mock_aws
    def test_malformed_role_is_skipped_not_fatal(self, monkeypatch):
        """One role that fails to process must not discard the others."""
        iam = boto3.client("iam", region_name=REGION)
        trust_policy = (
            '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
            '"Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}'
        )
        iam.create_role(RoleName="good-role", AssumeRolePolicyDocument=trust_policy)
        iam.create_role(RoleName="bad-role", AssumeRolePolicyDocument=trust_policy)

        original = iam_export._build_role_row

        def raise_for_bad(iam_client, role):
            if role.get("RoleName") == "bad-role":
                raise KeyError("SomeUnexpectedField")
            return original(iam_client, role)

        monkeypatch.setattr(iam_export, "_build_role_row", raise_for_bad)

        result = collect_iam_role_information()

        names = {row["Role Name"] for row in result}
        assert "good-role" in names, "healthy role was lost when a sibling failed"
        assert "bad-role" not in names, "malformed role should have been skipped"

    @mock_aws
    def test_roles_account_scope_failure_raises_not_empty(self, monkeypatch):
        """An account-scope API failure must propagate, not collapse to []."""

        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "ListRoles",
            )

        monkeypatch.setattr(iam_export.utils, "get_boto3_client", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            collect_iam_role_information()

    # -- Managed policies --------------------------------------------------

    @mock_aws
    def test_malformed_managed_policy_is_skipped_not_fatal(self, monkeypatch):
        """One policy that fails to process must not discard the others."""
        iam = boto3.client("iam", region_name=REGION)
        policy_doc = (
            '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
            '"Action": "s3:GetObject", "Resource": "*"}]}'
        )
        iam.create_policy(PolicyName="good-policy", PolicyDocument=policy_doc)
        iam.create_policy(PolicyName="bad-policy", PolicyDocument=policy_doc)

        original = iam_export.process_managed_policy

        def raise_for_bad(iam_client, policy, policy_type):
            if policy.get("PolicyName") == "bad-policy":
                raise KeyError("SomeUnexpectedField")
            return original(iam_client, policy, policy_type)

        monkeypatch.setattr(iam_export, "process_managed_policy", raise_for_bad)

        iam_client = boto3.client("iam", region_name=REGION)
        result = collect_managed_policies(iam_client)

        names = {row["Policy Name"] for row in result}
        assert "good-policy" in names, "healthy policy was lost when a sibling failed"
        assert "bad-policy" not in names, "malformed policy should have been skipped"

    @mock_aws
    def test_managed_policies_account_scope_failure_raises_not_empty(self, monkeypatch):
        """An account-scope API failure must propagate, not collapse to []."""
        iam_client = boto3.client("iam", region_name=REGION)

        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "ListPolicies",
            )

        monkeypatch.setattr(iam_client, "get_paginator", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            collect_managed_policies(iam_client)

    # -- Inline policies -----------------------------------------------------

    @mock_aws
    def test_inline_policies_account_scope_failure_raises_not_empty(self, monkeypatch):
        """An account-scope API failure must propagate, not collapse to []."""
        iam_client = boto3.client("iam", region_name=REGION)

        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "ListUsers",
            )

        monkeypatch.setattr(iam_client, "get_paginator", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            collect_inline_policies(iam_client)


# ---------------------------------------------------------------------------
# Issue #309: unattended default, Groups, appended columns, failure states
# ---------------------------------------------------------------------------

TRUST = (
    '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
    '"Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}'
)
POLICY_DOC = (
    '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
    '"Action": "s3:GetObject", "Resource": "*"}]}'
)


def _boom(code, op):
    def raiser(*args, **kwargs):
        raise botocore.exceptions.ClientError(
            {"Error": {"Code": code, "Message": f"{op} denied"}}, op
        )
    return raiser


class TestUnattendedDefault:
    """Auto-run must select the comprehensive export, not menu option 1."""

    def _patch_main(self, monkeypatch):
        monkeypatch.setattr(iam_export.utils, "ensure_dependencies", lambda *a, **k: True)
        monkeypatch.setattr(iam_export.utils, "setup_logging", lambda *a, **k: None)
        monkeypatch.setattr(iam_export.utils, "print_script_banner", lambda *a, **k: ("123456789012", "TEST"))
        calls = []
        monkeypatch.setattr(iam_export, "_run_comprehensive_export", lambda *a: calls.append("all"))
        monkeypatch.setattr(iam_export, "_run_users_export", lambda *a: calls.append("users"))
        monkeypatch.setattr(iam_export, "_run_groups_export", lambda *a: calls.append("groups"))
        return calls

    def test_auto_run_runs_comprehensive_only(self, monkeypatch):
        monkeypatch.setenv("STRATUSSCAN_AUTO_RUN", "1")
        calls = self._patch_main(monkeypatch)
        iam_export.main()
        assert calls == ["all"]

    @pytest.mark.parametrize("pick,expected", [(1, "users"), (4, "all"), (5, "groups")])
    def test_interactive_options_still_reachable(self, monkeypatch, pick, expected):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        calls = self._patch_main(monkeypatch)
        monkeypatch.setattr(iam_export.utils, "prompt_menu", lambda *a, **k: pick)
        monkeypatch.setattr(iam_export.utils, "prompt_confirmation", lambda *a, **k: "confirm")
        iam_export.main()
        assert calls == [expected]

    def test_menu_numbering_1_to_4_unchanged_groups_is_5(self):
        assert (iam_export.CHOICE_USERS, iam_export.CHOICE_ROLES,
                iam_export.CHOICE_POLICIES, iam_export.CHOICE_ALL,
                iam_export.CHOICE_GROUPS) == (1, 2, 3, 4, 5)


class TestGroups:
    @mock_aws
    def test_group_with_and_without_members(self):
        iam = boto3.client("iam", region_name=REGION)
        iam.create_user(UserName="alice")
        iam.create_user(UserName="bob")
        iam.create_group(GroupName="devs", Path="/team/")
        iam.create_group(GroupName="empty")
        iam.add_user_to_group(GroupName="devs", UserName="alice")
        iam.add_user_to_group(GroupName="devs", UserName="bob")
        pol = iam.create_policy(PolicyName="p1", PolicyDocument=POLICY_DOC)["Policy"]["Arn"]
        iam.attach_group_policy(GroupName="devs", PolicyArn=pol)
        iam.put_group_policy(GroupName="devs", PolicyName="inl", PolicyDocument=POLICY_DOC)

        rows = {r["Group Name"]: r for r in iam_export.collect_iam_group_information()}

        devs = rows["devs"]
        assert devs["Group ARN"].endswith(":group/team/devs")
        assert devs["Path"] == "/team/"
        assert devs["Member Count"] == 2
        assert set(devs["Members"].split(", ")) == {"alice", "bob"}
        assert devs["Attached Managed Policies"] == "p1"
        assert devs["Inline Policy Count"] == 1
        assert devs["Collection Note"] == ""
        assert rows["empty"]["Member Count"] == 0
        assert rows["empty"]["Members"] == "None"
        assert rows["empty"]["Attached Managed Policies"] == "None"
        assert rows["empty"]["Inline Policy Count"] == 0

    @mock_aws
    def test_empty_account_returns_empty_list(self):
        assert iam_export.collect_iam_group_information() == []

    @mock_aws
    def test_member_lookup_failure_is_stated_in_row(self, monkeypatch):
        iam = boto3.client("iam", region_name=REGION)
        iam.create_group(GroupName="g1")
        real_client = iam_export.utils.get_boto3_client

        def wrapped(*a, **k):
            client = real_client(*a, **k)
            real_pag = client.get_paginator

            def pag(name):
                if name == "get_group":
                    raise botocore.exceptions.ClientError(
                        {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "GetGroup")
                return real_pag(name)
            monkeypatch.setattr(client, "get_paginator", pag)
            return client

        monkeypatch.setattr(iam_export.utils, "get_boto3_client", wrapped)
        row = iam_export.collect_iam_group_information()[0]
        assert row["Members"] == "Unavailable (AccessDenied)"
        assert row["Member Count"] == "Unavailable (AccessDenied)"
        assert "GetGroup failed" in row["Collection Note"]

    @mock_aws
    def test_list_groups_failure_raises_not_empty(self, monkeypatch):
        monkeypatch.setattr(iam_export.utils, "get_boto3_client", _boom("Throttling", "ListGroups"))
        with pytest.raises(botocore.exceptions.ClientError):
            iam_export.collect_iam_group_information()

    @mock_aws
    def test_groups_paginated_beyond_one_page(self):
        iam = boto3.client("iam", region_name=REGION)
        for i in range(105):
            iam.create_group(GroupName=f"g{i:03d}")
        assert len(iam_export.collect_iam_group_information()) == 105

    def test_join_capped_states_truncation(self):
        names = [f"user-{i:06d}" for i in range(5000)]
        text = iam_export._join_capped(names)
        assert len(text) <= 32767
        assert "more; list truncated" in text


class TestRoleColumns:
    @mock_aws
    def test_new_columns_appended_at_end_in_order(self):
        iam = boto3.client("iam", region_name=REGION)
        iam.create_role(RoleName="r1", AssumeRolePolicyDocument=TRUST)
        row = iam_export.collect_iam_role_information()[0]
        cols = list(row)
        assert cols[:14] == [
            'Role Name', 'Role Type', 'Trusted Entities', 'Trust Policy Summary',
            'Permission Policies', 'Last Used', 'Days Since Last Used',
            'Max Session Duration (Hours)', 'Cross-Account Access', 'Service Usage',
            'Creation Date', 'Path', 'Description', 'Tags']
        assert cols[14:] == ['Role ARN', 'Service-Linked', 'Permissions Boundary',
                             'Inline Policy Count', 'Last Used Region', 'Collection Note']

    @mock_aws
    def test_service_linked_boundary_inline_and_region(self, monkeypatch):
        iam = boto3.client("iam", region_name=REGION)
        boundary = iam.create_policy(PolicyName="bnd", PolicyDocument=POLICY_DOC)["Policy"]["Arn"]
        iam.create_role(RoleName="plain", AssumeRolePolicyDocument=TRUST,
                        PermissionsBoundary=boundary)
        iam.put_role_policy(RoleName="plain", PolicyName="inl", PolicyDocument=POLICY_DOC)
        iam.create_role(RoleName="slr", Path="/aws-service-role/x.amazonaws.com/",
                        AssumeRolePolicyDocument=TRUST)

        rows = {r["Role Name"]: r for r in iam_export.collect_iam_role_information()}

        assert rows["plain"]["Role ARN"].endswith(":role/plain")
        assert rows["plain"]["Service-Linked"] == "No"
        assert rows["plain"]["Permissions Boundary"] == boundary
        assert rows["plain"]["Inline Policy Count"] == 1
        assert rows["plain"]["Last Used Region"] == "Never"
        # service-linked roles are flagged, not dropped
        assert rows["slr"]["Service-Linked"] == "Yes"
        assert rows["slr"]["Permissions Boundary"] == "None"

    @mock_aws
    def test_last_used_region_reported(self, monkeypatch):
        import datetime as dt
        iam = boto3.client("iam", region_name=REGION)
        iam.create_role(RoleName="used", AssumeRolePolicyDocument=TRUST)
        real = iam_export.utils.get_boto3_client

        def wrapped(*a, **k):
            client = real(*a, **k)
            orig = client.get_role

            def get_role(**kw):
                resp = orig(**kw)
                resp["Role"]["RoleLastUsed"] = {
                    "LastUsedDate": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                    "Region": "us-west-2",
                }
                return resp
            monkeypatch.setattr(client, "get_role", get_role)
            return client

        monkeypatch.setattr(iam_export.utils, "get_boto3_client", wrapped)
        row = iam_export.collect_iam_role_information()[0]
        assert row["Last Used Region"] == "us-west-2"

    @mock_aws
    def test_get_role_failure_is_stated(self, monkeypatch):
        iam = boto3.client("iam", region_name=REGION)
        iam.create_role(RoleName="r", AssumeRolePolicyDocument=TRUST)
        real = iam_export.utils.get_boto3_client

        def wrapped(*a, **k):
            client = real(*a, **k)
            monkeypatch.setattr(client, "get_role", _boom("AccessDenied", "GetRole"))
            return client

        monkeypatch.setattr(iam_export.utils, "get_boto3_client", wrapped)
        row = iam_export.collect_iam_role_information()[0]
        assert row["Permissions Boundary"] == "Unavailable (AccessDenied)"
        assert row["Last Used Region"] == "Unavailable (AccessDenied)"
        assert "GetRole failed" in row["Collection Note"]


class TestUserColumns:
    @mock_aws
    def test_appended_columns_and_two_access_keys(self):
        iam = boto3.client("iam", region_name=REGION)
        iam.create_user(UserName="u")
        k1 = iam.create_access_key(UserName="u")["AccessKey"]["AccessKeyId"]
        k2 = iam.create_access_key(UserName="u")["AccessKey"]["AccessKeyId"]
        pol = iam.create_policy(PolicyName="up", PolicyDocument=POLICY_DOC)["Policy"]["Arn"]
        iam.attach_user_policy(UserName="u", PolicyArn=pol)
        iam.put_user_policy(UserName="u", PolicyName="inl", PolicyDocument=POLICY_DOC)

        row = iam_export.collect_iam_user_information()[0]

        cols = list(row)
        assert cols[:11] == ['User Name', 'Groups', 'MFA', 'Password Age', 'Console Last Sign-in',
                             'Access Key ID', 'Active Key Age', 'Access Key Last Used',
                             'Creation Date', 'Console Access', 'Permission Policies']
        assert cols[11:] == ['Attached Managed Policies', 'Inline Policy Count', 'Collection Note']
        # both keys present in the single Access Key ID cell: key 2 is not dropped
        assert k1 in row["Access Key ID"] and k2 in row["Access Key ID"]
        assert row["Attached Managed Policies"] == "up"
        assert row["Inline Policy Count"] == 1
        assert row["Permission Policies"] == "up, inl (Inline)"

    @mock_aws
    def test_policy_lookup_failure_is_stated(self, monkeypatch):
        iam = boto3.client("iam", region_name=REGION)
        iam.create_user(UserName="u")
        real = iam_export.utils.get_boto3_client

        def wrapped(*a, **k):
            client = real(*a, **k)
            real_pag = client.get_paginator

            def pag(name):
                if name == "list_attached_user_policies":
                    raise botocore.exceptions.ClientError(
                        {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "ListAttachedUserPolicies")
                return real_pag(name)
            monkeypatch.setattr(client, "get_paginator", pag)
            return client

        monkeypatch.setattr(iam_export.utils, "get_boto3_client", wrapped)
        row = iam_export.collect_iam_user_information()[0]
        assert row["Attached Managed Policies"] == "Unavailable (AccessDenied)"
        assert row["Inline Policy Count"] == "Unavailable (AccessDenied)"
        assert "Policy lookup failed" in row["Collection Note"]


class TestPolicyColumns:
    @mock_aws
    def test_attached_flag_and_appended_columns(self):
        iam = boto3.client("iam", region_name=REGION)
        used = iam.create_policy(PolicyName="used", PolicyDocument=POLICY_DOC)["Policy"]["Arn"]
        iam.create_policy(PolicyName="idle", PolicyDocument=POLICY_DOC)
        iam.create_group(GroupName="g")
        iam.attach_group_policy(GroupName="g", PolicyArn=used)

        rows = {r["Policy Name"]: r for r in collect_managed_policies(boto3.client("iam", region_name=REGION))}

        assert rows["used"]["Attached To Anything"] == "Yes"
        assert rows["used"]["Attached To Count"] == 1
        assert rows["idle"]["Attached To Anything"] == "No"
        assert rows["idle"]["Usage Status"] == "Unused"
        cols = list(rows["used"])
        assert cols[-2:] == ['Attached To Anything', 'Collection Note']
        for col in ('Attached To Count', 'Default Version ID', 'Creation Date', 'Last Updated'):
            assert col in rows["used"]

    @mock_aws
    def test_entity_lookup_failure_is_not_reported_unused(self, monkeypatch):
        iam = boto3.client("iam", region_name=REGION)
        arn = iam.create_policy(PolicyName="p", PolicyDocument=POLICY_DOC)["Policy"]["Arn"]
        iam.create_group(GroupName="g")
        iam.attach_group_policy(GroupName="g", PolicyArn=arn)
        monkeypatch.setattr(iam_export, "get_policy_entities", _boom("AccessDenied", "ListEntitiesForPolicy"))

        row = collect_managed_policies(boto3.client("iam", region_name=REGION))[0]

        # falls back to ListPolicies' own AttachmentCount, never to a false "Unused"
        assert row["Usage Status"] == "Used"
        assert row["Attached To Anything"] == "Yes"
        assert row["Attached Users"] == "Unavailable (AccessDenied)"
        assert "ListEntitiesForPolicy failed" in row["Collection Note"]


class TestComprehensiveWorkbook:
    @mock_aws
    def test_all_sheets_in_one_workbook(self, monkeypatch, tmp_path):
        iam = boto3.client("iam", region_name=REGION)
        iam.create_user(UserName="u")
        iam.create_group(GroupName="g")
        iam.create_role(RoleName="r", AssumeRolePolicyDocument=TRUST)
        iam.create_policy(PolicyName="p", PolicyDocument=POLICY_DOC)
        iam.put_user_policy(UserName="u", PolicyName="inl", PolicyDocument=POLICY_DOC)

        captured = {}

        def fake_save(frames, filename):
            captured["frames"] = frames
            captured["filename"] = filename
            return tmp_path / "out.xlsx"

        monkeypatch.setattr(iam_export.utils, "save_multiple_dataframes_to_excel", fake_save)
        iam_export._run_comprehensive_export("123456789012", "TEST")

        assert list(captured["frames"]) == [
            "IAM Users", "IAM Roles", "IAM Policies", "IAM Groups", "IAM Inline Policies", "Summary"]
        assert "iam-comprehensive" in str(captured["filename"])

    @mock_aws
    def test_failed_scope_exits_nonzero_and_writes_marker(self, monkeypatch, tmp_path):
        iam = boto3.client("iam", region_name=REGION)
        iam.create_user(UserName="u")
        monkeypatch.setattr(iam_export, "collect_iam_group_information", _boom("Throttling", "ListGroups"))
        monkeypatch.setattr(iam_export.utils, "save_multiple_dataframes_to_excel",
                            lambda frames, filename: tmp_path / "out.xlsx")
        reported = {}
        monkeypatch.setattr(iam_export.utils, "report_collection_failures",
                            lambda acct, rtype, scopes: reported.update(rtype=rtype, scopes=scopes))

        with pytest.raises(SystemExit) as exc:
            iam_export._run_comprehensive_export("123456789012", "TEST")

        assert exc.value.code == 1
        assert reported["rtype"] == "iam-comprehensive"
        assert [name for name, _ in reported["scopes"]] == ["iam-groups"]
