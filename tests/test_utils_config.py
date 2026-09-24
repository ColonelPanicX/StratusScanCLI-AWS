"""
Unit tests for config functions (utils.py).
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import utils as cfg_mod
from utils import (
    config_value,
    get_account_name,
    get_account_name_formatted,
    get_config,
    get_resource_preference,
    is_valid_aws_account_id,
    load_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_config():
    """Reset the singleton state so each test starts clean."""
    cfg_mod._CONFIG_LOADED = False
    cfg_mod.ACCOUNT_MAPPINGS = {}
    cfg_mod.CONFIG_DATA = {}


# ---------------------------------------------------------------------------
# is_valid_aws_account_id
# ---------------------------------------------------------------------------


class TestIsValidAwsAccountId:
    def test_valid_12_digit(self):
        assert is_valid_aws_account_id("123456789012") is True

    def test_integer_input(self):
        assert is_valid_aws_account_id(123456789012) is True

    def test_too_short(self):
        assert is_valid_aws_account_id("12345") is False

    def test_too_long(self):
        assert is_valid_aws_account_id("1234567890123") is False

    def test_non_numeric(self):
        assert is_valid_aws_account_id("12345678901a") is False

    def test_empty(self):
        assert is_valid_aws_account_id("") is False


# ---------------------------------------------------------------------------
# load_config / get_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_loads_account_mappings(self, tmp_path):
        _reset_config()
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"account_mappings": {"111111111111": "TEST-ACCT"}}), encoding="utf-8"
        )
        with patch.object(cfg_mod, "_config_path", return_value=cfg_file):
            mappings, data = load_config()

        assert mappings.get("111111111111") == "TEST-ACCT"
        assert "account_mappings" in data

    def test_creates_default_when_missing(self, tmp_path):
        _reset_config()
        cfg_file = tmp_path / "config.json"  # does not exist yet
        with patch.object(cfg_mod, "_config_path", return_value=cfg_file):
            mappings, data = load_config()

        assert cfg_file.exists()
        assert isinstance(mappings, dict)
        assert isinstance(data, dict)

    def test_get_config_is_cached(self, tmp_path):
        _reset_config()
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"account_mappings": {}}), encoding="utf-8")
        call_count = {"n": 0}

        original_load = cfg_mod.load_config

        def counting_load():
            call_count["n"] += 1
            return original_load()

        with patch.object(cfg_mod, "_config_path", return_value=cfg_file), patch.object(cfg_mod, "load_config", side_effect=counting_load):
            get_config()
            get_config()
            get_config()

        assert call_count["n"] == 1, "load_config should only be called once"


# ---------------------------------------------------------------------------
# config_value
# ---------------------------------------------------------------------------


class TestConfigValue:
    def test_returns_top_level_key(self):
        with patch.object(cfg_mod, "get_config", return_value=({}, {"org": "ACME"})):
            assert config_value("org") == "ACME"

    def test_returns_default_when_missing(self):
        with patch.object(cfg_mod, "get_config", return_value=({}, {})):
            assert config_value("nonexistent", default="fallback") == "fallback"

    def test_returns_section_key(self):
        data = {"advanced_settings": {"timeout": 30}}
        with patch.object(cfg_mod, "get_config", return_value=({}, data)):
            assert config_value("timeout", section="advanced_settings") == 30

    def test_returns_none_default_when_no_config(self):
        with patch.object(cfg_mod, "get_config", return_value=({}, {})):
            result = config_value("missing")
        assert result is None


# ---------------------------------------------------------------------------
# get_resource_preference
# ---------------------------------------------------------------------------


class TestGetResourcePreference:
    def test_returns_preference(self):
        data = {"resource_preferences": {"ec2": {"include_stopped": True}}}
        with patch.object(cfg_mod, "get_config", return_value=({}, data)):
            assert get_resource_preference("ec2", "include_stopped") is True

    def test_returns_default_when_resource_missing(self):
        with patch.object(cfg_mod, "get_config", return_value=({}, {})):
            assert get_resource_preference("lambda", "timeout", default=60) == 60


# ---------------------------------------------------------------------------
# get_account_name / get_account_name_formatted
# ---------------------------------------------------------------------------


class TestGetAccountName:
    def test_returns_mapped_name(self):
        with patch.object(cfg_mod, "ACCOUNT_MAPPINGS", {"123456789012": "PROD"}), patch.object(cfg_mod, "get_config", return_value=({"123456789012": "PROD"}, {})):
            assert get_account_name("123456789012") == "PROD"

    def test_returns_default_when_unmapped(self):
        with patch.object(cfg_mod, "get_config", return_value=({}, {})):
            assert get_account_name("999999999999", default="UNKNOWN") == "UNKNOWN"

    def test_default_is_unknown_account(self):
        with patch.object(cfg_mod, "get_config", return_value=({}, {})):
            assert get_account_name("000000000000") == "UNKNOWN-ACCOUNT"


class TestGetAccountNameFormatted:
    def test_formats_when_mapped(self):
        mappings = {"123456789012": "PROD"}
        with patch.object(cfg_mod, "get_config", return_value=(mappings, {})):
            result = get_account_name_formatted("123456789012")
        assert result == "PROD (123456789012)"

    def test_returns_id_when_unmapped(self):
        with patch.object(cfg_mod, "get_config", return_value=({}, {})):
            result = get_account_name_formatted("999999999999")
        assert result == "999999999999"
