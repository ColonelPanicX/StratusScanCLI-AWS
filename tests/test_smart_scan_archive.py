#!/usr/bin/env python3
"""
Tests for the Smart Scan service-discovery archive and in-zip scan report.

Covers issue #294 (archive subdirectory + report inside the zip) and issue
#291 (a resumed session archives the union of every run, and reports prior
outputs that have gone missing rather than dropping them silently).
"""

import datetime
import importlib.util
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import utils  # noqa: E402


def _load_smart_scan_cli():
    """
    Load smart_scan.py as a module under a distinct name.

    The CLI file and the scripts/smart_scan package share a name, so the CLI
    has to be loaded by path to avoid shadowing the package.
    """
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    cached = sys.modules.get("smart_scan")
    if cached is not None and not hasattr(cached, "__path__"):
        for name in [n for n in sys.modules if n == "smart_scan" or n.startswith("smart_scan.")]:
            sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "_smart_scan_cli_for_archive_test", ROOT / "smart_scan.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class FakeResult:
    """Stand-in for ExecutionResult — only the fields the archive code reads."""

    script: str
    success: bool = True
    duration_seconds: float = 1.0
    output_file: Optional[str] = None


@pytest.fixture
def cli():
    return _load_smart_scan_cli()


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "get_output_dir", lambda: tmp_path)
    return tmp_path


def _make_export(out_dir: Path, name: str) -> Path:
    path = out_dir / name
    path.write_bytes(b"xlsx-placeholder")
    return path


def _date() -> str:
    return utils.get_export_date()


def _namelist(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.namelist()


def _report_text(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".md"))
        return zf.read(name).decode("utf-8")


class TestArchiveLocation:
    def test_zip_lands_in_service_discovery_subdirectory(self, cli, out_dir):
        export = _make_export(out_dir, f"ACCT-ec2-all-regions-export-{_date()}.xlsx")
        result = FakeResult(script="ec2_export.py", output_file=str(export))

        zip_path = cli._archive_session(None, [result], "ACCT", "123456789012", ["us-east-1"])

        assert zip_path is not None
        assert zip_path.parent == out_dir / "service-discovery"
        assert zip_path.parent.is_dir()
        assert zip_path.name == f"ACCT-service-discovery-export-{_date()}.zip"
        assert export.name in _namelist(zip_path)
        # Original is removed once it is safely inside the archive
        assert not export.exists()

    def test_subdirectory_created_when_absent(self, cli, out_dir):
        target = out_dir / "service-discovery"
        assert not target.exists()
        _make_export(out_dir, f"ACCT-s3-all-regions-export-{_date()}.xlsx")
        assert cli._service_discovery_dir() == target
        assert target.is_dir()

    def test_zip_without_report_is_unchanged(self, cli, out_dir):
        """Backwards compatibility: no report unless the caller supplies one."""
        export = _make_export(out_dir, f"ACCT-iam-all-regions-export-{_date()}.xlsx")
        result = FakeResult(script="iam_export.py", output_file=str(export))

        zip_path = cli._zip_export_files([result], "ACCT")

        assert zip_path is not None
        assert _namelist(zip_path) == [export.name]


class TestScanReport:
    def test_report_present_in_archive(self, cli, out_dir):
        export = _make_export(out_dir, f"ACCT-ec2-all-regions-export-{_date()}.xlsx")
        result = FakeResult(script="ec2_export.py", duration_seconds=65.0,
                            output_file=str(export))

        zip_path = cli._archive_session(
            None, [result], "ACCT", "123456789012", ["us-east-1", "us-west-2"],
            services=["EC2", "S3"],
        )

        names = _namelist(zip_path)
        report_name = f"ACCT-service-discovery-report-{_date()}.md"
        assert report_name in names

        text = _report_text(zip_path)
        assert "# AWS Service Discovery Scan Report" in text
        assert "us-east-1, us-west-2" in text
        assert utils.get_version() in text
        assert "## Services In Use" in text
        assert "- EC2" in text
        assert "- S3" in text
        assert "`ec2_export.py`" in text
        assert "1m 5s" in text
        assert export.name in text

    def test_services_in_use_carries_no_counts(self, cli, out_dir):
        export = _make_export(out_dir, f"ACCT-ec2-all-regions-export-{_date()}.xlsx")
        result = FakeResult(script="ec2_export.py", output_file=str(export))

        zip_path = cli._archive_session(
            None, [result], "ACCT", "123456789012", ["us-east-1"], services=["EC2"],
        )
        text = _report_text(zip_path)
        section = text.split("## Services In Use", 1)[1].split("---", 1)[0]
        assert section.strip() == "- EC2"

    def test_skipped_exporter_appears_in_report(self, cli, out_dir):
        marker = _make_export(
            out_dir, f"ACCT-billing-skipped-no-permission-export-{_date()}.xlsx"
        )
        export = _make_export(out_dir, f"ACCT-ec2-all-regions-export-{_date()}.xlsx")
        results = [
            FakeResult(script="billing_export.py", output_file=str(marker)),
            FakeResult(script="ec2_export.py", output_file=str(export)),
        ]

        zip_path = cli._archive_session(
            None, results, "ACCT", "123456789012", ["us-east-1"],
        )
        text = _report_text(zip_path)

        skip_section = text.split("## What Was Skipped And Why", 1)[1]
        assert "`billing_export.py`" in skip_section
        assert "missing permission" in skip_section
        assert marker.name in skip_section
        # The skip marker still travels inside the archive
        assert marker.name in _namelist(zip_path)
        # ...and is not listed as a successful collection
        ran_section = text.split("## What Ran", 1)[1].split("## What Was Skipped", 1)[0]
        assert "billing_export.py" not in ran_section

    def test_govcloud_skip_reason(self, cli, out_dir):
        marker = _make_export(
            out_dir, f"ACCT-billing-skipped-govcloud-export-{_date()}.xlsx"
        )
        result = FakeResult(script="billing_export.py", output_file=str(marker))
        zip_path = cli._archive_session(
            None, [result], "ACCT", "123456789012", ["us-gov-west-1"],
        )
        assert "not available in this partition (GovCloud)" in _report_text(zip_path)

    def test_exit_zero_without_output_is_reported_as_skipped(self, cli, out_dir):
        result = FakeResult(script="quiet_export.py", success=True, output_file=None)
        zip_path = cli._archive_session(
            None, [result], "ACCT", "123456789012", ["us-east-1"],
        )
        text = _report_text(zip_path)
        skip_section = text.split("## What Was Skipped And Why", 1)[1]
        assert "`quiet_export.py`" in skip_section
        assert "completed without producing an output file" in skip_section

    def test_crosscheck_section_included(self, cli, out_dir):
        export = _make_export(out_dir, f"ACCT-ec2-all-regions-export-{_date()}.xlsx")
        result = FakeResult(script="ec2_export.py", output_file=str(export))
        crosscheck = {
            "status": "ok",
            "period": {"start": "2026-08-01", "end": "2026-09-01"},
            "total_services_billed": 3,
            "not_collected": [
                {
                    "ce_service": "Amazon Kinesis",
                    "service": "Kinesis",
                    "monthly_cost": 12.5,
                    "exporters": ["kinesis_export.py"],
                }
            ],
            "unmapped": [],
        }
        zip_path = cli._archive_session(
            None, [result], "ACCT", "123456789012", ["us-east-1"], crosscheck=crosscheck,
        )
        text = _report_text(zip_path)
        assert "## Bill Cross-Check" in text
        assert "Amazon Kinesis" in text


class TestResumeUnion:
    def _session(self, tmp_path, results):
        path = tmp_path / "scan-20260916-120000.json"
        session = {
            "session_id": "20260916-120000",
            "scan_type": "smart-scan",
            "label": "Smart Scan (2 scripts)",
            "status": "running",
            "planned": [{"key": r["key"], "script": r["key"]} for r in results],
            "results": results,
            "account_id": "123456789012",
            "account_name": "ACCT",
            "regions": ["us-east-1"],
            "services": ["EC2", "S3"],
            "_path": str(path),
        }
        path.write_text(
            json.dumps({k: v for k, v in session.items() if not k.startswith("_")}),
            encoding="utf-8",
        )
        return session

    def test_archive_is_union_of_all_runs(self, cli, out_dir, tmp_path):
        first = _make_export(out_dir, f"ACCT-ec2-all-regions-export-{_date()}.xlsx")
        second = _make_export(out_dir, f"ACCT-s3-all-regions-export-{_date()}.xlsx")

        # Run 1 (interrupted) persisted its output path; run 2 is the current run.
        session = self._session(tmp_path, [
            {
                "key": "ec2_export.py", "script": "ec2_export.py", "status": "success",
                "exit_code": 0, "duration_s": 3.0, "output_file": str(first),
            },
            {
                "key": "s3_export.py", "script": "s3_export.py", "status": "success",
                "exit_code": 0, "duration_s": 4.0, "output_file": str(second),
            },
        ])
        current = [FakeResult(script="s3_export.py", output_file=str(second))]

        zip_path = cli._archive_session(
            session, current, "ACCT", "123456789012", ["us-east-1"], services=["EC2", "S3"],
        )

        names = _namelist(zip_path)
        assert first.name in names
        assert second.name in names
        assert sum(1 for n in names if n == second.name) == 1

    def test_missing_prior_output_is_reported_not_dropped(self, cli, out_dir, tmp_path):
        present = _make_export(out_dir, f"ACCT-s3-all-regions-export-{_date()}.xlsx")
        gone = out_dir / f"ACCT-ec2-all-regions-export-{_date()}.xlsx"

        session = self._session(tmp_path, [
            {
                "key": "ec2_export.py", "script": "ec2_export.py", "status": "success",
                "exit_code": 0, "duration_s": 3.0, "output_file": str(gone),
            },
            {
                "key": "s3_export.py", "script": "s3_export.py", "status": "success",
                "exit_code": 0, "duration_s": 4.0, "output_file": str(present),
            },
        ])

        zip_path = cli._archive_session(
            session, [], "ACCT", "123456789012", ["us-east-1"],
        )

        names = _namelist(zip_path)
        assert gone.name not in names
        assert present.name in names

        text = _report_text(zip_path)
        assert "## Missing Prior Outputs" in text
        assert gone.name in text
        assert "`ec2_export.py`" in text

    def test_resume_entry_point_zips(self, cli, out_dir, tmp_path, monkeypatch, capsys):
        first = _make_export(out_dir, f"ACCT-ec2-all-regions-export-{_date()}.xlsx")
        second = _make_export(out_dir, f"ACCT-s3-all-regions-export-{_date()}.xlsx")

        session = self._session(tmp_path, [
            {
                "key": "ec2_export.py", "script": "ec2_export.py", "status": "success",
                "exit_code": 0, "duration_s": 3.0, "output_file": str(first),
            },
        ])
        session["planned"] = [
            {"key": "ec2_export.py", "script": "ec2_export.py"},
            {"key": "s3_export.py", "script": "s3_export.py"},
        ]
        Path(session["_path"]).write_text(
            json.dumps({k: v for k, v in session.items() if not k.startswith("_")}),
            encoding="utf-8",
        )

        def _fake_execute(scripts, **kwargs):
            sess = kwargs["session"]
            sess["results"].append({
                "key": "s3_export.py", "script": "s3_export.py", "status": "success",
                "exit_code": 0, "duration_s": 4.0, "output_file": str(second),
            })
            return {"results": [FakeResult(script="s3_export.py", output_file=str(second))]}

        monkeypatch.setattr(cli, "execute_scripts", _fake_execute)
        monkeypatch.setattr(
            utils, "prompt_region_selection",
            lambda *a, **k: pytest.fail("resume must reuse the session's persisted regions"),
        )

        cli._resume_from_session(session["_path"])

        zips = list((out_dir / "service-discovery").glob("*.zip"))
        assert len(zips) == 1
        names = _namelist(zips[0])
        assert first.name in names
        assert second.name in names
        assert f"ACCT-service-discovery-report-{_date()}.md" in names

    def test_resume_with_nothing_remaining_still_zips(self, cli, out_dir, tmp_path, monkeypatch):
        export = _make_export(out_dir, f"ACCT-ec2-all-regions-export-{_date()}.xlsx")
        session = self._session(tmp_path, [
            {
                "key": "ec2_export.py", "script": "ec2_export.py", "status": "success",
                "exit_code": 0, "duration_s": 3.0, "output_file": str(export),
            },
        ])

        monkeypatch.setattr(
            cli, "execute_scripts",
            lambda *a, **k: pytest.fail("nothing remained to execute"),
        )

        cli._resume_from_session(session["_path"])

        zips = list((out_dir / "service-discovery").glob("*.zip"))
        assert len(zips) == 1
        assert export.name in _namelist(zips[0])


class TestSkipMarkerGlobIsolation:
    def test_marker_does_not_match_real_export_glob(self, cli):
        import fnmatch

        marker = f"ACCT-billing-skipped-no-permission-export-{_date()}.xlsx"
        assert cli._is_skip_marker(marker)
        assert not fnmatch.fnmatch(marker, "*billing-last-12-months-export-*.xlsx")

    def test_real_export_is_not_a_skip_marker(self, cli):
        assert not cli._is_skip_marker(
            f"ACCT-billing-last-12-months-export-{_date()}.xlsx"
        )


class TestSessionMetadataPersistence:
    def test_update_scan_session_persists_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr(utils, "get_output_dir", lambda: tmp_path)
        session = utils.start_scan_session(
            "smart-scan", "Smart Scan (1 script)", [{"key": "ec2_export.py"}]
        )
        utils.update_scan_session(
            session, regions=["us-east-1"], services=["EC2"], account_name="ACCT"
        )
        on_disk = json.loads(Path(session["_path"]).read_text(encoding="utf-8"))
        assert on_disk["regions"] == ["us-east-1"]
        assert on_disk["services"] == ["EC2"]
        assert on_disk["account_name"] == "ACCT"
        assert "_path" not in on_disk

    def test_executor_persists_output_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(utils, "get_output_dir", lambda: tmp_path)
        session = utils.start_scan_session(
            "smart-scan", "Smart Scan (1 script)", [{"key": "ec2_export.py"}]
        )
        utils.record_scan_result(
            session, "ec2_export.py", "success", 0, 3.0,
            script="ec2_export.py", output_file="/tmp/x.xlsx",
        )
        on_disk = json.loads(Path(session["_path"]).read_text(encoding="utf-8"))
        assert on_disk["results"][0]["output_file"] == "/tmp/x.xlsx"


class TestDurationFormatting:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "0s"),
        (45, "45s"),
        (65, "1m 5s"),
        (3725, "1h 2m 5s"),
    ])
    def test_format_duration(self, cli, seconds, expected):
        assert cli._format_duration(seconds) == expected


def test_report_timestamp_is_present(cli, out_dir):
    export = _make_export(out_dir, f"ACCT-ec2-all-regions-export-{_date()}.xlsx")
    result = FakeResult(script="ec2_export.py", output_file=str(export))
    zip_path = cli._archive_session(
        None, [result], "ACCT", "123456789012", ["us-east-1"],
    )
    text = _report_text(zip_path)
    assert datetime.datetime.now().strftime("%Y-%m-%d") in text
