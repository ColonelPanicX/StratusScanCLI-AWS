"""
Batch Script Executor

Executes multiple export scripts sequentially with progress tracking,
error handling, and result aggregation.
"""

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import utils
except ImportError:
    sys.path.append(str(Path(__file__).parent.parent))
    import utils


@dataclass
class ExecutionResult:
    """Result of a single script execution."""

    script: str
    success: bool
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    return_code: int
    output_file: Optional[str] = None
    error_message: Optional[str] = None
    # Full stderr content for persistence/diagnostics (console display is truncated)
    full_error_message: Optional[str] = None

    @property
    def duration_formatted(self) -> str:
        """Format duration as human-readable string."""
        seconds = int(self.duration_seconds)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        if hours > 0:
            return f"{hours}h {minutes}m {secs}s"
        if minutes > 0:
            return f"{minutes}m {secs}s"
        return f"{secs}s"


class ScriptExecutor:
    """Executes multiple export scripts with progress tracking."""

    def __init__(
        self,
        scripts: set[str],
        scripts_dir: Optional[str] = None,
        python_executable: str = "python3",
        regions: Optional[list[str]] = None,
        show_output: bool = True,
    ):
        """
        Initialize the executor.

        Args:
            scripts: Set of script filenames to execute
            scripts_dir: Directory containing the scripts (uses utils.get_scripts_dir() if None)
            python_executable: Python interpreter to use
            regions: Regions to pass to subprocesses via STRATUSSCAN_REGIONS.
                     If None, subprocesses use their own configured defaults.
            show_output: When True, stream script stdout to console (prefixed with
                         two spaces). When False, capture silently for a caller
                         that consumes the stream directly.
        """
        self.scripts = sorted(set(scripts))  # Deduplicate and sort for consistent ordering

        # Use utils to get the proper scripts directory
        if scripts_dir is None:
            self.scripts_dir = utils.get_scripts_dir()
        else:
            self.scripts_dir = Path(scripts_dir)

        self.python_executable = python_executable
        self.regions = regions
        self.show_output = show_output
        self.results: list[ExecutionResult] = []
        self.total_scripts = len(self.scripts)
        self.current_index = 0

    def _find_script_path(self, script_name: str) -> Optional[Path]:
        """
        Find the full path to a script.

        Args:
            script_name: Script filename

        Returns:
            Path to script, or None if not found
        """
        # Check in scripts directory
        script_path = self.scripts_dir / script_name
        if script_path.exists():
            return script_path

        # Check if script_name is already a full path
        if Path(script_name).exists():
            return Path(script_name)

        utils.log_warning(f"Script not found: {script_name}")
        return None

    def _execute_script(self, script_path: Path) -> ExecutionResult:
        """
        Execute a single script using Popen with streaming output.

        stdout and stderr are drained concurrently by two threads to prevent
        deadlock on scripts that produce significant output. When show_output
        is True, each stdout line is printed to the console prefixed with two
        spaces so the user can follow progress in real time. stderr lines are
        printed only on failure (after the fact).

        Args:
            script_path: Path to the script to execute

        Returns:
            ExecutionResult with execution details
        """
        script_name = script_path.name
        start_time = datetime.now()

        utils.log_info(f"Executing: {script_name}")

        # Snapshot existing output files before execution to detect new ones afterward
        pre_run_files = self._snapshot_output_files()

        # Build subprocess environment: force non-interactive mode so scripts
        # never block on prompts. STRATUSSCAN_AUTO_RUN bypasses all input()
        # calls; STRATUSSCAN_REGIONS passes the caller's region selection.
        env = os.environ.copy()
        env["STRATUSSCAN_AUTO_RUN"] = "1"
        if self.regions:
            env["STRATUSSCAN_REGIONS"] = ",".join(self.regions)

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def _drain(stream, lines: list[str], show: bool = False) -> None:
            """Drain a stream into lines, optionally printing each line."""
            for line in stream:
                line = line.rstrip("\n")
                lines.append(line)
                if show and line.strip():
                    print(f"  {line}")

        try:
            proc = subprocess.Popen(
                [self.python_executable, str(script_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                env=env,
            )

            # When not streaming output, run a background heartbeat so the user
            # knows a slow script is still working (fires every 10s).
            _hb_stop = threading.Event()
            _hb_thread: Optional[threading.Thread] = None
            if not self.show_output:
                _hb_t0 = time.monotonic()

                def _heartbeat() -> None:
                    while not _hb_stop.wait(timeout=10):
                        print(
                            f"  ... {script_name}: still running "
                            f"({int(time.monotonic() - _hb_t0)}s elapsed)",
                            flush=True,
                        )

                _hb_thread = threading.Thread(target=_heartbeat, daemon=True)
                _hb_thread.start()

            t_out = threading.Thread(
                target=_drain,
                args=(proc.stdout, stdout_lines, self.show_output),
            )
            t_err = threading.Thread(
                target=_drain,
                args=(proc.stderr, stderr_lines, False),
            )
            t_out.start()
            t_err.start()

            # Join with a 30-minute total timeout
            t_out.join(timeout=1800)
            t_err.join(timeout=1800)

            timed_out = t_out.is_alive() or t_err.is_alive()
            if timed_out:
                proc.kill()

            try:
                return_code = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                return_code = -1
                timed_out = True

            # Stop the per-script heartbeat before logging the result
            if _hb_thread is not None:
                _hb_stop.set()
                _hb_thread.join()

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            success = return_code == 0 and not timed_out

            if timed_out:
                utils.log_error(f"✗ {script_name} timed out after 30 minutes", None)
                return ExecutionResult(
                    script=script_name,
                    success=False,
                    start_time=start_time,
                    end_time=end_time,
                    duration_seconds=duration,
                    return_code=-1,
                    error_message="Execution timed out (30 minutes)",
                )

            output_file = self._find_output_file(script_name, pre_run_files)

            # Build error context: prefer stderr, fall back to last 20 stdout lines
            full_error = (
                "\n".join(stderr_lines).strip()
                or "\n".join(stdout_lines[-20:]).strip()
                or "Unknown error"
            )
            error_message: Optional[str] = None
            full_error_message: Optional[str] = None
            if not success:
                full_error_message = full_error
                error_message = (
                    (full_error[:200] + "...") if len(full_error) > 200 else full_error
                )
                # Print stderr to console now that we know the script failed
                if self.show_output and stderr_lines:
                    for line in stderr_lines:
                        if line.strip():
                            print(f"  [stderr] {line}")

            # Format duration inline — avoids constructing a temp ExecutionResult object
            _secs = int(duration)
            _h, _m, _s = _secs // 3600, (_secs % 3600) // 60, _secs % 60
            if _h > 0:
                _dur_str = f"{_h}h {_m}m {_s}s"
            elif _m > 0:
                _dur_str = f"{_m}m {_s}s"
            else:
                _dur_str = f"{_s}s"

            if success:
                utils.log_info(f"✓ {script_name} completed in {_dur_str}")
            else:
                utils.log_error(
                    f"✗ {script_name} failed (code {return_code}): {error_message}", None
                )

            return ExecutionResult(
                script=script_name,
                success=success,
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration,
                return_code=return_code,
                output_file=output_file,
                error_message=error_message,
                full_error_message=full_error_message,
            )

        except Exception as e:
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            utils.log_error(f"✗ {script_name} failed with exception", e)
            return ExecutionResult(
                script=script_name,
                success=False,
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration,
                return_code=-1,
                error_message=str(e),
            )

    def _snapshot_output_files(self) -> tuple:
        """
        Capture the current set of xlsx file paths and the snapshot epoch time.

        Returns:
            Tuple of (set of file path strings, epoch float of snapshot time)
        """
        try:
            output_dir = utils.get_output_dir()
            snapshot_time = time.time()
            return ({str(p) for p in output_dir.glob("*.xlsx")}, snapshot_time)
        except Exception as e:
            utils.log_debug(f"Output snapshot failed: {e}")
            return (set(), 0.0)

    def _find_output_file(self, script_name: str, pre_run_files=None) -> Optional[str]:
        """
        Try to find the output file created by a script.

        Args:
            script_name: Name of the script that was executed
            pre_run_files: Tuple of (set of pre-run xlsx paths, snapshot epoch time),
                           or a plain set for backwards compatibility

        Returns:
            Path to output file if found, None otherwise
        """
        try:
            output_dir = utils.get_output_dir()
            xlsx_files = list(output_dir.glob("*.xlsx"))

            if not xlsx_files:
                return None

            # Unpack snapshot tuple if provided
            if isinstance(pre_run_files, tuple):
                pre_run_set, start_time_epoch = pre_run_files
            elif isinstance(pre_run_files, set):
                pre_run_set = pre_run_files
                start_time_epoch = 0.0
            else:
                pre_run_set = None
                start_time_epoch = 0.0

            if pre_run_set is not None:
                # Prefer files that are both new (not in pre-run set) AND written
                # after the snapshot time to eliminate false matches on same-day files
                new_files = [
                    p for p in xlsx_files
                    if str(p) not in pre_run_set
                    and (start_time_epoch == 0.0 or p.stat().st_mtime >= start_time_epoch)
                ]
                if new_files:
                    new_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    return str(new_files[0])
                # pre_run_set was provided but no new file found — don't attribute
                # a pre-existing file to this script
                return None

            # Fall back to newest file overall (no pre-run snapshot available)
            xlsx_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return str(xlsx_files[0])

        except Exception as e:
            utils.log_debug(f"Output detection failed: {e}")
            return None

    def _show_progress_header(self) -> None:
        """Display progress header."""
        print()
        print("=" * 80)
        print(" " * 28 + "BATCH EXECUTION")
        print("=" * 80)
        print()
        print(f"Total Scripts: {self.total_scripts}")
        print()
        print("=" * 80)
        print()

    def _show_progress(self, script_name: str) -> None:
        """
        Display progress for current script.

        Args:
            script_name: Name of script being executed
        """
        self.current_index += 1
        progress_pct = (self.current_index / self.total_scripts) * 100

        print(f"[{self.current_index}/{self.total_scripts}] ({progress_pct:.0f}%) {script_name}")
        print("-" * 80)

    def _show_execution_summary(self) -> None:
        """Display summary of execution results."""
        print()
        print("=" * 80)
        print(" " * 28 + "EXECUTION SUMMARY")
        print("=" * 80)
        print()

        # Count successes and failures
        successful = [r for r in self.results if r.success]
        failed = [r for r in self.results if not r.success]

        # Calculate total time
        if self.results:
            total_start = min(r.start_time for r in self.results)
            total_end = max(r.end_time for r in self.results)
            total_duration = (total_end - total_start).total_seconds()
            total_minutes = int(total_duration // 60)
            total_seconds = int(total_duration % 60)
        else:
            total_minutes = 0
            total_seconds = 0

        # Overall statistics
        print(f"Total Scripts:     {self.total_scripts}")
        print(f"Successful:        {len(successful)} {utils.GLYPH_OK}")
        print(f"Failed:            {len(failed)} {utils.GLYPH_FAIL}")
        print(f"Success Rate:      {(len(successful)/self.total_scripts*100):.1f}%")
        print(f"Total Time:        {total_minutes}m {total_seconds}s")
        print()

        # Show successful scripts
        if successful:
            print("SUCCESSFUL SCRIPTS:")
            print("-" * 80)
            for result in successful:
                output_info = f" → {result.output_file}" if result.output_file else ""
                print(f"  {utils.GLYPH_OK} {result.script:<45} {result.duration_formatted:>8}{output_info}")
            print()

        # Show failed scripts with error details
        if failed:
            print("FAILED SCRIPTS:")
            print("-" * 80)
            for result in failed:
                print(f"  {utils.GLYPH_FAIL} {result.script:<45} {result.duration_formatted:>8}")
                if result.error_message:
                    # error_message is already truncated to 100 chars for console;
                    # full text is available in result.full_error_message
                    print(f"     Error: {result.error_message}")
            print()

        print("=" * 80)
        print()

    def execute_all(
        self,
        show_progress: bool = True,
        session: Optional[dict] = None,
        skip_scripts: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        """
        Execute all scripts in sequence.

        Args:
            show_progress: Whether to show progress display
            session: Optional scan session dict from utils.start_scan_session().
                     When provided, each completed script is recorded via
                     utils.record_scan_result() and the session is marked
                     complete after all scripts finish.
            skip_scripts: Optional set of script filenames to skip (already
                          completed in a prior session being resumed).

        Returns:
            Dictionary with execution summary:
                - total: Total scripts
                - successful: Number of successful executions
                - failed: Number of failed executions
                - results: List of ExecutionResult objects
                - duration_seconds: Total execution time
        """
        if show_progress:
            self._show_progress_header()

        batch_start = datetime.now()

        for script_name in self.scripts:
            # Skip scripts already completed in a resumed session
            if skip_scripts and script_name in skip_scripts:
                if show_progress:
                    self._show_progress(script_name)
                    print("⏭  Skipped (already completed in prior session)")
                    print()
                continue

            # Find script path
            script_path = self._find_script_path(script_name)

            if script_path is None:
                # Script not found, record as failure
                result = ExecutionResult(
                    script=script_name,
                    success=False,
                    start_time=datetime.now(),
                    end_time=datetime.now(),
                    duration_seconds=0,
                    return_code=-1,
                    error_message="Script file not found",
                )
                self.results.append(result)
                if show_progress:
                    self._show_progress(script_name)
                    print(f"{utils.GLYPH_FAIL} Script not found: {script_name}")
                    print()
                if session is not None:
                    utils.record_scan_result(
                        session, script_name, "failed", -1, 0.0, script=script_name
                    )
                continue

            # Show progress
            if show_progress:
                self._show_progress(script_name)

            # Execute script
            result = self._execute_script(script_path)
            self.results.append(result)

            # Persist result to session file. output_file travels with the record
            # so a resumed session can archive every run's outputs, not just the
            # last one (issue #291).
            if session is not None:
                status_str = "success" if result.success else "failed"
                utils.record_scan_result(
                    session,
                    script_name,
                    status_str,
                    result.return_code,
                    result.duration_seconds,
                    script=script_name,
                    output_file=result.output_file,
                )

            # Brief pause between scripts
            if show_progress:
                print()
                time.sleep(0.5)

        batch_end = datetime.now()
        total_duration = (batch_end - batch_start).total_seconds()

        # Mark session complete
        if session is not None:
            utils.complete_scan_session(session)

        # Show summary
        if show_progress:
            self._show_execution_summary()

        # Return summary
        successful = [r for r in self.results if r.success]
        failed = [r for r in self.results if not r.success]

        return {
            "total": self.total_scripts,
            "successful": len(successful),
            "failed": len(failed),
            "results": self.results,
            "duration_seconds": total_duration,
            "success_rate": (len(successful) / self.total_scripts * 100) if self.total_scripts > 0 else 0,
        }

    def save_execution_log(self, filename: Optional[str] = None) -> bool:
        """
        Save execution results to a log file.

        Args:
            filename: Output filename (auto-generated if None)

        Returns:
            True if saved successfully, False otherwise
        """
        if filename is None:
            timestamp = datetime.now().strftime("%m.%d.%Y-%H%M")
            filename = f"smart-scan-execution-log-{timestamp}.txt"

        try:
            log_path = utils.get_output_filepath(filename)
            with open(log_path, "w", encoding='utf-8') as f:
                f.write("=" * 80 + "\n")
                f.write(" " * 26 + "SMART SCAN EXECUTION LOG\n")
                f.write("=" * 80 + "\n\n")

                # Summary
                successful = [r for r in self.results if r.success]
                failed = [r for r in self.results if not r.success]

                f.write(f"Execution Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Total Scripts: {self.total_scripts}\n")
                f.write(f"Successful: {len(successful)}\n")
                f.write(f"Failed: {len(failed)}\n")
                f.write(f"Success Rate: {(len(successful)/self.total_scripts*100):.1f}%\n\n")
                f.write("=" * 80 + "\n\n")

                # Detailed results
                f.write("EXECUTION DETAILS:\n")
                f.write("-" * 80 + "\n\n")

                for result in self.results:
                    status = "SUCCESS" if result.success else "FAILED"
                    f.write(f"Script: {result.script}\n")
                    f.write(f"Status: {status}\n")
                    f.write(f"Duration: {result.duration_formatted}\n")
                    f.write(f"Start Time: {result.start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"End Time: {result.end_time.strftime('%Y-%m-%d %H:%M:%S')}\n")

                    if result.output_file:
                        f.write(f"Output File: {result.output_file}\n")

                    if result.error_message:
                        f.write(f"Error: {result.error_message}\n")

                    f.write("\n" + "-" * 80 + "\n\n")

                f.write("=" * 80 + "\n")

            utils.log_info(f"Execution log saved to: {log_path}")
            return True

        except Exception as e:
            utils.log_error(f"Error saving execution log to {filename}", e)
            return False


def execute_scripts(
    scripts: set[str],
    show_progress: bool = True,
    save_log: bool = False,
    regions: Optional[list[str]] = None,
    show_output: bool = True,
    session: Optional[dict] = None,
    skip_scripts: Optional[set[str]] = None,
) -> dict[str, Any]:
    """
    Execute multiple scripts in batch.

    Args:
        scripts: Set of script filenames to execute
        show_progress: Whether to show progress display
        save_log: Whether to save execution log to file
        regions: Regions to pass to subprocesses via STRATUSSCAN_REGIONS
        show_output: When True, stream script stdout to console in real time.
                     When False, capture silently for a caller that consumes it.
        session: Optional scan session dict from utils.start_scan_session().
                 When provided, results are persisted to disk after each script.
        skip_scripts: Optional set of script filenames to skip (already
                      completed in a resumed session).

    Returns:
        Execution summary dictionary
    """
    executor = ScriptExecutor(scripts, regions=regions, show_output=show_output)
    summary = executor.execute_all(
        show_progress=show_progress,
        session=session,
        skip_scripts=skip_scripts,
    )

    if save_log:
        executor.save_execution_log()

    return summary
