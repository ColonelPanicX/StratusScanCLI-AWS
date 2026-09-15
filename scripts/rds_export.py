#!/usr/bin/env python3

"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS RDS Instance Export Script
Date: NOV-15-2025

Description:
This script exports a list of all RDS instances across available AWS
regions into a spreadsheet. The export includes DB Identifier, DB Cluster
Identifier, Role, Engine, Engine Version, RDS Extended Support, Region, Status,
Multi-AZ, Availability Zone(s), Size,
Storage Type, Storage, Provisioned IOPS, Port, Endpoint, Master Username, VPC
(Name and ID), Subnet IDs, Security Groups (Name and ID), DB Subnet Group Name,
DB Certificate Expiry, Created Time, and Encryption information.

Phase 4B Update:
- Concurrent region scanning (4x-10x performance improvement)
- Automatic fallback to sequential on errors
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
args = utils.parse_script_args("Export RDS instances and clusters to Excel")

# Dependency checking handled by utils.ensure_dependencies()
def is_valid_aws_region(region_name):
    """
    Check if a region name is a valid AWS region.

    Args:
        region_name (str): AWS region name to validate

    Returns:
        bool: True if valid, False otherwise
    """
    return utils.is_aws_region(region_name)

def get_security_group_info(rds_client, sg_ids):
    """
    Get security group names and IDs from a list of security group IDs.

    Args:
        rds_client: The boto3 RDS client (used to determine region)
        sg_ids (list): A list of security group IDs

    Returns:
        str: Formatted list of security group names and IDs in format "name (id), name (id), ..."
    """
    if not sg_ids:
        return ""

    try:
        # Create EC2 client in the same region as the RDS client
        region = rds_client.meta.region_name
        ec2_client = utils.get_boto3_client('ec2', region_name=region)

        # Get security group information using EC2 describe_security_groups API
        response = ec2_client.describe_security_groups(GroupIds=sg_ids)
        # Format as "name (id), name (id), ..."
        sg_info = [f"{sg['GroupName']} ({sg['GroupId']})" for sg in response['SecurityGroups']]
        return ", ".join(sg_info)
    except Exception as e:
        # Return just the IDs if we can't get the names
        utils.log_warning(f"Could not get security group names for {sg_ids}: {e}")
        return ", ".join(sg_ids)

def get_vpc_info(rds_client, vpc_id):
    """
    Get VPC name and ID information from a VPC ID.

    Args:
        rds_client: The boto3 RDS client (used to determine region)
        vpc_id (str): The VPC ID

    Returns:
        str: Formatted VPC name and ID in format "name (id)" or just ID if name not found
    """
    if not vpc_id:
        return "N/A"

    try:
        # Create EC2 client in the same region as the RDS client
        region = rds_client.meta.region_name
        ec2_client = utils.get_boto3_client('ec2', region_name=region)

        # Get VPC information using EC2 describe_vpcs API
        response = ec2_client.describe_vpcs(VpcIds=[vpc_id])
        if response['Vpcs']:
            vpc = response['Vpcs'][0]
            vpc_name = "Unnamed"
            # Check for Name tag in VPC tags
            for tag in vpc.get('Tags', []):
                if tag['Key'] == 'Name':
                    vpc_name = tag['Value']
                    break
            return f"{vpc_name} ({vpc_id})"
        return vpc_id
    except Exception as e:
        # Return just the ID if we can't get the VPC details
        utils.log_warning(f"Could not get VPC info for {vpc_id}: {e}")
        return vpc_id

def get_subnet_ids(subnet_group):
    """
    Extract subnet IDs from a DB subnet group.

    Args:
        subnet_group (dict): The DB subnet group information from RDS API

    Returns:
        str: Comma-separated list of subnet IDs
    """
    if not subnet_group or 'Subnets' not in subnet_group:
        return "N/A"

    try:
        # Extract subnet identifiers from the subnet group
        subnet_ids = [subnet['SubnetIdentifier'] for subnet in subnet_group['Subnets']]
        return ", ".join(subnet_ids)
    except Exception:
        return "N/A"

def load_rds_pricing_data(region='us-east-1'):
    """
    Load RDS pricing data from the reference JSON file.
    Selects the correct pricing block based on the region's partition.

    Args:
        region (str): AWS region being scanned, used to detect partition and
                      select the appropriate pricing block (us-east-1 for
                      commercial, us-gov-west-1 for GovCloud).

    Returns:
        dict: {instance_type: {engine_key: float|None, ...}}
    """
    pricing_data = {}
    try:
        script_dir = Path(__file__).parent.absolute()
        pricing_file = script_dir.parent / 'reference' / 'rds-pricing.json'

        if not pricing_file.exists():
            utils.log_warning(f"RDS pricing file not found at {pricing_file}")
            return pricing_data

        with open(pricing_file, encoding='utf-8') as f:
            json_data = json.load(f)

        partition = utils.detect_partition(region)
        pricing_region = 'us-gov-west-1' if partition == 'aws-us-gov' else 'us-east-1'

        for instance_type, data in json_data.get('records', {}).items():
            regional = (
                data.get('pricing', {}).get(pricing_region)
                or data.get('pricing', {}).get('us-east-1', {})
            )
            pricing_data[instance_type] = regional

        utils.log_info(
            f"Loaded RDS pricing data for {len(pricing_data)} instance types "
            f"({pricing_region} pricing)"
        )
        return pricing_data

    except Exception as e:
        utils.log_warning(f"Error loading RDS pricing data: {e}")
        return pricing_data

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

def calculate_rds_monthly_cost(instance_type, engine, pricing_data):
    """
    Calculate monthly cost for an RDS instance based on instance type and engine.

    Args:
        instance_type (str): RDS instance type (e.g., 'db.m5.large')
        engine (str): Database engine (e.g., 'postgres', 'mysql', 'sqlserver-se')
        pricing_data (dict): RDS pricing data dictionary

    Returns:
        float or str: Monthly cost or 'N/A'
    """
    if instance_type not in pricing_data:
        return 'N/A'

    instance_pricing = pricing_data[instance_type]
    engine_lower = engine.lower()
    price_key = None

    if 'aurora-postgresql' in engine_lower or 'aurora-mysql' in engine_lower:
        price_key = 'aurora_on_demand_monthly_usd'
    elif 'postgres' in engine_lower:
        price_key = 'postgresql_on_demand_monthly_usd'
    elif 'mariadb' in engine_lower:
        price_key = 'mariadb_on_demand_monthly_usd'
    elif 'mysql' in engine_lower:
        price_key = 'mysql_on_demand_monthly_usd'
    elif 'sqlserver-ex' in engine_lower:
        price_key = 'sqlserver_ex_on_demand_monthly_usd'
    elif 'sqlserver' in engine_lower:
        # Web, Standard, and Enterprise all map to sqlserver_std — best available
        # approximation from MCP data; SQL Server EE pricing is not separately available
        price_key = 'sqlserver_std_on_demand_monthly_usd'
    elif 'oracle' in engine_lower:
        price_key = 'oracle_on_demand_monthly_usd'
    else:
        return 'N/A'

    price = instance_pricing.get(price_key)
    return price if price is not None else 'N/A'

def calculate_rds_storage_cost(storage_size, storage_type, storage_pricing):
    """
    Calculate monthly storage cost for an RDS instance

    Args:
        storage_size (int): Storage size in GB
        storage_type (str): Storage type (gp2, gp3, io1, etc.)
        storage_pricing (dict): Storage pricing data

    Returns:
        float or str: Monthly storage cost or 'N/A'
    """
    try:
        if storage_size == 'N/A' or storage_type == 'N/A':
            return 'N/A'

        # Get price per GB for the storage type
        price_per_gb = storage_pricing.get(storage_type, storage_pricing.get('gp3', 0.08))

        if price_per_gb is None:
            return 'N/A'

        total_cost = float(storage_size) * price_per_gb
        return round(total_cost, 2)

    except Exception as e:
        utils.log_warning(f"Error calculating storage cost: {e}")
        return 'N/A'

def _yes_no_or_na(value):
    """Map an API boolean to Yes/No; anything else (absent, None) is 'N/A', never guessed."""
    if value is True:
        return 'Yes'
    if value is False:
        return 'No'
    return 'N/A'


def get_deployment_fields(instance, db_cluster_id, cluster):
    """
    Derive the 'Multi-AZ' and 'Availability Zone(s)' export values for one instance.

    Every value is read straight from the API response; a field the API did not
    return is reported as 'N/A' rather than inferred from other fields.

    Standalone instances (no DBClusterIdentifier) use the instance-level
    ``MultiAZ``, ``AvailabilityZone`` and ``SecondaryAvailabilityZone`` fields
    (DescribeDBInstances / DBInstance data type). The secondary AZ is the
    Multi-AZ standby.

    Cluster members (Aurora, Neptune, DocumentDB, and non-Aurora Multi-AZ DB
    clusters) do not use instance-level ``MultiAZ`` — AWS documents it as not
    applicable to Aurora — so Multi-AZ comes from the cluster's ``MultiAZ``
    ("has instances in multiple Availability Zones"). The cluster's
    ``AvailabilityZones`` list is where instances *can* be created, not where
    they run, so it is labelled separately from this member's own
    ``AvailabilityZone``. ``cluster`` is the DBCluster dict already fetched for
    the Role column; when that lookup failed it is None and the cluster-derived
    parts are 'N/A'. No extra API call is made here.

    Args:
        instance (dict): A DBInstances entry from describe_db_instances.
        db_cluster_id (str): The instance's DBClusterIdentifier, or 'N/A'.
        cluster (dict | None): The matching DBClusters entry, if fetched.

    Returns:
        tuple[str, str]: (multi_az, availability_zones)
    """
    primary_az = instance.get('AvailabilityZone') or 'N/A'

    if db_cluster_id != 'N/A':
        if cluster is None:
            return 'N/A', f"{primary_az} (this instance); cluster AZs: N/A"
        cluster_azs = cluster.get('AvailabilityZones') or []
        cluster_azs_text = ", ".join(cluster_azs) if cluster_azs else 'N/A'
        return (
            _yes_no_or_na(cluster.get('MultiAZ')),
            f"{primary_az} (this instance); cluster AZs: {cluster_azs_text}",
        )

    multi_az = _yes_no_or_na(instance.get('MultiAZ'))
    secondary_az = instance.get('SecondaryAvailabilityZone')

    if secondary_az:
        return multi_az, f"{primary_az} (primary), {secondary_az} (standby)"
    if multi_az == 'Yes':
        # Multi-AZ but no standby AZ in the response: say so rather than guess one.
        return multi_az, f"{primary_az} (primary), N/A (standby)"
    return multi_az, primary_az


def _build_instance_data(instance, region, rds_client, pricing_data, storage_pricing, cost_note):
    """
    Build the export row for a single RDS instance.

    Extracted so the per-instance processing can be wrapped in try/except by the
    caller: a malformed instance (e.g. an engine variant such as RDS Custom that
    omits an expected field) is logged and skipped rather than discarding the
    whole region's results. Every required field is read with ``.get()`` and a
    safe default for the same reason.

    Args:
        instance (dict): A single DBInstances entry from describe_db_instances.
        region (str): AWS region name.
        rds_client: Boto3 RDS client (for cluster-role and SG/VPC lookups).
        pricing_data (dict): RDS instance pricing data.
        storage_pricing (dict): EBS/storage pricing data.
        cost_note (str): Partition-aware cost estimate note.

    Returns:
        dict: The assembled instance row.
    """
    instance_id = instance.get('DBInstanceIdentifier', 'Unknown')

    # Extract security group IDs from VPC security groups
    sg_ids = [sg['VpcSecurityGroupId'] for sg in instance.get('VpcSecurityGroups', [])]
    sg_info = get_security_group_info(rds_client, sg_ids)

    # Get VPC information from DB subnet group
    vpc_id = instance.get('DBSubnetGroup', {}).get('VpcId', 'N/A')
    vpc_info = get_vpc_info(rds_client, vpc_id) if vpc_id != 'N/A' else 'N/A'

    # Get subnet IDs from DB subnet group
    subnet_ids = get_subnet_ids(instance.get('DBSubnetGroup', {}))

    # Get port information from endpoint
    port = instance.get('Endpoint', {}).get('Port', 'N/A') if 'Endpoint' in instance else 'N/A'

    # Get endpoint address - RDS connection endpoint
    endpoint_address = instance.get('Endpoint', {}).get('Address', 'N/A') if 'Endpoint' in instance else 'N/A'

    # Get master username - the primary database user
    master_username = instance.get('MasterUsername', 'N/A')

    # Determine if instance is part of a cluster and its role
    db_cluster_id = instance.get('DBClusterIdentifier', 'N/A')
    role = 'Standalone'
    # Kept so the deployment columns can reuse the one lookup already made here.
    cluster = None
    if db_cluster_id != 'N/A':
        try:
            # Get cluster info to determine if this instance is primary or replica
            cluster_info = rds_client.describe_db_clusters(
                DBClusterIdentifier=db_cluster_id
            )
            if cluster_info and 'DBClusters' in cluster_info and cluster_info['DBClusters']:
                cluster = cluster_info['DBClusters'][0]
                # Check if this instance is the primary (writer) in the cluster
                if 'DBClusterMembers' in cluster:
                    for member in cluster['DBClusterMembers']:
                        if member.get('DBInstanceIdentifier') == instance_id:
                            role = 'Primary' if member.get('IsClusterWriter', False) else 'Replica'
        except Exception as e:
            # If we can't determine cluster role, leave as default
            utils.log_warning(f"Could not determine cluster role for {instance_id}: {e}")

    multi_az, availability_zones = get_deployment_fields(instance, db_cluster_id, cluster)

    # Check for RDS Extended Support status
    extended_support = 'No'
    try:
        if 'StatusInfos' in instance:
            for status_info in instance['StatusInfos']:
                if status_info.get('Status') == 'extended-support':
                    extended_support = 'Yes'
    except Exception:
        pass

    # Format certificate expiry date
    cert_expiry = 'N/A'
    try:
        if 'CertificateDetails' in instance and 'ValidTill' in instance['CertificateDetails']:
            valid_till = instance['CertificateDetails']['ValidTill']
            if isinstance(valid_till, datetime.datetime):
                cert_expiry = valid_till.replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        pass

    # Format creation time
    created_time = 'N/A'
    try:
        if 'InstanceCreateTime' in instance:
            create_time = instance['InstanceCreateTime']
            if isinstance(create_time, datetime.datetime):
                created_time = create_time.replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        pass

    # Required fields read defensively: engine variants (Aurora, RDS Custom) can
    # omit fields another engine always sets. A missing field yields 'N/A', not a
    # KeyError that would sink the entire region (see the 07.15.2026 audit).
    instance_class = instance.get('DBInstanceClass', 'N/A')
    engine = instance.get('Engine', 'N/A')
    engine_version = instance.get('EngineVersion', 'N/A')
    allocated_storage = instance.get('AllocatedStorage', 'N/A')
    storage_type = instance.get('StorageType', 'N/A')

    # Calculate monthly cost
    monthly_cost = calculate_rds_monthly_cost(instance_class, engine, pricing_data)

    # Calculate storage cost
    storage_cost = calculate_rds_storage_cost(allocated_storage, storage_type, storage_pricing)

    # Calculate total monthly cost
    total_monthly_cost = 'N/A'
    if monthly_cost != 'N/A' and storage_cost != 'N/A':
        total_monthly_cost = round(float(monthly_cost) + float(storage_cost), 2)
    elif monthly_cost != 'N/A':
        total_monthly_cost = float(monthly_cost)
    elif storage_cost != 'N/A':
        total_monthly_cost = float(storage_cost)

    return {
        'DB Identifier': instance_id,
        'DB Cluster Identifier': db_cluster_id,
        'Role': role,
        'Engine': engine,
        'Engine Version': engine_version,
        'RDS Extended Support': extended_support,
        'Region': region,
        'Status': instance.get('DBInstanceStatus', 'N/A'),
        'Multi-AZ': multi_az,
        'Availability Zone(s)': availability_zones,
        'Size': instance_class,
        'Monthly Cost (On-Demand)': monthly_cost,
        'Monthly Storage Cost': storage_cost,
        'Total Monthly Cost': total_monthly_cost,
        'Cost Note': cost_note,
        'Storage Type': storage_type,
        'Storage (GB)': allocated_storage,
        'Provisioned IOPS': instance.get('Iops', 'N/A'),
        'Port': port,
        'Endpoint': endpoint_address,  # RDS connection endpoint
        'Master Username': master_username,  # Primary database user
        'VPC': vpc_info,
        'Subnet IDs': subnet_ids,
        'Security Groups': sg_info,
        'DB Subnet Group Name': instance.get('DBSubnetGroup', {}).get('DBSubnetGroupName', 'N/A'),
        'DB Certificate Expiry': cert_expiry,
        'Created Time': created_time,
        'Encryption': 'Yes' if instance.get('StorageEncrypted', False) else 'No',
        'Owner ID': utils.get_account_name_formatted(instance.get('OwnerId', 'N/A'))
    }


def get_rds_instances(region):
    """
    Get all RDS instances in a specific AWS region with detailed information.

    Not wrapped in ``aws_error_handler``: a swallowed error here would return an
    empty list that ``main()`` cannot distinguish from a genuinely empty region,
    producing silent data loss (no file written). Region-level failures are
    allowed to raise so the caller can record the region as *failed* rather than
    *empty*. Per-instance errors are contained internally (logged and skipped).

    Args:
        region (str): AWS region name

    Returns:
        list: List of dictionaries containing RDS instance information

    Raises:
        Exception: Any AWS/pagination error for the region (caller records it as
            a failed region and surfaces it; it is never masked as empty).
    """
    # Validate region is AWS
    if not utils.is_aws_region(region):
        utils.log_error(f"Invalid AWS region: {region}")
        return []

    rds_instances = []

    # Load pricing data
    pricing_data = load_rds_pricing_data(region)
    storage_pricing = load_storage_pricing_data()

    _partition = utils.detect_partition(region)
    cost_note = (
        "Estimate (us-gov-west-1 pricing)"
        if _partition == 'aws-us-gov'
        else "Estimate (us-east-1 pricing)"
    )

    # Create RDS client for the specified AWS region
    rds_client = utils.get_boto3_client('rds', region_name=region)

    # Get all DB instances using pagination to handle large numbers of instances
    paginator = rds_client.get_paginator('describe_db_instances')
    all_instances = []
    for page in paginator.paginate():
        all_instances.extend(page['DBInstances'])
        time.sleep(0.1)

    total_instances = len(all_instances)
    if total_instances > 0:
        utils.log_info(f"Found {total_instances} RDS instances in {region} to process")

    # Process each instance. One malformed instance must not sink the region, so
    # each is built inside try/except; failures are logged and skipped.
    skipped = 0
    for processed, instance in enumerate(all_instances, start=1):
        instance_id = instance.get('DBInstanceIdentifier', 'Unknown')
        progress = (processed / total_instances) * 100 if total_instances > 0 else 0

        utils.log_info(f"[{progress:.1f}%] Processing RDS instance {processed}/{total_instances}: {instance_id}")
        if processed % 10 == 0:
            print(f"  [{region}] {processed}/{total_instances} RDS instances processed...", flush=True)

        try:
            instance_data = _build_instance_data(
                instance, region, rds_client, pricing_data, storage_pricing, cost_note
            )
        except Exception as e:
            skipped += 1
            utils.log_error(
                f"Skipping RDS instance '{instance_id}' in {region} due to a processing error", e
            )
            continue

        rds_instances.append(instance_data)

    if skipped:
        utils.log_warning(
            f"{skipped} of {total_instances} RDS instance(s) in {region} were skipped due to "
            "processing errors (see log above); the remaining instances were still collected."
        )

    return rds_instances

def export_to_excel(data, account_name, region_filter=None):
    """
    Export RDS instance data to an Excel file using pandas and openpyxl.

    Args:
        data (list): List of dictionaries containing RDS instance information
        account_name (str): Name of the AWS account for filename
        region_filter (str, optional): Region filter to include in filename

    Returns:
        str: Path to the exported file, or None if export failed
    """
    if not data:
        utils.log_warning("No RDS instances found to export.")
        return None

    try:
        # Import pandas (should be installed by now)
        import pandas as pd

        # Convert to pandas DataFrame
        df = pd.DataFrame(data)

        # Sanitize and prepare security-sensitive RDS data (contains usernames, endpoints)
        df = utils.sanitize_for_export(utils.prepare_dataframe_for_export(df))

        # Define output file name with date and optional region filter
        today = datetime.datetime.now().strftime("%m.%d.%Y")
        region_suffix = f"{region_filter}" if region_filter else ""

        # Use the utility function for consistent AWS file naming
        filename = utils.create_export_filename(
            account_name,
            "rds-instances",
            region_suffix,
            today
        )

        # Save using utils function
        saved_file = utils.save_dataframe_to_excel(df, filename, sheet_name='RDS Instances')

        if saved_file:
            utils.log_success("AWS RDS data exported successfully!")
            utils.log_success(f"File location: {saved_file}")
            return saved_file
        else:
            utils.log_error("Failed to save using utils.save_dataframe_to_excel()")
            return None

    except Exception as e:
        utils.log_error("Error exporting data", e)

        # Fallback to CSV if Excel export fails
        try:
            import pandas as pd
            csv_filename = filename.replace('.xlsx', '.csv')
            csv_file = utils.get_output_filepath(csv_filename)
            pd.DataFrame(data).to_csv(csv_file, index=False)
            utils.log_info(f"Exported to CSV instead: {csv_file}")
            return str(csv_file)
        except Exception as csv_error:
            utils.log_error("CSV export also failed", csv_error)
            return None

def main():
    """
    Main function to coordinate the AWS RDS instance export process.
    This function orchestrates the entire workflow from user input to final export.
    """
    # Print script title and get account information
    utils.setup_logging("rds-export")
    account_id, account_name = utils.print_script_banner("AWS RDS INSTANCE EXPORT")

    # Check and install dependencies using utils function
    if not utils.ensure_dependencies('pandas', 'openpyxl'):
        return

    if account_name == "UNKNOWN-ACCOUNT":
        proceed = utils.prompt_for_confirmation("Unable to determine account name. Proceed anyway?", default=False)
        if not proceed:
            utils.log_info("Exiting script...")
            sys.exit(0)

    # Detect partition and set partition-aware example regions
    regions = utils.prompt_region_selection()
    # Initialize data collection list
    all_rds_instances = []

    utils.log_info(f"Collecting RDS instance data across {len(regions)} AWS region(s)...")

    # Define region scan function for concurrent execution (Phase 4B). It lets
    # get_rds_instances raise on a region-level failure so the scanner records
    # that region as FAILED — a failed region must never be collapsed into
    # "empty," which is the silent-data-loss bug (07.15.2026 audit / Issue #231).
    def scan_region_rds(region):
        utils.log_info(f"Searching for RDS instances in AWS region: {region}")
        region_instances = get_rds_instances(region)
        utils.log_info(f"Found {len(region_instances)} RDS instances in {region}")
        return region_instances

    # Use concurrent region scanning (with automatic fallback to sequential on
    # errors). collect_failures=True returns the regions that raised so a failed
    # collection is distinguishable from a genuinely empty account.
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=scan_region_rds,
        show_progress=True,
        collect_failures=True,
    )

    # Flatten results
    for instances in region_results:
        all_rds_instances.extend(instances)

    # Export whatever succeeded, then decide on exit status based on failures.
    utils.log_success(f"Found {len(all_rds_instances)} RDS instances in total across all AWS regions.")

    if all_rds_instances:
        output_file = export_to_excel(all_rds_instances, account_name)
        if output_file:
            utils.log_info(f"Export contains data from {len(regions)} AWS region(s)")
            utils.log_info(f"Total RDS instances exported: {len(all_rds_instances)}")
            print("\nScript execution completed.")
        else:
            utils.log_error("Failed to export data. Please check the logs.")
            sys.exit(1)
    elif not failed_regions:
        # Genuinely empty account: every region succeeded and returned nothing.
        utils.log_warning("No RDS instances found in any AWS region. No file exported.")

    # If ANY region failed, make it loud: write a marker and exit non-zero, even
    # if some data was exported. A partial export that looks complete is exactly
    # the failure mode this guards against.
    if failed_regions:
        utils.report_collection_failures(account_name, "rds-instances", failed_regions)
        print(
            "\nERROR: RDS export completed with failures — data is incomplete. "
            "See the *-rds-instances-FAILED-*.txt marker in the output directory."
        )
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
        sys.exit(1)
    except Exception as e:
        utils.log_error("An unexpected error occurred", e)
        sys.exit(1)
