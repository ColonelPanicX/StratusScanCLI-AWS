#!/usr/bin/env python3

"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS S3 Bucket Inventory Export
Date: NOV-15-2025

Description:
This script exports information about S3 buckets across AWS regions including
bucket name, region, creation date, and total object count. Bucket sizes are retrieved
using S3 Storage Lens where available. The data is exported to a spreadsheet file with
a standardized naming convention including AWS identifiers for compliance and audit purposes.

Phase 4B Update:
- Concurrent region scanning for CloudWatch metrics collection
- Automatic fallback to sequential on errors
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Optional

# Add path to import utils module
try:
    # Try to import directly (if utils.py is in Python path)
    import utils
except ImportError:
    # If import fails, try to find the module relative to this script
    script_dir = Path(__file__).parent.absolute()

    # Check if we're in the scripts directory
    if script_dir.name.lower() == 'scripts':
        # Add the parent directory (StratusScan root) to the path
        sys.path.append(str(script_dir.parent))
    else:
        # Add the current directory to the path
        sys.path.append(str(script_dir))

    # Try import again
    try:
        import utils
    except ImportError:
        print("ERROR: Could not import the utils module. Make sure utils.py is in the StratusScan directory.")
        sys.exit(1)
args = utils.parse_script_args("Export S3 buckets inventory to Excel")

def is_valid_aws_region(region_name):
    """
    Check if a region name is a valid AWS region

    Args:
        region_name (str): The region name to validate

    Returns:
        bool: True if valid, False otherwise
    """
    return utils.is_aws_region(region_name)

@utils.aws_error_handler("Getting bucket region", default_return="unknown")
def get_bucket_region(bucket_name):
    """
    Determine the region of a specific S3 bucket

    Args:
        bucket_name (str): Name of the S3 bucket

    Returns:
        str: AWS region name of the bucket
    """
    # Create S3 client using utils
    s3_client = utils.get_boto3_client('s3')

    # Get the bucket's location
    response = s3_client.get_bucket_location(Bucket=bucket_name)
    location = response.get('LocationConstraint')

    # In AWS, handle the location constraint differently
    if location is None:
        # AWS returns None for buckets in us-east-1 (the S3 classic region)
        return 'us-east-1'
    return location

@utils.aws_error_handler("Getting bucket object count", default_return=0)
def get_bucket_object_count(bucket_name, region):
    """
    Get the approximate number of objects in a bucket using CloudWatch S3 metrics.

    Uses the CloudWatch NumberOfObjects metric (updated daily by AWS) which is far
    cheaper and faster than full enumeration via list_objects_v2.

    Args:
        bucket_name (str): Name of the S3 bucket
        region (str): AWS region where the bucket is located

    Returns:
        int: Total number of objects (approximate, from last daily metric point)
    """
    cw_client = utils.get_boto3_client('cloudwatch', region_name=region)  # S3 storage metrics live in the bucket's region

    end_time = datetime.datetime.utcnow()
    start_time = end_time - datetime.timedelta(days=3)

    response = cw_client.get_metric_statistics(
        Namespace='AWS/S3',
        MetricName='NumberOfObjects',
        Dimensions=[
            {'Name': 'BucketName', 'Value': bucket_name},
            {'Name': 'StorageType', 'Value': 'AllStorageTypes'},
        ],
        StartTime=start_time,
        EndTime=end_time,
        Period=86400,
        Statistics=['Average']
    )

    datapoints = response.get('Datapoints', [])
    if datapoints:
        latest = sorted(datapoints, key=lambda x: x['Timestamp'])[-1]
        return int(latest['Average'])

    return 0

def check_storage_lens_availability():
    """
    Check if S3 Storage Lens is configured and available in AWS

    Returns:
        bool: True if Storage Lens is available, False otherwise
    """
    try:
        # Create S3 Control client in a AWS region
        # S3Control is a global service - use partition-aware home region
        home_region = utils.get_partition_default_region()
        s3control_client = utils.get_boto3_client('s3control', region_name=home_region)

        # Get caller identity for Account ID
        account_id = utils.get_boto3_client('sts').get_caller_identity()["Account"]

        # List Storage Lens configurations
        response = s3control_client.list_storage_lens_configurations(
            AccountId=account_id
        )

        # Check if there are any Storage Lens configurations
        if 'StorageLensConfigurationList' in response and len(response['StorageLensConfigurationList']) > 0:
            utils.log_info("Found S3 Storage Lens configurations. Will attempt to use for bucket metrics.")
            return True
        else:
            utils.log_info("No S3 Storage Lens configurations found. Will use standard object counting for metrics.")
            return False
    except Exception as e:
        utils.log_warning(f"Error checking Storage Lens availability: {e}")
        utils.log_info("Will use standard object counting for metrics.")
        return False

def get_latest_storage_lens_data(account_id):
    """
    Get the latest available Storage Lens data from AWS

    Args:
        account_id (str): AWS account ID

    Returns:
        dict: Dictionary mapping bucket names to their metrics
    """
    try:
        # Create S3 Control client in AWS region
        # S3Control is a global service - use partition-aware home region
        home_region = utils.get_partition_default_region()
        s3control_client = utils.get_boto3_client('s3control', region_name=home_region)

        # List Storage Lens configurations
        configurations = s3control_client.list_storage_lens_configurations(
            AccountId=account_id
        )

        if 'StorageLensConfigurationList' not in configurations or len(configurations['StorageLensConfigurationList']) == 0:
            return {}

        # Try to get data from CloudWatch metrics in AWS
        latest_data = {}

        # Try to get data for yesterday (Storage Lens data is available the next day)
        today = datetime.datetime.now()
        yesterday = today - datetime.timedelta(days=1)

        # Get list of all buckets (global S3 call)
        s3_client = utils.get_boto3_client('s3')
        all_bucket_names = [
            bucket.get('Name') for bucket in s3_client.list_buckets().get('Buckets', [])
            if bucket.get('Name')
        ]

        # Define function to collect metrics for a single region (Phase 4B)
        def collect_cloudwatch_metrics_for_region(region):
            region_data = {}
            try:
                cw_client = utils.get_boto3_client('cloudwatch', region_name=region)

                for bucket_name in all_bucket_names:
                    # Skip if we already have data for this bucket
                    if bucket_name in latest_data:
                        continue

                    try:
                        # Get BucketSizeBytes metric
                        size_response = cw_client.get_metric_statistics(
                            Namespace='AWS/S3',
                            MetricName='BucketSizeBytes',
                            Dimensions=[
                                {'Name': 'BucketName', 'Value': bucket_name},
                                {'Name': 'StorageType', 'Value': 'StandardStorage'}
                            ],
                            StartTime=yesterday - datetime.timedelta(days=1),
                            EndTime=today,
                            Period=86400,
                            Statistics=['Average']
                        )

                        # Get NumberOfObjects metric
                        objects_response = cw_client.get_metric_statistics(
                            Namespace='AWS/S3',
                            MetricName='NumberOfObjects',
                            Dimensions=[
                                {'Name': 'BucketName', 'Value': bucket_name},
                                {'Name': 'StorageType', 'Value': 'AllStorageTypes'}
                            ],
                            StartTime=yesterday - datetime.timedelta(days=1),
                            EndTime=today,
                            Period=86400,
                            Statistics=['Average']
                        )

                        # Process metrics if available
                        size_bytes = 0
                        obj_count = 0

                        if 'Datapoints' in size_response and len(size_response['Datapoints']) > 0:
                            size_bytes = size_response['Datapoints'][0]['Average']

                        if 'Datapoints' in objects_response and len(objects_response['Datapoints']) > 0:
                            obj_count = int(objects_response['Datapoints'][0]['Average'])

                        if size_bytes > 0 or obj_count > 0:
                            region_data[bucket_name] = {
                                'size_bytes': size_bytes,
                                'object_count': obj_count
                            }

                    except Exception as e:
                        utils.log_warning(f"Error getting metrics for bucket {bucket_name} in region {region}: {e}")
                        continue

            except Exception as e:
                utils.log_warning(f"Error getting CloudWatch metrics in region {region}: {e}")

            return region_data

        # Use concurrent region scanning for CloudWatch metrics (Phase 4B)
        aws_regions = utils.get_aws_regions()
        region_results = utils.scan_regions_concurrent(
            regions=aws_regions,
            scan_function=collect_cloudwatch_metrics_for_region,
            max_workers=2,
            show_progress=False  # Don't show progress for S3 metrics collection
        )

        # Merge all region data
        for region_data in region_results:
            latest_data.update(region_data)

        return latest_data

    except Exception as e:
        utils.log_error("Error retrieving Storage Lens data", e)
        return {}

def convert_to_mb(size_in_bytes):
    """
    Convert bytes to megabytes

    Args:
        size_in_bytes (int or str): Size in bytes or "Not Available"

    Returns:
        float: Size in MB rounded to 2 decimal places, or 0.0 if not available
    """
    if size_in_bytes == "Not Available" or size_in_bytes == 0:
        return 0.0

    # Convert bytes to MB (1 MB = 1024 * 1024 bytes)
    try:
        size_in_mb = float(size_in_bytes) / (1024 * 1024)
        return round(size_in_mb, 2)
    except (ValueError, TypeError):
        return 0.0

def _load_s3_standard_rate() -> Optional[float]:
    """Load S3 Standard storage rate from pricing JSON; None (renders N/A) when unavailable."""
    pricing_file = Path(__file__).parent.parent / 'reference' / 's3-pricing.json'
    try:
        with open(pricing_file, encoding='utf-8') as fh:
            data = json.load(fh)
        rate = data.get('rates', {}).get('STANDARD')
        return float(rate) if rate is not None else None
    except Exception:
        return None


def _build_bucket_row(bucket, region, storage_lens_data, standard_rate, account_id):
    """
    Build the export row for a single S3 bucket.

    Extracted so the per-bucket processing can be wrapped in try/except by the
    caller: a malformed bucket entry (e.g. missing an expected field, or a
    per-field lookup that degrades but still raises unexpectedly) is logged and
    skipped rather than discarding every other bucket already collected. Required
    fields are read with ``.get()`` and a safe default for the same reason.

    Args:
        bucket (dict): A single Buckets entry from list_buckets().
        region (str): AWS region the bucket resolved to.
        storage_lens_data (dict): Bucket-name-keyed Storage Lens/CloudWatch metrics.
        standard_rate (float): S3 Standard storage rate ($/GB/month).
        account_id (str): AWS account ID (for owner formatting).

    Returns:
        dict: The assembled bucket row.
    """
    bucket_name = bucket.get('Name', 'Unknown')
    creation_date = bucket.get('CreationDate', 'N/A')

    # Initialize size and object count
    size_bytes = 0
    object_count = 0

    # Try to get info from Storage Lens if available
    if bucket_name in storage_lens_data:
        size_bytes = storage_lens_data[bucket_name].get('size_bytes', 0)
        object_count = storage_lens_data[bucket_name].get('object_count', 0)
        size_source = "Storage Lens/CloudWatch"
    else:
        # Fall back to counting objects directly
        object_count = get_bucket_object_count(bucket_name, region)
        size_source = "Not Available"

    # Convert size to MB
    size_mb = convert_to_mb(size_bytes)

    # Get owner information
    owner_id = utils.get_account_name_formatted(account_id)

    # Estimate monthly storage cost (Standard tier only)
    if standard_rate is None:
        monthly_cost = 'N/A'
        cost_note = 'S3 Standard rate unavailable'
    elif size_source != "Not Available" and size_mb > 0:
        monthly_cost = round(size_mb / 1024 * standard_rate, 4)
        cost_note = 'Standard storage estimate only'
    elif size_source != "Not Available":
        monthly_cost = 0.0
        cost_note = 'Standard storage estimate only'
    else:
        monthly_cost = 'N/A'
        cost_note = 'Size data unavailable'

    return {
        'Bucket Name': bucket_name,
        'Region': region,
        'Creation Date': creation_date,
        'Object Count': object_count,
        'Size (MB)': size_mb,
        'Size Source': size_source,
        'Owner': owner_id,
        'Monthly Cost (On-Demand)': monthly_cost,
        'Cost Note': cost_note,
    }


def get_s3_buckets_info(use_storage_lens=False, target_region=None):
    """
    Collect information about S3 buckets across AWS regions or a specific AWS region.

    Not wrapped in ``aws_error_handler``: a swallowed error here would return an
    empty list that ``main()`` cannot distinguish from a genuinely empty account,
    producing silent data loss (no file written). Top-level failures (e.g. a
    throttled ``list_buckets`` call) are allowed to raise so the caller sees a
    real failure rather than "zero buckets." Per-bucket errors are contained
    internally: they are logged, the bucket is skipped, and it is reported back
    to the caller via the ``failed_buckets`` return value instead of aborting the
    whole collection.

    Args:
        use_storage_lens (bool): Whether to try using Storage Lens for size metrics
        target_region (str): Specific AWS region to target or None for all AWS regions

    Returns:
        tuple: (buckets_info, failed_buckets)
            buckets_info (list): List of dictionaries containing bucket information.
            failed_buckets (list): List of (bucket_name, error_message) tuples for
                buckets that raised while being processed.

    Raises:
        Exception: Any AWS API error not scoped to a single bucket (e.g. listing
            buckets, client creation) propagates to the caller.
    """
    # Initialize global S3 client to list all buckets
    s3_client = utils.get_boto3_client('s3')
    standard_rate = _load_s3_standard_rate()

    all_buckets_info = []
    failed_buckets = []
    storage_lens_data = {}

    # Get account ID
    account_id = utils.get_boto3_client('sts').get_caller_identity()["Account"]

    # Validate target region if specified
    if target_region and not utils.is_aws_region(target_region):
        utils.log_error(f"Invalid AWS region: {target_region}")
        return [], []

    # Try to get Storage Lens data if requested
    if use_storage_lens:
        storage_lens_data = get_latest_storage_lens_data(account_id)

    # Get the list of all buckets. Not guarded here — a failure must propagate
    # (see docstring) rather than be swallowed into an empty result.
    response = s3_client.list_buckets()

    # Filter the buckets based on the target AWS region
    buckets_to_process = []
    for bucket in response.get('Buckets', []):
        bucket_name = bucket.get('Name')
        if not bucket_name:
            continue

        # Get the bucket's region if we need to filter
        if target_region:
            region = get_bucket_region(bucket_name)

            # Only include buckets in the specified AWS region
            if region == target_region:
                buckets_to_process.append(bucket)
        else:
            # For all regions, check if bucket is in any AWS region
            region = get_bucket_region(bucket_name)
            if utils.is_aws_region(region):
                buckets_to_process.append(bucket)

    total_buckets = len(buckets_to_process)
    utils.log_info(f"Found {total_buckets} S3 buckets" +
          (f" in AWS region {target_region}" if target_region else " across all AWS regions") +
          ". Gathering details for each bucket...")

    # Process each bucket. One malformed bucket must not discard every other
    # bucket already collected, so each is built inside try/except; failures
    # are logged, skipped, and tracked in failed_buckets.
    skipped = 0
    for i, bucket in enumerate(buckets_to_process, 1):
        bucket_name = bucket.get('Name', 'Unknown')

        progress = (i / total_buckets) * 100 if total_buckets > 0 else 0
        utils.log_info(f"[{progress:.1f}%] Processing bucket {i}/{total_buckets}: {bucket_name}")

        # Get the bucket's region if we haven't already
        region = target_region or get_bucket_region(bucket_name)

        try:
            bucket_info = _build_bucket_row(
                bucket, region, storage_lens_data, standard_rate, account_id
            )
        except Exception as e:
            skipped += 1
            failed_buckets.append((bucket_name, str(e)))
            utils.log_error(f"Skipping S3 bucket '{bucket_name}' due to a processing error", e)
            continue

        all_buckets_info.append(bucket_info)

    if skipped:
        utils.log_warning(
            f"{skipped} of {total_buckets} S3 bucket(s) were skipped due to "
            "processing errors (see log above); the remaining buckets were still collected."
        )

    return all_buckets_info, failed_buckets

def export_to_excel(buckets_info, account_name, target_region=None):
    """
    Export bucket information to an Excel file with AWS identifier

    Args:
        buckets_info (list): List of dictionaries with bucket information
        account_name (str): Name of the AWS account for file naming
        target_region (str): Specific AWS region being targeted (or None for all)

    Returns:
        str: Path to the created file
    """
    # Import pandas here to avoid issues if it's not installed
    import pandas as pd

    # Create a DataFrame from the bucket information
    df = pd.DataFrame(buckets_info)

    # Prepare and sanitize DataFrame (tags may contain secrets)
    df = utils.sanitize_for_export(
        utils.prepare_dataframe_for_export(df)
    )

    # Reorder columns for better readability
    column_order = [
        'Bucket Name',
        'Region',
        'Creation Date',
        'Object Count',
        'Size (MB)',
        'Size Source',
        'Monthly Cost (On-Demand)',
        'Cost Note',
        'Owner'
    ]

    # Reorder columns (only include columns that exist in the DataFrame)
    available_columns = [col for col in column_order if col in df.columns]
    df = df[available_columns]

    # Format the creation date to be more readable
    if 'Creation Date' in df.columns:
        df['Creation Date'] = df['Creation Date'].dt.strftime('%Y-%m-%d %H:%M:%S')

    # Generate filename with current date and AWS identifier
    current_date = datetime.datetime.now().strftime("%m.%d.%Y")

    # Create region indicator if applicable
    region_suffix = target_region if target_region else None

    # Use utils to create filename and save data with AWS identifier
    filename = utils.create_export_filename(
        account_name,
        "s3-buckets",
        region_suffix,
        current_date
    )

    # Use utils to save DataFrame to Excel
    output_path = utils.save_dataframe_to_excel(df, filename)

    if output_path:
        utils.log_success("AWS S3 data exported successfully!")
        utils.log_success(f"File location: {output_path}")
        return output_path
    else:
        utils.log_error("Error creating Excel file. Attempting to save as CSV instead.")
        # Fallback to CSV if Excel fails
        return export_to_csv(buckets_info, account_name, target_region)

def export_to_csv(buckets_info, account_name, target_region=None):
    """
    Export bucket information to a CSV file with AWS identifier

    Args:
        buckets_info (list): List of dictionaries with bucket information
        account_name (str): Name of the AWS account for file naming
        target_region (str): Specific AWS region being targeted (or None for all)

    Returns:
        str: Path to the created file
    """
    # Import pandas here to avoid issues if it's not installed
    import pandas as pd

    # Create a DataFrame from the bucket information
    df = pd.DataFrame(buckets_info)

    # Prepare and sanitize DataFrame (tags may contain secrets)
    df = utils.sanitize_for_export(
        utils.prepare_dataframe_for_export(df)
    )

    # Reorder columns for better readability
    column_order = [
        'Bucket Name',
        'Region',
        'Creation Date',
        'Object Count',
        'Size (MB)',
        'Size Source',
        'Monthly Cost (On-Demand)',
        'Cost Note',
        'Owner'
    ]

    # Reorder columns (only include columns that exist in the DataFrame)
    available_columns = [col for col in column_order if col in df.columns]
    df = df[available_columns]

    # Format the creation date to be more readable
    if 'Creation Date' in df.columns:
        df['Creation Date'] = df['Creation Date'].dt.strftime('%Y-%m-%d %H:%M:%S')

    # Generate filename with current date
    current_date = datetime.datetime.now().strftime("%m.%d.%Y")

    # Create region indicator if applicable
    region_suffix = f"-{target_region}" if target_region else ""

    # Use utils to get output filepath
    csv_filename = f"{account_name}-aws-s3-buckets{region_suffix}-export-{current_date}.csv"
    csv_path = utils.get_output_filepath(csv_filename)

    # Write data to CSV
    df.to_csv(csv_path, index=False)

    utils.log_success(f"AWS S3 data successfully exported to: {csv_path}")
    return str(csv_path)

def main():
    """
    Main function to execute the script
    """
    # Print script title and get account information
    utils.setup_logging("s3-export")
    account_id, account_name = utils.print_script_banner("AWS S3 BUCKET INVENTORY EXPORT")

    # Check if required dependencies are installed
    if not utils.ensure_dependencies('pandas', 'openpyxl'):
        return

    # Create argument parser
    parser = argparse.ArgumentParser(description='Export AWS S3 bucket information')
    parser.add_argument('--format', choices=['xlsx', 'csv'], default='xlsx',
                        help='Output format (xlsx or csv)')
    parser.add_argument('--skip-size', action='store_true',
                        help='Skip retrieving bucket sizes (faster)')
    parser.add_argument('--non-interactive', action='store_true',
                        help='Run in non-interactive mode using environment variables')
    parser.add_argument('--region', type=str, default=None,
                        help='Specific AWS region to scan (default: all AWS regions)')

    # Parse arguments
    args = parser.parse_args()

    # Detect partition and set partition-appropriate region examples
    partition = utils.detect_partition()
    if partition == 'aws-us-gov':
        example_regions = "us-gov-west-1, us-gov-east-1"
    else:
        example_regions = "us-east-1, us-west-1, us-west-2, eu-west-1"

    # Set target_region based on command line argument if provided
    if os.environ.get('STRATUSSCAN_AUTO_RUN') == '1':
        # Orchestrator/CI mode — S3 is global, always scan all regions
        target_region = None
    elif args.region:
        target_region = args.region if args.region.lower() != 'all' else None
    elif args.non_interactive:
        # Use environment variables for configuration in non-interactive mode
        region_input = os.environ.get('AWS_REGION', 'all')
        target_region = None if region_input.lower() == 'all' else region_input
    else:
        # Interactive: single-voice region selection. S3 is global, so this is a
        # single-region FILTER (or all regions) — not the multi-region
        # utils.prompt_region_selection. prompt_menu gives consistent b/x/q nav.
        all_available_regions = utils.get_partition_regions(partition, all_regions=True)
        try:
            region_choice = utils.prompt_menu(
                "REGION SELECTION",
                [
                    "Default Regions   (scan all regions for S3 buckets)",
                    "All Available Regions",
                    "Specific Region   (filter to a single region)",
                ],
            )
        except utils.BackSignal:
            sys.exit(10)
        except (utils.ExitToMainSignal, utils.QuitSignal):
            sys.exit(11)

        if region_choice in (1, 2):
            # S3 is global — both Default and All scan every region.
            target_region = None
            utils.log_info(f"Scanning all {len(all_available_regions)} AWS regions for S3 buckets")
        else:
            try:
                region_idx = utils.prompt_menu("AVAILABLE AWS REGIONS", all_available_regions)
            except utils.BackSignal:
                sys.exit(10)
            except (utils.ExitToMainSignal, utils.QuitSignal):
                sys.exit(11)
            target_region = all_available_regions[region_idx - 1]
            utils.log_info(f"Scanning region: {target_region}")

    # Validate region if a specific one was provided
    if target_region and not is_valid_aws_region(target_region):
        utils.log_warning(f"'{target_region}' is not a valid AWS region.")
        utils.log_info(f"Valid AWS regions include: {example_regions}")
        utils.log_info("Checking all AWS regions instead.")
        target_region = None

    utils.log_info("Checking for S3 Storage Lens availability in AWS...")
    use_storage_lens = check_storage_lens_availability()

    utils.log_info("Collecting S3 bucket information" +
          (f" for AWS region: {target_region}" if target_region else " across all AWS regions") +
          "...")
    utils.log_info("This may take some time depending on the number of buckets...")

    # Get information about S3 buckets in AWS
    buckets_info, failed_buckets = get_s3_buckets_info(
        use_storage_lens=use_storage_lens, target_region=target_region
    )

    # Check if we found any buckets. A genuinely empty account (no failures,
    # no buckets) still gets the original no-file warning. A failed collection
    # is handled below even if buckets_info is empty or partial — partial data
    # is exported first, then the failure is surfaced loudly.
    if not buckets_info and not failed_buckets:
        utils.log_warning("No S3 buckets found in AWS regions or unable to retrieve bucket information.")
        return

    if buckets_info:
        utils.log_success(f"Found {len(buckets_info)} S3 buckets" +
              (f" in AWS region {target_region}." if target_region else " across all AWS regions."))

        # Export the data to the selected format
        if args.format == 'xlsx':
            output_file = export_to_excel(buckets_info, account_name, target_region)
        else:
            output_file = export_to_csv(buckets_info, account_name, target_region)

        if output_file:
            utils.log_info("Export contains data from AWS region(s)")
            utils.log_info(f"Total S3 buckets exported: {len(buckets_info)}")
            print("\nScript execution completed successfully.")
        else:
            utils.log_error("Failed to export data. Please check the logs.")

    # If ANY bucket failed to process, make it loud: write a marker and exit
    # non-zero, even if some data was exported. A partial export that looks
    # complete is exactly the failure mode this guards against.
    if failed_buckets:
        utils.report_collection_failures(account_name, "s3-buckets", failed_buckets)
        print(
            "\nERROR: S3 export completed with failures — data is incomplete. "
            "See the *-s3-buckets-FAILED-*.txt marker in the output directory."
        )
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(1)
    except Exception as e:
        utils.log_error("An unexpected error occurred", e)
        sys.exit(1)
