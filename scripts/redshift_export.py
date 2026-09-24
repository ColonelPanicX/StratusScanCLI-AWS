#!/usr/bin/env python3
"""
Redshift Export Script for StratusScan

Exports comprehensive AWS Redshift data warehouse cluster information including
cluster configurations, snapshots, parameter groups, subnet groups, and usage metrics.

Features:
- Redshift Clusters: Node types, storage, encryption, maintenance windows
- Cluster Snapshots: Manual and automated snapshots with retention
- Parameter Groups: Cluster and workload management parameters
- Subnet Groups: VPC and subnet associations
- Summary: Cluster counts, storage totals, and key metrics

Output: Excel file with 5 worksheets
"""

import json
import sys
from pathlib import Path
from typing import Any

try:
    import utils
except ImportError:
    script_dir = Path(__file__).parent.absolute()
    if script_dir.name.lower() == 'scripts':
        sys.path.append(str(script_dir.parent))
    else:
        sys.path.append(str(script_dir))
    import utils
args = utils.parse_script_args("Export Amazon Redshift clusters to Excel")

def load_redshift_pricing_data(region: str = 'us-east-1') -> dict[str, Any]:
    """Load Redshift pricing data from the reference JSON file."""
    pricing_data: dict[str, Any] = {}
    try:
        script_dir = Path(__file__).parent.absolute()
        pricing_file = script_dir.parent / 'reference' / 'redshift-pricing.json'
        if not pricing_file.exists():
            utils.log_warning(f"Redshift pricing file not found at {pricing_file}")
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
            f"Loaded Redshift pricing data for {len(pricing_data)} node types "
            f"({pricing_region} pricing)"
        )
        return pricing_data
    except Exception as e:
        utils.log_warning(f"Error loading Redshift pricing data: {e}")
        return pricing_data


def calculate_redshift_monthly_cost(
    node_type: str,
    number_of_nodes: int,
    pricing_data: dict[str, Any],
) -> Any:
    """Calculate total monthly cost for a Redshift cluster (per-node price × node count)."""
    if node_type not in pricing_data or not number_of_nodes:
        return 'N/A'
    try:
        monthly = pricing_data[node_type].get('on_demand_monthly_usd')
        if monthly is None:
            return 'N/A'
        return round(float(monthly) * int(number_of_nodes), 2)
    except Exception as e:
        utils.log_warning(f"Error calculating Redshift cost for {node_type}: {e}")
        return 'N/A'


def scan_redshift_clusters_in_region(region: str) -> list[dict[str, Any]]:
    """
    Scan Redshift clusters in a single AWS region.

    This is the primary scope collector. It deliberately does NOT swallow
    errors: an API/permission failure here must propagate so
    ``scan_regions_concurrent(..., collect_failures=True)`` records the
    region as failed instead of silently reporting "no Redshift clusters"
    (the silent-collection-loss bug — see
    ``.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md``).

    Individual malformed clusters are skipped (logged) rather than aborting
    the whole region.

    Args:
        region: AWS region to scan

    Returns:
        list: List of Redshift cluster dictionaries for this region

    Raises:
        Exception: Any AWS/pagination error for the region (caller records
            it as a failed region and surfaces it; it is never masked as
            empty).
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    region_clusters = []
    pricing_data = load_redshift_pricing_data(region)
    partition = utils.detect_partition(region)
    cost_note = (
        "Estimate (us-gov-west-1 pricing)"
        if partition == 'aws-us-gov'
        else "Estimate (us-east-1 pricing)"
    )

    redshift_client = utils.get_boto3_client('redshift', region_name=region)

    paginator = redshift_client.get_paginator('describe_clusters')
    for page in paginator.paginate():
        clusters = page.get('Clusters', [])

        for cluster in clusters:
            try:
                region_clusters.append(
                    _build_cluster_row(cluster, region, pricing_data, cost_note)
                )
            except Exception as e:
                # One malformed cluster is skipped, not fatal to the region.
                utils.log_error(
                    f"Skipping malformed Redshift cluster in {region}: "
                    f"{cluster.get('ClusterIdentifier', '<unknown>')}",
                    e,
                )
                continue

    utils.log_info(f"Found {len(region_clusters)} Redshift clusters in {region}")
    return region_clusters


def _build_cluster_row(
    cluster: dict[str, Any],
    region: str,
    pricing_data: dict[str, Any],
    cost_note: str,
) -> dict[str, Any]:
    """Build a single Redshift cluster export row from a describe_clusters response."""
    cluster_id = cluster.get('ClusterIdentifier', 'N/A')

    # Basic cluster information
    cluster_status = cluster.get('ClusterStatus', 'unknown')
    cluster_version = cluster.get('ClusterVersion', 'N/A')
    node_type = cluster.get('NodeType', 'N/A')
    number_of_nodes = cluster.get('NumberOfNodes', 0)

    # Single node or multi-node
    cluster_type = 'Single-node' if number_of_nodes == 1 else f'Multi-node ({number_of_nodes} nodes)'

    # Database information
    db_name = cluster.get('DBName', 'N/A')
    master_username = cluster.get('MasterUsername', 'N/A')

    # Endpoint information
    endpoint = cluster.get('Endpoint', {})
    endpoint_address = endpoint.get('Address', 'N/A') if endpoint else 'N/A'
    endpoint_port = endpoint.get('Port', 0) if endpoint else 0

    # VPC and networking
    vpc_id = cluster.get('VpcId', 'N/A')
    availability_zone = cluster.get('AvailabilityZone', 'N/A')

    # VPC security groups
    vpc_security_groups = cluster.get('VpcSecurityGroups', [])
    security_group_ids = [sg.get('VpcSecurityGroupId', '') for sg in vpc_security_groups]
    security_groups_str = ', '.join(security_group_ids) if security_group_ids else 'N/A'

    # Cluster subnet group
    cluster_subnet_group_name = cluster.get('ClusterSubnetGroupName', 'N/A')

    # Public accessibility
    publicly_accessible = cluster.get('PubliclyAccessible', False)

    # Encryption
    encrypted = cluster.get('Encrypted', False)
    kms_key_id = cluster.get('KmsKeyId', 'N/A')
    if kms_key_id != 'N/A' and '/' in kms_key_id:
        kms_key_id = kms_key_id.split('/')[-1]  # Extract key ID from ARN

    # Enhanced VPC routing
    enhanced_vpc_routing = cluster.get('EnhancedVpcRouting', False)

    # Maintenance and backup windows
    preferred_maintenance_window = cluster.get('PreferredMaintenanceWindow', 'N/A')
    automated_snapshot_retention_period = cluster.get('AutomatedSnapshotRetentionPeriod', 0)
    manual_snapshot_retention_period = cluster.get('ManualSnapshotRetentionPeriod', -1)

    # Snapshot copy configuration
    cluster_snapshot_copy_status = cluster.get('ClusterSnapshotCopyStatus', {})
    snapshot_copy_enabled = bool(cluster_snapshot_copy_status)
    destination_region = cluster_snapshot_copy_status.get('DestinationRegion', 'N/A') if snapshot_copy_enabled else 'N/A'

    # Cluster parameter group
    cluster_parameter_groups = cluster.get('ClusterParameterGroups', [])
    parameter_group_name = cluster_parameter_groups[0].get('ParameterGroupName', 'N/A') if cluster_parameter_groups else 'N/A'

    # IAM roles
    iam_roles = cluster.get('IamRoles', [])
    iam_role_arns = [role.get('IamRoleArn', '') for role in iam_roles]
    iam_roles_str = ', '.join([arn.split('/')[-1] for arn in iam_role_arns]) if iam_role_arns else 'N/A'

    # Cluster creation time
    cluster_create_time = cluster.get('ClusterCreateTime')
    if cluster_create_time:
        cluster_create_time_str = cluster_create_time.strftime('%Y-%m-%d %H:%M:%S')
    else:
        cluster_create_time_str = 'N/A'

    # Allow version upgrade
    allow_version_upgrade = cluster.get('AllowVersionUpgrade', False)

    # Aqua (Advanced Query Accelerator) configuration
    aqua_configuration = cluster.get('AquaConfiguration', {})
    aqua_status = aqua_configuration.get('AquaStatus', 'N/A') if aqua_configuration else 'N/A'

    # Total storage capacity
    total_storage_capacity_in_megabytes = cluster.get('TotalStorageCapacityInMegaBytes', 0)
    total_storage_gb = round(total_storage_capacity_in_megabytes / 1024, 2) if total_storage_capacity_in_megabytes else 0

    # Cluster revision number
    cluster_revision_number = cluster.get('ClusterRevisionNumber', 'N/A')

    # Logging status
    logging_status = cluster.get('LoggingStatus', {})
    logging_enabled = logging_status.get('LoggingEnabled', False) if logging_status else False
    s3_bucket_name = logging_status.get('BucketName', 'N/A') if logging_enabled else 'N/A'

    # Cost estimation
    monthly_cost = calculate_redshift_monthly_cost(
        node_type, number_of_nodes, pricing_data
    )

    return {
        'Region': region,
        'Cluster ID': cluster_id,
        'Status': cluster_status,
        'Cluster Version': cluster_version,
        'Node Type': node_type,
        'Number of Nodes': number_of_nodes,
        'Cluster Type': cluster_type,
        'Database Name': db_name,
        'Master Username': master_username,
        'Endpoint': endpoint_address,
        'Port': endpoint_port,
        'VPC ID': vpc_id,
        'Availability Zone': availability_zone,
        'Security Groups': security_groups_str,
        'Subnet Group': cluster_subnet_group_name,
        'Publicly Accessible': 'Yes' if publicly_accessible else 'No',
        'Encrypted': 'Yes' if encrypted else 'No',
        'KMS Key ID': kms_key_id if encrypted else 'N/A',
        'Enhanced VPC Routing': 'Yes' if enhanced_vpc_routing else 'No',
        'Maintenance Window': preferred_maintenance_window,
        'Automated Snapshot Retention (Days)': automated_snapshot_retention_period,
        'Manual Snapshot Retention (Days)': manual_snapshot_retention_period if manual_snapshot_retention_period >= 0 else 'Unlimited',
        'Snapshot Copy': 'Enabled' if snapshot_copy_enabled else 'Disabled',
        'Snapshot Copy Destination': destination_region,
        'Parameter Group': parameter_group_name,
        'IAM Roles': iam_roles_str,
        'Allow Version Upgrade': 'Yes' if allow_version_upgrade else 'No',
        'Aqua Status': aqua_status,
        'Total Storage (GB)': total_storage_gb,
        'Logging': 'Enabled' if logging_enabled else 'Disabled',
        'Log S3 Bucket': s3_bucket_name,
        'Created': cluster_create_time_str,
        'Cluster Revision': cluster_revision_number,
        'Monthly Cost (On-Demand)': monthly_cost,
        'Cost Note': cost_note,
    }


def collect_redshift_clusters(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect Redshift cluster information across regions, surfacing failures.

    Uses ``collect_failures=True`` so a region whose collection errors is
    reported as a failed scope rather than silently collapsed into an empty
    result.

    Returns:
        tuple: ``(clusters, failed_regions)`` where ``failed_regions`` is a
        list of ``(region, error_message)`` tuples.
    """
    utils.log_info("Using concurrent region scanning for improved performance")

    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=scan_redshift_clusters_in_region,
        show_progress=True,
        collect_failures=True,
    )
    all_clusters = [cluster for result in region_results for cluster in result]
    return all_clusters, failed_regions


def scan_redshift_snapshots_in_region(region: str) -> list[dict[str, Any]]:
    """Scan Redshift snapshots in a single AWS region."""
    region_snapshots = []

    try:
        redshift_client = utils.get_boto3_client('redshift', region_name=region)

        paginator = redshift_client.get_paginator('describe_cluster_snapshots')
        for page in paginator.paginate():
            snapshots = page.get('Snapshots', [])

            for snapshot in snapshots:
                snapshot_id = snapshot.get('SnapshotIdentifier', 'N/A')
                cluster_id = snapshot.get('ClusterIdentifier', 'N/A')

                # Snapshot type
                snapshot_type = snapshot.get('SnapshotType', 'N/A')  # manual or automated

                # Snapshot status
                status = snapshot.get('Status', 'unknown')

                # Snapshot creation time
                snapshot_create_time = snapshot.get('SnapshotCreateTime')
                if snapshot_create_time:
                    snapshot_create_time_str = snapshot_create_time.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    snapshot_create_time_str = 'N/A'

                # Storage details
                total_backup_size_mb = snapshot.get('TotalBackupSizeInMegaBytes', 0)
                total_backup_size_gb = round(total_backup_size_mb / 1024, 2) if total_backup_size_mb else 0

                actual_incremental_backup_size_mb = snapshot.get('ActualIncrementalBackupSizeInMegaBytes', 0)
                actual_incremental_backup_size_gb = round(actual_incremental_backup_size_mb / 1024, 2) if actual_incremental_backup_size_mb else 0

                # Encryption
                encrypted = snapshot.get('Encrypted', False)
                kms_key_id = snapshot.get('KmsKeyId', 'N/A')
                if kms_key_id != 'N/A' and '/' in kms_key_id:
                    kms_key_id = kms_key_id.split('/')[-1]

                # Snapshot details
                node_type = snapshot.get('NodeType', 'N/A')
                number_of_nodes = snapshot.get('NumberOfNodes', 0)
                db_name = snapshot.get('DBName', 'N/A')
                vpc_id = snapshot.get('VpcId', 'N/A')

                # Availability zone
                availability_zone = snapshot.get('AvailabilityZone', 'N/A')

                # Master username
                master_username = snapshot.get('MasterUsername', 'N/A')

                # Cluster version
                cluster_version = snapshot.get('ClusterVersion', 'N/A')

                # Estimated seconds to completion (for in-progress snapshots)
                estimated_seconds_to_completion = snapshot.get('EstimatedSecondsToCompletion', 0)

                region_snapshots.append({
                    'Region': region,
                    'Snapshot ID': snapshot_id,
                    'Cluster ID': cluster_id,
                    'Snapshot Type': snapshot_type.upper(),
                    'Status': status,
                    'Created': snapshot_create_time_str,
                    'Total Size (GB)': total_backup_size_gb,
                    'Incremental Size (GB)': actual_incremental_backup_size_gb,
                    'Encrypted': 'Yes' if encrypted else 'No',
                    'KMS Key ID': kms_key_id if encrypted else 'N/A',
                    'Node Type': node_type,
                    'Number of Nodes': number_of_nodes,
                    'Database Name': db_name,
                    'VPC ID': vpc_id,
                    'Availability Zone': availability_zone,
                    'Master Username': master_username,
                    'Cluster Version': cluster_version,
                    'Est. Seconds to Complete': estimated_seconds_to_completion if estimated_seconds_to_completion > 0 else 'N/A',
                })

    except Exception as e:
        utils.log_error(f"Error scanning Redshift snapshots in {region}", e)

    utils.log_info(f"Found {len(region_snapshots)} Redshift snapshots in {region}")
    return region_snapshots


@utils.aws_error_handler("Collecting Redshift snapshots", default_return=[])
def collect_redshift_snapshots(regions: list[str]) -> list[dict[str, Any]]:
    """Collect Redshift snapshot information from AWS regions."""
    utils.log_info("Using concurrent region scanning for improved performance")

    all_snapshots = []
    for region_data in utils.scan_regions_concurrent(
        regions=regions,
        scan_function=scan_redshift_snapshots_in_region,
    ):
        all_snapshots.extend(region_data)

    return all_snapshots


def scan_redshift_parameter_groups_in_region(region: str) -> list[dict[str, Any]]:
    """Scan Redshift parameter groups in a single AWS region."""
    region_parameter_groups = []

    try:
        redshift_client = utils.get_boto3_client('redshift', region_name=region)

        paginator = redshift_client.get_paginator('describe_cluster_parameter_groups')
        for page in paginator.paginate():
            parameter_groups = page.get('ParameterGroups', [])

            for pg in parameter_groups:
                parameter_group_name = pg.get('ParameterGroupName', 'N/A')
                parameter_group_family = pg.get('ParameterGroupFamily', 'N/A')
                description = pg.get('Description', 'N/A')

                # Tags
                tags = pg.get('Tags', [])
                tags_str = ', '.join([f"{tag.get('Key', '')}={tag.get('Value', '')}" for tag in tags]) if tags else 'None'

                region_parameter_groups.append({
                    'Region': region,
                    'Parameter Group Name': parameter_group_name,
                    'Family': parameter_group_family,
                    'Description': description,
                    'Tags': tags_str,
                })

    except Exception as e:
        utils.log_error(f"Error scanning Redshift parameter groups in {region}", e)

    utils.log_info(f"Found {len(region_parameter_groups)} Redshift parameter groups in {region}")
    return region_parameter_groups


@utils.aws_error_handler("Collecting Redshift parameter groups", default_return=[])
def collect_redshift_parameter_groups(regions: list[str]) -> list[dict[str, Any]]:
    """Collect Redshift parameter group information from AWS regions."""
    utils.log_info("Using concurrent region scanning for improved performance")

    all_parameter_groups = []
    for region_data in utils.scan_regions_concurrent(
        regions=regions,
        scan_function=scan_redshift_parameter_groups_in_region,
    ):
        all_parameter_groups.extend(region_data)

    return all_parameter_groups


def scan_redshift_subnet_groups_in_region(region: str) -> list[dict[str, Any]]:
    """Scan Redshift subnet groups in a single AWS region."""
    region_subnet_groups = []

    try:
        redshift_client = utils.get_boto3_client('redshift', region_name=region)

        paginator = redshift_client.get_paginator('describe_cluster_subnet_groups')
        for page in paginator.paginate():
            subnet_groups = page.get('ClusterSubnetGroups', [])

            for sg in subnet_groups:
                subnet_group_name = sg.get('ClusterSubnetGroupName', 'N/A')
                description = sg.get('Description', 'N/A')
                vpc_id = sg.get('VpcId', 'N/A')

                # Subnets
                subnets = sg.get('Subnets', [])
                subnet_count = len(subnets)

                # Extract subnet IDs and availability zones
                subnet_ids = [s.get('SubnetIdentifier', '') for s in subnets]
                subnet_ids_str = ', '.join(subnet_ids) if subnet_ids else 'N/A'

                azs = {s.get('SubnetAvailabilityZone', {}).get('Name', '')
                          for s in subnets if s.get('SubnetAvailabilityZone')}
                az_list = ', '.join(sorted(azs)) if azs else 'N/A'

                # Status
                subnet_group_status = sg.get('SubnetGroupStatus', 'unknown')

                # Tags
                tags = sg.get('Tags', [])
                tags_str = ', '.join([f"{tag.get('Key', '')}={tag.get('Value', '')}" for tag in tags]) if tags else 'None'

                region_subnet_groups.append({
                    'Region': region,
                    'Subnet Group Name': subnet_group_name,
                    'Description': description,
                    'VPC ID': vpc_id,
                    'Subnet Count': subnet_count,
                    'Subnet IDs': subnet_ids_str,
                    'Availability Zones': az_list,
                    'Status': subnet_group_status,
                    'Tags': tags_str,
                })

    except Exception as e:
        utils.log_error(f"Error scanning Redshift subnet groups in {region}", e)

    utils.log_info(f"Found {len(region_subnet_groups)} Redshift subnet groups in {region}")
    return region_subnet_groups


@utils.aws_error_handler("Collecting Redshift subnet groups", default_return=[])
def collect_redshift_subnet_groups(regions: list[str]) -> list[dict[str, Any]]:
    """Collect Redshift subnet group information from AWS regions."""
    utils.log_info("Using concurrent region scanning for improved performance")

    all_subnet_groups = []
    for region_data in utils.scan_regions_concurrent(
        regions=regions,
        scan_function=scan_redshift_subnet_groups_in_region,
    ):
        all_subnet_groups.extend(region_data)

    return all_subnet_groups


def generate_summary(clusters: list[dict[str, Any]],
                     snapshots: list[dict[str, Any]],
                     parameter_groups: list[dict[str, Any]],
                     subnet_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate summary statistics for Redshift resources."""
    summary = []

    # Overall counts
    summary.append({
        'Metric': 'Total Redshift Clusters',
        'Count': len(clusters),
        'Details': f"{len([c for c in clusters if c['Status'] == 'available'])} available"
    })

    summary.append({
        'Metric': 'Total Snapshots',
        'Count': len(snapshots),
        'Details': f"{len([s for s in snapshots if s['Snapshot Type'] == 'MANUAL'])} manual, {len([s for s in snapshots if s['Snapshot Type'] == 'AUTOMATED'])} automated"
    })

    summary.append({
        'Metric': 'Total Parameter Groups',
        'Count': len(parameter_groups),
        'Details': f"{len(parameter_groups)} parameter groups"
    })

    summary.append({
        'Metric': 'Total Subnet Groups',
        'Count': len(subnet_groups),
        'Details': f"{sum(sg['Subnet Count'] for sg in subnet_groups)} total subnets"
    })

    # Encryption statistics
    encrypted_clusters = len([c for c in clusters if c['Encrypted'] == 'Yes'])
    summary.append({
        'Metric': 'Encrypted Clusters',
        'Count': encrypted_clusters,
        'Details': f"{encrypted_clusters}/{len(clusters)} clusters encrypted" if clusters else "N/A"
    })

    # Public accessibility warning
    public_clusters = len([c for c in clusters if c['Publicly Accessible'] == 'Yes'])
    if public_clusters > 0:
        summary.append({
            'Metric': '⚠️ Publicly Accessible',
            'Count': public_clusters,
            'Details': f"{public_clusters} clusters are publicly accessible"
        })

    # Enhanced VPC routing
    enhanced_vpc = len([c for c in clusters if c['Enhanced VPC Routing'] == 'Yes'])
    summary.append({
        'Metric': 'Enhanced VPC Routing',
        'Count': enhanced_vpc,
        'Details': f"{enhanced_vpc}/{len(clusters)} clusters with enhanced VPC routing" if clusters else "N/A"
    })

    # Total storage across all clusters
    if clusters:
        total_storage_gb = sum(c['Total Storage (GB)'] for c in clusters if isinstance(c['Total Storage (GB)'], (int, float)))
        summary.append({
            'Metric': 'Total Cluster Storage',
            'Count': round(total_storage_gb, 2),
            'Details': f"{round(total_storage_gb, 2)} GB across all clusters"
        })

    # Total snapshot storage
    if snapshots:
        total_snapshot_storage_gb = sum(s['Total Size (GB)'] for s in snapshots if isinstance(s['Total Size (GB)'], (int, float)))
        summary.append({
            'Metric': 'Total Snapshot Storage',
            'Count': round(total_snapshot_storage_gb, 2),
            'Details': f"{round(total_snapshot_storage_gb, 2)} GB in snapshots"
        })

    # Total nodes across all clusters
    if clusters:
        total_nodes = sum(c['Number of Nodes'] for c in clusters)
        summary.append({
            'Metric': 'Total Nodes',
            'Count': total_nodes,
            'Details': f"{total_nodes} nodes across {len(clusters)} clusters"
        })

    # Clusters by region
    if clusters:
        regions = {}
        for cluster in clusters:
            region = cluster['Region']
            regions[region] = regions.get(region, 0) + 1

        region_details = ', '.join([f"{region}: {count}" for region, count in sorted(regions.items())])
        summary.append({
            'Metric': 'Clusters by Region',
            'Count': len(regions),
            'Details': region_details
        })

    # Node types distribution
    if clusters:
        node_types = {}
        for cluster in clusters:
            node_type = cluster['Node Type']
            node_types[node_type] = node_types.get(node_type, 0) + 1

        top_types = sorted(node_types.items(), key=lambda x: x[1], reverse=True)[:3]
        type_details = ', '.join([f"{ntype}: {count}" for ntype, count in top_types])
        summary.append({
            'Metric': 'Top Node Types',
            'Count': len(node_types),
            'Details': type_details
        })

    # Snapshot copy enabled
    snapshot_copy_enabled = len([c for c in clusters if c['Snapshot Copy'] == 'Enabled'])
    summary.append({
        'Metric': 'Cross-Region Snapshot Copy',
        'Count': snapshot_copy_enabled,
        'Details': f"{snapshot_copy_enabled}/{len(clusters)} clusters with snapshot copy" if clusters else "N/A"
    })

    return summary


def _run_export(account_id: str, account_name: str, regions: list[str]) -> None:
    """Collect Redshift data and write the Excel export."""
    # Collect data
    print("\n=== Collecting Redshift Data ===")
    # STEP 1: Collect Redshift clusters (primary scope — region failures must
    # propagate as failed_regions, never collapse into "empty").
    clusters, failed_regions = collect_redshift_clusters(regions)
    snapshots = collect_redshift_snapshots(regions)
    parameter_groups = collect_redshift_parameter_groups(regions)
    subnet_groups = collect_redshift_subnet_groups(regions)

    # Generate summary
    summary = generate_summary(clusters, snapshots, parameter_groups, subnet_groups)

    # Convert to DataFrames
    clusters_df = pd.DataFrame(clusters) if clusters else pd.DataFrame()
    snapshots_df = pd.DataFrame(snapshots) if snapshots else pd.DataFrame()
    parameter_groups_df = pd.DataFrame(parameter_groups) if parameter_groups else pd.DataFrame()
    subnet_groups_df = pd.DataFrame(subnet_groups) if subnet_groups else pd.DataFrame()
    summary_df = pd.DataFrame(summary)

    # Prepare DataFrames for export
    if not clusters_df.empty:
        clusters_df = utils.prepare_dataframe_for_export(clusters_df)
    if not snapshots_df.empty:
        snapshots_df = utils.prepare_dataframe_for_export(snapshots_df)
    if not parameter_groups_df.empty:
        parameter_groups_df = utils.prepare_dataframe_for_export(parameter_groups_df)
    if not subnet_groups_df.empty:
        subnet_groups_df = utils.prepare_dataframe_for_export(subnet_groups_df)
    if not summary_df.empty:
        summary_df = utils.prepare_dataframe_for_export(summary_df)

    # Create export filename
    region_suffix = regions[0] if len(regions) == 1 else 'all-regions'
    filename = utils.create_export_filename(account_name, 'redshift', region_suffix)

    # Save to Excel with multiple sheets
    print("\n=== Exporting to Excel ===")
    dataframes = {
        'Redshift Clusters': clusters_df,
        'Cluster Snapshots': snapshots_df,
        'Parameter Groups': parameter_groups_df,
        'Subnet Groups': subnet_groups_df,
        'Summary': summary_df
    }

    utils.save_multiple_dataframes_to_excel(dataframes, filename)

    # If ANY region failed the primary cluster scope collection, make it
    # loud: write a marker and exit non-zero, even though the always-written
    # Summary sheet means the workbook still lands. A file that looks
    # complete but is silently missing data is exactly the failure mode
    # this guards against. Genuinely-empty (every region succeeded, zero
    # clusters found) stays a normal exit 0 — no marker.
    if failed_regions:
        utils.report_collection_failures(account_name, 'redshift', failed_regions)
        print(
            "\nERROR: Redshift export completed with failures — data is incomplete. "
            "See the *-redshift-FAILED-*.txt marker in the output directory."
        )
        sys.exit(1)


def main():
    """Main execution function — 3-step state machine (region -> confirm -> export)."""
    if not utils.ensure_dependencies('pandas', 'openpyxl'):
        return
    global pd
    import pandas as pd
    utils.setup_logging("redshift-export")

    try:
        account_id, account_name = utils.print_script_banner("AWS REDSHIFT DATA WAREHOUSE EXPORT")

        step = 1
        regions = None

        while True:
            if step == 1:
                result = utils.prompt_region_selection(service_name="Redshift")
                if result == 'back':
                    sys.exit(10)
                if result == 'exit':
                    sys.exit(11)
                regions = result
                step = 2

            elif step == 2:
                region_str = regions[0] if len(regions) == 1 else f"{len(regions)} regions"
                msg = f"Ready to export Redshift data ({region_str})."
                result = utils.prompt_confirmation(msg)
                if result == 'back':
                    step = 1
                    continue
                if result == 'exit':
                    sys.exit(11)
                step = 3

            elif step == 3:
                _run_export(account_id, account_name, regions)
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
