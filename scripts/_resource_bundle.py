#!/usr/bin/env python3
"""
Shared orchestration engine for the "All <Category> Resources" bundle exporters.

Every bundle script (compute_resources.py, storage_resources.py, ...) is a thin
wrapper that defines a category name, a filename slug, and a registry of
(display_name, exporter_filename) tuples, then calls run_bundle().  All of the
heavy lifting — the 3-step region/script/confirm state machine, non-interactive
AUTO_RUN handling, per-script subprocess execution with progress, new-output
detection, zip archiving, and the summary table — lives here so a fix lands in
one place instead of N copy-pasted files.

This module is the CLI layer (it is invoked only by bundle scripts that are
themselves launched as subprocesses), so print() is intentional and allowed —
the no-print rule in Issue #171 governs utils.py only.
"""

import os
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import utils
except ImportError:
    sys.path.append(str(Path(__file__).parent.parent))
    import utils


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: float) -> str:
    """Format a duration in seconds as a human-readable string."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _snapshot_xlsx(output_dir: Path) -> tuple[set, float]:
    """Capture existing .xlsx filenames and current epoch time."""
    try:
        return {str(p) for p in output_dir.glob("*.xlsx")}, time.time()
    except Exception:
        return set(), 0.0


def _detect_new_xlsx(
    output_dir: Path,
    pre: tuple[set, float],
) -> Optional[str]:
    """Return the path of the newest .xlsx file created after *pre*."""
    try:
        pre_set, snap_time = pre
        candidates = [
            p for p in output_dir.glob("*.xlsx")
            if str(p) not in pre_set and p.stat().st_mtime >= snap_time
        ]
        if candidates:
            return str(max(candidates, key=lambda p: p.stat().st_mtime))
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Multi-select script menu
# ---------------------------------------------------------------------------

def prompt_script_selection(
    category_name: str,
    scripts: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """
    Present a numbered multi-select menu for script selection.

    Returns the list of selected (display_name, filename) tuples. Navigation
    is handled the single-voice way via utils.prompt_multiselect, which raises
    BackSignal / ExitToMainSignal / QuitSignal for b / x / q.
    """
    # Auto-run mode: select everything without prompting
    if utils.is_auto_run():
        return list(scripts)

    indices = utils.prompt_multiselect(
        f"SELECT {category_name.upper()} TO EXPORT",
        [name for name, _ in scripts],
        all_label=f"All  (every {category_name.lower()} exporter)",
    )
    return [scripts[i - 1] for i in indices]


# ---------------------------------------------------------------------------
# Subprocess execution
# ---------------------------------------------------------------------------

@dataclass
class ScriptResult:
    """Result of a single child-script execution."""
    name: str
    filename: str
    success: bool
    duration_seconds: float
    output_file: Optional[str] = None
    error: Optional[str] = None


def run_script(
    name: str,
    script_path: Path,
    regions: list[str],
    output_dir: Path,
    index: int,
    total: int,
) -> ScriptResult:
    """Invoke a single exporter script as a subprocess."""
    print(f"\n{'=' * 70}")
    print(f"[{index}/{total}] {name.upper()}")
    print(f"{'=' * 70}")

    if not script_path.exists():
        utils.log_error(f"Script not found: {script_path.name}")
        return ScriptResult(
            name=name,
            filename=script_path.name,
            success=False,
            duration_seconds=0.0,
            error="Script file not found",
        )

    env = os.environ.copy()
    env['STRATUSSCAN_AUTO_RUN'] = '1'
    env['STRATUSSCAN_REGIONS'] = ','.join(regions)

    pre = _snapshot_xlsx(output_dir)
    start = time.time()

    try:
        result = utils.run_subprocess_with_progress(
            [sys.executable, str(script_path)],
            env=env,
            timeout=1800,
            start_time=start,
        )
        duration = time.time() - start
        success = result.returncode == 0
        output_file = _detect_new_xlsx(output_dir, pre) if success else None

        if success:
            utils.log_success(
                f"{name} completed in {_fmt_duration(duration)}"
            )
        else:
            utils.log_error(
                f"{name} failed (exit code {result.returncode})"
            )

        return ScriptResult(
            name=name,
            filename=script_path.name,
            success=success,
            duration_seconds=duration,
            output_file=output_file,
            error=None if success else f"Exit code {result.returncode}",
        )

    except subprocess.TimeoutExpired:
        duration = time.time() - start
        utils.log_error(f"{name} timed out after 30 minutes")
        return ScriptResult(
            name=name,
            filename=script_path.name,
            success=False,
            duration_seconds=duration,
            error="Timed out (30 min)",
        )

    except Exception as e:
        duration = time.time() - start
        utils.log_error(f"{name} failed with exception", e)
        return ScriptResult(
            name=name,
            filename=script_path.name,
            success=False,
            duration_seconds=duration,
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Zip archive
# ---------------------------------------------------------------------------

def create_zip_archive(
    output_files: list[str],
    account_name: str,
    slug: str,
    output_dir: Path,
) -> Optional[Path]:
    """Zip all successful export files into a single archive."""
    valid = [f for f in output_files if f and Path(f).exists()]
    if not valid:
        utils.log_error("No output files to archive")
        return None

    date = utils.get_export_date()
    zip_name = f"{account_name}-{slug}-all-export-{date}.zip"
    zip_path = output_dir / zip_name

    # Avoid overwriting an existing archive from the same day
    if zip_path.exists():
        v = 2
        while True:
            candidate = output_dir / (
                f"{account_name}-{slug}-all-export-{date}-v{v}.zip"
            )
            if not candidate.exists():
                zip_path = candidate
                break
            v += 1

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in valid:
                p = Path(f)
                zf.write(p, p.name)
                utils.log_info(f"  Archived: {p.name}")

        size_mb = zip_path.stat().st_size / (1024 * 1024)
        utils.log_success(
            f"Archive created: {zip_path.name} "
            f"({size_mb:.1f} MB, {len(valid)} file(s))"
        )
        return zip_path

    except Exception as e:
        utils.log_error("Failed to create zip archive", e)
        return None


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(
    category_name: str,
    results: list[ScriptResult],
    zip_path: Optional[Path],
) -> None:
    """Print a formatted completion summary table."""
    print(f"\n{'=' * 70}")
    print(f"{category_name.upper()} EXPORT — SUMMARY")
    print(f"{'=' * 70}")

    for r in results:
        status = utils.GLYPH_OK if r.success else utils.GLYPH_FAIL
        print(f"  {status} {r.name:<35} {_fmt_duration(r.duration_seconds):>8}")
        if not r.success and r.error:
            print(f"      Error: {r.error}")

    successful = sum(1 for r in results if r.success)
    failed = len(results) - successful

    print(f"{'=' * 70}")
    print(f"  Completed: {successful}/{len(results)}   Failed: {failed}")
    if zip_path:
        print(f"  Archive:   {zip_path.name}")
    elif failed == len(results):
        print("  No archive created — all exports failed")
    print(f"{'=' * 70}")


# ---------------------------------------------------------------------------
# Public entry point — gold-standard 3-step state machine
# ---------------------------------------------------------------------------

def run_bundle(
    category_name: str,
    slug: str,
    scripts: list[tuple[str, str]],
    description: str = "",
    note: Optional[str] = None,
) -> None:
    """
    Run an "All <Category>" bundle export.

    Args:
        category_name: Human-readable category, e.g. "Compute Resources".
        slug: Filename slug for the zip archive, e.g. "compute-resources".
        scripts: Ordered list of (display_name, exporter_filename) tuples.
        description: --help text passed to the argument parser.
        note: Optional advisory (e.g. a GovCloud caveat) shown before the
            interactive selection menu. Suppressed in AUTO_RUN mode.
    """
    utils.parse_script_args(description or f"Export all {category_name} to Excel")
    utils.setup_logging(slug)

    account_id, account_name = utils.print_script_banner(
        f"{category_name.upper()} ALL-IN-ONE EXPORT"
    )

    scripts_dir = utils.get_scripts_dir()
    output_dir = utils.get_output_dir()

    if note and not utils.is_auto_run():
        print(f"\nNote: {note}")

    step = 1
    selected_regions: list[str] = []
    selected_scripts: list[tuple[str, str]] = []

    while True:
        # ── Step 1: Region selection ──────────────────────────────────────
        if step == 1:
            result = utils.prompt_region_selection(category_name)
            if result == 'back':
                sys.exit(10)
            if result == 'exit':
                sys.exit(11)
            selected_regions = result
            step = 2

        # ── Step 2: Script selection ──────────────────────────────────────
        elif step == 2:
            try:
                selected_scripts = prompt_script_selection(category_name, scripts)
            except utils.BackSignal:
                step = 1
                continue
            except (utils.ExitToMainSignal, utils.QuitSignal):
                sys.exit(11)
            step = 3

        # ── Step 3: Confirmation ──────────────────────────────────────────
        elif step == 3:
            script_lines = '\n'.join(
                f"    • {name}" for name, _ in selected_scripts
            )
            region_str = ', '.join(selected_regions)
            zip_preview = f"{account_name}-{slug}-all-export-<date>.zip"
            msg = (
                f"Ready to export {len(selected_scripts)} "
                f"{category_name.lower()} exporter(s):\n"
                f"{script_lines}\n\n"
                f"  Regions : {region_str}\n"
                f"  Output  : {output_dir / zip_preview}"
            )
            result = utils.prompt_confirmation(msg)
            if result == 'back':
                step = 2
                continue
            if result == 'exit':
                sys.exit(11)
            break  # confirmed — proceed to execution

    # ── Execution ─────────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"EXECUTING {len(selected_scripts)} EXPORT SCRIPT(S)")
    print(f"Regions: {', '.join(selected_regions)}")
    print(f"{'=' * 70}")

    results: list[ScriptResult] = []
    total = len(selected_scripts)

    for i, (name, filename) in enumerate(selected_scripts, 1):
        script_path = scripts_dir / filename
        r = run_script(name, script_path, selected_regions, output_dir, i, total)
        results.append(r)

    # ── Archive ───────────────────────────────────────────────────────────
    output_files = [r.output_file for r in results if r.output_file]
    zip_path: Optional[Path] = None

    if output_files:
        print(f"\n{'=' * 70}")
        print("CREATING ARCHIVE")
        print(f"{'=' * 70}")
        zip_path = create_zip_archive(output_files, account_name, slug, output_dir)
    else:
        utils.log_warning(
            "No output files were generated — skipping archive creation"
        )

    # ── Summary ───────────────────────────────────────────────────────────
    print_summary(category_name, results, zip_path)

    # Exit non-zero if every sub-script failed so the orchestrator surfaces it
    if not any(r.success for r in results):
        sys.exit(1)
