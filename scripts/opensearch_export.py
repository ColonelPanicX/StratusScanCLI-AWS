#!/usr/bin/env python3
"""
OpenSearch Service Export Script for StratusScan

Exports comprehensive AWS OpenSearch Service (successor to Elasticsearch) domain information
including cluster configurations, access policies, encryption, VPC settings, and snapshots.

Features:
- OpenSearch Domains: Version, instance types, storage, encryption
- VPC Configuration: Subnets, security groups, endpoints
- Access Policies: Domain access control and fine-grained access control
- Encryption Settings: At-rest, in-transit, node-to-node encryption
- Snapshot Configuration: Automated snapshots to S3
- Summary: Domain counts, versions, and key metrics

Output: Excel file with 3 worksheets
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
args = utils.parse_script_args("Export Amazon OpenSearch Service domains to Excel")

def load_opensearch_pricing_data(region: str = 'us-east-1') -> dict[str, Any]:
    """Load OpenSearch pricing data from the reference JSON file."""
    pricing_data: dict[str, Any] = {}
    try:
        script_dir = Path(__file__).parent.absolute()
        pricing_file = script_dir.parent / 'reference' / 'opensearch-pricing.json'
        if not pricing_file.exists():
            utils.log_warning(f"OpenSearch pricing file not found at {pricing_file}")
            return pricing_data
        with open(pricing_file, encoding='utf-8') as fh:
            data = json.load(fh)
        partition = utils.detect_partition(region)
        pricing_region = 'us-gov-west-1' if partition == 'aws-us-gov' else 'us-east-1'
        for instance_type, info in data.get('records', {}).items():
            regional = (
                info.get('pricing', {}).get(pricing_region)
                or {}  # no feed row for this partition -> null, never a us-east-1 stand-in
            )
            if regional:
                pricing_data[instance_type] = regional
        utils.log_info(
            f"Loaded OpenSearch pricing data for {len(pricing_data)} instance types "
            f"({pricing_region} pricing)"
        )
        return pricing_data
    except Exception as e:
        utils.log_warning(f"Error loading OpenSearch pricing data: {e}")
        return pricing_data


def calculate_opensearch_monthly_cost(
    instance_type: str,
    instance_count: int,
    pricing_data: dict[str, Any],
) -> Any:
    """Calculate monthly cost for OpenSearch data nodes (excludes dedicated masters and warm nodes)."""
    # Strip .search suffix: 'm6g.large.search' -> 'm6g.large'
    lookup_type = instance_type.removesuffix('.search')
    if lookup_type not in pricing_data or not instance_count:
        return 'N/A'
    try:
        monthly = pricing_data[lookup_type].get('on_demand_monthly_usd')
        if monthly is None:
            return 'N/A'
        return round(float(monthly) * int(instance_count), 2)
    except Exception as e:
        utils.log_warning(f"Error calculating OpenSearch cost for {instance_type}: {e}")
        return 'N/A'


def _build_domain_row(domain: dict[str, Any], region: str, pricing_data: dict[str, Any], cost_note: str) -> dict[str, Any]:
    """Build a single OpenSearch domain export row from a describe_domain response."""
    domain_name = domain.get('DomainName', 'Unknown')
    domain_id = domain.get('DomainId', 'N/A')
    arn = domain.get('ARN', 'N/A')

    # Engine version
    engine_version = domain.get('EngineVersion', 'N/A')

    # Cluster configuration
    cluster_config = domain.get('ClusterConfig', {})
    instance_type = cluster_config.get('InstanceType', 'N/A')
    instance_count = cluster_config.get('InstanceCount', 0)
    dedicated_master_enabled = cluster_config.get('DedicatedMasterEnabled', False)
    dedicated_master_type = cluster_config.get('DedicatedMasterType', 'N/A') if dedicated_master_enabled else 'N/A'
    dedicated_master_count = cluster_config.get('DedicatedMasterCount', 0) if dedicated_master_enabled else 0
    zone_awareness_enabled = cluster_config.get('ZoneAwarenessEnabled', False)
    warm_enabled = cluster_config.get('WarmEnabled', False)
    warm_type = cluster_config.get('WarmType', 'N/A') if warm_enabled else 'N/A'
    warm_count = cluster_config.get('WarmCount', 0) if warm_enabled else 0

    # EBS storage
    ebs_options = domain.get('EBSOptions', {})
    ebs_enabled = ebs_options.get('EBSEnabled', False)
    volume_type = ebs_options.get('VolumeType', 'N/A') if ebs_enabled else 'N/A'
    volume_size = ebs_options.get('VolumeSize', 0) if ebs_enabled else 0
    iops = ebs_options.get('Iops', 0) if ebs_enabled else 0

    # VPC configuration
    vpc_options = domain.get('VPCOptions', {})
    vpc_id = vpc_options.get('VPCId', 'N/A')
    subnet_ids = vpc_options.get('SubnetIds', [])
    subnet_ids_str = ', '.join(subnet_ids) if subnet_ids else 'N/A'
    security_group_ids = vpc_options.get('SecurityGroupIds', [])
    security_groups_str = ', '.join(security_group_ids) if security_group_ids else 'N/A'
    availability_zones = vpc_options.get('AvailabilityZones', [])
    az_str = ', '.join(availability_zones) if availability_zones else 'N/A'

    # Endpoints
    endpoint = domain.get('Endpoint', 'N/A')
    endpoints = domain.get('Endpoints', {})
    vpc_endpoint = endpoints.get('vpc', 'N/A') if endpoints else 'N/A'

    # Encryption settings
    encryption_at_rest = domain.get('EncryptionAtRestOptions', {})
    encryption_enabled = encryption_at_rest.get('Enabled', False)
    kms_key_id = encryption_at_rest.get('KmsKeyId', 'N/A') if encryption_enabled else 'N/A'
    if kms_key_id != 'N/A' and '/' in kms_key_id:
        kms_key_id = kms_key_id.split('/')[-1]  # Extract key ID from ARN

    # Node-to-node encryption
    node_to_node_encryption = domain.get('NodeToNodeEncryptionOptions', {})
    node_encryption_enabled = node_to_node_encryption.get('Enabled', False)

    # Domain endpoint encryption (in-transit)
    domain_endpoint_options = domain.get('DomainEndpointOptions', {})
    enforce_https = domain_endpoint_options.get('EnforceHTTPS', False)
    tls_security_policy = domain_endpoint_options.get('TLSSecurityPolicy', 'N/A')

    # Advanced security options (fine-grained access control)
    advanced_security_options = domain.get('AdvancedSecurityOptions', {})
    fine_grained_access_enabled = advanced_security_options.get('Enabled', False)
    internal_user_database_enabled = advanced_security_options.get('InternalUserDatabaseEnabled', False)

    # Cognito options
    cognito_options = domain.get('CognitoOptions', {})
    cognito_enabled = cognito_options.get('Enabled', False)
    user_pool_id = cognito_options.get('UserPoolId', 'N/A') if cognito_enabled else 'N/A'

    # Snapshot configuration
    snapshot_options = domain.get('SnapshotOptions', {})
    automated_snapshot_start_hour = snapshot_options.get('AutomatedSnapshotStartHour', 'N/A')

    # Domain status
    processing = domain.get('Processing', False)
    created = domain.get('Created', False)
    deleted = domain.get('Deleted', False)

    # Auto-Tune options
    auto_tune_options = domain.get('AutoTuneOptions', {})
    auto_tune_state = auto_tune_options.get('State', 'N/A')

    # Access policies
    access_policies = domain.get('AccessPolicies', 'N/A')
    if access_policies != 'N/A':
        try:
            policy_json = json.loads(access_policies)
            # Check if policy allows public access
            statements = policy_json.get('Statement', [])
            public_access = 'No'
            for statement in statements:
                principal = statement.get('Principal', {})
                if principal == '*' or principal.get('AWS') == '*':
                    public_access = 'Yes - Review Policy'
                    break
        except Exception:
            public_access = 'Unknown'
    else:
        public_access = 'N/A'

    # Domain creation time
    created_time = domain.get('Created')
    created_time_str = 'Yes' if created and created_time else 'Unknown'

    # Cost estimation (data nodes only)
    monthly_cost = calculate_opensearch_monthly_cost(
        instance_type, instance_count, pricing_data
    )

    return {
        'Region': region,
        'Domain Name': domain_name,
        'Domain ID': domain_id,
        'Engine Version': engine_version,
        'Status': 'Processing' if processing else ('Deleted' if deleted else 'Active'),
        'Endpoint': endpoint if endpoint != 'N/A' else vpc_endpoint,
        'Instance Type': instance_type,
        'Instance Count': instance_count,
        'Dedicated Master': 'Yes' if dedicated_master_enabled else 'No',
        'Master Type': dedicated_master_type,
        'Master Count': dedicated_master_count,
        'Zone Awareness': 'Enabled' if zone_awareness_enabled else 'Disabled',
        'Warm Storage': 'Enabled' if warm_enabled else 'Disabled',
        'Warm Type': warm_type,
        'Warm Count': warm_count,
        'EBS Enabled': 'Yes' if ebs_enabled else 'No',
        'Volume Type': volume_type,
        'Volume Size (GB)': volume_size,
        'IOPS': iops if iops > 0 else 'N/A',
        'VPC ID': vpc_id,
        'Subnets': subnet_ids_str,
        'Security Groups': security_groups_str,
        'Availability Zones': az_str,
        'Encryption at Rest': 'Yes' if encryption_enabled else 'No',
        'KMS Key ID': kms_key_id,
        'Node-to-Node Encryption': 'Yes' if node_encryption_enabled else 'No',
        'Enforce HTTPS': 'Yes' if enforce_https else 'No',
        'TLS Policy': tls_security_policy,
        'Fine-Grained Access Control': 'Enabled' if fine_grained_access_enabled else 'Disabled',
        'Internal User DB': 'Yes' if internal_user_database_enabled else 'No',
        'Cognito Auth': 'Enabled' if cognito_enabled else 'Disabled',
        'Cognito User Pool': user_pool_id,
        'Snapshot Start Hour': automated_snapshot_start_hour,
        'Auto-Tune': auto_tune_state,
        'Public Access': public_access,
        'Created': created_time_str,
        'ARN': arn,
        'Monthly Cost (On-Demand)': monthly_cost,
        'Cost Note': cost_note,
    }


def scan_opensearch_domains_in_region(region: str) -> list[dict[str, Any]]:
    """
    Scan OpenSearch domains in a single AWS region.

    This is the primary scope collector. It deliberately does NOT swallow
    errors: an API/permission failure here (e.g. ``list_domain_names``) must
    propagate so ``scan_regions_concurrent(..., collect_failures=True)``
    records the region as failed instead of silently reporting "no
    OpenSearch domains" (the silent-collection-loss bug — see
    ``.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md``).

    An individual domain that fails to describe/build is skipped (logged)
    rather than aborting the whole region.
    """
    region_domains: list[dict[str, Any]] = []

    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return region_domains

    pricing_data = load_opensearch_pricing_data(region)
    partition = utils.detect_partition(region)
    cost_note = (
        "Estimate (us-gov-west-1 pricing); data nodes only"
        if partition == 'aws-us-gov'
        else "Estimate (us-east-1 pricing); data nodes only"
    )

    opensearch_client = utils.get_boto3_client('opensearch', region_name=region)

    # List all domain names. Deliberately not wrapped in try/except: a
    # failure here (throttling, access denied, etc.) must raise so the
    # region is recorded as FAILED, not silently treated as empty.
    response = opensearch_client.list_domain_names()
    domain_names = [
        d.get('DomainName') for d in response.get('DomainNames', []) if d.get('DomainName')
    ]

    if not domain_names:
        utils.log_info(f"No OpenSearch domains found in {region}")
        return region_domains

    # Describe each domain
    for domain_name in domain_names:
        try:
            domain_response = opensearch_client.describe_domain(DomainName=domain_name)
            domain = domain_response.get('DomainStatus', {})
            region_domains.append(_build_domain_row(domain, region, pricing_data, cost_note))
        except Exception as e:
            # One malformed/unreachable domain is skipped, not fatal to the region.
            utils.log_error(f"Skipping OpenSearch domain '{domain_name}' in {region}", e)
            continue

    utils.log_info(f"Found {len(region_domains)} OpenSearch domains in {region}")
    return region_domains


def collect_opensearch_domains(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect OpenSearch Service domain information across regions, surfacing failures.

    Uses ``collect_failures=True`` so a region whose collection errors is
    reported as a failed scope rather than silently collapsed into an empty
    result.

    Returns:
        tuple: ``(domains, failed_regions)`` where ``failed_regions`` is a
        list of ``(region, error_message)`` tuples.
    """
    utils.log_info("Using concurrent region scanning for improved performance")

    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=scan_opensearch_domains_in_region,
        show_progress=True,
        collect_failures=True,
    )
    all_domains = [domain for result in region_results for domain in result]
    return all_domains, failed_regions


def scan_opensearch_tags_in_region(region: str) -> list[dict[str, Any]]:
    """Scan OpenSearch domain tags in a single AWS region."""
    region_tags = []

    try:
        opensearch_client = utils.get_boto3_client('opensearch', region_name=region)

        # List all domain names
        try:
            response = opensearch_client.list_domain_names()
            domain_names = [domain['DomainName'] for domain in response.get('DomainNames', [])]
        except Exception as e:
            utils.log_warning(f"Could not list domains in {region}: {str(e)}")
            return region_tags

        if not domain_names:
            return region_tags

        # Get tags for each domain
        for domain_name in domain_names:
            try:
                # Get domain ARN first
                domain_response = opensearch_client.describe_domain(DomainName=domain_name)
                domain_arn = domain_response.get('DomainStatus', {}).get('ARN', '')

                if not domain_arn:
                    continue

                # List tags for this domain
                tags_response = opensearch_client.list_tags(ARN=domain_arn)
                tags = tags_response.get('TagList', [])

                for tag in tags:
                    tag_key = tag.get('Key', 'N/A')
                    tag_value = tag.get('Value', 'N/A')

                    region_tags.append({
                        'Region': region,
                        'Domain Name': domain_name,
                        'Tag Key': tag_key,
                        'Tag Value': tag_value,
                    })

            except Exception as e:
                utils.log_warning(f"Could not retrieve tags for domain {domain_name}: {str(e)}")
                continue

    except Exception as e:
        utils.log_error(f"Error scanning OpenSearch domain tags in {region}", e)

    utils.log_info(f"Found {len(region_tags)} OpenSearch domain tags in {region}")
    return region_tags


@utils.aws_error_handler("Collecting OpenSearch domain tags", default_return=[])
def collect_opensearch_tags(regions: list[str]) -> list[dict[str, Any]]:
    """Collect OpenSearch Service domain tags from AWS regions."""
    utils.log_info("Using concurrent region scanning for improved performance")

    all_tags = []
    for region_data in utils.scan_regions_concurrent(
        regions=regions,
        scan_function=scan_opensearch_tags_in_region,
    ):
        all_tags.extend(region_data)

    return all_tags


def generate_summary(domains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate summary statistics for OpenSearch domains."""
    summary = []

    # Overall counts
    summary.append({
        'Metric': 'Total OpenSearch Domains',
        'Count': len(domains),
        'Details': f"{len([d for d in domains if d['Status'] == 'Active'])} active"
    })

    # Domains by region
    if domains:
        regions = {}
        for domain in domains:
            region = domain['Region']
            regions[region] = regions.get(region, 0) + 1

        region_details = ', '.join([f"{region}: {count}" for region, count in sorted(regions.items())])
        summary.append({
            'Metric': 'Domains by Region',
            'Count': len(regions),
            'Details': region_details
        })

    # OpenSearch versions
    if domains:
        versions = {}
        for domain in domains:
            version = domain['Engine Version']
            versions[version] = versions.get(version, 0) + 1

        version_details = ', '.join([f"{ver}: {count}" for ver, count in sorted(versions.items())])
        summary.append({
            'Metric': 'Engine Versions',
            'Count': len(versions),
            'Details': version_details
        })

    # Encryption statistics
    encrypted_at_rest = len([d for d in domains if d['Encryption at Rest'] == 'Yes'])
    summary.append({
        'Metric': 'Encryption at Rest',
        'Count': encrypted_at_rest,
        'Details': f"{encrypted_at_rest}/{len(domains)} domains encrypted" if domains else "N/A"
    })

    node_encryption = len([d for d in domains if d['Node-to-Node Encryption'] == 'Yes'])
    summary.append({
        'Metric': 'Node-to-Node Encryption',
        'Count': node_encryption,
        'Details': f"{node_encryption}/{len(domains)} domains with node encryption" if domains else "N/A"
    })

    enforce_https = len([d for d in domains if d['Enforce HTTPS'] == 'Yes'])
    summary.append({
        'Metric': 'HTTPS Enforcement',
        'Count': enforce_https,
        'Details': f"{enforce_https}/{len(domains)} domains enforce HTTPS" if domains else "N/A"
    })

    # VPC deployment
    vpc_domains = len([d for d in domains if d['VPC ID'] != 'N/A'])
    summary.append({
        'Metric': 'VPC Deployment',
        'Count': vpc_domains,
        'Details': f"{vpc_domains}/{len(domains)} domains in VPC" if domains else "N/A"
    })

    # Fine-grained access control
    fine_grained = len([d for d in domains if d['Fine-Grained Access Control'] == 'Enabled'])
    summary.append({
        'Metric': 'Fine-Grained Access Control',
        'Count': fine_grained,
        'Details': f"{fine_grained}/{len(domains)} domains with fine-grained access" if domains else "N/A"
    })

    # Dedicated masters
    dedicated_masters = len([d for d in domains if d['Dedicated Master'] == 'Yes'])
    summary.append({
        'Metric': 'Dedicated Master Nodes',
        'Count': dedicated_masters,
        'Details': f"{dedicated_masters}/{len(domains)} domains with dedicated masters" if domains else "N/A"
    })

    # Zone awareness
    zone_aware = len([d for d in domains if d['Zone Awareness'] == 'Enabled'])
    summary.append({
        'Metric': 'Multi-AZ (Zone Awareness)',
        'Count': zone_aware,
        'Details': f"{zone_aware}/{len(domains)} domains multi-AZ" if domains else "N/A"
    })

    # Warm storage
    warm_storage = len([d for d in domains if d['Warm Storage'] == 'Enabled'])
    summary.append({
        'Metric': 'UltraWarm Storage',
        'Count': warm_storage,
        'Details': f"{warm_storage}/{len(domains)} domains with UltraWarm" if domains else "N/A"
    })

    # Public access warning
    public_access = len([d for d in domains if 'Yes' in d['Public Access']])
    if public_access > 0:
        summary.append({
            'Metric': '⚠️ Public Access Detected',
            'Count': public_access,
            'Details': f"{public_access} domains may have public access - review policies"
        })

    # Total storage
    if domains:
        total_storage_gb = sum(d['Volume Size (GB)'] for d in domains if isinstance(d['Volume Size (GB)'], (int, float)))
        summary.append({
            'Metric': 'Total EBS Storage',
            'Count': total_storage_gb,
            'Details': f"{total_storage_gb} GB across all domains"
        })

    # Instance types distribution
    if domains:
        instance_types = {}
        for domain in domains:
            instance_type = domain['Instance Type']
            instance_types[instance_type] = instance_types.get(instance_type, 0) + 1

        top_types = sorted(instance_types.items(), key=lambda x: x[1], reverse=True)[:3]
        type_details = ', '.join([f"{itype}: {count}" for itype, count in top_types])
        summary.append({
            'Metric': 'Top Instance Types',
            'Count': len(instance_types),
            'Details': type_details
        })

    return summary


def _run_export(account_id: str, account_name: str, regions: list[str]) -> None:
    """Collect OpenSearch data and write the Excel export."""
    # Collect data. STEP 1 is the primary scope — region failures must
    # propagate as failed_regions, never collapse into "empty".
    print("\n=== Collecting OpenSearch Data ===")
    domains, failed_regions = collect_opensearch_domains(regions)
    tags = collect_opensearch_tags(regions)

    # Generate summary
    summary = generate_summary(domains)

    # Convert to DataFrames
    domains_df = pd.DataFrame(domains) if domains else pd.DataFrame()
    tags_df = pd.DataFrame(tags) if tags else pd.DataFrame()
    summary_df = pd.DataFrame(summary)

    # Prepare DataFrames for export
    if not domains_df.empty:
        domains_df = utils.prepare_dataframe_for_export(domains_df)
    if not tags_df.empty:
        tags_df = utils.prepare_dataframe_for_export(tags_df)
    if not summary_df.empty:
        summary_df = utils.prepare_dataframe_for_export(summary_df)

    # Create export filename
    region_suffix = regions[0] if len(regions) == 1 else 'all-regions'
    filename = utils.create_export_filename(account_name, 'opensearch', region_suffix)

    # Save to Excel with multiple sheets — the Summary sheet is ALWAYS
    # written (forced, even on a genuinely-empty account), so the workbook
    # always lands. Preserve that behavior.
    print("\n=== Exporting to Excel ===")
    dataframes = {
        'OpenSearch Domains': domains_df,
        'Domain Tags': tags_df,
        'Summary': summary_df
    }

    utils.save_multiple_dataframes_to_excel(dataframes, filename)

    # If ANY region failed the primary OpenSearch domains scope collection,
    # make it loud: write a marker and exit non-zero, even though the
    # workbook (with its always-written Summary sheet) was still exported.
    # A complete-looking workbook that is silently missing failed-region
    # data is exactly the failure mode this guards against. Genuinely-empty
    # (every region succeeded, zero domains) stays exit 0 with no marker.
    if failed_regions:
        utils.report_collection_failures(account_name, 'opensearch', failed_regions)
        print(
            "\nERROR: OpenSearch export completed with failures — data is incomplete. "
            "See the *-opensearch-FAILED-*.txt marker in the output directory."
        )
        sys.exit(1)

def main():
    """Main execution function — 3-step state machine (region -> confirm -> export)."""
    if not utils.ensure_dependencies('pandas', 'openpyxl'):
        return
    global pd
    import pandas as pd
    utils.setup_logging("opensearch-export")

    try:
        account_id, account_name = utils.print_script_banner("AWS OPENSEARCH SERVICE EXPORT")

        step = 1
        regions = None

        while True:
            if step == 1:
                result = utils.prompt_region_selection(service_name="OpenSearch")
                if result == 'back':
                    sys.exit(10)
                if result == 'exit':
                    sys.exit(11)
                regions = result
                step = 2

            elif step == 2:
                region_str = regions[0] if len(regions) == 1 else f"{len(regions)} regions"
                msg = f"Ready to export OpenSearch data ({region_str})."
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
