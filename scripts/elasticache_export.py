#!/usr/bin/env python3
"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS ElastiCache Export Tool
Date: NOV-09-2025

Description:
This script exports AWS ElastiCache information into an Excel file with multiple
worksheets. The output includes Redis and Memcached clusters, replication groups,
cache nodes, parameter groups, and subnet groups.

Features:
- Redis replication groups with automatic failover
- Memcached clusters with cache nodes
- Cache node details (instance type, status, AZ)
- Parameter groups and subnet groups
- Snapshot retention and backup windows
- Encryption at-rest and in-transit
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
args = utils.parse_script_args("Export ElastiCache clusters and replication groups to Excel")


def load_elasticache_pricing_data(region: str = 'us-east-1') -> dict[str, Any]:
    """Load ElastiCache pricing data from the reference JSON file."""
    pricing_data: dict[str, Any] = {}
    try:
        script_dir = Path(__file__).parent.absolute()
        pricing_file = script_dir.parent / 'reference' / 'elasticache-pricing.json'
        if not pricing_file.exists():
            utils.log_warning(f"ElastiCache pricing file not found at {pricing_file}")
            return pricing_data
        with open(pricing_file, encoding='utf-8') as fh:
            data = json.load(fh)
        partition = utils.detect_partition(region)
        pricing_region = 'us-gov-west-1' if partition == 'aws-us-gov' else 'us-east-1'
        for node_type, info in data.get('records', {}).items():
            regional = (
                info.get('pricing', {}).get(pricing_region)
                or {}  # no feed row for this partition -> null, never a us-east-1 stand-in
            )
            if regional:
                pricing_data[node_type] = regional
        utils.log_info(
            f"Loaded ElastiCache pricing data for {len(pricing_data)} node types "
            f"({pricing_region} pricing)"
        )
        return pricing_data
    except Exception as e:
        utils.log_warning(f"Error loading ElastiCache pricing data: {e}")
        return pricing_data


def calculate_elasticache_monthly_cost(
    node_type: str,
    node_count: int,
    engine: str,
    pricing_data: dict[str, Any],
) -> Any:
    """Calculate total monthly cost for an ElastiCache cluster (engine-aware)."""
    if node_type not in pricing_data or not node_count:
        return 'N/A'
    try:
        node_pricing = pricing_data[node_type]
        engine_lower = (engine or '').lower()
        if engine_lower == 'memcached':
            key = 'memcached_on_demand_monthly_usd'
        elif engine_lower == 'valkey':
            key = 'valkey_on_demand_monthly_usd'
        else:  # redis and default
            key = 'redis_on_demand_monthly_usd'
        monthly = node_pricing.get(key)
        if monthly is None:
            return 'N/A'
        return round(float(monthly) * int(node_count), 2)
    except Exception as e:
        utils.log_warning(f"Error calculating ElastiCache cost for {node_type}: {e}")
        return 'N/A'


def _build_replication_group_row(
    rg: dict[str, Any],
    region: str,
    pricing_data: dict[str, Any],
    cost_note: str,
) -> dict[str, Any]:
    """
    Build the export row for a single ElastiCache replication group.

    Extracted so per-item processing can be wrapped in try/except by the
    caller: a malformed replication group entry is logged and skipped rather
    than discarding the whole region's results. Every field is read with
    ``.get()`` and a safe default for the same reason.
    """
    rg_id = rg.get('ReplicationGroupId', 'N/A')
    print(f"  Processing replication group: {rg_id}")

    # Basic info
    description = rg.get('Description', 'N/A')
    status = rg.get('Status', 'UNKNOWN')

    # Cluster mode
    cluster_enabled = rg.get('ClusterEnabled', False)

    # Member clusters
    member_clusters = rg.get('MemberClusters', [])
    member_count = len(member_clusters)

    # Node type
    cache_node_type = rg.get('CacheNodeType', 'N/A')

    # Engine
    engine = 'redis'  # Replication groups are always Redis
    engine_version = rg.get('EngineVersion', 'N/A')

    # Automatic failover
    automatic_failover = rg.get('AutomaticFailover', 'disabled')

    # Multi-AZ
    multi_az = rg.get('MultiAZ', 'disabled')

    # Snapshot retention
    snapshot_retention_limit = rg.get('SnapshotRetentionLimit', 0)
    snapshot_window = rg.get('SnapshotWindow', 'N/A')

    # Encryption
    at_rest_encryption = rg.get('AtRestEncryptionEnabled', False)
    transit_encryption = rg.get('TransitEncryptionEnabled', False)
    auth_token_enabled = rg.get('AuthTokenEnabled', False)

    # Parameter group
    cache_param_group_name = rg.get('CacheParameterGroup', {}).get('CacheParameterGroupName', 'N/A')

    # Subnet group
    cache_subnet_group = rg.get('CacheSubnetGroupName', 'N/A')

    # ARN
    arn = rg.get('ARN', 'N/A')

    # Cost estimation (member_count = total nodes across the replication group)
    monthly_cost = calculate_elasticache_monthly_cost(
        cache_node_type, member_count, engine, pricing_data
    )

    return {
        'Region': region,
        'Replication Group ID': rg_id,
        'Description': description,
        'Status': status,
        'Engine': engine,
        'Engine Version': engine_version,
        'Node Type': cache_node_type,
        'Cluster Mode': 'Enabled' if cluster_enabled else 'Disabled',
        'Member Clusters': member_count,
        'Automatic Failover': automatic_failover.upper(),
        'Multi-AZ': multi_az.upper(),
        'Snapshot Retention (days)': snapshot_retention_limit,
        'Snapshot Window': snapshot_window,
        'Encryption at Rest': 'Yes' if at_rest_encryption else 'No',
        'Encryption in Transit': 'Yes' if transit_encryption else 'No',
        'Auth Token Enabled': 'Yes' if auth_token_enabled else 'No',
        'Subnet Group': cache_subnet_group,
        'Parameter Group': cache_param_group_name,
        'ARN': arn,
        'Monthly Cost (On-Demand)': monthly_cost,
        'Cost Note': cost_note,
    }


def _scan_replication_groups_region(region: str) -> list[dict[str, Any]]:
    """
    Collect ElastiCache replication groups (Redis) from a single region.

    This is a primary scope collector. It deliberately does NOT swallow
    errors: an API/permission failure here must propagate so
    ``scan_regions_concurrent(..., collect_failures=True)`` records the
    region as failed instead of silently reporting "no replication groups"
    (the silent-collection-loss bug — see
    ``.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md``).

    Individual malformed replication groups are skipped (logged) rather than
    aborting the whole region.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    print(f"\nProcessing region: {region}")

    pricing_data = load_elasticache_pricing_data(region)
    partition = utils.detect_partition(region)
    cost_note = (
        "Estimate (us-gov-west-1 pricing)"
        if partition == 'aws-us-gov'
        else "Estimate (us-east-1 pricing)"
    )

    elasticache_client = utils.get_boto3_client('elasticache', region_name=region)
    paginator = elasticache_client.get_paginator('describe_replication_groups')
    regional_replication_groups = []
    rg_count = 0

    for page in paginator.paginate():
        replication_groups = page.get('ReplicationGroups', [])
        rg_count += len(replication_groups)

        for rg in replication_groups:
            try:
                regional_replication_groups.append(
                    _build_replication_group_row(rg, region, pricing_data, cost_note)
                )
            except Exception as e:
                # One malformed replication group is skipped, not fatal to the region.
                utils.log_error(
                    f"Skipping malformed ElastiCache replication group in {region}: "
                    f"{rg.get('ReplicationGroupId', '<unknown>')}",
                    e,
                )
                continue

    print(f"  Found {rg_count} ElastiCache replication groups")
    return regional_replication_groups


def collect_replication_groups(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect ElastiCache replication group (Redis) information across regions,
    surfacing failures.

    Uses ``collect_failures=True`` so a region whose collection errors is
    reported as a failed scope rather than silently collapsed into an empty
    result.

    Returns:
        tuple: ``(replication_groups, failed_regions)`` where
        ``failed_regions`` is a list of ``(region, error_message)`` tuples.
    """
    print("\n=== COLLECTING ELASTICACHE REPLICATION GROUPS (Redis) ===")

    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_replication_groups_region,
        show_progress=True,
        collect_failures=True,
    )
    all_replication_groups = [rg for result in region_results for rg in result]

    utils.log_success(f"Total ElastiCache replication groups collected: {len(all_replication_groups)}")
    return all_replication_groups, failed_regions


def _build_cache_cluster_row(
    cluster: dict[str, Any],
    region: str,
    pricing_data: dict[str, Any],
    cost_note: str,
) -> dict[str, Any]:
    """
    Build the export row for a single ElastiCache cache cluster.

    Extracted so per-item processing can be wrapped in try/except by the
    caller: a malformed cluster entry is logged and skipped rather than
    discarding the whole region's results. Every field is read with
    ``.get()`` and a safe default for the same reason.
    """
    cluster_id = cluster.get('CacheClusterId', 'N/A')
    print(f"  Processing cache cluster: {cluster_id}")

    # Basic info
    engine = cluster.get('Engine', 'N/A')
    engine_version = cluster.get('EngineVersion', 'N/A')
    status = cluster.get('CacheClusterStatus', 'UNKNOWN')

    # Node info
    cache_node_type = cluster.get('CacheNodeType', 'N/A')
    num_cache_nodes = cluster.get('NumCacheNodes', 0)

    # Preferred AZ
    preferred_az = cluster.get('PreferredAvailabilityZone', 'N/A')

    # Creation time
    creation_time = cluster.get('CacheClusterCreateTime', '')
    if creation_time:
        creation_time = creation_time.strftime('%Y-%m-%d %H:%M:%S') if isinstance(creation_time, datetime.datetime) else str(creation_time)

    # Parameter group
    param_group = cluster.get('CacheParameterGroup', {}).get('CacheParameterGroupName', 'N/A')

    # Subnet group
    subnet_group = cluster.get('CacheSubnetGroupName', 'N/A')

    # Security groups
    security_groups = cluster.get('SecurityGroups', [])
    sg_ids = ', '.join([sg.get('SecurityGroupId', '') for sg in security_groups]) if security_groups else 'None'

    # Replication group membership
    replication_group_id = cluster.get('ReplicationGroupId', 'None')

    # Endpoint
    endpoint = cluster.get('ConfigurationEndpoint') or (cluster.get('CacheNodes') or [{}])[0].get('Endpoint', {})
    endpoint_address = endpoint.get('Address', 'N/A') if endpoint else 'N/A'
    endpoint_port = endpoint.get('Port', 'N/A') if endpoint else 'N/A'

    # ARN
    arn = cluster.get('ARN', 'N/A')

    # Cost estimation
    monthly_cost = calculate_elasticache_monthly_cost(
        cache_node_type, num_cache_nodes, engine, pricing_data
    )

    return {
        'Region': region,
        'Cluster ID': cluster_id,
        'Engine': engine,
        'Engine Version': engine_version,
        'Status': status,
        'Node Type': cache_node_type,
        'Number of Nodes': num_cache_nodes,
        'Availability Zone': preferred_az,
        'Replication Group': replication_group_id,
        'Parameter Group': param_group,
        'Subnet Group': subnet_group,
        'Security Groups': sg_ids,
        'Endpoint Address': endpoint_address,
        'Endpoint Port': endpoint_port,
        'Created Date': creation_time if creation_time else 'N/A',
        'ARN': arn,
        'Monthly Cost (On-Demand)': monthly_cost,
        'Cost Note': cost_note,
    }


def _scan_cache_clusters_region(region: str) -> list[dict[str, Any]]:
    """
    Collect ElastiCache cache clusters from a single region.

    This is a primary scope collector. It deliberately does NOT swallow
    errors: an API/permission failure here must propagate so
    ``scan_regions_concurrent(..., collect_failures=True)`` records the
    region as failed instead of silently reporting "no cache clusters" (the
    silent-collection-loss bug — see
    ``.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md``).

    Individual malformed clusters are skipped (logged) rather than aborting
    the whole region.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    print(f"\nProcessing region: {region}")

    pricing_data = load_elasticache_pricing_data(region)
    partition = utils.detect_partition(region)
    cost_note = (
        "Estimate (us-gov-west-1 pricing)"
        if partition == 'aws-us-gov'
        else "Estimate (us-east-1 pricing)"
    )

    elasticache_client = utils.get_boto3_client('elasticache', region_name=region)
    paginator = elasticache_client.get_paginator('describe_cache_clusters')
    regional_clusters = []
    cluster_count = 0

    for page in paginator.paginate(ShowCacheNodeInfo=True):
        cache_clusters = page.get('CacheClusters', [])
        cluster_count += len(cache_clusters)

        for cluster in cache_clusters:
            try:
                regional_clusters.append(
                    _build_cache_cluster_row(cluster, region, pricing_data, cost_note)
                )
            except Exception as e:
                # One malformed cluster is skipped, not fatal to the region.
                utils.log_error(
                    f"Skipping malformed ElastiCache cache cluster in {region}: "
                    f"{cluster.get('CacheClusterId', '<unknown>')}",
                    e,
                )
                continue

    print(f"  Found {cluster_count} ElastiCache cache clusters")
    return regional_clusters


def collect_cache_clusters(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect ElastiCache cache cluster information across regions, surfacing
    failures.

    Uses ``collect_failures=True`` so a region whose collection errors is
    reported as a failed scope rather than silently collapsed into an empty
    result.

    Returns:
        tuple: ``(cache_clusters, failed_regions)`` where ``failed_regions``
        is a list of ``(region, error_message)`` tuples.
    """
    print("\n=== COLLECTING ELASTICACHE CACHE CLUSTERS ===")

    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_cache_clusters_region,
        show_progress=True,
        collect_failures=True,
    )
    all_clusters = [cluster for result in region_results for cluster in result]

    utils.log_success(f"Total ElastiCache cache clusters collected: {len(all_clusters)}")
    return all_clusters, failed_regions


def scan_cache_subnet_groups_in_region(region: str) -> list[dict[str, Any]]:
    """
    Scan ElastiCache cache subnet groups in a single region.

    Args:
        region: AWS region to scan

    Returns:
        list: List of dictionaries with subnet group information from this region
    """
    regional_subnet_groups = []

    try:
        elasticache_client = utils.get_boto3_client('elasticache', region_name=region)

        paginator = elasticache_client.get_paginator('describe_cache_subnet_groups')
        for page in paginator.paginate():
            subnet_groups = page.get('CacheSubnetGroups', [])

            for sg in subnet_groups:
                sg_name = sg.get('CacheSubnetGroupName', 'N/A')

                print(f"  Processing subnet group: {sg_name}")

                # Description
                description = sg.get('CacheSubnetGroupDescription', 'N/A')

                # VPC ID
                vpc_id = sg.get('VpcId', 'N/A')

                # Subnets
                subnets = sg.get('Subnets', [])
                subnet_count = len(subnets)
                subnet_ids = ', '.join([s.get('SubnetIdentifier', '') for s in subnets])

                # Availability zones
                azs = {s.get('SubnetAvailabilityZone', {}).get('Name', '') for s in subnets if s.get('SubnetAvailabilityZone')}
                az_list = ', '.join(sorted(azs)) if azs else 'N/A'

                # ARN
                arn = sg.get('ARN', 'N/A')

                regional_subnet_groups.append({
                    'Region': region,
                    'Subnet Group Name': sg_name,
                    'Description': description,
                    'VPC ID': vpc_id,
                    'Subnet Count': subnet_count,
                    'Subnet IDs': subnet_ids,
                    'Availability Zones': az_list,
                    'ARN': arn
                })

        utils.log_info(f"Found {len(regional_subnet_groups)} ElastiCache subnet groups in {region}")

    except Exception as e:
        utils.log_error(f"Error collecting subnet groups in region {region}", e)

    return regional_subnet_groups


@utils.aws_error_handler("Collecting ElastiCache subnet groups", default_return=[])
def collect_cache_subnet_groups(regions: list[str]) -> list[dict[str, Any]]:
    """
    Collect ElastiCache cache subnet group information from AWS regions using concurrent scanning.

    Enrichment scope: a region-level failure here degrades gracefully (it is
    not one of the primary ElastiCache scopes) and does not fail the whole
    export.

    Args:
        regions: List of AWS regions to scan

    Returns:
        list: List of dictionaries with subnet group information
    """
    print("\n=== COLLECTING ELASTICACHE SUBNET GROUPS ===")
    utils.log_info("Using concurrent region scanning for improved performance")

    # Use concurrent scanning
    all_subnet_groups = []
    for region_data in utils.scan_regions_concurrent(
        regions=regions,
        scan_function=scan_cache_subnet_groups_in_region,
    ):
        all_subnet_groups.extend(region_data)

    utils.log_success(f"Total ElastiCache subnet groups collected: {len(all_subnet_groups)}")
    return all_subnet_groups


def export_elasticache_data(account_id: str, account_name: str):
    """
    Export ElastiCache information to an Excel file.

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

    # STEP 1: Collect replication groups (Redis) — primary scope. Region
    # failures must propagate as failed_regions, never collapse into "empty".
    replication_groups, rg_failed_regions = collect_replication_groups(regions)
    if replication_groups:
        data_frames['Redis Replication Groups'] = pd.DataFrame(replication_groups)

    # STEP 2: Collect cache clusters — primary scope. Region failures must
    # propagate as failed_regions, never collapse into "empty".
    cache_clusters, cluster_failed_regions = collect_cache_clusters(regions)
    if cache_clusters:
        data_frames['Cache Clusters'] = pd.DataFrame(cache_clusters)

    # Both replication groups and cache clusters are primary ElastiCache
    # scopes; a failure in either means the export is incomplete, so their
    # failed regions are merged into one combined list driving a single
    # marker + exit below.
    failed_regions = rg_failed_regions + cluster_failed_regions

    # STEP 3: Collect subnet groups (enrichment — degrades gracefully; a
    # region-level failure here does not fail the whole export).
    subnet_groups = collect_cache_subnet_groups(regions)
    if subnet_groups:
        data_frames['Subnet Groups'] = pd.DataFrame(subnet_groups)

    # STEP 4: Create summary
    if replication_groups or cache_clusters or subnet_groups:
        summary_data = []

        total_rgs = len(replication_groups)
        total_clusters = len(cache_clusters)
        total_subnet_groups = len(subnet_groups)

        # Redis vs Memcached
        redis_clusters = sum(1 for c in cache_clusters if c.get('Engine') == 'redis')
        memcached_clusters = sum(1 for c in cache_clusters if c.get('Engine') == 'memcached')

        # Encryption
        encrypted_at_rest = sum(1 for rg in replication_groups if rg.get('Encryption at Rest') == 'Yes')
        encrypted_in_transit = sum(1 for rg in replication_groups if rg.get('Encryption in Transit') == 'Yes')

        # Cluster mode
        cluster_mode_enabled = sum(1 for rg in replication_groups if rg.get('Cluster Mode') == 'Enabled')

        summary_data.append({'Metric': 'Total Replication Groups', 'Value': total_rgs})
        summary_data.append({'Metric': 'Total Cache Clusters', 'Value': total_clusters})
        summary_data.append({'Metric': 'Redis Clusters', 'Value': redis_clusters})
        summary_data.append({'Metric': 'Memcached Clusters', 'Value': memcached_clusters})
        summary_data.append({'Metric': 'Cluster Mode Enabled', 'Value': cluster_mode_enabled})
        summary_data.append({'Metric': 'Encrypted at Rest', 'Value': encrypted_at_rest})
        summary_data.append({'Metric': 'Encrypted in Transit', 'Value': encrypted_in_transit})
        summary_data.append({'Metric': 'Total Subnet Groups', 'Value': total_subnet_groups})

        data_frames['Summary'] = pd.DataFrame(summary_data)

    # Export whatever succeeded first — a partial export is required even when
    # some regions failed (see the silent-collection-failure blast-radius audit).
    if data_frames:
        # STEP 5: Prepare all DataFrames for export
        for sheet_name in data_frames:
            data_frames[sheet_name] = utils.prepare_dataframe_for_export(data_frames[sheet_name])

        # STEP 6: Create filename and export
        current_date = datetime.datetime.now().strftime("%m.%d.%Y")
        final_excel_file = utils.create_export_filename(
            account_name,
            'elasticache',
            region_suffix,
            current_date
        )

        # Save using utils module for consistent formatting
        try:
            output_path = utils.save_multiple_dataframes_to_excel(data_frames, final_excel_file)

            if output_path:
                utils.log_success("ElastiCache data exported successfully!")
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
        utils.log_warning("No ElastiCache data was collected. Nothing to export.")
        print("\nNo ElastiCache resources found in the selected region(s).")

    # If ANY region failed either primary ElastiCache scope collection
    # (replication groups or cache clusters), make it loud: write a marker
    # and exit non-zero, even if some data was exported. A partial export
    # that looks complete is exactly the failure mode this guards against.
    if failed_regions:
        utils.report_collection_failures(account_name, 'elasticache', failed_regions)
        print(
            "\nERROR: ElastiCache export completed with failures — data is incomplete. "
            "See the *-elasticache-FAILED-*.txt marker in the output directory."
        )
        sys.exit(1)


def main():
    # Initialize logging
    utils.setup_logging("elasticache-export")
    SCRIPT_START_TIME = datetime.datetime.now()
    utils.log_script_start("elasticache-export.py", "AWS ElastiCache Export Tool")

    try:
        # Print title and get account information
        account_id, account_name = utils.print_script_banner("AWS ELASTICACHE EXPORT")

        # Check and install dependencies
        if not utils.ensure_dependencies('pandas', 'openpyxl'):
            sys.exit(1)

        # Check if account name is unknown
        if account_name == "unknown" and not utils.prompt_for_confirmation("Unable to determine account name. Proceed anyway?", default=False):
            print("Exiting script...")
            sys.exit(0)

        # Export ElastiCache data
        export_elasticache_data(account_id, account_name)

        print("\nElastiCache export script execution completed.")

    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        utils.log_info("Script cancelled by user")
        sys.exit(1)
    except Exception as e:
        utils.log_error("An unexpected error occurred", e)
        sys.exit(1)
    finally:
        utils.log_script_end("elasticache-export.py", SCRIPT_START_TIME)


if __name__ == "__main__":
    main()
