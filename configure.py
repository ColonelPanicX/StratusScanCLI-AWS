#!/usr/bin/env python3

"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: StratusScan Configuration Tool
Version: v0.2.0
Date: DEC-24-2024

Description:
Interactive configuration tool for setting up the StratusScan AWS environment.
This script provides a menu-driven interface for verifying dependencies,
AWS connectivity, account name mappings, and default scan regions.

Usage:
- python configure.py                              (interactive dashboard)
- python configure.py --deps                       (dependency check only)
- python configure.py --perms                      (permissions check only)
- python configure.py --account ID NAME            (quick account mapping)
- python configure.py --region REGION              (quick region update)
- python configure.py --validate                   (full validation check)
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Try to import boto3, but don't fail if it's missing (will be caught later)
try:
    import boto3  # noqa: F401  # imported to probe availability; not used directly here
    from botocore.exceptions import ClientError, NoCredentialsError
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False
    # Define placeholder exception classes
    class ClientError(Exception):
        pass
    class NoCredentialsError(Exception):
        pass

# Import shared utilities (provides FIPS-aware get_boto3_client and detect_partition)
try:
    import utils
except ImportError:
    sys.path.append(str(Path(__file__).parent))
    import utils

# ============================================================================
# GLOBAL STATE (for background checks)
# ============================================================================

_dependency_status = None  # Will be populated by background check
_permission_status = None  # Will be populated by background check
_aws_identity = None       # Current AWS identity info
_config_modified = False   # Track if config has unsaved changes

# ============================================================================
# AWS PARTITION & IDENTITY DETECTION
# ============================================================================

def detect_aws_partition() -> tuple[str, str]:
    """
    Detect the AWS partition (commercial vs govcloud) from caller identity.

    Returns:
        tuple: (partition, default_region) - e.g., ('aws', 'us-east-1') or ('aws-us-gov', 'us-gov-west-1')
    """
    partition = utils.detect_partition()
    default_region = 'us-gov-west-1' if partition == 'aws-us-gov' else 'us-east-1'
    return partition, default_region

def get_aws_identity() -> Optional[dict]:
    """
    Get current AWS identity information.

    Returns:
        dict: Identity info or None if credentials unavailable
    """
    global _aws_identity

    if _aws_identity is not None:
        return _aws_identity

    if not BOTO3_AVAILABLE:
        return None

    try:
        partition, default_region = detect_aws_partition()
        sts = utils.get_boto3_client('sts', default_region)
        identity = sts.get_caller_identity()

        _aws_identity = {
            'arn': identity.get('Arn', 'Unknown'),
            'account_id': identity.get('Account', 'Unknown'),
            'user_id': identity.get('UserId', 'Unknown'),
            'partition': partition,
            'partition_name': "AWS GovCloud (US)" if partition == 'aws-us-gov' else "AWS Commercial",
            'default_region': default_region
        }
        return _aws_identity
    except Exception:
        return None

# ============================================================================
# VISUAL HELPERS
# ============================================================================

def clear_screen():
    """
    Clear the terminal screen using ANSI escape codes (avoids os.system shell call).
    Works on Windows 10+, Linux, and macOS terminals.
    """
    print('\033[2J\033[H', end='', flush=True)


def print_box(title: str, width: int = 70):
    """Print a box with title."""
    print("╔" + "═" * (width - 2) + "╗")
    padding = (width - len(title) - 2) // 2
    print("║" + " " * padding + title + " " * (width - len(title) - padding - 2) + "║")
    print("╚" + "═" * (width - 2) + "╝")

def print_section(title: str, width: int = 70):
    """Print a section header."""
    print("\n" + "─" * width)
    print(title)
    print("─" * width)

def _visual_len(s: str) -> int:
    """Return terminal column width of s, counting emoji/wide chars as 2 columns."""
    count = 0
    for ch in s:
        cp = ord(ch)
        # Skip zero-width variation selectors and zero-width joiners
        if 0xFE00 <= cp <= 0xFE0F or cp == 0x200D:
            continue
        # Wide emoji/symbols: anything >= U+2600 that is not box-drawing (U+2500–U+257F)
        if cp >= 0x2600 and not (0x2500 <= cp <= 0x257F):
            count += 2
        else:
            count += 1
    return count

def print_status_line(label: str, status: str, width: int = 70):
    """Print a status line with alignment."""
    label_part = f"║ {label}: "
    status_part = f"{status} ║"
    padding = width - len(label_part) - _visual_len(status_part)
    if padding < 0:
        padding = 0
    print(label_part + " " * padding + status_part)

def get_status_icon(status: str) -> str:
    """Get status icon based on status string."""
    return {
        "ok": "✅",
        "warning": "⚠️ ",
        "error": "❌",
        "unknown": "❓",
    }.get(status, "  ")

# ============================================================================
# BACKGROUND CHECKS
# ============================================================================

def check_dependencies_silent() -> dict:
    """
    Check dependencies without user interaction.

    Returns:
        dict: Dependency status information
    """
    required_packages = [
        {'name': 'boto3', 'import_name': 'boto3', 'description': 'AWS SDK for Python'},
        {'name': 'pandas', 'import_name': 'pandas', 'description': 'Data manipulation and analysis library'},
        {'name': 'openpyxl', 'import_name': 'openpyxl', 'description': 'Excel file reading/writing library'}
    ]

    missing_packages = []
    installed_packages = []

    for package in required_packages:
        try:
            __import__(package['import_name'])
            installed_packages.append(package)
        except ImportError:
            missing_packages.append(package)

    return {
        'all_satisfied': len(missing_packages) == 0,
        'installed_count': len(installed_packages),
        'total_count': len(required_packages),
        'missing_packages': missing_packages,
        'installed_packages': installed_packages
    }

def check_permissions_silent() -> dict:
    """
    Check AWS permissions without user interaction.

    Returns:
        dict: Permission status information
    """
    identity = get_aws_identity()
    if not identity:
        return {
            'has_credentials': False,
            'has_required': False,
            'required_passed': 0,
            'required_failed': 0,
            'optional_passed': 0,
            'optional_failed': 0,
            'error': 'No AWS credentials configured'
        }

    test_region = identity['default_region']

    # Simplified permission tests (subset for speed)
    permission_tests = {
        'sts:GetCallerIdentity': {
            'test_function': lambda: utils.get_boto3_client('sts', test_region).get_caller_identity(),
            'required': True
        },
        'ec2:DescribeInstances': {
            'test_function': lambda: utils.get_boto3_client('ec2', test_region).describe_instances(MaxResults=5),
            'required': True
        },
        's3:ListBuckets': {
            'test_function': lambda: utils.get_boto3_client('s3', test_region).list_buckets(),
            'required': True
        },
        'iam:ListUsers': {
            'test_function': lambda: utils.get_boto3_client('iam', test_region).list_users(MaxItems=5),
            'required': True
        }
    }

    required_passed = 0
    required_failed = 0
    optional_passed = 0
    optional_failed = 0

    for _permission, config in permission_tests.items():
        try:
            config['test_function']()
            if config['required']:
                required_passed += 1
            else:
                optional_passed += 1
        except Exception:
            if config['required']:
                required_failed += 1
            else:
                optional_failed += 1

    return {
        'has_credentials': True,
        'has_required': required_failed == 0,
        'required_passed': required_passed,
        'required_failed': required_failed,
        'optional_passed': optional_passed,
        'optional_failed': optional_failed
    }

def run_background_checks():
    """Run background checks for dependencies and permissions."""
    global _dependency_status, _permission_status

    # Check dependencies
    _dependency_status = check_dependencies_silent()

    # Check permissions
    _permission_status = check_permissions_silent()

# ============================================================================
# CONFIGURATION FILE MANAGEMENT
# ============================================================================

def get_config_path() -> Path:
    """Get the path to config.json file."""
    script_dir = Path(__file__).parent.absolute()
    return script_dir / "config.json"

def load_existing_config(config_path: Path) -> dict:
    """
    Load existing configuration file if it exists.

    Args:
        config_path (Path): Path to the config file

    Returns:
        dict: Existing configuration or default structure
    """
    if config_path.exists():
        try:
            with open(config_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: Could not read existing config file: {e}")
            print("Creating new configuration...")

    # Return default AWS configuration structure
    return {
        "__comment": "StratusScan Configuration - Customize this file for your environment",
        "account_mappings": {},
        "default_regions": ["us-east-1", "us-east-2", "us-west-1", "us-west-2"],
    }

def save_configuration(config: dict, config_path: Path) -> bool:
    """
    Save the configuration to the JSON file.

    Args:
        config (dict): Configuration dictionary
        config_path (Path): Path to save the config file

    Returns:
        bool: True if successful, False otherwise
    """
    global _config_modified

    try:
        # Create backup if file exists
        if config_path.exists():
            backup_path = config_path.with_suffix('.json.backup')
            config_path.rename(backup_path)
            print(f"\n✅ Backup created: {backup_path}")

        # Save new configuration
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)

        print(f"✅ Configuration saved successfully to: {config_path}")
        _config_modified = False
        return True

    except Exception as e:
        print(f"❌ Error saving configuration: {e}")
        return False

def get_config_status(config: dict, config_path: Path) -> str:
    """
    Get configuration status string.

    Args:
        config (dict): Configuration dictionary
        config_path (Path): Path to config file

    Returns:
        str: Status string
    """
    if not config_path.exists():
        return "❌ Not configured"

    if _config_modified:
        return "⚠️  Modified (unsaved)"

    if not config.get('account_mappings'):
        return "⚠️  No accounts"

    # Get last modified time
    try:
        mtime = config_path.stat().st_mtime
        mod_date = datetime.fromtimestamp(mtime).strftime("%b %d")
        return f"✅ OK (Updated {mod_date})"
    except OSError:
        return "✅ OK"

# ============================================================================
# VALIDATION HELPERS
# ============================================================================

def validate_account_id(account_id: str) -> bool:
    """
    Validate that the account ID is a 12-digit number.

    Args:
        account_id (str): The account ID to validate

    Returns:
        bool: True if valid, False otherwise
    """
    account_id = account_id.strip()
    pattern = re.compile(r'^\d{12}$')
    return bool(pattern.match(account_id))

# ============================================================================
# MAIN MENU & DASHBOARD
# ============================================================================

def print_dashboard(config: dict, config_path: Path):
    """
    Print the main dashboard.

    Args:
        config (dict): Configuration dictionary
        config_path (Path): Path to config file
    """
    clear_screen()
    identity = get_aws_identity()

    # Header
    print("\n")
    print_box("STRATUSSCAN CONFIGURATION TOOL", 70)

    # Status box
    print("╔" + "═" * 68 + "╗")

    if identity:
        mappings = config.get('account_mappings', {})
        friendly = mappings.get(identity['account_id'], '—')
        default_regions = config.get('default_regions', [])
        region_str = ', '.join(default_regions) if default_regions else 'Not set'
        print_status_line("Environment", identity['partition_name'], 70)
        print_status_line("Account ID", identity['account_id'], 70)
        print_status_line("Account Friendly", friendly, 70)
        print_status_line("Region", region_str, 70)
    else:
        print_status_line("AWS Credentials", "❌ Not configured", 70)

    config_status = get_config_status(config, config_path)
    print_status_line("Configuration", config_status, 70)

    out_settings = config.get("output_settings", {})
    output_fmt = out_settings.get("format", utils.detect_default_format())
    print_status_line("Export Format", output_fmt, 70)

    destination = out_settings.get("destination", "local")
    if destination == "s3":
        bucket = (out_settings.get("s3", {}) or {}).get("bucket", "")
        dest_str = f"S3: {bucket}" if bucket else "S3 (bucket not set!)"
    else:
        dest_str = "local (output/)"
    print_status_line("Output Destination", dest_str, 70)

    print("╚" + "═" * 68 + "╝")

    # Main menu
    print("\n" + "═" * 70)
    print("MAIN MENU")
    print("═" * 70)

    # First-run wizard
    print("\nFirst Run:")
    print("  [0] Config Wizard             (account mappings → regions → deps → perms)")

    # Configuration options
    print("\nConfiguration:")
    print("  [1] View Current Configuration")
    print("  [2] Manage Account Mappings")
    print("  [3] Configure Default Regions")

    # System checks
    print("\nSystem Checks:")

    # Dependencies status
    if _dependency_status:
        if _dependency_status['all_satisfied']:
            dep_status = f"✅ All OK ({_dependency_status['installed_count']}/{_dependency_status['total_count']})"
        else:
            missing_count = len(_dependency_status['missing_packages'])
            dep_status = f"❌ {missing_count} missing"
    else:
        dep_status = "❓ Not checked"
    print(f"  [4] Check Dependencies                     {dep_status}")

    # Permissions status
    if _permission_status:
        if not _permission_status['has_credentials']:
            perm_status = "❌ No credentials"
        elif _permission_status['has_required']:
            optional_failed = _permission_status.get('optional_failed', 0)
            if optional_failed > 0:
                perm_status = f"⚠️  {optional_failed} optional missing"
            else:
                perm_status = "✅ All OK"
        else:
            required_failed = _permission_status['required_failed']
            perm_status = f"❌ {required_failed} required missing"
    else:
        perm_status = "❓ Not checked"
    print(f"  [5] Check AWS Permissions                  {perm_status}")

    # Cross-account roles count
    cross_roles = config.get('cross_account_roles', {})
    real_cross_roles = {k: v for k, v in cross_roles.items() if validate_account_id(k)}
    role_count = len(real_cross_roles)
    role_status = f"{role_count} configured" if role_count else "None configured"
    print(f"  [6] Cross-Account Roles                    {role_status}")

    out_settings = config.get("output_settings", {})
    output_fmt = out_settings.get("format", utils.detect_default_format())
    out_dest = out_settings.get("destination", "local")
    dest_label = "S3" if out_dest == "s3" else "local"
    print(f"  [7] Output Settings                        {output_fmt} → {dest_label}")

    # Actions
    print("\nActions:")
    print("  [S] Save & Exit")
    print("  [U] Exit Without Saving" + (" (Unsaved changes!)" if _config_modified else ""))

    print("\n" + "═" * 70)

def _ensure_output_settings(config: dict) -> dict:
    """Return config['output_settings'], creating it (and the nested s3 block) if absent."""
    out = config.setdefault("output_settings", {})
    out.setdefault("s3", {})
    return out


def _set_output_format(config: dict):
    """Prompt for and apply the export format (xlsx/csv)."""
    global _config_modified

    out = _ensure_output_settings(config)
    current_fmt = out.get("format", utils.detect_default_format())

    print("\n" + "─" * 70)
    print("EXPORT FORMAT")
    print("─" * 70)
    print(f"  Current: {current_fmt}")
    print("  [1] xlsx  — Excel workbook (requires openpyxl)")
    print("  [2] csv   — Universal CSV (stdlib only, multi-sheet exports split into files)")
    choice = input("Select format (1/2, or Enter to keep current): ").strip()

    if choice == "1":
        new_fmt = "xlsx"
    elif choice == "2":
        new_fmt = "csv"
    else:
        print("  No change made.")
        return

    if new_fmt == current_fmt:
        print(f"  Format already set to '{new_fmt}'. No change.")
    else:
        out["format"] = new_fmt
        _config_modified = True
        print(f"  Export format set to '{new_fmt}'.")


def _set_output_destination(config: dict):
    """Prompt for and apply the output destination (local/s3)."""
    global _config_modified

    out = _ensure_output_settings(config)
    current_dest = out.get("destination", "local")

    print("\n" + "─" * 70)
    print("OUTPUT DESTINATION")
    print("─" * 70)
    print(f"  Current: {current_dest}")
    print("  [1] local — save to output/ (default)")
    print("  [2] s3    — upload to an S3 bucket, then delete the local copy on success")
    choice = input("Select destination (1/2, or Enter to keep current): ").strip()

    if choice == "1":
        new_dest = "local"
    elif choice == "2":
        new_dest = "s3"
    else:
        print("  No change made.")
        return

    if new_dest == current_dest:
        print(f"  Destination already set to '{new_dest}'. No change.")
    else:
        out["destination"] = new_dest
        _config_modified = True
        print(f"  Output destination set to '{new_dest}'.")

    if new_dest == "s3" and not out.get("s3", {}).get("bucket"):
        print("  ⚠️  No S3 bucket configured yet — set it with [3] before exporting.")


def _set_s3_bucket(config: dict):
    """Prompt for and apply the S3 bucket name."""
    global _config_modified

    out = _ensure_output_settings(config)
    current = out["s3"].get("bucket", "")

    print("\n" + "─" * 70)
    print("S3 BUCKET")
    print("─" * 70)
    print(f"  Current: {current or '(not set)'}")
    new_bucket = input("Enter S3 bucket name (Enter to keep current): ").strip()
    if not new_bucket:
        print("  No change made.")
        return

    out["s3"]["bucket"] = new_bucket
    _config_modified = True
    print(f"  S3 bucket set to '{new_bucket}'.")


def _set_s3_prefix(config: dict):
    """Prompt for and apply the S3 key prefix."""
    global _config_modified

    out = _ensure_output_settings(config)
    current = out["s3"].get("prefix", "stratusscan/")

    print("\n" + "─" * 70)
    print("S3 PREFIX")
    print("─" * 70)
    print(f"  Current: {current}")
    print("  Key prefix within the bucket (e.g. 'stratusscan/'). Enter '/' for no prefix.")
    new_prefix = input("Enter S3 prefix (Enter to keep current): ").strip()
    if not new_prefix:
        print("  No change made.")
        return

    new_prefix = "" if new_prefix == "/" else new_prefix
    out["s3"]["prefix"] = new_prefix
    _config_modified = True
    print(f"  S3 prefix set to '{new_prefix}'.")


def _run_s3_connectivity_test(config: dict):
    """Run the HeadBucket → PutObject → DeleteObject roundtrip and print results."""
    out = _ensure_output_settings(config)
    bucket = out["s3"].get("bucket", "")
    prefix = out["s3"].get("prefix", "stratusscan/")

    print("\n" + "─" * 70)
    print("TEST S3 CONNECTION")
    print("─" * 70)

    if not bucket:
        print("  ❌ No S3 bucket configured. Set one with [3] first.")
        return

    print(f"  Bucket: {bucket}")
    print(f"  Prefix: {prefix}")
    print("  Running HeadBucket → PutObject → DeleteObject ...\n")

    result = utils.test_s3_connectivity(bucket, prefix)

    if result.get("region"):
        print(f"  Region: {result['region']}")
    for step in result["steps"]:
        if step["ok"]:
            print(f"  ✅ {step['step']}")
        else:
            print(f"  ❌ {step['step']}")
            print(f"       {step['error']}")

    print()
    if result["ok"]:
        print("  ✅ S3 connectivity OK — exports will upload to this bucket.")
    else:
        failed = result.get("failed_step")
        print(f"  ❌ S3 connectivity FAILED at step: {failed}")
        print("     Common causes: wrong region, PutObject denied by bucket policy/KMS/VPC")
        print("     endpoint condition, prefix-level permission mismatch, or Block Public")
        print("     Access conflict. See policies/s3-upload-permissions.json for the")
        print("     minimum IAM grant required.")


def configure_output_settings(config: dict):
    """
    Configure export output settings: format, destination, and S3 details.

    Submenu loop covering format (xlsx/csv), destination (local/s3), S3 bucket
    and prefix, and an on-demand S3 connectivity test. Mutates config in place
    and sets _config_modified when a change is made.

    Args:
        config (dict): Configuration dictionary (mutated in place)
    """
    while True:
        out = _ensure_output_settings(config)
        fmt = out.get("format", utils.detect_default_format())
        dest = out.get("destination", "local")
        bucket = out["s3"].get("bucket", "") or "(not set)"
        prefix = out["s3"].get("prefix", "stratusscan/")

        print("\n" + "═" * 70)
        print("OUTPUT SETTINGS")
        print("═" * 70)
        print(f"  Format:      {fmt}")
        print(f"  Destination: {dest}")
        if dest == "s3":
            print(f"  S3 bucket:   {bucket}")
            print(f"  S3 prefix:   {prefix}")
        print()
        try:
            choice = utils.prompt_menu(
                "Select an option",
                [
                    "Change export format (xlsx/csv)",
                    "Change destination (local/s3)",
                    "Set S3 bucket",
                    "Set S3 prefix",
                    "Test S3 connection",
                ],
            )
        except utils.BackSignal:
            return
        except (utils.ExitToMainSignal, utils.QuitSignal):
            return

        if choice == 1:
            _set_output_format(config)
        elif choice == 2:
            _set_output_destination(config)
        elif choice == 3:
            _set_s3_bucket(config)
        elif choice == 4:
            _set_s3_prefix(config)
        elif choice == 5:
            _run_s3_connectivity_test(config)


def config_wizard(config: dict):
    """
    First-run wizard: account mappings → export format → default regions → deps → perms → cross-account roles.

    Steps the user through six essential setup tasks in sequence.
    Re-entrant — safe to run on an already-configured system.
    """
    def _clr():
        return clear_screen()

    _clr()
    print_section("CONFIG WIZARD")
    print("This wizard walks you through six essential setup steps.")
    print("You can skip any step by pressing Enter with no input where prompted.\n")
    print("Step 1/6 — Account Mappings\n")
    manage_account_mappings(config)

    _clr()
    print("✅ Step 1/6 complete — Account Mappings\n")
    print("Step 2/6 — Export Format\n")
    _set_output_format(config)
    input("\nPress Enter to continue...")

    _clr()
    print("✅ Step 2/6 complete — Export Format\n")
    print("Step 3/6 — Default Regions\n")
    configure_default_regions(config)

    _clr()
    print("✅ Step 3/6 complete — Default Regions\n")
    print("Step 4/6 — Dependencies Check\n")
    dependency_management_menu()

    _clr()
    print("✅ Step 4/6 complete — Dependencies Check\n")
    print("Step 5/6 — AWS Permissions Check\n")
    permissions_management_menu()

    _clr()
    print("✅ Step 5/6 complete — AWS Permissions Check\n")
    print("Step 6/6 — Cross-Account Roles\n")
    manage_cross_account_roles(config)

    _clr()
    print("\n✅ Config Wizard complete.")
    input("Press Enter to return to the main menu...")


def main_menu_loop(config: dict, config_path: Path):
    """
    Main menu loop.

    Args:
        config (dict): Configuration dictionary
        config_path (Path): Path to config file
    """
    while True:
        print_dashboard(config, config_path)

        choice = input("\nSelect option (0-7, S to save, U to exit): ").strip().upper()

        if choice == '0':
            config_wizard(config)
        elif choice == '1':
            view_configuration(config)
        elif choice == '2':
            manage_account_mappings(config)
        elif choice == '3':
            configure_default_regions(config)
        elif choice == '4':
            dependency_management_menu()
        elif choice == '5':
            permissions_management_menu()
        elif choice == '6':
            manage_cross_account_roles(config)
        elif choice == '7':
            configure_output_settings(config)
        elif choice == 'S':
            # Save & Exit
            if _config_modified:
                print("\n" + "═" * 70)
                print("SAVE CONFIGURATION")
                print("═" * 70)
                display_summary(config)
                confirm = utils.prompt_for_confirmation("\nSave this configuration?", default=False)
                if confirm:
                    if save_configuration(config, config_path):
                        print("\n✅ Configuration saved successfully!")
                        print("You can now use StratusScan with your configured settings.")
                        print("Run 'python stratusscan.py' to start the main menu.")
                        return
                    else:
                        print("\n❌ Configuration save failed.")
                        input("\nPress Enter to return to menu...")
                        continue
                else:
                    input("\nPress Enter to return to menu...")
                    continue
            else:
                print("\n✅ No unsaved changes. Exiting...")
                return
        elif choice == 'U':
            # Exit without saving
            if _config_modified:
                print("\n⚠️  WARNING: You have unsaved changes!")
                confirm = utils.prompt_for_confirmation("Exit without saving?", default=False)
                if confirm:
                    print("\n❌ Configuration not saved. Exiting...")
                    return
                else:
                    continue
            else:
                print("\n✅ Exiting...")
                return
        else:
            print("\n❌ Invalid choice. Please select 0-7, S, or U.")
            input("Press Enter to continue...")

# ============================================================================
# CONFIGURATION VIEWERS & EDITORS
# ============================================================================

def view_configuration(config: dict):
    """View current configuration."""
    print("\n" + "═" * 70)
    print("CURRENT CONFIGURATION")
    print("═" * 70)
    display_summary(config)
    input("\nPress Enter to return to menu...")

def display_summary(config: dict):
    """
    Display a summary of the current configuration.

    Args:
        config (dict): Configuration dictionary
    """
    # Default regions
    default_regions = config.get('default_regions', [])
    print(f"\nDefault Regions: {', '.join(default_regions) if default_regions else 'Not set'}")

    # Account mappings — filter to valid 12-digit IDs only (excludes __comment keys)
    mappings = config.get('account_mappings', {})
    real_mappings = {k: v for k, v in mappings.items() if validate_account_id(k)}
    print(f"\nAccount Mappings ({len(real_mappings)} configured):")
    if real_mappings:
        for account_id, name in sorted(real_mappings.items()):
            print(f"  {account_id} → {name}")
    else:
        print("  None configured")

def manage_account_mappings(config: dict):
    """Manage account mappings."""
    global _config_modified

    while True:
        clear_screen()
        print_section("MANAGE ACCOUNT MAPPINGS")

        mappings = config.get('account_mappings', {})
        # Filter to valid 12-digit account IDs only (excludes __comment keys)
        real_mappings = {k: v for k, v in sorted(mappings.items()) if validate_account_id(k)}

        print(f"\nCurrent Account Mappings ({len(real_mappings)}):")
        if real_mappings:
            for idx, (account_id, name) in enumerate(real_mappings.items(), 1):
                print(f"  {idx}. {account_id} → {name}")
        else:
            print("  None configured")

        try:
            choice = utils.prompt_menu(
                "Select an option",
                ["Add new account", "Edit existing account", "Delete account"],
            )
        except utils.BackSignal:
            return
        except (utils.ExitToMainSignal, utils.QuitSignal):
            return

        if choice == 1:
            # Fetch current account identity for defaults
            identity = get_aws_identity()
            default_id = identity['account_id'] if identity and identity.get('account_id') != 'Unknown' else ''
            default_name = ''
            if identity and default_id:
                try:
                    iam = utils.get_boto3_client('iam', identity['default_region'])
                    aliases = iam.list_account_aliases().get('AccountAliases', [])
                    if aliases:
                        default_name = aliases[0]
                except Exception:
                    pass

            account_id = ''
            while True:
                prompt = f"\nEnter AWS Account ID [{default_id}]: " if default_id else "\nEnter AWS Account ID (12 digits): "
                raw = input(prompt).strip()
                account_id = raw if raw else default_id
                if not account_id:
                    break
                if validate_account_id(account_id):
                    if account_id in mappings:
                        overwrite = utils.prompt_for_confirmation(
                            f"Account {account_id} already exists. Overwrite?", default=False
                        )
                        if not overwrite:
                            continue
                    break
                else:
                    print("❌ Invalid account ID. Must be exactly 12 digits (e.g., 123456789012)")

            if account_id:
                prompt = f"Enter friendly name for account {account_id} [{default_name}]: " if default_name else f"Enter friendly name for account {account_id}: "
                raw = input(prompt).strip()
                account_name = raw if raw else default_name
                if account_name:
                    mappings[account_id] = account_name
                    config['account_mappings'] = mappings
                    _config_modified = True
                    print(f"\n✅ Added: {account_id} → {account_name}")
                else:
                    print("\n❌ Account name cannot be empty")

        elif choice == 2:
            # Edit existing account
            if not mappings:
                print("\n❌ No accounts to edit")
                input("Press Enter to continue...")
                continue

            account_id = input("\nEnter Account ID to edit: ").strip()
            if not validate_account_id(account_id):
                print("❌ Invalid account ID. Must be exactly 12 digits (e.g., 123456789012)")
                input("Press Enter to continue...")
                continue
            if account_id in mappings:
                print(f"Current name: {mappings[account_id]}")
                new_name = input("Enter new friendly name: ").strip()
                if new_name:
                    mappings[account_id] = new_name
                    config['account_mappings'] = mappings
                    _config_modified = True
                    print(f"\n✅ Updated: {account_id} → {new_name}")
            else:
                print(f"\n❌ Account {account_id} not found")

        elif choice == 3:
            # Delete account
            if not mappings:
                print("\n❌ No accounts to delete")
                input("Press Enter to continue...")
                continue

            account_id = input("\nEnter Account ID to delete: ").strip()
            if account_id in mappings:
                confirm = utils.prompt_for_confirmation(
                    f"Delete {account_id} ({mappings[account_id]})?", default=False
                )
                if confirm:
                    del mappings[account_id]
                    config['account_mappings'] = mappings
                    _config_modified = True
                    print(f"\n✅ Deleted: {account_id}")
            else:
                print(f"\n❌ Account {account_id} not found")

def configure_default_regions(config: dict):
    """Configure default regions."""
    global _config_modified

    print_section("CONFIGURE DEFAULT REGIONS")

    identity = get_aws_identity()
    is_govcloud = identity['partition'] == 'aws-us-gov' if identity else False

    current_regions = config.get('default_regions', [])
    print(f"\nCurrent default regions: {', '.join(current_regions) if current_regions else 'None'}")

    if is_govcloud:
        preset_label = "GovCloud — both regions (us-gov-west-1, us-gov-east-1)"
        preset_regions = ["us-gov-west-1", "us-gov-east-1"]
    else:
        preset_label = "US Standard — us-east-1, us-east-2, us-west-1, us-west-2"
        preset_regions = ["us-east-1", "us-east-2", "us-west-1", "us-west-2"]

    # Validates standard AWS region format: us-east-1, eu-west-2, us-gov-west-1, etc.
    region_pattern = re.compile(r'^[a-z]{2,3}(-[a-z]+)+-\d+$')

    while True:
        try:
            choice = utils.prompt_menu(
                "Select an option",
                [preset_label, "Custom — enter regions manually"],
            )
        except utils.BackSignal:
            return
        except (utils.ExitToMainSignal, utils.QuitSignal):
            return

        if choice == 1:
            regions = preset_regions
            config['default_regions'] = regions
            _config_modified = True
            print(f"\n✅ Default regions set: {', '.join(regions)}")
            break

        elif choice == 2:
            raw = input("  Regions, comma-separated (e.g. eu-west-1,ap-northeast-1): ").strip().lower()
            if not raw:
                continue

            candidates = [r.strip() for r in raw.split(',') if r.strip()]
            invalid = [r for r in candidates if not region_pattern.match(r)]

            if invalid:
                print(f"  ❌ Invalid region(s): {', '.join(invalid)}")
                print("  Expected format: us-east-1, eu-west-2, ap-south-2, etc.")
                input("  Press Enter to try again...")
                continue

            # Deduplicate while preserving order
            seen: set = set()
            regions = [r for r in candidates if not (r in seen or seen.add(r))]  # type: ignore[func-returns-value]

            config['default_regions'] = regions
            _config_modified = True
            print(f"\n✅ Default regions set: {', '.join(regions)}")
            break

    input("\nPress Enter to return to menu...")


# ============================================================================
# CROSS-ACCOUNT ROLES
# ============================================================================

_ROLE_ARN_RE = re.compile(r"^arn:(aws|aws-us-gov):iam::\d{12}:role/.+$")


def manage_cross_account_roles(config: dict):
    """
    Interactive menu for managing cross-account IAM role mappings.

    Mirrors manage_account_mappings() — numbered Add / Remove menu, b = back.
    Validates role ARN format before delegating to utils.
    """
    global _config_modified

    while True:
        print_section("CROSS-ACCOUNT ROLES")

        roles = config.get('cross_account_roles', {})
        # Filter comment keys
        real_roles = {k: v for k, v in sorted(roles.items()) if validate_account_id(k)}

        print(f"\nCurrent Cross-Account Roles ({len(real_roles)}):")
        if real_roles:
            for idx, (account_id, role_arn) in enumerate(real_roles.items(), 1):
                print(f"  {idx}. {account_id} → {role_arn}")
        else:
            print("  None configured")

        try:
            choice = utils.prompt_menu(
                "Select an option",
                ["Add role", "Remove role"],
            )
        except utils.BackSignal:
            return
        except (utils.ExitToMainSignal, utils.QuitSignal):
            return

        if choice == 1:
            while True:
                account_id = input("\nEnter target AWS Account ID (12 digits): ").strip()
                if not account_id:
                    break
                if not validate_account_id(account_id):
                    print("❌ Invalid account ID. Must be exactly 12 digits (e.g., 123456789012)")
                    continue
                break

            if not account_id:
                continue

            role_arn = input(f"Enter IAM role ARN for account {account_id}: ").strip()
            if not role_arn:
                print("\n❌ Role ARN cannot be empty")
                continue

            if not _ROLE_ARN_RE.match(role_arn):
                print(
                    "\n❌ Invalid role ARN format. Expected:"
                    "\n   arn:aws:iam::<12-digit-id>:role/<role-name>"
                    "\n   arn:aws-us-gov:iam::<12-digit-id>:role/<role-name>"
                )
                input("Press Enter to continue...")
                continue

            if utils.add_cross_account_role(account_id, role_arn):
                roles[account_id] = role_arn
                config['cross_account_roles'] = roles
                _config_modified = True
                print(f"\n✅ Added: {account_id} → {role_arn}")
            else:
                print(f"\n❌ Failed to add role for account {account_id}")
            input("Press Enter to continue...")

        elif choice == 2:
            if not real_roles:
                print("\n❌ No roles configured to remove")
                input("Press Enter to continue...")
                continue

            account_id = input("\nEnter Account ID to remove role for: ").strip()
            if not validate_account_id(account_id):
                print("❌ Invalid account ID. Must be exactly 12 digits.")
                input("Press Enter to continue...")
                continue

            if account_id not in roles:
                print(f"\n❌ No role configured for account {account_id}")
                input("Press Enter to continue...")
                continue

            confirm = utils.prompt_for_confirmation(
                f"Remove role for {account_id} ({roles[account_id]})?", default=False
            )
            if confirm:
                if utils.remove_cross_account_role(account_id):
                    del roles[account_id]
                    config['cross_account_roles'] = roles
                    _config_modified = True
                    print(f"\n✅ Removed role for account {account_id}")
                else:
                    print(f"\n❌ Failed to remove role for account {account_id}")
            else:
                print("\n↩ Cancelled")
            input("Press Enter to continue...")


# ============================================================================
# DEPENDENCY MANAGEMENT
# ============================================================================

def dependency_management_menu():
    """Interactive menu for dependency management."""
    while True:
        print_section("DEPENDENCY CHECK")

        # Refresh dependency status
        global _dependency_status
        _dependency_status = check_dependencies_silent()

        print("\nChecking required StratusScan dependencies...")

        for package in _dependency_status['installed_packages']:
            print(f"  ✅ {package['name']} - {package['description']}")

        for package in _dependency_status['missing_packages']:
            print(f"  ❌ {package['name']} - {package['description']}")

        print(f"\nSummary: {_dependency_status['installed_count']}/{_dependency_status['total_count']} dependencies satisfied")

        if _dependency_status['all_satisfied']:
            print("\n✅ All dependencies are installed and ready!")
            input("\nPress Enter to return to menu...")
            return

        try:
            choice = utils.prompt_menu(
                "Select an option",
                [
                    "Install missing dependencies automatically",
                    "Show installation commands for manual installation",
                    "Re-check dependencies",
                ],
            )
        except utils.BackSignal:
            return
        except (utils.ExitToMainSignal, utils.QuitSignal):
            return

        if choice == 1:
            install_dependencies(_dependency_status['missing_packages'])
            _dependency_status = check_dependencies_silent()
        elif choice == 2:
            print("\n" + "═" * 70)
            print("MANUAL INSTALLATION COMMANDS")
            print("═" * 70)
            print("\nRun the following commands in your terminal:\n")
            for package in _dependency_status['missing_packages']:
                print(f"pip install {package['name']}")
            print("\nAlternatively, install all at once:")
            print(f"pip install {' '.join([p['name'] for p in _dependency_status['missing_packages']])}")
            input("\nPress Enter to continue...")
        elif choice == 3:
            print("\n🔄 Re-checking dependencies...")
            continue

def install_dependencies(missing_packages: list[dict]) -> bool:
    """
    Install missing dependencies with user confirmation.

    Args:
        missing_packages (list): List of package dictionaries to install

    Returns:
        bool: True if installation was successful, False otherwise
    """
    if not missing_packages:
        return True

    print("\n" + "═" * 70)
    print("DEPENDENCY INSTALLATION")
    print("═" * 70)

    print("\nThe following packages will be installed:")
    for package in missing_packages:
        print(f"  - {package['name']} - {package['description']}")

    confirm = utils.prompt_for_confirmation(
        f"\nInstall these {len(missing_packages)} packages now?", default=True
    )

    if not confirm:
        print("\n❌ Installation cancelled.")
        input("Press Enter to continue...")
        return False

    print("\n🔧 Installing packages using pip...")

    all_successful = True

    for package in missing_packages:
        print(f"\n[INSTALLING] {package['name']}...")
        try:
            subprocess.run([
                sys.executable, "-m", "pip", "install", package['name']
            ], capture_output=True, text=True, check=True)

            print(f"  ✅ {package['name']} installed successfully")

            # Verify the installation
            try:
                __import__(package['import_name'])
                print(f"  ✅ {package['name']} import verification successful")
            except ImportError:
                print(f"  ⚠️  {package['name']} installed but import verification failed")
                all_successful = False

        except subprocess.CalledProcessError as e:
            print(f"  ❌ Failed to install {package['name']}")
            print(f"  Error: {e.stderr.strip()}")
            all_successful = False
        except Exception as e:
            print(f"  ❌ Unexpected error installing {package['name']}: {e}")
            all_successful = False

    if all_successful:
        print("\n✅ All dependencies installed successfully!")
    else:
        print("\n⚠️  Some dependencies failed to install.")
        print("You may need to install them manually or check your Python environment.")

    input("\nPress Enter to continue...")
    return all_successful

# ============================================================================
# PERMISSIONS MANAGEMENT
# ============================================================================

def permissions_management_menu():
    """Interactive menu for AWS permissions management."""
    while True:
        print_section("AWS PERMISSIONS CHECK")

        # Refresh permission status
        global _permission_status
        _permission_status = check_permissions_silent()

        identity = get_aws_identity()

        if not identity:
            print("\n❌ No AWS credentials found!")
            print("Please configure your AWS credentials before running StratusScan.")
            try:
                choice = utils.prompt_menu(
                    "Select an option",
                    ["Show credential configuration help"],
                )
            except utils.BackSignal:
                return
            except (utils.ExitToMainSignal, utils.QuitSignal):
                return

            if choice == 1:
                print("\n" + "═" * 70)
                print("AWS CREDENTIALS SETUP")
                print("═" * 70)
                print("\nTo configure AWS credentials, use one of these methods:")
                print("\n1. AWS CLI (Recommended):")
                print("   aws configure")
                print("\n2. Environment Variables:")
                print("   export AWS_ACCESS_KEY_ID=your_access_key")
                print("   export AWS_SECRET_ACCESS_KEY=your_secret_key")
                print("\n3. IAM Role (for EC2 instances)")
                print("   Attach an IAM role to your EC2 instance")
                input("\nPress Enter to continue...")
            continue

        print(f"\nAWS Identity: {identity['arn']}")
        print(f"Account ID: {identity['account_id']}")
        print(f"Partition: {identity['partition_name']}")

        print("\nPermission Status:")
        print(f"  Required permissions: {_permission_status['required_passed']}/{_permission_status['required_passed'] + _permission_status['required_failed']} passed")
        print(f"  Optional permissions: {_permission_status['optional_passed']}/{_permission_status['optional_passed'] + _permission_status['optional_failed']} passed")

        if _permission_status['has_required']:
            print("\n✅ All required permissions are available!")
            if _permission_status['optional_failed'] > 0:
                print(f"⚠️  {_permission_status['optional_failed']} optional permissions are missing.")
                print("Some advanced features may not be available.")
        else:
            print(f"\n❌ {_permission_status['required_failed']} required permissions are missing!")
            print("StratusScan scripts may fail without these permissions.")

        try:
            choice = utils.prompt_menu(
                "Select an option",
                [
                    "Show policy recommendations",
                    "View policy file locations",
                    "Re-test permissions",
                ],
            )
        except utils.BackSignal:
            return
        except (utils.ExitToMainSignal, utils.QuitSignal):
            return

        if choice == 1:
            show_policy_recommendations_brief()
        elif choice == 2:
            show_policy_file_locations()
        elif choice == 3:
            print("\n🔄 Re-testing permissions...")
            continue

def show_policy_recommendations_brief():
    """Show brief policy recommendations."""
    script_dir = Path(__file__).parent.absolute()
    required_policy_path = script_dir / "policies" / "stratusscan-required-permissions.json"
    optional_policy_path = script_dir / "policies" / "stratusscan-optional-permissions.json"

    print("\n" + "═" * 70)
    print("IAM POLICY RECOMMENDATIONS")
    print("═" * 70)

    print("\nOption 1: CUSTOM POLICIES (Recommended - Least Privilege)")
    print("─" * 70)
    print(f"Required Policy: {required_policy_path}")
    print("  - Covers 100+ export scripts across 80+ AWS services")
    print("  - Read-only permissions only (Get*, Describe*, List*)")
    print("  - ~250 specific actions for precise access control")
    print("")
    print(f"Optional Policy: {optional_policy_path}")
    print("  - Advanced features: Security Hub, Cost Optimization, Trusted Advisor")
    print("  - ~60 additional actions for optional functionality")

    print("\n" + "─" * 70)
    print("Option 2: AWS MANAGED POLICIES (Simpler but broader)")
    print("─" * 70)
    print("  - ReadOnlyAccess (covers most needs)")
    print("  - SecurityAudit (for security services)")
    print("  - AWSBillingReadOnlyAccess (for cost data)")

    print("\nFor detailed instructions, see:")
    print(f"  {script_dir / 'policies' / 'README.md'}")

    input("\nPress Enter to continue...")

def show_policy_file_locations():
    """Show policy file locations."""
    script_dir = Path(__file__).parent.absolute()

    print("\n" + "═" * 70)
    print("STRATUSSCAN POLICY FILES")
    print("═" * 70)
    print("\nRequired Permissions Policy (Core Functionality):")
    print(f"  {script_dir / 'policies' / 'stratusscan-required-permissions.json'}")
    print("  Covers: EC2, S3, RDS, VPC, IAM, Lambda, and 70+ other services")
    print("  Actions: ~250 read-only permissions")
    print("")
    print("Optional Permissions Policy (Advanced Features):")
    print(f"  {script_dir / 'policies' / 'stratusscan-optional-permissions.json'}")
    print("  Covers: Security Hub, Cost Optimization, Trusted Advisor, Identity Center")
    print("  Actions: ~60 read-only permissions")
    print("")
    print("Complete Documentation:")
    print(f"  {script_dir / 'policies' / 'README.md'}")
    print("  Includes: Usage instructions, troubleshooting, GovCloud considerations")

    input("\nPress Enter to continue...")

# ============================================================================
# QUICK EDIT CLI FUNCTIONS
# ============================================================================

def quick_edit_account(account_id: str, account_name: str) -> bool:
    """
    Quick add/edit account mapping via CLI.

    Args:
        account_id (str): AWS account ID
        account_name (str): Friendly name

    Returns:
        bool: True if successful
    """
    if not validate_account_id(account_id):
        print(f"\n❌ Invalid account ID: {account_id}")
        print("Account ID must be exactly 12 digits (e.g., 123456789012)")
        return False

    config_path = get_config_path()
    config = load_existing_config(config_path)

    if 'account_mappings' not in config:
        config['account_mappings'] = {}

    config['account_mappings'][account_id] = account_name

    if save_configuration(config, config_path):
        print(f"\n✅ Account mapping added: {account_id} → {account_name}")
        return True
    else:
        print("\n❌ Failed to add account mapping")
        return False

def quick_edit_region(region: str) -> bool:
    """
    Quick edit default region via CLI.

    Args:
        region (str): AWS region code

    Returns:
        bool: True if successful
    """
    config_path = get_config_path()
    config = load_existing_config(config_path)

    # Determine secondary region based on partition
    if region.startswith('us-gov-'):
        secondary_region = "us-gov-east-1" if region == "us-gov-west-1" else "us-gov-west-1"
    else:
        secondary_region = "us-west-2" if region == "us-east-1" else "us-east-1"

    config['default_regions'] = [region, secondary_region]

    if save_configuration(config, config_path):
        print(f"\n✅ Default regions updated: {region}, {secondary_region}")
        return True
    else:
        print("\n❌ Failed to update default regions")
        return False

def validate_only() -> bool:
    """
    Run validation checks only (deps + perms + config).

    Returns:
        bool: True if all checks pass
    """
    print_box("STRATUSSCAN VALIDATION CHECK", 70)

    # Check dependencies
    print("\n" + "═" * 70)
    print("1. DEPENDENCY CHECK")
    print("═" * 70)
    dep_status = check_dependencies_silent()

    for package in dep_status['installed_packages']:
        print(f"  ✅ {package['name']}")

    for package in dep_status['missing_packages']:
        print(f"  ❌ {package['name']}")

    print(f"\nSummary: {dep_status['installed_count']}/{dep_status['total_count']} dependencies satisfied")

    # Check permissions
    print("\n" + "═" * 70)
    print("2. AWS PERMISSIONS CHECK")
    print("═" * 70)
    perm_status = check_permissions_silent()

    identity = get_aws_identity()
    if identity:
        print(f"\nAWS Identity: {identity['arn']}")
        print(f"Account ID: {identity['account_id']}")
        print(f"Partition: {identity['partition_name']}")
        print(f"\nRequired permissions: {perm_status['required_passed']}/{perm_status['required_passed'] + perm_status['required_failed']} passed")
        print(f"Optional permissions: {perm_status['optional_passed']}/{perm_status['optional_passed'] + perm_status['optional_failed']} passed")
    else:
        print("\n❌ No AWS credentials found")

    # Check configuration
    print("\n" + "═" * 70)
    print("3. CONFIGURATION CHECK")
    print("═" * 70)
    config_path = get_config_path()

    if config_path.exists():
        config = load_existing_config(config_path)
        print(f"\n✅ Configuration file exists: {config_path}")
        print(f"Account mappings: {len(config.get('account_mappings', {}))} configured")
        print(f"Default regions: {', '.join(config.get('default_regions', []))}")
    else:
        print(f"\n❌ Configuration file not found: {config_path}")

    # Overall status
    print("\n" + "═" * 70)
    print("OVERALL STATUS")
    print("═" * 70)

    all_ok = (
        dep_status['all_satisfied'] and
        (perm_status['has_required'] if identity else False) and
        config_path.exists()
    )

    if all_ok:
        print("\n✅ All checks passed! StratusScan is ready to use.")
        return True
    else:
        print("\n⚠️  Some checks failed. Review the output above for details.")
        if not dep_status['all_satisfied']:
            print("  - Install missing dependencies: python configure.py --deps")
        if not (perm_status['has_required'] if identity else False):
            print("  - Fix AWS permissions: python configure.py --perms")
        if not config_path.exists():
            print("  - Run configuration: python configure.py")
        return False

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main():
    """Main function to run the configuration tool."""
    try:
        # Run background checks
        run_background_checks()

        # Load configuration
        config_path = get_config_path()
        is_fresh = not config_path.exists()
        config = load_existing_config(config_path)

        # On a brand-new config in GovCloud, override the commercial region defaults
        if is_fresh:
            identity = get_aws_identity()
            if identity and identity['partition'] == 'aws-us-gov':
                config['default_regions'] = ['us-gov-west-1', 'us-gov-east-1']

        # Start main menu loop
        main_menu_loop(config, config_path)

    except KeyboardInterrupt:
        print("\n\n❌ Configuration cancelled by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ An unexpected error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    # Check for command-line arguments
    if len(sys.argv) > 1:
        arg = sys.argv[1]

        if arg in ['--deps', '--dependencies']:
            # Quiet dependency check — print status and exit
            print_box("STRATUSSCAN DEPENDENCY CHECK", 70)
            dep_status = check_dependencies_silent()
            for package in dep_status['installed_packages']:
                print(f"  ✅ {package['name']} - {package['description']}")
            for package in dep_status['missing_packages']:
                print(f"  ❌ {package['name']} - {package['description']}")
            sdk = utils.check_sdk_floor()
            sdk_have = f"boto3 {sdk['installed']['boto3']} / botocore {sdk['installed']['botocore']}"
            if sdk['ok']:
                print(f"  {utils.GLYPH_OK} AWS SDK {sdk_have} (floor boto3 {sdk['required']['boto3']})")
            else:
                print(f"  {utils.GLYPH_FAIL} AWS SDK {sdk_have} is below floor boto3 {sdk['required']['boto3']}")
                print(f"     Upgrade: {sdk['upgrade_command']}")
            print(f"\nSummary: {dep_status['installed_count']}/{dep_status['total_count']} dependencies satisfied")
            if dep_status['all_satisfied']:
                print("\n✅ All dependencies are installed.")
            else:
                missing_names = " ".join(p['name'] for p in dep_status['missing_packages'])
                print(f"\nInstall missing: pip install {missing_names}")
            sys.exit(0 if dep_status['all_satisfied'] and sdk['ok'] else 1)

        elif arg in ['--perms', '--permissions']:
            # Run permissions check only
            print_box("STRATUSSCAN PERMISSIONS CHECK", 70)
            run_background_checks()
            permissions_management_menu()
            sys.exit(0)

        elif arg == '--account' and len(sys.argv) == 4:
            # Quick add account mapping
            success = quick_edit_account(sys.argv[2], sys.argv[3])
            sys.exit(0 if success else 1)

        elif arg == '--region' and len(sys.argv) == 3:
            # Quick edit default region
            success = quick_edit_region(sys.argv[2])
            sys.exit(0 if success else 1)

        elif arg == '--validate':
            # Full validation check
            success = validate_only()
            sys.exit(0 if success else 1)

        elif arg in ['--help', '-h']:
            print("StratusScan Configuration Tool v0.2.0")
            print("\nUsage:")
            print("  python configure.py                              # Interactive dashboard")
            print("  python configure.py --deps                       # Dependency check (quiet)")
            print("  python configure.py --perms                      # Permissions check only")
            print("  python configure.py --account ID NAME            # Quick account mapping")
            print("  python configure.py --region REGION              # Quick region update")
            print("  python configure.py --validate                   # Full validation check")
            print("  python configure.py --help                       # Show this help")
            print("\nExamples:")
            print("  python configure.py --account 123456789012 Production")
            print("  python configure.py --region us-east-1")
            sys.exit(0)

        else:
            print(f"❌ Unknown argument: {arg}")
            print("Use --help for usage information.")
            sys.exit(1)

    # Run full interactive configuration
    main()
