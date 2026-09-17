#!/usr/bin/env python3
"""
StratusScan Smart Scan

Unified service discovery and script recommendation workflow.

Discovers all AWS services in use, generates a report (console + Markdown +
Excel), then optionally executes the recommended export scripts.

Usage:
    python smart_scan.py               # interactive
    STRATUSSCAN_AUTO_RUN=1 python smart_scan.py  # CI / headless (Quick Scan)
"""

import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# Ensure the project root is on sys.path for utils
_root = Path(__file__).parent.absolute()
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# Ensure scripts/ is on sys.path so services_in_use_export and smart_scan package
# are importable as top-level names
_scripts_dir = _root / 'scripts'
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

try:
    import utils
except ImportError as exc:
    print(f"Error: could not import utils from {_root}: {exc}")
    sys.exit(1)

logger = utils.setup_logging('smart-scan')

try:
    import pandas as pd
except ImportError:
    print("Error: pandas is not installed. Install with: pip install pandas")
    sys.exit(1)

try:
    from services_in_use_export import (
        create_category_sheets,
        create_detailed_export,
        create_recommendations_sheet,
        discover_services,
        generate_summary,
    )
except ImportError as exc:
    utils.log_error(f"Could not import services_in_use_export: {exc}", exc)
    sys.exit(1)

try:
    from smart_scan.analyzer import analyze_services_from_dict
    from smart_scan.executor import execute_scripts
    from smart_scan.mapping import (
        ALWAYS_RUN_SCRIPTS,  # noqa: F401  # part of the import-or-die package check
    )
except ImportError as exc:
    utils.log_error(f"Could not import smart_scan package: {exc}", exc)
    sys.exit(1)


def _prompt_scan_mode() -> str:
    """Prompt user for Quick or Deep scan mode. Exits on b/x/q."""
    try:
        choice = utils.prompt_menu(
            "SCAN MODE",
            [
                "Quick Scan  — discover services, save a report, done",
                "Deep Scan   — discover services, then run export scripts",
            ],
        )
    except (utils.BackSignal, utils.ExitToMainSignal, utils.QuitSignal):
        sys.exit(0)
    return 'deep' if choice == 2 else 'quick'


def _format_detail(detail: dict[str, int]) -> str:
    """Format a detail dict as a readable inline string."""
    return "  |  ".join(f"{k}: {v}" for k, v in detail.items() if v > 0)


def _print_discovery_summary(
    services: dict[str, Any],
    recommendations: Optional[dict[str, Any]] = None,
) -> None:
    """Print formatted discovery results to console (Deep Scan only)."""
    print()
    print("=" * 70)
    print("  SERVICES DISCOVERED")
    print("=" * 70)

    # Group by category
    by_category: dict[str, list] = {}
    for name, data in sorted(services.items()):
        cat = data['category']
        by_category.setdefault(cat, []).append((name, data))

    service_scripts = (recommendations or {}).get('service_based', {})

    for category, items in sorted(by_category.items()):
        print(f"\n  {category}")
        print(f"  {'─' * 60}")
        for name, data in items:
            capped = data.get('capped', False)
            count_str = f"{'500+':>5}" if capped else f"{data['count']:>5}"
            print(f"  {name:<35} {count_str} {data['unit']}")
            if data.get('detail'):
                print(f"    └─ {_format_detail(data['detail'])}")
            if data['regional'] and data['regions']:
                region_breakdown = "  |  ".join(
                    f"{r}: {c}" for r, c in sorted(data['regions'].items())
                )
                print(f"    └─ {region_breakdown}")
            else:
                print("    └─ global")
            if capped:
                scripts = service_scripts.get(name, [])
                script_hint = f" — run {scripts[0]} for the complete inventory" if scripts else ""
                print(f"    └─ 500+ found{script_hint}")

    total_resources = sum(s['count'] for s in services.values())
    print()
    print("=" * 70)
    print(f"  Total services: {len(services)}   Total resources: {total_resources:,}")
    print("=" * 70)


def _write_quick_scan_excel(
    recommendations: dict[str, Any],
    account_name: str,
    regions: list[str],
) -> None:
    """
    Write a minimal two-column Excel for Quick Scan results.

    Columns: Service In Use | Recommended Script
    One row per service × script pair. Security baseline scripts are
    grouped under a 'Security Baseline' service label.
    """
    rows = []

    for script in sorted(recommendations.get('always_run', [])):
        rows.append({'Service In Use': 'Security Baseline', 'Recommended Script': script})

    for service_name, scripts in sorted(recommendations.get('service_based', {}).items()):
        for script in sorted(scripts):
            rows.append({'Service In Use': service_name, 'Recommended Script': script})

    if not rows:
        return

    df = pd.DataFrame(rows)
    df = utils.prepare_dataframe_for_export(df)

    region_suffix = 'all-regions' if len(regions) > 1 else regions[0]
    filename = utils.create_export_filename(account_name, 'quick-scan', region_suffix)
    utils.save_dataframe_to_excel(df, filename)
    utils.log_success(f"  Excel saved: {utils.get_output_filepath(filename)}")


def _write_markdown_report(
    services: dict[str, Any],
    recommendations: dict[str, Any],
    account_name: str,
    account_id: str,
    regions: list[str],
    mode: str,
    crosscheck: Optional[dict[str, Any]] = None,
) -> Optional[Path]:
    """
    Write discovery report as Markdown to reports/ directory.

    Returns the path to the written file, or None on failure.
    """
    reports_dir = _root / 'reports'
    reports_dir.mkdir(exist_ok=True)

    mode_label = 'deep' if mode == 'deep' else 'quick'
    timestamp = utils.get_export_date()
    filename = f"{account_name}-discovery-{mode_label}-{timestamp}.md"
    filepath = reports_dir / filename

    now = datetime.now().strftime('%Y-%m-%d %H:%M UTC')
    scan_label = 'Deep Scan' if mode == 'deep' else 'Quick Scan'

    lines = [
        "# AWS Service Discovery Report",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Account | {account_name} ({utils.mask_account_id(account_id)}) |",
        f"| Scan Date | {now} |",
        f"| Scan Mode | {scan_label} |",
        f"| Regions | {', '.join(regions)} |",
        f"| Services Found | {len(services)} |",
        f"| Total Resources | {sum(s['count'] for s in services.values()):,} |",
        "",
        "---",
        "",
        "## Services Discovered",
        "",
    ]

    # Group by category
    by_category: dict[str, list] = {}
    for name, data in sorted(services.items()):
        by_category.setdefault(data['category'], []).append((name, data))

    service_scripts = recommendations.get('service_based', {})

    for category, items in sorted(by_category.items()):
        lines.append(f"### {category}")
        lines.append("")
        if mode == 'deep':
            lines.append("| Service | Count | Unit | Regions | Detail |")
            lines.append("|---|---|---|---|---|")
            for name, data in items:
                region_str = (
                    ', '.join(sorted(data['regions'])) if data['regional'] else 'global'
                )
                detail_str = _format_detail(data.get('detail', {})) or '—'
                count_str = '500+' if data.get('capped') else str(data['count'])
                if data.get('capped'):
                    scripts = service_scripts.get(name, [])
                    if scripts:
                        detail_str = f"500+ found — run `{scripts[0]}` for complete data"
                lines.append(
                    f"| {name} | {count_str} | {data['unit']} | {region_str} | {detail_str} |"
                )
        else:
            lines.append("| Service | Count | Unit | Regions |")
            lines.append("|---|---|---|---|")
            for name, data in items:
                region_str = (
                    ', '.join(sorted(data['regions'])) if data['regional'] else 'global'
                )
                count_str = '500+' if data.get('capped') else str(data['count'])
                lines.append(
                    f"| {name} | {count_str} | {data['unit']} | {region_str} |"
                )
        lines.append("")

    n_baseline = len(recommendations.get('always_run', []))
    n_service = recommendations.get('coverage_stats', {}).get('service_based_count', 0)
    lines += [
        "---",
        "",
        "## Recommended Export Scripts",
        "",
        f"**{len(recommendations.get('all_scripts', set()))} scripts recommended** "
        f"({n_baseline} security baseline + {n_service} service-specific)",
        "",
    ]

    always_run = recommendations.get('always_run', [])
    if always_run:
        lines.append("### Security Baseline (Always Run)")
        lines.append("")
        for script in sorted(always_run):
            lines.append(f"- `{script}`")
        lines.append("")

    for category, scripts in sorted(recommendations.get('by_category', {}).items()):
        service_scripts = [s for s in scripts if s not in always_run]
        if not service_scripts:
            continue
        lines.append(f"### {category}")
        lines.append("")
        for script in service_scripts:
            lines.append(f"- `{script}`")
        lines.append("")

    lines += _crosscheck_markdown_lines(crosscheck)

    try:
        filepath.write_text('\n'.join(lines), encoding='utf-8')
        return filepath
    except Exception as e:
        utils.log_warning(f"Failed to write Markdown report: {e}")
        return None


def _service_discovery_dir() -> Path:
    """
    Return (creating if needed) the directory that holds service-discovery archives.

    Built from ``utils.get_output_dir()`` rather than a filename containing a
    separator: ``get_output_filepath()`` rejects separators by design
    (containment), so the subdirectory has to be composed from the output root.
    Deriving it this way also means ``--output-dir`` is honored for free.
    """
    target = utils.get_output_dir() / "service-discovery"
    target.mkdir(parents=True, exist_ok=True)
    return target


# Exporters that decline to run — no permission, service absent from the
# partition — still exit 0 and drop a marker workbook whose name carries this
# token. Deliberately distinct from the real-export filename patterns so a
# marker never satisfies an export glob.
_SKIP_MARKER_TOKEN = '-skipped-'


def _is_skip_marker(name: str) -> bool:
    """True when a produced filename is a deliberate-skip marker workbook."""
    return _SKIP_MARKER_TOKEN in name


def _skip_reason_from_filename(name: str) -> str:
    """
    Derive a human-readable skip reason from a marker workbook filename.

    Marker names look like ``ACCT-billing-skipped-no-permission-export-DATE.xlsx``;
    the text between the skip token and the trailing ``-export-`` is the reason.
    """
    try:
        tail = name.split(_SKIP_MARKER_TOKEN, 1)[1]
        reason = tail.split('-export-', 1)[0]
    except IndexError:
        return 'skipped'
    reason = reason.replace('-', ' ').strip()
    if not reason:
        return 'skipped'
    if reason == 'no permission':
        return 'missing permission'
    if reason == 'govcloud':
        return 'not available in this partition (GovCloud)'
    return reason


def _format_duration(seconds: float) -> str:
    """Format a wall-clock duration the same way ExecutionResult does."""
    secs = int(seconds or 0)
    hours, minutes, rem = secs // 3600, (secs % 3600) // 60, secs % 60
    if hours:
        return f"{hours}h {minutes}m {rem}s"
    if minutes:
        return f"{minutes}m {rem}s"
    return f"{rem}s"


def _session_output_records(session: Optional[dict], results: list) -> list[dict]:
    """
    Flatten every run of a session into one ordered list of result records.

    The session file is the union view: it accumulates records across the
    interrupted run and every resume, so archiving from it fixes the
    resumed-zip-omits-the-first-run defect (#291). Current-run
    ExecutionResult objects are folded in as a fallback for callers with no
    session, and to cover a record the session failed to persist.
    """
    records: list[dict] = []
    seen_keys: set[str] = set()

    for r in (session or {}).get('results', []):
        key = str(r.get('key') or r.get('script') or '')
        records.append({
            'script': r.get('script') or key,
            'status': r.get('status', 'unknown'),
            'duration_s': r.get('duration_s', 0.0),
            'output_file': r.get('output_file'),
        })
        seen_keys.add(key)

    for r in results:
        if r.script in seen_keys:
            continue
        records.append({
            'script': r.script,
            'status': 'success' if r.success else 'failed',
            'duration_s': r.duration_seconds,
            'output_file': r.output_file,
        })
        seen_keys.add(r.script)

    return records


def _partition_records(records: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[Path]]:
    """
    Split result records into (ran, skipped, missing, files-to-archive).

    - ran:     produced a real export workbook that is still on disk
    - skipped: exited 0 but declined to collect (marker workbook), or produced
               no output at all
    - missing: recorded an output file that is no longer on disk — reported,
               never silently dropped
    """
    ran: list[dict] = []
    skipped: list[dict] = []
    missing: list[dict] = []
    files: list[Path] = []
    seen_files: set[str] = set()

    for rec in records:
        out = rec.get('output_file')
        if not out:
            if rec.get('status') == 'success':
                rec = {**rec, 'reason': 'completed without producing an output file'}
                skipped.append(rec)
            else:
                ran.append(rec)
            continue

        path = Path(out)
        name = path.name
        if path.exists():
            if str(path) not in seen_files:
                files.append(path)
                seen_files.add(str(path))
        else:
            missing.append({**rec, 'reason': 'output file no longer present on disk'})
            continue

        if _is_skip_marker(name):
            skipped.append({**rec, 'reason': _skip_reason_from_filename(name)})
        else:
            ran.append(rec)

    return ran, skipped, missing, files


def _scan_report_markdown(
    account_name: str,
    account_id: str,
    regions: list[str],
    services: Optional[list[str]],
    ran: list[dict],
    skipped: list[dict],
    missing: list[dict],
    crosscheck: Optional[dict[str, Any]] = None,
) -> str:
    """
    Build the scan report that travels inside the archive.

    Deliberately count-free in the Services In Use section: a number beside a
    service name reads as an asset inventory, and several exporters emit
    summaries rather than inventories, which would make that number wrong.
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M UTC')
    lines = [
        "# AWS Service Discovery Scan Report",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Account | {account_name} ({utils.mask_account_id(account_id)}) |",
        f"| Regions Scanned | {', '.join(regions) if regions else 'unknown'} |",
        f"| Tool Version | {utils.get_version()} |",
        f"| Report Generated | {now} |",
        "",
        "---",
        "",
        "## Services In Use",
        "",
    ]

    if services:
        for name in sorted(services):
            lines.append(f"- {name}")
    else:
        lines.append("_Service discovery results were not recorded for this session._")
    lines += ["", "---", "", "## What Ran", ""]

    if ran:
        lines += [
            "| Script | Result | Duration | Output File |",
            "|---|---|---|---|",
        ]
        for rec in sorted(ran, key=lambda r: r['script']):
            status = 'success' if rec.get('status') == 'success' else 'FAILED'
            out = Path(rec['output_file']).name if rec.get('output_file') else '—'
            lines.append(
                f"| `{rec['script']}` | {status} | "
                f"{_format_duration(rec.get('duration_s', 0.0))} | {out} |"
            )
    else:
        lines.append("_No export scripts produced data in this session._")
    lines += ["", "---", "", "## What Was Skipped And Why", ""]

    if skipped:
        lines += [
            "These exporters exited cleanly without collecting data. A skip is not a "
            "clean result — treat each row as a coverage gap.",
            "",
            "| Script | Reason | Marker File |",
            "|---|---|---|",
        ]
        for rec in sorted(skipped, key=lambda r: r['script']):
            marker = Path(rec['output_file']).name if rec.get('output_file') else '—'
            lines.append(f"| `{rec['script']}` | {rec.get('reason', 'skipped')} | {marker} |")
    else:
        lines.append("No exporters were skipped.")
    lines.append("")

    if missing:
        lines += [
            "---",
            "",
            "## Missing Prior Outputs",
            "",
            "These scripts recorded an output file in an earlier run of this session, "
            "but the file is no longer on disk and is therefore **not** in this archive.",
            "",
            "| Script | Missing File |",
            "|---|---|",
        ]
        for rec in sorted(missing, key=lambda r: r['script']):
            lines.append(f"| `{rec['script']}` | {Path(rec['output_file']).name} |")
        lines.append("")

    lines += _crosscheck_markdown_lines(crosscheck)
    return '\n'.join(lines)


def _zip_export_files(
    results: list,
    account_name: str,
    report_text: Optional[str] = None,
    extra_files: Optional[list] = None,
) -> Optional[Path]:
    """
    Zip output files produced by the batch execution into a single archive.

    The archive lands in ``output/service-discovery/``; one-off exports still
    write to the output root.

    Args:
        results: List of ExecutionResult objects from execute_all()
        account_name: AWS account name (used in the zip and report filenames)
        report_text: Optional Markdown scan report written into the archive.
        extra_files: Additional output paths to include — used to fold in
                     outputs from earlier runs of a resumed session.

    Returns:
        Path to the zip file, or None if there is nothing to zip or on failure.
    """
    output_files: list[Path] = []
    seen: set[str] = set()
    for candidate in [Path(r.output_file) for r in results if r.output_file] + [
        Path(p) for p in (extra_files or [])
    ]:
        if str(candidate) not in seen:
            output_files.append(candidate)
            seen.add(str(candidate))

    if not output_files and not report_text:
        return None

    timestamp = utils.get_export_date()
    zip_name = f"{account_name}-service-discovery-export-{timestamp}.zip"
    zip_path = _service_discovery_dir() / zip_name

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file_path in output_files:
                if file_path.exists():
                    zf.write(file_path, file_path.name)
            if report_text:
                report_name = (
                    f"{account_name}-service-discovery-report-{timestamp}.md"
                )
                zf.writestr(report_name, report_text)

        # Remove individual files now that they are safely inside the zip
        for file_path in output_files:
            try:
                file_path.unlink(missing_ok=True)
            except Exception as e:
                utils.log_warning(f"Could not remove {file_path.name} after zipping: {e}")

        return zip_path
    except Exception as e:
        utils.log_warning(f"Failed to create zip archive: {e}")
        return None


def _archive_session(
    session: Optional[dict],
    results: list,
    account_name: str,
    account_id: str,
    regions: list[str],
    services: Optional[list[str]] = None,
    crosscheck: Optional[dict[str, Any]] = None,
) -> Optional[Path]:
    """
    Build the scan report and zip every output of the session into one archive.

    Shared by the fresh-run and resume paths so both entry points deliver the
    same archive: the union of all runs of the session, plus the report.
    """
    records = _session_output_records(session, results)
    ran, skipped, missing, files = _partition_records(records)

    report_text = _scan_report_markdown(
        account_name, account_id, regions, services, ran, skipped, missing,
        crosscheck=crosscheck,
    )
    return _zip_export_files([], account_name, report_text=report_text, extra_files=files)


def _resume_from_session(session_path: str) -> None:
    """
    Execute the remaining scripts from an interrupted smart-scan session.
    Skips service discovery entirely — uses the planned list from the session file.
    """
    import json
    try:
        session: dict = json.loads(Path(session_path).read_text(encoding="utf-8"))
        session["_path"] = session_path
    except Exception as exc:
        print(f"\n  ❌ Could not load session: {exc}")
        return

    account_id = session.get("account_id") or ""
    account_name = session.get("account_name") or ""
    if not account_name:
        account_id, account_name = utils.get_account_info()

    done_keys = {r["key"] for r in session.get("results", []) if r.get("status") == "success"}
    all_planned = {p["key"] for p in session.get("planned", [])}
    remaining = all_planned - done_keys

    n_done = len(done_keys)
    n_total = len(session.get("planned", []))

    print(f"\n  Resuming Smart Scan: {n_done}/{n_total} scripts already complete")
    print(f"  {len(remaining)} script(s) remaining\n")

    services = session.get("services")
    summary: dict[str, Any] = {}

    if remaining:
        regions = session.get("regions") or utils.prompt_region_selection()
        utils.resume_scan_session(session)

        print(f"\n  Executing {len(remaining)} scripts...\n")
        summary = execute_scripts(
            remaining,
            show_progress=True,
            save_log=False,
            regions=regions,
            show_output=False,
            session=session,
            skip_scripts=None,
        )
    else:
        print("  ✅ All scripts already completed.")
        regions = session.get("regions") or []
        utils.complete_scan_session(session)

    # Archive regardless of whether anything remained to run: the earlier
    # interrupted run never reached its zip step, so its outputs are still loose.
    zip_path = _archive_session(
        session,
        summary.get("results", []),
        account_name,
        account_id,
        regions,
        services=services,
        crosscheck=session.get("crosscheck"),
    )
    if zip_path:
        utils.log_success(f"  Exports zipped: {zip_path}")


def _run_bill_crosscheck(
    services: dict[str, Any], regions: list[str]
) -> Optional[dict[str, Any]]:
    """Run the Cost Explorer ground-truth cross-check unless opted out.

    Never raises — returns the cross-check result, a skip status dict, or None
    when disabled/unavailable so callers can always proceed.
    """
    if os.environ.get("STRATUSSCAN_SKIP_BILL_CROSSCHECK") == "1":
        utils.log_info("Bill cross-check disabled (STRATUSSCAN_SKIP_BILL_CROSSCHECK=1)")
        return None
    try:
        from smart_scan.bill_crosscheck import run_crosscheck
    except ImportError as exc:
        utils.log_warning(f"Bill cross-check unavailable: {exc}")
        return None
    partition = utils.detect_partition(regions[0]) if regions else None
    return run_crosscheck(set(services.keys()), partition=partition)


# (result bucket, human label) in audit-priority order.
_CROSSCHECK_STATUS_ORDER = [
    ('not_collected', 'SPEND, NOT COLLECTED'),
    ('confirmed', 'CONFIRMED'),
    ('unmapped', 'SPEND, UNMAPPED'),
    ('ignored', 'IGNORED (billing line item)'),
]


def _crosscheck_dataframe(result: dict[str, Any]) -> "pd.DataFrame":
    """Flatten a cross-check result into a single status-tagged DataFrame."""
    rows = []
    for bucket, label in _CROSSCHECK_STATUS_ORDER:
        for r in result.get(bucket, []):
            rows.append({
                'Status': label,
                'Billed Service (Cost Explorer)': r['ce_service'],
                'Mapped Service': r.get('service', ''),
                'Monthly Cost (USD)': r['monthly_cost'],
                'Exporters': ', '.join(r.get('exporters', [])),
            })
    return pd.DataFrame(rows)


def _crosscheck_markdown_lines(result: Optional[dict[str, Any]]) -> list[str]:
    """Render the cross-check as a Markdown report section."""
    if not result:
        return []
    lines = ["---", "", "## Bill Cross-Check", ""]
    if result.get('status') != 'ok':
        return lines + [f"_Skipped: {result.get('reason', 'unavailable')}_", ""]

    p = result['period']
    lines += [
        f"Cost Explorer spend for {p['start']} to {p['end']} "
        f"({result['total_services_billed']} billed services), reconciled against discovery.",
        "",
    ]
    not_collected = result.get('not_collected', [])
    if not_collected:
        lines += [
            "### ⚠ Spend detected but NOT collected",
            "",
            "| Billed Service | Mapped Service | Monthly Cost (USD) | Exporter(s) |",
            "|---|---|---|---|",
        ]
        for r in not_collected:
            lines.append(
                f"| {r['ce_service']} | {r['service']} | {r['monthly_cost']:,.2f} "
                f"| {', '.join(r['exporters'])} |"
            )
        lines.append("")
    else:
        lines += ["Every billed service was discovered. ✅", ""]

    unmapped = result.get('unmapped', [])
    if unmapped:
        lines += [
            "### Billed but unmapped (no known service)",
            "",
            "| Billed Service | Monthly Cost (USD) |",
            "|---|---|",
        ]
        for r in unmapped:
            lines.append(f"| {r['ce_service']} | {r['monthly_cost']:,.2f} |")
        lines.append("")
    return lines


def _print_crosscheck_summary(result: Optional[dict[str, Any]]) -> None:
    """Print a concise cross-check summary to the console."""
    if not result:
        return
    if result.get('status') != 'ok':
        print(f"\n  Bill cross-check skipped: {result.get('reason', 'unavailable')}")
        return
    not_collected = result.get('not_collected', [])
    unmapped = result.get('unmapped', [])
    print()
    print("  ─── BILL CROSS-CHECK ────────────────────────────────────────")
    print(
        f"  Period {result['period']['start']} → {result['period']['end']}  "
        f"({result['total_services_billed']} billed services)"
    )
    if not_collected:
        print(f"  ⚠ {len(not_collected)} service(s) with spend NOT collected by discovery:")
        for r in not_collected[:10]:
            print(f"      ${r['monthly_cost']:>12,.2f}  {r['service']}")
        if len(not_collected) > 10:
            print(f"      ... and {len(not_collected) - 10} more (see report)")
    else:
        print(f"  {utils.GLYPH_OK} Every billed service was discovered.")
    if unmapped:
        print(f"  • {len(unmapped)} billed service(s) could not be mapped (see report).")
    print("  ─────────────────────────────────────────────────────────────")


def main() -> None:
    """Main Smart Scan workflow."""
    utils.log_script_start('smart-scan')

    # Startup resume: stratusscan.py passes session path via env var
    resume_path = os.environ.get("STRATUSSCAN_RESUME_SESSION_PATH", "")
    if resume_path:
        utils.print_script_banner("SMART SCAN — RESUME INTERRUPTED SESSION")
        _resume_from_session(resume_path)
        return

    account_id, account_name = utils.print_script_banner(
        "SMART SCAN — SERVICE DISCOVERY & RECOMMENDATIONS"
    )
    if not account_id:
        utils.log_error("Unable to determine AWS account ID. Check credentials.", None)
        return

    utils.log_info(f"Account: {account_name} ({utils.mask_account_id(account_id)})")

    # Scan mode selection
    if utils.is_auto_run():
        scan_mode = 'quick'
        utils.log_info("Auto-run mode: defaulting to Quick Scan")
    else:
        scan_mode = _prompt_scan_mode()

    # Region selection
    regions = utils.prompt_region_selection()

    # Discovery
    print(f"\n  Running {scan_mode.title()} Scan across {len(regions)} region(s)...\n")
    services, errors = discover_services(regions, mode=scan_mode)

    if not services:
        utils.log_warning("No services with resources found.")
        return

    if errors:
        utils.log_warning(f"  {len(errors)} service(s) had unexpected check failures (see log)")

    # Recommendations — in-memory, no Excel roundtrip
    utils.log_info("Generating recommendations...")
    recommendations = analyze_services_from_dict(services)

    n_scripts = len(recommendations.get('all_scripts', set()))
    n_baseline = len(recommendations.get('always_run', []))
    n_service = recommendations.get('coverage_stats', {}).get('service_based_count', 0)
    print(
        f"\n  Recommended scripts: {n_scripts}"
        f"  ({n_baseline} security baseline + {n_service} service-specific)"
    )

    # Bill cross-check (Cost Explorer ground truth). Opt out with
    # STRATUSSCAN_SKIP_BILL_CROSSCHECK=1; skips cleanly in GovCloud / without perms.
    crosscheck = _run_bill_crosscheck(services, regions)
    _print_crosscheck_summary(crosscheck)

    # Quick Scan: write lightweight reports and exit
    if scan_mode == 'quick':
        md_path = _write_markdown_report(
            services, recommendations, account_name, account_id, regions, scan_mode,
            crosscheck=crosscheck,
        )
        if md_path:
            utils.log_success(f"  Report saved: {md_path}")

        _write_quick_scan_excel(recommendations, account_name, regions)

        print()
        print("  Quick Scan complete.")
        print(f"  {n_scripts} export scripts are recommended for this account.")
        print("  Run a Deep Scan or individual scripts to collect full resource data.")
        if not utils.is_auto_run():
            input("\n  Press Enter to return to menu...")
        return

    # Deep Scan: show full discovery table
    _print_discovery_summary(services, recommendations=recommendations)

    # Write Markdown report
    md_path = _write_markdown_report(
        services, recommendations, account_name, account_id, regions, scan_mode,
        crosscheck=crosscheck,
    )
    if md_path:
        utils.log_success(f"  Report saved: {md_path}")

    # Write Excel report (existing pipeline)
    try:
        summary_data = generate_summary(services)
        df_summary = pd.DataFrame(summary_data)
        df_summary = utils.prepare_dataframe_for_export(df_summary)

        df_details = create_detailed_export(services)
        df_details = utils.prepare_dataframe_for_export(df_details)

        category_sheets = create_category_sheets(services)

        df_recs = create_recommendations_sheet(services)
        df_recs = utils.prepare_dataframe_for_export(df_recs)

        dataframes: dict[str, Any] = {
            'Summary': df_summary,
            'Recommended Scripts': df_recs,
            'All Services': df_details,
        }
        for category, df in category_sheets.items():
            sheet_name = category.replace(' Resources', '').replace('&', 'and')[:31]
            dataframes[sheet_name] = utils.prepare_dataframe_for_export(df)

        if crosscheck and crosscheck.get('status') == 'ok':
            df_cc = _crosscheck_dataframe(crosscheck)
            if not df_cc.empty:
                dataframes['Bill Cross-Check'] = utils.prepare_dataframe_for_export(df_cc)

        region_suffix = 'all-regions' if len(regions) > 1 else regions[0]
        filename = utils.create_export_filename(account_name, 'services-in-use', region_suffix)
        utils.save_multiple_dataframes_to_excel(dataframes, filename)
        utils.log_success(f"  Excel saved: {utils.get_output_filepath(filename)}")
    except Exception as e:
        utils.log_warning(f"Excel export failed (continuing): {e}")

    # Deep Scan: prompt to execute recommended scripts
    # Skip execution prompt in CI/headless mode
    if utils.is_auto_run():
        utils.log_info("Auto-run mode: skipping execution prompt")
        return

    print()
    print(f"  {n_scripts} export scripts are recommended for this account.")
    try:
        gate = utils.prompt_menu(
            "RUN SCRIPTS",
            [
                "Run all recommended scripts now",
                "Exit — report saved, run scripts later",
                "Customize — choose specific scripts to run",
            ],
        )
    except (utils.BackSignal, utils.ExitToMainSignal, utils.QuitSignal):
        utils.log_info("Exiting. Reports saved.")
        return

    if gate == 2:
        utils.log_info("Exiting. Reports saved.")
        return

    selected_scripts = recommendations.get('all_scripts', set())

    if gate == 3:
        # interactive_select handles the questionary / plain-text fallback itself.
        try:
            from smart_scan.selector import interactive_select
            selected_scripts = interactive_select(recommendations) or set()
        except ImportError:
            utils.log_warning("Selector unavailable — running all recommended scripts")

    if not selected_scripts:
        utils.log_info("No scripts selected. Exiting.")
        return

    # Build planned list for session persistence
    planned = [{"key": s, "script": s} for s in sorted(selected_scripts)]

    # Offer resume of an interrupted smart-scan session
    session: dict
    skip_scripts: Optional[set] = None
    interrupted_smart = [
        s for s in utils.get_interrupted_sessions()
        if s.get("scan_type") == "smart-scan"
    ]
    if interrupted_smart and not utils.is_auto_run():
        prev = interrupted_smart[0]
        n_done = len(prev.get("results", []))
        n_total = len(prev.get("planned", []))
        if utils.prompt_for_confirmation(
            f"Resume interrupted smart scan? ({n_done}/{n_total} scripts done)",
            default=False,
        ):
            utils.resume_scan_session(prev)
            session = prev
            done_keys = {r["key"] for r in prev.get("results", []) if r.get("status") == "success"}
            selected_scripts = selected_scripts - done_keys
            skip_scripts = None  # already filtered from selected_scripts
        else:
            session = utils.start_scan_session(
                "smart-scan",
                f"Smart Scan ({len(selected_scripts)} scripts)",
                planned,
            )
    else:
        session = utils.start_scan_session(
            "smart-scan",
            f"Smart Scan ({len(selected_scripts)} scripts)",
            planned,
        )

    # Persist the run context so an interruption does not cost the report its
    # header, region list or discovered-service list on resume.
    utils.update_scan_session(
        session,
        account_id=account_id,
        account_name=account_name,
        regions=regions,
        services=sorted(services.keys()),
        crosscheck=crosscheck,
    )

    print(f"\n  Executing {len(selected_scripts)} scripts...\n")
    summary = execute_scripts(
        selected_scripts,
        show_progress=True,
        save_log=True,
        regions=regions,
        show_output=False,
        session=session,
        skip_scripts=skip_scripts,
    )

    # Archive every output of this session (all runs) plus the scan report
    zip_path = _archive_session(
        session,
        summary.get('results', []),
        account_name,
        account_id,
        regions,
        services=sorted(services.keys()),
        crosscheck=crosscheck,
    )
    if zip_path:
        utils.log_success(f"  Exports zipped: {zip_path}")

    print()
    print("=" * 70)
    print("  EXECUTION COMPLETE")
    print("=" * 70)
    print(f"  Total:        {summary['total']}")
    print(f"  Successful:   {summary['successful']}")
    print(f"  Failed:       {summary['failed']}")
    print(f"  Success Rate: {summary['success_rate']:.1f}%")
    print("=" * 70)
    if not utils.is_auto_run():
        input("\n  Press Enter to return to menu...")


if __name__ == "__main__":
    main()
