#!/usr/bin/env python3
"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS EFS (Elastic File System) Export Tool
Date: NOV-09-2025

Description:
This script exports AWS EFS file system information from all regions into an Excel file with
multiple worksheets. The output includes file system configurations, mount targets, access points,
backup policies, lifecycle policies, and performance settings.

Features:
- File system overview with size, performance mode, and throughput
- Mount targets with availability zones and IP addresses
- Access points for application-specific access
- Backup policies and replication configurations
- Lifecycle management policies
- Encryption settings and KMS keys
- File system policies
"""

import datetime
import json
import sys
from pathlib import Path
from typing import Any

# Add path to import utils module
try:
    import utils
except ImportError:
    script_dir = Path(__file__).parent.absolute()

    if script_dir.name.lower() == 'scripts':
        sys.path.append(str(script_dir.parent))
    else:
        sys.path.append(str(script_dir))

    try:
        import utils
    except ImportError:
        print("ERROR: Could not import the utils module. Make sure utils.py is in the StratusScan directory.")
        sys.exit(1)
args = utils.parse_script_args("Export Elastic File System volumes to Excel")


def load_efs_pricing_data() -> dict[str, float]:
    """Load EFS storage tier rates from pricing JSON."""
    pricing_file = Path(__file__).parent.parent / 'reference' / 'efs-pricing.json'
    defaults = {
        'regional_standard_per_gb_month': 0.30,
        'regional_ia_per_gb_month': 0.025,
        'one_zone_standard_per_gb_month': 0.16,
        'one_zone_ia_per_gb_month': 0.01,
    }
    try:
        with open(pricing_file, encoding='utf-8') as fh:
            data = json.load(fh)
        rates = data.get('rates', {})
        if rates:
            return {k: float(v) for k, v in rates.items()}
    except Exception:
        pass
    return defaults


def calculate_efs_monthly_cost(
    size_standard_bytes: int,
    size_ia_bytes: int,
    availability_zone: str,
    pricing: dict[str, float],
) -> float:
    """Return estimated monthly cost for an EFS file system based on storage tiers."""
    is_one_zone = availability_zone not in ('Regional', 'N/A', '')
    if is_one_zone:
        standard_rate = pricing.get('one_zone_standard_per_gb_month', 0.16)
        ia_rate = pricing.get('one_zone_ia_per_gb_month', 0.01)
    else:
        standard_rate = pricing.get('regional_standard_per_gb_month', 0.30)
        ia_rate = pricing.get('regional_ia_per_gb_month', 0.025)

    standard_gb = (size_standard_bytes or 0) / (1024 ** 3)
    ia_gb = (size_ia_bytes or 0) / (1024 ** 3)
    return round(standard_gb * standard_rate + ia_gb * ia_rate, 4)


def _build_filesystem_row(fs: dict, region: str, pricing: dict[str, float]) -> dict[str, Any]:
    """Build a single EFS file system export row from a describe response."""
    file_system_id = fs.get('FileSystemId', '')
    print(f"  Processing file system: {file_system_id}")

    # Basic information
    name = fs.get('Name', 'N/A')
    creation_token = fs.get('CreationToken', '')
    creation_time = fs.get('CreationTime', '')
    if creation_time:
        creation_time = creation_time.strftime('%Y-%m-%d %H:%M:%S') if isinstance(creation_time, datetime.datetime) else str(creation_time)

    life_cycle_state = fs.get('LifeCycleState', '')
    number_of_mount_targets = fs.get('NumberOfMountTargets', 0)

    # Size
    size_in_bytes = fs.get('SizeInBytes', {})
    value_bytes = size_in_bytes.get('Value', 0)
    value_gb = round(value_bytes / (1024**3), 2) if value_bytes else 0
    value_in_ia = size_in_bytes.get('ValueInIA', 0)
    value_in_standard = size_in_bytes.get('ValueInStandard', 0)

    # Performance mode
    performance_mode = fs.get('PerformanceMode', 'N/A')

    # Encrypted
    encrypted = fs.get('Encrypted', False)
    kms_key_id = fs.get('KmsKeyId', 'N/A')

    # Throughput mode
    throughput_mode = fs.get('ThroughputMode', 'N/A')
    provisioned_throughput = fs.get('ProvisionedThroughputInMibps', 'N/A')

    # Availability zone (for One Zone storage)
    availability_zone_name = fs.get('AvailabilityZoneName', 'Regional')

    # File system ARN
    file_system_arn = fs.get('FileSystemArn', '')

    # Tags
    tags = fs.get('Tags', [])
    tag_dict = {tag.get('Key'): tag.get('Value') for tag in tags if 'Key' in tag and 'Value' in tag}
    tags_str = ', '.join([f"{k}={v}" for k, v in tag_dict.items()]) if tag_dict else 'N/A'

    monthly_cost = calculate_efs_monthly_cost(
        value_in_standard, value_in_ia, availability_zone_name, pricing
    )

    return {
        'Region': region,
        'File System ID': file_system_id,
        'Name': name,
        'Life Cycle State': life_cycle_state,
        'Size (GB)': value_gb,
        'Size in IA (bytes)': value_in_ia,
        'Size in Standard (bytes)': value_in_standard,
        'Performance Mode': performance_mode,
        'Throughput Mode': throughput_mode,
        'Provisioned Throughput (MiB/s)': provisioned_throughput,
        'Encrypted': encrypted,
        'KMS Key ID': kms_key_id,
        'Availability Zone': availability_zone_name,
        'Mount Target Count': number_of_mount_targets,
        'Creation Time': creation_time,
        'Creation Token': creation_token,
        'Tags': tags_str,
        'File System ARN': file_system_arn,
        'Monthly Cost (On-Demand)': monthly_cost,
        'Cost Note': 'Estimate (us-east-1 on-demand; storage only; excludes throughput)',
    }


def _scan_efs_region(region: str) -> list[dict[str, Any]]:
    """
    Collect EFS file systems from a single region.

    This is the primary scope collector. It deliberately does NOT swallow
    errors: an API/permission failure here must propagate so
    ``scan_regions_concurrent(..., collect_failures=True)`` records the
    region as failed instead of silently reporting "no EFS file systems"
    (the silent-collection-loss bug — see
    ``.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md``).

    Individual malformed file systems are skipped (logged) rather than
    aborting the whole region.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    pricing = load_efs_pricing_data()
    efs_client = utils.get_boto3_client('efs', region_name=region)

    # Get file systems
    paginator = efs_client.get_paginator('describe_file_systems')
    regional_file_systems = []
    fs_count = 0

    for page in paginator.paginate():
        file_systems = page.get('FileSystems', [])
        fs_count += len(file_systems)

        for fs in file_systems:
            try:
                regional_file_systems.append(_build_filesystem_row(fs, region, pricing))
            except Exception as e:
                # One malformed file system is skipped, not fatal to the region.
                utils.log_error(
                    f"Skipping malformed EFS file system in {region}: "
                    f"{fs.get('FileSystemId', '<unknown>')}",
                    e,
                )
                continue

    utils.log_info(f"Found {fs_count} EFS file systems in {region}")
    return regional_file_systems


def collect_efs_file_systems(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect EFS file system information across regions, surfacing failures.

    Uses ``collect_failures=True`` so a region whose collection errors is
    reported as a failed scope rather than silently collapsed into an empty
    result.

    Returns:
        tuple: ``(file_systems, failed_regions)`` where ``failed_regions`` is
        a list of ``(region, error_message)`` tuples.
    """
    print("\n=== COLLECTING EFS FILE SYSTEMS ===")

    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_efs_region,
        show_progress=True,
        collect_failures=True,
    )
    all_file_systems = [fs for result in region_results for fs in result]

    utils.log_success(f"Total EFS file systems collected: {len(all_file_systems)}")
    return all_file_systems, failed_regions


def scan_mount_targets_in_region(region: str) -> list[dict[str, Any]]:
    """
    Scan EFS mount targets in a single region.

    Args:
        region: AWS region to scan

    Returns:
        list: List of dictionaries with mount target information from this region
    """
    regional_mount_targets = []

    try:
        efs_client = utils.get_boto3_client('efs', region_name=region)

        # Get all file systems first
        fs_paginator = efs_client.get_paginator('describe_file_systems')

        for fs_page in fs_paginator.paginate():
            file_systems = fs_page.get('FileSystems', [])

            for fs in file_systems:
                file_system_id = fs.get('FileSystemId', '')

                try:
                    # Get mount targets for this file system
                    mt_response = efs_client.describe_mount_targets(FileSystemId=file_system_id)
                    mount_targets = mt_response.get('MountTargets', [])

                    for mt in mount_targets:
                        mount_target_id = mt.get('MountTargetId', '')
                        subnet_id = mt.get('SubnetId', '')
                        availability_zone_name = mt.get('AvailabilityZoneName', '')
                        ip_address = mt.get('IpAddress', 'N/A')
                        network_interface_id = mt.get('NetworkInterfaceId', 'N/A')
                        life_cycle_state = mt.get('LifeCycleState', '')
                        vpc_id = mt.get('VpcId', 'N/A')

                        regional_mount_targets.append({
                            'Region': region,
                            'File System ID': file_system_id,
                            'Mount Target ID': mount_target_id,
                            'Availability Zone': availability_zone_name,
                            'Subnet ID': subnet_id,
                            'VPC ID': vpc_id,
                            'IP Address': ip_address,
                            'Network Interface ID': network_interface_id,
                            'Life Cycle State': life_cycle_state
                        })

                except Exception as e:
                    utils.log_warning(f"Could not get mount targets for {file_system_id}: {e}")

        utils.log_info(f"Found {len(regional_mount_targets)} mount targets in {region}")

    except Exception as e:
        utils.log_error(f"Error collecting mount targets in region {region}", e)

    return regional_mount_targets


@utils.aws_error_handler("Collecting mount targets", default_return=[])
def collect_mount_targets(regions: list[str]) -> list[dict[str, Any]]:
    """
    Collect EFS mount target information from AWS regions using concurrent scanning.

    Args:
        regions: List of AWS regions to scan

    Returns:
        list: List of dictionaries with mount target information
    """
    print("\n=== COLLECTING EFS MOUNT TARGETS ===")
    utils.log_info("Using concurrent region scanning for improved performance")

    # Use concurrent scanning
    all_mount_targets = []
    for region_data in utils.scan_regions_concurrent(
        regions=regions,
        scan_function=scan_mount_targets_in_region,
        show_progress=True
    ):
        all_mount_targets.extend(region_data)

    utils.log_success(f"Total mount targets collected: {len(all_mount_targets)}")
    return all_mount_targets


def scan_access_points_in_region(region: str) -> list[dict[str, Any]]:
    """
    Scan EFS access points in a single region.

    Args:
        region: AWS region to scan

    Returns:
        list: List of dictionaries with access point information from this region
    """
    regional_access_points = []

    try:
        efs_client = utils.get_boto3_client('efs', region_name=region)

        # Get access points
        access_points = []
        for page in efs_client.get_paginator('describe_access_points').paginate():
            access_points.extend(page.get('AccessPoints', []))

        for ap in access_points:
            access_point_id = ap.get('AccessPointId', '')
            file_system_id = ap.get('FileSystemId', '')
            name = ap.get('Name', 'N/A')
            life_cycle_state = ap.get('LifeCycleState', '')

            # POSIX user
            posix_user = ap.get('PosixUser', {})
            uid = str(posix_user.get('Uid', 'N/A'))
            gid = str(posix_user.get('Gid', 'N/A'))

            # Root directory
            root_directory = ap.get('RootDirectory', {})
            path = root_directory.get('Path', '/')

            # ARN
            access_point_arn = ap.get('AccessPointArn', '')

            # Tags
            tags = ap.get('Tags', [])
            tag_dict = {tag['Key']: tag['Value'] for tag in tags if 'Key' in tag and 'Value' in tag}
            tags_str = ', '.join([f"{k}={v}" for k, v in tag_dict.items()]) if tag_dict else 'N/A'

            regional_access_points.append({
                'Region': region,
                'File System ID': file_system_id,
                'Access Point ID': access_point_id,
                'Name': name,
                'Life Cycle State': life_cycle_state,
                'Root Path': path,
                'POSIX User UID': uid,
                'POSIX Group GID': gid,
                'Tags': tags_str,
                'Access Point ARN': access_point_arn
            })

        utils.log_info(f"Found {len(regional_access_points)} access points in {region}")

    except Exception as e:
        utils.log_error(f"Error collecting access points in region {region}", e)

    return regional_access_points


@utils.aws_error_handler("Collecting access points", default_return=[])
def collect_access_points(regions: list[str]) -> list[dict[str, Any]]:
    """
    Collect EFS access point information from AWS regions using concurrent scanning.

    Args:
        regions: List of AWS regions to scan

    Returns:
        list: List of dictionaries with access point information
    """
    print("\n=== COLLECTING EFS ACCESS POINTS ===")
    utils.log_info("Using concurrent region scanning for improved performance")

    # Use concurrent scanning
    all_access_points = []
    for region_data in utils.scan_regions_concurrent(
        regions=regions,
        scan_function=scan_access_points_in_region,
        show_progress=True
    ):
        all_access_points.extend(region_data)

    utils.log_success(f"Total access points collected: {len(all_access_points)}")
    return all_access_points


def export_efs_data(account_id: str, account_name: str):
    """
    Export EFS information to an Excel file.

    Args:
        account_id: The AWS account ID
        account_name: The AWS account name
    """
    # Detect partition for region examples
    regions = utils.prompt_region_selection()
    region_suffix = 'all'
    # Import pandas for DataFrame handling
    import pandas as pd

    # Dictionary to hold all DataFrames for export
    data_frames = {}

    # STEP 1: Collect file systems (primary scope — region failures must
    # propagate as failed_regions, never collapse into "empty").
    file_systems, failed_regions = collect_efs_file_systems(regions)
    if file_systems:
        data_frames['File Systems'] = pd.DataFrame(file_systems)

    # STEP 2: Collect mount targets (enrichment — degrades gracefully; a
    # region-level failure here does not fail the whole export).
    mount_targets = collect_mount_targets(regions)
    if mount_targets:
        data_frames['Mount Targets'] = pd.DataFrame(mount_targets)

    # STEP 3: Collect access points (enrichment — degrades gracefully; a
    # region-level failure here does not fail the whole export).
    access_points = collect_access_points(regions)
    if access_points:
        data_frames['Access Points'] = pd.DataFrame(access_points)

    # Export whatever succeeded first — a partial export is required even
    # when some regions failed (see the silent-collection-failure blast-radius audit).
    if data_frames:
        # STEP 4: Prepare all DataFrames for export
        for sheet_name in data_frames:
            data_frames[sheet_name] = utils.prepare_dataframe_for_export(data_frames[sheet_name])

        # STEP 5: Create filename and export
        current_date = datetime.datetime.now().strftime("%m.%d.%Y")
        final_excel_file = utils.create_export_filename(
            account_name,
            'efs',
            region_suffix,
            current_date
        )

        # Save using utils module for consistent formatting
        try:
            output_path = utils.save_multiple_dataframes_to_excel(data_frames, final_excel_file)

            if output_path:
                utils.log_success("EFS data exported successfully!")
                utils.log_success(f"File location: {output_path}")
                utils.log_info(f"Export contains data from {len(regions)} AWS region(s)")

                # Summary of exported data
                for sheet_name, df in data_frames.items():
                    utils.log_info(f"  - {sheet_name}: {len(df)} records")
                    print(f"  - {sheet_name}: {len(df)} records")
            else:
                utils.log_error("Error creating Excel file. Please check the logs.")

        except Exception as e:
            utils.log_error("Error creating Excel file", e)
    elif not failed_regions:
        # Genuinely empty account: every region succeeded and returned nothing.
        utils.log_warning("No EFS data was collected. Nothing to export.")
        print("\nNo EFS file systems found in the selected region(s).")

    # If ANY region failed the File Systems scope collection, make it loud:
    # write a marker and exit non-zero, even if some data was exported. A
    # partial export that looks complete is exactly the failure mode this guards.
    if failed_regions:
        utils.report_collection_failures(account_name, 'efs', failed_regions)
        print(
            "\nERROR: EFS export completed with failures — data is incomplete. "
            "See the *-efs-FAILED-*.txt marker in the output directory."
        )
        sys.exit(1)


def main():
    # Initialize logging
    utils.setup_logging("efs-export")
    SCRIPT_START_TIME = datetime.datetime.now()
    utils.log_script_start("efs-export.py", "AWS EFS Export Tool")

    try:
        # Print title and get account information
        account_id, account_name = utils.print_script_banner("AWS EFS (ELASTIC FILE SYSTEM) EXPORT")

        # Check and install dependencies
        if not utils.ensure_dependencies('pandas', 'openpyxl'):
            sys.exit(1)

        # Check if account name is unknown
        if account_name == "unknown" and not utils.prompt_for_confirmation("Unable to determine account name. Proceed anyway?", default=False):
            print("Exiting script...")
            sys.exit(0)

        # Export EFS data
        export_efs_data(account_id, account_name)

        print("\nEFS export script execution completed.")

    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        utils.log_info("Script cancelled by user")
        sys.exit(1)
    except Exception as e:
        utils.log_error("An unexpected error occurred", e)
        sys.exit(1)
    finally:
        utils.log_script_end("efs-export.py", SCRIPT_START_TIME)


if __name__ == "__main__":
    main()
