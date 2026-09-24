"""
Unit tests for aws_client functions (utils.py).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import utils as aws_mod
from utils import (
    _DEFAULT_REGIONS,
    _GOVCLOUD_DEFAULT_REGIONS,
    detect_partition,
    get_boto3_client,
    get_cached_account_info,
    get_partition_default_region,
    get_partition_regions,
    is_auto_run,
    is_aws_region,
    is_service_available_in_partition,
    validate_aws_credentials,
    validate_aws_region,
)

# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


class TestIsAutoRun:
    def test_false_when_not_set(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        assert is_auto_run() is False

    def test_true_when_set_to_1(self, monkeypatch):
        monkeypatch.setenv("STRATUSSCAN_AUTO_RUN", "1")
        assert is_auto_run() is True

    def test_true_when_set_to_true(self, monkeypatch):
        monkeypatch.setenv("STRATUSSCAN_AUTO_RUN", "true")
        assert is_auto_run() is True


# ---------------------------------------------------------------------------
# is_aws_region
# ---------------------------------------------------------------------------


class TestIsAwsRegion:
    def test_valid_commercial(self):
        assert is_aws_region("us-east-1") is True
        assert is_aws_region("eu-west-1") is True
        assert is_aws_region("ap-southeast-2") is True

    def test_valid_govcloud(self):
        assert is_aws_region("us-gov-west-1") is True
        assert is_aws_region("us-gov-east-1") is True

    def test_invalid(self):
        assert is_aws_region("not-a-region") is False
        assert is_aws_region("") is False


# ---------------------------------------------------------------------------
# validate_aws_region
# ---------------------------------------------------------------------------


class TestValidateAwsRegion:
    def test_all_is_valid(self):
        assert validate_aws_region("all") is True

    def test_valid_region(self):
        assert validate_aws_region("us-east-1") is True

    def test_invalid_region(self):
        assert validate_aws_region("invalid") is False


# ---------------------------------------------------------------------------
# detect_partition
# ---------------------------------------------------------------------------


class TestDetectPartition:
    def test_govcloud_region_by_name(self):
        assert detect_partition("us-gov-west-1") == "aws-us-gov"
        assert detect_partition("us-gov-east-1") == "aws-us-gov"

    def test_commercial_region_by_name(self):
        assert detect_partition("us-east-1") == "aws"
        assert detect_partition("eu-west-1") == "aws"

    def test_no_region_defaults_to_aws_on_error(self):
        with patch("utils.boto3.Session", side_effect=Exception("no creds")):
            result = detect_partition(None)
        assert result in ("aws", "aws-us-gov")


# ---------------------------------------------------------------------------
# get_boto3_client — FIPS injection (security-critical)
# ---------------------------------------------------------------------------


class TestGetBoto3ClientFips:
    def test_govcloud_injects_fips(self):
        """GovCloud regions MUST use FIPS endpoints — security-critical property.

        FIPS must be set on the botocore Config, NOT passed as a client() kwarg
        (boto3 rejects ``use_fips_endpoint`` as a client argument — see the
        GovCloud regression fixed in fix/govcloud-fips-endpoint-kwarg).
        """
        mock_session = MagicMock()
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client

        with patch("utils.boto3.Session", return_value=mock_session), patch("utils.config_value", return_value={}):
            get_boto3_client("ec2", region_name="us-gov-west-1")

        call_kwargs = mock_session.client.call_args[1]
        assert "use_fips_endpoint" not in call_kwargs, (
            "use_fips_endpoint must NOT be a client() kwarg — boto3 rejects it"
        )
        assert call_kwargs["config"].use_fips_endpoint is True, (
            "FIPS endpoint must be injected for GovCloud region us-gov-west-1"
        )

    def test_govcloud_east_injects_fips(self):
        """GovCloud us-gov-east-1 also requires FIPS."""
        mock_session = MagicMock()
        mock_session.client.return_value = MagicMock()

        with patch("utils.boto3.Session", return_value=mock_session), patch("utils.config_value", return_value={}):
            get_boto3_client("s3", region_name="us-gov-east-1")

        call_kwargs = mock_session.client.call_args[1]
        assert "use_fips_endpoint" not in call_kwargs
        assert call_kwargs["config"].use_fips_endpoint is True

    def test_session_default_govcloud_region_injects_fips(self):
        """No region_name passed: a GovCloud session default region still gets FIPS."""
        mock_session = MagicMock()
        mock_session.region_name = "us-gov-west-1"
        mock_session.client.return_value = MagicMock()

        with patch("utils.boto3.Session", return_value=mock_session), patch("utils.config_value", return_value={}):
            get_boto3_client("ec2")

        assert mock_session.client.call_args[1]["config"].use_fips_endpoint is True

    def test_session_default_commercial_region_no_fips(self):
        mock_session = MagicMock()
        mock_session.region_name = "us-east-1"
        mock_session.client.return_value = MagicMock()

        with patch("utils.boto3.Session", return_value=mock_session), patch("utils.config_value", return_value={}):
            get_boto3_client("ec2")

        assert mock_session.client.call_args[1]["config"].use_fips_endpoint is not True

    def test_commercial_does_not_inject_fips(self):
        """Commercial regions must NOT have FIPS forced on."""
        mock_session = MagicMock()
        mock_session.client.return_value = MagicMock()

        with patch("utils.boto3.Session", return_value=mock_session), patch("utils.config_value", return_value={}):
            get_boto3_client("ec2", region_name="us-east-1")

        call_kwargs = mock_session.client.call_args[1]
        assert "use_fips_endpoint" not in call_kwargs
        assert call_kwargs["config"].use_fips_endpoint is None

    def test_includes_retry_config(self):
        mock_session = MagicMock()
        mock_session.client.return_value = MagicMock()

        with patch("utils.boto3.Session", return_value=mock_session), patch("utils.config_value", return_value={}):
            get_boto3_client("iam")

        call_kwargs = mock_session.client.call_args[1]
        assert "config" in call_kwargs
        assert hasattr(call_kwargs["config"], "retries")


# ---------------------------------------------------------------------------
# is_service_available_in_partition
# ---------------------------------------------------------------------------


class TestIsServiceAvailableInPartition:
    def test_cost_explorer_unavailable_in_govcloud(self):
        assert is_service_available_in_partition("ce", "aws-us-gov") is False

    def test_trusted_advisor_unavailable_in_govcloud(self):
        assert is_service_available_in_partition("trustedadvisor", "aws-us-gov") is False

    def test_ec2_available_everywhere(self):
        assert is_service_available_in_partition("ec2", "aws") is True
        assert is_service_available_in_partition("ec2", "aws-us-gov") is True

    def test_all_services_available_in_commercial(self):
        assert is_service_available_in_partition("ce", "aws") is True
        assert is_service_available_in_partition("globalaccelerator", "aws") is True


# ---------------------------------------------------------------------------
# get_partition_regions
# ---------------------------------------------------------------------------


class TestGetPartitionRegions:
    def test_govcloud_returns_two_regions(self):
        regions = get_partition_regions("aws-us-gov")
        assert set(regions) == {"us-gov-west-1", "us-gov-east-1"}

    def test_govcloud_uses_govcloud_constant(self):
        """GovCloud path must return _GOVCLOUD_DEFAULT_REGIONS, not commercial ones."""
        regions = get_partition_regions("aws-us-gov")
        assert set(regions) == set(_GOVCLOUD_DEFAULT_REGIONS)
        assert "us-east-1" not in regions
        assert "us-west-2" not in regions

    def test_commercial_returns_default_list(self):
        regions = get_partition_regions("aws")
        assert set(regions) == set(_DEFAULT_REGIONS)

    def test_unknown_partition_returns_commercial_fallback(self):
        """Unknown partition must fall back to commercial defaults (not GovCloud)."""
        regions = get_partition_regions("aws-cn")
        assert set(regions) == set(_DEFAULT_REGIONS)

    def test_unknown_partition_logs_error(self, caplog):
        """Unknown partition must log an ERROR (not just a warning)."""
        import logging
        with caplog.at_level(logging.ERROR, logger="utils"):
            get_partition_regions("aws-cn")
        assert any("aws-cn" in r.message for r in caplog.records if r.levelno == logging.ERROR)


# ---------------------------------------------------------------------------
# get_partition_default_region
# ---------------------------------------------------------------------------


class TestGetPartitionDefaultRegion:
    def test_govcloud_default(self):
        assert get_partition_default_region("aws-us-gov") == "us-gov-west-1"

    def test_commercial_default(self):
        assert get_partition_default_region("aws") == "us-east-1"


# ---------------------------------------------------------------------------
# validate_aws_credentials
# ---------------------------------------------------------------------------


class TestValidateAwsCredentials:
    def test_success(self):
        mock_sts = Mock()
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}

        with patch("utils.get_boto3_client", return_value=mock_sts):
            valid, account_id, error = validate_aws_credentials()

        assert valid is True
        assert account_id == "123456789012"
        assert error is None

    def test_failure(self):
        with patch("utils.get_boto3_client", side_effect=Exception("no creds")):
            valid, account_id, error = validate_aws_credentials()

        assert valid is False
        assert account_id is None
        assert error is not None


# ---------------------------------------------------------------------------
# get_cached_account_info
# ---------------------------------------------------------------------------


class TestGetCachedAccountInfo:
    def test_returns_cached_value(self):
        with patch.object(aws_mod, "_account_info_cache", ("111", "ACCT", "aws")):
            result = get_cached_account_info()
        assert result == ("111", "ACCT", "aws")

    def test_calls_sts_when_no_cache(self):
        mock_sts = Mock()
        mock_sts.get_caller_identity.return_value = {"Account": "999999999999"}

        with patch.object(aws_mod, "_account_info_cache", None), patch("utils.get_boto3_client", return_value=mock_sts), patch("utils.get_account_name", return_value="PROD"), patch("utils.detect_partition", return_value="aws"):
            account_id, account_name, partition = get_cached_account_info()

        assert account_id == "999999999999"
        assert account_name == "PROD"
        assert partition == "aws"
        # Reset cache after test
        aws_mod._account_info_cache = None

    def test_returns_defaults_on_failure(self):
        with patch.object(aws_mod, "_account_info_cache", None), patch("utils.get_boto3_client", side_effect=Exception("boom")):
            account_id, account_name, partition = get_cached_account_info()

        assert account_id == "UNKNOWN"
        assert account_name == "UNKNOWN-ACCOUNT"
        assert partition == "aws"
        # Cache should NOT be set after failure
        assert aws_mod._account_info_cache is None
