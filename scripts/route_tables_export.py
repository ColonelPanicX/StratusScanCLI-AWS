#!/usr/bin/env python3

"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS Route Tables Export
Date: NOV-15-2025

Description:
This script exports AWS route table information across all regions or a specific region
into an Excel spreadsheet. It captures route table ID, VPC ID, route destinations, targets,
states, types, propagation status, subnet associations, edge associations, and tags.

Phase 4B Update:
- Concurrent region scanning (4x-10x performance improvement)
- Automatic fallback to sequential on errors
"""

import datetime
import sys
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
args = utils.parse_script_args("Export VPC route tables to Excel")
def get_all_regions():
    """Get list of all available AWS regions for the current partition."""
    try:
        # Detect partition and get ALL regions for that partition
        partition = utils.detect_partition()
        regions = utils.get_partition_regions(partition, all_regions=True)
        utils.log_info(f"Retrieved {len(regions)} regions for partition {partition}")
        return regions
    except Exception as e:
        utils.log_error("Error getting AWS regions", e)
        # Fallback to default regions for the partition
        partition = utils.detect_partition()
        return utils.get_partition_regions(partition, all_regions=False)

def is_valid_region(region_name):
    """
    Check if a region name is valid.

    Args:
        region_name (str): AWS region name

    Returns:
        bool: True if valid region, False otherwise
    """
    all_regions = get_all_regions()
    return region_name in all_regions

def get_vpc_tags(ec2_client, vpc_id):
    """
    Get tags for a VPC.

    Args:
        ec2_client: The boto3 EC2 client
        vpc_id (str): The VPC ID

    Returns:
        dict: Dictionary of tags
    """
    if not vpc_id:
        return {}

    try:
        response = ec2_client.describe_vpcs(VpcIds=[vpc_id])
        if response and 'Vpcs' in response and response['Vpcs']:
            return {tag['Key']: tag['Value'] for tag in response['Vpcs'][0].get('Tags', [])}
    except Exception:
        pass

    return {}

def format_tags(tags_list):
    """
    Format tags into a string.

    Args:
        tags_list (list): List of tag dictionaries

    Returns:
        str: Formatted tags string
    """
    if not tags_list:
        return "N/A"

    tags = []
    for tag in tags_list:
        tags.append(f"{tag.get('Key', '')}={tag.get('Value', '')}")

    return "; ".join(tags)

def get_route_type(route):
    """
    Determine the type of route based on its target.

    Args:
        route (dict): The route information

    Returns:
        str: The route type
    """
    if 'GatewayId' in route and route['GatewayId'].startswith('igw-'):
        return "Internet Gateway"
    elif 'GatewayId' in route and route['GatewayId'].startswith('vgw-'):
        return "Virtual Private Gateway"
    elif 'GatewayId' in route and route['GatewayId'].startswith('nat-'):
        return "NAT Gateway"
    elif 'GatewayId' in route and route['GatewayId'].startswith('tgw-'):
        return "Transit Gateway"
    elif 'NatGatewayId' in route:
        return "NAT Gateway"
    elif 'VpcPeeringConnectionId' in route:
        return "VPC Peering"
    elif 'InstanceId' in route:
        return "EC2 Instance"
    elif 'NetworkInterfaceId' in route:
        return "Network Interface"
    elif 'TransitGatewayId' in route:
        return "Transit Gateway"
    elif 'LocalGatewayId' in route:
        return "Local Gateway"
    elif 'CarrierGatewayId' in route:
        return "Carrier Gateway"
    elif 'GatewayId' in route and route['GatewayId'] == 'local':
        return "Local"
    else:
        return "Unknown"

def get_route_target(route):
    """
    Get the target identifier for a route.

    Args:
        route (dict): The route information

    Returns:
        str: The target identifier
    """
    if 'GatewayId' in route:
        return route['GatewayId']
    elif 'NatGatewayId' in route:
        return route['NatGatewayId']
    elif 'VpcPeeringConnectionId' in route:
        return route['VpcPeeringConnectionId']
    elif 'InstanceId' in route:
        return route['InstanceId']
    elif 'NetworkInterfaceId' in route:
        return route['NetworkInterfaceId']
    elif 'TransitGatewayId' in route:
        return route['TransitGatewayId']
    elif 'LocalGatewayId' in route:
        return route['LocalGatewayId']
    elif 'CarrierGatewayId' in route:
        return route['CarrierGatewayId']
    else:
        return "N/A"

def get_route_destination(route):
    """
    Get the destination for a route.

    Args:
        route (dict): The route information

    Returns:
        str: The route destination
    """
    if 'DestinationCidrBlock' in route:
        return route['DestinationCidrBlock']
    elif 'DestinationIpv6CidrBlock' in route:
        return route['DestinationIpv6CidrBlock']
    elif 'DestinationPrefixListId' in route:
        return route['DestinationPrefixListId']
    else:
        return "N/A"

def _build_route_entries(route_table, region):
    """
    Build the export row(s) for a single route table.

    Extracted so per-route-table processing can be wrapped in try/except by the
    caller: a malformed route table must not discard the whole region's results.
    Every required field is read with ``.get()`` and a safe default so a missing
    field yields 'N/A', not a KeyError that would sink the entire region.

    Args:
        route_table (dict): A single RouteTables entry from describe_route_tables.
        region (str): AWS region name.

    Returns:
        list: One or more route entry dictionaries for this route table.
    """
    rt_id = route_table.get('RouteTableId', 'N/A')
    vpc_id = route_table.get('VpcId', 'N/A')

    # Get routes
    routes = route_table.get('Routes', [])

    # Get subnet associations
    subnet_associations = []
    for association in route_table.get('Associations', []):
        if 'SubnetId' in association:
            subnet_associations.append(association['SubnetId'])

    # Get edge associations (like gateways)
    edge_associations = []
    for association in route_table.get('Associations', []):
        if 'GatewayId' in association:
            edge_associations.append(association['GatewayId'])

    # Get tags
    tags = format_tags(route_table.get('Tags', []))

    entries = []

    # For each route, create a separate entry
    if routes:
        for route in routes:
            route_destination = get_route_destination(route)
            route_target = get_route_target(route)
            route_state = route.get('State', 'N/A')
            route_type = get_route_type(route)
            propagated = route.get('Origin', '') == 'EnableVgwRoutePropagation'

            # Create entry for this route
            entries.append({
                'Region': region,
                'Route Table ID': rt_id,
                'VPC ID': vpc_id,
                'Route Destination': route_destination,
                'Target': route_target,
                'Route State': route_state,
                'Route Type': route_type,
                'Propagated': 'Yes' if propagated else 'No',
                'Subnet Association': '; '.join(subnet_associations) if subnet_associations else 'N/A',
                'Edge Association': '; '.join(edge_associations) if edge_associations else 'N/A',
                'Tags': tags
            })
    else:
        # Create an entry for the route table with no routes
        entries.append({
            'Region': region,
            'Route Table ID': rt_id,
            'VPC ID': vpc_id,
            'Route Destination': 'N/A',
            'Target': 'N/A',
            'Route State': 'N/A',
            'Route Type': 'N/A',
            'Propagated': 'N/A',
            'Subnet Association': '; '.join(subnet_associations) if subnet_associations else 'N/A',
            'Edge Association': '; '.join(edge_associations) if edge_associations else 'N/A',
            'Tags': tags
        })

    return entries


def get_route_tables(region):
    """
    Get all route tables in a specific region.

    Not wrapped in a broad try/except: a swallowed error here would return an
    empty list that the caller cannot distinguish from a genuinely empty
    region, producing silent data loss (no file written, or a zero-row sheet).
    Region-level failures are allowed to raise so the caller can record the
    region as *failed* rather than *empty*. Per-route-table errors are
    contained internally (logged and skipped).

    Args:
        region (str): AWS region name

    Returns:
        list: List of dictionaries with route table information

    Raises:
        Exception: Any AWS/pagination error for the region (caller records it
            as a failed region and surfaces it; it is never masked as empty).
    """
    route_table_data = []

    # Create EC2 client for the region
    ec2_client = utils.get_boto3_client('ec2', region_name=region)

    # Get all route tables in the region
    paginator = ec2_client.get_paginator('describe_route_tables')
    all_route_tables = []
    for page in paginator.paginate():
        all_route_tables.extend(page['RouteTables'])

    total_route_tables = len(all_route_tables)
    if total_route_tables > 0:
        utils.log_info(f"Found {total_route_tables} route tables in {region} to process")

    # Process each route table. One malformed route table must not sink the
    # region, so each is built inside try/except; failures are logged and skipped.
    skipped = 0
    for route_table in all_route_tables:
        rt_id = route_table.get('RouteTableId', 'Unknown')
        try:
            entries = _build_route_entries(route_table, region)
        except Exception as e:
            skipped += 1
            utils.log_error(
                f"Skipping route table '{rt_id}' in {region} due to a processing error", e
            )
            continue

        route_table_data.extend(entries)

    if skipped:
        utils.log_warning(
            f"{skipped} of {total_route_tables} route table(s) in {region} were skipped due to "
            "processing errors (see log above); the remaining route tables were still collected."
        )

    return route_table_data

def export_route_tables(account_name, regions, region_suffix=""):
    """
    Export route table information to an Excel file.

    Args:
        account_name (str): AWS account name
        regions (list): List of AWS regions to export from
        region_suffix (str): Suffix to add to filename (e.g., "-us-east-1")

    Returns:
        tuple: (output_path or None, failed_regions) where failed_regions is a
            list of (region, error_str) tuples for regions whose collection
            raised. Partial data is exported even when some regions failed.
    """
    import pandas as pd

    print("\nCollecting route table information...")

    # Determine which regions to process
    regions_to_process = regions
    if len(regions) == 1:
        print(f"Processing region: {regions[0]}")
    else:
        print(f"Processing {len(regions)} AWS regions")

    # Collect route table data from all regions (Phase 4B: concurrent)
    all_route_tables = []

    # Define region scan function. It lets get_route_tables raise on a
    # region-level failure so the scanner records that region as FAILED — a
    # failed region must never be collapsed into "empty," which is the
    # silent-data-loss bug (07.15.2026 audit / Issue #231).
    def scan_region_route_tables(region):
        print(f"  Collecting route tables from {region}...")
        region_route_tables = get_route_tables(region)
        print(f"  Found {len(region_route_tables)} route entries in {region}")
        return region_route_tables

    # Use concurrent region scanning (with automatic fallback to sequential on
    # errors). collect_failures=True returns the regions that raised so a
    # failed collection is distinguishable from a genuinely empty account.
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions_to_process,
        scan_function=scan_region_route_tables,
        show_progress=True,
        collect_failures=True,
    )

    # Flatten results
    for route_tables in region_results:
        all_route_tables.extend(route_tables)

    # Export whatever succeeded — a partial export is required, not optional.
    output_path = None
    if all_route_tables:
        # Create DataFrame
        df = pd.DataFrame(all_route_tables)

        # Generate file name
        current_date = datetime.datetime.now().strftime("%m.%d.%Y")
        filename = utils.create_export_filename(account_name, "route-tables", region_suffix, current_date)

        # Export to Excel
        output_path = utils.save_dataframe_to_excel(df, filename)
    elif not failed_regions:
        # Genuinely empty: every region succeeded and returned nothing.
        print("No route tables found.")

    return output_path, failed_regions

def main():
    """
    Main function to run the script.
    """
    try:
        # Print script header and get account info
        utils.setup_logging("route-tables-export")
        account_id, account_name = utils.print_script_banner("AWS ROUTE TABLES EXPORT")

        # Check for required dependencies
        if not utils.ensure_dependencies('pandas', 'openpyxl'):
            sys.exit(1)

        # Import pandas now that we've checked dependencies

        # Detect partition and set partition-aware example regions
        regions = utils.prompt_region_selection()

        output_file, failed_regions = export_route_tables(account_name, regions)

        # Report results
        if output_file:
            print("\nExport completed successfully!")
            print(f"Output file: {output_file}")
        elif not failed_regions:
            print("\nExport failed or no data was found.")

        # If ANY region failed, make it loud: write a marker and exit non-zero,
        # even if some data was exported. A partial export that looks complete
        # is exactly the failure mode this guards against.
        if failed_regions:
            utils.report_collection_failures(account_name, "route-tables", failed_regions)
            print(
                "\nERROR: Route Tables export completed with failures — data is incomplete. "
                "See the *-route-tables-FAILED-*.txt marker in the output directory."
            )
            sys.exit(1)

    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
