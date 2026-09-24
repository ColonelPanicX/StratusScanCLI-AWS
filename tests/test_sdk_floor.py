#!/usr/bin/env python3
"""
Tests for the declared boto3/botocore floor (Issue #213).

The floor is a measured value (see utils.BOTO3_MIN_VERSION). These tests keep
it declared in exactly one place per artifact and in agreement, and pin the
runtime behavior when the running SDK is older than the floor.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import utils  # noqa: E402
from tests.test_exporter_api_contracts import MEASURED_MIN_BOTOCORE  # noqa: E402

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _pyproject_boto3_floor() -> str:
    # tomllib is 3.11+; requires-python is 3.10, so read the pin with a regex.
    matches = re.findall(r'^\s*"boto3>=([0-9][0-9.]*)"', PYPROJECT.read_text(), re.MULTILINE)
    assert len(matches) == 1, f"expected exactly one boto3>= runtime pin, found {matches}"
    return matches[0]


def test_floor_constant_matches_pyproject():
    declared = _pyproject_boto3_floor()
    assert declared == utils.BOTO3_MIN_VERSION


def test_measured_symbols_are_covered_by_floor():
    """Every symbol known to postdate older SDKs must be inside the floor."""
    floor = utils._version_tuple(utils.BOTOCORE_MIN_VERSION)
    above = {k: v for k, v in MEASURED_MIN_BOTOCORE.items() if utils._version_tuple(v) > floor}
    assert not above, f"symbols newer than the declared botocore floor: {above}"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1.35.65", (1, 35, 65)),
        ("1.34.46", (1, 34, 46)),
        ("1.40.0.dev1", (1, 40, 0)),
        (" 2.0 ", (2, 0)),
        ("garbage", (0,)),
    ],
)
def test_version_tuple(raw, expected):
    assert utils._version_tuple(raw) == expected


def test_version_ordering_is_numeric_not_lexical():
    assert utils._version_tuple("1.35.100") > utils._version_tuple("1.35.65")


def _set_sdk(monkeypatch, version):
    monkeypatch.setattr(utils.boto3, "__version__", version)
    monkeypatch.setattr(utils.botocore, "__version__", version)


def test_check_sdk_floor_ok_at_floor(monkeypatch):
    _set_sdk(monkeypatch, utils.BOTO3_MIN_VERSION)
    status = utils.check_sdk_floor()
    assert status["ok"] is True
    assert status["below"] == []


def test_check_sdk_floor_reports_each_package_below(monkeypatch):
    monkeypatch.setattr(utils.boto3, "__version__", "1.40.0")
    monkeypatch.setattr(utils.botocore, "__version__", "1.34.46")
    status = utils.check_sdk_floor()
    assert status["ok"] is False
    assert status["below"] == [("botocore", "1.34.46", utils.BOTOCORE_MIN_VERSION)]
    assert f"boto3>={utils.BOTO3_MIN_VERSION}" in status["upgrade_command"]


def test_upgrade_command_uses_user_site_outside_venv(monkeypatch):
    monkeypatch.setattr(utils.sys, "prefix", "/usr")
    monkeypatch.setattr(utils.sys, "base_prefix", "/usr")
    cmd = utils.sdk_upgrade_command()
    assert cmd[:5] == [utils.sys.executable, "-m", "pip", "install", "--upgrade"]
    assert "--user" in cmd
    assert cmd[-1] == f"boto3>={utils.BOTO3_MIN_VERSION}"


def test_upgrade_command_omits_user_inside_venv(monkeypatch):
    monkeypatch.setattr(utils.sys, "prefix", "/home/x/venv")
    monkeypatch.setattr(utils.sys, "base_prefix", "/usr")
    assert "--user" not in utils.sdk_upgrade_command()


class _TTY:
    def isatty(self):
        return True


@pytest.fixture
def below_floor_interactive(monkeypatch):
    """Below-floor SDK, interactive TTY, not yet handled, not auto-run."""
    _set_sdk(monkeypatch, "1.34.46")
    monkeypatch.delenv("STRATUSSCAN_SDK_FLOOR_HANDLED", raising=False)
    monkeypatch.delenv("STRATUSSCAN_AUTO_RUN", raising=False)
    monkeypatch.setattr(utils.sys, "stdin", _TTY())
    warnings = []
    monkeypatch.setattr(utils, "log_warning", warnings.append)
    monkeypatch.setattr(utils, "log_success", lambda m: None)
    monkeypatch.setattr(utils, "log_error", lambda m, e=None: None)
    return warnings


def _no_prompt(*a, **kw):
    raise AssertionError("must not prompt")


def _no_pip():
    raise AssertionError("must not run pip")


def test_at_floor_is_silent(monkeypatch):
    _set_sdk(monkeypatch, "9.99.0")
    monkeypatch.setattr(utils, "prompt_for_confirmation", _no_prompt)
    monkeypatch.setattr(utils, "log_warning", _no_prompt)
    assert utils.ensure_sdk_floor() is True


def test_auto_run_warns_with_command_and_never_prompts(monkeypatch, below_floor_interactive):
    monkeypatch.setenv("STRATUSSCAN_AUTO_RUN", "1")
    monkeypatch.setattr(utils, "prompt_for_confirmation", _no_prompt)
    monkeypatch.setattr(utils, "upgrade_sdk", _no_pip)
    assert utils.ensure_sdk_floor() is True
    assert any(f'"boto3>={utils.BOTO3_MIN_VERSION}"' in w for w in below_floor_interactive)


def test_headless_caller_never_prompts(monkeypatch, below_floor_interactive):
    monkeypatch.setattr(utils, "prompt_for_confirmation", _no_prompt)
    monkeypatch.setattr(utils, "upgrade_sdk", _no_pip)
    assert utils.ensure_sdk_floor(allow_prompt=False) is True
    assert below_floor_interactive


def test_already_handled_in_parent_does_not_reprompt(monkeypatch, below_floor_interactive):
    monkeypatch.setenv("STRATUSSCAN_SDK_FLOOR_HANDLED", "1")
    monkeypatch.setattr(utils, "prompt_for_confirmation", _no_prompt)
    assert utils.ensure_sdk_floor() is True
    assert below_floor_interactive


def test_decline_continues_on_old_sdk(monkeypatch, below_floor_interactive):
    monkeypatch.setattr(utils, "prompt_for_confirmation", lambda *a, **kw: False)
    monkeypatch.setattr(utils, "upgrade_sdk", _no_pip)
    assert utils.ensure_sdk_floor() is True
    # Children launched afterwards must not ask again.
    assert utils.os.environ["STRATUSSCAN_SDK_FLOOR_HANDLED"] == "1"


def test_accept_in_exporter_asks_for_rerun(monkeypatch, below_floor_interactive):
    monkeypatch.setattr(utils, "prompt_for_confirmation", lambda *a, **kw: True)
    monkeypatch.setattr(utils, "upgrade_sdk", lambda: {"ok": True, "returncode": 0, "command": "", "error": ""})
    assert utils.ensure_sdk_floor() is False


def test_accept_in_launcher_continues(monkeypatch, below_floor_interactive):
    monkeypatch.setattr(utils, "prompt_for_confirmation", lambda *a, **kw: True)
    monkeypatch.setattr(utils, "upgrade_sdk", lambda: {"ok": True, "returncode": 0, "command": "", "error": ""})
    assert utils.ensure_sdk_floor(continue_after_upgrade=True) is True


def test_failed_upgrade_proceeds_and_logs_manual_command(monkeypatch, below_floor_interactive):
    errors = []
    monkeypatch.setattr(utils, "log_error", lambda m, e=None: errors.append(m))
    monkeypatch.setattr(utils, "prompt_for_confirmation", lambda *a, **kw: True)
    monkeypatch.setattr(
        utils, "upgrade_sdk",
        lambda: {"ok": False, "returncode": 1, "command": "", "error": "pip exited 1"},
    )
    assert utils.ensure_sdk_floor() is True
    assert errors and f"boto3>={utils.BOTO3_MIN_VERSION}" in errors[0]


def test_upgrade_sdk_reports_pip_failure(monkeypatch):
    class _Done:
        returncode = 1

    monkeypatch.setattr(utils.subprocess, "run", lambda *a, **kw: _Done())
    result = utils.upgrade_sdk()
    assert result["ok"] is False
    assert result["returncode"] == 1


def test_upgrade_sdk_reports_missing_interpreter(monkeypatch):
    def boom(*a, **kw):
        raise OSError("no such file")

    monkeypatch.setattr(utils.subprocess, "run", boom)
    result = utils.upgrade_sdk()
    assert result["ok"] is False
    assert result["returncode"] is None


def test_ensure_dependencies_stops_when_upgrade_needs_rerun(monkeypatch):
    monkeypatch.setattr(utils, "ensure_sdk_floor", lambda **kw: False)
    assert utils.ensure_dependencies("json") is False
