#!/usr/bin/env python3

"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS Shield Advanced Information Collection Script
Date: NOV-11-2025

Description:
This script collects comprehensive AWS Shield Advanced information from AWS environments
including subscription status, protected resources, attack history, DRT access configuration,
emergency contacts, and protection groups. The data is exported to an Excel spreadsheet
with multiple sheets for complete DDoS protection analysis.

IMPORTANT: AWS Shield Advanced is a premium service costing approximately $3,000/month
plus data transfer fees. This script will check subscription status first and exit
gracefully if Shield Advanced is not subscribed.

Shield Standard is automatically provided for all AWS customers at no additional cost.
This script does NOT export Shield Standard information as it requires no configuration.

Collected information includes: Subscription details, protected resources, attack events,
emergency contacts, DRT access permissions, protection groups, and attack statistics.

Prerequisites:
- AWS Shield Advanced subscription (premium service ~$3000/month)
- Requires IAM permissions: shield:DescribeSubscription, shield:ListProtections,
  shield:DescribeProtection, shield:ListAttacks, shield:DescribeAttack,
  shield:DescribeEmergencyContactSettings, shield:ListProtectionGroups,
  shield:DescribeDRTAccess
- Global service - uses us-east-1 endpoint
"""

import datetime
import sys
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError, NoCredentialsError

# Add path to import utils module
try:
    import utils
except ImportError:
    script_dir = Path(__file__).parent.absolute()
    if script_dir.name.lower() == 'scripts':
        sys.path.append(str(script_dir.parent))
    else:
        sys.path.append(str(script_dir))
    import utils
args = utils.parse_script_args("Export AWS Shield Advanced protections to Excel")


@utils.aws_error_handler("Checking Shield Advanced subscription", default_return=None)
def check_subscription() -> dict[str, Any]:
    """
    Check if Shield Advanced is subscribed.

    Returns:
        dict: Subscription information or None if not subscribed
    """
    # Shield is a global service - always use us-east-1
    # Shield is a global service - use partition-aware home region
    home_region = utils.get_partition_default_region()
    client = utils.get_boto3_client('shield', region_name=home_region)

    try:
        response = client.describe_subscription()
        subscription = response.get('Subscription')

        if subscription:
            utils.log_success("Shield Advanced subscription is active")
            return subscription
        else:
            utils.log_info("No active Shield Advanced subscription found")
            return None

    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'ResourceNotFoundException':
            utils.log_info("Shield Advanced is not subscribed in this account")
            return None
        else:
            utils.log_error(f"Error checking Shield subscription: {error_code}")
            raise


def _build_protection_row(client, protection: dict[str, Any], processed: int, total_protections: int) -> dict[str, Any]:
    """
    Build the export row for a single Shield protection.

    Extracted so per-protection processing can be wrapped in try/except by
    the caller: a malformed protection entry must not sink the whole
    account-scope collection. Required fields are read with ``.get()`` and a
    safe default for the same reason.

    Args:
        client: The boto3 Shield client.
        protection (dict): A single Protections entry from list_protections.
        processed: 1-based index of this protection within the current run.
        total_protections: Total protection count, for progress logging.

    Returns:
        dict: The assembled protection row.
    """
    progress = (processed / total_protections) * 100 if total_protections > 0 else 0

    # Log progress every 10 protections or at completion
    if processed % 10 == 0 or processed == total_protections:
        utils.log_info(f"[{progress:.1f}%] Processed {processed}/{total_protections} protections")

    # Get detailed protection info
    protection_id = protection.get('Id')
    protection_detail = None

    try:
        if protection_id:
            detail_response = client.describe_protection(ProtectionId=protection_id)
            protection_detail = detail_response.get('Protection', {})
    except Exception as e:
        utils.log_debug(f"Could not get details for protection {protection_id}: {e}")

    # Parse resource ARN for type
    resource_arn = protection.get('ResourceArn', 'N/A')
    resource_type = parse_resource_type(resource_arn)

    return {
        'Protection ID': protection.get('Id', 'N/A'),
        'Protection Name': protection.get('Name', 'N/A'),
        'Resource ARN': resource_arn,
        'Resource Type': resource_type,
        'Health Check IDs': format_list(protection.get('HealthCheckIds', [])),
        'Protection ARN': protection.get('ProtectionArn', 'N/A'),
        'Application Layer Automatic Response': 'Configured' if protection_detail and protection_detail.get('ApplicationLayerAutomaticResponseConfiguration') else 'Not Configured'
    }


def collect_protections() -> list[dict[str, Any]]:
    """
    Collect all Shield Advanced protections.

    Not wrapped in ``aws_error_handler`` and does not swallow errors to an
    empty list: a swallowed error here would be indistinguishable from a
    genuinely empty account (no protections configured), producing silent
    data loss (see the 07.15.2026 / 07.16.2026 silent-collection-failure
    audits). Shield Advanced is a global, account-scope service (not
    multi-region — see scripts/iam_export.py for the account-scope
    reference pattern this follows). Account-scope failures (client
    creation, pagination) are allowed to raise so the caller (main) can
    record this scope as *failed* rather than *empty*. Per-protection
    errors are contained internally (logged and skipped).

    Returns:
        list: List of protection information dictionaries.

    Raises:
        Exception: Any AWS/pagination error for the account scope (caller
            records it as a failed scope; it is never masked as empty).
    """
    protections_data = []
    home_region = utils.get_partition_default_region()
    client = utils.get_boto3_client('shield', region_name=home_region)

    # List protections using paginator
    paginator = client.get_paginator('list_protections')

    total_protections = 0
    for page in paginator.paginate():
        protections = page.get('Protections', [])
        total_protections += len(protections)

    if total_protections > 0:
        utils.log_info(f"Found {total_protections} Shield protection(s) to process")
    else:
        utils.log_info("No Shield protections found")
        return []

    # Reset paginator and process protections
    paginator = client.get_paginator('list_protections')
    processed = 0
    skipped = 0

    for page in paginator.paginate():
        protections = page.get('Protections', [])

        for protection in protections:
            processed += 1

            try:
                protection_info = _build_protection_row(client, protection, processed, total_protections)
            except Exception as e:
                skipped += 1
                protection_id = protection.get('Id', 'Unknown') if isinstance(protection, dict) else 'Unknown'
                utils.log_error(f"Skipping Shield protection '{protection_id}' due to a processing error", e)
                continue

            protections_data.append(protection_info)

    if skipped:
        utils.log_warning(
            f"{skipped} of {total_protections} Shield protection(s) were skipped due to "
            "processing errors (see log above); the remaining protections were still collected."
        )

    return protections_data


@utils.aws_error_handler("Collecting Shield attacks", default_return=[])
def collect_attacks() -> list[dict[str, Any]]:
    """
    Collect attack history (last 90 days).

    Returns:
        list: List of attack information dictionaries
    """
    attacks_data = []
    home_region = utils.get_partition_default_region()
    client = utils.get_boto3_client('shield', region_name=home_region)

    try:
        # Get attacks from last 90 days
        end_time = datetime.datetime.now()
        start_time = end_time - datetime.timedelta(days=90)

        utils.log_info(f"Querying attacks from {start_time.strftime('%Y-%m-%d')} to {end_time.strftime('%Y-%m-%d')}")

        # List attacks
        attack_summaries = []
        for page in client.get_paginator('list_attacks').paginate(
            StartTime={'FromInclusive': start_time, 'ToExclusive': end_time}
        ):
            attack_summaries.extend(page.get('AttackSummaries', []))

        if not attack_summaries:
            utils.log_info("No Shield attacks found in the last 90 days")
            return []

        utils.log_info(f"Found {len(attack_summaries)} attack(s) in the last 90 days")

        for attack_summary in attack_summaries:
            attack_id = attack_summary.get('AttackId')

            # Get detailed attack info
            attack_detail = None
            attack_vectors = []

            try:
                if attack_id:
                    detail_response = client.describe_attack(AttackId=attack_id)
                    attack_detail = detail_response.get('Attack', {})

                    # Extract attack vectors
                    vectors = attack_detail.get('AttackVectors', [])
                    attack_vectors = [
                        f"{v.get('VectorType', 'Unknown')} ({v.get('VectorCounters', [{}])[0].get('Max', 0)} max rate)"
                        for v in vectors
                    ]

            except Exception as e:
                utils.log_debug(f"Could not get details for attack {attack_id}: {e}")

            # Calculate duration
            start = attack_summary.get('StartTime')
            end = attack_summary.get('EndTime')
            duration = 'Ongoing' if not end else calculate_duration(start, end)

            attack_info = {
                'Attack ID': attack_id or 'N/A',
                'Resource ARN': attack_summary.get('ResourceArn', 'N/A'),
                'Resource Type': parse_resource_type(attack_summary.get('ResourceArn', '')),
                'Start Time': start or 'N/A',
                'End Time': end or 'N/A',
                'Duration': duration,
                'Attack Vectors': format_list(attack_vectors) if attack_vectors else 'N/A',
                'Mitigations': format_mitigations(attack_detail.get('Mitigations', [])) if attack_detail else 'N/A'
            }

            attacks_data.append(attack_info)

    except Exception as e:
        utils.log_error("Error collecting Shield attacks", e)

    return attacks_data


@utils.aws_error_handler("Collecting emergency contacts", default_return=[])
def collect_emergency_contacts() -> list[dict[str, Any]]:
    """
    Collect emergency contact settings.

    Returns:
        list: List of emergency contact dictionaries
    """
    contacts_data = []
    home_region = utils.get_partition_default_region()
    client = utils.get_boto3_client('shield', region_name=home_region)

    try:
        response = client.describe_emergency_contact_settings()
        contacts = response.get('EmergencyContactList', [])

        if not contacts:
            utils.log_info("No emergency contacts configured")
            return []

        utils.log_info(f"Found {len(contacts)} emergency contact(s)")

        for contact in contacts:
            contact_info = {
                'Email Address': contact.get('EmailAddress', 'N/A'),
                'Phone Number': contact.get('PhoneNumber', 'N/A'),
                'Contact Notes': contact.get('ContactNotes', 'N/A')
            }

            contacts_data.append(contact_info)

    except Exception as e:
        utils.log_error("Error collecting emergency contacts", e)

    return contacts_data


@utils.aws_error_handler("Collecting protection groups", default_return=[])
def collect_protection_groups() -> list[dict[str, Any]]:
    """
    Collect Shield protection groups.

    Returns:
        list: List of protection group dictionaries
    """
    groups_data = []
    home_region = utils.get_partition_default_region()
    client = utils.get_boto3_client('shield', region_name=home_region)

    try:
        # List protection groups
        next_token = None
        while True:
            params = {}
            params['MaxResults'] = 50
            if next_token:
                params['NextToken'] = next_token
            page = client.list_protection_groups(**params)
            groups = page.get('ProtectionGroups', [])

            for group in groups:
                group_info = {
                    'Protection Group ID': group.get('ProtectionGroupId', 'N/A'),
                    'Aggregation': group.get('Aggregation', 'N/A'),
                    'Pattern': group.get('Pattern', 'N/A'),
                    'Resource Type': group.get('ResourceType', 'N/A'),
                    'Members': format_list(group.get('Members', [])),
                    'Member Count': len(group.get('Members', [])),
                    'Protection Group ARN': group.get('ProtectionGroupArn', 'N/A')
                }

                groups_data.append(group_info)

            next_token = page.get('NextToken')
            if not next_token:
                break

        if groups_data:
            utils.log_info(f"Found {len(groups_data)} protection group(s)")
        else:
            utils.log_info("No protection groups configured")

    except Exception as e:
        utils.log_error("Error collecting protection groups", e)

    return groups_data


@utils.aws_error_handler("Collecting DRT access configuration", default_return={})
def collect_drt_access() -> dict[str, Any]:
    """
    Collect DDoS Response Team (DRT) access configuration.

    Returns:
        dict: DRT access information
    """
    home_region = utils.get_partition_default_region()
    client = utils.get_boto3_client('shield', region_name=home_region)

    try:
        response = client.describe_drt_access()

        drt_info = {
            'Role ARN': response.get('RoleArn', 'N/A'),
            'Log Bucket List': format_list(response.get('LogBucketList', [])),
            'Status': 'Configured' if response.get('RoleArn') else 'Not Configured'
        }

        utils.log_info(f"DRT access status: {drt_info['Status']}")
        return drt_info

    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'ResourceNotFoundException':
            utils.log_info("DRT access not configured")
            return {
                'Role ARN': 'Not Configured',
                'Log Bucket List': 'N/A',
                'Status': 'Not Configured'
            }
        else:
            utils.log_error(f"Error collecting DRT access: {error_code}")
            return {
                'Role ARN': 'Error',
                'Log Bucket List': 'Error',
                'Status': 'Error'
            }


def parse_resource_type(resource_arn: str) -> str:
    """
    Parse resource type from ARN.

    Args:
        resource_arn: Resource ARN

    Returns:
        str: Resource type
    """
    if not resource_arn or resource_arn == 'N/A':
        return 'Unknown'

    try:
        # ARN format: arn:partition:service:region:account:resource
        parts = resource_arn.split(':')
        if len(parts) >= 6:
            service = parts[2]
            resource = parts[5]

            # Map common Shield-protected resources
            if service == 'ec2' and 'eip' in resource.lower():
                return 'Elastic IP'
            elif service == 'elasticloadbalancing':
                if 'loadbalancer/app/' in resource:
                    return 'Application Load Balancer'
                elif 'loadbalancer/net/' in resource:
                    return 'Network Load Balancer'
                else:
                    return 'Classic Load Balancer'
            elif service == 'cloudfront':
                return 'CloudFront Distribution'
            elif service == 'route53':
                return 'Route 53 Hosted Zone'
            elif service == 'globalaccelerator':
                return 'Global Accelerator'
            else:
                return service.upper()
    except Exception:
        pass

    return 'Unknown'


def format_list(items: list[str]) -> str:
    """
    Format a list for display.

    Args:
        items: List of strings

    Returns:
        str: Formatted string
    """
    if not items:
        return 'N/A'
    if len(items) > 5:
        return '; '.join(items[:5]) + f' (and {len(items) - 5} more)'
    return '; '.join(items)


def format_mitigations(mitigations: list[dict[str, Any]]) -> str:
    """
    Format mitigation information.

    Args:
        mitigations: List of mitigation dictionaries

    Returns:
        str: Formatted mitigation string
    """
    if not mitigations:
        return 'N/A'

    mitigation_names = [m.get('MitigationName', 'Unknown') for m in mitigations]
    return format_list(mitigation_names)


def calculate_duration(start_time, end_time) -> str:
    """
    Calculate attack duration.

    Args:
        start_time: Start datetime
        end_time: End datetime

    Returns:
        str: Formatted duration
    """
    try:
        if isinstance(start_time, str):
            start_time = datetime.datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        if isinstance(end_time, str):
            end_time = datetime.datetime.fromisoformat(end_time.replace('Z', '+00:00'))

        duration = end_time - start_time
        total_seconds = int(duration.total_seconds())

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        if hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
    except Exception:
        return 'Unknown'


def export_to_excel(
    subscription_data: dict[str, Any],
    protections_data: list[dict[str, Any]],
    attacks_data: list[dict[str, Any]],
    contacts_data: list[dict[str, Any]],
    groups_data: list[dict[str, Any]],
    drt_data: dict[str, Any],
    account_name: str
) -> str:
    """
    Export Shield Advanced data to Excel file with multiple sheets.

    Args:
        subscription_data: Subscription information
        protections_data: List of protection information
        attacks_data: List of attack information
        contacts_data: List of emergency contacts
        groups_data: List of protection groups
        drt_data: DRT access information
        account_name: AWS account name

    Returns:
        str: Filename of exported file or None if failed
    """
    try:
        import pandas as pd

        # Generate filename
        current_date = datetime.datetime.now().strftime("%m.%d.%Y")
        filename = utils.create_export_filename(
            account_name,
            "shield-advanced",
            "comprehensive",
            current_date
        )

        # Create data frames for multi-sheet export
        data_frames = {}

        # Overall Summary Sheet
        summary_data = {
            'Metric': [
                'Subscription Status',
                'Subscription Start Time',
                'Auto Renew',
                'Proactive Engagement',
                'Total Protected Resources',
                'Protected EIPs',
                'Protected Load Balancers',
                'Protected CloudFront Distributions',
                'Protected Route 53 Zones',
                'Total Attacks (Last 90 Days)',
                'Emergency Contacts Configured',
                'Protection Groups',
                'DRT Access Status'
            ],
            'Value': [
                'Active' if subscription_data else 'Not Subscribed',
                subscription_data.get('StartTime', 'N/A') if subscription_data else 'N/A',
                subscription_data.get('AutoRenew', 'N/A') if subscription_data else 'N/A',
                subscription_data.get('ProactiveEngagementStatus', 'N/A') if subscription_data else 'N/A',
                len(protections_data),
                len([p for p in protections_data if p.get('Resource Type') == 'Elastic IP']),
                len([p for p in protections_data if 'Load Balancer' in p.get('Resource Type', '')]),
                len([p for p in protections_data if p.get('Resource Type') == 'CloudFront Distribution']),
                len([p for p in protections_data if p.get('Resource Type') == 'Route 53 Hosted Zone']),
                len(attacks_data),
                len(contacts_data),
                len(groups_data),
                drt_data.get('Status', 'Unknown')
            ]
        }
        summary_df = pd.DataFrame(summary_data)
        data_frames['Summary'] = summary_df

        # Subscription Sheet
        if subscription_data:
            subscription_info = {
                'Attribute': [
                    'Subscription ARN',
                    'Start Time',
                    'End Time',
                    'Time Commitment (Months)',
                    'Auto Renew',
                    'Proactive Engagement Status',
                    'Subscription Limits'
                ],
                'Value': [
                    subscription_data.get('SubscriptionArn', 'N/A'),
                    subscription_data.get('StartTime', 'N/A'),
                    subscription_data.get('EndTime', 'N/A'),
                    subscription_data.get('TimeCommitmentInSeconds', 0) // (30 * 24 * 3600) if subscription_data.get('TimeCommitmentInSeconds') else 'N/A',
                    subscription_data.get('AutoRenew', 'N/A'),
                    subscription_data.get('ProactiveEngagementStatus', 'N/A'),
                    str(subscription_data.get('SubscriptionLimits', {}))
                ]
            }
            subscription_df = pd.DataFrame(subscription_info)
            data_frames['Subscription'] = subscription_df

        # Protections Sheet
        if protections_data:
            protections_df = pd.DataFrame(protections_data)
            data_frames['Protections'] = protections_df

            # Protection by Type Summary
            type_counts = protections_df['Resource Type'].value_counts().reset_index()
            type_counts.columns = ['Resource Type', 'Count']
            data_frames['Protections by Type'] = type_counts
        else:
            data_frames['Protections'] = pd.DataFrame(columns=[
                'Protection ID', 'Protection Name', 'Resource ARN', 'Resource Type',
                'Health Check IDs', 'Protection ARN', 'Application Layer Automatic Response'
            ])

        # Attacks Sheet
        if attacks_data:
            attacks_df = pd.DataFrame(attacks_data)
            data_frames['Attacks'] = attacks_df

            # Attack Statistics
            attack_stats = {
                'Metric': [
                    'Total Attacks',
                    'Ongoing Attacks',
                    'Resolved Attacks',
                    'Most Targeted Resource Type'
                ],
                'Value': [
                    len(attacks_data),
                    len([a for a in attacks_data if a.get('Duration') == 'Ongoing']),
                    len([a for a in attacks_data if a.get('Duration') != 'Ongoing']),
                    attacks_df['Resource Type'].mode()[0] if not attacks_df.empty else 'N/A'
                ]
            }
            stats_df = pd.DataFrame(attack_stats)
            data_frames['Attack Statistics'] = stats_df
        else:
            data_frames['Attacks'] = pd.DataFrame(columns=[
                'Attack ID', 'Resource ARN', 'Resource Type', 'Start Time',
                'End Time', 'Duration', 'Attack Vectors', 'Mitigations'
            ])

        # Emergency Contacts Sheet
        if contacts_data:
            contacts_df = pd.DataFrame(contacts_data)
            data_frames['Emergency Contacts'] = contacts_df
        else:
            data_frames['Emergency Contacts'] = pd.DataFrame(columns=[
                'Email Address', 'Phone Number', 'Contact Notes'
            ])

        # Protection Groups Sheet
        if groups_data:
            groups_df = pd.DataFrame(groups_data)
            data_frames['Protection Groups'] = groups_df
        else:
            data_frames['Protection Groups'] = pd.DataFrame(columns=[
                'Protection Group ID', 'Aggregation', 'Pattern', 'Resource Type',
                'Members', 'Member Count', 'Protection Group ARN'
            ])

        # DRT Access Sheet
        drt_df = pd.DataFrame([drt_data])
        data_frames['DRT Access'] = drt_df

        # Save using utils function for multi-sheet Excel with preparation
        output_path = utils.save_multiple_dataframes_to_excel(data_frames, filename, prepare=True)

        if output_path:
            utils.log_success("AWS Shield Advanced data exported successfully!")
            utils.log_success(f"File location: {output_path}")

            # Log summary statistics
            total_protections = len(protections_data)
            total_attacks = len(attacks_data)
            utils.log_info(f"Export contains {total_protections} protection(s) and {total_attacks} attack(s)")

            return str(output_path)
        else:
            utils.log_error("Error exporting to Excel. Please check the logs.")
            return None

    except Exception as e:
        utils.log_error("Error exporting to Excel", e)
        return None


def main():
    """
    Main function to orchestrate the Shield Advanced information collection.

    Shield Advanced is a global, account-scope service (not multi-region),
    so failures are tracked per account-scope collector rather than via
    ``utils.scan_regions_concurrent`` (see scripts/iam_export.py for the
    account-scope reference pattern). Subscription-not-active is a
    legitimate, graceful "service not enabled" state (exit 0, no marker)
    and must not be confused with a real collection failure on the
    ``protections`` scope, which is exported as a partial result (if any
    data was collected) and always surfaced via
    ``utils.report_collection_failures`` + a non-zero exit — it must never
    be silently collapsed into "no protections" (07.15.2026 / 07.16.2026
    audits).
    """
    try:
        # Check dependencies first
        if not utils.ensure_dependencies('pandas', 'openpyxl', 'boto3'):
            return

        # Import pandas after dependency check

        # Setup logging
        utils.setup_logging("shield-export")

        # Print title and get account info
        account_id, account_name = utils.print_script_banner("AWS SHIELD ADVANCED INFORMATION COLLECTION EXPORT")

        # Validate AWS credentials
        try:
            sts = utils.get_boto3_client('sts')
            sts.get_caller_identity()
            utils.log_success("AWS credentials validated")
        except NoCredentialsError:
            utils.log_error("AWS credentials not found. Please configure your credentials using:")
            print("  - AWS CLI: aws configure")
            print("  - Environment variables: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
            print("  - IAM role (if running on EC2)")
            sys.exit(1)
        except Exception as e:
            utils.log_error("Error validating AWS credentials", e)
            sys.exit(1)

        utils.log_info("Checking AWS Shield Advanced subscription status...")
        print("====================================================================")

        # Check if Shield Advanced is subscribed
        subscription = check_subscription()

        if not subscription:
            print("\n====================================================================")
            print("AWS SHIELD ADVANCED NOT SUBSCRIBED")
            print("====================================================================")
            utils.log_warning("Shield Advanced is not subscribed in this account")
            print("\nAWS Shield Advanced is a premium DDoS protection service.")
            print("Pricing: ~$3,000/month subscription + data transfer fees")
            print("\nFeatures:")
            print("  - Advanced DDoS protection for web applications")
            print("  - 24/7 access to AWS DDoS Response Team (DRT)")
            print("  - DDoS cost protection")
            print("  - Real-time attack notifications and forensics")
            print("\nNOTE: Shield Standard is automatically included for all AWS customers")
            print("      at no additional cost. Shield Standard does not require configuration.")
            print("\nTo subscribe to Shield Advanced:")
            print("  1. Visit AWS Console > Shield > Subscribe to Shield Advanced")
            print("  2. Review pricing and terms")
            print("  3. Complete subscription process")
            print("\nExiting without export.")
            # Graceful skip — Shield Advanced not being subscribed is a
            # legitimate, expected state, NOT a collection failure. No
            # failure marker is written.
            sys.exit(0)

        utils.log_success("Shield Advanced subscription confirmed")
        utils.log_info("Collecting Shield Advanced information...")

        # Account-scope failure tracking (see scripts/iam_export.py).
        failed_scopes = []

        # STEP 1: Collect protections (PRIMARY scope — a real API error here
        # must propagate to failed_scopes, never collapse into an empty list
        # that reads as "no protections configured").
        utils.log_info("Collecting protected resources...")
        try:
            protections_data = collect_protections()
        except Exception as e:
            failed_scopes.append(('protections', str(e)))
            utils.log_error(f"Shield protections collection failed: {e}")
            protections_data = []

        # STEP 2+: Enrichment collectors — degrade gracefully to a safe
        # default via their own aws_error_handler decorators; a failure here
        # does not fail the whole export.
        utils.log_info("Collecting attack history (last 90 days)...")
        attacks_data = collect_attacks()

        utils.log_info("Collecting emergency contacts...")
        contacts_data = collect_emergency_contacts()

        utils.log_info("Collecting protection groups...")
        groups_data = collect_protection_groups()

        utils.log_info("Collecting DRT access configuration...")
        drt_data = collect_drt_access()

        print("\n====================================================================")
        print("COLLECTION COMPLETE")
        print("====================================================================")

        # Export whatever succeeded — a partial export is required even
        # when the protections scope failed (see
        # .collab/audit/07.16.2026-silent-collection-failure-blast-radius.md).
        filename = export_to_excel(
            subscription,
            protections_data,
            attacks_data,
            contacts_data,
            groups_data,
            drt_data,
            account_name
        )

        if filename:
            utils.log_info(f"Total protections: {len(protections_data)}")
            utils.log_info(f"Total attacks (last 90 days): {len(attacks_data)}")
            utils.log_info(f"Emergency contacts: {len(contacts_data)}")
            utils.log_info(f"Protection groups: {len(groups_data)}")
            utils.log_info(f"DRT access: {drt_data.get('Status', 'Unknown')}")
            print("\nScript execution completed.")
        elif not failed_scopes:
            # Genuinely empty: subscription active, protections scope
            # succeeded, and there is simply nothing configured.
            utils.log_warning("No Shield Advanced data was collected. Nothing to export.")
        else:
            utils.log_error("Export failed. Please check the logs.")

        # If the protections scope failed, make it loud: write a marker and
        # exit non-zero, even if a partial export (enrichment sheets) was
        # written. A partial export that looks complete is exactly the
        # failure mode this guards against.
        if failed_scopes:
            utils.report_collection_failures(account_name, 'shield-advanced', failed_scopes)
            print(
                "\nERROR: Shield Advanced export completed with failures — data is "
                "incomplete. See the *-shield-advanced-FAILED-*.txt marker in the "
                "output directory."
            )
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
        sys.exit(0)
    except Exception as e:
        utils.log_error("Unexpected error occurred", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
