#!/usr/bin/env python3
"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS VPC, Subnet, IGW, NAT Gateway, Peering Connection, and Elastic IP Export Tool
Date: NOV-15-2025

Description:
This script exports VPC, subnet, Internet Gateway, NAT Gateway, VPC Peering Connection, and Elastic IP information
from AWS regions into an Excel file with separate worksheets. The output filename
includes the AWS account name based on the account ID mapping in the configuration and includes
AWS identifiers for compliance and audit purposes.

Phase 4B Update:
- Concurrent region scanning (4x-10x performance improvement)
- Automatic fallback to sequential on errors
"""

import datetime
import json
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
args = utils.parse_script_args("Export VPCs, subnets, IGWs, NAT gateways, and Elastic IPs to Excel")

def _build_vpc_row(vpc, region, ec2_client):
    """
    Build the export row for a single VPC.

    Extracted so per-VPC processing can be wrapped in try/except by the
    caller: a malformed VPC entry (missing/unexpected fields) is logged and
    skipped rather than discarding the whole region's results. Required
    fields are read with ``.get()`` and a safe default; a missing VpcId
    raises so the caller's per-item guard treats the entry as malformed.

    Args:
        vpc (dict): A single Vpcs entry from describe_vpcs.
        region (str): AWS region name.
        ec2_client: Boto3 EC2 client for the region.

    Returns:
        dict: The assembled VPC row.
    """
    vpc_id = vpc.get('VpcId')
    if not vpc_id:
        raise ValueError("VPC entry missing VpcId")

    # Extract VPC name from tags
    vpc_name = None
    vpc_tags = {}
    for tag in vpc.get('Tags', []):
        key = tag.get('Key')
        if key is None:
            continue
        if key == 'Name':
            vpc_name = tag.get('Value')
        vpc_tags[key] = tag.get('Value')

    # Get primary IPv4 CIDR
    ipv4_cidr = vpc.get('CidrBlock', 'N/A')

    # Get all IPv4 CIDR blocks (including secondary)
    ipv4_cidrs = [ipv4_cidr]
    for assoc in vpc.get('CidrBlockAssociationSet', []):
        cidr = assoc.get('CidrBlock')
        if cidr and cidr not in ipv4_cidrs:
            ipv4_cidrs.append(cidr)
    ipv4_cidr_combined = ', '.join(ipv4_cidrs)

    # Get IPv6 CIDR if available
    ipv6_cidr = 'N/A'
    ipv6_cidrs = []
    for ipv6_assoc in vpc.get('Ipv6CidrBlockAssociationSet', []):
        if ipv6_assoc.get('Ipv6CidrBlockState', {}).get('State') == 'associated':
            cidr = ipv6_assoc.get('Ipv6CidrBlock', 'N/A')
            if cidr != 'N/A':
                ipv6_cidrs.append(cidr)
    if ipv6_cidrs:
        ipv6_cidr = ', '.join(ipv6_cidrs)

    # Get DHCP Options Set
    dhcp_options_id = vpc.get('DhcpOptionsId', 'N/A')

    # Check if default VPC
    is_default = vpc.get('IsDefault', False)
    default_vpc = 'Yes' if is_default else 'No'

    # Get Main Route Table
    main_route_table = 'N/A'
    try:
        rt_response = ec2_client.describe_route_tables(
            Filters=[
                {'Name': 'vpc-id', 'Values': [vpc_id]},
                {'Name': 'association.main', 'Values': ['true']}
            ]
        )
        route_tables = rt_response.get('RouteTables', [])
        if route_tables:
            main_route_table = route_tables[0].get('RouteTableId', 'N/A')
    except Exception as e:
        utils.log_warning(f"Error getting main route table for VPC {vpc_id}: {e}")

    # Get Main NACL
    main_nacl = 'N/A'
    try:
        nacl_response = ec2_client.describe_network_acls(
            Filters=[
                {'Name': 'vpc-id', 'Values': [vpc_id]},
                {'Name': 'default', 'Values': ['true']}
            ]
        )
        nacls = nacl_response.get('NetworkAcls', [])
        if nacls:
            main_nacl = nacls[0].get('NetworkAclId', 'N/A')
    except Exception as e:
        utils.log_warning(f"Error getting main NACL for VPC {vpc_id}: {e}")

    # Get Block Public Access settings (VPC-level BPA for IPv4)
    # Note: This is a newer feature and might not be available in all regions/accounts
    block_public_access = 'Off'  # Default to 'Off' to match AWS console behavior
    try:
        bpa_response = ec2_client.describe_vpc_block_public_access_options(
            VpcIds=[vpc_id]
        )
        bpa_options = bpa_response.get('VpcBlockPublicAccessOptions')
        if bpa_options:
            bpa_option = bpa_options[0]
            internet_gateway_block_mode = bpa_option.get('InternetGatewayBlockMode', 'off')
            # Capitalize first letter to match AWS console display
            block_public_access = internet_gateway_block_mode.capitalize() if internet_gateway_block_mode else 'Off'
    except Exception:
        # If API call fails entirely (feature not available in region), show 'Off'
        # This matches AWS console behavior where the feature just shows as disabled
        block_public_access = 'Off'

    # Format tags as string for better readability
    tags_str = ', '.join([f"{k}={v}" for k, v in vpc_tags.items()]) if vpc_tags else 'N/A'

    return {
        'Region': region,
        'VPC Name': vpc_name if vpc_name else 'N/A',
        'VPC ID': vpc_id,
        'Block Public Access': block_public_access,
        'IPv4 CIDR': ipv4_cidr_combined,
        'IPv6 CIDR': ipv6_cidr,
        'DHCP Option Set': dhcp_options_id,
        'Main Route Table': main_route_table,
        'Main NACL': main_nacl,
        'Default VPC': default_vpc,
        'Tags': tags_str
    }


def collect_vpc_data_for_region(region):
    """
    Collect comprehensive VPC information from a single AWS region.

    Not wrapped in ``aws_error_handler``: a swallowed error here would return
    an empty list that the caller cannot distinguish from a genuinely empty
    region, producing silent data loss. Region-level failures (paginator
    errors, client creation, etc.) are allowed to raise so
    ``scan_regions_concurrent(collect_failures=True)`` can record the region
    as FAILED. Per-VPC errors are contained internally (logged and skipped).

    Args:
        region: AWS region to scan

    Returns:
        list: List of dictionaries with VPC information

    Raises:
        Exception: Any AWS/pagination error for the region.
    """
    vpc_data = []

    # Validate region is AWS
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Collecting VPC details in AWS region: {region}")

    # Create EC2 client for this region
    ec2_client = utils.get_boto3_client('ec2', region_name=region)

    # Get all VPCs in the region
    paginator = ec2_client.get_paginator('describe_vpcs')
    vpcs = []
    for page in paginator.paginate():
        vpcs.extend(page.get('Vpcs', []))

    utils.log_info(f"Found {len(vpcs)} VPCs in AWS region {region}")

    # Process each VPC. One malformed VPC must not sink the region, so each
    # is built inside try/except; failures are logged and skipped.
    skipped = 0
    for vpc in vpcs:
        vpc_id_for_log = vpc.get('VpcId', 'Unknown')
        try:
            vpc_data.append(_build_vpc_row(vpc, region, ec2_client))
        except Exception as e:
            skipped += 1
            utils.log_error(f"Skipping VPC '{vpc_id_for_log}' in {region} due to a processing error", e)
            continue

    if skipped:
        utils.log_warning(
            f"{skipped} of {len(vpcs)} VPC(s) in {region} were skipped due to "
            "processing errors (see log above); the remaining VPCs were still collected."
        )

    return vpc_data

def collect_vpc_data(regions):
    """
    Collect VPC information from AWS regions (Phase 4B: concurrent).

    Args:
        regions: List of AWS regions to scan

    Returns:
        tuple: (list of VPC dictionaries, list of (scope, error) failures)
    """
    utils.log_info("=== COLLECTING VPC INFORMATION ===")

    # Use concurrent region scanning. collect_failures=True lets a failed
    # region surface instead of being collapsed into an empty result.
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=collect_vpc_data_for_region,
        show_progress=True,
        collect_failures=True,
    )

    # Flatten results
    all_vpc_data = []
    for vpcs in region_results:
        all_vpc_data.extend(vpcs)

    utils.log_success(f"Total VPCs collected: {len(all_vpc_data)}")
    tagged_failures = [(f"{region} (vpcs)", err) for region, err in failed_regions]
    return all_vpc_data, tagged_failures

def is_subnet_public(ec2_client, subnet_id, vpc_id):
    """
    Determine if a subnet is public by checking if it has a route to an Internet Gateway.

    Args:
        ec2_client: The boto3 EC2 client
        subnet_id: The ID of the subnet to check
        vpc_id: The ID of the VPC the subnet belongs to

    Returns:
        bool: True if the subnet is public, False otherwise
    """
    try:
        # Get the route tables associated with the subnet
        response = ec2_client.describe_route_tables(
            Filters=[
                {
                    'Name': 'association.subnet-id',
                    'Values': [subnet_id]
                }
            ]
        )

        # If there are explicit route table associations for this subnet
        route_tables = response.get('RouteTables', [])

        # If no explicit route table associated, get the main route table for the VPC
        if not route_tables:
            response = ec2_client.describe_route_tables(
                Filters=[
                    {
                        'Name': 'vpc-id',
                        'Values': [vpc_id]
                    },
                    {
                        'Name': 'association.main',
                        'Values': ['true']
                    }
                ]
            )
            route_tables = response.get('RouteTables', [])

        # Check if any route table has a route to an IGW
        for rt in route_tables:
            for route in rt.get('Routes', []):
                # Check for a default route (0.0.0.0/0) pointing to an IGW
                if route.get('DestinationCidrBlock') == '0.0.0.0/0' and 'GatewayId' in route and route['GatewayId'].startswith('igw-'):
                    return True

        # If we get here, no route to IGW was found
        return False
    except Exception as e:
        utils.log_warning(f"Error checking if subnet {subnet_id} is public: {e}")
        return "Unknown"

def _build_subnet_row(subnet, vpc_id, vpc_name, vpc_cidr, region, ec2_client):
    """
    Build the export row for a single subnet.

    Extracted so per-subnet processing can be wrapped in try/except by the
    caller. Required fields are read with ``.get()`` and a safe default; a
    missing SubnetId raises so the caller's per-item guard treats the entry
    as malformed.

    Args:
        subnet (dict): A single Subnets entry from describe_subnets.
        vpc_id (str): Parent VPC ID.
        vpc_name (str): Parent VPC name ('N/A' if unnamed).
        vpc_cidr (str): Parent VPC CIDR block.
        region (str): AWS region name.
        ec2_client: Boto3 EC2 client for the region.

    Returns:
        dict: The assembled subnet row.
    """
    subnet_id = subnet.get('SubnetId')
    if not subnet_id:
        raise ValueError("Subnet entry missing SubnetId")

    # Extract subnet name and all tags
    subnet_name = None
    subnet_tags = {}
    for tag in subnet.get('Tags', []):
        key = tag.get('Key')
        if key is None:
            continue
        if key == 'Name':
            subnet_name = tag.get('Value')
        subnet_tags[key] = tag.get('Value')

    availability_zone = subnet.get('AvailabilityZone', 'N/A')
    ipv4_cidr = subnet.get('CidrBlock', 'N/A')
    ipv4_address_count = subnet.get('AvailableIpAddressCount', 'N/A')

    # Get IPv6 CIDR if available
    ipv6_cidr = 'N/A'
    for ipv6_assoc in subnet.get('Ipv6CidrBlockAssociationSet', []) or []:
        if ipv6_assoc.get('Ipv6CidrBlockState', {}).get('State') == 'associated':
            ipv6_cidr = ipv6_assoc.get('Ipv6CidrBlock', 'N/A')
            break

    # Determine if subnet is public or private
    public_status = is_subnet_public(ec2_client, subnet_id, vpc_id)
    public_private = "Public" if public_status else "Private"

    # Format tags as string for better readability
    tags_str = ', '.join([f"{k}={v}" for k, v in subnet_tags.items()]) if subnet_tags else 'N/A'

    return {
        'Region': region,
        'VPC Name': vpc_name if vpc_name else 'N/A',
        'VPC ID': vpc_id,
        'VPC CIDR Block': vpc_cidr,
        'Subnet ID': subnet_id,
        'Subnet Name': subnet_name if subnet_name else 'N/A',
        'Availability Zone': availability_zone,
        'IPv4 CIDR Block': ipv4_cidr,
        'IPv4 Address Count': ipv4_address_count,
        'IPv6 CIDR Block': ipv6_cidr,
        'Public/Private': public_private,
        'Subnet Tags': tags_str
    }


def collect_vpc_subnet_data_for_region(region):
    """
    Collect VPC and subnet information from a single AWS region.

    Not wrapped in ``aws_error_handler``: a swallowed error here would return
    an empty list indistinguishable from a genuinely empty region.
    Region-level failures (paginator errors, client creation, etc.) are
    allowed to raise. A single VPC's subnet listing failing (e.g. a
    describe_subnets throttle) only skips that VPC; a single malformed
    subnet only skips that subnet.

    Args:
        region: AWS region to scan

    Returns:
        list: List of dictionaries with subnet information

    Raises:
        Exception: Any AWS/pagination error for the region.
    """
    subnet_data = []

    # Validate region is AWS
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Processing AWS region: {region}")

    # Create EC2 client for this region
    ec2_client = utils.get_boto3_client('ec2', region_name=region)

    # Get all VPCs in the region
    paginator = ec2_client.get_paginator('describe_vpcs')
    vpcs = []
    for page in paginator.paginate():
        vpcs.extend(page.get('Vpcs', []))

    utils.log_info(f"Found {len(vpcs)} VPCs in AWS region {region}")

    skipped_vpcs = 0
    skipped_subnets = 0

    # Process each VPC
    for vpc_index, vpc in enumerate(vpcs, 1):
        vpc_id = vpc.get('VpcId')
        if not vpc_id:
            skipped_vpcs += 1
            utils.log_error(f"Skipping VPC entry missing VpcId in {region}")
            continue

        vpc_progress = (vpc_index / len(vpcs)) * 100 if len(vpcs) > 0 else 0
        utils.log_info(f"  [{vpc_progress:.1f}%] Processing VPC {vpc_index}/{len(vpcs)}: {vpc_id}")

        # Extract VPC name from tags
        vpc_name = None
        for tag in vpc.get('Tags', []):
            if tag.get('Key') == 'Name':
                vpc_name = tag.get('Value')
                break

        # Get VPC CIDR Block
        vpc_cidr = vpc.get('CidrBlock', 'N/A')

        # Get all subnets for this VPC. A describe_subnets failure for one
        # VPC must not sink the whole region.
        try:
            subnet_response = ec2_client.describe_subnets(
                Filters=[
                    {
                        'Name': 'vpc-id',
                        'Values': [vpc_id]
                    }
                ]
            )
            subnets = subnet_response.get('Subnets', [])
        except Exception as e:
            skipped_vpcs += 1
            utils.log_error(f"Skipping subnet collection for VPC '{vpc_id}' in {region} due to a processing error", e)
            continue

        utils.log_info(f"    Found {len(subnets)} subnets")

        # Process each subnet. One malformed subnet must not sink the VPC.
        for subnet_index, subnet in enumerate(subnets, 1):
            subnet_id_for_log = subnet.get('SubnetId', 'Unknown')
            subnet_progress = (subnet_index / len(subnets)) * 100 if len(subnets) > 0 else 0
            if len(subnets) > 1:  # Only log subnet progress if there are multiple subnets
                utils.log_info(f"      [{subnet_progress:.1f}%] Processing subnet {subnet_index}/{len(subnets)}: {subnet_id_for_log}")

            try:
                subnet_data.append(_build_subnet_row(subnet, vpc_id, vpc_name, vpc_cidr, region, ec2_client))
            except Exception as e:
                skipped_subnets += 1
                utils.log_error(f"Skipping subnet '{subnet_id_for_log}' in VPC '{vpc_id}' in {region} due to a processing error", e)
                continue

    if skipped_vpcs or skipped_subnets:
        utils.log_warning(
            f"{skipped_vpcs} VPC(s) and {skipped_subnets} subnet(s) in {region} were skipped "
            "due to processing errors (see log above); the remaining data was still collected."
        )

    return subnet_data

def collect_vpc_subnet_data(regions):
    """
    Collect VPC and subnet information from AWS regions (Phase 4B: concurrent).

    Args:
        regions: List of AWS regions to scan

    Returns:
        tuple: (list of subnet dictionaries, list of (scope, error) failures)
    """
    utils.log_info("=== COLLECTING VPC AND SUBNET INFORMATION ===")

    # Use concurrent region scanning. collect_failures=True lets a failed
    # region surface instead of being collapsed into an empty result.
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=collect_vpc_subnet_data_for_region,
        show_progress=True,
        collect_failures=True,
    )

    # Flatten results
    all_subnet_data = []
    for subnets in region_results:
        all_subnet_data.extend(subnets)

    utils.log_success(f"Total subnets collected: {len(all_subnet_data)}")
    tagged_failures = [(f"{region} (subnets)", err) for region, err in failed_regions]
    return all_subnet_data, tagged_failures

def _load_natgw_monthly_cost() -> Optional[float]:
    """Return NAT Gateway base hourly cost × 730 from pricing JSON; None (renders N/A) when unavailable."""
    pricing_file = Path(__file__).parent.parent / 'reference' / 'natgw-pricing.json'
    try:
        with open(pricing_file, encoding='utf-8') as fh:
            data = json.load(fh)
        hourly = data.get('rates', {}).get('hourly')
        return round(float(hourly) * 730, 2) if hourly is not None else None
    except Exception:
        return None


def _build_natgw_row(nat_gw, region, natgw_monthly_cost):
    """
    Build the export row for a single NAT Gateway.

    Extracted so per-item processing can be wrapped in try/except by the
    caller. A missing NatGatewayId raises so the caller's per-item guard
    treats the entry as malformed.

    Args:
        nat_gw (dict): A single NatGateways entry from describe_nat_gateways.
        region (str): AWS region name.
        natgw_monthly_cost (float): Estimated monthly base cost.

    Returns:
        dict: The assembled NAT Gateway row.
    """
    nat_gw_id = nat_gw.get('NatGatewayId')
    if not nat_gw_id:
        raise ValueError("NAT Gateway entry missing NatGatewayId")

    state = nat_gw.get('State', '')
    connectivity = nat_gw.get('ConnectivityType', '')
    vpc_id = nat_gw.get('VpcId', '')
    subnet_id = nat_gw.get('SubnetId', '')

    # Get creation timestamp and format it
    creation_timestamp = nat_gw.get('CreateTime', '')
    if creation_timestamp:
        # Convert to datetime object and then to string format
        creation_date = creation_timestamp.strftime('%Y-%m-%d') if isinstance(creation_timestamp, datetime.datetime) else str(creation_timestamp)
    else:
        creation_date = ""

    # Extract name from tags
    name = None
    for tag in nat_gw.get('Tags', []):
        if tag.get('Key') == 'Name':
            name = tag.get('Value')
            break

    # Get primary network interface details
    primary_public_ip = ""
    primary_private_ip = ""
    primary_eni_id = ""

    nat_addresses = nat_gw.get('NatGatewayAddresses', [])
    if nat_addresses:
        primary_nat_address = nat_addresses[0]
        primary_public_ip = primary_nat_address.get('PublicIp', '')
        primary_private_ip = primary_nat_address.get('PrivateIp', '')
        primary_eni_id = primary_nat_address.get('NetworkInterfaceId', '')

    # Cost: hourly base charge applies to 'available' gateways only
    if state == 'available':
        monthly_cost = natgw_monthly_cost if natgw_monthly_cost is not None else 'N/A'
        cost_note = 'Hourly base only; excludes data processing charges'
    else:
        monthly_cost = 0.0
        cost_note = f'Not billed (state: {state})'

    return {
        'Region': region,
        'Name': name if name else 'N/A',
        'NAT Gateway ID': nat_gw_id,
        'State': state,
        'Connectivity': connectivity,
        'Primary Public IPv4': primary_public_ip,
        'Primary Private IPv4': primary_private_ip,
        'Primary Network Interface ID': primary_eni_id,
        'VPC': vpc_id,
        'Subnet': subnet_id,
        'Creation Date': creation_date,
        'Monthly Cost (On-Demand)': monthly_cost,
        'Cost Note': cost_note,
    }


def collect_nat_gateway_data_for_region(region):
    """
    Collect NAT Gateway information from a single AWS region.

    Not wrapped in ``aws_error_handler``: a swallowed error here would return
    an empty list indistinguishable from a genuinely empty region.
    Region-level failures are allowed to raise. Per-gateway errors are
    contained internally (logged and skipped).

    Args:
        region: AWS region to scan

    Returns:
        list: List of dictionaries with NAT Gateway information

    Raises:
        Exception: Any AWS error for the region.
    """
    nat_gateways = []
    natgw_monthly_cost = _load_natgw_monthly_cost()

    # Validate region is AWS
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Searching for NAT Gateways in AWS region: {region}")

    # Create EC2 client for this region
    ec2_client = utils.get_boto3_client('ec2', region_name=region)

    # Get NAT Gateways in the region
    nat_gw_response = ec2_client.describe_nat_gateways()
    nat_gws = nat_gw_response.get('NatGateways', [])
    utils.log_info(f"  Found {len(nat_gws)} NAT Gateways")

    # Process each NAT Gateway. One malformed gateway must not sink the region.
    skipped = 0
    for nat_gw in nat_gws:
        nat_gw_id_for_log = nat_gw.get('NatGatewayId', 'Unknown')
        utils.log_info(f"    Processing NAT Gateway: {nat_gw_id_for_log}")

        try:
            nat_gateways.append(_build_natgw_row(nat_gw, region, natgw_monthly_cost))
        except Exception as e:
            skipped += 1
            utils.log_error(f"Skipping NAT Gateway '{nat_gw_id_for_log}' in {region} due to a processing error", e)
            continue

    if skipped:
        utils.log_warning(
            f"{skipped} of {len(nat_gws)} NAT Gateway(s) in {region} were skipped due to "
            "processing errors (see log above); the remaining gateways were still collected."
        )

    return nat_gateways

def collect_nat_gateway_data(regions):
    """
    Collect NAT Gateway information from AWS regions (Phase 4B: concurrent).

    Args:
        regions: List of AWS regions to scan

    Returns:
        tuple: (list of NAT Gateway dictionaries, list of (scope, error) failures)
    """
    utils.log_info("=== COLLECTING NAT GATEWAY INFORMATION ===")

    # Use concurrent region scanning. collect_failures=True lets a failed
    # region surface instead of being collapsed into an empty result.
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=collect_nat_gateway_data_for_region,
        show_progress=True,
        collect_failures=True,
    )

    # Flatten results
    all_nat_gateways = []
    for nat_gws in region_results:
        all_nat_gateways.extend(nat_gws)

    utils.log_success(f"Total NAT Gateways collected: {len(all_nat_gateways)}")
    tagged_failures = [(f"{region} (nat-gateways)", err) for region, err in failed_regions]
    return all_nat_gateways, tagged_failures

def _build_peering_row(peering):
    """
    Build the export row for a single VPC Peering Connection.

    Extracted so per-item processing can be wrapped in try/except by the
    caller. A missing VpcPeeringConnectionId raises so the caller's
    per-item guard treats the entry as malformed.

    Args:
        peering (dict): A single VpcPeeringConnections entry.

    Returns:
        dict: The assembled VPC Peering Connection row.
    """
    peering_id = peering.get('VpcPeeringConnectionId')
    if not peering_id:
        raise ValueError("VPC Peering entry missing VpcPeeringConnectionId")

    # Get peering status
    status = peering.get('Status', {}).get('Code', '')

    # Get requester VPC information
    requester_info = peering.get('RequesterVpcInfo', {})
    requester_vpc = requester_info.get('VpcId', '')
    requester_cidr = requester_info.get('CidrBlock', '')
    requester_owner = requester_info.get('OwnerId', '')
    requester_region = requester_info.get('Region', '')

    # Get accepter VPC information
    accepter_info = peering.get('AccepterVpcInfo', {})
    accepter_vpc = accepter_info.get('VpcId', '')
    accepter_cidr = accepter_info.get('CidrBlock', '')
    accepter_owner = accepter_info.get('OwnerId', '')
    accepter_region = accepter_info.get('Region', '')

    # Format account owner IDs with names if available
    requester_owner_formatted = utils.get_account_name_formatted(requester_owner)
    accepter_owner_formatted = utils.get_account_name_formatted(accepter_owner)

    # Extract name from tags
    name = None
    for tag in peering.get('Tags', []):
        if tag.get('Key') == 'Name':
            name = tag.get('Value')
            break

    return {
        'Name': name if name else 'N/A',
        'Peering Connection ID': peering_id,
        'Status': status,
        'Requester VPC': requester_vpc,
        'Accepter VPC': accepter_vpc,
        'Requester CIDR': requester_cidr,
        'Accepter CIDR': accepter_cidr,
        'Requester Owner ID': requester_owner_formatted,
        'Accepter Owner ID': accepter_owner_formatted,
        'Requester Region': requester_region,
        'Accepter Region': accepter_region
    }


def collect_vpc_peering_data_for_region(region):
    """
    Collect VPC Peering Connection information from a single AWS region.

    Not wrapped in ``aws_error_handler``: a swallowed error here would return
    an empty list indistinguishable from a genuinely empty region.
    Region-level failures are allowed to raise. Per-connection errors are
    contained internally (logged and skipped).

    Args:
        region: AWS region to scan

    Returns:
        list: List of dictionaries with VPC Peering information

    Raises:
        Exception: Any AWS error for the region.
    """
    vpc_peerings = []

    # Validate region is AWS
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Searching for VPC Peering Connections in AWS region: {region}")

    # Create EC2 client for this region
    ec2_client = utils.get_boto3_client('ec2', region_name=region)

    # Get VPC Peering Connections in the region
    peering_response = ec2_client.describe_vpc_peering_connections()
    peerings = peering_response.get('VpcPeeringConnections', [])
    utils.log_info(f"  Found {len(peerings)} VPC Peering Connections")

    # Process each VPC Peering Connection. One malformed entry must not sink the region.
    skipped = 0
    for peering in peerings:
        peering_id_for_log = peering.get('VpcPeeringConnectionId', 'Unknown')
        utils.log_info(f"    Processing VPC Peering Connection: {peering_id_for_log}")

        try:
            vpc_peerings.append(_build_peering_row(peering))
        except Exception as e:
            skipped += 1
            utils.log_error(f"Skipping VPC Peering Connection '{peering_id_for_log}' in {region} due to a processing error", e)
            continue

    if skipped:
        utils.log_warning(
            f"{skipped} of {len(peerings)} VPC Peering Connection(s) in {region} were skipped due to "
            "processing errors (see log above); the remaining connections were still collected."
        )

    return vpc_peerings

def collect_vpc_peering_data(regions):
    """
    Collect VPC Peering Connection information from AWS regions (Phase 4B: concurrent).

    Args:
        regions: List of AWS regions to scan

    Returns:
        tuple: (list of VPC Peering dictionaries, list of (scope, error) failures)
    """
    utils.log_info("=== COLLECTING VPC PEERING CONNECTION INFORMATION ===")

    # Use concurrent region scanning. collect_failures=True lets a failed
    # region surface instead of being collapsed into an empty result.
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=collect_vpc_peering_data_for_region,
        show_progress=True,
        collect_failures=True,
    )

    # Flatten results
    all_vpc_peerings = []
    for peerings in region_results:
        all_vpc_peerings.extend(peerings)

    utils.log_success(f"Total VPC Peering Connections collected: {len(all_vpc_peerings)}")
    tagged_failures = [(f"{region} (vpc-peering)", err) for region, err in failed_regions]
    return all_vpc_peerings, tagged_failures

def _build_igw_row(igw, region):
    """
    Build the export row for a single Internet Gateway.

    Extracted so per-item processing can be wrapped in try/except by the
    caller. A missing InternetGatewayId raises so the caller's per-item
    guard treats the entry as malformed.

    Args:
        igw (dict): A single InternetGateways entry.
        region (str): AWS region name.

    Returns:
        dict: The assembled Internet Gateway row.
    """
    igw_id = igw.get('InternetGatewayId')
    if not igw_id:
        raise ValueError("Internet Gateway entry missing InternetGatewayId")

    owner_id = igw.get('OwnerId', '')

    name = None
    for tag in igw.get('Tags', []):
        if tag.get('Key') == 'Name':
            name = tag.get('Value')
            break

    attachments = igw.get('Attachments', [])
    if attachments:
        vpc_id = attachments[0].get('VpcId', '')
        attachment_state = attachments[0].get('State', '')
    else:
        vpc_id = ''
        attachment_state = 'detached'

    return {
        'Region': region,
        'Name': name if name else 'N/A',
        'Internet Gateway ID': igw_id,
        'Attached VPC ID': vpc_id,
        'Attachment State': attachment_state,
        'Owner Account ID': owner_id,
    }


def collect_internet_gateway_data_for_region(region):
    """
    Collect Internet Gateway information from a single AWS region.

    Not wrapped in ``aws_error_handler``: a swallowed error here would return
    an empty list indistinguishable from a genuinely empty region.
    Region-level failures (paginator errors, client creation, etc.) are
    allowed to raise. Per-gateway errors are contained internally (logged
    and skipped).

    Args:
        region: AWS region to scan

    Returns:
        list: List of dictionaries with Internet Gateway information

    Raises:
        Exception: Any AWS/pagination error for the region.
    """
    igw_data = []

    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Searching for Internet Gateways in AWS region: {region}")

    ec2_client = utils.get_boto3_client('ec2', region_name=region)

    paginator = ec2_client.get_paginator('describe_internet_gateways')
    igws = []
    for page in paginator.paginate():
        igws.extend(page.get('InternetGateways', []))

    utils.log_info(f"  Found {len(igws)} Internet Gateways")

    # Process each Internet Gateway. One malformed entry must not sink the region.
    skipped = 0
    for igw in igws:
        igw_id_for_log = igw.get('InternetGatewayId', 'Unknown')
        try:
            igw_data.append(_build_igw_row(igw, region))
        except Exception as e:
            skipped += 1
            utils.log_error(f"Skipping Internet Gateway '{igw_id_for_log}' in {region} due to a processing error", e)
            continue

    if skipped:
        utils.log_warning(
            f"{skipped} of {len(igws)} Internet Gateway(s) in {region} were skipped due to "
            "processing errors (see log above); the remaining gateways were still collected."
        )

    return igw_data


def collect_internet_gateway_data(regions):
    """
    Collect Internet Gateway information from AWS regions (concurrent).

    Args:
        regions: List of AWS regions to scan

    Returns:
        tuple: (list of Internet Gateway dictionaries, list of (scope, error) failures)
    """
    utils.log_info("=== COLLECTING INTERNET GATEWAY INFORMATION ===")

    # collect_failures=True lets a failed region surface instead of being
    # collapsed into an empty result.
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=collect_internet_gateway_data_for_region,
        show_progress=True,
        collect_failures=True,
    )

    all_igws = []
    for igws in region_results:
        all_igws.extend(igws)

    utils.log_success(f"Total Internet Gateways collected: {len(all_igws)}")
    tagged_failures = [(f"{region} (internet-gateways)", err) for region, err in failed_regions]
    return all_igws, tagged_failures


def _build_eip_row(eip, region):
    """
    Build the export row for a single Elastic IP.

    Extracted so per-item processing can be wrapped in try/except by the
    caller. A missing AllocationId AND PublicIp raises so the caller's
    per-item guard treats the entry as malformed (EC2-Classic EIPs lack
    AllocationId, so only require it when there's no public IP either).

    Args:
        eip (dict): A single Addresses entry from describe_addresses.
        region (str): AWS region name.

    Returns:
        dict: The assembled Elastic IP row.
    """
    allocated_ip = eip.get('PublicIp', '')
    allocation_id = eip.get('AllocationId', '')
    if not allocated_ip and not allocation_id:
        raise ValueError("Elastic IP entry missing both PublicIp and AllocationId")

    domain_type = eip.get('Domain', '')  # 'vpc' or 'standard'

    # Get associated information if available
    instance_id = eip.get('InstanceId', '')
    private_ip = eip.get('PrivateIpAddress', '')
    association_id = eip.get('AssociationId', '')
    network_interface_owner_id = eip.get('NetworkInterfaceOwnerId', '')
    network_border_group = eip.get('NetworkBorderGroup', '')

    # Get Public DNS (Reverse DNS record)
    public_dns = eip.get('PublicDnsName', '')

    # Extract name from tags
    name = None
    for tag in eip.get('Tags', []):
        if tag.get('Key') == 'Name':
            name = tag.get('Value')
            break

    return {
        'Region': region,
        'Name': name if name else 'N/A',
        'Allocated IPv4': allocated_ip,
        'Type': domain_type,
        'Allocation ID': allocation_id,
        'Reverse DNS Record': public_dns,
        'Associated Instance ID': instance_id,
        'Private IPv4': private_ip,
        'Association ID': association_id,
        'Network Interface Owner ID': network_interface_owner_id,
        'Network Border Group': network_border_group
    }


def collect_elastic_ip_data_for_region(region):
    """
    Collect Elastic IP information from a single AWS region.

    Not wrapped in ``aws_error_handler``: a swallowed error here would return
    an empty list indistinguishable from a genuinely empty region.
    Region-level failures are allowed to raise. Per-address errors are
    contained internally (logged and skipped).

    Args:
        region: AWS region to scan

    Returns:
        list: List of dictionaries with Elastic IP information

    Raises:
        Exception: Any AWS error for the region.
    """
    elastic_ips = []

    # Validate region is AWS
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Searching for Elastic IPs in AWS region: {region}")

    # Create EC2 client for this region
    ec2_client = utils.get_boto3_client('ec2', region_name=region)

    # Get Elastic IPs in the region
    eip_response = ec2_client.describe_addresses()
    eips = eip_response.get('Addresses', [])
    utils.log_info(f"  Found {len(eips)} Elastic IPs")

    # Process each Elastic IP. One malformed entry must not sink the region.
    skipped = 0
    for eip in eips:
        allocated_ip_for_log = eip.get('PublicIp', 'Unknown')
        utils.log_info(f"    Processing Elastic IP: {allocated_ip_for_log}")

        try:
            elastic_ips.append(_build_eip_row(eip, region))
        except Exception as e:
            skipped += 1
            utils.log_error(f"Skipping Elastic IP '{allocated_ip_for_log}' in {region} due to a processing error", e)
            continue

    if skipped:
        utils.log_warning(
            f"{skipped} of {len(eips)} Elastic IP(s) in {region} were skipped due to "
            "processing errors (see log above); the remaining addresses were still collected."
        )

    return elastic_ips

def collect_elastic_ip_data(regions):
    """
    Collect Elastic IP information from AWS regions (Phase 4B: concurrent).

    Args:
        regions: List of AWS regions to scan

    Returns:
        tuple: (list of Elastic IP dictionaries, list of (scope, error) failures)
    """
    utils.log_info("=== COLLECTING ELASTIC IP INFORMATION ===")

    # Use concurrent region scanning. collect_failures=True lets a failed
    # region surface instead of being collapsed into an empty result.
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=collect_elastic_ip_data_for_region,
        show_progress=True,
        collect_failures=True,
    )

    # Flatten results
    all_elastic_ips = []
    for eips in region_results:
        all_elastic_ips.extend(eips)

    utils.log_success(f"Total Elastic IPs collected: {len(all_elastic_ips)}")
    tagged_failures = [(f"{region} (elastic-ips)", err) for region, err in failed_regions]
    return all_elastic_ips, tagged_failures

def export_vpc_subnet_natgw_peering_info(account_id, account_name):
    """
    Export VPC, subnet, NAT Gateway, VPC Peering, and Elastic IP information to an Excel file.
    Uses AWS regions and includes AWS identifiers in filenames.

    Args:
        account_id: The AWS account ID
        account_name: The AWS account name
    """
    # Always export all VPC resource types
    export_vpc_subnet = True
    export_nat_gateways = True
    export_vpc_peering = True
    export_elastic_ip = True
    resource_type = "vpc-all"

    regions = utils.prompt_region_selection()
    region_suffix = 'all'
    # Get current date for file naming
    current_date = datetime.datetime.now().strftime("%m.%d.%Y")

    # Create filename using utils with AWS identifier
    final_excel_file = utils.create_export_filename(
        account_name,
        resource_type,
        region_suffix,
        current_date
    )

    utils.log_info(f"Processing {len(regions)} AWS regions: {', '.join(regions)}")

    # Import pandas for DataFrame handling (after dependency check)
    import pandas as pd

    # Dictionary to hold all DataFrames for export
    data_frames = {}

    # Accumulates (scope, error) tuples from every collector below. A region
    # can fail for one resource type (e.g. NAT Gateways) while succeeding for
    # another (e.g. VPCs) in the same pass, so failures are tagged per
    # collector rather than deduplicated by region alone.
    all_failed_regions = []

    # STEP 1: Collect VPC information (if VPC/Subnet selected)
    if export_vpc_subnet:
        all_vpc_data, failed = collect_vpc_data(regions)
        all_failed_regions.extend(failed)
        if all_vpc_data:
            data_frames['VPCs'] = pd.DataFrame(all_vpc_data)

    # STEP 2: Collect VPC and Subnet information (if selected)
    if export_vpc_subnet:
        all_subnet_data, failed = collect_vpc_subnet_data(regions)
        all_failed_regions.extend(failed)
        if all_subnet_data:
            data_frames['VPCs and Subnets'] = pd.DataFrame(all_subnet_data)

    # STEP 3: Collect NAT Gateway information (if selected)
    if export_nat_gateways:
        all_nat_gateway_data, failed = collect_nat_gateway_data(regions)
        all_failed_regions.extend(failed)
        if all_nat_gateway_data:
            data_frames['NAT Gateways'] = pd.DataFrame(all_nat_gateway_data)

    # STEP 4: Collect Internet Gateway information
    all_igw_data, failed = collect_internet_gateway_data(regions)
    all_failed_regions.extend(failed)
    if all_igw_data:
        data_frames['Internet Gateways'] = pd.DataFrame(all_igw_data)

    # STEP 5: Collect VPC Peering information (if selected)
    if export_vpc_peering:
        all_vpc_peering_data, failed = collect_vpc_peering_data(regions)
        all_failed_regions.extend(failed)
        if all_vpc_peering_data:
            data_frames['VPC Peering Connections'] = pd.DataFrame(all_vpc_peering_data)

    # STEP 6: Collect Elastic IP information (if selected)
    if export_elastic_ip:
        all_elastic_ip_data, failed = collect_elastic_ip_data(regions)
        all_failed_regions.extend(failed)
        if all_elastic_ip_data:
            data_frames['Elastic IPs'] = pd.DataFrame(all_elastic_ip_data)

    # STEP 7: Prepare and sanitize all DataFrames
    for sheet_name in data_frames:
        data_frames[sheet_name] = utils.sanitize_for_export(
            utils.prepare_dataframe_for_export(data_frames[sheet_name])
        )

    # STEP 8: Save the Excel file using utils module. Export whatever
    # succeeded (partial export is required — see the 07.16.2026 audit),
    # then decide on exit status based on collected failures below.
    if data_frames:
        try:
            output_path = utils.save_multiple_dataframes_to_excel(data_frames, final_excel_file)

            if output_path:
                utils.log_success("AWS VPC data exported successfully!")
                utils.log_success(f"File location: {output_path}")
                utils.log_info(f"Export contains data from {len(regions)} AWS region(s)")

                # Summary of exported data
                for sheet_name, df in data_frames.items():
                    utils.log_info(f"  - {sheet_name}: {len(df)} records")
            else:
                utils.log_error("Error creating Excel file. Please check the logs.")

        except Exception as e:
            utils.log_error("Error creating Excel file", e)
    elif not all_failed_regions:
        # Genuinely empty account: every collector succeeded and returned nothing.
        utils.log_warning("No data was collected. Nothing to export.")

    # If ANY collector failed for ANY region, make it loud: write a marker and
    # exit non-zero, even if some data was exported. A partial export that
    # looks complete is exactly the failure mode this guards against.
    if all_failed_regions:
        utils.report_collection_failures(account_name, resource_type, all_failed_regions)
        print(
            "\nERROR: VPC export completed with failures — data is incomplete. "
            f"See the *-{resource_type}-FAILED-*.txt marker in the output directory."
        )
        sys.exit(1)

def main():
    """Main function to execute the script."""
    try:
        # Print title and get account information
        utils.setup_logging("vpc-data-export")
        account_id, account_name = utils.print_script_banner("AWS VPC, SUBNET, IGW, NAT GATEWAY, PEERING, AND ELASTIC IP EXPORT")

        # Check and install dependencies
        if not utils.ensure_dependencies('pandas', 'openpyxl'):
            sys.exit(1)

        # Check if account name is unknown
        if account_name == "unknown" and not utils.prompt_for_confirmation("Unable to determine account name. Proceed anyway?", default=False):
            print("Exiting script...")
            sys.exit(0)

        # Export VPC, subnet, NAT Gateway, VPC Peering, and Elastic IP information
        export_vpc_subnet_natgw_peering_info(account_id, account_name)

        print("\nAWS VPC data export script execution completed.")

    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(1)
    except Exception as e:
        utils.log_error("An unexpected error occurred", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
