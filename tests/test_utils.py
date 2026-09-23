#!/usr/bin/env python3
"""
Test suite for utils.py core utility functions.

Tests cover:
- Account name mapping
- File naming conventions
- Configuration loading
- Region validation
- Logging setup
"""

import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Add parent directory to path to import utils
sys.path.insert(0, str(Path(__file__).parent.parent))
import utils


class TestMaskAccountId:
    """Test mask_account_id() helper for safe log output."""

    def test_standard_account_id(self):
        assert utils.mask_account_id('123456789012') == '...9012'

    def test_short_id_returned_unchanged(self):
        assert utils.mask_account_id('123') == '123'

    def test_empty_string(self):
        assert utils.mask_account_id('') == ''

    def test_exactly_four_chars(self):
        assert utils.mask_account_id('1234') == '...1234'

    def test_last_four_are_used(self):
        assert utils.mask_account_id('000000009999') == '...9999'


class TestAccountMapping:
    """Test account ID to name mapping functions."""

    def test_get_account_name_with_mapping(self):
        """Test retrieval of account name when mapping exists."""
        with patch.object(utils, 'get_config', return_value=({'123456789012': 'PROD-ACCOUNT'}, {})):
            result = utils.get_account_name('123456789012')
            assert result == 'PROD-ACCOUNT'

    def test_get_account_name_with_default(self):
        """Test fallback to default when no mapping exists."""
        with patch.object(utils, 'get_config', return_value=({}, {})):
            result = utils.get_account_name('999999999999', default='TEST-DEFAULT')
            assert result == 'TEST-DEFAULT'

    def test_get_account_name_default_fallback(self):
        """Test default fallback value is used."""
        with patch.object(utils, 'get_config', return_value=({}, {})):
            result = utils.get_account_name('999999999999')
            assert result == 'UNKNOWN-ACCOUNT'


class TestFileNaming:
    """Test file naming convention functions."""

    def test_create_export_filename_basic(self):
        """Test basic filename generation."""
        # Test
        filename = utils.create_export_filename(
            account_name='PROD-ACCOUNT',
            resource_type='ec2',
            suffix='running'
        )

        # Verify structure
        assert 'PROD-ACCOUNT' in filename
        assert 'ec2' in filename
        assert 'running' in filename
        assert 'export' in filename
        assert filename.endswith('.xlsx')

    def test_create_export_filename_date_format(self):
        """Test filename includes date in MM.DD.YYYY format."""
        import re

        # Test
        filename = utils.create_export_filename(
            account_name='TEST',
            resource_type='s3',
            suffix=''
        )

        # Verify date format MM.DD.YYYY appears
        date_pattern = r'\d{2}\.\d{2}\.\d{4}'
        assert re.search(date_pattern, filename), f"Date pattern not found in: {filename}"

    def test_create_export_filename_no_suffix(self):
        """Test filename generation without suffix."""
        # Test
        filename = utils.create_export_filename(
            account_name='DEV',
            resource_type='vpc',
            suffix=''
        )

        # Verify
        assert 'DEV' in filename
        assert 'vpc' in filename
        assert 'export' in filename
        # Should not have double hyphens
        assert '--' not in filename


class TestRegionValidation:
    """Test AWS region validation functions."""

    def test_is_aws_region_valid_commercial(self):
        """Test validation of commercial AWS regions."""
        # Test valid commercial regions
        assert utils.is_aws_region('us-east-1') is True
        assert utils.is_aws_region('us-west-2') is True
        assert utils.is_aws_region('eu-west-1') is True

    def test_is_aws_region_invalid(self):
        """Test rejection of invalid region names."""
        # Test invalid regions
        assert utils.is_aws_region('invalid-region') is False
        assert utils.is_aws_region('us-east-99') is False
        assert utils.is_aws_region('') is False


class TestAccountInfo:
    """Test account information retrieval."""

    @patch('utils.get_boto3_client')
    def test_get_account_info_success(self, mock_get_client):
        """Test successful account info retrieval."""
        mock_sts = Mock()
        mock_sts.get_caller_identity.return_value = {
            'Account': '123456789012',
            'Arn': 'arn:aws:iam::123456789012:user/test'
        }
        mock_get_client.return_value = mock_sts

        with patch.object(utils, '_account_info_cache', None), \
             patch.object(utils, 'get_config', return_value=({'123456789012': 'TEST-ACCOUNT'}, {})):
            account_id, account_name = utils.get_account_info()
            assert account_id == '123456789012'
            assert account_name == 'TEST-ACCOUNT'
            mock_sts.get_caller_identity.assert_called_once()

    @patch('utils.get_boto3_client')
    def test_get_account_info_with_fallback(self, mock_get_client):
        """Test account info with fallback for unmapped account."""
        mock_sts = Mock()
        mock_sts.get_caller_identity.return_value = {
            'Account': '999999999999',
            'Arn': 'arn:aws:iam::999999999999:user/test'
        }
        mock_get_client.return_value = mock_sts

        with patch.object(utils, '_account_info_cache', None), \
             patch.object(utils, 'get_config', return_value=({}, {})):
            account_id, account_name = utils.get_account_info()
            assert account_id == '999999999999'
            assert '999999999999' in account_name


class TestBoto3ClientCreation:
    """Test boto3 client creation with retry configuration."""

    @patch('utils.boto3.Session')
    def test_get_boto3_client_basic(self, mock_session):
        """Test basic client creation."""
        # Setup mock
        mock_boto_session = Mock()
        mock_client = Mock()
        mock_boto_session.client.return_value = mock_client
        mock_session.return_value = mock_boto_session

        # Test
        utils.get_boto3_client('ec2', region_name='us-east-1')

        # Verify
        mock_boto_session.client.assert_called_once()
        call_args = mock_boto_session.client.call_args

        # Check service name
        assert call_args[0][0] == 'ec2'

        # Check config was passed
        assert 'config' in call_args[1]

    @patch('utils.boto3.Session')
    def test_get_boto3_client_with_retries(self, mock_session):
        """Test client includes retry configuration."""
        # Setup mock
        mock_boto_session = Mock()
        mock_client = Mock()
        mock_boto_session.client.return_value = mock_client
        mock_session.return_value = mock_boto_session

        # Test
        utils.get_boto3_client('s3')

        # Verify config was created with retries
        call_args = mock_boto_session.client.call_args
        config = call_args[1]['config']

        assert config is not None
        assert hasattr(config, 'retries')


class TestCrossAccountSession:
    """Test the STRATUSSCAN_ROLE_ARN env-var injection path in get_aws_session."""

    @patch('utils._assume_role_cached')
    @patch('utils.boto3.Session')
    def test_role_arn_env_var_does_not_raise(self, mock_session, mock_assume, monkeypatch):
        """
        Regression: get_aws_session() must not NameError on the
        STRATUSSCAN_ROLE_ARN branch (org-scan / cross-account subprocess path).

        Exporters call get_boto3_client() without role_arn, so role_arn arrives
        as None; org-scan injects the role via the env var. The debug-log line
        on that branch previously referenced an undefined ``log`` symbol.
        """
        role = "arn:aws:iam::123456789012:role/CrossAccountAudit"
        monkeypatch.setenv("STRATUSSCAN_ROLE_ARN", role)

        mock_assume.return_value = {
            "AccessKeyId": "AKIA_TEST",
            "SecretAccessKey": "secret",
            "SessionToken": "token",
        }

        # Should complete without NameError and assume the env-supplied role.
        utils.get_aws_session(region_name="us-east-1")

        mock_assume.assert_called_once()
        assert mock_assume.call_args[0][0] == role


class TestLogging:
    """Test logging functions."""

    def test_log_info_callable(self):
        """Test that log_info function exists and is callable."""
        assert callable(utils.log_info)

    def test_log_error_callable(self):
        """Test that log_error function exists and is callable."""
        assert callable(utils.log_error)

    def test_log_warning_callable(self):
        """Test that log_warning function exists and is callable."""
        assert callable(utils.log_warning)

    def test_log_success_callable(self):
        """Test that log_success function exists and is callable."""
        assert callable(utils.log_success)


class TestPartitionDetection:
    """Test AWS partition detection."""

    def test_detect_partition_commercial(self):
        """Test detection of commercial AWS partition."""
        # Test commercial regions
        assert utils.detect_partition('us-east-1') == 'aws'
        assert utils.detect_partition('eu-west-1') == 'aws'
        assert utils.detect_partition('ap-southeast-1') == 'aws'

    def test_detect_partition_govcloud(self):
        """Test detection of GovCloud partition."""
        # Test GovCloud regions
        assert utils.detect_partition('us-gov-west-1') == 'aws-us-gov'
        assert utils.detect_partition('us-gov-east-1') == 'aws-us-gov'

    def test_detect_partition_default(self):
        """Test default partition when region is None."""
        # Should default to commercial
        result = utils.detect_partition(None)
        assert result in ['aws', 'aws-us-gov']  # Depends on environment


class TestARNBuilding:
    """Test ARN construction utilities."""

    def test_build_arn_basic(self):
        """Test basic ARN construction."""
        # Test
        arn = utils.build_arn(
            service='s3',
            resource='bucket/my-bucket',
            region='us-east-1',
            account_id='123456789012'
        )

        # Verify structure
        assert arn.startswith('arn:')
        assert ':s3:' in arn
        assert ':us-east-1:' in arn
        assert ':123456789012:' in arn
        assert 'bucket/my-bucket' in arn

    def test_build_arn_global_service(self):
        """Test ARN construction for global services (no region)."""
        # Test
        arn = utils.build_arn(
            service='iam',
            resource='user/testuser',
            region='',
            account_id='123456789012'
        )

        # Verify - should have empty region field
        parts = arn.split(':')
        assert parts[0] == 'arn'
        assert parts[2] == 'iam'
        assert parts[3] == ''  # Empty region for global service
        assert parts[4] == '123456789012'


class TestPromptMenu:
    """Tests for the new prompt_menu() utility function."""

    def test_valid_choice_returns_int(self):
        with patch('builtins.input', return_value='1'):
            result = utils.prompt_menu("TEST MENU", ["Option A", "Option B"])
        assert result == 1

    def test_back_raises_signal(self):
        with patch('builtins.input', return_value='b'), pytest.raises(utils.BackSignal):
            utils.prompt_menu("TEST MENU", ["Option A"])

    def test_exit_raises_exit_to_main_signal(self):
        # 'x' = main menu — the single-voice standard (was QuitSignal before).
        with patch('builtins.input', return_value='x'), pytest.raises(utils.ExitToMainSignal):
            utils.prompt_menu("TEST MENU", ["Option A"])

    def test_quit_raises_quit_signal(self):
        with patch('builtins.input', return_value='q'), pytest.raises(utils.QuitSignal):
            utils.prompt_menu("TEST MENU", ["Option A"])

    def test_invalid_then_valid(self):
        with patch('builtins.input', side_effect=['z', '2']):
            result = utils.prompt_menu("TEST MENU", ["Option A", "Option B"])
        assert result == 2

    def test_auto_run_returns_first(self, monkeypatch):
        monkeypatch.setenv("STRATUSSCAN_AUTO_RUN", "1")
        result = utils.prompt_menu("TEST MENU", ["Option A", "Option B"])
        assert result == 1


class TestNavFooter:
    """Locks the single-voice navigation footer + interaction constants."""

    def test_full_footer_wording(self):
        assert utils._nav_footer() == "  b = back  |  x = main menu  |  q = quit"

    def test_footer_includes_only_enabled_keys(self):
        assert utils._nav_footer(allow_back=False) == "  x = main menu  |  q = quit"
        assert utils._nav_footer(allow_exit=False, allow_quit=False) == "  b = back"

    def test_constants(self):
        assert utils.GLYPH_OK == "✅"
        assert utils.GLYPH_FAIL == "❌"
        assert utils.MSG_INVALID_SELECTION == "Invalid selection. Please try again."


class TestPromptMultiselect:
    """Tests for prompt_multiselect() — the shared numbered multi-select."""

    OPTS = ["Alpha", "Bravo", "Charlie"]

    def test_single_pick(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        with patch('builtins.input', return_value='2'):
            assert utils.prompt_multiselect("PICK", self.OPTS) == [2]

    def test_multi_pick(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        with patch('builtins.input', return_value='1 3'):
            assert utils.prompt_multiselect("PICK", self.OPTS) == [1, 3]

    def test_dedupe_preserves_order(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        with patch('builtins.input', return_value='3 1 3'):
            assert utils.prompt_multiselect("PICK", self.OPTS) == [3, 1]

    def test_all_row_selects_everything(self, monkeypatch):
        # With an All row, row 1 = All → returns every option index.
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        with patch('builtins.input', return_value='1'):
            assert utils.prompt_multiselect("PICK", self.OPTS, all_label="All") == [1, 2, 3]

    def test_all_row_offsets_option_indices(self, monkeypatch):
        # Row 2 (with All at row 1) maps back to option index 1.
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        with patch('builtins.input', return_value='2 4'):
            assert utils.prompt_multiselect("PICK", self.OPTS, all_label="All") == [1, 3]

    def test_back_raises_signal(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        with patch('builtins.input', return_value='b'), pytest.raises(utils.BackSignal):
            utils.prompt_multiselect("PICK", self.OPTS)

    def test_exit_raises_exit_to_main(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        with patch('builtins.input', return_value='x'), pytest.raises(utils.ExitToMainSignal):
            utils.prompt_multiselect("PICK", self.OPTS)

    def test_quit_raises_quit(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        with patch('builtins.input', return_value='q'), pytest.raises(utils.QuitSignal):
            utils.prompt_multiselect("PICK", self.OPTS)

    def test_invalid_then_valid(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        with patch('builtins.input', side_effect=['z', '9', '2']):
            assert utils.prompt_multiselect("PICK", self.OPTS) == [2]

    def test_auto_run_returns_all(self, monkeypatch):
        monkeypatch.setenv("STRATUSSCAN_AUTO_RUN", "1")
        assert utils.prompt_multiselect("PICK", self.OPTS) == [1, 2, 3]


class TestPromptRegionSelectionNew:
    """Tests for the rewritten prompt_region_selection() function."""

    def test_default_regions(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        defaults = ['us-east-1', 'us-west-2']
        with patch('utils.prompt_menu', return_value=1), \
             patch('utils.get_default_regions', return_value=defaults), \
             patch('utils.detect_partition', return_value='aws'):
            result = utils.prompt_region_selection()
        assert result == defaults

    def test_all_regions(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        all_r = ['us-east-1', 'us-west-2', 'eu-west-1']
        with patch('utils.prompt_menu', return_value=2), \
             patch('utils.get_default_regions', return_value=['us-east-1']), \
             patch('utils.detect_partition', return_value='aws'), \
             patch('utils.get_partition_regions', return_value=all_r):
            result = utils.prompt_region_selection()
        assert result == all_r

    def test_back_returns_string(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        with patch('utils.prompt_menu', side_effect=utils.BackSignal), \
             patch('utils.get_default_regions', return_value=['us-east-1']), \
             patch('utils.detect_partition', return_value='aws'):
            result = utils.prompt_region_selection()
        assert result == 'back'

    def test_exit_returns_string(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        with patch('utils.prompt_menu', side_effect=utils.QuitSignal), \
             patch('utils.get_default_regions', return_value=['us-east-1']), \
             patch('utils.detect_partition', return_value='aws'):
            result = utils.prompt_region_selection()
        assert result == 'exit'

    def test_exit_to_main_returns_string(self, monkeypatch):
        # 'x' from the region menu surfaces as ExitToMainSignal → 'exit'.
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        with patch('utils.prompt_menu', side_effect=utils.ExitToMainSignal), \
             patch('utils.get_default_regions', return_value=['us-east-1']), \
             patch('utils.detect_partition', return_value='aws'):
            result = utils.prompt_region_selection()
        assert result == 'exit'

    def test_select_single(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        available = ['us-east-1', 'us-west-2', 'eu-west-1']
        with patch('utils.prompt_menu', return_value=3), \
             patch('utils.get_default_regions', return_value=['us-east-1']), \
             patch('utils.detect_partition', return_value='aws'), \
             patch('utils.get_partition_regions', return_value=available), \
             patch('builtins.input', return_value='2'):
            result = utils.prompt_region_selection()
        assert result == ['us-west-2']

    def test_select_multi(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        available = ['us-east-1', 'us-west-2', 'eu-west-1']
        with patch('utils.prompt_menu', return_value=3), \
             patch('utils.get_default_regions', return_value=['us-east-1']), \
             patch('utils.detect_partition', return_value='aws'), \
             patch('utils.get_partition_regions', return_value=available), \
             patch('builtins.input', return_value='1 3'):
            result = utils.prompt_region_selection()
        assert result == ['us-east-1', 'eu-west-1']

    def test_back_from_sublist_returns_to_menu(self, monkeypatch):
        monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
        available = ['us-east-1', 'us-west-2']
        defaults = ['us-east-1']
        # First call to prompt_menu → 3 (Select Regions), user types 'b', then
        # second call to prompt_menu → 1 (Default Regions)
        menu_calls = iter([3, 1])
        with patch('utils.prompt_menu', side_effect=menu_calls), \
             patch('utils.get_default_regions', return_value=defaults), \
             patch('utils.detect_partition', return_value='aws'), \
             patch('utils.get_partition_regions', return_value=available), \
             patch('builtins.input', return_value='b'):
            result = utils.prompt_region_selection()
        assert result == defaults

    def test_auto_run_bypass(self, monkeypatch):
        monkeypatch.setenv("STRATUSSCAN_AUTO_RUN", "1")
        monkeypatch.setenv("STRATUSSCAN_REGIONS", "us-east-1,us-west-2")
        result = utils.prompt_region_selection()
        assert result == ['us-east-1', 'us-west-2']


class TestPromptConfirmation:
    """Tests for the new prompt_confirmation() utility function."""

    def test_enter_confirms(self):
        with patch('builtins.input', return_value=''):
            result = utils.prompt_confirmation("Ready to export?")
        assert result == 'confirm'

    def test_back_returns_string(self):
        with patch('builtins.input', return_value='b'):
            result = utils.prompt_confirmation("Ready to export?")
        assert result == 'back'

    def test_exit_returns_string(self):
        with patch('builtins.input', return_value='x'):
            result = utils.prompt_confirmation("Ready to export?")
        assert result == 'exit'

    def test_invalid_then_confirm(self):
        with patch('builtins.input', side_effect=['z', '']):
            result = utils.prompt_confirmation("Ready to export?")
        assert result == 'confirm'

    def test_auto_run_confirms(self, monkeypatch):
        monkeypatch.setenv("STRATUSSCAN_AUTO_RUN", "1")
        result = utils.prompt_confirmation("Ready to export?")
        assert result == 'confirm'


class TestParseScriptArgs:
    """Tests for parse_script_args() and the _SCRIPT_ARGS module store."""

    def _parse(self, argv: list, monkeypatch) -> "utils.argparse.Namespace":
        """Helper: patch sys.argv, reset _SCRIPT_ARGS, and call parse_script_args."""
        monkeypatch.setattr(sys, "argv", ["script.py"] + argv)
        # Reset the global store before each parse so tests are independent
        utils._SCRIPT_ARGS = None
        return utils.parse_script_args("Test script description")

    def test_single_region(self, monkeypatch):
        args = self._parse(["--region", "us-east-1"], monkeypatch)
        assert args.region == "us-east-1"
        assert args.regions is None
        assert args.all_regions is False

    def test_multiple_regions(self, monkeypatch):
        args = self._parse(["--regions", "us-east-1,us-west-2"], monkeypatch)
        assert args.regions == "us-east-1,us-west-2"
        region_list = [r.strip() for r in args.regions.split(",")]
        assert len(region_list) == 2
        assert "us-east-1" in region_list
        assert "us-west-2" in region_list

    def test_all_regions_flag(self, monkeypatch):
        args = self._parse(["--all-regions"], monkeypatch)
        assert args.all_regions is True
        assert args.region is None
        assert args.regions is None

    def test_region_and_all_regions_mutually_exclusive(self, monkeypatch):
        """--region and --all-regions together must raise SystemExit."""
        monkeypatch.setattr(sys, "argv", ["script.py", "--region", "us-east-1", "--all-regions"])
        utils._SCRIPT_ARGS = None
        with pytest.raises(SystemExit):
            utils.parse_script_args("Test script description")

    def test_yes_flag(self, monkeypatch):
        args = self._parse(["--yes"], monkeypatch)
        assert args.yes is True

    def test_yes_short_flag(self, monkeypatch):
        args = self._parse(["-y"], monkeypatch)
        assert args.yes is True

    def test_profile(self, monkeypatch):
        args = self._parse(["--profile", "my-profile"], monkeypatch)
        assert args.profile == "my-profile"

    def test_output_dir(self, monkeypatch):
        args = self._parse(["--output-dir", "/tmp/test"], monkeypatch)
        assert args.output_dir == "/tmp/test"

    def test_defaults_when_no_args(self, monkeypatch):
        args = self._parse([], monkeypatch)
        assert args.region is None
        assert args.regions is None
        assert args.all_regions is False
        assert args.yes is False
        assert args.profile is None
        assert args.output_dir == "output"

    def test_sets_module_global(self, monkeypatch):
        """parse_script_args() must populate utils._SCRIPT_ARGS."""
        utils._SCRIPT_ARGS = None
        self._parse(["--region", "eu-west-1"], monkeypatch)
        assert utils._SCRIPT_ARGS is not None
        assert utils._SCRIPT_ARGS.region == "eu-west-1"
        assert utils.get_script_args() is utils._SCRIPT_ARGS

    def test_get_script_args_returns_none_before_parse(self):
        """get_script_args() returns None when parse_script_args() has not run."""
        utils._SCRIPT_ARGS = None
        assert utils.get_script_args() is None

    def test_prompt_region_selection_respects_region_flag(self, monkeypatch):
        """prompt_region_selection() must return single-item list when --region set."""
        self._parse(["--region", "ap-southeast-1"], monkeypatch)
        result = utils.prompt_region_selection()
        assert result == ["ap-southeast-1"]

    def test_prompt_region_selection_respects_regions_flag(self, monkeypatch):
        """prompt_region_selection() must return list when --regions set."""
        self._parse(["--regions", "us-east-1,eu-west-1"], monkeypatch)
        result = utils.prompt_region_selection()
        assert result == ["us-east-1", "eu-west-1"]

    def test_prompt_for_confirmation_respects_yes_flag(self, monkeypatch):
        """prompt_for_confirmation() must return True when --yes set."""
        self._parse(["--yes"], monkeypatch)
        result = utils.prompt_for_confirmation("Continue?")
        assert result is True

    def teardown_method(self, method):
        """Reset _SCRIPT_ARGS after each test to avoid cross-test contamination."""
        utils._SCRIPT_ARGS = None


class TestSanitizeFilenameComponent:
    """sanitize_filename_component() strips path-altering characters (CWE-73)
    without disturbing ordinary names."""

    @pytest.mark.parametrize("value", [
        'PROD-ACCOUNT',
        'ec2',
        'my account 01',
        'acct_name-v2',
        'Account.Name',
        '',
    ])
    def test_ordinary_names_pass_through_unchanged(self, value):
        assert utils.sanitize_filename_component(value) == value

    def test_forward_slash_stripped(self):
        assert utils.sanitize_filename_component('a/b') == 'ab'

    def test_backslash_stripped(self):
        assert utils.sanitize_filename_component(r'a\b') == 'ab'

    def test_parent_directory_marker_stripped(self):
        assert utils.sanitize_filename_component('../etc') == 'etc'

    def test_nested_parent_markers_cannot_reconstitute(self):
        # A single non-repeating pass on '....' would leave '..'
        assert '..' not in utils.sanitize_filename_component('....')

    def test_null_byte_stripped(self):
        assert utils.sanitize_filename_component('a\x00b') == 'ab'

    def test_control_characters_stripped(self):
        assert utils.sanitize_filename_component('a\r\nb') == 'ab'

    def test_leading_and_trailing_dots_and_spaces_stripped(self):
        assert utils.sanitize_filename_component('  .hidden.  ') == 'hidden'

    def test_traversal_payload_is_defanged(self):
        assert utils.sanitize_filename_component('../../etc/passwd') == 'etcpasswd'

    def test_non_string_input_coerced(self):
        assert utils.sanitize_filename_component(42) == '42'


class TestContainedPath:
    """contained_path() confines a caller-supplied name to a base directory."""

    def test_plain_filename_resolves_inside_base(self, tmp_path):
        result = utils.contained_path(tmp_path, 'report.xlsx')
        assert result == tmp_path / 'report.xlsx'

    def test_traversal_is_reduced_to_basename(self, tmp_path):
        # basename() collapses the traversal rather than escaping
        result = utils.contained_path(tmp_path, '../../etc/passwd')
        assert result == tmp_path / 'passwd'
        assert result.parent == tmp_path

    def test_absolute_path_is_reduced_to_basename(self, tmp_path):
        result = utils.contained_path(tmp_path, '/etc/shadow')
        assert result == tmp_path / 'shadow'

    @pytest.mark.parametrize("bad", ['', '.', '..', '/', '../'])
    def test_names_with_no_usable_component_are_rejected(self, tmp_path, bad):
        with pytest.raises(ValueError):
            utils.contained_path(tmp_path, bad)

    def test_result_never_escapes_base(self, tmp_path):
        for candidate in ['a/b/c.txt', '../x.txt', './y.txt', 'z.txt']:
            result = utils.contained_path(tmp_path, candidate)
            assert result.resolve().parent == tmp_path.resolve()

    def test_get_output_filepath_delegates_to_containment(self, tmp_path, monkeypatch):
        monkeypatch.setattr(utils, 'get_output_dir', lambda: tmp_path)
        assert utils.get_output_filepath('../escape.xlsx') == tmp_path / 'escape.xlsx'


class TestExportFilenameContainment:
    """A crafted account_name in config.json must not steer the export path."""

    def test_normal_account_name_is_untouched(self):
        filename = utils.create_export_filename(
            account_name='PROD-ACCOUNT', resource_type='ec2', suffix='running'
        )
        assert filename.startswith('PROD-ACCOUNT-ec2-running-export-')

    def test_traversal_in_account_name_is_stripped(self):
        filename = utils.create_export_filename(
            account_name='../../etc', resource_type='ec2', suffix=''
        )
        assert '..' not in filename
        assert '/' not in filename

    def test_traversal_in_resource_type_is_stripped(self):
        filename = utils.create_export_filename(
            account_name='ACCT', resource_type='../ec2', suffix=''
        )
        assert '..' not in filename
        assert '/' not in filename

    def test_sanitized_filename_stays_contained(self, tmp_path, monkeypatch):
        monkeypatch.setattr(utils, 'get_output_dir', lambda: tmp_path)
        filename = utils.create_export_filename(
            account_name='../../../tmp/evil', resource_type='ec2', suffix=''
        )
        assert utils.get_output_filepath(filename).parent == tmp_path


class TestLogErrorScrubbing:
    """Every log_error() branch must scrub CRLF (CWE-117), including the
    debug/stack-trace branch."""

    def test_debug_branch_scrubs_crlf(self):
        mock_logger = Mock()
        with patch.object(utils, 'get_logger', return_value=mock_logger):
            utils.log_error('boom', ValueError('line1\r\nFAKE: forged entry'))

        debug_message = mock_logger.debug.call_args[0][0]
        assert '\n' not in debug_message
        assert '\r' not in debug_message
        assert 'FAKE: forged entry' in debug_message

    def test_error_branch_still_scrubs_crlf(self):
        mock_logger = Mock()
        with patch.object(utils, 'get_logger', return_value=mock_logger):
            utils.log_error('boom', ValueError('a\r\nb'))

        error_message = mock_logger.error.call_args[0][0]
        assert '\n' not in error_message
        assert '\r' not in error_message
