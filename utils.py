#!/usr/bin/env python3
"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: StratusScan Utilities Module
Version: v0.1.0
Date: NOV-15-2025

Description:
Shared utility functions for StratusScan scripts with multi-partition support.
Works seamlessly in both AWS Commercial and AWS GovCloud environments with
automatic partition detection. This module provides common functionality such as
path handling, file operations, standardized output formatting, account mapping,
region and partition handling, and cross-partition resource management.

Features:
- Multi-partition support (AWS Commercial & GovCloud)
- Automatic partition detection from credentials
- Partition-aware region selection and ARN building
- Service availability validation by partition
- Full service availability including Trusted Advisor (Commercial)
- Zero-configuration cross-environment compatibility
- Phase 4B Performance Optimization (concurrent region scanning, session-level caching)
"""

import datetime
import importlib
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from functools import wraps
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar, Union

import boto3
import botocore
from botocore.client import BaseClient
from botocore.config import Config

# openpyxl is imported lazily inside _adjust_column_widths to avoid a hard
# import failure when the package is not installed (e.g. fresh clone before
# running pip install).  stratusscan.py only needs utils for the menu; Excel
# output is only needed when exporters actually write files.

# Global logger instance
logger = None
# Tracks whether setup_logging() has been explicitly called
_logging_configured = False

# ---------------------------------------------------------------------------
# Script args store — populated by parse_script_args() at script start
# ---------------------------------------------------------------------------

_SCRIPT_ARGS: Optional["argparse.Namespace"] = None


def get_script_args() -> Optional["argparse.Namespace"]:
    """Return the parsed script args namespace, or None if not yet parsed."""
    return _SCRIPT_ARGS


# ---------------------------------------------------------------------------
# Navigation signals — raised by prompt_menu() for b / x input
# ---------------------------------------------------------------------------

class BackSignal(Exception):
    """Raised when the user enters 'b' to return to the parent menu."""


class ExitToMainSignal(Exception):
    """Raised when the user enters 'x' to exit directly to the main menu."""


class QuitSignal(Exception):
    """Raised when the user enters 'q' to quit."""


# ---------------------------------------------------------------------------
# Interaction constants — the single-voice standard for every interactive prompt
# ---------------------------------------------------------------------------

#: Status glyphs. Green check / red x is the tool-wide standard — use these
#: instead of ✓/✗/ERROR: so status output reads the same everywhere.
GLYPH_OK = "✅"
GLYPH_FAIL = "❌"

#: The single invalid-input message for every menu / selection prompt.
MSG_INVALID_SELECTION = "Invalid selection. Please try again."


def _nav_footer(
    allow_back: bool = True,
    allow_exit: bool = True,
    allow_quit: bool = True,
) -> str:
    """
    Build the canonical navigation footer shown beneath interactive menus.

    Returns a single line like ``  b = back  |  x = main menu  |  q = quit``,
    including only the keys that are enabled. The wording and punctuation here
    are the single-voice standard — every menu footer routes through this.
    """
    parts = []
    if allow_back:
        parts.append("b = back")
    if allow_exit:
        parts.append("x = main menu")
    if allow_quit:
        parts.append("q = quit")
    return "  " + "  |  ".join(parts)


def get_version() -> str:
    """Return the installed package version, or 'dev' if not installed."""
    try:
        return _pkg_version("stratusscancli-aws")
    except PackageNotFoundError:
        return "dev"


def _cleanup_old_logs(logs_dir: Path, log_retention_days: int = 14) -> None:
    """
    Remove log files older than log_retention_days from the logs directory.

    Args:
        logs_dir: Path to the logs directory
        log_retention_days: Number of days to retain log files (default: 14)
    """
    try:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=log_retention_days)
        cutoff_timestamp = cutoff.timestamp()
        removed = 0
        for log_file in logs_dir.glob("*.log"):
            try:
                if log_file.stat().st_mtime < cutoff_timestamp:
                    log_file.unlink()
                    removed += 1
            except Exception:
                pass  # Skip files we cannot stat or remove
        if removed:
            logging.getLogger('stratusscan').debug(
                f"Cleaned up {removed} log file(s) older than {log_retention_days} days"
            )
    except Exception:
        pass  # Log cleanup is best-effort; never raise


def setup_logging(script_name: str = "stratusscan", log_to_file: bool = True) -> logging.Logger:
    """
    Setup comprehensive logging for StratusScan with both console and file output.

    Args:
        script_name (str): Name of the script for log file naming
        log_to_file (bool): Whether to log to file in addition to console

    Returns:
        logging.Logger: Configured logger instance
    """
    global logger, _logging_configured

    # Create logger
    logger = logging.getLogger('stratusscan')
    logger.setLevel(logging.DEBUG)

    # Clear any existing handlers
    logger.handlers = []

    # Create formatters
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Console handler — WARNING+ only; INFO goes to the log file exclusively
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File handler (if enabled)
    if log_to_file:
        try:
            # Create logs directory if it doesn't exist
            logs_dir = Path(__file__).parent / "logs"
            logs_dir.mkdir(exist_ok=True)

            # Remove stale log files before creating the new one
            _cleanup_old_logs(logs_dir)

            # Generate timestamp for log filename: MM.DD.YYYY-HHMM
            timestamp = datetime.datetime.now().strftime("%m.%d.%Y-%H%M")
            log_filename = f"logs-{script_name}-{timestamp}.log"
            log_filepath = logs_dir / log_filename

            # File handler
            file_handler = logging.FileHandler(log_filepath, mode='w', encoding='utf-8')
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)

            # Log the initialization
            logger.info(_scrub_log(f"StratusScan logging initialized - Log file: {log_filepath}"))
            logger.info(_scrub_log(f"Script: {script_name}"))
            logger.info(f"Timestamp: {timestamp}")
            logger.info("=" * 80)

        except Exception as e:
            # If file logging fails, continue with console only
            logger.error(f"Failed to setup file logging: {e}")
            logger.warning("Continuing with console logging only")

    _logging_configured = True
    return logger

def get_logger() -> logging.Logger:
    """
    Get the current logger instance, creating one if it doesn't exist.
    If setup_logging() has not yet been called, returns a logger with a
    NullHandler so that library usage does not emit spurious output.

    Returns:
        logging.Logger: Logger instance
    """
    global logger, _logging_configured
    if logger is None:
        if _logging_configured:
            # setup_logging() was called but logger was somehow cleared — reinitialise
            logger = setup_logging()
        else:
            # setup_logging() has not been called yet; return a silent logger
            # so importing utils as a library doesn't emit unexpected output
            _null_logger = logging.getLogger('stratusscan')
            if not _null_logger.handlers:
                _null_logger.addHandler(logging.NullHandler())
            return _null_logger
    return logger

# Do NOT call setup_logging() or get_logger() at module import time.
# Scripts must call utils.setup_logging() explicitly to activate logging.
# This prevents side effects (file creation, console output) on import.

# AWS Commercial constants
DEFAULT_REGIONS = ['us-east-1', 'us-west-2', 'us-west-1', 'eu-west-1']
AWS_PARTITION = 'aws'

def prompt_menu(
    title: str,
    options: list[str],
    allow_back: bool = True,
    allow_exit: bool = True,
    allow_quit: bool = True,
) -> int:
    """
    Display a bordered numbered menu and return the user's choice.

    Navigation follows the single-voice standard: ``b`` = back, ``x`` = main
    menu, ``q`` = quit — surfaced as BackSignal / ExitToMainSignal / QuitSignal.

    Args:
        title: Menu title displayed above the border
        options: List of option strings (displayed as 1..N)
        allow_back: If True, show and accept 'b' to go back (default: True)
        allow_exit: If True, show and accept 'x' for the main menu (default: True)
        allow_quit: If True, show and accept 'q' to quit (default: True)

    Returns:
        int 1..N if the user picks a numbered option.

    Raises:
        BackSignal: if the user enters 'b' (and allow_back is True).
        ExitToMainSignal: if the user enters 'x' (and allow_exit is True).
        QuitSignal: if the user enters 'q' (and allow_quit is True) or
            presses Ctrl-C.
    """
    if is_auto_run():
        return 1

    print(f"\n{title}")
    print("=" * 64)
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    print("-" * 64)
    if allow_back or allow_exit or allow_quit:
        print(_nav_footer(allow_back, allow_exit, allow_quit))
    print("=" * 64)

    valid = {str(i) for i in range(1, len(options) + 1)}
    if allow_back:
        valid.add("b")
    if allow_exit:
        valid.add("x")
    if allow_quit:
        valid.add("q")

    while True:
        try:
            choice = input("Enter your choice: ").strip().lower()
        except KeyboardInterrupt:
            print()
            if allow_quit:
                raise QuitSignal from None
            continue

        if choice in valid:
            if choice == 'b':
                raise BackSignal
            if choice == 'x':
                raise ExitToMainSignal
            if choice == 'q':
                raise QuitSignal
            return int(choice)
        print(MSG_INVALID_SELECTION)


def prompt_multiselect(
    title: str,
    options: list[str],
    all_label: Optional[str] = None,
    allow_back: bool = True,
    allow_exit: bool = True,
    allow_quit: bool = True,
) -> list[int]:
    """
    Present a numbered multi-select menu and return the chosen option indices.

    Numbers are entered space-separated (e.g. ``1 3 5``). When *all_label* is
    provided it is shown as the first numbered row; selecting it returns every
    option — this replaces the old magic ``0 = All`` convention so every row is
    a real, consistently-numbered choice. Navigation follows the single-voice
    standard: ``b``/``x``/``q`` surface as BackSignal / ExitToMainSignal /
    QuitSignal.

    Args:
        title: Menu title displayed above the border.
        options: Selectable option labels (shown after the optional All row).
        all_label: If given, adds a leading "select all" row with this label.
        allow_back / allow_exit / allow_quit: Which navigation keys to offer.

    Returns:
        list[int]: 1-based indices into *options* (never empty).

    Raises:
        BackSignal / ExitToMainSignal / QuitSignal per the standard.
    """
    if is_auto_run():
        return list(range(1, len(options) + 1))

    has_all = all_label is not None
    while True:
        print(f"\n{title}")
        print("=" * 64)
        rows = ([all_label] if has_all else []) + list(options)
        for i, row in enumerate(rows, 1):
            print(f"  {i:2d}. {row}")
        print("-" * 64)
        if allow_back or allow_exit or allow_quit:
            print(_nav_footer(allow_back, allow_exit, allow_quit))
        print("=" * 64)

        try:
            raw = input(
                "Enter number(s) separated by spaces (e.g. 1  or  1 3 5): "
            ).strip().lower()
        except KeyboardInterrupt:
            print()
            if allow_quit:
                raise QuitSignal from None
            continue

        if allow_back and raw == 'b':
            raise BackSignal
        if allow_exit and raw == 'x':
            raise ExitToMainSignal
        if allow_quit and raw == 'q':
            raise QuitSignal

        tokens = raw.split()
        if not tokens or not all(t.isdigit() for t in tokens):
            print(MSG_INVALID_SELECTION)
            continue

        picks = [int(t) for t in tokens]
        if any(p < 1 or p > len(rows) for p in picks):
            print(MSG_INVALID_SELECTION)
            continue

        # The "All" row (row 1, when present) short-circuits to every option.
        if has_all and 1 in picks:
            return list(range(1, len(options) + 1))

        # De-dupe, preserve order, and offset past the All row.
        offset = 1 if has_all else 0
        seen: set = set()
        result: list[int] = []
        for p in picks:
            option_index = p - offset
            if option_index >= 1 and option_index not in seen:
                seen.add(option_index)
                result.append(option_index)
        if result:
            return result
        print(MSG_INVALID_SELECTION)


def prompt_region_selection(
    service_name: Optional[str] = None,
) -> Union[list[str], str]:
    """
    Prompt user for AWS region selection with a standardized 3-option menu.

    Args:
        service_name: Optional name of the AWS service (e.g. "EC2", "Lambda").
                      Displayed as context before the menu.

    Returns:
        List[str] of selected region names, 'back', or 'exit'.
        Never calls sys.exit() directly.
    """
    # Automation mode: bypass interactive prompts when STRATUSSCAN_AUTO_RUN is set
    if is_auto_run():
        auto_regions = get_auto_regions()
        if auto_regions:
            return auto_regions
        _partition = detect_partition()
        return get_partition_regions(_partition, all_regions=True)

    # CLI flag override — takes precedence over interactive prompts
    if _SCRIPT_ARGS is not None:
        if _SCRIPT_ARGS.region:
            return [_SCRIPT_ARGS.region]
        if _SCRIPT_ARGS.regions:
            return [r.strip() for r in _SCRIPT_ARGS.regions.split(",") if r.strip()]
        if _SCRIPT_ARGS.all_regions:
            _partition = detect_partition()
            return get_partition_regions(_partition, all_regions=True)

    partition = detect_partition()
    default_regions = get_default_regions()
    default_str = ", ".join(default_regions[:4])
    if len(default_regions) > 4:
        default_str += ", ..."

    if service_name:
        print(f"\n{service_name} region selection")

    options = [
        f"Default Regions    ({default_str})",
        "All Regions        (scan every region in the partition)",
        "Select Regions     (choose one or more from a list)",
    ]

    while True:
        try:
            choice = prompt_menu("REGION SELECTION", options)
        except BackSignal:
            return 'back'
        except (ExitToMainSignal, QuitSignal):
            return 'exit'

        if choice == 1:
            return default_regions

        if choice == 2:
            all_regions = get_partition_regions(partition, all_regions=True)
            print(f"\nScanning all {len(all_regions)} available regions.")
            return all_regions

        if choice == 3:
            # Sub-list: let user pick one or more regions by number
            available = get_partition_regions(partition, all_regions=True)
            while True:
                print("\nAVAILABLE REGIONS")
                print("=" * 64)
                # 2-column layout when > 8 regions
                if len(available) > 8:
                    half = (len(available) + 1) // 2
                    for i in range(half):
                        left = f"  {i + 1:2d}. {available[i]}"
                        if i + half < len(available):
                            right = f"  {i + half + 1:2d}. {available[i + half]}"
                            print(f"{left:<34}{right}")
                        else:
                            print(left)
                else:
                    for i, r in enumerate(available, 1):
                        print(f"  {i:2d}. {r}")
                print("=" * 64)
                print(_nav_footer())
                print("=" * 64)

                try:
                    raw = input(
                        "Enter region number(s) separated by spaces (e.g. 1  or  1 4 7): "
                    ).strip().lower()
                except KeyboardInterrupt:
                    print()
                    return 'exit'

                if raw == 'b':
                    break  # back to main region menu
                if raw in ('x', 'q'):
                    return 'exit'

                tokens = raw.split()
                valid = True
                selected = []
                for tok in tokens:
                    try:
                        idx = int(tok)
                        if 1 <= idx <= len(available):
                            selected.append(available[idx - 1])
                        else:
                            print(
                                f"Invalid number {tok}. "
                                f"Please enter values between 1 and {len(available)}."
                            )
                            valid = False
                            break
                    except ValueError:
                        print(f"Invalid input '{tok}'. Please enter numbers only.")
                        valid = False
                        break

                if valid and selected:
                    return selected
                if valid and not selected:
                    print("No regions selected. Please enter at least one number.")

def get_organization_name() -> str:
    """
    Get the organization name from configuration.

    Returns:
        str: Organization name or default
    """
    _, cfg = get_config()
    return cfg.get('organization_name', 'YOUR-ORGANIZATION')

def get_aws_environment() -> str:
    """
    Get the AWS environment type from configuration.

    Returns:
        str: Environment type (e.g., 'production', 'staging') or default
    """
    _, cfg = get_config()
    return cfg.get('aws_environment', 'production')

def _scrub_log(value: object) -> str:
    """
    Sanitize a value for safe logging (CWE-117 / log-forging prevention).

    Collapses carriage returns and line feeds to spaces so untrusted data
    (e.g. an invalid account ID or ARN) cannot inject forged log lines.
    Messages without CR/LF are returned unchanged.
    """
    return str(value).replace("\r", " ").replace("\n", " ")

def log_error(error_message: str, error_obj: Optional[Exception] = None) -> None:
    """
    Log an error message to both console and file.

    Args:
        error_message: The error message to display
        error_obj: Optional exception object
    """
    current_logger = get_logger()
    if error_obj:
        current_logger.error(_scrub_log(f"{error_message}: {str(error_obj)}"))
        # Log stack trace for debugging
        current_logger.debug(_scrub_log(f"Exception details: {error_obj}"), exc_info=True)
    else:
        current_logger.error(_scrub_log(error_message))

def log_warning(warning_message: str) -> None:
    """
    Log a warning message to both console and file.

    Args:
        warning_message: The warning message to display
    """
    current_logger = get_logger()
    current_logger.warning(_scrub_log(warning_message))

def log_info(info_message: str) -> None:
    """
    Log an informational message to both console and file.

    Args:
        info_message: The information message to display
    """
    current_logger = get_logger()
    current_logger.info(_scrub_log(info_message))

def log_debug(debug_message: str) -> None:
    """
    Log a debug message (file only, not console).

    Args:
        debug_message: The debug message to log
    """
    current_logger = get_logger()
    current_logger.debug(_scrub_log(debug_message))

def log_success(success_message: str) -> None:
    """
    Log a success message to both console and file.

    Args:
        success_message: The success message to display
    """
    current_logger = get_logger()
    current_logger.info(_scrub_log(f"SUCCESS: {success_message}"))
    print(f"[✓] {success_message}", flush=True)

def log_aws_info(message: str) -> None:
    """
    Log AWS-specific informational message to both console and file.

    Args:
        message: The AWS-specific message to display
    """
    current_logger = get_logger()
    current_logger.info(f"AWS: {message}")

def log_partition_info(partition: str, regions: list[str]) -> None:
    """
    Log AWS partition information for user awareness.

    Args:
        partition: AWS partition ('aws' or 'aws-us-gov')
        regions: List of regions being used
    """
    current_logger = get_logger()
    partition_name = "AWS GovCloud" if partition == 'aws-us-gov' else "AWS Commercial"
    current_logger.info(_scrub_log(f"AWS PARTITION: {partition_name} ({partition})"))
    current_logger.info(f"REGIONS: {', '.join(regions)}")

    if partition == 'aws-us-gov':
        current_logger.info("NOTE: GovCloud has different service availability - some features may not be available")

def log_script_start(script_name: str, description: str = "") -> None:
    """
    Log the start of a script execution with standardized format.

    Args:
        script_name: Name of the script being executed
        description: Optional description of the script's purpose
    """
    current_logger = get_logger()
    current_logger.info("=" * 80)
    current_logger.info(_scrub_log(f"SCRIPT START: {script_name}"))
    if description:
        current_logger.info(f"DESCRIPTION: {description}")
    current_logger.info(f"START TIME: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    current_logger.info("=" * 80)

def log_script_end(script_name: str, start_time: Optional[datetime.datetime] = None) -> None:
    """
    Log the end of a script execution with standardized format.

    Args:
        script_name: Name of the script that was executed
        start_time: Optional start time to calculate duration
    """
    current_logger = get_logger()
    end_time = datetime.datetime.now()

    current_logger.info("=" * 80)
    current_logger.info(_scrub_log(f"SCRIPT END: {script_name}"))
    current_logger.info(f"END TIME: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")

    if start_time:
        duration = end_time - start_time
        current_logger.info(f"DURATION: {duration}")

    current_logger.info("=" * 80)

def log_section(section_name: str) -> None:
    """
    Log a section header for better log organization.

    Args:
        section_name: Name of the section
    """
    current_logger = get_logger()
    current_logger.info("-" * 50)
    current_logger.info(_scrub_log(f"SECTION: {section_name}"))
    current_logger.info("-" * 50)

def log_aws_operation(operation_name: str, service: str, region: Optional[str] = None, details: str = "") -> None:
    """
    Log AWS API operations for audit trail.

    Args:
        operation_name: Name of the AWS operation (e.g., describe_instances)
        service: AWS service name (e.g., EC2)
        region: AWS region (optional)
        details: Additional details about the operation
    """
    current_logger = get_logger()
    region_info = f" in {region}" if region else ""
    details_info = f" - {details}" if details else ""
    current_logger.info(f"AWS API: {service}.{operation_name}{region_info}{details_info}")

def log_export_summary(resource_type: str, count: int, output_file: str) -> None:
    """
    Log export operation summary.

    Args:
        resource_type: Type of resource exported
        count: Number of resources exported
        output_file: Path to output file
    """
    current_logger = get_logger()
    current_logger.info(f"EXPORT SUMMARY: {resource_type}")
    current_logger.info(f"  Resources exported: {count}")
    current_logger.info(f"  Output file: {output_file}")

def log_system_info() -> None:
    """
    Log system information for debugging purposes.
    """
    current_logger = get_logger()
    current_logger.info("SYSTEM INFORMATION:")
    current_logger.info(f"  Platform: {platform.system()} {platform.release()}")
    current_logger.info(f"  Python version: {sys.version}")
    current_logger.info(f"  Working directory: {os.getcwd()}")
    current_logger.info(f"  Script location: {Path(__file__).parent}")

def log_menu_selection(menu_path: str, selection_name: str) -> None:
    """
    Log menu selections for user activity tracking.

    Args:
        menu_path: Path through menu (e.g., "4.2.1")
        selection_name: Name of the selected option
    """
    current_logger = get_logger()
    current_logger.info(_scrub_log(f"MENU SELECTION: {menu_path} - {selection_name}"))

def get_current_log_file() -> Optional[str]:
    """
    Get the path to the current log file if file logging is enabled.

    Returns:
        str: Path to current log file or None if not file logging
    """
    current_logger = get_logger()
    for handler in current_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            return handler.baseFilename
    return None

def prompt_confirmation(message: str) -> str:
    """
    Display a confirmation prompt and return the user's navigation choice.

    Args:
        message: Confirmation message to display before the prompt.

    Returns:
        'confirm' if the user presses Enter,
        'back' if the user enters 'b',
        'exit' if the user enters 'x'.
    """
    if is_auto_run():
        return 'confirm'

    print(f"\n{message}")
    while True:
        try:
            raw = input("Press Enter to confirm, b to go back, x to exit: ").strip().lower()
        except KeyboardInterrupt:
            print()
            return 'exit'

        if raw == '':
            return 'confirm'
        if raw == 'b':
            return 'back'
        if raw == 'x':
            return 'exit'
        print("Please press Enter, b, or x.")


def prompt_for_confirmation(message: str = "Do you want to continue?", default: bool = True) -> bool:
    """
    Prompt the user for confirmation.

    Args:
        message: Message to display
        default: Default response if user just presses Enter

    Returns:
        bool: True if confirmed, False otherwise
    """
    if is_auto_run():
        return default

    # CLI flag override — --yes / -y skips confirmation
    if _SCRIPT_ARGS is not None and _SCRIPT_ARGS.yes:
        return True

    # TODO: Issue #C — add b/x navigation support here
    default_prompt = " (Y/n): " if default else " (y/N): "
    response = input(f"{message}{default_prompt}").strip().lower()

    if not response:
        return default

    return response.lower() in ['y', 'yes']

def format_bytes(size_bytes: Union[int, float]) -> str:
    """
    Format bytes to human-readable format.

    Args:
        size_bytes: Size in bytes

    Returns:
        str: Formatted size string (e.g., "1.23 GB")
    """
    if size_bytes == 0:
        return "0 B"

    size_names = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = 0

    while size_bytes >= 1024 and i < len(size_names) - 1:
        size_bytes /= 1024.0
        i += 1

    return f"{size_bytes:.2f} {size_names[i]}"

def get_log_timestamp() -> str:
    """
    Get current timestamp in ISO-style format for log messages.

    Returns:
        str: Timestamp string in ``YYYY-MM-DD HH:MM:SS`` format
    """
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_export_date() -> str:
    """
    Get current date in the StratusScan export-filename format.

    Returns:
        str: Date string in ``MM.DD.YYYY`` format
    """
    return datetime.datetime.now().strftime("%m.%d.%Y")


def get_current_timestamp() -> str:
    """
    Get current timestamp in a standardized format.

    .. deprecated::
        Use :func:`get_log_timestamp` (ISO format) or :func:`get_export_date`
        (``MM.DD.YYYY`` for filenames) instead.

    Returns:
        str: Formatted timestamp
    """
    warnings.warn(
        "get_current_timestamp() is deprecated; use get_log_timestamp() or get_export_date().",
        DeprecationWarning,
        stacklevel=2,
    )
    return get_log_timestamp()

def resource_list_to_dataframe(resource_list: list[dict[str, Any]], columns: Optional[list[str]] = None) -> Any:
    """
    Convert a list of dictionaries to a pandas DataFrame with specific columns.

    Args:
        resource_list: List of resource dictionaries
        columns: Optional list of columns to include

    Returns:
        DataFrame: pandas DataFrame
    """
    import pandas as pd

    if not resource_list:
        return pd.DataFrame()

    df = pd.DataFrame(resource_list)

    if columns:
        # Keep only specified columns that exist in the DataFrame
        existing_columns = [col for col in columns if col in df.columns]
        df = df[existing_columns]

    return df

def get_stratusscan_root() -> Path:
    """
    Get the root directory of the StratusScan package.

    If the script using this function is in the scripts/ directory,
    this will return the parent directory. If the script is in the
    root directory, this will return that directory.

    Returns:
        Path: Path to the StratusScan root directory
    """
    # Anchor to this file (utils.py) rather than sys.argv[0] so the path is
    # correct regardless of how Python was invoked (e.g. pytest, subprocess, etc.)
    calling_script = Path(__file__).absolute()
    script_dir = calling_script.parent

    # Check if we're in a 'scripts' subdirectory
    if script_dir.name.lower() == 'scripts':
        # Return the parent (StratusScan root)
        return script_dir.parent
    else:
        # Assume we're already at the root
        return script_dir

def get_scripts_dir() -> Path:
    """
    Get the path to the scripts directory.

    Returns:
        Path: Path to the scripts directory
    """
    # Get StratusScan root directory
    root_dir = get_stratusscan_root()

    # Define the scripts directory path
    scripts_dir = root_dir / "scripts"

    return scripts_dir


def get_output_dir() -> Path:
    """
    Get the path to the output directory and create it if it doesn't exist.

    Resolution order:
      1. ``_SCRIPT_ARGS.output_dir`` set by ``parse_script_args()`` (absolute
         path is used as-is; relative path is resolved from CWD)
      2. ``output/`` subdirectory of the StratusScan project root

    Returns:
        Path: Path to the output directory (guaranteed to exist)
    """
    if _SCRIPT_ARGS is not None and _SCRIPT_ARGS.output_dir != "output":
        output_dir = Path(_SCRIPT_ARGS.output_dir)
        if not output_dir.is_absolute():
            output_dir = Path.cwd() / output_dir
    else:
        root_dir = get_stratusscan_root()
        output_dir = root_dir / "output"

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir

# Characters that can redirect a path or corrupt a filename: path separators,
# Windows-reserved punctuation, and control characters (including NUL).
_UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def sanitize_filename_component(value: object) -> str:
    """
    Reduce an untrusted value to a safe single filename component (CWE-73).

    Strips — rather than substitutes — path separators, control characters, and
    parent-directory markers, so ordinary names (letters, digits, spaces, dots,
    hyphens, underscores) are returned byte-identical and existing export
    filenames are unaffected.

    Args:
        value: The untrusted value (e.g. an account name from config.json)

    Returns:
        str: The value with path-altering characters removed
    """
    text = _UNSAFE_NAME_CHARS.sub("", str(value))
    # Remove parent-directory markers, repeating until stable so that crafted
    # sequences like '....' cannot reconstitute a '..' after a single pass.
    while ".." in text:
        text = text.replace("..", "")
    # A leading dot would hide the file; a trailing dot/space is stripped by
    # some filesystems and would let two distinct names collide.
    return text.strip(" .")

def contained_path(base_dir: "str | Path", filename: str) -> Path:
    """
    Resolve ``filename`` to a direct child of ``base_dir``, refusing escapes.

    Containment (CWE-73): reduce to a bare filename and confirm it resolves to a
    direct child of the base directory, so a crafted name containing path
    separators or '..' cannot escape it. Ordinary bare filenames pass through
    unchanged.

    Args:
        base_dir: The directory the file must live directly inside
        filename: The candidate filename

    Returns:
        Path: ``base_dir`` joined with the validated bare filename

    Raises:
        ValueError: If the name would resolve outside ``base_dir``.
    """
    base = Path(base_dir).resolve()
    safe_name = os.path.basename(str(filename))
    resolved = (base / safe_name).resolve()
    if safe_name in ("", ".", "..") or resolved.parent != base:
        raise ValueError(f"Refusing path outside {base}: {filename!r}")
    return base / safe_name

def get_output_filepath(filename: str) -> Path:
    """
    Get the full path for a file in the output directory.

    Args:
        filename: The name of the file

    Returns:
        Path: Full path to the file in the output directory

    Raises:
        ValueError: If the name would resolve outside the output directory.
    """
    return contained_path(get_output_dir(), filename)

def create_export_filename(
    account_name: str,
    resource_type: str,
    suffix: str = "",
    current_date: Optional[str] = None,
    fmt: Optional[str] = None
) -> str:
    """
    Create a standardized filename for exported data.

    Args:
        account_name: AWS account name
        resource_type: Type of resource being exported (e.g., "ec2", "vpc")
        suffix: Optional suffix for the filename (e.g., "running", "all")
        current_date: Date to use in the filename (defaults to today)
        fmt: Export format override ('xlsx' or 'csv'). Reads from config if None.

    Returns:
        str: Standardized filename with path
    """
    # Resolve format: explicit arg > config > default
    if fmt is None:
        fmt = config_value("format", default="xlsx", section="output_settings")

    ext = f".{fmt}"

    # Get current date if not provided
    if not current_date:
        current_date = datetime.datetime.now().strftime("%m.%d.%Y")

    # Sanitize caller-supplied components (CWE-73): account_name comes from the
    # account_mappings in config.json, so a crafted entry must not be able to
    # steer the export out of the output directory. Ordinary names are unchanged.
    account_name = sanitize_filename_component(account_name)
    resource_type = sanitize_filename_component(resource_type)
    suffix = sanitize_filename_component(suffix)

    # Build the base filename
    if suffix:
        base_filename = f"{account_name}-{resource_type}-{suffix}-export-{current_date}{ext}"
    else:
        base_filename = f"{account_name}-{resource_type}-export-{current_date}{ext}"

    # Same-day overwrite protection: append -v2, -v3, etc. until the name is unique
    output_dir = get_output_dir()
    candidate = base_filename
    version = 2
    while (output_dir / candidate).exists():
        stem = base_filename[: -len(ext)]
        candidate = f"{stem}-v{version}{ext}"
        version += 1

    return candidate

def _adjust_column_widths(worksheet, df) -> None:
    """Set Excel column widths to fit content (max 50 chars)."""
    from openpyxl.utils import get_column_letter
    for i, column in enumerate(df.columns):
        column_width = max(df[column].astype(str).map(len).max(), len(column)) + 2
        column_width = min(column_width, 50)
        worksheet.column_dimensions[get_column_letter(i + 1)].width = column_width


def save_dataframe_to_excel(df, filename: str, sheet_name: str = "Data", auto_adjust_columns: bool = True, prepare: bool = False) -> Optional[str]:
    """
    Save a pandas DataFrame to an Excel file (or CSV) in the output directory.

    The output format is determined by the 'format' key in the 'output_settings'
    config section ('xlsx' or 'csv'). Defaults to 'xlsx'.

    Args:
        df: pandas DataFrame to save
        filename: Name of the file to save
        sheet_name: Name of the sheet in Excel (ignored for CSV)
        auto_adjust_columns: Whether to auto-adjust column widths (xlsx only)
        prepare: If True, apply prepare_dataframe_for_export() before saving (default: False)

    Returns:
        str: Full path to the saved file, or None on error
    """
    try:
        # Import pandas here to avoid dependency issues
        import pandas as pd

        # Resolve output format from config
        fmt = config_value("format", default="xlsx", section="output_settings")

        # Prepare DataFrame if requested
        if prepare:
            df = prepare_dataframe_for_export(df)

        # Empty-export suppression + manifest accounting (applies to both formats)
        rows = int(len(df))
        skipped = _maybe_skip_empty(filename, rows)
        if skipped is not None:
            return skipped

        if fmt == "csv":
            # Normalise filename: strip .xlsx if caller passed it, force .csv
            if filename.endswith(".xlsx"):
                filename = filename[:-5] + ".csv"
            elif not filename.endswith(".csv"):
                filename = filename + ".csv"

            output_path = get_output_filepath(filename)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            df.to_csv(output_path, index=False)
            logger.info(_scrub_log(f"Data successfully exported to: {output_path}"))
            return _finalize_export(str(output_path), rows)

        # --- xlsx path ---
        # Ensure the output directory exists
        output_path = get_output_filepath(filename)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Save to Excel
        if auto_adjust_columns:
            # Create Excel writer using context manager to ensure proper close/save
            with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
                # Write DataFrame to Excel
                df.to_excel(writer, sheet_name=sheet_name, index=False)

                # Auto-adjust column widths (skip if DataFrame is empty)
                if not df.empty:
                    _adjust_column_widths(writer.sheets[sheet_name], df)
        else:
            # Save directly without adjusting columns
            df.to_excel(output_path, sheet_name=sheet_name, index=False)

        logger.info(_scrub_log(f"Data successfully exported to: {output_path}"))
        return _finalize_export(str(output_path), rows)

    except Exception as e:
        logger.error(f"Error saving file: {e}")
        return None

def save_multiple_dataframes_to_excel(dataframes_dict: dict[str, Any], filename: str, prepare: bool = False) -> Optional[str]:
    """
    Save multiple pandas DataFrames to a single Excel file with multiple sheets,
    or to individual CSV files (one per sheet) when format is 'csv'.

    The output format is determined by the 'format' key in the 'output_settings'
    config section ('xlsx' or 'csv'). Defaults to 'xlsx'.

    For CSV mode, each sheet is written as a separate file:
        <base>-<sheet-slug>.csv
    e.g. account-ec2-export-05.15.2026.xlsx + "EC2 Instances"
         → account-ec2-export-05.15.2026-ec2-instances.csv

    Args:
        dataframes_dict: Dictionary of {sheet_name: dataframe}
        filename: Name of the base file to save
        prepare: If True, apply prepare_dataframe_for_export() to each DataFrame (default: False)

    Returns:
        str: Full path to the saved file (xlsx), or path of the first CSV written, or None on error
    """
    import re as _re

    try:
        # Import pandas here to avoid dependency issues
        import pandas as pd

        # Resolve output format from config
        fmt = config_value("format", default="xlsx", section="output_settings")

        # Prepare DataFrames if requested
        if prepare:
            dataframes_dict = {
                sheet_name: prepare_dataframe_for_export(df)
                for sheet_name, df in dataframes_dict.items()
            }

        # Per-sheet counts drive empty-suppression (skip only when ALL sheets are
        # empty) and the manifest's asset accounting.
        sheets = {name: int(len(df)) for name, df in dataframes_dict.items()}
        total_rows = sum(sheets.values())
        skipped = _maybe_skip_empty(filename, total_rows, sheets)
        if skipped is not None:
            return skipped

        output_dir = get_output_dir()
        os.makedirs(output_dir, exist_ok=True)

        if fmt == "csv":
            # Strip any extension from the base filename to get a clean stem
            base_stem = filename
            for ext in (".xlsx", ".csv"):
                if base_stem.endswith(ext):
                    base_stem = base_stem[: -len(ext)]
                    break

            delivered: list[str] = []
            for sheet_name, df in dataframes_dict.items():
                slug = _re.sub(r'[^a-z0-9]+', '-', sheet_name.lower()).strip('-')
                csv_filename = f"{base_stem}-{slug}.csv"
                csv_path = output_dir / csv_filename
                df.to_csv(csv_path, index=False)
                logger.info(_scrub_log(f"Data successfully exported to: {csv_path}"))
                delivered.append(deliver_output(str(csv_path)))

            first = delivered[0] if delivered else None
            # One manifest line for the whole multi-sheet export (first CSV as the
            # representative file), with per-sheet counts preserved.
            _record_run_manifest(filename, total_rows, "written", first, sheets)
            return first

        # --- xlsx path ---
        output_path = get_output_filepath(filename)

        # Sanitize sheet names for Excel (max 31 chars, no invalid chars, unique)
        sanitized_dict: dict[str, Any] = {}
        for raw_name, df in dataframes_dict.items():
            safe = raw_name
            for ch in ('\\', '/', '*', '?', ':', '[', ']'):
                safe = safe.replace(ch, '')
            safe = safe[:31].strip()
            if safe in sanitized_dict:
                base = safe[:27]
                n = 2
                while f"{base} ({n})" in sanitized_dict:
                    n += 1
                safe = f"{base} ({n})"
            sanitized_dict[safe] = df
        dataframes_dict = sanitized_dict

        # Create Excel writer using context manager to ensure proper close/save
        with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
            # Write each DataFrame to a separate sheet
            for sheet_name, df in dataframes_dict.items():
                df.to_excel(writer, sheet_name=sheet_name, index=False)

                # Auto-adjust column widths (skip if DataFrame is empty)
                if not df.empty:
                    _adjust_column_widths(writer.sheets[sheet_name], df)

        logger.info(_scrub_log(f"Data successfully exported to: {output_path}"))
        return _finalize_export(str(output_path), total_rows, sheets)

    except Exception as e:
        logger.error(f"Error saving file: {e}")
        return None


# ---------------------------------------------------------------------------
# Output delivery — S3 upload (Issue #175)
# ---------------------------------------------------------------------------
#
# Two delivery destinations are supported via the ``output_settings`` config
# block (or the ``STRATUSSCAN_S3_BUCKET`` env var for headless/CI runs):
#
#   * ``local`` (default) — files stay in ``output/`` exactly as before.
#   * ``s3``            — files are written to ``output/`` first, uploaded to
#                          the configured bucket, then the local copy is deleted
#                          on a successful upload. On failure the local file is
#                          retained as a fallback and the local path is returned.
#
# Scanning stays read-only everywhere; the only write any of this performs is a
# write-only ``PutObject`` to the one designated bucket.


def resolve_s3_destination() -> dict[str, Any]:
    """
    Resolve the effective output destination from config + environment.

    Resolution priority for the destination:
      1. ``STRATUSSCAN_OUTPUT_DESTINATION`` env (``local``/``s3``) — authoritative;
         this is how the ``--output`` flag forces a destination for the headless
         orchestrator and every exporter subprocess it launches. ``local`` here
         wins even if a bucket is configured.
      2. ``STRATUSSCAN_S3_BUCKET`` env set — implies S3 (CI hook; no config.json
         needed). ``STRATUSSCAN_S3_PREFIX`` optionally overrides the prefix.
      3. config ``output_settings.destination``.

    Returns:
        dict: ``{"enabled": bool, "bucket": str, "prefix": str}``.
              ``enabled`` is True only when S3 delivery is active AND a bucket
              is known.
    """
    settings = config_value("s3", default={}, section="output_settings") or {}
    config_destination = str(config_value("destination", default="local", section="output_settings")).lower()

    env_dest = os.environ.get("STRATUSSCAN_OUTPUT_DESTINATION", "").strip().lower()
    env_bucket = os.environ.get("STRATUSSCAN_S3_BUCKET", "").strip()
    env_prefix = os.environ.get("STRATUSSCAN_S3_PREFIX", "").strip()

    bucket = env_bucket or (settings.get("bucket", "") or "").strip()
    prefix = env_prefix or settings.get("prefix", "stratusscan/")

    if env_dest in ("local", "s3"):
        # Explicit flag override wins over everything (including a set bucket).
        selected = env_dest == "s3"
    else:
        # Otherwise a set bucket env, or config, selects S3.
        selected = bool(env_bucket) or config_destination == "s3"

    return {
        "enabled": bool(selected and bucket),
        "bucket": bucket,
        "prefix": prefix,
    }


def _s3_bootstrap_region() -> str:
    """
    Pick a region for the initial bucket-location lookup and as the upload
    fallback. Order: STRATUSSCAN_REGIONS → config default_regions → us-east-1.

    Using an operating region (rather than hardcoding us-east-1) keeps the
    partition correct so GovCloud buckets resolve against a GovCloud endpoint.
    """
    env_regions = os.environ.get("STRATUSSCAN_REGIONS", "").strip()
    if env_regions:
        first = env_regions.split(",")[0].strip()
        if first:
            return first

    defaults = config_value("default_regions", default=[]) or []
    if defaults:
        return defaults[0]

    return "us-east-1"


def _resolve_bucket_region(bucket: str, bootstrap_region: str) -> Optional[str]:
    """
    Return the region a bucket lives in via ``GetBucketLocation``.

    ``LocationConstraint`` is ``None``/empty for us-east-1 (AWS quirk); map that
    back to ``us-east-1``. Returns None if the lookup fails (caller falls back
    to the bootstrap region).
    """
    try:
        client = get_boto3_client("s3", bootstrap_region)
        loc = client.get_bucket_location(Bucket=bucket).get("LocationConstraint")
        return loc or "us-east-1"
    except Exception as e:
        logging.getLogger("stratusscan").debug(
            "GetBucketLocation failed for bucket '%s' from %s: %s", bucket, bootstrap_region, e
        )
        return None


def _build_s3_key(prefix: str, local_path: str) -> str:
    """Join a prefix and a file's basename into an S3 key (single '/' separators)."""
    name = os.path.basename(local_path)
    prefix = (prefix or "").lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}{name}"


def upload_to_s3(
    local_path: str,
    bucket: str,
    prefix: str = "stratusscan/",
    region: Optional[str] = None,
) -> Optional[str]:
    """
    Upload a local file to S3 and delete the local copy on success.

    Uses :func:`get_boto3_client` so the upload inherits retry/FIPS behaviour
    (GovCloud buckets get a FIPS endpoint automatically). The local file is
    removed only after a successful upload; on any failure it is left in place
    as a fallback.

    Args:
        local_path: Path to the file to upload.
        bucket: Destination S3 bucket name.
        prefix: Key prefix within the bucket.
        region: Bucket region. If None, resolved via GetBucketLocation with a
                bootstrap-region fallback.

    Returns:
        str: The ``s3://bucket/key`` URI on success, or None on failure.
    """
    log = logging.getLogger("stratusscan")

    if not os.path.exists(local_path):
        log.error("upload_to_s3: local file does not exist: %s", local_path)
        return None
    if not bucket:
        log.error("upload_to_s3: no bucket configured; cannot upload %s", local_path)
        return None

    bootstrap = _s3_bootstrap_region()
    if region is None:
        region = _resolve_bucket_region(bucket, bootstrap) or bootstrap

    key = _build_s3_key(prefix, local_path)
    try:
        client = get_boto3_client("s3", region)
        client.upload_file(local_path, bucket, key)
    except Exception as e:
        log.error("S3 upload failed for %s -> s3://%s/%s: %s", local_path, bucket, key, e)
        log.error("Local file retained at: %s", local_path)
        return None

    uri = f"s3://{bucket}/{key}"
    log.info("Uploaded to %s", uri)

    try:
        os.remove(local_path)
    except OSError as e:
        # Upload succeeded — a stranded local copy is non-fatal, just noisy.
        log.warning("Uploaded to S3 but could not delete local copy %s: %s", local_path, e)

    return uri


def deliver_output(local_path: str) -> str:
    """
    Post-save delivery hook: route a freshly-saved file to its destination.

    Called by the ``save_*`` helpers after a file is written locally. If S3
    delivery is active and an upload succeeds, returns the ``s3://`` URI (the
    local copy is gone). Otherwise — local destination, no bucket, or a failed
    upload — returns the original local path unchanged.

    Args:
        local_path: Path to the file just written to ``output/``.

    Returns:
        str: The S3 URI when uploaded, else the local path.
    """
    dest = resolve_s3_destination()
    if not dest["enabled"]:
        return local_path

    uri = upload_to_s3(local_path, dest["bucket"], dest["prefix"])
    return uri if uri else local_path


def test_s3_connectivity(
    bucket: str,
    prefix: str = "stratusscan/",
    region: Optional[str] = None,
) -> dict[str, Any]:
    """
    Run a full write roundtrip against a bucket and report which step failed.

    Steps: ``HeadBucket`` → ``PutObject`` (probe file) → ``DeleteObject``
    (cleanup). S3 permissions are notoriously fiddly, so each step's outcome is
    captured individually with the verbatim boto3 error — no generic "failed"
    messages. This is library code: it returns a structured result and never
    prints; the CLI layer renders it.

    Args:
        bucket: Bucket to test.
        prefix: Key prefix the probe object is written under.
        region: Bucket region (resolved via GetBucketLocation if None).

    Returns:
        dict: ``{"ok": bool, "bucket": str, "region": str|None, "key": str,
                 "steps": [{"step", "ok", "error"}], "failed_step": str|None}``
    """
    result: dict[str, Any] = {
        "ok": False,
        "bucket": bucket,
        "region": region,
        "key": None,
        "steps": [],
        "failed_step": None,
    }

    if not bucket:
        result["steps"].append(
            {"step": "config", "ok": False, "error": "No bucket configured."}
        )
        result["failed_step"] = "config"
        return result

    bootstrap = _s3_bootstrap_region()
    if region is None:
        region = _resolve_bucket_region(bucket, bootstrap) or bootstrap
    result["region"] = region

    key = _build_s3_key(prefix, "stratusscan-connectivity-test.txt")
    result["key"] = key

    def _run(step_name, fn) -> bool:
        try:
            fn()
            result["steps"].append({"step": step_name, "ok": True, "error": None})
            return True
        except Exception as e:
            result["steps"].append({"step": step_name, "ok": False, "error": str(e)})
            result["failed_step"] = step_name
            return False

    try:
        client = get_boto3_client("s3", region)
    except Exception as e:
        result["steps"].append({"step": "client", "ok": False, "error": str(e)})
        result["failed_step"] = "client"
        return result

    if not _run("HeadBucket", lambda: client.head_bucket(Bucket=bucket)):
        return result
    if not _run(
        "PutObject",
        lambda: client.put_object(
            Bucket=bucket, Key=key, Body=b"stratusscan connectivity test"
        ),
    ):
        return result
    if not _run("DeleteObject", lambda: client.delete_object(Bucket=bucket, Key=key)):
        return result

    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# Empty-export suppression + per-run manifest (audit run report)
# ---------------------------------------------------------------------------
#
# Two coupled behaviours that make an unattended --run-all audit trustworthy:
#
#   * skip-empty — when an exporter produces no rows, no spreadsheet is written.
#     This removes the noise of dozens of 0-row workbooks and, crucially, kills
#     the "empty file looks like a broken exporter" false positive.
#
#   * run manifest — every save records what ran and how many assets it found
#     (including the empties). The manifest is the authoritative answer to
#     "EC2 was expected but no EC2 file exists — did it run?": yes, it ran and
#     found 0. Suppression removes the file; the manifest removes the ambiguity.
#
# The manifest is written only when STRATUSSCAN_RUN_MANIFEST points at a file
# (set by the --run-all orchestrator per exporter subprocess). Interactive
# single exports get suppression but no manifest — their console message
# ("No data found — export skipped") is the human-facing equivalent.

# Returned by save_* when an empty export is intentionally suppressed. Truthy
# so the universal exporter idiom `if output_path: log_success` does NOT fall
# through to a misleading "save failed" error across the 100+ exporters.
EMPTY_EXPORT_SENTINEL = "(no data found — export skipped)"


def skip_empty_exports_enabled() -> bool:
    """True when empty results should not produce a spreadsheet (default: on)."""
    return bool(config_value("skip_empty_exports", default=True, section="output_settings"))


def _record_run_manifest(
    filename: str,
    rows: int,
    status: str,
    file_location: Optional[str],
    sheets: Optional[dict[str, int]] = None,
) -> None:
    """
    Append one record to the per-run manifest if STRATUSSCAN_RUN_MANIFEST is set.

    Best-effort: any failure here is logged at debug and swallowed — recording
    a manifest line must never break an export. Account/exporter/region context
    is read from env vars the orchestrator injects into each subprocess.

    Args:
        filename: The export filename (basename used as the resource hint).
        rows: Total asset/row count across all sheets.
        status: "written" or "empty".
        file_location: Final delivered location (local path or s3:// URI), or None.
        sheets: Optional per-sheet row counts.
    """
    manifest_path = os.environ.get("STRATUSSCAN_RUN_MANIFEST", "").strip()
    if not manifest_path:
        return

    try:
        record = {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "exporter": os.environ.get("STRATUSSCAN_CURRENT_EXPORTER", "").strip() or None,
            "resource": os.path.basename(filename),
            "account_id": os.environ.get("STRATUSSCAN_ACCOUNT_ID", "").strip() or None,
            "account_name": os.environ.get("STRATUSSCAN_ACCOUNT_NAME", "").strip() or None,
            "regions": os.environ.get("STRATUSSCAN_REGIONS", "").strip() or None,
            "rows": rows,
            "status": status,
            "file": file_location,
            "sheets": sheets or None,
        }
        # Append-only JSONL; concurrent appends are avoided because exporters
        # run sequentially, but a single write() of one line is atomic enough.
        with open(manifest_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as e:  # noqa: BLE001
        logging.getLogger("stratusscan").debug("Run-manifest record failed: %s", e)


def _maybe_skip_empty(
    filename: str, rows: int, sheets: Optional[dict[str, int]] = None
) -> Optional[str]:
    """
    Decide whether an export with ``rows`` total rows should be suppressed.

    Returns the EMPTY_EXPORT_SENTINEL (and records an "empty" manifest line) when
    there is no data and suppression is enabled; otherwise None (caller proceeds
    to write). Centralises the empty-handling so every save path behaves alike.
    """
    if rows == 0 and skip_empty_exports_enabled():
        logger.info("No data for %s — empty export skipped", os.path.basename(filename))
        _record_run_manifest(filename, 0, "empty", None, sheets)
        return EMPTY_EXPORT_SENTINEL
    return None


def _finalize_export(
    local_path: str, rows: int, sheets: Optional[dict[str, int]] = None
) -> str:
    """
    Common tail for every successful save: deliver the file (local or S3) and
    record a "written" manifest line. Returns the final location (local path or
    ``s3://`` URI).
    """
    final = deliver_output(local_path)
    _record_run_manifest(local_path, rows, "written", final, sheets)
    return final


def read_run_manifest(manifest_path: str) -> list[dict[str, Any]]:
    """
    Read a per-run manifest (JSONL) into a list of records.

    Malformed lines are skipped. Returns an empty list if the file is missing.
    """
    records: list[dict[str, Any]] = []
    if not manifest_path or not os.path.exists(manifest_path):
        return records
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logging.getLogger("stratusscan").warning("Could not read run manifest %s: %s", manifest_path, e)
    return records


# Run-report status vocabulary (one per exporter attempted in a --run-all run).
RUN_STATUS_OK = "OK"                # exited clean, wrote at least one populated export
RUN_STATUS_EMPTY = "EMPTY"          # exited clean, ran, found 0 assets (no file written)
RUN_STATUS_NO_OUTPUT = "NO OUTPUT"  # exited clean but saved nothing (e.g. service N/A in partition)
RUN_STATUS_FAILED = "FAILED"        # non-zero exit / crash / timeout


def _format_sheets(sheets: Optional[dict[str, int]]) -> str:
    """Render per-sheet counts as 'Sheet:rows, ...' for the report."""
    if not sheets:
        return ""
    return ", ".join(f"{name}:{count}" for name, count in sheets.items())


def build_run_report_dataframe(
    executor_results: list[dict[str, Any]],
    manifest_records: list[dict[str, Any]],
):
    """
    Merge per-script execution results with per-save manifest records into a
    single run-report DataFrame — the authoritative "what ran / what was found"
    artifact for an unattended audit.

    Each row is one exporter attempted, with a status that distinguishes the
    four outcomes auditors care about: OK (data found), EMPTY (ran, 0 assets),
    NO OUTPUT (ran clean but nothing to export — e.g. service not in partition),
    and FAILED. The asset count is recorded even when no spreadsheet exists,
    which is precisely what kills the "no EC2 file, did it run?" false positive.

    Args:
        executor_results: list of dicts with at least ``script``, ``success``,
            ``return_code``, ``duration_seconds`` (ExecutionResult flattened).
        manifest_records: records from :func:`read_run_manifest`.

    Returns:
        pandas.DataFrame sorted by exporter name.
    """
    import pandas as pd

    # Index manifest records by exporter filename.
    by_exporter: dict[str, list[dict[str, Any]]] = {}
    for rec in manifest_records:
        key = rec.get("exporter") or rec.get("resource") or ""
        by_exporter.setdefault(key, []).append(rec)

    rows: list[dict[str, Any]] = []
    for res in executor_results:
        exporter = res.get("script", "")
        recs = by_exporter.get(exporter, [])
        written = [r for r in recs if r.get("status") == "written"]
        assets = sum(int(r.get("rows") or 0) for r in recs)

        if not res.get("success", False):
            status = RUN_STATUS_FAILED
        elif written:
            status = RUN_STATUS_OK
        elif recs:  # only empty records
            status = RUN_STATUS_EMPTY
        else:
            status = RUN_STATUS_NO_OUTPUT

        file_loc = ""
        if written:
            file_loc = written[0].get("file") or ""

        sheets_str = ""
        for r in recs:
            if r.get("sheets"):
                sheets_str = _format_sheets(r.get("sheets"))
                break

        regions = ""
        if recs and recs[0].get("regions"):
            regions = recs[0]["regions"]

        detail = ""
        if status == RUN_STATUS_FAILED:
            detail = (res.get("error_message") or f"exit code {res.get('return_code')}").strip()

        rows.append({
            "Exporter": exporter,
            "Status": status,
            "Assets": assets,
            "Output File": file_loc,
            "Sheets": sheets_str,
            "Regions": regions,
            "Duration (s)": round(float(res.get("duration_seconds") or 0), 1),
            "Detail": detail,
        })

    columns = ["Exporter", "Status", "Assets", "Output File", "Sheets", "Regions", "Duration (s)", "Detail"]
    df = pd.DataFrame(rows, columns=columns)
    if not df.empty:
        df = df.sort_values("Exporter").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Organization-wide scanning (--org-scan): account enumeration + roll-up
# ---------------------------------------------------------------------------
#
# The headless org audit runs from one account (the Audit account), enumerates
# every active account in the AWS Organization, assumes a uniformly-named
# read-only scan role in each, and runs the full exporter set per account. This
# matches the StackSet model where one role name exists org-wide.

# Org-account scan outcomes for the roll-up summary.
ACCOUNT_STATUS_SCANNED = "SCANNED"        # role assumed, exporters ran
ACCOUNT_STATUS_ROLE_FAILED = "ROLE FAILED"  # could not assume the scan role — skipped


def list_organization_accounts(active_only: bool = True) -> list[dict[str, str]]:
    """
    Enumerate accounts in the AWS Organization via ``organizations:ListAccounts``.

    Runs from the management or a delegated-admin account. Library code: returns
    structured data and never prints.

    Args:
        active_only: When True (default), only ``ACTIVE`` accounts are returned
            (SUSPENDED / PENDING_CLOSURE accounts are skipped).

    Returns:
        list of ``{"id", "name", "email", "status"}`` dicts.

    Raises:
        Propagates botocore errors (e.g. AccessDenied when the caller lacks
        Organizations read access) so the caller can report them verbatim.
    """
    client = get_boto3_client("organizations")
    accounts: list[dict[str, str]] = []
    paginator = client.get_paginator("list_accounts")
    for page in paginator.paginate():
        for a in page.get("Accounts", []):
            status = a.get("Status", "")
            if active_only and status != "ACTIVE":
                continue
            accounts.append({
                "id": a.get("Id", ""),
                "name": a.get("Name", ""),
                "email": a.get("Email", ""),
                "status": status,
            })
    return accounts


def verify_assume_role(role_arn: str, region_name: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """
    Pre-flight an STS AssumeRole so a non-assumable account can be skipped before
    launching 100+ doomed exporter subprocesses.

    Args:
        role_arn: The scan role ARN to assume.
        region_name: Optional region (drives FIPS endpoint selection in GovCloud).

    Returns:
        ``(True, None)`` if the role is assumable, else ``(False, error_string)``
        with the verbatim error for the run report.
    """
    try:
        sts = get_boto3_client("sts", region_name, role_arn=role_arn)
        sts.get_caller_identity()
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def build_org_summary_dataframe(account_summaries: list[dict[str, Any]]):
    """
    Roll up per-account audit outcomes into one org-level summary DataFrame —
    one row per account, the top-level index over the per-account run reports.

    Args:
        account_summaries: list of dicts with keys ``account_id``,
            ``account_name``, ``status`` (ACCOUNT_STATUS_*), ``exporters``,
            ``ok``, ``empty``, ``no_output``, ``failed``, ``total_assets``,
            ``report`` (delivered location or ""), and optional ``detail``.

    Returns:
        pandas.DataFrame sorted by account name.
    """
    import pandas as pd

    rows = []
    for s in account_summaries:
        rows.append({
            "Account ID": s.get("account_id", ""),
            "Account": s.get("account_name", ""),
            "Status": s.get("status", ""),
            "Exporters": s.get("exporters", 0),
            "With Data": s.get("ok", 0),
            "Empty": s.get("empty", 0),
            "No Output": s.get("no_output", 0),
            "Failed": s.get("failed", 0),
            "Total Assets": s.get("total_assets", 0),
            "Report": s.get("report", ""),
            "Detail": s.get("detail", ""),
        })

    columns = ["Account ID", "Account", "Status", "Exporters", "With Data",
               "Empty", "No Output", "Failed", "Total Assets", "Report", "Detail"]
    df = pd.DataFrame(rows, columns=columns)
    if not df.empty:
        df = df.sort_values("Account").reset_index(drop=True)
    return df


def detect_default_format() -> str:
    """
    Detect the default export format based on available dependencies.

    Returns 'xlsx' when openpyxl is importable, 'csv' otherwise. Intended
    for use by configure.py at wizard time to pre-populate a sensible default.
    The actual runtime format is always read from config by save functions.

    Returns:
        str: 'xlsx' or 'csv'
    """
    try:
        import openpyxl  # noqa: F401
        return "xlsx"
    except ImportError:
        return "csv"


def create_aws_arn(service: str, resource: str, region: Optional[str] = None, account_id: Optional[str] = None) -> str:
    """
    Create a properly formatted AWS ARN.

    DEPRECATED: Use build_arn() instead for partition-aware ARN construction.

    Args:
        service: AWS service name
        resource: Resource identifier
        region: AWS region (optional)
        account_id: AWS account ID (optional)

    Returns:
        str: Properly formatted AWS ARN
    """
    warnings.warn(
        "create_aws_arn() is deprecated; use build_arn() for partition-aware ARN construction.",
        DeprecationWarning,
        stacklevel=2,
    )
    # Delegate to the new partition-aware function
    return build_arn(service, resource, region=region, account_id=account_id)

def parse_aws_arn(arn: str) -> Optional[dict[str, str]]:
    """
    Parse an AWS ARN into its components (partition-aware).

    Args:
        arn: AWS ARN string

    Returns:
        dict: Dictionary with ARN components or None if invalid
    """
    try:
        parts = arn.split(':')
        # Accept both 'aws' and 'aws-us-gov' partitions
        if len(parts) >= 6 and parts[0] == 'arn' and parts[1] in ['aws', 'aws-us-gov']:
            return {
                'partition': parts[1],
                'service': parts[2],
                'region': parts[3],
                'account_id': parts[4],
                'resource': ':'.join(parts[5:])
            }
    except Exception as e:
        logger.warning(f"Error parsing ARN '{arn}': {e}")

    return None


# =============================================================================
# STANDARDIZED ERROR HANDLING
# =============================================================================

# TypeVar for generic return types
T = TypeVar('T')


def aws_error_handler(
    operation_name: str,
    default_return: Any = None,
    reraise: bool = False
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator for standardized AWS error handling.

    This decorator provides consistent error handling for AWS operations,
    including specific handling for NoCredentialsError, ClientError, and
    generic exceptions. All errors are logged using the existing logging
    infrastructure.

    Args:
        operation_name: Human-readable operation description for logging
        default_return: Value to return on error (if not reraising)
        reraise: Whether to re-raise the exception after logging

    Returns:
        Decorator function that wraps the target function

    Example:
        @aws_error_handler("Collecting IAM users", default_return=[])
        def collect_iam_users() -> list[dict[str, Any]]:
            iam = get_boto3_client('iam')
            users = []
            for user in iam.list_users()['Users']:
                users.append(user)
            return users

        @aws_error_handler("Creating EC2 instance", reraise=True)
        def create_instance(instance_type: str) -> str:
            ec2 = get_boto3_client('ec2', region_name='us-east-1')
            response = ec2.run_instances(
                ImageId='ami-12345',
                InstanceType=instance_type,
                MinCount=1,
                MaxCount=1
            )
            return response['Instances'][0]['InstanceId']
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # Import here to avoid circular imports
                try:
                    from botocore.exceptions import ClientError, NoCredentialsError

                    # Handle NoCredentialsError specifically
                    if isinstance(e, NoCredentialsError):
                        log_error(
                            f"{operation_name}: No AWS credentials found. "
                            "Please configure credentials using 'aws configure' or environment variables."
                        )
                        if reraise:
                            raise
                        return default_return

                    # Handle ClientError with error code extraction
                    elif isinstance(e, ClientError):
                        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
                        error_msg = e.response.get('Error', {}).get('Message', str(e))
                        log_error(f"{operation_name}: AWS error [{error_code}]: {error_msg}")
                        if reraise:
                            raise
                        return default_return

                    # Handle all other exceptions
                    else:
                        log_error(f"{operation_name}: Unexpected error", e)
                        if reraise:
                            raise
                        return default_return

                except ImportError:
                    # Fallback if botocore is not available
                    log_error(f"{operation_name}: Error occurred", e)
                    if reraise:
                        raise
                    return default_return

        return wrapper
    return decorator


@contextmanager
def handle_aws_operation(
    operation_name: str,
    default_return: Any = None,
    suppress_errors: bool = False
):
    """
    Context manager for AWS operations with standardized error handling.

    This context manager provides flexible error handling for AWS operations
    when more control is needed than the decorator provides. It allows for
    custom logic within the try block while maintaining consistent error logging.

    Args:
        operation_name: Human-readable operation description for logging
        default_return: Value to return on error (if suppress_errors=True)
        suppress_errors: Whether to suppress exceptions (False = reraise)

    Yields:
        None - allows execution of the with block

    Raises:
        Exception: Re-raises the caught exception if suppress_errors=False

    Example:
        # Suppress errors and return default value
        with handle_aws_operation("Fetching EC2 pricing", default_return={}, suppress_errors=True):
            pricing_client = get_boto3_client('pricing', region_name='us-east-1')
            response = pricing_client.get_products(ServiceCode='AmazonEC2')
            pricing_data = response['PriceList']

        # Re-raise errors after logging
        with handle_aws_operation("Creating S3 bucket", suppress_errors=False):
            s3 = get_boto3_client('s3')
            s3.create_bucket(Bucket='my-bucket')
            log_success("Bucket created successfully")

        # Multiple operations in one block
        with handle_aws_operation("Multi-step deployment", suppress_errors=False):
            ec2 = get_boto3_client('ec2', region_name='us-east-1')

            # Step 1: Create VPC
            vpc_response = ec2.create_vpc(CidrBlock='10.0.0.0/16')
            vpc_id = vpc_response['Vpc']['VpcId']
            log_info(f"Created VPC: {vpc_id}")

            # Step 2: Create subnet
            subnet_response = ec2.create_subnet(
                VpcId=vpc_id,
                CidrBlock='10.0.1.0/24'
            )
            subnet_id = subnet_response['Subnet']['SubnetId']
            log_info(f"Created subnet: {subnet_id}")
    """
    try:
        yield
    except Exception as e:
        # Import here to avoid circular imports
        try:
            from botocore.exceptions import ClientError, NoCredentialsError

            # Handle NoCredentialsError specifically
            if isinstance(e, NoCredentialsError):
                log_error(
                    f"{operation_name}: No AWS credentials found. "
                    "Please configure credentials using 'aws configure' or environment variables."
                )
                if not suppress_errors:
                    raise
                return default_return

            # Handle ClientError with error code extraction
            elif isinstance(e, ClientError):
                error_code = e.response.get('Error', {}).get('Code', 'Unknown')
                error_msg = e.response.get('Error', {}).get('Message', str(e))
                log_error(f"{operation_name}: AWS error [{error_code}]: {error_msg}")
                if not suppress_errors:
                    raise
                return default_return

            # Handle all other exceptions
            else:
                log_error(f"{operation_name}: Unexpected error", e)
                if not suppress_errors:
                    raise
                return default_return

        except ImportError:
            # Fallback if botocore is not available
            log_error(f"{operation_name}: Error occurred", e)
            if not suppress_errors:
                raise
            return default_return


# =============================================================================
# SHARED UTILITY FUNCTIONS FOR SCRIPTS
# =============================================================================


def run_subprocess_with_progress(
    cmd: list,
    env: dict,
    timeout: int,
    start_time: float,
) -> "subprocess.Popen":
    """
    Run a child script, relaying its stdout+stderr to the terminal and showing
    a spinner when no output has been received for more than one second.

    Replaces bare subprocess.run(capture_output=False) in the all-in-one
    orchestrators (compute / storage / network / database) so the user sees
    activity feedback during long AWS API calls instead of silence.

    Args:
        cmd:        Command list passed to Popen (e.g. [sys.executable, path]).
        env:        Environment dict for the child process.
        timeout:    Seconds before the child is force-killed.
        start_time: time.time() value from when the script slot started,
                    used for the elapsed-time display.

    Returns:
        The completed Popen object; caller checks .returncode.

    Raises:
        subprocess.TimeoutExpired: re-raised after killing the child.
    """
    SPINNER = "|/-\\"
    SPINNER_INTERVAL = 0.25   # seconds between spinner frames
    IDLE_THRESHOLD   = 1.0    # seconds of silence before spinner appears
    CLEAR            = "\r" + " " * 30 + "\r"

    spin_idx        = [0]
    last_output_at  = [start_time]
    spinner_showing = [False]
    stop_event      = threading.Event()

    def _spin() -> None:
        while not stop_event.is_set():
            idle = time.time() - last_output_at[0]
            if idle >= IDLE_THRESHOLD:
                elapsed = time.time() - start_time
                c = SPINNER[spin_idx[0] % 4]
                print(f"\r  {c} {elapsed:.0f}s", end="", flush=True)
                spin_idx[0] += 1
                spinner_showing[0] = True
            stop_event.wait(SPINNER_INTERVAL)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        bufsize=1,
    )

    spinner_thread = threading.Thread(target=_spin, daemon=True)
    spinner_thread.start()

    try:
        for line in proc.stdout:
            if spinner_showing[0]:
                print(CLEAR, end="", flush=True)
                spinner_showing[0] = False
            print(line, end="", flush=True)
            last_output_at[0] = time.time()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        stop_event.set()
        spinner_thread.join(timeout=1)
        if spinner_showing[0]:
            print(CLEAR, end="", flush=True)

    return proc


def ensure_dependencies(*packages: str) -> bool:
    """
    Check and optionally install required dependencies.

    This function checks if the specified packages are installed and offers to
    install any missing packages via pip. It's designed to eliminate duplicate
    dependency checking code across StratusScan scripts.

    Args:
        *packages: Variable number of package names to check/install

    Returns:
        bool: True if all dependencies are satisfied, False otherwise

    Examples:
        >>> # Check single package
        >>> if not ensure_dependencies('pandas'):
        ...     sys.exit(1)

        >>> # Check multiple packages
        >>> if not ensure_dependencies('pandas', 'openpyxl', 'boto3'):
        ...     sys.exit(1)

        >>> # Typical usage in scripts
        >>> def main():
        ...     if not utils.ensure_dependencies('pandas', 'openpyxl'):
        ...         return
        ...     import pandas as pd
        ...     # Continue with script logic
    """
    missing = []

    # Check each package
    for package in packages:
        try:
            __import__(package)
            log_info(f"[OK] {package} is already installed")
        except ImportError:
            missing.append(package)
            log_warning(f"[MISSING] {package} is not installed")

    # All dependencies satisfied
    if not missing:
        log_success("All required dependencies are installed")
        return True

    # Prompt user to install missing packages
    log_warning(f"Missing packages: {', '.join(missing)}")

    # In automation mode, never block on interactive prompts
    if is_auto_run():
        log_error(
            f"Cannot install missing packages in auto-run mode: {', '.join(missing)}. "
            "Run 'pip install " + " ".join(missing) + "' manually, then retry."
        )
        return False

    print(f"\nThe following packages are required but not installed: {', '.join(missing)}")
    response = input("Would you like to install these packages now? (y/n): ").lower().strip()

    if response != 'y':
        log_error(f"Cannot continue without required packages. Run manually: pip install {' '.join(missing)}")
        return False

    # Install missing packages
    log_warning(f"Installing: {' '.join(missing)}")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install"] + missing
        )
    except subprocess.CalledProcessError as e:
        log_error(f"pip install failed (exit {e.returncode}). Run manually: pip install {' '.join(missing)}")
        return False
    except Exception as e:
        log_error("Unexpected error running pip", e)
        return False

    # Refresh Python's import cache so newly installed packages are visible
    importlib.invalidate_caches()

    # Verify each package is now importable
    still_missing = []
    for package in missing:
        try:
            __import__(package)
        except ImportError:
            still_missing.append(package)

    if still_missing:
        log_error(
            f"pip reported success but {', '.join(still_missing)} still not importable. "
            "Try opening a new shell and re-running the script."
        )
        return False

    log_success("All dependencies installed successfully")
    return True


def mask_account_id(account_id: str) -> str:
    """
    Mask an AWS account ID for safe inclusion in INFO-level log output.

    Returns the last 4 digits prefixed with '...' so log consumers can identify
    the account without the full 12-digit ID being indexed by log aggregators.
    Full account IDs should be logged at DEBUG level for troubleshooting.

    Examples:
        >>> mask_account_id('123456789012')
        '...9012'
        >>> mask_account_id('')
        ''
    """
    if not account_id or len(account_id) < 4:
        return account_id
    return f"...{account_id[-4:]}"


def get_account_info() -> tuple[str, str]:
    """
    Get AWS account ID and name with caching.

    This function retrieves the current AWS account ID using STS and maps it to
    a friendly name using the account mappings from config.json. Results are
    cached to avoid repeated STS API calls.

    Returns:
        tuple: (account_id, account_name) where account_id is the 12-digit AWS
               account ID and account_name is the friendly name from config.json
               or a default value if not found. Returns ("UNKNOWN", "UNKNOWN-ACCOUNT")
               if unable to retrieve account information.

    Examples:
        >>> # Get account info
        >>> account_id, account_name = utils.get_account_info()
        >>> print(f"Account: {account_name} ({account_id})")
        Account: PROD-ACCOUNT (123456789012)

        >>> # Use in filename generation
        >>> account_id, account_name = utils.get_account_info()
        >>> filename = utils.create_export_filename(
        ...     account_name,
        ...     "ec2",
        ...     "running"
        ... )

        >>> # Typical usage pattern in scripts
        >>> def main():
        ...     account_id, account_name = utils.get_account_info()
        ...     utils.log_info(f"Scanning account: {account_name}")

    Note:
        - Delegates to get_cached_account_info() to avoid duplicate STS calls
        - Uses get_boto3_client() which includes automatic retry logic
        - Falls back to UNKNOWN values on error rather than raising exceptions
    """
    account_id, account_name, _ = get_cached_account_info()
    return account_id, account_name


def print_script_banner(subtitle: str) -> tuple[str, str]:
    """
    Print a standardized export script banner and return AWS account information.

    Prints a consistent 60-character banner using the provided subtitle, then
    retrieves and returns the current AWS account ID and name. Replaces the
    per-script print_title() / print_header() functions that previously
    duplicated this logic across every exporter.

    Args:
        subtitle: The script-specific title line displayed in the banner,
                  e.g. "AWS EC2 INSTANCE EXPORT". Should be ALL CAPS with
                  no trailing "TOOL", "SCRIPT", or "EXPORT TOOL" suffix.

    Returns:
        tuple: (account_id, account_name) as returned by get_account_info().

    Example:
        >>> account_id, account_name = utils.print_script_banner("AWS EC2 INSTANCE EXPORT")
        >>> # Prints:
        >>> # ============================================================
        >>> # AWS EC2 INSTANCE EXPORT
        >>> # ============================================================
    """
    print("\n" + "=" * 60)
    print(subtitle)
    print("=" * 60)
    return get_account_info()


# =============================================================================
# DATAFRAME PREPARATION & EXPORT UTILITIES
# =============================================================================


def prepare_dataframe_for_export(
    df,
    remove_timezone: bool = True,
    fill_na: str = 'N/A',
    truncate_strings: Optional[int] = None,
    max_column_width: int = 50
):
    """
    Prepare a pandas DataFrame for Excel export by standardizing data types and values.

    This function handles common issues that prevent clean Excel exports:
    - Removes timezone information from datetime columns (Excel doesn't support timezone-aware datetimes)
    - Standardizes NaN/None values to a consistent string
    - Truncates excessively long strings to prevent Excel cell overflow
    - Ensures all data types are Excel-compatible

    Args:
        df: Input pandas DataFrame to prepare
        remove_timezone: If True, remove timezone info from datetime columns (default: True)
        fill_na: String to replace NaN/None values (default: 'N/A')
        truncate_strings: Max string length before truncation, None to disable (default: 1000)
        max_column_width: Used for documentation, doesn't affect processing (default: 50)

    Returns:
        Cleaned DataFrame ready for Excel export

    Example:
        >>> df = collect_ec2_instances()
        >>> df = utils.prepare_dataframe_for_export(df)
        >>> utils.save_dataframe_to_excel(df, filename)

    Note:
        - This function creates a copy of the input DataFrame to avoid modifying the original
        - Empty DataFrames are returned unchanged
        - The max_column_width parameter is for reference only (used by save functions)
    """
    # Import pandas here to avoid requiring it at module load time
    import pandas as pd

    # Handle empty DataFrame
    if df is None or df.empty:
        log_debug("Empty DataFrame provided to prepare_dataframe_for_export, returning as-is")
        return df if df is not None else pd.DataFrame()

    # Make a copy to avoid modifying the original
    df_clean = df.copy()

    # Remove timezone information from datetime columns
    if remove_timezone:
        try:
            # Find datetime columns with timezone information
            datetime_cols = df_clean.select_dtypes(include=['datetime64[ns, UTC]', 'datetimetz']).columns

            # Also check for object columns that might contain datetime objects
            for col in df_clean.columns:
                if df_clean[col].dtype == 'object':
                    # Sample first non-null value to check if it's a datetime
                    sample = df_clean[col].dropna().head(1)
                    if not sample.empty and hasattr(sample.iloc[0], 'tzinfo') and sample.iloc[0].tzinfo is not None:
                        datetime_cols = datetime_cols.union(pd.Index([col]))

            # Remove timezone from identified columns
            for col in datetime_cols:
                try:
                    # Try pandas datetime conversion first
                    df_clean[col] = pd.to_datetime(df_clean[col]).dt.tz_localize(None)
                    log_debug(f"Removed timezone from column: {col}")
                except Exception as e:
                    log_debug(f"Could not remove timezone from {col}: {e}")
                    # Try alternative approach for object columns
                    try:
                        df_clean[col] = df_clean[col].apply(
                            lambda x: x.replace(tzinfo=None) if hasattr(x, 'replace') and hasattr(x, 'tzinfo') else x
                        )
                    except Exception as e2:
                        log_warning(f"Failed to remove timezone from column {col}: {e2}")
        except Exception as e:
            log_warning(f"Error processing datetime columns for timezone removal: {e}")

    # Fill NaN values with standard placeholder
    try:
        df_clean = df_clean.fillna(fill_na)
        log_debug(f"Filled NaN values with '{fill_na}'")
    except Exception as e:
        log_warning(f"Error filling NaN values: {e}")

    # Truncate excessively long strings
    if truncate_strings and truncate_strings > 0:
        try:
            # Get object (string) columns
            object_cols = df_clean.select_dtypes(include=['object']).columns

            for col in object_cols:
                try:
                    # Apply truncation only to strings longer than the limit
                    df_clean[col] = df_clean[col].apply(
                        lambda x: (str(x)[:truncate_strings] + '...' if isinstance(x, str) and len(x) > truncate_strings else x)
                    )
                except Exception as e:
                    log_debug(f"Could not truncate strings in column {col}: {e}")

            log_debug(f"Truncated strings longer than {truncate_strings} characters")
        except Exception as e:
            log_warning(f"Error truncating string columns: {e}")

    log_debug(f"DataFrame preparation complete: {len(df_clean)} rows, {len(df_clean.columns)} columns")
    return df_clean


def sanitize_for_export(
    df,
    sensitive_patterns: Optional[list[str]] = None,
    mask_string: str = '***REDACTED***'
):
    """
    Sanitize potentially sensitive data in DataFrame before export.

    This function searches for sensitive data patterns (passwords, API keys, tokens, credentials)
    in DataFrame values and masks them. Particularly useful for tag columns that may contain
    sensitive configuration data.

    Args:
        df: Input pandas DataFrame to sanitize
        sensitive_patterns: List of regex patterns to search for (default: common sensitive patterns)
        mask_string: String to replace sensitive data with (default: '***REDACTED***')

    Returns:
        Sanitized DataFrame with sensitive data masked

    Example:
        >>> df = collect_resources_with_tags()
        >>> df = utils.sanitize_for_export(df)
        >>> utils.save_dataframe_to_excel(df, filename)

    Note:
        - This function creates a copy of the input DataFrame to avoid modifying the original
        - Default patterns catch common secret formats in tags and environment variables
        - Case-insensitive pattern matching
        - Processes only string (object) columns
    """
    # Import pandas here to avoid requiring it at module load time
    import pandas as pd

    # Handle empty DataFrame
    if df is None or df.empty:
        log_debug("Empty DataFrame provided to sanitize_for_export, returning as-is")
        return df if df is not None else pd.DataFrame()

    # Make a copy to avoid modifying the original
    df_sanitized = df.copy()

    # Define default sensitive patterns if none provided
    if sensitive_patterns is None:
        sensitive_patterns = [
            r'(?i)(password|passwd|pwd)\s*[:=]\s*\S+',
            r'(?i)(api[_-]?key|apikey)\s*[:=]\s*\S+',
            r'(?i)(access[_-]?key|accesskey)\s*[:=]\s*\S+',
            r'(?i)(secret[_-]?key|secretkey)\s*[:=]\s*\S+',
            r'(?i)(token)\s*[:=]\s*\S+',
            r'(?i)(credential|cred)\s*[:=]\s*\S+',
            r'(?i)(auth)\s*[:=]\s*\S+',
        ]

    # Compile regex patterns for efficiency
    try:
        compiled_patterns = [re.compile(pattern) for pattern in sensitive_patterns]
        log_debug(f"Compiled {len(compiled_patterns)} sensitive data patterns")
    except Exception as e:
        log_error(f"Error compiling regex patterns: {e}")
        return df_sanitized

    # Track sanitization statistics
    total_masked = 0
    columns_affected = []

    # Get object (string) columns only
    try:
        object_cols = df_sanitized.select_dtypes(include=['object']).columns

        for col in object_cols:
            col_masked = 0
            try:
                # Apply sanitization to each cell in the column
                def mask_sensitive(cell_value):
                    nonlocal col_masked
                    if not isinstance(cell_value, str):
                        return cell_value

                    # Check each pattern
                    modified = cell_value
                    for pattern in compiled_patterns:
                        matches = pattern.findall(modified)
                        if matches:
                            col_masked += len(matches)
                            # Replace sensitive data while preserving the key name
                            modified = pattern.sub(lambda m: m.group(1) + mask_string, modified)

                    return modified

                # Apply the masking function
                df_sanitized[col] = df_sanitized[col].apply(mask_sensitive)

                if col_masked > 0:
                    total_masked += col_masked
                    columns_affected.append(col)
                    log_debug(f"Masked {col_masked} sensitive values in column: {col}")

            except Exception as e:
                log_warning(f"Error sanitizing column {col}: {e}")

        # Log summary
        if total_masked > 0:
            log_info(f"Sanitized {total_masked} sensitive values across {len(columns_affected)} columns")
            log_debug(f"Affected columns: {', '.join(columns_affected)}")
        else:
            log_debug("No sensitive data patterns found in DataFrame")

    except Exception as e:
        log_error(f"Error during DataFrame sanitization: {e}")

    return df_sanitized


# =============================================================================
# PROGRESS CHECKPOINTING & RESUME CAPABILITY
# =============================================================================


class ProgressCheckpoint:
    """
    Progress checkpointing system for long-running AWS operations.

    This class allows scripts to save their progress periodically and resume
    from the last checkpoint if interrupted. Useful for large-scale exports
    across multiple regions or accounts.

    Example:
        >>> checkpoint = ProgressCheckpoint('ec2-export', total_items=100)
        >>>
        >>> for i, instance in enumerate(instances):
        >>>     # Process instance
        >>>     process_instance(instance)
        >>>
        >>>     # Save checkpoint every 10 items
        >>>     checkpoint.save(current_index=i, data={'last_instance_id': instance['InstanceId']})
        >>>
        >>> checkpoint.mark_complete()
        >>> checkpoint.cleanup()
    """

    def __init__(self, operation_name: str, total_items: Optional[int] = None, checkpoint_dir: Optional[Path] = None):
        """
        Initialize progress checkpoint.

        Args:
            operation_name: Unique name for this operation
            total_items: Total number of items to process (optional)
            checkpoint_dir: Directory to store checkpoints (default: .checkpoints/)
        """
        self.operation_name = operation_name
        self.total_items = total_items

        # Set up checkpoint directory
        if checkpoint_dir is None:
            root_dir = get_stratusscan_root()
            checkpoint_dir = root_dir / '.checkpoints'

        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(exist_ok=True)

        # Checkpoint file path — operation_name is caller-supplied, so sanitize
        # the component and confirm containment in checkpoint_dir (CWE-73).
        timestamp = datetime.datetime.now().strftime("%Y%m%d")
        safe_operation = sanitize_filename_component(operation_name)
        self.checkpoint_file = contained_path(
            self.checkpoint_dir, f"{safe_operation}_{timestamp}.json"
        )

        # Load existing checkpoint if available
        self.checkpoint_data = self._load()

        log_debug(f"Initialized checkpoint for {operation_name}")

    def _load(self) -> dict[str, Any]:
        """Load checkpoint data from file if it exists."""
        if self.checkpoint_file.exists():
            try:
                with open(self.checkpoint_file, encoding='utf-8') as f:
                    data = json.load(f)
                log_info(f"Loaded checkpoint from {self.checkpoint_file}")
                log_info(f"Previous progress: {data.get('current_index', 0)}/{self.total_items or '?'}")
                return data
            except Exception as e:
                log_warning(f"Failed to load checkpoint: {e}")
                return {}
        return {}

    def save(self, current_index: int, data: Optional[dict[str, Any]] = None):
        """
        Save current progress to checkpoint file.

        Args:
            current_index: Current position in the operation
            data: Additional data to save with checkpoint
        """
        try:
            checkpoint_data = {
                'operation_name': self.operation_name,
                'current_index': current_index,
                'total_items': self.total_items,
                'timestamp': datetime.datetime.now().isoformat(),
                'data': data or {}
            }

            with open(self.checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump(checkpoint_data, f)

            # Log progress percentage if total known
            if self.total_items:
                progress_pct = (current_index / self.total_items) * 100
                log_debug(f"Checkpoint saved: {current_index}/{self.total_items} ({progress_pct:.1f}%)")
            else:
                log_debug(f"Checkpoint saved: {current_index} items processed")

        except Exception as e:
            log_warning(f"Failed to save checkpoint: {e}")

    def is_complete(self) -> bool:
        """Check if operation was previously completed."""
        return self.checkpoint_data.get('completed', False)

    def mark_complete(self):
        """Mark operation as complete."""
        try:
            self.checkpoint_data['completed'] = True
            self.checkpoint_data['completion_time'] = datetime.datetime.now().isoformat()

            with open(self.checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump(self.checkpoint_data, f)

            log_success(f"Operation {self.operation_name} marked as complete")
        except Exception as e:
            log_warning(f"Failed to mark checkpoint as complete: {e}")

    def get_data(self, key: str, default: Any = None) -> Any:
        """Get value from checkpoint data."""
        return self.checkpoint_data.get('data', {}).get(key, default)

    def get_completed_count(self) -> int:
        """Get number of items already processed."""
        return self.checkpoint_data.get('current_index', 0)

    def cleanup(self):
        """Remove checkpoint file after successful completion."""
        try:
            if self.checkpoint_file.exists():
                self.checkpoint_file.unlink()
                log_info(f"Cleaned up checkpoint file: {self.checkpoint_file}")
        except Exception as e:
            log_warning(f"Failed to cleanup checkpoint: {e}")


# =============================================================================
# DRY-RUN MODE & VALIDATION
# =============================================================================


def validate_export(
    df,
    resource_type: str,
    required_columns: Optional[list[str]] = None,
    dry_run: bool = False
) -> tuple[bool, str]:
    """
    Validate DataFrame before export (supports dry-run mode).

    This function validates that a DataFrame is ready for export by checking:
    - DataFrame is not empty
    - Required columns are present
    - Estimated file size is reasonable

    Args:
        df: DataFrame to validate
        resource_type: Type of resource being exported (for logging)
        required_columns: List of columns that must be present
        dry_run: If True, only validate without actually exporting

    Returns:
        tuple: (is_valid, error_message)

    Example:
        >>> df = collect_ec2_instances(region)
        >>> is_valid, error = utils.validate_export(df, 'EC2', required_columns=['InstanceId'])
        >>> if not is_valid:
        >>>     utils.log_error(f"Validation failed: {error}")
        >>>     return
    """

    # Check if DataFrame is None or empty
    if df is None or df.empty:
        error_msg = f"No {resource_type} resources found to export"
        log_warning(error_msg)
        return False, error_msg

    # Check required columns
    if required_columns:
        missing_cols = [col for col in required_columns if col not in df.columns]
        if missing_cols:
            error_msg = f"Missing required columns: {', '.join(missing_cols)}"
            log_error(error_msg)
            return False, error_msg

    # Estimate file size
    estimated_size = _estimate_excel_size(df)
    log_info(f"Estimated export size: {format_bytes(estimated_size)}")

    # Warn if file is very large
    if estimated_size > 100 * 1024 * 1024:  # 100 MB
        log_warning(f"Large export detected ({format_bytes(estimated_size)}). Consider filtering data.")

    # Log summary
    log_info(f"Validation summary for {resource_type}:")
    log_info(f"  Rows: {len(df)}")
    log_info(f"  Columns: {len(df.columns)}")
    log_info(f"  Estimated size: {format_bytes(estimated_size)}")

    if dry_run:
        log_info("DRY-RUN MODE: Validation complete, skipping actual export")
        log_info(f"Would export {len(df)} {resource_type} resources")
        return True, "Dry-run validation passed"

    return True, "Validation passed"

# =============================================================================
# CONFIG — Configuration singleton and account-mapping utilities
# (folded in from sslib/config.py — Issue #177)
# =============================================================================

# ---------------------------------------------------------------------------
# Module-level state (config singleton)
# ---------------------------------------------------------------------------

ACCOUNT_MAPPINGS: dict[str, str] = {}
CONFIG_DATA: dict[str, Any] = {}
_CONFIG_LOADED: bool = False
_CONFIG_LOCK: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# STS credential cache — keyed by (role_arn, region), stores (creds_dict, expiry)
# ---------------------------------------------------------------------------

_STS_CACHE: dict[tuple[str, Optional[str]], tuple[dict[str, str], datetime.datetime]] = {}
_STS_CACHE_LOCK: threading.Lock = threading.Lock()
_STS_CACHE_REFRESH_MARGIN: datetime.timedelta = datetime.timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _config_path() -> Path:
    """Return the absolute path to config.json (sibling of utils.py)."""
    return Path(__file__).parent / "config.json"


# ---------------------------------------------------------------------------
# Account ID validation
# ---------------------------------------------------------------------------


def is_valid_aws_account_id(account_id: Union[str, int]) -> bool:
    """
    Check if a string is a valid AWS account ID (12 digits).

    Args:
        account_id: The account ID to check

    Returns:
        bool: True if valid, False otherwise
    """
    pattern = re.compile(r"^\d{12}$")
    return bool(pattern.match(str(account_id)))


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config() -> tuple[dict[str, str], dict[str, Any]]:
    """
    Load configuration from config.json file.

    Returns:
        tuple: (ACCOUNT_MAPPINGS, CONFIG_DATA)
    """
    global ACCOUNT_MAPPINGS, CONFIG_DATA

    try:
        config_file = _config_path()

        if config_file.exists():
            with open(config_file, encoding="utf-8") as f:
                CONFIG_DATA = json.load(f)

            if "account_mappings" in CONFIG_DATA:
                ACCOUNT_MAPPINGS = CONFIG_DATA["account_mappings"]
                logging.getLogger(__name__).debug(
                    "Loaded %d account mappings from config.json", len(ACCOUNT_MAPPINGS)
                )

            logging.getLogger(__name__).debug("Configuration loaded successfully")
        else:
            logging.getLogger(__name__).warning(
                "config.json not found. Using default AWS configuration."
            )

            default_config = {
                "__comment": "StratusScan Configuration - Customize this file for your environment",
                "account_mappings": {},
                "organization_name": "YOUR-ORGANIZATION",
                "default_regions": ["us-east-1", "us-west-2", "us-west-1", "eu-west-1"],
                "aws_environment": "production",
                "resource_preferences": {
                    "ec2": {
                        "default_filter": "all",
                        "include_stopped": True,
                        "default_region": "us-east-1",
                    },
                    "vpc": {
                        "default_export_type": "all",
                        "default_region": "us-east-1",
                    },
                    "compute_optimizer": {
                        "enabled": True,
                        "note": "Available in commercial AWS",
                    },
                },
                "enabled_services": {
                    "trusted_advisor": {
                        "enabled": True,
                        "note": "Available in commercial AWS",
                    },
                    "cost_explorer": {
                        "enabled": True,
                        "note": "Available in commercial AWS",
                    },
                },
            }

            try:
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(default_config, f, indent=2)

                msg = (
                    f"Created default config.json at {config_file}. "
                    "Please run 'python configure.py' to set your account mappings and preferences."
                )
                logging.getLogger(__name__).info("[StratusScan] %s", msg)

                CONFIG_DATA = default_config
                ACCOUNT_MAPPINGS = {}
            except Exception as e:
                logging.getLogger(__name__).error("Failed to create default config.json: %s", e)

    except Exception as e:
        logging.getLogger(__name__).error("Error loading configuration: %s", e)

    return ACCOUNT_MAPPINGS, CONFIG_DATA


def get_config() -> tuple[dict[str, str], dict[str, Any]]:
    """
    Lazy-load configuration. First call loads from disk; subsequent calls return cached values.
    Thread-safe: uses _CONFIG_LOCK to prevent concurrent initialization.

    Returns:
        tuple: (ACCOUNT_MAPPINGS, CONFIG_DATA)
    """
    global _CONFIG_LOADED, ACCOUNT_MAPPINGS, CONFIG_DATA
    with _CONFIG_LOCK:
        if not _CONFIG_LOADED:
            ACCOUNT_MAPPINGS, CONFIG_DATA = load_config()
            _CONFIG_LOADED = True
    return ACCOUNT_MAPPINGS, CONFIG_DATA


# ---------------------------------------------------------------------------
# Config value accessors
# ---------------------------------------------------------------------------


def config_value(key: str, default: Any = None, section: Optional[str] = None) -> Any:
    """
    Get a value from the configuration.

    Args:
        key: Configuration key
        default: Default value if key is not found
        section: Optional section in the configuration

    Returns:
        The configuration value or default
    """
    _, cfg = get_config()
    if not cfg:
        return default

    try:
        if section:
            if section in cfg and key in cfg[section]:
                return cfg[section][key]
        else:
            if key in cfg:
                return cfg[key]
    except Exception as e:
        logging.getLogger(__name__).warning("Error reading config value '%s': %s", key, e)

    return default


def get_resource_preference(resource_type: str, preference: str, default: Any = None) -> Any:
    """
    Get a resource-specific preference from the configuration.

    Args:
        resource_type: Type of resource (e.g., 'ec2', 'vpc')
        preference: Preference name
        default: Default value if preference is not found

    Returns:
        The preference value or default
    """
    _, cfg = get_config()
    if "resource_preferences" in cfg:
        resource_prefs = cfg["resource_preferences"]
        if resource_type in resource_prefs and preference in resource_prefs[resource_type]:
            return resource_prefs[resource_type][preference]

    return default


# ---------------------------------------------------------------------------
# Account mapping management
# ---------------------------------------------------------------------------


def add_account_mapping(account_id: str, account_name: str) -> bool:
    """
    Add a new account mapping to the configuration.

    Args:
        account_id: AWS account ID
        account_name: Account name

    Returns:
        bool: True if successful, False otherwise
    """
    if not is_valid_aws_account_id(account_id):
        logging.getLogger(__name__).error("Invalid AWS account ID: %s", account_id)
        return False

    try:
        # Trigger lazy load outside the lock to avoid re-entrant lock deadlock
        get_config()

        config_file = _config_path()

        # Acquire lock only around file I/O to avoid deadlock with get_config()
        with _CONFIG_LOCK:
            if config_file.exists():
                with open(config_file, encoding="utf-8") as f:
                    config = json.load(f)

                if "account_mappings" not in config:
                    config["account_mappings"] = {}

                config["account_mappings"][account_id] = account_name

                # Atomic write: serialize to .tmp then os.replace for crash safety
                tmp_path = config_file.with_suffix(".json.tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2)
                os.replace(tmp_path, config_file)

                # Update in-memory cache only after successful file write
                ACCOUNT_MAPPINGS[account_id] = account_name

                logging.getLogger(__name__).info(
                    "Added account mapping: %s → %s", account_id, account_name
                )
                return True
            else:
                logging.getLogger(__name__).error("config.json not found")
                return False

    except Exception as e:
        logging.getLogger(__name__).error("Failed to add account mapping: %s", e)
        return False


def get_account_name(account_id: str, default: str = "UNKNOWN-ACCOUNT") -> str:
    """
    Get account name from account ID using configured mappings.

    Args:
        account_id: The AWS account ID
        default: Default value to return if account_id is not found in mappings

    Returns:
        str: The account name or default value
    """
    mappings, _ = get_config()
    return mappings.get(account_id, default)


def get_account_name_formatted(owner_id: str) -> str:
    """
    Get the formatted account name with ID from the owner ID.

    Args:
        owner_id: The AWS account owner ID

    Returns:
        str: Formatted as "ACCOUNT-NAME (ID)" if mapping exists, otherwise just the ID
    """
    mappings, _ = get_config()
    if owner_id in mappings:
        return f"{mappings[owner_id]} ({owner_id})"
    return owner_id


# ---------------------------------------------------------------------------
# Cross-account role management
# ---------------------------------------------------------------------------

_ROLE_ARN_RE = re.compile(
    r"^arn:(aws|aws-us-gov):iam::\d{12}:role/.+$"
)


def get_cross_account_roles() -> dict[str, str]:
    """
    Return the cross_account_roles map from config (account_id → role_arn).

    Returns:
        Dict[str, str]: Mapping of account_id to role ARN. Empty dict if not configured.
    """
    _, cfg = get_config()
    roles = cfg.get("cross_account_roles", {})
    # Filter comment keys — only return valid 12-digit account IDs
    return {k: v for k, v in roles.items() if is_valid_aws_account_id(k)}


def add_cross_account_role(account_id: str, role_arn: str) -> bool:
    """
    Add or update a cross-account role mapping in config.json.

    Mirrors add_account_mapping() — atomic write (tmp + os.replace) with
    in-memory cache update after successful file write.

    Args:
        account_id: 12-digit AWS account ID of the target account.
        role_arn: IAM role ARN to assume (must match arn:(aws|aws-us-gov):iam::...).

    Returns:
        bool: True on success, False on validation failure or I/O error.
    """
    log = logging.getLogger(__name__)

    if not is_valid_aws_account_id(account_id):
        log.error("Invalid AWS account ID: %s", _scrub_log(account_id))
        return False

    if not _ROLE_ARN_RE.match(role_arn):
        log.error(
            "Invalid role ARN format: %s — expected arn:(aws|aws-us-gov):iam::<12-digit-id>:role/<name>",
            _scrub_log(role_arn),
        )
        return False

    try:
        # Ensure config is loaded before acquiring lock
        get_config()

        config_file = _config_path()

        with _CONFIG_LOCK:
            if not config_file.exists():
                log.error("config.json not found — cannot add cross-account role")
                return False

            with open(config_file, encoding="utf-8") as f:
                config = json.load(f)

            if "cross_account_roles" not in config:
                config["cross_account_roles"] = {}

            config["cross_account_roles"][account_id] = role_arn

            tmp_path = config_file.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            os.replace(tmp_path, config_file)

            # Update in-memory cache after successful write
            CONFIG_DATA.setdefault("cross_account_roles", {})[account_id] = role_arn

        log.info("Added cross-account role: %s → %s", _scrub_log(account_id), _scrub_log(role_arn))
        return True

    except Exception as e:
        log.error("Failed to add cross-account role: %s", e)
        return False


def remove_cross_account_role(account_id: str) -> bool:
    """
    Remove a cross-account role mapping from config.json.

    Atomic write (tmp + os.replace) with in-memory cache update.

    Args:
        account_id: 12-digit AWS account ID whose role mapping to remove.

    Returns:
        bool: True on success, False if not found or on I/O error.
    """
    log = logging.getLogger(__name__)

    if not is_valid_aws_account_id(account_id):
        log.error("Invalid AWS account ID: %s", _scrub_log(account_id))
        return False

    try:
        get_config()

        config_file = _config_path()

        with _CONFIG_LOCK:
            if not config_file.exists():
                log.error("config.json not found — cannot remove cross-account role")
                return False

            with open(config_file, encoding="utf-8") as f:
                config = json.load(f)

            roles = config.get("cross_account_roles", {})
            if account_id not in roles:
                log.warning("No cross-account role found for account %s", _scrub_log(account_id))
                return False

            del roles[account_id]
            config["cross_account_roles"] = roles

            tmp_path = config_file.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            os.replace(tmp_path, config_file)

            # Update in-memory cache
            CONFIG_DATA.get("cross_account_roles", {}).pop(account_id, None)

        log.info("Removed cross-account role for account %s", _scrub_log(account_id))
        return True

    except Exception as e:
        log.error("Failed to remove cross-account role: %s", e)
        return False


# =============================================================================
# CONCURRENCY — Concurrent region scanning and pagination utilities
# (folded in from sslib/concurrency.py — Issue #177)
# =============================================================================

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class ConcurrentScanningError(Exception):
    """Raised when concurrent scanning encounters too many errors."""
    pass


# ---------------------------------------------------------------------------
# Multi-region scanning
# ---------------------------------------------------------------------------


def scan_regions_concurrent(
    regions: list[str],
    scan_function: Callable[[str], Any],
    max_workers: Optional[int] = None,
    show_progress: Optional[bool] = True,
    fallback_on_error: Optional[bool] = None,
    collect_failures: bool = False,
) -> Any:
    """
    Scan multiple AWS regions concurrently with automatic fallback to sequential.

    This function dramatically improves performance for multi-region exports by
    scanning regions in parallel instead of sequentially. It includes intelligent
    error handling with automatic fallback to sequential scanning if too many errors
    occur (typically due to API rate limiting).

    Args:
        regions: List of AWS regions to scan
        scan_function: Function that takes a region and returns data.
                      Function should handle its own AWS client creation.
        max_workers: Maximum concurrent workers (default: from config or 4)
        show_progress: Show progress as regions complete (default: True)
        fallback_on_error: Fallback to sequential on errors (default: from config or True)
        collect_failures: When True, also return the list of regions whose
                      scan_function raised. **Requires scan_function to raise on
                      failure** rather than swallow-and-return-empty — otherwise a
                      failed region is silently indistinguishable from an empty one
                      (the silent-data-loss bug; see Issue #233). Default False
                      preserves the legacy return type for existing callers.

    Returns:
        - When ``collect_failures`` is False (default): ``list`` of results from
          all regions (legacy behavior, unchanged).
        - When ``collect_failures`` is True: a ``(results, failed_regions)`` tuple,
          where ``failed_regions`` is a list of ``(region, error_message)`` tuples
          for regions whose scan raised. ``results`` holds only the regions that
          succeeded.

    Example:
        >>> def collect_region_instances(region):
        ...     ec2 = get_boto3_client('ec2', region_name=region)
        ...     return ec2.describe_instances()['Reservations']
        >>> results = scan_regions_concurrent(regions, collect_region_instances)
        >>> # Failure-aware form (scan_function must raise on error):
        >>> results, failed = scan_regions_concurrent(
        ...     regions, collect_region_instances, collect_failures=True)
        >>> if failed:
        ...     utils.report_collection_failures(account, "ec2", failed)

    Note:
        - Automatically loads settings from config.json (advanced_settings)
        - Falls back to sequential scanning if concurrent scanning fails
        - Each thread gets its own boto3 client (thread-safe)
    """
    # Load settings from config
    _, config = get_config()
    advanced = config.get("advanced_settings", {})
    concurrent_config = advanced.get("concurrent_scanning", {})

    if max_workers is None:
        max_workers = concurrent_config.get("max_workers", 4)

    if fallback_on_error is None:
        fallback_on_error = concurrent_config.get("fallback_on_error", True)

    if not concurrent_config.get("enabled", True):
        logging.getLogger(__name__).info(
            "Concurrent scanning disabled in config, using sequential scanning"
        )
        return _scan_regions_sequential(
            regions, scan_function, show_progress, collect_failures=collect_failures
        )

    try:
        logging.getLogger(__name__).info(
            "Scanning %d region(s) concurrently (max_workers=%d)", len(regions), max_workers
        )

        results = []
        failed: list[tuple[str, str]] = []
        completed = 0
        total = len(regions)
        error_count = 0

        if show_progress:
            logging.getLogger(__name__).info("Scanning %d region(s) concurrently...", total)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_region = {
                executor.submit(scan_function, region): region for region in regions
            }

            for future in as_completed(future_to_region):
                region = future_to_region[future]
                try:
                    result = future.result()
                    results.append(result)
                    completed += 1

                    if show_progress:
                        logging.getLogger(__name__).info(
                            "[%d/%d] %s done", completed, total, region
                        )

                except Exception as e:
                    error_count += 1
                    failed.append((region, str(e)))
                    logging.getLogger(__name__).error("Error scanning region %s: %s", region, e)

                    if fallback_on_error and error_count >= max(2, total // 2):
                        logging.getLogger(__name__).warning(
                            "Multiple concurrent scanning errors detected (%d errors)", error_count
                        )
                        raise ConcurrentScanningError(
                            f"Too many concurrent errors: {error_count}"
                        ) from e

                    completed += 1

        return (results, failed) if collect_failures else results

    except ConcurrentScanningError:
        if fallback_on_error:
            logging.getLogger(__name__).warning(
                "Falling back to sequential scanning due to concurrent errors"
            )
            logging.getLogger(__name__).warning(
                "This may indicate API rate limiting or network issues"
            )
            logging.getLogger(__name__).warning(
                "To disable concurrent scanning, run: python advanced_settings.py"
            )
            # The sequential retry re-runs every region, so its (results, failed)
            # supersede the partial concurrent tallies above.
            return _scan_regions_sequential(
                regions, scan_function, show_progress, collect_failures=collect_failures
            )
        else:
            raise

    except Exception as e:
        if fallback_on_error:
            logging.getLogger(__name__).error(
                "Unexpected error in concurrent scanning, falling back to sequential: %s", e
            )
            logging.getLogger(__name__).warning(
                "To disable concurrent scanning, run: python advanced_settings.py"
            )
            return _scan_regions_sequential(
                regions, scan_function, show_progress, collect_failures=collect_failures
            )
        else:
            raise


def _scan_regions_sequential(
    regions: list[str],
    scan_function: Callable[[str], Any],
    show_progress: bool = True,
    collect_failures: bool = False,
) -> Any:
    """
    Fallback: Scan regions sequentially (one at a time).

    This is the traditional method used in all scripts.
    Used as fallback when concurrent scanning fails.

    Args:
        regions: List of AWS regions to scan
        scan_function: Function that takes a region and returns data
        show_progress: Show progress as regions complete
        collect_failures: When True, return ``(results, failed_regions)`` instead
            of just ``results``; see :func:`scan_regions_concurrent`.

    Returns:
        list, or ``(results, failed_regions)`` when ``collect_failures`` is True.
    """
    logging.getLogger(__name__).info("Scanning %d region(s) sequentially", len(regions))

    results = []
    failed: list[tuple[str, str]] = []
    total = len(regions)

    for i, region in enumerate(regions, 1):
        try:
            if show_progress:
                logging.getLogger(__name__).info("[%d/%d] Scanning %s...", i, total, region)

            result = scan_function(region)
            results.append(result)

        except Exception as e:
            failed.append((region, str(e)))
            logging.getLogger(__name__).error("Error scanning region %s: %s", region, e)

    return (results, failed) if collect_failures else results


def write_failure_marker(
    account_name: str,
    resource_type: str,
    failed_scopes: list,
) -> Optional[str]:
    """
    Write a ``{ACCOUNT}-{resource_type}-FAILED-{date}.txt`` marker into the output
    directory recording which scopes (regions or account-level steps) failed to
    collect.

    This is the visible signal a downstream consumer needs to tell a *failed*
    export apart from a *genuinely empty* account. Without it, a scope that errors
    is indistinguishable from a scope that legitimately has no resources — the
    silent-data-loss bug (Issue #231 / #233).

    Args:
        account_name: AWS account name (used in the filename).
        resource_type: Resource slug matching the export filename (e.g.
            ``"rds-instances"``, ``"ec2"``, ``"vpc"``).
        failed_scopes: List of ``(scope, error_message)`` tuples, where ``scope``
            is a region name or a logical collection step.

    Returns:
        Path to the marker file as a string, or ``None`` if it could not be
        written (marker-writing never masks the underlying failure).
    """
    try:
        today = get_export_date()
        marker_name = f"{account_name}-{resource_type}-FAILED-{today}.txt"
        marker_path = get_output_filepath(marker_name)
        lines = [
            f"{resource_type.upper()} EXPORT FAILED FOR ONE OR MORE SCOPES",
            f"Account: {account_name}",
            f"Timestamp: {get_log_timestamp()}",
            "",
            "The scopes below raised an error during collection. Their data is "
            "MISSING or INCOMPLETE in any exported spreadsheet. Do NOT treat a "
            "missing file/rows for these scopes as 'no resources' — re-run the export.",
            "",
        ]
        for scope, error in failed_scopes:
            lines.append(f"  - {scope}: {error}")
        marker_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log_error(f"Wrote failure marker: {marker_path}")
        return str(marker_path)
    except Exception as e:
        # Never let marker-writing itself mask the underlying failure.
        log_error("Could not write failure marker", e)
        return None


def report_collection_failures(
    account_name: str,
    resource_type: str,
    failed_scopes: list,
) -> Optional[str]:
    """
    Log a failure summary and write a failure marker for a partially-failed export.

    Shared by exporters so a *failed* collection is never silently collapsed into
    "no resources." Does NOT ``sys.exit`` or ``print`` — deciding the process exit
    code and any user-facing message is the caller's (CLI-layer) responsibility.

    Args:
        account_name: AWS account name.
        resource_type: Resource slug matching the export filename.
        failed_scopes: List of ``(scope, error_message)`` tuples.

    Returns:
        The marker path (str) if failures were reported, else ``None`` when
        ``failed_scopes`` is empty.
    """
    if not failed_scopes:
        return None
    scopes = ", ".join(str(scope) for scope, _ in failed_scopes)
    log_error(
        f"{resource_type}: collection FAILED for {len(failed_scopes)} scope(s): "
        f"{scopes}. Exported data is INCOMPLETE."
    )
    return write_failure_marker(account_name, resource_type, failed_scopes)


# ---------------------------------------------------------------------------
# Pagination helpers
# ---------------------------------------------------------------------------


def paginate_with_progress(
    client,
    operation: str,
    operation_label: str = "resources",
    **kwargs,
):
    """
    Paginate AWS API calls with progress tracking (Phase 4B optimization).

    This generator function provides visibility into pagination progress for
    large datasets. Particularly useful for accounts with 1000+ resources.

    Args:
        client: Boto3 client
        operation: API operation name (e.g., 'describe_instances')
        operation_label: User-friendly label for logging (e.g., 'EC2 instances')
        **kwargs: Arguments to pass to paginate()

    Yields:
        Pages from the paginator

    Example:
        >>> ec2 = get_boto3_client('ec2', region_name='us-east-1')
        >>> for page in paginate_with_progress(ec2, 'describe_instances', 'EC2 instances'):
        ...     process(page['Reservations'])
    """
    _, config = get_config()
    advanced = config.get("advanced_settings", {})
    progress_config = advanced.get("progress_display", {})
    show_pagination = progress_config.get("show_pagination_progress", False)

    paginator = client.get_paginator(operation)
    logging.getLogger(__name__).debug("Streaming %s pages...", operation_label)

    page_num = 0
    for page in paginator.paginate(**kwargs):
        page_num += 1
        if show_pagination:
            logging.getLogger(__name__).debug(
                "Processing page %d of %s", page_num, operation_label
            )
        yield page

    logging.getLogger(__name__).info("Processed %d page(s) of %s", page_num, operation_label)


def build_dataframe_in_batches(
    data: list[dict],
    batch_size: int = 1000,
):
    """
    Build DataFrame from large data lists in batches for memory efficiency (Phase 4B).

    For datasets with 10,000+ resources, building DataFrames in batches reduces
    memory spikes and improves performance.

    Args:
        data: List of dictionaries (resource data)
        batch_size: Number of rows per batch (default: 1000)

    Returns:
        DataFrame with all data

    Example:
        >>> resources = [{'id': i, 'name': f'resource-{i}'} for i in range(10000)]
        >>> df = build_dataframe_in_batches(resources, batch_size=1000)

    Note:
        - Small datasets (<= batch_size) are processed normally
        - Large datasets are split into batches, converted separately, then concatenated
        - Reduces peak memory usage by 20-30% for large exports
    """
    import pandas as pd
    if len(data) <= batch_size:
        return pd.DataFrame(data)

    batches = []
    for i in range(0, len(data), batch_size):
        batch = data[i : i + batch_size]
        batches.append(pd.DataFrame(batch))
        logging.getLogger(__name__).debug(
            "Created batch %d (%d rows)", i // batch_size + 1, len(batch)
        )

    logging.getLogger(__name__).debug("Concatenating %d batches...", len(batches))
    return pd.concat(batches, ignore_index=True)


# =============================================================================
# AWS_CLIENT — FIPS-aware boto3 client factory and partition/region utilities
# (folded in from sslib/aws_client.py — Issue #177)
# =============================================================================

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_REGIONS = ["us-east-1", "us-west-2", "us-west-1", "eu-west-1"]
_GOVCLOUD_DEFAULT_REGIONS = ["us-gov-west-1", "us-gov-east-1"]
_AWS_PARTITION = "aws"

# ---------------------------------------------------------------------------
# Account info cache (session-level, thread-safe)
# ---------------------------------------------------------------------------

_account_info_cache: Optional[tuple[str, str, str]] = None
_account_info_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Environment / automation helpers
# ---------------------------------------------------------------------------


def is_auto_run() -> bool:
    """
    Check if StratusScan is running in non-interactive automation mode.

    Returns:
        bool: True if STRATUSSCAN_AUTO_RUN environment variable is set to 1/true/yes
    """
    return os.environ.get("STRATUSSCAN_AUTO_RUN", "").lower() in ("1", "true", "yes")


def get_auto_regions() -> Optional[list[str]]:
    """
    Get the list of regions from the STRATUSSCAN_REGIONS environment variable.

    Returns:
        Optional[List[str]]: List of region strings, or None if not set
    """
    val = os.environ.get("STRATUSSCAN_REGIONS", "")
    return [r.strip() for r in val.split(",") if r.strip()] if val else None


def parse_script_args(script_description: str) -> "argparse.Namespace":
    """
    Parse standard CLI arguments for an exporter script.

    Must be called at the top of each exporter script (after ``import utils``)
    to populate the module-level ``_SCRIPT_ARGS`` store.  Subsequent calls to
    ``prompt_region_selection()``, ``prompt_for_confirmation()``,
    ``get_aws_session()``, and ``create_export_filename()`` read from this
    store automatically — no per-script wiring required.

    Uses ``parse_known_args()`` so that pytest's own argv does not cause
    errors when test files import exporter modules.

    Args:
        script_description: One-line description shown in ``--help`` output.

    Returns:
        argparse.Namespace with attributes:
            region, regions, all_regions, profile, output_dir, yes
    """
    import argparse  # lazy import — keeps argparse out of the global namespace

    global _SCRIPT_ARGS

    parser = argparse.ArgumentParser(description=script_description)

    region_group = parser.add_mutually_exclusive_group()
    region_group.add_argument(
        "--region",
        help="Single AWS region to scan (e.g. us-east-1)",
    )
    region_group.add_argument(
        "--regions",
        help="Comma-separated list of AWS regions to scan (e.g. us-east-1,us-west-2)",
    )
    region_group.add_argument(
        "--all-regions",
        action="store_true",
        dest="all_regions",
        help="Scan all available regions for the detected partition",
    )

    parser.add_argument(
        "--profile",
        help="AWS named profile to use for authentication",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        dest="output_dir",
        help="Output directory for exported files (default: output/)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompts (non-interactive mode)",
    )

    args, _ = parser.parse_known_args()
    _SCRIPT_ARGS = args
    return args


# ---------------------------------------------------------------------------
# Region validation
# ---------------------------------------------------------------------------


def is_aws_region(region: str) -> bool:
    """
    Check if a region is a valid AWS region.

    Args:
        region: AWS region name

    Returns:
        bool: True if valid AWS region, False otherwise
    """
    # Pattern supports: us-east-1, us-gov-west-1, ap-southeast-2, etc.
    # Digit is limited to [1-9] (no AWS region uses 0 or multi-digit numbers).
    pattern = r"^[a-z]{2}(-gov)?-[a-z]+-[1-9]$"
    return bool(re.match(pattern, region)) or region in _DEFAULT_REGIONS


def validate_aws_region(region: str) -> bool:
    """
    Validate that a region is a valid AWS region and provide helpful error if not.

    .. deprecated::
        Use :func:`is_aws_region` for pure validation.  ``validate_aws_region``
        couples validation with error logging; prefer calling ``is_aws_region``
        and logging at the call site instead.

    Args:
        region: AWS region name

    Returns:
        bool: True if valid, False otherwise
    """
    warnings.warn(
        "validate_aws_region() is deprecated; use is_aws_region() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    if region == "all":
        return True

    if not is_aws_region(region):
        logging.getLogger(__name__).error("Invalid AWS region: %s", region)
        logging.getLogger(__name__).error(
            "Valid AWS regions include: us-east-1, us-west-1, us-west-2, eu-west-1, ap-southeast-1"
        )
        return False

    return True


def get_aws_regions() -> list[str]:
    """
    Get list of default AWS regions for the current partition.
    Partition-aware: Returns GovCloud regions when in GovCloud, Commercial otherwise.

    Returns:
        list: List of AWS region names
    """
    partition = detect_partition()
    return get_partition_regions(partition)


# ---------------------------------------------------------------------------
# Partition detection
# ---------------------------------------------------------------------------


def detect_partition(region_name: Optional[str] = None) -> str:
    """
    Detect AWS partition from region or credentials.

    Args:
        region_name: Optional region to check

    Returns:
        str: 'aws' or 'aws-us-gov'
    """
    if region_name:
        if region_name.startswith(("us-gov-", "us-gov")):
            return "aws-us-gov"
        return "aws"

    try:
        session = boto3.Session()

        region = session.region_name or "us-east-1"
        if region.startswith("us-gov"):
            return "aws-us-gov"

        sts = session.client("sts")
        arn = sts.get_caller_identity()["Arn"]
        if "aws-us-gov" in arn:
            return "aws-us-gov"

        return "aws"
    except Exception as e:
        logging.getLogger(__name__).warning(
            "Could not detect partition: %s, assuming commercial AWS", e
        )
        return "aws"


# ---------------------------------------------------------------------------
# Session and client factory
# ---------------------------------------------------------------------------


def _assume_role_cached(
    role_arn: str,
    region_name: Optional[str] = None,
    profile_name: Optional[str] = None,
) -> dict[str, str]:
    """
    Assume an IAM role via STS and return temporary credentials, using an
    in-memory cache keyed by (role_arn, region_name).  Credentials are
    refreshed automatically when within 5 minutes of expiry.

    Args:
        role_arn: Full IAM role ARN to assume.
        region_name: AWS region for the STS call (None = default).
        profile_name: Named profile for the caller session.

    Returns:
        Dict with keys: AccessKeyId, SecretAccessKey, SessionToken.

    Raises:
        ValueError: If the role ARN partition mismatches the detected partition.
        botocore.exceptions.ClientError: On STS API errors (after retries).
    """
    log = logging.getLogger(__name__)
    cache_key: tuple[str, Optional[str]] = (role_arn, region_name)

    # Validate partition alignment before any STS call
    arn_partition = role_arn.split(":")[1] if role_arn.startswith("arn:") else ""
    caller_partition = detect_partition(region_name)
    if arn_partition and arn_partition != caller_partition:
        raise ValueError(
            f"Role ARN partition '{arn_partition}' does not match detected partition "
            f"'{caller_partition}' for region '{region_name}'. Use a "
            f"{'arn:aws-us-gov:' if caller_partition == 'aws-us-gov' else 'arn:aws:'} "
            f"role ARN for this environment."
        )

    now = datetime.datetime.now(tz=datetime.timezone.utc)

    with _STS_CACHE_LOCK:
        if cache_key in _STS_CACHE:
            creds, expiry = _STS_CACHE[cache_key]
            if now < expiry - _STS_CACHE_REFRESH_MARGIN:
                log.debug("STS cache hit for role %s", role_arn)
                return creds

        # Extract account_id from ARN for session name
        try:
            account_id = role_arn.split(":")[4]
        except IndexError:
            account_id = "unknown"

        session_name = f"stratusscan-{account_id}"[:64]

        # Build caller session (uses profile / env creds, not the assumed role)
        profile = profile_name or (_SCRIPT_ARGS.profile if _SCRIPT_ARGS else None)
        caller_session = boto3.Session(region_name=region_name, profile_name=profile)

        # FIPS for GovCloud STS — must be set on the botocore Config, not as a
        # client() kwarg (boto3 rejects it there).
        sts_config = None
        if region_name and region_name.startswith("us-gov-"):
            sts_config = Config(use_fips_endpoint=True)

        sts_client = caller_session.client("sts", config=sts_config)

        # Assume role with exponential backoff for ThrottlingException
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                response = sts_client.assume_role(
                    RoleArn=role_arn,
                    RoleSessionName=session_name,
                    DurationSeconds=3600,
                )
                break
            except botocore.exceptions.ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code", "")
                if error_code in ("Throttling", "ThrottlingException") and attempt < max_attempts - 1:
                    backoff = (2 ** attempt) * 0.5
                    log.warning(
                        "STS assume_role throttled (attempt %d/%d); retrying in %.1fs",
                        attempt + 1,
                        max_attempts,
                        backoff,
                    )
                    time.sleep(backoff)
                else:
                    raise

        raw = response["Credentials"]
        creds = {
            "AccessKeyId": raw["AccessKeyId"],
            "SecretAccessKey": raw["SecretAccessKey"],
            "SessionToken": raw["SessionToken"],
        }
        expiry = raw["Expiration"]
        # Expiration may already be timezone-aware
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=datetime.timezone.utc)

        _STS_CACHE[cache_key] = (creds, expiry)
        log.info("Assumed role %s (expires %s)", role_arn, expiry.isoformat())
        return creds


def get_aws_session(
    region_name: Optional[str] = None,
    profile_name: Optional[str] = None,
    role_arn: Optional[str] = None,
) -> boto3.Session:
    """
    Create a boto3 session for the specified region.

    When ``role_arn`` is provided the caller's default session is used to call
    STS ``AssumeRole`` and a new session is built from the resulting temporary
    credentials.  A thread-safe cache avoids redundant STS calls; credentials
    are refreshed automatically within 5 minutes of expiry.

    Partition validation: raises ``ValueError`` if the role ARN partition does
    not match the partition implied by ``region_name`` (e.g., passing an
    ``arn:aws-us-gov:`` ARN in a commercial region or vice versa).

    Profile resolution order (when role_arn is None):
      1. ``profile_name`` argument (explicit caller override)
      2. ``_SCRIPT_ARGS.profile`` set by ``parse_script_args()``
      3. boto3 default (AWS_PROFILE env var, ~/.aws/config default)

    Args:
        region_name: AWS region (None = default from config)
        profile_name: AWS named profile (None = use CLI arg or boto3 default)
        role_arn: IAM role ARN to assume for cross-account access (optional)

    Returns:
        boto3.Session: Configured session

    Raises:
        ValueError: If role_arn partition mismatches the detected partition.
    """
    # Env var fallback: allows org-scan subprocess launches to inject a role
    # without modifying any exporter script.
    if role_arn is None:
        env_role = os.environ.get("STRATUSSCAN_ROLE_ARN", "").strip()
        if env_role:
            role_arn = env_role
            logging.getLogger(__name__).debug(
                "STRATUSSCAN_ROLE_ARN env var active: %s", role_arn
            )

    if role_arn:
        creds = _assume_role_cached(role_arn, region_name=region_name, profile_name=profile_name)
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region_name,
        )

    profile = profile_name or (_SCRIPT_ARGS.profile if _SCRIPT_ARGS else None)
    return boto3.Session(region_name=region_name, profile_name=profile)


def get_boto3_client(
    service: str,
    region_name: Optional[str] = None,
    role_arn: Optional[str] = None,
    **kwargs,
) -> BaseClient:
    """
    Create boto3 client with standard configuration including retries.

    Automatically injects ``use_fips_endpoint=True`` for GovCloud regions
    (``us-gov-west-1``, ``us-gov-east-1``). This is a security-critical property
    that must survive any refactoring.

    When ``role_arn`` is provided, credentials are obtained via STS
    ``AssumeRole`` (with caching) before creating the client.

    Args:
        service: AWS service name (e.g., 'ec2', 'iam', 's3')
        region_name: AWS region name (optional)
        role_arn: IAM role ARN for cross-account access (optional)
        **kwargs: Additional arguments to pass to client creation

    Returns:
        boto3.client: Configured boto3 client with retry logic
    """
    sdk_config = config_value("aws_sdk_config", default={})

    retry_config = sdk_config.get("retries", {"max_attempts": 10, "mode": "adaptive"})
    connect_timeout = sdk_config.get("connect_timeout", 10)
    read_timeout = sdk_config.get("read_timeout", 60)

    config_kwargs: dict[str, Any] = {
        "retries": retry_config,
        "connect_timeout": connect_timeout,
        "read_timeout": read_timeout,
    }

    # FIPS injection — GovCloud requires FIPS endpoints. This belongs on the
    # botocore Config, NOT as a client() kwarg (boto3 rejects it there). A
    # caller may still override via kwargs["use_fips_endpoint"].
    fips_override = kwargs.pop("use_fips_endpoint", None)
    if fips_override is not None:
        config_kwargs["use_fips_endpoint"] = fips_override
    elif region_name and region_name.startswith("us-gov-"):
        config_kwargs["use_fips_endpoint"] = True

    config = Config(**config_kwargs)

    session = get_aws_session(region_name, role_arn=role_arn)
    return session.client(service, config=config, **kwargs)


# ---------------------------------------------------------------------------
# ARN utilities
# ---------------------------------------------------------------------------


def build_arn(
    service: str,
    resource: str,
    region: Optional[str] = None,
    account_id: Optional[str] = None,
    partition: Optional[str] = None,
) -> str:
    """
    Build ARN with automatic partition detection.

    Args:
        service: AWS service name
        resource: Resource identifier
        region: AWS region (optional, empty string for global services)
        account_id: AWS account ID (optional, auto-detected if not provided)
        partition: AWS partition (optional, auto-detected if not provided)

    Returns:
        str: Properly formatted AWS ARN
    """
    if not partition:
        partition = detect_partition(region)

    if not account_id:
        try:
            sts = get_boto3_client("sts")
            account_id = sts.get_caller_identity()["Account"]
        except Exception:
            account_id = ""

    if region is None:
        region = ""

    return f"arn:{partition}:{service}:{region}:{account_id}:{resource}"


# ---------------------------------------------------------------------------
# Service availability
# ---------------------------------------------------------------------------


def is_service_available_in_partition(service: str, partition: str = "aws") -> bool:
    """
    Check if an AWS service is available in the specified partition.

    Args:
        service: AWS service name (e.g., 'ec2', 'iam', 's3')
        partition: AWS partition ('aws' or 'aws-us-gov')

    Returns:
        bool: True if service is available in partition, False otherwise
    """
    govcloud_unavailable = {
        "ce",
        "globalaccelerator",
        "trustedadvisor",
        "compute-optimizer",
        "cost-optimization-hub",
        "appstream",
        "chime",
        "sumerian",
        "gamelift",
        "robomaker",
        "cognito-idp",
        "cognito-identity",
        "comprehend",
        "connect",
        "rekognition",
        "bedrock",
    }

    govcloud_limited = {
        "marketplace",
        "organizations",
    }

    if partition == "aws-us-gov":
        if service.lower() in govcloud_unavailable:
            logging.getLogger(__name__).debug(
                "Service %s is not available in AWS GovCloud", service
            )
            return False
        if service.lower() in govcloud_limited:
            logging.getLogger(__name__).debug(
                "Service %s has limited functionality in AWS GovCloud", service
            )

    return True


def is_service_enabled(service_name: str) -> bool:
    """
    Check if a service is enabled in the current AWS environment.

    Args:
        service_name: Name of the AWS service

    Returns:
        bool: True if enabled, False if disabled
    """
    _, cfg = get_config()
    if "disabled_services" in cfg:
        disabled_services = cfg["disabled_services"]
        if service_name in disabled_services:
            return disabled_services[service_name].get("enabled", False)

    return True


def get_service_disability_reason(service_name: str) -> Optional[str]:
    """
    Get the reason why a service is disabled.

    Args:
        service_name: Name of the AWS service

    Returns:
        str: Reason for disability or None if service is enabled
    """
    _, cfg = get_config()
    if "disabled_services" in cfg:
        disabled_services = cfg["disabled_services"]
        if service_name in disabled_services:
            return disabled_services[service_name].get("reason", "Not available")

    return None


# ---------------------------------------------------------------------------
# Region helpers
# ---------------------------------------------------------------------------


def get_partition_regions(partition: str = "aws", all_regions: bool = False) -> list[str]:
    """
    Get available regions for a specific AWS partition.

    Args:
        partition: AWS partition ('aws' or 'aws-us-gov')
        all_regions: If True, query EC2 for all regions; if False, return default subset

    Returns:
        list: List of region names for the partition
    """
    if partition == "aws-us-gov":
        return _GOVCLOUD_DEFAULT_REGIONS
    elif partition == "aws":
        if all_regions:
            try:
                ec2 = get_boto3_client("ec2", region_name="us-east-1")
                response = ec2.describe_regions(AllRegions=True)
                regions = [
                    r["RegionName"]
                    for r in response["Regions"]
                    if r.get("OptInStatus") != "not-opted-in"
                ]
                return sorted(regions)
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "Could not query all regions from EC2, using default list: %s", e
                )
                return _DEFAULT_REGIONS
        else:
            return _DEFAULT_REGIONS
    else:
        logging.getLogger(__name__).error(
            "Unknown partition '%s' — cannot determine valid regions; returning commercial defaults",
            partition,
        )
        return _DEFAULT_REGIONS


def get_partition_default_region(partition: Optional[str] = None) -> str:
    """
    Get the default region for a specific AWS partition.

    Args:
        partition: AWS partition ('aws' or 'aws-us-gov')
                  If not provided, auto-detects from current credentials

    Returns:
        str: Default region for the partition
    """
    if partition is None:
        partition = detect_partition()

    if partition == "aws-us-gov":
        return "us-gov-west-1"
    else:
        return "us-east-1"


def get_default_regions(partition: Optional[str] = None) -> list[str]:
    """
    Get the default AWS regions from configuration.

    Args:
        partition: Optional partition to filter regions ('aws' or 'aws-us-gov')
                  If not provided, uses regions from config.json or auto-detects

    Returns:
        list: List of default AWS region names
    """
    if partition:
        return get_partition_regions(partition)

    _, cfg = get_config()
    config_regions = cfg.get("default_regions", _DEFAULT_REGIONS)

    if config_regions:
        detected_partition = detect_partition(config_regions[0])
        return [r for r in config_regions if detect_partition(r) == detected_partition]

    return config_regions


def get_partition_default_regions(partition: Optional[str] = None) -> list[str]:
    """
    Get the default AWS regions (alias for get_default_regions for consistency).

    .. deprecated::
        Use :func:`get_default_regions` directly — this function is a redundant alias.

    Args:
        partition: Optional partition to filter regions ('aws' or 'aws-us-gov')

    Returns:
        list: List of default AWS region names
    """
    warnings.warn(
        "get_partition_default_regions() is deprecated; use get_default_regions() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return get_default_regions(partition)


# ---------------------------------------------------------------------------
# Credential and region access validation
# ---------------------------------------------------------------------------


def validate_aws_credentials() -> tuple[bool, Optional[str], Optional[str]]:
    """
    Validate AWS credentials.

    Returns:
        tuple: (is_valid, account_id, error_message)
    """
    try:
        sts = get_boto3_client("sts")
        response = sts.get_caller_identity()
        account_id = response["Account"]
        return True, account_id, None
    except Exception as e:
        return False, None, str(e)


def check_aws_region_access(region: str) -> bool:
    """
    Check if a specific AWS region is accessible.

    Args:
        region: AWS region name

    Returns:
        bool: True if accessible, False otherwise
    """
    if not is_aws_region(region):
        return False

    try:
        ec2 = get_boto3_client("ec2", region_name=region)
        ec2.describe_regions(RegionNames=[region])
        return True
    except Exception as e:
        logging.getLogger(__name__).warning("Cannot access region %s: %s", region, e)
        return False


def get_available_aws_regions() -> list[str]:
    """
    Get list of AWS regions that are currently accessible.
    Partition-aware: Returns GovCloud regions when in GovCloud, Commercial otherwise.

    Returns:
        list: List of accessible AWS region names
    """
    partition = detect_partition()
    partition_regions = get_partition_regions(partition)

    available_regions = []

    for region in partition_regions:
        if check_aws_region_access(region):
            available_regions.append(region)
        else:
            logging.getLogger(__name__).warning("AWS region %s is not accessible", region)

    return available_regions


def is_aws_commercial_environment() -> bool:
    """
    Check if we're currently running in an AWS Commercial environment.

    Returns:
        bool: True if in AWS Commercial, False otherwise
    """
    try:
        sts = get_boto3_client("sts")
        caller_arn = sts.get_caller_identity()["Arn"]
        partition = caller_arn.split(":")[1]
        return partition == "aws"
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Cached account info (Phase 4B optimization)
# ---------------------------------------------------------------------------


def get_cached_account_info() -> tuple[str, str, str]:
    """
    Get AWS account info with session-level caching (Phase 4B optimization).

    Returns:
        tuple: (account_id, account_name, partition)

    Note:
        - Cached in _account_info_cache (module-level) only on successful STS call.
          A transient first-call failure returns default values without poisoning
          the cache, so the next call will retry the STS lookup.
        - Thread-safe: _account_info_lock guards both the read and write paths.
        - Uses get_boto3_client() which includes automatic retry logic.
    """
    global _account_info_cache

    if _account_info_cache is not None:
        return _account_info_cache

    with _account_info_lock:
        if _account_info_cache is not None:
            return _account_info_cache

        try:
            sts = get_boto3_client("sts")
            account_id = sts.get_caller_identity()["Account"]
            account_name = get_account_name(account_id, default=f"AWS-ACCOUNT-{account_id}")
            partition = detect_partition()

            _account_info_cache = (account_id, account_name, partition)
            logging.getLogger(__name__).debug(
                "Cached account info: %s (%s) in partition %s",
                account_name,
                account_id,
                partition,
            )
            return _account_info_cache

        except Exception as e:
            logging.getLogger(__name__).error("Failed to get account information: %s", e)
            logging.getLogger(__name__).warning("Using default account values")
            return "UNKNOWN", "UNKNOWN-ACCOUNT", "aws"


# =============================================================================
# COST — Cost estimation utilities
# (folded in from sslib/cost.py — Issue #177)
# =============================================================================

if TYPE_CHECKING:
    # argparse is imported lazily inside parse_script_args() to keep it out of
    # the runtime namespace; declare it here so the "argparse.Namespace"
    # forward-reference annotations resolve for type checkers and linters.
    import argparse

# Path to the reference/ directory (sibling of utils.py)
_REFERENCE_DIR = Path(__file__).parent / "reference"


def _load_pricing_json(filename: str, default: dict[str, float]) -> dict[str, float]:
    """
    Load a flat key→value pricing dict from a JSON file in reference/.

    The JSON file must have a top-level ``rates`` object whose values are
    floats.  Falls back to ``default`` on any I/O or parse error.

    Args:
        filename: JSON filename inside reference/ (e.g. 's3-pricing.json')
        default:  Fallback dict returned on error

    Returns:
        Dict mapping rate keys → float values
    """
    json_path = _REFERENCE_DIR / filename
    try:
        with json_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        rates = data.get("rates", {})
        if rates:
            return {k: float(v) for k, v in rates.items()}
        logging.getLogger(__name__).warning(
            "Pricing JSON %s has no 'rates' key — using built-in defaults", filename
        )
    except FileNotFoundError:
        logging.getLogger(__name__).warning(
            "Pricing JSON not found: %s — using built-in defaults", json_path
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "Error reading pricing JSON %s: %s — using built-in defaults", filename, exc
        )
    return default


# =============================================================================
# PRICING FEED ACCESS (Issue #297)
# =============================================================================


def _pricing_settings() -> dict[str, Any]:
    """
    Read the ``pricing`` block of advanced settings, with defaults for gaps.

    Defaults live in ``pricing_feed`` so the module is usable without a
    config.json at all -- a fresh CloudShell session has none until the wizard
    runs.
    """
    import pricing_feed

    defaults = {
        "live_feed_enabled": pricing_feed.DEFAULT_LIVE_FEED_ENABLED,
        "cache_ttl_hours": pricing_feed.DEFAULT_CACHE_TTL_HOURS,
        "max_feed_bytes": pricing_feed.DEFAULT_MAX_FEED_BYTES,
        "timeout_seconds": pricing_feed.DEFAULT_TIMEOUT_SECONDS,
        "max_seconds": pricing_feed.DEFAULT_MAX_SECONDS,
    }
    try:
        configured = config_value("pricing", {}, section="advanced_settings") or {}
        if isinstance(configured, dict):
            defaults.update({k: v for k, v in configured.items() if k in defaults})
    except Exception as exc:  # noqa: BLE001 - settings must never block an export
        logging.getLogger(__name__).warning(
            "Could not read pricing advanced settings (%s) - using defaults", exc
        )
    return defaults


def get_pricing_data(offer_code: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Return ``(records, provenance)`` for one AWS offer code.

    The one pricing entry point for exporters. Serves the live AWS Price List
    Bulk feed when it is reachable, a version-keyed local cache when it is
    fresh, and the bundled ``reference/`` snapshot otherwise -- all through a
    single code path, so the three differ only in what ``provenance`` reports.

    No value returned here is derived. Every rate came from a feed row, in the
    live case and the bundled case alike, because both are produced by the same
    extractor (see ``pricing_feed.extract_ec2_records`` and
    ``tools/refresh_pricing.py``).

    A fetch failure of any kind -- no egress, blocked DNS, proxy denial,
    timeout, oversized feed -- is not an error. It falls back, records the
    reason in provenance, and the export proceeds.

    Args:
        offer_code: AWS offer code, e.g. ``"AmazonEC2"``.

    Returns:
        ``(records, provenance)``. ``records`` is empty only when the bundled
        snapshot is also unreadable; provenance then reports
        ``source='unavailable'``. Callers must not read an empty map as
        "nothing costs anything".
    """
    import pricing_feed

    settings = _pricing_settings()
    try:
        return pricing_feed.get_pricing(
            offer_code,
            live_feed_enabled=bool(settings["live_feed_enabled"]),
            cache_ttl_hours=float(settings["cache_ttl_hours"]),
            max_feed_bytes=int(settings["max_feed_bytes"]),
            timeout_seconds=float(settings["timeout_seconds"]),
            max_seconds=float(settings["max_seconds"]),
        )
    except Exception as exc:  # noqa: BLE001 - pricing must never sink an export
        logging.getLogger(__name__).warning(
            "Pricing lookup for %s failed entirely: %s", offer_code, exc
        )
        return {}, {
            "source": pricing_feed.SOURCE_UNAVAILABLE,
            "fallback_reason": "pricing lookup raised",
        }


def pricing_provenance_note(provenance: dict[str, Any]) -> str:
    """
    Render a provenance block as one phrase for a workbook's ``Cost Note``.

    Exporters append this to their existing cost note so a reader can tell, per
    row, whether a figure came from a live feed version or a bundled snapshot.
    Issue #296 went unnoticed for seven months because no export said.
    """
    import pricing_feed

    return pricing_feed.provenance_note(provenance or {})


_INSTANCE_SPECS_LOCK = threading.Lock()
_INSTANCE_SPECS_CACHE: Optional[dict[str, dict[str, Any]]] = None


def load_instance_type_specs() -> dict[str, dict[str, Any]]:
    """
    Load static per-instance-type hardware specs from reference/ec2-pricing.json.

    Returns a ``{instance_type: {'vcpu': int|None, 'memory_gib': float|None,
    'architecture': str|None}}`` map. These values are partition-independent —
    an ``m5.xlarge`` has the same vCPU and memory count in GovCloud as in
    commercial — so unlike the pricing blocks this map needs no region argument.

    ``vcpu`` here is the true vCPU count (threads), not physical cores. Callers
    must prefer this over ``CpuOptions.CoreCount`` from a describe-instances
    response, which reports physical cores and under-reports by the SMT factor
    on most instance families (see Issue #259).

    The result is cached; the reference file does not change at runtime. Any
    I/O or parse error yields an empty map, leaving callers to fall back to
    ``ec2:DescribeInstanceTypes``.

    Returns:
        Dict mapping instance type → spec dict. Empty on error.
    """
    global _INSTANCE_SPECS_CACHE

    with _INSTANCE_SPECS_LOCK:
        if _INSTANCE_SPECS_CACHE is not None:
            return _INSTANCE_SPECS_CACHE

        specs: dict[str, dict[str, Any]] = {}
        json_path = _REFERENCE_DIR / "ec2-pricing.json"
        try:
            with json_path.open(encoding="utf-8") as fh:
                data = json.load(fh)
            for instance_type, record in data.get("records", {}).items():
                specs[instance_type] = {
                    "vcpu": record.get("vcpu"),
                    "memory_gib": record.get("memory_gib"),
                    "architecture": record.get("architecture"),
                }
        except FileNotFoundError:
            logging.getLogger(__name__).warning(
                "Instance spec reference not found: %s — falling back to describe_instance_types",
                json_path,
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "Error reading instance spec reference %s: %s — falling back to "
                "describe_instance_types",
                json_path,
                exc,
            )

        _INSTANCE_SPECS_CACHE = specs
        return specs


def _load_rds_instance_pricing() -> dict[str, float]:
    """
    Build an ``{instance_class: hourly_rate_usd}`` map from rds-pricing.json.

    Uses MySQL / us-east-1 on-demand monthly rate ÷ 730 as the hourly baseline.
    Falls back to a small built-in table if rds-pricing.json is missing.
    """
    _defaults: dict[str, float] = {
        "db.t3.micro": 0.017,
        "db.t3.small": 0.034,
        "db.t3.medium": 0.068,
        "db.t3.large": 0.136,
        "db.t3.xlarge": 0.272,
        "db.t3.2xlarge": 0.544,
        "db.m5.large": 0.192,
        "db.m5.xlarge": 0.384,
        "db.m5.2xlarge": 0.768,
        "db.m5.4xlarge": 1.536,
        "db.r5.large": 0.24,
        "db.r5.xlarge": 0.48,
        "db.r5.2xlarge": 0.96,
        "db.r5.4xlarge": 1.92,
    }
    json_path = _REFERENCE_DIR / "rds-pricing.json"
    try:
        with json_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        pricing: dict[str, float] = {}
        for instance_class, info in data.get("records", {}).items():
            monthly = (
                info.get("pricing", {})
                .get("us-east-1", {})
                .get("mysql_on_demand_monthly_usd")
            )
            if monthly is not None:
                pricing[instance_class] = round(monthly / 730, 6)
        if pricing:
            return pricing
        logging.getLogger(__name__).warning(
            "No RDS pricing extracted from rds-pricing.json — using built-in defaults"
        )
    except FileNotFoundError:
        logging.getLogger(__name__).warning(
            "rds-pricing.json not found — using built-in defaults"
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "Error reading rds-pricing.json: %s — using built-in defaults", exc
        )
    return _defaults


# =============================================================================
# EXCEL SIZE ESTIMATION
# =============================================================================


def _estimate_excel_size(df) -> int:
    """
    Estimate Excel file size for a DataFrame.

    Args:
        df: pandas DataFrame

    Returns:
        int: Estimated file size in bytes
    """
    # Rough estimation: 100 bytes per cell + overhead
    num_cells = len(df) * len(df.columns)
    base_size = num_cells * 100

    # Add overhead for Excel formatting
    overhead = base_size * 0.2

    return int(base_size + overhead)


# =============================================================================
# COST ESTIMATION UTILITIES
# =============================================================================


def estimate_rds_monthly_cost(
    instance_class: str,
    engine: str,
    storage_gb: int,
    storage_type: str = "gp2",
    multi_az: bool = False,
) -> dict[str, Any]:
    """
    Estimate monthly cost for RDS database instance.

    NOTE: All pricing figures are based on us-east-1 (N. Virginia) On-Demand rates.
    Actual costs will differ in other regions and under Reserved or Savings Plan pricing.
    For accurate pricing, consult AWS Pricing Calculator or AWS Cost Explorer.

    Args:
        instance_class: RDS instance class (e.g., 'db.t3.micro')
        engine: Database engine (e.g., 'mysql', 'postgres', 'oracle')
        storage_gb: Allocated storage in GB
        storage_type: Storage type ('gp2', 'gp3', 'io1')
        multi_az: Whether Multi-AZ deployment is enabled

    Returns:
        dict: Cost breakdown with instance, storage, and total costs

    Example:
        >>> cost = estimate_rds_monthly_cost('db.t3.micro', 'mysql', 20)
        >>> print(f"Estimated monthly cost: ${cost['total']:.2f}")

    Note:
        - Uses approximate pricing for us-east-1 region
        - Does not include data transfer, backups, or other charges
        - Multi-AZ deployments approximately double instance costs
    """
    instance_pricing = _load_rds_instance_pricing()

    # Storage pricing per GB/month
    _storage_defaults: dict[str, float] = {
        "gp2": 0.115,
        "gp3": 0.08,
        "io1": 0.125,
        "magnetic": 0.10,
    }
    storage_pricing = _load_pricing_json("rds-storage-pricing.json", _storage_defaults)

    # Get instance cost
    hourly_instance_cost = instance_pricing.get(instance_class, 0.10)
    monthly_instance_cost = hourly_instance_cost * 730  # 730 hours per month

    # Apply Multi-AZ multiplier (approximately 2x for instance)
    if multi_az:
        monthly_instance_cost *= 2

    # Get storage cost
    storage_price_per_gb = storage_pricing.get(storage_type, 0.115)
    monthly_storage_cost = storage_gb * storage_price_per_gb

    # Calculate total
    total_monthly_cost = monthly_instance_cost + monthly_storage_cost

    result = {
        "instance_cost": round(monthly_instance_cost, 2),
        "storage_cost": round(monthly_storage_cost, 2),
        "total": round(total_monthly_cost, 2),
        "multi_az_enabled": multi_az,
        "note": "Approximate estimate - see AWS Pricing Calculator for accurate costs",
    }

    logging.getLogger(__name__).debug(
        "RDS cost estimate for %s: $%.2f/month", instance_class, result["total"]
    )
    return result


def estimate_s3_monthly_cost(
    total_size_gb: float,
    storage_class: str = "STANDARD",
    requests_per_month: Optional[int] = None,
) -> dict[str, Any]:
    """
    Estimate monthly cost for S3 storage.

    This provides rough cost estimates for S3 buckets. For accurate pricing,
    consult AWS Pricing Calculator or AWS Cost Explorer.

    Args:
        total_size_gb: Total storage size in GB
        storage_class: S3 storage class ('STANDARD', 'INTELLIGENT_TIERING', 'GLACIER', etc.)
        requests_per_month: Optional number of requests per month

    Returns:
        dict: Cost breakdown with storage, request, and total costs

    Example:
        >>> cost = estimate_s3_monthly_cost(1000, 'STANDARD')
        >>> print(f"Estimated monthly cost: ${cost['total']:.2f}")

    Note:
        - Uses approximate pricing for us-east-1 region
        - Does not include data transfer costs
        - Request costs are minimal unless very high volume
    """
    # S3 storage pricing per GB/month (us-east-1)
    _s3_defaults: dict[str, float] = {
        "STANDARD": 0.023,
        "INTELLIGENT_TIERING": 0.023,
        "STANDARD_IA": 0.0125,
        "ONEZONE_IA": 0.01,
        "GLACIER": 0.004,
        "GLACIER_IR": 0.0036,
        "DEEP_ARCHIVE": 0.00099,
    }
    storage_pricing = _load_pricing_json("s3-pricing.json", _s3_defaults)

    # Request pricing (per 1,000 requests)
    request_pricing = {
        "STANDARD": {"PUT": 0.005, "GET": 0.0004},
        "INTELLIGENT_TIERING": {"PUT": 0.005, "GET": 0.0004},
    }

    # Calculate storage cost
    storage_price_per_gb = storage_pricing.get(storage_class, 0.023)
    monthly_storage_cost = total_size_gb * storage_price_per_gb

    # Calculate request costs (if provided)
    monthly_request_cost = 0.0
    if requests_per_month and storage_class in request_pricing:
        put_requests = requests_per_month * 0.5
        get_requests = requests_per_month * 0.5

        put_cost = (put_requests / 1000) * request_pricing[storage_class]["PUT"]
        get_cost = (get_requests / 1000) * request_pricing[storage_class]["GET"]

        monthly_request_cost = put_cost + get_cost

    # Add monitoring fee for Intelligent-Tiering
    monitoring_cost = 0.0
    if storage_class == "INTELLIGENT_TIERING":
        # $0.0025 per 1,000 objects monitored
        estimated_objects = (total_size_gb * 1024) / 10
        monitoring_cost = (estimated_objects / 1000) * 0.0025

    total_cost = monthly_storage_cost + monthly_request_cost + monitoring_cost

    result = {
        "storage_cost": round(monthly_storage_cost, 2),
        "request_cost": round(monthly_request_cost, 2),
        "monitoring_cost": round(monitoring_cost, 2),
        "total": round(total_cost, 2),
        "storage_class": storage_class,
        "note": "Approximate estimate - does not include data transfer costs",
    }

    logging.getLogger(__name__).debug(
        "S3 cost estimate for %.1fGB (%s): $%.2f/month",
        total_size_gb,
        storage_class,
        result["total"],
    )
    return result


def calculate_nat_gateway_monthly_cost(
    hours_per_month: int = 730,
    data_processed_gb: float = 0.0,
) -> dict[str, Any]:
    """
    Calculate monthly cost for NAT Gateway.

    NAT Gateways have both hourly and data processing charges.

    Args:
        hours_per_month: Number of hours the NAT Gateway is running (default: 730 for full month)
        data_processed_gb: Amount of data processed in GB per month

    Returns:
        dict: Cost breakdown with hourly, data processing, and total costs

    Example:
        >>> cost = calculate_nat_gateway_monthly_cost(730, 500)
        >>> print(f"Estimated monthly cost: ${cost['total']:.2f}")

    Note:
        - Uses pricing for us-east-1 region
        - Actual pricing varies by region
        - Each NAT Gateway incurs these costs independently
    """
    # NAT Gateway pricing (us-east-1)
    _natgw_defaults: dict[str, float] = {
        "hourly": 0.045,
        "data_processing_per_gb": 0.045,
    }
    natgw_pricing = _load_pricing_json("natgw-pricing.json", _natgw_defaults)
    hourly_rate = natgw_pricing.get("hourly", 0.045)
    data_processing_rate = natgw_pricing.get("data_processing_per_gb", 0.045)

    hourly_cost = hours_per_month * hourly_rate
    data_processing_cost = data_processed_gb * data_processing_rate

    total_cost = hourly_cost + data_processing_cost

    result = {
        "hourly_cost": round(hourly_cost, 2),
        "data_processing_cost": round(data_processing_cost, 2),
        "total": round(total_cost, 2),
        "hours": hours_per_month,
        "data_processed_gb": data_processed_gb,
        "warning": (
            "NAT Gateway costs can be significant - "
            "consider alternatives for dev/test environments"
        ),
    }

    logging.getLogger(__name__).debug(
        "NAT Gateway cost: $%.2f/month (%dh, %.1fGB)",
        result["total"],
        hours_per_month,
        data_processed_gb,
    )
    return result


def generate_cost_optimization_recommendations(
    resource_type: str,
    resource_data: dict[str, Any],
) -> list[str]:
    """
    Generate cost optimization recommendations for AWS resources.

    This function analyzes resource configurations and suggests potential
    cost savings opportunities.

    Args:
        resource_type: Type of resource ('ec2', 'rds', 's3', 'vpc', etc.)
        resource_data: Dictionary containing resource configuration details

    Returns:
        list: List of recommendation strings

    Example:
        >>> recommendations = generate_cost_optimization_recommendations(
        ...     'ec2',
        ...     {'state': 'stopped', 'instance_type': 't3.large', 'days_stopped': 30}
        ... )
        >>> for rec in recommendations:
        ...     print(f"- {rec}")

    Note:
        - Recommendations are general guidelines, not specific financial advice
        - Consider business requirements before implementing changes
    """
    recommendations = []

    if resource_type == "ec2":
        state = resource_data.get("state", "").lower()
        instance_type = resource_data.get("instance_type", "")
        days_stopped = resource_data.get("days_stopped", 0)

        if state == "stopped" and days_stopped > 7:
            recommendations.append(
                f"Instance stopped for {days_stopped} days - consider terminating if no longer needed"
            )

        if instance_type.startswith("t2."):
            recommendations.append(
                "Consider upgrading to t3 instance family for better price/performance"
            )

        if resource_data.get("ebs_optimized", False) and instance_type.startswith("t3."):
            recommendations.append(
                "EBS-optimized is included free for t3 instances - no change needed"
            )

    elif resource_type == "rds":
        multi_az = resource_data.get("multi_az", False)
        environment = resource_data.get("environment", "").lower()

        if multi_az and environment in ["dev", "test", "staging"]:
            recommendations.append(
                "Multi-AZ enabled in non-production environment - consider single-AZ for cost savings"
            )

        backup_retention = resource_data.get("backup_retention_period", 0)
        if backup_retention > 7 and environment in ["dev", "test"]:
            recommendations.append(
                f"Backup retention is {backup_retention} days - consider reducing for non-production"
            )

    elif resource_type == "s3":
        storage_class = resource_data.get("storage_class", "STANDARD")
        size_gb = resource_data.get("size_gb", 0)
        last_accessed = resource_data.get("days_since_last_access", 0)

        if storage_class == "STANDARD" and last_accessed > 90:
            recommendations.append(
                "Objects not accessed in 90+ days - consider moving to STANDARD_IA or GLACIER"
            )

        if storage_class == "STANDARD" and size_gb > 1000:
            recommendations.append(
                "Large bucket - consider enabling Intelligent-Tiering for automatic cost optimization"
            )

    elif resource_type == "nat_gateway":
        data_processed_gb = resource_data.get("data_processed_gb", 0)
        environment = resource_data.get("environment", "").lower()

        if environment in ["dev", "test"]:
            recommendations.append(
                "NAT Gateway in non-production - consider NAT instances or removing for cost savings"
            )

        if data_processed_gb > 5000:
            recommendations.append(
                "High data transfer - verify traffic patterns and consider VPC endpoints for AWS services"
            )

    if not recommendations:
        recommendations.append("No specific cost optimization recommendations at this time")

    return recommendations


# ============================================================================
# SCAN SESSION TRACKING
# ============================================================================

def _get_scan_sessions_dir() -> Path:
    """Return (and create) the scan-sessions directory inside the output dir."""
    sessions_dir = get_output_dir() / "scan-sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir


def start_scan_session(scan_type: str, label: str, planned: list) -> dict:
    """
    Create a new scan session file and return the session dict.

    Args:
        scan_type: 'org-scan' or 'smart-scan'
        label: Human-readable run description
        planned: List of dicts, each with at least 'key' (unique per item)

    Returns:
        Session dict — pass to record_scan_result() and complete_scan_session()
    """
    session_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    session_path = _get_scan_sessions_dir() / f"scan-{session_id}.json"
    session = {
        "session_id": session_id,
        "scan_type": scan_type,
        "label": label,
        "status": "running",
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "completed_at": None,
        "planned": planned,
        "results": [],
        "_path": str(session_path),
    }
    _write_scan_session(session)
    return session


def _write_scan_session(session: dict) -> None:
    """Atomically write session dict to its file (temp-rename pattern)."""
    path = Path(session["_path"])
    data = {k: v for k, v in session.items() if not k.startswith("_")}
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        log_warning(f"Failed to write scan session: {exc}")


def record_scan_result(
    session: dict,
    key: str,
    status: str,
    exit_code: int,
    duration_s: float,
    **kwargs,
) -> None:
    """
    Append a completed item to the session file.

    Args:
        session: Session dict from start_scan_session()
        key: Unique key matching a 'planned' entry
        status: 'success' or 'failed'
        exit_code: Process exit code
        duration_s: Wall-clock seconds
        **kwargs: Extra fields to include (account_id, account_name, script, etc.)
    """
    result = {
        "key": key,
        "status": status,
        "exit_code": exit_code,
        "duration_s": round(duration_s, 1),
        "completed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        **kwargs,
    }
    session["results"].append(result)
    _write_scan_session(session)


def complete_scan_session(session: dict) -> None:
    """Mark session as completed and write final state."""
    session["status"] = "completed"
    session["completed_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    _write_scan_session(session)


def load_scan_sessions(limit: int = 10) -> list:
    """Load recent scan sessions, newest first."""
    try:
        sessions_dir = _get_scan_sessions_dir()
    except Exception:
        return []
    files = sorted(sessions_dir.glob("scan-*.json"), reverse=True)[:limit]
    sessions = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_path"] = str(f)
            sessions.append(data)
        except Exception:
            pass
    return sessions


def get_interrupted_sessions() -> list:
    """Return sessions that were never completed (status still 'running')."""
    return [s for s in load_scan_sessions(20) if s.get("status") == "running"]


def resume_scan_session(session: dict) -> None:
    """Mark an interrupted session as running again (for resume flows)."""
    session["status"] = "running"
    session["completed_at"] = None
    _write_scan_session(session)


def update_scan_session(session: dict, **fields) -> None:
    """
    Merge arbitrary metadata into a session and persist it.

    Used by callers that need context to survive an interruption — the regions
    scanned, the discovered service list, the account the run targeted — so a
    resumed run can still produce a complete report. Keys beginning with an
    underscore stay in-memory only (see ``_write_scan_session``).

    Args:
        session: Session dict from start_scan_session()
        **fields: Metadata keys to set on the session
    """
    session.update(fields)
    _write_scan_session(session)
