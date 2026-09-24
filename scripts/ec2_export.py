#!/usr/bin/env python3

"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS EC2 Instance Data Export Script
Date: NOV-15-2025

Description:
This script queries AWS EC2 instances across available regions and exports detailed instance
information to an Excel spreadsheet. The output filename includes the AWS account name based
on the account ID mapping in the configuration.

Features:
- Supports all standard AWS regions
- Comprehensive instance data export
- Cost calculation integration
- Flexible region filtering
- Enhanced error handling and logging
- Phase 4B: Concurrent region scanning (4x-10x performance improvement)
"""

import datetime
import json
import re
import sys
import time
from pathlib import Path

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
args = utils.parse_script_args("Export EC2 instances to Excel")

def get_os_info_from_ssm(instance_id, region):
    """
    Retrieve detailed operating system information using SSM with timeout safeguards
    Returns the full OS version string or 'Unknown OS' if unavailable

    Note: SSM agent must be installed and running on the instance
    """
    try:
        ssm = utils.get_boto3_client('ssm', region_name=region)

        # Check if the instance is managed by SSM
        response = ssm.describe_instance_information(
            Filters=[{'Key': 'InstanceIds', 'Values': [instance_id]}]
        )

        if not response['InstanceInformationList']:
            return "Unknown OS"

        # Get platform information from SSM
        instance_info = response['InstanceInformationList'][0]
        platform_type = instance_info.get('PlatformType', '')
        platform_name = instance_info.get('PlatformName', '')
        platform_version = instance_info.get('PlatformVersion', '')

        # If we have detailed platform info from SSM directly, use it
        if platform_name and platform_version:
            return f"{platform_name} {platform_version}"
        elif platform_name:
            return platform_name

        # If it's Windows, return basic Windows info
        if platform_type == 'Windows':
            return "Windows (Use AMI Name for details)"

        # For Linux, return basic Linux info
        return "Linux (Use AMI Name for details)"

    except Exception as e:
        utils.log_warning(f"SSM error for instance {instance_id} in region {region}: {e}")
        return "Unknown OS"

def get_instance_stop_date(instance, state):
    """
    Extract the stop date from an instance dict using the StateTransitionReason field.

    StateTransitionReason is present in the describe_instances paginator response so
    no additional API call is needed — the caller passes the instance dict directly.

    Returns the stop date string or 'N/A' for non-stopped instances.
    """
    if state != 'stopped':
        return 'N/A'

    state_reason = instance.get('StateTransitionReason', '')

    # User initiated shutdowns: "User initiated (YYYY-MM-DD HH:MM:SS GMT)"
    if 'User initiated' in state_reason and '(' in state_reason and ')' in state_reason:
        date_start = state_reason.find('(') + 1
        date_end = state_reason.find(')')
        if date_start > 0 and date_end > date_start:
            return state_reason[date_start:date_end]
    elif state_reason:
        return f"System initiated ({state_reason})"

    return 'Stopped (Date Unknown)'

def get_os_info_from_ami(ec2_client, image_id, platform_details, platform):
    """
    Get detailed OS information from AMI metadata without making blocking calls

    Args:
        ec2_client: The boto3 EC2 client
        image_id (str): The AMI ID
        platform_details (str): The platform details from instance metadata
        platform (str): The platform from instance metadata

    Returns:
        str: Detailed operating system information where available
    """
    try:
        # Try to get AMI information (non-blocking)
        response = ec2_client.describe_images(ImageIds=[image_id])

        if response['Images']:
            image = response['Images'][0]

            # Check for Windows AMIs
            if platform == 'windows':
                description = image.get('Description', '')
                if description:
                    # Windows AMIs often contain the version in the description
                    return description
                name = image.get('Name', '')
                if name and 'Windows' in name:
                    return name
                return platform_details

            # For Linux AMIs
            name = image.get('Name', '')
            description = image.get('Description', '')

            # Amazon Linux AMIs
            if 'amzn' in name.lower() or 'amazon linux' in name.lower():
                return name if name else "Amazon Linux"

            # RHEL, Ubuntu, SUSE, etc.
            if any(distro in name.lower() or distro in description.lower() for distro in
                   ['rhel', 'red hat', 'ubuntu', 'debian', 'suse', 'centos']):
                return name if name else description

            # If we have a name but don't recognize the distribution
            if name:
                return name

            # Fall back to description
            if description:
                return description

        # If AMI lookup fails, use platform details
        return platform_details

    except Exception as e:
        utils.log_warning(f"Could not get detailed OS info for AMI {image_id}: {e}")
        return platform_details

def format_tags(tags):
    """
    Format EC2 instance tags in the format "Key1:Value1, Key2:Value2, etc..."

    Args:
        tags (list): List of tag dictionaries with Key and Value

    Returns:
        str: Formatted tags string or 'N/A' if no tags
    """
    if not tags:
        return 'N/A'

    formatted_tags = []
    for tag in tags:
        if 'Key' in tag and 'Value' in tag:
            formatted_tags.append(f"{tag['Key']}:{tag['Value']}")

    if formatted_tags:
        return ', '.join(formatted_tags)
    else:
        return 'N/A'

def load_pricing_data(region='us-east-1'):
    """
    Load EC2 pricing rates and their provenance.

    Rates come from AWS's published Price List Bulk feed via
    ``utils.get_pricing_data`` -- fetched live when the feed is reachable,
    replayed from a version-keyed cache when it is fresh, and read from the
    bundled reference/ snapshot otherwise. One code path, so the fallback
    cannot drift from the live data (Issue #297).

    The pricing block is selected by partition, never computed from the other:
    GovCloud rates are pulled from the GovCloud feed.

    Args:
        region (str): AWS region being scanned, used to detect partition and
                      select the appropriate pricing block (us-east-1 for
                      commercial, us-gov-west-1 for GovCloud).

    Returns:
        tuple: ``(pricing_data, provenance)`` where pricing_data is
        ``{instance_type: {'linux': float|None, 'windows': float|None,
        'memory_gib': float|None}}``.
    """
    pricing_data = {}
    records, provenance = utils.get_pricing_data('AmazonEC2')

    if not records:
        utils.log_warning(
            "No EC2 pricing records available "
            f"({provenance.get('fallback_reason', 'unknown reason')}) - "
            "cost columns will read N/A"
        )
        return pricing_data, provenance

    try:
        partition = utils.detect_partition(region)
        pricing_region = 'us-gov-west-1' if partition == 'aws-us-gov' else 'us-east-1'

        for instance_type, data in records.items():
            regional = (
                data.get('pricing', {}).get(pricing_region)
                or {}  # no feed row for this partition -> null, never a us-east-1 stand-in
            )
            pricing_data[instance_type] = {
                'linux': regional.get('linux_on_demand_monthly_usd'),
                'windows': regional.get('windows_on_demand_monthly_usd'),
                'memory_gib': data.get('memory_gib'),
            }

        utils.log_info(
            f"Loaded pricing data for {len(pricing_data)} instance types "
            f"({pricing_region} pricing; {utils.pricing_provenance_note(provenance)})"
        )
        return pricing_data, provenance

    except Exception as e:
        utils.log_warning(f"Error loading pricing data: {e}")
        return pricing_data, provenance

def load_storage_pricing_data():
    """
    Load EBS volume pricing data from the reference JSON file.

    Returns:
        dict: Dictionary mapping volume types to cost per GB/month
    """
    storage_pricing = {}
    try:
        script_dir = Path(__file__).parent.absolute()
        pricing_file = script_dir.parent / 'reference' / 'ebs-pricing.json'

        if not pricing_file.exists():
            utils.log_warning(f"Storage pricing file not found at {pricing_file}")
            return storage_pricing

        with open(pricing_file, encoding='utf-8') as fh:
            data = json.load(fh)
        storage_pricing = {k: float(v) for k, v in data.get('rates', {}).items()}
        utils.log_info(f"Loaded storage pricing data for {len(storage_pricing)} volume types")
        return storage_pricing

    except Exception as e:
        utils.log_warning(f"Error loading storage pricing data: {e}")
        return storage_pricing

def parse_price(price_str):
    """
    Parse price string and return float value

    Args:
        price_str (str): Price string like "$157.826" or "unavailable"

    Returns:
        float or None: Parsed price or None if unavailable
    """
    if not price_str or price_str.lower() in ['unavailable', 'n/a', '']:
        return None

    # Remove currency symbols, commas, and spaces
    cleaned = re.sub(r'[$,\s]', '', price_str)

    try:
        return float(cleaned)
    except ValueError:
        return None

def calculate_monthly_cost(instance_type, platform, state, pricing_data):
    """
    Calculate monthly cost for an EC2 instance

    Args:
        instance_type (str): EC2 instance type (e.g., 't3.micro')
        platform (str): Platform type ('windows' or Linux/UNIX)
        state (str): Instance state ('running', 'stopped', etc.)
        pricing_data (dict): Pricing data dictionary

    Returns:
        float or str: Numeric monthly cost value or 'N/A'
    """
    # Only calculate cost for running instances
    if state != 'running':
        return 0.00

    if instance_type not in pricing_data:
        return 'N/A'

    # Determine platform for pricing lookup
    is_windows = platform and platform.lower() == 'windows'
    price_key = 'windows' if is_windows else 'linux'

    price = pricing_data[instance_type].get(price_key)

    if price is None:
        return 'N/A'

    return price

def get_attached_volumes(ec2_client, instance, volumes_map=None):
    """
    Get all attached volumes for an instance, excluding the root volume.

    Args:
        ec2_client: The boto3 EC2 client
        instance: The instance object
        volumes_map: Optional pre-fetched dict of {volume_id: volume_dict} to avoid N+1 API calls

    Returns:
        tuple: (list of volume IDs, dict of volume info with size and type)
    """
    attached_volumes = []
    volume_info = {}
    root_device_name = instance.get('RootDeviceName')

    try:
        for device in instance.get('BlockDeviceMappings', []):
            device_name = device.get('DeviceName')
            volume_id = device.get('Ebs', {}).get('VolumeId')

            # Skip root device
            if device_name == root_device_name:
                continue

            if volume_id:
                attached_volumes.append(volume_id)

                # Use pre-fetched cache if available, else fall back to API call
                if volumes_map and volume_id in volumes_map:
                    vol = volumes_map[volume_id]
                    volume_info[volume_id] = {
                        'size': vol.get('Size', 0),
                        'type': vol.get('VolumeType', 'standard')
                    }
                else:
                    try:
                        volumes = ec2_client.describe_volumes(VolumeIds=[volume_id])
                        if volumes and 'Volumes' in volumes and len(volumes['Volumes']) > 0:
                            vol = volumes['Volumes'][0]
                            volume_info[volume_id] = {
                                'size': vol.get('Size', 0),
                                'type': vol.get('VolumeType', 'standard')
                            }
                    except Exception as e:
                        utils.log_warning(f"Error getting volume info for {volume_id}: {e}")
                        volume_info[volume_id] = {'size': 0, 'type': 'standard'}

    except Exception as e:
        utils.log_warning(f"Error getting attached volumes: {e}")

    return attached_volumes, volume_info

def calculate_storage_cost(root_size, root_type, attached_volume_info, storage_pricing):
    """
    Calculate total monthly storage cost for all volumes

    Args:
        root_size (int): Size of root volume in GiB
        root_type (str): Type of root volume (gp3, gp2, etc.)
        attached_volume_info (dict): Dictionary of volume IDs to their info (size, type)
        storage_pricing (dict): Storage pricing data

    Returns:
        float or str: Total monthly storage cost or 'N/A'
    """
    try:
        total_cost = 0.0

        # Calculate root volume cost
        if root_size != 'N/A' and root_type != 'N/A':
            root_price = storage_pricing.get(root_type)
            if root_price is None:
                return 'N/A'
            total_cost += float(root_size) * root_price

        # Calculate attached volumes cost
        for _vol_id, vol_info in attached_volume_info.items():
            vol_size = vol_info.get('size', 0)
            vol_type = vol_info.get('type', 'gp3')
            vol_price = storage_pricing.get(vol_type)
            if vol_price is None:
                return 'N/A'
            total_cost += float(vol_size) * vol_price

        return round(total_cost, 2)

    except Exception as e:
        utils.log_warning(f"Error calculating storage cost: {e}")
        return 'N/A'

# Dependency checking handled by utils.ensure_dependencies()

# Account info retrieval handled by utils.get_account_info()
def is_valid_aws_region(region_name):
    """Check if a region name is a valid AWS region"""
    return utils.is_aws_region(region_name)

def _build_instance_row(instance, region, ec2_client, pricing_data, storage_pricing, cost_note,
                         instance_types_map, volumes_map):
    """
    Build the export row for a single EC2 instance.

    Extracted so the per-instance processing can be wrapped in try/except by the
    caller: a malformed instance (e.g. one missing an expected field) is logged
    and skipped rather than discarding the whole region's results. Every
    required field is read with ``.get()`` and a safe default for the same
    reason.

    Args:
        instance (dict): A single Instances entry from describe_instances.
        region (str): AWS region name.
        ec2_client: Boto3 EC2 client (for AMI/volume lookups).
        pricing_data (dict): EC2 instance pricing data.
        storage_pricing (dict): EBS/storage pricing data.
        cost_note (str): Partition-aware cost estimate note.
        instance_types_map (dict): Prefetched
            {instance_type: {'memory_mib': int, 'vcpu': int}}.
        volumes_map (dict): Prefetched {volume_id: volume_dict}.

    Returns:
        dict: The assembled instance row.
    """
    # Get the root volume information
    root_device = next((device for device in instance.get('BlockDeviceMappings', [])
                      if device.get('DeviceName') == instance.get('RootDeviceName')), None)

    # Get detailed OS information
    os_info = get_os_info_from_ssm(instance.get('InstanceId', ''), region)

    # Get AMI name information
    ami_name = get_os_info_from_ami(
        ec2_client,
        instance.get('ImageId', ''),
        instance.get('PlatformDetails', 'N/A'),
        instance.get('Platform', '')
    )

    # Get vCPU and RAM from the prefetched instance types map
    instance_type = instance.get('InstanceType', 'N/A')
    type_spec = instance_types_map.get(instance_type) or {}
    ram_mib = type_spec.get('memory_mib', 'N/A')
    vcpu = type_spec.get('vcpu', 'N/A')

    # CoreCount and ThreadsPerCore are legitimate data in their own right —
    # they are just not vCPU. vCPU = cores x threads per core.
    cpu_options = instance.get('CpuOptions', {}) or {}

    # For root device size and type, we need to ensure we're fetching it correctly
    root_device_size = 'N/A'
    root_volume_id = 'N/A'
    root_volume_type = 'N/A'

    if root_device:
        root_volume_id = root_device.get('Ebs', {}).get('VolumeId', 'N/A')
        # If we have the volume ID, use prefetched cache or fall back to API
        if root_volume_id != 'N/A':
            if root_volume_id in volumes_map:
                root_device_size = volumes_map[root_volume_id].get('Size', 'N/A')
                root_volume_type = volumes_map[root_volume_id].get('VolumeType', 'N/A')
            else:
                try:
                    volumes = ec2_client.describe_volumes(VolumeIds=[root_volume_id])
                    if volumes and 'Volumes' in volumes and len(volumes['Volumes']) > 0:
                        root_device_size = volumes['Volumes'][0].get('Size', 'N/A')
                        root_volume_type = volumes['Volumes'][0].get('VolumeType', 'N/A')
                except Exception as e:
                    utils.log_warning(f"Error getting volume info for {root_volume_id}: {e}")
        else:
            # Try to get size from the device mapping directly
            root_device_size = root_device.get('Ebs', {}).get('VolumeSize', 'N/A')

    # Get attached volumes (non-root), using prefetched volume cache
    attached_vols, attached_vol_info = get_attached_volumes(ec2_client, instance, volumes_map)
    attached_volumes_str = ', '.join(attached_vols) if attached_vols else 'N/A'

    # Get instance state and stopped date if applicable
    instance_state = instance.get('State', {}).get('Name', 'N/A')
    stop_date = get_instance_stop_date(instance, instance_state)

    # Format tags
    instance_tags = format_tags(instance.get('Tags', []))

    # Calculate monthly cost
    monthly_cost = calculate_monthly_cost(
        instance.get('InstanceType', 'N/A'),
        instance.get('Platform', 'Linux/UNIX'),
        instance_state,
        pricing_data
    )

    # Calculate storage cost
    storage_cost = calculate_storage_cost(
        root_device_size,
        root_volume_type,
        attached_vol_info,
        storage_pricing
    )

    # Calculate total monthly cost
    total_monthly_cost = 'N/A'
    if monthly_cost != 'N/A' and storage_cost != 'N/A':
        total_monthly_cost = round(float(monthly_cost) + float(storage_cost), 2)
    elif monthly_cost != 'N/A':
        total_monthly_cost = float(monthly_cost)
    elif storage_cost != 'N/A':
        total_monthly_cost = float(storage_cost)

    # Extract instance information
    return {
        'Computer Name': next((tag.get('Value', 'N/A') for tag in instance.get('Tags', [])
                            if tag.get('Key') == 'Name'), 'N/A'),
        'Instance ID': instance.get('InstanceId', 'N/A'),
        'State': instance_state,
        'Stopped Date': stop_date,
        'Instance Type': instance.get('InstanceType', 'N/A'),
        'Platform': instance.get('Platform', 'Linux/UNIX'),
        'Monthly Cost (On-Demand)': monthly_cost,
        'Monthly Storage Cost': storage_cost,
        'Total Monthly Cost': total_monthly_cost,
        'Cost Note': cost_note,
        'Operating System': os_info,
        'AMI Name': ami_name,
        'Private IPv4': instance.get('PrivateIpAddress', 'N/A'),
        'Public IPv4': instance.get('PublicIpAddress', 'N/A'),
        'IPv6': next((
            next((addr.get('Ipv6Address', 'N/A')
                 for addr in interface.get('Ipv6Addresses', [])), 'N/A')
            for interface in instance.get('NetworkInterfaces', [])
        ), 'N/A'),
        'VPC ID': instance.get('VpcId', 'N/A'),
        'Subnet ID': instance.get('SubnetId', 'N/A'),
        'Availability Zone': instance.get('Placement', {}).get('AvailabilityZone', 'N/A'),
        'AMI ID': instance.get('ImageId', 'N/A'),
        'Launch Time': instance.get('LaunchTime', 'N/A'),
        'Key Pair': instance.get('KeyName', 'N/A'),
        'Region': region,
        'Owner ID': utils.get_account_name_formatted(instance.get('OwnerId', 'N/A')),
        'vCPU': vcpu,
        'CPU Cores': cpu_options.get('CoreCount', 'N/A'),
        'Threads per Core': cpu_options.get('ThreadsPerCore', 'N/A'),
        'RAM (MiB)': ram_mib,
        'Root Device Volume ID': root_volume_id,
        'Root Device Size (GiB)': root_device_size,
        'Attached Volumes': attached_volumes_str,
        'Tags': instance_tags
    }


def get_instance_data(region, instance_filter=None):
    """
    Retrieve EC2 instance data for a specific AWS region.

    Not wrapped in ``aws_error_handler``: a swallowed error here would return
    an empty list that ``_run_export`` cannot distinguish from a genuinely
    empty region, producing silent data loss (see the 07.15.2026 audit).
    Region-level failures (client creation, pagination, throttling) are
    allowed to raise so the caller can record the region as FAILED rather
    than EMPTY. Per-instance errors are contained internally (logged and
    skipped).

    Args:
        region (str): AWS region name
        instance_filter (str, optional): Filter by instance state ('running', 'stopped', or None for all)

    Returns:
        list: List of dictionaries containing EC2 instance information

    Raises:
        Exception: Any AWS/pagination error for the region (caller records it
            as a failed region and surfaces it; it is never masked as empty).
    """
    # Validate region is AWS
    if not utils.is_aws_region(region):
        utils.log_error(f"Invalid AWS region: {region}")
        return []

    ec2 = utils.get_boto3_client('ec2', region_name=region)
    instances = []

    # Load pricing data
    pricing_data, pricing_provenance = load_pricing_data(region)
    storage_pricing = load_storage_pricing_data()

    # Prepare filters if needed
    filters = []
    if instance_filter:
        filters.append({
            'Name': 'instance-state-name',
            'Values': [instance_filter]
        })

    # Get instances in the region with optional filter using paginator
    paginator = ec2.get_paginator('describe_instances')
    pages = paginator.paginate(Filters=filters) if filters else paginator.paginate()

    all_reservations = []
    for page in pages:
        all_reservations.extend(page.get('Reservations', []))
        time.sleep(0.1)

    # Count total instances first for progress tracking
    total_instances = 0
    for reservation in all_reservations:
        total_instances += len(reservation.get('Instances', []))

    if total_instances > 0:
        utils.log_info(f"Found {total_instances} instances in {region} to process")

    # Prefetch all volume details in bulk to avoid N+1 API calls
    volumes_map = {}
    if total_instances > 0:
        all_volume_ids = [
            device.get('Ebs', {}).get('VolumeId')
            for reservation in all_reservations
            for inst in reservation.get('Instances', [])
            for device in inst.get('BlockDeviceMappings', [])
            if device.get('Ebs', {}).get('VolumeId')
        ]
        for i in range(0, len(all_volume_ids), 500):
            chunk = all_volume_ids[i:i + 500]
            try:
                resp = ec2.describe_volumes(VolumeIds=chunk)
                for v in resp.get('Volumes', []):
                    volumes_map[v['VolumeId']] = v
            except Exception as e:
                utils.log_warning(f"Error prefetching volumes in {region}: {e}")

    # Build the per-type spec map (vCPU + RAM) from the static reference data,
    # falling back to describe_instance_types for any type absent from it.
    #
    # vCPU must come from here, not from the instance's CpuOptions.CoreCount:
    # CoreCount is *physical cores* and under-reports by the threads-per-core
    # factor — 2x on most families (Issue #259).
    instance_types_map = {}
    if total_instances > 0:
        unique_types = list({
            inst.get('InstanceType')
            for reservation in all_reservations
            for inst in reservation.get('Instances', [])
            if inst.get('InstanceType')
        })
        reference_specs = utils.load_instance_type_specs()
        unknown_types = []
        for it in unique_types:
            record = reference_specs.get(it) or {}
            memory_gib = record.get('memory_gib')
            vcpu = record.get('vcpu')
            if memory_gib is not None and vcpu is not None:
                instance_types_map[it] = {
                    'memory_mib': int(memory_gib * 1024),
                    'vcpu': vcpu,
                }
            else:
                unknown_types.append(it)
        for i in range(0, len(unknown_types), 100):
            chunk = unknown_types[i:i + 100]
            try:
                resp = ec2.describe_instance_types(InstanceTypes=chunk)
                for it in resp.get('InstanceTypes', []):
                    instance_types_map[it['InstanceType']] = {
                        'memory_mib': it.get('MemoryInfo', {}).get('SizeInMiB', 'N/A'),
                        'vcpu': it.get('VCpuInfo', {}).get('DefaultVCpus', 'N/A'),
                    }
            except Exception as e:
                utils.log_warning(f"Error fetching instance types in {region}: {e}")

    # The pricing source belongs in the workbook, not only in a docstring.
    # Issue #296 sat undetected for seven months because a Cost Note said
    # "Estimate (us-east-1 pricing)" whether the rate was retrieved or invented.
    _partition = utils.detect_partition(region)
    _pricing_region = 'us-gov-west-1' if _partition == 'aws-us-gov' else 'us-east-1'
    cost_note = (
        f"Estimate ({_pricing_region} pricing; "
        f"{utils.pricing_provenance_note(pricing_provenance)})"
    )

    # Process each instance. One malformed instance must not sink the region,
    # so each is built inside try/except; failures are logged and skipped.
    processed = 0
    skipped = 0
    for reservation in all_reservations:
        for instance in reservation.get('Instances', []):
            processed += 1
            instance_id = instance.get('InstanceId', 'Unknown')
            progress = (processed / total_instances) * 100 if total_instances > 0 else 0

            utils.log_info(f"[{progress:.1f}%] Processing instance {processed}/{total_instances}: {instance_id}")
            if processed % 25 == 0:
                print(f"  [{region}] {processed}/{total_instances} EC2 instances processed...", flush=True)

            try:
                instance_data = _build_instance_row(
                    instance, region, ec2, pricing_data, storage_pricing, cost_note,
                    instance_types_map, volumes_map
                )
            except Exception as e:
                skipped += 1
                utils.log_error(
                    f"Skipping EC2 instance '{instance_id}' in {region} due to a processing error", e
                )
                continue

            instances.append(instance_data)

    if skipped:
        utils.log_warning(
            f"{skipped} of {total_instances} EC2 instance(s) in {region} were skipped due to "
            "processing errors (see log above); the remaining instances were still collected."
        )

    return instances

def _prompt_instance_filter():
    """Prompt user to choose an instance state filter.

    Returns:
        tuple (instance_filter, filter_desc) on valid choice.

    Raises:
        utils.BackSignal: if the user enters 'b'.
        utils.QuitSignal: if the user enters 'x' or presses Ctrl-C.
    """
    choice = utils.prompt_menu(
        "INSTANCE FILTER",
        [
            "All instances",
            "Running instances only",
            "Stopped instances only",
        ],
    )
    filters = {
        1: (None, "all"),
        2: ("running", "running"),
        3: ("stopped", "stopped"),
    }
    return filters[choice]


def _run_export(account_id, account_name, regions, instance_filter, filter_desc):
    """Collect EC2 data and write the Excel export."""
    import pandas as pd

    utils.log_info(f"Scanning {len(regions)} region(s) for EC2 instances...")

    def scan_region(region):
        utils.log_info(f"Collecting data from AWS region: {region}")
        instances = get_instance_data(region, instance_filter)
        utils.log_info(f"Found {len(instances)} instances in {region}")
        return instances

    # collect_failures=True lets get_instance_data raise on a region-level
    # failure so the scanner records that region as FAILED — a failed region
    # must never be collapsed into "empty" (silent data loss, 07.15.2026 audit).
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=scan_region,
        show_progress=True,
        collect_failures=True,
    )

    all_instances = []
    for instances in region_results:
        all_instances.extend(instances)

    total_instances = len(all_instances)

    if not all_instances and not failed_regions:
        # Genuinely empty account: every region succeeded and returned nothing.
        utils.log_warning("No instances found in any AWS region. Exiting...")
        sys.exit(0)

    if all_instances:
        utils.log_success(f"Total EC2 Instances found across all AWS regions: {total_instances}")

        utils.log_info("Preparing data for export to Excel format...")
        df = pd.DataFrame(all_instances)
        df = utils.sanitize_for_export(utils.prepare_dataframe_for_export(df))

        region_desc = regions[0] if len(regions) == 1 else 'all'
        current_date = datetime.datetime.now().strftime("%m.%d.%Y")
        filename = utils.create_export_filename(
            account_name,
            "ec2",
            f"{filter_desc}-{region_desc}",
            current_date,
        )

        output_path = utils.save_dataframe_to_excel(df, filename)

        if output_path:
            utils.log_success("AWS EC2 data exported successfully!")
            utils.log_success(f"File location: {output_path}")
            utils.log_info(f"Export contains data from {len(regions)} AWS region(s)")
            utils.log_info(f"Total instances exported: {total_instances}")
            print("\nScript execution completed.")
        else:
            utils.log_error("Error exporting data. Please check the logs.")
            sys.exit(1)

    # If ANY region failed, make it loud: write a marker and exit non-zero,
    # even if some data was exported. A partial export that looks complete is
    # exactly the failure mode this guards against.
    if failed_regions:
        utils.report_collection_failures(account_name, "ec2", failed_regions)
        print(
            "\nERROR: EC2 export completed with failures — data is incomplete. "
            "See the *-ec2-FAILED-*.txt marker in the output directory."
        )
        sys.exit(1)


def main():
    """Main function — 4-step state machine with b/x navigation."""
    try:
        if not utils.ensure_dependencies('pandas', 'openpyxl', 'boto3'):
            return

        utils.setup_logging("ec2-export")
        account_id, account_name = utils.print_script_banner("AWS EC2 INSTANCES DATA EXPORT")

        if account_name == "UNKNOWN-ACCOUNT" and not utils.prompt_for_confirmation(
            "Unable to determine account name. Proceed anyway?", default=False
        ):
            print("Exiting script...")
            sys.exit(0)

        step = 1
        regions = None
        instance_filter = None
        filter_desc = "all"

        while True:
            if step == 1:
                result = utils.prompt_region_selection(service_name="EC2")
                if result == 'back':
                    sys.exit(10)
                if result == 'exit':
                    sys.exit(11)
                regions = result
                step = 2

            elif step == 2:
                try:
                    result = _prompt_instance_filter()
                except utils.BackSignal:
                    step = 1
                    continue
                except (utils.ExitToMainSignal, utils.QuitSignal):
                    sys.exit(11)
                instance_filter, filter_desc = result
                step = 3

            elif step == 3:
                region_str = ', '.join(regions) if len(regions) <= 3 else f"{len(regions)} regions"
                msg = f"Ready to export EC2 data ({filter_desc}, {region_str})."
                result = utils.prompt_confirmation(msg)
                if result == 'back':
                    step = 2
                    continue
                if result == 'exit':
                    sys.exit(11)
                step = 4

            elif step == 4:
                _run_export(account_id, account_name, regions, instance_filter, filter_desc)
                break

    except KeyboardInterrupt:
        print("\n\nScript interrupted by user. Exiting...")
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        utils.log_error("Unexpected error occurred", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
